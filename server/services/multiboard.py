"""Multi-board systems (roadmap #54): a .PrjMbd's boards joined pin by pin.

The system is the .PrjMbd beside the loaded project or in a folder above it (or one named
with system=). Its multi-board schematic (.MbsDoc, parsers/mbs_doc.py) places each board
as a module that names its child .PrjPcb and the connector parts imported from it, and
joins the modules' entries with connections: a direct mating (the two connectors plug
into each other), a wire, a cable or a harness (with plugs and wires of its own). Each
child project is compiled offline once (services/sch_netlister.py through
AltiumClient.compile_offline, cached by its files; the loaded project's own netlist is
used for it) without touching the loaded project, so every module pin has the net its
board gives it now. Two modules can place one child project (two copies of a memory
card): each is a board of its own.

A module's connector is matched to its child project's part by the UniqueId path the
.MbsDoc stores (SourceId: the last segment is the part's UniqueId), then by designator;
pins by pin number. From the connections' pin pairs come:

- the pin map: every pair of board pins a connection joins end to end (a direct mating
  pairs the contacts; a cable or harness is followed through its plugs and internal
  wires, a splice giving one row per pair of ends), with each pin's net on its board;
- system nets: board nets joined through mated pins, traced hop by hop across boards
  (trace_net_across_boards);
- the checks Altium's Connection Manager does not highlight. Five are Altium's own
  multi-board violations, with the report level the .PrjMbd sets for each (Different Net
  Names, Entry Is Empty, No Mated Part, No Net, Unresolved Conflict: Type114 to Type118);
  the others have no Altium counterpart: unwired cable or harness pins, two nets of one
  board joined through other boards, power-rail consistency across boards (voltages in
  rail names, ground carried between the boards), pins carrying a net that no connection
  mates, System=Connector parts the multi-board schematic does not place, connectors or
  pins deleted from a child project, and nets changed in a child project since the last
  import.

Different Net Names is common and often harmless (each board names a signal its own
way): a pair is info when the names look like one signal (one name a prefix of the
other, a shared tail, numbers with leading zeros, a common synonym, Altium's generated
names) and a warning or error when they suggest different signals (ground or a rail
against a signal, two rails of different voltages, the same stem with another index, P
against N, an active-low mark on one side only, the same bus or group name ending in
another signal, a swapped pair). It is a reading of names, not a proof.

When the .MbsDoc is missing or in a format the parser does not read, a user-supplied
mating map (mating=[{board_a, connector_a, board_b, connector_b, pin_map?}]) stands in
for its connections. Read-only: nothing is written and the loaded project is unchanged."""

import json
import logging
import os
import re
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

try:
    from altium import AltiumClient
    from parsers.mbs_doc import MbsDocError, MbsDocUnsupported, parse_mbs_doc
    from parsers.prj_mbd import PrjMbd, PrjMbdError, parse_prj_mbd
    from parsers.prj_pcb import parse_prj_pcb
    from services.bom import natural_key
    from services.decoupling import net_kind, net_tokens
    from services.derating import signed_rail_voltage
    from services.project_options import ERC_CHECKS, ERC_DEFAULTS, LEVELS
    from services.sch_netlister import file_signature
except ImportError:
    from server.altium import AltiumClient
    from server.parsers.mbs_doc import MbsDocError, MbsDocUnsupported, parse_mbs_doc
    from server.parsers.prj_mbd import PrjMbd, PrjMbdError, parse_prj_mbd
    from server.parsers.prj_pcb import parse_prj_pcb
    from server.services.bom import natural_key
    from server.services.decoupling import net_kind, net_tokens
    from server.services.derating import signed_rail_voltage
    from server.services.project_options import ERC_CHECKS, ERC_DEFAULTS, LEVELS
    from server.services.sch_netlister import file_signature

SEVERITIES = ("error", "warning", "info")
_SEVERITY_RANK = {severity: rank for rank, severity in enumerate(SEVERITIES)}
KINDS = ("direct", "wire", "cable", "harness")

# Our check -> Altium's multi-board violation type ([Electrical Rules Check] TypeN): the
# five checks Altium's multi-board Validate runs. The other checks have no counterpart.
ALTIUM_TYPES = {
    "different_net_names": 114, "entry_is_empty": 115, "no_mated_part": 116, "no_net": 117,
    "stored_conflict": 118,
}
CHECKS = ("different_net_names", "no_net", "no_mated_part", "entry_is_empty",
          "unwired_pin", "board_nets_joined", "power_rails", "unmated_pins",
          "connector_not_in_system", "connector_not_in_project", "net_names_unsynchronized",
          "stored_conflict", "board_not_loaded")

SEARCH_LEVELS = 2        # folders above the loaded project searched for a .PrjMbd
SEARCH_SUBFOLDERS = 200  # subfolders of each searched folder looked into
CACHE_SIZE = 6           # systems kept
CHILD_CACHE_SIZE = 16    # compiled child projects kept
SPLICE_ENDS = 64         # ends one board pin reaches through a cable or harness, at most
GROUP_EXAMPLES = 8       # examples listed in a grouped info finding
TRACE_DEPTH = 32         # hops followed from the start board

NOTE = ("Nets come from each child project compiled offline from its saved sheets (the "
        "loaded project's own netlist for that board); connections, entries and pin pairs "
        "from the multi-board schematic (.MbsDoc) as last saved. A name comparison is a "
        "reading of names, not a proof: Altium's Validate in the multi-board schematic is "
        "the final word.")
USER_MATING_NOTE = ("Connections come from the mating map passed with mating=, not from "
                    "the .MbsDoc: only the pins it names are joined.")

_lock = threading.Lock()
_systems: dict[tuple, "System"] = {}
_children: dict[str, tuple[tuple, dict]] = {}


# ---------- the model ----------

@dataclass
class BoardPin:
    key: str
    board: str | None       # the module (None: a cable's or harness's own plug pin)
    connection: str | None  # the cable or harness owning a plug pin
    part: str               # connector (or plug) designator
    number: str
    name: str = ""
    net: str | None = None         # the net on its board now
    stored_net: str | None = None  # ExternalNetName at the .MbsDoc's last import
    entry: str | None = None
    # child: net from the child project now; stored: the .MbsDoc's stored net (the board
    # did not compile, or its part or pin is not in the child project).
    net_from: str = "child"

    @property
    def label(self) -> str:
        return f"{self.part}-{self.number}"


@dataclass
class Connector:
    board: str
    designator: str
    ref: str | None = None       # the child project's designator it matched
    matched_by: str = "none"     # unique_id, designator, none
    comment: str = ""
    sheet: str = ""
    pins: list[str] = field(default_factory=list)
    system_connector: bool | None = None  # the child part has System=Connector
    missing_pins: list[str] = field(default_factory=list)  # pins the child part lacks


@dataclass
class EntryView:
    id: int | None
    owner: str          # board or connection key
    designator: str
    connection_id: int | None
    pins: list[str] = field(default_factory=list)
    parts: list[str] = field(default_factory=list)


@dataclass
class Board:
    key: str
    title: str = ""
    project_path: str | None = None
    project: str = ""
    pcb_file: str = ""
    netlist: dict | None = None
    netlist_source: str | None = None  # loaded, offline_compiler, stored_in_mbsdoc
    load_error: str | None = None
    connectors: dict[str, Connector] = field(default_factory=dict)
    entries: list[EntryView] = field(default_factory=list)
    order: int = 0

    def nets(self) -> dict:
        return (self.netlist or {}).get("nets") or {}


@dataclass
class Link:
    a: str
    b: str
    connection: str
    kind: str
    label: str = ""
    internal: bool = False


@dataclass
class Connection:
    key: str
    id: int | None
    kind: str
    type_name: str = ""
    plugs: dict[str, Connector] = field(default_factory=dict)
    entries: list[EntryView] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)
    source: dict = field(default_factory=dict)
    boards: list[str] = field(default_factory=list)
    unmatched_pins: list[str] = field(default_factory=list)  # user mating: one side only


@dataclass
class Pair:
    """Two board pins a connection joins end to end; b None: the path ends in a cable's
    or harness's plug pin, either one no wire continues (unwired) or one a wire reaches
    but no board pin mates (dead_end)."""
    id: int
    connection: str
    kind: str
    a: str
    b: str | None
    via: list[str] = field(default_factory=list)
    status: str = ""  # same_net, different_names, no_net, unconnected, unwired, dead_end


@dataclass
class System:
    prj_mbd: PrjMbd
    found_by: str
    mbs_path: str | None = None
    mbs_status: str = "missing"  # read, missing, unsupported, damaged, not_listed
    mbs_format: str | None = None
    mbs_message: str = ""
    doc: object = None
    source: str = "mbs_doc"      # mbs_doc, user_mating, none
    boards: dict[str, Board] = field(default_factory=dict)
    connections: dict[str, Connection] = field(default_factory=dict)
    pins: dict[str, BoardPin] = field(default_factory=dict)
    adjacency: dict[str, list[tuple[str, Link]]] = field(default_factory=dict)
    pairs: list[Pair] = field(default_factory=list)
    pairs_by_pin: dict[str, list[Pair]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    seconds: float = 0.0
    unmapped_pairs: int = 0  # pin pairs naming a pin id the document does not hold
    truncated_pins: int = 0  # board pins whose splice reaches more than SPLICE_ENDS pins
    inputs: list[str] = field(default_factory=list)  # files the model was built from
    input_signature: tuple = ()

    def altium_level(self, check: str) -> str | None:
        number = ALTIUM_TYPES.get(check)
        if number is None:
            return None
        raw = self.prj_mbd.erc_levels.get(f"Type{number}")
        try:
            level = int(raw) if raw not in (None, "") else ERC_DEFAULTS.get(number)
        except ValueError:
            level = ERC_DEFAULTS.get(number)
        return LEVELS[level] if level is not None and 0 <= level < len(LEVELS) else None


class MultiboardError(ValueError):
    """A structured tool error (payload)."""

    def __init__(self, payload: dict):
        super().__init__(payload.get("message", ""))
        self.payload = payload


def _error(code: str, message: str, **extra) -> MultiboardError:
    return MultiboardError({"error": code, "message": message, **extra})


def _norm(path: str) -> str:
    return os.path.normcase(str(Path(path).resolve()))


# ---------- finding the system ----------

def _prj_mbds(folder: Path) -> list[Path]:
    try:
        return sorted(p for p in folder.iterdir()
                      if p.is_file() and p.suffix.lower() == ".prjmbd")
    except OSError:
        return []


def _lists_project(prj_mbd: Path, project: str) -> bool:
    try:
        parsed = parse_prj_mbd(str(prj_mbd))
    except PrjMbdError:
        return False
    return any(_norm(document.path) == project for document in parsed.board_projects)


def locate_system(system: str | None, project_path: str | None) -> tuple[str, str]:
    """(path of the .PrjMbd, how it was found). system= is a .PrjMbd or a folder holding
    one; else the loaded project's folder and up to SEARCH_LEVELS folders above it (and
    their subfolders) are searched for a .PrjMbd listing the project. Raises
    MultiboardError."""
    if system is not None and str(system).strip():
        target = Path(str(system).strip().strip('"'))
        if target.is_file():
            if target.suffix.lower() != ".prjmbd":
                raise _error("not_a_prjmbd", f"'{target.name}' is not a multi-board project "
                             "(.PrjMbd): pass the .PrjMbd file or its folder.")
            return str(target.resolve()), "system_argument"
        if target.is_dir():
            found = _prj_mbds(target)
            if len(found) == 1:
                return str(found[0].resolve()), "system_argument"
            if found:
                raise _error("system_ambiguous", "The folder holds several .PrjMbd files: pass "
                             "one of them as system=.", candidates=[p.name for p in found])
        raise _error("system_not_found", "No .PrjMbd at system= (give the .PrjMbd file or the "
                     "folder holding it).")
    if not project_path:
        raise _error("no_project", "No project loaded. Call set_project_dir first, or pass "
                     "system= (a .PrjMbd or its folder).")
    project = _norm(project_path)
    folder = Path(project_path).resolve().parent
    candidates: list[tuple[Path, str]] = []
    seen: set[str] = set()
    for level in range(SEARCH_LEVELS + 1):
        if level and folder.parent == folder:
            break
        if level:
            folder = folder.parent
        found_by = "project_folder" if level == 0 else "parent_folder"
        places = [folder]
        if level and folder.parent != folder:  # never the subfolders of a drive's root
            try:
                subfolders = sorted(p for p in folder.iterdir() if p.is_dir())
            except OSError:
                subfolders = []
            places += subfolders[:SEARCH_SUBFOLDERS]
        for place in places:
            for prj_mbd in _prj_mbds(place):
                key = _norm(str(prj_mbd))
                if key in seen:
                    continue
                seen.add(key)
                if _lists_project(prj_mbd, project):
                    candidates.append((prj_mbd, found_by))
        if candidates:
            break
    if not candidates:
        raise _error("no_multiboard_system",
                     "No multi-board project (.PrjMbd) listing the loaded project was found in "
                     f"its folder or the {SEARCH_LEVELS} folders above it (and their "
                     "subfolders). Pass system= (a .PrjMbd or its folder) if it is elsewhere.",
                     prj_mbd_files_seen=len(seen))
    if len(candidates) > 1:
        raise _error("system_ambiguous", "Several multi-board projects list the loaded "
                     "project: pass one as system=.",
                     candidates=[str(p) for p, _ in candidates])
    return str(candidates[0][0].resolve()), candidates[0][1]


# ---------- child projects ----------

def project_files(path: str) -> list[str]:
    """A child project's own files (the .PrjPcb, its sheets and annotation files): what
    its offline compile reads."""
    if not Path(path).is_file():
        return [path]
    try:
        data = parse_prj_pcb(path)
    except Exception:  # the compile reports it
        return [path]
    return [path, *data.sheet_paths, *data.annotation_paths]


def _child_netlist(path: str, loaded: tuple[str, dict] | None) -> tuple[dict | None, str | None,
                                                                         str | None]:
    """(netlist, source, error) of a child project: the loaded project's netlist when it is
    that project, else an offline compile (cached by its files)."""
    key = _norm(path)
    if loaded and loaded[1] is not None and _norm(loaded[0]) == key:
        return loaded[1], "loaded", None
    if not Path(path).is_file():
        return None, None, "project_missing"
    try:
        data = parse_prj_pcb(path)
        signature = file_signature([path, *data.sheet_paths, *data.annotation_paths])
        with _lock:
            cached = _children.get(key)
        if cached is not None and cached[0] == signature:
            return cached[1], "offline_compiler", None
        netlist = AltiumClient().compile_offline(path, data.sheet_paths, data.annotation_paths)
    except Exception as failure:  # one board that cannot compile must not stop the system
        logging.warning("Multi-board child compile failed: %s", type(failure).__name__)
        return None, None, f"compile_failed ({type(failure).__name__})"
    with _lock:
        _children[key] = (signature, netlist)
        while len(_children) > CHILD_CACHE_SIZE:
            _children.pop(next(iter(_children)))
    return netlist, "offline_compiler", None


def _system_connector(component: dict | None) -> bool | None:
    if component is None:
        return None
    params = component.get("params") or {}
    return str(params.get("system", "")).strip().lower() == "connector"


def system_connectors(netlist: dict | None) -> list[str]:
    """The child project's parts with the parameter System=Connector."""
    return sorted((ref for ref, component in ((netlist or {}).get("components") or {}).items()
                   if _system_connector(component)), key=natural_key)


def _match_part(netlist: dict | None, designator: str, source_id: str) -> tuple[str | None, str]:
    """(the child project's designator, how) for a module connector."""
    components = (netlist or {}).get("components") or {}
    segments = [s for s in source_id.replace("/", "\\").split("\\") if s]
    if segments:
        path = "\\" + "\\".join(segments)
        for ref, component in components.items():
            if path in (component.get("unique_paths") or []):
                return ref, "unique_id"
        uid = segments[-1]
        owners = [ref for ref, component in components.items()
                  if component.get("unique_id") == uid or uid in (component.get("unique_ids")
                                                                   or [])]
        if designator in owners:
            return designator, "unique_id"
        if len(owners) == 1:
            return owners[0], "unique_id"
    if designator in components or designator in ((netlist or {}).get("pin_to_net") or {}):
        return designator, "designator"
    return None, "none"


def _pin_net(netlist: dict | None, ref: str | None, number: str) -> str | None:
    if netlist is None or ref is None:
        return None
    return ((netlist.get("pin_to_net") or {}).get(ref) or {}).get(number) or None


def _part_pins(netlist: dict | None, ref: str) -> list[str]:
    netlist = netlist or {}
    component = (netlist.get("components") or {}).get(ref) or {}
    numbers = set(component.get("pin_names") or {}) | set(component.get("pin_designators") or [])
    numbers |= set((netlist.get("pin_to_net") or {}).get(ref) or {})
    return sorted(numbers, key=natural_key)


# ---------- building ----------

def _unique_key(wanted: str, taken: set[str], fallback: str) -> str:
    key = wanted.strip() or fallback
    if key in taken:
        number = 2
        while f"{key}_{number}" in taken:
            number += 1
        key = f"{key}_{number}"
    taken.add(key)
    return key


def _read_mbs(system: System) -> None:
    documents = system.prj_mbd.schematics
    if not documents:
        system.mbs_status = "not_listed"
        system.mbs_message = "The .PrjMbd lists no multi-board schematic (.MbsDoc)."
        return
    document = documents[0]
    system.mbs_path = document.path
    if len(documents) > 1:
        system.warnings.append(f"The .PrjMbd lists {len(documents)} .MbsDoc files: the first "
                               f"({Path(document.path).name}) is read.")
    if not Path(document.path).is_file():
        system.mbs_status = "missing"
        system.mbs_message = f"{Path(document.path).name} is listed but not on disk."
        return
    try:
        system.doc = parse_mbs_doc(document.path)
    except MbsDocUnsupported as unsupported:
        system.mbs_status, system.mbs_format = "unsupported", unsupported.format
        system.mbs_message = f"{Path(document.path).name}: {str(unsupported).rstrip('.')}."
        return
    except MbsDocError as damaged:
        system.mbs_status = "damaged"
        system.mbs_message = f"{Path(document.path).name}: {str(damaged).rstrip('.')}."
        return
    system.mbs_status, system.mbs_format = "read", system.doc.format
    system.warnings.extend(system.doc.warnings)


def _boards_from_doc(system: System, loaded) -> None:
    doc = system.doc
    base = Path(system.mbs_path).parent
    listed = {_norm(document.path) for document in system.prj_mbd.board_projects}
    taken: set[str] = set()
    used_projects: set[str] = set()
    for order, module in enumerate(doc.modules):
        key = _unique_key(module.designator, taken, f"M#{module.id}")
        if module.designator.strip() and key != module.designator.strip():
            system.warnings.append(f"Two modules are designated {module.designator.strip()}: "
                                   f"this one is named {key} here (annotate the modules).")
        project_path = (str((base / module.source_project.replace("\\", "/")).resolve())
                        if module.source_project else None)
        board = Board(key=key, title=module.title, project_path=project_path,
                      project=Path(module.source_project.replace("\\", "/")).stem,
                      pcb_file=Path(module.board_file.replace("\\", "/")).name, order=order)
        if project_path:
            used_projects.add(_norm(project_path))
            if _norm(project_path) not in listed:
                system.warnings.append(f"Module {key}'s project {board.project} is not listed in "
                                       "the .PrjMbd.")
            board.netlist, board.netlist_source, board.load_error = _child_netlist(
                project_path, loaded)
        else:
            board.load_error = "no_source_project"
        if board.netlist is None:
            board.netlist_source = "stored_in_mbsdoc"
        for component in module.components:
            ref, matched_by = _match_part(board.netlist, component.designator,
                                          component.source_id)
            child = ((board.netlist or {}).get("components") or {}).get(ref) if ref else None
            connector = Connector(board=key, designator=component.designator, ref=ref,
                                  matched_by=matched_by, comment=component.comment,
                                  sheet=component.sheet,
                                  system_connector=_system_connector(child))
            # A part or pin the child project no longer has keeps the stored net, so one
            # deletion is one finding (connector_not_in_project), not a cascade of open pins.
            numbers = set(_part_pins(board.netlist, ref)) if ref else set()
            for pin in component.pins:
                pin_key = f"p{pin.id}"
                live = board.netlist is not None and ref is not None and (
                    not numbers or pin.number in numbers)
                net = (_pin_net(board.netlist, ref, pin.number) if live
                       else (pin.external_net or None))
                system.pins[pin_key] = BoardPin(
                    key=pin_key, board=key, connection=None, part=component.designator,
                    number=pin.number, name=pin.name, net=net,
                    stored_net=pin.external_net or None,
                    net_from="child" if live else "stored")
                connector.pins.append(pin_key)
                if board.netlist is not None and ref is not None and not live:
                    connector.missing_pins.append(pin_key)
            board.connectors[component.designator] = connector
        for entry in module.entries:
            view = EntryView(id=entry.id, owner=key, designator=entry.designator,
                             connection_id=entry.connection_id,
                             pins=[f"p{pin}" for pin in entry.pin_ids])
            parts = []
            for pin_key in view.pins:
                pin = system.pins.get(pin_key)
                if pin is not None:
                    pin.entry = entry.designator
                    if pin.part not in parts:
                        parts.append(pin.part)
            view.parts = parts
            board.entries.append(view)
        system.boards[key] = board
    _natural_order(system)
    for document in system.prj_mbd.board_projects:
        if _norm(document.path) not in used_projects:
            system.warnings.append(f"The .PrjMbd lists {Path(document.path).name}, which no "
                                   "module of the .MbsDoc places.")


def _natural_order(system: System) -> None:
    for order, board in enumerate(sorted(system.boards.values(),
                                         key=lambda b: natural_key(b.key))):
        board.order = order


def _connections_from_doc(system: System) -> None:
    doc = system.doc
    taken: set[str] = set()
    for connection in doc.connections:
        key = _unique_key(connection.designator, taken, f"#{connection.id}")
        view = Connection(key=key, id=connection.id, kind=connection.kind,
                          type_name=connection.type_name, source=connection.source)
        for component in connection.components:
            plug = Connector(board="", designator=component.designator,
                             comment=component.comment)
            for pin in component.pins:
                pin_key = f"p{pin.id}"
                system.pins[pin_key] = BoardPin(
                    key=pin_key, board=None, connection=key, part=component.designator,
                    number=pin.number, name=pin.name, net=None,
                    stored_net=pin.external_net or None)
                plug.pins.append(pin_key)
            view.plugs[str(component.id)] = plug
        # A plug is named by its entry (P1) rather than its unannotated designator (P?).
        for entry in connection.entries:
            entry_view = EntryView(id=entry.id, owner=key, designator=entry.designator,
                                   connection_id=connection.id,
                                   pins=[f"p{pin}" for pin in entry.pin_ids])
            for pin_key in entry_view.pins:
                pin = system.pins.get(pin_key)
                if pin is not None and pin.connection == key:
                    pin.entry = entry.designator
                    if entry.designator and pin.part.endswith("?"):
                        pin.part = entry.designator
            view.entries.append(entry_view)
        system.connections[key] = view
    by_id = {view.id: view for view in system.connections.values()}
    for connection in doc.connections:
        view = by_id[connection.id]
        for link in connection.links:
            for pair in link.pairs:
                a, b = f"p{pair.pin1}", f"p{pair.pin2}"
                if a not in system.pins or b not in system.pins:
                    system.unmapped_pairs += 1
                    continue
                view.links.append(Link(a=a, b=b, connection=view.key, kind=view.kind,
                                       label=pair.designator, internal=link.internal))
    for board in system.boards.values():
        for entry in board.entries:
            if entry.connection_id in by_id:
                view = by_id[entry.connection_id]
                if board.key not in view.boards:
                    view.boards.append(board.key)
    if system.unmapped_pairs:
        system.warnings.append(f"{system.unmapped_pairs} pin pairs name a pin the .MbsDoc does "
                               "not hold: left out.")


def _boards_from_projects(system: System, loaded) -> None:
    taken: set[str] = set()
    for order, document in enumerate(system.prj_mbd.board_projects):
        key = _unique_key(Path(document.path).stem, taken, f"B{order + 1}")
        board = Board(key=key, project_path=document.path, project=Path(document.path).stem,
                      order=order)
        board.netlist, board.netlist_source, board.load_error = _child_netlist(
            document.path, loaded)
        system.boards[key] = board
    _natural_order(system)


MATING_HELP = ("mating is a list of {board_a, connector_a, board_b, connector_b, pin_map?, "
               "kind?, name?}: boards by module designator, title or project name, connectors "
               "by designator; pin_map maps board_a's pins to board_b's ({'1': '2'} or "
               "[['1', '2']]; default: the same pin numbers); kind is direct, wire, cable or "
               "harness (default direct).")


def _side(spec: dict, side: str):
    nested = spec.get(side)
    if isinstance(nested, dict):
        return nested.get("board"), nested.get("connector")
    return spec.get(f"board_{side}"), spec.get(f"connector_{side}")


def _user_connector(system: System, board: Board, designator: str, index: int) -> Connector:
    netlist = board.netlist
    if netlist is None:
        raise _error("invalid_mating", f"mating[{index}]: board {board.key} has no netlist "
                     f"({board.load_error or 'not loaded'}).", help=MATING_HELP)
    components = netlist.get("components") or {}
    ref = designator if designator in components or designator in (
        netlist.get("pin_to_net") or {}) else next(
        (name for name in components if name.lower() == designator.lower()), None)
    if ref is None:
        raise _error("invalid_mating", f"mating[{index}]: no part {designator} on board "
                     f"{board.key}.", help=MATING_HELP)
    existing = board.connectors.get(ref)
    if existing is not None and existing.pins and existing.pins[0].startswith("u:"):
        return existing
    connector = Connector(board=board.key, designator=ref, ref=ref, matched_by="designator",
                          comment=(components.get(ref) or {}).get("value") or "",
                          system_connector=_system_connector(components.get(ref)))
    for number in _part_pins(netlist, ref):
        pin_key = f"u:{board.key}:{ref}:{number}"
        pin_name = ((components.get(ref) or {}).get("pin_names") or {}).get(number, "")
        system.pins[pin_key] = BoardPin(key=pin_key, board=board.key, connection=None, part=ref,
                                        number=number, name=pin_name,
                                        net=_pin_net(netlist, ref, number))
        connector.pins.append(pin_key)
    board.connectors[ref] = connector
    return connector


def _pin_map(raw, index: int) -> list[tuple[str, str]] | None:
    if raw is None:
        return None
    pairs: list[tuple[str, str]] = []
    if isinstance(raw, dict):
        pairs = [(str(a).strip(), str(b).strip()) for a, b in raw.items()]
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                pairs.append((str(item[0]).strip(), str(item[1]).strip()))
            elif isinstance(item, str) and re.fullmatch(r"\s*[^:=\s]+\s*[:=]\s*[^:=\s]+\s*", item):
                a, b = re.split(r"\s*[:=]\s*", item.strip(), maxsplit=1)
                pairs.append((a, b))
            else:
                raise _error("invalid_mating", f"mating[{index}].pin_map: each item is "
                             "['pin_a', 'pin_b'] or 'pin_a:pin_b'.", help=MATING_HELP)
    else:
        raise _error("invalid_mating", f"mating[{index}].pin_map is an object or a list.",
                     help=MATING_HELP)
    return pairs


def _connections_from_mating(system: System, mating: list) -> None:
    if not isinstance(mating, list) or not mating:
        raise _error("invalid_mating", "mating is a non-empty list.", help=MATING_HELP)
    taken: set[str] = set()
    uses: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    pairs_named: dict[frozenset, list[str]] = defaultdict(list)
    for index, spec in enumerate(mating):
        if not isinstance(spec, dict):
            raise _error("invalid_mating", f"mating[{index}] is not an object.", help=MATING_HELP)
        ends = []
        for side in ("a", "b"):
            board_text, connector_text = _side(spec, side)
            if not isinstance(board_text, str) or not isinstance(connector_text, str) \
                    or not board_text.strip() or not connector_text.strip():
                raise _error("invalid_mating", f"mating[{index}]: board_{side} and "
                             f"connector_{side} are required.", help=MATING_HELP)
            boards = find_boards(system, board_text)
            if len(boards) != 1:
                raise _error("invalid_mating", f"mating[{index}]: board '{board_text}' names "
                             f"{'no board' if not boards else 'several boards'}.",
                             boards=board_list(system), help=MATING_HELP)
            ends.append(_user_connector(system, boards[0], connector_text.strip(), index))
        if ends[0] is ends[1]:
            raise _error("invalid_mating", f"mating[{index}] mates {ends[0].board} "
                         f"{ends[0].designator} with itself: name two different connectors.",
                         help=MATING_HELP)
        kind = str(spec.get("kind") or "direct").strip().lower()
        if kind not in KINDS:
            raise _error("invalid_mating", f"mating[{index}].kind is one of {', '.join(KINDS)}.",
                         help=MATING_HELP)
        name = spec.get("name")
        key = _unique_key(name.strip() if isinstance(name, str) else "", taken, f"U{index + 1}")
        for end in ends:
            uses[(end.board, end.designator)].append((key, kind))
        pairs_named[frozenset((end.board, end.designator) for end in ends)].append(key)
        view = Connection(key=key, id=None, kind=kind, type_name="user_mating",
                          boards=sorted({ends[0].board, ends[1].board}))
        by_number = [{system.pins[k].number: k for k in end.pins} for end in ends]
        wanted = _pin_map(spec.get("pin_map"), index)
        if wanted is None:
            numbers = sorted(set(by_number[0]) & set(by_number[1]), key=natural_key)
            wanted = [(number, number) for number in numbers]
            for side, other, end in ((0, 1, ends[0]), (1, 0, ends[1])):
                view.unmatched_pins += [f"{end.board} {end.designator}-{number}"
                                        for number in sorted(set(by_number[side])
                                                             - set(by_number[other]),
                                                             key=natural_key)]
        for pin_a, pin_b in wanted:
            if pin_a not in by_number[0] or pin_b not in by_number[1]:
                missing = (f"{ends[0].designator}-{pin_a}" if pin_a not in by_number[0]
                           else f"{ends[1].designator}-{pin_b}")
                raise _error("invalid_mating", f"mating[{index}].pin_map: no pin {missing}.",
                             help=MATING_HELP)
            view.links.append(Link(a=by_number[0][pin_a], b=by_number[1][pin_b],
                                   connection=key, kind=kind, label=pin_a))
        system.connections[key] = view
    for keys in pairs_named.values():
        if len(keys) > 1:
            system.warnings.append(f"The matings {', '.join(keys)} join the same two "
                                   "connectors: a pin pair they both name is listed once, "
                                   f"under the first ({keys[0]}).")
    for (board, designator), used in uses.items():
        direct = [key for key, kind in used if kind == "direct"]
        if len(direct) > 1:
            system.warnings.append(
                f"{board} {designator} is in {len(direct)} direct matings ({', '.join(direct)}), "
                "but a connector mates once: their nets are joined through it. Without the "
                ".MbsDoc the boards are named per project, so a project placed several times "
                "cannot be told apart; give it as a cable or harness if that is what joins them.")


def _index(system: System) -> None:
    adjacency: dict[str, list[tuple[str, Link]]] = defaultdict(list)
    for connection in system.connections.values():
        for link in connection.links:
            adjacency[link.a].append((link.b, link))
            adjacency[link.b].append((link.a, link))
        for link in connection.links:
            for key in (link.a, link.b):
                board = system.pins[key].board
                if board and board not in connection.boards:
                    connection.boards.append(board)
        connection.boards.sort(key=lambda b: system.boards[b].order if b in system.boards else 0)
    system.adjacency = dict(adjacency)
    seen: set[tuple] = set()
    pairs: list[Pair] = []
    for key in sorted((k for k, pin in system.pins.items() if pin.board and k in adjacency),
                      key=lambda k: pin_order(system, k)):
        parent: dict[str, tuple[str, Link] | None] = {key: None}
        queue, ends, last = [key], [], key
        while queue and len(ends) < SPLICE_ENDS:
            current = queue.pop(0)
            for other, link in adjacency.get(current, ()):
                if other in parent:
                    continue
                parent[other] = (current, link)
                if system.pins[other].board:
                    ends.append(other)
                else:
                    queue.append(other)
                    last = other
        if queue and len(ends) >= SPLICE_ENDS:
            system.truncated_pins += 1
        if not ends and len(parent) > 1:
            ends = [None]
        for end in ends:
            node = end if end is not None else last
            path, first_link = [], None
            while parent.get(node) is not None:
                previous, link = parent[node]
                first_link = link
                if not system.pins[node].board:
                    path.append(system.pins[node].label)
                node = previous
            path.reverse()
            ordered = sorted([key, end], key=lambda k: pin_order(system, k)) if end else [key, None]
            marker = (first_link.connection, ordered[0], ordered[1])
            if marker in seen:
                continue
            seen.add(marker)
            if end is not None and ordered[0] != key:
                path.reverse()
            pair = Pair(id=len(pairs), connection=first_link.connection, kind=first_link.kind,
                        a=ordered[0], b=ordered[1], via=path)
            pair.status = _pair_status(system, pair)
            pairs.append(pair)
    pairs.sort(key=lambda pair: (natural_key(pair.connection), pin_order(system, pair.a),
                                 pin_order(system, pair.b) if pair.b else ()))
    for number, pair in enumerate(pairs):
        pair.id = number
    system.pairs = pairs
    by_pin: dict[str, list[Pair]] = defaultdict(list)
    for pair in pairs:
        by_pin[pair.a].append(pair)
        if pair.b:
            by_pin[pair.b].append(pair)
    system.pairs_by_pin = dict(by_pin)
    if system.truncated_pins:
        system.warnings.append(
            f"{system.truncated_pins} board pins reach more than {SPLICE_ENDS} other board pins "
            "through one cable or harness splice: their pin table rows stop at about "
            f"{SPLICE_ENDS} per pin (the system nets still join them all).")


def pin_order(system: System, key: str):
    pin = system.pins[key]
    board = system.boards.get(pin.board or "")
    return (board.order if board else 10**6, natural_key(pin.part), natural_key(pin.number))


def _pair_status(system: System, pair: Pair) -> str:
    if pair.b is None:
        # The walk stopped in a plug pin: the first one (no wire on) or one a wire reaches
        # that no board pin mates.
        return "unwired" if len(pair.via) <= 1 else "dead_end"
    a, b = system.pins[pair.a].net, system.pins[pair.b].net
    if a and b:
        return "same_net" if a == b else "different_names"
    if a or b:
        return "no_net"
    return "unconnected"


def build_system(prj_mbd_path: str, found_by: str, mating: list | None = None,
                 loaded: tuple[str, dict] | None = None) -> System:
    """The system model of a .PrjMbd. mating replaces the .MbsDoc's connections. Raises
    MultiboardError for a .PrjMbd that cannot be read or a bad mating map."""
    started = time.perf_counter()
    try:
        prj = parse_prj_mbd(prj_mbd_path)
    except PrjMbdError as error:
        raise _error("prj_mbd_unreadable", f"{Path(prj_mbd_path).name}: {error}.") from error
    system = System(prj_mbd=prj, found_by=found_by)
    _read_mbs(system)
    if system.doc is not None:
        _boards_from_doc(system, loaded)
    else:
        _boards_from_projects(system, loaded)
    if mating is not None:
        system.source = "user_mating"
        _connections_from_mating(system, mating)
    elif system.doc is not None:
        _connections_from_doc(system)
    else:
        system.source = "none"
    _index(system)
    system.inputs = _inputs(system)
    system.input_signature = file_signature(system.inputs)
    system.seconds = round(time.perf_counter() - started, 3)
    return system


def _inputs(system: System) -> list[str]:
    """Every file the model was read from: the .PrjMbd, its multi-board schematics, the
    child projects it lists and each board's project with its sheets (a module may place
    a project the .PrjMbd omits). The .MbaDoc and other documents are not read."""
    files = [system.prj_mbd.path, *(d.path for d in system.prj_mbd.schematics)]
    for document in system.prj_mbd.board_projects:
        files += project_files(document.path)
    for board in system.boards.values():
        if board.project_path:
            files += project_files(board.project_path)
    seen: set[str] = set()
    return [f for f in files if not (_norm(f) in seen or seen.add(_norm(f)))]


def cached_system(prj_mbd_path: str, found_by: str, mating: list | None = None,
                  loaded: tuple[str, dict] | None = None, loaded_stamp=None) -> System:
    """build_system, reused while every file it was read from (the .PrjMbd, its documents,
    each board's project and sheets), the mating map and the loaded netlist are
    unchanged."""
    key = (_norm(prj_mbd_path), found_by,
           json.dumps(mating, sort_keys=True, default=str) if mating is not None else None,
           _norm(loaded[0]) if loaded else None, id(loaded[1]) if loaded else None,
           loaded_stamp)
    with _lock:
        system = _systems.get(key)
    if system is not None and file_signature(system.inputs) == system.input_signature:
        return system
    system = build_system(prj_mbd_path, found_by, mating, loaded)
    with _lock:
        _systems.pop(key, None)
        _systems[key] = system
        while len(_systems) > CACHE_SIZE:
            _systems.pop(next(iter(_systems)))
    return system


def clear_cache() -> None:
    with _lock:
        _systems.clear()
        _children.clear()


# ---------- names ----------

# Altium's generated names: Net<designator>_<pin> (NetR1_2, NetU3_A7, NetC295_1). The
# designator always carries a number, so Network_Reset is a name of its own.
_AUTO_NAME = re.compile(r"^Net[A-Za-z]+\d+[A-Za-z]*_\w+$|^N\d{5,}$")
# A user's generic numbering (PCB_A(Net1), CAN_NET2): the user's own name, but one that
# says nothing about the signal.
_NUMBERED = re.compile(r"^NET\d+$")
_GENERIC_TOKEN = re.compile(r"^(?:PIN\d*|P\d+|J\d+|CN\d+|CON\d*|CONN\d*|X\d+|IO\d+|GPIO\d*|"
                            r"NET\d+)$")
_INDEXED = re.compile(r"^([A-Z0-9]*[A-Z])(\d+)$")
_POLARITY_MARKS = {"P": "P", "POS": "P", "N": "N", "NEG": "N", "H": "H", "HI": "H",
                   "L": "L", "LO": "L"}
_OPPOSITE = ({"P", "N"}, {"H", "L"})
_ACTIVE_LOW = ("N", "L", "B", "BAR")
_MARKS = frozenset(_POLARITY_MARKS) | frozenset(_ACTIVE_LOW)
# Each board names its own side of a crossed link: a UART's TX is the other side's RX, an
# SPI device's SDO the host's SDI. MOSI and MISO name the bus: the same on both boards.
_CROSSED = {"TX": "RX", "RX": "TX", "TXD": "RXD", "RXD": "TXD", "SDO": "SDI", "SDI": "SDO"}
# Common names of one signal.
_SYNONYMS = {"SCK": "CLK", "SCLK": "CLK", "CK": "CLK", "SS": "CS", "RST": "RESET",
             "IRQ": "INT", "SDATA": "SDA"}


def name_class(net: str) -> str:
    """ground, power, auto (a name Altium generated: NetR1_2) or signal."""
    kind = net_kind(net)
    if kind["ground"]:
        return "ground"
    if kind["power"] and not kind["status"]:
        return "power"
    if _AUTO_NAME.match(net.strip()):
        return "auto"
    return "signal"


def _tokens(net: str) -> list[str]:
    """The name's parts in upper case with leading zeros dropped (DQ05 -> DQ5); a trailing
    + or - is P or N (USB_D+), an active-low notation (RESET#, ~RESET, Altium's overline
    R\\E\\S\\E\\T\\) a trailing B."""
    text = net.strip()
    low = "\\" in text or text.endswith("#") or text[:1] in ("~", "!")
    text = text.replace("\\", "").rstrip("#").lstrip("~!")
    text = re.sub(r"(?<=[A-Za-z0-9])\+(?=$|[_\s/.()])", "_P", text)
    text = re.sub(r"(?<=[A-Za-z0-9])-(?=$|[_\s/.()])", "_N", text)
    raw = net_tokens(text.replace("(", "_").replace(")", "_").replace(".", "_"))
    tokens = [re.sub(r"(?<!\d)0+(?=\d)", "", token) for token in raw]
    if low and tokens:
        tokens.append("B")
    return tokens


def _common_tail(a: list[str], b: list[str]) -> int:
    count = 0
    while count < min(len(a), len(b)) and a[-1 - count] == b[-1 - count]:
        count += 1
    return count


def _polarity(tokens: list[str]) -> tuple[list[str], str | None]:
    """(the stem, P / N / H / L) of a differential half's name: CLK_P, USB_DN, USB_DM,
    USB_D+, CANH."""
    if not tokens:
        return tokens, None
    last = tokens[-1]
    if last in _POLARITY_MARKS and len(tokens) > 1:
        return tokens[:-1], _POLARITY_MARKS[last]
    match = re.fullmatch(r"(.*\d|.*D|CAN)(P|N|H|L)", last)
    if match:
        return [*tokens[:-1], match[1]], match[2]
    match = re.fullmatch(r"(.*D)M", last)
    if match:
        return [*tokens[:-1], match[1]], "N"
    return tokens, None


def _low_forms(tokens: list[str]) -> set[tuple[str, bool]]:
    """(the name without an active-low mark, whether it had one): RESET_N, RESET_L,
    RESET#, nRESET -> (RESET, True); RESET -> (RESET, False)."""
    forms = {("".join(tokens), False)}
    if len(tokens) > 1 and tokens[-1] in _ACTIVE_LOW:
        forms.add(("".join(tokens[:-1]), True))
    if tokens and tokens[0].startswith("N") and len(tokens[0]) > 3:
        forms.add(("".join([tokens[0][1:], *tokens[1:]]), True))
    return forms


def _active_low(a: list[str], b: list[str]) -> str | None:
    """same (one name in two active-low notations: RESET_N and nRESET), differs (an
    active-low mark on one side only: RESET_N and RESET) or None."""
    matches = {(low_a, low_b) for core_a, low_a in _low_forms(a)
               for core_b, low_b in _low_forms(b) if core_a == core_b}
    if not matches:
        return None
    return "same" if any(low_a == low_b for low_a, low_b in matches) else "differs"


def _index_differs(a: str, b: str) -> bool:
    """Two parts of one stem with other numbers (I2C1 and I2C2, UART1 and UART2); a
    connector-style part (J1, P2, CN3) names each board's own connector instead."""
    if _GENERIC_TOKEN.match(a) or _GENERIC_TOKEN.match(b):
        return False
    match_a, match_b = _INDEXED.match(a), _INDEXED.match(b)
    return bool(match_a and match_b and match_a[1] == match_b[1] and match_a[2] != match_b[2])


def _suffix_of(base: list[str], other: list[str]) -> bool:
    """other is base plus a one-letter part (LINK and LINK_C, DATA_0 and DATA0_G)."""
    return (len(other) >= 2 and len(other[-1]) == 1 and other[-1].isalpha()
            and other[-1] not in _MARKS and "".join(other[:-1]) == "".join(base))


def compare_names(a: str, b: str) -> tuple[str, str]:
    """(reason, severity) of two different net names on mated pins."""
    class_a, class_b = name_class(a), name_class(b)
    classes = {class_a, class_b}
    volts_a, volts_b = signed_rail_voltage(a), signed_rail_voltage(b)
    if classes == {"ground"}:
        return "ground_renamed", "info"
    if classes == {"ground", "power"}:
        return "ground_to_power", "error"
    if "ground" in classes:
        return ("ground_to_signal", "warning") if "signal" in classes else (
            "ground_to_auto_name", "info")
    if classes == {"power"}:
        if volts_a is not None and volts_b is not None and abs(volts_a - volts_b) > 0.01:
            return "different_voltages", "error"
        return "rail_renamed", "info"
    if "power" in classes:
        return ("power_to_signal", "warning") if "signal" in classes else (
            "power_to_auto_name", "info")
    if "auto" in classes:
        return "auto_named", "info"
    tokens_a, tokens_b = _tokens(a), _tokens(b)
    if not tokens_a or not tokens_b:
        return "renamed", "info"
    if tokens_a == tokens_b or "".join(tokens_a) == "".join(tokens_b):
        return "same_after_normalizing", "info"
    canon_a = [_SYNONYMS.get(token, token) for token in tokens_a]
    canon_b = [_SYNONYMS.get(token, token) for token in tokens_b]
    if canon_a == canon_b:
        return "synonym", "info"
    stem_a, polarity_a = _polarity(canon_a)
    stem_b, polarity_b = _polarity(canon_b)
    if polarity_a and polarity_b and {polarity_a, polarity_b} in _OPPOSITE \
            and stem_a and stem_b and _common_tail(stem_a, stem_b) >= 1:
        return "polarity_differs", "warning"
    tail = _common_tail(canon_a, canon_b)
    head_a, head_b = canon_a[:len(canon_a) - tail], canon_b[:len(canon_b) - tail]
    if tail and len(head_a) == 1 and len(head_b) == 1 and _index_differs(head_a[0], head_b[0]):
        return "index_differs", "warning"  # QBUS1_CLK and QBUS2_CLK: another bus
    shorter = min(len(canon_a), len(canon_b))
    if tail == shorter or tail >= 2 or (tail == 1 and len(canon_a[-1]) >= 3):
        return "prefix_differs", "info"
    low = _active_low(canon_a, canon_b)
    if low == "same":
        return "same_after_normalizing", "info"
    if low == "differs":
        return "active_low_differs", "warning"
    if polarity_a == polarity_b and stem_a and stem_b:
        if _NUMBERED.match(stem_a[-1]) and _NUMBERED.match(stem_b[-1]) \
                and stem_a[:-1] != stem_b[:-1]:
            return "numbered_per_board", "info"
        last_a, last_b = _INDEXED.match(stem_a[-1]), _INDEXED.match(stem_b[-1])
        if last_a and last_b and (last_a[1].endswith(last_b[1])
                                  or last_b[1].endswith(last_a[1])):
            if last_a[2] != last_b[2]:
                return "index_differs", "warning"
            return "abbreviated", "info"
    crossed = [_CROSSED.get(token, token) for token in canon_a]
    if crossed != canon_a and _common_tail(crossed, canon_b) == min(len(crossed),
                                                                    len(canon_b)):
        return "tx_rx_crossed", "info"
    if _suffix_of(canon_a, canon_b) or _suffix_of(canon_b, canon_a):
        return "suffix_differs", "info"
    if any(_GENERIC_TOKEN.match(token) for token in canon_a + canon_b):
        return "generic_name", "info"
    if (set(canon_a) & set(canon_b)) - _MARKS:
        return "same_group_other_signal", "warning"
    return "unrelated_names", "warning"


def _volts(net: str | None) -> float | None:
    return signed_rail_voltage(net) if net else None


# ---------- views ----------

def board_list(system: System) -> list[dict]:
    rows = []
    for board in sorted(system.boards.values(), key=lambda b: b.order):
        row = {"board": board.key}
        if board.title:
            row["title"] = board.title
        row["project"] = board.project
        if board.pcb_file:
            row["pcb_file"] = board.pcb_file
        row["netlist_source"] = board.netlist_source
        if board.load_error:
            row["load_error"] = board.load_error
        row["connectors"] = sorted(board.connectors, key=natural_key)
        if board.netlist is not None:
            row["system_connector_parts"] = len(system_connectors(board.netlist))
        rows.append(row)
    return rows


def connection_list(system: System) -> list[dict]:
    rows = []
    for connection in system.connections.values():
        pairs = [p for p in system.pairs if p.connection == connection.key]
        row = {"connection": connection.key, "kind": connection.kind,
               "boards": list(connection.boards), "pin_pairs": len(pairs)}
        ends = sorted({(system.pins[k].board, system.pins[k].part)
                       for p in pairs for k in (p.a, p.b) if k},
                      key=lambda e: (system.boards[e[0]].order, natural_key(e[1])))
        row["connectors"] = [f"{board} {part}" for board, part in ends]
        if connection.plugs:
            row["plugs"] = sorted({system.pins[k].part for plug in connection.plugs.values()
                                   for k in plug.pins}, key=natural_key)
        if connection.kind == "unknown":
            row["type"] = connection.type_name
        if connection.source:
            row["source"] = {key: value for key, value in connection.source.items()
                             if isinstance(value, (str, int, float))}
        if connection.unmatched_pins:
            row["pins_on_one_side_only"] = len(connection.unmatched_pins)
        rows.append(row)
    return rows


def header(system: System) -> dict:
    mbs: dict = {"status": system.mbs_status}
    if system.mbs_path:
        mbs["document"] = Path(system.mbs_path).name
    if system.mbs_format:
        mbs["format"] = system.mbs_format
    if system.mbs_message:
        mbs["message"] = system.mbs_message
    if system.doc is not None:
        mbs["version"] = system.doc.version
        if system.doc.modified:
            mbs["modified"] = system.doc.modified
        mbs["modules"] = len(system.doc.modules)
        mbs["connections"] = len(system.doc.connections)
        mbs["power_ports"] = len(system.doc.power_ports)
        mbs["stored_conflicts"] = len(system.doc.conflicts)
        if system.doc.extra:
            mbs["unknown_keys"] = sorted(system.doc.extra)[:20]
    out = {
        "system": {
            "prj_mbd": Path(system.prj_mbd.path).name,
            "path": system.prj_mbd.path,
            "found_by": system.found_by,
            "multiboard_schematic": mbs,
            "connections_source": system.source,
        },
        "boards": board_list(system),
    }
    assemblies = system.prj_mbd.assemblies
    if assemblies:
        out["system"]["assembly"] = {"document": Path(assemblies[0].path).name,
                                     "read": False,
                                     "note": "The 3D assembly (.MbaDoc) adds placement only, "
                                             "no connectivity: it is not read."}
    if system.warnings:
        out["system"]["warnings"] = system.warnings[:20]
    out["note"] = USER_MATING_NOTE + " " + NOTE if system.source == "user_mating" else NOTE
    return out


def _end(system: System, key: str | None) -> dict | None:
    if key is None:
        return None
    pin = system.pins[key]
    row = {"board": pin.board, "connector": pin.part, "pin": pin.number}
    if pin.name and pin.name != pin.number:
        row["pin_name"] = pin.name
    row["net"] = pin.net
    return row


def _pin_text(system: System, key: str) -> str:
    pin = system.pins[key]
    return f"{pin.board} {pin.part}-{pin.number}" + (f" {pin.net}" if pin.net else " (no net)")


def pin_rows(system: System, board: list[str] | None = None, connection: str | None = None,
             net: str | None = None) -> list[dict]:
    rows = []
    wanted_net = net.strip().lower() if net and net.strip() else None
    swapped = _swapped(system, system.pairs)
    for pair in system.pairs:
        if connection and pair.connection.lower() != connection.lower():
            continue
        ends = [k for k in (pair.a, pair.b) if k]
        if board and not any(system.pins[k].board in board for k in ends):
            continue
        if wanted_net and not any((system.pins[k].net or "").lower() == wanted_net
                                  for k in ends):
            continue
        row = {"connection": pair.connection, "kind": pair.kind, "a": _end(system, pair.a),
               "b": _end(system, pair.b), "status": pair.status}
        if pair.via:
            row["via"] = " > ".join(pair.via)
        if pair.status == "different_names":
            reason, severity = pair_reading(system, pair, swapped)
            row["names"] = {"reason": reason, "severity": severity}
        rows.append(row)
    return rows


def find_boards(system: System, text: str) -> list[Board]:
    wanted = text.strip().lower()
    if not wanted:
        return []
    for field_name in ("key", "title"):
        found = [b for b in system.boards.values()
                 if str(getattr(b, field_name) or "").lower() == wanted]
        if found:
            return found
    stem = Path(wanted).stem if wanted.endswith(".prjpcb") else wanted
    return [b for b in system.boards.values() if b.project.lower() == stem]


def resolve_boards(system: System, board: str | None) -> list[str] | None:
    if board is None or not str(board).strip():
        return None
    found = find_boards(system, str(board))
    if not found:
        raise _error("board_not_found", f"No board '{board}' in the system: name a module "
                     "designator, its title or its project.", boards=board_list(system))
    return [b.key for b in found]


def resolve_connection(system: System, connection: str | None) -> str | None:
    if connection is None or not str(connection).strip():
        return None
    wanted = str(connection).strip().lower()
    for key in system.connections:
        if key.lower() == wanted:
            return key
    raise _error("connection_not_found", f"No connection '{connection}' in the system.",
                 connections=[c["connection"] for c in connection_list(system)])


# ---------- checks ----------

NO_REPORT_NOTE = "The .PrjMbd sets this check to No Report: shown as info."


def _finding(system: System, check: str, severity: str, message: str, **fields) -> dict:
    number = ALTIUM_TYPES.get(check)
    level = system.altium_level(check) if number is not None else None
    note = None
    if level == "No Report" and severity == "warning":
        severity, note = "info", NO_REPORT_NOTE  # errors stay errors
    row = {"check": check, "severity": severity}
    if number is not None:
        row["altium_check"] = ERC_CHECKS[number - 1]
        row["altium_level"] = level
    row.update({key: value for key, value in fields.items() if value is not None})
    row["message"] = message
    if note:
        row["note"] = note
    return row


_MOUNT_PIN = re.compile(r"^(?:MP|MH|MNT|MTG|MOUNT|MOUNTING|SH|SHLD|SHIELD|SHELL|EP|EPAD|PAD|"
                        r"TAB|CASE)\s*\d*$", re.I)


def mounting_pin(pin: BoardPin) -> bool:
    """A mounting, shield or exposed-pad pin (MP1, MNT 2, SH1, EP), by number or name."""
    return bool(_MOUNT_PIN.match(pin.number.strip()) or _MOUNT_PIN.match(pin.name.strip()))


def _class_severity(net: str | None) -> str:
    return "warning" if net and name_class(net) in ("ground", "power") else "info"


def _swapped(system: System, pairs: list[Pair]) -> set[int]:
    """Ids of different-name pairs swapped within their connection: board A's X mates
    board B's Y while A's Y mates B's X."""
    mated: dict[str, set] = defaultdict(set)
    named = [pair for pair in pairs if pair.status == "different_names"]
    for pair in named:
        a, b = system.pins[pair.a], system.pins[pair.b]
        mated[pair.connection].add(((a.board, a.net), (b.board, b.net)))
    found = set()
    for pair in named:
        a, b = system.pins[pair.a], system.pins[pair.b]
        if a.board == b.board:
            continue  # a loopback on one board: its partner would be itself
        partner = ((a.board, b.net), (b.board, a.net))
        if partner in mated[pair.connection] or partner[::-1] in mated[pair.connection]:
            found.add(pair.id)
    return found


# Info readings a swap does not override: a crossed link, two names of one ground or rail.
_SWAP_EXEMPT = ("tx_rx_crossed", "ground_renamed", "rail_renamed", "polarity_differs")


def pair_reading(system: System, pair: Pair, swapped: set[int]) -> tuple[str, str]:
    """(reason, severity) of a different-names pair: compare_names, or swapped when its
    partner carries the two names the other way round."""
    reason, severity = compare_names(system.pins[pair.a].net, system.pins[pair.b].net)
    if pair.id in swapped and reason not in _SWAP_EXEMPT:
        return "swapped", "warning"
    return reason, severity


def _pair_findings(system: System, pairs: list[Pair]) -> tuple[list[dict], dict]:
    findings, grouped = [], defaultdict(list)
    swapped = _swapped(system, pairs)
    for pair in pairs:
        a = system.pins[pair.a]
        b = system.pins[pair.b] if pair.b else None
        where = {"connection": pair.connection, "kind": pair.kind}
        if pair.status == "different_names":
            reason, severity = pair_reading(system, pair, swapped)
            if severity == "info":
                grouped[("different_net_names", reason, pair.connection)].append(pair)
                continue
            text = (f"{a.board} {a.net} mates {b.board} {b.net} and {a.board} {b.net} mates "
                    f"{b.board} {a.net}: " if reason == "swapped" else "") + _REASON_TEXT[reason]
            findings.append(_finding(
                system, "different_net_names", severity,
                f"{a.board} {a.label} ({a.net}) mates {b.board} {b.label} ({b.net}): " + text,
                reason=reason, **where, a=_end(system, pair.a), b=_end(system, pair.b),
                via=" > ".join(pair.via) or None))
        elif pair.status == "no_net":
            netted, bare = (a, b) if a.net else (b, a)
            severity = _class_severity(netted.net)
            if severity == "info":
                grouped[("no_net", "signal_on_one_side", pair.connection)].append(pair)
                continue
            findings.append(_finding(
                system, "no_net", severity,
                f"{bare.board} {bare.label} has no net on its board while {netted.board} "
                f"{netted.label} carries {netted.net} (a {name_class(netted.net)} net): the "
                "pin is left open.", reason="supply_on_one_side", **where,
                a=_end(system, pair.a), b=_end(system, pair.b),
                via=" > ".join(pair.via) or None, pins_without_net=1))
        elif pair.status == "unconnected":
            grouped[("no_net", "open_on_both_sides", pair.connection)].append(pair)
        elif pair.status in ("unwired", "dead_end"):
            if not a.net:
                continue  # a pin with no net: counted under no_net (_unpaired_no_net)
            reason = "no_wire" if pair.status == "unwired" else "plug_not_mated"
            severity = _class_severity(a.net)
            if severity == "info":
                grouped[("unwired_pin", reason, pair.connection)].append(pair)
                continue
            end = pair.via[-1] if pair.via else "?"
            findings.append(_finding(
                system, "unwired_pin", severity,
                f"{a.board} {a.label} ({a.net}) mates {' > '.join(pair.via)}"
                + (f": no wire of {pair.connection} continues from {end}." if reason == "no_wire"
                   else f": the wire of {pair.connection} ends in {end}, which no board pin "
                        "mates."), reason=reason, **where, a=_end(system, pair.a),
                via=" > ".join(pair.via) or None))
    return findings, grouped


_REASON_TEXT = {
    "ground_to_power": "a ground mated to a power rail.",
    "different_voltages": "two rails whose names give different voltages.",
    "ground_to_signal": "a ground mated to a named signal.",
    "power_to_signal": "a power rail mated to a named signal.",
    "polarity_differs": "the same signal with opposite polarity (P against N): a swapped pair?",
    "index_differs": "the same stem with another index: a shifted or swapped pin, another "
                     "bus or channel, or a deliberate remap (a second memory slot on another "
                     "rank or clock, data bits swapped within a DDR byte lane).",
    "active_low_differs": "an active-low mark (_N, #, n...) on one side only: opposite logic "
                          "levels, or one half of a differential pair against a single-ended "
                          "name?",
    "same_group_other_signal": "the same bus or group name ending in another signal (SDA "
                               "against SCL, A against B): a swapped or misrouted pin?",
    "swapped": "the two signals are swapped between the boards (deliberate only where the "
               "interface allows it, such as DDR data bits within a byte lane).",
    "unrelated_names": "names with nothing in common: check that the pin carries the "
                       "intended signal.",
}
_GROUP_TEXT = {
    ("different_net_names", "ground_renamed"): "ground nets named differently on each side",
    ("different_net_names", "ground_to_auto_name"): "a ground against a net name Altium "
                                                    "generated",
    ("different_net_names", "rail_renamed"): "rails named differently, no voltage conflict "
                                             "in the names",
    ("different_net_names", "power_to_auto_name"): "a rail against a net name Altium generated",
    ("different_net_names", "auto_named"): "a net name Altium generated (Net<part>_<pin>) on "
                                           "one side",
    ("different_net_names", "same_after_normalizing"): "the same name apart from case, "
                                                       "separators, leading zeros or the "
                                                       "active-low notation",
    ("different_net_names", "synonym"): "the same signal under another common name (CLK and "
                                        "SCK, CS and SS, RST and RESET)",
    ("different_net_names", "prefix_differs"): "the same signal with a board prefix or "
                                               "another leading part",
    ("different_net_names", "suffix_differs"): "the same name with a one-letter suffix on one "
                                               "side (a board's own suffix)",
    ("different_net_names", "numbered_per_board"): "nets numbered per board (Net1, Net2) under "
                                                   "each board's own prefix: nothing to "
                                                   "compare by name",
    ("different_net_names", "tx_rx_crossed"): "TX against RX (or SDO against SDI): crossed as "
                                              "the link needs, if each board names its own "
                                              "side",
    ("different_net_names", "generic_name"): "a connector-style or numbered name (PIN3, J1, "
                                             "GPIO2, Net4) on one side",
    ("different_net_names", "renamed"): "names with no letters or digits to compare",
    ("different_net_names", "abbreviated"): "the same index with a shortened stem (S0 and "
                                            "CS0)",
    ("no_net", "signal_on_one_side"): "a signal on one side mates a pin with no net on the "
                                      "other (an unused feature of that board?)",
    ("no_net", "open_on_both_sides"): "mated pins with no net on either board (unused "
                                      "positions)",
    ("unwired_pin", "no_wire"): "a signal mates a cable or harness pin that no wire continues",
    ("unwired_pin", "plug_not_mated"): "a signal's wire ends in a cable or harness plug pin "
                                       "that no board pin mates",
}


def _group_rows(system: System, grouped: dict) -> list[dict]:
    rows = []
    for (check, reason, connection), pairs in grouped.items():
        examples = []
        for pair in pairs:
            end = (" (unwired)" if pair.status == "unwired" else " (no board mates it)")
            text = _pin_text(system, pair.a) + " | " + (
                _pin_text(system, pair.b) if pair.b else " > ".join(pair.via) + end)
            examples.append(text)
        bare = ({k for pair in pairs for k in (pair.a, pair.b)
                 if k and not system.pins[k].net} if check == "no_net" else None)
        rows.append(_finding(system, check, "info",
                             f"{len(pairs)} pin pairs of {connection}: "
                             + _GROUP_TEXT.get((check, reason), reason) + ".",
                             reason=reason, connection=connection,
                             kind=system.connections[connection].kind
                             if connection in system.connections else None,
                             count=len(pairs),
                             pins_without_net=len(bare) if bare is not None else None,
                             examples=examples))
    return rows


def _reaches_board(system: System, key: str) -> bool:
    return any(pair.b is not None for pair in system.pairs_by_pin.get(key, ()))


def _connections_of(system: System, keys: list[str]) -> list[str]:
    return sorted({pair.connection for key in keys for pair in system.pairs_by_pin.get(key, ())}
                  | {link.connection for key in keys for _, link in system.adjacency.get(key, ())},
                  key=natural_key)


def _entry_findings(system: System, boards: list[str] | None) -> list[dict]:
    findings = []
    linked_pins = {key for key in system.adjacency}
    connection_ids = {c.id for c in system.connections.values() if c.id is not None}
    for board in sorted(system.boards.values(), key=lambda b: b.order):
        if boards and board.key not in boards:
            continue
        for entry in board.entries:
            if not entry.pins or not entry.parts:
                findings.append(_finding(
                    system, "entry_is_empty", "warning",
                    f"Entry {entry.designator} of {board.key} carries "
                    + ("no pins." if not entry.pins else "pins of no connector part."),
                    board=board.key, entry=entry.designator))
                continue
            if system.source != "mbs_doc" or any(_reaches_board(system, k) for k in entry.pins):
                continue
            if any(k in linked_pins for k in entry.pins):
                findings.append(_finding(
                    system, "no_mated_part", "warning",
                    f"Entry {entry.designator} of {board.key} ({', '.join(entry.parts)}) mates "
                    f"{', '.join(_connections_of(system, entry.pins))}, whose wires reach no "
                    "other module (a plug no module mates, or no wire on).",
                    reason="reaches_no_module", board=board.key, entry=entry.designator))
                continue
            reason = ("no_connection" if entry.connection_id not in connection_ids
                      else "no_pin_pairs")
            findings.append(_finding(
                system, "no_mated_part", "warning",
                f"Entry {entry.designator} of {board.key} ({', '.join(entry.parts)}) is "
                "mated to nothing: no connection joins its pins.", reason=reason,
                board=board.key, entry=entry.designator))
        if system.source == "mbs_doc":
            in_entries = {k for entry in board.entries for k in entry.pins}
            for connector in board.connectors.values():
                if connector.pins and not any(k in in_entries for k in connector.pins):
                    findings.append(_finding(
                        system, "entry_is_empty", "warning",
                        f"Connector {connector.designator} of {board.key} is in no entry of "
                        "its module.", board=board.key, connector=connector.designator,
                        reason="connector_without_entry"))
    return findings


def _labels(pins: list[BoardPin], with_net: bool = False) -> str:
    text = ", ".join(p.label + (f" {p.net}" if with_net else "") for p in pins[:GROUP_EXAMPLES])
    return text + ("" if len(pins) <= GROUP_EXAMPLES else ", ...")


def _connector_findings(system: System, boards: list[str] | None) -> list[dict]:
    findings = []
    for board in sorted(system.boards.values(), key=lambda b: b.order):
        if boards and board.key not in boards:
            continue
        if board.netlist is None:
            continue
        used = {c.ref for c in board.connectors.values()
                if c.ref and any(k in system.adjacency for k in c.pins)}
        if system.source == "mbs_doc":
            used |= {c.ref for c in board.connectors.values() if c.ref}
        for ref in system_connectors(board.netlist):
            if ref not in used:
                findings.append(_finding(
                    system, "connector_not_in_system", "warning",
                    f"{ref} on {board.key} has System=Connector but "
                    + ("is not among the module's connectors in the multi-board schematic "
                       "(import the changes from the child project, or remove the parameter)."
                       if system.source == "mbs_doc" else "no mating names it."),
                    board=board.key, connector=ref))
        for connector in board.connectors.values():
            if system.source == "mbs_doc" and connector.ref is None:
                findings.append(_finding(
                    system, "connector_not_in_project", "warning",
                    f"{connector.designator} of {board.key} is in the multi-board schematic but "
                    f"not in {board.project} (deleted or re-annotated): its pins keep the nets "
                    "the multi-board schematic stored at its last import. Import the changes "
                    "from the child project.", reason="part_not_in_project", board=board.key,
                    connector=connector.designator, count=len(connector.pins)))
                continue
            if connector.missing_pins:
                missing = [system.pins[k] for k in connector.missing_pins]
                findings.append(_finding(
                    system, "connector_not_in_project", "warning",
                    f"{len(missing)} pin(s) of {connector.designator} on {board.key} are not "
                    f"pins of {connector.ref} in {board.project} (renumbered, or another "
                    f"part): {_labels(missing)}. They keep the nets stored at the last import; "
                    "import the changes from the child project.", reason="pins_not_in_project",
                    board=board.key, connector=connector.designator, count=len(missing)))
            if system.source == "mbs_doc" and connector.system_connector is False:
                entries = sorted({system.pins[k].entry for k in connector.pins
                                  if system.pins[k].entry}, key=natural_key)
                connections = _connections_of(system, connector.pins)
                findings.append(_finding(
                    system, "entry_is_empty", "warning",
                    f"{connector.ref} in {board.project} no longer has the parameter "
                    f"System=Connector: the next Import From Child Projects removes "
                    f"{connector.designator} from {board.key}"
                    + (f", which empties entry {', '.join(entries)}" if entries else "")
                    + (f" and breaks {', '.join(connections)}" if connections else "")
                    + ". Put the parameter back if the part still joins the boards.",
                    reason="system_parameter_removed", board=board.key,
                    connector=connector.designator, entry=", ".join(entries) or None))
            mated = [k for k in connector.pins if k in system.adjacency]
            if not mated:
                continue
            open_pins = [system.pins[k] for k in connector.pins
                         if k not in system.adjacency and system.pins[k].net]
            if open_pins:
                mounting = all(mounting_pin(p) for p in open_pins)
                severity = "info" if mounting else max(
                    (_class_severity(p.net) for p in open_pins if not mounting_pin(p)),
                    key=lambda s: -_SEVERITY_RANK[s])
                findings.append(_finding(
                    system, "unmated_pins", severity,
                    f"{len(open_pins)} pin(s) of {connector.designator} on {board.key} carry a "
                    "net but no connection mates them"
                    + (" (mounting or shield pins)" if mounting else "") + ": "
                    + _labels(open_pins, with_net=True),
                    reason="mounting_pins_not_mated" if mounting else "pin_not_mated",
                    board=board.key, connector=connector.designator, count=len(open_pins)))
            if system.source == "mbs_doc" and board.netlist_source != "stored_in_mbsdoc":
                stale = [system.pins[k] for k in connector.pins
                         if system.pins[k].net_from == "child"
                         and (system.pins[k].stored_net or None) != (system.pins[k].net or None)]
                if stale:
                    findings.append(_finding(
                        system, "net_names_unsynchronized", "warning",
                        f"{len(stale)} pin(s) of {connector.designator} on {board.key} have "
                        "another net in the child project than the multi-board schematic "
                        "stored at its last import (" + ", ".join(
                            f"{p.label}: {p.stored_net or 'none'} -> {p.net or 'none'}"
                            for p in stale[:GROUP_EXAMPLES]) + ("" if len(stale) <= GROUP_EXAMPLES
                                                               else ", ...")
                        + "). Import the changes from the child projects; nets here are the "
                        "child project's.", board=board.key, connector=connector.designator,
                        count=len(stale)))
    return findings


def _unpaired_no_net(system: System, boards: list[str] | None) -> tuple[list[dict], set[str]]:
    """No Net for the pins no pair covers: an entry's pins (every pin of a mated connector
    with a mating map) with no net on their board that mate no other board's pin."""
    findings, keys = [], set()
    for board in sorted(system.boards.values(), key=lambda b: b.order):
        if boards and board.key not in boards:
            continue
        for connector in sorted(board.connectors.values(),
                                key=lambda c: natural_key(c.designator)):
            if system.source == "mbs_doc":
                candidates = [k for k in connector.pins if system.pins[k].entry]
            elif any(k in system.adjacency for k in connector.pins):
                candidates = list(connector.pins)
            else:
                candidates = []
            bare = [system.pins[k] for k in candidates
                    if not system.pins[k].net and not _reaches_board(system, k)]
            for mounting in (False, True):
                pins = [p for p in bare if mounting_pin(p) == mounting]
                if not pins:
                    continue
                keys |= {p.key for p in pins}
                findings.append(_finding(
                    system, "no_net", "info",
                    f"{len(pins)} pin(s) of {connector.designator} on {board.key} have no net "
                    "on their board and mate no other board's pin"
                    + (" (mounting or shield pins)" if mounting else "") + ": "
                    + _labels(pins) + ".",
                    reason="mounting_pins_without_net" if mounting else "unmated_pin_without_net",
                    board=board.key, connector=connector.designator, count=len(pins),
                    pins_without_net=len(pins)))
    return findings, keys


def _board_findings(system: System, boards: list[str] | None) -> list[dict]:
    findings = []
    for board in sorted(system.boards.values(), key=lambda b: b.order):
        if (boards and board.key not in boards) or board.netlist is not None:
            continue
        findings.append(_finding(
            system, "board_not_loaded", "warning",
            f"{board.key}'s project {board.project or '(none)'} could not be compiled "
            f"({board.load_error}): "
            + ("its pins carry the nets the multi-board schematic stored at its last import."
               if system.doc is not None and system.source == "mbs_doc" else
               "its pins have no nets."), board=board.key, reason=board.load_error))
    return findings


def _joined_findings(system: System) -> list[dict]:
    """Two or more nets of one board joined through other boards (a short through a mated
    board or a harness splice). Ground against a rail and rails of different voltages are
    power_rails errors and are left to it."""
    findings = []
    for group in system_nets(system):
        by_board: dict[str, list[str]] = defaultdict(list)
        for board, net in group["nets"]:
            by_board[board].append(net)
        connections = sorted({pair.connection for pair in group["pairs"]}, key=natural_key)
        for board in sorted(by_board, key=lambda b: system.boards[b].order):
            nets = sorted(by_board[board], key=natural_key)
            if len(nets) < 2:
                continue
            classes = {name_class(net) for net in nets}
            volts = sorted({v for v in (_volts(net) for net in nets
                                        if name_class(net) == "power") if v is not None})
            if {"ground", "power"} <= classes or (
                    classes == {"power"} and volts and volts[-1] - volts[0] > 0.01):
                continue
            if classes == {"ground"}:
                reason, severity, text = ("grounds_joined", "info",
                                          "grounds of one board tied together elsewhere (a "
                                          "single-point tie, or a ground split that is not "
                                          "kept?)")
            elif classes == {"power"}:
                reason, severity, text = ("rails_joined", "info",
                                          "rails of one board fed from one rail elsewhere")
            else:
                reason, severity, text = ("nets_shorted", "warning",
                                          "nets of one board shorted through another board or "
                                          "a harness splice")
            mine = {(board, net) for net in nets}
            neighbours = set()
            for pair in group["pairs"]:
                ends = [(system.pins[k].board, system.pins[k].net) for k in (pair.a, pair.b)]
                if ends[0] in mine and ends[1] not in mine:
                    neighbours.add(ends[1])
                elif ends[1] in mine and ends[0] not in mine:
                    neighbours.add(ends[0])
            through = [f"{b} {n}" for b, n in sorted(
                neighbours, key=lambda item: (system.boards[item[0]].order, item[1]))]
            findings.append(_finding(
                system, "board_nets_joined", severity,
                f"{board}'s {' and '.join(nets)} are one net across the system: they mate "
                f"{', '.join(through[:GROUP_EXAMPLES]) or 'each other'}"
                f"{', ...' if len(through) > GROUP_EXAMPLES else ''} "
                f"({', '.join(connections)}): {text}.", reason=reason, board=board,
                nets=nets, boards=sorted({b for b, _ in group["nets"]},
                                         key=lambda b: system.boards[b].order),
                connections=connections))
    return findings


def system_nets(system: System) -> list[dict]:
    """Board nets joined through mated pins: [{nets: [(board, net)], pairs: [Pair]}]."""
    parent: dict = {}

    def find(item):
        parent.setdefault(item, item)
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for pair in system.pairs:
        if pair.b is None:
            continue
        a, b = system.pins[pair.a], system.pins[pair.b]
        if a.net and b.net:
            union((a.board, a.net), (b.board, b.net))
    groups: dict = defaultdict(lambda: {"nets": set(), "pairs": []})
    for pair in system.pairs:
        if pair.b is None:
            continue
        a, b = system.pins[pair.a], system.pins[pair.b]
        if a.net and b.net:
            group = groups[find((a.board, a.net))]
            group["nets"] |= {(a.board, a.net), (b.board, b.net)}
            group["pairs"].append(pair)
    return [{"nets": sorted(g["nets"], key=lambda n: (system.boards[n[0]].order, n[1])),
             "pairs": g["pairs"]} for g in groups.values()]


def power_rails(system: System) -> tuple[list[dict], list[dict]]:
    """(rails, findings): every system net with a power or ground board net, and the
    voltage, ground-to-rail and ground continuity findings across hops."""
    rails, findings = [], []
    direct = set()
    for pair in system.pairs:
        if pair.status == "different_names":
            a, b = system.pins[pair.a], system.pins[pair.b]
            direct.add(frozenset({(a.board, a.net), (b.board, b.net)}))
    ground_links: set[frozenset] = set()
    for group in system_nets(system):
        nets = group["nets"]
        classes = {(board, net): name_class(net) for board, net in nets}
        if not any(c in ("power", "ground") for c in classes.values()):
            continue
        boards = sorted({board for board, _ in nets}, key=lambda b: system.boards[b].order)
        if len(boards) < 2:
            continue
        pin_counts: dict[str, int] = defaultdict(int)
        for pair in group["pairs"]:
            pin_counts[pair.connection] += 1
        kind = "ground" if all(c == "ground" for c in classes.values()) else (
            "power" if all(c == "power" for c in classes.values()) else "mixed")
        rail = {"kind": kind, "nets": [{"board": board, "net": net, "class": classes[(board, net)],
                                        **({"volts": _volts(net)} if _volts(net) is not None
                                           else {})} for board, net in nets],
                "pins_per_connection": dict(pin_counts)}
        rails.append(rail)
        ground_boards = sorted({board for (board, _), c in classes.items() if c == "ground"})
        for i, a in enumerate(ground_boards):
            for b in ground_boards[i + 1:]:
                ground_links.add(frozenset({a, b}))
        volts = {(board, net): _volts(net) for board, net in nets if classes[(board, net)] == "power"}
        known = {key: value for key, value in volts.items() if value is not None}
        ordered = sorted(known.items(), key=lambda kv: (system.boards[kv[0][0]].order, kv[0][1]))
        for i, (a, va) in enumerate(ordered):
            for b, vb in ordered[i + 1:]:
                if abs(va - vb) > 0.01 and frozenset({a, b}) not in direct:
                    findings.append(_finding(
                        system, "power_rails", "error",
                        f"{a[0]} {a[1]} ({va:g} V) and {b[0]} {b[1]} ({vb:g} V) are joined "
                        "through the connections (more than one hop): two rails of different "
                        "voltages.", reason="rail_voltage_mismatch",
                        nets=[f"{a[0]} {a[1]}", f"{b[0]} {b[1]}"],
                        boards=sorted({a[0], b[0]}, key=lambda k: system.boards[k].order)))
        grounds = [key for key, c in classes.items() if c == "ground"]
        powers = [key for key, c in classes.items() if c == "power"]
        for g in grounds:
            for p in powers:
                if frozenset({g, p}) not in direct:
                    findings.append(_finding(
                        system, "power_rails", "error",
                        f"{g[0]} {g[1]} (ground) and {p[0]} {p[1]} (a rail) are joined through "
                        "the connections (more than one hop).", reason="ground_to_rail",
                        nets=[f"{g[0]} {g[1]}", f"{p[0]} {p[1]}"],
                        boards=sorted({g[0], p[0]}, key=lambda k: system.boards[k].order)))
    # Ground continuity: boards joined by any connection should share a ground.
    joined: dict[str, set[str]] = defaultdict(set)
    for connection in system.connections.values():
        for i, a in enumerate(connection.boards):
            for b in connection.boards[i + 1:]:
                joined[a].add(b)
                joined[b].add(a)
    has_ground = {board.key for board in system.boards.values()
                  if any(name_class(net) == "ground" for net in board.nets())
                  or any(system.pins[k].net and name_class(system.pins[k].net) == "ground"
                         for c in board.connectors.values() for k in c.pins)}
    reached: dict[str, set[str]] = defaultdict(set)
    for link in ground_links:
        a, b = tuple(link)
        reached[a].add(b)
        reached[b].add(a)
    for board in sorted(joined, key=lambda b: system.boards[b].order):
        if board not in has_ground:
            continue
        partners = sorted((b for b in joined[board] if b in has_ground),
                          key=lambda b: system.boards[b].order)
        missing = [b for b in partners if b not in reached[board]]
        if partners and len(missing) == len(partners):
            findings.append(_finding(
                system, "power_rails", "warning",
                f"No ground net of {board} is mated to a ground of the boards it connects to "
                f"({', '.join(partners)}): its ground is not carried through the connections "
                "(fine for an isolated interface, otherwise a missing return path).",
                reason="ground_not_shared", board=board))
    return rails, findings


def _stored_conflicts(system: System, computed: list[dict], grouped: dict
                      ) -> tuple[list[dict], dict]:
    doc = system.doc
    if doc is None:
        return [], {"stored": 0}
    known: dict[int, str] = {}
    for board in system.boards.values():
        for connector in board.connectors.values():
            for key in connector.pins:
                if key.startswith("p"):
                    known[int(key[1:])] = f"{board.key} {system.pins[key].label}"
        for entry in board.entries:
            if entry.id is not None:
                known[entry.id] = f"{board.key} entry {entry.designator}"
    for connection in system.connections.values():
        if connection.id is not None:
            known[connection.id] = f"connection {connection.key}"
    findings, named = [], set()
    for conflict in doc.conflicts:
        objects = [known[i] for i in conflict.ids if i in known]
        named |= set(objects)
        findings.append(_finding(
            system, "stored_conflict", "warning",
            "Altium recorded this conflict in the multi-board schematic: "
            + (conflict.text or conflict.kind or "no text") + ".",
            kind=conflict.kind or None, objects=objects or None,
            raw_keys=sorted(conflict.raw)[:12] if isinstance(conflict.raw, dict) else None))
    # What the findings here name, in the stored conflicts' terms: pins (of single and of
    # grouped findings), entries and connections.
    computed_objects = set()
    for finding in computed:
        for side in ("a", "b"):
            end = finding.get(side)
            if isinstance(end, dict):
                computed_objects.add(f"{end['board']} {end['connector']}-{end['pin']}")
        if finding.get("board") and finding.get("entry"):
            computed_objects |= {f"{finding['board']} entry {entry.strip()}"
                                 for entry in str(finding["entry"]).split(",")}
        if finding.get("connection"):
            computed_objects.add(f"connection {finding['connection']}")
    for pairs in grouped.values():
        for pair in pairs:
            for key in (pair.a, pair.b):
                if key:
                    computed_objects.add(f"{system.pins[key].board} {system.pins[key].label}")
    agreement = {"stored": len(doc.conflicts),
                 "computed_warnings_and_errors": sum(1 for f in computed
                                                     if f["severity"] != "info"),
                 "stored_objects_also_flagged_here": len(named & computed_objects)}
    if not doc.conflicts:
        agreement["note"] = ("The multi-board schematic stores no conflict: Altium had none "
                             "unresolved at its last save. The findings here are computed from "
                             "the child projects as saved now.")
    return findings, agreement


def check(system: System, boards: list[str] | None = None,
          connection: str | None = None) -> dict:
    """Every finding of the system (or of the boards / the connection given), sorted
    errors first, with a per-connection summary."""
    pairs = [p for p in system.pairs
             if (not connection or p.connection == connection)
             and (not boards or any(system.pins[k].board in boards for k in (p.a, p.b) if k))]
    findings, grouped = _pair_findings(system, pairs)
    no_net_pins = {key for pair in pairs if pair.status in ("no_net", "unconnected")
                   for key in (pair.a, pair.b) if key and not system.pins[key].net}

    def on_boards(finding: dict) -> bool:
        return not boards or finding.get("board") in boards or any(
            board in boards for board in finding.get("boards") or [])

    if not connection:
        findings += _entry_findings(system, boards)
        findings += _connector_findings(system, boards)
        findings += _board_findings(system, boards)
        unpaired, keys = _unpaired_no_net(system, boards)
        findings += unpaired
        no_net_pins |= keys
        findings += [f for f in _joined_findings(system) if on_boards(f)]
    rails, rail_findings = power_rails(system)
    if not connection:
        findings += [f for f in rail_findings if on_boards(f)]
    stored, agreement = _stored_conflicts(system, findings, grouped)
    if not connection and not boards:
        findings += stored
    findings += _group_rows(system, grouped)
    findings.sort(key=lambda f: (_SEVERITY_RANK[f["severity"]], CHECKS.index(f["check"])
                                 if f["check"] in CHECKS else 99))
    for number, finding in enumerate(findings, 1):
        finding["id"] = f"MB{number}"
    summary_rows = []
    for row in connection_list(system):
        if connection and row["connection"] != connection:
            continue
        if boards and not set(row["boards"]) & set(boards):
            continue
        mine = [p for p in system.pairs if p.connection == row["connection"]]
        statuses = defaultdict(int)
        for pair in mine:
            statuses[pair.status] += 1
        severities = defaultdict(int)
        for finding in findings:
            if finding.get("connection") == row["connection"]:
                severities[finding["severity"]] += finding.get("count", 1)
        row["pins"] = dict(statuses)
        row["findings"] = {s: severities[s] for s in SEVERITIES if severities[s]}
        summary_rows.append(row)
    by_check: dict = defaultdict(lambda: defaultdict(int))
    for finding in findings:
        by_check[finding["check"]][finding["severity"]] += 1
    levels = {ERC_CHECKS[number - 1]: system.altium_level(check)
              for check, number in ALTIUM_TYPES.items()}
    ports = []
    if system.doc is not None:
        ports = [{key: value for key, value in (("designator", port.designator),
                                                 ("name", port.name)) if value}
                 for port in system.doc.power_ports[:50]]
    return {
        **({"stored_power_ports": ports} if ports else {}),
        "summary": {"findings": {s: sum(1 for f in findings if f["severity"] == s)
                                 for s in SEVERITIES},
                    "by_check": {check: dict(counts) for check, counts in by_check.items()},
                    "pin_pairs": len(pairs), "boards": len(system.boards),
                    "connections": len(system.connections),
                    # Altium's No Net counts every entry pin with no net in its child project.
                    "no_net_pins": len(no_net_pins)},
        "altium_report_levels": levels,
        "connections": summary_rows,
        "power_rails": rails,
        "stored_conflicts": agreement,
        "findings": findings,
    }


# ---------- tracing ----------

def _pin_splits(text: str) -> list[tuple[str, str]]:
    """(connector, pin) readings of a start such as J5-3, J5.3, J5:3, 'J5 pin 3' or 'J5 3'."""
    splits: list[tuple[str, str]] = []
    lowered = text.lower()
    for separator in (" pin ", "-", ".", ":", "/", " "):
        index = lowered.find(separator)
        while index > 0:
            part, pin = text[:index].strip(), text[index + len(separator):].strip()
            if part and pin and (part, pin) not in splits:
                splits.append((part, pin))
            index = lowered.find(separator, index + 1)
    return splits


def _find_pins(system: System, boards: list[Board], text: str) -> list[str]:
    """Board pins a connector-pin start names (by pin number, else by pin name)."""
    for part, pin in _pin_splits(text):
        for by in ("number", "name"):
            keys = [key for board in boards for connector in board.connectors.values()
                    if connector.designator.lower() == part.lower()
                    for key in connector.pins
                    if str(getattr(system.pins[key], by) or "").lower() == pin.lower()]
            if keys:
                return keys
    return []


def _start_pin(system: System, key: str) -> tuple[Board, str, str]:
    pin = system.pins[key]
    if not pin.net:
        mates = [_end(system, pair.b if pair.a == key else pair.a)
                 for pair in system.pairs_by_pin.get(key, ()) if pair.b]
        raise _error("pin_has_no_net", f"{pin.board} {pin.label} has no net on its board: "
                     + ("it mates " + ", ".join(f"{m['board']} {m['connector']}-{m['pin']} "
                                                f"({m['net'] or 'no net'})" for m in mates)
                        if mates else "it mates no other board's pin") + ".",
                     mates=mates or None)
    return system.boards[pin.board], pin.net, pin.label


def find_start(system: System, net: str, board: str | None, loaded_path: str | None
               ) -> tuple[Board, str, str | None]:
    """(board, net name, the connector pin named instead of a net or None) a trace starts
    from: a net name, or a module connector's pin (J5-3, J5.3, 'J5 pin 3'). Raises
    MultiboardError."""
    wanted = net.strip()
    if not wanted:
        raise _error("net_required", "Pass net= (a net name on one of the boards, or a "
                     "connector pin such as J5-3).")

    def match(candidate: Board) -> str | None:
        nets = candidate.nets()
        if wanted in nets:
            return wanted
        lowered = wanted.lower()
        exact = [name for name in nets if name.lower() == lowered]
        if exact:
            return exact[0]
        pins = {system.pins[k].net for c in candidate.connectors.values() for k in c.pins}
        return wanted if wanted in pins else None

    ordered = sorted(system.boards.values(), key=lambda b: b.order)
    if board is not None and str(board).strip():
        found = sorted(find_boards(system, str(board)), key=lambda b: b.order)
        if not found:
            raise _error("board_not_found", f"No board '{board}' in the system.",
                         boards=board_list(system))
        if len(found) > 1:
            raise _error("board_ambiguous", f"'{board}' names {len(found)} boards (one project "
                         "placed several times): pass a module designator.",
                         boards=[b.key for b in found])
        name = match(found[0])
        if name is not None:
            return found[0], name, None
        keys = _find_pins(system, found, wanted)
        if len(keys) == 1:
            return _start_pin(system, keys[0])
        similar = sorted((n for n in found[0].nets() if wanted.lower() in n.lower()),
                         key=natural_key)[:10]
        raise _error("net_not_found", f"No net or connector pin '{net}' on {found[0].key}.",
                     similar=similar or None)
    hits = [(b, match(b)) for b in ordered]
    hits = [(b, name) for b, name in hits if name is not None]
    own_board = [b for b in ordered if loaded_path and b.project_path
                 and _norm(b.project_path) == _norm(loaded_path)]
    if not hits:
        keys = _find_pins(system, ordered, wanted)
        mine = [k for k in keys if system.pins[k].board in {b.key for b in own_board}]
        if len(mine) == 1 or len(keys) == 1:
            return _start_pin(system, (mine or keys)[0])
        if keys:
            raise _error("pin_on_several_boards", f"'{net}' is a connector pin of several "
                         "boards: pass board=.",
                         boards=sorted({system.pins[k].board for k in keys},
                                       key=lambda k: system.boards[k].order))
        raise _error("net_not_found", f"No board of the system has a net or connector pin "
                     f"'{net}': pass board= and a net name of that board (query_net finds "
                     "names on the loaded board).")
    own = [(b, name) for b, name in hits if b in own_board]
    if len(own) == 1:
        return own[0][0], own[0][1], None
    if own:
        hits = own
    if len(hits) > 1:
        groups = system_nets(system)
        group_of = {}
        for index, group in enumerate(groups):
            for item in group["nets"]:
                group_of[item] = index
        if len({group_of.get((b.key, name), (b.key, name)) for b, name in hits}) > 1:
            raise _error("net_on_several_boards", f"'{net}' is a net of several boards that the "
                         "connections do not join: pass board=.",
                         boards=[b.key for b, _ in hits])
    return hits[0][0], hits[0][1], None


def trace(system: System, start: Board, net: str, pins_listed: int,
          start_pin: str | None = None) -> dict:
    """The system net reached from start's net (start_pin: the connector pin the user
    named instead of the net), hop by hop."""
    queue = [(start.key, net, 0)]
    visited = {(start.key, net): 0}
    used: set[int] = set()
    hops: list[dict] = []
    dead_ends: list[dict] = []
    cross_links = 0
    while queue:
        board_key, board_net, depth = queue.pop(0)
        if depth >= TRACE_DEPTH:
            continue
        board = system.boards[board_key]
        mine = [k for c in board.connectors.values() for k in c.pins
                if system.pins[k].net == board_net]
        groups: dict[tuple, list] = defaultdict(list)
        for key in sorted(mine, key=lambda k: pin_order(system, k)):
            for pair in system.pairs_by_pin.get(key, ()):
                if pair.id in used:
                    continue
                used.add(pair.id)
                other = pair.b if pair.a == key else pair.a
                target = system.pins[other] if other else None
                if target is not None and target.net and visited.get(
                        (target.board, target.net), depth + 1) <= depth:
                    cross_links += 1  # a splice between two nets already reached
                    continue
                via = list(pair.via) if pair.a == key else list(reversed(pair.via))
                group_key = (pair.connection, system.pins[key].part,
                             target.board if target else None, target.part if target else None,
                             target.net if target else None,
                             None if target else pair.status)
                groups[group_key].append((key, other, via))
        for (connection, part, to_board, to_part, to_net, end), items in groups.items():
            row = {"depth": depth + 1, "connection": connection,
                   "kind": system.connections[connection].kind,
                   "from": {"board": board_key, "connector": part, "net": board_net},
                   "to": ({"board": to_board, "connector": to_part, "net": to_net}
                          if to_board else None),
                   "pin_count": len(items),
                   "pins": [{"from": system.pins[a].label,
                             **({"to": system.pins[b].label} if b else {}),
                             **({"via": " > ".join(v)} if v else {})}
                            for a, b, v in items[:pins_listed]]}
            if to_board and to_net and to_net != board_net:
                reason, severity = compare_names(board_net, to_net)
                row["names"] = {"reason": reason, "severity": severity}
            if to_board is None:
                row["status"] = end  # unwired or dead_end
                dead_ends.append(row)
                continue
            if to_net is None:
                row["status"] = "no_net"
            elif (to_board, to_net) not in visited:
                visited[(to_board, to_net)] = depth + 1
                queue.append((to_board, to_net, depth + 1))
            hops.append(row)
    nets = []
    for (board_key, board_net), depth in sorted(visited.items(), key=lambda kv: kv[1]):
        board = system.boards[board_key]
        connector_parts = {c.ref or c.designator for c in board.connectors.values()}
        members = [f"{ref}-{pin}" for ref, pin in board.nets().get(board_net, ())
                   if ref not in connector_parts]
        members.sort(key=natural_key)
        row = {"board": board_key, "net": board_net, "depth": depth,
               "class": name_class(board_net)}
        volts = _volts(board_net)
        if volts is not None:
            row["volts"] = volts
        if board.netlist is None:
            row["other_pins"] = None
            row["other_pins_note"] = "the board's project did not load"
        else:
            row["other_pins_total"] = len(members)
            row["other_pins"] = members[:pins_listed]
        nets.append(row)
    boards_reached = sorted({n["board"] for n in nets}, key=lambda b: system.boards[b].order)
    origin = {"board": start.key, "project": start.project, "net": net}
    if start_pin:
        origin["from_pin"] = start_pin
    return {"start": origin,
            "system_net": {"boards": boards_reached, "board_nets": len(nets),
                           "names": sorted({n["net"] for n in nets}, key=natural_key),
                           "classes": sorted({n["class"] for n in nets})},
            "board_nets": nets, "hops": hops, "unwired": dead_ends,
            "pairs_between_reached_nets": cross_links}


# ---------- pages ----------

def page_check(model: dict, offset: int, limit: int, examples: int) -> dict:
    findings = model["findings"]
    page = []
    for finding in findings[offset:offset + limit]:
        row = dict(finding)
        if "examples" in row:
            row["examples"] = row["examples"][:examples]
        page.append(row)
    result = {key: value for key, value in model.items() if key != "findings"}
    result.update({"findings_total": len(findings), "offset": offset, "findings": page,
                   "has_more": offset + limit < len(findings)})
    if result["has_more"]:
        result["next_offset"] = offset + limit
    return result


def page_rows(rows: list[dict], offset: int, limit: int) -> dict:
    result = {"rows_total": len(rows), "offset": offset, "rows": rows[offset:offset + limit],
              "has_more": offset + limit < len(rows)}
    if result["has_more"]:
        result["next_offset"] = offset + limit
    return result


def page_trace(result: dict, offset: int, limit: int) -> dict:
    hops = result["hops"]
    out = {key: value for key, value in result.items() if key != "hops"}
    out.update({"hops_total": len(hops), "offset": offset, "hops": hops[offset:offset + limit],
                "has_more": offset + limit < len(hops)})
    if out["has_more"]:
        out["next_offset"] = offset + limit
    return out
