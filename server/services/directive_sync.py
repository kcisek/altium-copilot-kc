"""Schematic directives against the saved PCB: did each one reach the board?

In a classic project the schematic carries design intent that Update PCB (the ECO)
turns into PCB objects: Parameter Set directives with a ClassName parameter become net
classes, Differential Pair directives (DifferentialPair=True on both legs, named by the
project's _P / _N suffixes) become differential pairs (with a ClassName, a differential
pair class), Rule parameters become design rules marked as defined by the schematic
(DEFINEDBYLOGICALDOCUMENT), buses become net classes, and per-sheet class generation makes
a component class and a room for each sheet. Net classes, component classes and rooms
transfer only while the project's Class Generation options enable them; with a kind left
out, Update PCB neither adds nor changes its classes, so their differences are info. In a
project converted to the Constraint Manager (the .PrjPcb's [Design]
ConstraintManagerFlow switch) only the Constraint Manager's own items transfer: directives
are ignored unless imported, so every finding there is info.

The schematic side is the offline compile (services/sch_netlister): each Parameter Set
with the nets it covers (the wire's net, a bus's members, a signal harness's nets, the
nets under a blanket), the compiled sheet copies with their parts, and the buses. Nets
reach the PCB through check_sch_pcb_sync's matching (shared members, then name), so a
net renamed on the PCB is still found; parts likewise (unique ID, then designator), and a
part Update PCB may not place (mechanical, graphical, no footprint) is in no class. The
PCB side is the PcbDoc's classes, pairs and rules.

Rule parameter text: no project read so far carries one, so its format is decoded
leniently: KEY=value properties as Rules6 stores them (RULEKIND=Width|MINLIMIT=10mil...)
or a description ("Width Constraint (Min Width = 10mil) (Max Width = 12mil)").
"""

import base64
import re
import struct
import xml.etree.ElementTree as ElementTree
import zlib
from collections import Counter, defaultdict
from pathlib import Path

import olefile

try:
    from parsers.pcb_doc import PcbRule
    from services.pcb_rules import ROOM_KIND, ScopeSyntaxError, parse_scope, rule_values
    from services.sch_pcb_sync import SyncMaps, sync_maps
except ImportError:
    from server.parsers.pcb_doc import PcbRule
    from server.services.pcb_rules import (
        ROOM_KIND, ScopeSyntaxError, parse_scope, rule_values,
    )
    from server.services.sch_pcb_sync import SyncMaps, sync_maps

KINDS = ("net_class", "diff_pair", "rule", "component_class", "room")
CHECKS = (
    "directive_class_missing", "bus_class_missing", "class_name_differs",
    "class_members_differ",
    "diff_pair_missing", "diff_pair_nets_differ", "diff_pair_name_differs",
    "directive_rule_missing", "rule_values_differ", "rule_text_not_decoded",
    "component_class_missing", "room_missing", "stale_pcb_item",
)
_SEVERITY_RANK = {"error": 0, "warning": 1, "info": 2}
_STATUS_RANK = {"missing": 0, "differs": 1, "not_decoded": 2, "transferred": 3,
                "not_generated": 4}
DEFAULT_LIMIT = 100
ROWS_LISTED = 60
LISTED = 20          # nets, members or values listed in one finding
CACHE_SIZE = 4
TOLERANCE_MIL = 0.01
_TRUE = ("true", "t", "1", "-1", "yes")
# The .PrjPcb switch of the Constraint Manager flow: [Design] ConstraintManagerFlow, named
# so in Altium's own code (cParamName_ConstraintManagerFlow, IsConstraintManagerFlowEnabled).
# Altium writes it as 0 in classic projects; non-zero means the project uses the
# Constraint Manager.
CM_FLOW_SECTION, CM_FLOW_KEY = "Design", "ConstraintManagerFlow"

# Project Options > Class Generation ([PrjClassGen]) and Altium's defaults when a key is
# not written (every project read writes them all).
PROJECT_OPTIONS = {
    "NetClassManualEnabled": ("user_defined_net_classes", True),
    "CompClassManualEnabled": ("user_defined_component_classes", True),
    "CompClassManualRoomEnabled": ("user_defined_component_class_rooms", False),
    "NetClassAutoBusEnabled": ("bus_net_classes", True),
    "NetClassSeparateForBusSections": ("separate_bus_section_classes", False),
    "NetClassAutoCompEnabled": ("component_net_classes", False),
    "NetClassAutoNamedHarnessEnabled": ("named_harness_net_classes", False),
}
SHEET_OPTIONS = {
    "ClassGenCCAutoEnabled": ("component_class", True),
    "ClassGenCCAutoRoomEnabled": ("room", True),
    "ClassGenNCAutoScope": ("net_classes", "None"),
    "GenerateClassCluster": ("structure_class", False),
}
CM_NOTE = (
    "Constraint Manager project: Update PCB transfers the Constraint Manager's own classes, "
    "pairs and rules; schematic directives are ignored unless imported into the Constraint "
    "Manager (Design > Constraint Manager, import from schematic directives). Findings here "
    "are info: compare with the Constraint Manager in Altium."
)
RULE_TEXT_NOTE = (
    "Rule parameters are decoded leniently (KEY=value properties as the PcbDoc stores rules, "
    "or a 'Width Constraint (Min Width = 10mil)' description); no project read so far "
    "carries one, so check a rule_text_not_decoded or rule_values_differ finding in Altium."
)
NOT_CHECKED = [
    "Signal harness net classes (Generate Net Classes for Named Signal Harnesses), "
    "component net classes and per-sheet net classes (Net Classes scope) are not compared; "
    "their options are listed under class_generation.",
    "Net parameters other than ClassName, DifferentialPair and Rule are not compared.",
    "The Comparator's Difference Levels (differences set to be ignored) are not read: a "
    "difference reported here may be one the project ignores.",
    "A ClassName on a Differential Pair directive is taken to make a differential pair "
    "class that follows the user-defined net classes switch: no project read so far "
    "confirms either.",
]

# Altium's rule type names (Rules dialog and descriptions) -> RULEKIND.
_RULE_KINDS = {
    "width": "Width", "width constraint": "Width",
    "clearance": "Clearance", "clearance constraint": "Clearance",
    "differential pairs routing": "DiffPairsRouting", "diff pairs routing": "DiffPairsRouting",
    "diffpairsrouting": "DiffPairsRouting", "differential pair routing": "DiffPairsRouting",
    "routing via style": "RoutingVias", "routing vias": "RoutingVias",
    "routingvias": "RoutingVias", "length": "Length", "length constraint": "Length",
    "matched lengths": "MatchedLengths", "matched net lengths": "MatchedLengths",
    "matchedlengths": "MatchedLengths", "routing layers": "RoutingLayers",
    "routinglayers": "RoutingLayers", "routing topology": "RoutingTopology",
    "routingtopology": "RoutingTopology", "routing priority": "RoutingPriority",
    "routingpriority": "RoutingPriority", "routing corners": "RoutingCorners",
    "routingcorners": "RoutingCorners", "supply nets": "SupplyNets",
    "supplynets": "SupplyNets", "power plane connect style": "PlaneConnect",
    "plane connect": "PlaneConnect", "planeconnect": "PlaneConnect",
    "power plane clearance": "PlaneClearance", "plane clearance": "PlaneClearance",
    "planeclearance": "PlaneClearance", "polygon connect style": "PolygonConnect",
    "polygon connect": "PolygonConnect", "polygonconnect": "PolygonConnect",
    "fanout control": "FanoutControl", "fanoutcontrol": "FanoutControl",
    "short-circuit": "ShortCircuit", "short circuit": "ShortCircuit",
    "shortcircuit": "ShortCircuit", "un-routed net": "UnRoutedNet",
    "unrouted net": "UnRoutedNet", "unroutednet": "UnRoutedNet",
    "hole size": "HoleSize", "holesize": "HoleSize", "height": "Height",
}
# Compared values per kind: property key (as Rules6 stores it) -> field, in mil.
_KEY_FIELDS = {
    "Width": {"MINLIMIT": "min_width", "PREFEREDWIDTH": "preferred_width",
              "MAXLIMIT": "max_width"},
    "Clearance": {"GENERICCLEARANCE": "clearance", "GAP": "clearance"},
    "DiffPairsRouting": {"MINLIMIT": "min_gap", "MOSTFREQGAP": "preferred_gap",
                         "MAXLIMIT": "max_gap", "MAXUNCOUPLEDLENGTH": "max_uncoupled_length",
                         "MINWIDTH": "min_width", "PREFWIDTH": "preferred_width",
                         "MAXWIDTH": "max_width"},
    "Length": {"MINLIMIT": "min_length", "MAXLIMIT": "max_length"},
    "MatchedLengths": {"TOLERANCE": "tolerance"},
    "RoutingVias": {"MINWIDTH": "min_diameter", "WIDTH": "preferred_diameter",
                    "MAXWIDTH": "max_diameter", "MINHOLEWIDTH": "min_hole",
                    "HOLEWIDTH": "preferred_hole", "MAXHOLEWIDTH": "max_hole"},
    "PlaneClearance": {"CLEARANCE": "clearance"},
    "HoleSize": {"MINLIMIT": "min_hole", "MAXLIMIT": "max_hole"},
    "Height": {"MINHEIGHT": "min_height", "PREFHEIGHT": "preferred_height",
               "MAXHEIGHT": "max_height"},
}
# Description labels -> property key, per kind.
_LABEL_KEYS = {
    "Width": {"min width": "MINLIMIT", "minimum width": "MINLIMIT", "min": "MINLIMIT",
              "max width": "MAXLIMIT", "maximum width": "MAXLIMIT", "max": "MAXLIMIT",
              "pref width": "PREFEREDWIDTH", "preferred width": "PREFEREDWIDTH",
              "preferred": "PREFEREDWIDTH", "pref": "PREFEREDWIDTH"},
    "Clearance": {"clearance": "GENERICCLEARANCE", "min clearance": "GENERICCLEARANCE",
                  "minimum clearance": "GENERICCLEARANCE", "gap": "GENERICCLEARANCE",
                  "min": "GENERICCLEARANCE"},
    "DiffPairsRouting": {"min gap": "MINLIMIT", "minimum gap": "MINLIMIT",
                         "max gap": "MAXLIMIT", "maximum gap": "MAXLIMIT",
                         "pref gap": "MOSTFREQGAP", "preferred gap": "MOSTFREQGAP",
                         "gap": "MOSTFREQGAP",
                         "max uncoupled length": "MAXUNCOUPLEDLENGTH",
                         "maximum uncoupled length": "MAXUNCOUPLEDLENGTH",
                         "min width": "MINWIDTH", "minimum width": "MINWIDTH",
                         "pref width": "PREFWIDTH", "preferred width": "PREFWIDTH",
                         "max width": "MAXWIDTH", "maximum width": "MAXWIDTH"},
    "Length": {"min length": "MINLIMIT", "minimum length": "MINLIMIT", "min": "MINLIMIT",
               "max length": "MAXLIMIT", "maximum length": "MAXLIMIT", "max": "MAXLIMIT"},
    "MatchedLengths": {"tolerance": "TOLERANCE"},
    "RoutingVias": {"min via diameter": "MINWIDTH", "min diameter": "MINWIDTH",
                    "via diameter": "WIDTH", "preferred via diameter": "WIDTH",
                    "max via diameter": "MAXWIDTH", "max diameter": "MAXWIDTH",
                    "min via hole size": "MINHOLEWIDTH", "min hole": "MINHOLEWIDTH",
                    "via hole size": "HOLEWIDTH", "preferred via hole size": "HOLEWIDTH",
                    "max via hole size": "MAXHOLEWIDTH", "max hole": "MAXHOLEWIDTH"},
    "PlaneClearance": {"clearance": "CLEARANCE"},
    "HoleSize": {"min hole size": "MINLIMIT", "min": "MINLIMIT",
                 "max hole size": "MAXLIMIT", "max": "MAXLIMIT"},
    "Height": {"min height": "MINHEIGHT", "max height": "MAXHEIGHT",
               "pref height": "PREFHEIGHT", "preferred height": "PREFHEIGHT"},
}
_NUMBER = r"-?(?:\d+(?:\.\d*)?|\.\d+)(?:e[-+]?\d+)?"
_UNIT = r"mils?|mm|cm|um|inch|in|thou|th"
_LENGTH_RE = re.compile(rf"^\s*({_NUMBER})\s*({_UNIT})?\s*$", re.IGNORECASE)
# A label, '=' or ':', and a whole number with its unit: "1e1mil" and "10cm" are read
# whole, never as a leading "1" or "10" mil.
_LABEL_VALUE_RE = re.compile(
    rf"([A-Za-z][A-Za-z .]*?)\s*[=:]\s*({_NUMBER}\s*(?:{_UNIT})?)(?![A-Za-z0-9])",
    re.IGNORECASE)
_LAYER_FIELD_RE = re.compile(r"(TOPLAYER|BOTTOMLAYER|MIDLAYER\d+)_(MINWIDTH|MAXWIDTH|PREFWIDTH|"
                             r"MINGAP|MAXGAP|PREFGAP)$")
_LAYER_FIELDS = {"MINWIDTH": "min_width", "PREFWIDTH": "preferred_width",
                 "MAXWIDTH": "max_width", "MINGAP": "min_gap", "PREFGAP": "preferred_gap",
                 "MAXGAP": "max_gap"}


def _scope_calls(text: str | None) -> list[tuple[str, list[str]]]:
    """(function in lower case, arguments) of every query call in a rule scope, as
    pcb_rules' scope parser reads it; none for a scope it cannot parse."""
    try:
        node = parse_scope(text or "")
    except ScopeSyntaxError:
        return []
    calls: list[tuple[str, list[str]]] = []

    def walk(current) -> None:
        if current[0] == "call":
            calls.append((current[1], list(current[2])))
        elif current[0] == "not":
            walk(current[1])
        elif current[0] in ("and", "or", "xor"):
            walk(current[1])
            walk(current[2])

    walk(node)
    return calls


def _scope_names(text: str | None) -> set[str]:
    """Every name a scope's calls take (nets, classes, pairs), in lower case."""
    return {argument.lower() for _, arguments in _scope_calls(text) for argument in arguments}


def _single_net(text: str | None) -> str | None:
    """The net of a scope that is just InNet('name'), else None."""
    try:
        node = parse_scope(text or "")
    except ScopeSyntaxError:
        return None
    if node[0] == "call" and node[1] == "innet" and len(node[2]) == 1:
        return node[2][0]
    return None


# ---------- project type ----------

def _xml_outline(data: bytes) -> dict:
    """The root element and the counts of its direct children by tag: no decoding."""
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError as error:
        return {"readable": False, "error": type(error).__name__}
    counts = Counter(child.tag.split("}")[-1] for child in root)
    return {"readable": True, "root": root.tag.split("}")[-1],
            "top_level_elements": dict(counts.most_common(30))}


def _pcb_constraint_stream(pcb_path: str | None) -> dict | None:
    """The PcbDoc's ConstraintManager/Data stream: a length-prefixed UTF-16 text holding
    base64 of zlib data. Altium writes an empty one (a stub) in classic boards."""
    if not pcb_path or not Path(pcb_path).is_file():
        return None
    try:
        with olefile.OleFileIO(pcb_path) as ole:
            if not ole.exists("ConstraintManager/Data"):
                return None
            raw = ole.openstream("ConstraintManager/Data").read()
    except Exception:  # not an OLE file, or a damaged one: no evidence either way
        return None
    result: dict = {"stream_bytes": len(raw)}
    try:
        (size,) = struct.unpack_from("<I", raw, 0)
        text = raw[4:4 + size].decode("utf-16-le").rstrip("\x00")
        content = zlib.decompress(base64.b64decode(text))
    except (struct.error, UnicodeDecodeError, ValueError, zlib.error):
        result["decoded"] = False
        result["stub"] = len(raw) <= 32
        return result
    result["decoded"] = True
    result["content_bytes"] = len(content)
    result["stub"] = not content.strip()
    if content.strip().startswith(b"<"):
        result["outline"] = _xml_outline(content)
    return result


def _nonzero(value: str) -> bool:
    text = value.strip().lower()
    if text in ("true", "t", "yes"):
        return True
    try:
        return int(float(text)) != 0
    except ValueError:
        return False


def detect_project_type(prj_pcb_path: str | None, pcb_path: str | None = None) -> dict:
    """Classic or Constraint Manager: a Settings/Constraints file next to the .PrjPcb, or
    a Constraints.xml beside it, a .CstrDot constraints document, the .PrjPcb's [Design]
    ConstraintManagerFlow switch when it is not 0, or a ConstraintManager stream in the
    PcbDoc that is more than the empty stub classic boards carry. The switch written as 0
    makes the project classic whatever else is found (listed as contrary_evidence). Other
    .PrjPcb keys or sections naming the Constraint Manager are listed, not taken as
    evidence. A Constraints.xml is only outlined (root and element counts), not decoded."""
    evidence: list[dict] = []
    result: dict = {}
    root = Path(prj_pcb_path).parent if prj_pcb_path else None
    if root is not None:
        folder = root / "Settings" / "Constraints"
        files = sorted(folder.glob("*.xml")) if folder.is_dir() else []
        # Altium's message: "Schematic Constraint file Constraints.xml has been added to
        # the project"; where it is kept is not confirmed, so the project folder counts too.
        files += [path for path in sorted(root.glob("*.xml"))
                  if path.name.lower() == "constraints.xml"]
        files.sort(key=lambda path: path.name.lower() != "constraints.xml")
        for path in files:
            evidence.append({"source": "constraints_file",
                             "file": str(path.relative_to(root))})
        if files:
            try:
                data = files[0].read_bytes()[:50_000_000]
                result["constraints_xml"] = {"file": str(files[0].relative_to(root)),
                                             "bytes": files[0].stat().st_size,
                                             **_xml_outline(data)}
            except OSError as error:
                result["constraints_xml"] = {"file": str(files[0].relative_to(root)),
                                             "readable": False, "error": type(error).__name__}
        for path in sorted({*root.glob("*.CstrDot"), *(folder.glob("*.CstrDot")
                                                       if folder.is_dir() else [])}):
            evidence.append({"source": "constraints_document",
                             "file": str(path.relative_to(root))})
        options: list[dict] = []
        try:
            text = Path(prj_pcb_path).read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            text = ""
        section = ""
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                section = stripped[1:-1].strip()
                if "constraintmanager" in section.lower().replace(" ", ""):
                    options.append({"section": section})
                continue
            key, sep, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if not sep or "constraintmanager" not in key.lower().replace(" ", ""):
                continue
            if section.lower() == CM_FLOW_SECTION.lower() and key.lower() == CM_FLOW_KEY.lower():
                result["constraint_manager_flow"] = {"section": section, "key": key,
                                                     "value": value}
                if _nonzero(value):
                    evidence.append({"source": "project_option", "section": section,
                                     "key": key, "value": value})
            else:
                options.append({"section": section, "key": key, "value": value})
        if options:
            result["other_project_options"] = options[:10]
    stream = _pcb_constraint_stream(pcb_path)
    if stream is not None:
        result["pcb_constraint_stream"] = stream
        if stream.get("decoded") and not stream["stub"]:
            evidence.append({"source": "pcb_constraint_stream",
                             "content_bytes": stream["content_bytes"]})
    flow = result.get("constraint_manager_flow")
    if flow is not None and not _nonzero(flow["value"]) and evidence:
        # The project's own switch says classic: files or board data left from a
        # Constraint Manager trial do not make it one.
        result["contrary_evidence"] = evidence
        result["note"] = (f"[{flow['section']}] {flow['key']}={flow['value']} says classic; "
                          "the Constraint Manager traces in contrary_evidence are not used.")
        evidence = []
    result["type"] = "constraint_manager" if evidence else "classic"
    result["evidence"] = evidence
    return {"type": result.pop("type"), **result}


# ---------- options ----------

def _flag(value: str | None, default):
    if value is None:
        return default
    text = value.strip()
    if text.lower() in _TRUE:
        return True
    if text.lower() in ("0", "false", "f", "no"):
        return False
    return default


def class_generation(options) -> dict:
    """The project's Class Generation options decoded: project-wide switches and, per
    sheet document (by name), its component class, room, net class scope and structure
    class; Altium's defaults where a key is not written (listed in defaulted)."""
    project: dict = {}
    defaulted: list[str] = []
    written = getattr(options, "class_generation", {}) or {}
    for key, (name, default) in PROJECT_OPTIONS.items():
        if key in written:
            project[name] = _flag(written[key], default)
        else:
            project[name] = default
            defaulted.append(name)
    sheets: dict[str, dict] = {}
    names: dict[str, str] = {}
    for raw, fields in getattr(options, "sheet_class_generation", []) or []:
        stem = Path(raw.replace("\\", "/")).stem
        row = {}
        for key, (name, default) in SHEET_OPTIONS.items():
            if key == "ClassGenNCAutoScope":
                row[name] = (fields.get(key) or default).strip()
            else:
                row[name] = _flag(fields.get(key), default)
        sheets[stem.lower()] = row
        names[stem.lower()] = stem
    comparison = {}
    for row in getattr(options, "comparison", []) or []:
        if row.get("Kind", "").strip().lower() == "rule" and "InclAllRules" in row:
            comparison["include_rules_without_unique_id"] = _flag(row["InclAllRules"], False)
    result = {"project": project, "sheets": sheets, "sheet_names": names}
    if defaulted:
        result["defaulted"] = defaulted
    if comparison:
        result["comparator"] = comparison
    return result


def _generation_summary(generation: dict) -> dict:
    """class_generation for the result: the per-sheet options as the setting most sheets
    share, and the sheets that differ from it."""
    result = {key: value for key, value in generation.items()
              if key not in ("sheets", "sheet_names")}
    rows = generation["sheets"]
    if rows:
        combos = Counter(tuple(sorted(row.items())) for row in rows.values())
        common, count = combos.most_common(1)[0]
        sheets: dict = {"total": len(rows), "most_sheets": {**dict(common), "count": count}}
        others = [{"sheet": generation["sheet_names"].get(key, key), **row}
                  for key, row in rows.items() if tuple(sorted(row.items())) != common]
        if others:
            sheets["other_sheets"] = others[:LISTED]
        result["sheets"] = sheets
    return result


# Project Options > Comparator: how classes match by members when their names differ.
DEFAULT_MATCHING = {"net class": (75, 3), "component class": (75, 3),
                    "differential pair": (50, 1)}


def class_matching(options) -> dict[str, tuple[int, int]]:
    """(Min Match %, Min Matched Members) per object kind, the project's Comparator rows
    else Altium's defaults."""
    result = dict(DEFAULT_MATCHING)
    for row in getattr(options, "comparison", []) or []:
        kind = row.get("Kind", "").strip().lower()
        if kind in result:
            try:
                result[kind] = (int(row.get("MinPercent", result[kind][0])),
                                int(row.get("MinMatch", result[kind][1])))
            except ValueError:
                continue
    return result


def _sheet_option(generation: dict, sheet: str, name: str):
    row = generation["sheets"].get(sheet.lower())
    if row is None:
        return SHEET_OPTIONS[{"component_class": "ClassGenCCAutoEnabled",
                              "room": "ClassGenCCAutoRoomEnabled"}[name]][1]
    return row[name]


def _suffixes(options) -> list[tuple[str, str]]:
    """Polarity suffix pairs: Altium's _P / _N, then the project's own [DiffPairSuffixN]
    pairs (their keys are not documented: a key naming pos(itive) or neg(ative))."""
    pairs = [("_P", "_N")]
    for fields in getattr(options, "diff_pair_suffixes", []) or []:
        positive = next((v for k, v in fields.items() if "pos" in k.lower() and v), None)
        negative = next((v for k, v in fields.items() if "neg" in k.lower() and v), None)
        if positive and negative and (positive, negative) not in pairs:
            pairs.append((positive.strip(), negative.strip()))
    return pairs


# ---------- rule text ----------

def _length_mil(text: str) -> float | None:
    match = _LENGTH_RE.match(text or "")
    if not match:
        return None
    value = float(match.group(1))
    unit = (match.group(2) or "mil").lower()
    if unit == "mm":
        return value / 0.0254
    if unit == "cm":
        return value / 0.00254
    if unit == "um":
        return value / 25.4
    if unit in ("in", "inch"):
        return value * 1000.0
    return value


def _kind_of(text: str) -> str | None:
    words = re.sub(r"\s+", " ", text.strip().lower())
    if words in _RULE_KINDS:
        return _RULE_KINDS[words]
    known = {kind.lower(): kind for kind in {*_RULE_KINDS.values(), *_KEY_FIELDS}}
    return known.get(words.replace(" ", ""))


def decode_rule_text(text: str) -> dict:
    """A rule parameter's value as {kind, name, scope, properties (Rules6 keys), fields
    (compared values in mil), format}; format "properties" (KEY=value pairs with a
    RULEKIND), "description" (a rule type name and label = value pairs), or None when
    neither could be read."""
    raw = (text or "").replace("~2", "|").strip()
    result: dict = {"format": None, "kind": None, "name": None, "scope": None,
                    "properties": {}, "fields": {}}
    if re.search(r"\bRULEKIND\s*=", raw, re.IGNORECASE):
        separator = "|" if "|" in raw else (";" if ";" in raw else ",")
        properties: dict[str, str] = {}
        for token in raw.split(separator):
            key, sep, value = token.partition("=")
            if sep and key.strip():
                properties.setdefault(key.strip().upper().replace(" ", ""), value.strip())
        kind = _kind_of(properties.get("RULEKIND", "")) or properties.get("RULEKIND") or None
        result.update(format="properties", kind=kind, properties=properties,
                      name=properties.get("NAME") or None,
                      scope=properties.get("SCOPE1EXPRESSION") or None)
    else:
        head = re.split(r"[(:=\[]", raw, maxsplit=1)[0]
        kind = _kind_of(head)
        if kind is None:
            head = re.sub(r"\s*(constraint|rule)\s*$", "", head.strip(), flags=re.IGNORECASE)
            kind = _kind_of(head) or _kind_of(head + " constraint")
        if kind is None:
            return result
        labels = _LABEL_KEYS.get(kind, {})
        properties = {"RULEKIND": kind}
        for label, value in _LABEL_VALUE_RE.findall(raw[len(head):]):
            key = labels.get(re.sub(r"\s+", " ", label.strip().lower()).rstrip("."))
            if key and key not in properties:
                properties[key] = value.strip()
        result.update(format="description", kind=kind, properties=properties)
    fields: dict = {}
    keys = _KEY_FIELDS.get(result["kind"] or "", {})
    for key, value in result["properties"].items():
        if key in keys:
            mil = _length_mil(value)
            if mil is not None:
                fields.setdefault(keys[key], round(mil, 4))
            continue
        layer = _LAYER_FIELD_RE.match(key)
        if layer:
            mil = _length_mil(value)
            if mil is not None:
                fields.setdefault(f"{_LAYER_FIELDS[layer.group(2)]}@{layer.group(1)}",
                                  round(mil, 4))
    result["fields"] = fields
    return result


def _pcb_fields(rule: PcbRule, layers: dict[int, str]) -> dict[str, dict[str, float]]:
    """A PCB rule's compared values: field -> {layer name or "all": mil}."""
    values = rule_values(rule, layers)
    keys = _KEY_FIELDS.get(rule.kind, {})
    wanted = set(keys.values())
    flat: dict[str, dict[str, float]] = defaultdict(dict)
    for key, value in values.items():
        if key.endswith("_mil") and isinstance(value, (int, float)):
            field = key[:-4]
            if field in wanted:
                flat[field]["all"] = float(value)
    for layer, entry in (values.get("layers_mil") or {}).items():
        for field, value in entry.items():
            if field in wanted:
                flat[field][layer] = float(value)
    if rule.kind == "DiffPairsRouting":
        # Pair widths live only in per-layer keys; MINWIDTH etc. read as other_mil.
        for key, field in (("minwidth", "min_width"), ("prefwidth", "preferred_width"),
                           ("maxwidth", "max_width")):
            value = (values.get("other_mil") or {}).get(key)
            if value is not None and field not in flat:
                flat[field]["all"] = float(value)
    return dict(flat)


def _layer_name(slot: str, layers: dict[int, str]) -> str | None:
    if slot == "TOPLAYER":
        return layers.get(1)
    if slot == "BOTTOMLAYER":
        return layers.get(32)
    match = re.match(r"MIDLAYER(\d+)$", slot)
    return layers.get(int(match.group(1)) + 1) if match else None


def _compare_fields(fields: dict, pcb: dict[str, dict[str, float]],
                    layers: dict[int, str]) -> list[dict]:
    """The schematic values the PCB rule does not hold: [{field, schematic_mil, pcb_mil}]."""
    differences = []
    for key, value in sorted(fields.items()):
        field, _, slot = key.partition("@")
        held = pcb.get(field)
        if not held:
            continue
        if slot:
            layer = _layer_name(slot, layers)
            compared = {layer: held[layer]} if layer in held else (
                {"all": held["all"]} if "all" in held else {})
        else:
            compared = held
        off = {where: round(have, 4) for where, have in compared.items()
               if abs(have - value) > TOLERANCE_MIL}
        if off:
            row = {"field": field, "schematic_mil": value,
                   "pcb_mil": off["all"] if list(off) == ["all"] else off}
            if slot:
                row["layer"] = _layer_name(slot, layers) or slot
            differences.append(row)
    return differences


# ---------- the comparison ----------

class _Names:
    """Schematic names on the PCB: nets through check_sch_pcb_sync's matching (the loaded
    netlist's names; the offline compile's names reach them through their pins), parts by
    their linked PCB designator; else by name, exactly or ignoring case when only one PCB
    name fits."""

    def __init__(self, compiled, netlist: dict | None, index, net_matching: dict | None):
        pcb = index.pcb
        self.pcb_nets = set(pcb.nets)
        self.pcb_lower: dict[str, list[str]] = defaultdict(list)
        for net in pcb.nets:
            self.pcb_lower[net.lower()].append(net)
        try:
            maps = sync_maps(index, netlist, net_matching)
        except Exception:  # matching is a refinement: fall back to names
            maps = SyncMaps()
        self.net_map, self.part_map = maps.nets, maps.parts
        # Parts on no PCB part that Update PCB may not place (mechanical, graphical, no
        # footprint): no class on the PCB can hold them.
        self.unplaced = maps.unplaced
        self.loaded = set((netlist or {}).get("nets") or {})
        self.pin_to_net = (netlist or {}).get("pin_to_net") or {}
        self.compiled_pins = getattr(compiled, "nets", {}) or {}
        self.renamed: dict[str, str] = {}

    def loaded_name(self, net: str) -> str:
        if net in self.loaded or not self.pin_to_net:
            return net
        votes = Counter(self.pin_to_net.get(ref, {}).get(pin)
                        for ref, pin in self.compiled_pins.get(net, ()))
        votes.pop(None, None)
        return votes.most_common(1)[0][0] if votes else net

    def net(self, net: str, record: bool = True) -> str | None:
        loaded = self.loaded_name(net)
        found = self.net_map.get(loaded)
        if found is None:
            if loaded in self.pcb_nets:
                found = loaded
            elif net in self.pcb_nets:
                found = net
            else:
                folded = self.pcb_lower.get(net.lower(), [])
                found = folded[0] if len(folded) == 1 else None
        if record and found is not None and found != net:
            self.renamed[net] = found
        return found

    def part(self, designator: str) -> str:
        return self.part_map.get(designator, designator)


def _truthy(value: str) -> bool:
    return value.strip().lower() in ("true", "t", "1")


def _place(item) -> dict:
    return {"sheet": item.sheet, "x_mil": round(item.x_mil, 1), "y_mil": round(item.y_mil, 1),
            "via": item.via or "nothing"}


class _Report:
    def __init__(self, cm: bool):
        self.cm = cm
        self.findings: list[dict] = []

    def add(self, check: str, severity: str, kind: str, issue: str, **fields) -> None:
        if self.cm:
            fields["reason"] = "constraint_manager_project"
            severity = "info"
        self.findings.append({"check": check, "severity": severity, "kind": kind,
                              **fields, "issue": issue})


def _listed(values, limit: int = LISTED) -> list:
    values = sorted(values, key=lambda v: str(v).lower())
    return values[:limit]


def _best(candidates: list, wanted: set, members) -> object | None:
    """The candidate whose members overlap wanted most (first on a tie)."""
    if not candidates:
        return None
    return max(candidates, key=lambda c: (len(set(members(c)) & wanted),
                                          -candidates.index(c)))


class _Builder:
    def __init__(self, compiled, index, options, netlist, net_matching, project_type):
        self.facts = compiled.erc
        self.index = index
        self.pcb = index.pcb
        self.netlist = netlist or {}
        self.names = _Names(compiled, netlist, index, net_matching)
        self.generation = class_generation(options)
        self.suffixes = _suffixes(options)
        self.project_type = project_type or {"type": "classic", "evidence": []}
        self.cm = self.project_type.get("type") == "constraint_manager"
        self.report = _Report(self.cm)
        self.rows: list[dict] = []
        self.layers = index.signal_layers() if hasattr(index, "signal_layers") else {}
        self.net_classes = [c for c in self.pcb.classes if c.kind == "net" and not c.superclass]
        self.pair_classes = [c for c in self.pcb.classes
                             if c.kind == "diff_pair" and not c.superclass]
        self.component_classes = [c for c in self.pcb.classes if c.kind == "component"
                                  and not c.superclass and not c.auto_generated]
        self.rooms = [rule for rule in self.pcb.rules if rule.kind == ROOM_KIND]
        self.used_net_classes: set[int] = set()
        self.used_pairs: set[int] = set()
        self.used_rules: set[int] = set()
        self.no_net: list[dict] = []
        # ClassName directives on no net, per class (lower case): one moved off its wire
        # since the last Update PCB leaves its nets in the PCB class.
        self.no_net_classes: Counter = Counter()
        self.duplicates: set[str] = set()  # physical designators given to several parts
        self.matching = class_matching(options)
        # Names the schematic gives classes: a PCB class of one of these names is never
        # taken for another class by its members.
        self.reserved: set[str] = {
            value.strip().lower() for item in self.facts.parameter_sets
            for name, value in item.parameters if name.strip().lower() == "classname"
        } | {bus.name.lower() for bus in self.facts.buses}
        self.used_component_classes: set[int] = set()

    # ----- helpers -----

    def option(self, name: str):
        return self.generation["project"][name]

    def row(self, **fields) -> dict:
        self.rows.append(fields)
        return fields

    def classes_named(self, classes, name: str) -> tuple[list, bool]:
        exact = [c for c in classes if c.name == name]
        if exact:
            return exact, False
        return [c for c in classes if c.name.lower() == name.lower()], True

    def by_members(self, classes, expected: set, kind: str, used: set[int],
                   ignored: frozenset[str] = frozenset()):
        """The class Altium's comparator would match by members when no class has the
        name: identical members, or at least Min Matched Members shared that are at least
        Min Match % of the larger side; the one sharing most. None when none does.
        Members named in ignored (lower case) are not counted."""
        percent, least = self.matching[kind]
        best, most = None, 0
        for cls in classes:
            if id(cls) in used or cls.name.lower() in self.reserved:
                continue
            members = {m for m in cls.members if m.lower() not in ignored}
            shared = len(members & expected)
            size = max(len(members), len(expected))
            if not shared or not size:
                continue
            if members == expected or (shared >= least and 100.0 * shared / size >= percent):
                if shared > most:
                    best, most = cls, shared
        return best

    # ----- net classes (ClassName directives, buses) -----

    def net_class(self, name: str, nets: list[str], sheets: list[str], source: str,
                  enabled: bool, option: str, places: list[dict] | None = None) -> None:
        mapped = {net: self.names.net(net) for net in nets}
        expected = {pcb for pcb in mapped.values() if pcb}
        not_on_pcb = [net for net, pcb in mapped.items() if pcb is None]
        candidates, case_only = self.classes_named(
            [c for c in self.net_classes if id(c) not in self.used_net_classes], name)
        found = _best(candidates, expected, lambda c: c.members)
        row = {"kind": "net_class", "name": name, "source": source, "sheets": sheets,
               "nets": len(nets), "net_list": _listed(nets)}
        if places:
            row["places"] = places[:5]
        check = "directive_class_missing" if source == "directive" else "bus_class_missing"
        where = "directive" if source == "directive" else "bus"
        renamed = None
        if found is None and expected:
            renamed = found = self.by_members(self.net_classes, expected, "net class",
                                              self.used_net_classes)
        if found is None:
            if not enabled:
                row["status"] = "not_generated"
                self.row(**row, option_off=option)
                if source == "directive":
                    self.report.add(
                        check, "info", "net_class",
                        f"Net class {name} (ClassName directive on {len(nets)} nets) is not on "
                        f"the PCB, and Class Generation '{option}' is off: Update PCB does not "
                        "create it.", name=name, sheets=sheets, option_off=option)
                return
            row["status"] = "missing"
            self.row(**row)
            severity = "warning" if source == "directive" else "info"
            self.report.add(
                check, severity, "net_class",
                f"Net class {name} ({where} on {len(nets)} nets) is not on the PCB: Update PCB "
                "would add it.", name=name, sheets=sheets, nets=_listed(nets),
                **({"nets_not_on_pcb": _listed(not_on_pcb)} if not_on_pcb else {}))
            return
        self.used_net_classes.add(id(found))
        members = set(found.members)
        missing = expected - members
        extra = members - expected
        row["pcb_name"] = found.name
        # With the kind left out by Class Generation, Update PCB leaves the PCB class as
        # it is: a difference is info, and nothing would change it.
        off = {} if enabled else {"option_off": option}
        if case_only and renamed is None:
            row["name_case_differs"] = True
        if renamed is not None:
            row["matched_by"] = "members"
            self.report.add(
                "class_name_differs",
                "warning" if source == "directive" and enabled else "info", "net_class",
                f"Net class {name} ({where}) is {found.name} on the PCB, matched by its "
                f"members ({len(members & expected)} shared): "
                + ("Update PCB would rename it, and rules naming the old class would match "
                   "nothing." if enabled else
                   f"Class Generation '{option}' is off, so Update PCB does not rename it."),
                name=name, pcb_name=found.name, sheets=sheets, source=source, **off)
        if missing or extra:
            row["status"] = "differs"
            row["pcb_members_missing"] = len(missing)
            row["pcb_members_extra"] = len(extra)
            severity = "warning" if missing and source == "directive" and enabled else "info"
            parts = []
            if missing:
                parts.append(f"{len(missing)} of its nets are not in the PCB class")
            if extra:
                parts.append(f"the PCB class has {len(extra)} nets no {where} gives it")
            floating = self.no_net_classes.get(name.lower(), 0) if source == "directive" else 0
            hint = {}
            if extra and floating:
                hint["directives_of_class_on_no_net"] = floating
                parts.append(f"{floating} ClassName directives of this class sit on no net "
                             "(moved off their wire since the last Update PCB?)")
            self.report.add(
                "class_members_differ", severity, "net_class",
                f"Net class {name}: " + "; ".join(parts)
                + (" (Update PCB would make them match)." if enabled else
                   f" (Class Generation '{option}' is off, so Update PCB does not change "
                   "the class)."),
                name=name, pcb_name=found.name, sheets=sheets,
                missing_from_pcb_class=_listed(missing), extra_on_pcb=_listed(extra),
                source=source, **off, **hint)
        else:
            row["status"] = "differs" if renamed is not None else "transferred"
        if not_on_pcb:
            row["nets_not_on_pcb"] = _listed(not_on_pcb)
        self.row(**row, **off)

    # ----- directives -----

    def directives(self) -> None:
        classes: dict[str, dict] = {}
        pairs: dict[tuple[str, str], dict] = {}
        pair_classes: dict[str, dict] = {}
        rules: list[tuple] = []
        for item in self.facts.parameter_sets:
            params = [(name.strip(), value) for name, value in item.parameters]
            lowered = {name.lower(): value for name, value in params}
            is_pair = _truthy(lowered.get("differentialpair", ""))
            class_name = (lowered.get("classname") or "").strip()
            rule_params = [(index, value) for index, (name, value) in enumerate(params)
                           if re.fullmatch(r"rule\d*", name.lower())]
            if not (is_pair or class_name or rule_params):
                continue
            if not item.nets:
                place = {**_place(item), "parameters": [n for n, _ in params][:6]}
                if place not in self.no_net:  # one per place, not per sheet copy
                    self.no_net.append(place)
                    if class_name and not is_pair:
                        self.no_net_classes[class_name.lower()] += 1
                continue
            place = _place(item)
            if class_name and not is_pair:
                held = classes.setdefault(class_name.lower(), {
                    "name": class_name, "nets": {}, "sheets": {}, "places": {}})
                held["nets"].update(dict.fromkeys(item.nets))
                held["sheets"][item.sheet] = None
                held["places"][(item.sheet, place["x_mil"], place["y_mil"])] = place
            if is_pair:
                for net in item.nets:
                    leg = self.leg(net, item.room)
                    if leg is None:
                        held = pairs.setdefault(("", net), {"name": net, "legs": {},
                                                            "sheets": {}, "unnamed": True})
                        held["legs"]["?"] = net
                        held["sheets"][item.sheet] = None
                        continue
                    base, polarity = leg
                    held = pairs.setdefault((base.lower(), ""), {"name": base, "legs": {},
                                                                 "sheets": {}})
                    held["legs"].setdefault(polarity, net)
                    held["sheets"][item.sheet] = None
                    if class_name:
                        group = pair_classes.setdefault(class_name.lower(), {
                            "name": class_name, "pairs": {}, "sheets": {}})
                        group["pairs"][base.lower()] = base
                        group["sheets"][item.sheet] = None
            for index, value in rule_params:
                ids = item.parameter_ids if index < len(item.parameter_ids) else []
                rules.append((item, value, ids[index] if ids else "", class_name, is_pair))
        enabled = self.option("user_defined_net_classes")
        for held in sorted(classes.values(), key=lambda h: h["name"].lower()):
            self.net_class(held["name"], list(held["nets"]), sorted(held["sheets"]),
                           "directive", enabled, "Generate user-defined net classes",
                           list(held["places"].values()))
        for held in sorted(pairs.values(), key=lambda h: h["name"].lower()):
            self.pair(held)
        for held in sorted(pair_classes.values(), key=lambda h: h["name"].lower()):
            self.pair_class(held)
        for item, value, parameter_id, class_name, is_pair in rules:
            self.rule(item, value, parameter_id, class_name, is_pair)

    def leg(self, net: str, room: str) -> tuple[str, str] | None:
        """(pair name, P or N) from a leg's net name and the project's suffixes; a channel
        copy's room suffix is set aside and put back on the pair name."""
        name, channel = net, ""
        if room and name.lower().endswith(f"_{room.lower()}"):
            name, channel = name[:-len(room) - 1], name[-len(room) - 1:]
        for positive, negative in self.suffixes:
            for suffix, polarity in ((positive, "P"), (negative, "N")):
                if len(name) > len(suffix) and name.upper().endswith(suffix.upper()):
                    return name[:-len(suffix)] + channel, polarity
        return None

    def pair(self, held: dict) -> None:
        name, legs, sheets = held["name"], held["legs"], sorted(held["sheets"])
        row = {"kind": "diff_pair", "name": name, "source": "directive", "sheets": sheets,
               "positive": legs.get("P"), "negative": legs.get("N")}
        pcb_pairs = self.pcb.diff_pairs
        if held.get("unnamed"):
            row.update(status="missing", positive=None, negative=None, net=name)
            self.row(**row)
            self.report.add(
                "diff_pair_missing", "warning", "diff_pair",
                f"Net {name} carries a Differential Pair directive but its name ends in no "
                f"polarity suffix ({', '.join(p + '/' + n for p, n in self.suffixes)}): no "
                "pair is made from it.", name=name, sheets=sheets)
            return
        positive = self.names.net(legs["P"]) if "P" in legs else None
        negative = self.names.net(legs["N"]) if "N" in legs else None
        by_nets = {(p.positive_net, p.negative_net): p for p in pcb_pairs}
        found = by_nets.get((positive, negative)) if positive and negative else None
        if found is not None:
            self.used_pairs.add(id(found))
            row.update(status="transferred", pcb_name=found.name)
            if found.name.lower() != name.lower():
                # The comparator matches the pair by its nets; Update PCB renames it.
                row["status"] = "differs"
                self.report.add(
                    "diff_pair_name_differs", "info", "diff_pair",
                    f"Differential pair {name} ({legs['P']} / {legs['N']}) is {found.name} on "
                    "the PCB, the same nets under another name: Update PCB would rename it, "
                    "and rules naming the old pair would match nothing.", name=name,
                    pcb_name=found.name, sheets=sheets, positive=legs["P"],
                    negative=legs["N"])
            self.row(**row)
            return
        named = [p for p in pcb_pairs if p.name.lower() == name.lower()]
        using = [p for p in pcb_pairs
                 if {p.positive_net, p.negative_net} & {positive, negative} - {None}]
        if "P" not in legs or "N" not in legs:
            have, lacking = ("positive", "negative") if "P" in legs else ("negative", "positive")
            other = named[0] if named else (using[0] if using else None)
            if other is not None:
                self.used_pairs.add(id(other))
            row.update(status="differs" if other else "missing",
                       **({"pcb_name": other.name} if other else {}))
            self.row(**row)
            self.report.add(
                "diff_pair_missing" if other is None else "diff_pair_nets_differ",
                "warning" if other is None else "info", "diff_pair",
                f"Differential pair {name}: only the {have} net carries the directive (ERC: "
                f"Missing {lacking.title()} Net), so Update PCB does not make the pair"
                + (f"; the PCB has pair {other.name}." if other else "."),
                name=name, sheets=sheets, **{have: legs.get("P") or legs.get("N")})
            return
        other = None
        if (negative, positive) in by_nets:
            other = by_nets[(negative, positive)]
            issue = (f"Differential pair {name}: the PCB pair {other.name} has the legs "
                     "swapped (positive and negative nets the other way round).")
        elif named or using:
            other = (named or using)[0]
            issue = (f"Differential pair {name} ({legs['P']} / {legs['N']}) is {other.name} "
                     f"on the PCB with nets {other.positive_net} / {other.negative_net}: "
                     "Update PCB would change it.")
        if other is not None:
            self.used_pairs.add(id(other))
            row.update(status="differs", pcb_name=other.name,
                       pcb_positive=other.positive_net, pcb_negative=other.negative_net)
            self.row(**row)
            self.report.add("diff_pair_nets_differ", "warning", "diff_pair", issue,
                            name=name, sheets=sheets, positive=legs["P"], negative=legs["N"],
                            pcb_name=other.name, pcb_positive=other.positive_net,
                            pcb_negative=other.negative_net)
            return
        absent = [net for net, pcb in ((legs["P"], positive), (legs["N"], negative))
                  if pcb is None]
        row["status"] = "missing"
        if absent:
            row["nets_not_on_pcb"] = absent
        self.row(**row)
        self.report.add(
            "diff_pair_missing", "warning", "diff_pair",
            f"Differential pair {name} ({legs['P']} / {legs['N']}) is not on the PCB: Update "
            "PCB would add it" + (f" (nets not on the PCB yet: {', '.join(absent)})."
                                  if absent else "."),
            name=name, sheets=sheets, positive=legs["P"], negative=legs["N"])

    def pair_class(self, held: dict) -> None:
        name, sheets = held["name"], sorted(held["sheets"])
        pcb_names = {}
        for base in held["pairs"].values():
            match = next((row.get("pcb_name") for row in self.rows
                          if row["kind"] == "diff_pair" and row["name"] == base), None)
            pcb_names[base] = match or base
        expected = set(pcb_names.values())
        row = {"kind": "diff_pair", "name": name, "source": "directive_pair_class",
               "sheets": sheets, "pairs": len(expected)}
        # Assumed (no project read carries one): a class from a directive follows Class
        # Generation's user-defined net classes switch, as a ClassName net class does.
        option = "Generate user-defined net classes"
        enabled = self.option("user_defined_net_classes")
        off = {} if enabled else {"option_off": option}
        candidates, _ = self.classes_named(self.pair_classes, name)
        found = _best(candidates, expected, lambda c: c.members)
        if found is None:
            nets_class, _ = self.classes_named(self.net_classes, name)
            if nets_class:
                self.used_net_classes.add(id(nets_class[0]))
                row.update(status="differs", pcb_net_class=nets_class[0].name)
                self.row(**row, **off)
                self.report.add(
                    "directive_class_missing", "info", "diff_pair",
                    f"Differential pair class {name} (ClassName on Differential Pair "
                    f"directives) is not on the PCB; a net class {nets_class[0].name} is.",
                    name=name, sheets=sheets, **off)
                return
            if not enabled:
                self.row(**row, status="not_generated", **off)
                self.report.add(
                    "directive_class_missing", "info", "diff_pair",
                    f"Differential pair class {name} (ClassName on Differential Pair "
                    f"directives, {len(expected)} pairs) is not on the PCB, and Class "
                    f"Generation '{option}' is off: Update PCB does not create it (assumed: "
                    "the switch is taken to cover pair classes too).", name=name,
                    sheets=sheets, **off)
                return
            row["status"] = "missing"
            self.row(**row)
            self.report.add(
                "directive_class_missing", "warning", "diff_pair",
                f"Differential pair class {name} (ClassName on Differential Pair directives, "
                f"{len(expected)} pairs) is not on the PCB.", name=name, sheets=sheets,
                pairs=_listed(expected))
            return
        members = set(found.members)
        missing, extra = expected - members, members - expected
        row["pcb_name"] = found.name
        if missing or extra:
            row["status"] = "differs"
            self.report.add(
                "class_members_differ", "warning" if missing and enabled else "info",
                "diff_pair",
                f"Differential pair class {name}: {len(missing)} pairs missing from the PCB "
                f"class, {len(extra)} PCB members no directive gives it"
                + ("." if enabled else f" (Class Generation '{option}' is off, so Update "
                   "PCB does not change the class)."), name=name,
                sheets=sheets, missing_from_pcb_class=_listed(missing),
                extra_on_pcb=_listed(extra), **off)
        else:
            row["status"] = "transferred"
        self.row(**row, **off)

    def rule(self, item, value: str, parameter_id: str, class_name: str, is_pair: bool) -> None:
        decoded = decode_rule_text(value)
        nets = list(item.nets)
        mapped = [pcb for pcb in (self.names.net(net) for net in nets) if pcb]
        row = {"kind": "rule", "name": decoded["name"] or decoded["kind"] or "Rule",
               "source": "directive", "sheets": [item.sheet], "rule_kind": decoded["kind"],
               "format": decoded["format"], "nets": len(nets), "net_list": _listed(nets),
               "values_mil": decoded["fields"], "places": [_place(item)]}
        if decoded["kind"] is None:
            row["status"] = "not_decoded"
            row["text"] = value[:200]
            self.row(**row)
            self.report.add(
                "rule_text_not_decoded", "info", "rule",
                f"A Rule parameter on {len(nets)} nets could not be read: compare it with the "
                "PCB in Altium.", sheets=[item.sheet], text=value[:200], nets=_listed(nets))
            return
        ids = {uid.upper() for uid in (parameter_id, item.unique_id) if uid}
        names = {n.lower() for n in mapped} | {n.lower() for n in nets}
        if class_name:
            names.add(class_name.lower())
        if is_pair:
            for net in nets:
                leg = self.leg(net, item.room)
                if leg is not None:
                    names.add(leg[0].lower())
        candidates = [rule for rule in self.pcb.rules if rule.kind == decoded["kind"]]
        matched = [rule for rule in candidates
                   if rule.unique_id and rule.unique_id.upper() in ids]
        if not matched and decoded["name"]:
            matched = [rule for rule in candidates
                       if rule.name.lower() == decoded["name"].lower()]
        # A directive covering several nets (a bus, a harness, a blanket) or placed on
        # several sheet copies may give one rule per net: every schematic-defined rule of
        # the kind whose scope names one of its nets, its class or its pair is its own.
        held = {id(rule) for rule in matched}
        matched += [rule for rule in candidates if id(rule) not in held and rule.from_schematic
                    and _scope_names(rule.scope1) & names]
        found = next((rule for rule in matched if id(rule) not in self.used_rules),
                     matched[0] if matched else None)
        if found is None:
            row["status"] = "missing"
            self.row(**row)
            self.report.add(
                "directive_rule_missing", "warning", "rule",
                f"{decoded['kind']} rule from a Rule parameter on {len(nets)} nets "
                f"({', '.join(_listed(nets, 3))}) is not on the PCB: Update PCB would add "
                "it.", sheets=[item.sheet], rule_kind=decoded["kind"], name=decoded["name"],
                nets=_listed(nets))
            return
        self.used_rules.update(id(rule) for rule in matched)
        row["pcb_name"] = found.name
        row["pcb_scope"] = found.scope1
        if len(matched) > 1:
            row["pcb_rules"] = _listed((rule.name for rule in matched), 10)
        differences = []
        for each in [found, *(rule for rule in matched if rule is not found)]:
            for difference in _compare_fields(decoded["fields"], _pcb_fields(each, self.layers),
                                              self.layers):
                differences.append({**difference, **({"pcb_rule": each.name}
                                                     if len(matched) > 1 else {})})
        if differences:
            row["status"] = "differs"
            row["differences"] = differences[:LISTED]
            self.row(**row)
            self.report.add(
                "rule_values_differ", "warning", "rule",
                f"{found.kind} rule {found.name}: "
                + "; ".join(f"{d['field']} {d['schematic_mil']} mil on the schematic, "
                            f"{d['pcb_mil']} mil on the PCB"
                            + (f" ({d['pcb_rule']})" if "pcb_rule" in d else "")
                            for d in differences[:4]) + ".",
                sheets=[item.sheet], name=found.name, rule_kind=found.kind,
                differences=differences[:LISTED])
        else:
            row["status"] = "transferred"
            self.row(**row)

    # ----- buses -----

    def buses(self) -> None:
        held: dict[str, dict] = {}
        for bus in self.facts.buses:
            entry = held.setdefault(bus.name.lower(), {"name": bus.name, "nets": {},
                                                       "sheets": {}})
            entry["nets"].update(dict.fromkeys(bus.nets))
            entry["sheets"][bus.sheet] = None
        enabled = self.option("bus_net_classes")
        for entry in sorted(held.values(), key=lambda e: e["name"].lower()):
            if not entry["nets"]:
                continue
            self.net_class(entry["name"], list(entry["nets"]), sorted(entry["sheets"]), "bus",
                           enabled, "Generate net classes for buses")

    # ----- component classes and rooms -----

    def component_classes_and_rooms(self) -> None:
        facts = self.facts
        instances = list(getattr(facts, "instances", []) or [])
        flat = bool(instances) and all(inst.depth == 0 for inst in instances)
        copies_of: dict[tuple[str, str], set[int]] = defaultdict(set)
        parts_of: dict[int, list[tuple[str, str]]] = defaultdict(list)
        for part in facts.parts:
            if part.physical and not part.masked:
                key = (part.physical, part.designator)
                copies_of[key].add(part.copy)
                if key not in parts_of[part.copy]:
                    parts_of[part.copy].append(key)
        drawn: dict[str, set[str]] = defaultdict(set)
        for physical, designator in copies_of:
            drawn[physical].add(designator)
        self.duplicates = {physical for physical, names in drawn.items() if len(names) > 1}
        generated: list[tuple[str, str, list[str], bool, bool]] = []
        for inst in instances:
            if inst.depth == 0 and not flat:
                continue  # the top sheet of a hierarchy gets no class
            name = inst.sheet if inst.depth == 0 else (inst.room or inst.level)
            # A multi-part component whose parts sit on several sheet copies is in none.
            parts = list(dict.fromkeys(key[0] for key in parts_of.get(inst.copy, [])
                                       if len(copies_of[key]) == 1))
            # A sheet none of whose parts can reach the PCB gets no class.
            if not name or all(part in self.names.unplaced for part in parts):
                continue
            generated.append((name, inst.sheet, parts,
                              bool(_sheet_option(self.generation, inst.sheet, "component_class")),
                              bool(_sheet_option(self.generation, inst.sheet, "room"))))
        used = self.used_component_classes
        self.reserved |= {name.lower() for name, *_ in generated}
        self.reserved |= {
            ((meta or {}).get("params") or {}).get("classname", "").strip().lower()
            for meta in (self.netlist.get("components") or {}).values()} - {""}
        room_classes = {argument.lower() for rule in self.rooms
                        for function, arguments in _scope_calls(rule.scope1)
                        if function == "incomponentclass" for argument in arguments}
        # (schematic class name, its PCB class name when matched under another name,
        # sheet, Generate Rooms on)
        expected_rooms: list[tuple[str, str | None, str, bool]] = []
        for name, sheet, parts, enabled, rooms in generated:
            found = self.component_class(name, [sheet], parts, "sheet", enabled,
                                         f"Component Classes (sheet {sheet})", used)
            if enabled:
                expected_rooms.append((name, found, sheet, rooms))
        manual: dict[str, dict] = {}
        for designator, meta in (self.netlist.get("components") or {}).items():
            name = ((meta or {}).get("params") or {}).get("classname", "").strip()
            if name:
                entry = manual.setdefault(name.lower(), {"name": name, "parts": [],
                                                         "sheets": {}})
                entry["parts"].append(designator)
                entry["sheets"][(meta or {}).get("sheet") or ""] = None
        enabled = self.option("user_defined_component_classes")
        manual_rooms = self.option("user_defined_component_class_rooms")
        for entry in sorted(manual.values(), key=lambda e: e["name"].lower()):
            sheets = sorted(s for s in entry["sheets"] if s)
            if all(part in self.names.unplaced for part in entry["parts"]):
                continue  # no part of it can reach the PCB: no class is made
            found = self.component_class(entry["name"], sheets, entry["parts"],
                                         "component_parameter", enabled,
                                         "Generate user-defined component classes", used)
            if enabled:
                expected_rooms.append((entry["name"], found, ", ".join(sheets[:3]),
                                       manual_rooms))

        def has_room(name: str, pcb_name: str | None) -> bool:
            return any(each.lower() in room_classes for each in (name, pcb_name) if each)

        any_generated_room = any(has_room(name, pcb_name)
                                 for name, pcb_name, _, on in expected_rooms if on)
        absent = [(name, sheet) for name, pcb_name, sheet, on in expected_rooms
                  if not self.room(name, pcb_name, sheet, on, room_classes,
                                   any_generated_room)]
        if absent and not any_generated_room:
            self.report.add(
                "room_missing", "info", "room",
                f"Generate Rooms is on for {len(absent)} component classes, but the board has "
                "none of their rooms: Update PCB would add them (clear Generate Rooms in "
                "Project Options > Class Generation if they are not wanted).",
                classes=_listed(name for name, _ in absent), count=len(absent),
                sheets=sorted({sheet for _, sheet in absent if sheet}))

    def component_class(self, name: str, sheets: list[str], parts: list[str], source: str,
                        enabled: bool, option: str, used: set[int]) -> str | None:
        # Parts Update PCB may not place (check_sch_pcb_sync reports them) are listed, not
        # compared: a PCB class may hold them or not (both seen), so they are neither
        # expected nor extra.
        left_out = [p for p in parts if p in self.names.unplaced]
        ignored = {p.lower() for p in left_out}
        expected = {self.names.part(p) for p in parts if p not in self.names.unplaced}
        candidates = [c for c in self.classes_named(self.component_classes, name)[0]
                      if id(c) not in used]
        found = _best(candidates, expected, lambda c: c.members)
        row = {"kind": "component_class", "name": name, "source": source, "sheets": sheets,
               "parts": len(parts) - len(left_out)}
        if left_out:
            row["parts_not_on_pcb"] = _listed(left_out)
        off = {} if enabled else {"option_off": option}
        renamed = None
        if found is None:
            renamed = found = self.by_members(self.component_classes, expected,
                                              "component class", used, frozenset(ignored))
        if found is None:
            if not enabled:
                self.row(**row, status="not_generated", option_off=option)
                return None
            self.row(**row, status="missing")
            self.report.add(
                "component_class_missing", "warning", "component_class",
                f"Component class {name} ({len(expected)} parts"
                + (f" of sheet {sheets[0]}" if source == "sheet" and sheets else "")
                + f") is not on the PCB, and '{option}' is on: Update PCB would add it.",
                name=name, sheets=sheets,
                parts=_listed(p for p in parts if p not in self.names.unplaced),
                source=source)
            return None
        used.add(id(found))
        members = {m for m in found.members if m.lower() not in ignored}
        missing, extra = expected - members, members - expected
        row["pcb_name"] = found.name
        if renamed is not None:
            row["matched_by"] = "members"
            row["status"] = "differs"
            self.report.add(
                "class_name_differs", "info", "component_class",
                f"Component class {name} is {found.name} on the PCB, matched by its members "
                f"({len(members & expected)} shared): "
                + ("Update PCB would rename it (rooms and rules naming the old class would "
                   "match nothing)." if enabled else
                   f"'{option}' is off, so Update PCB does not rename it."),
                name=name, pcb_name=found.name, sheets=sheets, source=source, **off)
        if missing or extra:
            row.update(status="differs", pcb_members_missing=len(missing),
                       pcb_members_extra=len(extra))
            doubled = sorted(p for p in missing if p in self.duplicates)
            fields = {"duplicate_designators": doubled} if doubled else {}
            pieces = []
            if missing:
                pieces.append(f"{len(missing)} of its parts are not in the PCB class")
            if extra:
                pieces.append(f"the PCB class has {len(extra)} parts the schematic does not "
                              "give it")
            self.report.add(
                "class_members_differ",
                "warning" if missing - set(doubled) and enabled else "info",
                "component_class",
                f"Component class {name}: " + "; ".join(pieces)
                + (f" ({len(doubled)} of those designators name several parts on the "
                   "sheets: run_erc reports Duplicate Part Designators)" if doubled else "")
                + (" (Update PCB would make them match)." if enabled else
                   f" ('{option}' is off, so Update PCB does not change the class)."),
                name=name, pcb_name=found.name, sheets=sheets,
                missing_from_pcb_class=_listed(missing), extra_on_pcb=_listed(extra),
                source=source, **fields, **off)
        else:
            row["status"] = "differs" if renamed is not None else "transferred"
        self.row(**row, **off)
        return found.name

    def room(self, name: str, pcb_name: str | None, sheet: str, enabled: bool,
             room_classes: set[str], others: bool) -> bool:
        """The room of a generated component class, named after the schematic's class
        (pcb_name: the PCB class it was matched to under another name, whose room counts
        too); False when it is missing although Generate Rooms is on (a finding of its own
        when the board has other such rooms, else one finding for all of them)."""
        names = {each.lower() for each in (name, pcb_name) if each}
        present = bool(names & room_classes) or any(
            rule.name.lower() in names for rule in self.rooms)
        row = {"kind": "room", "name": name, "source": "class_generation",
               "sheets": [sheet] if sheet else [], "component_class": name}
        if pcb_name and pcb_name.lower() != name.lower():
            row["pcb_component_class"] = pcb_name
        if present:
            self.row(**row, status="transferred")
            return True
        if not enabled:
            self.row(**row, status="not_generated", option_off="Generate Rooms")
            return True
        self.row(**row, status="missing")
        if others:
            self.report.add(
                "room_missing", "warning", "room",
                f"No room holds component class {name}, though Generate Rooms is on for it "
                "and the board has the rooms of other classes: Update PCB would add one.",
                name=name, sheets=row["sheets"])
        return False

    # ----- what the PCB holds that no directive gives -----

    def stale(self, kinds_defined: Counter, power_nets: set[str]) -> dict:
        counts = Counter()
        for rule in self.pcb.rules:
            if not rule.from_schematic or id(rule) in self.used_rules:
                continue
            target = _single_net(rule.scope1)
            if rule.kind == "SupplyNets" and target and target.lower() in power_nets:
                counts["supply_net_rules_from_power_ports"] += 1
                continue
            counts["rules"] += 1
            self.report.add(
                "stale_pcb_item", "info", "rule",
                f"{rule.kind} rule {rule.name} is marked as defined by the schematic, but no "
                "directive or power port gives it now: Update PCB would remove it.",
                name=rule.name, rule_kind=rule.kind, scope=rule.scope1, item="rule")
        if kinds_defined["diff_pair"]:
            for pair in self.pcb.diff_pairs:
                if id(pair) in self.used_pairs:
                    continue
                counts["pairs"] += 1
                self.report.add(
                    "stale_pcb_item", "info", "diff_pair",
                    f"Differential pair {pair.name} ({pair.positive_net} / "
                    f"{pair.negative_net}) has no Differential Pair directive: it was defined "
                    "on the PCB, or its directive was removed (Update PCB may remove it).",
                    name=pair.name, item="diff_pair")
        if kinds_defined["net_class"]:
            for cls in self.net_classes:
                if id(cls) in self.used_net_classes:
                    continue
                counts["net_classes"] += 1
                floating = self.no_net_classes.get(cls.name.lower(), 0)
                self.report.add(
                    "stale_pcb_item", "info", "net_class",
                    f"Net class {cls.name} ({len(cls.members)} nets) comes from no ClassName "
                    "directive on a net or bus: "
                    + (f"{floating} ClassName directives of this name sit on no net (moved "
                       "off their wire since the last Update PCB?)." if floating else
                       "defined on the PCB, generated otherwise (harness, component net "
                       "classes), or its directive was removed."),
                    name=cls.name, item="net_class",
                    **({"directives_of_class_on_no_net": floating} if floating else {}))
        return dict(counts)


def _power_nets(facts, names: _Names) -> set[str]:
    """PCB names (lower case) of the nets that carry a power port."""
    found = set()
    for net in getattr(facts, "nets", []) or []:
        if any(obj.kind == "power" for obj in net.objects):
            name = net.netlist_name or net.name
            if not name:
                continue
            pcb = names.net(name, record=False)
            found.add((pcb or name).lower())
    return found


def build_directive_sync(compiled, index, options, netlist: dict | None = None,
                         net_matching: dict | None = None,
                         project_type: dict | None = None) -> dict:
    """The whole comparison: rows for every schematic item, findings, summary per kind."""
    if compiled is None or getattr(compiled, "erc", None) is None:
        return {"error": "no_netlist",
                "message": "The sheets could not be compiled offline, so the directives "
                           "are not known."}
    builder = _Builder(compiled, index, options, netlist, net_matching, project_type)
    builder.directives()
    builder.buses()
    builder.component_classes_and_rooms()
    defined = Counter(row["kind"] for row in builder.rows if row.get("source") == "directive")
    stale = builder.stale(defined, _power_nets(compiled.erc, builder.names))
    rows = sorted(builder.rows, key=lambda r: (KINDS.index(r["kind"]),
                                               _STATUS_RANK.get(r["status"], 9),
                                               str(r["name"]).lower()))
    findings = sorted(builder.report.findings, key=lambda f: (
        _SEVERITY_RANK[f["severity"]], KINDS.index(f["kind"]), CHECKS.index(f["check"]),
        str(f.get("name") or f["issue"]).lower()))
    summary: dict = {}
    for kind in KINDS:
        held = [row for row in rows if row["kind"] == kind]
        statuses = Counter(row["status"] for row in held)
        entry = {"items": len(held), "transferred": statuses["transferred"],
                 "missing": statuses["missing"], "differing": statuses["differs"],
                 "disabled_by_options": statuses["not_generated"],
                 # Items Parameter Set directives define (not buses, sheets or parts).
                 "from_directives": sum(1 for row in held
                                        if row["source"].startswith("directive"))}
        if statuses["not_decoded"]:
            entry["not_decoded"] = statuses["not_decoded"]
        sources = Counter(row["source"] for row in held)
        if sources:
            entry["by_source"] = dict(sources)
        summary[kind] = entry
    summary["parameter_sets"] = len(compiled.erc.parameter_sets)
    if builder.cm:
        summary["directives_ignored_unless_imported"] = sum(
            1 for row in rows if row.get("source", "").startswith("directive"))
    if builder.no_net:
        summary["directives_on_no_net"] = len(builder.no_net)
    if stale:
        summary["pcb_items_without_directive"] = stale
    if builder.names.renamed:
        summary["nets_renamed_on_pcb"] = len(builder.names.renamed)
    severities = Counter(f["severity"] for f in findings)
    notes = []
    if builder.cm:
        notes.append(CM_NOTE)
    if any(row["kind"] == "rule" for row in rows):
        notes.append(RULE_TEXT_NOTE)
    if builder.no_net:
        notes.append(f"{len(builder.no_net)} directives sit on no net (floating, or on a sheet "
                     "symbol): run_erc reports floating ones.")
    if builder.names.renamed:
        notes.append("Nets renamed on the PCB are followed through check_sch_pcb_sync's "
                     "matching; pcb names are given where they differ.")
    not_checked = list(NOT_CHECKED)
    scopes = sorted({row["net_classes"] for row in builder.generation["sheets"].values()
                     if row["net_classes"].lower() != "none"})
    if scopes:
        not_checked.insert(0, f"Per-sheet net classes ({', '.join(scopes)}) are on for some "
                              "sheets and not compared.")
    return {
        "project_type": builder.project_type,
        "in_sync": not any(f["severity"] in ("error", "warning") for f in findings),
        "summary": summary,
        "severity_counts": {level: severities.get(level, 0)
                            for level in ("error", "warning", "info")},
        "class_generation": _generation_summary(builder.generation),
        "differential_pair_suffixes": [{"positive": p, "negative": n}
                                       for p, n in builder.suffixes],
        "rows": rows,
        "findings": findings,
        "directives_on_no_net": builder.no_net[:LISTED],
        "renamed_nets": dict(sorted(builder.names.renamed.items())[:LISTED]),
        "notes": notes,
        "not_checked": not_checked,
    }


def build_directive_sync_cached(compiled, index, options, netlist=None, net_matching=None,
                                project_type=None) -> dict:
    """build_directive_sync cached on the index for this compile and netlist (the last
    CACHE_SIZE), so paging reuses it: treat the result as read-only."""
    cache = index.__dict__.setdefault("_directive_sync_cache", {})
    key = (id(compiled), id(netlist), repr(project_type), repr(net_matching))
    held = cache.get(key)
    if held is not None and held[0] is compiled and held[1] is netlist:
        return held[2]
    model = build_directive_sync(compiled, index, options, netlist, net_matching,
                                 project_type)
    if "error" not in model:
        if len(cache) >= CACHE_SIZE:
            cache.pop(next(iter(cache)))
        cache[key] = (compiled, netlist, model)
    return model


def _on_sheet(entry: dict, sheet: str) -> bool:
    """Whether the row or finding is on the sheet, named with or without .SchDoc."""
    wanted = sheet.strip().lower()
    if wanted.endswith(".schdoc"):
        wanted = wanted[:-len(".schdoc")]
    return any(name.lower() == wanted for name in entry.get("sheets") or [])


def page_directive_sync(model: dict, kind: str | None = None, sheet: str | None = None,
                        offset: int = 0, limit: int = DEFAULT_LIMIT,
                        rows_offset: int = 0, rows_limit: int = ROWS_LISTED) -> dict:
    """One page: findings from offset and directive rows from rows_offset, narrowed to a
    kind and a sheet (findings with no sheet, such as stale PCB items, drop out with a
    sheet)."""
    if kind is not None and kind not in KINDS:
        return {"error": "invalid_kind",
                "message": f"Unknown kind '{kind}'; use one of {list(KINDS)}."}
    findings = model["findings"]
    rows = model["rows"]
    if kind:
        findings = [f for f in findings if f["kind"] == kind]
        rows = [r for r in rows if r["kind"] == kind]
    if sheet:
        findings = [f for f in findings if _on_sheet(f, sheet)]
        rows = [r for r in rows if _on_sheet(r, sheet)]
    offset = max(0, int(offset or 0))
    rows_offset = max(0, int(rows_offset or 0))
    limit = max(1, int(limit or DEFAULT_LIMIT))
    result = {key: value for key, value in model.items()
              if key not in ("rows", "findings", "notes", "not_checked")}
    if kind or sheet:
        result["filtered"] = {k: v for k, v in (("kind", kind), ("sheet", sheet)) if v}
        counts = Counter(f["severity"] for f in findings)
        result["filtered_severity_counts"] = {level: counts.get(level, 0)
                                              for level in ("error", "warning", "info")}
    result["findings"] = findings[offset:offset + limit]
    result["total"] = len(findings)
    result["offset"] = offset
    result["has_more"] = offset + limit < len(findings)
    if result["has_more"]:
        result["next_offset"] = offset + limit
    result["directives"] = rows[rows_offset:rows_offset + rows_limit]
    result["directives_total"] = len(rows)
    if rows_offset or rows_offset + rows_limit < len(rows):
        result["directives_offset"] = rows_offset
        if rows_offset + rows_limit < len(rows):
            result["next_directives_offset"] = rows_offset + rows_limit
    result["notes"] = model["notes"]
    result["not_checked"] = model["not_checked"]
    return result
