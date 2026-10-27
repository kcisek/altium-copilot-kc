"""check_libraries and get_library_part (roadmap #52, stage B) on synthetic libraries,
boards and projects built here and by tests/library_fixture.py. Every name and value
is made up."""
import json
from pathlib import Path

import pytest

import server.main as main_mod
from parsers.prj_pcb import VariantDefinition, VariantState
from server.parsers.pcb_doc import (
    PAD_SHAPE_RECT, PAD_SHAPE_ROUND, BareHole, ComponentBody, PcbComponent, PcbData, Primitive,
    decode_board,
)
from server.parsers.pcb_lib import parse_pcb_lib
from server.parsers.sch_lib import LibPin, SchLibComponent
from server.services import libraries as libs
from server.services.libraries import (
    LibraryCheckInputs, copper_report, footprint_rule_findings, pin_pad_findings,
    project_libraries, run_library_checks, sequence_gaps, symbol_rule_findings,
)
from tests.library_fixture import (
    binary_pin, body, fill, footprint_streams, int_lib_streams, pad, pcb_lib_streams, region,
    sch_component, sch_lib_streams, text_props, track, write_ole,
)
from tests.sch_fixture import record, stream

SQUARE = [(-50.0, -40.0), (50.0, -40.0), (50.0, 40.0), (-50.0, 40.0)]
COURTYARD = {"LAYER59MECHKIND": "CourtyardTop"}  # Mechanical 3
NETS = ["A", "B", "C", "GND"]


# ---------- builders ----------

def _chip(name: str = "CHIP-2", height: str = "25mil") -> dict:
    """Two rectangular pads, a courtyard on Mechanical 3 and a 3D body."""
    return footprint_streams(name, [
        pad("1", -40.0, 0.0, 30.0, 40.0), pad("2", 40.0, 0.0, 30.0, 40.0),
        track(59, -70.0, -30.0, 70.0, -30.0, width=2.0),
        track(59, -70.0, 30.0, 70.0, 30.0, width=2.0),
        body(SQUARE, layer=58, OVERALLHEIGHT="20mil", **{"MODEL.NAME": "made-up.step"}),
    ], height=height, description="made-up chip")


def _three() -> dict:
    """Three pads, one off both axes and turned, to show mirroring and turning."""
    return footprint_streams("CHIP-3", [
        pad("1", -40.0, 0.0, 30.0, 40.0), pad("2", 40.0, 0.0, 30.0, 40.0),
        pad("3", 10.0, 20.0, 10.0, 16.0, rotation=30.0),
    ], height="25mil", description="made-up three-pad part")


def _soic() -> dict:
    """Pads 1 to 5 and a large exposed pad EP."""
    pads = [pad(str(n), -60.0 + 30.0 * n, -50.0, 15.0, 40.0) for n in range(1, 6)]
    return footprint_streams("SOIC-5", [*pads, pad("EP", 30.0, 30.0, 100.0, 60.0)],
                             height="0mil", description="made-up package")


def _tie(name: str, joined: bool) -> dict:
    primitives = [pad("1", -20.0, 0.0, 20.0, 20.0), pad("2", 20.0, 0.0, 20.0, 20.0)]
    if joined:
        primitives.append(track(1, -20.0, 0.0, 20.0, 0.0, width=8.0))
    return footprint_streams(name, primitives, description="made-up tie")


def _short() -> dict:
    """Two pads that overlap."""
    return footprint_streams("SHORT-2", [pad("1", -5.0, 0.0, 20.0, 20.0),
                                         pad("2", 5.0, 0.0, 20.0, 20.0)],
                             description="made-up shorted pads")


def _parts_pcb_lib() -> dict:
    return pcb_lib_streams(_chip(), _three(), _soic(), _tie("TIE-2", True), _tie("OPEN-2", False),
                           _short(), names=["CHIP-2", "CHIP-3", "SOIC-5", "TIE-2", "OPEN-2",
                                            "SHORT-2"], layer_props=COURTYARD)


def _parts_sch_lib() -> dict:
    two = [binary_pin("1", "A"), binary_pin("2", "B")]
    amp = [binary_pin("1", "OUT", electrical=2), binary_pin("2", "IN", electrical=0),
           binary_pin("3", "V+", electrical=7), binary_pin("4", "V-", electrical=7),
           binary_pin("6", "SPARE")]
    return sch_lib_streams(
        ("RES", sch_component("RES", two, "made-up resistor", designator="R?",
                              footprints=["CHIP-2"])),
        ("AMP", sch_component("AMP", amp, "made-up amplifier", designator="U?",
                              footprints=["SOIC-5"])),
        ("TIE", sch_component("TIE", two, "made-up net tie", designator="NT?",
                              footprints=["TIE-2"], extra="|COMPONENTKIND=4")),
        ("TIEOPEN", sch_component("TIEOPEN", two, "made-up open tie", designator="NT?",
                                  footprints=["OPEN-2"], extra="|COMPONENTKIND=4")),
        ("CONN", sch_component("CONN", two, "made-up connector", designator="J?",
                               footprints=["SHORT-2"])),
    )


def _vendor_int_lib() -> dict:
    ldo = sch_component("LDO", [binary_pin("1", "IN"), binary_pin("2", "GND"),
                                binary_pin("3", "OUT")], "made-up regulator", designator="U?",
                        footprints=[("SOT-3", True, "", True)])
    sot = footprint_streams("SOT-3", [pad("1", -40.0, -40.0, 20.0, 30.0),
                                      pad("2", 0.0, -40.0, 20.0, 30.0),
                                      pad("3", 0.0, 40.0, 60.0, 30.0)], height="40mil",
                            description="made-up SOT")
    return int_lib_streams(sch_lib_streams(("LDO", ldo)),
                           pcb_lib_streams(sot, names=["SOT-3"]))


def _write_libraries(folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    write_ole(folder / "Parts.SchLib", _parts_sch_lib())
    write_ole(folder / "Parts.PcbLib", _parts_pcb_lib())
    write_ole(folder / "Vendor.IntLib", _vendor_int_lib())


def _footprint(tmp_path: Path, streams: dict, name: str):
    path = write_ole(tmp_path / f"{name}.PcbLib", pcb_lib_streams(streams, names=[name]))
    return parse_pcb_lib(str(path)).find(name)


def _part(index, designator, x, y, rotation=0.0, side="top", footprint=None, kind=0,
          height=None, **links) -> PcbComponent:
    return PcbComponent(index=index, refdes=designator, source_uid_tail=None,
                        channel_path=None, x=x, y=y, rotation=rotation, side=side,
                        footprint=footprint, designator=designator, kind=kind,
                        height_mil=height, **links)


def _place(component: PcbComponent, footprint, nets=None) -> list[Primitive]:
    """The footprint's pads placed on the board as Altium places them (board_frame is
    checked against hand-worked numbers in the first test)."""
    bottom = component.side == "bottom"
    placed = []
    for item in footprint.pads:
        x, y = libs.board_frame(item.x1, item.y1, component)
        rotation = (component.rotation - item.rotation) if bottom else (
            component.rotation + item.rotation)
        placed.append(Primitive(
            kind="pad", layer=libs.flip_layer(item.layer) if bottom else item.layer,
            net=(nets or {}).get(item.pad_name), component=component.index, x1=x, y1=y, x2=x,
            y2=y, width=item.width, height=item.height, rotation=rotation % 360.0,
            shape=item.shape, hole=item.hole, pad_name=item.pad_name, plated=item.plated))
    return placed


def _place_lines(component: PcbComponent, footprint) -> list[Primitive]:
    """The footprint's tracks placed with it (mechanical layers stay)."""
    bottom = component.side == "bottom"
    placed = []
    for line in footprint.tracks:
        (x1, y1), (x2, y2) = (libs.board_frame(line.x1, line.y1, component),
                              libs.board_frame(line.x2, line.y2, component))
        placed.append(Primitive(kind="track", layer=libs.flip_layer(line.layer) if bottom
                                else line.layer, net=None, component=component.index, x1=x1,
                                y1=y1, x2=x2, y2=y2, width=line.width))
    return placed


def _body(index: int, height: float = 20.0, model: str | None = "made-up.step") -> ComponentBody:
    return ComponentBody(component=index, layer=58, outline=[], overall_height_mil=height,
                         standoff_height_mil=0.0, projection=None, model_type=1,
                         model_name=model)


def _kinds(findings) -> set:
    return {finding["kind"] for finding in findings}


# ---------- placement convention ----------

def test_a_bottom_side_footprint_is_mirrored_about_its_x_axis_then_turned():
    bottom = _part(0, "R1", 2000.0, 1000.0, 90.0, side="bottom")
    # (10, 20): mirrored to (10, -20), turned 90 degrees to (20, 10).
    assert libs.board_frame(10.0, 20.0, bottom) == pytest.approx((2020.0, 1010.0))
    assert libs.board_frame(-40.0, 0.0, bottom) == pytest.approx((2000.0, 960.0))
    assert libs.footprint_frame(2020.0, 1010.0, bottom) == pytest.approx((10.0, 20.0))
    assert libs.footprint_rotation(60.0, bottom) == pytest.approx(30.0)  # 90 - 60
    top = _part(1, "R2", 0.0, 0.0, 90.0)
    assert libs.board_frame(10.0, 20.0, top) == pytest.approx((-20.0, 10.0))
    assert libs.footprint_frame(-20.0, 10.0, top) == pytest.approx((10.0, 20.0))
    assert libs.footprint_rotation(120.0, top) == pytest.approx(30.0)
    assert (libs.flip_layer(1), libs.flip_layer(33), libs.flip_layer(74)) == (32, 34, 74)
    assert libs.layer_label(2) == "Mid-Layer 1" and libs.layer_label(60) == "Mechanical 4"


@pytest.mark.parametrize("side,rotation", [("top", 0.0), ("top", 90.0), ("top", 33.3),
                                           ("bottom", 0.0), ("bottom", 90.0),
                                           ("bottom", 215.0)])
def test_a_footprint_placed_as_the_library_draws_it_matches(tmp_path, side, rotation):
    footprint = _footprint(tmp_path, _three(), "CHIP-3")
    part = _part(0, "R1", 1234.5, 987.6, rotation, side, height=25.0)
    assert libs.compare_footprint(part, footprint, _place(part, footprint)) == []


# ---------- footprint drift ----------

def test_drift_reports_moved_resized_renamed_pads_height_and_bodies(tmp_path):
    footprint = _footprint(tmp_path, _three(), "CHIP-3")
    part = _part(0, "R1", 1000.0, 1000.0, 90.0, height=30.0)
    placed = _place(part, footprint)
    placed[0].x1 += 5.0  # on the board's x: the footprint's -y when turned 90 degrees
    placed[1].width = 36.0
    placed[2].pad_name = "4"
    differences = {d["kind"]: d for d in libs.compare_footprint(
        part, footprint, placed, bodies=[_body(0)])}
    assert set(differences) == {"pads_moved", "pad_size", "pads_missing_on_board",
                                "pads_extra_on_board", "height", "bodies"}
    (moved,) = differences["pads_moved"]["pads"]
    assert (moved["pad"], moved["delta_mil"]) == ("1", [0.0, -5.0])
    assert moved["library_at_mil"] == [-40.0, 0.0]
    size = differences["pad_size"]["pads"][0]
    assert (size["pad"], size["library"]["size_mil"], size["board"]["size_mil"]) == (
        "2", [30.0, 40.0], [36.0, 40.0])
    assert differences["pads_missing_on_board"]["pads"] == ["3"]
    assert differences["pads_extra_on_board"]["pads"] == ["4"]
    assert (differences["height"]["in_library_mil"], differences["height"]["on_board_mil"]) == (
        25.0, 30.0)
    assert differences["bodies"]["in_library"]["bodies"] == 0
    assert differences["bodies"]["on_board"]["models"] == ["made-up.step"]
    assert {d["severity"] for k, d in differences.items() if k not in ("height", "bodies")} == {
        "warning"}


def test_drift_reports_an_origin_offset_and_a_turned_or_mirrored_footprint(tmp_path):
    footprint = _footprint(tmp_path, _three(), "CHIP-3")
    part = _part(0, "R1", 1000.0, 1000.0, 0.0)
    shifted = _place(part, footprint)
    for item in shifted:
        item.x1 += 10.0
        item.y1 -= 4.0
    (offset,) = libs.compare_footprint(part, footprint, shifted)
    assert (offset["kind"], offset["offset_mil"]) == ("origin_offset", [10.0, -4.0])
    turned_part = _part(1, "R2", 1000.0, 1000.0, 180.0)
    turned = _place(turned_part, footprint)
    for item in turned:
        item.component = 0
    (turn,) = libs.compare_footprint(part, footprint, turned)
    assert (turn["kind"], turn["turn_degrees"], turn["mirrored"]) == ("footprint_turned", 180, False)
    flipped = _place(_part(2, "R3", 1000.0, 1000.0, 0.0, side="bottom"), footprint)
    for item in flipped:
        item.layer = 1  # the copper stays on top: only the drawing is mirrored
    kinds = {d["kind"]: d for d in libs.compare_footprint(part, footprint, flipped)}
    assert kinds["footprint_turned"]["mirrored"] is True


def test_drift_reports_holes_plating_layers_and_primitive_counts(tmp_path):
    footprint = _footprint(tmp_path, footprint_streams("THT-2", [
        pad("1", 0.0, 0.0, 60.0, 60.0, shape=1, layer=74, hole=35.0, plated=True),
        pad("2", 100.0, 0.0, 30.0, 30.0),
        pad("MH", 200.0, 0.0, 0.0, 0.0, layer=74, hole=120.0, plated=False),
        track(33, -50.0, 50.0, 250.0, 50.0),
    ]), "THT-2")
    assert [hole.pad_name for hole in footprint.holes] == ["MH"]
    part = _part(0, "J1", 500.0, 500.0, 0.0, side="bottom")
    placed = _place(part, footprint)
    placed[0].hole, placed[0].plated = 40.0, False
    placed[1].layer = 1  # should have moved to the bottom layer
    counts = libs.primitive_counts([], [], [], [], bottom=True)
    differences = {d["kind"]: d for d in libs.compare_footprint(
        part, footprint, placed, holes=(), counts=counts)}
    assert {"pad_count", "pads_missing_on_board", "pad_hole", "pad_plating", "pad_layer",
            "primitives"} <= set(differences)
    assert differences["pad_count"]["in_library"] == {"pads": 2, "bare_holes": 1}
    assert differences["pad_count"]["on_board"] == {"pads": 2, "bare_holes": 0}
    assert differences["pads_missing_on_board"]["pads"] == ["MH"]
    layer = differences["pad_layer"]["pads"][0]
    assert (layer["library"]["layer"], layer["board"]["layer"]) == ("Top Layer", "Bottom Layer")
    assert differences["primitives"]["counts"] == [
        {"type": "track", "layer": "Top Overlay", "in_library": 1, "on_board": 0}]
    # The overlay track placed on the bottom side counts as the footprint's top overlay.
    board_track = Primitive(kind="track", layer=34, net=None, component=0, x1=0, y1=0,
                            x2=1, y2=1, width=5.0)
    assert libs.primitive_counts([board_track], [], [], [], bottom=True) == \
        libs.footprint_counts(footprint)


# ---------- copper, symbol and footprint rules ----------

def test_copper_report_finds_shorted_and_unconnected_copper(tmp_path):
    footprint = _footprint(tmp_path, footprint_streams("COPPER", [
        pad("1", 0.0, 0.0, 20.0, 20.0), pad("2", 100.0, 0.0, 20.0, 20.0),
        track(1, 0.0, 0.0, 100.0, 0.0, width=6.0),  # joins 1 and 2
        pad("3", 300.0, 0.0, 30.0, 30.0), pad("4", 320.0, 0.0, 40.0, 40.0, shape=1, layer=74,
                                                   hole=20.0),  # overlaps 3
        pad("5", 600.0, 0.0, 20.0, 20.0),
        region([(605.0, -5.0), (700.0, -5.0), (700.0, 5.0), (605.0, 5.0)]),  # on pad 5
        fill(1, 900.0, 0.0, 950.0, 20.0),  # touches no pad
        track(32, -10.0, 0.0, 10.0, 0.0),  # under pad 1, but on the other side
        track(33, 0.0, 0.0, 600.0, 0.0),  # overlay: not copper
    ]), "COPPER")
    report = copper_report(footprint)
    assert report.shorts == [["1", "2"], ["3", "4"]]
    assert sorted((list(piece["kinds"]), piece["layers"]) for piece in report.unconnected) == [
        (["fill"], ["Top Layer"]), (["track"], ["Bottom Layer"])]


def _pin(designator, name="P", electrical="Passive", part=1, mode=0):
    return LibPin(designator, name, electrical, part, mode, False, 0.0, 0.0, 10.0, 0, 0.0, 0.0)


def test_symbol_rules():
    symbol = SchLibComponent("BAD", "BAD", designator="", pins=[
        _pin("1", "X"), _pin("1", "Y"), _pin("2", "Z"), _pin("2", "Z"), _pin("", "Q"),
        _pin("3", ""), _pin("7", "W"), _pin("7", "W", part=2), _pin("9", "ALT", mode=1)])
    found = {f["kind"]: f for f in symbol_rule_findings(symbol)}
    assert found["missing_pin_designators"]["severity"] == "error"
    assert found["duplicate_pin_designators"]["pins"] == ["1"]
    assert found["duplicate_pin_designators_same_name"]["pins"] == ["2"]
    assert found["duplicate_pin_designators_across_parts"]["pins"] == ["7"]
    assert found["missing_pin_names"]["pins"] == ["3"]
    assert found["pin_sequence_gaps"]["missing"] == {"(numbers)": "4-6"}  # 9 is alternate
    assert {"missing_description", "missing_default_designator"} <= set(found)
    assert found["missing_footprint"]["severity"] == "warning"
    ic = SchLibComponent("IC", "IC", description="x", designator="U?",
                         pins=[_pin(str(n)) for n in range(1, 5)])
    assert _kinds(symbol_rule_findings(ic)) == {"missing_footprint", "untyped_pins"}
    drawing = SchLibComponent("LOGO", "LOGO", description="x", designator="X?")
    (only,) = symbol_rule_findings(drawing)
    assert (only["kind"], only["severity"]) == ("missing_footprint", "info")
    assert symbol_rule_findings(SchLibComponent("G", "G", description="x", designator="X?",
                                                kind=2)) == []


def test_sequence_gaps():
    assert sequence_gaps(["2", "3", "5", "A1", "A4", "B2"]) == {"": [1, 4], "A": [2, 3]}
    assert sequence_gaps(["1", "2"]) == {}
    assert libs._ranges([1, 2, 3, 5, 7, 8]) == "1-3, 5, 7-8"


def test_footprint_rules(tmp_path):
    footprint = _footprint(tmp_path, footprint_streams("MESSY", [
        pad("", 100.0, 100.0, 20.0, 20.0, layer=32), pad("1", 140.0, 100.0, 20.0, 20.0, layer=32),
        pad("1", 140.0, 100.0, 20.0, 20.0, layer=32),  # an exact copy
        pad("2", 155.0, 100.0, 20.0, 20.0, layer=32),  # overlaps pad 1
        pad("2", 240.0, 100.0, 20.0, 20.0, layer=32),
        track(32, 300.0, 300.0, 400.0, 300.0),
    ], description=""), "MESSY")
    found = {f["kind"]: f for f in footprint_rule_findings(footprint, kinds_declared=True)}
    assert found["missing_pad_designators"]["count"] == 1
    assert found["duplicate_pad_designators"]["pads"] == [{"pad": "1", "pads": 2},
                                                          {"pad": "2", "pads": 2}]
    assert found["duplicate_primitives"]["types"] == {"pad": 1}
    assert {"mirrored", "reference_outside_pads", "shorted_copper", "unconnected_copper",
            "missing_description", "no_courtyard"} <= set(found)
    assert found["shorted_copper"]["severity"] == "warning"
    assert found["reference_outside_pads"]["pads_box_mil"] == [90.0, 90.0, 250.0, 110.0]
    tie = _footprint(tmp_path, _tie("TIE-2", True), "TIE-2")
    assert "shorted_copper" in _kinds(footprint_rule_findings(tie))
    assert "shorted_copper" not in _kinds(footprint_rule_findings(tie, net_tie=True))


def test_pins_against_pads(tmp_path):
    soic = _footprint(tmp_path, _soic(), "SOIC-5")
    symbol = SchLibComponent("AMP", "AMP", pins=[_pin(n) for n in ("1", "2", "3", "4", "6")])
    found = {f["kind"]: f for f in pin_pad_findings(symbol, soic)}
    assert found["pins_without_pads"]["pins"] == ["6"]
    assert found["pins_without_pads"]["severity"] == "warning"
    assert found["pads_without_pins"]["pads"] == ["5", "EP"]
    assert found["pads_without_pins"]["large_pads"] == ["EP"]
    assert found["pads_without_pins"]["severity"] == "warning"
    assert {f["severity"] for f in pin_pad_findings(symbol, soic, unpadded="info")
            if f["kind"] == "pins_without_pads"} == {"info"}


# ---------- a project ----------

def _netlist_part(lib_reference, library, footprint, nets, kind=0, footprint_library="",
                  workspace=False, pins=None):
    return {"lib_reference": lib_reference, "design_item_id": lib_reference,
            "library": library, "footprint": footprint, "footprint_library": footprint_library,
            "workspace_part": workspace, "database_table": "", "kind": kind,
            "pins": {pin: {"name": "", "net": net} for pin, net in nets.items()},
            "pin_designators": pins or list(nets)}


def _project(tmp_path: Path, moved=None):
    """A project: Board.PrjPcb listing Libs/Parts.SchLib, Libs/Parts.PcbLib and
    Libs/Vendor.IntLib, its netlist and its board (PcbData). moved: designator -> (dx, dy)
    by which pad 2 of that part is moved on the board (default R3's by 5 mil along x)."""
    moved = {"R3": (5.0, 0.0)} if moved is None else moved
    root = tmp_path / "Board"
    _write_libraries(root / "Libs")
    (root / "Board.PrjPcb").write_text(
        "[Design]\n[Document1]\nDocumentPath=Board.SchDoc\n[Document2]\nDocumentPath=Board.PcbDoc\n"
        "[Document3]\nDocumentPath=Libs\\Parts.SchLib\n[Document4]\nDocumentPath=Libs\\Parts.PcbLib\n"
        "[Document5]\nDocumentPath=Libs\\Vendor.IntLib\n", encoding="utf-8")
    libraries = project_libraries(str(root / "Board.PrjPcb"), installed=[])
    netlist = {"components": {
        "R1": _netlist_part("RES", "Parts.SchLib", "CHIP-2", {"1": "A", "2": "B"}),
        "R2": _netlist_part("RES", "Parts.SchLib", "CHIP-2", {"1": "A", "2": "B"}),
        "R3": _netlist_part("RES", "Parts.SchLib", "CHIP-2", {"1": "A", "2": "C"}),
        "R4": _netlist_part("RES", "Gone.SchLib", "CHIP-2", {"1": "B", "2": "C"},
                            footprint_library="Gone.PcbLib"),
        "U1": _netlist_part("AMP", "Parts.SchLib", "SOIC-5",
                            {"1": "A", "2": "B", "3": "C", "6": "GND"},
                            pins=["1", "2", "3", "4", "6"]),
        "U2": _netlist_part("LDO", "Vendor.IntLib", "SOT-3", {"1": "A", "2": "GND", "3": "B"}),
        "U3": _netlist_part("MCU", "Some Workspace", "QFN", {"1": "A"}, workspace=True),
        "NT1": _netlist_part("TIE", "Parts.SchLib", "TIE-2", {"1": "A", "2": "GND"}, kind=4),
        "NT2": _netlist_part("TIEOPEN", "Parts.SchLib", "OPEN-2", {"1": "B", "2": "GND"}, kind=4),
        "J1": _netlist_part("CONN", "Parts.SchLib", "SHORT-2", {"1": "A", "2": "B"}),
    }}
    footprints = {name: parse_pcb_lib(str(root / "Libs" / "Parts.PcbLib")).find(name)
                  for name in ("CHIP-2", "SOIC-5", "TIE-2", "OPEN-2", "SHORT-2")}
    from server.parsers.int_lib import parse_int_lib
    sot = parse_int_lib(str(root / "Libs" / "Vendor.IntLib")).find_footprint("SOT-3")[1]
    lib = {"footprint_library": "Parts.PcbLib", "component_library": "Parts.SchLib"}
    components = [
        _part(0, "R1", 1000.0, 1000.0, 90.0, footprint="CHIP-2", height=25.0, lib_reference="RES", **lib),
        _part(1, "R2", 2000.0, 1000.0, 90.0, "bottom", footprint="CHIP-2", height=25.0,
              lib_reference="RES", **lib),
        _part(2, "R3", 3000.0, 1000.0, 0.0, footprint="CHIP-2", lib_reference="RES", **lib),
        _part(3, "R4", 4000.0, 1000.0, 0.0, footprint="CHIP-2", lib_reference="RES",
              footprint_library="Gone.PcbLib", component_library="Gone.SchLib"),
        _part(4, "U1", 1000.0, 2000.0, 0.0, footprint="SOIC-5", lib_reference="AMP", **lib),
        _part(5, "U2", 2000.0, 2000.0, 270.0, footprint="SOT-3", height=40.0, lib_reference="LDO",
              footprint_library="Vendor.IntLib", component_library="Vendor.IntLib"),
        _part(6, "U3", 3000.0, 2000.0, footprint="QFN", lib_reference="MCU",
              footprint_library="Some Workspace", component_library="Some Workspace",
              workspace_part=True),
        _part(7, "NT1", 1000.0, 3000.0, footprint="TIE-2", kind=4, lib_reference="TIE", **lib),
        _part(8, "NT2", 2000.0, 3000.0, footprint="OPEN-2", kind=4, lib_reference="TIEOPEN", **lib),
        _part(9, "J1", 3000.0, 3000.0, footprint="SHORT-2", lib_reference="CONN", **lib),
    ]
    nets = {name: NETS.index(net) for name, net in (("1", "A"), ("2", "B"))}
    primitives = []
    for component in components:
        name = component.designator
        footprint = sot if name == "U2" else footprints.get(component.footprint)
        if footprint is None:
            continue
        net_map = {pin: NETS.index(meta["net"])
                   for pin, meta in netlist["components"][name]["pins"].items()}
        placed = _place(component, footprint, net_map if name != "J1" else nets)
        if name in moved:
            pad_2 = next(p for p in placed if p.pad_name == "2")
            pad_2.x1 += moved[name][0]
            pad_2.y1 += moved[name][1]
        primitives += placed + _place_lines(component, footprint)
    bodies = [_body(index) for index in range(4)]
    pcb = PcbData(board=decode_board([]), nets=list(NETS), components=components,
                  primitives=primitives, pours=[], layer_name_to_v6={}, bodies=bodies)
    return root, libraries, netlist, pcb


def _by_kind(model) -> dict:
    found: dict = {}
    for finding in model["findings"]:
        found.setdefault((finding["check"], finding["kind"]), []).append(finding)
    return found


def test_project_checks(tmp_path):
    _, libraries, netlist, pcb = _project(tmp_path)
    model = run_library_checks(LibraryCheckInputs(libraries, netlist, pcb))
    assert model["checks_run"] == list(libs.PROJECT_CHECKS)
    found = _by_kind(model)
    # Resolution: R4 was placed from libraries the project does not have.
    (missing,) = found[("resolution", "symbol_library_missing")]
    assert (missing["designators"], missing["library"], missing["same_name_in"]) == (
        ["R4"], "Gone.SchLib", {"Parts.SchLib": 1})
    (elsewhere,) = found[("resolution", "footprint_from_another_library")]
    assert (elsewhere["designators"], elsewhere["found_in"]) == (["R4"], "Parts.PcbLib")
    assert found[("resolution", "symbol_workspace")][0]["designators"] == ["U3"]
    assert model["not_readable_offline_parts"] == {"pcb workspace part": 1,
                                                   "schematic workspace part": 1}
    # Drift: only R3's pad 2 moved; R4 is compared with the footprint found elsewhere.
    drift = model["summary"]["footprint_drift"]
    assert drift["status"] == {"differs (pads)": 1, "match": 8,
                               "not compared: workspace part": 1}
    (moved,) = found[("footprint_drift", "pads_moved")]
    assert (moved["designators"], moved["pads"][0]["delta_mil"]) == (["R3"], [5.0, 0.0])
    assert (moved["footprint"], moved["library"], moved["severity"]) == (
        "CHIP-2", "Parts.PcbLib", "warning")
    # Pins on nets: U1's pin 6 has no pad, in the library or on the board.
    (library_pad,) = found[("pads_cover_pins", "used_pins_without_library_pad")]
    assert (library_pad["pins"], library_pad["footprint"], library_pad["severity"]) == (
        ["6"], "SOIC-5", "error")
    assert found[("pads_cover_pins", "used_pins_without_board_pad")][0]["designators"] == ["U1"]
    # Rules: the amplifier's numbering gap and exposed pad, the connector's short.
    assert found[("symbol_rules", "pads_without_pins")][0]["large_pads"] == ["EP"]
    # The library item's short is a warning; net_ties reports what it does on the board.
    (short,) = found[("footprint_rules", "shorted_copper")]
    assert (short["severity"], short["on_different_nets"]) == ("warning", ["J1"])
    assert ("footprint_rules", "shorted_copper") in found and not any(
        f["footprint"] == "TIE-2" for f in found[("footprint_rules", "shorted_copper")])
    # Net ties: NT1 ties A to GND; NT2's footprint ties nothing; J1 shorts A and B.
    assert model["summary"]["net_ties"] == {"net tie parts": 2, "net ties joining their pads": 1,
                                            "shorted parts that are not net ties": 1}
    assert found[("net_ties", "net_tie_without_copper")][0]["designators"] == ["NT2"]
    (short,) = found[("net_ties", "short_not_net_tie")]
    assert (short["nets"], short["severity"], short["footprint"]) == (
        {"J1": ["A", "B"]}, "error", "SHORT-2")
    # 3D: R1-R4 have bodies, U2 only its Height; net ties are left out.
    assert model["summary"]["height_3d"]["parts"] == {"3d_model": 4, "height_only": 1,
                                                      "no_height": 3}
    assert found[("height_3d", "height_only")][0]["designators"] == ["U2"]
    # Bulk findings are grouped per library: every footprint but CHIP-2 lacks a courtyard.
    (courtyard,) = found[("footprint_rules", "no_courtyard")]
    assert (courtyard["count"], courtyard["library"]) == (4, "Parts.PcbLib")
    severities = [f["severity"] for f in model["findings"]]
    assert severities == sorted(severities, key=libs.SEVERITY_ORDER.get)
    assert model["severity_counts"]["error"] == 4


def test_drift_groups_parts_and_labels_a_footprint_from_another_library(tmp_path):
    # On the board R1 and R2 (turned 90 degrees, R2 on the bottom side) moved along y: in
    # the footprint's frame both moved like R3, along x.
    moves = {"R1": (0.0, 5.0), "R2": (0.0, 5.0), "R3": (5.0, 0.0), "R4": (5.0, 0.0)}
    _, libraries, netlist, pcb = _project(tmp_path, moved=moves)
    model = run_library_checks(LibraryCheckInputs(libraries, netlist, pcb,
                                                  checks=["footprint_drift"]))
    groups = {tuple(f["designators"]): f for f in model["findings"]}
    assert set(groups) == {("R1", "R2", "R3"), ("R4",)}
    assert groups[("R1", "R2", "R3")]["pads"][0]["delta_mil"] == [5.0, 0.0]
    assert groups[("R1", "R2", "R3")]["parts"] == 3
    assert "compared_with" not in groups[("R1", "R2", "R3")]
    # R4's has the same difference, but its footprint came from another library.
    assert "Gone.PcbLib" in groups[("R4",)]["compared_with"]


def test_net_tie_variants_and_pin_edge_cases(tmp_path):
    _, libraries, netlist, pcb = _project(tmp_path)
    by_name = {component.designator: component for component in pcb.components}
    by_name["NT1"].kind = 0  # the PCB lost the Net Tie type the schematic has
    for item in pcb.primitives:
        if item.component == by_name["NT1"].index:
            item.net = NETS.index("A")  # both pads on one net
        if item.component == by_name["U1"].index and item.pad_name == "EP":
            item.net = NETS.index("GND")  # a net assigned on the board
    netlist["components"]["U2"]["pins"]["9"] = {"name": "", "net": "A"}  # not the LDO's pin
    by_name["U1"].footprint = "SOIC-5-OLD"  # the board kept an older footprint
    model = run_library_checks(LibraryCheckInputs(
        libraries, netlist, pcb, checks=["net_ties", "pads_cover_pins"]))
    found = _by_kind(model)
    (differs,) = found[("net_ties", "net_tie_type_differs")]
    assert differs["types"] == {"pcb": "Standard", "schematic": "Net Tie", "library": "Net Tie"}
    assert found[("net_ties", "net_tie_one_net")][0]["nets"] == ["A"]
    (stray,) = found[("pads_cover_pins", "board_pads_on_nets_without_pins")]
    assert stray["pads"] == [{"pad": "EP", "net": "GND"}]
    board_pads = {tuple(f["designators"]): f
                  for f in found[("pads_cover_pins", "used_pins_without_board_pad")]}
    assert (board_pads[("U1",)]["footprint"], board_pads[("U1",)]["schematic_footprint"]) == (
        "SOIC-5-OLD", "SOIC-5")
    assert "schematic_footprint" not in board_pads[("U3",)]  # placed with no pads at all
    assert model["summary"]["pads_cover_pins"][
        "skipped: netlist pins not on the part's symbol"] == 1


def test_a_net_tie_pad_left_out_of_the_tie(tmp_path):
    three = _footprint(tmp_path, footprint_streams("TIE-3", [
        pad("1", -20.0, 0.0, 20.0, 20.0), pad("2", 20.0, 0.0, 20.0, 20.0),
        pad("3", 80.0, 0.0, 20.0, 20.0), track(1, -20.0, 0.0, 20.0, 0.0, width=8.0)]), "TIE-3")
    assert copper_report(three).shorts == [["1", "2"]]
    root = tmp_path / "P"
    root.mkdir()
    write_ole(root / "Ties.PcbLib", pcb_lib_streams(footprint_streams("TIE-3", [
        pad("1", -20.0, 0.0, 20.0, 20.0), pad("2", 20.0, 0.0, 20.0, 20.0),
        pad("3", 80.0, 0.0, 20.0, 20.0), track(1, -20.0, 0.0, 20.0, 0.0, width=8.0)]),
        names=["TIE-3"]))
    (root / "P.PrjPcb").write_text("[Document1]\nDocumentPath=Ties.PcbLib\n", encoding="utf-8")
    libraries = project_libraries(str(root / "P.PrjPcb"), installed=[])
    part = _part(0, "NT9", 0.0, 0.0, footprint="TIE-3", kind=3, footprint_library="Ties.PcbLib")
    pcb = PcbData(board=decode_board([]), nets=list(NETS), components=[part],
                  primitives=_place(part, three, {"1": 0, "2": 1}), pours=[],
                  layer_name_to_v6={})
    model = run_library_checks(LibraryCheckInputs(libraries, None, pcb, checks=["net_ties"]))
    (loose,) = model["findings"]
    assert (loose["kind"], loose["pads"], loose["joined"]) == (
        "net_tie_pads_not_tied", ["3"], [["1", "2"]])
    assert model["summary"]["net_ties"]["net ties joining their pads"] == 1


def test_project_checks_without_a_board_or_a_netlist(tmp_path):
    _, libraries, netlist, pcb = _project(tmp_path)
    schematic_only = run_library_checks(LibraryCheckInputs(libraries, netlist, None))
    assert schematic_only["summary"]["footprint_drift"] == {"skipped": "no PcbDoc"}
    assert schematic_only["summary"]["height_3d"]["source"] == "library footprints"
    board_only = run_library_checks(LibraryCheckInputs(libraries, None, pcb))
    assert board_only["summary"]["pads_cover_pins"] == {"skipped": "no netlist"}
    assert board_only["summary"]["footprint_drift"]["status"]["match"] == 8
    assert board_only["summary"]["symbol_rules"]["symbols"] == 6


def test_part_filter_and_a_footprint_swap(tmp_path):
    _, libraries, netlist, pcb = _project(tmp_path)
    model = run_library_checks(LibraryCheckInputs(libraries, netlist, pcb, parts=["R1", "nope"],
                                                  checks=["footprint_drift"]))
    assert model["parts"] == {"schematic": 1, "pcb": 1}
    assert model["parts_not_found"] == ["nope"]
    by_name = run_library_checks(LibraryCheckInputs(libraries, netlist, pcb, parts=["amp"],
                                                    checks=["resolution"]))
    assert by_name["parts"] == {"schematic": 1, "pcb": 1}
    swap = run_library_checks(LibraryCheckInputs(libraries, netlist, pcb, parts=["U1", "R1"],
                                                 checks=["resolution"], footprint="SOT-3"))
    assert swap["checks_run"] == ["resolution", "pads_cover_pins"]
    rows = {row["designator"]: row for row in swap["swap"]}
    assert rows["R1"]["keeps_all_used_pins"] is True
    assert rows["R1"]["pads_without_pins"] == ["3"]
    assert rows["U1"]["keeps_all_used_pins"] is False
    assert rows["U1"]["used_pins_without_pad"] == ["6"]
    assert rows["U1"]["source"]["library"] == "Vendor.IntLib"
    assert ("pads_cover_pins", "swap_loses_pins") in _by_kind(swap)
    absent = run_library_checks(LibraryCheckInputs(libraries, netlist, pcb, parts=["R1"],
                                                   footprint="NOTHING"))
    assert absent["swap"][0]["keeps_all_used_pins"] is None


def test_errors(tmp_path):
    _, libraries, netlist, pcb = _project(tmp_path)

    def run(**arguments):
        return run_library_checks(LibraryCheckInputs(libraries, netlist, pcb, **arguments))

    assert run(checks=["nope"])["error"] == "invalid_checks"
    assert run(footprint="SOT-3")["error"] == "part_required"
    assert run(parts=["NOPE"])["error"] == "part_not_found"
    assert run(library="Missing.PcbLib")["error"] == "library_not_found"
    assert run(checks=["where_used"])["error"] == "where_used_name_required"
    assert run(library="Parts.PcbLib", footprint="X", parts=["R1"])["error"] == "invalid_arguments"


def test_a_whole_library(tmp_path):
    root, libraries, _, _ = _project(tmp_path)
    model = run_library_checks(LibraryCheckInputs(libraries, library="Parts.SchLib"))
    assert model["library"]["kind"] == "SchLib"
    assert model["checks_run"] == list(libs.LIBRARY_CHECKS)
    found = _by_kind(model)
    # The symbols' footprints are found in the project's PcbLib.
    assert found[("symbol_rules", "pins_without_pads")][0]["symbol"] == "AMP"
    assert found[("net_ties", "net_tie_without_copper")][0]["symbol"] == "TIEOPEN"
    pcb_lib = run_library_checks(LibraryCheckInputs(libraries, library=str(
        root / "Libs" / "Parts.PcbLib")))
    shorts = {f["footprint"] for f in _by_kind(pcb_lib)[("footprint_rules", "shorted_copper")]}
    assert shorts == {"TIE-2", "SHORT-2"}  # no symbol says TIE-2 is a net tie here
    assert pcb_lib["summary"]["library"]["footprints_by_3d"] == {
        "3d_model": 1, "height_only": 1, "no_height": 4}
    int_lib = run_library_checks(LibraryCheckInputs(libraries, library="Vendor.IntLib",
                                                    checks=["symbol_rules", "resolution"]))
    assert int_lib["summary"]["library"] == {"symbols": 1, "footprints": 1}
    assert int_lib["not_run"]["checks"] == ["resolution"]
    loose = tmp_path / "Loose.SchLib"
    write_ole(loose, _parts_sch_lib())
    by_path = run_library_checks(LibraryCheckInputs(libraries, library=str(loose)))
    assert by_path["library"]["origin"] == "source"


def test_the_loader_reads_each_library_once(tmp_path, monkeypatch):
    _, libraries, netlist, pcb = _project(tmp_path)
    calls = []
    original = libs.parse_pcb_lib
    monkeypatch.setattr(libs, "parse_pcb_lib",
                        lambda path, names=None: calls.append(names) or original(path, names))
    run_library_checks(LibraryCheckInputs(libraries, netlist, pcb, checks=["footprint_drift"]))
    read = [names for names in calls if names]  # names=set() reads the name index only
    assert len(read) == 1 and read[0] >= {"chip-2", "soic-5"}


def test_paging(tmp_path):
    _, libraries, netlist, pcb = _project(tmp_path)
    model = run_library_checks(LibraryCheckInputs(libraries, netlist, pcb))
    total = len(model["findings"])
    first = libs.page_library_checks(model, 0, 3)
    assert (len(first["findings"]), first["findings_total"], first["next_offset"]) == (3, total, 3)
    last = libs.page_library_checks(model, total - 1, 3)
    assert last["has_more"] is False and "next_offset" not in last
    assert last["summary"] == model["summary"] and last["notes"]


# ---------- where used ----------

def _schdoc(path: Path, designator: str, lib_reference: str, footprint: str) -> None:
    write_ole(path, {"FileHeader": stream([
        f"|RECORD=1|LIBREFERENCE={lib_reference}|SOURCELIBRARYNAME=Parts.SchLib"
        "|CURRENTPARTID=1|PARTCOUNT=2|UNIQUEID=AAAAAAAA",
        f"|RECORD=34|OWNERINDEX=1|NAME=Designator|TEXT={designator}",
        "|RECORD=44|OWNERINDEX=1",
        f"|RECORD=45|OWNERINDEX=3|MODELNAME={footprint}|MODELTYPE=PCBLIB|ISCURRENT=T",
    ])})


def _pcbdoc(path: Path, rows: list[dict]) -> None:
    write_ole(path, {"Components6/Data": b"".join(text_props(row) for row in rows)})


def test_where_used_across_projects(tmp_path):
    first, second = tmp_path / "P1", tmp_path / "P2"
    for folder, prj in ((first, "One"), (second, "Two")):
        folder.mkdir()
        (folder / f"{prj}.PrjPcb").write_text(
            f"[Document1]\nDocumentPath={prj}.SchDoc\n[Document2]\nDocumentPath={prj}.PcbDoc\n",
            encoding="utf-8")
    _schdoc(first / "One.SchDoc", "R7", "RES", "CHIP-2")
    _pcbdoc(first / "One.PcbDoc", [{"PATTERN": "CHIP-2", "SOURCEDESIGNATOR": "R7",
                                    "SOURCELIBREFERENCE": "RES",
                                    "SOURCEFOOTPRINTLIBRARY": "Parts.PcbLib"},
                                   {"PATTERN": "OTHER", "SOURCEDESIGNATOR": "C1"}])
    _schdoc(second / "Two.SchDoc", "U1", "AMP", "SOIC-5")
    (second / "Two.PcbDoc").write_bytes(b"not an OLE file")
    registry = {"projects": [{"name": "One.PrjPcb", "dir": str(first)},
                             {"name": "Gone.PrjPcb", "dir": str(tmp_path / "gone")}]}
    projects = libs.registry_projects(registry)
    assert [Path(path).name for path in projects] == ["One.PrjPcb"]
    projects += libs.project_files([str(second)])
    found = libs.where_used("chip-2", projects)
    assert (found["projects_scanned"], found["projects_using"]) == (2, 1)
    (use,) = found["uses"]
    assert (use["project"], use["schematic_parts_with_footprint"], use["pcb_parts_with_footprint"],
            use["pcb_parts_with_symbol"], use["designators"]) == ("One.PrjPcb", 1, 1, 0, ["R7"])
    assert libs.where_used("RES", projects)["uses"][0]["schematic_parts_with_symbol"] == 1
    assert libs.where_used("AMP", projects)["uses"][0]["project"] == "Two.PrjPcb"
    assert libs.where_used("CHIP-2", projects, library="Other.PcbLib")["projects_using"] == 0
    root, libraries, netlist, pcb = _project(tmp_path / "x")
    model = run_library_checks(LibraryCheckInputs(libraries, netlist, pcb, where_used="RES",
                                                  projects=projects))
    assert model["checks_run"] == ["where_used"] and model["findings"] == []
    assert model["where_used"]["projects_using"] == 1


# ---------- get_library_part ----------

def test_library_part_for_a_placed_part(tmp_path):
    _, libraries, netlist, pcb = _project(tmp_path)
    model = libs.library_part(libraries, netlist, pcb, designator="r3")
    assert model["schematic"]["lib_reference"] == "RES"
    assert model["placement"]["side"] == "top"
    assert (model["symbol"]["library"], model["symbol"]["origin"]) == ("Parts.SchLib", "project")
    assert [pin["designator"] for pin in model["symbol"]["pins"]] == ["1", "2"]
    footprint = model["footprint"]
    assert (footprint["name"], footprint["height_mil"], footprint["courtyard"], footprint["3d"]) == (
        "CHIP-2", 25.0, "found", "3d_model")
    assert footprint["pads"][0] == {"designator": "1", "x_mil": -40.0, "y_mil": 0.0,
                                    "layer": "Top Layer", "shape": "rectangular",
                                    "width_mil": 30.0, "height_mil": 40.0, "rotation": 0.0}
    assert footprint["bodies"][0]["model"] == "made-up.step"
    checks = model["checks"]
    assert checks["footprint_drift"]["status"] == "differs"
    assert checks["used_pins_without_library_pad"] == [] and checks["3d"] == "3d_model"
    u1 = libs.library_part(libraries, netlist, pcb, designator="U1")
    assert u1["checks"]["used_pins_without_library_pad"] == ["6"]
    assert {"pins_without_pads", "pads_without_pins"} <= _kinds(u1["checks"]["pins_vs_pads"])
    workspace = libs.library_part(libraries, netlist, pcb, designator="U3")
    assert workspace["symbol"] == {"found": False, "reason": "workspace part"}
    assert workspace["not_available_offline"]
    assert libs.library_part(libraries, netlist, pcb, designator="Q9")["error"] == "component_not_found"
    # Netlist pins that are not the symbol's (another part compiled under U2) are not
    # checked against the library footprint, as pads_cover_pins skips them.
    netlist["components"]["U2"]["pins"]["9"] = {"name": "", "net": "A"}
    u2 = libs.library_part(libraries, netlist, pcb, designator="U2")
    assert u2["checks"]["used_pins_without_library_pad"].startswith("not checked")
    r4 = libs.library_part(libraries, netlist, pcb, designator="R4")
    assert r4["symbol"]["reason"] == "library file missing"
    assert r4["symbol_elsewhere"]["library"] == "Parts.SchLib"
    assert r4["footprint"]["in_named_library"] is False


def test_library_part_for_library_items(tmp_path):
    _, libraries, _, _ = _project(tmp_path)
    ldo = libs.library_part(libraries, lib_reference="ldo")
    assert (ldo["symbol"]["library"], ldo["symbol"]["case_differs"]) == ("Vendor.IntLib", True)
    assert ldo["footprint"]["name"] == "SOT-3"  # the symbol's footprint, from its IntLib
    assert ldo["symbol"]["footprint_links"] == [
        {"name": "SOT-3", "current": True, "library": "the component's own library"}]
    named = libs.library_part(libraries, lib_reference="RES", library="Vendor.IntLib")
    assert named["symbol"]["found"] is False
    assert named["symbol_elsewhere"]["library"] == "Parts.SchLib"
    only = libs.library_part(libraries, footprint="SOIC-5")
    assert "symbol" not in only and only["footprint"]["pad_count"] == 6
    paged = libs.page_library_part(only, 2, 3)
    assert [pad["designator"] for pad in paged["footprint"]["pads"]] == ["3", "4", "5"]
    assert (paged["footprint"]["pads_total"], paged["next_offset"]) == (6, 5)


# ---------- the MCP tools ----------

@pytest.fixture
def loaded(monkeypatch, tmp_path):
    root, _, netlist, pcb = _project(tmp_path)
    (root / "Board.PcbDoc").write_bytes(b"pcb")
    project = {"name": "Board", "root_dir": str(root), "prj_pcb_path": str(root / "Board.PrjPcb"),
               "sheets": [], "pcb_doc_paths": [str(root / "Board.PcbDoc")], "parameters": {}}
    session = main_mod.PcbSession()
    session._load = lambda path, net: main_mod.PcbIndex(pcb, net)
    monkeypatch.setattr(main_mod, "_project", project)
    monkeypatch.setattr(main_mod._altium, "_netlist", netlist)
    monkeypatch.setattr(main_mod, "_variant_state", VariantState([VariantDefinition(name="Default")]))
    monkeypatch.setattr(main_mod, "_pcb_session", session)
    monkeypatch.setattr(main_mod, "_installed_library_paths", [])
    monkeypatch.setattr(main_mod, "read_registry", lambda: {"projects": []})
    return root


def test_check_libraries_tool(loaded):
    result = json.loads(main_mod.check_libraries())
    assert (result["project"], result["pcb_doc"], result["units"]) == ("Board", "Board.PcbDoc", "mil")
    assert result["findings_total"] == len(result["findings"]) and not result["has_more"]
    assert result["libraries"]["available"] == 3
    mm = json.loads(main_mod.check_libraries(checks=["footprint_drift"], units="mm"))
    (moved,) = [f for f in mm["findings"] if f["kind"] == "pads_moved"]
    assert moved["pads"][0]["delta_mm"] == [0.127, 0.0]
    page = json.loads(main_mod.check_libraries(offset=2))
    assert page["offset"] == 2 and len(page["findings"]) == result["findings_total"] - 2
    swap = json.loads(main_mod.check_libraries(part="U1", footprint="SOT-3"))
    assert swap["swap"][0]["keeps_all_used_pins"] is False
    used = json.loads(main_mod.check_libraries(where_used="RES", folders=[str(loaded)]))
    assert used["where_used"]["projects_scanned"] == 1


def test_check_libraries_tool_errors_and_size(loaded, monkeypatch):
    assert json.loads(main_mod.check_libraries(units="inch"))["error"] == "invalid_units"
    assert json.loads(main_mod.check_libraries(checks=["x"]))["error"] == "invalid_checks"
    assert json.loads(main_mod.check_libraries(folders=[str(loaded / "nope")]))["error"] == \
        "folder_not_found"
    full = main_mod.check_libraries()
    monkeypatch.setattr(main_mod, "QUERY_NET_MAX_RESULT_SIZE_CHARS", len(full) - 1)
    smaller = json.loads(main_mod.check_libraries())
    assert smaller["findings_limited_to"] < 100 and smaller["has_more"]
    monkeypatch.setattr(main_mod, "QUERY_NET_MAX_RESULT_SIZE_CHARS", 500)
    brief = json.loads(main_mod.check_libraries())
    assert "findings_omitted" in brief and brief["findings_total"] > 0
    monkeypatch.setattr(main_mod, "_project", None)
    assert json.loads(main_mod.check_libraries())["error"] == "no_project"
    assert json.loads(main_mod.get_library_part("R1"))["error"] == "no_project"


def test_get_library_part_tool(loaded, monkeypatch):
    result = json.loads(main_mod.get_library_part("U1", units="mm"))
    assert result["units"] == "mm" and result["footprint"]["pads"][0]["x_mm"] == pytest.approx(
        -0.762)
    assert result["footprint"]["pads_total"] == 6 and result["has_more"] is False
    assert json.loads(main_mod.get_library_part())["error"] == "nothing_to_look_up"
    assert json.loads(main_mod.get_library_part("U1", footprint="X"))["error"] == \
        "invalid_arguments"
    assert json.loads(main_mod.get_library_part(lib_reference="TIE"))["symbol"]["type"] == "Net Tie"
    full = main_mod.get_library_part("U1")
    monkeypatch.setattr(main_mod, "QUERY_NET_MAX_RESULT_SIZE_CHARS", len(full) - 1)
    smaller = json.loads(main_mod.get_library_part("U1"))
    assert smaller["page_limited_to"] < 400


def test_the_tools_are_registered():
    for name in ("get_library_part", "check_libraries"):
        tool = main_mod.mcp._tool_manager.get_tool(name)
        assert tool.annotations.readOnlyHint is True
        assert main_mod.SERVER_INSTRUCTIONS.index(name) > 4096
    manifest = json.loads((Path(__file__).parents[1] / "manifest.json").read_text("utf-8"))
    names = {entry["name"] for entry in manifest["tools"]}
    assert {"get_library_part", "check_libraries"} <= names
    readme = (Path(__file__).parents[1] / "README.md").read_text("utf-8")
    assert "`check_libraries`" in readme and "`get_library_part`" in readme
    description = " ".join(main_mod.mcp._tool_manager.get_tool("check_libraries").description.split())
    for phrase in ("Footprint Comparison Report", "Workspace (Altium 365)",
                   "check_db_parameters", "where_used", "library="):
        assert phrase in description, phrase


def test_round_and_rect_pads_compare_by_outline():
    assert libs._form(30.0, 40.0, 90.0, PAD_SHAPE_RECT, 0.05) == (40.0, 30.0, 0.0)
    assert libs._form(30.0, 30.0, 45.0, PAD_SHAPE_ROUND, 0.05) == (30.0, 30.0, 0.0)
    assert libs._form(30.0, 40.0, 270.0, PAD_SHAPE_RECT, 0.05) == (40.0, 30.0, 0.0)
    assert libs._form(30.0, 40.0, 30.0, PAD_SHAPE_RECT, 0.05) == (30.0, 40.0, 30.0)


# ---------- review fixes ----------

def test_a_used_pin_whose_only_pad_is_a_bare_hole_is_not_covered(tmp_path):
    # Pad 2 is a hole without copper, in the library and on the board: pin 2 is on a net
    # but has no copper to connect to.
    root = tmp_path / "H"
    root.mkdir()
    write_ole(root / "Parts.SchLib", _parts_sch_lib())
    holed = footprint_streams("HOLE-2", [
        pad("1", -40.0, 0.0, 30.0, 40.0),
        pad("2", 40.0, 0.0, 0.0, 0.0, layer=74, hole=30.0, plated=True)])
    write_ole(root / "Holes.PcbLib", pcb_lib_streams(holed, names=["HOLE-2"]))
    (root / "H.PrjPcb").write_text("[Document1]\nDocumentPath=Parts.SchLib\n"
                                   "[Document2]\nDocumentPath=Holes.PcbLib\n", encoding="utf-8")
    libraries = project_libraries(str(root / "H.PrjPcb"), installed=[])
    netlist = {"components": {"R1": _netlist_part("RES", "Parts.SchLib", "HOLE-2",
                                                  {"1": "A", "2": "B"})}}
    footprint = parse_pcb_lib(str(root / "Holes.PcbLib")).find("HOLE-2")
    assert [hole.pad_name for hole in footprint.holes] == ["2"]
    part = _part(0, "R1", 500.0, 500.0, footprint="HOLE-2", footprint_library="Holes.PcbLib",
                 component_library="Parts.SchLib", lib_reference="RES")
    hole = BareHole(x=540.0, y=500.0, hole=30.0, layer=74, net=1, component=0, pad_name="2")
    pcb = PcbData(board=decode_board([]), nets=list(NETS), components=[part],
                  primitives=_place(part, footprint, {"1": 0}), pours=[], layer_name_to_v6={},
                  bare_holes=[hole])
    model = run_library_checks(LibraryCheckInputs(libraries, netlist, pcb, parts=["R1"],
                                                  footprint="HOLE-2"))
    found = _by_kind(model)
    (library_pad,) = found[("pads_cover_pins", "used_pins_without_library_pad")]
    assert (library_pad["pins"], library_pad["on_bare_holes"]) == (["2"], ["2"])
    assert "bare hole" in library_pad["message"]
    (board_pad,) = found[("pads_cover_pins", "used_pins_without_board_pad")]
    assert (board_pad["pins"], board_pad["on_bare_holes"]) == (["2"], ["2"])
    assert "Unknown Pin" not in board_pad["message"]
    (swap,) = model["swap"]
    assert (swap["keeps_all_used_pins"], swap["used_pins_on_bare_holes"]) == (False, ["2"])
    rules = {f["kind"]: f for f in pin_pad_findings(SchLibComponent(
        "RES", "RES", pins=[_pin("1"), _pin("2")]), footprint)}
    assert rules["pins_without_pads"]["on_bare_holes"] == ["2"]
    part_model = libs.library_part(libraries, netlist, pcb, designator="R1")
    assert part_model["checks"]["used_pins_without_library_pad"] == ["2"]
    assert part_model["checks"]["used_pins_on_library_bare_holes"] == ["2"]


def test_a_graphical_symbol_without_pin_designators_is_info():
    symbol = SchLibComponent("EARTH", "EARTH", description="x", designator="#",
                             kind=2, pins=[_pin("", "GND", electrical="Power")])
    (finding,) = [f for f in symbol_rule_findings(symbol) if f["kind"] == "missing_pin_designators"]
    assert finding["severity"] == "info"
    symbol.kind = 0
    (finding,) = [f for f in symbol_rule_findings(symbol) if f["kind"] == "missing_pin_designators"]
    assert finding["severity"] == "error"


def test_library_part_narrows_a_footprint_to_library(tmp_path):
    _, libraries, _, _ = _project(tmp_path)
    elsewhere = libs.library_part(libraries, footprint="SOIC-5", library="Vendor.IntLib")
    assert elsewhere["footprint"] == {"found": False, "reason": "name not found"}
    assert elsewhere["footprint_elsewhere"]["library"] == "Parts.PcbLib"
    gone = libs.library_part(libraries, footprint="SOIC-5", library="Gone.PcbLib")
    assert gone["footprint"]["reason"] == "library file missing"
    inside = libs.library_part(libraries, footprint="SOT-3", library="Vendor.IntLib")
    assert (inside["footprint"]["library"], inside["footprint"]["pad_count"]) == ("Vendor.IntLib", 3)
    assert libs.library_part(libraries, lib_reference="RES", library="Parts.PcbLib")["error"] == \
        "library_kind_mismatch"
    assert libs.library_part(libraries, footprint="SOT-3", library="Parts.SchLib")["error"] == \
        "library_kind_mismatch"
    both = libs.library_part(libraries, lib_reference="RES", footprint="SOT-3",
                             library="Parts.SchLib")
    assert both["symbol"]["library"] == "Parts.SchLib"
    assert both["footprint"]["library"] == "Vendor.IntLib" and both["notes"]


def test_workspace_parts_are_one_finding_per_side(tmp_path):
    _, libraries, netlist, pcb = _project(tmp_path)
    # Each Workspace part's footprint names its own cached PcbLib.
    pcb.components.append(_part(10, "U4", 4000.0, 2000.0, footprint="QFN-B", lib_reference="MCU",
                                footprint_library=str(tmp_path / "cache" / "QFN-B.PcbLib"),
                                component_library="Some Workspace", workspace_part=True))
    model = run_library_checks(LibraryCheckInputs(libraries, netlist, pcb, checks=["resolution"]))
    (workspace,) = _by_kind(model)[("resolution", "footprint_workspace")]
    assert (workspace["parts"], workspace["library_count"]) == (2, 2)
    assert workspace["libraries"] == {"QFN-B.PcbLib": 1, "Some Workspace": 1}
    assert "library" not in workspace


def test_a_library_of_symbols_without_footprints_is_grouped(tmp_path):
    pins = [binary_pin("1", "A"), binary_pin("2", "B")]
    symbols = [(f"PAD{n}", sch_component(f"PAD{n}", pins, "made-up sensor", designator="S?"))
               for n in range(12)]
    source = sch_component("VSRC", pins, "made-up source", designator="V?") + record(
        "|RECORD=45|MODELNAME=VSRC|MODELTYPE=SIM|ISCURRENT=T")
    path = write_ole(tmp_path / "Sensors.SchLib", sch_lib_streams(*symbols, ("VSRC", source)))
    libraries = project_libraries(str(_project(tmp_path / "p")[0] / "Board.PrjPcb"), installed=[])
    model = run_library_checks(LibraryCheckInputs(libraries, library=str(path),
                                                  checks=["symbol_rules"]))
    found = _by_kind(model)
    (warning,) = [f for f in found[("symbol_rules", "missing_footprint")] if f["severity"] == "warning"]
    assert (warning["count"], len(warning["items"])) == (12, libs.EXAMPLES)
    assert "designators" not in warning
    (info,) = [f for f in found[("symbol_rules", "missing_footprint")] if f["severity"] == "info"]
    assert info["items"] == ["VSRC"]


def test_get_library_part_pages_bodies_and_falls_back_to_a_brief(loaded, monkeypatch):
    bodies = [body(SQUARE, OVERALLHEIGHT=f"{n + 1}mil", **{"MODEL.NAME": f"made-up-{n}.step"})
              for n in range(12)]
    write_ole(loaded / "Libs" / "Tall.PcbLib", pcb_lib_streams(
        footprint_streams("TALL", [pad("1", 0.0, 0.0, 20.0, 20.0), *bodies]), names=["TALL"]))
    prj = loaded / "Board.PrjPcb"
    prj.write_text(prj.read_text(encoding="utf-8") + "[Document6]\nDocumentPath=Libs\\Tall.PcbLib\n",
                   encoding="utf-8")
    model = libs.library_part(project_libraries(str(prj), installed=[]), footprint="TALL")
    summary = model["footprint"]["bodies_summary"]
    assert (summary["bodies"], summary["model_count"], summary["height_mil"]) == (12, 12, 12.0)
    page = libs.page_library_part(model, 10, 5)
    assert (len(page["footprint"]["bodies"]), page["footprint"]["bodies_total"]) == (2, 12)
    assert page["has_more"] is False
    assert libs.page_library_part(model, 0, 5)["next_offset"] == 5
    monkeypatch.setattr(main_mod, "QUERY_NET_MAX_RESULT_SIZE_CHARS", 1500)
    brief = json.loads(main_mod.get_library_part(footprint="TALL"))
    assert "omitted" in brief and "bodies" not in brief["footprint"]
    assert brief["footprint"]["bodies_summary"]["bodies"] == 12


def test_a_short_only_in_the_library_footprint_is_a_warning(tmp_path):
    _, libraries, netlist, pcb = _project(tmp_path)
    j1 = next(c for c in pcb.components if c.designator == "J1")
    for item in pcb.primitives:
        if item.component == j1.index and item.pad_name == "2":
            item.x1 += 30.0  # the placed copy's pads no longer touch
    model = run_library_checks(LibraryCheckInputs(libraries, netlist, pcb,
                                                  checks=["net_ties", "footprint_rules"]))
    found = _by_kind(model)
    assert ("net_ties", "short_not_net_tie") not in found
    (latent,) = found[("net_ties", "library_short_not_net_tie")]
    assert (latent["severity"], latent["nets"], latent["designators"]) == (
        "warning", {"J1": ["A", "B"]}, ["J1"])
    assert found[("footprint_rules", "shorted_copper")][0]["severity"] == "warning"
    assert model["severity_counts"].get("error", 0) == 0


def test_drift_groups_differences_within_the_tolerance(tmp_path):
    moves = {"R1": (0.0, 5.0), "R2": (0.0, 5.0), "R3": (5.03, 0.0)}
    _, libraries, netlist, pcb = _project(tmp_path, moved=moves)
    model = run_library_checks(LibraryCheckInputs(libraries, netlist, pcb,
                                                  checks=["footprint_drift"]))
    (group,) = [f for f in model["findings"] if f["kind"] == "pads_moved"]
    assert group["designators"] == ["R1", "R2", "R3"]
    assert "mil" not in group["message"] and group["max_delta_mil"] == pytest.approx(5.0)


def test_where_used_narrowed_to_a_library(tmp_path):
    folder = tmp_path / "P1"
    folder.mkdir()
    (folder / "One.PrjPcb").write_text(
        "[Document1]\nDocumentPath=One.SchDoc\n[Document2]\nDocumentPath=One.PcbDoc\n",
        encoding="utf-8")
    _schdoc(folder / "One.SchDoc", "R7", "RES", "CHIP-2")
    _pcbdoc(folder / "One.PcbDoc", [{"PATTERN": "CHIP-2", "SOURCEDESIGNATOR": "R7",
                                     "SOURCELIBREFERENCE": "RES",
                                     "SOURCECOMPONENTLIBRARY": "Parts.SchLib",
                                     "SOURCEFOOTPRINTLIBRARY": "Parts.PcbLib"}])
    projects = [str(folder / "One.PrjPcb")]
    (symbol,) = libs.where_used("RES", projects, library="Parts.SchLib")["uses"]
    assert (symbol["schematic_parts_with_symbol"], symbol["pcb_parts_with_symbol"]) == (1, 1)
    (footprint,) = libs.where_used("CHIP-2", projects, library="parts.pcblib")["uses"]
    assert (footprint["schematic_parts_with_footprint"], footprint["pcb_parts_with_footprint"]) == (
        0, 1)  # the sheet's footprint link names no library
    assert libs.where_used("RES", projects, library="Parts.PcbLib")["projects_using"] == 0
    _, libraries, _, _ = _project(tmp_path / "x")
    model = run_library_checks(LibraryCheckInputs(libraries, where_used="RES",
                                                  library="Parts.SchLib", projects=projects))
    assert "library" not in model and model["where_used"]["placed_from_library"] == "Parts.SchLib"
