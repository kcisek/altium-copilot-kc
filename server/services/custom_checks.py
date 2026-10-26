"""Team-defined deterministic check packs (run_custom_checks, roadmap #49).

A check pack is a JSON file a team keeps in its repository: check_packs.json, or *.json
files in a check_packs folder, next to the .PrjPcb or in the root of the Git repository
above it (the repository's first, then the project folder's; a pack whose id was already
loaded replaces it), plus a pack file named in the call. A pack has an id, a title, a
version, optional rails (net voltages its rules assume, {"VIN": 24}) and rules. Each file
is validated like a checklist override or a fab profile: a problem is reported with the
file, the place in it and what to fix, and nothing runs until it is fixed.

Two kinds of rule:

- select-and-assert: select parts, nets or pins of the design (design_api: designator,
  prefix, kind, fitted, parameters, text, side, case size, net classes, voltages; one
  net per line) and assert on each one (has_parameter, has, parameter_matches, matches,
  count of parts of a kind on the net or its line, connected within N series parts,
  nearest part of a kind on the PCB, rating_ratio, side, case_size, none). Each object
  that fails an assertion is a finding; one whose assertion cannot be evaluated (no PCB,
  no voltage, no rating read) is counted as not evaluated, or is a finding with
  on_unknown "fail". Selection is three-valued: an object only a rail of unknown voltage
  or a PCB side without a PcbDoc could select is counted as not evaluated, in nested
  filters too, never taken as selected or as left out.
- tool rules: run a read-only tool in-process with arguments (review_checklist's runner:
  the same function, arguments, defaults and active variant as a direct call) and test its
  result with review_checklist's condition lists: fail (each matching row is a finding),
  pass (all must hold), na (the rule does not apply), na_errors (tool errors that mean it
  does not apply). A count missing from a summary of counts is 0; another path missing
  leaves the rule could_not_evaluate unless findings were read.

Findings are ERC-style, as check_bom's: the rule id (check), the pack, the rule's
severity (error, warning or info), designators, nets, pins, location and the message
(issue), from the rule's template ({designator}, {net}, {value} ...). Every rule has a
row on every page (shortened when the page must shrink, never dropped).

Python plugins (check_packs/plugins/*.py in the project folder only; a folder linked to
another place gives none) run only when the call asks for them and the server's
environment has ALTIUM_COPILOT_ALLOW_PLUGINS=1: they are repository code run with the
server's permissions, with no time limit, and output written past sys.stdout (os.write,
sys.__stdout__) is not captured. Each defines check(api) returning findings; an
exception or sys.exit in one is reported in its rule row, never raised.

Pure logic, no MCP: main.py passes the design API, the tools and the PCB in.
"""

import contextlib
import inspect
import io
import json
import math
import os
import re
import string
import sys
import time
import traceback
import types
from collections import Counter
from pathlib import Path
from typing import Any, Callable

try:
    from services.bom import natural_key
    from services.design_api import (
        CASE_ORDER, KIND_ALIASES, KINDS, NET_CLASS_ALIASES, NET_CLASSES, PIN_ROLES,
        PIN_TYPES, THROUGH, DesignAPI,
    )
    from services.review_checklist import (
        EVIDENCE_TOOLS, RESERVED_ARGS, ChecklistError, ToolRunner, anchors_of, clip,
        evaluate, list_keys, read_json, repository_root, row_text, validate_condition,
    )
except ImportError:
    from server.services.bom import natural_key
    from server.services.design_api import (
        CASE_ORDER, KIND_ALIASES, KINDS, NET_CLASS_ALIASES, NET_CLASSES, PIN_ROLES,
        PIN_TYPES, THROUGH, DesignAPI,
    )
    from server.services.review_checklist import (
        EVIDENCE_TOOLS, RESERVED_ARGS, ChecklistError, ToolRunner, anchors_of, clip,
        evaluate, list_keys, read_json, repository_root, row_text, validate_condition,
    )

PACK_FILE = "check_packs.json"
PACK_FOLDER = "check_packs"
PLUGIN_FOLDER = "plugins"
PLUGIN_ENV = "ALTIUM_COPILOT_ALLOW_PLUGINS"
PLUGIN_PACK = "plugins"
MAX_PACK_FILES = 20
MAX_RULES = 300
MAX_ASSERTIONS = 20
MAX_PLUGINS = 20
MAX_PLUGIN_BYTES = 256 * 1024
MAX_FINDINGS_PER_RULE = 2000
MAX_HOPS = 5
MAX_DEPTH = 4  # nested filters (not, net, part, has_part, other_end)
MAX_LIST = 200  # names in one filter list
SEVERITIES = ("error", "warning", "info")
_SEVERITY_RANK = {level: rank for rank, level in enumerate(SEVERITIES)}
STATUSES = ("pass", "fail", "not_applicable", "could_not_evaluate")
OBJECTS = ("parts", "nets", "pins")
ON_UNKNOWN = ("skip", "fail")
SIDES = ("top", "bottom")
VOLTAGE_KEYS = ("net_voltage", "voltage")
VOLTAGE_UNKNOWN = "net voltage unknown"  # a rail neither its name nor the pack's rails give

TEXT_LIMITS = {"title": 120, "description": 600, "version": 20, "source": 300,
               "comment": 600, "message": 300, "label": 120}
_PACK_ID = re.compile(r"[a-z][a-z0-9_-]{0,39}")
_RULE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,39}")
_PACK_KEYS = ("id", "title", "version", "description", "source", "comment", "rails", "rules")
_RULE_KEYS = ("id", "title", "description", "severity", "message", "on_unknown", "disabled",
              "select", "assert", "tool", "args", "fail", "pass", "na", "na_errors")
_SELECT_RULE_ONLY = ("select", "assert")
_TOOL_RULE_ONLY = ("tool", "args", "fail", "pass", "na", "na_errors")
DEFAULT_NA_ERRORS = ("no_pcb_document",)

# The tools a tool rule may run: the checklist's evidence tools (read-only and
# deterministic); never run_custom_checks or run_review_checklist themselves.
TOOL_RULE_TOOLS = tuple(tool for tool in EVIDENCE_TOOLS
                        if tool not in ("run_custom_checks", "run_review_checklist"))

PART_TEXT_FIELDS = ("designator", "value", "comment", "description", "footprint", "mpn",
                    "manufacturer", "lib_reference", "sheet")
PART_FILTER_KEYS = (*PART_TEXT_FIELDS, "prefix", "kind", "fitted", "in_bom", "needs_mpn",
                    "text", "parameter", "side", "case_size", "net", "net_voltage",
                    "other_end", "not")
NET_FILTER_KEYS = ("name", "class", "voltage", "has_part", "pin_count", "one_per_line", "not")
PIN_FILTER_KEYS = ("name", "number", "type", "role", "part", "net", "connected", "not")
HAS_FIELDS = ("value", "comment", "description", "footprint", "mpn", "manufacturer",
              "lib_reference", "sheet", "case_size")
MATCH_FIELDS = {
    "parts": (*PART_TEXT_FIELDS, "kind", "case_size", "prefix"),
    "nets": ("name",),
    "pins": ("name", "number", "type", "role", "net", "designator"),
}
# Assertion -> the objects it applies to.
ASSERTIONS = {
    "has_parameter": ("parts", "pins"),
    "has": ("parts", "pins"),
    "parameter_matches": ("parts", "pins"),
    "matches": ("parts", "nets", "pins"),
    "count": ("nets", "pins"),
    "connected": ("parts", "nets", "pins"),
    "nearest": ("parts", "pins"),
    "rating_ratio": ("parts", "pins"),
    "side": ("parts", "pins"),
    "case_size": ("parts", "pins"),
    "none": ("parts", "nets", "pins"),
}
TEMPLATE_FIELDS = frozenset({
    "designator", "designators", "net", "nets", "pin", "pin_name", "pin_type", "value",
    "comment", "description", "footprint", "mpn", "manufacturer", "kind", "side",
    "case_size", "sheet", "voltage", "rated_voltage", "ratio", "distance_mm", "nearest",
    "count", "missing", "failed", "rule", "title", "pack", "severity", "label", "issue",
    "tool", "connectors",
})
_FORMAT_SPEC = re.compile(r"[<>^]?\d{0,3}(?:\.\d{1,2})?[fgde%s]?")
PLUGIN_HINT = (
    f"Plugins run repository code with the server's permissions: they run only when the "
    f"call passes allow_plugins=true and the server was started with {PLUGIN_ENV}=1 in its "
    "environment.")
PACK_HINT = ("Show the user what to fix in the pack file (the place is in 'where'); nothing "
             "ran.")


class PackError(ValueError):
    """A pack file or an argument that cannot be used; .error is the structured tool
    error."""

    def __init__(self, error: dict):
        super().__init__(error["message"])
        self.error = error


def _invalid(origin: str, where: str, message: str, **extra) -> PackError:
    return PackError({"error": "invalid_check_pack", "file": origin, "where": where,
                      "message": f"{origin}: {where + ': ' if where else ''}{message}",
                      "hint": PACK_HINT, **extra})


def _from_checklist(error: ChecklistError) -> PackError:
    """A review_checklist validation error (JSON reading, a condition) as a pack error."""
    data = dict(error.error)
    data["error"] = "invalid_check_pack"
    data["hint"] = PACK_HINT
    return PackError(data)


# ---------- reading and validating ----------

def _keys(data: dict, allowed: tuple, origin: str, where: str) -> None:
    for key in data:
        if not isinstance(key, str) or (key not in allowed and not key.startswith("_")):
            raise _invalid(origin, where, f"unknown key '{clip(key, 60)}'.",
                           valid_keys=list(allowed))


def _text(data: dict, key: str, origin: str, where: str, required: bool = False):
    value = data.get(key)
    place = f"{where}.{key}" if where else key
    if value is None:
        if required:
            raise _invalid(origin, where, f"'{key}' is required.")
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool) and key == "version":
        value = str(value)
    if not isinstance(value, str) or not value.strip():
        raise _invalid(origin, place, "must be a non-empty string.")
    if len(value) > TEXT_LIMITS[key]:
        raise _invalid(origin, place, f"longer than {TEXT_LIMITS[key]} characters.")
    return " ".join(value.split())


def _regex(value, origin: str, where: str) -> re.Pattern:
    if not isinstance(value, str) or not value or len(value) > 200:
        raise _invalid(origin, where, "must be a regular expression of 1 to 200 characters.")
    try:
        return re.compile(value, re.IGNORECASE)
    except re.error as error:
        raise _invalid(origin, where, f"not a valid regular expression ({error}).") from None


def _names(value, origin: str, where: str, valid: tuple | None = None,
           aliases: dict | None = None, fold=str.lower) -> frozenset:
    """A name or a list of names, checked against valid (aliases mapped first)."""
    items = [value] if isinstance(value, str) else value
    if not isinstance(items, list) or not items or len(items) > MAX_LIST or not all(
            isinstance(item, str) and item.strip() for item in items):
        raise _invalid(origin, where, "must be a name or a list of names.")
    out = set()
    for item in items:
        name = fold(item.strip())
        name = (aliases or {}).get(name, name)
        if valid is not None:
            lookup = {fold(v): v for v in valid}
            if name not in lookup:
                raise _invalid(origin, where, f"'{clip(item, 40)}' is not one of "
                                              f"{', '.join(valid)}.")
            name = lookup[name]
        out.add(name)
    return frozenset(out)


def _number(value, origin: str, where: str, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(
            value) or (positive and value <= 0):
        raise _invalid(origin, where, "must be a positive number." if positive
                       else "must be a number.")
    return float(value)


def _range(value, origin: str, where: str) -> dict:
    """{"min", "max" (inclusive), "above", "below" (exclusive)}: numbers."""
    if not isinstance(value, dict) or not value:
        raise _invalid(origin, where, "must be an object of min, max, above and below.")
    _keys(value, ("min", "max", "above", "below"), origin, where)
    return {key: _number(number, origin, f"{where}.{key}") for key, number in value.items()
            if not key.startswith("_")}


def _in_range(number: float | None, spec: dict) -> bool:
    if number is None:
        return False
    return (number >= spec.get("min", -math.inf) and number <= spec.get("max", math.inf)
            and number > spec.get("above", -math.inf) and number < spec.get("below", math.inf))


def _case_spec(value, origin: str, where: str) -> dict:
    """A case size, a list of them, or {"in", "not_in", "min", "max"}."""
    def code(item, place):
        text = str(item).strip() if isinstance(item, (str, int)) and not isinstance(
            item, bool) else None
        if text and text.isdigit() and len(text) == 3:
            text = "0" + text  # 402 written as a number
        if text not in CASE_ORDER:
            raise _invalid(origin, place, f"'{clip(item, 20)}' is not an imperial case code "
                                          f"({', '.join(CASE_ORDER)}).")
        return text

    if isinstance(value, (str, int)) and not isinstance(value, bool):
        return {"in": frozenset({code(value, where)})}
    if isinstance(value, list):
        if not value:
            raise _invalid(origin, where, "must name at least one case size.")
        return {"in": frozenset(code(item, where) for item in value)}
    if not isinstance(value, dict) or not value:
        raise _invalid(origin, where, "must be a case code, a list of them or an object of "
                                      "in, not_in, min and max.")
    _keys(value, ("in", "not_in", "min", "max"), origin, where)
    out: dict = {}
    for key in ("in", "not_in"):
        if key in value:
            items = value[key] if isinstance(value[key], list) else [value[key]]
            out[key] = frozenset(code(item, f"{where}.{key}") for item in items)
    for key in ("min", "max"):
        if key in value:
            out[key] = code(value[key], f"{where}.{key}")
    return out


def _case_ok(case: str, spec: dict) -> bool | None:
    """Whether a case code meets the spec; None when min or max cannot order it."""
    if "in" in spec and case not in spec["in"]:
        return False
    if "not_in" in spec and case in spec["not_in"]:
        return False
    if "min" in spec or "max" in spec:
        if case not in CASE_ORDER:
            return None
        rank = CASE_ORDER.index(case)
        if "min" in spec and rank < CASE_ORDER.index(spec["min"]):
            return False
        if "max" in spec and rank > CASE_ORDER.index(spec["max"]):
            return False
    return True


def _filter(kind: str, value, origin: str, where: str, top: bool = True, depth: int = 0,
            context: bool = False) -> dict:
    """A validated filter of parts, nets or pins. top filters default to fitted parts
    (fitted: null takes every part); context allows other_end (inside an assertion)."""
    if depth > MAX_DEPTH:
        raise _invalid(origin, where, "filters are nested too deeply.")
    if not isinstance(value, dict):
        raise _invalid(origin, where, "a filter is an object ({} selects every one).")
    allowed = {"parts": PART_FILTER_KEYS, "nets": NET_FILTER_KEYS, "pins": PIN_FILTER_KEYS}[kind]
    _keys(value, allowed, origin, where)
    out: dict = {}
    for key, spec in value.items():
        if key.startswith("_"):
            continue
        place = f"{where}.{key}"
        if key == "not":
            out["not"] = _filter(kind, spec, origin, place, top=False, depth=depth + 1,
                                 context=context)
        elif kind == "parts":
            out[key] = _part_key(key, spec, origin, place, depth, context)
        elif kind == "nets":
            out[key] = _net_key(key, spec, origin, place, depth, top)
        else:
            out[key] = _pin_key(key, spec, origin, place, depth)
    if top and kind == "parts" and "fitted" not in out:
        out["fitted"] = True
    if top and kind == "pins":
        out.setdefault("connected", True)
        part = out.get("part") or {"fitted": True}
        out["part"] = part
    for key in VOLTAGE_KEYS:  # tested last: cheaper keys reject most objects first
        if key in out:
            out[key] = out.pop(key)
    return out


def _part_key(key: str, spec, origin: str, where: str, depth: int, context: bool):
    if key in PART_TEXT_FIELDS or key == "text":
        return _regex(spec, origin, where)
    if key == "prefix":
        return _names(spec, origin, where, fold=str.upper)
    if key == "kind":
        return _names(spec, origin, where, KINDS, KIND_ALIASES)
    if key in ("fitted", "in_bom", "needs_mpn"):
        if spec is not None and not isinstance(spec, bool):
            raise _invalid(origin, where, "must be true, false or null (either).")
        return spec
    if key == "parameter":
        if not isinstance(spec, dict) or not spec:
            raise _invalid(origin, where, "must be an object of parameter name: regular "
                                          "expression, true (present) or false (missing).")
        out = []
        for name, test in spec.items():
            if isinstance(test, bool):
                out.append((name, test))
            else:
                out.append((name, _regex(test, origin, f"{where}.{name}")))
        return tuple(out)
    if key == "side":
        if spec not in SIDES:
            raise _invalid(origin, where, "must be 'top' or 'bottom'.")
        return spec
    if key == "case_size":
        return _case_spec(spec, origin, where)
    if key == "net":
        return _filter("nets", spec, origin, where, top=False, depth=depth + 1)
    if key == "net_voltage":
        return _range(spec, origin, where)
    if key == "other_end":
        if not context:
            raise _invalid(origin, where, "'other_end' tests the far side of a part found on "
                                          "a net: it goes in an assertion's parts filter.")
        if isinstance(spec, (str, list)):
            spec = {"class": spec}
        return _filter("nets", spec, origin, where, top=False, depth=depth + 1)
    raise _invalid(origin, where, f"unknown key '{key}'.")  # pragma: no cover


def _net_key(key: str, spec, origin: str, where: str, depth: int, top: bool = False):
    if key == "one_per_line":
        if not top:
            raise _invalid(origin, where, "'one_per_line' goes in the rule's own nets "
                                          "filter (select.nets), where it keeps one net of "
                                          "each line.")
        if not isinstance(spec, bool):
            raise _invalid(origin, where, "must be true or false.")
        return spec
    if key == "name":
        return _regex(spec, origin, where)
    if key == "class":
        return _names(spec, origin, where, NET_CLASSES, NET_CLASS_ALIASES)
    if key == "voltage":
        return _range(spec, origin, where)
    if key == "pin_count":
        return _range(spec, origin, where)
    if key == "has_part":
        return _filter("parts", spec, origin, where, top=True, depth=depth + 1)
    raise _invalid(origin, where, f"unknown key '{key}'.")  # pragma: no cover


def _pin_key(key: str, spec, origin: str, where: str, depth: int):
    if key in ("name", "number"):
        return _regex(spec, origin, where)
    if key == "type":
        return _names(spec, origin, where, PIN_TYPES)
    if key == "role":
        return _names(spec, origin, where, PIN_ROLES)
    if key == "part":
        return _filter("parts", spec, origin, where, top=True, depth=depth + 1)
    if key == "net":
        return _filter("nets", spec, origin, where, top=False, depth=depth + 1)
    if key == "connected":
        if spec is not None and not isinstance(spec, bool):
            raise _invalid(origin, where, "must be true, false or null (either).")
        return spec
    raise _invalid(origin, where, f"unknown key '{key}'.")  # pragma: no cover


def _flag(spec: dict, key: str, origin: str, where: str) -> bool:
    value = spec.get(key, False)
    if not isinstance(value, bool):
        raise _invalid(origin, f"{where}.{key}", "must be true or false.")
    return value


def _hops(value, origin: str, where: str, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_HOPS:
        raise _invalid(origin, where, f"must be a whole number from 0 to {MAX_HOPS}.")
    return value


def _through(value, origin: str, where: str) -> tuple:
    if value is None:
        return tuple(THROUGH)
    return tuple(sorted(_names(value, origin, where, tuple(THROUGH))))


def _assertion(value, objects: str, origin: str, where: str) -> dict:
    if not isinstance(value, dict):
        raise _invalid(origin, where, "an assertion is an object such as "
                                      "{\"has_parameter\": \"Manufacturer\"}.")
    keys = [key for key in value if not str(key).startswith("_")]
    if len(keys) != 1 or keys[0] not in ASSERTIONS:
        raise _invalid(origin, where, "an assertion has exactly one of "
                                      f"{', '.join(ASSERTIONS)}.", valid_assertions=list(ASSERTIONS))
    name = keys[0]
    spec = value[name]
    place = f"{where}.{name}"
    if objects not in ASSERTIONS[name]:
        raise _invalid(origin, place, f"'{name}' applies to {' and '.join(ASSERTIONS[name])}, "
                                      f"not to {objects}.")
    out: dict = {"name": name}
    if name == "has_parameter":
        if isinstance(spec, dict):
            _keys(spec, ("any", "all"), origin, place)
            if len([k for k in spec if not k.startswith("_")]) != 1:
                raise _invalid(origin, place, "give 'any' or 'all' with a list of names.")
            mode = "any" if "any" in spec else "all"
            names = spec[mode]
        else:
            mode, names = "all", spec
        out["mode"] = mode
        out["names"] = tuple(sorted(_names(names, origin, place, fold=lambda text: text)))
    elif name == "has":
        out["fields"] = tuple(sorted(_names(spec, origin, place, HAS_FIELDS)))
    elif name in ("parameter_matches", "matches"):
        if not isinstance(spec, dict) or not spec:
            raise _invalid(origin, place, "must be an object of name: regular expression.")
        if name == "matches":
            for field_name in spec:
                if field_name not in MATCH_FIELDS[objects]:
                    raise _invalid(origin, place, f"'{clip(field_name, 30)}' is not a field of "
                                                  f"{objects}; use "
                                                  f"{', '.join(MATCH_FIELDS[objects])}.")
        out["tests"] = tuple((key, _regex(test, origin, f"{place}.{key}"))
                             for key, test in spec.items())
    elif name in ("count", "connected", "nearest"):
        allowed = {"count": ("parts", "min", "max", "within_hops", "through", "on_line",
                             "label"),
                   "connected": ("parts", "within_hops", "through", "on_line", "label"),
                   "nearest": ("parts", "max_mm", "same_net", "label")}[name]
        if not isinstance(spec, dict):
            raise _invalid(origin, place, f"must be an object of {', '.join(allowed)}.")
        _keys(spec, allowed, origin, place)
        out["parts"] = _filter("parts", spec.get("parts", {}), origin, f"{place}.parts",
                               context=True)
        out["label"] = _text(spec, "label", origin, place) or describe_filter(out["parts"])
        if name == "count":
            if "min" not in spec and "max" not in spec:
                raise _invalid(origin, place, "give 'min', 'max' or both.")
            for key in ("min", "max"):
                if key in spec:
                    number = spec[key]
                    if isinstance(number, bool) or not isinstance(number, int) or number < 0:
                        raise _invalid(origin, f"{place}.{key}", "must be a whole number of 0 "
                                                                 "or more.")
                    out[key] = number
            if out.get("min", 0) > out.get("max", math.inf):
                raise _invalid(origin, place, "'min' is larger than 'max'.")
            out["hops"] = _hops(spec.get("within_hops"), origin, f"{place}.within_hops", 0)
            out["through"] = _through(spec.get("through"), origin, f"{place}.through")
            out["on_line"] = _flag(spec, "on_line", origin, place)
        elif name == "connected":
            out["hops"] = _hops(spec.get("within_hops"), origin, f"{place}.within_hops", 1)
            out["through"] = _through(spec.get("through"), origin, f"{place}.through")
            out["on_line"] = _flag(spec, "on_line", origin, place)
        else:
            if "max_mm" not in spec:
                raise _invalid(origin, place, "'max_mm' is required (the largest distance, "
                                              "pad edge to pad edge).")
            out["max_mm"] = _number(spec["max_mm"], origin, f"{place}.max_mm", positive=True)
            same = spec.get("same_net", True)
            if not isinstance(same, bool):
                raise _invalid(origin, f"{place}.same_net", "must be true or false.")
            out["same_net"] = same
    elif name == "rating_ratio":
        if not isinstance(spec, dict) or "min" not in spec:
            raise _invalid(origin, place, "must be {\"min\": ratio}, e.g. 2 for a part rated "
                                          "twice the voltage across it.")
        _keys(spec, ("min",), origin, place)
        out["min"] = _number(spec["min"], origin, f"{place}.min", positive=True)
    elif name == "side":
        if spec not in SIDES:
            raise _invalid(origin, place, "must be 'top' or 'bottom'.")
        out["side"] = spec
    elif name == "case_size":
        out["spec"] = _case_spec(spec, origin, place)
    elif name == "none":
        if spec is not True:
            raise _invalid(origin, place, "must be true (every object selected is a finding).")
    return out


def describe_filter(flt: dict) -> str:
    """A short label of a parts filter for messages ('tvs', 'resistor to a rail')."""
    parts = []
    if flt.get("kind"):
        parts.append("/".join(sorted(flt["kind"])))
    if flt.get("prefix"):
        parts.append("prefix " + "/".join(sorted(flt["prefix"])))
    for key in PART_TEXT_FIELDS:
        if key in flt:
            parts.append(f"{key} /{flt[key].pattern}/")
    if "other_end" in flt and flt["other_end"].get("class"):
        classes = "/".join(sorted(flt["other_end"]["class"]))
        parts.append("to ground" if classes == "ground" else f"to a {classes}")
    if "case_size" in flt and flt["case_size"].get("in"):
        parts.append("/".join(sorted(flt["case_size"]["in"])))
    return " ".join(parts) or "part"


def _template(value, origin: str, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _invalid(origin, where, "must be a non-empty string.")
    if len(value) > TEXT_LIMITS["message"]:
        raise _invalid(origin, where, f"longer than {TEXT_LIMITS['message']} characters.")
    text = " ".join(value.split())
    try:
        parsed = list(string.Formatter().parse(text))
    except ValueError as error:
        raise _invalid(origin, where, f"not a valid message template ({error}): write "
                                      "{name} for a value and {{ }} for a brace.") from None
    for _, name, spec, conversion in parsed:
        if name is None:
            continue
        if name not in TEMPLATE_FIELDS:
            raise _invalid(origin, where, f"'{{{clip(name, 30)}}}' is not a message field; "
                                          f"use {', '.join(sorted(TEMPLATE_FIELDS))}.")
        if conversion or (spec and not _FORMAT_SPEC.fullmatch(spec)):
            raise _invalid(origin, where, f"'{{{name}}}' takes no conversion and only a "
                                          "simple format such as {ratio:.2f}.")
    return text


def validate_rule(rule, origin: str, where: str,
                  tools: dict[str, Callable[..., Any]] | None = None) -> dict:
    if not isinstance(rule, dict):
        raise _invalid(origin, where, "a rule must be an object.")
    _keys(rule, _RULE_KEYS, origin, where)
    rule_id = rule.get("id")
    if not isinstance(rule_id, str) or not _RULE_ID.fullmatch(rule_id):
        shown = clip(rule_id, 40) if isinstance(rule_id, str) else type(rule_id).__name__
        raise _invalid(origin, f"{where}.id", f"rule id '{shown}' is not valid: letters, "
                                              "digits, '_', '.' and '-', at most 40 characters.")
    where = f"{where} ({rule_id})"
    out: dict = {"id": rule_id, "title": _text(rule, "title", origin, where) or rule_id}
    description = _text(rule, "description", origin, where)
    if description:
        out["description"] = description
    severity = rule.get("severity", "warning")
    if severity not in SEVERITIES:
        raise _invalid(origin, f"{where}.severity", f"'{clip(severity, 20)}' is not a "
                                                    f"severity; use {', '.join(SEVERITIES)}.")
    out["severity"] = severity
    on_unknown = rule.get("on_unknown", "skip")
    if on_unknown not in ON_UNKNOWN:
        raise _invalid(origin, f"{where}.on_unknown", "must be 'skip' (count it as not "
                                                      "evaluated) or 'fail' (a finding).")
    out["on_unknown"] = on_unknown
    disabled = rule.get("disabled", False)
    if not isinstance(disabled, bool):
        raise _invalid(origin, f"{where}.disabled", "must be true or false.")
    out["disabled"] = disabled
    if "message" in rule:
        out["message"] = _template(rule["message"], origin, f"{where}.message")
    select_keys = [key for key in _SELECT_RULE_ONLY if key in rule]
    tool_keys = [key for key in _TOOL_RULE_ONLY if key in rule]
    if select_keys and tool_keys:
        raise _invalid(origin, where, f"a rule is either select-and-assert ({', '.join(_SELECT_RULE_ONLY)}) "
                                      f"or a tool rule ({', '.join(_TOOL_RULE_ONLY)}), not both.")
    if "tool" in rule:
        out.update(_tool_rule(rule, origin, where, tools))
    elif "select" in rule:
        out.update(_select_rule(rule, origin, where))
    else:
        raise _invalid(origin, where, "a rule needs 'select' and 'assert', or 'tool' and "
                                      "'fail' or 'pass'.")
    return out


def _select_rule(rule: dict, origin: str, where: str) -> dict:
    select = rule["select"]
    if not isinstance(select, dict):
        raise _invalid(origin, f"{where}.select", "must be {\"parts\": {...}}, {\"nets\": "
                                                  "{...}} or {\"pins\": {...}}.")
    keys = [key for key in select if not str(key).startswith("_")]
    if len(keys) != 1 or keys[0] not in OBJECTS:
        raise _invalid(origin, f"{where}.select", "selects exactly one of parts, nets or "
                                                  "pins, e.g. {\"parts\": {\"kind\": "
                                                  "\"capacitor\"}}.")
    objects = keys[0]
    out = {"kind": "select", "objects": objects,
           "filter": _filter(objects, select[objects], origin, f"{where}.select.{objects}")}
    assertions = rule.get("assert")
    if not isinstance(assertions, list) or not assertions or len(assertions) > MAX_ASSERTIONS:
        raise _invalid(origin, f"{where}.assert", f"must be a list of 1 to {MAX_ASSERTIONS} "
                                                  "assertions.")
    out["assert"] = [_assertion(item, objects, origin, f"{where}.assert[{i}]")
                     for i, item in enumerate(assertions)]
    return out


def _tool_rule(rule: dict, origin: str, where: str,
               tools: dict[str, Callable[..., Any]] | None) -> dict:
    tool = rule["tool"]
    if tool not in TOOL_RULE_TOOLS:
        raise _invalid(origin, f"{where}.tool", f"'{clip(tool, 60)}' is not a tool a rule can "
                                                "run.", valid_tools=list(TOOL_RULE_TOOLS))
    args = rule.get("args", {})
    if not isinstance(args, dict):
        raise _invalid(origin, f"{where}.args", "must be an object of argument: value.")
    for name in args:
        if name in RESERVED_ARGS:
            raise _invalid(origin, f"{where}.args", f"'{name}' is set by the runner (every "
                                                    "page is read, as JSON).")
    function = (tools or {}).get(tool)
    if function is not None:
        try:
            signature = inspect.signature(function)
            signature.bind(**args)
        except TypeError as error:
            raise _invalid(origin, f"{where}.args", f"{tool} does not take these arguments "
                                                    f"({error}).") from None
        except ValueError:
            pass
    out: dict = {"kind": "tool", "tool": tool, "args": json.loads(json.dumps(args))}
    if "fail" not in rule and "pass" not in rule:
        raise _invalid(origin, where, "a tool rule needs 'fail' conditions (any one hit is a "
                                      "finding) or 'pass' conditions (all must hold).")
    try:
        for group in ("fail", "pass", "na"):
            if group not in rule:
                continue
            conditions = rule[group]
            if not isinstance(conditions, list) or not conditions or len(conditions) > 20:
                raise _invalid(origin, f"{where}.{group}", "must be a list of 1 to 20 "
                                                           "conditions.")
            out[group] = [validate_condition(cond, origin, f"{where}.{group}[{i}]")
                          for i, cond in enumerate(conditions)]
    except ChecklistError as error:
        raise _from_checklist(error) from None
    na_errors = rule.get("na_errors", list(DEFAULT_NA_ERRORS))
    if not isinstance(na_errors, list) or not all(isinstance(e, str) for e in na_errors):
        raise _invalid(origin, f"{where}.na_errors", "must be a list of tool error codes.")
    out["na_errors"] = tuple(na_errors)
    return out


def validate_pack(data, origin: str, tools: dict[str, Callable[..., Any]] | None = None) -> dict:
    """A whole pack: id, title, version, optional rails and its rules (each validated)."""
    if not isinstance(data, dict):
        raise _invalid(origin, "", "a pack is one JSON object.")
    _keys(data, _PACK_KEYS, origin, "")
    pack_id = data.get("id")
    if not isinstance(pack_id, str) or not _PACK_ID.fullmatch(pack_id):
        shown = clip(pack_id, 40) if isinstance(pack_id, str) else type(pack_id).__name__
        raise _invalid(origin, "id", f"pack id '{shown}' is not valid: lower-case letters, "
                                     "digits, '_' and '-', starting with a letter, at most 40 "
                                     "characters.")
    if pack_id == PLUGIN_PACK:
        raise _invalid(origin, "id", f"'{PLUGIN_PACK}' names the Python plugin files "
                                     "(pack='plugins'): give the pack another id.")
    pack: dict = {"id": pack_id, "title": _text(data, "title", origin, "", required=True),
                  "version": _text(data, "version", origin, "", required=True)}
    for key in ("description", "source"):
        value = _text(data, key, origin, "")
        if value:
            pack[key] = value
    rails = data.get("rails")
    if rails is not None:
        if not isinstance(rails, dict):
            raise _invalid(origin, "rails", "must be an object of net name: volts, e.g. "
                                            "{\"VIN\": 24}.")
        for name, volts in rails.items():
            if name.startswith("_"):
                continue
            number = _number(volts, origin, f"rails.{name}")
            if not -1000 <= number <= 1000:
                raise _invalid(origin, f"rails.{name}", "must be between -1000 and 1000 V.")
            pack.setdefault("rails", {})[str(name)] = number
    rules = data.get("rules")
    if not isinstance(rules, list) or not rules or len(rules) > MAX_RULES:
        raise _invalid(origin, "rules", f"must be a list of 1 to {MAX_RULES} rules.")
    pack["rules"] = []
    seen: set[str] = set()
    for i, rule in enumerate(rules):
        row = validate_rule(rule, origin, f"rules[{i}]", tools)
        if row["id"].lower() in seen:
            raise _invalid(origin, f"rules[{i}]", f"rule id '{row['id']}' is used twice (ids "
                                                  "ignore case).")
        seen.add(row["id"].lower())
        row["pack"] = pack_id
        pack["rules"].append(row)
    pack["file"] = origin
    return pack


# ---------- finding the packs ----------

def pack_files(project_dir) -> list[tuple[Path, Path, str]]:
    """The pack files to load, in order: the repository root's (when it is above the
    project folder), then the project folder's; in each check_packs.json, then
    check_packs/*.json by name. Each entry: (file, its folder, a label)."""
    project = Path(project_dir)
    folders: list[tuple[Path, str]] = []
    root = repository_root(project)
    try:
        if root is not None and root.resolve() != project.resolve():
            folders.append((root, "repository root"))
    except OSError:
        pass
    folders.append((project, "project folder"))
    files: list[tuple[Path, Path, str]] = []
    for folder, where in folders:
        try:
            single = folder / PACK_FILE
            if single.is_file():
                files.append((single, folder, f"{PACK_FILE} ({where})"))
            sub = folder / PACK_FOLDER
            if sub.is_dir():
                for path in sorted(sub.glob("*.json"), key=lambda p: p.name.lower()):
                    if path.is_file():
                        files.append((path, sub, f"{PACK_FOLDER}/{path.name} ({where})"))
        except OSError:
            continue
    return files


def plugin_files(project_dir) -> list[Path]:
    """check_packs/plugins/*.py in the project folder (never anywhere else), by name. A
    folder that resolves outside the project folder (check_packs or plugins a link or
    junction to another place) gives none, and a file that resolves outside the plugins
    folder (a link) is left out."""
    folder = Path(project_dir) / PACK_FOLDER / PLUGIN_FOLDER
    try:
        if not folder.is_dir():
            return []
        resolved_folder = folder.resolve()
        if not resolved_folder.is_relative_to(Path(project_dir).resolve()):
            return []
        out = []
        for path in sorted(folder.glob("*.py"), key=lambda p: p.name.lower()):
            if path.name.startswith("_") or not path.is_file():
                continue
            if path.resolve().parent != resolved_folder:
                continue
            out.append(path)
        return out[:MAX_PLUGINS]
    except OSError:
        return []


def plugin_id(path: Path) -> str:
    stem = re.sub(r"[^A-Za-z0-9_.-]", "_", path.stem)[:40] or "plugin"
    return stem if stem[0].isalnum() else f"p{stem}"[:40]


def pack_signature(project_dir, extra: str | None = None) -> tuple:
    """(file, size, save time) of every pack and plugin file: a change detector for
    callers that cache runs."""
    if project_dir is None:
        return ()
    paths = [path for path, _, _ in pack_files(project_dir)] + plugin_files(project_dir)
    if extra:
        path = Path(extra)
        paths.append(path if path.is_absolute() else Path(project_dir) / path)
    out = []
    for path in paths:
        try:
            stat = path.stat()
            out.append((str(path), stat.st_size, stat.st_mtime_ns))
        except OSError:
            out.append((str(path), None, None))
    return tuple(out)


def _read(path: Path, label: str, folder: Path | None) -> dict:
    try:
        return read_json(path, label, folder)
    except ChecklistError as error:
        raise _from_checklist(error) from None


def load_packs(project_dir, extra: str | None = None,
               tools: dict[str, Callable[..., Any]] | None = None) -> tuple[list[dict], dict]:
    """The packs for a project: discovered (pack_files), then the file extra names
    (absolute, or relative to the project folder). A pack whose id was loaded before
    replaces it. Returns (packs, {"files": [...], "replaced": [...]}); PackError on any
    problem."""
    sources = pack_files(project_dir) if project_dir is not None else []
    if extra:
        path = Path(extra)
        if not path.is_absolute() and project_dir is not None:
            path = Path(project_dir) / path
        try:
            found = path.is_file()
        except OSError:
            found = False
        if not found:
            raise PackError({"error": "pack_not_found",
                             "message": f"No pack file at {clip(extra, 200)}.",
                             "hint": "Give the path of a .json pack file, absolute or relative "
                                     "to the project folder."})
        if path.suffix.lower() != ".json":
            raise PackError({"error": "pack_not_found",
                             "message": f"{path.name} is not a .json pack file."})
        sources.append((path, None, f"{path.name} (path)"))
    if len(sources) > MAX_PACK_FILES:
        raise _invalid(sources[MAX_PACK_FILES][2], "", f"more than {MAX_PACK_FILES} pack "
                                                       "files.")
    seen_paths: set[str] = set()
    packs: dict[str, dict] = {}
    replaced: list[dict] = []
    files: list[str] = []
    for path, folder, label in sources:
        try:
            key = os.path.normcase(str(path.resolve()))
        except OSError:
            key = os.path.normcase(str(path))
        if key in seen_paths:
            continue
        seen_paths.add(key)
        data = _read(path, label, folder)
        pack = validate_pack(data, label, tools)
        if pack["id"] in packs:
            replaced.append({"pack": pack["id"], "file": packs[pack["id"]]["file"],
                             "by": label})
        packs[pack["id"]] = pack
        files.append(label)
    owner: dict[str, tuple[str, str]] = {}
    total = 0
    for pack in packs.values():
        for rule in pack["rules"]:
            total += 1
            key = rule["id"].lower()
            if key in owner:
                raise _invalid(pack["file"], "", (
                    f"rule id '{rule['id']}' is in both the '{owner[key][0]}' pack "
                    f"({owner[key][1]}) and the '{pack['id']}' pack: rule ids are unique "
                    "across packs (ignoring case)."))
            owner[key] = (pack["id"], pack["file"])
    if total > MAX_RULES:
        raise _invalid(files[-1], "", f"more than {MAX_RULES} rules across the packs.")
    return list(packs.values()), {"files": files, "replaced": replaced}


def choose(packs: list[dict], pack: str | None, rules: list[str] | None,
           plugin_ids: list[str] | None = None) -> tuple[list[dict], list[dict], bool]:
    """What to run: (the packs, their rules, whether plugins may run). pack names packs
    (comma-separated ids, 'plugins' for the plugin files; default every one); rules
    names rules and plugins (ids, any case; default every enabled rule; a disabled rule
    runs only when named). PackError unknown_pack / unknown_rule (its 'unknown' lists
    the ids not found)."""
    chosen = packs
    plugins_wanted = True
    if pack and pack.strip():
        wanted = [name.strip().lower() for name in pack.split(",") if name.strip()]
        known = {p["id"]: p for p in packs}
        plugins_wanted = PLUGIN_PACK in wanted and bool(plugin_ids)
        wanted = [name for name in wanted if not (name == PLUGIN_PACK and plugin_ids)]
        unknown = [name for name in wanted if name not in known]
        if unknown:
            raise PackError({"error": "unknown_pack",
                             "message": f"No pack {', '.join(repr(n) for n in unknown)}.",
                             "packs": sorted(known) + ([PLUGIN_PACK] if plugin_ids else [])})
        chosen = [known[name] for name in dict.fromkeys(wanted)]
    out = []
    if rules:
        wanted_ids = [name.strip() for name in rules if isinstance(name, str) and name.strip()]
        by_id = {rule["id"].lower(): rule for p in chosen for rule in p["rules"]}
        plugins = {name.lower() for name in plugin_ids or []} if plugins_wanted else set()
        unknown = [name for name in wanted_ids
                   if name.lower() not in by_id and name.lower() not in plugins]
        if unknown:
            raise PackError({"error": "unknown_rule",
                             "message": f"No rule {', '.join(repr(n) for n in unknown)} in the "
                                        "packs run.", "unknown": unknown,
                             "rules": [rule["id"] for p in chosen for rule in p["rules"]][:200]
                             + sorted(plugins)})
        keep = {name.lower() for name in wanted_ids}
        for p in chosen:
            out += [rule for rule in p["rules"] if rule["id"].lower() in keep]
        return chosen, out, plugins_wanted
    for p in chosen:
        out += [rule for rule in p["rules"] if not rule["disabled"]]
    return chosen, out, plugins_wanted


# ---------- selecting ----------

# Matching is three-valued: True, False, or None when the answer depends on something
# not known (a rail's voltage that neither its name, the power tree nor the pack's rails
# give; a PCB side without a PcbDoc). A None object is counted as not evaluated, never
# taken as matching or as not matching. notes, when given, collects why: "rails" (the
# rails of unknown voltage) and "no_pcb".
NO_PCB_SIDE = "no PCB: side unknown"


def _any3(results) -> bool | None:
    """True when one is True, else None when one is None, else False."""
    unknown = False
    for result in results:
        if result:
            return True
        if result is None:
            unknown = True
    return None if unknown else False


def _not3(result: bool | None) -> bool | None:
    return None if result is None else not result


def _text_match(pattern: re.Pattern, value) -> bool:
    return bool(pattern.search(str(value))) if value not in (None, "") else False


def _lower_bounds_only(spec: dict) -> bool:
    return not ({"max", "below"} & set(spec))


def _open_rails(api: DesignAPI, nets) -> list[str]:
    return [net for net in nets
            if api.voltage(net) is None and "rail" in api.net_classes(net)]


def _note_rails(notes: dict | None, rails: list[str]) -> None:
    if notes is not None:
        notes.setdefault("rails", set()).update(rails)


def part_voltage_match(api: DesignAPI, part, spec: dict, notes: dict | None = None
                       ) -> bool | None:
    """Whether the highest voltage on the part's nets is in the range; None when a rail
    of unknown voltage on it could change the answer (signal nets of unknown voltage do
    not count: a part on such nets only is in no range)."""
    known = [abs(v.volts) for net in part.nets if (v := api.voltage(net)) is not None]
    value = max(known) if known else None
    open_rails = _open_rails(api, part.nets)
    if not open_rails:
        return _in_range(value, spec)
    if value is not None and _lower_bounds_only(spec) and _in_range(value, spec):
        return True
    _note_rails(notes, open_rails)
    return None


def net_voltage_match(api: DesignAPI, net, spec: dict, notes: dict | None = None
                      ) -> bool | None:
    volt = api.voltage(net.name)
    if volt is not None:
        return _in_range(abs(volt.volts), spec)
    if "rail" in api.net_classes(net.name):
        _note_rails(notes, [net.name])
        return None
    return False


def match_part(api: DesignAPI, part, flt: dict, context_net: str | None = None,
               notes: dict | None = None) -> bool | None:
    """Whether the part meets the filter: True, False, or None when only an unknown
    voltage or PCB side leaves it open (see above)."""
    unknown = False
    for key, spec in flt.items():
        found = _part_key_match(api, part, key, spec, context_net, notes)
        if found is False:
            return False
        unknown = unknown or found is None
    return None if unknown else True


def _part_key_match(api: DesignAPI, part, key: str, spec, context_net: str | None,
                    notes: dict | None) -> bool | None:
    if key == "not":
        return _not3(match_part(api, part, spec, context_net, notes))
    if key in PART_TEXT_FIELDS:
        return _text_match(spec, getattr(part, key))
    if key == "text":
        text = " ".join(str(value) for value in (
            part.value, part.comment, part.description, part.footprint, part.mpn,
            part.manufacturer, part.lib_reference) if value)
        return bool(spec.search(text))
    if key == "prefix":
        return part.prefix in spec
    if key == "kind":
        return part.kind in spec
    if key in ("fitted", "in_bom", "needs_mpn"):
        return spec is None or bool(getattr(part, key)) == spec
    if key == "parameter":
        for name, test in spec:
            value = part.parameter(name)
            if test is True and value is None or test is False and value is not None:
                return False
            if isinstance(test, re.Pattern) and (value is None or not test.search(value)):
                return False
        return True
    if key == "side":
        if not api.has_pcb:  # without a PcbDoc the side is unknown, never assumed
            if notes is not None:
                notes["no_pcb"] = True
            return None
        placement = api.placement(part.designator)
        return placement is not None and placement.side == spec
    if key == "case_size":
        case = part.case_size
        return case is not None and bool(_case_ok(case, spec))
    if key == "net":
        return _any3(match_net(api, api.net(net), spec, notes) for net in part.nets
                     if api.net(net) is not None)
    if key == "net_voltage":
        return part_voltage_match(api, part, spec, notes)
    if key == "other_end":
        return _any3(match_net(api, api.net(net), spec, notes) for net in part.nets
                     if net != context_net and api.net(net) is not None)
    return True  # pragma: no cover


def match_net(api: DesignAPI, net, flt: dict, notes: dict | None = None) -> bool | None:
    unknown = False
    for key, spec in flt.items():
        found = _net_key_match(api, net, key, spec, notes)
        if found is False:
            return False
        unknown = unknown or found is None
    return None if unknown else True


def _net_key_match(api: DesignAPI, net, key: str, spec, notes: dict | None) -> bool | None:
    if key == "not":
        return _not3(match_net(api, net, spec, notes))
    if key == "name":
        return bool(spec.search(net.name))
    if key == "class":
        return bool(spec & api.net_classes(net.name))
    if key == "voltage":
        return net_voltage_match(api, net, spec, notes)
    if key == "pin_count":
        return _in_range(len(net.pins), spec)
    if key == "has_part":
        return _any3(match_part(api, part, spec, net.name, notes) for part in net.parts)
    return True  # one_per_line applies to the selection, not to one net


def match_pin(api: DesignAPI, pin, flt: dict, notes: dict | None = None) -> bool | None:
    unknown = False
    for key, spec in flt.items():
        found = _pin_key_match(api, pin, key, spec, notes)
        if found is False:
            return False
        unknown = unknown or found is None
    return None if unknown else True


def _pin_key_match(api: DesignAPI, pin, key: str, spec, notes: dict | None) -> bool | None:
    if key == "not":
        return _not3(match_pin(api, pin, spec, notes))
    if key in ("name", "number"):
        return _text_match(spec, getattr(pin, key))
    if key == "type":
        return pin.type in spec
    if key == "role":
        return pin.role in spec
    if key == "connected":
        return spec is None or (pin.net is not None) == spec
    if key == "part":
        part = api.part(pin.part)
        return False if part is None else match_part(api, part, spec, pin.net, notes)
    if key == "net":
        net = api.net(pin.net) if pin.net else None
        return False if net is None else match_net(api, net, spec, notes)
    return True  # pragma: no cover


def _line_order(name: str) -> tuple:
    """check_pull_resistors' order for naming a line: a net named by the designer before
    an automatic Net... name."""
    return name.upper().startswith("NET"), natural_key(name)


def select(api: DesignAPI, objects: str, flt: dict, unknown: list | None = None) -> list:
    """The objects the filter selects. unknown collects (object, reason, rails) for those
    the filter cannot decide (a rail of unknown voltage, a side without a PCB): they are
    left out and counted as not evaluated. one_per_line keeps one net per line."""
    if objects == "parts":
        items, match = api.parts, match_part
    elif objects == "nets":
        items, match = api.nets, match_net
    else:
        items, match = api.pins, match_pin
    out = []
    for item in items:
        notes: dict = {}
        found = match(api, item, flt, notes=notes)
        if found:
            out.append(item)
        elif found is None and unknown is not None:
            rails = sorted(notes.get("rails") or (), key=natural_key)
            unknown.append((item, VOLTAGE_UNKNOWN if rails else NO_PCB_SIDE, rails))
    if objects == "nets" and flt.get("one_per_line"):
        kept, covered = [], set()
        for net in sorted(out, key=lambda item: _line_order(item.name)):
            if net.name not in covered:
                kept.append(net)
                covered.update(api.line(net.name))
        out = sorted(kept, key=lambda item: natural_key(item.name))
    return out


# ---------- asserting ----------

class Outcome:
    """One assertion on one object: status pass, fail or unknown, the reason (a short
    text) and values for the message template."""

    __slots__ = ("status", "text", "values", "related")

    def __init__(self, status: str, text: str = "", values: dict | None = None,
                 related: list[str] | None = None):
        self.status = status
        self.text = text
        self.values = values or {}
        self.related = related or []


def _part_of(api: DesignAPI, obj, objects: str):
    return obj if objects == "parts" else api.part(obj.part) if objects == "pins" else None


def _field(obj, objects: str, name: str):
    if objects == "nets":
        return obj.name if name == "name" else None
    if objects == "pins":
        return obj.part if name == "designator" else getattr(obj, name, None)
    return getattr(obj, name, None)


def _start_nets(api: DesignAPI, obj, objects: str, on_line: bool = False) -> list[str]:
    """The nets a count or connected assertion starts from (a part's other than ground;
    with on_line, every net of their lines)."""
    if objects == "nets":
        nets = [obj.name]
    elif objects == "pins":
        nets = [obj.net] if obj.net else []
    else:
        nets = [net for net in obj.nets if "ground" not in api.net_classes(net)]
    if on_line:
        nets = list(dict.fromkeys(member for net in nets for member in api.line(net)))
    return nets


def _found_parts(api: DesignAPI, reached: dict[str, int], flt: dict, skip: str | None
                 ) -> tuple[list[str], list[str], str]:
    """(the parts on the reached nets that meet the filter, nearest first; those an
    unknown voltage or side leaves open; why they are open)."""
    found: dict[str, None] = {}
    maybe: dict[str, None] = {}
    notes: dict = {}
    for net in sorted(reached, key=lambda name: (reached[name], natural_key(name))):
        record = api.net(net)
        if record is None:
            continue
        for part in record.parts:
            if part.designator == skip or part.designator in found:
                continue
            matched = match_part(api, part, flt, net, notes)
            if matched:
                found[part.designator] = None
                maybe.pop(part.designator, None)
            elif matched is None:
                maybe[part.designator] = None
    return list(found), list(maybe), VOLTAGE_UNKNOWN if notes.get("rails") else NO_PCB_SIDE


def check_assertion(api: DesignAPI, obj, objects: str, spec: dict) -> Outcome:
    name = spec["name"]
    part = _part_of(api, obj, objects)
    if name == "none":
        return Outcome("fail", "", {"forbidden": True})
    if name == "has_parameter":
        missing = [item for item in spec["names"] if part.parameter(item) is None]
        if spec["mode"] == "any":
            if len(missing) == len(spec["names"]):
                return Outcome("fail", f"none of {', '.join(spec['names'])}",
                               {"missing": " or ".join(spec["names"])})
            return Outcome("pass")
        if missing:
            return Outcome("fail", f"no {', '.join(missing)}", {"missing": ", ".join(missing)})
        return Outcome("pass")
    if name == "has":
        missing = [item for item in spec["fields"] if not getattr(part, item, None)]
        if missing:
            return Outcome("fail", f"no {', '.join(missing)}", {"missing": ", ".join(missing)})
        return Outcome("pass")
    if name == "parameter_matches":
        for key, pattern in spec["tests"]:
            value = part.parameter(key)
            if value is None:
                return Outcome("fail", f"no {key}", {"missing": key})
            if not pattern.search(value):
                return Outcome("fail", f"{key} '{clip(value, 60)}' does not match "
                                       f"/{pattern.pattern}/")
        return Outcome("pass")
    if name == "matches":
        for key, pattern in spec["tests"]:
            value = _field(obj, objects, key)
            if not _text_match(pattern, value):
                shown = f"'{clip(value, 60)}'" if value not in (None, "") else "empty"
                return Outcome("fail", f"{key} {shown} does not match /{pattern.pattern}/")
        return Outcome("pass")
    if name in ("count", "connected"):
        starts = _start_nets(api, obj, objects, spec.get("on_line", False))
        if not starts:
            return Outcome("unknown", "not connected")
        reached = api.reachable(starts, spec["hops"], spec["through"])
        skip = obj.designator if objects == "parts" else None
        found, maybe, open_reason = _found_parts(api, reached, spec["parts"], skip)
        hops = spec["hops"]
        where = "on the net" if hops == 0 else f"within {hops} series part{'s' * (hops > 1)}"
        if spec.get("on_line"):
            where = ("on the line" if hops == 0 else
                     f"on the line or within {hops} series part{'s' * (hops > 1)}")
        if name == "connected":
            if found:
                return Outcome("pass", values={"count": len(found)}, related=found)
            if maybe:
                return Outcome("unknown", open_reason)
            return Outcome("fail", f"no {spec['label']} {where}", {"count": 0})
        count = len(found)
        values = {"count": count}
        low, high = spec.get("min", 0), spec.get("max", math.inf)
        if count > high or count + len(maybe) < low:
            wanted = (f"exactly {low}" if low == high else f"at least {low}"
                      if high == math.inf else f"at most {high}" if not low
                      else f"{low} to {high}")
            listed = f" ({', '.join(found[:6])}{'...' if count > 6 else ''})" if found else ""
            return Outcome("fail", f"found {count} {spec['label']} {where}{listed}; {wanted} "
                                   "expected", values, found[:10])
        if maybe and (count < low or count + len(maybe) > high):
            return Outcome("unknown", open_reason, values)
        return Outcome("pass", values=values, related=found)
    if name == "nearest":
        return _nearest(api, obj, objects, part, spec)
    if name == "rating_ratio":
        if part.kind != "capacitor":
            return Outcome("unknown", "not a capacitor")
        rated = api.rated_voltage(part.designator)
        across = api.voltage_across(part.designator)
        values = {"rated_voltage": rated, "voltage": across}
        if rated is None:
            return Outcome("unknown", "no voltage rating read", values)
        if across is None:
            return Outcome("unknown", "voltage across it unknown", values)
        if across <= 0:
            return Outcome("pass", values=values)
        ratio = rated / across
        values["ratio"] = round(ratio, 3)
        if ratio < spec["min"] - 1e-9:
            return Outcome("fail", f"rated {rated:g} V for {across:g} V across it: "
                                   f"{ratio:.2g}x, {spec['min']:g}x wanted", values)
        return Outcome("pass", values=values)
    if name == "side":
        placement = api.placement(part.designator)
        if placement is None:
            return Outcome("unknown", "no PCB" if not api.has_pcb else "not on the PCB")
        if placement.side != spec["side"]:
            return Outcome("fail", f"on the {placement.side} side", {"side": placement.side})
        return Outcome("pass")
    if name == "case_size":
        case = part.case_size
        if case is None:
            return Outcome("unknown", "case size not read")
        result = _case_ok(case, spec["spec"])
        if result is None:
            return Outcome("unknown", f"case {case} cannot be ordered by size")
        if not result:
            return Outcome("fail", f"case {case}", {"case_size": case})
        return Outcome("pass")
    raise ValueError(f"unknown assertion {name}")  # pragma: no cover


def _nearest(api: DesignAPI, obj, objects: str, part, spec: dict) -> Outcome:
    if not api.has_pcb:
        return Outcome("unknown", "no PCB")
    if objects == "pins":
        source = api.pads(part.designator, obj.number)
        nets = [obj.net] if obj.net else []
    else:
        source = api.pads(part.designator)
        nets = [net for net in part.nets if "ground" not in api.net_classes(net)]
    if not source:
        return Outcome("unknown", "not on the PCB")
    candidates: dict[str, list] = {}
    maybe: list[str] = []
    notes: dict = {}

    def consider(other, context: str | None, pads: list) -> None:
        matched = match_part(api, other, spec["parts"], context, notes)
        if matched:
            candidates.setdefault(other.designator, []).extend(pads)
        elif matched is None:
            maybe.append(other.designator)

    if spec["same_net"]:
        for net in nets:
            record = api.net(net)
            if record is None:
                continue
            for pin in record.pins:
                other = api.part(pin.part)
                if pin.part != part.designator and other is not None:
                    consider(other, net, api.pads(pin.part, pin.number))
    else:
        for other in api.parts:
            if other.designator != part.designator:
                consider(other, None, api.pads(other.designator))
    open_reason = VOLTAGE_UNKNOWN if notes.get("rails") else NO_PCB_SIDE
    on_net = " on the same net" if spec["same_net"] else ""
    if not candidates:
        if maybe:
            return Outcome("unknown", open_reason)
        return Outcome("fail", f"no {spec['label']}{on_net}", {"count": 0})
    found = api.nearest(source, candidates)
    if found is None:
        return Outcome("unknown", f"no {spec['label']}{on_net} is on the PCB")
    nearest, distance = found
    values = {"distance_mm": distance, "nearest": nearest}
    if distance > spec["max_mm"] + 1e-9:
        if maybe:  # a part the filter leaves open may be the nearer one
            return Outcome("unknown", open_reason, values)
        return Outcome("fail", f"nearest {spec['label']}{on_net} is {nearest}, "
                               f"{_mm(distance, spec['max_mm'])} mm away (max "
                               f"{spec['max_mm']:g} mm)", values, [nearest])
    return Outcome("pass", values=values, related=[nearest])


def _mm(distance: float, limit: float) -> str:
    """A distance for a message: 2 decimals, 3 near the limit (5.002 mm, not 5.00)."""
    return f"{distance:.3f}" if abs(distance - limit) < 0.01 else f"{distance:.2f}"


# ---------- findings ----------

class _Formatter(string.Formatter):
    def get_value(self, key, args, kwargs):
        value = kwargs.get(key)
        if value is None or value == "" or (isinstance(value, (list, tuple)) and not value):
            return "?"
        if isinstance(value, (list, tuple)):
            return ", ".join(str(item) for item in value[:10]) + (
                f" +{len(value) - 10}" if len(value) > 10 else "")
        return value

    def format_field(self, value, format_spec):
        if isinstance(value, float) and not format_spec:
            return f"{value:.4g}"
        try:
            return format(value, format_spec)
        except (TypeError, ValueError):
            return str(value)


_FORMAT = _Formatter()


def render(template: str, values: dict) -> str:
    try:
        return clip(_FORMAT.vformat(template, (), values), 400)
    except (KeyError, IndexError, ValueError, AttributeError):
        return clip(template, 400)


def template_fields(template: str | None) -> frozenset[str]:
    """The fields a validated message template names."""
    if not template:
        return frozenset()
    return frozenset(name for _, name, _, _ in string.Formatter().parse(template) if name)


def _object_values(api: DesignAPI, obj, objects: str, wanted: frozenset[str]) -> dict:
    """The message values of a selected object. side, voltage (a net's, a pin's net's or
    the highest of a part's nets) and connectors (those its nets leave the board through)
    are read only when the template names them; a net's connectors always are (they are
    its finding's designators)."""
    if objects == "nets":
        volt = api.voltage(obj.name) if "voltage" in wanted else None
        return {"net": obj.name, "nets": [obj.name], "designators": list(obj.designators),
                "voltage": volt.volts if volt else None,
                "connectors": list(api.connectors_of(obj.name))}
    part = _part_of(api, obj, objects)
    values = {key: getattr(part, key) for key in (
        "designator", "value", "comment", "description", "footprint", "mpn",
        "manufacturer", "kind", "sheet")}
    values["designators"] = [part.designator]
    values["case_size"] = part.case_size
    if objects == "pins":
        nets = [obj.net] if obj.net else []
        values.update(pin=obj.number, pin_name=obj.name, pin_type=obj.type, net=obj.net,
                      nets=nets)
    else:
        nets = [net for net in part.nets if "ground" not in api.net_classes(net)]
        values["nets"] = nets
        values["net"] = nets[0] if nets else None
    if "side" in wanted:
        values["side"] = part.side if api.has_pcb else None
    if "voltage" in wanted:
        known = [abs(v.volts) for net in nets if (v := api.voltage(net)) is not None]
        values["voltage"] = max(known) if known else None
    if "connectors" in wanted:
        values["connectors"] = list(dict.fromkeys(
            ref for net in nets for ref in api.connectors_of(net)))
    return values


def _label(objects: str, values: dict) -> str:
    if objects == "nets":
        return str(values["net"])
    if objects == "pins":
        return f"{values['designator']}.{values['pin']}" + (
            f" ({values['pin_name']})" if values.get("pin_name") and
            values["pin_name"] != values["pin"] else "") + (
            f" on {values['net']}" if values.get("net") else "")
    return str(values["designator"])


def _location(api: DesignAPI, obj, objects: str) -> dict:
    if objects == "nets":
        sheets = list(dict.fromkeys(part.sheet for part in obj.parts if part.sheet))
        return {"sheets": sheets[:3]} if sheets else {}
    part = _part_of(api, obj, objects)
    out: dict = {}
    if part.sheet:
        out["sheet"] = part.sheet
    placement = api.placement(part.designator)
    if placement is not None:
        out.update(side=placement.side, x_mm=placement.x_mm, y_mm=placement.y_mm)
    return out


def _finding(rule: dict, issue: str, designators=(), nets=(), pins=(), location=None) -> dict:
    out = {"check": rule["id"], "pack": rule["pack"], "severity": rule["severity"]}
    if designators:
        out["designators"] = list(dict.fromkeys(designators))[:20]
    if nets:
        out["nets"] = list(dict.fromkeys(net for net in nets if net))[:10]
    if pins:
        out["pins"] = list(pins)[:20]
    if location:
        out["location"] = location
    out["issue"] = issue
    return out


# ---------- running ----------

def _row(rule: dict, kind: str) -> dict:
    return {"id": rule["id"], "pack": rule["pack"], "kind": kind, "title": rule["title"],
            "severity": rule["severity"], "status": "pass", "checked": 0, "findings": 0}


def _summarize(row: dict, found: list[dict]) -> None:
    """A failed rule row's first message, anchors (designators and nets of its
    findings) and counts by severity: what a checklist item quotes."""
    if row["status"] != "fail" or not found:
        return
    row["first"] = found[0]["issue"]
    anchors: dict = {}
    designators = list(dict.fromkeys(d for f in found for d in f.get("designators", [])))
    nets = list(dict.fromkeys(n for f in found for n in f.get("nets", [])))
    if designators:
        anchors["designators"] = designators[:10]
    if nets:
        anchors["nets"] = nets[:10]
    if anchors:
        row["anchors"] = anchors
    row["severity_counts"] = dict(Counter(f["severity"] for f in found))


def run_select_rule(api: DesignAPI, rule: dict) -> tuple[dict, list[dict]]:
    row = _row(rule, "select")
    left_open: list = []
    objects = select(api, rule["objects"], rule["filter"], left_open)
    row["checked"] = len(objects)
    findings: list[dict] = []
    unknown: Counter = Counter()
    rails: Counter = Counter()
    for _, reason, open_rails in left_open:
        unknown[reason] += 1
        rails.update(open_rails)
    if rails:
        row["voltage_unknown_rails"] = [net for net, _ in rails.most_common(5)]
    wanted = template_fields(rule.get("message"))
    passed = 0
    forbid = any(item["name"] == "none" for item in rule["assert"])
    for obj in objects:
        outcomes = [check_assertion(api, obj, rule["objects"], item) for item in rule["assert"]]
        failed = [o for o in outcomes if o.status == "fail"]
        open_ = [o for o in outcomes if o.status == "unknown"]
        if not failed and open_ and rule["on_unknown"] == "fail":
            failed = open_
        if not failed:
            if open_:
                unknown[open_[0].text] += 1
            else:
                passed += 1
            continue
        if len(findings) >= MAX_FINDINGS_PER_RULE:
            row["findings_capped"] = True
            row["findings"] += 1
            continue
        values = _object_values(api, obj, rule["objects"], wanted)
        for outcome in outcomes:
            values.update({k: v for k, v in outcome.values.items() if v is not None})
        failed_text = "; ".join(o.text for o in failed if o.text)
        if any(o.values.get("forbidden") for o in failed):
            failed_text = rule["title"] + (f"; {failed_text}" if failed_text else "")
        values.pop("forbidden", None)
        values.update(failed=failed_text, rule=rule["id"], title=rule["title"],
                      pack=rule["pack"], severity=rule["severity"])
        if "message" in rule:
            issue = render(rule["message"], values)
        else:
            issue = f"{_label(rule['objects'], values)}: {failed_text}"
        related = [ref for o in failed for ref in o.related]
        designators = values.get("designators") or []
        if rule["objects"] == "nets":
            designators = related or values.get("connectors") or []
        else:
            designators = [*designators, *related]
        pins = [f"{obj.part}.{obj.number}"] if rule["objects"] == "pins" else ()
        nets = values.get("nets") or []
        if rule["objects"] == "parts" and len(nets) > 3:
            nets = []
        findings.append(_finding(rule, issue, designators, nets, pins,
                                 _location(api, obj, rule["objects"])))
        row["findings"] += 1
    if unknown:
        row["not_evaluated"] = sum(unknown.values())
        row["not_evaluated_reasons"] = dict(unknown.most_common(3))
    if row["findings"]:
        row["status"] = "fail"
    elif not objects and not unknown:
        row["status"] = "pass" if forbid else "not_applicable"
        if not forbid:
            row["reason"] = "nothing selected"
    elif passed == 0 and unknown:
        row["status"] = "could_not_evaluate"
        row["reason"] = unknown.most_common(1)[0][0]
        if row["reason"] == VOLTAGE_UNKNOWN and rails:
            names = [net for net, _ in rails.most_common()]
            more = f" and {len(names) - 3} more" if len(names) > 3 else ""
            row["reason"] = (f"{VOLTAGE_UNKNOWN} on {', '.join(names[:3])}{more}: give them "
                             "in the pack's rails")
    return row, findings


def run_tool_rule(runner: ToolRunner, rule: dict) -> tuple[dict, list[dict]]:
    row = _row(rule, "tool")
    row["tool"] = rule["tool"]
    if rule["args"]:
        row["args"] = rule["args"]
    result = runner.run(rule["tool"], rule["args"])
    if "error" in result:
        code = result["error"]
        row["status"] = "not_applicable" if code in rule["na_errors"] else "could_not_evaluate"
        row["reason"] = clip(f"{code}: {result.get('message') or code}", 200)
        return row, []
    data = result["data"]
    row["checked"] = 1
    unresolved: list[str] = []
    zero: list[str] = []

    def test(cond: dict) -> dict:
        # A count missing from a summary of counts is 0 (a clean board lists none).
        hit = evaluate(cond, data, absent_counts=True)
        unresolved.extend(hit["unresolved"])
        zero.extend(hit.get("zero", []))
        return hit

    for cond in rule.get("na", []):
        hit = test(cond)
        if hit["hit"] and not hit["unresolved"]:
            row["status"] = "not_applicable"
            row["reason"] = clip(hit["label"], 200)
            return row, []
    findings: list[dict] = []
    base = {"tool": rule["tool"], "rule": rule["id"], "title": rule["title"],
            "pack": rule["pack"], "severity": rule["severity"]}
    for cond in rule.get("fail", []):
        hit = test(cond)
        if not hit["hit"]:
            continue
        rows = hit["items"]
        if not rows:
            value = hit.get("value", hit.get("count"))
            issue = render(rule["message"], {**base, "label": hit["label"], "count": hit.get(
                "count"), "issue": hit["label"]}) if "message" in rule else (
                f"{hit['label']}: {value}" if value is not None else hit["label"])
            findings.append(_finding(rule, issue))
            continue
        for item in rows:
            if len(findings) >= MAX_FINDINGS_PER_RULE:
                row["findings_capped"] = True
                break
            anchors = anchors_of([item], rule["tool"])
            text = row_text([item]) or hit["label"]
            values = {**base, "label": hit["label"], "issue": text, "count": len(rows),
                      "designators": anchors.get("designators") or [],
                      "nets": anchors.get("nets") or []}
            values["designator"] = (values["designators"] or [None])[0]
            values["net"] = (values["nets"] or [None])[0]
            issue = render(rule["message"], values) if "message" in rule else text
            location = {"sheets": anchors["sheets"][:3]} if anchors.get("sheets") else None
            findings.append(_finding(rule, issue, anchors.get("designators") or [],
                                     anchors.get("nets") or [], location=location))
    for cond in rule.get("pass", []):
        hit = test(cond)
        if not hit["hit"] and not hit["unresolved"]:  # what was not read is not "not met"
            issue = render(rule["message"], {**base, "label": hit["label"],
                                             "issue": f"not met: {hit['label']}",
                                             "count": hit.get("count")}) \
                if "message" in rule else f"Not met: {hit['label']}"
            findings.append(_finding(rule, issue))
    row["findings"] = len(findings)
    if zero:
        row["counted_as_zero"] = list(dict.fromkeys(zero))[:5]
    if unresolved:
        reason = clip(f"unresolved: {', '.join(dict.fromkeys(unresolved))} not in the "
                      f"{rule['tool']} result", 200)
        if not findings:  # what was not read cannot pass
            row["status"] = "could_not_evaluate"
            row["reason"] = reason
            return row, []
        row["unresolved"] = reason  # the findings that were read still count
    if findings:
        row["status"] = "fail"
    elif result.get("truncated"):
        row["status"] = "could_not_evaluate"
        row["reason"] = "the tool's result had more pages than were read"
    return row, findings


# ---------- plugins ----------

def plugins_allowed(allow: bool, environ=None) -> tuple[bool, str | None]:
    """(run plugins?, why not)."""
    environ = os.environ if environ is None else environ
    enabled = str(environ.get(PLUGIN_ENV, "")).strip() == "1"
    if allow and enabled:
        return True, None
    if not allow and not enabled:
        return False, ("not run: call with allow_plugins=true, and start the server with "
                       f"{PLUGIN_ENV}=1 in its environment")
    if not allow:
        return False, "not run: call with allow_plugins=true to run them"
    return False, (f"not run: the server's environment does not set {PLUGIN_ENV}=1 (the "
                   "user sets it where the server is started; it cannot be set from a call)")


def _plugin_line(error: BaseException, path: Path) -> int | None:
    line = None
    for frame in traceback.extract_tb(error.__traceback__):
        if os.path.normcase(frame.filename) == os.path.normcase(str(path)):
            line = frame.lineno
    return line


def _plugin_findings(items, rule: dict) -> tuple[list[dict], int]:
    """The plugin's findings, checked; and how many items were not findings."""
    out: list[dict] = []
    bad = 0
    for item in items:
        if isinstance(item, str):
            item = {"message": item}
        if not isinstance(item, dict):
            bad += 1
            continue
        message = item.get("message", item.get("issue"))
        if not isinstance(message, str) or not message.strip():
            bad += 1
            continue
        severity = item.get("severity", rule["severity"])
        if severity not in SEVERITIES:
            bad += 1
            continue

        def names(value) -> list[str]:
            if isinstance(value, str):
                return [value]
            if isinstance(value, (list, tuple)):
                return [str(v) for v in value if isinstance(v, (str, int))][:20]
            return []

        location = item.get("location")
        if not isinstance(location, dict) or not all(
                isinstance(v, (str, int, float, bool)) or v is None
                for v in location.values()):
            location = None
        finding = _finding({**rule, "severity": severity}, clip(message, 400),
                           names(item.get("designators")), names(item.get("nets")),
                           names(item.get("pins")), location)
        check = item.get("check")
        if isinstance(check, str) and _RULE_ID.fullmatch(check):
            finding["check"] = check
        out.append(finding)
        if len(out) >= MAX_FINDINGS_PER_RULE:
            break
    return out, bad


def run_plugin(path: Path, api: DesignAPI | None, reason: str | None = None
               ) -> tuple[dict, list[dict]]:
    """One plugin file: exec its code in a fresh module, call check(api) and check what
    it returns. Every error (import, a missing check, an exception, a bad result) is
    reported in the row; standard output and error are captured, never written (the MCP
    protocol uses standard output)."""
    ident = plugin_id(path)
    rule = {"id": ident, "pack": PLUGIN_PACK, "title": ident, "severity": "warning"}
    row = {**_row(rule, "plugin"), "file": f"{PACK_FOLDER}/{PLUGIN_FOLDER}/{path.name}"}
    if reason:
        row.update(status="could_not_evaluate", reason=reason)
        return row, []
    start = time.perf_counter()
    name = f"altium_copilot_plugin_{ident.replace('.', '_').replace('-', '_')}"
    module = types.ModuleType(name)
    module.__file__ = str(path)
    captured = io.StringIO()
    try:
        if path.stat().st_size > MAX_PLUGIN_BYTES:
            raise ValueError(f"the file is larger than {MAX_PLUGIN_BYTES // 1024} KB")
        source = path.read_bytes().decode("utf-8-sig")
        code = compile(source, str(path), "exec", dont_inherit=True)
        sys.modules[name] = module
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
            exec(code, module.__dict__)  # opted in twice: repository code
            check = getattr(module, "check", None)
            if not callable(check):
                raise TypeError("the plugin defines no check(api) function")
            title = getattr(module, "TITLE", None)
            if isinstance(title, str) and title.strip():
                row["title"] = clip(title, 120)
            severity = getattr(module, "SEVERITY", None)
            if severity in SEVERITIES:
                rule["severity"] = row["severity"] = severity
            result = check(api)
            if result is None:
                result = []
            if isinstance(result, (str, dict)) or not hasattr(result, "__iter__"):
                raise TypeError("check(api) must return a list of findings")
            items = list(result)
    except (Exception, SystemExit) as error:
        line = _plugin_line(error, path)
        row.update(status="could_not_evaluate",
                   reason=clip(f"{type(error).__name__}"
                               f"{f' at line {line}' if line else ''}: {error}", 240))
        row["ms"] = round((time.perf_counter() - start) * 1000)
        return row, []
    finally:
        sys.modules.pop(name, None)
    findings, bad = _plugin_findings(items, rule)
    row["checked"] = 1
    row["findings"] = len(findings)
    if bad:
        row["bad_items"] = bad
        row["reason"] = (f"{bad} returned item(s) were not findings (a dict with a message, "
                         "and severity error, warning or info)")
    if captured.getvalue():
        row["output_chars"] = len(captured.getvalue())
    row["status"] = "fail" if findings else "pass"
    row["ms"] = round((time.perf_counter() - start) * 1000)
    return row, findings


# ---------- the run ----------

def tool_needs(rules: list[dict]) -> dict[tuple[str, str], set[str]]:
    """The result lists each tool call's conditions read (the runner pages only those)."""
    needs: dict[tuple[str, str], set[str]] = {}
    for rule in rules:
        if rule["kind"] == "tool":
            keys = list_keys({"no": [*rule.get("fail", []), *rule.get("pass", []),
                                     *rule.get("na", [])]})
            needs.setdefault(ToolRunner.key(rule["tool"], rule["args"]), set()).update(keys)
    return needs


def run_rules(rules: list[dict], *, api: DesignAPI | None, tools: dict[str, Callable[..., Any]],
              packs: list[dict], no_design: str | None = None,
              plugins: list[Path] | None = None, plugin_reason: str | None = None,
              clock: Callable[[], float] = time.perf_counter) -> dict:
    """Run the chosen rules (and the plugin files, with plugin_reason None to run them)
    and return the model run_custom_checks pages: rule rows, findings (errors first),
    summary by pack and severity, tool runs and the time taken. api None (a netlist-free
    load) leaves select rules could_not_evaluate with no_design as the reason."""
    start = clock()
    runner = ToolRunner(tools, clock=clock, needs=tool_needs(rules))
    rails_of = {pack["id"]: pack.get("rails") for pack in packs}
    order = {pack["id"]: index for index, pack in enumerate(packs)}
    rows: list[dict] = []
    findings: list[dict] = []
    views: dict[str, DesignAPI] = {}
    for rule in rules:
        began = clock()
        if rule["kind"] == "tool":
            row, found = run_tool_rule(runner, rule)
        elif api is None:
            row, found = _row(rule, "select"), []
            row.update(status="could_not_evaluate",
                       reason=no_design or "netlist_required: the design is not loaded")
        else:
            rails = rails_of.get(rule["pack"])
            key = json.dumps(rails, sort_keys=True)
            if key not in views:
                views[key] = api.with_rails(rails)
            try:
                row, found = run_select_rule(views[key], rule)
            except Exception as error:  # one broken rule must not stop the others
                row, found = _row(rule, "select"), []
                row.update(status="could_not_evaluate",
                           reason=f"the check failed ({type(error).__name__}: "
                                  f"{clip(error, 160)})")
        row["ms"] = round((clock() - began) * 1000)
        _summarize(row, found)
        rows.append(row)
        findings += found
    plugin_ids = [plugin_id(path) for path in plugins or []]
    for path in plugins or []:
        if plugin_reason is not None:
            continue
        row, found = run_plugin(path, api, None if api is not None else (
            no_design or "netlist_required: the design is not loaded"))
        _summarize(row, found)
        rows.append(row)
        findings += found
    pack_rank = {**order, PLUGIN_PACK: len(order)}
    rule_rank = {(row["pack"], row["id"]): index for index, row in enumerate(rows)}
    findings.sort(key=lambda f: (
        _SEVERITY_RANK[f["severity"]], pack_rank.get(f["pack"], 99),
        rule_rank.get((f["pack"], f["check"]), 9999),
        natural_key((f.get("designators") or f.get("nets") or [""])[0])))
    severity = Counter(f["severity"] for f in findings)
    by_pack: dict[str, dict[str, int]] = {}
    for pack in [*packs, *([{"id": PLUGIN_PACK}] if plugins and plugin_reason is None else [])]:
        counts = Counter(f["severity"] for f in findings if f["pack"] == pack["id"])
        by_pack[pack["id"]] = {level: counts.get(level, 0) for level in SEVERITIES}
    statuses = Counter(row["status"] for row in rows)
    return {
        "rules": rows,
        "findings": findings,
        "severity_counts": {level: severity.get(level, 0) for level in SEVERITIES},
        "summary": {"rules": {status: statuses.get(status, 0) for status in STATUSES},
                    "by_pack": by_pack},
        "verdict": _verdict(rows, severity),
        "plugin_ids": plugin_ids,
        "tool_runs": runner.runs,
        "elapsed_ms": round((clock() - start) * 1000),
    }


def _verdict(rows: list[dict], severity: Counter) -> str:
    """fail (an error finding), warn (warnings only), incomplete (no error or warning,
    but a rule could not be evaluated or left objects not evaluated, or no rule ran),
    pass: what was not evaluated is never passed."""
    if severity.get("error"):
        return "fail"
    if severity.get("warning"):
        return "warn"
    if not rows or any(row["status"] == "could_not_evaluate" or row.get("not_evaluated")
                       for row in rows):
        return "incomplete"
    return "pass"


ROW_DETAILS = ("full", "compact", "minimal")
_COMPACT_KEYS = ("id", "pack", "kind", "status", "checked", "findings", "not_evaluated",
                 "reason", "first", "anchors", "file")
_MINIMAL_KEYS = ("id", "pack", "status", "checked", "findings", "not_evaluated", "reason")


def rule_row(row: dict, detail: str = "full") -> dict:
    """A rule row at a detail level: full; compact (status, counts, the reason and the
    first finding clipped, 3 anchors of each kind); minimal (status and counts, the reason
    of a rule that did not run). Every level keeps id, pack and status: a checklist's
    custom item reads them from any page."""
    if detail == "full":
        return row
    keys = _COMPACT_KEYS if detail == "compact" else _MINIMAL_KEYS
    out = {key: row[key] for key in keys if key in row}
    limit = 160 if detail == "compact" else 80
    for key in ("reason", "first"):
        if key in out:
            out[key] = clip(out[key], limit)
    if "anchors" in out:
        out["anchors"] = {kind: names[:3] for kind, names in out["anchors"].items()}
    if detail == "minimal" and out["status"] in ("pass", "fail"):
        out.pop("reason", None)
    return out


def page(model: dict, offset: int, limit: int, *, rows: str = "full") -> dict:
    """One page of findings; the verdict, summary and every rule's row go with every page
    (rows: their detail, see rule_row)."""
    offset = max(0, int(offset or 0))
    findings = model["findings"]
    out: dict = {
        "verdict": model["verdict"],
        "severity_counts": model["severity_counts"],
        "summary": model["summary"],
        "rules": [rule_row(row, rows) for row in model["rules"]],
    }
    if rows != "full":
        out["rule_detail"] = rows
    out.update({"findings": findings[offset:offset + limit], "total": len(findings),
                "offset": offset, "has_more": offset + limit < len(findings)})
    if out["has_more"]:
        out["next_offset"] = offset + limit
    return out
