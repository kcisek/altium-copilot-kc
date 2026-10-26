"""run_review_checklist (roadmap #46): checklist files and their validation, team
overrides, the mapping of tool results to Yes / No / N/A / needs judgment, the tool
runner, the design state, the review record and its comparison, and the MCP tool. Tool
results are stubs with made-up part and net names; the integration tests at the end run
the bundled checklists through the real tools on the synthetic netlist-free fixtures."""
import asyncio
import hashlib
import inspect
import json
import re
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

import server.main as main
import services.review_checklist as rc
from parsers.prj_pcb import VariantState
from services.clone_status import head_state
from tests.test_netlist_free_load import _load, env, offline_env  # noqa: F401
from tests.test_pcb_index import pcb_data  # noqa: F401

REPO = Path(__file__).resolve().parents[1]
BUNDLED = REPO / "server" / "checklists"
EXAMPLE = REPO / "examples" / "review_checklists.json"


# ---------- helpers ----------

def demo_checklist(**changes) -> dict:
    """A small checklist over two stub tools (check_bom and check_dfm)."""
    data = {
        "id": "demo", "title": "Demo review", "scope": "schematic", "version": "1",
        "sections": [{"id": "alpha", "title": "Alpha"}, {"id": "beta", "title": "Beta"}],
        "items": [
            {"id": "D-01", "section": "alpha", "kind": "auto", "text": "No errors.",
             "evidence": {"tool": "check_bom", "args": {},
                          "no": [{"path": "severity_counts.error", "op": ">", "value": 0,
                                  "label": "errors"}],
                          "anchors": [{"list": "findings", "where": {"severity": "error"}}]}},
            {"id": "D-02", "section": "alpha", "kind": "assisted", "text": "Warnings seen.",
             "evidence": {"tool": "check_bom", "args": {},
                          "no": [{"list": "findings", "where": {"severity": "warning"},
                                  "label": "warnings"}]}},
            {"id": "D-03", "section": "beta", "kind": "judgment", "text": "A person decides.",
             "hint": "Ask the owner."},
            {"id": "D-04", "section": "beta", "kind": "auto", "text": "Board is clean.",
             "evidence": {"tool": "check_dfm", "args": {},
                          "no": [{"list": "findings", "label": "board findings"}]}},
        ],
    }
    data.update(changes)
    return data


def validated(data: dict | None = None) -> dict:
    return rc.validate_checklist(data or demo_checklist(), "test", "bundled")


BOM_RESULT = {
    "severity_counts": {"error": 1, "warning": 1, "info": 0},
    "findings": [
        {"check": "duplicate_designator", "severity": "error", "issue": "R1 is used twice.",
         "designators": ["R1", "R1"]},
        {"check": "missing_mpn", "severity": "warning", "issue": "No part number on C2.",
         "designators": ["C2"], "nets": ["NET_A"]},
    ],
}


class Stubs:
    """Stub tools counting their calls; each returns JSON text like the MCP tools."""

    def __init__(self, bom=None, dfm=None):
        self.calls: list[tuple[str, int]] = []
        self.bom = BOM_RESULT if bom is None else bom
        self.dfm = dfm if dfm is not None else {
            "error": "no_pcb_document", "message": "The project lists no PcbDoc."}

    def check_bom(self, variant=None, checks=None, offset=0):
        self.calls.append(("check_bom", offset))
        return json.dumps(self.bom)

    def check_dfm(self, checks=None, profile=None, limits=None, offset=0):
        self.calls.append(("check_dfm", offset))
        return json.dumps(self.dfm)

    def tools(self) -> dict:
        return {"check_bom": self.check_bom, "check_dfm": self.check_dfm}


def run_demo(stubs: Stubs | None = None, checklist: dict | None = None, **kwargs) -> dict:
    stubs = stubs or Stubs()
    return rc.run_review_checklist({"name": "Demo", "prj_pcb_path": "Demo.PrjPcb"}, None,
                                   checklist or validated(), tools=stubs.tools(), **kwargs)


def row(model: dict, item_id: str) -> dict:
    return next(r for r in model["items"] if r["id"] == item_id)


def item_with(evidence: dict, kind: str = "auto") -> dict:
    return validated(demo_checklist(items=[{
        "id": "X-01", "section": "alpha", "kind": kind, "text": "Item.",
        "evidence": evidence}]))["items"][0]


def evaluate(evidence: dict, data: dict, kind: str = "auto", **result) -> dict:
    return rc.evaluate_item(item_with(evidence, kind), {"data": data, **result})


def write(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return path


def invalid(callable_, *args, **kwargs) -> dict:
    with pytest.raises(rc.ChecklistError) as caught:
        callable_(*args, **kwargs)
    return caught.value.error


# ---------- bundled checklists ----------

def test_bundled_checklists_load():
    checklists = rc.load_bundled(BUNDLED)
    assert set(checklists) == {"schematic", "pcb", "release", "bom"}
    sections = {cid: [s["id"] for s in c["sections"]] for cid, c in checklists.items()}
    assert sections["schematic"] == ["power", "signals", "components", "connectors",
                                     "testability", "style"]
    assert sections["pcb"] == ["placement", "power_thermal", "high_speed", "manufacturing",
                               "testability"]
    for checklist in checklists.values():
        assert checklist["source"] and "resources.altium.com" in checklist["source"]
        kinds = {item["kind"] for item in checklist["items"]}
        assert kinds <= set(rc.KINDS) and "judgment" in kinds
        for item in checklist["items"]:
            assert item["scope"] == checklist["scope"]
            assert item["section"] in sections[checklist["id"]]


def test_bundled_items_name_registered_read_only_tools_with_valid_arguments():
    registered = {tool.name: tool for tool in asyncio.run(main.mcp.list_tools())}
    for name in rc.EVIDENCE_TOOLS:
        assert name in registered, name
        assert registered[name].annotations.readOnlyHint is True, name
    for checklist in rc.load_bundled(BUNDLED).values():
        for item in checklist["items"]:
            if item["kind"] == "judgment":
                continue
            evidence = item["evidence"]
            inspect.signature(getattr(main, evidence["tool"])).bind(**evidence["args"])
            for group in rc.GROUPS:
                for cond in evidence.get(group, []):
                    assert cond.get("label"), (item["id"], group)


def test_bundled_item_ids_are_unique_and_texts_short():
    checklists = rc.load_bundled(BUNDLED)
    ids = [item["id"] for c in checklists.values() for item in c["items"]]
    assert len(ids) == len(set(ids))
    for checklist in checklists.values():
        texts = [item["text"] for item in checklist["items"]]
        assert len(texts) == len(set(texts)), checklist["id"]
        assert all(len(text) <= rc.TEXT_LIMITS["text"] for text in texts)


def test_packaging_adds_the_checklists_to_the_binary():
    release = (REPO / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    pack = (REPO / ".github" / "pack.ps1").read_text(encoding="utf-8")
    assert '--add-data "server/checklists;checklists"' in release
    assert '--add-data "$root\\server\\checklists;checklists"' in pack
    assert rc.bundled_dir() == BUNDLED


def test_frozen_bundle_reads_checklists_next_to_meipass(tmp_path, monkeypatch):
    shutil.copytree(BUNDLED, tmp_path / "checklists")
    monkeypatch.setattr(rc.sys, "frozen", True, raising=False)
    monkeypatch.setattr(rc.sys, "_MEIPASS", str(tmp_path), raising=False)
    assert rc.bundled_dir() == tmp_path / "checklists"
    assert set(rc.load_bundled()) == {"schematic", "pcb", "release", "bom"}


def test_missing_bundled_folder_is_an_error(tmp_path):
    assert invalid(rc.load_bundled, tmp_path)["error"] == "checklists_missing"


# ---------- validation ----------

@pytest.mark.parametrize("change, where, words", [
    ({"id": "Bad Id"}, "id", "not valid"),
    ({"scope": "firmware"}, "scope", "not a scope"),
    ({"title": ""}, "title", "non-empty"),
    ({"sections": []}, "sections", "1 to"),
    ({"extra": 1}, "", "unknown key 'extra'"),
])
def test_checklist_level_errors(change, where, words):
    error = invalid(rc.validate_checklist, demo_checklist(**change), "f.json", "f.json")
    assert error["error"] == "invalid_checklist"
    assert error["file"] == "f.json" and error["where"] == where
    assert words in error["message"]


def _with_item(**fields) -> dict:
    item = {"id": "X-01", "section": "alpha", "kind": "auto", "text": "Item.",
            "evidence": {"tool": "check_bom", "args": {}}}
    item.update(fields)
    return demo_checklist(items=[{k: v for k, v in item.items() if v is not None}])


@pytest.mark.parametrize("fields, words", [
    ({"section": "gamma"}, "not a section"),
    ({"kind": "maybe"}, "not a kind"),
    ({"kind": "judgment"}, "has no evidence"),
    ({"evidence": None}, "needs 'evidence'"),
    ({"evidence": {"tool": "set_project_dir"}}, "not a tool a checklist can call"),
    ({"evidence": {"tool": "run_erc", "args": {"offset": 5}}}, "set by the checklist runner"),
    ({"evidence": {"tool": "run_erc", "no": [{"path": "a", "op": "~"}]}}, "not an operator"),
    ({"evidence": {"tool": "run_erc", "no": [{"path": "a", "op": ">"}]}}, "needs a 'value'"),
    ({"evidence": {"tool": "run_erc", "no": [{"path": "a", "op": "exists", "value": 1}]}},
     "takes no 'value'"),
    ({"evidence": {"tool": "run_erc", "no": [{"path": "a", "op": ">", "value": "x"}]}},
     "needs a number"),
    ({"evidence": {"tool": "run_erc", "no": [{"path": "a", "list": "b"}]}}, "exactly one"),
    ({"evidence": {"tool": "run_erc", "no": [{"path": "a", "where": {"x": 1}}]}},
     "'where' goes with 'list'"),
    ({"evidence": {"tool": "run_erc", "anchors": [{"path": "a"}]}}, "anchor selector"),
    ({"evidence": {"tool": "run_erc", "na_errors": "no_pcb_document"}}, "error codes"),
    ({"text": "x" * 301}, "longer than 300"),
    ({"id": "bad id!"}, "not valid"),
])
def test_item_errors(fields, words):
    error = invalid(rc.validate_checklist, _with_item(**fields), "f.json", "f.json")
    assert words in error["message"], error["message"]
    assert error["where"].startswith("items[0]")


def test_duplicate_item_and_section_ids():
    data = demo_checklist()
    data["items"].append(dict(data["items"][2]))
    assert "used twice" in invalid(rc.validate_checklist, data, "f", "f")["message"]
    data = demo_checklist(sections=[{"id": "alpha", "title": "A"}, {"id": "alpha", "title": "B"}])
    assert "used twice" in invalid(rc.validate_checklist, data, "f", "f")["message"]


def test_item_ids_ignore_case_everywhere(tmp_path):
    data = demo_checklist()
    data["items"].append({**data["items"][2], "id": "d-03"})
    assert "used twice" in invalid(rc.validate_checklist, data, "f", "f")["message"]
    write(tmp_path / rc.OVERRIDE_FILE, {"add": [{"checklist": "demo", "id": "d-01",
                                                 "section": "alpha", "kind": "judgment",
                                                 "text": "Same id, other case."}]})
    error = invalid(rc.load_checklists, tmp_path, bundled=bundled_demo())
    assert "already in the 'demo' checklist" in error["message"]
    write(tmp_path / rc.OVERRIDE_FILE, {"disable": ["d-02"]})
    checklists, info = rc.load_checklists(tmp_path, bundled=bundled_demo())
    assert [i["id"] for i in checklists["demo"]["items"]] == ["D-01", "D-03", "D-04"]
    assert checklists["demo"]["disabled"] == [{"id": "D-02", "file": info["files"][0]}]
    other = {**demo_checklist(id="other"), "items": [{**demo_checklist()["items"][2],
                                                      "id": "D-03"}]}
    write(tmp_path / rc.OVERRIDE_FILE, {"checklists": [{**other, "items": [
        {**other["items"][0], "id": "d-03"}]}]})
    error = invalid(rc.load_checklists, tmp_path, bundled=bundled_demo())
    assert "unique across checklists" in error["message"]


def test_nested_conditions_validate_and_depth_is_limited():
    deep = {"path": "a"}
    for _ in range(rc.MAX_DEPTH + 1):
        deep = {"all": [deep]}
    error = invalid(rc.validate_checklist,
                    _with_item(evidence={"tool": "run_erc", "no": [deep]}), "f", "f")
    assert "nested too deeply" in error["message"]
    ok = rc.validate_condition({"any": [{"path": "a"}, {"list": "b", "where": {
        "x": [1, 2], "y": {"op": ">", "value": 3}, "z": {"op": "missing"}}}]}, "f", "w")
    assert ok["any"][1]["where"] == {"x": {"op": "in", "value": [1, 2]},
                                     "y": {"op": ">", "value": 3}, "z": {"op": "missing"}}


def test_read_json_reports_line_and_column_and_duplicates(tmp_path):
    bad = write(tmp_path / "a.json", '{\n  "name": "x",\n  "disable": [1,\n}')
    error = invalid(rc.read_json, bad, "a.json")
    assert error["line"] == 4 and "not valid JSON at line 4" in error["message"]
    dup = write(tmp_path / "b.json", '{"name": "x", "name": "y"}')
    assert "duplicate key 'name'" in invalid(rc.read_json, dup, "b.json")["message"]
    nan = write(tmp_path / "c.json", '{"name": NaN}')
    assert "NaN is not allowed" in invalid(rc.read_json, nan, "c.json")["message"]
    array = write(tmp_path / "d.json", "[]")
    assert "one JSON object" in invalid(rc.read_json, array, "d.json")["message"]
    big = write(tmp_path / "e.json", '{"x": "' + "a" * rc.MAX_OVERRIDE_BYTES + '"}')
    assert "larger than" in invalid(rc.read_json, big, "e.json")["message"]
    latin = tmp_path / "f.json"
    latin.write_bytes(b'{"name": "\xe9"}')
    assert "not UTF-8" in invalid(rc.read_json, latin, "f.json")["message"]


# ---------- overrides ----------

def bundled_demo() -> dict:
    return {"demo": validated()}


def test_override_adds_sections_and_items_and_disables(tmp_path):
    write(tmp_path / rc.OVERRIDE_FILE, {
        "name": "Team rules",
        "disable": ["D-03"],
        "add_sections": [{"checklist": "demo", "id": "team", "title": "Team"}],
        "add": [{"checklist": "demo", "id": "T-01", "section": "team", "kind": "judgment",
                 "text": "Team rule."}],
    })
    checklists, info = rc.load_checklists(tmp_path, bundled=bundled_demo())
    demo = checklists["demo"]
    assert [i["id"] for i in demo["items"]] == ["D-01", "D-02", "D-04", "T-01"]
    assert demo["sections"][-1] == {"id": "team", "title": "Team"}
    assert demo["disabled"] == [{"id": "D-03", "file": "review_checklists.json (project folder)"}]
    added = next(i for i in demo["items"] if i["id"] == "T-01")
    assert added["origin"] == "review_checklists.json (project folder)"
    assert info["applied"] == [{"file": "review_checklists.json (project folder)",
                                "name": "Team rules", "added_sections": ["demo.team"],
                                "added": ["T-01"], "disabled": ["D-03"]}]
    # The bundled set itself is untouched.
    assert len(bundled_demo()["demo"]["items"]) == 4


def test_override_replaces_and_adds_whole_checklists(tmp_path):
    replacement = demo_checklist(title="Our demo", items=[
        {"id": "N-01", "section": "alpha", "kind": "judgment", "text": "Only this."}])
    other = demo_checklist(id="other", items=[
        {"id": "O-01", "section": "beta", "kind": "judgment", "text": "Other."}])
    write(tmp_path / rc.OVERRIDE_FOLDER / "a.json", {"checklists": [replacement, other]})
    checklists, info = rc.load_checklists(tmp_path, bundled=bundled_demo())
    assert checklists["demo"]["title"] == "Our demo"
    assert [i["id"] for i in checklists["demo"]["items"]] == ["N-01"]
    assert set(checklists) == {"demo", "other"}
    assert info["applied"][0]["replaced"] == ["demo"]
    assert info["applied"][0]["added_checklists"] == ["other"]
    assert info["files"] == ["review_checklists/a.json (project folder)"]


def test_override_files_order_repository_root_first(tmp_path):
    repo = tmp_path / "repo"
    project = repo / "boards" / "main"
    project.mkdir(parents=True)
    (repo / ".git").mkdir()
    write(repo / rc.OVERRIDE_FILE, {"add": [{"checklist": "demo", "id": "R-01",
                                              "section": "alpha", "kind": "judgment",
                                              "text": "From the repository."}]})
    write(project / rc.OVERRIDE_FOLDER / "b.json", {"disable": ["R-01"]})
    write(project / rc.OVERRIDE_FOLDER / "a.json", {"disable": ["D-01"]})
    write(project / rc.OVERRIDE_FILE, {"disable": ["D-02"]})
    labels = [label for _, _, label in rc.override_files(project)]
    assert labels == ["review_checklists.json (repository root)",
                      "review_checklists.json (project folder)",
                      "review_checklists/a.json (project folder)",
                      "review_checklists/b.json (project folder)"]
    checklists, _ = rc.load_checklists(project, bundled=bundled_demo())
    assert [i["id"] for i in checklists["demo"]["items"]] == ["D-03", "D-04"]
    assert rc.repository_root(project) == repo


@pytest.mark.parametrize("override, words", [
    ({"disable": ["NOPE-1"]}, "no item has the id 'NOPE-1'"),
    ({"disable": "D-01"}, "list of item ids"),
    ({"add": [{"checklist": "nope", "id": "A", "section": "alpha", "kind": "judgment",
               "text": "x"}]}, "'nope' is not a checklist"),
    ({"add": [{"checklist": "demo", "id": "D-01", "section": "alpha", "kind": "judgment",
               "text": "x"}]}, "already in the 'demo' checklist"),
    ({"add": [{"checklist": "demo", "id": "A-1", "section": "zeta", "kind": "judgment",
               "text": "x"}]}, "not a section"),
    ({"add_sections": [{"checklist": "demo", "id": "alpha", "title": "Again"}]}, "used twice"),
    ({"checklists": [demo_checklist(id="other")]}, "in both the 'demo' and the 'other'"),
    ({"surprise": 1}, "unknown key 'surprise'"),
])
def test_override_errors_name_the_file(tmp_path, override, words):
    write(tmp_path / rc.OVERRIDE_FILE, override)
    error = invalid(rc.load_checklists, tmp_path, bundled=bundled_demo())
    assert error["error"] == "invalid_checklist"
    assert error["file"] == "review_checklists.json (project folder)"
    assert words in error["message"], error["message"]
    assert "overrides=false" in error["hint"]


def test_override_arguments_must_fit_the_tool(tmp_path):
    stubs = Stubs()
    item = {"checklist": "demo", "id": "T-01", "section": "alpha", "kind": "auto",
            "text": "Board is clean.", "evidence": {"tool": "check_dfm",
                                                     "args": {"profile": "x", "bogus": 1}}}
    write(tmp_path / rc.OVERRIDE_FILE, {"add": [item]})
    error = invalid(rc.load_checklists, tmp_path, bundled=bundled_demo(),
                    tools=stubs.tools())
    assert error["error"] == "invalid_checklist" and error["where"] == "add[0] (T-01).evidence.args"
    assert "check_dfm does not take these arguments" in error["message"]
    assert error["valid_arguments"] == ["checks", "profile", "limits"]
    whole = {**demo_checklist(id="other"), "items": [{**item, "id": "T-02"}]}
    whole["items"][0].pop("checklist")
    write(tmp_path / rc.OVERRIDE_FILE, {"checklists": [whole]})
    error = invalid(rc.load_checklists, tmp_path, bundled=bundled_demo(),
                    tools=stubs.tools())
    assert error["where"] == "checklists[0].items[0] (T-02).evidence.args"
    # A value is checked when the tool runs; a tool the runner lacks is not checked here.
    item["evidence"]["args"] = {"profile": "no-such-fab"}
    write(tmp_path / rc.OVERRIDE_FILE, {"add": [item, {**item, "id": "T-03", "evidence": {
        "tool": "run_erc", "args": {"anything": 1}}}]})
    checklists, _ = rc.load_checklists(tmp_path, bundled=bundled_demo(), tools=stubs.tools())
    assert [i["id"] for i in checklists["demo"]["items"]][-2:] == ["T-01", "T-03"]


def test_override_checklist_errors_name_their_place(tmp_path):
    bad = demo_checklist(id="other", scope="nope")
    write(tmp_path / rc.OVERRIDE_FILE, {"checklists": [bad]})
    error = invalid(rc.load_checklists, tmp_path, bundled=bundled_demo())
    assert error["where"] == "checklists[0].scope"


def test_overrides_false_ignores_the_files(tmp_path):
    write(tmp_path / rc.OVERRIDE_FILE, "not json")
    checklists, info = rc.load_checklists(tmp_path, overrides=False, bundled=bundled_demo())
    assert info == {"files": [], "applied": [], "used": False}
    assert len(checklists["demo"]["items"]) == 4


def test_example_override_file_applies_to_the_bundled_checklists(tmp_path):
    shutil.copy(EXAMPLE, tmp_path / rc.OVERRIDE_FILE)
    checklists, info = rc.load_checklists(tmp_path, bundled=rc.load_bundled(BUNDLED))
    assert "prototype" in checklists
    assert any(i["id"] == "TEAM-SCH-01" for i in checklists["schematic"]["items"])
    assert any(i["id"] == "TEAM-BOM-01" for i in checklists["bom"]["items"])
    assert not any(i["id"] == "SCH-STY-05" for i in checklists["schematic"]["items"])
    assert info["applied"][0]["name"] == "Example team review rules"


def test_catalog_and_selection():
    checklists = {"demo": validated()}
    catalog = rc.catalog(checklists, {"files": []})
    entry = catalog["checklists"][0]
    assert entry["items"] == 4 and entry["by_kind"] == {"auto": 2, "assisted": 1, "judgment": 1}
    assert entry["sections"] == [{"id": "alpha", "title": "Alpha", "items": 2},
                                 {"id": "beta", "title": "Beta", "items": 2}]
    demo = checklists["demo"]
    assert [i["id"] for i in rc.select(demo, ["Beta"], None)] == ["D-03", "D-04"]
    assert [i["id"] for i in rc.select(demo, None, ["d-02", "D-04"])] == ["D-02", "D-04"]
    assert invalid(rc.select, demo, ["gamma"], None)["error"] == "unknown_section"
    error = invalid(rc.select, demo, None, ["D-99"])
    assert error["error"] == "unknown_item" and "D-01" in error["items"]


# ---------- status mapping ----------

def test_auto_no_with_anchors_from_the_selector_and_finding_ids():
    model = run_demo()
    first = row(model, "D-01")
    assert first["status"] == "no"
    assert first["evidence"] == "errors: 1"
    assert first["anchors"] == {"designators": ["R1"]}
    assert first["findings"] == {"duplicate_designator": 1}
    assert first["tool"] == "check_bom" and "args" not in first


def test_assisted_leans_and_never_decides():
    model = run_demo()
    second = row(model, "D-02")
    assert second["status"] == "needs_judgment" and second["lean"] == "no"
    assert second["evidence"] == "warnings — No part number on C2."
    assert second["anchors"] == {"designators": ["C2"], "nets": ["NET_A"]}
    clean = run_demo(Stubs(bom={"severity_counts": {"error": 0}, "findings": []}))
    assert row(clean, "D-02")["lean"] == "yes"
    assert row(clean, "D-01")["status"] == "yes"
    assert row(clean, "D-01")["evidence"] == "errors: 0"


def test_judgment_items_and_tool_error_codes():
    model = run_demo()
    assert row(model, "D-03")["status"] == "needs_judgment"
    assert row(model, "D-03")["hint"] == "Ask the owner."
    board = row(model, "D-04")
    assert board["status"] == "n/a"
    assert board["evidence"] == "Not applicable: The project lists no PcbDoc."
    other = run_demo(Stubs(dfm={"error": "netlist_required", "message": "No netlist."}))
    assert row(other, "D-04")["status"] == "needs_judgment"
    assert row(other, "D-04")["could_not_run"] == "netlist_required"
    assert other["could_not_run"] == [{"tool": "check_dfm", "error": "netlist_required",
                                       "items": ["D-04"]}]


def test_groups_in_order_and_yes_conditions():
    data = {"summary": {"a": 1, "b": 0}, "rows": [{"id": "g1", "status": "pass",
                                                    "evidence": "Gate one passed fine."}]}
    na = {"path": "summary.a", "op": "==", "value": 1, "label": "nothing to check"}
    no = {"path": "summary.a", "op": ">", "value": 0, "label": "a count"}
    judge = {"path": "summary.b", "op": "==", "value": 0, "label": "b unknown"}
    assert evaluate({"tool": "run_erc", "na": [na], "no": [no]}, data)["status"] == "n/a"
    assert evaluate({"tool": "run_erc", "no": [no], "judge": [judge]}, data)["status"] == "no"
    assert evaluate({"tool": "run_erc", "judge": [judge]}, data)["status"] == "needs_judgment"
    passed = evaluate({"tool": "run_erc", "yes": [{"list": "rows", "where": {
        "status": "pass"}, "label": "gate passed"}]}, data)
    assert passed["status"] == "yes"
    assert passed["evidence"] == "gate passed — Gate one passed fine."
    unmet = evaluate({"tool": "run_erc", "yes": [{"list": "rows", "where": {
        "status": "fail"}, "label": "gate failed"}]}, data)
    assert unmet["status"] == "needs_judgment"
    assert unmet["evidence"] == "not met: gate failed"
    nothing = evaluate({"tool": "run_erc"}, data)
    assert nothing["status"] == "yes"
    assert nothing["evidence"] == "The item names no No or needs-judgment condition."


def test_condition_operators_and_paths():
    data = {"top": {"inner": {"n": 3, "flag": True, "text": "x", "empty": []}},
            "rows": [{"check": "a", "severity": "warning", "nested": {"count": 2}},
                     {"check": "b", "severity": "info", "nested": {"count": 0}},
                     "not a dict"]}

    def hit(cond):
        return rc.evaluate(rc.validate_condition(cond, "t", "w"), data)["hit"]

    assert hit({"path": "top.inner.n", "op": ">=", "value": 3})
    assert not hit({"path": "top.inner.n", "op": "<", "value": 3})
    assert hit({"path": "top.inner.flag"})
    assert hit({"path": "top.inner.empty", "op": "empty"})
    assert hit({"path": "top.inner.text", "op": "in", "value": ["x", "y"]})
    assert hit({"path": "top.inner.text", "op": "not_in", "value": ["y"]})
    assert hit({"path": "top.missing", "op": "missing"})
    assert not hit({"path": "top.missing", "op": "==", "value": None})
    assert not hit({"path": "top.inner.text", "op": ">", "value": 1})
    assert hit({"list": "rows", "where": {"nested.count": {"op": ">", "value": 1}}})
    assert hit({"list": "rows", "where": {"check": ["a", "b"]}, "op": "==", "value": 2})
    assert hit({"list": "rows", "where": {"nested.count": {"op": "exists"}}, "op": "==",
                "value": 2})
    assert hit({"all": [{"path": "top.inner.flag"}, {"list": "rows"}]})
    assert not hit({"all": [{"path": "top.inner.flag"}, {"path": "top.missing"}]})
    assert hit({"any": [{"path": "top.missing"}, {"list": "rows"}]})
    assert not hit({"list": "top", "op": ">", "value": 0})  # not a list: no rows


def test_a_path_missing_from_the_result_is_not_a_yes():
    misspelt = {"tool": "run_erc", "no": [{"path": "summary.by_levl.Error", "op": ">",
                                           "value": 0, "label": "ERC errors"}]}
    data = {"summary": {"by_level": {"Error": 3}}, "violations": [{"level": "Error"}]}
    row_ = evaluate(misspelt, data)
    assert row_["status"] == "needs_judgment" and row_["unresolved"] == ["summary.by_levl.Error"]
    assert row_["evidence"].startswith("Could not check: summary.by_levl.Error not in the "
                                       "run_erc result")
    no_list = evaluate({"tool": "run_erc", "no": [{"list": "violatons", "label": "rows"}]}, data)
    assert no_list["status"] == "needs_judgment" and no_list["unresolved"] == ["violatons"]
    assisted = evaluate(misspelt, data, kind="assisted")
    assert assisted["status"] == "needs_judgment" and "lean" not in assisted
    # A hit decides even when another condition's path is missing.
    hit = evaluate({"tool": "run_erc", "no": [{"list": "violations", "label": "rows"},
                                              {"path": "nope", "op": ">", "value": 0}]}, data)
    assert hit["status"] == "no" and "unresolved" not in hit
    # Presence tests and optional flags may be absent.
    for op in ("exists", "missing", "truthy", "falsy"):
        absent = evaluate({"tool": "run_erc", "judge": [{"path": "flag", "op": op}]},
                          {"other": 1})
        assert "unresolved" not in absent, op
    assert evaluate({"tool": "run_erc", "judge": [{"path": "flag", "op": "missing"}]},
                    {})["status"] == "needs_judgment"


def test_a_yes_says_what_was_checked():
    data = {"stale": [], "summary": {"errors": 0}}
    listed = evaluate({"tool": "run_erc", "no": [{"list": "stale", "label": "stale entries"}],
                       "judge": [{"path": "summary.errors", "op": ">", "value": 0,
                                  "label": "errors"}]}, data)
    assert listed["status"] == "yes" and listed["evidence"] == "stale entries: 0; errors: 0"
    paths_only = evaluate({"tool": "run_erc", "no": [
        {"path": "stale", "op": "nonempty", "label": "stale entries"},
        {"path": "summary.errors", "op": "!=", "value": 0, "label": "any error"}]}, data)
    assert paths_only["status"] == "yes"
    assert paths_only["evidence"] == "None of these reported: stale entries; any error"


def test_where_compares_two_fields_of_a_row_and_absent_fields_are_falsy():
    data = {"rules": [{"name": "R_A", "summary": {"violations": 3, "marginal": 3}},
                      {"name": "R_B", "summary": {"violations": 4, "marginal": 1}}],
            "inputs": [{"source": "J1", "exposure": "external"},
                       {"source": "J2", "exposure": "external", "fuse": ["F1"]},
                       {"source": "J3", "exposure": "external", "fuse": []}]}

    def count(cond):
        return rc.evaluate(rc.validate_condition(cond, "t", "w"), data)["count"]

    assert count({"list": "rules", "where": {"summary.violations": {
        "op": ">", "field": "summary.marginal"}}}) == 1
    assert count({"list": "rules", "where": {"summary.violations": {
        "op": "==", "field": "summary.marginal"}}}) == 1
    assert count({"list": "rules", "where": {"summary.violations": {
        "op": ">", "field": "summary.nothing"}}}) == 0
    assert count({"list": "inputs", "where": {"fuse": {"op": "falsy"}}}) == 2
    assert count({"list": "inputs", "where": {"fuse": {"op": "truthy"}}}) == 1
    assert rc.evaluate(rc.validate_condition({"path": "absent", "op": "falsy"}, "t", "w"),
                       data)["hit"] is False  # a top-level path is not a row field


@pytest.mark.parametrize("spec, words", [
    ({"op": ">", "field": "b", "value": 1}, "instead of 'value'"),
    ({"op": "in", "field": "b"}, "instead of 'value'"),
    ({"op": ">", "field": ""}, "dotted path in the row"),
    ({"op": "exists", "field": "b"}, "takes no 'value' or 'field'"),
    ({"op": ">"}, "needs a 'value' or a 'field'"),
])
def test_where_field_errors(spec, words):
    error = invalid(rc.validate_condition, {"list": "rows", "where": {"a": spec}}, "t", "w")
    assert words in error["message"]


def test_assisted_na_and_truncated_results():
    evidence = {"tool": "run_erc", "na": [{"path": "n", "op": "==", "value": 0,
                                           "label": "nothing found"}]}
    assert evaluate(evidence, {"n": 0}, kind="assisted")["status"] == "n/a"
    truncated = evaluate({"tool": "run_erc"}, {"n": 1}, truncated=True)
    assert truncated["status"] == "needs_judgment"
    assert "more pages than were read" in truncated["evidence"]


def test_finding_ids_and_anchor_extraction():
    rows = [
        {"check": "skew", "issue": "in_pair_rule_stale", "pair": "P_A"},
        {"check": "files", "code": "output_not_found"},
        {"violation": "Duplicate Part Designators", "sheet": "Power", "count": 3},
        {"id": "drc", "status": "fail"},
        {"kind": "io_mismatch", "sheet": "Top",
         "examples": [{"designator": "U5", "net": "NET_B"}], "names": ["LIB_X"]},
    ]
    assert rc.finding_ids(rows) == {"skew/in_pair_rule_stale": 1, "files/output_not_found": 1,
                                    "Duplicate Part Designators": 1, "drc": 1, "io_mismatch": 1}
    anchors = rc.anchors_of(rows, "check_diff_pairs")
    assert anchors == {"designators": ["U5"], "nets": ["P_A", "NET_B"],
                       "sheets": ["Power", "Top"], "other": ["LIB_X"]}
    many = rc.anchors_of([{"designators": [f"R{i}" for i in range(15)]}])
    assert many["designators"] == [f"R{i}" for i in range(10)] and many["designators_total"] == 15


def test_anchor_keys_that_name_nets_or_parts_in_some_tools_only():
    # run_erc's 'pair' is two pin types and get_project_options' a Connection Matrix cell.
    erc = [{"violation": "Nets with no driving source", "pair": ["Input Pin", "IO Pin"],
            "nets": ["NET_A"]}]
    assert rc.anchors_of(erc, "run_erc") == {"nets": ["NET_A"]}
    assert rc.anchors_of([{"pair": "Input Pin x Unconnected"}], "get_project_options") == {}
    assert rc.anchors_of([{"pair": "PAIR_A"}], "check_vias") == {"nets": ["PAIR_A"]}
    assert rc.anchors_of([{"bus": "i2c", "mcu": "U1"}], "get_firmware_pin_map") == {
        "designators": ["U1"]}
    assert rc.anchors_of([{"bus": "SDA / SCL"}], "check_pull_resistors") == {
        "nets": ["SDA / SCL"]}
    power_input = [{"source": "J1", "net": "VIN_RAW", "exposure": "external"}]
    assert rc.anchors_of(power_input, "get_power_tree") == {"designators": ["J1"],
                                                            "nets": ["VIN_RAW"]}
    assert rc.anchors_of([{"source": "fab_profile.json"}], "check_dfm") == {}


def test_coverage_counts_by_section():
    model = run_demo()
    assert model["coverage"] == [
        {"section": "alpha", "title": "Alpha", "yes": 0, "no": 1, "n/a": 0,
         "needs_judgment": 1, "total": 2},
        {"section": "beta", "title": "Beta", "yes": 0, "no": 0, "n/a": 1,
         "needs_judgment": 1, "total": 2},
        {"section": "all", "title": "All", "yes": 0, "no": 1, "n/a": 1,
         "needs_judgment": 2, "total": 4},
    ]
    narrowed = run_demo(sections=["beta"])
    assert [s["id"] for s in narrowed["sections"]] == ["beta"]
    assert narrowed["selection"] == {"sections": ["beta"], "items": None}


# ---------- the tool runner ----------

def test_runner_caches_per_tool_and_arguments():
    stubs = Stubs()
    model = run_demo(stubs)
    assert stubs.calls == [("check_bom", 0), ("check_dfm", 0)]
    assert [(r["tool"], r["pages"]) for r in model["tool_runs"]] == [
        ("check_bom", 1), ("check_dfm", 1)]
    assert model["tool_runs"][1]["error"] == "no_pcb_document"


def paged_tool(pages: list[dict], calls: list):
    def tool(checks=None, offset=0):
        calls.append(offset)
        index = offset // 10
        return json.dumps(pages[index])
    return tool


def test_runner_reads_pages_while_needed_lists_grow():
    pages = [
        {"summary": {"n": 3}, "findings": [{"check": "a"}], "notes": ["same"], "next_offset": 10},
        {"summary": {"n": 3}, "findings": [{"check": "b"}], "notes": ["same"], "next_offset": 20},
        {"summary": {"n": 3}, "findings": [{"check": "c"}], "notes": ["same"]},
    ]
    calls: list = []
    runner = rc.ToolRunner({"check_bom": paged_tool(pages, calls)},
                           needs={rc.ToolRunner.key("check_bom", {}): {"findings"}})
    result = runner.run("check_bom", {})
    assert calls == [0, 10, 20] and result["pages"] == 3
    assert [f["check"] for f in result["data"]["findings"]] == ["a", "b", "c"]
    assert result["data"]["notes"] == ["same"]


def test_runner_stops_when_needed_lists_repeat_or_are_not_paged():
    pages = [{"findings": [{"check": "a"}], "rows": [1], "next_offset": 10},
             {"findings": [{"check": "a"}], "rows": [2], "next_offset": 20},
             {"findings": [{"check": "a"}], "rows": [3]}]
    calls: list = []
    runner = rc.ToolRunner({"check_bom": paged_tool(pages, calls)},
                           needs={rc.ToolRunner.key("check_bom", {}): {"findings"}})
    assert runner.run("check_bom", {})["pages"] == 2 and calls == [0, 10]
    calls.clear()
    summary_only = rc.ToolRunner({"check_bom": paged_tool(pages, calls)},
                                 needs={rc.ToolRunner.key("check_bom", {}): set()})
    assert summary_only.run("check_bom", {})["pages"] == 1 and calls == [0]
    calls.clear()
    every = rc.ToolRunner({"check_bom": paged_tool(pages, calls)})
    assert every.run("check_bom", {})["data"]["rows"] == [1, 2, 3]


def test_runner_marks_results_cut_at_max_pages():
    pages = [{"findings": [{"n": i}], "next_offset": (i + 1) * 10} for i in range(10)]
    runner = rc.ToolRunner({"check_bom": paged_tool(pages, [])}, max_pages=3)
    result = runner.run("check_bom", {})
    assert result["truncated"] and result["pages"] == 3
    assert runner.runs[0]["truncated"] is True


def test_runner_errors():
    def broken(offset=0):
        raise RuntimeError("boom")

    def text(offset=0):
        return "not json"

    runner = rc.ToolRunner({"check_bom": broken, "check_dfm": text})
    failed = runner.run("check_bom", {})
    assert failed["error"] == "tool_failed" and failed["exception"] == "RuntimeError"
    assert runner.run("check_dfm", {})["error"] == "not_json"
    assert runner.run("run_erc", {})["error"] == "tool_not_available"
    assert runner.run("check_bom", {"nope": 1})["error"] == "invalid_arguments"
    item = item_with({"tool": "check_bom"})
    assert rc.evaluate_item(item, failed)["could_not_run"] == "RuntimeError"


def test_runner_calls_tools_without_offset_once():
    calls = []

    def status(project_dir=None):
        calls.append(project_dir)
        return json.dumps({"altium365": True, "next_offset": 5})

    assert rc.ToolRunner({"get_clone_status": status}).run("get_clone_status", {})["pages"] == 1
    assert calls == [None]


# ---------- decisions ----------

def test_decisions_parse_and_apply():
    model = run_demo()
    decisions = rc.parse_decisions({"d-03": "Yes: owner agreed", "D-01": "n/a",
                                    "D-02": "no — the warning is real"}, model["items"])
    assert decisions == {"D-03": {"status": "yes", "note": "owner agreed"},
                         "D-01": {"status": "n/a", "note": ""},
                         "D-02": {"status": "no", "note": "the warning is real"}}
    decided = rc.apply_decisions(model, decisions)
    third = row(decided, "D-03")
    assert third["status"] == "yes" and third["tool_status"] == "needs_judgment"
    assert third["decided_in_review"] == "owner agreed"
    assert row(decided, "D-01")["decided_in_review"] == "decided in review"
    assert decided["coverage"][-1] == {"section": "all", "title": "All", "yes": 1, "no": 1,
                                       "n/a": 2, "needs_judgment": 0, "total": 4}
    assert decided["decisions_applied"] == 3
    assert row(model, "D-03")["status"] == "needs_judgment"  # the model is not changed


@pytest.mark.parametrize("decisions, words", [
    ({"D-99": "yes"}, "not an item of this run"),
    ({"D-01": "perhaps"}, "must start with"),
    ({"D-01": 1}, "must start with"),
    (["D-01"], "is an object"),
])
def test_decision_errors(decisions, words):
    error = invalid(rc.parse_decisions, decisions, run_demo()["items"])
    assert error["error"] == "invalid_decision" and words in error["message"]


# ---------- design state ----------

def make_project(root: Path) -> dict:
    (root / "src").mkdir(parents=True)
    (root / "Demo.PrjPcb").write_text("[Design]\n", encoding="utf-8")
    (root / "src" / "Main.SchDoc").write_bytes(b"sheet one")
    (root / "Demo.PcbDoc").write_bytes(b"board")
    return {"name": "Demo", "root_dir": str(root), "prj_pcb_path": str(root / "Demo.PrjPcb"),
            "sheets": [{"name": "Main", "path": str(root / "src" / "Main.SchDoc")},
                       {"name": "Gone", "path": str(root / "src" / "Gone.SchDoc")}],
            "pcb_doc_paths": [str(root / "Demo.PcbDoc")], "pcb_dwf_paths": []}


def test_design_state_hashes_the_project_files(tmp_path):
    project = make_project(tmp_path / "p")
    state = rc.design_state(project)
    assert [f["file"] for f in state["files"]] == ["Demo.PrjPcb", "src/Main.SchDoc",
                                                   "src/Gone.SchDoc", "Demo.PcbDoc"]
    sheet = state["files"][1]
    assert sheet["sha1"] == hashlib.sha1(b"sheet one").hexdigest()
    assert sheet["bytes"] == 9 and sheet["saved_utc"].endswith("Z")
    assert state["files"][2] == {"file": "src/Gone.SchDoc", "missing": True}
    assert state["missing_files"] == 1
    assert state["kind"] == "files" and state["id"] == f"files {state['fingerprint'][:12]}"
    (tmp_path / "p" / "src" / "Main.SchDoc").write_bytes(b"sheet two")
    assert rc.design_state(project)["fingerprint"] != state["fingerprint"]


def test_head_state_pathspecs_are_literal_and_fit_a_command_line():
    from services import clone_status
    assert clone_status._pathspec_chunks(None) == [[]]
    assert clone_status._pathspec_chunks([]) == []
    paths = [f"Board [A]/Sheet{n:03d}.SchDoc" for n in range(400)]
    chunks = clone_status._pathspec_chunks(paths)
    assert len(chunks) > 1 and sum(len(chunk) for chunk in chunks) == 400
    assert all(sum(len(spec) + 1 for spec in chunk) <= clone_status.PATHSPEC_CHARS
               for chunk in chunks)
    assert chunks[0][0] == ":(literal,icase)Board [A]/Sheet000.SchDoc"


def test_design_state_names_sheets_beside_the_project_folder_apart(tmp_path):
    for folder, text in (("a", "one"), ("b", "two")):
        (tmp_path / folder).mkdir()
        (tmp_path / folder / "Power.SchDoc").write_text(text, encoding="utf-8")
    project = make_project(tmp_path / "p")
    project["sheets"] = [{"name": "Power", "path": str(tmp_path / f / "Power.SchDoc")}
                         for f in ("a", "b")]
    state = rc.design_state(project)
    assert [f["file"] for f in state["files"]] == [
        "Demo.PrjPcb", "../a/Power.SchDoc", "../b/Power.SchDoc", "Demo.PcbDoc"]


def test_design_state_takes_the_git_commit(tmp_path):
    project = make_project(tmp_path / "p")
    commit = "0123456789abcdef0123456789abcdef01234567"
    asked = []

    def clean_state(folder, paths):
        asked.append((folder, paths))
        return {"commit": commit, "date_utc": "2026-10-01T10:00:00+00:00", "branch": "main",
                "design_files_changed": 0, "altium365": False}

    clean = rc.design_state(project, clean_state)
    assert clean["kind"] == "git" and clean["id"] == "git 0123456789ab"
    assert clean["changes_since_commit"] is False and clean["branch"] == "main"
    # The git state compares exactly the files the state hashes.
    assert asked == [(project["root_dir"], [str(path) for path in rc.design_files(project)])]
    dirty = rc.design_state(project, lambda folder, paths: {"commit": commit,
                                                            "design_files_changed": 2})
    assert dirty["id"] == "git 0123456789ab+changes" and dirty["design_files_changed"] == 2
    unborn = rc.design_state(project, lambda folder, paths: {"commit": None})
    assert unborn["kind"] == "files"
    failed = rc.design_state(project, lambda folder, paths: {"error": "git_not_found"})
    assert failed["kind"] == "files" and failed["git_error"] == "git_not_found"

    def raises(folder, paths):
        raise OSError("no")

    assert rc.design_state(project, raises)["git_error"] == "OSError"


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_head_state_reads_a_real_work_tree(tmp_path, monkeypatch):
    empty = tmp_path / "empty.gitconfig"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Test")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "test@example.com")
    repo = tmp_path / "repo"
    project = repo / "boards" / "demo"

    def git(*args):
        return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True,
                              text=True).stdout

    assert head_state(tmp_path / "nowhere") is None
    repo.mkdir()
    git("init", "-q", "-b", "main")
    assert head_state(repo) == {"commit": None, "date_utc": None, "branch": "main",
                                "design_files_changed": 0, "changed_files": 0,
                                "outside_work_tree": 0, "altium365": False}
    project.mkdir(parents=True)
    (project / "Demo.SchDoc").write_text("v1", encoding="utf-8")
    (repo / "notes.txt").write_text("x", encoding="utf-8")
    git("add", ".")
    git("commit", "-q", "-m", "first")
    full = git("rev-parse", "HEAD").strip()
    state = head_state(project)
    assert state["commit"] == full and state["design_files_changed"] == 0
    (project / "Demo.SchDoc").write_text("v2", encoding="utf-8")
    (repo / "notes.txt").write_text("y", encoding="utf-8")  # outside the project folder
    state = head_state(project)
    assert state["design_files_changed"] == 1 and state["changed_files"] == 1
    git("remote", "add", "origin",
        "https://ws.365.altium.com/git/0F1E2D3C-4B5A-6978-8796-A5B4C3D2E1F0.git")
    assert head_state(project)["altium365"] is True


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_head_state_compares_exactly_the_project_design_files(tmp_path, monkeypatch):
    empty = tmp_path / "empty.gitconfig"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Test")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "test@example.com")
    repo = tmp_path / "repo"
    board = repo / "Board [A]"  # brackets: a literal path, not a glob
    shared = repo / "shared"
    for folder in (board, shared):
        folder.mkdir(parents=True)
    (board / "Demo.PrjPcb").write_text("[Design]", encoding="utf-8")
    (board / "Main.SchDoc").write_text("main", encoding="utf-8")
    (shared / "Common.SchDoc").write_text("v1", encoding="utf-8")
    (repo / ".gitignore").write_text("Scratch.SchDoc\n", encoding="utf-8")

    def git(*args):
        return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True,
                              text=True).stdout

    git("init", "-q", "-b", "main")
    git("add", ".")
    git("commit", "-q", "-m", "first")
    files = [board / "Demo.PrjPcb", board / "Main.SchDoc", shared / "Common.SchDoc"]

    def changed(paths):
        return head_state(board, [str(path) for path in paths])["design_files_changed"]

    assert changed(files) == 0
    # A sheet the project uses from outside its folder.
    (shared / "Common.SchDoc").write_text("v2", encoding="utf-8")
    assert changed(files) == 1
    git("checkout", "--", "shared/Common.SchDoc")
    # A new sheet in the folder that the project does not list is not a design file.
    (board / "Loose.SchDoc").write_text("x", encoding="utf-8")
    assert changed(files) == 0
    # Listed but in no commit: untracked, git-ignored, outside the work tree.
    (board / "Extra.SchDoc").write_text("x", encoding="utf-8")
    (board / "Scratch.SchDoc").write_text("x", encoding="utf-8")
    outside = tmp_path / "elsewhere" / "Far.SchDoc"
    outside.parent.mkdir()
    outside.write_text("x", encoding="utf-8")
    state = head_state(board, [str(path) for path in files] + [
        str(board / "Extra.SchDoc"), str(board / "Scratch.SchDoc"), str(outside)])
    assert state["design_files_changed"] == 3 and state["outside_work_tree"] == 1
    # The path's case as the PrjPcb wrote it does not matter.
    (board / "Main.SchDoc").write_text("main v2", encoding="utf-8")
    assert changed([board / "main.schdoc"]) == 1


# ---------- the record ----------

STATE = {"kind": "files", "id": "files aaaaaaaaaaaa", "fingerprint": "a" * 40,
         "files": [{"file": "Demo.PrjPcb", "sha1": "b" * 40, "saved_utc": "2026-10-01T10:00:00Z",
                    "bytes": 10}]}


def record_model(**kwargs) -> dict:
    return run_demo(state=STATE, variant="Prod", **kwargs)


def test_record_renders_every_part():
    model = rc.apply_decisions(record_model(), {"D-03": {"status": "yes", "note": "agreed -- ok"}})
    text = rc.render_record(model, generated_utc="2026-10-24 10:00 UTC", generator="9.9.9")
    assert text.startswith("# Review record: Demo review\n")
    assert "- Design state: **files aaaaaaaaaaaa**" in text
    assert "| Demo.PrjPcb | `bbbbbbbbbbbb` | 2026-10-01T10:00:00Z |" in text
    assert "| Section | Yes | No | N/A | Needs judgment | Total |" in text
    assert "| **All** | 1 | 1 | 1 | 1 | 4 |" in text
    assert "### Alpha" in text and "### Beta" in text
    assert "| D-01 | No errors. | auto | No | errors: 1 | check_bom | R1 |" in text
    assert "Needs judgment (leans No)" in text
    assert "Yes (review; tool: Needs judgment)" in text
    assert "- [ ] D-01 (No): No errors." in text
    assert "| Reviewer | | | | |" in text
    assert "--" not in text.split("<!-- ")[1].split("\n", 1)[1].rsplit("-->", 1)[0]


def test_record_round_trip_and_escaping():
    model = record_model(items=["D-01", "D-03"])
    model["items"][0]["text"] = "Pipes | and -- dashes"
    text = rc.render_record(model, generated_utc="2026-10-24 10:00 UTC")
    assert "Pipes \\| and -- dashes" in text
    block = rc.parse_record(text)
    assert block["format"] == rc.RECORD_MARKER and block["version"] == 1
    assert block["checklist"]["id"] == "demo" and block["variant"] == "Prod"
    assert block["state"]["files"] == {"Demo.PrjPcb": "b" * 40}
    assert block["items"] == {"D-01": {"status": "no", "kind": "auto", "section": "alpha"},
                              "D-03": {"status": "needs_judgment", "kind": "judgment",
                                       "section": "beta"}}
    assert block["selection"] == {"sections": None, "items": ["D-01", "D-03"]}


@pytest.mark.parametrize("text, words", [
    ("# just notes", "holds no review record block"),
    (f"<!-- {rc.RECORD_MARKER} v1\n{{bad\n-->", "not valid JSON"),
    (f"<!-- {rc.RECORD_MARKER} v7\n{{}}\n-->", "newer than this server reads"),
    (f"<!-- {rc.RECORD_MARKER} v1\n{{}}\n-->", "has no items"),
])
def test_parse_record_errors(text, words):
    error = invalid(rc.parse_record, text)
    assert error["error"] == "previous_not_a_record" and words in error["message"]


def test_read_previous(tmp_path):
    text = rc.render_record(record_model(), generated_utc="t")
    write(tmp_path / "reviews" / "r1.md", text)
    assert rc.read_previous("reviews/r1.md", tmp_path)["file"] == "r1.md"
    assert rc.read_previous(str(tmp_path / "reviews" / "r1.md"), None)["items"]
    assert invalid(rc.read_previous, "missing.md", tmp_path)["error"] == "previous_not_found"
    (tmp_path / "latin.md").write_bytes(b"\xe9\xe9")
    assert "not UTF-8" in invalid(rc.read_previous, "latin.md", tmp_path)["message"]


def test_compare_with_a_previous_record():
    before = record_model()
    before = rc.apply_decisions(before, {"D-03": {"status": "no", "note": "missing"}})
    previous = rc.parse_record(rc.render_record(before, generated_utc="t0"))
    previous["file"] = "r0.md"
    previous["items"]["D-GONE"] = {"status": "no"}
    now_stubs = Stubs(bom={"severity_counts": {"error": 0}, "findings": []},
                      dfm={"findings": [{"check": "spacing", "issue": "Too close here."}]})
    state = {**STATE, "id": "files cccccccccccc", "fingerprint": "c" * 40,
             "files": [{"file": "Demo.PrjPcb", "sha1": "d" * 40}, {"file": "New.SchDoc",
                                                                    "sha1": "e" * 40}]}
    now = run_demo(now_stubs, state=state)
    comparison = rc.compare(now, previous)
    assert comparison["state_changed"] is True
    assert comparison["files_changed"] == ["Demo.PrjPcb", "New.SchDoc"]
    assert comparison["newly_failing"] == ["D-04"]
    assert comparison["fixed"] == ["D-01"]
    assert comparison["still_open"] == ["D-02", "D-03"]
    assert comparison["changed"] == [{"id": "D-03", "from": "no", "to": "needs_judgment"}]
    assert comparison["not_in_this_run"] == ["D-GONE"]
    assert comparison["previous_decisions"] == [{"id": "D-03", "status": "no"}]
    lines = "\n".join(rc.render_comparison(comparison))
    assert "- Newly failing: D-04" in lines and "- Fixed: D-01" in lines
    assert "Design state changed: 2 file(s) differ" in lines
    assert "D-03 (No)" in lines
    same = rc.compare(before, previous)
    assert same["state_changed"] is False and same["files_changed"] == []


def test_compare_keeps_newly_failing_out_of_still_open_and_n_a_out_of_fixed():
    model = {"items": [{"id": "A", "status": "no"}, {"id": "B", "status": "n/a"},
                       {"id": "C", "status": "yes"}, {"id": "D", "status": "needs_judgment"}],
             "state": {}, "checklist": {"id": "demo"}}
    previous = {"items": {"A": {"status": "needs_judgment"}, "B": {"status": "no"},
                          "C": {"status": "no"}, "D": {"status": "no"}}, "state": {}}
    comparison = rc.compare(model, previous)
    assert comparison["newly_failing"] == ["A"]
    assert comparison["fixed"] == ["C"]
    assert comparison["still_open"] == ["D"]
    assert comparison["changed"] == [{"id": "B", "from": "no", "to": "n/a"},
                                     {"id": "D", "from": "no", "to": "needs_judgment"}]
    assert "B No -> N/A" in "\n".join(rc.render_comparison(comparison))


def test_compare_notes_another_checklist():
    previous = {"items": {"D-01": {"status": "yes"}}, "checklist": {"id": "other"},
                "state": {}}
    comparison = rc.compare(record_model(), previous)
    assert "another checklist" in comparison["note"]
    assert comparison["newly_failing"] == ["D-01"]
    assert [n["id"] for n in comparison["new_items"]] == ["D-02", "D-03", "D-04"]


def test_page_review_pages_items_and_cuts_files():
    model = record_model()
    model["state"] = {**STATE, "files": STATE["files"] * 5}
    page = rc.page_review(model, 1, 2, files_listed=2)
    assert [i["id"] for i in page["items"]] == ["D-02", "D-03"]
    assert page["has_more"] and page["next_offset"] == 3 and page["items_total"] == 4
    assert len(page["design_state"]["files"]) == 2 and page["design_state"]["files_total"] == 5
    assert page["needs_judgment"] == ["D-02", "D-03"] and "tool_runs" in page
    brief = rc.page_review(model, 0, 1, full=False)
    assert "tool_runs" not in brief and "needs_judgment" not in brief


# ---------- the MCP tool ----------

@pytest.fixture
def tool_env(tmp_path):
    """main with a loaded demo project (no netlist, no variants), the demo checklist as
    the bundled set and stub evidence tools."""
    project = make_project(tmp_path / "proj")
    stubs = Stubs()
    with patch.object(main, "_project", project), \
            patch.object(main, "_variant_state", VariantState([])), \
            patch.object(main._altium, "_netlist", None), \
            patch.object(main, "_bundled_checklists", {"demo": validated()}), \
            patch.object(main, "_review_cache", {}), \
            patch.object(main, "_review_tools", stubs.tools), \
            patch.object(main, "head_state", lambda folder, paths=None: None):
        yield project, stubs


def call(**kwargs) -> dict:
    return json.loads(main.run_review_checklist(**kwargs))


def test_tool_lists_the_checklists(tool_env):
    result = call()
    assert [c["id"] for c in result["checklists"]] == ["demo"]
    assert result["overrides"] == {"files": [], "applied": [], "used": True}
    assert call(items=["D-01"])["error"] == "checklist_required"


def test_tool_runs_a_checklist(tool_env):
    result = call(checklist="Demo")
    assert result["checklist"]["id"] == "demo" and result["load"] == "netlist_free"
    assert result["variant"] is None
    assert result["design_state"]["kind"] == "files"
    assert result["coverage"][-1]["total"] == 4
    assert [i["status"] for i in result["items"]] == ["no", "needs_judgment",
                                                      "needs_judgment", "n/a"]
    assert result["needs_judgment"] == ["D-02", "D-03"]
    assert "format='markdown'" in result["record"]


@pytest.mark.parametrize("kwargs, error", [
    ({"checklist": "nope"}, "unknown_checklist"),
    ({"checklist": "demo", "sections": ["gamma"]}, "unknown_section"),
    ({"checklist": "demo", "items": ["D-99"]}, "unknown_item"),
    ({"checklist": "demo", "decisions": {"D-01": "maybe"}}, "invalid_decision"),
    ({"checklist": "demo", "previous": "nothing.md"}, "previous_not_found"),
    ({"checklist": "demo", "format": "html"}, "invalid_format"),
])
def test_tool_errors(tool_env, kwargs, error):
    assert call(**kwargs)["error"] == error


def test_tool_reports_override_problems_and_can_ignore_them(tool_env):
    project, _ = tool_env
    write(Path(project["root_dir"]) / rc.OVERRIDE_FILE, {"disable": ["D-77"]})
    error = call(checklist="demo")
    assert error["error"] == "invalid_checklist" and "D-77" in error["message"]
    assert call(checklist="demo", overrides=False)["coverage"][-1]["total"] == 4


def test_tool_reuses_the_run_for_the_record(tool_env):
    _, stubs = tool_env
    call(checklist="demo")
    record = main.run_review_checklist(checklist="demo", format="markdown",
                                       decisions={"D-03": "yes: fine"})
    assert stubs.calls == [("check_bom", 0), ("check_dfm", 0)]
    assert record.startswith("# Review record: Demo review")
    assert "Yes (review; tool: Needs judgment)" in record
    rc.parse_record(record)


def test_tool_compares_with_a_saved_record(tool_env):
    project, _ = tool_env
    record = main.run_review_checklist(checklist="demo", format="markdown")
    write(Path(project["root_dir"]) / "review.md", record)
    result = call(checklist="demo", previous="review.md")
    assert result["comparison"]["state_changed"] is False
    assert result["comparison"]["still_open"] == ["D-01", "D-02", "D-03"]
    markdown = main.run_review_checklist(checklist="demo", previous="review.md",
                                         format="markdown")
    assert "## Changes since the previous record" in markdown


def test_tool_shrinks_pages_and_records(tool_env):
    full = main.run_review_checklist(checklist="demo")
    full_record = main.run_review_checklist(checklist="demo", format="markdown")
    with patch.object(main, "QUERY_NET_MAX_RESULT_SIZE_CHARS", len(full) - 1):
        result = call(checklist="demo")
        assert result["items_limited_to"] < 80 and result["has_more"]
    with patch.object(main, "QUERY_NET_MAX_RESULT_SIZE_CHARS", len(full_record) - 1):
        record = main.run_review_checklist(checklist="demo", format="markdown")
        assert len(record) < len(full_record)
        assert rc.parse_record(record)["checklist"]["id"] == "demo"
    with patch.object(main, "QUERY_NET_MAX_RESULT_SIZE_CHARS", 100):
        brief = call(checklist="demo")
        assert brief["items_limited_to"] == 1 and "omitted" in brief


def test_tool_never_returns_a_record_cut_through_its_block(tool_env):
    many = demo_checklist(items=[
        {"id": f"J-{n:03d}", "section": "alpha", "kind": "judgment", "text": "word " * 59,
         "hint": "hint " * 70} for n in range(150)])
    with patch.object(main, "_bundled_checklists", {"demo": validated(many)}):
        record = main.run_review_checklist(checklist="demo", format="markdown")
        assert len(record) <= main.QUERY_NET_MAX_RESULT_SIZE_CHARS
        assert rc.parse_record(record)["items"]["J-149"] == {"status": "needs_judgment",
                                                             "kind": "judgment",
                                                             "section": "alpha"}
        assert "- [ ] J-000 (Needs judgment)\n" in record  # open items by id only
        with patch.object(main, "QUERY_NET_MAX_RESULT_SIZE_CHARS", 20_000):
            error = json.loads(main.run_review_checklist(checklist="demo", format="markdown"))
        assert error["error"] == "record_too_large" and error["sections"] == ["alpha"]


def test_tool_runs_again_after_an_outjob_or_report_is_saved(tool_env):
    project, stubs = tool_env
    call(checklist="demo")
    call(checklist="demo")
    assert stubs.calls.count(("check_bom", 0)) == 1
    (Path(project["root_dir"]) / "Project Outputs").mkdir()
    (Path(project["root_dir"]) / "Project Outputs" / "Design Rule Check.html").write_text(
        "report", encoding="utf-8")
    call(checklist="demo")
    assert stubs.calls.count(("check_bom", 0)) == 2
    with patch.object(rc, "FOLDER_SIGNATURE_FILES", 1):  # too many files: never reused
        call(checklist="demo")
        call(checklist="demo")
    assert stubs.calls.count(("check_bom", 0)) == 4


def test_folder_signature(tmp_path):
    (tmp_path / "a.txt").write_text("1", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "index").write_text("x", encoding="utf-8")
    first = rc.folder_signature(tmp_path)
    (tmp_path / ".git" / "index").write_text("changed", encoding="utf-8")
    assert rc.folder_signature(tmp_path) == first  # .git is left out
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("2", encoding="utf-8")
    assert rc.folder_signature(tmp_path) != first
    assert rc.folder_signature(tmp_path, max_files=1) is None
    assert rc.folder_signature(tmp_path / "missing") is None


def test_tool_runs_again_after_a_save(tool_env):
    project, stubs = tool_env
    call(checklist="demo")
    Path(project["prj_pcb_path"]).write_text("[Design]\nchanged\n", encoding="utf-8")
    call(checklist="demo")
    assert stubs.calls.count(("check_bom", 0)) == 2


# ---------- prompts and instructions ----------

def test_review_prompts_call_the_checklist():
    from services.constraints_review import CONSTRAINTS_REVIEW_PROMPT, CONSTRAINTS_REVIEW_TOOLS
    from services.manufacturing_review import (
        MANUFACTURING_REVIEW_TOOLS, PCB_MANUFACTURING_REVIEW_PROMPT,
    )
    from services.release_gate import RELEASE_REVIEW_PROMPT, RELEASE_REVIEW_TOOLS
    assert 'run_review_checklist(checklist="schematic")' in main.SCHEMATIC_REVIEW_PROMPT
    assert 'run_review_checklist(checklist="pcb", sections=["high_speed"])' in \
        CONSTRAINTS_REVIEW_PROMPT
    assert 'run_review_checklist(checklist="bom")' in PCB_MANUFACTURING_REVIEW_PROMPT
    assert 'run_review_checklist(checklist="release")' in RELEASE_REVIEW_PROMPT
    for prompt in (main.SCHEMATIC_REVIEW_PROMPT, CONSTRAINTS_REVIEW_PROMPT,
                   PCB_MANUFACTURING_REVIEW_PROMPT, RELEASE_REVIEW_PROMPT):
        assert "Checklist coverage" in prompt and 'format="markdown"' in prompt
    for tools in (CONSTRAINTS_REVIEW_TOOLS, MANUFACTURING_REVIEW_TOOLS, RELEASE_REVIEW_TOOLS):
        assert "run_review_checklist" in tools


def test_instructions_describe_the_tool_after_the_cut():
    position = main.SERVER_INSTRUCTIONS.index("## Review Checklists")
    assert position > 4096
    assert "run_review_checklist" in main.SERVER_INSTRUCTIONS[position:position + 1500]


def test_readme_documents_the_override_format():
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    assert "### Review checklists" in readme and "`run_review_checklist`" in readme
    block = re.search(r"### Review checklists.*?```json\n(.*?)```", readme, re.DOTALL)[1]
    override = json.loads(block)
    bundled = {"schematic": rc.load_bundled(BUNDLED)["schematic"]}
    with patch.object(rc, "read_json", lambda path, label, folder=None: override):
        checklists, _ = rc.apply_overrides(bundled, [(Path("x.json"), Path("."), "readme")])
    assert any(i["id"] == "TEAM-01" for i in checklists["schematic"]["items"])


# ---------- the bundled checklists on the synthetic project ----------

@pytest.mark.parametrize("checklist", ["schematic", "pcb", "release", "bom"])
def test_bundled_checklists_run_through_the_real_tools(offline_env, checklist):
    root, _, _ = offline_env
    _load(root)
    with patch.object(main, "_review_cache", {}):
        result = call(checklist=checklist)
        assert "error" not in result, result
        rows = result["items"]
        while result.get("has_more"):
            result = call(checklist=checklist, offset=result["next_offset"])
            rows += result["items"]
        record = main.run_review_checklist(checklist=checklist, format="markdown")
    assert len(rows) == result["items_total"]
    assert all(r["status"] in rc.STATUSES for r in rows)
    broken = [(r["id"], r.get("could_not_run")) for r in rows if r.get("could_not_run") in (
        "tool_failed", "invalid_arguments", "not_json", "tool_not_available", "RuntimeError",
        "TypeError", "KeyError", "AttributeError", "ValueError")]
    assert broken == []
    assert [r["id"] for r in rows if r.get("unresolved")] == []
    assert rc.parse_record(record)["checklist"]["id"] == checklist


def test_every_bundled_condition_reads_fields_the_tools_give(offline_env):
    """Each path and list a bundled condition reads is in its tool's result on the
    synthetic project (every page), unless the item is N/A there: a renamed field would
    otherwise turn a check into needs judgment."""
    root, _, _ = offline_env
    _load(root)
    runner = rc.ToolRunner(main._review_tools())
    checked, missing = 0, []
    for checklist in rc.load_bundled(BUNDLED).values():
        for item in checklist["items"]:
            evidence = item.get("evidence")
            result = runner.run(evidence["tool"], evidence["args"]) if evidence else None
            if result is None or "error" in result:
                continue
            data = result["data"]
            if any(rc.evaluate(cond, data)["hit"] for cond in evidence.get("na", [])):
                continue
            for group in ("no", "judge", "yes"):
                for cond in evidence.get(group, []):
                    checked += 1
                    missing += [(item["id"], path) for path in rc.evaluate(cond, data)["unresolved"]]
    assert checked > 100 and missing == []


# ---------- the bundled mappings on small tool results ----------

BUNDLED_ITEMS = {item["id"]: item for checklist in rc.load_bundled(BUNDLED).values()
                 for item in checklist["items"]}


def bundled(item_id: str, data: dict) -> dict:
    return rc.evaluate_item(BUNDLED_ITEMS[item_id], {"data": data})


def lean(row_: dict):
    return row_["status"], row_.get("lean")


def power_tree(findings=(), inputs=()):
    return {"overview": {"rails": 2, "converters": 1, "power_inputs": len(inputs) or 1},
            "inputs": list(inputs), "findings": list(findings)}


def test_power_items_lean_no_only_on_what_the_tool_warns_about():
    info_rail = {"check": "rail_without_source", "severity": "info", "rail": "NET_A",
                 "designators": ["U1"], "issue": "A pin the part regulates itself."}
    assert lean(bundled("SCH-PWR-01", power_tree([info_rail]))) == ("needs_judgment", None)
    warned = {**info_rail, "severity": "warning"}
    assert lean(bundled("SCH-PWR-01", power_tree([warned]))) == ("needs_judgment", "no")
    assert lean(bundled("SCH-PWR-01", power_tree())) == ("needs_judgment", "yes")
    near = {"check": "fb_divider_mismatch", "severity": "info", "part": "U2",
            "implied_vref": 0.6, "near_common_vref": 0.6}
    assert lean(bundled("SCH-PWR-03", power_tree([near]))) == ("needs_judgment", "yes")
    odd = {k: v for k, v in near.items() if k != "near_common_vref"}
    assert lean(bundled("SCH-PWR-03", power_tree([near, odd]))) == ("needs_judgment", None)
    assert lean(bundled("SCH-PWR-03", power_tree([{**near, "severity": "warning"}]))) == (
        "needs_judgment", "no")
    # Capacitor findings are notes (one behind a ferrite is not seen): no lean either way.
    capless = {"check": "no_output_capacitor", "severity": "info", "part": "U2"}
    assert lean(bundled("SCH-PWR-05", power_tree([capless]))) == ("needs_judgment", None)
    assert lean(bundled("SCH-PWR-05", power_tree())) == ("needs_judgment", "yes")
    unfiltered = {"check": "smps_input_unfiltered", "severity": "info", "part": "U2"}
    assert lean(bundled("SCH-PWR-05", power_tree([unfiltered]))) == ("needs_judgment", None)


def test_power_input_protection_leans_no_for_external_inputs():
    jack = {"source": "J1", "kind": "connector", "net": "NET_IN", "exposure": "external",
            "power_only": True}
    row_ = bundled("SCH-PWR-04", power_tree(inputs=[jack]))
    assert lean(row_) == ("needs_judgment", "no")
    assert row_["anchors"] == {"designators": ["J1"], "nets": ["NET_IN"]}
    assert row_["evidence"].startswith("external power inputs with no fuse: 1")
    protected = {**jack, "fuse": ["F1"], "reverse_polarity": ["D1"], "tvs": ["D2"]}
    assert lean(bundled("SCH-PWR-04", power_tree(inputs=[protected]))) == (
        "needs_judgment", "yes")
    usb = {**jack, "usb": True, "tvs": ["D2"]}  # the host limits VBUS; no reversal
    assert lean(bundled("SCH-PWR-04", power_tree(inputs=[usb]))) == ("needs_judgment", "yes")
    unknown = {**jack, "exposure": "unknown"}
    note = {"check": "no_input_fuse", "severity": "info", "source": "J1", "nets": ["NET_IN"]}
    assert lean(bundled("SCH-PWR-04", power_tree([note], [unknown]))) == (
        "needs_judgment", None)


def test_esd_behind_a_series_part_is_a_note():
    interfaces = {"connectors_total": 1, "findings": [
        {"check": "esd_behind_series", "severity": "info", "connector": "J2"}]}
    assert lean(bundled("SCH-CON-01", interfaces)) == ("needs_judgment", None)
    interfaces["findings"].append({"check": "no_esd", "severity": "warning", "connector": "J2"})
    assert lean(bundled("SCH-CON-01", interfaces)) == ("needs_judgment", "no")


def test_board_items_follow_the_tools_verdicts():
    sync = {"severity_counts": {"error": 0, "warning": 2, "info": 1}, "findings": [
        {"check": "footprint", "severity": "warning", "component": "R1"},
        {"check": "comment", "severity": "warning", "component": "R2"}]}
    assert bundled("PCB-PLC-01", sync)["status"] == "no"
    clean = {"severity_counts": {"error": 0, "warning": 0, "info": 1}, "findings": []}
    assert bundled("PCB-PLC-01", clean)["status"] == "yes"
    assert bundled("PCB-PLC-01", clean)["evidence"] == "sync errors: 0; sync warnings: 0"
    assert bundled("PCB-PLC-01", {**clean, "netlist_stale": True})["status"] == "needs_judgment"
    ring = {"kinds": [{"kind": "MinimumAnnularRing", "violation_count": 3}]}
    assert bundled("PCB-HS-01", ring)["status"] == "no"
    marginal = {"rule_count": 1, "rules": [{"name": "LEN_A", "summary": {
        "violations": 2, "marginal_violations": 2, "incomplete": 0}}]}
    row_ = bundled("PCB-HS-02", marginal)
    assert row_["status"] == "needs_judgment"
    assert row_["evidence"].startswith("length rules with marginal violations only: 1")
    marginal["rules"][0]["summary"]["violations"] = 3
    assert bundled("PCB-HS-02", marginal)["status"] == "no"


def test_variant_items_cover_every_variant_and_need_both_outputs():
    none = {"not_applicable": {"variant_entry": "the project has no variant entries"},
            "findings": []}
    assert bundled("BOM-VAR-02", none)["status"] == "n/a"
    stale = {"not_applicable": {}, "findings": [
        {"check": "variant_entry", "severity": "warning", "kind": "no_such_part",
         "examples": [{"variant": "Lite", "designator": "R99"}]}]}
    row_ = bundled("BOM-VAR-02", stale)
    assert row_["status"] == "no" and row_["findings"] == {"variant_entry/no_such_part": 1}
    assert row_["anchors"] == {"designators": ["R99"], "other": ["Lite"]}
    assert bundled("BOM-VAR-02", {"not_applicable": {}, "findings": []})["status"] == "yes"
    outjobs = {"coverage": {"bom": {"status": "connected"},
                            "pick_and_place": {"status": "missing"}},
               "findings": [{"check": "outjob_files", "severity": "info"}]}
    assert bundled("BOM-VAR-03", outjobs)["status"] == "n/a"
    outjobs["coverage"]["pick_and_place"]["status"] = "connected"
    assert bundled("BOM-VAR-03", outjobs)["status"] == "yes"
    outjobs["findings"].append({"check": "bom_pnp_variant", "severity": "warning"})
    assert bundled("BOM-VAR-03", outjobs)["status"] == "no"


def test_netlist_free_load_says_which_items_could_not_run(env):
    root, _, _ = env
    _load(root)
    with patch.object(main, "_review_cache", {}):
        result = call(checklist="schematic", sections=["power"])
    assert result["load"] == "netlist_free"
    assert {r["could_not_run"] for r in result["items"] if r["kind"] != "judgment"} == {
        "netlist_required"}
    assert result["could_not_run"][0]["error"] == "netlist_required"
