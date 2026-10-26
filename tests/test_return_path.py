"""Return-path check (services/return_path, check_return_path): reference layers per signal
layer, net selection, split crossings, voids, plane edges, plane splits and stitching vias,
on synthetic boards."""

import asyncio
import json
import math
from pathlib import Path

import pytest

import server.main as main_mod
from server.parsers.pcb_doc import (
    PAD_SHAPE_RECT,
    BoardInfo,
    DiffPair,
    PcbClass,
    PcbComponent,
    PcbData,
    Primitive,
    XSignal,
    shape_points,
)
from server.parsers.prj_pcb import VariantDefinition, VariantState
from server.services import return_path as rpath
from server.services.pcb_index import PcbIndex
from tests.test_pcb_health import _region
from tests.test_reference_planes import (
    BOTTOM,
    MID,
    PLANE,
    TOP,
    _hatch,
    _layers,
    _square,
)
from tests.test_reference_planes import Board as _Board

NAMES = ["GND", "PWR", "SIG", "SIG2", "CLK_P", "CLK_N", "DDR_DQ0", "SPI_SCK", "USB_VBUS",
         "LED", "MISC"]
N = {name: number for number, name in enumerate(NAMES)}
GND, PWR, SIG, SIG2 = N["GND"], N["PWR"], N["SIG"], N["SIG2"]
REPO = Path(__file__).parents[1]


class Board(_Board):
    """The 1000 x 1000 mil board of test_reference_planes (Top / 5 mil / plane / 20 mil /
    Mid / 8 mil / Bottom), with more nets, parts, classes, pairs and xSignals."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.components: list[PcbComponent] = []
        self.classes: list[PcbClass] = []
        self.diff_pairs: list[DiffPair] = []
        self.xsignals: list[XSignal] = []

    def component(self, designator: str, x: float, y: float, comment: str | None = None) -> int:
        self.components.append(PcbComponent(
            index=len(self.components), refdes=designator, source_uid_tail=None,
            channel_path=None, x=x, y=y, rotation=0.0, side="top", designator=designator,
            comment=comment))
        return len(self.components) - 1

    def smd_pad(self, net, x, y, size=20.0, component=None, name="1", layer=TOP):
        self.prims.append(Primitive(
            kind="pad", layer=layer, net=net, component=component, x1=x, y1=y, x2=x, y2=y,
            width=size, height=size, shape=PAD_SHAPE_RECT, hole=0.0, pad_name=name))

    def net_class(self, name, members, kind="net"):
        self.classes.append(PcbClass(name=name, kind=kind, members=list(members),
                                     superclass=False, auto_generated=False, unique_id=None))

    def pour_layer(self, net, layer):
        """A pour of net covering the board on a signal layer, poured."""
        pour = self.pour(net, layer, _square(0, 0, 1000, 1000))
        self.area(net, layer, _square(10, 10, 990, 990), pour_id=pour)

    def index(self) -> PcbIndex:
        board = BoardInfo(
            origin_x=0.0, origin_y=0.0, sheet_x=0.0, sheet_y=0.0, sheet_width=2000.0,
            sheet_height=2000.0, display_unit="mil", layers=_layers(self.plane),
            stackup_source="v9", outline_vertices=shape_points(self.outline),
            outline_shape=list(self.outline), impedance_profiles=list(self.profiles),
            plane_nets={PLANE: self.plane_net} if self.plane and self.plane_net else {},
            plane_pullbacks={PLANE: self.pullback} if self.plane and self.pullback else {},
        )
        raw = {}
        if self.regions:
            raw["ShapeBasedRegions6/Data"] = self.regions
        data = PcbData(
            board=board, nets=list(NAMES), components=list(self.components),
            primitives=list(self.prims), pours=list(self.pours),
            layer_name_to_v6={"top layer": TOP, "ground plane": PLANE, "mid signal": MID,
                              "bottom layer": BOTTOM},
            rules=list(self.rules), classes=list(self.classes),
            diff_pairs=list(self.diff_pairs), xsignals=list(self.xsignals), raw=raw,
            areas=list(self.areas), area_streams=tuple(self.area_streams),
            bare_holes=list(self.bare_holes),
        )
        return PcbIndex(data, None)


def _split(gap_from=490.0, gap_to=510.0, line=True, **kwargs) -> Board:
    """GND split plane polygon left of gap_from, PWR right of gap_to (a split line between
    when line)."""
    board = Board(**kwargs)
    if line:
        board.track(None, PLANE, 500, 0, 500, 1000, width=gap_to - gap_from)
    board.split_plane(GND, _square(10, 10, gap_from, 990))
    board.split_plane(PWR, _square(gap_to, 10, 990, 990))
    return board


def _run(board: Board, **kwargs) -> dict:
    return rpath.prepare_return_path(board.index(), **kwargs)


def _findings(result: dict, check: str, severity: str | None = None) -> list[dict]:
    return [finding for finding in result["findings"] if finding["check"] == check
            and (severity is None or finding["severity"] == severity)]


def _places(result: dict, check: str, severity: str | None = None) -> list[dict]:
    return [place for finding in _findings(result, check, severity)
            for place in finding["occurrences"]]


# ---------- names ----------


@pytest.mark.parametrize("name, reason", [
    ("CLK_100M", "clock"), ("SPI_SCK", "clock"), ("DDR_CK_P", "clock"), ("REFCLK", "clock"),
    ("PCIE_TX0_P", "high_speed_bus"), ("USB_DP", "high_speed_bus"),
    ("USB2_D-", "high_speed_bus"), ("ETH_TXD0", "high_speed_bus"),
    ("DDR_DQ12", "high_speed_bus"), ("HDMI_D2_N", "high_speed_bus"),
    ("RF_IN", "high_speed_bus"), ("LED_RED", None), ("UART_TX", None), ("USB_EN", None),
    ("BLOCK_SEL", None), ("RESET_N", None),
    # An interface's sideband and control nets are slow: no name-only selection.
    ("PCIE_EN", None), ("PCIE_PERST_N", None), ("PCIE_CLKREQ_N", None), ("PCIE_WAKE", None),
    ("CLOCK_I2C_SCL", None), ("CLK_EN", None), ("DDR_RESET_N", None), ("DDR_ALERT", None),
    ("SMB_CLK", None), ("JTAG_TCK", None), ("SWCLK", None), ("ETH_LED1", None),
])
def test_high_speed_names(name, reason):
    assert rpath.high_speed_name(name) == reason


@pytest.mark.parametrize("name, power", [
    ("GND", True), ("AGND", True), ("+3V3", True), ("VDD_DDR", True), ("1V8_PLL", True),
    ("USB_VBUS", True), ("DDR_VREF", True), ("CLK_P", False), ("SIG", False),
    # Prefixed and suffixed supplies.
    ("AVDD", True), ("DVDD_1V2", True), ("IOVDD", True), ("PLLVCC", True), ("PGND", True),
    ("P3V3", True), ("PCIE_AUX3V3", True), ("PCIE_VAUX", True), ("VCORE", True),
    ("HDMI_AVDD", True), ("LVDS_HLVDDI_P", False), ("LVDS_TX0_N", False),
])
def test_power_names(name, power):
    assert rpath.power_name(name) is power


# ---------- selection ----------


def test_default_selection_and_its_reasons():
    board = _split()
    for number, name in enumerate(NAMES[2:]):
        y = 60 + number * 80
        board.track(N[name], TOP, 100, y, 400, y)
    board.diff_pairs.append(DiffPair("CLK", "CLK_P", "CLK_N", None))
    board.net_class("HighSpeed", ["SIG", "USB_VBUS"])
    board.net_class("Slow", ["LED"])
    first, second = board.component("U1", 100, 950), board.component("U2", 400, 950)
    board.smd_pad(N["MISC"], 100, 950, component=first)
    board.smd_pad(N["MISC"], 400, 950, component=second)
    board.xsignals.append(XSignal("XS1", ["U1-1", "U2-1"]))
    board.rule("Width", scope="InNet('SIG2')", IMPEDANCEPROFILEDRIVEN="TRUE")
    board.rule("Width", scope="All", IMPEDANCEPROFILEDRIVEN="TRUE")
    board.rule("Length", scope="InNet('LED')")
    result = _run(board)
    reasons = {row["net"]: row["reasons"] for row in result["selection"]["nets"]}
    assert reasons == {
        "CLK_P": ["diff_pair", "clock"], "CLK_N": ["diff_pair", "clock"],
        "SIG": ["high_speed_class"],
        "SIG2": ["impedance_rule"], "LED": ["length_rule"], "MISC": ["xsignal"],
        "DDR_DQ0": ["high_speed_bus"], "SPI_SCK": ["clock"],
    }
    assert result["selection"]["left_out"] == {"impedance_rule_scoped_all": 1,
                                               "power_or_ground_name": 1}
    assert result["summary"]["selection"] == "default"
    assert result["summary"]["nets_selected"] == 8
    pair = next(row for row in result["selection"]["nets"] if row["net"] == "CLK_P")
    assert pair["pair"] == "CLK" and pair["segments"] == 1 and pair["length_mil"] == 300.0


def test_requested_nets_pairs_and_classes():
    board = _split()
    board.track(SIG, TOP, 100, 200, 400, 200)
    board.track(N["CLK_P"], TOP, 100, 300, 400, 300)
    board.track(N["CLK_N"], TOP, 100, 340, 400, 340)
    board.diff_pairs.append(DiffPair("CLK", "CLK_P", "CLK_N", None))
    board.net_class("Mine", ["LED"])
    board.track(N["LED"], TOP, 100, 600, 400, 600)
    result = _run(board, nets=["CLK"], net_classes=["Mine"])
    assert {row["net"] for row in result["selection"]["nets"]} == {"CLK_P", "CLK_N", "LED"}
    assert result["selection"]["by_reason"] == {"requested": 3}
    assert result["summary"]["selection"] == "requested"
    assert _run(board, nets=["NOPE"])["error"] == "net_not_found"
    missing = _run(board, net_classes=["Nope"])
    assert missing["error"] == "net_class_not_found" and missing["net_classes"] == ["Mine"]


def test_a_rule_scope_taking_in_every_routed_net_selects_nothing(monkeypatch):
    monkeypatch.setattr(rpath, "SCOPE_ALL_MIN_NETS", 5)
    board = _split()
    for number, name in enumerate(["SIG", "SIG2", "LED", "MISC", "CLK_N"]):
        board.track(N[name], TOP, 100, 100 + number * 80, 400, 100 + number * 80)
    board.net_class("Everything", NAMES)
    board.net_class("Some", ["LED"])
    board.rule("Length", scope="InNetClass('Everything')")
    board.rule("Length", scope="InNetClass('Some')")
    result = _run(board)
    assert result["selection"]["left_out"] == {"length_rule_scoped_all": 1}
    reasons = {row["net"]: row["reasons"] for row in result["selection"]["nets"]}
    assert reasons == {"LED": ["length_rule"], "CLK_N": ["clock"]}


def test_nothing_selected_says_so():
    board = _split()
    board.track(N["LED"], TOP, 100, 200, 400, 200)
    result = _run(board)
    assert result["summary"]["nets_selected"] == 0 and result["selection"]["nets"] == []
    assert any("No net was selected" in note for note in result["notes"])


# ---------- layers ----------


def test_layer_report_and_no_adjacent_reference():
    board = Board(plane=False)
    board.track(SIG, TOP, 100, 500, 900, 500)
    board.track(N["LED"], BOTTOM, 100, 500, 900, 500)
    result = _run(board, nets=["SIG"])
    layers = {row["layer"]: row for row in result["layers"]}
    top = layers["Top Layer"]
    assert top["basis"] == "stackup_neighbours" and top["has_reference_copper"] is False
    assert top["references"] == [{"layer": "Mid Signal", "side": "below", "kind": "signal",
                                  "dielectric_mil": 5.0}]
    assert (top["routing_segments"], top["selected_segments"]) == (1, 1)
    findings = {finding["layer"]: finding for finding in
                _findings(result, "no_adjacent_reference")}
    assert set(findings) == {"Top Layer", "Bottom Layer"}  # Mid carries no routing
    assert findings["Top Layer"]["severity"] == "warning"
    assert findings["Bottom Layer"]["severity"] == "info"
    assert "above Mid Signal (signal)" in findings["Bottom Layer"]["issue"]
    assert result["summary"]["segments_without_reference"] == 1
    assert result["summary"]["signal_layers_without_reference"] == 3


def test_reference_edge_distances_follow_the_dielectric_or_the_override():
    board = Board()
    board.track(SIG, MID, 100, 500, 900, 500)
    result = _run(board, nets=["SIG"])
    mid = next(row for row in result["layers"] if row["layer"] == "Mid Signal")
    plane, bottom = mid["references"]
    assert (plane["kind"], plane["dielectric_mil"], plane["edge_distance_mil"],
            plane["edge_distance_basis"]) == ("plane", 20.0, 60.0, "3x_dielectric")
    assert bottom["kind"] == "signal" and "edge_distance_mil" not in bottom
    result = _run(board, nets=["SIG"], edge_distance_mil=12.5)
    plane = next(row for row in result["layers"] if row["layer"] == "Mid Signal")["references"][0]
    assert (plane["edge_distance_mil"], plane["edge_distance_basis"]) == (12.5, "edge_distance")


# ---------- crossings and voids ----------


def test_a_track_across_a_split_line_crosses_from_gnd_to_pwr():
    board = _split()
    board.track(SIG, TOP, 100, 500, 900, 500)
    result = _run(board, nets=["SIG"])
    (finding,) = _findings(result, "segment_crosses_split")
    assert finding["severity"] == "warning" and finding["nets"] == ["GND", "PWR"]
    assert (finding["net"], finding["layer"], finding["reference_layer"]) == (
        "SIG", "Top Layer", "Ground Plane")
    (place,) = finding["occurrences"]
    assert place["at"] == {"x_mil": 500.0, "y_mil": 500.0}
    assert (place["from_net"], place["to_net"], place["gap_mil"]) == ("GND", "PWR", 20.0)
    assert place["gap_causes"] == {"outside_split_planes": 1}
    assert not _findings(result, "segment_over_void")
    assert "crosses from one reference net to another" in finding["issue"]
    # Drawn the other way round, the same crossing.
    board = _split()
    board.track(SIG, TOP, 900, 500, 100, 500)
    place = _places(_run(board, nets=["SIG"]), "segment_crosses_split")[0]
    assert {place["from_net"], place["to_net"]} == {"GND", "PWR"}


def test_a_crossing_spanning_segments_is_found_on_the_chain():
    board = _split()
    board.track(SIG, TOP, 100, 500, 495, 500)  # ends in the gap
    board.track(SIG, TOP, 495, 500, 495, 450)
    board.track(SIG, TOP, 495, 450, 900, 450)
    places = _places(_run(board, nets=["SIG"]), "segment_crosses_split")
    assert len(places) == 1 and places[0]["gap_mil"] == pytest.approx(70.0)
    assert places[0]["at"] == {"x_mil": 495.0, "y_mil": 470.0}


@pytest.mark.parametrize("gap, check", [(100.0, "segment_crosses_split"),
                                        (101.0, "segment_over_void")])
def test_a_wide_gap_between_two_nets_is_a_void(gap, check):
    board = _split(gap_from=500 - gap / 2, gap_to=500 + gap / 2, line=False)
    board.track(SIG, TOP, 100, 500, 900, 500)
    result = _run(board, nets=["SIG"])
    (place,) = _places(result, check)
    if check == "segment_over_void":
        assert (place["from_net"], place["to_net"]) == ("GND", "PWR")
        assert place["length_mil"] == pytest.approx(gap)
        assert place["cause"] == "outside_split_planes"
        assert _findings(result, check)[0]["severity"] == "warning"
    else:
        assert place["gap_mil"] == pytest.approx(gap)


def test_void_causes_and_their_severity():
    board = Board()
    board.regions = _region(PLANE, _square(300, 400, 400, 500))  # a drawn void
    board.track(SIG, TOP, 100, 450, 900, 450)
    board.via(SIG2, 600, 450)  # one anti-pad: 10 + 20 mil
    board.via(SIG2, 720, 450)
    board.via(SIG2, 760, 450)  # two anti-pads merged into a slot
    board.via(SIG, 900, 450)  # its own via: its anti-pad is not a void
    result = _run(board, nets=["SIG"])
    warnings = _places(result, "segment_over_void", "warning")
    assert sorted((place["cause"], place["length_mil"]) for place in warnings) == [
        ("drawn_void", 100.0), ("hole_clearances_merged", 100.0)]
    (info,) = _places(result, "segment_over_void", "info")
    assert info["cause"] == "hole_clearance" and info["length_mil"] == pytest.approx(60.0)
    assert info["hole"] == {"kind": "via", "at": {"x_mil": 600.0, "y_mil": 450.0},
                            "net": "SIG2"}
    assert info["likely"] is True  # anti-pad sizes are assumed
    (finding,) = _findings(result, "segment_over_void", "warning")
    assert finding["count"] == 2 and finding["length_mil"] == 200.0
    assert finding["nets"] == ["GND"] and finding["likely"] is True
    drawn = next(place for place in warnings if place["cause"] == "drawn_void")
    assert (drawn["from"], drawn["to"]) == ({"x_mil": 300.0, "y_mil": 450.0},
                                            {"x_mil": 400.0, "y_mil": 450.0})
    assert result["summary"]["segments_checked"] == 1
    assert not _findings(result, "segment_near_plane_edge")


def test_a_short_gap_is_not_reported():
    board = Board()
    board.regions = _region(PLANE, _square(500, 400, 501.5, 500))
    board.track(SIG, TOP, 100, 450, 900, 450)
    assert not _findings(_run(board, nets=["SIG"]), "segment_over_void")
    board.regions = _region(PLANE, _square(500, 400, 502.5, 500))
    assert _places(_run(board, nets=["SIG"]), "segment_over_void")[0]["length_mil"] == 2.5


def test_a_cut_out_under_the_nets_own_pad_is_info():
    board = Board()
    board.smd_pad(SIG, 700, 300, size=60.0)
    board.regions = _region(PLANE, _square(660, 260, 740, 340))
    board.track(SIG, TOP, 500, 300, 700, 300)
    (finding,) = _findings(_run(board, nets=["SIG"]), "segment_over_void")
    assert finding["severity"] == "info"
    assert finding["occurrences"][0]["cause"] == "under_own_pad"
    assert finding["length_mil"] == pytest.approx(40.0)


def test_a_stripline_whose_other_reference_stays_continuous_is_info():
    board = Board()
    board.pour_layer(GND, BOTTOM)
    board.regions = _region(PLANE, _square(300, 400, 400, 500))
    board.track(SIG, MID, 100, 450, 900, 450)
    result = _run(board, nets=["SIG"])
    (finding,) = _findings(result, "segment_over_void")
    assert finding["severity"] == "info" and finding["reference_layer"] == "Ground Plane"
    assert finding["occurrences"][0]["other_reference_continuous"] == {
        "layer": "Bottom Layer", "net": "GND"}
    mid = next(row for row in result["layers"] if row["layer"] == "Mid Signal")
    assert [ref["kind"] for ref in mid["references"]] == ["plane", "pour"]


def test_a_crossing_under_a_continuous_second_reference_is_info():
    board = _split()
    board.pour_layer(GND, BOTTOM)
    board.track(SIG, MID, 100, 500, 900, 500)
    (finding,) = _findings(_run(board, nets=["SIG"]), "segment_crosses_split")
    assert finding["severity"] == "info"
    assert finding["occurrences"][0]["other_reference_continuous"]["layer"] == "Bottom Layer"


def _circle(cx, cy, radius, count=90):
    return [(cx + radius * math.cos(2 * math.pi * k / count),
             cy + radius * math.sin(2 * math.pi * k / count)) for k in range(count)]


def _union_ring(circles, cx, cy, count=180, step=0.02):
    """The outline of a union of circles that is star-shaped from (cx, cy)."""
    ring = []
    for k in range(count):
        angle = 2 * math.pi * k / count
        radius = 0.0
        while any(math.hypot(cx + (radius + step) * math.cos(angle) - x,
                             cy + (radius + step) * math.sin(angle) - y) <= r
                  for x, y, r in circles):
            radius += step
        ring.append((cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
    return ring


def _pour_board(*holes, net=GND, areas=None) -> Board:
    """No plane: Mid's only plane or pour reference is a pour of net on Bottom, its copper
    one region with holes (or the areas given)."""
    board = Board(plane=False)
    pour = board.pour(net, BOTTOM, _square(0, 0, 1000, 1000))
    for outline, cut in areas or [(_square(10, 10, 990, 990), holes)]:
        board.area(net, BOTTOM, outline, holes=list(cut), pour_id=pour)
    return board


def test_a_pour_clearance_is_measured_around_the_hole():
    # A via of another net (copper radius 5) with 10 mil of pour clearance around it: the
    # gap where the track passes beside it is that via's clearance.
    board = _pour_board(_circle(500, 512, 15))
    board.via(SIG2, 500, 512, diameter=10.0, hole=5.0)
    board.track(N["CLK_P"], MID, 100, 503, 900, 503, width=4.0)
    (place,) = _places(_run(board, nets=["CLK_P"]), "segment_over_void", "info")
    assert place["cause"] == "hole_clearance" and place["hole"]["net"] == "SIG2"
    assert place["length_mil"] == pytest.approx(24.0, abs=0.1)
    # A slot reaching 26 mil past the same via's copper is not its clearance (10 mil).
    board = _pour_board(_square(470, 490, 530, 530))
    board.via(SIG2, 500, 512, diameter=10.0, hole=5.0)
    board.track(N["CLK_P"], MID, 100, 500, 900, 500, width=4.0)
    (finding,) = _findings(_run(board, nets=["CLK_P"]), "segment_over_void")
    assert finding["severity"] == "warning"
    assert finding["occurrences"][0]["cause"] == "no_copper"
    assert finding["length_mil"] == pytest.approx(60.0)


def test_merged_pour_clearances_of_several_vias_are_a_warning():
    vias = [(478, 512), (500, 512), (522, 512)]
    board = _pour_board(_union_ring([(x, y, 15.0) for x, y in vias], 500, 512))
    for (x, y), net in zip(vias, (SIG, SIG2, N["LED"])):
        board.via(net, x, y, diameter=10.0, hole=5.0)
    board.track(N["CLK_P"], MID, 100, 503, 900, 503, width=4.0)
    (finding,) = _findings(_run(board, nets=["CLK_P"]), "segment_over_void")
    assert finding["severity"] == "warning"
    (place,) = finding["occurrences"]
    assert place["cause"] == "hole_clearances_merged" and "hole" not in place
    assert place["length_mil"] == pytest.approx(68.0, abs=0.2)


def test_only_the_nets_own_via_clearance_is_left_out():
    # The via's own clearance (10 mil) is left out; a slot 20 to 40 mil past its copper is
    # a void.
    board = _pour_board(areas=[(_square(10, 10, 530, 990), [_circle(500, 500, 20)]),
                               (_square(550, 10, 990, 990), [])])
    board.track(N["CLK_P"], TOP, 100, 500, 500, 500)
    board.via(N["CLK_P"], 500, 500)
    board.track(N["CLK_P"], MID, 500, 500, 900, 500)
    (place,) = _places(_run(board, nets=["CLK_P"], checks=["voids"]), "segment_over_void",
                       "warning")
    assert place["cause"] == "no_copper"
    assert (place["from"], place["to"]) == ({"x_mil": 530.0, "y_mil": 500.0},
                                            {"x_mil": 550.0, "y_mil": 500.0})


def test_a_gap_running_on_past_the_cut_out_under_the_own_pad_is_a_void():
    board = Board()
    board.smd_pad(SIG, 700, 300, size=60.0)
    board.regions = _region(PLANE, _square(660, 260, 740, 340)) + \
        _region(PLANE, _square(500, 290, 660, 310))  # a slot on from the cut-out
    board.track(SIG, TOP, 400, 300, 700, 300)
    (finding,) = _findings(_run(board, nets=["SIG"]), "segment_over_void")
    assert finding["severity"] == "warning"
    (place,) = finding["occurrences"]
    assert place["cause"] == "drawn_void"
    assert place["from"] == {"x_mil": 500.0, "y_mil": 300.0}
    assert place["to"]["x_mil"] == pytest.approx(658.0, abs=0.05)  # 10 + 2 mil off the pad


def test_another_nets_copper_on_a_pour_reference_is_a_gap_unless_listed_or_a_supply():
    def board_with(net, island, track=None):
        board = _pour_board(_square(290, 390, 710, 610))
        board.area(net, BOTTOM, island)
        if track:
            board.track(net, BOTTOM, *track, width=40.0)
        board.track(N["CLK_P"], MID, 350, 500, 650, 500)
        return _run(board, nets=["CLK_P"], checks=["voids", "splits"])

    # A signal net's small fill elsewhere does not make its routing a reference.
    result = board_with(SIG2, _square(300, 400, 310, 410), track=(300, 500, 700, 500))
    (place,) = _places(result, "segment_over_void", "warning")
    assert place["cause"] == "other_net_copper" and place["copper_net"] == "SIG2"
    assert place["length_mil"] == pytest.approx(300.0)
    # A supply's island (0.4% of the board) is reference copper: the run sits on it.
    result = board_with(PWR, _square(320, 450, 680, 550))
    assert not _findings(result, "segment_over_void")
    assert not _findings(result, "segment_crosses_split")
    layer = next(row for row in result["layers"] if row["layer"] == "Mid Signal")
    assert [ref["nets"] for ref in layer["references"] if ref["kind"] == "pour"] == [["GND"]]


def test_a_reference_nets_pad_inside_another_nets_pour_is_part_of_the_gap():
    # A GND pad on Bottom inside the PWR pour's clearance: not two crossings to GND and
    # back, one gap.
    board = _pour_board(net=PWR, areas=[(_square(10, 10, 990, 890), [_circle(500, 500, 20)])])
    ground = board.pour(GND, BOTTOM, _square(0, 900, 1000, 1000))
    board.area(GND, BOTTOM, _square(10, 900, 990, 990), pour_id=ground)
    board.smd_pad(GND, 500, 500, size=20.0, layer=BOTTOM)
    board.track(N["CLK_P"], MID, 100, 500, 900, 500)
    result = _run(board, nets=["CLK_P"])
    assert not _findings(result, "segment_crosses_split")
    assert not _findings(result, "segment_near_plane_edge")  # nor an edge over the pad
    (place,) = _places(result, "segment_over_void")
    assert place["length_mil"] == pytest.approx(40.0, abs=0.1)
    assert place["cause"] == "reference_net_island"
    # A GND pad relieved into a GND pour stays copper: only the air gaps are gaps.
    board = _pour_board(areas=[(_square(10, 10, 990, 990), [_circle(500, 500, 20)])])
    board.smd_pad(GND, 500, 500, size=20.0, layer=BOTTOM)
    board.track(N["CLK_P"], MID, 100, 500, 900, 500)
    places = _places(_run(board, nets=["CLK_P"]), "segment_over_void")
    assert sorted(place["length_mil"] for place in places) == pytest.approx([10.0, 10.0])
    assert {place["cause"] for place in places} == {"no_copper"}


def test_a_channel_a_hatched_pour_leaves_out_is_a_void():
    # A hatch as wide as its grid: read exactly, the channel (rows 120-134) is a void.
    board = Board(plane=False)
    _hatch(board, GND, BOTTOM, 0, 0, 1000, 1000, grid=4.0, width=4.0, skip=range(120, 135))
    board.track(N["CLK_P"], MID, 500, 100, 500, 900)
    (place,) = _places(_run(board, nets=["CLK_P"], checks=["voids"]), "segment_over_void")
    assert place["cause"] == "no_copper" and "likely" not in place
    assert place["length_mil"] == pytest.approx(59.9, abs=1e-3)
    # A hatch with openings is read with them bridged: likely.
    board = Board(plane=False)
    _hatch(board, GND, BOTTOM, 0, 0, 1000, 1000, grid=20.0, width=4.0, skip=range(24, 28))
    board.track(N["CLK_P"], MID, 500, 100, 500, 900)
    (place,) = _places(_run(board, nets=["CLK_P"], checks=["voids"]), "segment_over_void")
    assert place["cause"] == "no_copper" and place["likely"] is True
    assert place["length_mil"] == pytest.approx(79.9, abs=1e-3)


def test_a_dot_of_track_does_not_break_the_chain():
    board = Board()
    board.regions = _region(PLANE, _square(450, 400, 550, 500))
    board.track(SIG, TOP, 100, 450, 500, 450)
    board.track(SIG, TOP, 500, 450, 500.0001, 450)
    board.track(SIG, TOP, 500, 450, 900, 450)
    (place,) = _places(_run(board, nets=["SIG"]), "segment_over_void")
    assert place["length_mil"] == pytest.approx(100.0)


# ---------- plane edges ----------


@pytest.mark.parametrize("x, edge, found", [
    (480.0, None, True), (470.0, None, False), (476.0, None, True), (474.0, None, False),
    (470.0, 25.0, True), (470.0, 19.0, False),
])
def test_runs_within_the_edge_distance_of_a_split(x, edge, found):
    board = _split()
    board.track(SIG, TOP, x, 200, x, 800)  # GND copper ends at x = 490; edge 3 x 5 mil
    result = _run(board, nets=["SIG"], edge_distance_mil=edge)
    findings = _findings(result, "segment_near_plane_edge")
    assert bool(findings) is found
    if found:
        (finding,) = findings
        assert finding["severity"] == "info"
        assert finding["edge_distance_mil"] == (edge or 15.0)
        (place,) = finding["occurrences"]
        assert place["length_mil"] == pytest.approx(600.0)
        assert place["edge"] == "outside_split_planes" and place["both_sides"] is False
    assert not _findings(result, "segment_over_void")


def test_a_via_clearance_beside_a_track_is_not_an_edge():
    board = Board()
    board.track(SIG, TOP, 100, 500, 900, 500)
    board.via(SIG2, 500, 530)  # anti-pad radius 30 reaches the offset line at y = 515
    result = _run(board, nets=["SIG"])
    assert not _findings(result, "segment_near_plane_edge")
    assert not _findings(result, "segment_over_void")


def test_the_board_edge_pullback_is_an_edge_and_a_void():
    board = Board()  # 10 mil pullback
    board.track(SIG, TOP, 20, 100, 20, 900)  # 10 mil from the plane's edge
    board.track(SIG2, TOP, 5, 100, 5, 900)  # over the pullback
    result = _run(board, nets=["SIG", "SIG2"])
    (edge,) = _findings(result, "segment_near_plane_edge")
    assert edge["net"] == "SIG" and edge["occurrences"][0]["edge"] == "pullback"
    (void,) = _findings(result, "segment_over_void")
    assert void["net"] == "SIG2" and void["occurrences"][0]["cause"] == "pullback"
    assert void["length_mil"] == pytest.approx(800.0)


# ---------- plane splits ----------


def test_split_planes_and_split_reference_pours_are_listed():
    board = _split()
    board.track(SIG, MID, 100, 100, 200, 100)
    left = board.pour(GND, BOTTOM, _square(0, 0, 500, 1000))
    board.area(GND, BOTTOM, _square(10, 10, 490, 990), pour_id=left)
    right = board.pour(PWR, BOTTOM, _square(500, 0, 1000, 1000))
    board.area(PWR, BOTTOM, _square(510, 10, 990, 990), pour_id=right)
    result = _run(board, nets=["SIG"])
    splits = {entry["layer"]: entry for entry in result["plane_splits"]}
    plane = splits["Ground Plane"]
    assert plane["type"] == "plane" and plane["nets"] == ["GND", "PWR"]
    assert plane["used_as_reference_by"] == ["Top Layer", "Mid Signal"]
    assert [region["net"] for region in plane["regions"]] == ["GND", "PWR"]
    bottom = splits["Bottom Layer"]
    assert bottom["type"] == "signal_layer_pours" and bottom["nets"] == ["GND", "PWR"]
    assert bottom["copper"][0]["share"] == pytest.approx(0.47, abs=0.01)
    assert {finding["layer"] for finding in _findings(result, "plane_split")} == {
        "Ground Plane", "Bottom Layer"}
    assert result["summary"]["plane_splits"] == 2


# ---------- stitching ----------


def _layer_change(board: Board, net: int, x: float, y: float) -> None:
    """A track on Top into a through via at (x, y), on from it on Bottom."""
    board.track(net, TOP, 100, y, x, y)
    board.via(net, x, y)
    board.track(net, BOTTOM, x, y, 900, y)


def test_a_layer_change_between_references_needs_a_stitching_via():
    board = Board()
    board.pour_layer(GND, MID)  # Bottom's reference: Mid's GND pour; Top's: the plane
    _layer_change(board, SIG, 500, 500)
    result = _run(board, nets=["SIG"])
    (finding,) = _findings(result, "missing_stitching_via")
    assert finding["severity"] == "info" and finding["reference_net_change"] is False
    (place,) = finding["occurrences"]
    assert place["layers"] == ["Top Layer", "Bottom Layer"]
    assert place["reference_layers"] == {"Top Layer": ["Ground Plane"],
                                         "Bottom Layer": ["Mid Signal"]}
    assert place["reference_nets"] == ["GND"]
    assert "nearest_stitching_via_mil" not in place
    assert result["summary"]["layer_changes"] == {"total": 1, "missing": 1}
    assert not _findings(result, "segment_over_void")
    board.via(GND, 500, 560)  # a GND via 60 mil away: too far
    place = _places(_run(board, nets=["SIG"]), "missing_stitching_via")[0]
    assert place["nearest_stitching_via_mil"] == 60.0
    assert place["nearest_stitching_via"]["net"] == "GND"
    result = _run(board, nets=["SIG"], stitch_radius_mil=61.0)
    assert result["summary"]["layer_changes"] == {"total": 1, "stitched": 1}
    board.via(GND, 500, 530)
    result = _run(board, nets=["SIG"])
    assert not _findings(result, "missing_stitching_via")
    assert result["summary"]["layer_changes"] == {"total": 1, "stitched": 1}


def test_a_stitching_via_must_reach_both_reference_layers():
    board = Board()
    board.pour_layer(GND, MID)
    _layer_change(board, SIG, 500, 500)
    board.via(GND, 500, 530, layer=TOP, layer_to=PLANE)  # a blind via to the plane only
    result = _run(board, nets=["SIG"])
    assert result["summary"]["layer_changes"] == {"total": 1, "missing": 1}


def test_a_shared_reference_needs_no_stitching_via():
    board = Board()
    board.pour_layer(GND, MID)
    board.track(SIG, TOP, 100, 500, 500, 500)
    board.via(SIG, 500, 500, layer=TOP, layer_to=MID)
    board.track(SIG, MID, 500, 500, 900, 500)
    result = _run(board, nets=["SIG"])
    assert result["summary"]["layer_changes"] == {"total": 1, "shared_reference": 1}
    assert not _findings(result, "missing_stitching_via")


def test_a_reference_net_change_needs_a_capacitor():
    board = Board()
    board.pour_layer(PWR, MID)  # Top over GND, Bottom over PWR
    _layer_change(board, SIG, 500, 500)
    result = _run(board, nets=["SIG"])
    (finding,) = _findings(result, "missing_stitching_via")
    assert finding["severity"] == "warning" and finding["reference_net_change"] is True
    assert finding["occurrences"][0]["reference_nets"] == ["GND", "PWR"]
    assert "reference net changes" in finding["issue"]
    capacitor = board.component("C1", 500, 535)
    board.smd_pad(GND, 485, 535, component=capacitor, name="1")
    board.smd_pad(PWR, 515, 535, component=capacitor, name="2")
    result = _run(board, nets=["SIG"])
    assert not _findings(result, "missing_stitching_via")
    assert result["summary"]["layer_changes"] == {"total": 1, "stitched_by_capacitor": 1}
    resistor = Board()
    resistor.pour_layer(PWR, MID)
    _layer_change(resistor, SIG, 500, 500)
    part = resistor.component("R1", 500, 535)
    resistor.smd_pad(GND, 485, 535, component=part, name="1")
    resistor.smd_pad(PWR, 515, 535, component=part, name="2")
    assert _run(resistor, nets=["SIG"])["summary"]["layer_changes"] == {
        "total": 1, "reference_net_change": 1}


@pytest.mark.parametrize("designator, comment, status", [
    ("C7", None, "stitched_by_capacitor"), ("CA1", "100nF 16V", "stitched_by_capacitor"),
    ("CN1", None, "reference_net_change"), ("CR1", None, "reference_net_change"),
    ("CON2", None, "reference_net_change"), ("CLK1", None, "reference_net_change"),
    ("CA1", None, "reference_net_change"),
])
def test_only_a_capacitor_stitches_a_reference_net_change(designator, comment, status):
    board = Board()
    board.pour_layer(PWR, MID)
    _layer_change(board, SIG, 500, 500)
    part = board.component(designator, 500, 535, comment=comment)
    board.smd_pad(GND, 485, 535, component=part, name="1")
    board.smd_pad(PWR, 515, 535, component=part, name="2")
    assert _run(board, nets=["SIG"])["summary"]["layer_changes"] == {"total": 1, status: 1}


def test_every_pair_of_changing_reference_nets_needs_a_capacitor():
    board = Board()
    capacitor = board.component("C1", 500, 535)
    board.smd_pad(GND, 485, 535, component=capacitor, name="1")
    board.smd_pad(PWR, 515, 535, component=capacitor, name="2")
    index = board.index()
    check = rpath._Check(index, {}, True, None, set(rpath.CHECKS), None, 39.37, "mil")

    class Ref:
        def __init__(self, layer):
            self.layer = layer

    top, plane, bottom = Ref(TOP), Ref(PLANE), Ref(BOTTOM)
    local = {TOP: "GND", PLANE: "PWR", BOTTOM: "SIG2"}
    found, missing = check._stitch_capacitors({"x": 500.0, "y": 500.0},
                                              [(top, plane), (top, bottom)], local)
    assert found == [{"designator": "C1", "nets": ["GND", "PWR"]}]
    assert missing == [["GND", "SIG2"]]
    found, missing = check._stitch_capacitors({"x": 500.0, "y": 500.0}, [(top, plane)], local)
    assert missing == []


def test_a_pairs_two_vias_are_judged_together():
    board = Board()
    board.pour_layer(GND, MID)
    board.diff_pairs.append(DiffPair("CLK", "CLK_P", "CLK_N", None))
    _layer_change(board, N["CLK_P"], 500, 300)
    _layer_change(board, N["CLK_N"], 500, 340)
    result = _run(board)
    (finding,) = _findings(result, "missing_stitching_via")
    assert finding["pair"] == "CLK" and "net" not in finding and finding["count"] == 1
    place = finding["occurrences"][0]
    assert {place["at"]["y_mil"], place["pair_via"]["y_mil"]} == {300.0, 340.0}
    assert "Pair CLK" in finding["issue"]
    assert result["summary"]["layer_changes"] == {"total": 2, "missing": 2}
    assert result["summary"]["pair_layer_changes"] == {"missing": 1}
    board.via(GND, 530, 320)  # one GND via beside the pair stitches both
    result = _run(board)
    assert result["summary"]["pair_layer_changes"] == {"stitched": 1}
    assert not _findings(result, "missing_stitching_via")


# ---------- options, paging and errors ----------


def test_checks_layer_filter_and_errors():
    board = _split()
    board.track(SIG, TOP, 100, 500, 900, 500)
    board.track(SIG, MID, 100, 700, 900, 700)
    only = _run(board, nets=["SIG"], checks=["splits"])
    assert only["checks_run"] == ["segment_crosses_split"]
    assert {finding["check"] for finding in only["findings"]} == {"segment_crosses_split"}
    top = _run(board, nets=["SIG"], layer="Top Layer", checks=["segment_crosses_split"])
    assert {finding["layer"] for finding in top["findings"]} == {"Top Layer"}
    assert top["parameters"]["layer"] == "Top Layer"
    plane = _run(board, nets=["SIG"], layer="Ground Plane", checks=["splits"])
    assert {finding["layer"] for finding in plane["findings"]} == {"Top Layer", "Mid Signal"}
    bad = _run(board, checks=["bogus"])
    assert bad["error"] == "invalid_checks" and "segment_over_void" in bad["valid_checks"]
    missing = _run(board, layer="Mechanical 1")
    assert missing["error"] == "layer_not_found" and "Ground Plane" in missing["layers"]


def test_render_pages_findings_and_caps_places():
    board = Board()
    for number in range(4):
        board.regions += _region(PLANE, _square(200 + number * 150, 400, 260 + number * 150,
                                                500))
    board.track(SIG, TOP, 100, 450, 900, 450)
    board.track(SIG2, TOP, 100, 300, 900, 300)
    board.regions += _region(PLANE, _square(400, 250, 500, 350))
    prepared = rpath.prepare_return_path(board.index(), nets=["SIG", "SIG2"])
    assert prepared["summary"]["findings_by_check"] == {"segment_over_void": 2}
    page = rpath.render_return_path(prepared, offset=0, limit=1, occurrences=2)
    assert page["total"] == 2 and page["has_more"] is True and page["next_offset"] == 1
    (finding,) = page["findings"]
    assert finding["net"] == "SIG" and finding["count"] == 4
    assert len(finding["occurrences"]) == 2 and finding["occurrences_listed"] == 2
    last = rpath.render_return_path(prepared, offset=1, limit=1)
    assert last["findings"][0]["net"] == "SIG2" and last["has_more"] is False
    assert "next_offset" not in last and last["approximations"] == rpath.APPROXIMATIONS
    assert len(prepared["findings"][0]["occurrences"]) == 4  # not changed by rendering
    nets = rpath.render_return_path(prepared, nets=1)["selection"]
    assert len(nets["nets"]) == 1 and nets["nets_total"] == 2


def test_cached_results_are_reused_per_arguments():
    index = _split().index()
    first = rpath.prepare_return_path_cached(index, nets=None)
    assert rpath.prepare_return_path_cached(index, nets=None) is first
    assert rpath.prepare_return_path_cached(index, nets=None, layer="Top Layer") is not first
    assert "error" in rpath.prepare_return_path_cached(index, nets=["NOPE"])


# ---------- the MCP tool ----------


@pytest.fixture
def tool_board(monkeypatch, tmp_path):
    board = _split()
    board.track(SIG, TOP, 100, 500, 900, 500)
    board.track(N["CLK_P"], TOP, 480, 200, 480, 400)
    index = board.index()
    pcb_file = tmp_path / "Board.PcbDoc"
    pcb_file.write_bytes(b"fake")
    monkeypatch.setattr(main_mod, "_project", {
        "name": "Demo", "root_dir": str(tmp_path), "prj_pcb_path": "x", "sheets": [],
        "pcb_doc_paths": [str(pcb_file)],
    })
    monkeypatch.setattr(main_mod._altium, "_netlist", None)
    monkeypatch.setattr(main_mod, "_variant_state",
                        VariantState([VariantDefinition(name="Prod", dnp_refdes=[])]))
    monkeypatch.setattr(main_mod._pcb_session, "_load", lambda path, netlist: index)
    main_mod._pcb_session._index = None
    yield index
    main_mod._pcb_session._index = None


def test_tool_runs_in_mils_and_millimetres(tool_board):
    result = json.loads(main_mod.check_return_path(net="SIG,CLK_P", units="mil"))
    assert result["pcb_doc"] == "Board.PcbDoc" and result["units"] == "mil"
    assert result["summary"]["findings_by_check"]["segment_crosses_split"] == 1
    assert result["summary"]["findings_by_check"]["segment_near_plane_edge"] == 1
    text = main_mod.check_return_path(net="SIG", units="mm", stitch_radius=2)
    assert "_mil" not in text
    millimetres = json.loads(text)
    assert millimetres["parameters"]["stitch_radius_mm"] == 2.0
    place = _places(millimetres, "segment_crosses_split")[0]
    assert place["at"] == {"x_mm": 12.7, "y_mm": 12.7} and place["gap_mm"] == 0.508
    edge = json.loads(main_mod.check_return_path(net="CLK_P", edge_distance=0.1,
                                                 units="mm"))
    assert edge["parameters"]["edge_distance_mm"] == 0.1
    assert not _findings(edge, "segment_near_plane_edge")


@pytest.mark.parametrize("kwargs, error", [
    ({"units": "inch"}, "invalid_units"),
    ({"stitch_radius": 0}, "invalid_stitch_radius"),
    ({"edge_distance": -1}, "invalid_edge_distance"),
    ({"edge_distance": float("nan")}, "invalid_edge_distance"),
    ({"stitch_radius": 20000, "units": "mil"}, "invalid_stitch_radius"),
    ({"checks": ["nope"]}, "invalid_checks"),
    ({"net": "NOPE"}, "net_not_found"),
    ({"net_class": "Nope"}, "net_class_not_found"),
    ({"layer": "Nope"}, "layer_not_found"),
])
def test_tool_errors(tool_board, kwargs, error):
    assert json.loads(main_mod.check_return_path(**kwargs))["error"] == error


def test_tool_shrinks_its_page_to_fit(tool_board, monkeypatch):
    monkeypatch.setattr(main_mod, "QUERY_NET_MAX_RESULT_SIZE_CHARS", 6000)
    result = json.loads(main_mod.check_return_path())
    assert result["page_limited_to"] < 100
    assert len(json.dumps(result, indent=2)) <= 6000 or result["page_limited_to"] == 1


def test_tool_without_a_project():
    main_mod._pcb_session._index = None
    original = main_mod._project
    try:
        main_mod._project = None
        assert json.loads(main_mod.check_return_path())["error"] == "no_project"
    finally:
        main_mod._project = original


def test_tool_registration_manifest_readme_and_instructions():
    tools = {tool.name: tool for tool in asyncio.run(main_mod.mcp.list_tools())}
    tool = tools["check_return_path"]
    assert tool.annotations.readOnlyHint is True
    assert tool.title == "Check Return Path (Reference Planes)"
    assert tool.meta["anthropic/maxResultSizeChars"] == main_mod.QUERY_NET_MAX_RESULT_SIZE_CHARS
    assert set(tool.inputSchema["properties"]) == {
        "net", "net_class", "layer", "checks", "edge_distance", "stitch_radius", "units",
        "offset"}
    assert "likely" in tool.description and "next_offset" in tool.description
    assert "check_return_path" not in main_mod.NETLIST_TOOLS
    assert main_mod.SERVER_INSTRUCTIONS.index("check_return_path") > 4096
    manifest = json.loads((REPO / "manifest.json").read_text("utf-8"))
    assert "check_return_path" in {entry["name"] for entry in manifest["tools"]}
    assert "| `check_return_path` |" in (REPO / "README.md").read_text("utf-8")
    assert "`check_return_path`" in (REPO / "CHANGELOG.md").read_text("utf-8")
    from server.services.review_checklist import EVIDENCE_TOOLS
    assert "check_return_path" in EVIDENCE_TOOLS
    checklist = json.loads((REPO / "server" / "checklists" / "pcb.json").read_text("utf-8"))
    item = next(item for item in checklist["items"] if item["id"] == "PCB-HS-06")
    assert item["kind"] == "assisted" and item["evidence"]["tool"] == "check_return_path"


def test_places_next_to_an_assumed_hatched_pour_are_likely():
    board = Board()
    hatched = board.pour(GND, MID, _square(0, 0, 500, 1000), hatch="45Degree")
    board.track(GND, MID, 0, 0, 500, 1000, width=5.0, poured_by=hatched)
    board.track(SIG, BOTTOM, 100, 700, 900, 700)
    result = _run(board, nets=["SIG"])
    bottom = next(row for row in result["layers"] if row["layer"] == "Bottom Layer")
    assert [(ref["layer"], ref["kind"]) for ref in bottom["references"]] == [
        ("Mid Signal", "pour")]
    (finding,) = _findings(result, "segment_over_void")
    assert finding["severity"] == "warning" and finding["likely"] is True
    assert finding["likely_occurrences"] == 1
    (place,) = finding["occurrences"]
    assert place["cause"] == "no_copper" and place["length_mil"] == pytest.approx(400.0)
    assert finding["issue"].startswith("Likely: ")


def test_the_layer_filter_narrows_the_layer_changes():
    board = Board()
    board.pour_layer(GND, MID)
    _layer_change(board, SIG, 500, 500)
    assert _findings(_run(board, nets=["SIG"], layer="Bottom Layer"), "missing_stitching_via")
    assert _findings(_run(board, nets=["SIG"], layer="Mid Signal"), "missing_stitching_via")
    result = _run(board, nets=["SIG"], layer="Ground Plane", checks=["stitching"])
    assert _findings(result, "missing_stitching_via")
    board.track(SIG2, TOP, 100, 800, 900, 800)
    board.via(SIG2, 900, 800, layer=TOP, layer_to=MID)
    board.track(SIG2, MID, 900, 800, 900, 900)
    result = _run(board, nets=["SIG2"], checks=["stitching"])
    assert result["summary"]["layer_changes"] == {"total": 1, "shared_reference": 1}
