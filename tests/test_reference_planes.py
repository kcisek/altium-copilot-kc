"""Reference copper (services/reference_planes): reference layers per signal layer, internal
planes (nets, split plane polygons, split lines, voids, pullback, anti-pads, connections),
positive pours, and coverage intervals along a segment, on synthetic boards."""

import math

import pytest

from server.parsers.pcb_doc import (
    PAD_SHAPE_RECT,
    BareHole,
    BoardInfo,
    CopperArea,
    ImpedanceLayer,
    ImpedanceProfile,
    PcbData,
    PcbLayer,
    PcbRule,
    PolygonPour,
    Primitive,
    ShapeVertex,
    decode_board,
    shape_points,
)
from server.services import reference_planes as rp
from server.services.pcb_index import PcbIndex
from tests.test_connectivity import _fill_record
from tests.test_pcb_health import _fill, _region

NETS = ["GND", "PWR", "SIG", "SIG2"]
GND, PWR, SIG, SIG2 = range(4)
TOP, PLANE, MID, BOTTOM = 1, 39, 2, 32
MULTI = 74
# v7 ids of the stack's copper layers (signal 0x0100nnnn, planes 0x0101nnnn).
V7 = {TOP: 0x01000001, PLANE: 0x01010001, MID: 0x01000002, BOTTOM: 0x0100FFFF}
NO_LAYER = 0x01030000


def _square(x1, y1, x2, y2):
    return [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]


def _layers(plane=True):
    """Top / 5 mil / plane / 20 mil / Mid / 8 mil / Bottom (the plane left out on request)."""
    rows = [("Top Layer", TOP, "copper", None), ("Prepreg 1", None, "dielectric", 5.0)]
    if plane:
        rows += [("Ground Plane", PLANE, "copper", None), ("Core 1", None, "dielectric", 20.0)]
    rows += [("Mid Signal", MID, "copper", None), ("Prepreg 2", None, "dielectric", 8.0),
             ("Bottom Layer", BOTTOM, "copper", None)]
    return [
        PcbLayer(v6_id=v6, name=name, kind=kind, stack_order=order,
                 copper_thick_mil=1.4 if kind == "copper" else None,
                 diel_height_mil=height, v7_id=V7.get(v6))
        for order, (name, v6, kind, height) in enumerate(rows)
    ]


class Board:
    """A 1000 x 1000 mil board built object by object."""

    def __init__(self, plane=True, plane_net="GND", pullback=(10.0, "stack"),
                 outline=_square(0, 0, 1000, 1000)):
        self.plane = plane
        self.plane_net = plane_net
        self.pullback = pullback
        self.outline = [ShapeVertex(x, y) for x, y in outline] if outline else []
        self.prims: list[Primitive] = []
        self.pours: list[PolygonPour] = []
        self.areas: list[CopperArea] = []
        self.rules: list[PcbRule] = []
        self.regions = b""  # ShapeBasedRegions6
        self.fills = b""
        self.bare_holes: list[BareHole] = []
        self.profiles: list[ImpedanceProfile] = []
        self.area_streams = ("Regions6", "Fills6")

    def track(self, net, layer, x1, y1, x2, y2, width=10.0, poured_by=None):
        self.prims.append(Primitive(
            kind="track", layer=layer, net=net, component=None, x1=x1, y1=y1, x2=x2,
            y2=y2, width=width, poured_by=poured_by,
        ))

    def via(self, net, x, y, diameter=20.0, hole=10.0, layer=TOP, layer_to=BOTTOM):
        self.prims.append(Primitive(
            kind="via", layer=layer, layer_to=layer_to, net=net, component=None,
            x1=x, y1=y, x2=x, y2=y, width=diameter, hole=hole,
        ))

    def th_pad(self, net, x, y, size=60.0, hole=30.0, shape=PAD_SHAPE_RECT):
        self.prims.append(Primitive(
            kind="pad", layer=MULTI, net=net, component=None, x1=x, y1=y, x2=x, y2=y,
            width=size, height=size, shape=shape, hole=hole, pad_name="1", plated=True,
        ))

    def split_plane(self, net, vertices, shelved=False):
        self.pours.append(PolygonPour(
            pour_id=len(self.pours), net=net, layer=PLANE, hatch_style=None,
            vertices=list(vertices), shelved=shelved, polygon_type="Split Plane",
        ))

    def pour(self, net, layer, vertices, hatch="Solid", shelved=False, grid=None,
             track_width=None):
        self.pours.append(PolygonPour(
            pour_id=len(self.pours), net=net, layer=layer, hatch_style=hatch,
            vertices=list(vertices), shelved=shelved, polygon_type="Polygon",
            grid_mil=grid, track_width_mil=track_width,
        ))
        return len(self.pours) - 1

    def area(self, net, layer, outline, holes=(), pour_id=None, kind="region"):
        self.areas.append(CopperArea(
            kind=kind, layer=layer, net=net, component=None, outline=list(outline),
            holes=[list(hole) for hole in holes], pour_id=pour_id,
        ))

    def rule(self, kind, scope="All", **properties):
        self.rules.append(PcbRule(
            name=f"{kind}_{len(self.rules)}", kind=kind, kind_id=0, enabled=True,
            priority=len(self.rules) + 1, scope1=scope, scope2="All", net_scope="AnyNet",
            comment="", unique_id=None, from_schematic=False, properties=properties,
        ))

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
        if self.fills:
            raw["Fills6/Data"] = self.fills
        data = PcbData(
            board=board, nets=list(NETS), components=[], primitives=list(self.prims),
            pours=list(self.pours),
            layer_name_to_v6={"top layer": TOP, "ground plane": PLANE, "mid signal": MID,
                              "bottom layer": BOTTOM},
            rules=list(self.rules), raw=raw, areas=list(self.areas),
            area_streams=tuple(self.area_streams), bare_holes=list(self.bare_holes),
        )
        return PcbIndex(data, None)


_ALIVE: list[PcbIndex] = []  # the model holds its index weakly


def _model(board: Board) -> rp.ReferencePlanes:
    index = board.index()
    _ALIVE.append(index)
    return rp.reference_planes(index)


def _split_board(**kwargs) -> Board:
    """A plane split down the middle at x = 500 (a 20 mil split line), GND left and PWR
    right, set back 10 mil from the edge as Altium pours them."""
    board = Board(**kwargs)
    board.track(None, PLANE, 500, 0, 500, 1000, width=20.0)
    board.split_plane(GND, _square(10, 10, 490, 990))
    board.split_plane(PWR, _square(510, 10, 990, 990))
    return board


def _close(coverage, expected, tolerance=1e-6):
    assert [piece[0] for piece in coverage] == [piece[0] for piece in expected]
    for got, want in zip(coverage, expected):
        assert got[1] == pytest.approx(want[1], abs=tolerance)
        assert got[2] == pytest.approx(want[2], abs=tolerance)


# ---------- parser: plane pullback ----------


def test_plane_pullback_is_read_from_the_stack_then_the_plane_keys_then_the_cache():
    stack = {
        "V9_STACK_LAYER0_NAME": "Top", "V9_STACK_LAYER0_LAYERID": str(V7[TOP]),
        "V9_STACK_LAYER0_COPTHICK": "1.4mil",
        "V9_STACK_LAYER1_NAME": "Core", "V9_STACK_LAYER1_DIELHEIGHT": "10mil",
        "V9_STACK_LAYER2_NAME": "Plane A", "V9_STACK_LAYER2_LAYERID": str(V7[PLANE]),
        "V9_STACK_LAYER2_COPTHICK": "1.4mil", "V9_STACK_LAYER2_PULLBACKDISTANCE": "15mil",
        "V9_STACK_LAYER3_NAME": "Plane B", "V9_STACK_LAYER3_LAYERID": str(0x01010002),
        "V9_STACK_LAYER3_COPTHICK": "1.4mil",
        "PLANE1PULLBACK": "30mil", "PLANE2PULLBACK": "0.5mm", "PLANE3PULLBACK": "bad",
        "V9_CACHE_LAYER7_LAYERID": str(0x01010003),
        "V9_CACHE_LAYER7_PULLBACKDISTANCE": "12mil",
        "V9_CACHE_LAYER8_LAYERID": str(0x01010001),
        "V9_CACHE_LAYER8_PULLBACKDISTANCE": "99mil",
        "V9_CACHE_LAYER9_LAYERID": str(0x01020001),  # a mechanical layer: not a plane
        "V9_CACHE_LAYER9_PULLBACKDISTANCE": "5mil",
        "PLANE1NETNAME": "GND", "PLANE2NETNAME": "(No Net)",
    }
    board = decode_board([stack])
    assert board.plane_pullbacks[39] == (15.0, "stack")
    assert board.plane_pullbacks[40] == (pytest.approx(0.5 / 0.0254), "plane")
    assert board.plane_pullbacks[41] == (12.0, "cache")
    assert set(board.plane_pullbacks) == {39, 40, 41}
    assert board.plane_nets == {39: "GND"}
    assert decode_board([{}]).plane_pullbacks == {}


# ---------- internal planes ----------


def test_plane_net_covers_the_board_less_the_pullback():
    model = _model(Board())
    assert model.planes() == [PLANE]
    assert model.copper_at(PLANE, 500, 500) == "GND"
    assert model.copper_at("Ground Plane", 15, 500) == "GND"
    assert model.copper_at(PLANE, 5, 500) is None  # within the 10 mil pullback
    assert model.copper_at(PLANE, -20, 500) is None  # off the board
    summary = model.plane_partition(PLANE)
    assert summary["net"] == "GND" and summary["net_on_board"] is True
    assert summary["pullback_mil"] == 10.0 and summary["pullback_basis"] == "stack"
    assert summary["regions"] == [
        {"net": "GND", "basis": "whole plane (no split plane polygons saved)"}]
    assert summary["nets"] == ["GND"] and summary["split_among_nets"] is False


def test_default_pullback_and_a_plane_without_a_net():
    model = _model(Board(plane_net=None, pullback=None))
    assert model.copper_at(PLANE, 15, 500) is None  # inside the default 20 mil
    assert model.copper_at(PLANE, 25, 500) == rp.NO_NET
    summary = model.plane_partition(PLANE)
    assert summary["pullback_mil"] == rp.DEFAULT_PULLBACK_MIL
    assert summary["pullback_basis"] == "default"
    assert summary["net"] is None and summary["nets"] == [rp.NO_NET]


def test_split_line_and_split_plane_polygons_make_two_regions():
    model = _model(_split_board())
    assert model.copper_at(PLANE, 250, 500) == "GND"
    assert model.copper_at(PLANE, 750, 500) == "PWR"
    assert model.copper_at(PLANE, 500, 500) is None  # the split line
    assert model.copper_at(PLANE, 495, 500) is None
    summary = model.plane_partition(PLANE)
    assert summary["nets"] == ["GND", "PWR"] and summary["split_among_nets"] is True
    assert [(region["net"], region["basis"]) for region in summary["regions"]] == [
        ("GND", "split plane polygon"), ("PWR", "split plane polygon")]
    assert summary["regions"][0]["area_mm2"] == pytest.approx(480 * 980 * 0.0254 ** 2, abs=0.01)
    assert summary["split_lines"] == 1 and summary["boundary_lines"] == 0


def test_without_split_plane_polygons_the_split_line_is_a_void_in_the_plane_net():
    board = Board()
    board.track(None, PLANE, 500, 0, 500, 1000, width=20.0)
    model = _model(board)
    assert model.copper_at(PLANE, 250, 500) == "GND"
    assert model.copper_at(PLANE, 750, 500) == "GND"
    assert model.copper_at(PLANE, 500, 500) is None


def test_split_plane_polygons_rule_the_plane_and_the_innermost_wins():
    board = Board()
    board.split_plane(GND, _square(10, 10, 990, 990))
    board.split_plane(PWR, _square(400, 400, 600, 600))  # an island inside GND
    board.split_plane(SIG, _square(700, 700, 800, 800), shelved=True)
    model = _model(board)
    assert model.copper_at(PLANE, 500, 500) == "PWR"
    assert model.copper_at(PLANE, 300, 300) == "GND"
    assert model.copper_at(PLANE, 750, 750) == "GND"  # the shelved one is not copper
    summary = model.plane_partition(PLANE)
    assert summary["split_planes_shelved"] == 1
    board = Board()
    board.split_plane(PWR, _square(10, 10, 490, 990))
    model = _model(board)
    assert model.copper_at(PLANE, 750, 500) is None  # outside every split plane polygon


def test_boundary_lines_along_the_edge_are_told_from_split_lines():
    board = _split_board()
    for x1, y1, x2, y2 in ((0, 0, 1000, 0), (1000, 0, 1000, 1000), (1000, 1000, 0, 1000),
                           (0, 1000, 0, 0)):
        board.track(None, PLANE, x1, y1, x2, y2, width=20.0)  # twice the pullback
    summary = _model(board).plane_partition(PLANE)
    assert summary["boundary_lines"] == 4 and summary["split_lines"] == 1


def test_regions_and_fills_on_a_plane_are_voids_but_keepouts_are_not():
    board = Board()
    board.regions = (
        _region(PLANE, _square(300, 400, 400, 500))
        + _region(PLANE, _square(600, 100, 700, 200), keepout=True)
        + _region(TOP, _square(300, 600, 400, 700))  # a signal layer's: not the plane's
    )
    board.fills = (_fill(PLANE, 600, 600, 700, 650)
                   + _fill_record(PLANE, 0xFFFF, 100, 800, 200, 900, flags2=2))
    model = _model(board)
    assert model.copper_at(PLANE, 350, 450) is None
    assert model.copper_at(PLANE, 450, 450) == "GND"
    assert model.copper_at(PLANE, 650, 150) == "GND"  # keepout region
    assert model.copper_at(PLANE, 350, 650) == "GND"
    assert model.copper_at(PLANE, 650, 625) is None
    assert model.copper_at(PLANE, 150, 850) == "GND"  # keepout fill
    summary = model.plane_partition(PLANE)
    assert summary["voids"] == {"regions": 1, "fills": 1}


def test_board_cutouts_take_the_plane_and_its_pullback():
    board = Board()
    board.regions = _region(MULTI, _square(400, 400, 600, 600), kind=3)
    model = _model(board)
    assert model.copper_at(PLANE, 500, 500) is None
    assert model.copper_at(PLANE, 605, 500) is None  # within 10 mil of the cutout
    assert model.copper_at(PLANE, 615, 500) == "GND"


def test_anti_pads_around_holes_not_connected_to_the_region():
    board = _split_board()
    board.rule("PlaneClearance", CLEARANCE="15mil")
    board.via(SIG, 250, 250)  # another net: anti-pad of 10 + 15 mil
    board.via(GND, 250, 750)  # GND in the GND region: connected
    board.via(GND, 750, 750)  # GND in the PWR region: anti-pad
    board.via(SIG, 250, 600, layer=MID, layer_to=BOTTOM)  # blind below the plane
    board.th_pad(SIG2, 750, 250)  # a 60 mil square pad: its copper grown by 15 mil
    model = _model(board)
    assert model.copper_at(PLANE, 250 + 24, 250) is None
    assert model.copper_at(PLANE, 250 + 26, 250) == "GND"
    assert model.copper_at(PLANE, 250, 750) == "GND"
    assert model.copper_at(PLANE, 750 + 24, 750) is None
    assert model.copper_at(PLANE, 750 + 26, 750) == "PWR"
    assert model.copper_at(PLANE, 250, 600) == "GND"  # the blind via does not cross it
    assert model.copper_at(PLANE, 750 + 40, 250 + 40) is None  # inside the grown square
    assert model.copper_at(PLANE, 750 + 44, 250 + 44) == "PWR"  # its rounded corner
    assert model.copper_at(PLANE, 750 + 30 + 16, 250) == "PWR"
    summary = model.plane_partition(PLANE)
    assert summary["connections"] == {"unknown": 1}  # no Power Plane Connect Style rule
    assert summary["anti_pads"] == {"pad": 1, "via": 2}
    assert summary["anti_pad_clearances"] == [
        {"clearance_mil": 15.0, "basis": "rule", "count": 3}]


def test_default_clearance_bare_holes_and_connect_styles():
    board = Board()
    board.rule("PlaneConnect", PLANECONNECTSTYLE="Relief", RELIEFAIRGAP="10mil",
               RELIEFCONDUCTORWIDTH="8mil", RELIEFEXPANSION="20mil", RELIEFENTRIES="4")
    board.via(GND, 300, 300)
    board.bare_holes.append(BareHole(x=700, y=700, hole=40.0, layer=MULTI, net=None,
                                     component=None, pad_name="M1", plated=False))
    model = _model(board)
    assert model.copper_at(PLANE, 300, 300) == "GND"  # relief: connected
    assert model.copper_at(PLANE, 700 + 39, 700) is None  # 20 + 20 mil default
    assert model.copper_at(PLANE, 700 + 41, 700) == "GND"
    summary = model.plane_partition(PLANE)
    assert summary["connections"] == {"relief": 1}
    assert summary["anti_pads"] == {"bare_hole": 1}
    assert summary["anti_pad_clearances"] == [
        {"clearance_mil": rp.DEFAULT_PLANE_CLEARANCE_MIL, "basis": "default", "count": 1}]
    assert summary["thermal_reliefs"] == [{"air_gap_mil": 10.0, "conductor_width_mil": 8.0,
                                           "expansion_mil": 20.0, "entries": "4"}]


def test_no_connect_style_gives_a_same_net_hole_an_anti_pad():
    board = Board()
    board.rule("PlaneConnect", **{"VIA.PLANECONNECTSTYLE": "No Connect",
                                  "PAD.PLANECONNECTSTYLE": "Direct"})
    board.via(GND, 300, 300)
    board.th_pad(GND, 700, 700, size=40.0, hole=20.0)
    model = _model(board)
    assert model.copper_at(PLANE, 300 + 25, 300) is None
    assert model.copper_at(PLANE, 700 + 25, 700) == "GND"
    summary = model.plane_partition(PLANE)
    assert summary["connections"] == {"direct": 1}
    assert summary["anti_pads"] == {"via": 1}


# ---------- positive layers ----------


def test_signal_layer_copper_from_pours_regions_fills_and_routing():
    board = Board()
    pour = board.pour(GND, BOTTOM, _square(0, 0, 1000, 500))
    board.area(GND, BOTTOM, _square(10, 10, 990, 490), holes=[_square(400, 200, 600, 300)],
               pour_id=pour)
    board.area(None, BOTTOM, _square(100, 600, 200, 700))  # a free region without a net
    board.area(PWR, BOTTOM, _square(300, 600, 400, 700), kind="fill")
    board.track(SIG, BOTTOM, 450, 250, 550, 250, width=10.0)  # in the pour's hole
    board.via(SIG2, 800, 800)
    model = _model(board)
    assert model.copper_at(BOTTOM, 100, 100) == "GND"
    assert model.copper_at(BOTTOM, 420, 250) is None  # the pour's clearance hole
    assert model.copper_at(BOTTOM, 500, 250) == "SIG"
    assert model.copper_at(BOTTOM, 150, 650) == rp.NO_NET
    assert model.copper_at(BOTTOM, 350, 650) == "PWR"
    assert model.copper_at(BOTTOM, 800, 805) == "SIG2"  # the via's pad
    assert model.copper_at(BOTTOM, 800, 950) is None
    summary = model.plane_partition(BOTTOM)
    assert summary["type"] == "signal" and summary["kind"] == "pour"
    assert summary["largest_pour"]["net"] == "GND"
    assert summary["largest_pour"]["share"] == pytest.approx(
        (980 * 480 - 200 * 100) / 1e6, abs=1e-3)
    assert summary["sources"] == {"fills": 1, "free_regions": 1, "pour_regions": 1}
    assert summary["board_area_basis"] == "outline"


def test_hatched_pours_count_as_their_outline_and_shelved_pours_as_nothing():
    board = Board()
    hatched = board.pour(GND, BOTTOM, _square(0, 0, 500, 500), hatch="45Degree")
    board.track(GND, BOTTOM, 0, 0, 500, 500, width=5.0, poured_by=hatched)
    border = board.pour(PWR, BOTTOM, _square(600, 0, 900, 300), hatch="None")
    board.track(PWR, BOTTOM, 600, 0, 900, 0, width=5.0, poured_by=border)
    board.pour(SIG, BOTTOM, _square(600, 600, 900, 900), shelved=True)
    board.pour(SIG2, MID, _square(0, 0, 1000, 1000))  # never poured: no copper
    model = _model(board)
    assert model.copper_at(BOTTOM, 400, 100) == "GND"  # off the hatch track, in the outline
    assert model.copper_at(BOTTOM, 750, 0) == "PWR"  # the outline-only pour's border
    assert model.copper_at(BOTTOM, 750, 150) is None
    assert model.copper_at(BOTTOM, 750, 750) is None
    assert model.copper_at(MID, 500, 500) is None
    summary = model.plane_partition(BOTTOM)
    assert summary["sources"] == {"hatched_pour_outlines": 1, "outline_only_pours": 1,
                                  "shelved_pours": 1}
    assert model.plane_partition(MID)["sources"] == {"pours_without_copper": 1}


def _hatch(board: Board, net, layer, x1, y1, x2, y2, grid, width, skip=()):
    """A horizontal hatch over a box: rows of tracks grid apart, the rows in skip left out
    (a channel the hatch did not fill)."""
    pour = board.pour(net, layer, _square(x1, y1, x2, y2), hatch="Horizontal", grid=grid,
                      track_width=width)
    row, y = 0, y1 + width / 2.0
    while y <= y2 - width / 2.0 + 1e-9:
        if row not in skip:
            board.track(net, layer, x1 + width / 2.0, y, x2 - width / 2.0, y, width=width,
                        poured_by=pour)
        row, y = row + 1, y + grid
    return pour


def test_a_hatched_pour_is_its_hatch_with_the_openings_of_its_grid_bridged():
    # A hatch as wide as its grid is solid: its rows are its copper, exactly; the rows it
    # leaves out (a channel) are no copper although they lie in its outline.
    board = Board()
    _hatch(board, GND, BOTTOM, 0, 0, 1000, 400, grid=4.0, width=4.0, skip=range(40, 50))
    # A hatch with openings: 4 mil tracks on a 20 mil grid, read as one conductor.
    _hatch(board, PWR, MID, 0, 0, 1000, 400, grid=20.0, width=4.0, skip=(8, 9, 10))
    model = _model(board)
    assert model.copper_at(BOTTOM, 500, 100) == "GND"
    assert model.copper_source(BOTTOM, 500, 100) == "hatched_pour"
    assert model.copper_at(BOTTOM, 500, 180) is None  # rows 40-49: y 160..200
    assert model.copper_at(BOTTOM, 500, 202.0) == "GND"
    assert model.copper_at(MID, 500, 10.0) == "PWR"  # in an opening: bridged
    assert model.copper_source(MID, 500, 10.0) == "hatched_pour_bridged"
    assert model.copper_source(MID, 500, 2.0) == "hatched_pour"  # on a track
    # Rows 8-10 left out: no copper from 8 mil past row 7 (y 144) to 8 mil before row 11.
    assert model.copper_at(MID, 500, 190.0) is None
    assert model.copper_at(MID, 500, 500.0) is None  # outside the outline
    coverage = model.segment_coverage(MID, (500, 100, 500, 300))
    assert [piece[0] for piece in coverage] == ["PWR", None, "PWR"]
    assert coverage[1][1] * 200 + 100 == pytest.approx(152.05, abs=1e-6)
    assert coverage[1][2] * 200 + 100 == pytest.approx(211.95, abs=1e-6)
    coverage = model.segment_coverage(BOTTOM, (500, 100, 500, 300))
    assert [piece[0] for piece in coverage] == ["GND", None, "GND"]
    assert coverage[1][1] * 200 + 100 == pytest.approx(160.05, abs=1e-6)
    assert coverage[1][2] * 200 + 100 == pytest.approx(199.95, abs=1e-6)
    summary = model.plane_partition(MID)
    assert summary["sources"] == {"hatched_pours": 1}
    assert summary["copper"][0]["net"] == "PWR"


def test_solid_pour_outline_stands_in_when_regions_were_not_read():
    board = Board()
    board.pour(GND, BOTTOM, _square(0, 0, 500, 500))
    board.area_streams = ("Fills6",)
    model = _model(board)
    assert model.copper_at(BOTTOM, 250, 250) == "GND"
    assert model.plane_partition(BOTTOM)["sources"] == {"pour_outlines_assumed": 1}


# ---------- coverage along a segment ----------


def test_coverage_across_a_split_line():
    model = _model(_split_board())
    coverage = model.segment_coverage(PLANE, (100, 500, 900, 500))
    _close(coverage, [("GND", 0.0, 390 / 800), (None, 390 / 800, 410 / 800),
                      ("PWR", 410 / 800, 1.0)])
    reverse = model.segment_coverage(PLANE, (900, 500, 100, 500))
    assert [piece[0] for piece in reverse] == ["PWR", None, "GND"]


def test_coverage_through_a_void_an_anti_pad_and_off_the_board():
    board = Board()
    board.regions = _region(PLANE, _square(300, 400, 400, 500))
    board.via(SIG, 600, 450)  # anti-pad radius 10 + 20 mil
    model = _model(board)
    coverage = model.segment_coverage(PLANE, (200, 450, 800, 450))
    _close(coverage, [("GND", 0, 100 / 600), (None, 100 / 600, 200 / 600),
                      ("GND", 200 / 600, 370 / 600), (None, 370 / 600, 430 / 600),
                      ("GND", 430 / 600, 1.0)])
    edge = model.segment_coverage(PLANE, (-100, 450, 100, 450))
    _close(edge, [(None, 0, 110 / 200), ("GND", 110 / 200, 1.0)])


def test_coverage_on_a_diagonal_through_a_round_anti_pad_is_exact():
    board = Board()
    board.via(SIG, 500, 500, diameter=30.0)  # anti-pad radius 35
    model = _model(board)
    coverage = model.segment_coverage(PLANE, (400, 400, 600, 600))
    half = 35 / math.hypot(200, 200)
    _close(coverage, [("GND", 0, 0.5 - half), (None, 0.5 - half, 0.5 + half),
                      ("GND", 0.5 + half, 1.0)], tolerance=1e-9)


def test_an_arc_split_line_is_a_void_along_its_arc():
    board = Board()
    # A half circle of radius 200 about (500, 500), 10 mil wide, as two chord rows.
    for start, end in ((0.0, 90.0), (90.0, 180.0)):
        x1, y1 = 500 + 200 * math.cos(math.radians(start)), 500 + 200 * math.sin(math.radians(start))
        x2, y2 = 500 + 200 * math.cos(math.radians(end)), 500 + 200 * math.sin(math.radians(end))
        board.prims.append(Primitive(
            kind="arc", layer=PLANE, net=None, component=None, x1=x1, y1=y1, x2=x2, y2=y2,
            width=10.0, source_id=0, arc_x=500.0, arc_y=500.0, arc_radius=200.0,
        ))
    model = _model(board)
    top = (500 + 200 * math.cos(math.radians(45)), 500 + 200 * math.sin(math.radians(45)))
    assert model.copper_at(PLANE, *top) is None  # on the arc, off its chord's middle
    assert model.copper_at(PLANE, 500, 500) == "GND"
    assert model.copper_at(PLANE, 500, 300) == "GND"  # the open side
    _close(model.segment_coverage(PLANE, (500, 600, 500, 800)),
           [("GND", 0.0, 0.475), (None, 0.475, 0.525), ("GND", 0.525, 1.0)], tolerance=1e-9)
    assert model.plane_partition(PLANE)["split_lines"] == 1  # one arc


def test_coverage_on_a_signal_layer_crosses_pour_holes_and_tracks():
    board = Board()
    pour = board.pour(GND, BOTTOM, _square(0, 0, 1000, 500))
    board.area(GND, BOTTOM, _square(10, 10, 990, 490), holes=[_square(400, 200, 600, 300)],
               pour_id=pour)
    board.track(SIG, BOTTOM, 500, 210, 500, 290, width=20.0)
    model = _model(board)
    coverage = model.segment_coverage(BOTTOM, (300, 250, 700, 250))
    _close(coverage, [("GND", 0, 0.25), (None, 0.25, 190 / 400), ("SIG", 190 / 400, 210 / 400),
                      (None, 210 / 400, 0.75), ("GND", 0.75, 1.0)])
    assert model.segment_coverage(BOTTOM, (100, 100, 100, 100)) == [("GND", 0.0, 1.0)]
    assert model.segment_coverage(BOTTOM, (100, 100, 200, 100)) == [("GND", 0.0, 1.0)]


def test_coverage_agrees_with_point_queries_everywhere():
    board = _split_board()
    board.rule("PlaneClearance", CLEARANCE="12mil")
    board.regions = _region(PLANE, [(200, 200), (300, 220), (260, 320)])
    for number in range(8):
        board.via(SIG if number % 2 else GND, 120 + number * 100, 130 + number * 90)
    model = _model(board)
    for segment in ((50, 60, 950, 940), (980, 20, 30, 970), (100, 700, 900, 120)):
        coverage = model.segment_coverage(PLANE, segment)
        assert coverage[0][1] == 0.0 and coverage[-1][2] == 1.0
        assert all(a[2] == b[1] and a[0] != b[0] for a, b in zip(coverage, coverage[1:]))
        for label, t0, t1 in coverage:
            for share in (0.01, 0.5, 0.99):
                t = t0 + (t1 - t0) * share
                x = segment[0] + (segment[2] - segment[0]) * t
                y = segment[1] + (segment[3] - segment[1]) * t
                assert model.copper_at(PLANE, x, y) == label


# ---------- reference layers ----------


def test_reference_layers_from_the_stack_neighbours():
    board = Board()
    pour = board.pour(GND, BOTTOM, _square(0, 0, 1000, 1000))
    board.area(GND, BOTTOM, _square(10, 10, 990, 990), pour_id=pour)
    board.area(PWR, MID, _square(10, 10, 100, 100))  # a small one: not a large pour
    entries = {entry["layer"]: entry for entry in rp.reference_layers(board.index())}
    assert list(entries) == ["Top Layer", "Mid Signal", "Bottom Layer"]
    top = entries["Top Layer"]
    assert top["basis"] == "stackup_neighbours" and top["above"] is None
    assert top["below"] == {"layer": "Ground Plane", "side": "below", "dielectric_mil": 5.0,
                            "kind": "plane", "nets": ["GND"]}
    assert top["references"] == [top["below"]] and top["has_reference_copper"] is True
    mid = entries["Mid Signal"]
    assert [(ref["layer"], ref["kind"], ref["dielectric_mil"]) for ref in mid["references"]] == [
        ("Ground Plane", "plane", 20.0), ("Bottom Layer", "pour", 8.0)]
    assert mid["below"]["nets"] == ["GND"]
    assert mid["below"]["largest_pour"]["share"] == pytest.approx(0.9604, abs=1e-3)
    bottom = entries["Bottom Layer"]
    assert bottom["above"]["kind"] == "signal" and bottom["has_reference_copper"] is False
    assert bottom["above"]["largest_pour"] == {"net": "PWR", "share": 0.008}


def test_reference_layers_from_impedance_profiles():
    board = Board()
    entry = dict(substack_id=None, width_mil=5.0, min_width_mil=None, max_width_mil=None,
                 gap_mil=None, min_gap_mil=None, max_gap_mil=None, calculated_ohm=50.0,
                 error_pct=None, propagation_speed=None)
    board.profiles = [
        ImpedanceProfile(id="{S50}", name="S50", display_name=None, target_ohm=50.0,
                         tolerance_pct=10.0, differential=False, layers=[
                             ImpedanceLayer(layer_v7=V7[BOTTOM], ref_top_v7=V7[PLANE],
                                            ref_bottom_v7=NO_LAYER, enabled=True, **entry),
                             ImpedanceLayer(layer_v7=V7[TOP], ref_top_v7=NO_LAYER,
                                            ref_bottom_v7=V7[MID], enabled=False, **entry),
                         ]),
    ]
    entries = {entry["layer"]: entry for entry in rp.reference_layers(board.index())}
    bottom = entries["Bottom Layer"]
    assert bottom["basis"] == "impedance_profile"
    assert bottom["profiles"] == [{"profile": "S50", "above": "Ground Plane", "below": None}]
    assert bottom["references"] == [{
        "layer": "Ground Plane", "side": "above", "dielectric_mil": 28.0,
        "copper_layers_between": 1, "kind": "plane", "nets": ["GND"]}]
    assert entries["Top Layer"]["basis"] == "stackup_neighbours"  # its entry is disabled


def test_a_split_plane_reference_lists_its_nets_and_legacy_stacks_have_no_dielectric():
    model = _model(_split_board())
    top = model.reference_layers()[0]
    assert top["below"]["nets"] == ["GND", "PWR"]
    board = Board(plane=False)
    index = board.index()
    for layer in index.pcb.board.layers:
        layer.diel_height_mil = None
    index = PcbIndex(index.pcb, None)
    entries = rp.reference_layers(index)
    assert entries[0]["below"]["layer"] == "Mid Signal"
    assert entries[0]["below"]["dielectric_mil"] is None


def test_layers_outside_the_stack_are_refused():
    model = _model(Board())
    with pytest.raises(ValueError):
        model.copper_at(40, 0, 0)
    with pytest.raises(ValueError):
        model.plane_partition("no such layer")
    assert rp.copper_at(model.index, "Top Layer", 500, 500) is None
    assert rp.plane_partition(model.index, TOP)["kind"] == "signal"


# ---------- why a point has no copper, what the copper is ----------


def test_void_causes_sources_area_nets_and_holes_near():
    board = _split_board()
    board.via(SIG, 250, 250)
    board.regions = _region(PLANE, _square(300, 600, 400, 700))
    hatched = board.pour(GND, BOTTOM, _square(0, 0, 1000, 500), hatch="45Degree")
    board.track(GND, BOTTOM, 0, 0, 500, 500, width=5.0, poured_by=hatched)
    board.area(PWR, BOTTOM, _square(600, 600, 700, 700), kind="fill")
    board.area(None, BOTTOM, _square(800, 600, 900, 700))
    model = _model(board)
    assert model.void_cause(PLANE, 250, 500) is None
    assert model.void_cause(PLANE, 500, 500) == {"cause": "outside_split_planes"}
    assert model.void_cause(PLANE, -5, 500) == {"cause": "off_board"}
    assert model.void_cause(PLANE, 5, 500) == {"cause": "outside_split_planes"}
    assert model.void_cause(PLANE, 350, 650) == {"cause": "drawn_void"}
    anti_pad = model.void_cause(PLANE, 275, 250)
    assert anti_pad["cause"] == "anti_pad" and anti_pad["approximate"] is True
    assert anti_pad["hole"]["net"] == "SIG" and anti_pad["hole"]["kind"] == "via"
    assert model.copper_source(PLANE, 250, 500) == "split_plane_polygon"
    assert model.copper_source(PLANE, 500, 500) is None
    assert model.copper_source(BOTTOM, 400, 100) == "hatched_pour_outlines"
    assert model.copper_source(BOTTOM, 250, 250) == "routing"  # the via's pad
    assert model.copper_source(BOTTOM, 650, 650) == "area"
    assert model.void_cause(BOTTOM, 650, 650) is None
    assert model.void_cause(BOTTOM, 850, 650) == {"cause": "no_net_copper"}
    assert model.void_cause(BOTTOM, 850, 850) == {"cause": "no_copper"}
    assert model.area_nets(PLANE) == {"GND", "PWR"}
    assert model.area_nets("Bottom Layer") == {"GND", "PWR"}
    assert model.area_nets(MID) == set()
    (via,) = model.holes_near(250, 250, 5.0)
    assert via["net"] == "SIG" and via["row"] is not None and via["extent"] == 10.0
    assert model.holes_near(250, 290, 5.0) == []
    assert model.holes_near(250, 290, 31.0) == [via]


def test_a_plane_partitioned_only_by_split_lines_is_an_assumed_source():
    board = Board()
    assert _model(board).copper_source(PLANE, 500, 500) == "whole_plane"
    board.track(None, PLANE, 500, 0, 500, 1000, width=20.0)
    model = _model(board)
    assert model.copper_source(PLANE, 250, 500) == "whole_plane_split_lines_unpartitioned"
    assert model.void_cause(PLANE, 500, 500) == {"cause": "split_line"}
    board = Board()
    board.track(None, PLANE, 0, 0, 1000, 0, width=30.0)  # a boundary line on the edge
    model = _model(board)
    assert model.void_cause(PLANE, 500, 12) == {"cause": "boundary_line"}
    assert model.void_cause(PLANE, 5, 500) == {"cause": "pullback", "approximate": False}
    assert _model(Board(pullback=None)).void_cause(PLANE, 5, 500) == {
        "cause": "pullback", "approximate": True}
