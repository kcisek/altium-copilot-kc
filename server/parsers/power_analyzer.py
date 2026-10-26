"""Altium's Power Analyzer by Keysight files (and the earlier PDN Analyzer's), read offline.

Setup. Power Analyzer keeps its setup and its result summary in the project's
<Project>.pdnaK document (an Analysis document: board properties, power nets with their
source, series parts and loads, loads configured as VRMs with their child networks, and
per net the Voltage Drop, Current Density and Max Via Current of the last run). Its file
format is not documented: a .pdnaK is read only when it is plain text (XML or JSON), with
the same generic reader as the earlier PDN Analyzer's setups; otherwise only its presence
is reported. The PDN Analyzer (before Keysight) saved its setups as XML (*.pdna: a
SimulatorManager with Networks of NetworkModel, whose CompPinModel entries are typed
BatterySourceModelData, CurrentLoadModelData, ResisterLoadModelData,
SeriesElementModelData; VoltageRegulators of VRMModelData with InNetwork and OutNetwork)
and JSON (*.pidc_config: a SourceModel and LoadModels with PowerNet, PowerComponent,
PowerPins and Source/LoadProperties), as Altium's SpiritLevel-SL1 example ships them.
Any element carrying an xsi type is read: its kind from the type name (source, load,
series, VRM), its part and net from its power terminal, and its numeric parameters.

Reports. Save Report writes a set of HTML files into a time-stamped folder,
PowerAnalyzerByKeysight_Output\\HTMLReport\\<Project> [PDNA]_<Date>_<Time>, opened at
Report.html: the full report lists every analysed power net with its Pass/Fail status;
each net's report gives its calculated Vdrop, Idensity and Ivia_max with the status, and
Network Details (Power Net Members: sources and loads; Power Consumption; Least Margins
of non-via current density and via current; List of Violations; Failed Via Summary).
Altium documents the content, not the markup, so the reader is generic: every table with
its header row, and label/value pairs in two-cell rows and in text ('Vdrop: 25 mV'). A
table with a net column and a result column (Vdrop / voltage drop, Idensity / current
density, Ivia_max / via current, status) gives results per net; a page about one net (its
title or first heading names it) gives that net's values; a table with a designator
column is a members table.
"""

import json
import os
import re
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path, PureWindowsPath

MAX_FILE_BYTES = 20 * 1024 * 1024  # setup and report files larger than this are not read
MAX_REPORT_FILES = 200  # HTML files read from one report folder
TABLE_ROWS_KEPT = 50  # rows kept per table in a parsed page
_WALK_DEPTH = 3  # folders below the project folder searched for setups and reports
_SKIPPED_DIRS = frozenset({"history", "__previews", ".git", "node_modules"})
SETUP_SUFFIXES = (".pdnak", ".pdna", ".pidc_config")
OUTPUT_FOLDER = "poweranalyzerbykeysight_output"
REPORT_FOLDER = "htmlreport"

_XSI_TYPE = "{http://www.w3.org/2001/XMLSchema-instance}type"
_NUMBER = re.compile(r"[-+]?\d+(?:[.,]\d+)?(?:[eE][-+]?\d+)?")
_VALUE = re.compile(
    r"([-+]?\d+(?:[.,]\d+)?(?:[eE][-+]?\d+)?)\s*(%|[munµμk]?[VAW](?:/(?:mm|mil|um|µm)\^?2|/mm²)?"
    r"|[munµμ]?ohm|Ω)?", re.IGNORECASE)
_SCALE = {"": 1.0, "m": 1e-3, "u": 1e-6, "µ": 1e-6, "μ": 1e-6, "n": 1e-9, "k": 1e3}


# ---------- finding the files ----------

def find_files(root_dir: str | Path | None) -> dict:
    """{'setups': [paths], 'reports': [report folders, newest first]} under the project
    folder (and _WALK_DEPTH levels below it, history and preview folders skipped). A
    report folder is a folder under PowerAnalyzerByKeysight_Output\\HTMLReport holding
    HTML files (or the HTMLReport folder itself when the files sit there)."""
    found = {"setups": [], "reports": []}
    if not root_dir or not Path(root_dir).is_dir():
        return found
    root = Path(root_dir)
    outputs: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        depth = len(Path(dirpath).relative_to(root).parts)
        dirnames[:] = sorted(name for name in dirnames
                             if name.lower() not in _SKIPPED_DIRS and depth < _WALK_DEPTH)
        for name in dirnames:
            if name.lower() == OUTPUT_FOLDER:
                outputs.append(Path(dirpath) / name)
        for name in sorted(filenames):
            if name.lower().endswith(SETUP_SUFFIXES):
                found["setups"].append(Path(dirpath) / name)
    reports: list[Path] = []
    for output in outputs:
        for child in sorted(output.iterdir()) if output.is_dir() else ():
            if child.is_dir() and child.name.lower() == REPORT_FOLDER:
                folders = [entry for entry in child.iterdir() if entry.is_dir()]
                reports += [folder for folder in folders if _html_files(folder)]
                if _html_files(child):
                    reports.append(child)
    found["reports"] = sorted(reports, key=lambda folder: (folder.stat().st_mtime, folder.name),
                              reverse=True)
    return found


def _html_files(folder: Path) -> list[Path]:
    try:
        return sorted(path for path in folder.iterdir()
                      if path.is_file() and path.suffix.lower() in (".html", ".htm"))
    except OSError:
        return []


def _read_text(path: Path) -> str | None:
    """The file as text (UTF-8, else UTF-16 with a BOM, else the Windows code page); None
    when it is binary or too large."""
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            return None
    if b"\x00" in data[:4096]:
        return None
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


# ---------- setups ----------

def _number(text) -> float | None:
    if text is None:
        return None
    match = _NUMBER.fullmatch(str(text).strip())
    if not match:
        return None
    try:
        return float(match[0].replace(",", "."))
    except ValueError:
        return None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(element, *names):
    """The first descendant along a path of local names (each one level down)."""
    for name in names:
        if element is None:
            return None
        element = next((child for child in element if _local(child.tag) == name), None)
    return element


def _text(element, *names) -> str | None:
    found = _child(element, *names)
    if found is None or found.text is None:
        return None
    return found.text.strip() or None


_PARAMETERS = {
    "VoltageParameter": "voltage_v",
    "LoadCurrentParameter": "current_a",
    "CurrentParameter": "current_a",
    "ResistanceParameter": "resistance_ohm",
    "MinVoltageCriteria": "min_voltage_v",
    "MaxVoltageCriteria": "max_voltage_v",
    "MaxCurrentCriteria": "max_current_a",
}


def _kind(type_name: str) -> str | None:
    lowered = type_name.lower()
    if "vrm" in lowered or "regulator" in lowered:
        return "vrm"
    if "source" in lowered:
        return "source"
    if "load" in lowered:
        return "load"
    if "series" in lowered:
        return "series"
    return None


def _xml_entry(element, network: str | None) -> dict | None:
    """One typed model element as {kind, type, part, net, pins, network, parameters}."""
    type_name = element.get(_XSI_TYPE) or ""
    kind = _kind(type_name)
    if kind is None:
        return None
    terminal = _child(element, "PowerTerminal")
    if terminal is None:
        terminal = element
    entry: dict = {"kind": kind, "type": type_name}
    part = _text(terminal, "Component", "Designator")
    net = _text(terminal, "Net", "Name") or _text(terminal, "Net")
    listed = _child(terminal, "Pins")
    pins = [_text(pin, "Descriptor") for pin in (listed if listed is not None else ())]
    if network:
        entry["network"] = network
    if part:
        entry["part"] = part
    if net:
        entry["net"] = net
    if pins := [pin for pin in pins if pin]:
        entry["pins"] = pins
    for child in element:
        name = _local(child.tag)
        if name in ("InNetwork", "OutNetwork") and child.text:
            entry["in_network" if name == "InNetwork" else "out_network"] = child.text.strip()
        elif name in _PARAMETERS and (value := _number(child.text)) is not None:
            entry[_PARAMETERS[name]] = value
    return entry


def _read_xml_setup(text: str) -> dict:
    root = ET.fromstring(text)
    entries: list[dict] = []
    networks: list[str] = []

    def walk(element, network: str | None) -> None:
        name = _local(element.tag)
        if name == "NetworkModel":
            network = _text(element, "Name") or network
            if network and network not in networks:
                networks.append(network)
        if element.get(_XSI_TYPE):
            entry = _xml_entry(element, network)
            if entry is not None:
                entries.append(entry)
                return
        for child in element:
            walk(child, network)

    walk(root, None)
    board = next((child.text.strip() for child in root
                  if _local(child.tag) == "BoardFileName" and child.text), None)
    return {"networks": networks, "entries": entries, "board": board}


def _last_field(value) -> str:
    """A designator or pin from a .pidc_config field: some name the part as
    '<unique id>|<x>|<y>|U1' (and a pin '<id>|<x>|<y>|U1-14'): the last '|' field."""
    return str(value).rsplit("|", 1)[-1].strip()


def _read_json_setup(text: str) -> dict:
    data = json.loads(text)
    entries: list[dict] = []
    if not isinstance(data, dict):
        return {"networks": [], "entries": entries}

    def entry(model: dict, kind: str) -> dict | None:
        if not isinstance(model, dict):
            return None
        out: dict = {"kind": kind, "type": "SourceModel" if kind == "source" else "LoadModel"}
        if model.get("PowerComponent"):
            out["part"] = _last_field(model["PowerComponent"])
        if model.get("PowerNet"):
            out["net"] = str(model["PowerNet"])
        if isinstance(model.get("PowerPins"), list):
            out["pins"] = [_last_field(pin) for pin in model["PowerPins"]]
        props = model.get("SourceProperties" if kind == "source" else "LoadProperties") or {}
        if kind == "source":
            for key, name in (("MinDCVoltage", "min_voltage_v"), ("MaxDCCurrent", "max_current_a"),
                              ("MaxResistance", "resistance_ohm")):
                if (value := _number(props.get(key))) is not None and value:
                    out[name] = value
        else:
            if props.get("UseCurrent") and (value := _number(props.get("MaxDCCurrent"))):
                out["current_a"] = value
            elif (value := _number(props.get("MinResistance"))):
                out["resistance_ohm"] = value
            if (value := _number(props.get("MinDCVoltage"))):
                out["min_voltage_v"] = value
        return out

    if (source := entry(data.get("SourceModel"), "source")) is not None:
        entries.append(source)
    for model in data.get("LoadModels") or ():
        if (load := entry(model, "load")) is not None:
            entries.append(load)
    net = data.get("SourcePowerNet") or data.get("LoadPowerNet")
    board = data.get("BoardFile")
    return {"networks": [str(net)] if net else [], "entries": entries,
            "board": str(board) if board else None}


def read_setup(path: str | Path) -> dict:
    """A setup file: {file, kind (pdnak | pdna | pidc_config), format (xml | json |
    binary | unreadable), read, board (the PcbDoc's file name it names), networks, sources,
    loads, series, vrms}. A .pdnaK that is not plain text is reported, not read."""
    path = Path(path)
    suffix = path.suffix.lower().lstrip(".")
    out: dict = {"file": path.name, "kind": suffix, "read": False}
    text = _read_text(path)
    if text is None:
        out["format"] = "binary"
        return out
    stripped = text.lstrip()
    try:
        if stripped.startswith("<"):
            out["format"] = "xml"
            parsed = _read_xml_setup(stripped)
        elif stripped.startswith(("{", "[")):
            out["format"] = "json"
            parsed = _read_json_setup(stripped)
        else:
            out["format"] = "text"
            return out
    except (ET.ParseError, ValueError) as error:
        out["format"] = "unreadable"
        out["error"] = type(error).__name__
        return out
    out["read"] = True
    if parsed.get("board"):  # the PcbDoc it was set up on, by file name
        out["board"] = PureWindowsPath(parsed["board"]).name
    out["networks"] = parsed["networks"]
    for kind in ("source", "load", "series", "vrm"):
        out[f"{kind}s" if kind != "series" else "series"] = [
            {key: value for key, value in entry.items() if key != "kind"}
            for entry in parsed["entries"] if entry["kind"] == kind]
    return out


# ---------- HTML reports ----------

class _Tables(HTMLParser):
    """Every table of a page as rows of cell texts (with whether a row is a header row),
    the title, the headings and the text outside tables."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.headings: list[str] = []
        self.tables: list[dict] = []
        self.text: list[str] = []
        self._stack: list[dict] = []  # open tables (nested tables kept apart)
        self._row: list[str] | None = None
        self._row_header = False
        self._cell: list[str] | None = None
        self._capture: str | None = None  # title | heading
        self._buffer: list[str] = []
        self._last_heading = ""

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._stack.append({"heading": self._last_heading, "rows": []})
        elif tag == "tr" and self._stack:
            self._row, self._row_header = [], False
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
            if tag == "th":
                self._row_header = True
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")
        elif tag == "title" or re.fullmatch(r"h[1-6]|caption", tag):
            self._capture, self._buffer = tag, []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None and self._stack:
            if any(self._row):
                self._stack[-1]["rows"].append((self._row, self._row_header))
            self._row = None
        elif tag == "table" and self._stack:
            table = self._stack.pop()
            if table["rows"]:
                self.tables.append(table)
        elif self._capture is not None and tag == self._capture:
            text = " ".join("".join(self._buffer).split())
            if self._capture == "title":
                self.title = text
            elif self._capture == "caption" and self._stack:
                self._stack[-1]["heading"] = text
            elif text:
                self.headings.append(text)
                self._last_heading = text
            self._capture = None

    def handle_data(self, data):
        if self._capture is not None:
            self._buffer.append(data)
        if self._cell is not None:
            self._cell.append(data)
        elif self._capture is None and data.strip():
            self.text.append(" ".join(data.split()))


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _column_role(header: str) -> str | None:
    """What a header cell names: net, vdrop, idensity, ivia, status, designator, type,
    voltage, current, power, margin."""
    key = _key(header)
    if not key:
        return None
    if "vdrop" in key or "voltagedrop" in key or key.startswith("drop"):
        return "vdrop"
    if "idensity" in key or "currentdensity" in key:
        return "idensity"
    if "ivia" in key or "viacurrent" in key:
        return "ivia"
    if key in ("status", "result", "passfail", "verdict") or key.endswith("status"):
        return "status"
    if key in ("net", "netname", "powernet", "powernetname", "network", "networkname",
               "powernetwork", "name", "nets"):
        return "net"
    if key in ("designator", "component", "refdes", "part", "device", "componentdesignator"):
        return "designator"
    if key in ("type", "role", "kind", "model", "membertype", "devicetype"):
        return "type"
    if "margin" in key:
        return "margin"
    if "voltage" in key:
        return "voltage"
    if "current" in key:
        return "current"
    if "power" in key:
        return "power"
    return None


def parse_value(text: str | None) -> dict | None:
    """'25.3 mV' -> {'value': 0.0253, 'unit': 'V', 'text': '25.3 mV'}; a bare number keeps
    no unit; '12 %' -> {'value': 12, 'unit': '%'}. None without a number."""
    if not text:
        return None
    match = _VALUE.search(text)
    if not match:
        return None
    number = float(match[1].replace(",", "."))
    unit = (match[2] or "").strip()
    out = {"text": text.strip()}
    if unit and unit != "%" and unit[0] in _SCALE and len(unit) > 1 and unit[1:2].upper() in "VAW":
        out.update(value=number * _SCALE[unit[0]], unit=unit[1:].replace("²", "^2"))
    elif unit.lower().endswith("ohm") or unit == "Ω":
        scale = _SCALE.get(unit[0], 1.0) if len(unit) > 3 else 1.0
        out.update(value=number * scale, unit="ohm")
    else:
        out.update(value=number, unit=unit.replace("²", "^2") or None)
    return out


_LABELS = {
    "vdrop": re.compile(r"\b(?:V\s?drop|Voltage\s+Drop)\b\s*[:=]?\s*([-+]?\d[^,;|]*)", re.I),
    "idensity": re.compile(r"\b(?:I\s?density|Current\s+Density)\b\s*[:=]?\s*([-+]?\d[^,;|]*)",
                           re.I),
    "ivia": re.compile(r"\b(?:I\s?via(?:_?max)?|Max(?:imum)?\s+Via\s+Current)\b\s*[:=]?\s*"
                       r"([-+]?\d[^,;|]*)", re.I),
}
_STATUS = re.compile(r"\b(Pass(?:ed)?|Fail(?:ed)?|Warning)\b", re.I)


def _status(text: str | None) -> str | None:
    match = _STATUS.search(text or "")
    if not match:
        return None
    word = match[1].lower()
    return "pass" if word.startswith("pass") else "fail" if word.startswith("fail") else "warning"


def _metrics(cells: dict[str, str]) -> dict:
    out: dict = {}
    for role in ("vdrop", "idensity", "ivia"):
        if (value := parse_value(cells.get(role))) is not None:
            out[role] = value
    if (status := _status(cells.get("status"))) is not None:
        out["status"] = status
    return out


def parse_report_page(text: str, single_net: bool = True) -> dict:
    """One HTML page: {title, net (the net a single-net page is about; not looked for when
    single_net is False, as on Report.html), tables (heading, header, rows), results ({net:
    metrics}), members, values (label/value pairs)}."""
    parser = _Tables()
    parser.feed(text)
    parser.close()
    page: dict = {"title": parser.title, "headings": parser.headings[:20], "tables": [],
                  "results": {}, "members": [], "values": {}}
    for table in parser.tables:
        rows = table["rows"]
        header_at = next((number for number, (_, header) in enumerate(rows) if header), None)
        if header_at is None and len(rows) > 1 and len(rows[0][0]) > 2:
            header_at = 0  # a first row without <th> holding labels
        header = rows[header_at][0] if header_at is not None else []
        body = [cells for cells, _ in rows[(header_at + 1) if header_at is not None else 0:]]
        roles = [_column_role(cell) for cell in header]
        entry = {"heading": table["heading"], "header": header,
                 "rows": body[:TABLE_ROWS_KEPT], "rows_total": len(body)}
        page["tables"].append(entry)
        if "net" in roles and {"vdrop", "idensity", "ivia", "status"} & set(roles):
            for cells in body:
                named = {role: cell for role, cell in zip(roles, cells) if role}
                net = (named.get("net") or "").strip()
                if net and (found := _metrics(named)):
                    page["results"][net] = {**page["results"].get(net, {}), **found}
        elif "designator" in roles:
            for cells in body:
                named = {role: cell for role, cell in zip(roles, cells) if role}
                member = {key: named[key] for key in ("designator", "net", "type", "voltage",
                                                      "current", "power") if named.get(key)}
                if member.get("designator"):
                    page["members"].append(member)
        # Two-cell rows are label/value pairs (the net report's calculated parameters).
        for cells in body if len(header) <= 2 else ():
            if len(cells) == 2 and cells[0] and cells[1]:
                page["values"].setdefault(cells[0].rstrip(":"), cells[1])
        if len(header) == 2 and header[0] and header[1] and _column_role(header[0]) in (
                "vdrop", "idensity", "ivia", "status"):
            page["values"].setdefault(header[0].rstrip(":"), header[1])
    # Values in text: 'Vdrop: 25 mV', 'Status: Pass'.
    labels = {}
    joined = " | ".join(parser.text)
    for role, pattern in _LABELS.items():
        if match := pattern.search(joined):
            labels[role] = match[1].strip()
    for label, value in page["values"].items():
        role = _column_role(label)
        if role in ("vdrop", "idensity", "ivia", "status"):
            labels.setdefault(role, value)
    if "status" not in labels:
        status_text = next((line for line in parser.text if _STATUS.search(line)), None)
        if status_text:
            labels["status"] = status_text
    single = _single_net(parser, page) if single_net else None
    if single and (found := _metrics(labels)):
        page["net"] = single
        page["results"][single] = {**found, **page["results"].get(single, {})}
    return page


def _single_net(parser: _Tables, page: dict) -> str | None:
    """The net a per-net page is about: 'Net: <name>' in its text or values, else its
    title or first heading when that is a single word (a net name)."""
    for label, value in page["values"].items():
        if _column_role(label) == "net" and value:
            return value.strip()
    for line in parser.text:
        if match := re.match(r"\s*(?:Power\s+)?Net(?:work)?\s*[:=]\s*(\S+)", line, re.I):
            return match[1]
    for text in (parser.title, *parser.headings[:1]):
        words = (text or "").split()
        if len(words) == 1 and _key(words[0]) not in _NOT_NETS:
            return words[0]
    return None


_NOT_NETS = frozenset({"report", "summary", "fullreport", "powerreport", "results", "index"})


def read_report(folder: str | Path, names=()) -> dict:
    """A report folder: {folder, files, report_html (Report.html is there), results ({net:
    metrics, with the page they came from}), members, tables (per page, trimmed),
    pages_unread, names_found (which of names, such as the project's and the PcbDoc's, a
    page's text mentions)}."""
    folder = Path(folder)
    files = _html_files(folder)[:MAX_REPORT_FILES]
    out: dict = {"folder": folder.name, "files": len(files),
                 "report_html": any(path.name.lower() == "report.html" for path in files),
                 "results": {}, "members": [], "pages": [], "pages_unread": 0}
    wanted = {name.casefold(): name for name in names if name}
    found_names: set[str] = set()
    # Report.html first: a net's own page refines its row.
    files.sort(key=lambda path: (path.name.lower() != "report.html", path.name.lower()))
    for path in files:
        text = _read_text(path)
        if text is None:
            out["pages_unread"] += 1
            continue
        folded = text.casefold()
        found_names |= {name for key, name in wanted.items() if key in folded and re.search(
            r"(?<![\w.-])" + re.escape(key) + r"(?![\w-])", folded)}
        page = parse_report_page(text, single_net=path.name.lower() != "report.html")
        for net, metrics in page["results"].items():
            merged = out["results"].setdefault(net, {})
            merged.update(metrics)
            merged.setdefault("pages", []).append(path.name)
        for member in page["members"]:
            out["members"].append({**member, "page": path.name,
                                   **({"report_net": page["net"]} if page.get("net") else {})})
        out["pages"].append({"file": path.name, "title": page["title"],
                             "net": page.get("net"),
                             "tables": [{"heading": table["heading"], "header": table["header"],
                                         "rows_total": table["rows_total"]}
                                        for table in page["tables"]]})
    out["names_found"] = sorted(found_names)
    return out


def report_names(folder_name: str, names) -> bool:
    """Does a report folder's name (<Project> [PDNA]_<Date>_<Time>) start with one of
    names?"""
    return any(name and re.match(re.escape(name) + r"\s*\[PDNA\]", folder_name, re.I)
               for name in names)
