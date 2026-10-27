"""Multi-board systems (roadmap #54): the .PrjMbd reader (parsers/prj_mbd.py), the
multi-board schematic reader (parsers/mbs_doc.py), the system model and its checks
(services/multiboard.py) and the tools trace_net_across_boards, check_multiboard and
get_system_connections.

Every system here is synthetic: three made-up boards (Main, Card, Rear) with one sheet
each, compiled offline like any child project, and an .MbsDoc written as Altium's zip of
JSON files: Main's J1 mates Card's P1 directly, and a harness with three plugs joins
Main's J2, Card's J3 and Rear's J4 (two splices, one unwired plug pin)."""

import json
import os
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

import server.main as main
from altium import AltiumClient
from server.parsers import mbs_doc as md
from server.parsers import prj_mbd as pm
from tests.library_fixture import write_ole
from tests.sch_fixture import Sheet

mb = main.multiboard  # the module the server uses (its caches and its errors)

# ---------------------------------------------------------------------------
# Synthetic boards
# ---------------------------------------------------------------------------

MAIN = {
    "J1": ("MAINJ1AA", {"1": "GND", "2": "3V3", "3": "SIG_A", "4": "DATA0", "5": "CLK_P",
                        "6": None, "7": "5V", "8": "GND"}),
    "J2": ("MAINJ2AA", {"1": "GND", "2": "LINK", "3": "SPARE_SIG"}),
    "J9": ("MAINJ9AA", {"1": "AUX"}),  # System=Connector, not placed in the .MbsDoc
}
CARD = {
    "P1": ("CARDP1AA", {"1": "GND", "2": "VCC_3V3", "3": "CARD_SIG_A", "4": "DATA1",
                        "5": "CLK_N", "6": "CARD_EN", "7": "3V3", "8": "VBUS"}),
    "J3": ("CARDJ3AA", {"1": "GND", "2": "LINK_C", "3": "CARD_SIG_A"}),
    "J5": ("CARDJ5AA", {"1": "CARD_EN"}),  # placed, mated to nothing
}
REAR = {
    "J4": ("REARJ4AA", {"1": "GND", "2": "LINK_R", "3": "REAR_SIG"}),
}


def write_board(folder: Path, name: str, connectors: dict, system_parameter: bool = True
                ) -> Path:
    """<name>.PrjPcb with one sheet: each connector (designator -> (UniqueId, {pin: net or
    None})) with System=Connector, each netted pin wired to a net label and a resistor."""
    folder.mkdir(parents=True, exist_ok=True)
    sheet = Sheet()
    y, count = 3000, 0
    for designator, (uid, pins) in connectors.items():
        rows = []
        for number, net in pins.items():
            rows.append((number, y, net))
            y -= 20
        sheet.part(designator, [(number, 100, row_y) for number, row_y, _ in rows], uid=uid,
                   params={"System": "Connector"} if system_parameter else None)
        for _, row_y, net in rows:
            if net is None:
                continue
            count += 1
            sheet.wire((100, row_y), (200, row_y))
            sheet.label(net, 150, row_y)
            sheet.part(f"R{count}", [("1", 200, row_y), ("2", 300, row_y)])
        y -= 40
    write_ole(folder / f"{name}.SchDoc", {"FileHeader": sheet.streams()[0]})
    prj = folder / f"{name}.PrjPcb"
    prj.write_text(f"[Design]\nVersion=1.0\n[Document1]\nDocumentPath={name}.SchDoc\n",
                   encoding="utf-8")
    return prj


class Mbs:
    """A multi-board schematic's logical model, written as Altium's zip of JSON files."""

    def __init__(self):
        self.next_id = 1000
        self.modules: list[dict] = []
        self.connections: list[dict] = []
        self.conflicts: list = []
        self.power_ports: list = []
        self.pins: dict[tuple, int] = {}     # (module, connector, pin) -> id
        self.entries: dict[tuple, dict] = {}  # (module, connector) -> entry record
        self.version = 1

    def id(self) -> int:
        self.next_id += 1
        return self.next_id

    def _parameter(self, name: str, value: str) -> dict:
        return {"name": name, "value": value, "id": self.id()}

    def module(self, designator: str, project: str, connectors: dict, title: str = "",
               stored: dict | None = None, empty_entries=()) -> None:
        """connectors as write_board takes them; stored overrides a pin's stored net
        ((connector, pin) -> name or None)."""
        components, entries, pin_map = [], [], []
        for name, (uid, pins) in connectors.items():
            rows = []
            for number, net in pins.items():
                pin_id = self.id()
                self.pins[(designator, name, number)] = pin_id
                parameters = [self._parameter("SourceId", f"S{pin_id}"),
                              self._parameter("ParentSourceId", f"\\{uid}")]
                net = (stored or {}).get((name, number), net)
                if net:
                    parameters.append(self._parameter("ExternalNetName", net))
                rows.append({"pinId": number, "name": number, "number": number,
                             "parameters": parameters, "id": pin_id})
            components.append({"designator": name, "comment": "CONN", "pins": rows,
                               "parameters": [self._parameter("SourceId", f"\\{uid}"),
                                              self._parameter("PhysicalPath", "Sheet")],
                               "id": self.id()})
            entry = {"logicalConnection": 0, "calculatedDesignator": name, "parameters": [],
                     "id": self.id()}
            self.entries[(designator, name)] = entry
            entries.append(entry)
            pin_map += [{"key": self.pins[(designator, name, number)], "value": entry["id"]}
                        for number in pins]
        for name in empty_entries:
            entries.append({"logicalConnection": 0, "calculatedDesignator": name,
                            "parameters": [], "id": self.id()})
        self.modules.append({
            "designator": designator, "title": title,
            "source": {"sourceProject": project,
                       "boardFileName": project.replace(".PrjPcb", ".PcbDoc")},
            "functionalBlocks": [], "components": components, "entries": entries,
            "entryPinMap": {"map": pin_map}, "parameters": [], "id": self.id()})

    def _physical(self, pin1: int, pin2: int, label: str, internal: bool = False) -> dict:
        kind = "__logicalInternalConnection" if internal else "__logicalPinToPinConnection"
        return {"$type": kind, "pinPair": {"pin1": pin1, "pin2": pin2, "parameters": [],
                                           "id": self.id()},
                "designator": label, "parameters": [], "id": self.id()}

    def direct(self, designator: str, a: tuple, b: tuple, pins) -> None:
        """a and b are (module, connector); pins [(pin of a, pin of b)]."""
        connection_id = self.id()
        for side in (a, b):
            self.entries[side]["logicalConnection"] = connection_id
        physical = [self._physical(self.pins[(*a, pa)], self.pins[(*b, pb)], pa)
                    for pa, pb in pins]
        self.connections.append({
            "$type": "__logicalDirectConnection",
            "entriesConnection": {"entry1": self.entries[a]["id"],
                                  "entry2": self.entries[b]["id"],
                                  "physicalConnections": physical, "id": self.id()},
            "designator": designator, "parameters": [], "id": connection_id})

    def harness(self, designator: str, plugs: dict, mates: list, wires: list,
                kind: str = "__logicalHarnessConnection") -> None:
        """plugs {plug: [pins]}; mates [((module, connector), plug, [(module pin, plug
        pin)])]; wires [(plug, pin, plug, pin)]."""
        connection_id = self.id()
        plug_pins, components, entries, pin_map = {}, [], [], []
        for plug, numbers in plugs.items():
            rows = []
            for number in numbers:
                plug_pins[(plug, number)] = self.id()
                rows.append({"pinId": number, "name": number, "number": number,
                             "parameters": [self._parameter("SourceId", "")],
                             "id": plug_pins[(plug, number)]})
            components.append({"designator": "P?", "comment": "PLUG", "pins": rows,
                               "parameters": [self._parameter("DesignItemId", "PLUG-1")],
                               "id": self.id()})
            entry = {"definedDesignator": plug, "calculatedDesignator": "P?", "entryType": 1,
                     "parameters": [], "id": self.id()}
            entries.append(entry)
            pin_map += [{"key": plug_pins[(plug, number)], "value": entry["id"]}
                        for number in numbers]
        entry_of = {entry["definedDesignator"]: entry["id"] for entry in entries}
        mated = []
        for side, plug, pins in mates:
            self.entries[side]["logicalConnection"] = connection_id
            mated.append({"entry1": self.entries[side]["id"], "entry2": entry_of[plug],
                          "physicalConnections": [
                              self._physical(self.pins[(*side, mp)], plug_pins[(plug, pp)], pp)
                              for mp, pp in pins], "id": self.id()})
        internal: dict[tuple, list] = {}
        for plug_a, pin_a, plug_b, pin_b in wires:
            internal.setdefault((plug_a, plug_b), []).append(self._physical(
                plug_pins[(plug_a, pin_a)], plug_pins[(plug_b, pin_b)], pin_a, internal=True))
        self.connections.append({
            "$type": kind, "source": {}, "components": components, "entries": entries,
            "entryPinMap": {"map": pin_map}, "entriesConnections": mated,
            "connectionEntriesConnections": [
                {"entry1": entry_of[a], "entry2": entry_of[b], "physicalConnections": rows,
                 "id": self.id()} for (a, b), rows in internal.items()],
            "designator": designator, "parameters": [], "id": connection_id})

    def logical(self) -> dict:
        return {"uniqueId": "00000000-0000-0000-0000-000000000001", "modules": self.modules,
                "powerPorts": self.power_ports, "connections": self.connections,
                "conflicts": self.conflicts, "parameters": [], "futureKey": {"kept": True}}

    def write(self, path: Path) -> Path:
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("core.json", json.dumps({"version": self.version,
                                                      "modified": "2026-01-02T03:04:05Z"}))
            archive.writestr("logical.json", "﻿" + json.dumps(self.logical()))
            archive.writestr("Pages/page_1.json", json.dumps({"items": []}))
            archive.writestr("defaults.json", json.dumps({"module": {}}))
        return path


def prj_mbd_text(documents, erc: str = "") -> str:
    docs = "".join(f"[Document{i}]\nDocumentPath={path}\nDocumentUniqueId=\n"
                   for i, path in enumerate(documents, 1))
    return ("[Design]\nVersion=1.0\nHierarchyMode=0\n[Preferences]\nPrefsVaultGUID=\n" + docs
            + "[Electrical Rules Check]\nType114=2\nType115=2\nType116=2\nType117=3\n"
              "Type118=3\nType122=2\n" + erc)


def standard_mbs() -> Mbs:
    mbs = Mbs()
    mbs.module("M1", "Main\\Main.PrjPcb", {k: MAIN[k] for k in ("J1", "J2")}, title="Main",
               stored={("J1", "3"): "OLD_SIG"})
    mbs.module("M2", "Card\\Card.PrjPcb", CARD, title="Card")
    mbs.module("M3", "Rear\\Rear.PrjPcb", {**REAR, "J7": ("GONE0000", {"1": "X"})},
               title="Rear", empty_entries=("E9",))
    mbs.direct("C1", ("M1", "J1"), ("M2", "P1"), [(str(n), str(n)) for n in range(1, 9)])
    mbs.harness("H1", {"P1": ["1", "2", "3"], "P2": ["1", "2", "3"], "P3": ["1", "2", "3"]},
                [(("M1", "J2"), "P1", [("1", "1"), ("2", "2"), ("3", "3")]),
                 (("M2", "J3"), "P2", [("1", "1"), ("2", "2"), ("3", "3")]),
                 (("M3", "J4"), "P3", [("1", "1"), ("2", "2"), ("3", "3")])],
                [("P1", "1", "P2", "1"), ("P1", "1", "P3", "1"), ("P1", "2", "P2", "2"),
                 ("P1", "2", "P3", "2"), ("P2", "3", "P3", "3")])
    mbs.conflicts.append({"$type": "__logicalConflict", "message": "Pin names differ",
                          "objectId": mbs.pins[("M1", "J1", "4")], "id": mbs.id()})
    mbs.power_ports.append({"designator": "P_1", "name": "VBUS", "id": mbs.id()})
    return mbs


def build(root: Path, mbs: Mbs | None = None, documents=None) -> Path:
    """The standard system under root/System; returns the .PrjMbd."""
    system = root / "System"
    write_board(system / "Main", "Main", MAIN)
    write_board(system / "Card", "Card", CARD)
    write_board(system / "Rear", "Rear", REAR)
    (mbs or standard_mbs()).write(system / "System.MbsDoc")
    if documents is None:
        documents = ["Main\\Main.PrjPcb", "System.MbsDoc", "System.MbaDoc", "Card\\Card.PrjPcb",
                     "Rear\\Rear.PrjPcb"]
    (system / "System.MbaDoc").write_bytes(b"PK\x05\x06" + b"\0" * 18)
    prj = system / "System.PrjMbd"
    prj.write_text(prj_mbd_text(documents), encoding="utf-8")
    return prj


@pytest.fixture(autouse=True)
def fresh_cache():
    mb.clear_cache()
    yield
    mb.clear_cache()


@pytest.fixture
def system_path(tmp_path) -> Path:
    return build(tmp_path)


@pytest.fixture
def model(system_path):
    return mb.build_system(str(system_path), "system_argument")


@pytest.fixture
def state():
    """Isolated main state: nothing loaded, Altium not running."""
    with patch.object(main, "_project", None), \
         patch.object(main, "_variant_state", None), \
         patch.object(main, "_netlist_last_updated", None), \
         patch.object(main, "_altium", AltiumClient()):
        yield


def call(tool, **kwargs) -> dict:
    return json.loads(tool(**kwargs))


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

def test_prj_mbd_lists_child_projects_and_documents(system_path):
    project = pm.parse_prj_mbd(str(system_path))
    assert project.name == "System"
    assert [Path(d.path).name for d in project.board_projects] == ["Main.PrjPcb", "Card.PrjPcb",
                                                                    "Rear.PrjPcb"]
    assert [d.kind for d in project.documents] == ["board_project", "schematic", "assembly",
                                                   "board_project", "board_project"]
    assert all(d.exists for d in project.documents)
    assert project.erc_levels["Type117"] == "3"
    assert project.design["HierarchyMode"] == "0"


def test_prj_mbd_that_is_not_one_is_refused(tmp_path):
    bad = tmp_path / "Bad.PrjMbd"
    bad.write_text("just text", encoding="utf-8")
    with pytest.raises(pm.PrjMbdError):
        pm.parse_prj_mbd(str(bad))
    with pytest.raises(pm.PrjMbdError):
        pm.parse_prj_mbd(str(tmp_path / "missing.PrjMbd"))


def test_mbs_doc_reads_modules_entries_and_connections(system_path):
    doc = md.parse_mbs_doc(str(system_path.parent / "System.MbsDoc"))
    assert doc.version == 1 and doc.modified.startswith("2026")
    assert [m.designator for m in doc.modules] == ["M1", "M2", "M3"]
    main_module = doc.modules[0]
    assert main_module.source_project == "Main\\Main.PrjPcb"
    j1 = main_module.components[0]
    assert j1.designator == "J1" and j1.source_id == "\\MAINJ1AA" and j1.sheet == "Sheet"
    assert [p.number for p in j1.pins] == [str(n) for n in range(1, 9)]
    assert j1.pins[2].external_net == "OLD_SIG" and j1.pins[5].external_net == ""
    assert [len(e.pin_ids) for e in main_module.entries] == [8, 3]
    direct, harness = doc.connections
    assert (direct.kind, direct.designator, len(direct.links)) == ("direct", "C1", 1)
    assert len(direct.links[0].pairs) == 8 and not direct.links[0].internal
    assert harness.kind == "harness" and len(harness.components) == 3
    assert [e.designator for e in harness.entries] == ["P1", "P2", "P3"]
    assert sum(1 for link in harness.links if link.internal) == 3
    assert sum(len(link.pairs) for link in harness.links if link.internal) == 5
    assert doc.extra == {"futureKey": {"kept": True}}
    assert len(doc.conflicts) == 1 and doc.conflicts[0].text == "Pin names differ"
    assert doc.conflicts[0].ids == [doc.modules[0].components[0].pins[3].id]
    assert doc.warnings == []


def test_unknown_connection_types_and_versions_are_read_with_a_warning(tmp_path):
    mbs = Mbs()
    mbs.version = 2
    mbs.module("M1", "A.PrjPcb", {"J1": ("AAAAAAAA", {"1": "N1"})})
    mbs.module("M2", "B.PrjPcb", {"J1": ("BBBBBBBB", {"1": "N1"})})
    mbs.direct("X1", ("M1", "J1"), ("M2", "J1"), [("1", "1")])
    mbs.connections[0]["$type"] = "__logicalFutureConnection"
    doc = md.parse_mbs_doc(str(mbs.write(tmp_path / "S.MbsDoc")))
    assert doc.connections[0].kind == "unknown"
    assert len(doc.connections[0].links[0].pairs) == 1
    assert any("version 2" in w for w in doc.warnings)
    assert any("unknown type" in w for w in doc.warnings)
    assert md.connection_kind("__logicalCableConnection") == "cable"
    assert md.connection_kind("__logicalWireConnection") == "wire"


@pytest.mark.parametrize("content, kind", [
    (None, "ole_compound"),
    (b"|RECORD=1|NAME=Module\r\n", "ascii_records"),
    (b'{"modules": []}', "json_text"),
    (b"\x00\x01\x02 binary", "unknown"),
])
def test_older_mbs_doc_formats_are_reported_unsupported(tmp_path, content, kind):
    path = tmp_path / "Old.MbsDoc"
    if content is None:
        write_ole(path, {"FileHeader": b"|HEADER=Multi-board"})
    else:
        path.write_bytes(content)
    with pytest.raises(md.MbsDocUnsupported) as raised:
        md.parse_mbs_doc(str(path))
    assert raised.value.format == kind


def test_zip_without_logical_json_or_damaged_is_not_guessed(tmp_path):
    path = tmp_path / "NoLogical.MbsDoc"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("core.json", "{}")
    with pytest.raises(md.MbsDocUnsupported) as raised:
        md.parse_mbs_doc(str(path))
    assert raised.value.format == "zip_without_logical"
    damaged = tmp_path / "Damaged.MbsDoc"
    damaged.write_bytes(b"PK\x03\x04" + b"\xff" * 40)
    with pytest.raises(md.MbsDocError):
        md.parse_mbs_doc(str(damaged))
    bad_json = tmp_path / "BadJson.MbsDoc"
    with zipfile.ZipFile(bad_json, "w") as archive:
        archive.writestr("logical.json", "{not json")
    with pytest.raises(md.MbsDocError):
        md.parse_mbs_doc(str(bad_json))


# ---------------------------------------------------------------------------
# The system model
# ---------------------------------------------------------------------------

def test_boards_are_modules_with_their_child_projects_compiled(model):
    assert list(sorted(model.boards)) == ["M1", "M2", "M3"]
    main_board = model.boards["M1"]
    assert main_board.netlist_source == "offline_compiler" and main_board.project == "Main"
    j1 = main_board.connectors["J1"]
    assert (j1.ref, j1.matched_by, j1.system_connector) == ("J1", "unique_id", True)
    assert model.mbs_status == "read" and model.source == "mbs_doc"
    rear_ghost = model.boards["M3"].connectors["J7"]
    assert rear_ghost.ref is None and rear_ghost.matched_by == "none"


def test_direct_mating_pairs_pins_with_their_nets(model):
    rows = mb.pin_rows(model, connection="C1")
    assert len(rows) == 8
    first = rows[0]
    assert first["a"] == {"board": "M1", "connector": "J1", "pin": "1", "net": "GND"}
    assert first["b"] == {"board": "M2", "connector": "P1", "pin": "1", "net": "GND"}
    assert first["status"] == "same_net"
    statuses = {row["a"]["pin"]: row["status"] for row in rows}
    assert statuses["6"] == "no_net" and statuses["4"] == "different_names"
    assert rows[3]["names"] == {"reason": "index_differs", "severity": "warning"}


def test_harness_is_followed_through_plugs_wires_and_splices(model):
    rows = mb.pin_rows(model, connection="H1")
    pairs = {(r["a"]["board"] + "." + r["a"]["connector"] + "-" + r["a"]["pin"],
              r["b"]["board"] + "." + r["b"]["connector"] + "-" + r["b"]["pin"]
              if r["b"] else None): r for r in rows}
    assert set(pairs) == {
        ("M1.J2-1", "M2.J3-1"), ("M1.J2-1", "M3.J4-1"), ("M2.J3-1", "M3.J4-1"),
        ("M1.J2-2", "M2.J3-2"), ("M1.J2-2", "M3.J4-2"), ("M2.J3-2", "M3.J4-2"),
        ("M2.J3-3", "M3.J4-3"), ("M1.J2-3", None)}
    assert pairs[("M1.J2-1", "M3.J4-1")]["via"] == "P1-1 > P3-1"
    assert pairs[("M2.J3-1", "M3.J4-1")]["via"] == "P2-1 > P1-1 > P3-1"
    assert pairs[("M1.J2-3", None)]["status"] == "unwired"
    assert pairs[("M1.J2-3", None)]["via"] == "P1-3"
    listed = mb.connection_list(model)
    harness = next(row for row in listed if row["connection"] == "H1")
    assert harness["kind"] == "harness" and harness["boards"] == ["M1", "M2", "M3"]
    assert harness["plugs"] == ["P1", "P2", "P3"] and harness["pin_pairs"] == 8


def test_cable_and_wire_connections_map_like_harnesses_and_matings(tmp_path):
    root = tmp_path / "System"
    write_board(root / "A", "A", {"J1": ("AAAAAAAA", {"1": "TX", "2": "GND"}),
                                  "J2": ("AAAAAAAB", {"1": "EN"})})
    write_board(root / "B", "B", {"J1": ("BBBBBBBB", {"1": "RX", "2": "GND"}),
                                  "J2": ("BBBBBBBC", {"1": "EN"})})
    mbs = Mbs()
    mbs.module("M1", "A\\A.PrjPcb", {"J1": ("AAAAAAAA", {"1": "TX", "2": "GND"}),
                                     "J2": ("AAAAAAAB", {"1": "EN"})})
    mbs.module("M2", "B\\B.PrjPcb", {"J1": ("BBBBBBBB", {"1": "RX", "2": "GND"}),
                                     "J2": ("BBBBBBBC", {"1": "EN"})})
    mbs.harness("W1", {"P1": ["1", "2"], "P2": ["1", "2"]},
                [(("M1", "J1"), "P1", [("1", "1"), ("2", "2")]),
                 (("M2", "J1"), "P2", [("1", "1"), ("2", "2")])],
                [("P1", "1", "P2", "1"), ("P1", "2", "P2", "2")],
                kind="__logicalCableConnection")
    mbs.direct("W2", ("M1", "J2"), ("M2", "J2"), [("1", "1")])
    mbs.connections[-1]["$type"] = "__logicalWireConnection"
    mbs.write(root / "S.MbsDoc")
    (root / "S.PrjMbd").write_text(prj_mbd_text(["A\\A.PrjPcb", "B\\B.PrjPcb", "S.MbsDoc"]),
                                   encoding="utf-8")
    model = mb.build_system(str(root / "S.PrjMbd"), "x")
    rows = mb.pin_rows(model)
    assert [(r["connection"], r["kind"], r["a"]["net"], r["b"]["net"], r.get("via"))
            for r in rows] == [("W1", "cable", "TX", "RX", "P1-1 > P2-1"),
                               ("W1", "cable", "GND", "GND", "P1-2 > P2-2"),
                               ("W2", "wire", "EN", "EN", None)]
    assert rows[0]["names"] == {"reason": "tx_rx_crossed", "severity": "info"}
    result = mb.check(model)
    assert result["summary"]["findings"]["error"] == 0
    assert not [f for f in result["findings"] if f["severity"] == "warning"]


def test_filters_by_board_and_net(model):
    rear = mb.pin_rows(model, board=["M3"])
    assert rear and all("M3" in (r["a"]["board"], (r["b"] or {}).get("board")) for r in rear)
    link = mb.pin_rows(model, net="link_r")
    assert len(link) == 2


def test_trace_crosses_two_hops(model):
    start, net, _ = mb.find_start(model, "SIG_A", "Main", None)
    traced = mb.trace(model, start, net, 10)
    assert traced["system_net"]["boards"] == ["M1", "M2", "M3"]
    assert traced["system_net"]["names"] == ["CARD_SIG_A", "REAR_SIG", "SIG_A"]
    hops = traced["hops"]
    assert [(h["depth"], h["connection"], h["to"]["board"], h["to"]["net"]) for h in hops] == [
        (1, "C1", "M2", "CARD_SIG_A"), (2, "H1", "M3", "REAR_SIG")]
    assert hops[1]["pins"] == [{"from": "J3-3", "to": "J4-3", "via": "P2-3 > P3-3"}]
    assert hops[0]["names"]["reason"] == "prefix_differs"
    nets = {(n["board"], n["net"]): n for n in traced["board_nets"]}
    assert nets[("M1", "SIG_A")]["other_pins"] == ["R3-1"]
    assert nets[("M3", "REAR_SIG")]["depth"] == 2


def test_trace_of_a_spliced_ground_counts_splice_links_once(model):
    start, net, _ = mb.find_start(model, "GND", "M3", None)
    traced = mb.trace(model, start, net, 10)
    assert traced["system_net"]["boards"] == ["M1", "M2", "M3"]
    # The ground on Main also reaches Card's VBUS through J1-8.
    assert ("M2", "VBUS") in {(n["board"], n["net"]) for n in traced["board_nets"]}
    assert traced["pairs_between_reached_nets"] >= 1


def test_trace_reports_unwired_and_no_net_ends(model):
    start, net, _ = mb.find_start(model, "SPARE_SIG", None, None)
    traced = mb.trace(model, start, net, 10)
    assert traced["hops"] == [] and traced["unwired"][0]["pins"] == [{"from": "J2-3",
                                                                        "via": "P1-3"}]
    start, net, _ = mb.find_start(model, "CARD_EN", "Card", None)
    traced = mb.trace(model, start, net, 10)
    assert traced["hops"][0]["status"] == "no_net" and traced["hops"][0]["to"]["net"] is None


def test_trace_start_errors(model):
    with pytest.raises(mb.MultiboardError) as raised:
        mb.find_start(model, "NOPE", "M1", None)
    assert raised.value.payload["error"] == "net_not_found"
    with pytest.raises(mb.MultiboardError) as raised:
        mb.find_start(model, "SIG", "M1", None)
    assert raised.value.payload["similar"] == ["SIG_A", "SPARE_SIG"]
    with pytest.raises(mb.MultiboardError) as raised:
        mb.find_start(model, "GND", "nowhere", None)
    assert raised.value.payload["error"] == "board_not_found"
    # GND is on every board, and all are joined: no board needed.
    board, _, _ = mb.find_start(model, "GND", None, None)
    assert board.key == "M1"
    # The loaded project's board is preferred.
    board, _, _ = mb.find_start(model, "GND", None, model.boards["M3"].project_path)
    assert board.key == "M3"


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def _findings(result: dict, check: str, reason: str | None = None) -> list[dict]:
    return [f for f in result["findings"] if f["check"] == check
            and (reason is None or f.get("reason") == reason)]


def test_different_net_names_follow_the_naming_policy(model):
    result = mb.check(model)
    errors = _findings(result, "different_net_names")
    by_reason = {f["reason"]: f for f in errors}
    assert by_reason["ground_to_power"]["severity"] == "error"
    assert by_reason["ground_to_power"]["a"]["pin"] == "8"
    assert by_reason["different_voltages"]["severity"] == "error"
    assert by_reason["index_differs"]["severity"] == "warning"
    assert by_reason["polarity_differs"]["severity"] == "warning"
    # Harmless renames are grouped per connection as info.
    assert by_reason["prefix_differs"]["severity"] == "info"
    assert by_reason["rail_renamed"]["count"] == 1
    # LINK against LINK_C and LINK_R: a board's own suffix; LINK_C against LINK_R and
    # CARD_SIG_A against REAR_SIG end in other signals of one group.
    assert by_reason["suffix_differs"]["connection"] == "H1"
    assert by_reason["suffix_differs"]["count"] == 2
    group = _findings(result, "different_net_names", "same_group_other_signal")
    assert sorted((f["a"]["net"], f["b"]["net"]) for f in group) == [
        ("CARD_SIG_A", "REAR_SIG"), ("LINK_C", "LINK_R")]
    assert all(f["severity"] == "warning" for f in group)
    assert by_reason["index_differs"]["altium_check"] == "Different Net Names"
    assert by_reason["index_differs"]["altium_level"] == "Error"


def test_no_net_no_mated_part_and_empty_entries(model):
    result = mb.check(model)
    no_net = _findings(result, "no_net")
    assert len(no_net) == 1 and no_net[0]["severity"] == "info" and no_net[0]["count"] == 1
    assert no_net[0]["altium_level"] == "Fatal Error"
    unmated = {(f.get("board"), f.get("entry") or f.get("connector"), f["reason"])
               for f in _findings(result, "no_mated_part")}
    assert unmated == {("M2", "J5", "no_connection"),  # an entry mated to nothing
                       ("M3", "J7", "no_connection")}
    not_placed = _findings(result, "connector_not_in_system")  # System=Connector, not placed
    assert [(f["board"], f["connector"]) for f in not_placed] == [("M1", "J9")]
    assert "altium_check" not in not_placed[0]
    empty = _findings(result, "entry_is_empty")
    assert [(f["board"], f["entry"]) for f in empty] == [("M3", "E9")]
    gone = _findings(result, "connector_not_in_project")
    assert [(f["board"], f["connector"], f["reason"]) for f in gone] == [
        ("M3", "J7", "part_not_in_project")]
    assert "altium_check" not in gone[0]
    unwired = _findings(result, "unwired_pin")
    assert unwired[0]["count"] == 1 and "SPARE_SIG" in unwired[0]["examples"][0]


def test_stale_import_and_stored_conflicts(model):
    result = mb.check(model)
    stale = _findings(result, "net_names_unsynchronized")
    assert len(stale) == 1 and "J1-3: OLD_SIG -> SIG_A" in stale[0]["message"]
    stored = _findings(result, "stored_conflict")
    assert len(stored) == 1 and stored[0]["objects"] == ["M1 J1-4"]
    assert stored[0]["altium_check"] == "Unresolved Conflict"
    assert result["stored_conflicts"]["stored"] == 1
    assert result["stored_conflicts"]["stored_objects_also_flagged_here"] == 1
    assert result["stored_power_ports"] == [{"designator": "P_1", "name": "VBUS"}]
    assert mb.header(model)["system"]["multiboard_schematic"]["unknown_keys"] == ["futureKey"]


def test_power_rails_across_more_than_one_hop(model):
    result = mb.check(model)
    rails = _findings(result, "power_rails")
    reasons = sorted((f["reason"], tuple(f["nets"])) for f in rails)
    # Main's GND mates Card's VBUS (a direct error); through Main's GND, Card's and Rear's
    # grounds reach VBUS as well.
    assert ("ground_to_rail", ("M2 GND", "M2 VBUS")) in reasons
    assert ("ground_to_rail", ("M3 GND", "M2 VBUS")) in reasons
    assert all(f["severity"] == "error" for f in rails)
    ground = next(r for r in result["power_rails"]
                  if any(n["net"] == "GND" and n["board"] == "M3" for n in r["nets"]))
    assert ground["kind"] == "mixed" and ground["pins_per_connection"]["H1"] == 3


def test_findings_are_sorted_and_summarized_per_connection(model):
    result = mb.check(model)
    ranks = [mb.SEVERITIES.index(f["severity"]) for f in result["findings"]]
    assert ranks == sorted(ranks)
    assert [f["id"] for f in result["findings"]][:2] == ["MB1", "MB2"]
    rows = {row["connection"]: row for row in result["connections"]}
    assert rows["C1"]["pins"] == {"same_net": 1, "different_names": 6, "no_net": 1}
    assert rows["C1"]["findings"]["error"] == 2
    assert rows["H1"]["pins"]["unwired"] == 1
    assert result["summary"]["pin_pairs"] == 16
    assert result["altium_report_levels"]["No Net"] == "Fatal Error"
    only_h1 = mb.check(model, connection="H1")
    assert {f.get("connection") for f in only_h1["findings"]} == {"H1"}
    only_rear = mb.check(model, boards=["M3"])
    assert all(f.get("board") in (None, "M3") for f in only_rear["findings"])


def test_ground_not_shared_with_the_boards_it_connects_to(tmp_path):
    root = tmp_path / "System"
    write_board(root / "A", "A", {"J1": ("AAAAAAAA", {"1": "SIG", "2": "GND"})})
    write_board(root / "B", "B", {"J1": ("BBBBBBBB", {"1": "SIG", "2": "GND"})})
    mbs = Mbs()
    mbs.module("M1", "A\\A.PrjPcb", {"J1": ("AAAAAAAA", {"1": "SIG", "2": "GND"})})
    mbs.module("M2", "B\\B.PrjPcb", {"J1": ("BBBBBBBB", {"1": "SIG", "2": "GND"})})
    mbs.direct("C1", ("M1", "J1"), ("M2", "J1"), [("1", "1")])  # ground pin left unmated
    mbs.write(root / "S.MbsDoc")
    (root / "S.PrjMbd").write_text(prj_mbd_text(["A\\A.PrjPcb", "B\\B.PrjPcb", "S.MbsDoc"]),
                                   encoding="utf-8")
    result = mb.check(mb.build_system(str(root / "S.PrjMbd"), "system_argument"))
    shared = [f for f in result["findings"] if f.get("reason") == "ground_not_shared"]
    assert [f["board"] for f in shared] == ["M1", "M2"]
    unmated = [f for f in result["findings"] if f.get("reason") == "pin_not_mated"]
    assert unmated and unmated[0]["severity"] == "warning"


@pytest.mark.parametrize("a, b, reason, severity", [
    ("GND", "DGND", "ground_renamed", "info"),
    ("GND", "VCC", "ground_to_power", "error"),
    ("GND", "SPI_CLK", "ground_to_signal", "warning"),
    ("+5V", "3V3", "different_voltages", "error"),
    ("VDD_DDR", "VDD", "rail_renamed", "info"),
    ("3V3", "SPI_MOSI", "power_to_signal", "warning"),
    ("VREF", "NetXY9_4", "power_to_auto_name", "info"),
    ("NetR1_2", "SDA", "auto_named", "info"),
    ("DQ05", "dq5", "same_after_normalizing", "info"),
    ("BOARD_SDA", "SDA", "prefix_differs", "info"),
    ("HOST_USB_D_N", "PHY_USB_D_N", "prefix_differs", "info"),
    ("RESET_N", "nRESET", "same_after_normalizing", "info"),
    ("CLK_P", "CLK_N", "polarity_differs", "warning"),
    ("CANH", "CANL", "polarity_differs", "warning"),
    ("DATA4", "DATA5", "index_differs", "warning"),
    ("CK0_P", "BUS_CK2_P", "index_differs", "warning"),
    ("CS0", "S0", "abbreviated", "info"),
    ("UART_TX", "UART_RX", "tx_rx_crossed", "info"),
    ("J5_PIN3", "SENSOR_EN", "generic_name", "info"),
    ("LINK", "LINK_C", "suffix_differs", "info"),
    ("DATA_0", "DATA0_G", "suffix_differs", "info"),
    ("SPI_CLK", "UART_RX", "unrelated_names", "warning"),
    # A shared bus or group name with another signal at the end is not one signal.
    ("QBUS_DAT", "QBUS_CLK", "same_group_other_signal", "warning"),
    ("SPI_MOSI", "SPI_MISO", "same_group_other_signal", "warning"),
    ("MOTOR_A", "MOTOR_B", "same_group_other_signal", "warning"),
    ("LINK_C", "LINK_R", "same_group_other_signal", "warning"),
    ("CLK_P", "DATA_P", "unrelated_names", "warning"),
    # MOSI and MISO name the bus (the same on both boards); SDO and SDI cross.
    ("MOSI", "MISO", "unrelated_names", "warning"),
    ("SPI_SDO", "SPI_SDI", "tx_rx_crossed", "info"),
    # An index on the leading part: another bus or channel.
    ("QBUS1_CLK", "QBUS2_CLK", "index_differs", "warning"),
    ("QPORT1_TX", "QPORT2_TX", "index_differs", "warning"),
    ("J1_SDA", "J2_SDA", "prefix_differs", "info"),
    # USB's DP/DM and D+/D- are polarity marks.
    ("USB_DP", "USB_DM", "polarity_differs", "warning"),
    ("USB_D+", "USB_D-", "polarity_differs", "warning"),
    ("USB_D-", "USB_D_N", "same_after_normalizing", "info"),
    # An active-low mark on one side only is the opposite logic.
    ("RESET_N", "RESET", "active_low_differs", "warning"),
    ("ENABLE", "ENABLE_N", "active_low_differs", "warning"),
    ("RESET#", "nRESET", "same_after_normalizing", "info"),
    ("SPI_SCK", "SPI_CLK", "synonym", "info"),
    ("IRQ", "INT", "synonym", "info"),
    # Only Altium's Net<designator>_<pin> is a generated name; a user's NetN is a label.
    ("GND", "CAN_NET1", "ground_to_signal", "warning"),
    ("3V3", "MOTOR_NET2", "power_to_signal", "warning"),
    ("PCB_A(Net4)", "PCB_C(Net1)", "numbered_per_board", "info"),
    ("PCB_A(Net1)", "PCB_B(Net1)", "prefix_differs", "info"),
    ("PCB_A(Net4)", "PCB_A(Net1)", "index_differs", "warning"),
    ("Network_Reset", "SDA", "unrelated_names", "warning"),
])
def test_name_comparison(a, b, reason, severity):
    assert mb.compare_names(a, b) == (reason, severity)


def _two_boards(root: Path, a: dict, b: dict, pins, name: str = "S") -> Path:
    """Boards A and B (one connector each, as write_board takes them) mated directly by
    C1 on pins [(pin of A, pin of B)]; returns the .PrjMbd."""
    write_board(root / "A", "A", a)
    write_board(root / "B", "B", b)
    mbs = Mbs()
    mbs.module("M1", "A\\A.PrjPcb", a)
    mbs.module("M2", "B\\B.PrjPcb", b)
    mbs.direct("C1", ("M1", next(iter(a))), ("M2", next(iter(b))), pins)
    mbs.write(root / f"{name}.MbsDoc")
    prj = root / f"{name}.PrjMbd"
    prj.write_text(prj_mbd_text(["A\\A.PrjPcb", "B\\B.PrjPcb", f"{name}.MbsDoc"]),
                   encoding="utf-8")
    return prj


def test_signals_swapped_between_two_boards_are_a_warning(tmp_path):
    a = {"J1": ("AAAAJ1AA", {"1": "QBUS_DAT", "2": "QBUS_CLK", "3": "UART_TX", "4": "UART_RX"})}
    b = {"P1": ("BBBBP1AA", {"1": "QBUS_CLK", "2": "QBUS_DAT", "3": "UART_RX", "4": "UART_TX"})}
    model = mb.build_system(str(_two_boards(tmp_path, a, b, [(str(n), str(n))
                                                             for n in range(1, 5)])), "x")
    result = mb.check(model)
    swapped = _findings(result, "different_net_names", "swapped")
    assert sorted(f["a"]["pin"] for f in swapped) == ["1", "2"]
    assert all(f["severity"] == "warning" for f in swapped)
    # A crossed UART is what the link needs, swapped or not.
    assert _findings(result, "different_net_names", "tx_rx_crossed")[0]["count"] == 2
    rows = {r["a"]["pin"]: r["names"]["reason"] for r in mb.pin_rows(model)}
    assert rows == {"1": "swapped", "2": "swapped", "3": "tx_rx_crossed", "4": "tx_rx_crossed"}


def test_no_net_counts_every_entry_pin_without_a_net(tmp_path):
    a = {"J1": ("AAAAJ1AB", {"1": "SIG", "2": None, "3": None, "4": None, "MP1": None})}
    b = {"P1": ("BBBBP1AB", {"1": "SIG", "2": None, "3": "B_SIG", "4": None, "MP1": None})}
    model = mb.build_system(str(_two_boards(tmp_path, a, b, [("1", "1"), ("2", "2"),
                                                             ("3", "3")])), "x")
    result = mb.check(model)
    reasons = {(f["reason"], f.get("board"), f.get("connector")): f
               for f in _findings(result, "no_net")}
    assert reasons[("open_on_both_sides", None, None)]["pins_without_net"] == 2
    assert reasons[("signal_on_one_side", None, None)]["pins_without_net"] == 1
    assert reasons[("unmated_pin_without_net", "M1", "J1")]["count"] == 1  # J1-4
    assert reasons[("mounting_pins_without_net", "M2", "P1")]["count"] == 1
    assert all(f["altium_check"] == "No Net" for f in reasons.values())
    # Altium's No Net is per pin: 2 + 1 + J1-4, P1-4 + both MP1 pins.
    assert result["summary"]["no_net_pins"] == 7


def test_harness_reaching_no_other_module_is_no_mated_part(tmp_path):
    root = tmp_path / "System"
    a = {"J1": ("AAAAJ1AC", {"1": "SIG_X", "2": "SIG_Y"})}
    b = {"J2": ("BBBBJ2AC", {"1": "SIG_X", "2": "SIG_Y"})}
    write_board(root / "A", "A", a)
    write_board(root / "B", "B", b)
    mbs = Mbs()
    mbs.module("M1", "A\\A.PrjPcb", a)
    mbs.module("M2", "B\\B.PrjPcb", b)
    # M1's plug P1 is wired to P3, which no module mates; M2's P2 has no wire at all.
    mbs.harness("H1", {"P1": ["1", "2"], "P2": ["1", "2"], "P3": ["1", "2"]},
                [(("M1", "J1"), "P1", [("1", "1"), ("2", "2")]),
                 (("M2", "J2"), "P2", [("1", "1"), ("2", "2")])],
                [("P1", "1", "P3", "1"), ("P1", "2", "P3", "2")])
    mbs.write(root / "S.MbsDoc")
    (root / "S.PrjMbd").write_text(prj_mbd_text(["A\\A.PrjPcb", "B\\B.PrjPcb", "S.MbsDoc"]),
                                   encoding="utf-8")
    model = mb.build_system(str(root / "S.PrjMbd"), "x")
    assert sorted(p.status for p in model.pairs) == ["dead_end", "dead_end", "unwired",
                                                     "unwired"]
    result = mb.check(model)
    unmated = {(f["board"], f["entry"], f["reason"]) for f in _findings(result, "no_mated_part")}
    assert unmated == {("M1", "J1", "reaches_no_module"), ("M2", "J2", "reaches_no_module")}
    wires = {f["reason"]: f for f in _findings(result, "unwired_pin")}
    assert wires["plug_not_mated"]["count"] == 2 and "no board pin mates" in \
        wires["plug_not_mated"]["message"]
    assert "(no board mates it)" in wires["plug_not_mated"]["examples"][0]
    assert wires["no_wire"]["count"] == 2


def test_two_nets_of_one_board_joined_through_another_board(tmp_path):
    a = {"J1": ("AAAAJ1AD", {"1": "SIG_ONE", "2": "SIG_TWO", "3": "VDDA", "4": "VDDB",
                             "5": "GND"})}
    b = {"P1": ("BBBBP1AD", {"1": "LINK", "2": "LINK", "3": "VCC", "4": "VCC", "5": "GND"})}
    model = mb.build_system(str(_two_boards(tmp_path, a, b, [(str(n), str(n))
                                                             for n in range(1, 6)])), "x")
    joined = {f["reason"]: f for f in _findings(mb.check(model), "board_nets_joined")}
    assert joined["nets_shorted"]["severity"] == "warning"
    assert joined["nets_shorted"]["nets"] == ["SIG_ONE", "SIG_TWO"]
    assert "they mate M2 LINK (C1)" in joined["nets_shorted"]["message"]
    assert joined["rails_joined"]["severity"] == "info"
    assert joined["rails_joined"]["nets"] == ["VDDA", "VDDB"]


def test_a_connector_or_pins_gone_from_the_child_are_one_finding(tmp_path):
    a = {"J1": ("AAAAJ1AE", {"1": "GND", "2": "SIG"})}
    b = {"P1": ("BBBBP1AE", {"1": "GND", "2": "SIG"})}
    prj = _two_boards(tmp_path, a, b, [("1", "1"), ("2", "2")])
    # B's part renumbered (A1, A2) without a new import: the stored nets stand in.
    write_board(tmp_path / "B", "B", {"P1": ("BBBBP1AE", {"A1": "GND", "A2": "SIG"})})
    result = mb.check(mb.build_system(str(prj), "x"))
    assert [(f["check"], f["reason"], f["count"]) for f in result["findings"]
            if f["severity"] != "info"] == [("connector_not_in_project", "pins_not_in_project",
                                             2)]
    # The part deleted from the child project: one finding, its pins keep the stored nets.
    write_board(tmp_path / "B", "B", {"X9": ("BBBBX9AE", {"1": "GND"})}, system_parameter=False)
    model = mb.build_system(str(prj), "x")
    assert {model.pins[k].net for k in model.boards["M2"].connectors["P1"].pins} == {"GND", "SIG"}
    result = mb.check(model)
    assert [(f["check"], f.get("reason")) for f in result["findings"]
            if f["severity"] != "info"] == [("connector_not_in_project", "part_not_in_project")]


def test_a_connector_that_lost_system_connector_will_empty_its_entry(tmp_path):
    a = {"J1": ("AAAAJ1AF", {"1": "GND", "2": "SIG"})}
    b = {"P1": ("BBBBP1AF", {"1": "GND", "2": "SIG"})}
    prj = _two_boards(tmp_path, a, b, [("1", "1"), ("2", "2")])
    write_board(tmp_path / "B", "B", b, system_parameter=False)
    result = mb.check(mb.build_system(str(prj), "x"))
    lost = _findings(result, "entry_is_empty", "system_parameter_removed")
    assert [(f["board"], f["connector"], f["entry"]) for f in lost] == [("M2", "P1", "P1")]
    assert "breaks C1" in lost[0]["message"] and lost[0]["severity"] == "warning"


def test_only_altiums_five_multiboard_checks_carry_its_names(model):
    result = mb.check(model)
    assert list(result["altium_report_levels"]) == [
        "Different Net Names", "Entry Is Empty", "No Mated Part", "No Net",
        "Unresolved Conflict"]
    for check in ("net_names_unsynchronized", "connector_not_in_project", "unmated_pins",
                  "connector_not_in_system", "power_rails"):
        assert all("altium_check" not in f for f in _findings(result, check))


def test_checks_set_to_no_report_are_info(tmp_path):
    prj = build(tmp_path)
    prj.write_text(prj_mbd_text(["Main\\Main.PrjPcb", "System.MbsDoc", "Card\\Card.PrjPcb",
                                 "Rear\\Rear.PrjPcb"]).replace("Type114=2", "Type114=0")
                   .replace("Type116=2", "Type116=0"), encoding="utf-8")
    result = mb.check(mb.build_system(str(prj), "x"))
    names = _findings(result, "different_net_names")
    assert {f["severity"] for f in names if f["reason"] == "index_differs"} == {"info"}
    assert all(f["note"] == mb.NO_REPORT_NOTE for f in names if f["reason"] == "index_differs")
    # An error stays an error.
    assert {f["severity"] for f in names if f["reason"] == "ground_to_power"} == {"error"}
    assert {f["severity"] for f in _findings(result, "no_mated_part")} == {"info"}
    assert {f["severity"] for f in _findings(result, "entry_is_empty")} == {"warning"}


def test_board_filter_keeps_rail_findings_of_a_board_with_a_space(tmp_path):
    root = tmp_path / "System"
    write_board(root / "Alpha", "Alpha", {"J1": ("ALPHAJ1A", {"1": "1V8", "2": "GND"})})
    write_board(root / "Beta", "Beta", {"P1": ("BETAP1AA", {"1": "VCC", "2": "GND"}),
                                        "P2": ("BETAP2AA", {"1": "VCC", "2": "GND"})})
    write_board(root / "Gamma Rear", "Gamma Rear", {"J3": ("GAMMAJ3A", {"1": "5V", "2": "GND"})})
    prj = root / "S.PrjMbd"
    prj.write_text(prj_mbd_text(["Alpha\\Alpha.PrjPcb", "Beta\\Beta.PrjPcb",
                                 "Gamma Rear\\Gamma Rear.PrjPcb"]), encoding="utf-8")
    mating = [{"board_a": "Alpha", "connector_a": "J1", "board_b": "Beta", "connector_b": "P1"},
              {"board_a": "Beta", "connector_a": "P2", "board_b": "Gamma Rear",
               "connector_b": "J3"}]
    model = mb.build_system(str(prj), "x", mating=mating)
    everything = _findings(mb.check(model), "power_rails", "rail_voltage_mismatch")
    narrowed = _findings(mb.check(model, boards=["Gamma Rear"]), "power_rails",
                         "rail_voltage_mismatch")
    assert len(everything) == 1 and narrowed == everything
    assert narrowed[0]["boards"] == ["Alpha", "Gamma Rear"]


def test_mating_a_connector_with_itself_or_twice(tmp_path):
    prj = build(tmp_path)
    _unreadable_mbs(prj)
    with pytest.raises(mb.MultiboardError) as raised:
        mb.build_system(str(prj), "x", mating=[{"board_a": "Main", "connector_a": "J1",
                                                "board_b": "Main", "connector_b": "J1"}])
    assert raised.value.payload["error"] == "invalid_mating"
    assert "with itself" in raised.value.payload["message"]
    twice = [{"board_a": "Main", "connector_a": "J1", "board_b": "Card", "connector_b": "P1"},
             {"board_a": "Main", "connector_a": "J1", "board_b": "Rear", "connector_b": "J4"}]
    model = mb.build_system(str(prj), "x", mating=twice)
    assert any("Main J1 is in 2 direct matings" in w for w in model.warnings)
    again = [twice[0], {**twice[0], "name": "again"}]
    model = mb.build_system(str(prj), "x", mating=again)
    assert any("U1, again join the same two connectors" in w for w in model.warnings)


def test_a_splice_wider_than_the_limit_is_reported(system_path):
    with patch.object(mb, "SPLICE_ENDS", 1):
        model = mb.build_system(str(system_path), "x")
    assert model.truncated_pins > 0
    assert any("through one cable or harness splice" in w for w in model.warnings)
    assert not mb.build_system(str(system_path), "x").truncated_pins


def test_duplicate_module_designators_are_warned_about(tmp_path):
    root = tmp_path / "System"
    a = {"J1": ("AAAAJ1AG", {"1": "GND"})}
    write_board(root / "A", "A", a)
    mbs = Mbs()
    mbs.module("M1", "A\\A.PrjPcb", a)
    mbs.module("M1", "A\\A.PrjPcb", a)
    mbs.write(root / "S.MbsDoc")
    (root / "S.PrjMbd").write_text(prj_mbd_text(["A\\A.PrjPcb", "S.MbsDoc"]), encoding="utf-8")
    model = mb.build_system(str(root / "S.PrjMbd"), "x")
    assert sorted(model.boards) == ["M1", "M1_2"]
    assert any("Two modules are designated M1" in w for w in model.warnings)


def test_trace_starts_from_a_connector_pin(model):
    board, net, pin = mb.find_start(model, "J1-3", "Main", None)
    assert (board.key, net, pin) == ("M1", "SIG_A", "J1-3")
    assert mb.find_start(model, "J1 pin 3", None, None)[1] == "SIG_A"
    assert mb.find_start(model, "P1.4", None, None)[1] == "DATA1"
    traced = mb.trace(model, board, net, 5, pin)
    assert traced["start"]["from_pin"] == "J1-3"
    with pytest.raises(mb.MultiboardError) as raised:
        mb.find_start(model, "J1-6", "M1", None)  # no net on Main's J1-6
    assert raised.value.payload["error"] == "pin_has_no_net"
    assert raised.value.payload["mates"][0]["net"] == "CARD_EN"
    with pytest.raises(mb.MultiboardError) as raised:
        mb.find_start(model, "J1-99", "M1", None)
    assert raised.value.payload["error"] == "net_not_found"


def test_stored_conflicts_on_grouped_pairs_are_matched(tmp_path):
    mbs = standard_mbs()
    mbs.conflicts[0]["objectId"] = mbs.pins[("M1", "J1", "3")]  # SIG_A: a grouped info pair
    result = mb.check(mb.build_system(str(build(tmp_path, mbs)), "x"))
    assert _findings(result, "stored_conflict")[0]["objects"] == ["M1 J1-3"]
    assert result["stored_conflicts"]["stored_objects_also_flagged_here"] == 1


# ---------------------------------------------------------------------------
# Finding the system, child projects, fallbacks
# ---------------------------------------------------------------------------

def test_system_is_found_beside_or_above_the_loaded_project(system_path, tmp_path):
    main_prj = system_path.parent / "Main" / "Main.PrjPcb"
    path, found_by = mb.locate_system(None, str(main_prj))
    assert Path(path) == system_path and found_by == "parent_folder"
    assert mb.locate_system(str(system_path.parent), None)[1] == "system_argument"
    assert Path(mb.locate_system(str(system_path), None)[0]) == system_path


def test_system_in_a_sibling_folder_is_found(tmp_path):
    boards = tmp_path / "Boards"
    write_board(boards / "Main", "Main", MAIN)
    sibling = tmp_path / "Assembly"
    sibling.mkdir()
    (sibling / "Sys.PrjMbd").write_text(prj_mbd_text(["..\\Boards\\Main\\Main.PrjPcb"]),
                                        encoding="utf-8")
    path, found_by = mb.locate_system(None, str(boards / "Main" / "Main.PrjPcb"))
    assert Path(path).name == "Sys.PrjMbd" and found_by == "parent_folder"


def test_system_not_found_or_ambiguous(system_path, tmp_path):
    lonely = write_board(tmp_path / "Elsewhere" / "Deep", "Lonely", MAIN)
    with pytest.raises(mb.MultiboardError) as raised:
        mb.locate_system(None, str(lonely))
    assert raised.value.payload["error"] == "no_multiboard_system"
    with pytest.raises(mb.MultiboardError) as raised:
        mb.locate_system(str(tmp_path / "nothing"), None)
    assert raised.value.payload["error"] == "system_not_found"
    with pytest.raises(mb.MultiboardError) as raised:
        mb.locate_system(str(system_path.parent / "Main" / "Main.PrjPcb"), None)
    assert raised.value.payload["error"] == "not_a_prjmbd"
    copy = system_path.parent / "Copy.PrjMbd"
    copy.write_text(system_path.read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(mb.MultiboardError) as raised:
        mb.locate_system(None, str(system_path.parent / "Main" / "Main.PrjPcb"))
    assert raised.value.payload["error"] == "system_ambiguous"
    with pytest.raises(mb.MultiboardError) as raised:
        mb.locate_system(str(system_path.parent), None)
    assert raised.value.payload["error"] == "system_ambiguous"
    with pytest.raises(mb.MultiboardError) as raised:
        mb.locate_system(None, None)
    assert raised.value.payload["error"] == "no_project"


def test_the_loaded_netlist_serves_its_own_board(system_path):
    main_prj = str(system_path.parent / "Main" / "Main.PrjPcb")
    netlist = AltiumClient().compile_offline(main_prj)
    netlist["pin_to_net"]["J1"]["3"] = "LOADED_NAME"
    model = mb.build_system(str(system_path), "x", loaded=(main_prj, netlist))
    assert model.boards["M1"].netlist_source == "loaded"
    assert model.boards["M2"].netlist_source == "offline_compiler"
    assert model.pins[next(k for k in model.boards["M1"].connectors["J1"].pins
                           if model.pins[k].number == "3")].net == "LOADED_NAME"


def test_a_child_that_does_not_compile_keeps_the_stored_nets(tmp_path):
    prj = build(tmp_path)
    (prj.parent / "Rear" / "Rear.SchDoc").write_bytes(b"not a sheet")
    model = mb.build_system(str(prj), "x")
    rear = model.boards["M3"]
    assert rear.netlist is None and rear.netlist_source == "stored_in_mbsdoc"
    assert rear.load_error.startswith("compile_failed")
    nets = {model.pins[k].number: model.pins[k].net for k in rear.connectors["J4"].pins}
    assert nets == {"1": "GND", "2": "LINK_R", "3": "REAR_SIG"}
    result = mb.check(model)
    assert [f["board"] for f in _findings(result, "board_not_loaded")] == ["M3"]


def test_child_compiles_are_cached_until_a_sheet_changes(system_path):
    first = mb.cached_system(str(system_path), "x")
    assert mb.cached_system(str(system_path), "x") is first
    sheet = system_path.parent / "Card" / "Card.SchDoc"
    stat = sheet.stat()
    os.utime(sheet, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
    assert mb.cached_system(str(system_path), "x") is not first


def test_a_module_project_the_prj_mbd_omits_still_invalidates_the_cache(tmp_path):
    prj = build(tmp_path, documents=["Main\\Main.PrjPcb", "Card\\Card.PrjPcb",
                                     "System.MbsDoc"])
    first = mb.cached_system(str(prj), "x")
    assert "Rear" in {board.project for board in first.boards.values()}
    sheet = prj.parent / "Rear" / "Rear.SchDoc"
    stat = sheet.stat()
    os.utime(sheet, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
    assert mb.cached_system(str(prj), "x") is not first


def _unreadable_mbs(prj: Path) -> None:
    write_ole(prj.parent / "System.MbsDoc", {"FileHeader": b"|HEADER=old"})


def test_user_mating_stands_in_for_an_unreadable_mbs_doc(tmp_path):
    prj = build(tmp_path)
    _unreadable_mbs(prj)
    model = mb.build_system(str(prj), "x")
    assert model.mbs_status == "unsupported" and model.mbs_format == "ole_compound"
    assert model.source == "none" and sorted(model.boards) == ["Card", "Main", "Rear"]
    mating = [{"board_a": "Main", "connector_a": "J1", "board_b": "Card", "connector_b": "P1"},
              {"a": {"board": "Card", "connector": "J3"}, "b": {"board": "Rear",
                                                              "connector": "J4"},
               "pin_map": [["1", "1"], "3:3"], "kind": "cable", "name": "W7"}]
    model = mb.build_system(str(prj), "x", mating=mating)
    assert model.source == "user_mating"
    rows = mb.pin_rows(model)
    assert len(rows) == 10
    assert {r["connection"] for r in rows} == {"U1", "W7"}
    cable = [r for r in rows if r["connection"] == "W7"]
    assert [(r["a"]["pin"], r["b"]["pin"], r["kind"]) for r in cable] == [
        ("1", "1", "cable"), ("3", "3", "cable")]
    start, net, _ = mb.find_start(model, "SIG_A", "Main", None)
    traced = mb.trace(model, start, net, 5)
    assert traced["system_net"]["boards"] == ["Card", "Main", "Rear"]
    result = mb.check(model)
    unmated = {(f["board"], f["connector"]) for f in _findings(result, "connector_not_in_system")}
    assert ("Main", "J2") in unmated and ("Main", "J9") in unmated
    assert not _findings(result, "entry_is_empty")


@pytest.mark.parametrize("mating, text", [
    ([], "non-empty list"),
    (["x"], "not an object"),
    ([{"board_a": "Main", "connector_a": "J1", "board_b": "Card"}], "connector_b"),
    ([{"board_a": "Nope", "connector_a": "J1", "board_b": "Card", "connector_b": "P1"}],
     "names no board"),
    ([{"board_a": "Main", "connector_a": "J99", "board_b": "Card", "connector_b": "P1"}],
     "no part J99"),
    ([{"board_a": "Main", "connector_a": "J1", "board_b": "Card", "connector_b": "P1",
       "pin_map": {"1": "77"}}], "no pin P1-77"),
    ([{"board_a": "Main", "connector_a": "J1", "board_b": "Card", "connector_b": "P1",
       "kind": "laser"}], "kind"),
    ([{"board_a": "Main", "connector_a": "J1", "board_b": "Card", "connector_b": "P1",
       "pin_map": 5}], "pin_map"),
])
def test_bad_mating_maps_are_explained(tmp_path, mating, text):
    prj = build(tmp_path)
    with pytest.raises(mb.MultiboardError) as raised:
        mb.build_system(str(prj), "x", mating=mating)
    assert raised.value.payload["error"] == "invalid_mating"
    assert text in raised.value.payload["message"]


def test_missing_mbs_doc_and_unlisted_projects(tmp_path):
    prj = build(tmp_path, documents=["Main\\Main.PrjPcb", "Card\\Card.PrjPcb",
                                     "System.MbsDoc", "Missing.MbsDoc"])
    model = mb.build_system(str(prj), "x")
    assert any("Rear.PrjPcb" in w or "Rear" in w for w in model.warnings)
    assert any("2 .MbsDoc" in w for w in model.warnings)
    (prj.parent / "System.MbsDoc").unlink()
    model = mb.build_system(str(prj), "x")
    assert model.mbs_status == "missing" and model.source == "none"
    none_listed = build(tmp_path / "other", documents=["Main\\Main.PrjPcb"])
    assert mb.build_system(str(none_listed), "x").mbs_status == "not_listed"


# ---------------------------------------------------------------------------
# The tools
# ---------------------------------------------------------------------------

def test_tools_need_a_project_or_a_system(state):
    for tool in (main.check_multiboard, main.get_system_connections):
        assert call(tool)["error"] == "no_project"
    assert call(main.trace_net_across_boards, net="GND")["error"] == "no_project"


def test_check_multiboard_tool(state, system_path):
    result = call(main.check_multiboard, system=str(system_path))
    assert result["system"]["prj_mbd"] == "System.PrjMbd"
    assert result["system"]["multiboard_schematic"]["status"] == "read"
    assert result["system"]["assembly"]["read"] is False
    assert [b["board"] for b in result["boards"]] == ["M1", "M2", "M3"]
    assert result["findings_total"] == len(result["findings"])
    assert result["summary"]["findings"]["error"] >= 4
    assert result["has_more"] is False
    narrowed = call(main.check_multiboard, system=str(system_path), connection="c1")
    assert {f["connection"] for f in narrowed["findings"]} == {"C1"}
    assert call(main.check_multiboard, system=str(system_path),
                connection="Z9")["error"] == "connection_not_found"
    assert call(main.check_multiboard, system=str(system_path),
                board="Nope")["error"] == "board_not_found"


def test_tools_find_the_system_of_the_loaded_project(state, system_path):
    main_prj = str(system_path.parent / "Main" / "Main.PrjPcb")
    netlist = AltiumClient().compile_offline(main_prj)
    project = {"name": "Main", "root_dir": str(Path(main_prj).parent), "prj_pcb_path": main_prj}
    with patch.object(main, "_project", project):
        main._altium._netlist = netlist
        result = call(main.trace_net_across_boards, net="SIG_A")
        assert result["system"]["found_by"] == "parent_folder"
        assert result["start"] == {"board": "M1", "project": "Main", "net": "SIG_A"}
        assert result["boards"][0]["netlist_source"] == "loaded"
        assert [h["to"]["board"] for h in result["hops"]] == ["M2", "M3"]
        by_pin = call(main.trace_net_across_boards, net="J1-3")
        assert by_pin["start"] == {"board": "M1", "project": "Main", "net": "SIG_A",
                                   "from_pin": "J1-3"}
        # The loaded project is untouched.
        assert main._project is project and main._altium._netlist is netlist


def test_get_system_connections_pages_and_filters(state, system_path):
    result = call(main.get_system_connections, system=str(system_path))
    assert result["rows_total"] == 16 and not result["has_more"]
    assert [c["connection"] for c in result["connections"]] == ["C1", "H1"]
    with patch.object(main, "MULTIBOARD_PIN_STEPS", (5, 2)):
        page = call(main.get_system_connections, system=str(system_path), offset=5)
    assert page["offset"] == 5 and len(page["rows"]) == 5 and page["next_offset"] == 10
    rear = call(main.get_system_connections, system=str(system_path), board="Rear")
    assert rear["rows_total"] == 5 and [c["connection"] for c in rear["connections"]] == ["H1"]


def test_results_shrink_to_fit(state, system_path):
    with patch.object(main, "QUERY_NET_MAX_RESULT_SIZE_CHARS", 5000):
        rows = call(main.get_system_connections, system=str(system_path))
        assert rows["rows_limited_to"] < 16 and rows["has_more"]
        checked = call(main.check_multiboard, system=str(system_path))
        assert checked["limited_to"]["findings"] < 100 and checked["has_more"]
        traced = call(main.trace_net_across_boards, net="GND", board="M1",
                      system=str(system_path))
        assert "limited_to" in traced or len(json.dumps(traced, indent=2)) <= 5000
    with patch.object(main, "QUERY_NET_MAX_RESULT_SIZE_CHARS", 500):
        assert "rows_omitted" in call(main.get_system_connections, system=str(system_path))
        assert "findings_omitted" in call(main.check_multiboard, system=str(system_path))


def test_tool_errors_for_unreadable_schematic_and_mating(state, tmp_path):
    prj = build(tmp_path)
    _unreadable_mbs(prj)
    result = call(main.check_multiboard, system=str(prj))
    assert result["error"] == "no_multiboard_schematic"
    assert "mating" in result["message"] and result["boards"][0]["board"] == "Card"
    assert "format): not read. Its connections are unknown." in result["message"]
    mating = [{"board_a": "Main", "connector_a": "J1", "board_b": "Card", "connector_b": "P1"}]
    checked = call(main.check_multiboard, system=str(prj), mating=mating)
    assert checked["system"]["connections_source"] == "user_mating"
    assert checked["note"].startswith("Connections come from the mating map")
    assert call(main.check_multiboard, system=str(prj), mating="J1=P1")["error"] == \
        "invalid_mating"
    assert call(main.trace_net_across_boards, net="GND", board="Card", system=str(prj),
                mating=mating)["system_net"]["boards"] == ["Card", "Main"]


def test_trace_tool_errors(state, system_path):
    assert call(main.trace_net_across_boards, net="NOPE", board="M1",
                system=str(system_path))["error"] == "net_not_found"
    assert call(main.trace_net_across_boards, net="",
                system=str(system_path))["error"] == "net_required"
    two = Mbs()
    for designator in ("M1", "M2"):
        two.module(designator, "Main\\Main.PrjPcb", {"J1": MAIN["J1"]}, title="Twin")
    two.write(system_path.parent / "System.MbsDoc")
    mb.clear_cache()
    result = call(main.trace_net_across_boards, net="SIG_A", board="Twin",
                  system=str(system_path))
    assert result["error"] == "board_ambiguous" and result["boards"] == ["M1", "M2"]
    # The same net on two copies of a board that no connection joins: board= is needed.
    result = call(main.trace_net_across_boards, net="SIG_A", system=str(system_path))
    assert result["error"] == "net_on_several_boards"


def test_a_failure_while_reading_never_breaks_the_server(state, system_path):
    with patch.object(mb, "build_system", side_effect=KeyError("boom")):
        result = call(main.check_multiboard, system=str(system_path))
    assert result == {"error": "multiboard_failed",
                      "message": "The multi-board system could not be read (KeyError)."}


TOOLS = ("trace_net_across_boards", "check_multiboard", "get_system_connections")


def test_the_tools_are_read_only_and_documented_everywhere():
    import asyncio
    tools = {tool.name: tool for tool in asyncio.run(main.mcp.list_tools())}
    root = Path(__file__).parents[1]
    manifest = {entry["name"] for entry in json.loads(
        (root / "manifest.json").read_text("utf-8"))["tools"]}
    readme = (root / "README.md").read_text("utf-8")
    changelog = (root / "CHANGELOG.md").read_text("utf-8")
    unreleased = changelog[changelog.index("## [Unreleased]"):]
    unreleased = unreleased[:unreleased.index("\n## [", 1)]
    for name in TOOLS:
        assert tools[name].annotations.readOnlyHint is True
        assert main.SERVER_INSTRUCTIONS.index(name) > 4096
        assert name in manifest and f"`{name}`" in readme and name in unreleased
        assert name not in main.NETLIST_TOOLS  # the board netlists are compiled here
    assert "check_multiboard" in main.REVIEW_EVIDENCE_TOOLS
    from server import cli
    assert "no_multiboard_system" in cli.NOT_RUN_ERRORS  # a single board: not applicable
