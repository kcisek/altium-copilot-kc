"""Review checklists: run a design-review checklist against the loaded project, offline.

A checklist is a JSON file (bundled in server/checklists, or a team's own) whose items
each say what to check, how (kind) and, for the items a tool can answer, which tool call
gives the evidence and which of its findings or summary fields mean Yes, No or N/A:

- auto: a deterministic tool decides (run_erc, check_bom, check_sch_pcb_sync, a release
  gate ...): Yes, No or N/A, or needs judgment when the tool could not run or its
  evidence is mixed.
- assisted: a tool gives evidence (a heuristic such as get_power_tree or
  check_interfaces) and Claude, the datasheets or the reviewer decide: needs judgment,
  with the way the evidence leans (lean yes or no), or N/A.
- judgment: a person decides; the item is listed with a hint.
- custom: a rule of the team's check packs (run_custom_checks) decides: its pass is Yes,
  its fail No, not applicable N/A; a rule that could not be evaluated (or checked some
  objects only) needs judgment. Plugins are not run from a checklist.

Each tool runs in-process through the same function the MCP tool is (main.py passes them
in), with the same arguments, defaults and active variant as a direct call, once per
call of run_review_checklist (calls are cached by tool and arguments), reading further
pages (next_offset) while they bring rows the items look at. Its findings are mapped to
the item's status with evidence: a short summary, the finding ids (check / code / kind)
and anchors (designators, nets, sheets). A field an item reads that is not in the
result (a renamed field, a misspelt override) leaves the item needs judgment, never Yes.

The result also carries the design state it belongs to (the Git commit and whether any
design file differs from it, else SHA1s of the project files), a coverage table (items
by status per section), and a Markdown review record with a machine-readable block that a
later run reads back (previous=) to say what newly fails, what was fixed and what is
still open. Nothing is written: the record is returned for the user to keep.

Teams override the bundled checklists with review_checklists.json, or *.json files in a
review_checklists folder, next to the .PrjPcb or in the root of the Git repository above
it: replace a whole checklist, add sections and items, disable items by id. Every file
is validated; a problem is reported with the file, the place in it and what to fix.

Pure logic, no MCP. Item texts are this project's own words; the topics follow Altium's
schematic review checklist (resources.altium.com/p/schematic-review-checklist) and its
PCB design review checklists (resources.altium.com/p/pcb-design-and-review-checklist,
resources.altium.com/p/pcb-design-review-checklist-gets-you-manufacturing-quickly).
"""

import hashlib
import inspect
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Any, Callable

KINDS = ("auto", "assisted", "judgment", "custom")
SCOPES = ("schematic", "pcb", "release", "bom")
STATUSES = ("yes", "no", "n/a", "needs_judgment")
STATUS_LABELS = {"yes": "Yes", "no": "No", "n/a": "N/A", "needs_judgment": "Needs judgment"}
GROUPS = ("na", "no", "judge", "yes")  # evaluated in this order

# The read-only, deterministic tools a checklist item may call. Tools that change the
# session (set_project_dir, set_active_variant, refresh_netlist), write files
# (package_for_xfn), return prompts or pictures are not here.
EVIDENCE_TOOLS = (
    "run_erc", "check_project_integrity", "get_project_options", "get_design_hierarchy",
    "check_pull_resistors", "check_interfaces", "get_power_tree", "check_derating",
    "check_logic_levels", "get_circuit_calculations", "check_decoupling",
    "get_firmware_pin_map", "compare_variants", "list_variants", "get_bom", "check_bom",
    "check_sch_pcb_sync", "check_connectivity", "check_pcb_health",
    "check_rule_compliance", "check_lengths", "check_diff_pairs", "list_diff_pairs",
    "get_classes", "get_design_rules", "check_vias", "check_testpoints", "check_dfm",
    "check_thermal", "check_hv_clearance", "check_power_paths", "check_fab_drawing",
    "get_output_jobs", "verify_outputs", "check_release_readiness", "check_libraries",
    "get_drc_report", "get_board_info", "get_design_notes", "get_clone_status",
)
RESERVED_ARGS = ("offset", "format")
CUSTOM_TOOL = "run_custom_checks"  # answers the custom items
DEFAULT_NA_ERRORS = ("no_pcb_document",)
OPS = ("==", "!=", ">", ">=", "<", "<=", "in", "not_in", "exists", "missing", "truthy",
       "falsy", "empty", "nonempty")
_UNARY_OPS = {"exists", "missing", "truthy", "falsy", "empty", "nonempty"}

CHECKLIST_DIR = "checklists"
OVERRIDE_FILE = "review_checklists.json"
OVERRIDE_FOLDER = "review_checklists"
MAX_OVERRIDE_BYTES = 256 * 1024
MAX_OVERRIDE_FILES = 20
MAX_ITEMS = 300
MAX_SECTIONS = 40
MAX_CONDITIONS = 20
MAX_DEPTH = 3
MAX_PAGES = 25  # pages of one tool's result read for one item
MAX_RECORD_BYTES = 2 * 1024 * 1024
REPO_LEVELS = 12  # folders walked up from the project to find the repository root

TEXT_LIMITS = {"title": 120, "text": 300, "hint": 400, "source": 300, "description": 600,
               "label": 120, "version": 20, "name": 80, "comment": 600}
_CHECKLIST_ID = re.compile(r"[a-z][a-z0-9_-]{0,39}")
_ITEM_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,39}")
_CHECKLIST_KEYS = ("id", "title", "scope", "version", "description", "source", "sections",
                   "items")
_SECTION_KEYS = ("id", "title")
_ITEM_KEYS = ("id", "section", "text", "kind", "scope", "evidence", "rule", "hint", "source")
_EVIDENCE_KEYS = ("tool", "args", "na", "no", "judge", "yes", "na_errors", "anchors")
_CONDITION_KEYS = ("list", "where", "path", "op", "value", "label", "all", "any")
_OVERRIDE_KEYS = ("name", "comment", "checklists", "add_sections", "add", "disable")

EVIDENCE_CHARS = 400
ITEM_TEXT_CHARS = 160  # a finding's own text quoted in an evidence line
ANCHORS_LISTED = 10
FINDING_IDS_LISTED = 8
FILES_LISTED = 40  # design-state files listed in a JSON page

# Keys whose values name the objects a finding is about.
DESIGNATOR_KEYS = ("designator", "designators", "refdes", "sch_refdes", "part", "connector",
                   "ic", "ics", "component", "components", "mcu", "source")
NET_KEYS = ("net", "nets", "rail", "pair", "bus", "schematic_net", "pcb_net", "joined_nets")
SHEET_KEYS = ("sheet", "sheets", "document", "drawing")
OTHER_KEYS = ("name", "names", "rule", "variant", "library", "outjob")
ANCHOR_GROUPS = (("designators", DESIGNATOR_KEYS), ("nets", NET_KEYS),
                 ("sheets", SHEET_KEYS), ("other", OTHER_KEYS))
# Keys that name a net or a part in some tools' rows only: elsewhere 'pair' is a pair of
# pin types (run_erc) or a Connection Matrix cell (get_project_options), 'bus' a bus kind
# (get_firmware_pin_map) and 'source' a file, a rule or a profile.
ANCHOR_KEY_TOOLS = {
    "pair": ("check_diff_pairs", "list_diff_pairs", "check_vias", "check_lengths"),
    "bus": ("check_pull_resistors", "check_interfaces"),
    "source": ("get_power_tree",),
}
FINDING_ID_KEYS = ("check", "violation", "advisory", "id", "code", "kind")
FINDING_DETAIL_KEYS = ("code", "kind", "issue")  # appended to a check: files/output_not_found
_CODE = re.compile(r"[a-z][a-z0-9_]*")
FINDING_TEXT_KEYS = ("issue", "message", "evidence")

RECORD_MARKER = "altium-copilot review-record"
RECORD_VERSION = 1
_RECORD_BLOCK = re.compile(
    r"<!--\s*" + re.escape(RECORD_MARKER) + r"\s+v(\d+)\s*\n(.*?)\n\s*-->", re.DOTALL)
_DECISION = re.compile(r"\s*(yes|no|n/?a|not applicable)\b\s*(?:[:\-–—]\s*)?(.*)\Z",
                       re.IGNORECASE | re.DOTALL)
DECISION_NOTE_CHARS = 300

_MISSING = object()


class ChecklistError(ValueError):
    """A checklist, override file or argument that cannot be used; .error is the
    structured tool error."""

    def __init__(self, error: dict):
        super().__init__(error["message"])
        self.error = error


def clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _md(text) -> str:
    """Text for a Markdown table cell."""
    return " ".join(str(text).split()).replace("|", "\\|")


# ---------- bundled checklists ----------

def bundled_dir() -> Path:
    """The bundled checklists: server/checklists, or 'checklists' in a PyInstaller
    bundle (release.yml and pack.ps1 add it with --add-data)."""
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / CHECKLIST_DIR
    return Path(__file__).resolve().parent.parent / CHECKLIST_DIR


def load_bundled(folder: Path | None = None) -> dict[str, dict]:
    """Every bundled checklist by id, validated (ChecklistError on a problem: the
    bundled files are tested, so a problem is a packaging fault)."""
    folder = folder or bundled_dir()
    checklists: dict[str, dict] = {}
    try:
        paths = sorted(folder.glob("*.json"))
    except OSError:
        paths = []
    if not paths:
        raise ChecklistError({"error": "checklists_missing",
                              "message": f"No bundled checklist was found in {folder}."})
    for path in paths:
        data = read_json(path, f"bundled {path.name}")
        checklist = validate_checklist(data, f"bundled {path.name}", "bundled")
        if checklist["id"] in checklists:
            raise ChecklistError(_invalid(f"bundled {path.name}", "id",
                                          f"checklist id '{checklist['id']}' is used twice."))
        checklists[checklist["id"]] = checklist
    _unique_item_ids(checklists, "bundled checklists")
    return checklists


# ---------- reading and validating ----------

def _invalid(origin: str, where: str, message: str, **extra) -> dict:
    return {"error": "invalid_checklist", "file": origin, "where": where,
            "message": f"{origin}: {where + ': ' if where else ''}{message}",
            "hint": ("Show the user what to fix in the file (the place is in 'where'), or "
                     "call again with overrides=false when they want the bundled "
                     "checklists only."), **extra}


def _no_duplicates(pairs: list) -> dict:
    seen: dict = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"duplicate key '{clip(key, 60)}'")
        seen[key] = value
    return seen


def _no_constants(text: str):
    raise ValueError(f"{text} is not allowed in a checklist")


def read_json(path: Path, origin: str, folder: Path | None = None) -> dict:
    """The JSON object in path: UTF-8 (a BOM is fine), no duplicate keys, at most
    MAX_OVERRIDE_BYTES; with folder, only a file that resolves inside it (no link out)."""
    try:
        resolved = path.resolve(strict=True)
        if folder is not None and resolved.parent != folder.resolve():
            raise ChecklistError(_invalid(origin, "", "the file is not inside its folder "
                                                      "(a link out of it?)."))
        if resolved.stat().st_size > MAX_OVERRIDE_BYTES:
            raise ChecklistError(_invalid(origin, "", "the file is larger than "
                                                      f"{MAX_OVERRIDE_BYTES // 1024} KB."))
        text = resolved.read_bytes().decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ChecklistError(_invalid(origin, "", "the file is not UTF-8 text.")) from None
    except OSError as error:
        raise ChecklistError(_invalid(
            origin, "", f"the file could not be read ({type(error).__name__}).")) from None
    try:
        data = json.loads(text, object_pairs_hook=_no_duplicates,
                          parse_constant=_no_constants)
    except json.JSONDecodeError as error:
        raise ChecklistError(_invalid(
            origin, "", f"not valid JSON at line {error.lineno}, column {error.colno}: "
                        f"{error.msg}.", line=error.lineno, column=error.colno)) from None
    except (ValueError, RecursionError) as error:
        raise ChecklistError(_invalid(origin, "", f"{error}.")) from None
    if not isinstance(data, dict):
        raise ChecklistError(_invalid(origin, "", "the file must hold one JSON object {...}."))
    return data


def _keys(data: dict, allowed: tuple, origin: str, where: str) -> None:
    for key in data:
        if not isinstance(key, str) or (key not in allowed and not key.startswith("_")):
            raise ChecklistError(_invalid(origin, where, f"unknown key '{clip(key, 60)}'.",
                                          valid_keys=list(allowed)))


def _text(data: dict, key: str, origin: str, where: str, required: bool = False):
    value = data.get(key)
    if value is None:
        if required:
            raise ChecklistError(_invalid(origin, where, f"'{key}' is required."))
        return None
    if isinstance(value, int) and not isinstance(value, bool) and key == "version":
        value = str(value)
    if not isinstance(value, str) or not value.strip():
        raise ChecklistError(_invalid(origin, f"{where}.{key}" if where else key,
                                      "must be a non-empty string."))
    limit = TEXT_LIMITS[key]
    if len(value) > limit:
        raise ChecklistError(_invalid(origin, f"{where}.{key}" if where else key,
                                      f"longer than {limit} characters."))
    return " ".join(value.split())


def _identifier(value, pattern: re.Pattern, origin: str, where: str, what: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        shown = clip(value, 60) if isinstance(value, str) else type(value).__name__
        rule = ("lower-case letters, digits, '_' and '-', starting with a letter, at most 40"
                if pattern is _CHECKLIST_ID else
                "letters, digits, '_', '.' and '-', at most 40")
        raise ChecklistError(_invalid(origin, where, f"{what} '{shown}' is not valid: "
                                                     f"{rule} characters."))
    return value


def validate_condition(cond, origin: str, where: str, depth: int = 0) -> dict:
    """A condition: {"list": path, "where": {...}} (the number of matching items, op
    default '>' value 0), {"path": path} (a value, op default 'truthy'), or {"all": [...]}
    / {"any": [...]} of conditions; label is the evidence text."""
    if not isinstance(cond, dict):
        raise ChecklistError(_invalid(origin, where, "a condition must be an object."))
    _keys(cond, _CONDITION_KEYS, origin, where)
    shapes = [key for key in ("list", "path", "all", "any") if key in cond]
    if len(shapes) != 1:
        raise ChecklistError(_invalid(origin, where, "a condition has exactly one of "
                                                     "'list', 'path', 'all' or 'any'."))
    shape = shapes[0]
    out: dict = {}
    label = _text(cond, "label", origin, where)
    if label:
        out["label"] = label
    if shape in ("all", "any"):
        if depth >= MAX_DEPTH:
            raise ChecklistError(_invalid(origin, where, "conditions are nested too deeply."))
        members = cond[shape]
        if not isinstance(members, list) or not members or len(members) > MAX_CONDITIONS:
            raise ChecklistError(_invalid(origin, f"{where}.{shape}", "must be a list of 1 to "
                                          f"{MAX_CONDITIONS} conditions."))
        for key in ("where", "op", "value"):
            if key in cond:
                raise ChecklistError(_invalid(origin, where, f"'{key}' does not go with "
                                                             f"'{shape}'."))
        out[shape] = [validate_condition(member, origin, f"{where}.{shape}[{i}]", depth + 1)
                      for i, member in enumerate(members)]
        return out
    path = cond[shape]
    if not isinstance(path, str) or not path.strip() or len(path) > 200:
        raise ChecklistError(_invalid(origin, f"{where}.{shape}",
                                      "must be a dotted path such as 'summary.errors'."))
    out[shape] = path.strip()
    if "where" in cond:
        if shape != "list":
            raise ChecklistError(_invalid(origin, where, "'where' goes with 'list'."))
        out["where"] = _validate_where(cond["where"], origin, f"{where}.where")
    op = cond.get("op", ">" if shape == "list" else "truthy")
    if op not in OPS:
        raise ChecklistError(_invalid(origin, f"{where}.op", f"'{clip(op, 20)}' is not an "
                                      f"operator; valid: {', '.join(OPS)}."))
    out["op"] = op
    if op in _UNARY_OPS:
        if "value" in cond:
            raise ChecklistError(_invalid(origin, where, f"'{op}' takes no 'value'."))
    else:
        value = cond.get("value", 0 if shape == "list" else _MISSING)
        if value is _MISSING:
            raise ChecklistError(_invalid(origin, where, f"'{op}' needs a 'value'."))
        _check_value(value, op, origin, f"{where}.value")
        out["value"] = value
    return out


def _check_value(value, op: str, origin: str, where: str) -> None:
    if op in ("in", "not_in"):
        if not isinstance(value, list) or not all(
                isinstance(v, (str, int, float, bool)) or v is None for v in value):
            raise ChecklistError(_invalid(origin, where, f"'{op}' needs a list of values."))
    elif op in (">", ">=", "<", "<="):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ChecklistError(_invalid(origin, where, f"'{op}' needs a number."))
    elif not (isinstance(value, (str, int, float, bool)) or value is None):
        raise ChecklistError(_invalid(origin, where, "must be a string, number, true, false "
                                                     "or null."))


def _validate_where(where_spec, origin: str, where: str) -> dict:
    if not isinstance(where_spec, dict) or not where_spec:
        raise ChecklistError(_invalid(origin, where, "must be an object of field: value."))
    out = {}
    for field, spec in where_spec.items():
        if not isinstance(field, str) or not field.strip():
            raise ChecklistError(_invalid(origin, where, "a field name must be a string."))
        if isinstance(spec, dict):
            _keys(spec, ("op", "value", "field"), origin, f"{where}.{field}")
            op = spec.get("op", "==")
            if op not in OPS:
                raise ChecklistError(_invalid(origin, f"{where}.{field}.op",
                                              f"'{clip(op, 20)}' is not an operator."))
            if op in _UNARY_OPS:
                if "value" in spec or "field" in spec:
                    raise ChecklistError(_invalid(origin, f"{where}.{field}",
                                                  f"'{op}' takes no 'value' or 'field'."))
                out[field] = {"op": op}
            elif "field" in spec:
                # Compared with another field of the same row: violations > marginal.
                other = spec["field"]
                if "value" in spec or op in ("in", "not_in"):
                    raise ChecklistError(_invalid(origin, f"{where}.{field}", (
                        "'field' compares with another field of the row: give it instead "
                        "of 'value', with ==, !=, >, >=, < or <=.")))
                if not isinstance(other, str) or not other.strip() or len(other) > 200:
                    raise ChecklistError(_invalid(origin, f"{where}.{field}.field",
                                                  "must be a dotted path in the row."))
                out[field] = {"op": op, "field": other.strip()}
            else:
                if "value" not in spec:
                    raise ChecklistError(_invalid(origin, f"{where}.{field}",
                                                  f"'{op}' needs a 'value' or a 'field'."))
                _check_value(spec["value"], op, origin, f"{where}.{field}.value")
                out[field] = {"op": op, "value": spec["value"]}
        elif isinstance(spec, list):
            _check_value(spec, "in", origin, f"{where}.{field}")
            out[field] = {"op": "in", "value": spec}
        else:
            _check_value(spec, "==", origin, f"{where}.{field}")
            out[field] = {"op": "==", "value": spec}
    return out


def validate_evidence(evidence, origin: str, where: str) -> dict:
    if not isinstance(evidence, dict):
        raise ChecklistError(_invalid(origin, where, "must be an object."))
    _keys(evidence, _EVIDENCE_KEYS, origin, where)
    tool = evidence.get("tool")
    if tool not in EVIDENCE_TOOLS:
        raise ChecklistError(_invalid(
            origin, f"{where}.tool", f"'{clip(tool, 60)}' is not a tool a checklist can "
                                     "call.", valid_tools=list(EVIDENCE_TOOLS)))
    args = evidence.get("args", {})
    if not isinstance(args, dict):
        raise ChecklistError(_invalid(origin, f"{where}.args", "must be an object of "
                                                               "argument: value."))
    for name in args:
        if name in RESERVED_ARGS:
            raise ChecklistError(_invalid(origin, f"{where}.args", f"'{name}' is set by the "
                                          "checklist runner (every page is read, as JSON)."))
    out: dict = {"tool": tool, "args": json.loads(json.dumps(args))}
    for group in GROUPS:
        if group not in evidence:
            continue
        conditions = evidence[group]
        if not isinstance(conditions, list) or len(conditions) > MAX_CONDITIONS:
            raise ChecklistError(_invalid(origin, f"{where}.{group}", "must be a list of at "
                                          f"most {MAX_CONDITIONS} conditions."))
        out[group] = [validate_condition(cond, origin, f"{where}.{group}[{i}]")
                      for i, cond in enumerate(conditions)]
    na_errors = evidence.get("na_errors", list(DEFAULT_NA_ERRORS))
    if not isinstance(na_errors, list) or not all(isinstance(e, str) for e in na_errors):
        raise ChecklistError(_invalid(origin, f"{where}.na_errors",
                                      "must be a list of tool error codes."))
    out["na_errors"] = na_errors
    anchors = evidence.get("anchors", [])
    if not isinstance(anchors, list) or len(anchors) > MAX_CONDITIONS:
        raise ChecklistError(_invalid(origin, f"{where}.anchors", "must be a list of "
                                                                  "{'list': ...} selectors."))
    out["anchors"] = []
    for i, selector in enumerate(anchors):
        cond = validate_condition(selector, origin, f"{where}.anchors[{i}]")
        if "list" not in cond:
            raise ChecklistError(_invalid(origin, f"{where}.anchors[{i}]",
                                          "an anchor selector is {'list': ..., 'where': ...}."))
        out["anchors"].append(cond)
    return out


def validate_item(item, origin: str, where: str, sections: set[str], scope: str,
                  origin_label: str) -> dict:
    if not isinstance(item, dict):
        raise ChecklistError(_invalid(origin, where, "an item must be an object."))
    _keys(item, _ITEM_KEYS + ("checklist",), origin, where)
    out = {"id": _identifier(item.get("id"), _ITEM_ID, origin, f"{where}.id", "item id")}
    where = f"{where} ({out['id']})"
    section = item.get("section")
    if section not in sections:
        raise ChecklistError(_invalid(origin, f"{where}.section",
                                      f"'{clip(section, 40)}' is not a section of the "
                                      "checklist.", valid_sections=sorted(sections)))
    out["section"] = section
    out["text"] = _text(item, "text", origin, where, required=True)
    kind = item.get("kind")
    if kind not in KINDS:
        raise ChecklistError(_invalid(origin, f"{where}.kind", f"'{clip(kind, 20)}' is not a "
                                      f"kind; valid: {', '.join(KINDS)}."))
    out["kind"] = kind
    item_scope = item.get("scope", scope)
    if item_scope not in SCOPES:
        raise ChecklistError(_invalid(origin, f"{where}.scope", f"'{clip(item_scope, 20)}' "
                                      f"is not a scope; valid: {', '.join(SCOPES)}."))
    out["scope"] = item_scope
    if kind == "custom":
        if "evidence" in item:
            raise ChecklistError(_invalid(origin, f"{where}.evidence", "a custom item names "
                                          "a check-pack rule ('rule'), not evidence."))
        out["rule"] = _identifier(item.get("rule"), _ITEM_ID, origin, f"{where}.rule",
                                  "rule id")
    elif "rule" in item:
        raise ChecklistError(_invalid(origin, f"{where}.rule", "'rule' (a check-pack rule id) "
                                      "goes with kind 'custom'."))
    elif kind == "judgment":
        if "evidence" in item:
            raise ChecklistError(_invalid(origin, f"{where}.evidence", "a judgment item has "
                                          "no evidence; make it 'assisted' to call a tool."))
    elif "evidence" not in item:
        raise ChecklistError(_invalid(origin, where, f"an {kind} item needs 'evidence' (the "
                                                     "tool and how its findings map)."))
    else:
        out["evidence"] = validate_evidence(item["evidence"], origin, f"{where}.evidence")
    for key in ("hint", "source"):
        value = _text(item, key, origin, where)
        if value:
            out[key] = value
    out["origin"] = origin_label
    return out


def validate_checklist(data, origin: str, origin_label: str, where: str = "") -> dict:
    """A whole checklist: id, title, scope, sections and items (each validated). where
    prefixes the places named in errors (an override file's 'checklists[0]')."""
    def at(place: str) -> str:
        return f"{where}.{place}" if where and place else (where or place)

    if not isinstance(data, dict):
        raise ChecklistError(_invalid(origin, where, "a checklist must be an object."))
    _keys(data, _CHECKLIST_KEYS, origin, where)
    checklist = {"id": _identifier(data.get("id"), _CHECKLIST_ID, origin, at("id"),
                                   "checklist id")}
    checklist["title"] = _text(data, "title", origin, where, required=True)
    scope = data.get("scope")
    if scope not in SCOPES:
        raise ChecklistError(_invalid(origin, at("scope"), f"'{clip(scope, 20)}' is not a "
                                      f"scope; valid: {', '.join(SCOPES)}."))
    checklist["scope"] = scope
    for key in ("version", "description", "source"):
        value = _text(data, key, origin, where)
        if value:
            checklist[key] = value
    sections = data.get("sections")
    if not isinstance(sections, list) or not sections or len(sections) > MAX_SECTIONS:
        raise ChecklistError(_invalid(origin, at("sections"), "must be a list of 1 to "
                                      f"{MAX_SECTIONS} {{'id', 'title'}} objects."))
    checklist["sections"] = []
    seen: set[str] = set()
    for i, section in enumerate(sections):
        checklist["sections"].append(
            _validate_section(section, origin, at(f"sections[{i}]"), seen))
    items = data.get("items")
    if not isinstance(items, list) or len(items) > MAX_ITEMS:
        raise ChecklistError(_invalid(origin, at("items"), f"must be a list of at most "
                                                           f"{MAX_ITEMS} items."))
    checklist["items"] = []
    ids: set[str] = set()
    for i, item in enumerate(items):
        place = at(f"items[{i}]")
        if isinstance(item, dict) and "checklist" in item:
            raise ChecklistError(_invalid(origin, place, "'checklist' belongs to an item "
                                                         "under 'add' in an override file."))
        row = validate_item(item, origin, place, seen, scope, origin_label)
        if row["id"].lower() in ids:
            raise ChecklistError(_invalid(origin, place, f"item id '{row['id']}' is used "
                                                         "twice (ids ignore case)."))
        ids.add(row["id"].lower())
        checklist["items"].append(row)
    checklist["origin"] = origin_label
    return checklist


def _validate_section(section, origin: str, where: str, seen: set[str]) -> dict:
    if not isinstance(section, dict):
        raise ChecklistError(_invalid(origin, where, "a section is {'id', 'title'}."))
    _keys(section, _SECTION_KEYS + ("checklist",), origin, where)
    section_id = _identifier(section.get("id"), _CHECKLIST_ID, origin, f"{where}.id",
                             "section id")
    if section_id in seen:
        raise ChecklistError(_invalid(origin, where, f"section '{section_id}' is used twice."))
    seen.add(section_id)
    return {"id": section_id, "title": _text(section, "title", origin, where, required=True)}


def _unique_item_ids(checklists: dict[str, dict], origin: str) -> None:
    """Item ids are unique across checklists, ignoring case (items= and decisions= name
    them in any case)."""
    owner: dict[str, str] = {}
    for checklist in checklists.values():
        for item in checklist["items"]:
            key = item["id"].lower()
            if key in owner:
                raise ChecklistError(_invalid(origin, "", (
                    f"item id '{item['id']}' is in both the '{owner[key]}' and the "
                    f"'{checklist['id']}' checklist: item ids are unique across checklists "
                    "(ignoring case).")))
            owner[key] = checklist["id"]


# ---------- team overrides ----------

def repository_root(project_dir: Path) -> Path | None:
    """The nearest folder at or above project_dir holding a .git entry (a Git work tree's
    root), or None."""
    folder = Path(project_dir)
    for _ in range(REPO_LEVELS):
        try:
            if (folder / ".git").exists():
                return folder
        except OSError:
            return None
        if folder.parent == folder:
            return None
        folder = folder.parent
    return None


def override_files(project_dir) -> list[tuple[Path, Path, str]]:
    """The override files to apply, in order: the repository root's (when it is above the
    project folder), then the project folder's; in each folder review_checklists.json,
    then review_checklists/*.json by name. Each entry: (file, its folder, a label)."""
    project = Path(project_dir)
    folders: list[tuple[Path, str]] = []
    root = repository_root(project)
    if root is not None and root.resolve() != project.resolve():
        folders.append((root, "repository root"))
    folders.append((project, "project folder"))
    files: list[tuple[Path, Path, str]] = []
    for folder, where in folders:
        try:
            single = folder / OVERRIDE_FILE
            if single.is_file():
                files.append((single, folder, f"{OVERRIDE_FILE} ({where})"))
            sub = folder / OVERRIDE_FOLDER
            if sub.is_dir():
                for path in sorted(sub.glob("*.json"), key=lambda p: p.name.lower()):
                    if path.is_file():
                        files.append((path, sub, f"{OVERRIDE_FOLDER}/{path.name} ({where})"))
        except OSError:
            continue
    return files


def check_arguments(item: dict, tools: dict[str, Callable[..., Any]] | None, origin: str,
                    where: str) -> None:
    """ChecklistError (invalid_checklist) when the item's evidence arguments do not fit
    its tool's parameters (tools: name -> the function the runner calls; a tool not in
    it is not checked). Values are checked when the tool runs."""
    evidence = item.get("evidence")
    function = (tools or {}).get(evidence["tool"]) if evidence else None
    if function is None:
        return
    try:
        signature = inspect.signature(function)
    except (TypeError, ValueError):
        return
    try:
        signature.bind(**evidence["args"])
    except TypeError as error:
        raise ChecklistError(_invalid(origin, f"{where}.evidence.args", (
            f"{evidence['tool']} does not take these arguments ({error})."),
            valid_arguments=[name for name in signature.parameters
                             if name not in RESERVED_ARGS])) from None


def apply_overrides(bundled: dict[str, dict], files: list[tuple[Path, Path, str]],
                    tools: dict[str, Callable[..., Any]] | None = None
                    ) -> tuple[dict[str, dict], list[dict]]:
    """The checklists after each override file, in order, and what each file changed.
    ChecklistError (invalid_checklist, with the file and the place) on any problem: an
    override is applied whole or not at all. With tools, item arguments are checked
    against the tools' parameters (check_arguments)."""
    if len(files) > MAX_OVERRIDE_FILES:
        raise ChecklistError(_invalid(files[MAX_OVERRIDE_FILES][2], "",
                                      f"more than {MAX_OVERRIDE_FILES} override files."))
    checklists = {key: {**value, "items": list(value["items"]),
                        "sections": list(value["sections"])}
                  for key, value in bundled.items()}
    applied: list[dict] = []
    for path, folder, label in files:
        data = read_json(path, label, folder)
        _keys(data, _OVERRIDE_KEYS, label, "")
        summary: dict = {"file": label}
        name = _text(data, "name", label, "")
        if name:
            summary["name"] = name
        for i, entry in enumerate(_list(data, "checklists", label)):
            checklist = validate_checklist(entry, label, label, f"checklists[{i}]")
            for j, item in enumerate(checklist["items"]):
                check_arguments(item, tools, label, f"checklists[{i}].items[{j}] ({item['id']})")
            summary.setdefault("replaced" if checklist["id"] in checklists else "added_checklists",
                               []).append(checklist["id"])
            checklists[checklist["id"]] = checklist
        for i, entry in enumerate(_list(data, "add_sections", label)):
            target = _target(entry, checklists, label, f"add_sections[{i}]")
            seen = {section["id"] for section in target["sections"]}
            section = _validate_section(entry, label, f"add_sections[{i}]", seen)
            target["sections"].append(section)
            summary.setdefault("added_sections", []).append(f"{target['id']}.{section['id']}")
        for i, entry in enumerate(_list(data, "add", label)):
            target = _target(entry, checklists, label, f"add[{i}]")
            item = validate_item(entry, label, f"add[{i}]",
                                 {section["id"] for section in target["sections"]},
                                 target["scope"], label)
            check_arguments(item, tools, label, f"add[{i}] ({item['id']})")
            if any(other["id"].lower() == item["id"].lower() for other in target["items"]):
                raise ChecklistError(_invalid(label, f"add[{i}]", (
                    f"item id '{item['id']}' is already in the '{target['id']}' checklist "
                    "(ids ignore case): disable it and add the new item under another id.")))
            target["items"].append(item)
            summary.setdefault("added", []).append(item["id"])
        disable = data.get("disable", [])
        if not isinstance(disable, list) or not all(isinstance(i, str) for i in disable):
            raise ChecklistError(_invalid(label, "disable", "must be a list of item ids."))
        for item_id in disable:
            hits = [(checklist, item) for checklist in checklists.values()
                    for item in checklist["items"] if item["id"].lower() == item_id.lower()]
            if not hits:
                raise ChecklistError(_invalid(label, "disable", f"no item has the id "
                                              f"'{clip(item_id, 40)}'."))
            for checklist, item in hits:
                checklist["items"].remove(item)
                checklist.setdefault("disabled", []).append(
                    {"id": item["id"], "file": label})
                summary.setdefault("disabled", []).append(item["id"])
        _unique_item_ids(checklists, label)
        applied.append(summary)
    return checklists, applied


def _list(data: dict, key: str, label: str) -> list:
    value = data.get(key, [])
    if not isinstance(value, list) or len(value) > MAX_ITEMS:
        raise ChecklistError(_invalid(label, key, f"must be a list of at most {MAX_ITEMS} "
                                                  "entries."))
    return value


def _target(entry, checklists: dict[str, dict], label: str, where: str) -> dict:
    if not isinstance(entry, dict):
        raise ChecklistError(_invalid(label, where, "must be an object."))
    target = entry.get("checklist")
    if target not in checklists:
        raise ChecklistError(_invalid(label, f"{where}.checklist",
                                      f"'{clip(target, 40)}' is not a checklist.",
                                      valid_checklists=sorted(checklists)))
    return checklists[target]


def load_checklists(project_dir, *, overrides: bool = True,
                    bundled: dict[str, dict] | None = None,
                    tools: dict[str, Callable[..., Any]] | None = None
                    ) -> tuple[dict[str, dict], dict]:
    """The checklists for a project: bundled, then the team's overrides (unless
    overrides is False; tools checks their arguments). Returns (checklists by id,
    {"files": [...], "applied": [...], "used"})."""
    base = bundled if bundled is not None else load_bundled()
    if not overrides or project_dir is None:
        return base, {"files": [], "applied": [], "used": False}
    files = override_files(project_dir)
    checklists, applied = apply_overrides(base, files, tools)
    return checklists, {"files": [label for _, _, label in files], "applied": applied,
                        "used": True}


def catalog(checklists: dict[str, dict], overrides: dict) -> dict:
    """What run_review_checklist can run: each checklist's sections with item counts."""
    rows = []
    for checklist in checklists.values():
        by_section = Counter(item["section"] for item in checklist["items"])
        rows.append({
            "id": checklist["id"], "title": checklist["title"], "scope": checklist["scope"],
            "version": checklist.get("version"), "origin": checklist["origin"],
            "items": len(checklist["items"]),
            "by_kind": dict(Counter(item["kind"] for item in checklist["items"])),
            "sections": [{"id": s["id"], "title": s["title"], "items": by_section[s["id"]]}
                         for s in checklist["sections"]],
            **({"disabled": checklist["disabled"]} if checklist.get("disabled") else {}),
        })
    return {"checklists": rows, "overrides": overrides}


def select(checklist: dict, sections: list[str] | None, items: list[str] | None) -> list[dict]:
    """The checklist's items in the named sections and/or with the named ids, in
    checklist order; ChecklistError (unknown_section / unknown_item) for a name that is
    not in it."""
    section_ids = [s["id"] for s in checklist["sections"]]
    chosen = list(checklist["items"])
    if sections:
        wanted = {s.strip().lower() for s in sections if s and s.strip()}
        unknown = sorted(wanted - set(section_ids))
        if unknown:
            raise ChecklistError({
                "error": "unknown_section",
                "message": f"No section {', '.join(repr(s) for s in unknown)} in the "
                           f"'{checklist['id']}' checklist.",
                "sections": section_ids})
        chosen = [item for item in chosen if item["section"] in wanted]
    if items:
        by_id = {item["id"].lower(): item["id"] for item in checklist["items"]}
        wanted_ids = [i.strip() for i in items if i and i.strip()]
        unknown = [i for i in wanted_ids if i.lower() not in by_id]
        if unknown:
            raise ChecklistError({
                "error": "unknown_item",
                "message": f"No item {', '.join(repr(i) for i in unknown)} in the "
                           f"'{checklist['id']}' checklist.",
                "items": [item["id"] for item in checklist["items"]][:200]})
        keep = {by_id[i.lower()] for i in wanted_ids}
        chosen = [item for item in chosen if item["id"] in keep]
    return chosen


def parse_decisions(decisions: dict | None, items: list[dict]) -> dict[str, dict]:
    """{item id: "yes|no|n/a[: note]"} -> {item id: {"status", "note"}}; ChecklistError
    (invalid_decision) for an unknown item or an unreadable value."""
    if not decisions:
        return {}
    if not isinstance(decisions, dict):
        raise ChecklistError({"error": "invalid_decision",
                              "message": "decisions is an object of item id: 'yes', 'no' or "
                                         "'n/a', optionally followed by ': note'."})
    by_id = {item["id"].lower(): item["id"] for item in items}
    out: dict[str, dict] = {}
    for key, value in decisions.items():
        item_id = by_id.get(str(key).strip().lower())
        if item_id is None:
            raise ChecklistError({"error": "invalid_decision",
                                  "message": f"decisions names '{clip(key, 40)}', which is not "
                                             "an item of this run.",
                                  "items": [item["id"] for item in items][:200]})
        match = _DECISION.match(value) if isinstance(value, str) else None
        if match is None:
            raise ChecklistError({"error": "invalid_decision",
                                  "message": f"The decision for {item_id} must start with "
                                             "'yes', 'no' or 'n/a' (then ': note')."})
        word = match[1].lower()
        status = "n/a" if word in ("n/a", "na", "not applicable") else word
        out[item_id] = {"status": status, "note": clip(match[2], DECISION_NOTE_CHARS)}
    return out


# ---------- running the tools ----------

class ToolRunner:
    """Runs evidence tools once per (tool, arguments) and merges their pages. tools maps a
    name to the MCP tool function (it returns JSON text; a dict is taken as is).

    Pages (next_offset) are read only while they bring new rows of a list some item looks
    at (needs: tool -> arguments key -> top-level list names, from list_keys): a list a
    tool repeats on every page, or one no item reads, stops the paging. Without needs
    every page is read. At most max_pages pages; a result cut there is marked truncated."""

    def __init__(self, tools: dict[str, Callable[..., Any]], max_pages: int = MAX_PAGES,
                 clock: Callable[[], float] = time.perf_counter,
                 needs: dict[tuple[str, str], set[str]] | None = None):
        self.tools = tools
        self.max_pages = max_pages
        self.clock = clock
        self.needs = needs
        self.cache: dict[tuple[str, str], dict] = {}
        self.runs: list[dict] = []

    @staticmethod
    def key(tool: str, args: dict) -> tuple[str, str]:
        return tool, json.dumps(args, sort_keys=True)

    def run(self, tool: str, args: dict) -> dict:
        key = self.key(tool, args)
        if key not in self.cache:
            start = self.clock()
            needed = None if self.needs is None else self.needs.get(key, set())
            result = self._run(tool, args, needed)
            elapsed = round((self.clock() - start) * 1000)
            row = {"tool": tool, "ms": elapsed, "pages": result.get("pages", 0)}
            if args:
                row["args"] = args
            if "error" in result:
                row["error"] = result["error"]
            if result.get("truncated"):
                row["truncated"] = True
            self.runs.append(row)
            self.cache[key] = result
        return self.cache[key]

    def _run(self, tool: str, args: dict, needed: set[str] | None) -> dict:
        function = self.tools.get(tool)
        if function is None:
            return {"error": "tool_not_available",
                    "message": f"{tool} is not available to the checklist runner."}
        try:
            signature = inspect.signature(function)
        except (TypeError, ValueError):
            signature = None
        if signature is not None:
            try:
                signature.bind(**args)
            except TypeError as error:
                return {"error": "invalid_arguments", "message": f"{tool}: {error}"}
        pages_offset = signature is not None and "offset" in signature.parameters
        pages, offset = 0, 0
        merged: dict | None = None
        first_lists: dict[str, list] = {}
        try:
            while True:
                call_args = {**args, "offset": offset} if pages_offset else dict(args)
                raw = function(**call_args)
                data = json.loads(raw) if isinstance(raw, str) else raw
                pages += 1
                if not isinstance(data, dict):
                    return {"error": "not_json", "pages": pages,
                            "message": f"{tool} did not return a JSON object."}
                if merged is None:
                    if "error" in data:
                        return {"error": str(data["error"]), "pages": pages,
                                "message": clip(data.get("message") or data["error"], 300),
                                "payload": data}
                    merged = data
                    first_lists = {k: list(v) for k, v in data.items() if isinstance(v, list)}
                else:
                    if "error" in data:  # a later page failing: keep what was read
                        merged["truncated"] = True
                        break
                    new_rows = False
                    for name, value in data.items():
                        if (isinstance(value, list) and isinstance(merged.get(name), list)
                                and value and value != first_lists.get(name)):
                            merged[name].extend(value)
                            new_rows = new_rows or needed is None or name in needed
                    if not new_rows:
                        break
                following = data.get("next_offset")
                if (not pages_offset or not isinstance(following, int)
                        or isinstance(following, bool) or following <= offset):
                    break
                if needed is not None and not (needed & set(first_lists)):
                    break
                if pages >= self.max_pages:
                    merged["truncated"] = True
                    break
                offset = following
        except json.JSONDecodeError:
            return {"error": "not_json", "pages": pages,
                    "message": f"{tool} did not return JSON."}
        except Exception as error:  # a tool failing must not stop the checklist
            return {"error": "tool_failed", "exception": type(error).__name__, "pages": pages,
                    "message": f"{tool} failed: {type(error).__name__}: {clip(error, 200)}"}
        result = {"data": merged, "pages": pages}
        if merged.get("truncated"):
            result["truncated"] = True
        return result


def run_custom_rules(runner: ToolRunner, rule_ids: list[str]) -> dict:
    """run_custom_checks on the custom items' rules, once. Rule ids no pack defines are
    left out and run again without them (those items say unknown_rule)."""
    result = runner.run(CUSTOM_TOOL, {"rules": rule_ids})
    if result.get("error") != "unknown_rule":
        return result
    unknown = {str(name).lower() for name in (result.get("payload") or {}).get("unknown") or []}
    known = [rule for rule in rule_ids if rule.lower() not in unknown]
    if not unknown:
        return result
    if known:
        return runner.run(CUSTOM_TOOL, {"rules": known})
    return {"data": {"rules": []}, "pages": 0}


def runner_needs(items: list[dict]) -> dict[tuple[str, str], set[str]]:
    """The ToolRunner needs of these checklist items (of one checklist or several that
    share a runner): each auto and assisted item's tool call -> the result lists its
    conditions and anchors read. A call not listed (the custom items' run_custom_checks:
    its rule rows come with every page) reads its first page only."""
    needs: dict[tuple[str, str], set[str]] = {}
    for item in items:
        if item["kind"] in ("auto", "assisted"):
            evidence = item["evidence"]
            needs.setdefault(ToolRunner.key(evidence["tool"], evidence["args"]),
                             set()).update(list_keys(evidence))
    return needs


def list_keys(evidence: dict) -> set[str]:
    """The top-level result keys whose lists the evidence's conditions and anchor
    selectors read (paging needs only these)."""
    keys: set[str] = set()

    def walk(cond: dict) -> None:
        for member in cond.get("all") or cond.get("any") or []:
            walk(member)
        if "list" in cond:
            keys.add(cond["list"].split(".")[0])

    for group in GROUPS:
        for cond in evidence.get(group, []):
            walk(cond)
    for selector in evidence.get("anchors", []):
        walk(selector)
    return keys


# ---------- evaluating conditions ----------

def resolve(data, path: str):
    """The value at a dotted path ('summary.by_level.Error'), or _MISSING."""
    current = data
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return _MISSING
    return current


def _compare(actual, op: str, expected=None, absent_is_falsy: bool = False) -> bool:
    """actual op expected. A missing value (_MISSING) matches only 'missing', and 'falsy'
    as well with absent_is_falsy (a row's optional field: no 'fuse' key is no fuse)."""
    if op == "exists":
        return actual is not _MISSING
    if op == "missing":
        return actual is _MISSING
    if actual is _MISSING:
        return absent_is_falsy and op == "falsy"
    if expected is _MISSING:
        return False
    if op == "truthy":
        return bool(actual)
    if op == "falsy":
        return not actual
    if op in ("empty", "nonempty"):
        sized = isinstance(actual, (list, dict, str))
        return sized and (len(actual) == 0) == (op == "empty")
    if op == "in":
        return actual in expected
    if op == "not_in":
        return actual not in expected
    if op == "==":
        return actual == expected
    if op == "!=":
        return actual != expected
    if isinstance(actual, bool) or not isinstance(actual, (int, float)):
        return False
    return {">": actual > expected, ">=": actual >= expected, "<": actual < expected,
            "<=": actual <= expected}[op]


def _matches(row, where: dict) -> bool:
    if not isinstance(row, dict):
        return False
    return all(_compare(resolve(row, field), spec["op"],
                        resolve(row, spec["field"]) if "field" in spec else spec.get("value"),
                        absent_is_falsy=True)
               for field, spec in where.items())


# Path conditions whose path may be absent by design (an optional flag, a presence test):
# for every other condition a path or list missing from the tool's result is reported.
_ABSENCE_OPS = {"exists", "missing", "truthy", "falsy"}


def _absent_count(data: dict, path: str, op: str, expected) -> bool:
    """Whether a missing path is a count of zero: its parent is an object of counts (a
    tool's summary by check, which lists only the checks that found something; {} on a
    clean board) and it is compared with a number."""
    if op not in (*_NUMERIC_OPS, "==", "!=") or isinstance(expected, bool) or             not isinstance(expected, (int, float)):
        return False
    head, _, _ = path.rpartition(".")
    parent = resolve(data, head) if head else data
    return isinstance(parent, dict) and all(
        isinstance(value, (int, float)) and not isinstance(value, bool)
        for value in parent.values())


def evaluate(cond: dict, data: dict, absent_counts: bool = False) -> dict:
    """{"hit", "count" or "value", "items" (matching rows), "label", "unresolved" (paths
    and lists not in data)}. With absent_counts a path missing from an object of counts
    is compared as 0 and listed in "zero" (check packs' tool rules)."""
    if "all" in cond or "any" in cond:
        members = [evaluate(member, data, absent_counts)
                   for member in cond.get("all") or cond.get("any")]
        hit = all(m["hit"] for m in members) if "all" in cond else any(m["hit"] for m in members)
        items = [row for m in members if m["hit"] or "all" in cond for row in m["items"]]
        label = cond.get("label") or (" and " if "all" in cond else " or ").join(
            m["label"] for m in members)
        shown = [m for m in members if m["hit"]] or members
        out = {"hit": hit, "items": items, "label": label,
               "count": sum(m.get("count", 0) for m in shown if "count" in m)
               if all("count" in m for m in shown) else None,
               "unresolved": [path for m in members for path in m["unresolved"]]}
        zero = [path for m in members for path in m.get("zero", [])]
        if zero:
            out["zero"] = zero
        return out
    if "list" in cond:
        found = resolve(data, cond["list"])
        rows = found if isinstance(found, list) else []
        matched = [row for row in rows if _matches(row, cond["where"])] if "where" in cond \
            else [row for row in rows]
        count = len(matched)
        label = cond.get("label") or _default_label(cond)
        return {"hit": _compare(count, cond["op"], cond.get("value")), "count": count,
                "items": [row for row in matched if isinstance(row, dict)], "label": label,
                "unresolved": [] if isinstance(found, list) else [cond["list"]]}
    value = resolve(data, cond["path"])
    label = cond.get("label") or _default_label(cond)
    if value is _MISSING and absent_counts and _absent_count(data, cond["path"], cond["op"],
                                                             cond.get("value")):
        return {"hit": _compare(0, cond["op"], cond.get("value")), "op": cond["op"],
                "value": 0, "items": [], "label": label, "unresolved": [],
                "zero": [cond["path"]]}
    return {"hit": _compare(value, cond["op"], cond.get("value")), "op": cond["op"],
            "value": None if value is _MISSING else value, "items": [], "label": label,
            "unresolved": [cond["path"]] if value is _MISSING
            and cond["op"] not in _ABSENCE_OPS else []}


def _default_label(cond: dict) -> str:
    if "list" in cond:
        where = ", ".join(
            f"{field} {spec['op']} {spec['field'] if 'field' in spec else spec.get('value', '')}"
            .strip() for field, spec in (cond.get("where") or {}).items())
        return f"{cond['list']}{' where ' + where if where else ''}"
    value = f" {cond['value']}" if "value" in cond else ""
    return f"{cond['path']} {cond['op']}{value}"


def _strings(value) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str) and v.strip()]
    return []


def anchors_of(rows: list[dict], tool: str | None = None) -> dict[str, list[str]]:
    """Designators, nets, sheets and other names the rows (and their example rows) of
    tool's result are about, each list in first-seen order, at most ANCHORS_LISTED, with
    *_total when there are more. A key of ANCHOR_KEY_TOOLS counts for its tools only."""
    found: dict[str, list[str]] = {group: [] for group, _ in ANCHOR_GROUPS}
    groups = [(group, [key for key in keys
                       if key not in ANCHOR_KEY_TOOLS or tool in ANCHOR_KEY_TOOLS[key]])
              for group, keys in ANCHOR_GROUPS]

    def take(row: dict, depth: int) -> None:
        for group, keys in groups:
            for key in keys:
                for value in _strings(row.get(key)):
                    if value not in found[group]:
                        found[group].append(value)
        if depth == 0:
            for value in row.values():
                if isinstance(value, list):
                    for sub in value[:50]:
                        if isinstance(sub, dict):
                            take(sub, 1)

    for row in rows:
        take(row, 0)
    out: dict = {}
    for group, values in found.items():
        if values:
            out[group] = values[:ANCHORS_LISTED]
            if len(values) > ANCHORS_LISTED:
                out[f"{group}_total"] = len(values)
    return out


def finding_ids(rows: list[dict]) -> dict[str, int]:
    """How many finding rows of each id the rows hold: a row's check (violation,
    advisory, gate id ...), with its code or kind after a '/' when it has one."""
    counts: Counter = Counter()
    for row in rows:
        for key in FINDING_ID_KEYS:
            value = row.get(key)
            if not isinstance(value, str) or not value.strip():
                continue
            name = value
            if key not in FINDING_DETAIL_KEYS:
                detail = next((row[k] for k in FINDING_DETAIL_KEYS
                               if isinstance(row.get(k), str) and _CODE.fullmatch(row[k])),
                              None)
                if detail:
                    name = f"{value}/{detail}"
            counts[name] += 1
            break
    return dict(counts.most_common(FINDING_IDS_LISTED))


def row_text(rows: list[dict]) -> str | None:
    """The first row's own words (its issue, message or evidence: a sentence, not a
    code)."""
    for row in rows:
        for key in FINDING_TEXT_KEYS:
            value = row.get(key)
            if isinstance(value, str) and " " in value.strip():
                return clip(value, ITEM_TEXT_CHARS)
    return None


_NUMERIC_OPS = (">", ">=", "<", "<=")


def _hit_text(hit: dict, clean: bool = False) -> str | None:
    """One evidence phrase for a condition: 'label: count (ids) - the first finding's
    text' for a list, 'label: value' for a numeric threshold, else the label. clean (the
    Yes summary of conditions that did not hit) keeps counts and numbers only."""
    quoted = row_text(hit["items"])
    if "value" in hit:
        numeric = hit.get("op") in _NUMERIC_OPS and isinstance(hit["value"], (int, float))
        if clean and not numeric:
            return None
        return f"{hit['label']}: {hit['value']}" if numeric else hit["label"]
    count = hit.get("count")
    if count is None and clean:
        return None
    if count == 1 and quoted:
        return f"{hit['label']} — {quoted}"
    text = hit["label"] if count is None else f"{hit['label']}: {count}"
    ids = finding_ids(hit["items"])
    if ids and (len(ids) > 1 or count is None or count > 1):
        text += " (" + ", ".join(f"{name} x{n}" if n > 1 else name
                                 for name, n in list(ids.items())[:3]) + ")"
    if quoted:
        text += f" — {quoted}"
    return text


def evaluate_item(item: dict, result: dict | None) -> dict:
    """The item's row: status, lean (assisted), evidence line, finding ids, anchors."""
    row: dict = {"id": item["id"], "section": item["section"], "text": item["text"],
                 "kind": item["kind"]}
    if item["kind"] == "judgment":
        row.update(status="needs_judgment", evidence="A person decides: no tool answers this.")
        return _finish(row, item)
    evidence = item["evidence"]
    row["tool"] = evidence["tool"]
    if evidence["args"]:
        row["args"] = evidence["args"]
    if result is None or "error" in result:
        result = result or {"error": "not_run", "message": "the tool did not run"}
        code = result["error"]
        if code in evidence["na_errors"]:
            row.update(status="n/a", evidence=f"Not applicable: {result.get('message') or code}")
        else:
            row.update(status="needs_judgment", could_not_run=result.get("exception") or code,
                       evidence=f"Could not run: {result.get('message') or code}")
        return _finish(row, item)
    data = result["data"]
    decided, hits, evaluated = None, [], {}
    for group in ("na", "no", "judge"):
        evaluated[group] = [evaluate(cond, data) for cond in evidence.get(group, [])]
        hits = [r for r in evaluated[group] if r["hit"]]
        if hits:
            decided = {"na": "n/a", "no": "no", "judge": "needs_judgment"}[group]
            break
    if decided is None:
        evaluated["yes"] = required = [evaluate(cond, data) for cond in evidence.get("yes", [])]
        missing = [r for r in required if not r["hit"]]
        if missing:
            decided = "needs_judgment"
            text = "; ".join(f"not met: {r['label']}" for r in missing[:3])
        elif required:
            decided = "yes"
            text = "; ".join(_hit_text(r) for r in required[:3])
        else:
            decided = "yes"
            checked = evaluated["no"] + evaluated["judge"]
            phrases = [phrase for phrase in (_hit_text(r, clean=True) for r in checked)
                       if phrase]
            text = "; ".join(phrases[:4]) or (
                "None of these reported: " + "; ".join(r["label"] for r in checked[:4])
                if checked else "The item names no No or needs-judgment condition.")
        unresolved = list(dict.fromkeys(path for results in evaluated.values()
                                        for r in results for path in r["unresolved"]))
        if unresolved:
            # A path the item reads is not in the tool's result (renamed, or misspelt in an
            # override): what was not read cannot pass.
            row["unresolved"] = unresolved[:FINDING_IDS_LISTED]
            decided = "needs_judgment"
            text = (f"Could not check: {', '.join(unresolved[:3])} not in the "
                    f"{evidence['tool']} result (a field the item reads): confirm with the "
                    "tool.")
    if hits:
        if decided == "n/a":
            text = "; ".join(hit["label"] + (f" — {row_text(hit['items'])}"
                                             if row_text(hit["items"]) else "")
                             for hit in hits[:3])
        else:
            text = "; ".join(_hit_text(r) for r in hits[:3])
        if len(hits) > 3:
            text += f"; and {len(hits) - 3} more"
        rows = [r for hit in hits for r in hit["items"]]
        if decided != "n/a":
            if not rows:
                rows = [r for selector in evidence.get("anchors", [])
                        for r in evaluate(selector, data)["items"]]
            anchors = anchors_of(rows, evidence["tool"])
            if anchors:
                row["anchors"] = anchors
            ids = finding_ids(rows)
            if ids:
                row["findings"] = ids
    if result.get("truncated") and decided == "yes":
        decided = "needs_judgment"
        text += "; the tool's result had more pages than were read: confirm with the tool."
    row["evidence"] = clip(text, EVIDENCE_CHARS)
    if item["kind"] == "assisted" and decided != "n/a":
        row["status"] = "needs_judgment"
        if decided in ("yes", "no"):
            row["lean"] = decided
    else:
        row["status"] = decided
    return _finish(row, item)


_CUSTOM_STATUS = {"pass": "yes", "fail": "no", "not_applicable": "n/a",
                  "could_not_evaluate": "needs_judgment"}


def evaluate_custom_item(item: dict, result: dict | None) -> dict:
    """A custom item's row from run_custom_checks' result: its rule's pass is Yes, fail
    No, not applicable N/A; a rule that could not be evaluated, or left objects not
    evaluated, needs judgment (could_not_run names why), as does a rule no pack defines
    or a plugin that was not run."""
    rule_id = item["rule"]
    row: dict = {"id": item["id"], "section": item["section"], "text": item["text"],
                 "kind": "custom", "tool": CUSTOM_TOOL, "args": {"rules": [rule_id]},
                 "rule": rule_id}
    if result is None or "error" in result:
        result = result or {"error": "not_run", "message": "the check packs did not run"}
        code = result["error"]
        row.update(status="needs_judgment", could_not_run=result.get("exception") or code,
                   evidence=clip(f"Could not run: {result.get('message') or code}",
                                 EVIDENCE_CHARS))
        return _finish(row, item)
    data = result["data"]
    rule = next((r for r in data.get("rules") or [] if isinstance(r, dict)
                 and str(r.get("id", "")).lower() == rule_id.lower()), None)
    if rule is None:  # a result too large for even the shortest rows gives statuses only
        status = next((value for key, value in (data.get("rule_status") or {}).items()
                       if str(key).lower() == rule_id.lower()), None)
        if status is not None:
            rule = {"id": rule_id, "status": status}
    if rule is None:
        plugins = data.get("plugins") or {}
        if rule_id.lower() in {str(p).lower() for p in plugins.get("found") or []}:
            row.update(status="needs_judgment", could_not_run="plugins_not_run",
                       evidence=clip(f"Plugin {rule_id} was not run (plugins run only from "
                                     "run_custom_checks with allow_plugins=true): "
                                     f"{plugins.get('reason') or ''}", EVIDENCE_CHARS))
        else:
            row.update(status="needs_judgment", could_not_run="unknown_rule",
                       evidence=f"No check pack defines the rule {rule_id}.")
        return _finish(row, item)
    status = _CUSTOM_STATUS.get(rule.get("status"), "needs_judgment")
    name = f"{rule_id} ({rule['pack']})" if rule.get("pack") else rule_id
    checked, count = rule.get("checked", 0), rule.get("findings", 0)
    if status == "no":
        levels = ", ".join(f"{level} x{n}" for level, n in (rule.get("severity_counts")
                                                           or {}).items())
        text = f"{name}: {count} finding(s)" + (f" ({levels})" if levels else "")
        if rule.get("first"):
            text += f" — {clip(rule['first'], ITEM_TEXT_CHARS)}"
        if rule.get("anchors"):
            row["anchors"] = rule["anchors"]
        row["findings"] = {rule_id: count}
    elif status == "yes":
        text = f"{name}: {checked} checked, none failed"
        if rule.get("not_evaluated"):
            status = "needs_judgment"
            text += (f"; {rule['not_evaluated']} could not be evaluated "
                     f"({', '.join(rule.get('not_evaluated_reasons') or {})})")
    elif status == "n/a":
        text = f"{name}: not applicable ({rule.get('reason') or 'nothing selected'})"
    else:
        reason = str(rule.get("reason") or "could_not_evaluate")
        row["could_not_run"] = reason.split(":")[0].split(" (")[0].split(" on ")[0]
        text = f"{name}: could not be evaluated ({rule.get('reason') or 'unknown'})"
    row["status"] = status
    row["evidence"] = clip(text, EVIDENCE_CHARS)
    return _finish(row, item)


def _finish(row: dict, item: dict) -> dict:
    for key in ("hint", "source"):
        if item.get(key):
            row[key] = item[key]
    if item.get("origin") not in (None, "bundled"):
        row["origin"] = item["origin"]
    return row


def coverage(rows: list[dict], sections: list[dict]) -> list[dict]:
    """Items by status per section (sections with items only), then an 'all' row."""
    table = []
    for section in sections:
        mine = [row for row in rows if row["section"] == section["id"]]
        if not mine:
            continue
        counts = Counter(row["status"] for row in mine)
        table.append({"section": section["id"], "title": section["title"],
                      **{status: counts.get(status, 0) for status in STATUSES},
                      "total": len(mine)})
    counts = Counter(row["status"] for row in rows)
    table.append({"section": "all", "title": "All", **{s: counts.get(s, 0) for s in STATUSES},
                  "total": len(rows)})
    return table


def run_review_checklist(project: dict, netlist: dict | None, checklist: dict, *,
                         tools: dict[str, Callable[..., Any]],
                         sections: list[str] | None = None, items: list[str] | None = None,
                         variant: str | None = None, state: dict | None = None,
                         runner: ToolRunner | None = None,
                         clock: Callable[[], float] = time.perf_counter) -> dict:
    """Run the checklist's selected items: each auto and assisted item's tool (once per
    tool and arguments), mapped to its status. Returns the model the pages and the
    record are rendered from."""
    start = clock()
    chosen = select(checklist, sections, items)
    custom_ids = sorted({item["rule"] for item in chosen if item["kind"] == "custom"},
                        key=str.lower)
    if runner is None:
        runner = ToolRunner(tools, clock=clock, needs=runner_needs(chosen))
    custom = run_custom_rules(runner, custom_ids) if custom_ids else None
    rows = []
    for item in chosen:
        if item["kind"] == "custom":
            rows.append(evaluate_custom_item(item, custom))
            continue
        result = None
        if item["kind"] != "judgment":
            evidence = item["evidence"]
            result = runner.run(evidence["tool"], evidence["args"])
        rows.append(evaluate_item(item, result))
    could_not_run: dict[tuple[str, str], list[str]] = {}
    for row in rows:
        if row.get("could_not_run"):
            could_not_run.setdefault((row["tool"], row["could_not_run"]), []).append(row["id"])
    return {
        "project": project.get("name"),
        "prj_pcb": PureWindowsPath(project.get("prj_pcb_path") or "").name or None,
        "checklist": {key: checklist[key] for key in (
            "id", "title", "scope", "version", "description", "source", "origin")
            if checklist.get(key)},
        "disabled": checklist.get("disabled") or [],
        "sections": [s for s in checklist["sections"]
                     if any(row["section"] == s["id"] for row in rows)],
        "selection": {"sections": sections or None, "items": items or None},
        "variant": variant,
        "load": "netlist" if netlist is not None else "netlist_free",
        "state": state or {},
        "items": rows,
        "coverage": coverage(rows, checklist["sections"]),
        "could_not_run": [{"tool": tool, "error": error, "items": ids}
                          for (tool, error), ids in could_not_run.items()],
        "tool_runs": runner.runs,
        "elapsed_ms": round((clock() - start) * 1000),
    }


def apply_decisions(model: dict, decisions: dict[str, dict]) -> dict:
    """The model with the review's decisions applied: status is the decision, the tool's
    own stays as tool_status; coverage is recounted."""
    if not decisions:
        return model
    rows = []
    for row in model["items"]:
        decision = decisions.get(row["id"])
        if decision:
            row = {**row, "tool_status": row["status"], "status": decision["status"],
                   "decided_in_review": decision["note"] or "decided in review"}
        rows.append(row)
    sections = model["sections"]
    return {**model, "items": rows, "coverage": coverage(rows, sections),
            "decisions_applied": len(decisions)}


# ---------- design state ----------

def _sha1(path: Path) -> str:
    digest = hashlib.sha1()  # a file fingerprint, not a security hash
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _utc(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def design_files(project: dict) -> list[Path]:
    """The project's design files: the .PrjPcb, its sheets, PcbDocs and Draftsman
    drawings, each once, in that order."""
    paths: list[str] = [project.get("prj_pcb_path") or ""]
    paths += [sheet.get("path") or "" for sheet in project.get("sheets") or []]
    paths += list(project.get("pcb_doc_paths") or [])
    paths += list(project.get("pcb_dwf_paths") or [])
    seen, out = set(), []
    for text in paths:
        if not text:
            continue
        key = str(text).lower()
        if key not in seen:
            seen.add(key)
            out.append(Path(text))
    return out


def design_state(project: dict,
                 git_state: Callable[[str, list[str]], dict | None] | None = None) -> dict:
    """The design state a review belongs to: SHA1 and save time of each design file and a
    fingerprint over them, plus the Git commit and whether any of these files differs
    from it when the project is in a Git work tree (git_state(project_dir, design file
    paths), clone_status's head_state: a file outside the work tree, untracked or
    git-ignored differs too). id is what a sign-off names: 'git <commit>' (with
    '+changes' when a design file differs) or 'files <fingerprint>'."""
    root = Path(project.get("root_dir") or ".")
    paths = design_files(project)
    files = []
    for path in paths:
        try:
            # Relative to the project folder, '../' for a sheet beside it: two sheets
            # of the same name in different folders stay apart.
            name = Path(os.path.relpath(path.resolve(), root.resolve())).as_posix()
        except (ValueError, OSError):  # another drive
            name = PureWindowsPath(str(path)).name
        try:
            stat = path.stat()
            files.append({"file": name, "sha1": _sha1(path), "saved_utc": _utc(stat.st_mtime),
                          "bytes": stat.st_size})
        except OSError:
            files.append({"file": name, "missing": True})
    lines = "\n".join(f"{row['file']}:{row.get('sha1', 'missing')}" for row in files)
    fingerprint = hashlib.sha1(lines.encode("utf-8")).hexdigest()
    state: dict = {"kind": "files", "fingerprint": fingerprint, "files": files}
    git = None
    if git_state is not None:
        try:
            git = git_state(str(root), [str(path) for path in paths])
        except Exception as error:  # the hashes stand on their own
            state["git_error"] = f"{type(error).__name__}"
    if git and git.get("commit"):
        state.update(kind="git", commit=git["commit"],
                     commit_date_utc=git.get("date_utc"),
                     changes_since_commit=bool(git.get("design_files_changed")),
                     design_files_changed=git.get("design_files_changed", 0))
        for key in ("branch", "altium365"):
            if git.get(key) is not None:
                state[key] = git[key]
        state["id"] = f"git {git['commit'][:12]}" + (
            "+changes" if state["changes_since_commit"] else "")
    else:
        if git and git.get("error"):
            state["git_error"] = git["error"]
        state["id"] = f"files {fingerprint[:12]}"
    if any(row.get("missing") for row in files):
        state["missing_files"] = sum(1 for row in files if row.get("missing"))
    return state


FOLDER_SIGNATURE_FILES = 20000  # files stat'ed for folder_signature


def folder_signature(root, max_files: int | None = None) -> str | None:
    """A fingerprint of every file's path, size and save time under root (.git left
    out): it changes when an OutJob, a DRC report, a generated output, a library or a
    fab profile in the project folder is saved, which the design state does not cover.
    None when there are more than max_files (FOLDER_SIGNATURE_FILES) files or the folder
    cannot be read."""
    max_files = FOLDER_SIGNATURE_FILES if max_files is None else max_files
    digest = hashlib.sha1()  # a change detector, not a security hash
    count = 0
    pending = [Path(root)]
    try:
        while pending:
            folder = pending.pop()
            with os.scandir(folder) as entries:
                for entry in sorted(entries, key=lambda e: e.name):
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name != ".git":
                            pending.append(Path(entry.path))
                        continue
                    count += 1
                    if count > max_files:
                        return None
                    stat = entry.stat(follow_symlinks=False)
                    digest.update(f"{entry.path}|{stat.st_size}|{stat.st_mtime_ns}\n"
                                  .encode("utf-8", "surrogatepass"))
    except OSError:
        return None
    return digest.hexdigest()


# ---------- the review record ----------

def _state_line(state: dict) -> str:
    if state.get("kind") == "git":
        line = f"Git commit `{state['commit'][:12]}`"
        if state.get("commit_date_utc"):
            line += f" ({state['commit_date_utc']})"
        line += (f", with {state['design_files_changed']} design file(s) changed since it "
                 "(not committed)" if state.get("changes_since_commit")
                 else ", design files as committed")
        return line + f"; files SHA1 fingerprint `{state['fingerprint'][:12]}`"
    return f"Files SHA1 fingerprint `{state.get('fingerprint', '')[:12]}` (not in a Git work tree)"


def _anchor_text(anchors: dict | None, limit: int) -> str:
    if not anchors:
        return ""
    parts = []
    for group, _ in ANCHOR_GROUPS:
        values = anchors.get(group) or []
        if values:
            shown = ", ".join(values[:limit])
            total = anchors.get(f"{group}_total", len(values))
            if total > min(limit, len(values)):
                shown += f" +{total - min(limit, len(values))}"
            parts.append(shown)
    return "; ".join(parts)


def _status_text(row: dict) -> str:
    text = STATUS_LABELS[row["status"]]
    if row.get("lean"):
        text += f" (leans {STATUS_LABELS[row['lean']]})"
    if row.get("tool_status"):
        text += f" (review; tool: {STATUS_LABELS[row['tool_status']]})"
    return text


def record_block(model: dict, generated_utc: str, generator: str | None) -> dict:
    """The machine-readable part of the record, which previous= reads back."""
    state = model["state"]
    return {
        "format": RECORD_MARKER, "version": RECORD_VERSION, "generator": generator,
        "generated_utc": generated_utc, "project": model.get("prj_pcb"),
        "checklist": {key: model["checklist"].get(key) for key in ("id", "version", "title")},
        "selection": model.get("selection"),
        "variant": model.get("variant"),
        "state": {key: state.get(key) for key in (
            "id", "kind", "commit", "changes_since_commit", "fingerprint") if key in state}
        | {"files": {row["file"]: row.get("sha1") for row in state.get("files") or []}},
        "items": {row["id"]: {key: row[key] for key in ("status", "tool_status", "kind",
                                                        "section") if row.get(key)}
                  for row in model["items"]},
    }


def render_record(model: dict, *, generated_utc: str, generator: str | None = None,
                  comparison: dict | None = None, evidence_chars: int = 220,
                  anchors_shown: int = 6, files_shown: int = 60, text_chars: int = 300,
                  open_detail: bool = True) -> str:
    """The review record as Markdown: header, design state, coverage table, changes since
    the previous record, every item with its status and evidence, the open items, the
    sign-off table and the machine-readable block. The size steps shorten the evidence,
    anchors, design files and item texts; without open_detail the open items are listed
    by id and status only (their text is in the items table)."""
    checklist = model["checklist"]
    state = model["state"]
    lines = [f"# Review record: {checklist['title']}", ""]
    lines.append(f"- Project: `{model.get('prj_pcb') or model.get('project')}`")
    version = f" v{checklist['version']}" if checklist.get("version") else ""
    lines.append(f"- Checklist: `{checklist['id']}`{version} ({checklist.get('origin', 'bundled')})")
    selection = model.get("selection") or {}
    if selection.get("sections") or selection.get("items"):
        parts = []
        if selection.get("sections"):
            parts.append("sections " + ", ".join(selection["sections"]))
        if selection.get("items"):
            parts.append("items " + ", ".join(selection["items"]))
        lines.append(f"- Scope of this run: {'; '.join(parts)}")
    if model.get("overrides_applied"):
        lines.append("- Team overrides: " + ", ".join(a["file"] for a in model["overrides_applied"]))
    if model.get("disabled"):
        lines.append("- Disabled by overrides: " + ", ".join(d["id"] for d in model["disabled"]))
    lines.append(f"- Variant: {model.get('variant') or '[No Variations]'}")
    lines.append(f"- Load: {'netlist' if model.get('load') == 'netlist' else 'netlist-free (schematic tools could not run)'}")
    lines.append(f"- Design state: **{state.get('id', '?')}**: {_state_line(state)}")
    lines.append(f"- Generated: {generated_utc} by altium-copilot"
                 f"{' ' + generator if generator else ''} (run_review_checklist; read-only)")
    lines += ["", "Sign-off applies to this design state only: a later save or commit is a new "
                  "state and needs its own review.", ""]
    files = state.get("files") or []
    if files:
        lines += ["## Design files", "", "| File | SHA1 | Saved (UTC) |", "|---|---|---|"]
        for row in files[:files_shown]:
            lines.append(f"| {_md(row['file'])} | "
                         f"{'missing' if row.get('missing') else '`' + row['sha1'][:12] + '`'} | "
                         f"{row.get('saved_utc', '')} |")
        if len(files) > files_shown:
            lines.append(f"| ... {len(files) - files_shown} more | | |")
        lines.append("")
    lines += ["## Checklist coverage", "",
              "| Section | Yes | No | N/A | Needs judgment | Total |",
              "|---|---|---|---|---|---|"]
    for row in model["coverage"]:
        title = f"**{row['title']}**" if row["section"] == "all" else _md(row["title"])
        lines.append(f"| {title} | {row['yes']} | {row['no']} | {row['n/a']} | "
                     f"{row['needs_judgment']} | {row['total']} |")
    lines.append("")
    if comparison:
        lines += render_comparison(comparison)
    lines += ["## Items", ""]
    titles = {s["id"]: s["title"] for s in model["sections"]}
    for section in model["sections"]:
        mine = [row for row in model["items"] if row["section"] == section["id"]]
        if not mine:
            continue
        lines += [f"### {titles[section['id']]}", "",
                  "| ID | Item | Kind | Status | Evidence | Tool | Anchors |",
                  "|---|---|---|---|---|---|---|"]
        for row in mine:
            evidence = row.get("evidence") or ""
            if row.get("decided_in_review"):
                evidence = f"Review: {row['decided_in_review']}. Tool: {evidence}"
            lines.append(
                f"| {row['id']} | {_md(clip(row['text'], text_chars))} | {row['kind']} | "
                f"{_status_text(row)} | "
                f"{_md(clip(evidence, evidence_chars))} | {row.get('tool', '')} | "
                f"{_md(_anchor_text(row.get('anchors'), anchors_shown))} |")
        lines.append("")
    open_rows = [row for row in model["items"] if row["status"] in ("no", "needs_judgment")]
    if open_rows:
        lines += ["## Open items", ""]
        for row in open_rows:
            if not open_detail:
                lines.append(f"- [ ] {row['id']} ({STATUS_LABELS[row['status']]})")
                continue
            hint = f" ({clip(row['hint'], 160)})" if row.get("hint") else ""
            lines.append(f"- [ ] {row['id']} ({STATUS_LABELS[row['status']]}): "
                         f"{clip(row['text'], 160)}{hint}")
        lines.append("")
    lines += ["## Sign-off", "",
              "| Role | Name | Decision (approve / approve with actions / reject) | Date | Signature |",
              "|---|---|---|---|---|",
              "| Reviewer | | | | |",
              "| Design owner | | | | |",
              "| Approver | | | | |", "",
              "Notes and actions:", "", "- ", ""]
    block = json.dumps(record_block(model, generated_utc, generator), separators=(",", ":"),
                       ensure_ascii=True).replace("--", "-\\u002d")
    lines += [f"<!-- {RECORD_MARKER} v{RECORD_VERSION}", block, "-->", ""]
    return "\n".join(lines)


def parse_record(text: str) -> dict:
    """The machine-readable block of a record render_record wrote; ChecklistError
    (previous_not_a_record) when there is none or it cannot be read."""
    match = _RECORD_BLOCK.search(text)
    if match is None:
        raise ChecklistError({"error": "previous_not_a_record",
                              "message": "The previous file holds no review record block "
                                         f"('<!-- {RECORD_MARKER} v1 ... -->'): pass a "
                                         "record saved from run_review_checklist(format="
                                         "'markdown')."})
    if int(match[1]) > RECORD_VERSION:
        raise ChecklistError({"error": "previous_not_a_record",
                              "message": f"The previous record is format v{match[1]}, newer "
                                         "than this server reads (v1): update the server."})
    try:
        data = json.loads(match[2])
    except json.JSONDecodeError as error:
        raise ChecklistError({"error": "previous_not_a_record",
                              "message": "The previous record's block is not valid JSON "
                                         f"(line {error.lineno}): was it edited?"}) from None
    if not isinstance(data, dict) or not isinstance(data.get("items"), dict):
        raise ChecklistError({"error": "previous_not_a_record",
                              "message": "The previous record's block has no items."})
    return data


def read_previous(path_text: str, project_dir) -> dict:
    """The record at path_text (absolute, or relative to the project folder), parsed."""
    path = Path(path_text)
    if not path.is_absolute() and project_dir is not None:
        path = Path(project_dir) / path
    try:
        if not path.is_file():
            raise ChecklistError({"error": "previous_not_found",
                                  "message": f"No file at {path_text}."})
        if path.stat().st_size > MAX_RECORD_BYTES:
            raise ChecklistError({"error": "previous_not_a_record",
                                  "message": "The previous file is larger than "
                                             f"{MAX_RECORD_BYTES // (1024 * 1024)} MB."})
        text = path.read_bytes().decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ChecklistError({"error": "previous_not_a_record",
                              "message": "The previous file is not UTF-8 text."}) from None
    except OSError as error:
        raise ChecklistError({"error": "previous_not_found",
                              "message": f"The previous file could not be read "
                                         f"({type(error).__name__})."}) from None
    record = parse_record(text)
    record["file"] = path.name
    return record


_OPEN = ("no", "needs_judgment")


def compare(model: dict, previous: dict) -> dict:
    """What changed since the previous record: the design state, then per item newly
    failing (No now, not before), fixed (No before, Yes now), still open (No or needs
    judgment both times, newly failing ones aside), other status changes (No to N/A among
    them: an item that no longer applies is not a fix), items new to or gone from this
    run."""
    now = {row["id"]: row["status"] for row in model["items"]}
    before = {key: (value or {}).get("status") for key, value in previous["items"].items()}
    newly_failing = [i for i, s in now.items() if i in before and s == "no"
                     and before[i] != "no"]
    fixed = [i for i, s in now.items() if before.get(i) == "no" and s == "yes"]
    state, old_state = model["state"], previous.get("state") or {}
    old_files = old_state.get("files") or {}
    new_files = {row["file"]: row.get("sha1") for row in state.get("files") or []}
    files_changed = sorted(name for name in set(old_files) | set(new_files)
                           if old_files.get(name) != new_files.get(name))
    result = {
        "previous": {"file": previous.get("file"),
                     "generated_utc": previous.get("generated_utc"),
                     "state": old_state.get("id"),
                     "checklist": (previous.get("checklist") or {}).get("id")},
        "state_changed": old_state.get("id") != state.get("id")
        or old_state.get("fingerprint") != state.get("fingerprint"),
        "files_changed": files_changed,
        "newly_failing": newly_failing,
        "fixed": fixed,
        "still_open": [i for i, s in now.items() if s in _OPEN and before.get(i) in _OPEN
                       and i not in newly_failing],
        "changed": [{"id": i, "from": before[i], "to": s} for i, s in now.items()
                    if i in before and before[i] != s and i not in newly_failing
                    and i not in fixed],
        "new_items": [{"id": i, "status": s} for i, s in now.items() if i not in before],
        "not_in_this_run": sorted(set(before) - set(now)),
    }
    # Decisions the previous review recorded on items still open to the tools now: offer
    # them again (decisions=), they are not applied by themselves.
    decided_now = {row["id"] for row in model["items"] if row.get("tool_status")}
    earlier = [{"id": i, "status": before[i]} for i, value in previous["items"].items()
               if (value or {}).get("tool_status") and i in now and i not in decided_now
               and now[i] == "needs_judgment" and before[i] in STATUSES]
    if earlier:
        result["previous_decisions"] = earlier
    if result["previous"]["checklist"] and result["previous"]["checklist"] != \
            model["checklist"]["id"]:
        result["note"] = ("The previous record is for another checklist: only items with "
                          "the same id are compared.")
    return result


def render_comparison(comparison: dict) -> list[str]:
    previous = comparison["previous"]
    lines = ["## Changes since the previous record", "",
             f"- Previous record: {previous.get('file')} ({previous.get('generated_utc')}), "
             f"design state {previous.get('state')}"]
    if comparison["state_changed"]:
        changed = comparison["files_changed"]
        lines.append(f"- Design state changed: {len(changed)} file(s) differ"
                     + (": " + ", ".join(_md(f) for f in changed[:10]) if changed else "")
                     + (f" and {len(changed) - 10} more" if len(changed) > 10 else ""))
    else:
        lines.append("- Design state unchanged")
    for key, title in (("newly_failing", "Newly failing"), ("fixed", "Fixed"),
                       ("still_open", "Still open")):
        ids = comparison[key]
        lines.append(f"- {title}: {', '.join(ids) if ids else 'none'}")
    if comparison["changed"]:
        lines.append("- Other changes: " + ", ".join(
            f"{c['id']} {STATUS_LABELS.get(c['from'], c['from'])} -> "
            f"{STATUS_LABELS.get(c['to'], c['to'])}" for c in comparison["changed"]))
    if comparison["new_items"]:
        lines.append("- New in this run: " + ", ".join(
            f"{c['id']} ({STATUS_LABELS[c['status']]})" for c in comparison["new_items"]))
    if comparison["not_in_this_run"]:
        lines.append("- In the previous record, not in this run: "
                     + ", ".join(comparison["not_in_this_run"]))
    if comparison.get("previous_decisions"):
        lines.append("- Decided in the previous review, open again (confirm before reusing): "
                     + ", ".join(f"{d['id']} ({STATUS_LABELS[d['status']]})"
                                 for d in comparison["previous_decisions"]))
    if comparison.get("note"):
        lines.append(f"- {comparison['note']}")
    return lines + [""]


# ---------- JSON pages ----------

def page_review(model: dict, offset: int, limit: int, *, files_listed: int = FILES_LISTED,
                comparison: dict | None = None, full: bool = True) -> dict:
    """One JSON page: the header, design state (files cut to files_listed), coverage and
    comparison on every page, items offset..offset+limit."""
    offset = max(0, int(offset or 0))
    rows = model["items"]
    state = dict(model["state"])
    files = state.get("files") or []
    if len(files) > files_listed:
        state["files"] = files[:files_listed]
        state["files_total"] = len(files)
    page = rows[offset:offset + limit]
    result: dict = {
        "project": model.get("project"),
        "checklist": model["checklist"],
        "variant": model.get("variant"),
        "load": model.get("load"),
        "design_state": state,
        "coverage": model["coverage"],
    }
    if model.get("overrides_applied"):
        result["overrides_applied"] = model["overrides_applied"]
    if model.get("disabled"):
        result["disabled"] = model["disabled"]
    if comparison:
        result["comparison"] = comparison
    if model.get("decisions_applied"):
        result["decisions_applied"] = model["decisions_applied"]
    if full:
        open_judgment = [row["id"] for row in rows if row["status"] == "needs_judgment"]
        if open_judgment:
            result["needs_judgment"] = open_judgment
        if model.get("could_not_run"):
            result["could_not_run"] = model["could_not_run"]
        result["tool_runs"] = model.get("tool_runs") or []
        result["elapsed_ms"] = model.get("elapsed_ms")
    result.update({"items_total": len(rows), "offset": offset,
                   "has_more": offset + limit < len(rows)})
    if result["has_more"]:
        result["next_offset"] = offset + limit
    result["items"] = page
    return result
