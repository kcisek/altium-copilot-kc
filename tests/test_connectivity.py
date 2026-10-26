"""Copper connectivity (check_connectivity): unrouted connections, isolated copper,
antennae and weak joins on synthetic boards, the area copper they read (Regions6,
Fills6, planes) and the MCP tool."""

import json
import struct
from pathlib import Path

import pytest

import server.main as main_mod
from server.parsers.drc_report import RULE, DrcReport, DrcTable, DrcViolation
from server.parsers.prj_pcb import VariantState
from server.parsers.pcb_doc import (
    PAD_SHAPE_RECT,
    BoardInfo,
    CopperArea,
    PcbClass,
    PcbComponent,
    PcbData,
    PcbLayer,
    PcbRule,
    PolygonPour,
    Primitive,
    _build_layer_aliases,
    decode_board,
    decode_fill_records,
    decode_fills,
    decode_polygons,
    decode_region_records,
    decode_regions,
    parse_streams,
)
from server.services.connectivity import (
    APPROXIMATIONS,
    CHECKS,
    DEFAULT_CHECKS,
    NEAR_MIL,
    check_connectivity,
    connectivity,
    drc_comparison,
    page_result,
)
from server.services.pcb_index import PcbIndex

MULTI = 74
PLANE = 39
COMPONENTS = ["U1", "U2", "U3", "U4", "C1", "C2"]
C = {name: number for number, name in enumerate(COMPONENTS)}


# ---------- synthetic boards ----------


def _board(plane: bool = False, plane_nets: dict | None = None) -> BoardInfo:
    layers = [
        PcbLayer(v6_id=1, name="Top Layer", kind="copper", stack_order=0,
                 copper_thick_mil=1.4),
        PcbLayer(v6_id=None, name="Core", kind="dielectric", stack_order=1,
                 diel_height_mil=20.0),
    ]
    if plane:
        layers += [
            PcbLayer(v6_id=PLANE, name="GND Plane", kind="copper", stack_order=2,
                     copper_thick_mil=1.4),
            PcbLayer(v6_id=None, name="Core 2", kind="dielectric", stack_order=3,
                     diel_height_mil=20.0),
        ]
    layers.append(
        PcbLayer(v6_id=32, name="Bottom Layer", kind="copper", stack_order=len(layers),
                 copper_thick_mil=1.4)
    )
    return BoardInfo(
        origin_x=0.0, origin_y=0.0, sheet_x=0.0, sheet_y=0.0, sheet_width=5000.0,
        sheet_height=5000.0, display_unit="mil", layers=layers, stackup_source="v9",
        plane_nets=plane_nets or {},
    )


class Board:
    """Build a PcbData net by net."""

    def __init__(self, nets):
        self.nets = list(nets)
        self.prims: list[Primitive] = []
        self.pours: list[PolygonPour] = []
        self.areas: list[CopperArea] = []
        self.rules: list[PcbRule] = []
        self.classes: list[PcbClass] = []

    def net(self, name):
        return self.nets.index(name)

    def pad(self, net, component, name, x, y, size=10.0, layer=1, hole=0.0):
        self.prims.append(Primitive(
            kind="pad", layer=layer, net=self.net(net), component=C[component],
            x1=x, y1=y, x2=x, y2=y, width=size, height=size, shape=PAD_SHAPE_RECT,
            hole=hole, pad_name=name,
        ))

    def track(self, net, x1, y1, x2, y2, width=4.0, layer=1, poured_by=None):
        self.prims.append(Primitive(
            kind="track", layer=layer, net=self.net(net), component=None,
            x1=x1, y1=y1, x2=x2, y2=y2, width=width, poured_by=poured_by,
        ))

    def via(self, net, x, y, diameter=20.0):
        self.prims.append(Primitive(
            kind="via", layer=1, layer_to=32, net=self.net(net), component=None,
            x1=x, y1=y, x2=x, y2=y, width=diameter, hole=10.0,
        ))

    def pour(self, net, layer, vertices, hatch="Solid", shelved=False, polygon_type="Polygon",
             name=None):
        self.pours.append(PolygonPour(
            pour_id=len(self.pours), net=None if net is None else self.net(net),
            layer=layer, hatch_style=hatch, vertices=list(vertices), shelved=shelved,
            polygon_type=polygon_type, name=name,
        ))
        return len(self.pours) - 1

    def region(self, net, layer, outline, holes=(), pour_id=None, kind="region"):
        self.areas.append(CopperArea(
            kind=kind, layer=layer, net=None if net is None else self.net(net),
            component=None, outline=list(outline), holes=[list(h) for h in holes],
            pour_id=pour_id,
        ))

    def rule(self, kind, scope="All", name=None, enabled=True, scope2="All", **properties):
        self.rules.append(PcbRule(
            name=name or kind, kind=kind, kind_id=0, enabled=enabled,
            priority=len(self.rules) + 1, scope1=scope, scope2=scope2, net_scope="AnyNet",
            comment="", unique_id=None, from_schematic=False, properties=properties,
        ))

    def index(self, plane=False, plane_nets=None, area_streams=("Regions6", "Fills6"),
              drc_rule_kinds=None) -> PcbIndex:
        data = PcbData(
            board=_board(plane, plane_nets),
            nets=self.nets,
            components=[
                PcbComponent(index=number, refdes=name, source_uid_tail=None,
                             channel_path=None, x=0.0, y=0.0, rotation=0.0, side="top",
                             designator=name)
                for number, name in enumerate(COMPONENTS)
            ],
            primitives=self.prims,
            pours=self.pours,
            layer_name_to_v6={"top layer": 1, "bottom layer": 32, "gnd plane": PLANE},
            rules=self.rules,
            classes=self.classes,
            areas=self.areas,
            area_streams=tuple(area_streams),
            drc_rule_kinds=drc_rule_kinds,
        )
        return PcbIndex(data, None)


def _square(x1, y1, x2, y2):
    return [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]


def _finding(result, check, net):
    return [item for item in result["findings"] if item["check"] == check and item["net"] == net]


def _status(index, net):
    board = connectivity(index)
    return board.net(index.net_id(net))["status"]


# ---------- routing status ----------


def test_routed_unrouted_and_partly_routed_nets():
    board = Board(["ROUTED", "OPEN", "PART"])
    board.pad("ROUTED", "U1", "1", 0, 0)
    board.pad("ROUTED", "U2", "1", 100, 0)
    board.track("ROUTED", 0, 0, 100, 0)
    board.pad("OPEN", "U1", "2", 0, 100)
    board.pad("OPEN", "U2", "2", 300, 100)
    # PART: U1-3 and U2-3 joined, the track to U3-3 stops 5 mil short of its pad.
    board.pad("PART", "U1", "3", 0, 200)
    board.pad("PART", "U2", "3", 100, 200)
    board.pad("PART", "U3", "3", 300, 200)
    board.track("PART", 0, 200, 100, 200)
    board.track("PART", 100, 200, 288, 200)
    result = check_connectivity(board.index())
    summary = result["summary"]
    assert (summary["routed"], summary["partly_routed"], summary["unrouted"]) == (1, 1, 1)
    assert summary["nets_with_2_or_more_pads"] == 3
    assert summary["routed_pct"] == 33.3
    assert summary["connections"] == {
        "total": 4, "routed": 2, "missing": 2, "completion_pct": 50.0,
    }
    (open_net,) = _finding(result, "unrouted", "OPEN")
    assert open_net["status"] == "unrouted"
    assert open_net["connections"] == [{
        "from": "U1-2", "to": "U2-2", "distance_mil": 300.0,
        "from_layer": "Top Layer", "to_layer": "Top Layer",
    }]
    (part,) = _finding(result, "unrouted", "PART")
    assert part["pad_islands"] == 2 and part["missing_connections"] == 1
    connection = part["connections"][0]
    assert {connection["from"], connection["to"]} == {"U2-3", "U3-3"}
    assert connection["distance_mil"] == 200.0
    # Track cap at 290, pad edge at 295: a near miss.
    assert connection["copper_gap_mil"] == pytest.approx(5.0, abs=0.01)
    assert connection["copper_gap_layer"] == "Top Layer"
    # The track that stops short is also an open end.
    (antenna,) = _finding(result, "antenna", "PART")
    assert antenna["ends_in"] == "nothing"
    assert antenna["stub_mil"] == pytest.approx(188.0)
    assert antenna["open_track_mil"] == pytest.approx(188.0)
    assert antenna["location_mil"] == {"x_mil": 288.0, "y_mil": 200.0}
    assert result["findings"][0]["check"] == "unrouted"
    assert result["checks_run"] == list(DEFAULT_CHECKS)
    assert result["approximations"] == APPROXIMATIONS


def test_single_pad_and_padless_nets_are_counted_apart():
    board = Board(["ONE", "NONE"])
    board.pad("ONE", "U1", "1", 0, 0)
    board.track("NONE", 0, 100, 50, 100)
    result = check_connectivity(board.index())
    summary = result["summary"]
    assert summary["single_pad_nets"] == 1
    assert summary["nets_without_pads"] == 1
    assert summary["nets_with_2_or_more_pads"] == 0
    assert summary["routed_pct"] is None and summary["connections"]["completion_pct"] is None
    (island,) = _finding(result, "isolated_copper", "NONE")
    assert island["net_has_pads"] is False


def test_missing_connections_span_islands_by_nearest_pads():
    # Four islands in a row 100 mil apart: three missing connections, nearest first.
    board = Board(["BUS"])
    for number, x in enumerate((0, 100, 210, 330)):
        board.pad("BUS", COMPONENTS[number], "1", x, 0)
    result = check_connectivity(board.index())
    (finding,) = _finding(result, "unrouted", "BUS")
    assert finding["missing_connections"] == 3
    assert [c["distance_mil"] for c in finding["connections"]] == [100.0, 110.0, 120.0]


def test_spanning_edges_match_a_brute_force_minimum_spanning_tree():
    import itertools
    import math
    import random

    from server.services.connectivity import _spanning_edges

    generator = random.Random(24)
    for _ in range(200):
        count = generator.randint(1, 30)
        points = [
            (generator.randint(0, 40) * 25.0, generator.randint(0, 40) * 25.0)
            for _ in range(count)
        ]
        groups = [generator.randint(0, 6) for _ in range(count)]
        edges = _spanning_edges(points, groups)
        distinct = sorted(set(groups))
        assert len(edges) == max(0, len(distinct) - 1)
        # Kruskal over the groups with the nearest point pair between each two.
        nearest = {}
        for i, j in itertools.combinations(range(count), 2):
            if groups[i] != groups[j]:
                key = tuple(sorted((groups[i], groups[j])))
                distance = math.dist(points[i], points[j])
                nearest[key] = min(nearest.get(key, math.inf), distance)
        parent = {group: group for group in distinct}

        def find(group):
            while parent[group] != group:
                group = parent[group]
            return group

        best = 0.0
        for (a, b), distance in sorted(nearest.items(), key=lambda item: item[1]):
            if find(a) != find(b):
                parent[find(a)] = find(b)
                best += distance
        assert sum(edge[2] for edge in edges) == pytest.approx(best)
        for first, second, distance in edges:
            assert groups[first] != groups[second]
            assert distance == pytest.approx(math.dist(points[first], points[second]))


def test_jumper_component_joins_its_pads():
    board = Board(["J"])
    board.pad("J", "U1", "1", 0, 0)
    board.pad("J", "C1", "1", 100, 0)
    board.pad("J", "C1", "2", 400, 0)
    board.pad("J", "U2", "1", 500, 0)
    board.track("J", 0, 0, 100, 0)
    board.track("J", 400, 0, 500, 0)
    index = board.index()
    assert _status(index, "J") == "partly_routed"
    jumpered = board.index()
    jumpered.pcb.components[C["C1"]].kind = 6
    assert _status(jumpered, "J") == "routed"


# ---------- touching copper ----------


def test_crossing_tracks_connect_without_a_weak_join():
    board = Board(["X"])
    board.pad("X", "U1", "1", 0, 0)
    board.pad("X", "U2", "1", 50, -50)
    board.track("X", 0, 0, 100, 0)  # horizontal from U1
    board.track("X", 50, -50, 50, 50)  # vertical from U2, crossing it mid-body
    index = board.index()
    result = check_connectivity(index, checks=list(CHECKS))
    assert _status(index, "X") == "routed"
    assert not _finding(result, "weak_connection", "X")
    # The far ends of both tracks are open.
    assert len(_finding(result, "antenna", "X")) == 2


def test_edge_only_touch_connects_and_is_reported_weak():
    # The second track ends 3 mil beside the first one's centreline: their 4 mil wide
    # copper overlaps, but neither centreline reaches the other's copper.
    board = Board(["W"])
    board.pad("W", "U1", "1", 0, 0)
    board.pad("W", "U2", "1", 50, 100)
    board.track("W", 0, 0, 100, 0)
    board.track("W", 50, 100, 50, 3)
    index = board.index()
    result = check_connectivity(index, checks=list(CHECKS))
    assert _status(index, "W") == "routed"
    (weak,) = _finding(result, "weak_connection", "W")
    assert weak["layer"] == "Top Layer"
    assert sorted(weak["objects"]) == ["Track on Top Layer", "Track on Top Layer"]
    assert result["summary"]["weak_connections"] == 1
    narrowed = check_connectivity(index, checks=["unrouted"])
    assert narrowed["findings"] == [] and narrowed["checks_run"] == ["unrouted"]


def test_touching_pads_are_not_weak():
    board = Board(["TIE"])
    board.pad("TIE", "U1", "1", 0, 0)
    board.pad("TIE", "U2", "1", 9.99, 0)  # 10 mil pads overlapping by 0.01 mil
    index = board.index()
    result = check_connectivity(index)
    assert _status(index, "TIE") == "routed"
    assert not result["findings"]


def test_copper_near_a_pad_corner_does_not_touch_it():
    board = Board(["T", "V"])
    board.pad("T", "U1", "1", 0, 0, size=20.0)
    board.pad("T", "U2", "1", 200, 200)
    # The track's round end is 0.24 mil off the pad's corner (13 - 10 = 3 mil on
    # each axis, 4 mil half width): inside a square-cornered grown pad, not touching.
    board.track("T", 200, 200, 13, 13, width=8.0)
    board.pad("V", "U1", "2", 500, 0, size=20.0)
    board.pad("V", "U2", "2", 700, 200)
    board.via("V", 518, 18, diameter=20.0)  # 1.3 mil off the corner
    board.track("V", 518, 18, 700, 200)
    index = board.index()
    assert _status(index, "T") == "unrouted"
    assert _status(index, "V") == "unrouted"
    result = check_connectivity(index, nets=["T"])
    assert _finding(result, "antenna", "T")
    (finding,) = _finding(result, "unrouted", "T")
    assert finding["connections"][0]["copper_gap_mil"] == pytest.approx(0.243, abs=0.01)


def test_graph_joins_are_checked_for_weak_connections():
    board = Board(["A", "B", "C"])
    # A: the track ends 2 mil outside U1-1; only its round end overlaps the pad.
    board.pad("A", "U1", "1", 0, 0, size=10.0)
    board.pad("A", "U2", "1", 200, 0)
    board.track("A", 7, 0, 200, 0, width=6.0)
    # B: two tracks whose ends meet 1 mil apart sideways.
    board.pad("B", "U1", "2", 0, 100)
    board.pad("B", "U2", "2", 200, 101)
    board.track("B", 0, 100, 100, 100, width=6.0)
    board.track("B", 100, 101, 200, 101, width=6.0)
    # C: the track ends just outside a via, its round end overlapping it.
    board.pad("C", "U1", "3", 0, 300)
    board.via("C", 100, 300, diameter=20.0)
    board.pad("C", "U2", "3", 100, 300, size=10.0)
    board.track("C", 0, 300, 88, 300, width=6.0)
    index = board.index()
    result = check_connectivity(index, checks=list(CHECKS))
    for net in ("A", "B", "C"):
        assert _status(index, net) == "routed", net
        assert _finding(result, "weak_connection", net), net
    assert result["summary"]["nets_joined_only_by_weak_connections"] == 3
    board_check = connectivity(index)
    assert board_check.net(index.net_id("A"))["strict_pad_islands"] == 2


def test_locations_are_where_the_copper_meets():
    board = Board(["W", "G"])
    board.pad("W", "U1", "1", 0, 0)
    board.pad("W", "U2", "1", 1000, 1000)
    board.track("W", 0, 0, 2000, 0)
    board.track("W", 1000, 1000, 1000, 3)  # its end 3 mil beside the long track
    # G: a long track stops 2 mil short of U2-2's pad edge 1000 mil away.
    board.pad("G", "U1", "2", 0, 500)
    board.pad("G", "U2", "2", 2000, 500, size=10.0)
    board.track("G", 0, 500, 1991, 500, width=4.0)
    result = check_connectivity(board.index(), checks=list(CHECKS))
    (weak,) = _finding(result, "weak_connection", "W")
    assert weak["location_mil"]["x_mil"] == pytest.approx(1000.0, abs=1.0)
    assert 1.0 <= weak["location_mil"]["y_mil"] <= 2.0  # where the copper overlaps
    (finding,) = _finding(result, "unrouted", "G")
    at = finding["connections"][0]["copper_gap_at_mil"]
    assert at["x_mil"] == pytest.approx(1993.0, abs=3.0)
    assert at["y_mil"] == pytest.approx(500.0, abs=1.0)


def test_arc_ending_on_a_lone_via_is_an_antenna():
    import math

    board = Board(["S"])
    board.pad("S", "U1", "1", 0, 0)
    board.pad("S", "U2", "1", 100, 0)
    board.track("S", 0, 0, 100, 0)
    # A quarter arc of radius 50 from U2-1 up to a via, as decode_arcs stores it.
    steps = 8
    points = [
        (50 + 50 * math.cos(math.radians(90 * k / steps)),
         50 * math.sin(math.radians(90 * k / steps)))
        for k in range(steps + 1)
    ]
    for (x1, y1), (x2, y2) in zip(points, points[1:]):
        board.prims.append(Primitive(
            kind="arc", layer=1, net=board.net("S"), component=None, x1=x1, y1=y1,
            x2=x2, y2=y2, width=6.0, source_id=7, arc_x=50.0, arc_y=0.0,
            arc_radius=50.0, length=50 * math.radians(90 / steps),
        ))
    board.via("S", 50, 50, diameter=24.0)
    (antenna,) = _finding(check_connectivity(board.index()), "antenna", "S")
    assert antenna["ends_in"] == "via"
    assert antenna["open_track_mil"] == pytest.approx(50 * math.pi / 2, abs=0.01)


def test_near_misses_are_listed_beyond_the_spanning_tree():
    board = Board(["N"])
    board.pad("N", "U1", "1", 0, 0)
    board.pad("N", "U2", "1", 1000, 0)
    board.track("N", 0, 0, 993, 0, width=2.0)  # 1 mil short of U2-1's edge
    board.pad("N", "U3", "1", 995, 60)  # nearer U1-1 than U2-1 is
    result = check_connectivity(board.index())
    (finding,) = _finding(result, "unrouted", "N")
    assert all("copper_gap_mil" not in item for item in finding["connections"])
    (miss,) = finding["near_misses"]
    assert miss["islands_of"] == ["U1-1", "U2-1"]
    assert miss["copper_gap_mil"] == pytest.approx(1.0, abs=0.01)


def test_planes_and_split_planes_off_the_stack_are_counted_not_used():
    board = Board(["GND", "VCC"])
    board.pad("GND", "U1", "1", 0, 0, layer=MULTI, hole=20.0, size=40.0)
    board.pad("GND", "U2", "1", 500, 0, layer=MULTI, hole=20.0, size=40.0)
    board.pad("VCC", "U3", "1", 0, 500)
    board.pour("VCC", PLANE, _square(-50, 450, 50, 550), hatch="None",
               polygon_type="Split Plane")
    # The plane is assigned a net, but the board's stack has no plane layer.
    result = check_connectivity(board.index(plane=False, plane_nets={PLANE: "GND"}))
    assert result["copper_model"]["planes_not_in_stack"] == 2
    assert result["copper_model"]["planes"] == 0
    assert not _finding(result, "isolated_copper", "VCC")
    (finding,) = _finding(result, "unrouted", "GND")
    assert "not checked" in finding["hint"]


def test_floating_track_on_a_lone_via_is_one_antenna():
    board = Board(["N"])
    board.pad("N", "U1", "1", 0, 0)
    board.pad("N", "U2", "1", 100, 0)
    board.track("N", 0, 0, 100, 0)
    board.track("N", 0, 300, 80, 300)
    board.via("N", 80, 300)
    result = check_connectivity(board.index())
    (antenna,) = _finding(result, "antenna", "N")
    assert antenna["open_ends"] == 2 and antenna["in_isolated_copper"] is True
    assert result["summary"]["antennae"] == 1


def test_near_miss_to_area_copper_and_its_location():
    board = Board(["F", "P"])
    # F: U1-1 sits on a fill; the track from U2-1 stops 3 mil short of the fill.
    board.pad("F", "U1", "1", 0, 0)
    board.pad("F", "U2", "1", 600, 0)
    board.areas.append(CopperArea(
        kind="fill", layer=1, net=board.net("F"), component=None,
        outline=_square(-50, -50, 200, 50),
    ))
    board.track("F", 600, 0, 205, 0, width=4.0)
    # P: a track stops 3 mil short of a 200 mil pad's edge at x = -100.
    board.pad("P", "U3", "1", 0, 400, size=200.0)
    board.pad("P", "U4", "1", -400, 480)
    board.track("P", -400, 480, -105, 480, width=4.0)
    result = check_connectivity(board.index())
    (fill_gap,) = _finding(result, "unrouted", "F")
    connection = fill_gap["connections"][0]
    assert connection["copper_gap_mil"] == pytest.approx(3.0, abs=0.01)
    assert connection["copper_gap_at_mil"]["x_mil"] == pytest.approx(201.5, abs=0.5)
    assert fill_gap["near_misses"][0]["copper_gap_mil"] == pytest.approx(3.0, abs=0.01)
    (pad_gap,) = _finding(result, "unrouted", "P")
    at = pad_gap["connections"][0]["copper_gap_at_mil"]
    # Placed toward the pad's centre: within a few mil of the gap.
    assert at["x_mil"] == pytest.approx(-101.5, abs=3.0)
    assert at["y_mil"] == pytest.approx(480.0, abs=3.0)


def test_near_misses_beyond_five_are_counted():
    board = Board(["N"])
    for number in range(7):
        x = number * 100.0
        board.pad("N", COMPONENTS[number % 6], str(number + 1), x, 0, size=10.0)
        if number:
            board.track("N", x - 93, 0, x, 0, width=2.0)  # 1 mil short of the last pad
    (finding,) = _finding(check_connectivity(board.index()), "unrouted", "N")
    assert len(finding["near_misses"]) == 5 and finding["near_misses_not_listed"] == 1


def test_layers_missing_from_the_stack_join_strictly_too():
    board = Board(["L"])
    board.pad("L", "U1", "1", 0, 0, layer=MULTI, hole=20.0, size=40.0)
    board.via("L", 300, 0)
    board.track("L", 0, 0, 300, 0, layer=2)  # Mid-Layer 1: no stack entry
    board.pad("L", "U2", "1", 300, 100)
    board.track("L", 300, 0, 300, 100)
    index = board.index()
    result = check_connectivity(index)
    assert _status(index, "L") == "routed"
    assert result["summary"]["nets_joined_only_by_weak_connections"] == 0


# ---------- area copper ----------


def _pour_board(region=True, shelved=False):
    board = Board(["GND"])
    board.pad("GND", "U1", "1", 0, 0)
    board.pad("GND", "U2", "1", 400, 0)
    pour = board.pour("GND", 1, _square(-50, -50, 450, 50), shelved=shelved)
    if region:
        board.region(None, 1, _square(-40, -40, 440, 40), pour_id=pour)
    return board


def test_pour_regions_connect_pads():
    board = _pour_board()
    # Regions6 saves a pour's copper without a net; the parser gives it the pour's.
    board.areas[0].net = board.net("GND")
    index = board.index()
    result = check_connectivity(index)
    assert _status(index, "GND") == "routed"
    assert result["copper_model"]["pour_regions"] == 1
    assert result["summary"]["nets_connected_through_assumed_copper"] == 0


def test_pour_without_saved_copper_connects_nothing():
    index = _pour_board(region=False).index()
    result = check_connectivity(index)
    (finding,) = _finding(result, "unrouted", "GND")
    assert finding["pours_without_copper"] == {"Top Layer": 1}
    assert "no saved copper" in finding["hint"]
    assert result["copper_model"]["pours_without_copper"] == 1


def test_pour_outline_stands_in_when_regions_were_not_read():
    index = _pour_board(region=False).index(area_streams=())
    result = check_connectivity(index, nets=["GND"])
    assert result["nets"] == [{
        "net": "GND", "status": "routed", "pads": 2, "pad_islands": 1,
        "connected_through_assumed_copper": ["pour outline (Regions6 not read)"],
    }]
    assert check_connectivity(index)["assumed_copper_nets"] == [
        {"net": "GND", "through": ["pour outline (Regions6 not read)"]},
    ]
    assert result["copper_model"]["pour_outlines_assumed"] == 1
    assert result["summary"]["nets_connected_through_assumed_copper"] == 1
    assert "Regions6 stream" in result["approximations"][0]


def test_pour_outline_follows_its_arcs():
    # The outline stands in for the copper (Regions6 not read); its right edge is
    # a Polygons6 arc bulging out to x = 500, round U2-1, 30 mil past the chord.
    board = Board(["GND"])
    board.pad("GND", "U1", "1", 0, 0)
    board.pad("GND", "U2", "1", 485, 0)
    record = {
        "LAYER": "TOP", "NET": str(board.net("GND")), "HATCHSTYLE": "Solid",
        "VX0": "-50mil", "VY0": "-50mil", "VX1": "450mil", "VY1": "-50mil", "KIND1": "1",
        "CX1": "450mil", "CY1": "0mil", "R1": "50mil", "SA1": "270", "EA1": "90",
        "VX2": "450mil", "VY2": "50mil", "VX3": "-50mil", "VY3": "50mil",
    }
    (pour,), _ = decode_polygons([record], {"top": 1})
    board.pours.append(pour)
    index = board.index(area_streams=())
    assert _status(index, "GND") == "routed"
    assert check_connectivity(index)["copper_model"]["pour_outlines_assumed"] == 1


def test_shelved_pour_is_named_with_the_unrouted_net():
    index = _pour_board(region=False, shelved=True).index()
    result = check_connectivity(index)
    assert result["copper_model"]["pours_without_copper"] == 0
    assert result["copper_model"]["shelved_pours"] == 1
    (finding,) = _finding(result, "unrouted", "GND")
    assert "pours_without_copper" not in finding
    assert finding["shelved_pours"] == {"Top Layer": 1}
    assert "shelved" in finding["hint"]


def test_region_hole_and_thermal_spoke():
    board = Board(["GND"])
    board.pad("GND", "U1", "1", 0, 0)
    board.pad("GND", "U2", "1", 200, 0)
    board.pad("GND", "U3", "1", 400, 0)
    hole_u2 = _square(185, -15, 215, 15)  # 10 mil air gap round U2-1
    # U3-1 sits in a hole whose spoke (a notch of copper) reaches its left edge.
    spoke_hole = [(385, -15), (415, -15), (415, 15), (385, 15), (385, 2), (395, 2),
                  (395, -2), (385, -2)]
    board.region("GND", 1, _square(-50, -50, 450, 50), holes=[hole_u2, spoke_hole])
    index = board.index()
    result = check_connectivity(index)
    (finding,) = _finding(result, "unrouted", "GND")
    connection = finding["connections"][0]
    assert finding["pad_islands"] == 2
    assert "U2-1" in (connection["from"], connection["to"])


def test_fill_and_free_region_connect():
    board = Board(["A"])
    board.pad("A", "U1", "1", 0, 0)
    board.pad("A", "U2", "1", 100, 0)
    board.pad("A", "U3", "1", 200, 0)
    board.areas.append(CopperArea(
        kind="fill", layer=1, net=board.net("A"), component=None,
        outline=_square(0, -3, 100, 3),
    ))
    board.region("A", 1, _square(99, -3, 201, 3))
    index = board.index()
    assert _status(index, "A") == "routed"
    model = check_connectivity(index)["copper_model"]
    assert (model["fills"], model["free_regions"]) == (1, 1)


def test_overlapping_areas_join():
    board = Board(["A"])
    board.pad("A", "U1", "1", 0, 0)
    board.pad("A", "U2", "1", 300, 0)
    board.region("A", 1, _square(-20, -20, 160, 20))
    board.region("A", 1, _square(150, -20, 320, 20))  # overlaps the first, no pad shared
    assert _status(board.index(), "A") == "routed"


def test_dead_pour_island_is_info_and_floating_via_a_warning():
    board = Board(["GND"])
    board.pad("GND", "U1", "1", 0, 0)
    board.pad("GND", "U2", "1", 100, 0)
    board.track("GND", 0, 0, 100, 0)
    pour = board.pour("GND", 32, _square(500, 500, 700, 700))
    board.region("GND", 32, _square(510, 510, 690, 690), pour_id=pour)
    board.via("GND", 0, 400)
    result = check_connectivity(board.index())
    islands = _finding(result, "isolated_copper", "GND")
    assert {item["severity"] for item in islands} == {"info", "warning"}
    pour_island = next(item for item in islands if item["severity"] == "info")
    assert pour_island["objects"] == {"pour_regions": 1}
    assert pour_island["layers"] == ["Bottom Layer"]
    via = next(item for item in islands if item["severity"] == "warning")
    assert via["objects"] == {"vias": 1}
    assert via["location_mil"] == {"x_mil": 0.0, "y_mil": 400.0}
    assert result["findings"][0]["severity"] == "warning"


def test_isolated_track_reports_the_nearest_copper():
    board = Board(["N"])
    board.pad("N", "U1", "1", 0, 0)
    board.pad("N", "U2", "1", 100, 0)
    board.track("N", 0, 0, 100, 0)
    board.track("N", 0, 10, 100, 10)  # its edge 3 mil from the pads, 6 from the track
    result = check_connectivity(board.index())
    (island,) = _finding(result, "isolated_copper", "N")
    assert island["objects"] == {"tracks": 1}
    assert island["routed_mil"] == 100.0
    assert island["nearest_copper_mil"] == pytest.approx(3.0)
    assert island["nearest_copper_reaches_pads"] is True
    assert NEAR_MIL == 25.0


def test_track_grazing_pour_copper_is_weak_but_a_relief_via_is_not_tested():
    board = Board(["GND"])
    board.pad("GND", "U1", "1", 0, 0)
    board.pad("GND", "U2", "1", 300, 50)
    board.pad("GND", "U3", "1", 300, -100)
    pour = board.pour("GND", 1, _square(-20, -20, 200, 20))
    board.region("GND", 1, _square(-20, -20, 200, 20), pour_id=pour)
    # A track along the pour's top edge, 3 mil above it: its copper overlaps
    # the pour, its centreline does not reach it.
    board.track("GND", 100, 23, 300, 23, width=8.0)
    board.track("GND", 300, 23, 300, 50, width=8.0)
    # A via just outside the pour touching its copper: connected by relief,
    # its centre is not tested against the pour outline.
    board.via("GND", 209, -10)
    board.track("GND", 209, -10, 300, -100)
    board.rule("PolygonConnect", CONNECTSTYLE="Relief")
    index = board.index()
    result = check_connectivity(index, checks=list(CHECKS))
    assert _status(index, "GND") == "routed"
    (weak,) = _finding(result, "weak_connection", "GND")
    assert weak["objects"] == ["Track on Top Layer", "Pour copper on Top Layer"]


def _straddling_board(region=True, pour_layer=1, pour_name=None, **connect_style):
    """GND with a pour round U1-1, an SMD pad (U2-1) whose centre is 4 mil past
    the pour's right edge and a via (on to U3-1) 6 mil past its left edge, each
    joined to the pour only by touching its copper (a region on the top layer)."""
    board = Board(["GND"])
    board.pad("GND", "U1", "1", 0, 0)
    board.pad("GND", "U2", "1", 104, 0, size=10.0)
    board.via("GND", -106, 0)
    board.track("GND", -106, 0, -106, -100)
    board.pad("GND", "U3", "1", -106, -100)
    pour = board.pour("GND", pour_layer, _square(-100, -20, 100, 20), name=pour_name)
    if region:
        board.region("GND", 1, _square(-100, -20, 100, 20), pour_id=pour)
    if connect_style:
        board.rule("PolygonConnect", **connect_style)
    return board


BOTH = ["Pad U2-1", "Via"]


@pytest.mark.parametrize("connect_style, flagged", [
    ({"CONNECTSTYLE": "Direct"}, BOTH),
    ({"CONNECTSTYLE": "Relief"}, []),
    ({"CONNECTSTYLE": "NoConnect"}, []),
    ({}, []),  # no Polygon Connect Style rule: not tested
    (
        {"THPAD.CONNECTSTYLE": "Direct", "SMDPAD.CONNECTSTYLE": "Direct",
         "VIA.CONNECTSTYLE": "Relief"},
        ["Pad U2-1"],
    ),
    (
        {"THPAD.CONNECTSTYLE": "Direct", "SMDPAD.CONNECTSTYLE": "Relief",
         "VIA.CONNECTSTYLE": "Direct"},
        ["Via"],
    ),
])
def test_direct_pad_or_via_with_its_centre_off_the_pour_outline_is_weak(
    connect_style, flagged
):
    index = _straddling_board(**connect_style).index()
    result = check_connectivity(index, checks=list(CHECKS))
    assert _status(index, "GND") == "routed"
    weak = _finding(result, "weak_connection", "GND")
    assert sorted(item["objects"][0] for item in weak) == flagged
    assert {item["objects"][1] for item in weak} <= {"Pour copper on Top Layer"}
    assert result["summary"]["nets_joined_only_by_weak_connections"] == (1 if flagged else 0)


@pytest.mark.parametrize("scope1, scope2, first, otherwise, flagged", [
    # The pour side decides: the first rule is for bottom-layer pours only.
    ("All", "OnLayer('Bottom Layer')", "Direct", "Relief", []),
    ("All", "OnLayer('Top Layer') And IsPolygon", "Direct", "Relief", BOTH),
    ("All", "Not IsSplitPlane", "Direct", "Relief", BOTH),
    ("IsVia", "All", "Direct", "Relief", ["Via"]),
    # Matched only the other way round (U2-1 by the second scope, the pour by the
    # first; the via is not a pour): Altium's help does not say whether that
    # counts, so the pad is tested only when the next rule agrees.
    ("All", "InComponent('U2')", "Direct", "Relief", []),
    ("All", "InComponent('U2')", "Direct", "Direct", BOTH),
    ("Not IsVia", "All", "Relief", "Direct", []),
    # A pour without NAME and a scope that does not parse are not known: the
    # style is known only when the rules that may apply agree.
    ("All", "IsNamedPolygon('GND_TOP')", "Direct", "Relief", []),
    ("All", "IsNamedPolygon('GND_TOP')", "Direct", "Direct", BOTH),
    ("InNet('GND'", "All", "Relief", "Direct", []),
    ("InNet('GND'", "All", "Direct", "Relief", []),
    ("InNet('GND'", "All", "Direct", "Direct", BOTH),
])
def test_polygon_connect_rule_matches_the_pad_or_via_and_the_pour(
    scope1, scope2, first, otherwise, flagged
):
    board = _straddling_board()
    board.rule("PolygonConnect", scope=scope1, scope2=scope2, CONNECTSTYLE=first)
    board.rule("PolygonConnect", CONNECTSTYLE=otherwise)
    result = check_connectivity(board.index(), checks=["weak"])
    weak = _finding(result, "weak_connection", "GND")
    assert sorted(item["objects"][0] for item in weak) == flagged


@pytest.mark.parametrize("scope1, scope2, first, otherwise, flagged", [
    # A direct rule for the pour by name or class, a relief rule after it.
    ("All", "IsNamedPolygon('gnd_top')", "Direct", "Relief", BOTH),
    ("All", "IsNamedPolygon('GND_BOTTOM')", "Direct", "Relief", []),
    ("All", "InNamedPolygon('GND_TOP')", "Direct", "Relief", BOTH),
    ("All", "IsPolygon And (Name Like 'GND_T*')", "Direct", "Relief", BOTH),
    ("All", "Name = 'VCC'", "Direct", "Relief", []),
    ("All", "InPolygonClass('TopPours')", "Direct", "Relief", BOTH),
    ("All", "InPolygonClass('All Polygons')", "Direct", "Relief", BOTH),
    ("All", "InPolygonClass('BottomPours')", "Direct", "Relief", []),
    # A relief rule for another pour is ruled out: the direct one applies.
    ("All", "IsNamedPolygon('GND_BOTTOM')", "Relief", "Direct", BOTH),
    # Named in the first scope, the pour matches only the other way round,
    # which may count: nothing is tested while the styles differ.
    ("IsNamedPolygon('GND_TOP')", "All", "Relief", "Direct", []),
    ("IsNamedPolygon('GND_BOTTOM')", "All", "Relief", "Direct", BOTH),
])
def test_polygon_connect_rule_naming_the_pour(scope1, scope2, first, otherwise, flagged):
    board = _straddling_board(pour_name="GND_TOP")
    board.classes.extend(
        PcbClass(name=name, kind="polygon", members=members, superclass=superclass,
                 auto_generated=False, unique_id=None)
        for name, members, superclass in (
            ("TopPours", ["GND_TOP"], False),
            ("BottomPours", ["GND_BOTTOM"], False),
            ("All Polygons", [], True),
        )
    )
    board.rule("PolygonConnect", scope=scope1, scope2=scope2, CONNECTSTYLE=first)
    board.rule("PolygonConnect", CONNECTSTYLE=otherwise)
    result = check_connectivity(board.index(), checks=["weak"])
    weak = _finding(result, "weak_connection", "GND")
    assert sorted(item["objects"][0] for item in weak) == flagged


@pytest.mark.parametrize("enabled, flagged", [(True, []), (False, BOTH)])
def test_polygon_connect_rules_go_by_priority_when_enabled(enabled, flagged):
    board = _straddling_board(CONNECTSTYLE="Direct")
    board.rule("PolygonConnect", enabled=enabled, CONNECTSTYLE="Relief")
    board.rules[0].priority, board.rules[1].priority = 2, 1  # the relief rule first
    result = check_connectivity(board.index(), checks=["weak"])
    weak = _finding(result, "weak_connection", "GND")
    assert sorted(item["objects"][0] for item in weak) == flagged


@pytest.mark.parametrize("room, term, flagged", [
    # A relief rule for a room that the pads, the via and the pour all lie clear of.
    ((1000, 1000, 1200, 1200), "WithinRoom", BOTH),
    ((1000, 1000, 1200, 1200), "TouchesRoom", BOTH),
    # A room over the pour's middle: the pads and the via are outside it, but how
    # much of the pour lies in it is not measured, so the relief rule may apply.
    ((-50, -10, 50, 10), "WithinRoom", []),
    ((-50, -10, 50, 10), "TouchesRoom", []),
])
def test_room_rule_is_decided_on_the_pour_only_when_it_lies_clear(room, term, flagged):
    board = _straddling_board()
    x1, y1, x2, y2 = (f"{value}mil" for value in room)
    board.rule("RoomDefinition", name="PSU", VX0=x1, VY0=y1, VX1=x2, VY1=y1,
               VX2=x2, VY2=y2, VX3=x1, VY3=y2)
    board.rule("PolygonConnect", scope=f"{term}('PSU')", CONNECTSTYLE="Relief")
    board.rule("PolygonConnect", CONNECTSTYLE="Direct")
    result = check_connectivity(board.index(), checks=["weak"])
    weak = _finding(result, "weak_connection", "GND")
    assert sorted(item["objects"][0] for item in weak) == flagged


def test_room_term_on_the_pour_side_is_not_decided_by_its_middle():
    # The pour's middle is in the room but most of it is not: the direct rule for
    # pours within the room may or may not apply, so nothing is tested.
    board = _straddling_board()
    board.rule("RoomDefinition", name="PSU", VX0="-50mil", VY0="-10mil", VX1="50mil",
               VY1="-10mil", VX2="50mil", VY2="10mil", VX3="-50mil", VY3="10mil")
    board.rule("PolygonConnect", scope2="WithinRoom('PSU')", CONNECTSTYLE="Direct")
    board.rule("PolygonConnect", CONNECTSTYLE="Relief")
    result = check_connectivity(board.index(), checks=["weak"])
    assert _finding(result, "weak_connection", "GND") == []


def test_pour_whose_layer_is_not_read_takes_the_copper_layer():
    # The pour's own layer did not resolve; its copper is on the top layer, which
    # the rule's pour scope asks for.
    board = _straddling_board(pour_layer=None)
    board.rule("PolygonConnect", scope2="OnTopLayer", CONNECTSTYLE="Direct")
    result = check_connectivity(board.index(), checks=["weak"])
    weak = _finding(result, "weak_connection", "GND")
    assert sorted(item["objects"][0] for item in weak) == BOTH


def test_pour_outline_standing_in_for_unread_copper_tests_pads_too():
    index = _straddling_board(region=False, CONNECTSTYLE="Direct").index(area_streams=())
    result = check_connectivity(index, checks=["weak"])
    assert _status(index, "GND") == "routed"
    weak = _finding(result, "weak_connection", "GND")
    assert sorted(item["objects"][0] for item in weak) == BOTH
    assert {item["objects"][1] for item in weak} == {"Pour outline on Top Layer"}


def test_through_hole_pad_takes_the_rule_thpad_style():
    board = Board(["GND"])
    board.pad("GND", "U1", "1", 0, 0)
    # A through-hole pad 15 mil past the pour's edge, its 40 mil copper over it.
    board.pad("GND", "U2", "1", 115, 0, size=40.0, hole=20.0)
    pour = board.pour("GND", 1, _square(-100, -20, 100, 20))
    board.region("GND", 1, _square(-100, -20, 100, 20), pour_id=pour)
    board.rule("PolygonConnect", **{
        "THPAD.CONNECTSTYLE": "Direct", "SMDPAD.CONNECTSTYLE": "Relief",
        "VIA.CONNECTSTYLE": "Relief",
    })
    result = check_connectivity(board.index(), checks=["weak"])
    (weak,) = _finding(result, "weak_connection", "GND")
    assert weak["objects"] == ["Pad U2-1", "Pour copper on Top Layer"]


@pytest.mark.parametrize("style, weak", [("Direct", 1), ("Relief", 0)])
def test_hatched_pour_tests_a_direct_pad_centre_too(style, weak):
    board = Board(["GND"])
    board.pad("GND", "U1", "1", 0, 0)
    board.pad("GND", "U2", "1", 104, 0, size=10.0)  # 4 mil past the pour's edge
    pour = board.pour("GND", 1, _square(-100, -20, 100, 20), hatch="Hatched")
    board.track("GND", -98, 0, 98, 0, poured_by=pour)  # its copper reaches x = 100
    board.rule("PolygonConnect", CONNECTSTYLE=style)
    index = board.index()
    result = check_connectivity(index, checks=["weak"])
    assert _status(index, "GND") == "routed"
    found = _finding(result, "weak_connection", "GND")
    assert len(found) == weak
    if weak:
        assert sorted(found[0]["objects"]) == ["Pad U2-1", "Pour track on Top Layer"]


@pytest.mark.parametrize("past, weak", [(0.4, 0), (0.6, 1)])
def test_pad_centre_on_or_within_centre_mil_of_the_pour_outline_is_not_weak(past, weak):
    board = Board(["GND"])
    board.pad("GND", "U1", "1", 0, 0)
    board.pad("GND", "U2", "1", 100, 0)  # on the pour's right edge
    board.via("GND", -100 - past, 0)  # past its left edge (CENTRE_MIL 0.5)
    board.track("GND", -100 - past, 0, -100 - past, -100)
    board.pad("GND", "U3", "1", -100 - past, -100)
    pour = board.pour("GND", 1, _square(-100, -20, 100, 20))
    board.region("GND", 1, _square(-100, -20, 100, 20), pour_id=pour)
    board.rule("PolygonConnect", CONNECTSTYLE="Direct")
    index = board.index()
    result = check_connectivity(index, checks=list(CHECKS))
    assert _status(index, "GND") == "routed"
    found = _finding(result, "weak_connection", "GND")
    assert [item["objects"][0] for item in found] == ["Via"] * weak
    assert result["summary"]["nets_joined_only_by_weak_connections"] == weak


def test_pad_centre_is_tested_against_the_pour_outline_arcs():
    # The pour's right edge is a Polygons6 arc bulging out to x = 500; U2-1 sits
    # in the bulge, 30 mil past the chord, so its centre is inside the outline.
    # U3-1 lies 4 mil past the arc.
    board = Board(["GND"])
    board.pad("GND", "U1", "1", 0, 0)
    board.pad("GND", "U2", "1", 480, 0)
    board.pad("GND", "U3", "1", 504, 0, size=10.0)
    board.track("GND", 504, 0, 504, -200)
    board.pad("GND", "C1", "1", 504, -200)
    record = {
        "LAYER": "TOP", "NET": str(board.net("GND")), "HATCHSTYLE": "Solid",
        "VX0": "-50mil", "VY0": "-50mil", "VX1": "450mil", "VY1": "-50mil", "KIND1": "1",
        "CX1": "450mil", "CY1": "0mil", "R1": "50mil", "SA1": "270", "EA1": "90",
        "VX2": "450mil", "VY2": "50mil", "VX3": "-50mil", "VY3": "50mil",
    }
    (pour,), _ = decode_polygons([record], {"top": 1})
    board.pours.append(pour)
    board.region("GND", 1, pour.vertices, pour_id=pour.pour_id)
    board.rule("PolygonConnect", CONNECTSTYLE="Direct")
    index = board.index()
    result = check_connectivity(index, checks=["weak"])
    assert _status(index, "GND") == "routed"
    (weak,) = _finding(result, "weak_connection", "GND")
    assert weak["objects"] == ["Pad U3-1", "Pour copper on Top Layer"]


def test_track_edge_touching_a_pad_is_weak():
    board = Board(["P"])
    board.pad("P", "U1", "1", 0, 0, size=10.0)
    board.pad("P", "U2", "1", 200, 0)
    # The track runs past U1-1 4 mil above its edge: 4 mil wide copper touches the
    # pad, its centreline does not, and no end of it lies on the pad.
    board.track("P", -50, 7, 200, 7)
    board.track("P", 200, 7, 200, 0)
    index = board.index()
    result = check_connectivity(index, checks=list(CHECKS))
    assert _status(index, "P") == "routed"
    (weak,) = _finding(result, "weak_connection", "P")
    assert sorted(weak["objects"]) == ["Pad U1-1", "Track on Top Layer"]


def test_floating_track_is_one_antenna_with_two_open_ends():
    board = Board(["N"])
    board.pad("N", "U1", "1", 0, 0)
    board.pad("N", "U2", "1", 100, 0)
    board.track("N", 0, 0, 100, 0)
    board.track("N", 0, 300, 50, 300)
    board.rule("NetAntennae", NETANTENNAETOLERANCE="0mil")
    result = check_connectivity(board.index())
    (antenna,) = _finding(result, "antenna", "N")
    assert antenna["in_isolated_copper"] is True and antenna["open_ends"] == 2
    assert antenna["open_track_mil"] == pytest.approx(50.0)
    assert antenna["stub_over_tolerance"] is True
    assert result["summary"]["antennae_in_isolated_copper"] == 1
    assert len(_finding(result, "isolated_copper", "N")) == 1


def test_missing_connection_between_pads_of_one_part():
    board = Board(["S"])
    board.pad("S", "U1", "1", 0, 0)
    board.pad("S", "U1", "2", 100, 0)
    (finding,) = _finding(check_connectivity(board.index()), "unrouted", "S")
    assert finding["connections"][0]["same_component"] is True
    assert "Jumper ID" in finding["hint"]


# ---------- internal planes ----------


def test_plane_connects_through_hole_pads_except_in_a_split():
    board = Board(["GND", "VCC"])
    board.pad("GND", "U1", "1", 0, 0, layer=MULTI, hole=20.0, size=40.0)
    board.pad("GND", "U2", "1", 500, 0, layer=MULTI, hole=20.0, size=40.0)
    board.pad("GND", "U3", "1", 1000, 0, layer=MULTI, hole=20.0, size=40.0)
    board.pad("VCC", "U4", "1", 1000, 500, layer=MULTI, hole=20.0, size=40.0)
    board.pad("VCC", "C1", "1", 1200, 500, layer=MULTI, hole=20.0, size=40.0)
    # A VCC split plane covers U3-1 (GND) and both VCC pads.
    board.pour("VCC", PLANE, _square(900, -100, 1300, 600), hatch="None",
               polygon_type="Split Plane")
    index = board.index(plane=True, plane_nets={PLANE: "GND"})
    result = check_connectivity(index, nets=["GND", "VCC"])
    statuses = {entry["net"]: entry for entry in result["nets"]}
    assert statuses["VCC"]["status"] == "routed"
    assert statuses["VCC"]["connected_through_assumed_copper"] == ["split plane (outline)"]
    assert statuses["GND"]["status"] == "partly_routed"
    assert statuses["GND"]["connected_through_assumed_copper"] == ["internal plane"]
    (finding,) = _finding(result, "unrouted", "GND")
    assert "U3-1" in (finding["connections"][0]["from"], finding["connections"][0]["to"])
    model = result["copper_model"]
    assert (model["planes"], model["split_planes"]) == (1, 1)


def test_split_plane_honours_no_connect():
    board = Board(["VCC"])
    board.pad("VCC", "U1", "1", 0, 0, layer=MULTI, hole=20.0, size=40.0)
    board.pad("VCC", "U2", "1", 500, 0, layer=MULTI, hole=20.0, size=40.0)
    board.pour("VCC", PLANE, _square(-100, -100, 600, 100), hatch="None",
               polygon_type="Split Plane")
    board.rule("PlaneConnect", scope="InComponent('U2')", PLANECONNECTSTYLE="NoConnect")
    assert _status(board.index(plane=True), "VCC") == "unrouted"


def test_plane_no_connect_rule_keeps_a_pad_off_the_plane():
    board = Board(["GND"])
    board.pad("GND", "U1", "1", 0, 0, layer=MULTI, hole=20.0, size=40.0)
    board.pad("GND", "U2", "1", 500, 0, layer=MULTI, hole=20.0, size=40.0)
    board.rule("PlaneConnect", scope="InComponent('U2')", PLANECONNECTSTYLE="NoConnect")
    index = board.index(plane=True, plane_nets={PLANE: "GND"})
    assert _status(index, "GND") == "unrouted"


def test_plane_connect_style_set_per_object_kind():
    """A rule with only PAD. / VIA. styles: the object's own kind decides."""
    board = Board(["GND"])
    board.pad("GND", "U1", "1", 0, 0, layer=MULTI, hole=20.0, size=40.0)
    board.pad("GND", "U2", "1", 500, 0, layer=MULTI, hole=20.0, size=40.0)
    board.rule("PlaneConnect", scope="InComponent('U2')",
               **{"PAD.PLANECONNECTSTYLE": "NoConnect", "VIA.PLANECONNECTSTYLE": "Direct"})
    index = board.index(plane=True, plane_nets={PLANE: "GND"})
    assert _status(index, "GND") == "unrouted"
    board = Board(["GND"])
    board.pad("GND", "U1", "1", 0, 0, layer=MULTI, hole=20.0, size=40.0)
    board.pad("GND", "U2", "1", 500, 0, layer=MULTI, hole=20.0, size=40.0)
    board.rule("PlaneConnect", scope="InComponent('U2')",
               **{"VIA.PLANECONNECTSTYLE": "NoConnect", "PLANECONNECTSTYLE": "Direct"})
    index = board.index(plane=True, plane_nets={PLANE: "GND"})
    assert _status(index, "GND") == "routed"


# ---------- antennae ----------


def test_track_ending_on_a_lone_via_is_an_antenna():
    board = Board(["S", "T"])
    board.pad("S", "U1", "1", 0, 0)
    board.pad("S", "U2", "1", 100, 0)
    board.track("S", 0, 0, 100, 0)
    board.track("S", 100, 0, 100, 60)
    board.via("S", 100, 60)
    # T: a via on the junction of a routed track connects nothing else: no stub.
    board.pad("T", "U1", "2", 0, 200)
    board.pad("T", "U2", "2", 200, 200)
    board.track("T", 0, 200, 100, 200)
    board.track("T", 100, 200, 200, 200)
    board.via("T", 100, 200)
    board.rule("NetAntennae", NETANTENNAETOLERANCE="50mil")
    result = check_connectivity(board.index())
    (antenna,) = _finding(result, "antenna", "S")
    assert antenna["ends_in"] == "via"
    assert antenna["location_mil"] == {"x_mil": 100.0, "y_mil": 60.0}
    assert antenna["stub_mil"] == pytest.approx(60.0)
    assert antenna["open_track_mil"] == pytest.approx(60.0)
    assert (antenna["rule"], antenna["tolerance_mil"]) == ("NetAntennae", 50.0)
    assert antenna["severity"] == "warning"
    assert not _finding(result, "antenna", "T")
    assert result["summary"]["antennae_over_tolerance"] == 1
    rules = result["rules"]["NetAntennae"]
    assert rules["rules"][0]["tolerance_mil"] == 50.0
    assert rules["checked_by_batch_drc"] is None


def test_antenna_within_tolerance_is_info_and_stub_runs_to_the_branch():
    board = Board(["S"])
    board.pad("S", "U1", "1", 0, 0)
    board.pad("S", "U2", "1", 200, 0)
    board.track("S", 0, 0, 200, 0)
    # A stub of two tracks off the middle of the route: 40 + 30 mil.
    board.track("S", 100, 0, 100, 40)
    board.track("S", 100, 40, 130, 40)
    board.rule("NetAntennae", NETANTENNAETOLERANCE="35mil")
    result = check_connectivity(board.index(drc_rule_kinds={56}))
    (antenna,) = _finding(result, "antenna", "S")
    assert antenna["stub_mil"] == pytest.approx(70.0)
    assert antenna["open_track_mil"] == pytest.approx(30.0)
    assert antenna["severity"] == "info"
    assert result["rules"]["NetAntennae"]["checked_by_batch_drc"] is True
    assert result["rules"]["UnRoutedNet"]["checked_by_batch_drc"] is False


def test_track_end_touching_a_pour_or_pad_is_not_open():
    board = Board(["G"])
    board.pad("G", "U1", "1", 0, 0)
    board.pad("G", "U2", "1", 300, 0)
    board.region("G", 1, _square(150, -100, 450, 100))
    board.track("G", 0, 0, 160, 0)  # ends inside the region
    board.track("G", 300, 0, 300, 200)  # leaves the region upwards...
    board.pad("G", "U3", "1", 300, 205)  # ...and ends on a pad's edge
    index = board.index()
    result = check_connectivity(index)
    assert _status(index, "G") == "routed"
    assert not result["findings"]


def test_antenna_without_an_enabled_rule_is_info():
    board = Board(["S"])
    board.pad("S", "U1", "1", 0, 0)
    board.pad("S", "U2", "1", 100, 0)
    board.track("S", 0, 0, 100, 0)
    board.track("S", 50, 0, 50, 10)
    board.rule("NetAntennae", enabled=False, NETANTENNAETOLERANCE="500mil")
    result = check_connectivity(board.index())
    (antenna,) = _finding(result, "antenna", "S")
    assert (antenna["rule"], antenna["tolerance_mil"]) == (None, None)
    # Altium checks nothing without an enabled rule: not over any tolerance.
    assert antenna["over_tolerance"] is None and antenna["severity"] == "info"
    assert result["summary"]["antennae_over_tolerance"] == 0
    assert result["summary"]["antennae_without_enabled_rule"] == 1


def test_poured_tracks_join_but_are_no_antennae():
    board = Board(["GND"])
    board.pad("GND", "U1", "1", 0, 0)
    board.pad("GND", "U2", "1", 200, 0)
    pour = board.pour("GND", 1, _square(-20, -20, 220, 20), hatch="Horizontal")
    board.track("GND", 0, 0, 200, 0, width=8.0, poured_by=pour)
    board.track("GND", 50, 0, 50, 15, width=8.0, poured_by=pour)  # an open hatch end
    index = board.index()
    result = check_connectivity(index)
    assert _status(index, "GND") == "routed"
    assert not result["findings"]
    assert result["copper_model"]["hatched_pour_primitives"] == 2
    assert result["copper_model"]["pours_without_copper"] == 0


# ---------- filters, errors and paging ----------


def _filter_board():
    board = Board(["A", "B", "C"])
    board.pad("A", "U1", "1", 0, 0)
    board.pad("A", "U2", "1", 100, 0, layer=32)
    board.pad("B", "U1", "2", 0, 100)
    board.pad("B", "U2", "2", 100, 100)
    board.pad("C", "U1", "3", 0, 200)
    board.pad("C", "U2", "3", 100, 200)
    board.track("C", 0, 200, 100, 200)
    board.classes.append(PcbClass(
        name="Pair", kind="net", members=["A", "B"], superclass=False,
        auto_generated=False, unique_id=None,
    ))
    return board.index()


def test_net_class_and_layer_filters():
    index = _filter_board()
    by_class = check_connectivity(index, net_classes=["pair"])
    assert by_class["summary"]["nets_checked"] == 2
    assert [entry["net"] for entry in by_class["nets"]] == ["A", "B"]
    one = check_connectivity(index, nets=["c"])
    assert one["nets"] == [{"net": "C", "status": "routed", "pads": 2, "pad_islands": 1}]
    bottom = check_connectivity(index, layer="Bottom Layer")
    assert [item["net"] for item in bottom["findings"]] == ["A"]
    assert bottom["layer"] == "Bottom Layer" and "layer_note" in bottom
    assert bottom["summary"]["unrouted"] == 2


def test_errors():
    index = _filter_board()
    assert check_connectivity(index, nets=["NOPE"])["error"] == "net_not_found"
    missing = check_connectivity(index, net_classes=["Gone"])
    assert missing["error"] == "net_class_not_found"
    no_layer = check_connectivity(index, layer="Mid 9")
    assert no_layer["error"] == "layer_not_found"
    assert no_layer["layers"] == ["Top Layer", "Bottom Layer"]
    assert missing["net_classes"] == ["Pair"]
    bad = check_connectivity(index, checks=["bogus"])
    assert bad["error"] == "invalid_checks" and bad["valid_checks"] == list(CHECKS)


def test_weak_findings_are_listed_only_when_asked_for_without_the_option():
    board = Board(["W"])
    board.pad("W", "U1", "1", 0, 0)
    board.pad("W", "U2", "1", 50, 100)
    board.track("W", 0, 0, 100, 0)
    board.track("W", 50, 100, 50, 3)
    result = check_connectivity(board.index())
    assert not _finding(result, "weak_connection", "W")
    assert result["summary"]["weak_connections"] == 1
    assert result["summary"]["incomplete_connections_checked"] is False
    assert "nets_routed_here_unrouted_in_altium" not in result["summary"]
    assert "nets_unrouted_in_altium" not in result


def _weak_board(nets=("W",)) -> Board:
    """Each net: two pads joined by tracks whose copper only overlaps (a weak join)."""
    board = Board(list(nets))
    for number, net in enumerate(nets):
        y = number * 500.0
        board.pad(net, "U1", str(number + 1), 0, y)
        board.pad(net, "U2", str(number + 1), 50, y + 100)
        board.track(net, 0, y, 100, y)
        board.track(net, 50, y + 100, 50, y + 3)
    return board


def test_rule_summary_reads_check_for_incomplete_connections():
    board = _weak_board()
    board.rule("UnRoutedNet", name="On", CHECKBADCONNECTIONS="TRUE")
    board.rule("UnRoutedNet", name="Off", CHECKBADCONNECTIONS="FALSE")
    board.rule("UnRoutedNet", name="Unsaved")
    board.rule("UnRoutedNet", name="Lower", checkbadconnections=" true ")
    rules = check_connectivity(board.index())["rules"]["UnRoutedNet"]["rules"]
    assert [(rule["name"], rule["check_incomplete_connections"]) for rule in rules] == [
        ("On", True), ("Off", False), ("Unsaved", None), ("Lower", True),
    ]


def test_weak_joins_are_warnings_when_the_rule_checks_incomplete_connections():
    board = _weak_board()
    board.rule("UnRoutedNet", CHECKBADCONNECTIONS="TRUE")
    index = board.index()
    result = check_connectivity(index)
    assert result["checks_run"] == [*DEFAULT_CHECKS, "weak"]
    (weak,) = _finding(result, "weak_connection", "W")
    assert weak["severity"] == "warning"
    assert weak["incomplete_connections_checked"] is True
    summary = result["summary"]
    # Touching copper still routes the net here; Altium reads it un-routed.
    assert summary["routed"] == 1 and summary["connections"]["missing"] == 0
    assert summary["incomplete_connections_checked"] is True
    assert summary["nets_joined_only_by_weak_connections"] == 1
    assert summary["nets_routed_here_unrouted_in_altium"] == 1
    assert result["nets_unrouted_in_altium"] == ["W"]
    note = summary["incomplete_connections_note"]
    assert "1 net routed here only through them reads un-routed" in note
    assert "findings show where" in note
    # Warnings come first (the info is the open track end past the join).
    assert [item["check"] for item in result["findings"]] == ["weak_connection", "antenna"]
    # checks still narrows; the summary and the list still name the net.
    narrowed = check_connectivity(index, checks=["unrouted"], nets=["W"])
    assert not _finding(narrowed, "weak_connection", "W")
    assert "Name weak in checks" in narrowed["summary"]["incomplete_connections_note"]
    assert narrowed["nets_unrouted_in_altium"] == ["W"]
    assert narrowed["nets"][0]["unrouted_in_altium"] is True
    page = page_result(narrowed, net_limit=0)
    assert page["nets_unrouted_in_altium"] == []
    assert page["nets_unrouted_in_altium_not_listed"] == 1


@pytest.mark.parametrize("rules", [
    [{"CHECKBADCONNECTIONS": "FALSE"}],
    [{"CHECKBADCONNECTIONS": "TRUE", "enabled": False}, {}],
    # The rule with the option does not apply to the net; the next one does.
    [{"CHECKBADCONNECTIONS": "TRUE", "scope": "InNetClass('Other')"}, {}],
    # A rule that applies to the net comes first.
    [{}, {"CHECKBADCONNECTIONS": "TRUE"}],
    # No rule applies to the net: Altium does not check it.
    [{"CHECKBADCONNECTIONS": "TRUE", "scope": "InNet('V')"}],
])
def test_weak_joins_stay_info_when_the_rule_that_applies_has_the_option_off(rules):
    board = _weak_board()
    for properties in rules:
        board.rule("UnRoutedNet", **properties)
    result = check_connectivity(board.index(), checks=["weak"])
    (weak,) = _finding(result, "weak_connection", "W")
    assert (weak["severity"], weak["incomplete_connections_checked"]) == ("info", False)
    assert result["summary"]["incomplete_connections_checked"] is False
    assert "incomplete_connections_note" not in result["summary"]
    assert check_connectivity(board.index())["checks_run"] == list(DEFAULT_CHECKS)


def test_option_on_for_some_nets_is_per_net():
    board = _weak_board(("W", "V"))
    board.rule("UnRoutedNet", scope="InNet('W')", CHECKBADCONNECTIONS="TRUE")
    board.rule("UnRoutedNet")
    result = check_connectivity(board.index())
    assert "weak" in result["checks_run"]
    (on,) = _finding(result, "weak_connection", "W")
    (off,) = _finding(result, "weak_connection", "V")
    assert (on["severity"], on["incomplete_connections_checked"]) == ("warning", True)
    assert (off["severity"], off["incomplete_connections_checked"]) == ("info", False)
    summary = result["summary"]
    assert summary["incomplete_connections_checked"] is None
    assert summary["nets_joined_only_by_weak_connections"] == 2
    assert summary["nets_routed_here_unrouted_in_altium"] == 1
    assert "applies to some of the nets checked" in summary["incomplete_connections_note"]
    narrowed = check_connectivity(board.index(), nets=["W"])
    assert narrowed["summary"]["incomplete_connections_checked"] is True


def test_option_in_a_rule_whose_scope_is_undecided_for_the_net_is_unknown():
    # OnLayer is not decided for a whole net: that rule may come first.
    board = _weak_board()
    board.rule("UnRoutedNet", scope="OnLayer('Top Layer')", CHECKBADCONNECTIONS="TRUE")
    board.rule("UnRoutedNet")
    result = check_connectivity(board.index())
    assert "weak" in result["checks_run"]
    (weak,) = _finding(result, "weak_connection", "W")
    assert (weak["severity"], weak["incomplete_connections_checked"]) == ("info", None)
    assert result["summary"]["incomplete_connections_checked"] is None
    # Only a net whose rule has the option on for certain reads un-routed in Altium.
    assert result["summary"]["nets_routed_here_unrouted_in_altium"] == 0
    assert result["nets_unrouted_in_altium"] == []
    assert "1 more may" in result["summary"]["incomplete_connections_note"]


def test_finding_names_select_their_checks():
    index = _filter_board()
    result = check_connectivity(index, checks=["isolated_copper", "antenna", "weak_connection"])
    assert result["checks_run"] == ["isolated", "antennae", "weak"]


def test_page_result():
    full = check_connectivity(_filter_board())
    assert full["summary"]["unrouted"] == 2
    first = page_result(full, offset=0, limit=1)
    assert len(first["findings"]) == 1 and first["has_more"] and first["next_offset"] == 1
    second = page_result(full, offset=1, limit=1)
    assert second["total"] == 2 and not second["has_more"] and "next_offset" not in second
    assert list(second)[-1] == "approximations"
    assert not any(key.startswith("_") for key in second)


def test_page_result_trims_connections_and_net_lists():
    board = Board(["BUS"])
    for number in range(6):
        board.pad("BUS", COMPONENTS[number], "1", number * 100.0, 0)
    full = check_connectivity(board.index(), nets=["BUS"])
    assert len(full["findings"][0]["connections"]) == 5
    page = page_result(full, connection_limit=2, net_limit=0)
    (finding,) = page["findings"]
    assert len(finding["connections"]) == 2 and finding["connections_not_listed"] == 3
    assert full["findings"][0]["missing_connections"] == 5
    assert page["nets"] == [] and page["nets_not_listed"] == 1 and "nets_hint" in page


def test_results_are_cached_per_board():
    index = _filter_board()
    board = connectivity(index)
    assert connectivity(index) is board
    first = board.net(index.net_id("A"))
    assert board.net(index.net_id("A")) is first


# ---------- Altium's DRC report ----------


def test_drc_comparison_lists_nets_each_side():
    index = _filter_board()
    full = check_connectivity(index)
    report = DrcReport(format="html", tables=[
        DrcTable(type=RULE, title="Un-Routed Net Constraint: ( (All) )", count=2, violations=[
            DrcViolation("Un-Routed Net Constraint: Net B Between Pad U1-2(0mil,100mil) "
                         "on Top Layer And Pad U2-2(100mil,100mil) on Top Layer"),
            DrcViolation("Un-Routed Net Constraint: Net C Between Pad U1-3(0mil,200mil) "
                         "on Top Layer And Pad U2-3(100mil,200mil) on Top Layer"),
        ]),
        DrcTable(type=RULE, title="Net Antennae (Tolerance=0mil) (All)", count=3),
    ])
    block = drc_comparison(report, full)
    assert block["unrouted"] == {
        "report_violations": 2,
        "report_missing_connections": 2,
        "report_other_rows": 0,
        "report_nets": 2,
        "missing_connections_here": 2,
        "nets_not_routed_here": 2,
        "report_nets_routed_here": ["C"],
        "not_routed_here_not_in_report": ["A"],
    }
    assert block["net_antennae"] == {
        "report_violations": 3, "report_checked_net_antennae": True, "enabled_rules": 0,
        "checked_by_batch_drc": None, "antennae_here": 0, "over_tolerance_here": 0,
        "without_enabled_rule_here": 0,
    }
    narrowed = drc_comparison(report, check_connectivity(index, nets=["A"]))
    assert narrowed["unrouted"]["report_nets_routed_here"] == []
    assert narrowed["unrouted"]["not_routed_here_not_in_report"] == ["A"]
    # The checks and layer filters narrow the findings, not the comparison.
    for filtered in (
        check_connectivity(index, checks=["antennae"]),
        check_connectivity(index, layer="Bottom Layer"),
    ):
        block = drc_comparison(report, filtered)
        assert block["unrouted"]["report_nets_routed_here"] == ["C"]
        assert block["unrouted"]["not_routed_here_not_in_report"] == ["A"]


def test_drc_comparison_counts_dead_copper_rows_apart():
    report = DrcReport(format="html", tables=[
        DrcTable(type=RULE, title="Un-Routed Net Constraint: ( (All) )", count=2, violations=[
            DrcViolation("Un-Routed Net Constraint: Net B Between Pad U1-2(0mil,100mil) "
                         "on Top Layer And Pad U2-2(100mil,100mil) on Top Layer"),
            DrcViolation("Un-Routed Net Constraint: Dead Copper - Net Not Assigned"),
        ]),
    ])
    block = drc_comparison(report, check_connectivity(_filter_board()))["unrouted"]
    assert (block["report_missing_connections"], block["report_other_rows"]) == (1, 1)
    assert block["report_nets"] == 1


@pytest.mark.parametrize("option", ["on", "unsaved", "undecided"])
def test_drc_comparison_counts_weak_only_nets_as_not_routed_when_checked(option):
    board = _weak_board(("W", "V"))
    board.pad("V", "U3", "9", 900, 900)  # V has a missing connection too
    if option == "undecided":
        board.rule("UnRoutedNet", scope="OnLayer('Top Layer')", CHECKBADCONNECTIONS="TRUE")
    board.rule("UnRoutedNet", **({"CHECKBADCONNECTIONS": "TRUE"} if option == "on" else {}))
    report = DrcReport(format="html", tables=[
        DrcTable(type=RULE, title="Un-Routed Net Constraint: ( (All) )", count=1, violations=[
            DrcViolation("Un-Routed Net Constraint: Net W Between Track (0mil,0mil)"
                         "(100mil,0mil) on Top Layer And Track (50mil,100mil)(50mil,3mil) "
                         "on Top Layer"),
        ]),
    ])
    full = check_connectivity(board.index())
    assert full["summary"]["connections"]["missing"] == 1  # V's far pad
    block = drc_comparison(report, full)["unrouted"]
    if option == "on":
        # W's join and V's join both count as missing; W reads un-routed.
        assert block["missing_connections_here"] == 3
        assert block["missing_connections_here_only_by_weak_connections"] == 2
        assert block["nets_not_routed_here"] == 2
        assert block["nets_not_routed_here_only_by_weak_connections"] == 1
        assert block["report_nets_routed_here"] == []
    else:
        assert block["missing_connections_here"] == 1
        assert block["nets_not_routed_here"] == 1
        assert block["report_nets_routed_here"] == ["W"]
        weak_keys = {key for key in block if "only_by_weak" in key}
        if option == "unsaved":
            assert not weak_keys
        else:  # the option may be on: said, but nothing counted
            assert block["nets_not_routed_here_only_by_weak_connections"] == 0
            assert block["missing_connections_here_only_by_weak_connections"] == 0
    assert block["not_routed_here_not_in_report"] == ["V"]


# ---------- parser: Regions6, Fills6, planes ----------


def _region_record(layer, net, pour, outline, holes=(), kind="0", flags2=0, extra=""):
    text = f"|V7_LAYER=TOP|KIND={kind}|SUBPOLYINDEX=0{extra}|".encode("latin-1") + b"\x00"
    payload = bytearray()
    payload += bytes([layer, 12, flags2])
    payload += struct.pack("<HHH", net, pour, 0xFFFF)
    payload += b"\xff\xff\xff\xff\x00"
    payload += struct.pack("<H", len(holes)) + b"\x00\x00"
    payload += struct.pack("<I", len(text)) + text
    for ring in (outline, *holes):
        payload += struct.pack("<I", len(ring))
        for x, y in ring:
            payload += struct.pack("<dd", x * 10000.0, y * 10000.0)
    return bytes([11]) + struct.pack("<I", len(payload)) + bytes(payload)


def _fill_record(layer, net, x1, y1, x2, y2, rotation=0.0, flags2=0):
    payload = bytearray(50)
    payload[0], payload[1], payload[2] = layer, 12, flags2
    struct.pack_into("<HHH", payload, 3, net, 0xFFFF, 0xFFFF)
    struct.pack_into("<iiii", payload, 13, *(int(v * 10000) for v in (x1, y1, x2, y2)))
    struct.pack_into("<d", payload, 29, rotation)
    return bytes([6]) + struct.pack("<I", len(payload)) + bytes(payload)


def test_decode_regions():
    square = _square(0, 0, 100, 50)
    data = (
        _region_record(1, 0xFFFF, 3, square, holes=[_square(10, 10, 20, 20)])
        + _region_record(32, 2, 0xFFFF, square)
        + _region_record(1, 2, 0xFFFF, square, kind="1")  # polygon cutout
        + _region_record(1, 2, 0xFFFF, square, flags2=2)  # keepout
        + _region_record(1, 2, 0xFFFF, square, extra="|ISBOARDCUTOUT=TRUE")
        + _region_record(33, 2, 0xFFFF, square)  # overlay, not copper
        + _region_record(1, 2, 0xFFFF, [(0, 0), (1, 1)])  # no area
        + _region_record(PLANE, 2, 0xFFFF, square)  # a void in a plane, not copper
    )
    areas, skipped = decode_regions(data + bytes([11]) + struct.pack("<I", 4) + b"\x01\x02\x03\x04")
    assert skipped == 1
    assert [(area.layer, area.net, area.pour_id) for area in areas] == [
        (1, None, 3), (32, 2, None),
    ]
    assert areas[0].outline == square and areas[0].holes == [_square(10, 10, 20, 20)]
    assert areas[0].kind == "region" and areas[0].component is None
    truncated = _region_record(1, 2, 0xFFFF, square)[:-8]
    record = bytes([11]) + struct.pack("<I", len(truncated) - 5) + truncated[5:]
    assert decode_regions(record) == ([], 1)
    # A record running past the stream, and vertices that are not coordinates.
    assert decode_regions(_region_record(1, 2, 0xFFFF, square)[:-3]) == ([], 1)
    for corrupt in (float("nan"), float("inf"), 1e9):
        bad = _region_record(1, 2, 0xFFFF, [(0, 0), (corrupt, 0), (0, 10)])
        assert decode_regions(bad) == ([], 1)


def test_decode_fills():
    data = (
        _fill_record(1, 4, 0, 0, 100, 20)
        + _fill_record(32, 0xFFFF, 0, 0, 100, 20, rotation=90.0)
        + _fill_record(1, 4, 0, 0, 100, 20, flags2=2)  # keepout
        + _fill_record(33, 4, 0, 0, 100, 20)  # not copper
        + _fill_record(PLANE, 4, 0, 0, 100, 20)  # a void in a plane
    )
    areas, skipped = decode_fills(data)
    assert skipped == 0
    assert [(area.kind, area.layer, area.net) for area in areas] == [
        ("fill", 1, 4), ("fill", 32, None),
    ]
    assert areas[0].outline == [(0, 0), (100, 0), (100, 20), (0, 20)]
    rotated = areas[1].outline
    assert rotated[0] == pytest.approx((60.0, -40.0))
    assert rotated[2] == pytest.approx((40.0, 60.0))


def test_both_region_decoders_read_the_same_records():
    # decode_regions (copper for connectivity) and decode_region_records (every
    # region, for the health check) read one record layout.
    square = _square(0, 0, 100, 50)
    data = (
        _region_record(1, 0xFFFF, 3, square, holes=[_square(10, 10, 20, 20)])
        + _region_record(32, 2, 0xFFFF, square)
        + _region_record(33, 2, 0xFFFF, square)  # overlay: a region, not copper
        + _region_record(1, 2, 0xFFFF, square, flags2=2)  # keepout
        + bytes([4]) + struct.pack("<I", 3) + b"\x00\x00\x00"  # not a region record
        + _region_record(1, 2, 0xFFFF, [(0, 0), (float("nan"), 0), (0, 10)])
        + _region_record(1, 2, 0xFFFF, square)[:-3]  # running past the stream
    )
    areas, area_skipped = decode_regions(data)
    regions, region_skipped = decode_region_records(data, False)
    assert (area_skipped, region_skipped) == (3, 3)
    assert [region.index for region in regions] == [0, 1, 2, 3]
    assert [(region.layer, region.keepout) for region in regions] == [
        (1, False), (32, False), (33, False), (1, True),
    ]
    assert [(a.layer, a.net, a.pour_id, a.component, a.outline, a.holes) for a in areas] == [
        (r.layer, r.net, r.polygon, r.component, [(v.x, v.y) for v in r.vertices], r.holes)
        for r in regions[:2]
    ]
    assert (regions[0].polygon, regions[0].net, regions[1].net) == (3, None, 2)


def test_both_fill_decoders_read_the_same_records():
    data = (
        _fill_record(1, 4, 0, 0, 100, 20)
        + _fill_record(32, 0xFFFF, 0, 0, 100, 20, rotation=90.0)
        + _fill_record(33, 4, 0, 0, 100, 20)  # not copper
        + _fill_record(1, 4, 0, 0, 100, 20, flags2=2)  # keepout
        + _fill_record(1, 4, 0, 0, 100, 20, rotation=float("nan"))
        + _fill_record(1, 4, 0, 0, 100, 20)[:-1]  # running past the stream
    )
    areas, area_skipped = decode_fills(data)
    fills, fill_skipped = decode_fill_records(data)
    assert (area_skipped, fill_skipped) == (1, 1)
    assert [(fill.layer, fill.net, fill.keepout, fill.rotation) for fill in fills] == [
        (1, 4, False, 0.0), (32, None, False, 90.0), (33, 4, False, 0.0), (1, 4, True, 0.0),
        (1, 4, False, 0.0),  # a rotation that is not finite reads 0 here, no copper there
    ]
    assert [(area.layer, area.net, area.component) for area in areas] == [
        (fill.layer, fill.net, fill.component) for fill in fills[:2]
    ]
    first = fills[0]
    assert areas[0].outline == [
        (first.x1, first.y1), (first.x2, first.y1), (first.x2, first.y2), (first.x1, first.y2),
    ]


def test_decode_board_reads_plane_nets_and_plane_aliases():
    board = decode_board([{
        "PLANE1NETNAME": "GND", "PLANE2NETNAME": "(No Net)", "PLANE16NETNAME": "VCC\r",
    }])
    assert board.plane_nets == {39: "GND", 54: "VCC"}
    aliases = _build_layer_aliases(board)
    assert aliases["plane1"] == 39 and aliases["plane16"] == 54


def test_parse_streams_resolves_pour_areas():
    def text(*records):
        return b"".join(
            struct.pack("<I", len(body)) + body
            for body in (record.encode("latin-1") + b"\x00" for record in records)
        )

    square = _square(0, 0, 100, 100)
    streams = {
        "Board6/Data": text("|LAYER1NAME=Top Layer|LAYER32NAME=Bottom Layer"),
        "Nets6/Data": text("|NAME=GND", "|NAME=VCC"),
        "Polygons6/Data": text(
            "|LAYER=TOP|NET=0|HATCHSTYLE=Solid|POLYGONTYPE=Polygon"
            "|VX0=0mil|VY0=0mil|VX1=100mil|VY1=0mil|VX2=100mil|VY2=100mil",
            "|LAYER=TOP|NET=1|HATCHSTYLE=Solid|SHELVED=TRUE"
            "|VX0=0mil|VY0=0mil|VX1=100mil|VY1=0mil|VX2=100mil|VY2=100mil",
        ),
        "Regions6/Data": (
            _region_record(1, 0xFFFF, 0, square) + _region_record(1, 0xFFFF, 1, square)
            + _region_record(1, 7, 0xFFFF, square)  # net past the net table
        ),
    }
    data = parse_streams(streams)
    assert data.area_streams == ("Regions6",)
    assert [(area.net, area.pour_id) for area in data.areas] == [(0, 0)]
    assert data.skipped_records["shelved_pour_copper"] == 1
    assert data.skipped_records["net_range"] == 1
    assert data.pours[0].polygon_type == "Polygon" and data.pours[1].polygon_type is None
    assert not any("Fills6" in warning or "Regions6" in warning for warning in data.warnings)
    broken = parse_streams({**streams, "Fills6/Data": None})
    assert any(warning.startswith("Fills6 parse failed") for warning in broken.warnings)
    # Copper that cannot all be read does not count as read.
    partial = parse_streams({
        **streams, "Regions6/Data": streams["Regions6/Data"] + b"\x0b\xff\xff\x00\x00",
    })
    assert partial.area_streams == ()
    assert [(area.net, area.pour_id) for area in partial.areas] == [(0, 0)]
    assert partial.skipped_records["Regions6"] == 1
    assert "Regions6: 1 records could not be read" in partial.warnings


def test_pour_layer_tokens_are_not_shadowed_by_layer_names():
    from server.parsers.pcb_doc import decode_polygons

    aliases = {"plane1": 2, "top layer": 1}  # a mid layer the user named 'Plane1'
    pours, _ = decode_polygons(
        [
            {"LAYER": "PLANE1", "NET": "0", "VX0": "0", "VY0": "0", "VX1": "1",
             "VY1": "0", "VX2": "1", "VY2": "1"},
            {"LAYER": "Top Layer", "NET": "0"},
            {"LAYER": "MID31", "NET": "0"},
        ],
        aliases,
    )
    assert [pour.layer for pour in pours] == [39, 1, None]


# ---------- MCP tool ----------


@pytest.fixture
def connectivity_tool(monkeypatch, tmp_path: Path):
    index = _filter_board()
    pcb_file = tmp_path / "Board.PcbDoc"
    pcb_file.write_bytes(b"fake")
    monkeypatch.setattr(main_mod, "_project", {
        "name": "Board",
        "root_dir": str(tmp_path),
        "prj_pcb_path": str(tmp_path / "Board.PrjPcb"),
        "sheets": [],
        "pcb_doc_paths": [str(pcb_file)],
    })
    monkeypatch.setattr(main_mod._altium, "_netlist", None)
    monkeypatch.setattr(main_mod, "_variant_state", VariantState([]))
    monkeypatch.setattr(main_mod._pcb_session, "_load", lambda path, netlist: index)
    main_mod._pcb_session._index = None
    return tmp_path


def test_tool_reports_and_converts_units(connectivity_tool):
    result = json.loads(main_mod.check_connectivity())
    assert result["pcb_doc"] == "Board.PcbDoc" and result["units"] == "mil"
    assert result["summary"]["unrouted"] == 2
    assert result["altium_drc"]["report"] is None
    in_mm = main_mod.check_connectivity(units="mm")
    assert "_mil" not in in_mm
    assert json.loads(in_mm)["findings"][0]["connections"][0]["distance_mm"] == 2.54
    assert json.loads(main_mod.check_connectivity(units="inch"))["error"] == "invalid_units"
    assert json.loads(main_mod.check_connectivity(net="NOPE"))["error"] == "net_not_found"
    without = json.loads(main_mod.check_connectivity(compare_drc=False))
    assert "altium_drc" not in without


def test_tool_compares_with_the_drc_report(connectivity_tool):
    outputs = connectivity_tool / "Project Outputs for Board"
    outputs.mkdir()
    (outputs / "Design Rule Check - Board.drc").write_text(
        "Protel Design System Design Rule Check\n"
        "PCB File : Board.PcbDoc\n"
        "Date     : 1/1/2026\n"
        "Time     : 10:00:00 AM\n\n"
        "Processing Rule : Un-Routed Net Constraint ( (All) )\n"
        "   Violation between Un-Routed Net Constraint: Net B Between Pad U1-2(0mil,100mil) "
        "on Top Layer And Pad U2-2(100mil,100mil) on Top Layer \n"
        "Rule Violations :1\n\n"
        "Violations Detected : 1\n",
        encoding="utf-8",
    )
    block = json.loads(main_mod.check_connectivity(net="A,B"))["altium_drc"]
    assert block["report"] == "Design Rule Check - Board.drc"
    assert block["unrouted"]["report_violations"] == 1
    assert block["unrouted"]["not_routed_here_not_in_report"] == ["A"]
    assert "whole board" in block["note"]


def test_tool_shrinks_findings_to_fit(connectivity_tool, monkeypatch):
    monkeypatch.setattr(main_mod, "QUERY_NET_MAX_RESULT_SIZE_CHARS", 100)
    result = json.loads(main_mod.check_connectivity())
    assert result["findings_limited_to"] == 2
    assert result["connections_per_finding_limited_to"] == 1
    assert result["total"] == 2 and result["summary"]["unrouted"] == 2


def test_tool_without_project(monkeypatch):
    monkeypatch.setattr(main_mod, "_project", None)
    assert json.loads(main_mod.check_connectivity())["error"] == "no_project"


def test_boards_parsed_before_areas_still_check():
    # PcbData from before areas existed (no areas, no area_streams) still checks.
    index = _filter_board()
    index.pcb.__dict__.pop("areas")
    index.pcb.__dict__.pop("area_streams")
    result = check_connectivity(index)
    assert result["copper_model"]["regions_read"] is False
    assert result["summary"]["unrouted"] == 2
