"""Reader for Altium multi-board schematic documents (.MbsDoc).

An AD25 .MbsDoc is a zip of JSON files: core.json (version, modified), logical.json (the
connectivity model, below), Pages/page_<n>.json (the drawing: module rectangles, entry
markers, connection lines, which name their logical object by id), styles.json,
options.json, defaults.json (the drawing defaults of each object kind: module,
moduleEntry, functionalBlock, directConnection, wireConnection, cableConnection,
harnessConnection, powerPort ...) and parameters.json. Only core.json and logical.json are
read: the drawing adds no connectivity.

logical.json holds uniqueId, modules, connections, powerPorts, conflicts and parameters.
Every object carries an integer id, unique in the document, by which the others refer to
it:

- A module is one placed board: designator (M1), title, source.sourceProject (the child
  .PrjPcb, relative to the .MbsDoc) and source.boardFileName, functionalBlocks,
  components (the connector parts imported from the child project: designator, comment,
  parameters SourceId (the part's UniqueId path in the child project, '\\ABCDEFGH'),
  ParentSourceId and PhysicalPath (its sheet), and pins: pinId, name, number and
  parameters SourceId, ParentSourceId (the part's SourceId) and ExternalNetName, the net
  the child project gave the pin when it was imported, absent on a pin with no net),
  entries (one per connector shown on the module's edge: calculatedDesignator, the
  connection it belongs to as logicalConnection) and entryPinMap ({map: [{key: pin id,
  value: entry id}]}: which pins each entry carries).
- A connection joins entries: $type __logicalDirectConnection (two boards' connectors
  mated directly), __logicalWireConnection, __logicalCableConnection or
  __logicalHarnessConnection. A direct connection has one entriesConnection {entry1,
  entry2, physicalConnections}; a cable or harness owns its own components (the plugs at
  its ends: designator, comment, DesignItemId, pins with ExternalNetName), entries (one
  per plug: definedDesignator P1, entryType) and entryPinMap, with entriesConnections
  mating each module entry to one of its plug entries and connectionEntriesConnections
  wiring its plugs to each other (a splice: one plug pin wired to several). Each physical
  connection ($type __logicalPinToPinConnection, __logicalInternalConnection ...) is a
  pinPair {pin1, pin2} of pin ids with a designator (the pin or wire label).
- powerPorts and conflicts are lists; neither the AD25 examples nor any file seen holds
  one, so they are kept as written (conflicts are what Altium recorded, e.g. after an
  import from the child projects).

Unknown keys are kept (extra) and unknown connection or physical-connection types are
read by their pinPairs, so a newer file still maps. An .MbsDoc that is not a zip (an
older OLE compound or text format) or a zip without logical.json is reported as
unsupported rather than guessed at. Pure parsing: nothing about the child projects is
read here."""

import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

MAX_FILE_BYTES = 512 << 20
MAX_JSON_BYTES = 256 << 20  # logical.json inflated (1 MB on the largest example)
OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
ZIP_MAGIC = b"PK\x03\x04"
SUPPORTED_VERSIONS = (1,)

KINDS = ("direct", "wire", "cable", "harness")
_MODULE_KEYS = {"designator", "title", "source", "functionalBlocks", "components",
                "entries", "entryPinMap", "parameters", "id"}
_CONNECTION_KEYS = {"$type", "source", "components", "entries", "entryPinMap",
                    "entriesConnection", "entriesConnections",
                    "connectionEntriesConnections", "designator", "parameters", "id"}
_COMPONENT_KEYS = {"designator", "comment", "pins", "parameters", "id"}
_ENTRY_KEYS = {"logicalConnection", "calculatedDesignator", "definedDesignator",
               "entryType", "parameters", "id"}
_TOP_KEYS = {"uniqueId", "modules", "powerPorts", "connections", "conflicts", "parameters"}


class MbsDocError(ValueError):
    """The file cannot be read as an .MbsDoc (damaged, not JSON where JSON is expected)."""


class MbsDocUnsupported(MbsDocError):
    """An .MbsDoc in a format this reader does not decode; format names it."""

    def __init__(self, format_name: str, message: str):
        super().__init__(message)
        self.format = format_name


@dataclass
class MbsPin:
    id: int
    number: str
    name: str
    pin_id: str
    parameters: dict[str, str] = field(default_factory=dict)

    @property
    def external_net(self) -> str:
        """The net the child project gave this pin at the last import ('' for none)."""
        return self.parameters.get("ExternalNetName", "") or ""


@dataclass
class MbsComponent:
    id: int
    designator: str
    comment: str
    pins: list[MbsPin] = field(default_factory=list)
    parameters: dict[str, str] = field(default_factory=dict)
    extra: dict = field(default_factory=dict)

    @property
    def source_id(self) -> str:
        """The part's UniqueId path in its child project ('\\ABCDEFGH'), '' for none."""
        return self.parameters.get("SourceId", "") or ""

    @property
    def sheet(self) -> str:
        return self.parameters.get("PhysicalPath", "") or ""


@dataclass
class MbsEntry:
    id: int
    calculated: str
    defined: str
    connection_id: int | None
    entry_type: int | None
    pin_ids: list[int] = field(default_factory=list)
    parameters: dict[str, str] = field(default_factory=dict)
    extra: dict = field(default_factory=dict)

    @property
    def designator(self) -> str:
        return self.defined or self.calculated


@dataclass
class MbsPinPair:
    """One physical connection: two pin ids joined (a mated contact or a wire)."""
    id: int
    type_name: str
    pin1: int | None
    pin2: int | None
    designator: str
    parameters: dict[str, str] = field(default_factory=dict)
    extra: dict = field(default_factory=dict)


@dataclass
class MbsEntryLink:
    """Two entries joined by physical connections; internal: two plugs of one cable or
    harness wired to each other (connectionEntriesConnections)."""
    id: int
    entry1: int | None
    entry2: int | None
    pairs: list[MbsPinPair] = field(default_factory=list)
    internal: bool = False


@dataclass
class MbsModule:
    id: int
    designator: str
    title: str
    source_project: str  # as written, relative to the .MbsDoc
    board_file: str
    components: list[MbsComponent] = field(default_factory=list)
    entries: list[MbsEntry] = field(default_factory=list)
    functional_blocks: list = field(default_factory=list)
    parameters: dict[str, str] = field(default_factory=dict)
    source: dict = field(default_factory=dict)
    extra: dict = field(default_factory=dict)


@dataclass
class MbsConnection:
    id: int
    designator: str
    type_name: str
    kind: str  # direct, wire, cable, harness, unknown
    components: list[MbsComponent] = field(default_factory=list)  # a cable's or harness's plugs
    entries: list[MbsEntry] = field(default_factory=list)
    links: list[MbsEntryLink] = field(default_factory=list)
    parameters: dict[str, str] = field(default_factory=dict)
    source: dict = field(default_factory=dict)
    extra: dict = field(default_factory=dict)


@dataclass
class MbsPowerPort:
    id: int | None
    designator: str
    name: str
    parameters: dict[str, str] = field(default_factory=dict)
    raw: dict = field(default_factory=dict)


@dataclass
class MbsConflict:
    id: int | None
    kind: str
    text: str
    ids: list[int] = field(default_factory=list)  # object ids the record names
    raw: dict = field(default_factory=dict)


@dataclass
class MbsDocument:
    path: str
    format: str = "zip_json"
    version: int | None = None
    modified: str = ""
    unique_id: str = ""
    modules: list[MbsModule] = field(default_factory=list)
    connections: list[MbsConnection] = field(default_factory=list)
    power_ports: list[MbsPowerPort] = field(default_factory=list)
    conflicts: list[MbsConflict] = field(default_factory=list)
    parameters: dict[str, str] = field(default_factory=dict)
    extra: dict = field(default_factory=dict)  # unknown top-level keys of logical.json
    warnings: list[str] = field(default_factory=list)


def connection_kind(type_name: str) -> str:
    lowered = (type_name or "").lower()
    for kind in ("harness", "cable", "wire", "direct"):
        if kind in lowered:
            return kind
    return "unknown"


def _int(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def _text(value) -> str:
    return value if isinstance(value, str) else ("" if value is None else str(value))


def _parameters(value) -> dict[str, str]:
    """[{name, value, id}] -> {name: value}; the first of a name wins."""
    result: dict[str, str] = {}
    for item in value if isinstance(value, list) else []:
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            result.setdefault(item["name"], _text(item.get("value")))
    return result


def _extra(record: dict, known: set[str]) -> dict:
    return {key: value for key, value in record.items() if key not in known}


def _pin(record: dict) -> MbsPin | None:
    pin_id = _int(record.get("id"))
    if pin_id is None:
        return None
    number = _text(record.get("number")) or _text(record.get("pinId"))
    return MbsPin(id=pin_id, number=number, name=_text(record.get("name")),
                  pin_id=_text(record.get("pinId")),
                  parameters=_parameters(record.get("parameters")))


def _component(record: dict) -> MbsComponent | None:
    component_id = _int(record.get("id"))
    if component_id is None:
        return None
    pins = [pin for pin in (_pin(item) for item in record.get("pins") or []
                            if isinstance(item, dict)) if pin is not None]
    return MbsComponent(id=component_id, designator=_text(record.get("designator")),
                        comment=_text(record.get("comment")), pins=pins,
                        parameters=_parameters(record.get("parameters")),
                        extra=_extra(record, _COMPONENT_KEYS))


def _entries(records, pin_map: dict) -> list[MbsEntry]:
    by_entry: dict[int, list[int]] = {}
    items = pin_map.get("map") if isinstance(pin_map, dict) else None
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict):
            pin, entry = _int(item.get("key")), _int(item.get("value"))
            if pin is not None and entry is not None:
                by_entry.setdefault(entry, []).append(pin)
    entries = []
    for record in records if isinstance(records, list) else []:
        if not isinstance(record, dict):
            continue
        entry_id = _int(record.get("id"))
        if entry_id is None:
            continue
        entries.append(MbsEntry(
            id=entry_id, calculated=_text(record.get("calculatedDesignator")),
            defined=_text(record.get("definedDesignator")),
            connection_id=_int(record.get("logicalConnection")),
            entry_type=_int(record.get("entryType")),
            pin_ids=by_entry.get(entry_id, []),
            parameters=_parameters(record.get("parameters")),
            extra=_extra(record, _ENTRY_KEYS)))
    return entries


def _pair(record: dict) -> MbsPinPair | None:
    if not isinstance(record, dict):
        return None
    pair = record.get("pinPair") if isinstance(record.get("pinPair"), dict) else record
    pair_id = _int(record.get("id"))
    pin1, pin2 = _int(pair.get("pin1")), _int(pair.get("pin2"))
    if pin1 is None and pin2 is None:
        return None
    return MbsPinPair(id=pair_id if pair_id is not None else -1,
                      type_name=_text(record.get("$type")), pin1=pin1, pin2=pin2,
                      designator=_text(record.get("designator")),
                      parameters=_parameters(record.get("parameters")),
                      extra=_extra(record, {"$type", "pinPair", "designator", "parameters",
                                            "id"}))


def _links(value, internal: bool) -> list[MbsEntryLink]:
    records = value if isinstance(value, list) else [value] if isinstance(value, dict) else []
    links = []
    for record in records:
        if not isinstance(record, dict):
            continue
        pairs = [pair for pair in (_pair(item) for item in record.get("physicalConnections")
                                   or []) if pair is not None]
        link_id = _int(record.get("id"))
        links.append(MbsEntryLink(id=link_id if link_id is not None else -1,
                                  entry1=_int(record.get("entry1")),
                                  entry2=_int(record.get("entry2")), pairs=pairs,
                                  internal=internal))
    return links


def _module(record: dict) -> MbsModule | None:
    module_id = _int(record.get("id"))
    if module_id is None:
        return None
    source = record.get("source") if isinstance(record.get("source"), dict) else {}
    components = [c for c in (_component(item) for item in record.get("components") or []
                              if isinstance(item, dict)) if c is not None]
    return MbsModule(
        id=module_id, designator=_text(record.get("designator")),
        title=_text(record.get("title")),
        source_project=_text(source.get("sourceProject")),
        board_file=_text(source.get("boardFileName")), components=components,
        entries=_entries(record.get("entries"), record.get("entryPinMap")),
        functional_blocks=list(record.get("functionalBlocks") or []),
        parameters=_parameters(record.get("parameters")), source=dict(source),
        extra=_extra(record, _MODULE_KEYS))


def _connection(record: dict) -> MbsConnection | None:
    connection_id = _int(record.get("id"))
    if connection_id is None:
        return None
    type_name = _text(record.get("$type"))
    components = [c for c in (_component(item) for item in record.get("components") or []
                              if isinstance(item, dict)) if c is not None]
    links = (_links(record.get("entriesConnection"), False)
             + _links(record.get("entriesConnections"), False)
             + _links(record.get("connectionEntriesConnections"), True))
    source = record.get("source") if isinstance(record.get("source"), dict) else {}
    return MbsConnection(
        id=connection_id, designator=_text(record.get("designator")), type_name=type_name,
        kind=connection_kind(type_name), components=components,
        entries=_entries(record.get("entries"), record.get("entryPinMap")), links=links,
        parameters=_parameters(record.get("parameters")), source=dict(source),
        extra=_extra(record, _CONNECTION_KEYS))


def _ids_in(value, found: list[int], depth: int = 0) -> None:
    """Integer values of keys that name objects (id, pin, entry, ...) anywhere in a record."""
    if depth > 6:
        return
    if isinstance(value, dict):
        for key, item in value.items():
            number = _int(item)
            if number is not None and key != "id" and (
                    key.lower().endswith("id") or key.lower() in (
                        "pin", "pin1", "pin2", "entry", "entry1", "entry2", "object",
                        "module", "connection", "component")):
                found.append(number)
            else:
                _ids_in(item, found, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _ids_in(item, found, depth + 1)


def _conflict(record) -> MbsConflict:
    if not isinstance(record, dict):
        return MbsConflict(id=None, kind="", text=_text(record), raw={"value": record})
    text = next((_text(record.get(key)) for key in ("message", "description", "text",
                                                     "name", "details")
                 if record.get(key)), "")
    kind = next((_text(record.get(key)) for key in ("$type", "type", "kind", "conflictType")
                 if record.get(key) is not None), "")
    ids: list[int] = []
    _ids_in({key: value for key, value in record.items() if key != "id"}, ids)
    return MbsConflict(id=_int(record.get("id")), kind=kind, text=text,
                       ids=sorted(set(ids)), raw=record)


def _power_port(record) -> MbsPowerPort | None:
    if not isinstance(record, dict):
        return None
    parameters = _parameters(record.get("parameters"))
    name = next((_text(record.get(key)) for key in ("name", "netName", "net", "text",
                                                     "value") if record.get(key)), "")
    return MbsPowerPort(id=_int(record.get("id")), designator=_text(record.get("designator")),
                        name=name or parameters.get("Name", "") or parameters.get("Net", ""),
                        parameters=parameters, raw=record)


def parse_logical(data: dict, path: str = "", version: int | None = None,
                  modified: str = "") -> MbsDocument:
    """The model of a decoded logical.json."""
    if not isinstance(data, dict):
        raise MbsDocError("logical.json is not a JSON object")
    document = MbsDocument(path=path, version=version, modified=modified,
                           unique_id=_text(data.get("uniqueId")),
                           parameters=_parameters(data.get("parameters")),
                           extra=_extra(data, _TOP_KEYS))
    for record in data.get("modules") or []:
        module = _module(record) if isinstance(record, dict) else None
        if module is not None:
            document.modules.append(module)
    for record in data.get("connections") or []:
        connection = _connection(record) if isinstance(record, dict) else None
        if connection is not None:
            document.connections.append(connection)
    for record in data.get("powerPorts") or []:
        port = _power_port(record)
        if port is not None:
            document.power_ports.append(port)
    document.conflicts = [_conflict(record) for record in data.get("conflicts") or []]
    if version is not None and version not in SUPPORTED_VERSIONS:
        document.warnings.append(f"core.json version {version}: only version 1 was seen; "
                                 "the model was read the same way")
    for connection in document.connections:
        if connection.kind == "unknown":
            document.warnings.append(f"connection {connection.designator or connection.id}: "
                                     f"unknown type '{connection.type_name}', read by its pin "
                                     "pairs")
    return document


def sniff_format(head: bytes) -> str:
    """zip_json, ole_compound, ascii_records, json_text or unknown, from a file's start."""
    if head.startswith(ZIP_MAGIC):
        return "zip_json"
    if head.startswith(OLE_MAGIC):
        return "ole_compound"
    stripped = head.lstrip(b"\xef\xbb\xbf \t\r\n\x00")
    if stripped.startswith(b"|") or b"RECORD=" in head.upper():
        return "ascii_records"
    if stripped.startswith(b"{"):
        return "json_text"
    return "unknown"


def _member(archive: zipfile.ZipFile, name: str) -> zipfile.ZipInfo | None:
    wanted = name.lower()
    return next((info for info in archive.infolist()
                 if info.filename.replace("\\", "/").lower() == wanted), None)


def _read_json(archive: zipfile.ZipFile, info: zipfile.ZipInfo):
    if info.file_size > MAX_JSON_BYTES:
        raise MbsDocError(f"{info.filename} is larger than {MAX_JSON_BYTES >> 20} MB")
    try:
        return json.loads(archive.read(info).decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError) as error:
        raise MbsDocError(f"{info.filename} is not valid JSON ({type(error).__name__})") \
            from error


def parse_mbs_doc(path: str) -> MbsDocument:
    """The logical model of the .MbsDoc at path. Raises MbsDocUnsupported for a format
    this reader does not decode and MbsDocError for a damaged file."""
    file = Path(path)
    try:
        if file.stat().st_size > MAX_FILE_BYTES:
            raise MbsDocError(f"the file is larger than {MAX_FILE_BYTES >> 20} MB")
        with file.open("rb") as handle:
            head = handle.read(64)
    except OSError as error:
        raise MbsDocError(f"cannot read the file ({type(error).__name__})") from error
    kind = sniff_format(head)
    if kind != "zip_json":
        raise MbsDocUnsupported(kind, {
            "ole_compound": "an OLE compound document (an older Altium format)",
            "ascii_records": "a text record document (an older Altium format)",
            "json_text": "a bare JSON document, not the zip of JSON files Altium writes",
        }.get(kind, "not a zip of JSON files") + ": not read")
    try:
        with zipfile.ZipFile(file) as archive:
            logical = _member(archive, "logical.json")
            if logical is None:
                raise MbsDocUnsupported("zip_without_logical",
                                        "a zip without logical.json: not read")
            core_info = _member(archive, "core.json")
            core = _read_json(archive, core_info) if core_info is not None else {}
            data = _read_json(archive, logical)
    except zipfile.BadZipFile as error:
        raise MbsDocError("the zip is damaged") from error
    core = core if isinstance(core, dict) else {}
    return parse_logical(data, str(file.resolve()), _int(core.get("version")),
                         _text(core.get("modified")))
