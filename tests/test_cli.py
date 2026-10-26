"""The check command (roadmap #50, server/cli.py): headless CI runs of the offline checks
with JUnit XML, Markdown and JSON output, exit codes per --fail-on, check packs and the
plugin switch, a design diff against a base revision of a temporary Git repository, and
the guarantees a CI job relies on: no Altium, no registry or state file, nothing written
into the project folder, and the MCP server still starting without a subcommand."""
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import types
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

import pytest

import cli
import server.main as main
from altium import AltiumClient

REPO = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"
GIT = shutil.which("git")

PRJ_TEXT = ("[Design]\nVersion=1.0\n"
            "[Document1]\nDocumentPath=sheet1.SchDoc\n"
            "[Document2]\nDocumentPath=sheet2.SchDoc\n")
VARIANT_TEXT = "[ProjectVariant1]\nDescription=Prod\nVariationCount=0\n"
PCB_TEXT = "[Document3]\nDocumentPath=Board.PcbDoc\n"

DEMO_PACK = {
    "id": "demo", "title": "Demo pack", "version": "1",
    "rules": [
        {"id": "DEMO-ERR", "title": "No resistors at all", "severity": "error",
         "select": {"parts": {"prefix": "R"}}, "assert": [{"none": True}],
         "message": "{designator} is a resistor"},
        {"id": "DEMO-WARN", "title": "No capacitors at all", "severity": "warning",
         "select": {"parts": {"prefix": "C"}}, "assert": [{"none": True}]},
        {"id": "DEMO-NA", "title": "Nothing is selected", "severity": "error",
         "select": {"parts": {"prefix": "ZZQ"}}, "assert": [{"has": ["mpn"]}]},
        {"id": "DEMO-OK", "title": "None of what is forbidden", "severity": "error",
         "select": {"parts": {"prefix": "ZZQ"}}, "assert": [{"none": True}]},
    ],
}
WARN_PACK = {"id": "warnings", "title": "Warnings only", "version": "1",
             "rules": [DEMO_PACK["rules"][1]]}
TEAM_CHECKLIST = {"checklists": [{
    "id": "team", "title": "Team gate", "scope": "schematic", "version": "1",
    "sections": [{"id": "rules", "title": "Rules"}],
    "items": [
        {"id": "TEAM-01", "section": "rules", "kind": "custom",
         "text": "No capacitors (a demo rule).", "rule": "DEMO-WARN"},
        {"id": "TEAM-02", "section": "rules", "kind": "judgment", "text": "A person decides."},
    ],
}]}


def make_project(root: Path, variants: bool = False, pcb: bool = False) -> Path:
    """A synthetic project from the generic fixture sheets; optionally a variant and a
    PcbDoc that does not parse."""
    root.mkdir(parents=True, exist_ok=True)
    for name in ("sheet1.SchDoc", "sheet2.SchDoc"):
        shutil.copy(FIXTURES / name, root / name)
    text = PRJ_TEXT
    if pcb:
        (root / "Board.PcbDoc").write_bytes(b"not a board")
        text += PCB_TEXT
    if variants:
        text += VARIANT_TEXT
    prj = root / "Board.PrjPcb"
    prj.write_text(text, encoding="utf-8")
    return prj


def write_json(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def folder_state(folder: Path) -> dict:
    return {str(p): (p.stat().st_size, p.stat().st_mtime_ns)
            for p in sorted(folder.rglob("*")) if p.is_file() and ".git" not in p.parts}


def _forbidden(*args, **kwargs):
    raise AssertionError("the check command must not call this")


@pytest.fixture(autouse=True)
def no_step_summary(monkeypatch):
    """This repository's own CI sets GITHUB_STEP_SUMMARY for the pytest step: no test
    may append to it."""
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)


@pytest.fixture
def isolated(monkeypatch):
    """main's state reset; Altium, the registry, the state file and the update check are
    forbidden: the check command must touch none of them."""
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.delenv("ALTIUM_COPILOT_ALLOW_PLUGINS", raising=False)
    registry = sys.modules[main.read_registry.__module__]
    client = AltiumClient()
    with patch.object(main, "_project", None), \
            patch.object(main, "_variant_state", None), \
            patch.object(main, "_netlist_last_updated", None), \
            patch.object(main, "_pcb_session", main.PcbSession()), \
            patch.object(main, "_design_notes", main.DesignNotesCache()), \
            patch.object(main, "_installed_library_paths", []), \
            patch.object(main, "_erc_cache", None), \
            patch.object(main, "_review_cache", {}), \
            patch.object(main, "_custom_cache", {}), \
            patch.object(main, "_altium", client), \
            patch.object(client, "get_status", side_effect=_forbidden), \
            patch.object(main, "upsert_registry_entry", side_effect=_forbidden), \
            patch.object(main, "mark_xfn_exported", side_effect=_forbidden), \
            patch.object(registry, "write_registry", side_effect=_forbidden), \
            patch.object(main, "_write_state", side_effect=_forbidden), \
            patch.object(main, "_check_for_update", side_effect=_forbidden):
        yield


def run(*argv) -> int:
    return cli.main([str(a) for a in argv], server=main)


def run_json(tmp_path: Path, *argv) -> tuple[int, dict]:
    out = tmp_path / "out" / "result.json"
    code = run(*argv, "--json", out)
    return code, json.loads(out.read_text(encoding="utf-8"))


def suite_of(result: dict, suite_id: str) -> dict:
    return next(item for item in result["suites"] if item["id"] == suite_id)


def case_of(suite: dict, case_id: str) -> dict:
    return next(item for item in suite["cases"] if item["id"] == case_id)


# The element and attribute rules of the Jenkins xunit plugin's JUnit schema
# (junit-10.xsd), which a validating consumer applies; GitHub and GitLab reporters read
# the same shape.
ROOT_ATTRIBUTES = {"name", "time", "tests", "failures", "errors"}
SUITE_ATTRIBUTES = {"name", "tests", "failures", "errors", "group", "time", "skipped",
                    "timestamp", "hostname", "id", "package", "file", "log", "url", "version"}
SUITE_CHILDREN = {"testsuite", "properties", "testcase", "system-out", "system-err"}
CASE_ATTRIBUTES = {"name", "time", "classname", "group"}
CASE_CHILDREN = {"skipped", "error", "failure", "rerunFailure", "rerunError", "flakyFailure",
                 "flakyError", "system-out", "system-err"}
TIME = r"\d+(\.\d{1,3})?"


def junit(path: Path) -> ET.Element:
    """Parse a JUnit file and check it against junit-10.xsd's elements and attributes
    (no properties in a testcase, no skipped or timestamp on testsuites), and that the
    counts agree and each case has one outcome at most."""
    root = ET.parse(path).getroot()
    assert root.tag == "testsuites"
    assert set(root.attrib) <= ROOT_ATTRIBUTES
    assert re.fullmatch(TIME, root.get("time"))
    totals = {"tests": 0, "failures": 0, "errors": 0}
    for suite in root:
        assert suite.tag == "testsuite"
        assert set(suite.attrib) <= SUITE_ATTRIBUTES
        assert {"name", "tests", "failures", "errors"} <= set(suite.attrib)
        assert {child.tag for child in suite} <= SUITE_CHILDREN
        assert re.fullmatch(TIME, suite.get("time"))
        for properties in suite.findall("properties"):
            for prop in properties:
                assert prop.tag == "property" and set(prop.attrib) == {"name", "value"}
        cases = suite.findall("testcase")
        counts = {"failure": 0, "error": 0, "skipped": 0}
        for case in cases:
            assert set(case.attrib) <= CASE_ATTRIBUTES and case.get("name")
            assert case.get("classname") is not None
            float(case.get("time"))
            tags = [child.tag for child in case]
            assert set(tags) <= CASE_CHILDREN
            outcomes = [tag for tag in tags if tag in counts]
            assert len(outcomes) <= 1
            for tag in outcomes:
                counts[tag] += 1
            for child in case:
                assert len(child) == 0  # text only
                allowed = {"type", "message"} if child.tag in counts else set()
                assert set(child.attrib) <= allowed
            for child in case.findall("failure") + case.findall("error"):
                assert child.get("message") is not None and child.get("type")
        assert suite.get("tests") == str(len(cases))
        assert suite.get("failures") == str(counts["failure"])
        assert suite.get("errors") == str(counts["error"])
        assert suite.get("skipped") == str(counts["skipped"])
        totals["tests"] += len(cases)
        totals["failures"] += counts["failure"]
        totals["errors"] += counts["error"]
    for key, value in totals.items():
        assert root.get(key) == str(value)
    return root


# ---------- arguments and usage errors ----------

def test_parser_defaults_and_comma_lists():
    args = cli.build_parser().parse_args(["Board.PrjPcb"])
    assert args.fail_on == "error" and args.packs == "auto" and not args.allow_plugins
    assert args.checklist is None and args.checks is None and not args.drc_report
    assert cli.split_names(["a,b", " B ,c", ""]) == ["a", "b", "c"]
    assert cli.DEFAULT_CHECKLISTS == ("schematic", "release")


def test_help_and_argparse_errors(capsys):
    assert run("--help") == 0
    assert "--fail-on" in capsys.readouterr().out
    assert run("Board.PrjPcb", "--fail-on", "sometimes") == 2
    assert "invalid choice" in capsys.readouterr().err
    assert run() == 2


@pytest.mark.parametrize("argv, words", [
    (("Board.PrjPcb", "--bogus-option"), "unrecognized arguments: --bogus-option"),
    (("Board.PrjPcb", "--fail-on", "sometimes"), "invalid choice"),
    ((), "the following arguments are required: project"),
])
def test_a_command_line_that_does_not_parse_still_writes_the_reports(
        tmp_path, isolated, monkeypatch, capsys, argv, words):
    summary = tmp_path / "step.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    out = tmp_path / "out"
    assert run(*argv, "--junit", out / "junit.xml", "--markdown", out / "summary.md",
               "--json", out / "result.json") == 2
    assert words in capsys.readouterr().err
    error = junit(out / "junit.xml").find("testsuite/testcase/error")
    assert error.get("type") == "invalid_arguments" and words in error.get("message")
    result = json.loads((out / "result.json").read_text(encoding="utf-8"))
    assert result["exit_code"] == 2 and result["verdict"] == "error"
    assert "`invalid_arguments`" in (out / "summary.md").read_text(encoding="utf-8")
    assert "**ERROR** (exit 2" in summary.read_text(encoding="utf-8")


def test_project_argument_errors(tmp_path, isolated, capsys):
    assert run(tmp_path / "missing") == 2
    assert "No file or folder" in capsys.readouterr().err
    (tmp_path / "empty").mkdir()
    assert run(tmp_path / "empty") == 2
    assert "No .PrjPcb" in capsys.readouterr().err
    notes = tmp_path / "notes.txt"
    notes.write_text("x", encoding="utf-8")
    assert run(notes) == 2
    assert "not a .PrjPcb" in capsys.readouterr().err
    two = tmp_path / "two"
    make_project(two)
    (two / "Other.PrjPcb").write_text(PRJ_TEXT, encoding="utf-8")
    assert run(two) == 2
    assert "2 .PrjPcb files" in capsys.readouterr().err


def test_a_folder_with_one_project_is_found(tmp_path):
    prj = make_project(tmp_path / "Board")
    assert cli.find_project(str(tmp_path / "Board")) == prj.resolve()
    assert cli.find_project(str(prj)) == prj.resolve()


@pytest.mark.parametrize("argv, words", [
    (("--checks", "run_erc,no_such_tool"), "no tool no_such_tool"),
    (("--checks", "set_project_dir"), "no tool set_project_dir"),
    (("--checks", "get_design_notes"), "get_design_notes answers in text"),
    (("--checklist", "none,schematic"), "cannot be combined"),
    (("--vref", "lots"), "--vref"),
    (("--rails", "VIN=lots"), "--rails"),
    (("--hv", "L=lots"), "--hv"),
])
def test_bad_options_exit_2_before_anything_runs(tmp_path, isolated, capsys, argv, words):
    prj = make_project(tmp_path / "Board")
    out = tmp_path / "out" / "junit.xml"
    assert run(prj, *argv, "--junit", out) == 2
    assert words in capsys.readouterr().err
    root = junit(out)  # the report says why
    assert root.get("errors") == "1"
    assert main._project is None  # nothing was loaded


# ---------- a run ----------

def test_a_run_writes_junit_markdown_and_json(tmp_path, isolated, capsys):
    prj = make_project(tmp_path / "Board")
    before = folder_state(prj.parent)
    out = tmp_path / "out"
    code = run(prj, "--checklist", "schematic", "--checks", "run_erc", "--junit",
               out / "junit.xml", "--markdown", out / "summary.md", "--json",
               out / "result.json")
    printed = capsys.readouterr().out
    assert code in (0, 1)
    assert folder_state(prj.parent) == before  # nothing written into the project folder
    root = junit(out / "junit.xml")
    names = [suite.get("name") for suite in root]
    assert names[0] == "Project load"
    assert names[1].startswith("Checklist schematic") and names[2] == "run_erc"
    result = json.loads((out / "result.json").read_text(encoding="utf-8"))
    assert result["project"]["load_mode"] == "offline_netlist"
    assert result["project"]["netlist"] == "compiled offline from the saved sheets"
    load = suite_of(result, "load")
    assert [(c["id"], c["status"]) for c in load["cases"]] == [("netlist", "pass")]
    assert load["cases"][0]["message"].startswith("2 sheet(s) listed")
    assert result["design_state"]["kind"] in ("files", "git")
    assert result["exit_code"] == code
    checklist = suite_of(result, "checklist:schematic")
    statuses = {case["properties"]["status"] for case in checklist["cases"]}
    assert statuses <= {"yes", "no", "n/a", "needs_judgment"}
    for case in checklist["cases"]:
        expected = {"yes": "pass", "no": "fail", "n/a": "skip",
                    "needs_judgment": "skip"}[case["properties"]["status"]]
        assert case["status"] == expected
        if case["properties"].get("kind") == "judgment":
            assert case["status"] == "skip"  # a person decides: never passed
    assert checklist["coverage"][-1]["total"] == len(checklist["cases"])
    markdown = (out / "summary.md").read_text(encoding="utf-8")
    assert markdown.startswith("# Altium design checks: Board.PrjPcb")
    assert "## Coverage" in markdown and "| Section | Yes | No | N/A |" in markdown
    assert "Design state" in markdown
    assert "Board.PrjPcb" in printed and "Result:" in printed


def test_quiet_prints_nothing(tmp_path, isolated, capsys):
    prj = make_project(tmp_path / "Board")
    run(prj, "--checklist", "none", "--packs", "none", "--checks", "run_erc", "--quiet")
    captured = capsys.readouterr()
    assert captured.out == ""


def test_the_step_summary_is_appended(tmp_path, isolated, monkeypatch):
    prj = make_project(tmp_path / "Board")
    summary = tmp_path / "step" / "summary.md"
    summary.parent.mkdir()
    summary.write_text("earlier step\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    run(prj, "--checklist", "none", "--checks", "run_erc", "--markdown", tmp_path / "md.md")
    text = summary.read_text(encoding="utf-8")
    assert text.startswith("earlier step\n# Altium design checks")
    assert (tmp_path / "md.md").read_text(encoding="utf-8").strip() in text


def test_a_project_that_does_not_load_exits_2_with_a_report(tmp_path, isolated, capsys):
    root = tmp_path / "Empty"
    root.mkdir()
    (root / "Empty.PrjPcb").write_text("[Design]\nVersion=1.0\n", encoding="utf-8")
    out = tmp_path / "junit.xml"
    assert run(root, "--junit", out) == 2
    assert "no_documents" in capsys.readouterr().err
    error = junit(out).find("testsuite/testcase/error")
    assert error is not None and error.get("type") == "no_documents"


def test_a_netlist_free_load_still_runs(tmp_path, isolated):
    prj = make_project(tmp_path / "Board")
    with patch.object(main._altium, "compile_offline", side_effect=RuntimeError("unreadable")):
        code, result = run_json(tmp_path, prj, "--checklist", "schematic", "--checks", "run_erc")
    assert code == 1  # nothing could be compiled: the run must not pass on it
    assert result["project"]["load_mode"] == "netlist_free"
    erc = suite_of(result, "tool:run_erc")
    assert erc["cases"][0]["status"] == "skip"
    assert erc["cases"][0]["properties"]["error"] == "netlist_required"
    netlist = case_of(suite_of(result, "load"), "netlist")
    assert (netlist["status"], netlist["severity"]) == ("fail", "error")
    assert "RuntimeError: unreadable" in netlist["message"]


def test_unreadable_sheets_fail_the_load_suite(tmp_path, isolated, capsys):
    """A sheet that cannot be read (corrupt, a Git LFS pointer, a path whose case does
    not match on Linux) fails as an error, named in the console and the Markdown."""
    prj = make_project(tmp_path / "Board")
    (prj.parent / "sheet2.SchDoc").write_bytes(b"version https://git-lfs.github.com/spec/v1\n")
    with prj.open("a", encoding="utf-8") as handle:
        handle.write("[Document3]\nDocumentPath=Missing.SchDoc\n")
    out = tmp_path / "out"
    code = run(prj, "--checklist", "none", "--checks", "run_erc", "--markdown",
               out / "summary.md", "--json", out / "result.json")
    printed = capsys.readouterr().out
    result = json.loads((out / "result.json").read_text(encoding="utf-8"))
    load = suite_of(result, "load")
    assert [(c["id"], c["status"], c["severity"]) for c in load["cases"]] == [
        ("netlist", "pass", None), ("sheet.sheet2", "fail", "error"),
        ("sheet.Missing", "fail", "error")]
    assert "file not found" in case_of(load, "sheet.Missing")["message"]
    assert code == 1
    markdown = (out / "summary.md").read_text(encoding="utf-8")
    assert "**sheet sheet2 read** (error): could not be read" in markdown
    assert "load sheet sheet2 read: could not be read" in printed
    # Every sheet unreadable: the netlist is not compiled, an error too.
    (prj.parent / "sheet1.SchDoc").write_bytes(b"\x00" * 64)
    code, result = run_json(tmp_path, prj, "--checklist", "none", "--checks", "run_erc")
    assert code == 1 and result["project"]["load_mode"] == "netlist_free"
    netlist = case_of(suite_of(result, "load"), "netlist")
    assert (netlist["status"], netlist["severity"]) == ("fail", "error")
    assert "could not be compiled offline" in netlist["message"]


def test_annotation_and_component_problems_in_the_load_suite():
    load = {"sheet_count": 1, "component_count": 0, "load_mode": "offline_netlist",
            "annotation_errors": [{"path": "/x/Board.Annotation", "error": "file not found"}],
            "warning": "no_components_loaded", "message": "No component metadata."}
    cases = cli.load_suite(load, True, 0.1)["cases"]
    assert [(c["id"], c["status"], c["severity"]) for c in cases] == [
        ("netlist", "pass", None), ("annotation.Board.Annotation", "fail", "warning"),
        ("components", "fail", "error")]
    no_sheets = cli.load_suite({"sheet_count": 0}, False, 0)["cases"]
    assert [(c["id"], c["status"]) for c in no_sheets] == [("netlist", "skip")]


def test_a_board_document_the_board_tools_cannot_read_fails_the_load(tmp_path, isolated):
    board = tmp_path / "Board.PcbDoc"
    assert cli.board_problem(str(board)) == "file not found"
    board.write_bytes(b"version https://git-lfs.github.com/spec/v1\noid sha256:00\n")
    assert "Git LFS pointer" in cli.board_problem(str(board))
    board.write_bytes(b"not a board")
    assert "not an Altium binary PcbDoc" in cli.board_problem(str(board))
    board.write_bytes(cli.OLE_MAGIC + b"\x00" * 504)
    assert cli.board_problem(str(board)) is None
    prj = make_project(tmp_path / "Board", pcb=True)
    code, result = run_json(tmp_path, prj, "--checklist", "none", "--checks", "check_dfm")
    case = case_of(suite_of(result, "load"), "board")
    assert (case["status"], case["severity"]) == ("fail", "error") and code == 1
    dfm = suite_of(result, "tool:check_dfm")["cases"][0]
    assert dfm["status"] == "skip" and dfm["message"].startswith("could not run")


def test_variants(tmp_path, isolated, capsys):
    prj = make_project(tmp_path / "Board", variants=True)
    code, result = run_json(tmp_path, prj, "--checklist", "none", "--checks", "list_variants",
                            "--variant", "prod")
    assert code == 0 and result["project"]["variant"] == "Prod"
    assert run(prj, "--checklist", "none", "--checks", "list_variants", "--variant",
               "Nope") == 2
    assert "no variant 'Nope'" in capsys.readouterr().err


def test_unknown_checklist_is_a_configuration_error(tmp_path, isolated, capsys):
    prj = make_project(tmp_path / "Board")
    code, result = run_json(tmp_path, prj, "--checklist", "bom,nosuch")
    assert code == 2
    assert result["errors"] == [{"suite": "checklists", "error": "unknown_checklist",
                                 "message": result["errors"][0]["message"]}]
    assert "nosuch" in result["errors"][0]["message"]
    assert suite_of(result, "checklist:bom")["cases"]  # the known one still ran


# ---------- exit codes and outcomes ----------

def synthetic(cases: list[dict], fail_on: str = "error", errors=()) -> dict:
    result = {"generator": "altium-copilot-kc test", "generated_utc": "2026-01-01 00:00 UTC",
              "fail_on": fail_on, "options": {}, "project": {"prj_pcb": "Demo.PrjPcb"},
              "suites": [cli.suite("tool:demo", "demo", "tool", cases)],
              "errors": list(errors), "notes": []}
    return cli.finish(result)


def fail(severity: str, name: str = "x") -> dict:
    return cli.case(name, name, "tool.demo", "fail", severity=severity, message="m")


@pytest.mark.parametrize("severity, fail_on, code", [
    ("error", "error", 1), ("error", "warning", 1), ("error", "never", 0),
    ("warning", "error", 0), ("warning", "warning", 1), ("warning", "never", 0),
    ("info", "error", 0), ("info", "warning", 0),
])
def test_exit_codes_per_fail_on(severity, fail_on, code):
    assert synthetic([fail(severity)], fail_on)["exit_code"] == code


def test_skips_and_passes_never_fail_and_errors_exit_2():
    cases = [cli.case("a", "a", "c", "pass"), cli.case("b", "b", "c", "skip", message="n/a")]
    assert synthetic(cases, "warning")["exit_code"] == 0
    errored = synthetic([cli.case("e", "e", "c", "error", message="bad")])
    assert errored["exit_code"] == 2 and errored["verdict"] == "error"
    assert synthetic([], errors=[{"suite": "packs", "error": "x", "message": "y"}])[
        "exit_code"] == 2


def test_outcomes_follow_the_threshold():
    warning = fail("warning")
    assert cli.outcome(warning, "error") == "passed"
    assert cli.outcome(warning, "warning") == "failure"
    assert cli.outcome(warning, "never") == "failure"  # reported, the exit code is 0
    result = synthetic([warning, fail("error", "y")], "error")
    assert result["counts"] == {"passed": 0, "failure": 1, "skipped": 0, "error": 0,
                                "below_threshold": 1, "total": 2}


# ---------- JUnit ----------

def test_junit_marks_failures_skips_errors_and_below_threshold_cases(tmp_path):
    cases = [
        cli.case("p", "passes", "tool.demo", "pass", message="fine",
                 properties={"evidence": "checked"}),
        cli.case("e", "an error", "tool.demo", "fail", severity="error", message="broken",
                 detail="- [error] broken"),
        cli.case("w", "a warning", "tool.demo", "fail", severity="warning", message="iffy"),
        cli.case("s", "skipped", "tool.demo", "skip", message="needs judgment"),
        cli.case("x", "config", "tool.demo", "error", message="bad pack",
                 properties={"error": "invalid_check_pack"}),
    ]
    path = tmp_path / "junit.xml"
    path.write_bytes(cli.render_junit(synthetic(cases, "error")))
    root = junit(path)
    assert path.read_bytes().startswith(b'<?xml version="1.0" encoding="UTF-8"?>')
    by_name = {case.get("name"): case for case in root.iter("testcase")}
    failure = by_name["an error"].find("failure")
    assert failure.get("type") == "error" and failure.text == "- [error] broken"
    assert by_name["a warning"].find("failure") is None  # below --fail-on error
    assert "below --fail-on error" in by_name["a warning"].find("system-out").text
    assert by_name["skipped"].find("skipped").get("message") == "needs judgment"
    assert by_name["config"].find("error").get("type") == "invalid_check_pack"
    assert by_name["passes"].find("properties") is None  # not in junit-10.xsd's testcase
    assert by_name["passes"].find("system-out").text == "fine"
    assert root.get("failures") == "1" and root.get("errors") == "1"
    assert root.get("skipped") is None and root.get("timestamp") is None
    path.write_bytes(cli.render_junit(synthetic(cases, "warning")))
    assert junit(path).get("failures") == "2"


def test_junit_escapes_markup_and_drops_what_xml_cannot_carry(tmp_path):
    text = 'R&D <tag> "quoted" \'single\' \x00\x07\x1b\ud800 end ✓ \U0001F600'
    cases = [cli.case("x", text, "tool.demo", "fail", severity="error", message=text,
                      detail=text, properties={text: text})]
    result = synthetic(cases)
    result["suites"][0]["properties"] = {text: text}
    path = tmp_path / "junit.xml"
    path.write_bytes(cli.render_junit(result))
    root = junit(path)
    clean = 'R&D <tag> "quoted" \'single\'  end ✓ \U0001F600'
    case = root.find("testsuite/testcase")
    assert case.get("name") == clean
    assert case.find("failure").get("message") == clean
    assert case.find("failure").text == clean
    prop = root.find("testsuite/properties/property[3]")
    assert (prop.get("name"), prop.get("value")) == (clean, " ".join(clean.split()))
    assert cli.xml_text("a\x0bb\tc\nd\re￾") == "ab\tc\nd\re"


def test_a_real_run_gives_valid_junit_for_every_suite_kind(tmp_path, isolated):
    prj = make_project(tmp_path / "Board", pcb=True)
    write_json(prj.parent / "check_packs.json", DEMO_PACK)
    out = tmp_path / "junit.xml"
    run(prj, "--checklist", "bom", "--checks", "run_erc,check_release_readiness,check_dfm",
        "--drc-report", "--junit", out)
    kinds = {prop.get("value") for prop in junit(out).iter("property")
             if prop.get("name") == "kind"}
    assert {"checklist", "pack", "tool", "drc"} <= kinds


# ---------- Markdown ----------

def test_markdown_lists_failures_with_evidence_and_escapes_tables():
    cases = [cli.case("e", "a|b", "tool.demo", "fail", severity="error",
                      message="broken | badly <here>",
                      properties={"anchors": "designators: X1"}),
             cli.case("s", "later", "tool.demo", "skip", message="needs judgment")]
    result = synthetic(cases)
    result["design_state"] = {"kind": "git", "commit": "0123456789abcdef", "branch": "main",
                              "changes_since_commit": False}
    text = cli.render_markdown(result)
    assert "**FAIL** (exit 1, `--fail-on error`)" in text
    assert "| demo | 0 | 1 | 0 | 1 | 0 | 2 |" in text
    assert "- **a\\|b** (error): broken \\| badly &lt;here&gt; Anchors: designators: X1." in text
    assert "**git `0123456789ab`** on `main`, design files as committed" in text
    assert "<details><summary>Not evaluated (1)" in text


def test_markdown_shows_design_text_as_written_never_as_markup():
    hostile = "ctl \x01\x07\x1b[31m red [a](http://example.invalid) `x` *b* \\ | <i> \ud800 end"
    cases = [cli.case("e", hostile, "tool.demo", "fail", severity="error", message=hostile,
                      properties={"anchors": hostile})]
    result = synthetic(cases)
    result["project"] = {"prj_pcb": "Odd `tick` | name.PrjPcb", "variant": hostile}
    result["notes"] = [hostile]
    text = cli.markdown_fitting(result)
    text.encode("utf-8")  # no lone surrogate left
    assert not [ch for ch in text if ord(ch) < 32 and ch != "\n"]
    assert "[a](http" not in text and "\\[a\\](http" in text  # no link
    assert "\\`x\\`" in text and "\\*b\\*" in text and "\\\\" in text
    assert "&lt;i&gt;" in text and "<i>" not in text
    assert "| Project | ``Odd `tick` \\| name.PrjPcb`` |" in text
    assert cli.code("`a`") == "`` `a` ``" and cli.code("") == ""


def test_a_lone_surrogate_in_a_pack_does_not_stop_the_reports(tmp_path, isolated,
                                                              monkeypatch):
    """A valid JSON escape of a lone surrogate in a pack title: every report is still
    written, as valid UTF-8."""
    prj = make_project(tmp_path / "Board")
    (prj.parent / "check_packs.json").write_text(
        json.dumps(DEMO_PACK).replace('"Demo pack"', '"Demo \\ud800 pack"')
        .replace('"No resistors at all"', '"No resistors \\udfff at all"'), encoding="utf-8")
    summary = tmp_path / "step.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    out = tmp_path / "out"
    code = run(prj, "--checklist", "none", "--junit", out / "junit.xml", "--markdown",
               out / "summary.md", "--json", out / "result.json")
    assert code == 1
    junit(out / "junit.xml")
    markdown = (out / "summary.md").read_bytes().decode("utf-8")
    assert "Check pack demo: Demo  pack" not in markdown and "Demo pack" in markdown
    assert summary.read_bytes().decode("utf-8").strip() == markdown.strip()
    json.loads((out / "result.json").read_text(encoding="utf-8"))


def test_an_unexpected_exception_exits_2_with_the_reports(tmp_path, isolated, capsys):
    prj = make_project(tmp_path / "Board")
    write_json(prj.parent / "check_packs.json", DEMO_PACK)
    out = tmp_path / "out"
    # In one suite: that suite is an internal error, the others still run.
    with patch.object(cli, "pack_suites", side_effect=KeyError("boom")):
        code = run(prj, "--checklist", "none", "--checks", "list_variants", "--json",
                   out / "result.json", "--junit", out / "junit.xml")
    assert code == 2
    assert "Traceback" in capsys.readouterr().err
    result = json.loads((out / "result.json").read_text(encoding="utf-8"))
    assert result["errors"] == [{"suite": "packs", "error": "internal_error",
                                 "message": "KeyError: 'boom'"}]
    assert suite_of(result, "tool:list_variants")["cases"]
    assert junit(out / "junit.xml").get("errors") == "1"
    # Outside the suites: the whole run is an internal error, still reported.
    with patch.object(cli, "load_suite", side_effect=ValueError("bad")):
        code = run(prj, "--checklist", "none", "--json", out / "result.json")
    assert code == 2
    result = json.loads((out / "result.json").read_text(encoding="utf-8"))
    assert result["errors"] == [{"suite": "run", "error": "internal_error",
                                 "message": "ValueError: bad"}]


def test_markdown_shrinks_to_fit_the_step_summary():
    cases = [fail("error", f"case {i}") | {"message": "x" * 400} for i in range(300)]
    result = synthetic(cases)
    full = cli.render_markdown(result)
    short = cli.markdown_fitting(result, limit=20_000)
    assert len(full.encode()) > 20_000 >= len(short.encode())
    assert "more (see the JUnit or JSON output)" in short


# ---------- converting tool results ----------

class RC:
    """The review checklist helpers the converters use (the module main imported)."""
    module = sys.modules[main.build_review_checklist.__module__]


def test_findings_group_by_check_with_the_highest_severity():
    data = {"findings": [
        {"check": "alpha", "severity": "warning", "issue": "first alpha issue", "net": "N1"},
        {"check": "alpha", "severity": "error", "issue": "second alpha issue"},
        {"check": "beta", "severity": "info", "issue": "a note only"},
    ], "checks_run": ["alpha", "beta"], "severity_counts": {"error": 1}}
    suite = cli.tool_suite(RC.module, "check_demo", {"data": data, "pages": 1}, 0.1)
    alpha, beta = suite["cases"]
    assert (alpha["id"], alpha["status"], alpha["severity"]) == ("alpha", "fail", "error")
    assert alpha["message"].startswith("2 finding(s) (error x1, warning x1)")
    assert "nets: N1" in alpha["properties"]["anchors"]
    assert (beta["status"], beta["severity"]) == ("pass", None)  # info never fails
    assert suite["properties"]["checks_run"] == "alpha, beta"


def test_erc_levels_and_advisories():
    data = {"violations": [{"violation": "Bad thing", "level": "Fatal Error",
                            "message": "a bad thing happened"},
                           {"violation": "Odd thing", "level": "Warning",
                            "message": "an odd thing here"}],
            "advisories": [{"advisory": "notes", "level": "info", "message": "just so you know"}]}
    cases = cli.finding_cases(RC.module, "run_erc", "tool:run_erc", data)
    assert [(c["id"], c["status"], c["severity"]) for c in cases] == [
        ("Bad thing", "fail", "error"), ("Odd thing", "fail", "warning"),
        ("notes", "pass", None)]
    assert cli.finding_cases(RC.module, "t", "s", {"findings": []})[0]["id"] == "no_findings"
    assert cli.finding_cases(RC.module, "t", "s", {"lines": []}) is None


def test_tools_without_findings_and_tool_errors_are_skipped():
    informational = cli.tool_suite(RC.module, "get_bom", {"data": {"lines": []}}, 0)
    assert informational["cases"][0]["status"] == "skip"
    no_pcb = cli.tool_suite(RC.module, "check_dfm", {"error": "no_pcb_document",
                                                     "message": "no board"}, 0)
    assert no_pcb["cases"][0]["status"] == "skip"
    assert no_pcb["cases"][0]["message"].startswith("not applicable: no_pcb_document")
    crashed = cli.tool_suite(RC.module, "check_dfm", {"error": "tool_failed",
                                                      "exception": "KeyError",
                                                      "message": "check_dfm failed"}, 0)
    assert crashed["cases"][0]["message"].startswith("could not run")
    truncated = cli.tool_suite(RC.module, "check_demo",
                               {"data": {"findings": []}, "truncated": True}, 0)
    assert truncated["cases"][-1]["id"] == "truncated"
    assert truncated["cases"][-1]["status"] == "skip"


def test_release_gates():
    data = {"verdict": "fail", "gates": [
        {"id": "g1", "title": "One", "status": "fail", "blocking": True, "evidence": "e1"},
        {"id": "g2", "title": "Two", "status": "fail", "blocking": False, "evidence": "e2"},
        {"id": "g3", "title": "Three", "status": "warn", "blocking": True, "evidence": "e3"},
        {"id": "g4", "title": "Four", "status": "not_verified", "blocking": True},
        {"id": "g5", "title": "Five", "status": "not_applicable", "blocking": False},
        {"id": "g6", "title": "Six", "status": "pass", "blocking": True},
    ]}
    suite = cli.tool_suite(RC.module, "check_release_readiness", {"data": data}, 0)
    assert [(c["status"], c["severity"]) for c in suite["cases"]] == [
        ("fail", "error"), ("fail", "warning"), ("fail", "warning"), ("skip", None),
        ("skip", None), ("pass", None)]
    assert suite["properties"]["verdict"] == "fail"


def test_rule_kinds_lengths_and_drc_reports():
    kinds = {"kinds": [{"kind": "Width", "violation_count": 2, "violation_groups": 1,
                        "violations": [{"net": "N1", "count": 2}], "objects_checked": 9},
                       {"kind": "HoleSize", "violation_count": 0, "objects_checked": 4},
                       {"kind": "Clearance", "skipped": "no rule"}],
             "not_checked": [{"kind": "Corners", "reason": "not a DRC rule"}]}
    cases = cli.tool_suite(RC.module, "check_rule_compliance", {"data": kinds}, 0)["cases"]
    assert [(c["id"], c["status"]) for c in cases] == [
        ("Width", "fail"), ("HoleSize", "pass"), ("Clearance", "skip"), ("Corners", "skip")]
    assert "net=N1" in cases[0]["detail"]
    lengths = {"rules": [{"rule": "L1", "summary": {"members": 3, "violations": 1}},
                         {"rule": "L2", "summary": {"members": 2, "violations": 0}},
                         {"rule": "L3", "status": "no_members", "summary": {"members": 0}}]}
    cases = cli.tool_suite(RC.module, "check_lengths", {"data": lengths}, 0)["cases"]
    assert [c["status"] for c in cases] == ["fail", "pass", "skip"]
    drc = {"report": {"file": "out/Design Rule Check - Demo.html"}, "rules_checked": 5,
           "assessment": [{"issue": "older_than_board", "severity": "warning", "detail": "old"},
                          {"issue": "no_health_section", "severity": "info", "detail": "none"}],
           "groups": [{"rule": "Clearance Constraint", "count": 3, "rule_kind": "Clearance",
                       "components": {"X1": 2}, "nets": {"N1": 3},
                       "examples": [{"message": "too close"}]}],
           "health_issues": [{"title": "Unused pads", "count": 2, "examples": []}],
           "waived": [{"title": "Waived", "count": 1, "examples": []}]}
    suite = cli.tool_suite(RC.module, "get_drc_report", {"data": drc}, 0)
    assert suite["id"] == "drc-report" and suite["kind"] == "drc"
    assert [(c["id"], c["status"], c["severity"]) for c in suite["cases"]] == [
        ("assessment.older_than_board", "fail", "warning"),
        ("assessment.no_health_section", "pass", None),
        ("Clearance Constraint", "fail", "error"),
        ("health.Unused pads", "fail", "warning"),
        ("waived.Waived", "pass", None)]
    assert suite["properties"]["report"] == "out/Design Rule Check - Demo.html"
    missing = cli.tool_suite(RC.module, "get_drc_report",
                             {"error": "no_drc_report", "message": "none found"}, 0)
    assert (missing["cases"][0]["status"], missing["cases"][0]["severity"]) == ("fail", "error")


def test_drc_report_with_a_board_and_no_report_fails(tmp_path, isolated):
    prj = make_project(tmp_path / "Board", pcb=True)
    code, result = run_json(tmp_path, prj, "--checklist", "none", "--drc-report")
    assert code == 1
    report = suite_of(result, "drc-report")["cases"][0]
    assert (report["status"], report["severity"]) == ("fail", "error")


def test_drc_report_without_a_board_is_not_applicable(tmp_path, isolated):
    prj = make_project(tmp_path / "Board")
    code, result = run_json(tmp_path, prj, "--checklist", "none", "--drc-report")
    assert code == 0
    assert suite_of(result, "drc-report")["cases"][0]["status"] == "skip"


# ---------- tool arguments ----------

def test_tool_defaults_fill_missing_arguments_and_are_restored():
    def check_demo(net=None, rails=None, offset=0):
        return json.dumps({"rails": rails, "net": net})

    def other(offset=0):
        return "{}"

    server = types.SimpleNamespace(check_demo=check_demo, other=other)
    with cli.tool_defaults(server, ["check_demo", "other", "missing"],
                           {"rails": "VIN=24"}) as wrapped:
        assert wrapped == ["check_demo"]
        assert json.loads(server.check_demo())["rails"] == "VIN=24"
        assert json.loads(server.check_demo(rails="V=5"))["rails"] == "V=5"
        assert json.loads(server.check_demo("N1"))["net"] == "N1"
        assert list(inspect.signature(server.check_demo).parameters) == ["net", "rails",
                                                                         "offset"]
        assert server.other is other
    assert server.check_demo is check_demo


def test_rails_reach_the_tools_that_take_them(tmp_path, isolated):
    prj = make_project(tmp_path / "Board")
    calls = []

    def check_derating(part=None, net=None, checks=None, rails=None, vref=None, policy=None,
                       offset=0):
        calls.append((rails, vref))
        return json.dumps({"findings": [], "checks_run": []})

    with patch.object(main, "check_derating", check_derating):
        code = run(prj, "--checklist", "none", "--packs", "none", "--checks",
                   "check_derating", "--rails", "VIN=24", "--vref", "0.8")
        assert main.check_derating is check_derating  # restored after the run
    assert code == 0 and calls == [("VIN=24", "0.8")]


# ---------- check packs and plugins ----------

def test_packs_found_next_to_the_project_run_with_their_severities(tmp_path, isolated):
    prj = make_project(tmp_path / "Board")
    write_json(prj.parent / "check_packs.json", DEMO_PACK)
    code, result = run_json(tmp_path, prj, "--checklist", "none")
    pack = suite_of(result, "pack:demo")
    assert pack["properties"]["file"] == "check_packs.json (project folder)"
    err, warn, na, ok = (case_of(pack, rule) for rule in
                         ("DEMO-ERR", "DEMO-WARN", "DEMO-NA", "DEMO-OK"))
    assert (err["status"], err["severity"]) == ("fail", "error")
    assert (warn["status"], warn["severity"]) == ("fail", "warning")
    assert na["status"] == "skip" and na["message"] == "not applicable: nothing selected"
    assert ok["status"] == "pass" and ok["message"] == "0 checked, none failed"
    assert "is a resistor" in err["detail"]
    assert code == 1
    assert run(prj, "--checklist", "none", "--fail-on", "never") == 0


def test_a_warning_pack_fails_only_on_warnings(tmp_path, isolated):
    prj = make_project(tmp_path / "Board")
    pack = write_json(tmp_path / "packs" / "warn.json", WARN_PACK)
    assert run(prj, "--checklist", "none", "--packs", "none", "--checks", "list_variants") == 0
    assert run(prj, "--checklist", "none", "--pack-file", pack) == 0
    assert run(prj, "--checklist", "none", "--pack-file", pack, "--fail-on", "warning") == 1


def test_pack_selection_and_pack_errors(tmp_path, isolated, capsys):
    prj = make_project(tmp_path / "Board")
    write_json(prj.parent / "check_packs.json", DEMO_PACK)
    code, result = run_json(tmp_path, prj, "--checklist", "none", "--packs", "none",
                            "--checks", "list_variants")
    assert code == 0 and not [s for s in result["suites"] if s["kind"] == "pack"]
    assert run(prj, "--checklist", "none", "--packs", "other") == 2
    assert "unknown_pack" in capsys.readouterr().err
    write_json(prj.parent / "check_packs.json", {**DEMO_PACK, "colour": "red"})
    out = tmp_path / "junit.xml"
    assert run(prj, "--checklist", "none", "--junit", out) == 2
    assert "invalid_check_pack" in capsys.readouterr().err
    assert junit(out).find("testsuite/testcase/error").get("type") == "invalid_check_pack"


def test_no_packs_is_a_note_not_an_error(tmp_path, isolated):
    prj = make_project(tmp_path / "Board")
    code, result = run_json(tmp_path, prj, "--checklist", "none", "--checks", "list_variants")
    assert code == 0 and not result["errors"]
    assert "No check pack was found" in result["notes"][0]


def test_a_run_that_checks_nothing_is_not_a_pass(tmp_path, isolated, capsys):
    prj = make_project(tmp_path / "Board")
    out = tmp_path / "junit.xml"
    # Known before loading: exit 2, and the report says why.
    assert run(prj, "--checklist", "none", "--packs", "none", "--junit", out) == 2
    assert "Nothing to check" in capsys.readouterr().err
    assert junit(out).find("testsuite/testcase/error").get("type") == "nothing_to_run"
    assert main._project is None
    # Known only after looking: no checklist and no pack found.
    code, result = run_json(tmp_path, prj, "--checklist", "none")
    assert code == 2 and result["errors"][0]["error"] == "nothing_ran"
    assert "No check pack was found" in result["notes"][0]
    assert "nothing_ran" in capsys.readouterr().err


PLUGIN = "def check(api):\n    return [{'message': 'the plugin found one', 'severity': 'warning'}]\n"


@pytest.mark.parametrize("flag, env, status", [
    (False, None, "skip"), (False, "1", "skip"), (True, None, "skip"), (True, "1", "fail"),
])
def test_plugins_need_the_flag_and_the_environment(tmp_path, isolated, monkeypatch, flag, env,
                                                   status):
    prj = make_project(tmp_path / "Board")
    plugin = prj.parent / "check_packs" / "plugins" / "demo_plugin.py"
    plugin.parent.mkdir(parents=True)
    plugin.write_text(PLUGIN, encoding="utf-8")
    if env:
        monkeypatch.setenv("ALTIUM_COPILOT_ALLOW_PLUGINS", env)
    argv = ["--checklist", "none", *(["--allow-plugins"] if flag else [])]
    code, result = run_json(tmp_path, prj, *argv)
    case = suite_of(result, "pack:plugins")["cases"][0]
    assert case["id"] == "demo_plugin" and case["status"] == status
    if status == "fail":
        assert case["severity"] == "warning" and "the plugin found one" in case["detail"]
    else:
        assert "not run" in case["message"] or "ALTIUM_COPILOT_ALLOW_PLUGINS" in case["message"]
        assert any("plugins were not run" in note for note in result["notes"])
    assert code == 0  # a warning: below --fail-on error


def test_a_team_checklist_custom_item_takes_its_rules_severity(tmp_path, isolated):
    prj = make_project(tmp_path / "Board")
    write_json(prj.parent / "check_packs.json", DEMO_PACK)
    write_json(prj.parent / "review_checklists.json", TEAM_CHECKLIST)
    code, result = run_json(tmp_path, prj, "--checklist", "team", "--packs", "none")
    team = suite_of(result, "checklist:team")
    item = case_of(team, "TEAM-01")
    assert item["properties"]["status"] == "no"
    assert (item["status"], item["severity"]) == ("fail", "warning")
    assert case_of(team, "TEAM-02")["status"] == "skip"
    assert code == 0
    assert run(prj, "--checklist", "team", "--packs", "none", "--fail-on", "warning") == 1


def test_custom_items_see_the_pack_file_too(tmp_path, isolated):
    prj = make_project(tmp_path / "Board")
    pack = write_json(tmp_path / "ci" / "packs.json", DEMO_PACK)
    write_json(prj.parent / "review_checklists.json", TEAM_CHECKLIST)
    code, result = run_json(tmp_path, prj, "--checklist", "team", "--pack-file", pack,
                            "--fail-on", "warning")
    item = case_of(suite_of(result, "checklist:team"), "TEAM-01")
    assert (item["properties"]["status"], item["severity"]) == ("no", "warning")
    assert case_of(suite_of(result, "pack:demo"), "DEMO-WARN")["status"] == "fail"
    assert code == 1


# ---------- design diff ----------

def git(cwd: Path, *args: str) -> str:
    return subprocess.run([GIT, "-c", "user.name=ci", "-c", "user.email=ci@example.invalid",
                           "-c", "commit.gpgsign=false", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout


@pytest.mark.skipif(GIT is None, reason="git is not installed")
def test_diff_base_summarises_changes_without_touching_the_work_tree(tmp_path, isolated):
    repo = tmp_path / "repo"
    prj = make_project(repo / "hw")
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    with prj.open("a", encoding="utf-8") as handle:
        handle.write("[Parameter1]\nName=Revision\nValue=B\n")
    git(repo, "commit", "-q", "-am", "revision B")
    head = git(repo, "rev-parse", "HEAD").strip()
    out = tmp_path / "out"
    code = run(prj, "--checklist", "none", "--diff-base", "HEAD~1", "--json",
               out / "result.json", "--markdown", out / "summary.md", "--junit",
               out / "junit.xml")
    assert code == 0
    assert git(repo, "status", "--porcelain") == ""  # the work tree is untouched
    assert git(repo, "rev-parse", "HEAD").strip() == head
    result = json.loads((out / "result.json").read_text(encoding="utf-8"))
    assert result["design_state"]["kind"] == "git"
    assert result["design_state"]["commit"] == head
    diff = result["diff"]
    assert diff["summary"] == {"project": {"parameters": 1}}
    assert diff["changes"][0]["text"] == "Project parameters: add Revision = B"
    project = case_of(suite_of(result, "design-diff"), "project")
    assert project["status"] == "pass"
    markdown = (out / "summary.md").read_text(encoding="utf-8")
    assert "## Design diff against `HEAD~1`" in markdown
    assert "- [project] Project parameters: add Revision = B" in markdown
    assert f"**git `{head[:12]}`**" in markdown
    junit(out / "junit.xml")


@pytest.mark.skipif(GIT is None, reason="git is not installed")
def test_an_unknown_diff_base_exits_2(tmp_path, isolated, capsys):
    repo = tmp_path / "repo"
    prj = make_project(repo)
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    code, result = run_json(tmp_path, prj, "--checklist", "none", "--diff-base", "no-such-ref")
    assert code == 2
    assert result["errors"][0]["error"] == "revision_not_found"
    assert "design-diff: revision_not_found" in capsys.readouterr().err


@pytest.mark.skipif(GIT is None, reason="git is not installed")
def test_a_board_new_in_this_change_is_not_a_configuration_error(tmp_path, isolated,
                                                                 capsys):
    """A pull request that adds, moves or renames the board: the base revision has no
    such project, so there is nothing to compare; the other checks decide the run."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("design\n", encoding="utf-8")
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    base = git(repo, "rev-parse", "HEAD").strip()
    prj = make_project(repo / "hw")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "add the board")
    out = tmp_path / "out"
    code = run(prj, "--checklist", "none", "--checks", "list_variants", "--diff-base", base,
               "--json", out / "result.json", "--markdown", out / "summary.md", "--junit",
               out / "junit.xml")
    assert code == 0 and git(repo, "status", "--porcelain") == ""
    result = json.loads((out / "result.json").read_text(encoding="utf-8"))
    assert result["errors"] == []
    case = suite_of(result, "design-diff")["cases"][0]
    assert (case["id"], case["status"]) == ("not_in_base", "skip")
    assert "adds, moves or renames it" in case["message"]
    assert "adds, moves or renames it" in (out / "summary.md").read_text(encoding="utf-8")
    assert "Design diff: The project is not in" in capsys.readouterr().out
    junit(out / "junit.xml")


def test_ict_hazards_are_warnings():
    summary = cli.diff_summary({
        "a": {"commit": "abc"}, "total_changes": 2,
        "summary": {"ict_hazards": {"designator_reused": 1}, "components": {"added": 1}},
        "changes": [{"section": "ict_hazards", "change": "designator_reused", "eco": "Warning",
                     "text": "Designator X1 is now a different part"},
                    {"section": "components", "change": "added", "eco": "Add",
                     "text": "Added X2"}]}, "base")
    suite = cli.diff_suite(summary, 0.1)
    hazard, components = suite["cases"]
    assert (hazard["status"], hazard["severity"]) == ("fail", "warning")
    assert components["status"] == "pass" and "Added X2" in components["detail"]
    assert suite["properties"]["base_commit"] == "abc"


# ---------- headless guarantees ----------

def test_the_mcp_server_still_starts_without_a_subcommand():
    saved = sys.stdout, sys.stderr
    try:
        with patch.object(main.mcp, "run") as serve, \
                patch.object(main.threading, "Thread") as thread, \
                patch.object(cli, "main") as check:
            assert main.run_entry([]) == 0
    finally:
        for stream in (sys.stdout, sys.stderr):
            if stream not in saved:
                stream.close()
        sys.stdout, sys.stderr = saved
    serve.assert_called_once_with()
    thread.assert_called_once()
    assert thread.call_args.kwargs["target"] is main._check_for_update
    check.assert_not_called()


def test_version_and_check_do_not_start_the_server(capsys):
    with patch.object(main.mcp, "run", side_effect=_forbidden), \
            patch.object(main.threading, "Thread", side_effect=_forbidden):
        assert main.run_entry(["--version"]) == 0
        assert capsys.readouterr().out.strip() == f"altium-copilot-kc v{main._read_version()}"
        with patch.object(cli, "main", return_value=7) as check:
            assert main.run_entry(["check", "Board.PrjPcb", "--quiet"]) == 7
        check.assert_called_once_with(["Board.PrjPcb", "--quiet"], server=main)


def test_the_headless_load_writes_no_registry(tmp_path, isolated):
    prj = make_project(tmp_path / "Board")
    response = main.load_project_headless(str(prj))
    assert response["loaded"] is True and response["load_mode"] == "offline_netlist"
    assert "altium365" not in response


def test_a_fresh_interpreter_run_writes_only_its_outputs(tmp_path):
    """As a CI job runs it: python server/main.py check ..., with an empty home folder
    that must stay empty (no registry, no state file, no update check)."""
    prj = make_project(tmp_path / "Board")
    home = tmp_path / "home"
    home.mkdir()
    before = folder_state(prj.parent)
    env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home), "PYTHONUTF8": "1"}
    env.pop("GITHUB_STEP_SUMMARY", None)
    out = tmp_path / "out"
    proc = subprocess.run(
        [sys.executable, str(REPO / "server" / "main.py"), "check", str(prj), "--checklist",
         "none", "--checks", "run_erc", "--junit", str(out / "junit.xml"), "--json",
         str(out / "result.json")], capture_output=True, text=True, env=env, cwd=tmp_path,
        timeout=300)
    assert proc.returncode in (0, 1), proc.stderr
    assert "Result:" in proc.stdout
    assert list(home.rglob("*")) == []
    assert folder_state(prj.parent) == before
    junit(out / "junit.xml")
    result = json.loads((out / "result.json").read_text(encoding="utf-8"))
    assert suite_of(result, "tool:run_erc")["cases"]


def test_the_frozen_bundle_reads_its_own_checklists_and_version(tmp_path, isolated,
                                                               monkeypatch):
    bundle = tmp_path / "bundle"
    shutil.copytree(REPO / "server" / "checklists", bundle / "checklists")
    bom = json.loads((bundle / "checklists" / "bom.json").read_text(encoding="utf-8"))
    bom["title"] = "Bundled BOM checklist"
    write_json(bundle / "checklists" / "bom.json", bom)
    write_json(bundle / "manifest.json", {"version": "9.8.7"})
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
    monkeypatch.setattr(main, "_bundled_checklists", None)
    prj = make_project(tmp_path / "Board")
    code, result = run_json(tmp_path, prj, "--checklist", "bom", "--packs", "none")
    assert result["generator"] == "altium-copilot-kc 9.8.7"
    assert suite_of(result, "checklist:bom")["name"] == "Checklist bom: Bundled BOM checklist"


def test_release_build_bundles_what_the_check_command_needs():
    release = (REPO / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert '--add-data "server/checklists;checklists"' in release
    assert "--paths server" in release
    source = (REPO / "server" / "main.py").read_text(encoding="utf-8")
    assert "            import cli\n" in source  # a static import PyInstaller follows


def test_ci_examples_run_the_check_command():
    folder = REPO / "examples" / "ci"
    for name in ("github-actions.yml", "github-actions-windows.yml", "gitlab-ci.yml"):
        text = (folder / name).read_text(encoding="utf-8")
        assert " check " in text and "--junit" in text and "--diff-base" in text
        assert "ALTIUM_COPILOT_REF" in text
    assert "altium-copilot-kc.exe" in (folder / "github-actions-windows.yml").read_text(
        encoding="utf-8")
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    assert "## Continuous integration" in readme


def test_configuration_errors_name_the_file_and_the_place(tmp_path, isolated):
    prj = make_project(tmp_path / "Board")
    write_json(prj.parent / "review_checklists.json", {"name": "x", "disable": "SCH-PWR-01"})
    code, result = run_json(tmp_path, prj, "--checklist", "schematic", "--packs", "none")
    assert code == 2
    error = result["errors"][0]
    assert error["suite"] == "checklists"
    assert "review_checklists.json" in error["message"]
    assert cli.error_text({"error": "e", "message": "Bad.", "file": "f.json",
                           "where": "rules[0]", "hint": "Fix it."}) == "Bad. (f.json rules[0]) Fix it."
