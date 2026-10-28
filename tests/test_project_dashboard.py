"""get_project_dashboard (roadmap #74, services/project_dashboard.py): the catalogue of
cards, each card's counts against its tool's own totals on Altium's public examples, the
honesty of its states (synthetic payloads), the run order and time budget (a fake clock
and fake tools), the cache and its staleness, the size of the answer, unchanged tool
results and server state, and the protocol."""

import asyncio
import contextlib
import json
import os
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from mcp import types

import server.main as main
import server.services.board_view as board_view
import server.services.ui_views as uv

pd = main.project_dashboard  # the module main uses (its constants are what the tool reads)
ROOT = Path(__file__).parents[1]
EXAMPLES = Path(r"C:\Users\Public\Documents\Altium\AD25\Examples")
PUBLIC = {
    "SL1": EXAMPLES / "SpiritLevel-SL1" / "SL1 Xilinx Spartan-IIE PQ208 Rev1.02.PrjPcb",
    "Sentinel": EXAMPLES / "Bluetooth Sentinel" / "Bluetooth_Sentinel.PrjPcb",
    "Harness A": EXAMPLES / "Harness Demo MB" / "Harness_Demo_PCB_A" / "Harness_Demo_PCB_A.PrjPcb",
}
MINIPC = EXAMPLES / "Mini PC" / "Mini PC - Motherboard" / "MiniPC.PrjPcb"
DRC_FIXTURE = Path(__file__).parent / "fixtures" / "drc" / "Design Rule Check - Demo.html"
SEVERITIES = ("error", "warning", "info")


def _forbidden(*args, **kwargs):
    raise AssertionError("the dashboard must not call this")


@contextlib.contextmanager
def isolated_main():
    """main's state reset (a fresh dashboard cache among it); Altium and the registry are
    forbidden, and no installed library is read."""
    client = main.AltiumClient()
    with patch.object(main, "_project", None), \
            patch.object(main, "_variant_state", None), \
            patch.object(main, "_netlist_last_updated", None), \
            patch.object(main, "_pcb_session", main.PcbSession()), \
            patch.object(main, "_design_notes", main.DesignNotesCache()), \
            patch.object(main, "_installed_library_paths", []), \
            patch.object(main, "_erc_cache", None), \
            patch.object(main, "_review_cache", {}), \
            patch.object(main, "_custom_cache", {}), \
            patch.object(main, "_dashboard_cache", pd.DashboardCache(main.DASHBOARD_CACHE_SIZE)), \
            patch.object(main, "_altium", client), \
            patch.object(client, "get_status", side_effect=_forbidden), \
            patch.object(main, "upsert_registry_entry", side_effect=_forbidden), \
            patch.object(main, "_write_state", side_effect=_forbidden):
        yield


def _load(path: Path) -> None:
    """Load a project offline (the check command's headless load: no Altium, no registry)."""
    response = main.load_project_headless(str(path))
    assert response.get("loaded"), response


def _need(path: Path) -> Path:
    if not path.is_file():
        pytest.skip(f"{path.name} is not installed")
    return path


def _handle(request):
    handler = main.mcp._mcp_server.request_handlers[type(request)]
    result = asyncio.run(handler(request))
    return result.model_dump(mode="json", by_alias=True, exclude_none=True)


def _tools() -> dict:
    return {tool["name"]: tool
            for tool in _handle(types.ListToolsRequest(method="tools/list"))["tools"]}


# ---------- (1) the catalogue ----------

def test_every_check_tool_is_a_card_or_excluded_with_a_reason():
    registered = {tool.name for tool in asyncio.run(main.mcp.list_tools())}
    cards = set(pd.CARD_TOOLS)
    assert len(pd.CATALOGUE) == len(cards) == 33
    wanted = {name for name in registered if name.startswith("check_")}
    wanted |= {"run_erc", "get_drc_report", "get_power_tree", "get_output_jobs", "verify_outputs",
               "run_custom_checks", "get_bom", "compare_variants"}
    for name in wanted:
        assert name in cards or pd.EXCLUDED.get(name), name
    assert not cards & set(pd.EXCLUDED)
    assert set(pd.EXCLUDED) <= registered and cards <= registered
    assert all(reason.strip() for reason in pd.EXCLUDED.values())


def test_views_board_flags_and_read_only_tools():
    tools = _tools()
    view_tools = {view.tool for view in uv.VIEWS}
    for spec in pd.CATALOGUE:
        if spec.view:
            assert spec.view in view_tools, spec.tool
        assert spec.board == (spec.tool in board_view.FINDING_SOURCES), spec.tool
        assert tools[spec.tool]["annotations"]["readOnlyHint"] is True, spec.tool
        assert spec.group in dict(pd.GROUPS) and spec.title and spec.noun
    assert {spec.tool for spec in pd.CATALOGUE if spec.view} == {
        "run_erc", "get_power_tree", "check_bom", "get_drc_report"}
    for name in (pd.RELEASE_TOOL, *pd.FACT_TOOLS, "get_bom", "get_board_view", "compare_variants",
                 "project_tour", "release_review"):
        assert tools[name]["annotations"]["readOnlyHint"] is True, name
    # Every card tool is one the checklist runner may call (read-only, deterministic).
    assert set(pd.RUN_ORDER) <= set(main._review_tools())


def test_the_run_order_and_the_slow_cards():
    assert pd.RUN_ORDER[:3] == ("check_release_readiness", "get_board_info", "list_variants")
    assert sorted(pd.ORDER) == sorted(pd.CARD_TOOLS) and len(set(pd.ORDER)) == 33
    assert pd.SLOW == {"check_thermal", "check_power_paths", "check_diff_pair_geometry",
                       "check_rule_compliance", "check_dfm", "check_return_path"}
    # The slow cards come last, cheapest first.
    assert pd.ORDER[-6:] == ("check_thermal", "check_power_paths", "check_diff_pair_geometry",
                             "check_rule_compliance", "check_dfm", "check_return_path")
    assert (pd.BUDGET_S, pd.SLOW_START_S, pd.MAX_RESULT_CHARS) == (20.0, 10.0, 16_000)
    assert main.DASHBOARD_CACHE_SIZE == 4


# ---------- (2) counts on Altium's public examples ----------

def _own_totals(tool: str, data: dict) -> dict:
    """The tool's own totals, read as each tool documents them."""
    summary = data.get("summary") or {}
    if tool == "run_erc":
        by_level = summary["by_level"]
        info = data.get("advisories_total")
        return {"error": by_level.get("Fatal Error", 0) + by_level.get("Error", 0),
                "warning": by_level.get("Warning", 0),
                "info": info if isinstance(info, int) else len(data.get("advisories") or [])}
    if tool == "get_drc_report":
        totals = data["totals"]
        return {"error": totals["rule_violations"],
                "warning": totals.get("health_issues", 0) + totals.get("warnings", 0),
                "info": totals.get("waived", 0)}
    if tool == "check_rule_compliance":
        return {"error": data["violation_count"], "warning": 0, "info": 0}
    if tool == "check_lengths":
        # A rule not evaluated may carry no summary, or one without violations.
        rules = data["rules"]
        summaries = [rule.get("summary") or {} for rule in rules]
        return {"error": sum(summary.get("violations") or 0 for summary in summaries),
                "warning": 0,
                "info": sum(1 for rule, summary in zip(rules, summaries) if rule["status"] != "evaluated"
                            or summary.get("incomplete") or summary.get("unknown"))}
    if tool == "check_fab_drawing":
        return {level: summary[level] for level in SEVERITIES}
    levels = summary["findings"] if tool == "check_multiboard" else \
        data["severity_counts"] if "severity_counts" in data else summary["severity_counts"]
    assert set(levels) <= set(SEVERITIES), (tool, sorted(levels))
    return {level: levels.get(level, 0) for level in SEVERITIES}


@pytest.fixture(scope="module", params=sorted(PUBLIC))
def public(request):
    path = _need(PUBLIC[request.param])
    with isolated_main():
        _load(path)
        dashboard = json.loads(main.get_project_dashboard())
        yield request.param, dashboard


def test_each_card_counts_the_tools_own_totals(public):
    name, dashboard = public
    tools = main._review_tools()
    assert len(dashboard["cards"]) == 33 and dashboard["not_run"]["checks"] == []
    ran = 0
    for card in dashboard["cards"]:
        data = json.loads(tools[card["tool"]]())
        if "error" in data:
            assert "counts" not in card, (name, card["tool"])
            assert card["state"] in ("not_applicable", "not_verified", "unavailable"), (name, card["tool"])
            continue
        ran += 1
        assert card["counts"] == _own_totals(card["tool"], data), (name, card["tool"])
        assert isinstance(card["subject"]["count"], int), (name, card["tool"])
        assert card["state"] in pd.STATES and card["state"] not in ("failed", "unreadable")
    assert ran >= 12


def test_findings_are_worst_first_at_most_20_and_say_when_page_1_lacks_the_worst(public):
    name, dashboard = public
    rank = {level: index for index, level in enumerate(SEVERITIES)}
    for tool in pd.CARD_TOOLS:
        answer = json.loads(main.get_project_dashboard(checks=[tool]))
        (card,) = answer["cards"]
        assert card["current"], (name, tool)
        rows = card.get("findings") or []
        assert len(rows) <= 20
        assert [rank[row["sev"]] for row in rows] == sorted(rank[row["sev"]] for row in rows)
        counts = card.get("counts")
        if counts:
            worst = next((level for level in SEVERITIES if counts[level]), None)
            if worst and not any(row["sev"] == worst for row in rows) and card.get("findings_total", 0) <= 20:
                assert card["first_note"].startswith("page 1 shows none of the"), (name, tool)


def test_most_cards_with_counts_list_a_finding(public):
    name, dashboard = public
    with_counts = [card for card in dashboard["cards"] if any((card.get("counts") or {}).values())]
    listed = [card for card in with_counts if card.get("findings")]
    assert len(listed) >= 0.9 * len(with_counts), (name, len(listed), len(with_counts))
    # The catalogue's facts are the view's, not repeated per card.
    for card in dashboard["cards"]:
        assert not {"title", "group", "view", "board", "slow"} & set(card), card["tool"]


def test_the_erc_card_counts_rows_and_objects_as_run_erc_does(public):
    name, dashboard = public
    erc = json.loads(main.run_erc())
    if "error" in erc:
        pytest.skip("run_erc does not run here")
    answer = json.loads(main.get_project_dashboard(checks=["run_erc"]))
    (card,) = answer["cards"]
    advisories = erc.get("advisories_total", len(erc.get("advisories") or []))
    assert card.get("findings_total", 0) == erc["total"] + advisories, name
    by_violation = sorted(erc["summary"]["by_violation"], key=lambda row: -row["count"])
    assert card.get("ids", []) == [[row["violation"], row["count"]] for row in by_violation[:3]], name


def test_the_overview_and_release_come_from_their_tools(public):
    name, dashboard = public
    release = json.loads(main.check_release_readiness())
    assert dashboard["release"]["verdict"] == release["verdict"]
    assert dashboard["release"]["counts"] == release["counts"]
    assert [row[0] for row in dashboard["release"]["gates"]] == [row["id"] for row in release["gates"]]
    assert [row["id"] for row in dashboard["release"]["blocking_failed"]] == release["blocking_failed"]
    overview = dashboard["overview"]
    assert overview["sheets"] == len(main._project["sheets"])
    assert overview["variants"]["active"] == main._variant_state.active.name
    info = json.loads(main.get_board_info(units="mm"))
    if "error" in info:
        assert "board" not in overview and overview["notes"]
    else:
        assert overview["board"]["nets"] == info["counts"]["nets"]
        assert overview["board"]["copper_layers"] == sum(1 for row in info["stackup"] if row["kind"] == "copper")
    # The ERC never reads clean; the netlist-free gate is read from the release.
    erc = next(card for card in dashboard["cards"] if card["tool"] == "run_erc")
    assert erc["state"] != "clean" and "offline_approximation" in erc["tags"]
    fresh = next(row for row in release["gates"] if row["id"] == "netlist_fresh")
    assert dashboard["state"]["netlist_older_than_sheets"] == (fresh["status"] in ("fail", "warn"))


# ---------- (3) honesty: synthetic payloads ----------

def _spec(tool: str):
    return pd.SPECS[tool]


VALID = {
    "sc": ("check_vias", {"severity_counts": {"info": 2}, "summary": {"vias": 10},
                          "findings": [{"severity": "info", "check": "x", "issue": "a b"}]}),
    "summary_sc": ("check_return_path", {"summary": {"severity_counts": {"warning": 1},
                                                     "segments_checked": 4},
                                         "findings": [{"severity": "warning", "check": "y", "issue": "c d"}]}),
    "summary_levels": ("check_fab_drawing", {"summary": {"error": 1, "warning": 0, "info": 0},
                                             "drawings_total": 1}),
    "summary_findings": ("check_multiboard", {"summary": {"findings": {"error": 0, "warning": 1, "info": 0},
                                                          "pin_pairs": 3}}),
    "erc": ("run_erc", {"summary": {"by_level": {"Fatal Error": 1, "Error": 1, "Warning": 2}},
                        "checks_run": ["a"], "advisories": [{"advisory": "z", "level": "info", "message": "e f"}]}),
    "drc": ("get_drc_report", {"totals": {"rule_violations": 3, "health_issues": 1, "warnings": 1, "waived": 2},
                               "assessment": [], "rules_checked": 5,
                               "groups": [{"rule": "Clearance", "rule_kind": "Clearance", "count": 3}]}),
    "compliance": ("check_rule_compliance", {"violation_count": 2, "kinds": [
        {"kind": "Width", "violation_count": 2, "violation_groups": 1}]}),
    "lengths": ("check_lengths", {"rule_count": 2, "rules": [
        {"name": "L1", "status": "evaluated", "summary": {"violations": 2}},
        {"name": "L2", "status": "no_members", "summary": {"violations": 0}}]}),
}
REQUIRED = {
    "sc": ["severity_counts"], "summary_sc": ["summary.severity_counts"],
    "summary_levels": ["summary.error", "summary.warning", "summary.info"],
    "summary_findings": ["summary.findings"], "erc": ["summary.by_level"],
    "drc": ["totals", "totals.rule_violations", "assessment"],
    "compliance": ["violation_count", "kinds"], "lengths": ["rules", "rule_count"],
}


def _without(data: dict, path: str) -> dict:
    data = json.loads(json.dumps(data))
    *parents, last = path.split(".")
    node = data
    for key in parents:
        node = node[key]
    del node[last]
    return data


@pytest.mark.parametrize("rule", sorted(VALID))
def test_a_missing_required_key_makes_the_card_unreadable(rule):
    tool, data = VALID[rule]
    spec = _spec(tool)
    assert spec.rule == rule
    card = pd.normalise(spec, {"data": data})
    assert card["state"] in ("errors", "warnings", "notes"), card
    for path in REQUIRED[rule]:
        broken = pd.normalise(spec, {"data": _without(data, path)})
        assert broken["state"] == "unreadable", (rule, path)
        assert f"ask Claude to run {tool}" in broken["reason"]


def test_counts_per_rule():
    expect = {
        "sc": {"error": 0, "warning": 0, "info": 2},
        "summary_sc": {"error": 0, "warning": 1, "info": 0},
        "summary_levels": {"error": 1, "warning": 0, "info": 0},
        "summary_findings": {"error": 0, "warning": 1, "info": 0},
        "erc": {"error": 2, "warning": 2, "info": 1},
        "drc": {"error": 3, "warning": 2, "info": 2},
        "compliance": {"error": 2, "warning": 0, "info": 0},
        "lengths": {"error": 2, "warning": 0, "info": 1},
    }
    for rule, (tool, data) in VALID.items():
        assert pd.normalise(_spec(tool), {"data": data})["counts"] == expect[rule], rule
    assert pd.severity("Fatal Error") == pd.severity("fail") == "error"
    assert pd.severity("Warn") == "warning" and pd.severity(None) == "info"
    compliance = pd.normalise(_spec("check_rule_compliance"), {"data": VALID["compliance"][1]})
    assert compliance["findings"] == [{"sev": "error", "check": "Width", "text": "2 violations in 1 group"}]


def _present(card: dict, key: str = "k", netlist_stale: bool = False, tool: str = "check_vias") -> dict:
    parts = {name: "p" for name, _ in pd.PARTS}
    stored = {**card, "key": card.get("key", "k"), "parts": parts, "as_of": "t", "ms": 1}
    return pd.present(_spec(tool), stored, key, parts, netlist_stale, main.NETLIST_TOOLS)


def test_clean_needs_everything():
    base = {"severity_counts": {"error": 0}, "summary": {"vias": 12}, "findings": []}
    clean = pd.normalise(_spec("check_vias"), {"data": base})
    assert clean["state"] == "clean" and _present(clean)["state"] == "clean"
    # Something left unchecked, nothing examined, or what was examined unknown.
    assert pd.normalise(_spec("check_vias"), {"data": {**base, "not_checked": ["x"]}})["state"] == "partial"
    assert pd.normalise(_spec("check_vias"), {"data": {**base, "summary": {"vias": 0}}})["state"] == "nothing_to_check"
    unknown = pd.normalise(_spec("check_vias"), {"data": {**base, "summary": {}}})
    assert unknown["state"] == "partial" and "unknown" in unknown["reason"]
    # An old key: never clean (it was clean before something changed).
    old = _present(clean, key="other")
    assert old["state"] == "stale" and old["was_state"] == "clean" and not old["current"]
    # A netlist tool while the netlist is older than the sheets.
    decoupling = pd.normalise(_spec("check_decoupling"), {"data": {
        "severity_counts": {}, "rails_total": 3, "findings": []}})
    assert decoupling["state"] == "clean"
    stale_netlist = _present(decoupling, netlist_stale=True, tool="check_decoupling")
    assert stale_netlist["state"] == "partial" and "netlist_older_than_sheets" in stale_netlist["tags"]
    assert _present(clean, netlist_stale=True)["state"] == "clean"  # a PCB tool reads no netlist
    # A limiting tag.
    erc = pd.normalise(_spec("run_erc"), {"data": {"summary": {"by_level": {}}, "checks_run": ["a"]}})
    assert erc["state"] == "partial" and erc["tags"] == ["offline_approximation"]
    untrusted = pd.normalise(_spec("get_drc_report"), {"data": {
        "totals": {"rule_violations": 0}, "rules_checked": 9,
        "assessment": [{"issue": "older_than_board", "severity": "warning", "detail": "x"}]}})
    assert untrusted["state"] == "not_verified" and "report_untrusted" in untrusted["tags"]


JUDGING = {  # payloads of the checks that need an input the files may not give
    "check_derating": lambda known: {
        "severity_counts": {}, "findings": [],
        "summary": {"parts_checked": {"capacitors": 3, "resistors": 2, "leds": 0},
                    "capacitors": {"applied_voltage_known": known},
                    "resistors": {"voltage_across_known": 0}}},
    "check_logic_levels": lambda known: {
        "severity_counts": {}, "summary": {"signals": {"checked": 4, "judged": known}}},
    "check_thermal": lambda known: {
        "severity_counts": {}, "parts_total": 2,
        "summary": {"dissipation": {"computed": known, "given": 0, "needs_inputs": 2 - known}}},
    "check_power_paths": lambda known: {
        "summary": {"severity_counts": {}, "rails": {"analysed": 3, "by_status": (
            {"estimated": known, "no_loads": 3 - known} if known else {"no_loads": 3})}}},
    "run_custom_checks": lambda known: {
        "severity_counts": {}, "rules": [{"id": "A-1"}, {"id": "A-2"}],
        "verdict": "pass" if known else "incomplete",
        "summary": {"rules": {"pass": known, "fail": 0, "not_applicable": 2 - known if known else 0,
                              "could_not_evaluate": 0 if known else 2}}},
}


def test_an_incomplete_verdict_is_never_clean():
    """run_custom_checks says incomplete when a rule could not be evaluated or left objects
    not evaluated, even when other rules passed: the card is partial, as the tool says."""
    data = JUDGING["run_custom_checks"](1) | {"verdict": "incomplete"}
    card = pd.normalise(_spec("run_custom_checks"), {"data": data})
    assert card["state"] == "partial" and "verdict is incomplete" in card["reason"]


def test_a_pack_whose_rules_could_not_run_is_not_clean(tmp_path):
    """The repo's example pack on a project loaded without a netlist: every rule says
    could_not_evaluate and the tool's verdict is incomplete."""
    from tests.test_netlist_free_load import _write_project
    prj = _write_project(tmp_path / "Board")
    shutil.copy(ROOT / "examples" / "check_packs.json", prj.parent / "check_packs.json")
    with isolated_main(), patch.object(main._altium, "compile_offline",
                                       side_effect=RuntimeError("sheets unreadable")):
        _load(prj)
        tool = json.loads(main.run_custom_checks())
        (card,) = json.loads(main.get_project_dashboard(checks=["run_custom_checks"]))["cards"]
    assert tool["verdict"] == "incomplete" and not any(tool["severity_counts"].values())
    assert card["state"] == "partial" and card["judged"] == 0, card


@pytest.mark.parametrize("tool", sorted(JUDGING))
def test_a_check_that_judged_nothing_is_never_clean(tool):
    assert set(JUDGING) == set(pd.JUDGED)
    spec = _spec(tool)
    nothing = pd.normalise(spec, {"data": JUDGING[tool](0)})
    assert nothing["state"] == "partial" and nothing["judged"] == 0, nothing
    assert "could be judged" in nothing["reason"] and pd.JUDGED[tool][1] in nothing["reason"]
    assert _present(nothing, tool=tool)["state"] == "partial"
    some = pd.normalise(spec, {"data": JUDGING[tool](1)})
    assert some["state"] == "clean" and some["judged"] == 1 and some["subject"] > 1, some
    # A result that does not say what it judged is not clean either.
    unknown = pd.normalise(spec, {"data": {key: value for key, value in JUDGING[tool](1).items()
                                           if key != "summary"} | (
        {"summary": {"severity_counts": {}}} if spec.rule == "summary_sc" else {})})
    assert unknown["state"] != "clean", unknown


def test_the_public_cards_that_judged_nothing_are_not_clean(public):
    name, dashboard = public
    cards = _cards(dashboard)
    tools = main._review_tools()
    for tool in pd.JUDGED:
        card = cards[tool]
        data = json.loads(tools[tool]())
        if "error" in data:
            continue
        judged = pd.judged_of(pd.SPECS[tool], data)
        if judged == 0:
            assert card["state"] != "clean", (name, tool)
        if card["state"] == "clean":
            assert judged, (name, tool)
    # Harness A's ICs share no signal: nothing to check, not clean.
    if name == "Harness A":
        assert cards["check_logic_levels"]["state"] == "nothing_to_check"


def test_a_board_with_no_known_voltage_has_no_clean_derating():
    path = _need(EXAMPLES / "Mini PC" / "Mini PC - SODIMM" / "PC4-SODIMM_V200_RC_A1_20141015.PrjPcb")
    with isolated_main():
        _load(path)
        answer = json.loads(main.get_project_dashboard(checks=["check_derating", "check_logic_levels"]))
    for card in answer["cards"]:
        assert card["state"] != "clean", card["tool"]
    derating = _cards(answer)["check_derating"]
    assert derating["state"] == "partial" and derating["judged"] == 0


def test_run_erc_is_never_clean():
    for by_level in ({}, {"Error": 0, "Warning": 0}):
        card = pd.normalise(_spec("run_erc"), {"data": {"summary": {"by_level": by_level},
                                                        "checks_run": ["a", "b"], "advisories": []}})
        assert card["state"] != "clean" and _present(card, tool="run_erc")["state"] != "clean"


def test_error_codes_and_exceptions():
    spec = _spec("get_drc_report")
    assert pd.normalise(spec, {"error": "no_drc_report", "message": "x"})["state"] == "not_verified"
    assert "run the Batch DRC in Altium" in pd.normalise(spec, {"error": "no_drc_report"})["reason"]
    for code in pd.NOT_APPLICABLE:
        assert pd.normalise(_spec("check_vias"), {"error": code, "message": "m"})["state"] == "not_applicable"
    unavailable = pd.normalise(_spec("check_bom"), {"error": "netlist_required", "message": "m"})
    assert unavailable["state"] == "unavailable" and "refresh_netlist" in unavailable["reason"]
    failed = pd.normalise(_spec("check_vias"), {"error": "tool_failed", "exception": "KeyError",
                                                "message": "check_vias failed: KeyError: 'x'"})
    assert failed["state"] == "failed" and failed["reason"].startswith("KeyError")
    assert pd.normalise(_spec("check_vias"), {"error": "not_json"})["state"] == "failed"
    assert pd.normalise(_spec("check_vias"), {"error": "surprise", "message": "m"})["state"] == "failed"


def test_a_drc_report_for_another_board_is_errors_and_untrusted(tmp_path):
    source = _need(PUBLIC["SL1"]).parent
    copy = tmp_path / "SL1"
    shutil.copytree(source, copy)
    prj = copy / PUBLIC["SL1"].name
    outputs = copy / f"Project Outputs for {prj.stem}"
    outputs.mkdir()
    shutil.copy(DRC_FIXTURE, outputs / f"Design Rule Check - {prj.stem}.html")
    with isolated_main():
        _load(prj)
        answer = json.loads(main.get_project_dashboard(checks=["get_drc_report"]))
    (card,) = answer["cards"]
    assert card["state"] == "errors" and card["counts"]["error"] > 0
    assert "report_untrusted" in card["tags"]
    assert any(row["check"] == "other_board" for row in card["findings"])


def test_every_call_reads_the_netlists_age_itself(tmp_path):
    """A sheet saved after the netlist: a narrowed call (which runs no release) and a first
    call with an empty cache both mark the netlist cards, so none reads clean."""
    source = _need(PUBLIC["SL1"]).parent
    copy = tmp_path / "SL1"
    shutil.copytree(source, copy)
    prj = copy / PUBLIC["SL1"].name
    netlist_cards = [tool for tool in pd.CARD_TOOLS if tool in main.NETLIST_TOOLS]

    def save_a_sheet():
        sheet = sorted(copy.glob("*.SchDoc"))[0]
        later = time.time() + 120
        os.utime(sheet, (later, later))

    def assert_marked(answer):
        assert answer["state"]["netlist_older_than_sheets"] is True
        assert "saved after the netlist was compiled" in answer["state"]["netlist_note"]
        for card in answer["cards"]:
            if "counts" in card:
                assert "netlist_older_than_sheets" in card["tags"], card["tool"]
                assert card["state"] != "clean", card["tool"]

    with isolated_main():
        _load(prj)
        before = json.loads(main.get_project_dashboard())
        assert before["state"]["netlist_older_than_sheets"] is False
        save_a_sheet()
        assert_marked(json.loads(main.get_project_dashboard(checks=netlist_cards)))
        main._dashboard_cache.clear()
        assert_marked(json.loads(main.get_project_dashboard(checks=netlist_cards)))


# ---------- (4) run order and time budget ----------

class Fakes:
    """Fake tools that count their calls and advance a fake clock."""

    def __init__(self, cost=1.0, costs=None, payloads=None):
        self.now = 0.0
        self.calls: list[str] = []
        self.cost = cost
        self.costs = costs or {}
        self.payloads = payloads or {}

    def clock(self) -> float:
        return self.now

    def tool(self, name):
        def run(**kwargs):
            self.calls.append(name)
            self.now += self.costs.get(name, self.cost)
            payload = self.payloads.get(name)
            if callable(payload):
                return payload()
            if payload is not None:
                return json.dumps(payload)
            if name == pd.RELEASE_TOOL:
                return json.dumps({"verdict": "pass", "verdict_summary": "PASS", "counts": {"pass": 1},
                                   "blocking_failed": [], "blocking_not_verified": [], "gates_total": 1,
                                   "gates": [{"id": "drc", "title": "DRC", "status": "pass", "blocking": True,
                                              "evidence": "ok", "source_tool": "get_drc_report", "ms": 0}]})
            if name in pd.FACT_TOOLS:
                return json.dumps({"active_variant": "A", "variants": []})
            return json.dumps({"severity_counts": {"info": 1}, "checks_run": ["a"], "rails_total": 2,
                               "findings": [{"severity": "info", "check": "c", "issue": "a note"}],
                               "summary": {"severity_counts": {"info": 1}, "error": 0, "warning": 0, "info": 1,
                                           "findings": {"info": 1}, "by_level": {"Warning": 1}},
                               "totals": {"rule_violations": 0}, "assessment": [], "violation_count": 0,
                               "kinds": [], "rules": [], "rule_count": 0})
        return run

    def tools(self) -> dict:
        return {name: self.tool(name) for name in pd.RUN_ORDER}


def _project(tmp_path: Path) -> dict:
    (tmp_path / "Board.PrjPcb").write_text("[Design]\n", encoding="utf-8")
    (tmp_path / "Board.PcbDoc").write_bytes(b"board")
    (tmp_path / "Power.SchDoc").write_bytes(b"sheet")
    return {"name": "Board", "root_dir": str(tmp_path), "prj_pcb_path": str(tmp_path / "Board.PrjPcb"),
            "sheets": [{"name": "Power", "path": str(tmp_path / "Power.SchDoc")}],
            "pcb_doc_paths": [str(tmp_path / "Board.PcbDoc")], "pcb_dwf_paths": []}


NETLIST = {"nets": {}}  # one netlist object: its identity is part of the state key


def _inputs(project: dict, fakes: Fakes, **extra) -> object:
    values = dict(project=project, netlist=NETLIST, load="offline_netlist", variant="A",
                  defined_variants=1, netlist_updated_utc="2026-01-01T00:00:00Z", tools=fakes.tools(),
                  netlist_tools=main.NETLIST_TOOLS, clock=fakes.clock,
                  now=lambda: datetime(2026, 10, 28, 9, 0, tzinfo=timezone.utc))
    values.update(extra)
    return pd.Inputs(**values)


def _cards(model: dict) -> dict:
    return {card["tool"]: card for card in model["cards"]}


def test_the_release_runs_first_and_is_never_skipped(tmp_path):
    fakes = Fakes()
    model = pd.build(_inputs(_project(tmp_path), fakes, budget_s=0.0), pd.DashboardCache())
    assert fakes.calls == [pd.RELEASE_TOOL]
    assert model["release"]["verdict"] == "pass" and model["release"]["current"]
    # The facts and every card are left out, the slow cards last.
    assert model["not_run"]["checks"] == [*pd.FACT_TOOLS, *pd.ORDER]
    assert model["overview"]["notes"] == ["Board facts not run (time budget).",
                                          "Variant facts not run (time budget)."]
    assert all(card["state"] == "not_run" and card["reason"] == "time budget" for card in model["cards"])
    assert model["budget"]["stopped_at"] == "get_board_info"
    assert model["not_run"]["next"].startswith('get_project_dashboard(checks=["get_board_info"')


def test_the_budget_stops_before_a_card_and_slow_cards_start_only_early(tmp_path):
    fakes = Fakes(cost=1.0)
    model = pd.build(_inputs(_project(tmp_path), fakes), pd.DashboardCache())
    # The release and the facts take 3 s; then a card a second until 20 s have passed.
    assert fakes.calls == list(pd.RUN_ORDER[:20])
    assert model["budget"]["limit_s"] == 20.0 and model["budget"]["stopped_at"] == pd.RUN_ORDER[20]
    left = model["not_run"]["checks"]
    assert set(left) == set(pd.RUN_ORDER[20:]) - set(pd.FACT_TOOLS)
    assert left == [tool for tool in pd.ORDER if tool in left]  # non-slow first, slow last
    # Cheap cards with a slow start: every non-slow card runs, no slow one after 10 s.
    fakes = Fakes(cost=0.4)
    model = pd.build(_inputs(_project(tmp_path), fakes), pd.DashboardCache())
    assert set(model["not_run"]["checks"]) == pd.SLOW
    fakes = Fakes(cost=0.1)
    model = pd.build(_inputs(_project(tmp_path), fakes), pd.DashboardCache())
    assert model["not_run"]["checks"] == [] and set(fakes.calls) == set(pd.RUN_ORDER)


def test_checks_ignore_the_budget_and_old_cards_come_back_stale(tmp_path):
    project = _project(tmp_path)
    cache = pd.DashboardCache()
    fakes = Fakes(cost=0.1)
    pd.build(_inputs(project, fakes), cache)
    (tmp_path / "Board.PcbDoc").write_bytes(b"board saved again")
    fakes = Fakes(cost=0.1)
    model = pd.build(_inputs(project, fakes, budget_s=0.0), cache)
    assert fakes.calls == [pd.RELEASE_TOOL]
    cards = _cards(model)
    assert all(not card["current"] and card["stale_because"] == "design files saved" for card in cards.values())
    assert set(model["not_run"]["checks"]) == set(pd.CARD_TOOLS) | set(pd.FACT_TOOLS)
    narrowed = pd.build(_inputs(project, fakes, budget_s=0.0), cache, checks=["check_dfm", "check_vias"])
    assert [card["tool"] for card in narrowed["cards"]] == ["check_vias", "check_dfm"]
    assert all(card["current"] for card in narrowed["cards"]) and narrowed["budget"]["limit_s"] is None
    assert narrowed["others"] == {"current": 0, "stale": 31, "not_run": 0}


def test_naming_the_facts_or_the_release_returns_them(tmp_path):
    """not_run.next can name the facts: the narrowed answer then carries the overview; one
    that names the release carries the whole release block."""
    project = _project(tmp_path)
    cache = pd.DashboardCache()
    fakes = Fakes(cost=30.0)  # the release alone outruns the budget
    model = pd.build(_inputs(project, fakes), cache)
    assert model["not_run"]["checks"][:2] == list(pd.FACT_TOOLS)
    facts = pd.build(_inputs(project, fakes), cache, checks=list(pd.FACT_TOOLS))
    assert facts["cards"] == [] and facts["overview"]["variants"]["active"] == "A"
    assert "notes" not in facts["overview"]
    assert "overview" not in pd.build(_inputs(project, fakes), cache, checks=["check_vias"])
    text = json.loads(pd.render(pd.build(_inputs(project, fakes), cache, checks=[pd.RELEASE_TOOL])))
    assert {"verdict", "verdict_summary", "gates", "blocking_failed", "blocking_not_verified"} <= set(text["release"])
    narrowed = json.loads(pd.render(pd.build(_inputs(project, fakes), cache, checks=["check_vias"])))
    assert set(narrowed["release"]) <= {"verdict", "counts", "current", "state", "reason"}


def test_cards_say_when_and_under_which_variant_they_ran(tmp_path):
    project = _project(tmp_path)
    cache = pd.DashboardCache()
    first = datetime(2026, 10, 28, 9, 0, tzinfo=timezone.utc)
    model = pd.build(_inputs(project, Fakes(cost=0.01), now=lambda: first), cache)
    assert model["state"]["cards_as_of_utc"] == model["state"]["as_of_utc"] == "2026-10-28T09:00:00Z"
    assert all("as_of" not in card and "variant" not in card for card in model["cards"])
    later = first + timedelta(minutes=5)
    narrowed = pd.build(_inputs(project, Fakes(cost=0.01), now=lambda: later), cache,
                        checks=["check_vias"], refresh=True)
    (card,) = narrowed["cards"]
    assert "as_of" not in card and narrowed["state"]["cards_as_of_utc"] == "2026-10-28T09:05:00Z"
    model = pd.build(_inputs(project, Fakes(cost=0.01), now=lambda: later), cache)
    cards = _cards(model)
    assert cards["check_vias"]["as_of"] == "2026-10-28T09:05:00Z"
    assert model["state"]["cards_as_of_utc"] == "2026-10-28T09:00:00Z"
    # Another variant: the old cards are stale and name the variant they ran under.
    switched = pd.build(_inputs(project, Fakes(cost=0.01), variant="B", budget_s=0.0), cache)
    vias = _cards(switched)["check_vias"]
    assert not vias["current"] and vias["stale_because"] == "variant changed" and vias["variant"] == "A"


# ---------- (5) the cache ----------

def test_an_unchanged_second_call_runs_nothing(tmp_path):
    project = _project(tmp_path)
    cache = pd.DashboardCache()
    fakes = Fakes(cost=0.01)
    first = pd.render(pd.build(_inputs(project, fakes), cache))
    count = len(fakes.calls)
    assert count == len(pd.RUN_ORDER)
    second = pd.render(pd.build(_inputs(project, fakes), cache))
    assert len(fakes.calls) == count
    assert json.loads(first)["cards"] == json.loads(second)["cards"]


@pytest.mark.parametrize("change, because", [
    (lambda root, extra: (root / "Power.SchDoc").write_bytes(b"sheet saved"), "design files saved"),
    (lambda root, extra: (root / "Board.PcbDoc").write_bytes(b"board saved"), "design files saved"),
    (lambda root, extra: (root / "Report.html").write_text("x", encoding="utf-8"), "project folder changed"),
    (lambda root, extra: extra.update(variant="B"), "variant changed"),
    (lambda root, extra: extra.update(settings=["units mm"]), "settings changed"),
    (lambda root, extra: extra.update(packs=[["check_packs.json", 1, 2]]), "check packs changed"),
    (lambda root, extra: extra.update(netlist_updated_utc="2026-02-02T00:00:00Z"), "netlist changed"),
])
def test_what_makes_a_card_stale(tmp_path, change, because):
    project = _project(tmp_path)
    cache = pd.DashboardCache()
    pd.build(_inputs(project, Fakes(cost=0.01)), cache)
    extra: dict = {}
    change(tmp_path, extra)
    fakes = Fakes(cost=0.01)
    model = pd.build(_inputs(project, fakes, budget_s=0.0, **extra), cache)
    stale = [card for card in model["cards"] if not card["current"]]
    assert len(stale) == 33 and {card["stale_because"] for card in stale} == {because}
    # Then a call with time recomputes them, keeping the old counts when they changed.
    model = pd.build(_inputs(project, Fakes(cost=0.01), **extra), cache)
    assert all(card["current"] for card in model["cards"])


def test_refresh_bumps_the_generation_and_a_card_keeps_what_it_was(tmp_path):
    project = _project(tmp_path)
    cache = pd.DashboardCache()
    pd.build(_inputs(project, Fakes(cost=0.01)), cache)
    fakes = Fakes(cost=0.01)
    model = pd.build(_inputs(project, fakes, budget_s=0.0), cache, refresh=True)
    assert {card["stale_because"] for card in model["cards"]} == {"refreshed"}
    # refresh with checks recomputes those only (no new generation).
    fakes = Fakes(cost=0.01, payloads={"check_vias": {"severity_counts": {"warning": 2}, "summary": {"vias": 5},
                                                      "findings": []}})
    model = pd.build(_inputs(project, fakes), cache, checks=["check_vias"], refresh=True)
    assert fakes.calls == ["check_vias"]
    (card,) = model["cards"]
    assert card["counts"] == {"error": 0, "warning": 2, "info": 0}
    assert card["was"]["counts"] == {"error": 0, "warning": 0, "info": 1}


def test_the_cache_keeps_four_projects(tmp_path):
    cache = pd.DashboardCache(4)
    for index in range(5):
        cache.entry(f"p{index}")
    assert cache.projects() == ["p1", "p2", "p3", "p4"]
    cache.entry("p1")
    cache.entry("p5")
    assert cache.projects() == ["p3", "p4", "p1", "p5"]


def test_failed_and_unreadable_cards_are_not_cached(tmp_path):
    project = _project(tmp_path)
    cache = pd.DashboardCache()

    def boom():
        raise RuntimeError("broken")
    fakes = Fakes(cost=0.01, payloads={"check_dfm": boom, "check_vias": {"findings": []}})
    model = pd.build(_inputs(project, fakes), cache)
    cards = _cards(model)
    assert cards["check_dfm"]["state"] == "failed" and cards["check_dfm"]["current"]
    assert cards["check_dfm"]["reason"].startswith("RuntimeError")
    assert cards["check_vias"]["state"] == "unreadable"
    entry = cache.entry(project["prj_pcb_path"])
    assert "check_dfm" not in entry["cards"] and "check_vias" not in entry["cards"]
    fakes.calls.clear()
    pd.build(_inputs(project, fakes), cache)
    assert sorted(fakes.calls) == ["check_dfm", "check_vias"]


def test_an_unwatched_folder_is_said(tmp_path, monkeypatch):
    monkeypatch.setattr(pd.rc, "folder_signature", lambda root, max_files=None: None)
    model = pd.build(_inputs(_project(tmp_path), Fakes(cost=0.01)), pd.DashboardCache())
    assert model["state"]["folder_watched"] is False


def test_the_git_state_is_part_of_the_key(tmp_path):
    project = _project(tmp_path)
    git = tmp_path / ".git"
    (git / "logs").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git / "index").write_bytes(b"1")
    before, _ = pd.state_parts(_inputs(project, Fakes()))
    (git / "index").write_bytes(b"12")
    after, _ = pd.state_parts(_inputs(project, Fakes()))
    assert pd.stale_because(before, after) == "Git state changed"


# ---------- (6) size ----------

def test_a_worst_case_shrinks_step_by_step(tmp_path):
    long = "word " * 120
    rows = [{"severity": "error", "check": f"c{index}", "issue": long, "designators": [f"R{index}"]}
            for index in range(30)]
    payload = {"severity_counts": {"error": 30}, "checks_run": ["a"] * 9, "findings": rows,
               "not_checked": [long, long, long], "rails_total": 4,
               "summary": {"severity_counts": {"error": 30}, "error": 30, "warning": 0, "info": 0,
                           "findings": {"error": 30}},
               "totals": {"rule_violations": 30}, "assessment": [], "violation_count": 30,
               "kinds": [], "rules": [], "rule_count": 0}
    fakes = Fakes(cost=0.01, payloads={name: payload for name in pd.CARD_TOOLS})
    model = pd.build(_inputs(_project(tmp_path), fakes), pd.DashboardCache())
    sizes = [len(pd.dumps(pd._shrunk(model, step, False))) for step in pd.LADDER]
    assert sizes == sorted(sizes, reverse=True) and sizes[0] > pd.MAX_RESULT_CHARS
    text = pd.render(model)
    assert len(text) <= pd.MAX_RESULT_CHARS
    data = json.loads(text)
    assert len(data["cards"]) == 33 and "next_offset" not in text and "has_more" not in text
    # Three names or fewer: up to 20 findings each.
    narrowed = pd.render(pd.build(_inputs(_project(tmp_path), fakes), pd.DashboardCache(),
                                  checks=["check_vias"]))
    (card,) = json.loads(narrowed)["cards"]
    assert len(card["findings"]) == 20 and len(narrowed) <= pd.MAX_RESULT_CHARS


@pytest.fixture(scope="module")
def minipc():
    path = _need(MINIPC)
    with isolated_main():
        _load(path)
        yield main.get_project_dashboard()


def test_the_mini_pc_default_call_fits_and_names_what_it_left_out(minipc):
    assert len(minipc) <= pd.MAX_RESULT_CHARS
    data = json.loads(minipc)
    assert data["budget"]["limit_s"] == 20.0 and len(data["cards"]) == 33
    left = data["not_run"]["checks"]
    if left:  # a large board: the budget stops the call
        assert data["budget"]["stopped_at"] in left
        assert data["not_run"]["next"] == f"get_project_dashboard(checks={json.dumps(left)})"
        assert all(card["state"] == "not_run" for card in data["cards"] if card["tool"] in left)


# ---------- (7) unchanged tool results and server state ----------

VOLATILE = ("elapsed_ms", "ms")


def _steady(text: str):
    def strip(value):
        if isinstance(value, dict):
            return {key: strip(item) for key, item in value.items() if key not in VOLATILE}
        if isinstance(value, list):
            return [strip(item) for item in value]
        return value
    try:
        return strip(json.loads(text))
    except ValueError:
        return text


def test_tool_results_and_server_state_are_unchanged():
    path = _need(PUBLIC["SL1"])
    with isolated_main():
        _load(path)
        names = (*pd.CARD_TOOLS, pd.RELEASE_TOOL)
        calls = {name: (lambda name=name: getattr(main, name)()) for name in names}
        calls["get_board_info"] = lambda: main.get_board_info(units="mm")
        calls["list_variants"] = main.list_variants
        before = {name: call() for name, call in calls.items()}
        state = (main._variant_state.active.name, main._pcb_session.loaded, id(main._altium._netlist),
                 main._netlist_last_updated, main._project)
        main.get_project_dashboard()
        main.get_project_dashboard(checks=["check_dfm"], refresh=True)
        after = {name: call() for name, call in calls.items()}
        assert (main._variant_state.active.name, main._pcb_session.loaded, id(main._altium._netlist),
                main._netlist_last_updated, main._project) == state
    for name in calls:
        if before[name] != after[name]:
            assert _steady(before[name]) == _steady(after[name]), name


# ---------- (8) protocol ----------

def test_the_tool_is_read_only_titled_linked_and_in_the_manifest():
    tool = _tools()["get_project_dashboard"]
    view = uv.view_for("get_project_dashboard")
    assert tool["title"] == "Project Dashboard"
    assert tool["annotations"]["readOnlyHint"] is True
    assert tool["_meta"] == {"anthropic/maxResultSizeChars": main.QUERY_NET_MAX_RESULT_SIZE_CHARS,
                             "ui": {"resourceUri": view.uri}, "ui/resourceUri": view.uri}
    assert set(tool["inputSchema"]["properties"]) == {"checks", "refresh"}
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    assert "get_project_dashboard" in {entry["name"] for entry in manifest["tools"]}
    assert "get_project_dashboard" not in main.NETLIST_TOOLS


def test_the_text_is_the_structured_result(tmp_path):
    with isolated_main():
        main._dashboard_cache.clear()
        result = _handle(types.CallToolRequest(method="tools/call", params=types.CallToolRequestParams(
            name="get_project_dashboard", arguments={})))
        assert json.loads(result["content"][0]["text"])["error"] == "no_project"
        assert result["structuredContent"] == {"result": result["content"][0]["text"]}
        path = _need(PUBLIC["Harness A"])
        _load(path)
        direct = json.loads(main.get_project_dashboard())
        result = _handle(types.CallToolRequest(method="tools/call", params=types.CallToolRequestParams(
            name="get_project_dashboard", arguments={})))
    (content,) = result["content"]
    assert content["type"] == "text" and result["structuredContent"] == {"result": content["text"]}
    assert not result.get("isError")
    # The second call is served from the cache: the same cards (only the time of the answer moves).
    assert json.loads(content["text"])["cards"] == direct["cards"]


# ---------- (9) arguments ----------

def test_unknown_names_empty_lists_and_name_cleaning(tmp_path):
    project = _project(tmp_path)
    fakes = Fakes(cost=0.01)
    error = pd.build(_inputs(project, fakes), pd.DashboardCache(), checks=["check_dfm", "nope"])
    assert error["error"] == "unknown_checks" and "nope" in error["message"]
    assert set(error["accepted"]) == set(pd.ACCEPTED) and fakes.calls == []
    assert pd.render(error) == json.dumps(error)
    model = pd.build(_inputs(project, fakes), pd.DashboardCache(), checks=[])
    assert model["args"] == {"checks": None, "refresh": False} and len(model["cards"]) == 33
    names, _ = pd.parse_checks(["  CHECK_DFM ", "check_dfm", "run_erc", ""])
    assert names == ["run_erc", "check_dfm"]


def test_no_project_and_the_netlist_free_load(tmp_path):
    with isolated_main():
        assert json.loads(main.get_project_dashboard()) == {
            "error": "no_project", "message": "No project loaded. Call set_project_dir first."}


def test_a_netlist_free_load_makes_the_netlist_cards_unavailable(tmp_path, monkeypatch):
    from tests.test_netlist_free_load import _write_project
    root = tmp_path / "Board"
    prj = _write_project(root)
    with isolated_main(), patch.object(main._altium, "compile_offline",
                                       side_effect=RuntimeError("sheets unreadable")):
        _load(prj)
        assert main._altium._netlist is None
        text = main.get_project_dashboard()
        data = json.loads(text)
    cards = _cards(data)
    assert data["load"] == "netlist_free"
    for tool in pd.CARD_TOOLS:
        if tool in main.NETLIST_TOOLS:
            assert cards[tool]["state"] == "unavailable", tool
            assert "netlist" in cards[tool]["reason"]
    assert len(text) <= pd.MAX_RESULT_CHARS


def test_the_docstring_says_when_to_call_it():
    doc = " ".join(main.get_project_dashboard.__doc__.split())
    for words in ("status, an overview or a dashboard", "Not at session start", "about 20 s",
                  "not_run lists the checks the time budget left out", "clean means",
                  "partial, nothing_to_check, not_verified, not_applicable, unavailable, failed, "
                  "unreadable, not_run and stale", "call that check's own tool"):
        assert words in doc, words


def test_the_registry_decision_is_recorded():
    doc = (ROOT / "server" / "services" / "ui_views.py").read_text(encoding="utf-8")
    assert "The project dashboard (#74) is linked from get_project_dashboard" in doc
    assert "check_release_readiness and project_tour keep their text and link no" in doc
    assert "ui/message" in doc and uv.APP_ONLY_TOOLS == ("get_board_view_geometry",)
