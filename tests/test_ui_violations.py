"""The ERC and DRC results views (roadmap #60, server/ui/erc_results.html and
drc_results.html): their ui:// resources and tool links, the self-contained pages, the
tools they call, their data contracts against run_erc's and get_drc_report's output
(Altium's public examples, the repo's DRC report fixture and synthetic reports) and
paging the way the views page."""

import ast
import asyncio
import json
import os
import re
import shutil
from collections import Counter
from functools import cache
from pathlib import Path

import pytest
from mcp import types

import server.main as main_mod
import server.services.drc_results as drc
import server.services.erc as erc
import server.services.ui_views as uv
from server.parsers.drc_report import HEALTH, WAIVED, WARNING, DrcTable, DrcViolation
from server.parsers.pcb_doc import parse_pcb_doc
from server.parsers.prj_pcb import VariantState, parse_prj_pcb
from server.services.pcb_index import PcbIndex
from tests.test_drc_report import _report, _table

ROOT = Path(__file__).parents[1]
UI = ROOT / "server" / "ui"
EXAMPLES = Path(r"C:\Users\Public\Documents\Altium\AD25\Examples")
FIXTURES = ROOT / "tests" / "fixtures" / "drc"
ERC_VIEW = uv.view_for("run_erc")
DRC_VIEW = uv.view_for("get_drc_report")
ERC_EXAMPLES = [
    ("Mini PC", "Mini PC - Motherboard", "MiniPC.PrjPcb"),
    ("SpiritLevel-SL1", "SL1 Xilinx Spartan-IIE PQ208 Rev1.02.PrjPcb"),
    ("Bluetooth Sentinel", "Bluetooth_Sentinel.PrjPcb"),
]
SL1 = EXAMPLES / "SpiritLevel-SL1" / "SL1 Xilinx Spartan-IIE PQ208 Rev1.02.PrjPcb"
# The tools each page may call: its own (paging, refresh, regrouping) and the details.
CALLED = {
    "run_erc": {"run_erc", "get_sheet_context", "get_component", "query_net"},
    "get_drc_report": {"get_drc_report", "query_pcb_region", "get_component", "query_net"},
}


def _handle(request):
    handler = main_mod.mcp._mcp_server.request_handlers[type(request)]
    result = asyncio.run(handler(request))
    return result.model_dump(mode="json", by_alias=True, exclude_none=True)


def _tools() -> dict:
    return {tool["name"]: tool
            for tool in _handle(types.ListToolsRequest(method="tools/list"))["tools"]}


def _page(view) -> str:
    return (UI / view.file).read_text(encoding="utf-8")


def _contract(view) -> dict:
    block = re.search(r'<script type="application/json" id="data-contract">(.*?)</script>',
                      _page(view), re.S)
    return json.loads(block.group(1))


def _script(view) -> str:
    return _page(view).split(uv.JS_MARKER, 1)[1]


def _rows_at(data, path: str) -> list:
    """The rows a contract path names, read as the views' rowsAt does."""
    if path == "result":
        return [data]
    rows = [data]
    for part in path.split("."):
        key = part.removesuffix("[]")
        out = []
        for row in rows:
            value = row.get(key) if isinstance(row, dict) else None
            if part.endswith("[]"):
                out.extend(value if isinstance(value, list) else [])
            elif value is not None:
                out.append(value)
        rows = out
    return rows


def _check(view, data: dict) -> list[str]:
    problems = []
    for path, shape in _contract(view)["shapes"].items():
        for key in shape["required"]:
            missing = sum(1 for row in _rows_at(data, path) if key not in row)
            if missing:
                problems.append(f"{path}: {key} missing in {missing}")
    return problems


# ---------- loading Altium's public examples and the DRC report ----------

@cache
def _compiled(path: Path):
    """A public example compiled offline once (its client caches the ERC compile too)."""
    client = main_mod.AltiumClient()
    client.compile_offline(str(path))
    return parse_prj_pcb(str(path)), client


def _example(monkeypatch, *parts: str) -> Path:
    path = EXAMPLES.joinpath(*parts)
    if not path.is_file():
        pytest.skip(f"{path.name} is not installed")
    prj, client = _compiled(path)
    monkeypatch.setattr(main_mod, "_project", {
        "name": path.stem, "root_dir": str(path.parent), "prj_pcb_path": str(path),
        "sheets": [], "parameters": prj.parameters, "pcb_doc_paths": [],
    })
    monkeypatch.setattr(main_mod, "_altium", client)
    monkeypatch.setattr(main_mod, "_variant_state", VariantState(prj.variants))
    return path


@cache
def _sl1_index() -> PcbIndex:
    return PcbIndex(parse_pcb_doc(str(SL1.with_suffix(".PcbDoc"))), None)


@pytest.fixture
def board(monkeypatch, tmp_path):
    """A copy of the SpiritLevel example's project and board in a temp folder, with the
    repo's DRC report fixture in its output folder (made for another board, Demo)."""
    if not SL1.with_suffix(".PcbDoc").is_file():
        pytest.skip("the SpiritLevel example is not installed")
    for source in (SL1, SL1.with_suffix(".PcbDoc")):
        shutil.copy(source, tmp_path / source.name)
    pcb = tmp_path / SL1.with_suffix(".PcbDoc").name
    out = tmp_path / f"Project Outputs for {SL1.stem}"
    out.mkdir()
    report = out / f"Design Rule Check - {pcb.stem}.html"
    shutil.copy(FIXTURES / "Design Rule Check - Demo.html", report)
    for item in (pcb, report):
        os.utime(item, (2_000_000_000, 2_000_000_000))
    session = main_mod.PcbSession()
    monkeypatch.setattr(session, "_load", lambda path, netlist: _sl1_index())
    monkeypatch.setattr(main_mod, "_pcb_session", session)
    monkeypatch.setattr(main_mod, "_project", {
        "name": SL1.stem, "root_dir": str(tmp_path), "prj_pcb_path": str(tmp_path / SL1.name),
        "sheets": [], "pcb_doc_paths": [str(pcb)], "output_path": "", "log_folder_path": "",
    })
    monkeypatch.setattr(main_mod._altium, "_netlist", None)
    monkeypatch.setattr(main_mod, "_variant_state", VariantState([]))
    return {"folder": tmp_path, "pcb": pcb, "report": report}


def _rows(prefix: str, count: int, other: str) -> list[str]:
    return [f"{prefix}: Between Pad U1-{i % 40 + 1}({i % 9 + 10}mm,{i % 7 + 1}mm) on Top Layer "
            f"And Pad {other}-{i % 3 + 1}({i % 5 + 20}mm,{i % 6 + 2}mm) on Top Layer"
            for i in range(count)]


def _synthetic(board, monkeypatch, *, stopped=False, stale=False, sizes=(300, 150, 40)):
    """A synthetic report (test_drc_report's helpers) in place of the file: 300, 150 and
    40 violations over three rules (or sizes), two rules sharing a description, health
    issues past the 20 rows a section shows, waived violations; stopped at the violation
    limit or older than the board when asked."""
    clearance, width, unrouted = sizes
    tables = [
        _table("Clearance Constraint (Gap=0.2mm) (All),(All)", clearance,
               _rows("Clearance Constraint: (0.1mm < 0.2mm)", clearance, "R1")),
        _table("Width Constraint (Min=0.15mm) (All)", width,
               [f"Width Constraint: Track ({i}mm,5mm)({i + 1}mm,5mm) on Top Layer" for i in range(width)]),
        _table("Short-Circuit Constraint (Allowed=No) (All),(All)"),
        _table("Un-Routed Net Constraint ( (All) )", unrouted,
               [f"Un-Routed Net Constraint: Net N{i} Between Pad R{i}-1(3mm,{i}mm) on Top Layer And "
                f"Pad C{i}-2(4mm,{i}mm) on Top Layer" for i in range(unrouted)]),
        _table("Hole To Hole Clearance (Gap=0.25mm) (All),(All)", 3, ["h1", "h2", "h3"]),
        _table("Hole To Hole Clearance (Gap=0.25mm) (All),(All)", 2, ["h4", "h5"]),
    ]
    report = _report(*tables)
    if stopped:
        report.tables.append(DrcTable(type=WARNING, title="More than 500 violations detected, "
                                      "DRC was stopped", count=0))
    report.tables.append(DrcTable(type=HEALTH, title="Unconnected Pins", count=25, violations=[
        DrcViolation(f"Unconnected pin: Pad U1-{i}(1mm,{i}mm) on Top Layer") for i in range(25)]))
    report.tables.append(DrcTable(type=WAIVED, title="Hole Size Constraint (Max=6mm) (All)", count=2,
                                  violations=[DrcViolation("Hole Size Constraint: (6.5mm > 6mm) Pad "
                                                           f"MH{i}-1(5mm,5mm) on Multi-Layer",
                                                           waiver="Waived by K\nmounting hole")
                                              for i in (1, 2)]))
    path = board["report"]
    os.utime(path, (2_000_000_000, 2_000_000_000))
    os.utime(board["pcb"], (2_000_000_000 + (7200 if stale else 0),) * 2)
    monkeypatch.setattr(main_mod, "load_report",
                        lambda project, pcb_path: (report, path, [], [str(path.parent)], []))
    return report


# ---------- the views' paging, as the pages do it ----------

def _erc_read(**args) -> dict:
    """run_erc read the way the ERC view reads it: the first page, then every next_offset
    (the summary, levels and advisories come with the first)."""
    first = json.loads(main_mod.run_erc(**args))
    key = "nets" if "nets" in first and "violations" not in first else "violations"
    data = {**first, key: list(first[key])}
    offset = first.get("next_offset")
    while offset is not None:
        page = json.loads(main_mod.run_erc(**args, offset=offset))
        assert "summary" not in page and page["offset"] == offset
        data[key].extend(page[key])
        offset = page.get("next_offset")
    return data


def _drc_pages(args: dict, field: str, first: dict | None = None) -> tuple[dict, list]:
    first = first or json.loads(main_mod.get_drc_report(**args, offset=0))
    items, page = list(first.get(field) or []), first
    while page.get("has_more"):
        page = json.loads(main_mod.get_drc_report(**args, offset=page["next_offset"]))
        items += page[field]
    return first, items


def _drc_read(**args) -> dict:
    """get_drc_report read the way the DRC view reads it: the groups of the call, the
    rule groups, each rule's violations through rule= (once per description, rows given
    back by exact title, other sections' rows left out), the sections in full."""
    units = {"units": args["units"]} if "units" in args else {}
    first, groups = _drc_pages(args, "groups", json.loads(main_mod.get_drc_report(**args)))
    if groups and "rule" not in groups[0]:
        _, rule_groups = _drc_pages({**units, "group_by": "rule"}, "groups")
    else:
        rule_groups = groups
    rows = []
    for title in dict.fromkeys(group["rule"].strip().lower() for group in rule_groups):
        exact = next(group["rule"] for group in rule_groups if group["rule"].strip().lower() == title)
        _, listed = _drc_pages({**units, "rule": exact}, "violations")
        rows += [row for row in listed if "section" not in row]
    sections = {}
    for key, section in _contract(DRC_VIEW)["sections"].items():
        for entry in first.get(key) or []:
            _, listed = _drc_pages({**units, "rule": entry["title"]}, "violations")
            sections[entry["title"]] = [row for row in listed if row.get("section") == section]
    return {"first": first, "groups": groups, "rule_groups": rule_groups, "rows": rows,
            "sections": sections}


# ---------- resources and links ----------

def test_both_views_are_listed_resources_with_mime_type_and_meta():
    listed = {row["uri"]: row for row in
              _handle(types.ListResourcesRequest(method="resources/list"))["resources"]}
    for view, name in ((ERC_VIEW, "erc-results"), (DRC_VIEW, "drc-results")):
        assert view.uri == f"ui://altium-copilot-kc/{name}.html"
        row = listed[view.uri]
        assert row["mimeType"] == "text/html;profile=mcp-app" and row["_meta"] == uv.RESOURCE_META
        assert row["description"].startswith("HTML page (MCP Apps view)")
        result = _handle(types.ReadResourceRequest(
            method="resources/read", params=types.ReadResourceRequestParams(uri=view.uri)))
        (content,) = result["contents"]
        assert content["mimeType"] == uv.MIME_TYPE and content["_meta"] == uv.RESOURCE_META
        assert content["text"] == uv.render(view)


def test_run_erc_and_get_drc_report_link_their_views():
    tools = _tools()
    for name, view in (("run_erc", ERC_VIEW), ("get_drc_report", DRC_VIEW)):
        assert tools[name]["_meta"] == {
            "anthropic/maxResultSizeChars": main_mod.QUERY_NET_MAX_RESULT_SIZE_CHARS,
            "ui": {"resourceUri": view.uri}, "ui/resourceUri": view.uri}
        assert uv.view_for(name) is view
    # One view per tool: every view's tool carries its link, no tool links two views.
    assert len({view.tool for view in uv.VIEWS}) == len(uv.VIEWS)


def test_the_tools_keep_their_output_schema_and_text_results(monkeypatch):
    tools = _tools()
    for name in ("run_erc", "get_drc_report"):
        assert tools[name]["outputSchema"]["properties"] == {
            "result": {"title": "Result", "type": "string"}}
    _example(monkeypatch, *ERC_EXAMPLES[2])
    for name, args in (("run_erc", {}), ("run_erc", {"min_level": "all", "offset": 50}),
                       ("run_erc", {"report": "single_pin_nets"}), ("get_drc_report", {})):
        direct = getattr(main_mod, name)(**args)
        result = _handle(types.CallToolRequest(
            method="tools/call", params=types.CallToolRequestParams(name=name, arguments=args)))
        assert result["content"] == [{"type": "text", "text": direct}], name
        assert result["structuredContent"] == {"result": direct} and not result.get("isError")


# ---------- the pages ----------

def _called(view) -> set[str]:
    script = _script(view)
    names = set(re.findall(r'callTool\("([a-z_]+)"', script))
    names |= set(re.findall(r'(?:ask|calls\.push)\(\[?"([a-z_]+)"', script))
    return names


@pytest.mark.parametrize("view", [ERC_VIEW, DRC_VIEW], ids=["erc", "drc"])
def test_the_pages_call_only_registered_tools_open_to_apps(view):
    tools = _tools()
    assert _called(view) == CALLED[view.tool]
    for name in _called(view):
        visibility = ((tools[name].get("_meta") or {}).get("ui") or {}).get("visibility")
        assert visibility is None or "app" in visibility
    # get_sheet_context with no sheet_name would ask the running Altium: always named.
    if view is ERC_VIEW:
        args = re.findall(r'"get_sheet_context", (\{[^}]*\})', _script(view))
        assert args and all(arg == "{ sheet_name: row.sheet }" for arg in args)


@pytest.mark.parametrize("view", [ERC_VIEW, DRC_VIEW], ids=["erc", "drc"])
def test_the_pages_ask_the_host_only_for_what_it_offers(view):
    script = _script(view)
    shell = (UI / "shell.js").read_text(encoding="utf-8")
    # Every tools/call goes through the shell, which checks serverTools first: paging
    # (readPages), details (toolCache), reloads and regrouping.
    assert script.count("Shell.callTool(") == 1
    assert "else if (!canCallTools()) stopped" in shell
    assert "if ((entry && !entry.error) || !canCallTools() || tornDown) return entry;" in shell
    assert "if (view.busy || !canCall() || view.closed) return;" in script
    assert "Shell.readPages(" in script and "closed: () => view.closed" in script
    # The display-mode button asks only for a mode the host lists.
    assert "Shell.displayModeButton(" in script and "return canDisplay(mode) ? mode : null;" in shell
    # A failed detail call is never kept as the answer, and a stop says why.
    assert "next = { error: error.message };" in shell
    for reason in ("the view was closed", "does not let the view call ${tool} for the next pages",
                   "the view reads at most ${budget.total} pages", "could not be read: ${error.message}"):
        assert reason in shell, reason
    # DOM from text only.
    assert "innerHTML" not in script and "insertAdjacentHTML" not in script


def test_the_shell_table_keeps_focus_and_groups():
    shell = (UI / "shell.js").read_text(encoding="utf-8")
    for snippet in ("function focusState()", "if (target) target.focus();", "data: { col: column.key }",
                    'class: "grp"', '"aria-expanded"', "setGroup(next)", "setFilter(next)",
                    "expandAll()", "collapseAll()", "select(match)"):
        assert snippet in shell, snippet
    # The power tree's tables still use the same table (no group: as before).
    assert "Shell.table({" in _page(uv.view_for("get_power_tree"))


# ---------- the ERC view's contract ----------

def _source_has(key: str, *sources: str) -> bool:
    return any(f'"{key}"' in text or f"{key}=" in text for text in sources)


def test_the_erc_contract_names_keys_run_erc_produces():
    contract = _contract(ERC_VIEW)
    assert contract["tool"] == "run_erc"
    sources = (Path(erc.__file__).read_text(encoding="utf-8"),
               (ROOT / "server" / "main.py").read_text(encoding="utf-8"))
    for path, shape in contract["shapes"].items():
        for key in shape["required"] + shape["optional"]:
            # An advisory's capped list says its length in <list>_total.
            capped = key.endswith("_total") and 'f"{key}_total"' in sources[0]
            assert _source_has(key, *sources) or capped, f"{path}: {key}"
    assert contract["paged"] == {"violations": "violations", "single_pin_nets": "nets"}


def test_the_erc_view_reads_the_keys_its_contract_names():
    script = _script(ERC_VIEW)
    for path, shape in _contract(ERC_VIEW)["shapes"].items():
        for key in shape["required"] + shape["optional"]:
            assert re.search(rf'\.{key}\b|"{key}"', script), f"{path}: {key}"


def test_the_erc_view_labels_every_field_a_violation_adds():
    tree = ast.parse(Path(erc.__file__).read_text(encoding="utf-8"))
    extras = {keyword.arg for node in ast.walk(tree)
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
              and node.func.attr == "add" for keyword in node.keywords if keyword.arg}
    extras -= {"sheet", "obj", "message", "location", "net", "matrix_level", "marks", "count"}
    labels = set(re.findall(r'"([a-z_]+)": "', re.search(r"const EXTRA_FIELDS = \{(.*?)\};",
                                                          _script(ERC_VIEW), re.S).group(1)))
    assert extras <= labels | {"note"}, extras - labels
    script = _script(ERC_VIEW)
    matrix = set(re.findall(r'"([^"]+)"', re.search(r"const MATRIX_TYPES = \[(.*?)\];", script, re.S).group(1)))
    assert matrix == set(erc.MATRIX_TYPES)
    levels = re.findall(r'"([^"]+)"', re.search(r"const LEVELS = \[(.*?)\];", script).group(1))
    assert set(levels) == set(erc.LEVELS)  # Fatal Error, Error, Warning, No Report


@pytest.mark.parametrize("parts", ERC_EXAMPLES, ids=[parts[-1] for parts in ERC_EXAMPLES])
def test_the_erc_contract_holds_on_the_public_examples(monkeypatch, parts):
    _example(monkeypatch, *parts)
    for args in ({}, {"min_level": "all"}, {"report": "single_pin_nets"}, {"min_level": "error"}):
        data = _erc_read(**args)
        assert _check(ERC_VIEW, data) == [], args
        rows = data.get("violations", data.get("nets"))
        assert len(rows) == data["total"], args
    first = json.loads(main_mod.run_erc())
    for key in ("summary", "levels", "advisories", "checks_run", "not_checked"):
        assert key in first


@pytest.mark.parametrize("parts", ERC_EXAMPLES, ids=[parts[-1] for parts in ERC_EXAMPLES])
def test_erc_paging_as_the_view_does_gives_every_row_once(monkeypatch, parts):
    path = _example(monkeypatch, *parts)
    # Small pages: run_erc cuts them to fit, the view follows next_offset.
    monkeypatch.setattr(main_mod, "QUERY_NET_MAX_RESULT_SIZE_CHARS", 9000)
    _, model = main_mod._offline_erc(main_mod._project)
    for args in ({}, {"min_level": "all"}):
        data = _erc_read(**args)
        whole = erc.page_erc(model, limit=10 ** 6, **args)["violations"]
        assert data["violations"] == whole, (path.name, args)
        # Each violation once: a drawing row stands for its count.
        counted = sum(row["count"] if "examples" in row else 1 for row in data["violations"])
        if not args:
            assert counted == data["summary"]["reported"]
        else:
            assert counted == data["summary"]["reported"] + sum(
                sum(kind.values()) for kind in data["summary"]["suppressed"].values())
    nets = _erc_read(report="single_pin_nets")["nets"]
    assert nets == erc.page_erc(model, report="single_pin_nets", limit=10 ** 6)["nets"]


def test_suppressed_rows_are_listed_only_with_min_level_all(monkeypatch):
    _example(monkeypatch, *ERC_EXAMPLES[2])
    reported = _erc_read()
    listed = _erc_read(min_level="all")
    suppressed = reported["summary"]["suppressed"]
    assert not any("suppressed" in row for row in reported["violations"])
    marked = Counter()
    for row in listed["violations"]:
        if "suppressed" in row:
            marked[row["suppressed"]] += row["count"] if "examples" in row else 1
    assert marked == {"No Report": sum(suppressed["no_report"].values()),
                      "No ERC": sum(suppressed["no_erc"].values())}
    assert all(row["suppressed_by_no_erc"]["sheet"] for row in listed["violations"]
               if row.get("suppressed") == "No ERC")
    script = _script(ERC_VIEW)
    assert 'min_level: "all", report: "violations"' in script  # the "List them" re-ask


def test_the_mini_pc_example_is_the_empty_state_with_its_summary(monkeypatch):
    _example(monkeypatch, *ERC_EXAMPLES[0])
    data = _erc_read()
    assert data["violations"] == [] and data["summary"]["reported"] == 0
    assert sum(data["summary"]["suppressed"]["no_report"].values()) > 0
    assert 'Shell.state("empty", "No ERC violations"' in _script(ERC_VIEW)


def test_the_erc_views_designators_match_the_objects_run_erc_names(monkeypatch):
    # The view asks get_component for the parts a row names: its Pin U1-5 objects, a
    # Components row's object, "Component U1 has ...": not "Component Designators".
    script = _script(ERC_VIEW)
    assert r"/\bPin ([^\s,()]+)-[^\s,()]+/g" in script
    assert r"/\bComponent ([^\s,()]+) has\b/g" in script
    _example(monkeypatch, *ERC_EXAMPLES[2])
    data = _erc_read(min_level="all")
    netlist = main_mod._altium._netlist
    found = 0
    for row in data["violations"]:
        text = f"{row.get('object', '')} {row['message']}"
        for name in re.findall(r"\bPin ([^\s,()]+)-[^\s,()]+", text):
            found += 1
            assert name in netlist["components"], name
    assert found


def test_the_error_payloads_are_the_tools_own(monkeypatch):
    monkeypatch.setattr(main_mod, "_project", None)
    for name in ("run_erc", "get_drc_report"):
        payload = json.loads(getattr(main_mod, name)())
        assert payload["error"] == "no_project" and payload["message"]
    for view, codes in ((ERC_VIEW, ("no_project", "compile_failed", "unknown_checks")),
                        (DRC_VIEW, ("no_project", "no_pcb_document", "no_drc_report", "rule_not_in_report"))):
        script = _script(view)
        assert "if (first.error) {" in script and "showError(first)" in script
        for code in codes:
            assert f"{code}:" in script, code


# ---------- the DRC view's contract ----------

def test_the_drc_contract_names_keys_get_drc_report_produces():
    contract = _contract(DRC_VIEW)
    assert contract["tool"] == "get_drc_report"
    sources = (Path(drc.__file__).read_text(encoding="utf-8"),
               (ROOT / "server" / "main.py").read_text(encoding="utf-8"))
    for path, shape in contract["shapes"].items():
        for key in shape["required"] + shape["optional"]:
            # _convert_units renames *_mil keys *_mm for units="mm".
            assert _source_has(key.replace("_mm", "_mil"), *sources), f"{path}: {key}"
    assert contract["sections"] == {"health_issues": HEALTH, "waived": WAIVED, "warnings": WARNING}


def test_the_drc_view_reads_the_keys_its_contract_names():
    script = _script(DRC_VIEW)
    for path, shape in _contract(DRC_VIEW)["shapes"].items():
        for key in shape["required"] + shape["optional"]:
            assert re.search(rf'\.{key}\b|"{key}"', script), f"{path}: {key}"


@pytest.mark.parametrize("args", [{}, {"group_by": "component"}, {"group_by": "net"},
                                  {"group_by": "part"}, {"units": "mm"}, {"units": "mil"},
                                  {"rule": "Clearance"}])
def test_the_drc_contract_holds_on_the_fixture_report(board, args):
    first = json.loads(main_mod.get_drc_report(**args))
    assert _check(DRC_VIEW, first) == [], args
    issues = {issue["issue"] for issue in first["assessment"]}
    assert "other_board" in issues  # the fixture was made for another board (Demo)
    units = "mm" if args.get("units", "mm") == "mm" else "mil"  # SL1's display unit is mm
    assert f"origin_{units}" in first
    if "rule" in args:
        assert first["violations"] and all("rule" in row for row in first["violations"])
        return
    read = _drc_read(**args)
    assert _check(DRC_VIEW, {**first, "groups": read["groups"]}) == []
    located = [row for row in read["rows"] if f"location_{units}" in row]
    assert located and all(set(row[f"location_{units}"]) == {f"x_{units}", f"y_{units}"} for row in located)


def test_the_drc_fixture_reads_every_violation_once(board):
    read = _drc_read()
    report = drc.parse_drc_report(board["report"])
    rule_rows = [violation.message for table in report.tables if table.type == "rule"
                 for violation in table.violations]
    assert Counter(row["message"] for row in read["rows"]) == Counter(rule_rows)
    assert len(read["rows"]) == sum(group["count"] for group in read["rule_groups"])
    # The fixture has two rules of one description, one of them empty: read once.
    titles = [group["rule"] for group in read["rule_groups"]]
    assert len(titles) == len(set(titles))
    for entry in read["first"]["health_issues"]:
        assert len(read["sections"][entry["title"]]) == entry["count"]


@pytest.mark.parametrize("trimmed", [False, True], ids=["pages", "trimmed pages"])
def test_drc_paging_as_the_view_does_gives_every_violation_once(board, monkeypatch, trimmed):
    sizes = (60, 31, 12) if trimmed else (300, 150, 40)
    report = _synthetic(board, monkeypatch, stopped=True, sizes=sizes)
    if trimmed:  # get_drc_report trims its pages to 15 or 5 rows to fit
        monkeypatch.setattr(main_mod, "QUERY_NET_MAX_RESULT_SIZE_CHARS", 9000)
    read = _drc_read()
    expected = Counter((table.title, violation.message) for table in report.tables
                       if table.type == "rule" for violation in table.violations)
    assert Counter((row["rule"], row["message"]) for row in read["rows"]) == expected
    assert sum(expected.values()) == sum(sizes) + 5
    # Two rules share a description: two groups, read with one rule= call, 5 rows.
    shared = [group for group in read["rule_groups"] if group["rule"].startswith("Hole To Hole")]
    assert [group["count"] for group in shared] == [3, 2]
    assert sum(1 for row in read["rows"] if row["rule"].startswith("Hole To Hole")) == 5
    # The health section past its first 20 rows, and the waived rows with their waiver.
    assert len(read["sections"]["Unconnected Pins"]) == 25
    health = read["first"]["health_issues"][0]
    assert health["omitted"] == 25 - len(health["examples"]) > 0
    assert all(row["waiver"] for row in read["sections"]["Hole Size Constraint (Max=6mm) (All)"])
    if trimmed:
        assert json.loads(main_mod.get_drc_report(rule=report.tables[0].title))["trimmed_to"] in (15, 5)


def test_the_views_grouping_matches_get_drc_reports(board, monkeypatch):
    # Once every violation is read the view groups them itself, by component or net the
    # way get_drc_report does: the same groups with the same counts.
    _synthetic(board, monkeypatch)
    rows = _drc_read()["rows"]
    for grouping in ("component", "net"):
        _, groups = _drc_pages({"group_by": grouping}, "groups")
        local = Counter()
        for row in rows:
            for key in row.get(f"{grouping}s") or [f"(no {grouping})"]:
                local[key] += 1
        assert local == {group[grouping]: group["count"] for group in groups}, grouping
    script = _script(DRC_VIEW)
    assert 'const NO_KEY = { component: "(no component)", net: "(no net)" };' in script
    assert "return list.length ? list : [NO_KEY[by]];" in script


def test_a_stopped_and_a_stale_report_are_assessed_first(board, monkeypatch):
    _synthetic(board, monkeypatch, stopped=True, stale=True)
    first = json.loads(main_mod.get_drc_report())
    issues = {issue["issue"]: issue for issue in first["assessment"]}
    assert issues["violation_limit_reached"]["severity"] == "error"
    assert issues["older_than_board"]["severity"] == "warning"
    assert _check(DRC_VIEW, first) == []
    script = _script(DRC_VIEW)
    # The assessment is drawn before the tabs, the worst severity giving the banner.
    assert "headBlock(),\n      assessmentBlock()," in script
    assert 'worst >= 2 ? "error" : worst === 1 ? "warning" : "ok"' in script


def test_a_clean_report_is_the_empty_state(board, monkeypatch):
    clean = _report(_table("Short-Circuit Constraint (Allowed=No) (All),(All)"),
                    _table("Un-Routed Net Constraint ( (All) )"))
    monkeypatch.setattr(main_mod, "load_report", lambda project, pcb_path: (
        clean, board["report"], [], [], []))
    first = json.loads(main_mod.get_drc_report())
    assert first["groups"] == [] and first["totals"]["rule_violations"] == 0
    assert _check(DRC_VIEW, first) == []
    assert 'Shell.state("empty", "No rule violations in this report"' in _script(DRC_VIEW)


def test_no_report_and_no_board_are_error_payloads(board, monkeypatch):
    board["report"].unlink()
    missing = json.loads(main_mod.get_drc_report())
    assert missing["error"] == "no_drc_report" and missing["searched"]
    project = {**main_mod._project, "pcb_doc_paths": []}
    monkeypatch.setattr(main_mod, "_project", project)
    assert json.loads(main_mod.get_drc_report())["error"] == "no_pcb_document"
    script = _script(DRC_VIEW)
    assert "payload.searched" in script and "payload.rules" in script


def test_the_drc_view_asks_the_board_in_the_units_of_the_location(board):
    script = _script(DRC_VIEW)
    assert '["query_pcb_region", { x: row.location.x, y: row.location.y, units: view.units }' in script
    first = json.loads(main_mod.get_drc_report(units="mil"))
    example = next(example for group in first["groups"] for example in group["examples"]
                   if "location_mil" in example)
    location = example["location_mil"]
    region = json.loads(main_mod.query_pcb_region(x=location["x_mil"], y=location["y_mil"], units="mil"))
    assert "error" not in region and region["x_mil"] == location["x_mil"]


# ---------- a later page, narrowed calls and what the tools cannot answer ----------

def test_a_later_page_as_the_result_is_read_again_from_offset_0(board, monkeypatch):
    # get_drc_report(offset=1) leaves out the report's first group; run_erc(offset=50) its
    # first rows and its summary: both views ask for offset 0 when they can, and say what
    # is missing when they cannot (the summary's counts as not known, not 0).
    whole = json.loads(main_mod.get_drc_report())
    later = json.loads(main_mod.get_drc_report(offset=1))
    assert later["offset"] == 1 and later["groups"] == whole["groups"][1:]
    assert later["total"] == whole["total"]  # the views' check that a page belongs to the result
    listing = json.loads(main_mod.get_drc_report(rule="Clearance", offset=1))
    assert listing["violations"] == json.loads(main_mod.get_drc_report(rule="Clearance"))["violations"][1:]
    drc_script = _script(DRC_VIEW)
    assert "if (first.offset > 0) {" in drc_script
    assert "start = await getPage({ ...own, offset: 0 }, first.total);" in drc_script
    assert "it starts at offset ${first.offset}" in drc_script
    erc_script = _script(ERC_VIEW)
    assert "firstPage = await getPage(0, baseArgs(), first.total);" in erc_script
    assert "this result starts at offset ${first.offset}" in erc_script
    assert 'label: `Suppressed (${known ? suppressedTotals()' in erc_script and ': "?"})`' in erc_script
    for script in (drc_script, erc_script):
        assert "if (total !== null && page.total !== total) {" in script


def test_a_narrowed_erc_call_keeps_the_whole_projects_summary(monkeypatch):
    # run_erc's summary counts the whole project whatever checks, sheet or min_level
    # narrow the rows: the view counts its header from the rows and marks the summary.
    _example(monkeypatch, *ERC_EXAMPLES[2])
    narrowed = _erc_read(min_level="error")
    listed = sum(row["count"] if "examples" in row else 1 for row in narrowed["violations"])
    assert listed < narrowed["summary"]["reported"]
    assert narrowed["summary"] == _erc_read()["summary"]
    script = _script(ERC_VIEW)
    assert "function narrowed()" in script and "whole project: ${summary.reported} reported" in script
    assert 'Suppressed${whole ? " in the whole project" : ""}' in script
    # "List them" keeps the first call's floor in the Level chips and says so.
    assert 'view.keepFloor = floor === "error" || floor === "fatal" ? floor : null;' in script
    assert "The Level chips keep the first call's floor" in script


def test_single_pin_nets_get_no_query_net_button(monkeypatch):
    # The netlist leaves out nets with a single pin, so query_net never finds them.
    _example(monkeypatch, *ERC_EXAMPLES[1])
    nets = _erc_read(report="single_pin_nets")["nets"]
    assert nets
    for row in nets[:20]:
        answer = json.loads(main_mod.query_net(pattern="^" + re.escape(row["net"]) + "$"))
        assert answer.get("error") or not (answer.get("nets") or answer.get("net")), row["net"]
    script = _script(ERC_VIEW)
    types_ = re.findall(r'"([^"]+)"', re.search(r"const SINGLE_PIN_TYPES = \[(.*?)\];", script).group(1))
    assert set(types_) <= {name for name in json.loads(main_mod.run_erc())["checks_run"]}
    assert "if (net && !singlePin(row)) calls.push" in script


def test_drc_sections_of_one_title_are_read_once(board, monkeypatch):
    # Two waived tables of one title (two rules sharing a description): rule=<title> lists
    # the rows of both, so the view keeps one entry per title with the counts summed.
    report = _synthetic(board, monkeypatch)
    title = "Hole To Hole Clearance (Gap=0.25mm) (All),(All)"
    for count in (25, 3):
        report.tables.append(DrcTable(type=WAIVED, title=title, count=count, violations=[
            DrcViolation(f"Hole To Hole Clearance: (0.1mm < 0.25mm) Pad H{i}-1({i}mm,1mm) on Multi-Layer",
                         waiver=f"waiver {count}") for i in range(count)]))
    first = json.loads(main_mod.get_drc_report())
    assert [entry["count"] for entry in first["waived"] if entry["title"] == title] == [25, 3]
    _, listed = _drc_pages({"rule": title}, "violations")
    assert sum(1 for row in listed if row.get("section") == WAIVED) == 28
    script = _script(DRC_VIEW)
    assert "function sectionEntries(first)" in script and "entry.count += Number(table.count) || 0;" in script
    # The sections are read in full in a rule= listing too, and a stop names them.
    assert "await readSections(budget, stop, progress, complete ? rows : null);" in script
    assert 'stop(`the ${SECTION_LABEL[section]}s of "${entry.title}"`, read.stopped || "no rows came back");' in script


def test_a_rule_the_report_counts_but_does_not_list_is_named(board, monkeypatch):
    report = _synthetic(board, monkeypatch)
    report.tables.append(_table("Silk to Silk (Clearance=0.1mm) (All),(All)", 7, []))
    groups = json.loads(main_mod.get_drc_report())["groups"]
    silk = next(group for group in groups if group["rule"].startswith("Silk to Silk"))
    assert silk["count"] == 0 and silk["reported_count"] == 7
    script = _script(DRC_VIEW)
    assert "const unlisted = view.rules.filter((rule) => !rule.count && rule.reported);" in script
    # No rule= call for it, and the rows of a rule read before a stop are kept.
    assert "view.rules.filter((rule) => rule.count > 0)" in script
    assert "entry.partial = true;" in script


def test_the_drc_views_follow_up_calls_use_the_results_own_units(board):
    # The location keys say the units, so a follow-up call answers in them whatever the
    # host sent as arguments; the grouping comes from the groups' own keys.
    for units in ("mm", "mil"):
        first = json.loads(main_mod.get_drc_report(units=units))
        assert f"origin_{units}" in first
        again = json.loads(main_mod.get_drc_report(units=units, rule=first["groups"][0]["rule"]))
        assert all(f"location_{units}" in row for row in again["violations"] if any(k.startswith("location_") for k in row))
    script = _script(DRC_VIEW)
    assert "return { units: view.units };" in script
    assert "return { ...unitArgs(), group_by: groupingOf(first) };" in script


# ---------- the shell: safety, focus, accessibility, layout ----------

def _hex_luminance(color: str) -> float:
    channels = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(a: str, b: str) -> float:
    high, low = sorted((_hex_luminance(a), _hex_luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def test_tertiary_text_meets_wcag_aa_on_every_background():
    css = (UI / "shell.css").read_text(encoding="utf-8")
    blocks = [re.search(r":root \{(.*?)\}", css, re.S).group(1),
              re.search(r':root\[data-theme="dark"\] \{(.*?)\}', css, re.S).group(1)]
    for block in blocks:
        tokens = dict(re.findall(r"--(fb-[a-z0-9-]+): (#[0-9a-f]{6});", block))
        for background in ("fb-bg", "fb-bg2", "fb-bg3", "fb-info-bg"):
            assert _contrast(tokens["fb-text3"], tokens[background]) >= 4.5, background
    assert "opacity: .75" not in css  # a switched-off chip keeps its contrast


def test_the_shell_refuses_style_values_that_could_load_anything():
    shell = (UI / "shell.js").read_text(encoding="utf-8")
    allowed = set(re.findall(r'"([a-z-]*)"', re.search(r"const STYLE_FUNCTIONS = new Set\(\[(.*?)\]\);",
                                                       shell, re.S).group(1)))
    assert allowed == {"", "rgb", "rgba", "hsl", "hsla", "hwb", "lab", "lch", "oklab", "oklch", "color",
                       "color-mix", "light-dark", "calc", "min", "max", "clamp", "var"}
    # A backslash (a CSS escape can spell any function), an at-rule or a block is refused.
    assert r"if (/[\\@;{}<>]/.test(text)) return false;" in shell
    assert "else if (safeStyleValue(value)) root.style.setProperty(name, String(value));" in shell


def test_the_shell_sends_nothing_after_teardown():
    shell = (UI / "shell.js").read_text(encoding="utf-8")
    assert 'if (tornDown) return Promise.reject(new Error("The view was closed."));' in shell
    assert "tornDown = true;\n        await emit(\"teardown\"" in shell
    assert 'if (closed() || tornDown) stopped = "the view was closed";' in shell


def test_the_shell_table_is_a_grid_with_focus_kept_and_counted_rows():
    shell = (UI / "shell.js").read_text(encoding="utf-8")
    for snippet in ('const role = grouping ? "treegrid" : onActivate ? "grid" : null;', '"aria-level": "1"',
                    '"aria-level": grouping ? "2" : null', 'panel.setAttribute("role", "tabpanel");',
                    '"aria-controls": panel ? panel.id : null', "focus(match) {", "function announce(text)",
                    'plural(data.length, "row")', "chooser.open = false;", "function neighbour(node)"):
        assert snippet in shell, snippet
    for view in (ERC_VIEW, DRC_VIEW):
        script = _script(view)
        # Closing the details gives focus back to the row; the pane is not a live region.
        assert "function closeDetails()" in script and 'text: "Close", on: { click: closeDetails }' in script
        assert '"aria-live"' not in script and "Shell.announce(" in script
        assert "Shell.focusMark(app)" in script and "Shell.refocus(app, mark" in script
    css = (UI / "shell.css").read_text(encoding="utf-8")
    for rule in (".tbl td.c-msg { min-width: 14em;", ".notice .grow { flex: 1 1 auto; min-width: 0; overflow-wrap: anywhere; }",
                 ".banner li { margin: 4px 0; color: var(--c-text); overflow-wrap: anywhere; }",
                 ".split > .pane.idle { display: none; }", ".sr-only {"):
        assert rule in css, rule


# ---------- docs ----------

def test_readme_and_changelog_describe_the_violation_views():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    section = readme.split("## Interactive views", 1)[1].split("\n## ", 1)[0]
    for tool in ("run_erc", "get_drc_report"):
        assert f"| `{tool}` |" in section, tool
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    unreleased = changelog.split("## [Unreleased]", 1)[1].split("\n## [", 1)[0]
    assert "roadmap #60" in unreleased
