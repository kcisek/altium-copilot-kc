"""Headless CI mode (roadmap #50): the check command.

    altium-copilot-kc.exe check <project> [options]
    python server/main.py check <project> [options]

runs the offline checks on a saved Altium project the way a CI job (GitHub Actions,
GitLab) needs them, with no Altium licence and no Windows: review checklists
(run_review_checklist's runner), the team's check packs (run_custom_checks), single tools
(run_erc, check_sch_pcb_sync, check_interfaces ...), the committed Batch DRC report
(get_drc_report) and a design diff against a base commit (diff_design's read-only git
export). It writes JUnit XML (--junit), a Markdown summary (--markdown, and
GITHUB_STEP_SUMMARY when the variable is set) and JSON (--json), prints a short summary,
and exits 1 when a failing result at or above --fail-on exists, 2 on a usage, load or
configuration error (or when nothing was checked, or the command itself failed), 0
otherwise. The reports are written in every case the output options can be read, and
name the problem.

Everything runs in-process through the same functions the MCP tools are: main.py passes
its own module in (`server`), so the tools, their caches and the services are the ones
the server uses. The project is loaded headless (main.load_project_headless): the
netlist is compiled offline from the saved sheets, Altium is never started or asked, the
project registry and the state file are not written, the update check does not run and
nothing is written into the project folder. Only the files named by --junit, --markdown
and --json (and GITHUB_STEP_SUMMARY) are written.

Settings: the ALTIUM_COPILOT_* environment variables the MCP server reads (roadmap #73:
Units, Fab profile, Derating policy, Default variant, Project folders, Allow check-pack
Python plugins) apply here too, through the same tools; --variant wins over the Default
variant, and Units applies to the --checks tools only (checklists and packs keep each
tool's own default unit, as in the MCP server). A setting that cannot be applied is a
configuration error (exit 2): a CI job must not pass with a setting silently ignored.
Two are notes instead: Project folders (only a pack's where-used rule reads them) and a
plugin switch other than true or 1 (the plugins stay off, as they always did). The
update check never runs here, so ALTIUM_COPILOT_CHECK_UPDATES is not read.

Statuses: the project load is a suite of its own (a sheet that cannot be read or a
netlist that cannot be compiled fails as an error: the checks after it would otherwise
pass on what was not read). A checklist item's Yes passes, No fails (severity error; a
custom item takes its check-pack rule's severity), N/A and needs judgment are skipped
(judgment items, and what could not run or be evaluated, are never passed). A pack rule
fails with its own severity. A single tool gives one test case per finding group (its
check, violation or gate) with the group's highest severity; info never fails.
--fail-on error (default) fails on errors, warning on warnings too, never exits 0
whatever is found (JUnit then marks errors and warnings as failures for the report).
"""

import argparse
import functools
import inspect
import json
import logging
import os
import re
import sys
import time
import traceback
import xml.etree.ElementTree as ET
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath

PROG = "altium-copilot-kc check"
EXIT_OK, EXIT_FAIL, EXIT_USAGE = 0, 1, 2
DEFAULT_CHECKLISTS = ("schematic", "release")
FAIL_ON = ("error", "warning", "never")
SEVERITIES = ("info", "warning", "error")
RANK = {level: rank for rank, level in enumerate(SEVERITIES)}
STATUSES = ("pass", "fail", "skip", "error")
PASSTHROUGH = ("rails", "hv", "vref")  # tool arguments given to every tool that takes them
FINDING_LISTS = ("findings", "violations", "advisories")
# Tool errors that mean the check does not apply (skipped as not applicable); any other
# error is skipped as could not run.
NOT_RUN_ERRORS = ("no_pcb_document", "netlist_required", "no_components_loaded",
                  "no_variants", "no_multiboard_system")
OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"  # Altium's binary documents
LFS_POINTER = b"version https://git-lfs"
# Tools a check pack's tool rule may name that answer in text, not JSON: --checks leaves
# them out (nothing in their answer passes or fails).
TEXT_TOOLS = ("get_design_notes",)
STEP_SUMMARY = "GITHUB_STEP_SUMMARY"
STEP_SUMMARY_BYTES = 1000 * 1024  # GitHub keeps 1 MiB of a step's summary
DETAIL_LINES = 25  # finding lines in one test case's text
CHECKLIST_STATUS = {"yes": "pass", "no": "fail", "n/a": "skip", "needs_judgment": "skip"}
CHECKLIST_LABELS = {"yes": "Yes", "no": "No", "n/a": "N/A", "needs_judgment": "Needs judgment"}
RULE_STATUS = {"pass": "pass", "fail": "fail", "not_applicable": "skip",
               "could_not_evaluate": "skip"}
GATE_STATUS = {"pass": "pass", "fail": "fail", "warn": "fail", "not_verified": "skip",
               "not_applicable": "skip"}
# (characters of evidence, failing cases listed per suite, skipped cases listed, diff
# changes listed) of the Markdown summary, tried in turn until it fits the step summary.
MARKDOWN_STEPS = ((400, 200, 300, 60), (240, 60, 100, 30), (160, 20, 30, 15), (100, 5, 0, 5))
# Characters XML 1.0 cannot carry (control characters, lone surrogates, non-characters).
_NOT_XML = re.compile("[^\t\n\r\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")
# What Markdown would read as markup in a table cell or a list line: escaped.
_MD_SPECIAL = re.compile(r"([\\`*\[\]|])")


class UsageError(Exception):
    """A usage, load or configuration problem: exit code 2."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class ArgumentsError(Exception):
    """The command line does not parse (an unknown option, a bad choice, no project)."""


class _Parser(argparse.ArgumentParser):
    """argparse that raises ArgumentsError instead of exiting, so the reports can still
    say what was wrong."""

    def error(self, message):
        raise ArgumentsError(message)


# ---------- arguments ----------

def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog=PROG,
        description="Run the offline Altium checks on a saved project (no Altium, no "
                    "licence, no Windows needed) and write JUnit XML, a Markdown summary "
                    "and JSON for CI.",
        epilog="Exit codes: 0 nothing fails at or above --fail-on, 1 something does, 2 a "
               "usage, load or configuration error (or nothing was checked).")
    parser.add_argument("project", help="the .PrjPcb file, or a folder holding exactly one")
    parser.add_argument("--checklist", action="append", metavar="NAMES",
                        help="review checklists to run, comma-separated: schematic, pcb, "
                             "release, bom or a team checklist's id; 'none' runs none "
                             "(default: schematic,release)")
    parser.add_argument("--checks", action="append", metavar="TOOLS",
                        help="single tools to run as well, comma-separated, e.g. "
                             "run_erc,check_sch_pcb_sync,check_interfaces")
    parser.add_argument("--packs", default="auto", metavar="IDS",
                        help="check packs to run: 'auto' (default: every pack found next to "
                             "the .PrjPcb or in the Git repository root, none if there is "
                             "none), 'none', or pack ids, comma-separated ('plugins' for "
                             "the plugin files)")
    parser.add_argument("--pack-file", metavar="FILE",
                        help="an extra check pack file (absolute or relative to the project "
                             "folder); the checklists' custom items see its rules too")
    parser.add_argument("--allow-plugins", action="store_true",
                        help="run the packs' Python plugins: they run only when "
                             "ALTIUM_COPILOT_ALLOW_PLUGINS=1 (or true) is set in the "
                             "environment too")
    parser.add_argument("--variant", help="the variant to check (default: the "
                                          "ALTIUM_COPILOT_DEFAULT_VARIANT setting when the "
                                          "project defines it, else the project's first, as "
                                          "the tools use)")
    parser.add_argument("--rails", help="net voltages for the tools that take rails=, e.g. "
                                        "'VIN=24,VBAT=12.6'")
    parser.add_argument("--hv", help="working voltages for check_hv_clearance, e.g. "
                                     "'L=230AC,N=0,HV_BUS=400'")
    parser.add_argument("--vref", help="feedback references for the tools that take "
                                       "vref=, e.g. 'U5=0.8,U7=0.6' or '0.6'")
    parser.add_argument("--diff-base", metavar="REF",
                        help="a Git revision (commit, branch, tag, HEAD~1) to summarise the "
                             "design changes against; read with git into a temporary "
                             "folder, the work tree is not touched")
    parser.add_argument("--drc-report", action="store_true",
                        help="read the committed Batch DRC report of the board (where "
                             "Altium writes it, else anywhere under the project folder)")
    parser.add_argument("--junit", metavar="FILE", help="write JUnit XML here")
    parser.add_argument("--markdown", metavar="FILE",
                        help="write the Markdown summary here (it is also appended to "
                             "$GITHUB_STEP_SUMMARY when that is set)")
    parser.add_argument("--json", metavar="FILE", help="write the full result as JSON here")
    parser.add_argument("--fail-on", choices=FAIL_ON, default="error",
                        help="exit 1 when a result fails at this level or above (default: "
                             "error); 'never' always exits 0")
    parser.add_argument("--quiet", action="store_true", help="print nothing but errors")
    return parser


def split_names(values: list[str] | None) -> list[str]:
    """Comma-separated values of a repeatable option, trimmed, each once, in order."""
    names: list[str] = []
    for value in values or []:
        for name in str(value).split(","):
            name = name.strip()
            if name and name.lower() not in (n.lower() for n in names):
                names.append(name)
    return names


def find_project(text: str) -> Path:
    """The .PrjPcb a project argument names: the file, or the only one in a folder."""
    path = Path(text).expanduser()
    try:
        if path.is_file():
            if path.suffix.lower() != ".prjpcb":
                raise UsageError("not_a_project", f"{path.name} is not a .PrjPcb file.")
            return path.resolve()
        if path.is_dir():
            found = sorted(p for p in path.iterdir()
                           if p.suffix.lower() == ".prjpcb" and p.is_file())
        else:
            raise UsageError("project_not_found", f"No file or folder at {text}.")
    except OSError as error:
        raise UsageError("project_not_found", f"{text} cannot be read: "
                                              f"{type(error).__name__}.") from error
    if not found:
        raise UsageError("no_prjpcb", f"No .PrjPcb file in {text}.")
    if len(found) > 1:
        raise UsageError("several_prjpcb",
                         f"{len(found)} .PrjPcb files in {text}: name the one to check "
                         f"({', '.join(p.name for p in found[:5])}).")
    return found[0].resolve()


def _module_of(function) -> object:
    """The module a function the server imported comes from: the same copy main.py uses
    (its classes, e.g. the error types, are the ones raised)."""
    return sys.modules[function.__module__]


# ---------- tool defaults (--rails, --hv, --vref) ----------

def _with_defaults(function, values: dict[str, str]):
    """function giving these keyword arguments their value when a call leaves them out
    or None; None when it takes none of them."""
    try:
        signature = inspect.signature(function)
    except (TypeError, ValueError):
        return None
    wanted = {key: value for key, value in values.items() if key in signature.parameters}
    if not wanted:
        return None

    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        bound = signature.bind_partial(*args, **kwargs)
        for key, value in wanted.items():
            if bound.arguments.get(key) is None:
                bound.arguments[key] = value
        return function(*bound.args, **bound.kwargs)

    return wrapper


@contextmanager
def tool_defaults(server, names, values: dict[str, str]):
    """For the run, every tool among names that takes rails=, hv= or vref= gets the
    command's value when a call leaves it out: a checklist item, a check pack's tool rule
    and --checks alike (they all call the module's tool functions)."""
    saved: dict[str, object] = {}
    try:
        if values:
            for name in names:
                function = getattr(server, name, None)
                wrapped = _with_defaults(function, values) if function else None
                if wrapped is not None:
                    saved[name] = function
                    setattr(server, name, wrapped)
        yield sorted(saved)
    finally:
        for name, function in saved.items():
            setattr(server, name, function)


# ---------- results ----------

def clip(text, limit: int) -> str:
    """One line of at most limit characters, without control characters or lone
    surrogates (no terminal escape sequence reaches the console or a report)."""
    text = " ".join(_NOT_XML.sub("", str(text if text is not None else "")).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def severity_of(value) -> str:
    """error, warning or info from a tool's severity or level ('Fatal Error', 'Error',
    'warn', 'info', 'No Report' ...)."""
    text = str(value or "").strip().lower()
    if "error" in text or text in ("fatal", "critical", "high"):
        return "error"
    if text.startswith("warn") or text == "medium":
        return "warning"
    return "info"


def case(case_id: str, name: str, classname: str, status: str, *, severity: str | None = None,
         message: str = "", detail: str = "", properties: dict | None = None,
         time_s: float = 0.0) -> dict:
    """One test case: status pass, fail (with severity), skip or error."""
    return {"id": case_id, "name": name, "classname": classname, "status": status,
            "severity": severity if status == "fail" else None, "message": message,
            "detail": detail, "properties": {k: v for k, v in (properties or {}).items()
                                             if v not in (None, "", [], {})},
            "time": round(time_s, 3)}


def suite(suite_id: str, name: str, kind: str, cases: list[dict], *, time_s: float = 0.0,
          properties: dict | None = None, **extra) -> dict:
    return {"id": suite_id, "name": name, "kind": kind, "time": round(time_s, 3),
            "properties": {k: v for k, v in (properties or {}).items()
                           if v not in (None, "", [], {})},
            "cases": cases, **extra}


def error_suite(suite_id: str, name: str, code: str, message: str, kind: str = "error",
                time_s: float = 0.0) -> dict:
    """A suite that could not run because of a usage, load or configuration problem."""
    return suite(suite_id, name, kind, [case(f"{suite_id}.{code}", f"{name}: {code}",
                                             _classname(suite_id), "error",
                                             message=clip(message, 600), detail=message,
                                             properties={"error": code})], time_s=time_s,
                 error={"error": code, "message": message})


def board_problem(path: str) -> str | None:
    """Why the board document cannot be read by the board tools, from its first bytes
    (no parse): None when it is an Altium binary document."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(64)
    except FileNotFoundError:
        return "file not found"
    except OSError as error:
        return f"cannot be read: {type(error).__name__}"
    if head.startswith(OLE_MAGIC):
        return None
    if head.startswith(LFS_POINTER):
        return "a Git LFS pointer, not the board: fetch the LFS objects in the checkout"
    return "not an Altium binary PcbDoc (an ASCII PcbDoc?): the board tools cannot read it"


def load_suite(load: dict, compiled: bool, time_s: float, board: str | None = None) -> dict:
    """The project load as a suite: the netlist compiled offline from the saved sheets (an
    error when it could not be: every netlist check would then be skipped and the run pass
    on what was not read), each sheet that could not be read (an error: a corrupt file, a
    Git LFS pointer, a document path whose case does not match the file on Linux), the
    board document (an error when missing or not a binary document, for the same
    reasons), each .Annotation file that could not be read (a warning: board designators
    may differ) and a netlist without component data (an error)."""
    suite_id = "load"
    cases = []
    if compiled:
        cases.append(case("netlist", "netlist compiled offline", _classname(suite_id), "pass",
                          message=f"{load.get('sheet_count', 0)} sheet(s) listed, "
                                  f"{load.get('component_count', 0)} component(s) read"))
    elif load.get("compile_error"):
        reason = f"The sheets could not be compiled offline: {load['compile_error']}"
        cases.append(case("netlist", "netlist compiled offline", _classname(suite_id), "fail",
                          severity="error", message=clip(reason, 500), detail=reason,
                          properties={"error": "compile_failed"}))
    else:
        cases.append(case("netlist", "netlist compiled offline", _classname(suite_id), "skip",
                          message="The project lists no schematic sheet to compile."))
    for error in load.get("sheet_errors") or []:
        if not isinstance(error, dict):
            continue
        sheet = str(error.get("sheet") or "sheet")
        reason = f"{error.get('path') or sheet} could not be read: {error.get('error')}"
        cases.append(case(f"sheet.{sheet}", f"sheet {sheet} read",
                          _classname(suite_id, "sheets"), "fail", severity="error",
                          message=clip(f"could not be read: {error.get('error')}", 500),
                          detail=reason))
    if board:
        problem = board_problem(board)
        name = PureWindowsPath(board).name
        cases.append(case("board", "board document present", _classname(suite_id),
                          "fail" if problem else "pass", severity="error",
                          message=clip(f"{name}: {problem or 'an Altium binary document'}",
                                       500),
                          detail=f"{board}: {problem}" if problem else ""))
    for error in load.get("annotation_errors") or []:
        if not isinstance(error, dict):
            continue
        name = PureWindowsPath(str(error.get("path") or "annotation")).name
        reason = f"{error.get('path') or name} was not applied: {error.get('error')}"
        cases.append(case(f"annotation.{name}", f"annotation {name} read",
                          _classname(suite_id, "annotation"), "fail", severity="warning",
                          message=clip(f"not applied: {error.get('error')}", 500),
                          detail=reason))
    if load.get("warning") == "no_components_loaded":
        message = str(load.get("message") or "No component data was read from the sheets.")
        cases.append(case("components", "component data read", _classname(suite_id), "fail",
                          severity="error", message=clip(message, 500), detail=message))
    return suite(suite_id, "Project load", "load", cases, time_s=time_s,
                 properties={"load_mode": load.get("load_mode"),
                             "sheets": str(load.get("sheet_count", "")),
                             "components": str(load.get("component_count", ""))})


def _classname(suite_id: str, *parts: str) -> str:
    text = ".".join([suite_id.replace(":", "."), *[p for p in parts if p]])
    return re.sub(r"\s+", "_", text)


def anchor_text(anchors: dict | None, limit: int = 8) -> str:
    """'designators: U1, U2 (+3 more); nets: 3V3' from a tool's anchors."""
    if not anchors:
        return ""
    parts = []
    for group in ("designators", "nets", "sheets", "other"):
        values = [str(v) for v in anchors.get(group) or []]
        if not values:
            continue
        total = anchors.get(f"{group}_total", len(values))
        shown = ", ".join(values[:limit])
        if total > min(limit, len(values)):
            shown += f" (+{total - min(limit, len(values))} more)"
        parts.append(f"{group}: {shown}")
    return "; ".join(parts)


# ---------- checklists ----------

def checklist_suite(model: dict, rule_severity: dict[str, str], time_s: float) -> dict:
    """A checklist run as a suite: one case per item."""
    checklist = model["checklist"]
    suite_id = f"checklist:{checklist['id']}"
    titles = {section["id"]: section["title"] for section in model["sections"]}
    cases = []
    for row in model["items"]:
        status = CHECKLIST_STATUS.get(row["status"], "skip")
        severity = None
        if status == "fail":
            severity = (rule_severity.get(str(row.get("rule", "")).lower(), "error")
                        if row.get("kind") == "custom" else "error")
        label = CHECKLIST_LABELS.get(row["status"], row["status"])
        if row.get("lean"):
            label += f" (leans {CHECKLIST_LABELS.get(row['lean'], row['lean'])})"
        evidence = row.get("evidence") or ""
        anchors = anchor_text(row.get("anchors"))
        detail_lines = [f"{row['id']}: {row['text']}", f"Status: {label}",
                        f"Evidence: {evidence}"]
        if anchors:
            detail_lines.append(f"Anchors: {anchors}")
        if row.get("tool"):
            args = json.dumps(row["args"], sort_keys=True) if row.get("args") else ""
            detail_lines.append(f"Tool: {row['tool']}{' ' + args if args else ''}")
        if row.get("hint"):
            detail_lines.append(f"Hint: {row['hint']}")
        cases.append(case(
            row["id"], f"{row['id']}: {clip(row['text'], 140)}",
            _classname(suite_id, row.get("section", "")), status, severity=severity,
            message=clip(f"{label}: {evidence}", 500), detail="\n".join(detail_lines),
            properties={
                "status": row["status"], "kind": row.get("kind"), "lean": row.get("lean"),
                "section": titles.get(row.get("section"), row.get("section")),
                "evidence": evidence, "anchors": anchors, "tool": row.get("tool"),
                "args": json.dumps(row["args"], sort_keys=True) if row.get("args") else None,
                "findings": ", ".join(f"{k} x{v}" for k, v in (row.get("findings") or {}).items()),
                "could_not_run": row.get("could_not_run"), "rule": row.get("rule"),
                "origin": row.get("origin")}))
    return suite(suite_id, f"Checklist {checklist['id']}: {checklist.get('title', '')}".strip(),
                 "checklist", cases, time_s=time_s,
                 properties={"checklist": checklist["id"], "version": checklist.get("version"),
                             "origin": checklist.get("origin", "bundled"),
                             "overrides": ", ".join(a.get("file", "") for a in
                                                    model.get("overrides_applied") or []),
                             "disabled": ", ".join(d.get("id", "") for d in
                                                   model.get("disabled") or [])},
                 coverage=model["coverage"])


# ---------- check packs ----------

def pack_suites(header: dict, model: dict) -> list[dict]:
    """One suite per pack (and one for the plugins): one case per rule."""
    findings_of: dict[tuple, list[dict]] = {}
    for finding in model["findings"]:
        findings_of.setdefault((finding["pack"], str(finding["check"]).lower()), []).append(finding)
    packs = [(item["id"], item["title"], item.get("version"), item.get("file"))
             for item in header.get("packs") or []]
    if any(row["pack"] == "plugins" for row in model["rules"]) or \
            (header.get("plugins") or {}).get("found"):
        packs.append(("plugins", "Python plugins", None, (header.get("plugins") or {}).get("folder")))
    suites = []
    for pack_id, title, version, file in packs:
        suite_id = f"pack:{pack_id}"
        cases = []
        rows = [row for row in model["rules"] if row["pack"] == pack_id]
        for row in rows:
            status = RULE_STATUS.get(row["status"], "skip")
            found = findings_of.get((pack_id, str(row["id"]).lower()), [])
            checked = row.get("checked", 0)
            if status == "pass" and row.get("not_evaluated"):
                # What was not evaluated is never passed (the checklist reads it the same).
                status = "skip"
                message = (f"{checked} checked, none failed; {row['not_evaluated']} could not "
                           f"be evaluated ({', '.join(row.get('not_evaluated_reasons') or {})})")
            elif status == "fail":
                counts = Counter(f["severity"] for f in found)
                message = f"{row.get('findings', len(found))} finding(s)" + (
                    " (" + ", ".join(f"{level} x{n}" for level, n in counts.items()) + ")"
                    if counts else "") + (f": {row['first']}" if row.get("first") else "")
            elif status == "pass":
                message = f"{checked} checked, none failed"
            else:
                message = f"{row['status'].replace('_', ' ')}: {row.get('reason') or ''}".strip(": ")
            lines = [f"{row['id']}: {row.get('title', '')}", message]
            for finding in found[:DETAIL_LINES]:
                anchors = anchor_text({"designators": finding.get("designators"),
                                       "nets": finding.get("nets")}, 5)
                lines.append(f"- [{finding['severity']}] {clip(finding.get('issue'), 300)}"
                             + (f" ({anchors})" if anchors else ""))
            if len(found) > DETAIL_LINES:
                lines.append(f"- ... {len(found) - DETAIL_LINES} more")
            severity = row.get("severity") if status == "fail" else None
            if status == "fail" and found:
                severity = max((severity_of(f["severity"]) for f in found), key=RANK.get)
            cases.append(case(
                row["id"], f"{row['id']}: {clip(row.get('title', ''), 140)}",
                _classname(suite_id), status, severity=severity_of(severity) if severity else None,
                message=clip(message, 500), detail="\n".join(lines),
                properties={"status": row["status"], "severity": row.get("severity"),
                            "kind": row.get("kind"), "tool": row.get("tool"),
                            "checked": str(checked), "findings": str(row.get("findings", 0)),
                            "not_evaluated": str(row["not_evaluated"])
                            if row.get("not_evaluated") else None,
                            "reason": row.get("reason"),
                            "anchors": anchor_text(row.get("anchors")),
                            "file": row.get("file")},
                time_s=(row.get("ms") or 0) / 1000))
        if pack_id == "plugins":
            plugins = header.get("plugins") or {}
            ran = {str(row["id"]).lower() for row in rows}
            for plugin in plugins.get("found") or []:
                if str(plugin).lower() in ran:
                    continue
                reason = plugins.get("reason") or "not selected by --packs"
                cases.append(case(str(plugin), f"{plugin}: plugin not run",
                                  _classname(suite_id), "skip", message=clip(reason, 500),
                                  detail=f"{reason}\n{plugins.get('note', '')}".strip(),
                                  properties={"reason": reason}))
        suites.append(suite(suite_id, f"Check pack {pack_id}: {title}", "pack", cases,
                            time_s=sum(c["time"] for c in cases),
                            properties={"pack": pack_id, "version": version, "file": file}))
    return suites


def rule_severities(packs: list[dict]) -> dict[str, str]:
    return {str(rule["id"]).lower(): severity_of(rule.get("severity"))
            for pack in packs for rule in pack.get("rules") or []}


# ---------- single tools ----------

def _finding_id(rc, row: dict, fallback: str) -> str:
    ids = rc.finding_ids([row])
    return next(iter(ids), fallback)


def _finding_line(rc, row: dict, tool: str) -> str:
    text = rc.row_text([row]) or next(
        (str(row[key]) for key in ("issue", "message", "evidence", "detail") if row.get(key)),
        None) or ", ".join(f"{key}={value}" for key, value in row.items()
                           if isinstance(value, (str, int, float)) and not isinstance(value, bool))
    anchors = anchor_text(rc.anchors_of([row], tool), 5)
    level = severity_of(row.get("severity") or row.get("level"))
    return f"- [{level}] {clip(text, 300)}" + (f" ({anchors})" if anchors else "")


def finding_cases(rc, tool: str, suite_id: str, data: dict) -> list[dict] | None:
    """One case per finding group of a tool's findings (or ERC violations and
    advisories), with the group's highest severity; None when the result has no such
    list (an informational tool)."""
    lists = [key for key in FINDING_LISTS if isinstance(data.get(key), list)]
    if not lists:
        return None
    groups: dict[str, list[dict]] = {}
    for key in lists:
        for row in data[key]:
            if isinstance(row, dict):
                groups.setdefault(_finding_id(rc, row, key), []).append(row)
    cases = []
    for group_id, rows in groups.items():
        severity = max((severity_of(r.get("severity") or r.get("level")) for r in rows),
                       key=RANK.get)
        status = "fail" if severity in ("error", "warning") else "pass"
        counts = Counter(severity_of(r.get("severity") or r.get("level")) for r in rows)
        first = rc.row_text(rows) or ""
        message = (f"{len(rows)} finding(s) ("
                   + ", ".join(f"{level} x{counts[level]}" for level in reversed(SEVERITIES)
                               if counts.get(level)) + ")" + (f": {first}" if first else ""))
        lines = [_finding_line(rc, row, tool) for row in rows[:DETAIL_LINES]]
        if len(rows) > DETAIL_LINES:
            lines.append(f"- ... {len(rows) - DETAIL_LINES} more")
        cases.append(case(group_id, group_id, _classname(suite_id), status, severity=severity,
                          message=clip(message, 500), detail="\n".join(lines),
                          properties={"findings": str(len(rows)),
                                      "anchors": anchor_text(rc.anchors_of(rows, tool))}))
    if not cases:
        cases.append(case("no_findings", "no findings", _classname(suite_id), "pass",
                          message="The tool reported no findings."))
    return cases


def gate_cases(suite_id: str, data: dict) -> list[dict]:
    """check_release_readiness: one case per gate; a blocking fail is an error, a
    non-blocking fail or a warn a warning, not_verified and not_applicable skipped."""
    cases = []
    for gate in data.get("gates") or []:
        status = GATE_STATUS.get(gate.get("status"), "skip")
        severity = "error" if gate.get("status") == "fail" and gate.get("blocking") else "warning"
        cases.append(case(gate.get("id", "gate"), f"{gate.get('id')}: {gate.get('title', '')}",
                          _classname(suite_id), status, severity=severity,
                          message=clip(f"{gate.get('status')}: {gate.get('evidence', '')}", 500),
                          detail=str(gate.get("evidence") or ""),
                          properties={"status": gate.get("status"),
                                      "blocking": str(bool(gate.get("blocking"))).lower(),
                                      "source_tool": gate.get("source_tool")},
                          time_s=(gate.get("ms") or 0) / 1000))
    return cases


def rule_kind_cases(rc, suite_id: str, data: dict) -> list[dict]:
    """check_rule_compliance: one case per rule kind (violations are errors, like the
    Batch DRC's), and the kinds it does not check skipped."""
    cases = []
    for kind in data.get("kinds") or []:
        if not isinstance(kind, dict):
            continue
        name = str(kind.get("kind") or "kind")
        if kind.get("skipped"):
            cases.append(case(name, name, _classname(suite_id), "skip",
                              message=clip(f"not checked: {kind['skipped']}", 500)))
            continue
        count = kind.get("violation_count") or 0
        undecided = (kind.get("undecided") or {}).get("count", 0) if isinstance(
            kind.get("undecided"), dict) else 0
        rows = kind.get("violations") if isinstance(kind.get("violations"), list) else []
        lines = [_finding_line(rc, row, "check_rule_compliance") for row in rows[:DETAIL_LINES]
                 if isinstance(row, dict)]
        if count:
            cases.append(case(name, name, _classname(suite_id), "fail", severity="error",
                              message=f"{count} violation(s) in {kind.get('violation_groups', 0)} "
                                      "group(s)", detail="\n".join(lines),
                              properties={"objects_checked": str(kind.get("objects_checked"))}))
        else:
            note = f"; {undecided} not decided" if undecided else ""
            cases.append(case(name, name, _classname(suite_id), "pass",
                              message=f"{kind.get('objects_checked', 0)} object(s) checked, "
                                      f"no violation{note}",
                              properties={"undecided": str(undecided) if undecided else None}))
    for item in data.get("not_checked") or []:
        if isinstance(item, dict):
            name = str(item.get("kind") or "not_checked")
            cases.append(case(name, name, _classname(suite_id), "skip",
                              message=clip(f"not checked: {item.get('reason', '')}", 500)))
    return cases


def length_cases(suite_id: str, data: dict) -> list[dict]:
    """check_lengths: one case per Length or MatchedLengths rule."""
    cases = []
    for rule in data.get("rules") or []:
        if not isinstance(rule, dict):
            continue
        name = str(rule.get("rule") or rule.get("name") or "rule")
        summary = rule.get("summary") or {}
        if summary.get("violations"):
            cases.append(case(name, name, _classname(suite_id), "fail", severity="error",
                              message=f"{summary['violations']} of {summary.get('members', '?')} "
                                      "member(s) violate the rule"))
        elif rule.get("status") in ("not_evaluated_delay_units", "no_members") or \
                summary.get("incomplete") or summary.get("unknown"):
            cases.append(case(name, name, _classname(suite_id), "skip",
                              message=clip(f"not evaluated: {rule.get('status') or ''} "
                                           f"{json.dumps(summary)}", 500)))
        else:
            cases.append(case(name, name, _classname(suite_id), "pass",
                              message=f"{summary.get('members', 0)} member(s) within the rule"))
    if not cases:
        cases.append(case("no_rules", "no length rules", _classname(suite_id), "skip",
                          message="The board has no Length or MatchedLengths rule."))
    return cases


def drc_cases(suite_id: str, data: dict) -> list[dict]:
    """get_drc_report: the assessment (whether to trust the report), one case per rule
    with violations (errors), PCB health issues and the report's warnings (warnings)."""
    cases = []
    for issue in data.get("assessment") or []:
        severity = severity_of(issue.get("severity"))
        cases.append(case(f"assessment.{issue.get('issue')}", f"report: {issue.get('issue')}",
                          _classname(suite_id, "assessment"),
                          "fail" if severity != "info" else "pass", severity=severity,
                          message=clip(issue.get("detail"), 500), detail=str(issue.get("detail"))))
    groups = data.get("groups") or []
    for group in groups:
        rule = str(group.get("rule") or "rule")
        lines = [f"- {clip(example.get('message'), 300)}" for example in
                 group.get("examples") or [] if isinstance(example, dict)]
        anchors = anchor_text({"designators": list(group.get("components") or {}),
                               "nets": list(group.get("nets") or {})})
        if anchors:
            lines.append(f"Anchors: {anchors}")
        cases.append(case(rule, rule, _classname(suite_id, "rules"), "fail", severity="error",
                          message=f"{group.get('count', 0)} violation(s)",
                          detail="\n".join(lines),
                          properties={"rule_kind": group.get("rule_kind"), "anchors": anchors}))
    if not groups:
        cases.append(case("rules", "rule violations", _classname(suite_id, "rules"), "pass",
                          message=f"{data.get('rules_checked', 0)} rule(s) checked, none "
                                  "violated"))
    for key, label, severity in (("health_issues", "health", "warning"),
                                 ("warnings", "warning", "warning"),
                                 ("waived", "waived", "info")):
        for table in data.get(key) or []:
            count = table.get("count") or 0
            failing = count and severity != "info"
            cases.append(case(f"{label}.{table.get('title')}", f"{label}: {table.get('title')}",
                              _classname(suite_id, label), "fail" if failing else "pass",
                              severity=severity, message=f"{count} row(s)",
                              detail="\n".join(f"- {clip(e.get('message'), 300)}" for e in
                                               table.get("examples") or [] if isinstance(e, dict))))
    return cases


def tool_suite(rc, tool: str, result: dict, time_s: float) -> dict:
    """A single tool's run (ToolRunner result) as a suite."""
    suite_id = "drc-report" if tool == "get_drc_report" else f"tool:{tool}"
    name = "Batch DRC report (get_drc_report)" if tool == "get_drc_report" else tool
    kind = "drc" if tool == "get_drc_report" else "tool"
    if "error" in result:
        code = str(result["error"])
        message = str(result.get("message") or code)
        if tool == "get_drc_report" and code == "no_drc_report":
            cases = [case("report", "report found", _classname(suite_id), "fail",
                          severity="error", message=clip(message, 500), detail=message)]
        else:
            reason = "not applicable" if code in NOT_RUN_ERRORS else "could not run"
            cases = [case(code, f"{tool}: {reason}", _classname(suite_id), "skip",
                          message=clip(f"{reason}: {code}: {message}", 500), detail=message,
                          properties={"error": code, "exception": result.get("exception")})]
        return suite(suite_id, name, kind, cases, time_s=time_s, properties={"error": code})
    data = result["data"]
    if tool == "check_release_readiness":
        cases = gate_cases(suite_id, data)
    elif tool == "check_rule_compliance":
        cases = rule_kind_cases(rc, suite_id, data)
    elif tool == "check_lengths":
        cases = length_cases(suite_id, data)
    elif tool == "get_drc_report":
        cases = drc_cases(suite_id, data)
    else:
        cases = finding_cases(rc, tool, suite_id, data)
        if cases is None:
            cases = [case("result", f"{tool}: informational", _classname(suite_id), "skip",
                          message="This tool reports information, not pass or fail: grade "
                                  "it with a check pack's tool rule.")]
    properties = {
        "verdict": data.get("verdict") if isinstance(data.get("verdict"), str) else None,
        "severity_counts": json.dumps(data["severity_counts"], sort_keys=True)
        if isinstance(data.get("severity_counts"), dict) else None,
        "checks_run": ", ".join(str(c) for c in data["checks_run"])
        if isinstance(data.get("checks_run"), list) else None,
        "pages": str(result.get("pages")),
        "truncated": "true" if result.get("truncated") else None,
    }
    if tool == "get_drc_report":
        report = data.get("report") or {}
        properties.update({"report": report.get("file"), "generated": report.get("generated"),
                           "made_for": report.get("made_for"),
                           "totals": json.dumps(data.get("totals"), sort_keys=True)})
    if result.get("truncated"):
        cases.append(case("truncated", "result complete", _classname(suite_id), "skip",
                          message="The tool's result had more pages than were read; the "
                                  "cases above do not cover all of it."))
    return suite(suite_id, name, kind, cases, time_s=time_s, properties=properties)


# ---------- design diff ----------

DIFF_SECTIONS = ("ict_hazards", "project", "components", "variants", "bom", "nets",
                 "placement", "rules", "classes", "diff_pairs", "stackup", "routing")


def diff_summary(model: dict, base: str, limit: int = 200) -> dict:
    """What the Markdown and JSON keep of a design diff: sources, counts per section,
    the ICT hazards and the first changes."""
    changes = model.get("changes") or []
    return {
        "base": base,
        "a": model.get("a"), "b": model.get("b"),
        "units": model.get("units"),
        "total_changes": model.get("total_changes", len(changes)),
        "summary": model.get("summary") or {},
        "ict_hazards": [entry for entry in changes if entry.get("section") == "ict_hazards"],
        "changes": [{key: entry.get(key) for key in ("section", "change", "eco", "text")}
                    for entry in changes[:limit]],
        "unchanged_or_not_compared": model.get("unchanged_or_not_compared") or {},
        "documents_not_read": model.get("documents_not_read") or {},
        "routing_totals": model.get("routing_totals"),
    }


def not_in_base_summary(payload: dict, base: str) -> dict:
    """The diff summary when the base revision has no such project: a change that adds,
    moves or renames the board has nothing to compare against (not a configuration
    problem; the other checks decide the run)."""
    commit = payload.get("commit")
    message = (f"The project is not in {base}"
               + (f" (commit {commit})" if commit and commit != base else "")
               + ": this change adds, moves or renames it, so there is nothing to compare "
                 "against.")
    return {"base": base, "a": {"commit": commit} if commit else {}, "b": None,
            "units": None, "total_changes": 0, "summary": {}, "ict_hazards": [],
            "changes": [], "unchanged_or_not_compared": {}, "documents_not_read": {},
            "routing_totals": None, "not_in_base": message}


def diff_suite(summary: dict, time_s: float) -> dict:
    """The design diff as a suite: one case per section that changed; ICT hazards (a net
    that kept its name while its pins changed, a designator now on another part) are
    warnings, other changes pass with what changed listed."""
    suite_id = "design-diff"
    if summary.get("not_in_base"):
        cases = [case("not_in_base", "project in the base revision", _classname(suite_id),
                      "skip", message=clip(summary["not_in_base"], 500),
                      detail=summary["not_in_base"])]
        return suite(suite_id, f"Design diff against {summary['base']}", "diff", cases,
                     time_s=time_s, properties={"base": summary["base"],
                                                "base_commit": (summary.get("a") or {}).get(
                                                    "commit")})
    by_section: dict[str, list[dict]] = {}
    for entry in summary["changes"]:
        by_section.setdefault(entry.get("section") or "other", []).append(entry)
    cases = []
    for section in [*DIFF_SECTIONS, *sorted(set(summary["summary"]) - set(DIFF_SECTIONS))]:
        counts = summary["summary"].get(section)
        if not counts:
            continue
        total = sum(counts.values())
        listed = summary["ict_hazards"] if section == "ict_hazards" else by_section.get(section, [])
        lines = [f"- {clip(entry.get('text'), 300)}" for entry in listed[:DETAIL_LINES]]
        message = f"{total} change(s): " + ", ".join(f"{k} x{v}" for k, v in counts.items())
        cases.append(case(section, section, _classname(suite_id),
                          "fail" if section == "ict_hazards" else "pass",
                          severity="warning", message=clip(message, 500),
                          detail="\n".join(lines)))
    if not cases:
        cases.append(case("no_changes", "no changes", _classname(suite_id), "pass",
                          message=f"The design files match {summary['base']}."))
    a = summary.get("a") or {}
    return suite(suite_id, f"Design diff against {summary['base']}", "diff", cases,
                 time_s=time_s,
                 properties={"base": summary["base"], "base_commit": a.get("commit"),
                             "total_changes": str(summary["total_changes"])})


# ---------- the run ----------

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _state_view(state: dict) -> dict:
    keys = ("id", "kind", "commit", "commit_date_utc", "branch", "changes_since_commit",
            "design_files_changed", "fingerprint", "missing_files", "git_error")
    return {key: state[key] for key in keys if key in state}


def error_text(payload: dict) -> str:
    """A structured tool error as one line: its message, the file and the place in it (a
    checklist override or a pack file that does not validate) and its hint."""
    text = str(payload.get("message") or payload.get("error") or "error")
    place = " ".join(str(payload[key]) for key in ("file", "where") if payload.get(key))
    if place:
        text += f" ({place})"
    if isinstance(payload.get("hint"), str):
        text += f" {payload['hint']}"
    return text


def _validate_passthrough(server, args) -> dict[str, str]:
    values: dict[str, str] = {}
    checks = (("rails", getattr(server, "parse_rail_map", None)),
              ("hv", getattr(server, "parse_hv", None)),
              ("vref", getattr(server, "_parse_vref", None)))
    for key, parse in checks:
        text = getattr(args, key)
        if text is None:
            continue
        if parse is not None:
            parsed, error = parse(text)
            if error or parsed is None:
                message = (error or {}).get("message") or f"--{key} could not be read."
                raise UsageError(f"invalid_{key}", f"--{key}: {message}")
        values[key] = text
    return values


# Settings the check command does not use: their problems are not its errors.
UNUSED_SETTINGS = ("check_updates",)
# Settings whose problem is a note, not a configuration error: the project folders serve
# only check_libraries' where-used (a pack's tool rule), and a plugin switch other than
# true or 1 leaves the plugins off, as it always did.
NOTED_SETTINGS = ("project_folders", "allow_plugins")


def _problem_text(problem: dict) -> str:
    return f"{problem['variable']} ({problem['title']}): {problem['message']}"


def read_settings(server) -> tuple:
    """(the server's settings from the environment (the tools read the same), notes on
    the settings not applied that the run can do without), or (None, []) for a server
    module without them; UsageError when one the run needs cannot be applied."""
    module = getattr(server, "app_settings", None)
    if module is None:
        return None, []
    settings = module.current()
    problems = [problem for problem in settings.problems_now()
                if problem["setting"] not in UNUSED_SETTINGS]
    errors = [problem for problem in problems if problem["setting"] not in NOTED_SETTINGS]
    if errors:
        raise UsageError("invalid_setting", " ".join(map(_problem_text, errors)))
    return settings, [f"Setting not applied: {_problem_text(problem)}"
                      for problem in problems]


def run(args, server) -> dict:
    """Load the project headless and run what the arguments ask for; the result model
    every output is rendered from. UsageError before anything ran."""
    rc = _module_of(server.build_review_checklist)
    cc = _module_of(server.load_check_packs)
    checklist_names = split_names(args.checklist) or list(DEFAULT_CHECKLISTS)
    if [name.lower() for name in checklist_names] == ["none"]:
        checklist_names = []
    elif any(name.lower() == "none" for name in checklist_names):
        raise UsageError("invalid_checklist", "--checklist none cannot be combined with "
                                              "checklist names.")
    tools = split_names(args.checks)
    allowed = check_tools(cc)
    text_only = [name for name in tools if name in TEXT_TOOLS]
    if text_only:
        raise UsageError("unknown_check",
                         f"--checks: {', '.join(text_only)} answers in text, not JSON, so "
                         f"nothing in it passes or fails. Tools: {', '.join(allowed)}.")
    unknown = [name for name in tools if name not in allowed]
    if unknown:
        raise UsageError("unknown_check",
                         f"--checks: no tool {', '.join(unknown)}. Tools: "
                         f"{', '.join(allowed)}.")
    packs_mode = (args.packs or "auto").strip().lower()
    if not (checklist_names or tools or args.drc_report or args.diff_base) \
            and packs_mode == "none":
        raise UsageError("nothing_to_run",
                         "Nothing to check: --checklist none and --packs none, and no "
                         "--checks, --drc-report or --diff-base.")
    defaults = _validate_passthrough(server, args)
    settings, setting_notes = read_settings(server)
    prj = find_project(args.project)
    started = time.perf_counter()
    result: dict = {
        "generator": f"altium-copilot-kc {server._read_version()}",
        "generated_utc": _now(),
        "fail_on": args.fail_on,
        "options": {"checklists": checklist_names, "checks": tools, "packs": args.packs,
                    "pack_file": args.pack_file, "allow_plugins": args.allow_plugins,
                    "variant": args.variant, "diff_base": args.diff_base,
                    "drc_report": args.drc_report, **defaults},
        "project": {"prj_pcb": prj.name, "name": prj.stem},
        "suites": [],
        "errors": [],
        "notes": list(setting_notes),
    }
    applied = {key: value for key, value in (settings.applied() if settings else {}).items()
               if key not in UNUSED_SETTINGS}
    if applied:
        result["options"]["settings"] = applied

    def config_error(suite_id: str, name: str, code: str, message: str, time_s: float = 0.0):
        result["suites"].append(error_suite(suite_id, name, code, message, time_s=time_s))
        result["errors"].append({"suite": suite_id, "error": code, "message": message})

    def guarded(suite_id: str, name: str, work) -> None:
        """work(); an exception it raises (a bug, a result of an unexpected shape) is an
        internal error of that suite, the others still run."""
        began = time.perf_counter()
        try:
            work()
        except UsageError:
            raise
        except Exception as error:
            _err(f"{PROG}: internal error in {name}:\n{traceback.format_exc()}")
            config_error(suite_id, name, "internal_error",
                         f"{type(error).__name__}: {error}", time.perf_counter() - began)

    try:
        load = server.load_project_headless(str(prj))
    except Exception as error:  # a .PrjPcb that cannot be parsed
        load = {"error": "load_failed", "message": f"{type(error).__name__}: {error}"}
    if "error" in load or not load.get("loaded"):
        config_error("load", "Project load", str(load.get("error", "load_failed")),
                     str(load.get("message") or "The project could not be loaded."),
                     time.perf_counter() - started)
        result["elapsed_s"] = round(time.perf_counter() - started, 2)
        return result
    project, netlist, variant_state = server._require_project(netlist=False)
    result["suites"].append(load_suite(load, netlist is not None, time.perf_counter() - started,
                                       board=(project.get("pcb_doc_paths") or [None])[0]))
    result["project"].update({
        "name": project.get("name"),
        "load_mode": load.get("load_mode"),
        "netlist": "compiled offline from the saved sheets" if netlist is not None
        else f"none (netlist-free load: {load.get('compile_error') or 'no sheet to compile'})",
        "sheets": load.get("sheet_count"),
        "components": load.get("component_count"),
        "compile_warnings": len((netlist or {}).get("compile_warnings") or []),
        "sheet_errors": len(load.get("sheet_errors") or []),
        "pcb_doc": Path(project["pcb_doc_paths"][0]).name if project.get("pcb_doc_paths")
        else None,
    })
    result["load_s"] = round(time.perf_counter() - started, 2)
    if args.variant:
        switched = json.loads(server._set_active_variant_impl(variant_state, args.variant))
        if "error" in switched:
            names = [v.name for v in variant_state.variants]
            raise UsageError("variant_not_found",
                             f"--variant: no variant '{args.variant}' in the project "
                             f"(variants: {', '.join(names) or 'none'}).")
    elif settings is not None and settings.default_variant:
        wanted = settings.default_variant
        if variant_state.get(wanted) is not None:
            variant_state.set_variant(wanted)
        elif variant_state.variants:
            result["notes"].append(
                f"The Default variant setting (ALTIUM_COPILOT_DEFAULT_VARIANT) names "
                f"'{wanted}', which the project does not define: the project's first "
                "variant is checked.")
    variant = variant_state.active.name if variant_state.variants else None
    result["project"]["variant"] = variant
    state = rc.design_state(project, server.head_state)
    result["design_state"] = _state_view(state)

    def run_tool(tool: str) -> None:
        began = time.perf_counter()
        outcome = rc.ToolRunner({tool: getattr(server, tool)}).run(tool, {})
        result["suites"].append(tool_suite(rc, tool, outcome, time.perf_counter() - began))

    with tool_defaults(server, rc.EVIDENCE_TOOLS, defaults) as wrapped:
        if wrapped:
            result["options"]["defaults_given_to"] = wrapped
        guarded("checklists", "Review checklists",
                lambda: _run_checklists(server, rc, args, project, netlist, variant, state,
                                        checklist_names, result, config_error))
        guarded("packs", "Check packs", lambda: _run_packs(server, args, result, config_error))
        for tool in tools:
            guarded(f"tool:{tool}", tool, functools.partial(run_tool, tool))
        if args.drc_report and "get_drc_report" not in tools:
            guarded("drc-report", "Batch DRC report (get_drc_report)",
                    functools.partial(run_tool, "get_drc_report"))
    if args.diff_base:
        guarded("design-diff", f"Design diff against {args.diff_base}",
                lambda: _run_diff(server, args.diff_base, project, result, config_error))
    if not result["errors"] and not any(item["kind"] not in ("load", "error")
                                        for item in result["suites"]):
        config_error("run", "Checks run", "nothing_ran",
                     "No check ran: no checklist, no check pack (none was found), no "
                     "--checks tool, no --drc-report and no --diff-base. A run that checks "
                     "nothing is not a pass.")
    result["elapsed_s"] = round(time.perf_counter() - started, 2)
    return result


def check_tools(cc) -> list[str]:
    """The tools --checks may name: those a check pack's tool rule may run that answer in
    JSON."""
    return [tool for tool in cc.TOOL_RULE_TOOLS if tool not in TEXT_TOOLS]


def _run_diff(server, base: str, project: dict, result: dict, config_error) -> None:
    began = time.perf_counter()
    name = f"Design diff against {base}"
    try:
        model = server.diff_revisions(project["prj_pcb_path"], base, "working")
    except server.DiffError as error:
        payload = getattr(error, "payload", {}) or {}
        if payload.get("error") == "project_not_in_revision":
            summary = not_in_base_summary(payload, base)
            result["diff"] = summary
            result["suites"].append(diff_suite(summary, time.perf_counter() - began))
            return
        config_error("design-diff", name, str(payload.get("error", "diff_failed")),
                     str(payload.get("message", error)), time.perf_counter() - began)
        return
    summary = diff_summary(model, base)
    result["diff"] = summary
    result["suites"].append(diff_suite(summary, time.perf_counter() - began))


def _run_checklists(server, rc, args, project, netlist, variant, state, names, result,
                    config_error) -> None:
    if not names:
        return
    began = time.perf_counter()
    try:
        checklists, info = server.load_checklists(
            project.get("root_dir"), overrides=True, bundled=server._checklists_bundled(),
            tools=server._review_tools())
    except server.ChecklistError as error:
        config_error("checklists", "Review checklists", str(error.error.get("error")),
                     error_text(error.error), time.perf_counter() - began)
        return
    by_name = {key.lower(): key for key in checklists}
    unknown = [name for name in names if name.lower() not in by_name]
    if unknown:
        config_error("checklists", "Review checklists", "unknown_checklist",
                     f"--checklist: no checklist {', '.join(unknown)}. Checklists: "
                     f"{', '.join(sorted(checklists))}.")
        names = [name for name in names if name.lower() in by_name]
    chosen = [checklists[by_name[name.lower()]] for name in names]
    severities: dict[str, str] = {}
    if any(item.get("kind") == "custom" for checklist in chosen
           for item in checklist.get("items") or []):
        try:
            packs, _ = server.load_check_packs(project.get("root_dir"), args.pack_file,
                                               server._custom_tools())
            severities = rule_severities(packs)
        except server.PackError:
            pass  # the packs' own suite reports it
    tools = server._review_tools()
    if args.pack_file:
        # The custom items read the same packs as the pack suites, --pack-file's too
        # (plugins are never run from a checklist).
        tools[rc.CUSTOM_TOOL] = functools.partial(tools[rc.CUSTOM_TOOL], path=args.pack_file)
    # One runner serves every checklist, so a tool two of them call runs once.
    runner = rc.ToolRunner(tools, needs=rc.runner_needs(
        [item for checklist in chosen for item in rc.select(checklist, None, None)]))
    for checklist in chosen:
        began = time.perf_counter()
        model = server.build_review_checklist(project, netlist, checklist, tools=tools,
                                              variant=variant, state=state, runner=runner)
        model = {**model, "overrides_applied": info.get("applied") or []}
        result["suites"].append(checklist_suite(model, severities, time.perf_counter() - began))


def _run_packs(server, args, result, config_error) -> None:
    mode = (args.packs or "auto").strip()
    if mode.lower() == "none":
        return
    pack = None if mode.lower() in ("auto", "all", "") else mode
    began = time.perf_counter()
    error, header, model = server._custom_run(pack, None, args.pack_file, args.allow_plugins)
    if error is not None:
        if error.get("error") == "no_check_packs" and pack is None:
            result["notes"].append("No check pack was found next to the .PrjPcb or in the Git "
                                   "repository root: no pack ran.")
            return
        config_error("packs", "Check packs", str(error["error"]), error_text(error),
                     time.perf_counter() - began)
        return
    result["suites"].extend(pack_suites(header, model))
    if header.get("plugins") and not header["plugins"].get("run"):
        result["notes"].append(f"Check-pack plugins were not run: "
                               f"{header['plugins'].get('reason') or 'not selected'}.")


# ---------- verdict ----------

def threshold(fail_on: str) -> int:
    """The severity rank a failing case must reach to count as a failure: --fail-on
    never reports errors and warnings as failures (and still exits 0)."""
    return RANK["warning"] if fail_on in ("warning", "never") else RANK["error"]


def outcome(case_: dict, fail_on: str) -> str:
    """The case's JUnit outcome: passed, failure, skipped or error. A failing case below
    the threshold passes (its text says so)."""
    if case_["status"] == "error":
        return "error"
    if case_["status"] == "skip":
        return "skipped"
    if case_["status"] == "fail" and RANK.get(case_["severity"] or "info", 0) >= threshold(fail_on):
        return "failure"
    return "passed"


def tally(result: dict) -> dict:
    """Cases by outcome over all suites, per suite too."""
    fail_on = result["fail_on"]
    total: Counter = Counter()
    for item in result["suites"]:
        counts = Counter(outcome(c, fail_on) for c in item["cases"])
        counts["below_threshold"] = sum(1 for c in item["cases"] if c["status"] == "fail"
                                        and outcome(c, fail_on) == "passed")
        counts["passed"] -= counts["below_threshold"]  # passed: nothing failed at all
        counts["total"] = len(item["cases"])
        item["counts"] = {key: counts.get(key, 0) for key in
                          ("passed", "failure", "skipped", "error", "below_threshold", "total")}
        total.update(item["counts"])
    return {key: total.get(key, 0) for key in
            ("passed", "failure", "skipped", "error", "below_threshold", "total")}


def exit_code(result: dict) -> int:
    if result["errors"] or any(c["status"] == "error" for s in result["suites"]
                               for c in s["cases"]):
        return EXIT_USAGE
    if result["fail_on"] == "never":
        return EXIT_OK
    return EXIT_FAIL if any(outcome(c, result["fail_on"]) == "failure"
                            for s in result["suites"] for c in s["cases"]) else EXIT_OK


def finish(result: dict) -> dict:
    """Counts, verdict and exit code on the result."""
    result["counts"] = tally(result)
    result["exit_code"] = exit_code(result)
    result["verdict"] = {EXIT_OK: "pass", EXIT_FAIL: "fail", EXIT_USAGE: "error"}[
        result["exit_code"]]
    return result


# ---------- JUnit XML ----------

def xml_text(value) -> str:
    """Text XML 1.0 can carry: characters it cannot (control characters, lone
    surrogates) are dropped; ElementTree escapes the rest."""
    return _NOT_XML.sub("", str(value if value is not None else ""))


def _properties(parent, values: dict) -> None:
    if not values:
        return
    element = ET.SubElement(parent, "properties")
    for key, value in values.items():
        ET.SubElement(element, "property", name=xml_text(key),
                      value=xml_text(clip(value, 2000)))


def render_junit(result: dict) -> bytes:
    """JUnit XML (testsuites > testsuite > testcase) valid against the JUnit schema of
    the Jenkins xunit plugin (junit-10.xsd), which GitHub and GitLab test reporters read
    too: suite properties, then per case failure (type error or warning), error or
    skipped, and system-out. A case's own properties (status, kind, tool ...) are in the
    JSON output: the schema has no properties in a testcase."""
    fail_on = result["fail_on"]
    counts = result.get("counts") or tally(result)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    root = ET.Element("testsuites", name=xml_text(f"altium-copilot-kc check "
                                                  f"{result['project'].get('prj_pcb', '')}"),
                      tests=str(counts["total"]), failures=str(counts["failure"]),
                      errors=str(counts["error"]),
                      time=f"{sum(s['time'] for s in result['suites']):.3f}")
    for item in result["suites"]:
        tally_ = item.get("counts") or {}
        element = ET.SubElement(root, "testsuite", name=xml_text(item["name"]),
                                tests=str(len(item["cases"])),
                                failures=str(tally_.get("failure", 0)),
                                errors=str(tally_.get("error", 0)),
                                skipped=str(tally_.get("skipped", 0)),
                                time=f"{item['time']:.3f}", timestamp=stamp)
        properties = {"suite": item["id"], "kind": item["kind"], **item["properties"]}
        if item["kind"] == "checklist" or result.get("design_state"):
            properties.setdefault("design_state", (result.get("design_state") or {}).get("id"))
        _properties(element, {k: v for k, v in properties.items() if v not in (None, "")})
        for case_ in item["cases"]:
            testcase = ET.SubElement(element, "testcase", name=xml_text(case_["name"]),
                                     classname=xml_text(case_["classname"]),
                                     time=f"{case_['time']:.3f}")
            kind = outcome(case_, fail_on)
            text = case_["detail"] or case_["message"]
            if kind == "failure":
                child = ET.SubElement(testcase, "failure", message=xml_text(case_["message"]),
                                      type=xml_text(case_["severity"] or "error"))
                child.text = xml_text(text)
            elif kind == "error":
                child = ET.SubElement(testcase, "error", message=xml_text(case_["message"]),
                                      type=xml_text(case_["properties"].get("error", "error")))
                child.text = xml_text(text)
            elif kind == "skipped":
                ET.SubElement(testcase, "skipped", message=xml_text(case_["message"]))
            out = text
            if case_["status"] == "fail" and kind == "passed":
                out = (f"{case_['severity']} (below --fail-on {fail_on}): "
                       f"{case_['message']}\n{case_['detail']}").strip()
            if out and kind != "failure" and kind != "error":
                ET.SubElement(testcase, "system-out").text = xml_text(out)
    ET.indent(root)
    return (b'<?xml version="1.0" encoding="UTF-8"?>\n'
            + ET.tostring(root, encoding="unicode").encode("utf-8") + b"\n")


# ---------- Markdown ----------

def md(text) -> str:
    """Design or pack text for a Markdown table cell or list line, shown as written: one
    line without control characters or lone surrogates, and the characters Markdown
    reads as markup (backslash, backtick, asterisk, brackets, pipe) escaped, angle
    brackets as entities (no link, code span, emphasis or HTML comes from the text)."""
    text = " ".join(_NOT_XML.sub("", str(text if text is not None else "")).split())
    return _MD_SPECIAL.sub(r"\\\1", text).replace("<", "&lt;").replace(">", "&gt;")


def code(text) -> str:
    """Text as a Markdown code span in a table cell: a backtick fence longer than any
    run of backticks in it, pipes escaped (a table cell needs that even in code)."""
    text = " ".join(_NOT_XML.sub("", str(text if text is not None else "")).split())
    if not text:
        return ""
    fence = "`" * (max((len(run) for run in re.findall("`+", text)), default=0) + 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return fence + pad + text.replace("|", "\\|") + pad + fence


def _state_line(state: dict | None) -> str:
    if not state:
        return "not read"
    if state.get("kind") == "git":
        line = f"**git {code(str(state.get('commit', ''))[:12])}**"
        if state.get("branch"):
            line += f" on {code(state['branch'])}"
        if state.get("commit_date_utc"):
            line += f" ({state['commit_date_utc']})"
        line += (f", {state.get('design_files_changed', 0)} design file(s) changed since the "
                 "commit" if state.get("changes_since_commit") else ", design files as committed")
        return line
    return (f"**files {code(str(state.get('fingerprint', ''))[:12])}** (SHA1 of the design "
            "files; not in a Git work tree)")


def render_markdown(result: dict, step: tuple = MARKDOWN_STEPS[0]) -> str:
    evidence_chars, failing_listed, skipped_listed, changes_listed = step
    fail_on = result["fail_on"]
    counts = result.get("counts") or tally(result)
    project = result["project"]
    verdict = {"pass": "PASS", "fail": "FAIL", "error": "ERROR"}.get(result.get("verdict"), "?")
    lines = [f"# Altium design checks: {md(project.get('prj_pcb'))}", ""]
    lines.append(f"**{verdict}** (exit {result.get('exit_code')}, `--fail-on {fail_on}`): "
                 f"{counts['failure']} failing, {counts['below_threshold']} below the threshold, "
                 f"{counts['skipped']} not evaluated, {counts['passed']} passed"
                 + (f", {counts['error']} error(s)" if counts["error"] else "") + ".")
    lines += ["", "| | |", "|---|---|",
              f"| Project | {code(project.get('prj_pcb'))}"
              + (f", board {code(project['pcb_doc'])}" if project.get("pcb_doc") else "") + " |"]
    if project.get("netlist"):
        lines.append(f"| Netlist | {md(project['netlist'])}"
                     + (f"; {project['compile_warnings']} compile warning(s)"
                        if project.get("compile_warnings") else "") + " |")
    lines.append(f"| Variant | {md(project.get('variant') or '[No Variations]')} |")
    lines.append(f"| Design state | {_state_line(result.get('design_state'))} |")
    lines.append(f"| Generated | {result['generated_utc']} by {md(result['generator'])} "
                 f"in {result.get('elapsed_s', 0)} s |")
    lines.append("")
    if result["errors"]:
        lines += ["## Errors", ""]
        lines += [f"- **{md(e['suite'])}**: {code(e['error'])}: {md(clip(e['message'], 600))}"
                  for e in result["errors"]]
        lines.append("")
    if result["suites"]:
        lines += ["## Coverage", "",
                  "| Suite | Passed | Failing | Below threshold | Not evaluated | Errors | Total | Time |",
                  "|---|---|---|---|---|---|---|---|"]
        for item in result["suites"]:
            c = item.get("counts") or {}
            lines.append(f"| {md(item['name'])} | {c.get('passed', 0)} | {c.get('failure', 0)} | "
                         f"{c.get('below_threshold', 0)} | {c.get('skipped', 0)} | "
                         f"{c.get('error', 0)} | {c.get('total', 0)} | {item['time']:.1f} s |")
        lines.append("")
    for item in result["suites"]:
        if item["kind"] != "checklist" or not item.get("coverage"):
            continue
        lines += [f"### {md(item['name'])}", "",
                  "| Section | Yes | No | N/A | Needs judgment | Total |", "|---|---|---|---|---|---|"]
        for row in item["coverage"]:
            title = f"**{md(row['title'])}**" if row["section"] == "all" else md(row["title"])
            lines.append(f"| {title} | {row['yes']} | {row['no']} | {row['n/a']} | "
                         f"{row['needs_judgment']} | {row['total']} |")
        lines.append("")
    for heading, wanted in ((f"Failing (at or above `{fail_on if fail_on != 'never' else 'warning'}`)",
                             "failure"),
                            ("Below the threshold", "below")):
        blocks = []
        for item in result["suites"]:
            rows = [c for c in item["cases"] if (outcome(c, fail_on) == "failure"
                                                  if wanted == "failure" else
                                                  c["status"] == "fail"
                                                  and outcome(c, fail_on) == "passed")]
            if not rows:
                continue
            block = [f"### {md(item['name'])}", ""]
            for c in rows[:failing_listed]:
                anchors = c["properties"].get("anchors")
                block.append(f"- **{md(c['name'])}** ({c['severity']}): "
                             f"{md(clip(c['message'], evidence_chars))}"
                             + (f" Anchors: {md(clip(anchors, 200))}." if anchors else ""))
            if len(rows) > failing_listed:
                block.append(f"- ... {len(rows) - failing_listed} more (see the JUnit or JSON "
                             "output)")
            blocks += block + [""]
        if blocks:
            lines += [f"## {heading}", "", *blocks]
    diff = result.get("diff")
    if diff:
        a = diff.get("a") or {}
        lines += [f"## Design diff against {code(diff['base'])}", ""]
        if diff.get("not_in_base"):
            lines += [md(diff["not_in_base"]), ""]
        else:
            lines += [f"Compared commit {code(a.get('commit') or '?')}"
                      + (f" ({md(a.get('subject'))})" if a.get("subject") else "")
                      + f" with the files checked: {diff['total_changes']} change(s).", ""]
        if diff["summary"]:
            lines += ["| Section | Changes |", "|---|---|"]
            for section in [*DIFF_SECTIONS, *sorted(set(diff["summary"]) - set(DIFF_SECTIONS))]:
                change = diff["summary"].get(section)
                if change:
                    lines.append(f"| {section} | " + ", ".join(
                        f"{md(k)} {v}" for k, v in change.items()) + " |")
            lines.append("")
        if diff["ict_hazards"]:
            lines += ["**ICT hazards** (test fixtures and programs may no longer match):", ""]
            lines += [f"- {md(clip(e.get('text'), evidence_chars))}"
                      for e in diff["ict_hazards"][:changes_listed]] + [""]
        others = [e for e in diff["changes"] if e.get("section") != "ict_hazards"]
        if others and changes_listed:
            lines += [f"Changes (first {min(changes_listed, len(others))}):", ""]
            lines += [f"- [{md(e.get('section'))}] {md(clip(e.get('text'), evidence_chars))}"
                      for e in others[:changes_listed]] + [""]
        if diff.get("unchanged_or_not_compared"):
            lines += ["Not compared: " + "; ".join(
                f"{md(k)}: {md(v)}" for k, v in diff["unchanged_or_not_compared"].items()), ""]
    skipped = [(item, c) for item in result["suites"] for c in item["cases"]
               if outcome(c, fail_on) == "skipped"]
    if skipped and skipped_listed:
        lines += [f"<details><summary>Not evaluated ({len(skipped)}): needs judgment, not "
                  "applicable or could not run</summary>", ""]
        for item, c in skipped[:skipped_listed]:
            lines.append(f"- {md(item['id'])} **{md(c['name'])}**: "
                         f"{md(clip(c['message'], evidence_chars))}")
        if len(skipped) > skipped_listed:
            lines.append(f"- ... {len(skipped) - skipped_listed} more")
        lines += ["", "</details>", ""]
    for note in result.get("notes") or []:
        lines.append(f"> {md(note)}")
    if result.get("notes"):
        lines.append("")
    return "\n".join(lines)


def markdown_fitting(result: dict, limit: int = STEP_SUMMARY_BYTES) -> str:
    """The Markdown summary, shortened (fewer items listed, shorter evidence) until it
    fits limit bytes."""
    for step in MARKDOWN_STEPS:
        text = render_markdown(result, step)
        if len(text.encode("utf-8", "replace")) <= limit:
            return text
    return text.encode("utf-8", "replace")[:limit].decode("utf-8", "ignore")


# ---------- console ----------

def console_summary(result: dict, failures_listed: int = 10) -> str:
    fail_on = result["fail_on"]
    counts = result.get("counts") or tally(result)
    project = result["project"]
    state = (result.get("design_state") or {}).get("id")
    head = f"{PROG}: {project.get('prj_pcb')}"
    details = [part for part in (
        f"netlist {project['netlist']}" if project.get("netlist") else None,
        f"variant {project['variant']}" if project.get("variant") else None,
        state) if part]
    lines = [head + (f" ({'; '.join(details)})" if details else "")]
    width = max([len(item["name"]) for item in result["suites"]] or [10])
    width = min(width, 60)
    for item in result["suites"]:
        c = item.get("counts") or {}
        lines.append(f"  {clip(item['name'], 60):<{width}}  {c.get('total', 0):>4} cases  "
                     f"{c.get('passed', 0):>4} passed  {c.get('failure', 0):>3} failing  "
                     f"{c.get('below_threshold', 0):>3} below  "
                     f"{c.get('skipped', 0):>4} not evaluated"
                     + (f"  {c.get('error', 0)} error(s)" if c.get("error") else "")
                     + f"  {item['time']:.1f} s")
    failing = [(item, c) for item in result["suites"] for c in item["cases"]
               if outcome(c, fail_on) in ("failure", "error")]
    if failing:
        lines.append("Failing:" if fail_on != "never" else "Failing (not failing the run: "
                                                            "--fail-on never):")
        for item, c in failing[:failures_listed]:
            lines.append(f"  [{c['severity'] or 'error'}] {item['id']} {clip(c['name'], 90)}: "
                         f"{clip(c['message'], 140)}")
        if len(failing) > failures_listed:
            lines.append(f"  ... and {len(failing) - failures_listed} more")
    if (result.get("diff") or {}).get("not_in_base"):
        lines.append(f"Design diff: {result['diff']['not_in_base']}")
    for note in result.get("notes") or []:
        lines.append(f"Note: {note}")
    verdict = {"pass": "PASS", "fail": "FAIL", "error": "ERROR"}[result["verdict"]]
    lines.append(f"Result: {verdict}, exit {result['exit_code']} ({counts['failure']} failing at "
                 f"or above --fail-on {fail_on}, {counts['below_threshold']} below it, "
                 f"{counts['skipped']} not evaluated, {counts['passed']} passed)")
    return "\n".join(lines)


# ---------- entry ----------

def _write(path: str, data: bytes | str, append: bool = False) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = data.encode("utf-8", "replace") if isinstance(data, str) else data
    with open(target, "ab" if append else "wb") as handle:
        handle.write(payload)


def write_outputs(result: dict, args) -> list[str]:
    """Write the requested files; the problems writing them (one that cannot be rendered
    or written does not stop the others)."""
    problems = []
    outputs = []
    markdown: list[str] = []

    def summary() -> str:
        if not markdown:
            markdown.append(markdown_fitting(result))
        return markdown[0]

    if args.junit:
        outputs.append((args.junit, lambda: render_junit(result), False))
    if args.json:
        outputs.append((args.json, lambda: json.dumps(result, indent=2, default=str), False))
    if args.markdown:
        outputs.append((args.markdown, summary, False))
    if os.environ.get(STEP_SUMMARY):
        outputs.append((os.environ[STEP_SUMMARY], lambda: summary() + "\n", True))
    for path, render, append in outputs:
        try:
            _write(path, render(), append)
        except Exception as error:
            problems.append(f"{path}: {type(error).__name__}: {error}")
    return problems


def _plain(text: str) -> str:
    """Console text without control characters (no terminal escape sequence from design
    or pack text reaches a CI log) or lone surrogates."""
    return _NOT_XML.sub("", str(text))


def _err(text: str) -> None:
    try:
        sys.stderr.write(_plain(text) + "\n")
        sys.stderr.flush()
    except Exception:
        pass


def _out(text: str) -> None:
    try:
        sys.stdout.write(_plain(text) + "\n")
        sys.stdout.flush()
    except Exception:
        pass


def _server_module():
    """main.py's module when the command is not given one (tests, imports)."""
    for name in ("main", "server.main"):
        module = sys.modules.get(name)
        if module is not None and hasattr(module, "load_project_headless"):
            return module
    try:
        import main as module
    except ImportError:
        from server import main as module
    return module


def _output_args(argv: list[str]):
    """The output options of a command line that does not parse, read leniently, so the
    reports can still name the problem; None when even they cannot be read."""
    lenient = _Parser(add_help=False)
    for option in ("--junit", "--markdown", "--json", "--fail-on"):
        lenient.add_argument(option)
    lenient.add_argument("--quiet", action="store_true")
    try:
        args, rest = lenient.parse_known_args(argv)
    except (ArgumentsError, SystemExit):
        return None
    args.project = next((item for item in rest if not item.startswith("-")), "")
    if args.fail_on not in FAIL_ON:
        args.fail_on = "error"
    return args


def _error_result(server, args, suite_id: str, name: str, code: str, message: str) -> dict:
    """The result of a run stopped before any check: one error suite naming why."""
    try:
        version = server._read_version()
    except Exception:
        version = "unknown"
    return {"generator": f"altium-copilot-kc {version}", "generated_utc": _now(),
            "fail_on": args.fail_on, "options": {},
            "project": {"prj_pcb": PureWindowsPath(str(args.project or "")).name},
            "suites": [error_suite(suite_id, name, code, message)],
            "errors": [{"suite": suite_id, "error": code, "message": message}],
            "notes": []}


def _conclude(result: dict, args) -> int:
    """Verdict, reports and console output of a result; the exit code."""
    finish(result)
    problems = write_outputs(result, args)
    for problem in problems:
        _err(f"{PROG}: error: could not write {problem}")
    usage = any(error["suite"] == "usage" for error in result["errors"])
    if not args.quiet and not usage:
        try:
            _out(console_summary(result))
        except Exception:
            _err(f"{PROG}: internal error in the console summary:\n{traceback.format_exc()}")
    for error in result["errors"]:
        if error["suite"] != "usage":
            _err(f"{PROG}: error: {error['suite']}: {error['error']}: "
                 f"{clip(error['message'], 400)}")
    return EXIT_USAGE if problems else result["exit_code"]


def main(argv: list[str] | None = None, server=None) -> int:
    """Run the check command; the exit code. Every way a run can end (a command line that
    does not parse, a usage or configuration error, an unexpected exception) writes the
    reports the output options name, except --help."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    argv = [str(item) for item in (sys.argv[1:] if argv is None else argv)]
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as stop:  # --help (0)
        return int(stop.code or 0) if isinstance(stop.code, int) else EXIT_USAGE
    except ArgumentsError as error:
        try:
            parser.print_usage(sys.stderr)
        except Exception:
            pass
        _err(f"{PROG}: error: {error}")
        args = _output_args(argv)
        if args is None:
            return EXIT_USAGE
        result = _error_result(server or _server_module(), args, "usage", "Usage",
                               "invalid_arguments", str(error))
        _conclude(result, args)
        return EXIT_USAGE
    server = server or _server_module()
    logging.getLogger().setLevel(logging.ERROR if args.quiet else logging.WARNING)
    try:
        result = run(args, server)
    except UsageError as error:
        _err(f"{PROG}: error: {error.message}")
        result = _error_result(server, args, "usage", "Usage", error.code, error.message)
    except Exception as error:  # a bug: still a report, and exit 2 rather than 1
        _err(f"{PROG}: internal error:\n{traceback.format_exc()}")
        result = _error_result(server, args, "run", "Checks run", "internal_error",
                               f"{type(error).__name__}: {error}")
    return _conclude(result, args)
