"""Length engine (pad-to-pad paths, via barrels, xSignals) and length rules."""

import math
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from server.parsers.pcb_doc import (
    PAD_INNER_LAYERS,
    PAD_SHAPE_RECT,
    PAD_SHAPE_ROUND,
    BoardInfo,
    DiffPair,
    PadCopper,
    PcbClass,
    PcbComponent,
    PcbData,
    PcbLayer,
    PcbRule,
    PolygonPour,
    Primitive,
    XSignal,
)
from server.services import pcb_length
from server.services.pcb_index import PcbIndex
from server.services.pcb_length import (
    NetGraph,
    check_length_rules,
    net_graph,
    net_length,
    pad_to_pad,
    pair_lengths,
    stack_z,
    xsignal_length,
)

MULTI = 74
NETS = [
    "STRAIGHT", "WIGGLE", "VIA_L3", "VIA_FULL", "TEE", "GAP", "ARC",
    "SER_A", "SER_B", "G100", "G97", "G90", "DP_P", "DP_N", "FLY", "VIP",
    "DIAG", "NOCOPPER",
]
N = {name: number for number, name in enumerate(NETS)}
COMPONENTS = ["U1", "U2", "U3", "R1", "J1"]
C = {name: number for number, name in enumerate(COMPONENTS)}

# z of copper centres: Top 0.7, L2 7.0, L3 48.2, Bottom 54.5.
Z_TOP_TO_L3 = 47.5
Z_TOP_TO_BOTTOM = 53.8


def _layer(v6, name, order, kind="copper", copper=None, dielectric=None):
    return PcbLayer(
        v6_id=v6, name=name, kind=kind, stack_order=order,
        copper_thick_mil=copper, diel_height_mil=dielectric,
    )


def _board():
    return BoardInfo(
        origin_x=0.0, origin_y=0.0, sheet_x=0.0, sheet_y=0.0,
        sheet_width=5000.0, sheet_height=5000.0, display_unit="mil",
        stackup_source="v9",
        layers=[
            _layer(1, "Top Layer", 0, copper=1.4),
            _layer(None, "Dielectric 1", 1, kind="dielectric", dielectric=5.0),
            _layer(2, "L2", 2, copper=1.2),
            _layer(None, "Dielectric 2", 3, kind="dielectric", dielectric=40.0),
            _layer(3, "L3", 4, copper=1.2),
            _layer(None, "Dielectric 3", 5, kind="dielectric", dielectric=5.0),
            _layer(32, "Bottom Layer", 6, copper=1.4),
        ],
    )


def _track(net, x1, y1, x2, y2, layer=1, width=4.0, length=None, kind="track"):
    return Primitive(
        kind=kind, layer=layer, net=N[net], component=None,
        x1=x1, y1=y1, x2=x2, y2=y2, width=width, length=length,
    )


def _pad(net, component, name, x, y, layer=1, size=10.0, shape=2, hole=0.0):
    return Primitive(
        kind="pad", layer=layer, net=N[net], component=C[component],
        x1=x, y1=y, x2=x, y2=y, width=size, height=size, shape=shape,
        hole=hole, pad_name=name,
    )


def _via(net, x, y, layer_from=1, layer_to=32, diameter=20.0):
    return Primitive(
        kind="via", layer=layer_from, layer_to=layer_to, net=N[net],
        component=None, x1=x, y1=y, x2=x, y2=y, width=diameter, hole=10.0,
    )


def _arc_chords(net, cx, cy, radius, start_deg, end_deg, steps):
    sweep = end_deg - start_deg
    true = radius * math.radians(sweep) / steps
    points = [
        (
            cx + radius * math.cos(math.radians(start_deg + sweep * k / steps)),
            cy + radius * math.sin(math.radians(start_deg + sweep * k / steps)),
        )
        for k in range(steps + 1)
    ]
    return [
        _track(net, a[0], a[1], b[0], b[1], width=2.0, length=true, kind="arc")
        for a, b in zip(points, points[1:])
    ]


def _straight_net(net, y, length, pin):
    return [
        _pad(net, "U1", pin, 0.0, y),
        _pad(net, "U2", pin, length, y),
        _track(net, 0.0, y, length, y),
    ]


def _primitives():
    prims = []
    prims += _straight_net("STRAIGHT", 0.0, 1000.0, "1")
    # In-pad wiggle: 10 extra mil of copper inside the 40 mil pad.
    prims += [
        _pad("WIGGLE", "U1", "2", 0.0, 100.0, size=40.0),
        _pad("WIGGLE", "U2", "2", 1000.0, 100.0),
        _track("WIGGLE", 0.0, 100.0, 0.0, 105.0),
        _track("WIGGLE", 0.0, 105.0, 10.0, 105.0),
        _track("WIGGLE", 10.0, 105.0, 10.0, 100.0),
        _track("WIGGLE", 10.0, 100.0, 1000.0, 100.0),
    ]
    # Top -> through via -> L3 -> through-hole pad: barrel only Top..L3.
    prims += [
        _pad("VIA_L3", "U1", "3", 0.0, 200.0),
        _track("VIA_L3", 0.0, 200.0, 100.0, 200.0),
        _via("VIA_L3", 100.0, 200.0),
        _track("VIA_L3", 100.0, 200.0, 300.0, 200.0, layer=3),
        _pad("VIA_L3", "J1", "1", 300.0, 200.0, layer=MULTI, size=30.0, shape=1, hole=15.0),
    ]
    # Top -> through via -> Bottom SMD pad: the full span.
    prims += [
        _pad("VIA_FULL", "U1", "4", 0.0, 300.0),
        _track("VIA_FULL", 0.0, 300.0, 100.0, 300.0),
        _via("VIA_FULL", 100.0, 300.0),
        _track("VIA_FULL", 100.0, 300.0, 300.0, 300.0, layer=32),
        _pad("VIA_FULL", "U3", "1", 300.0, 300.0, layer=32),
    ]
    # One long segment with a stub branching from its middle (T-junction).
    prims += [
        _pad("TEE", "U1", "5", 0.0, 400.0),
        _pad("TEE", "U2", "5", 1000.0, 400.0),
        _pad("TEE", "U3", "5", 500.0, 700.0),
        _track("TEE", 0.0, 400.0, 1000.0, 400.0),
        _track("TEE", 500.0, 400.0, 500.0, 700.0),
    ]
    # Unrouted middle: 200 mil in X and 30 mil in Y missing.
    prims += [
        _pad("GAP", "U1", "6", 0.0, 800.0),
        _pad("GAP", "U2", "6", 1000.0, 830.0),
        _track("GAP", 0.0, 800.0, 400.0, 800.0),
        _track("GAP", 600.0, 830.0, 1000.0, 830.0),
    ]
    # A 90 degree r = 20 arc stored as 3 chords carrying the true length.
    prims += [
        _pad("ARC", "U1", "7", 0.0, 900.0, size=4.0),
        _pad("ARC", "U2", "7", 20.0, 920.0, size=4.0),
        *_arc_chords("ARC", 0.0, 920.0, 20.0, -90.0, 0.0, 3),
    ]
    # Series resistor R1 between SER_A and SER_B.
    prims += [
        _pad("SER_A", "U1", "8", 0.0, 1000.0),
        _pad("SER_A", "R1", "1", 200.0, 1000.0),
        _track("SER_A", 0.0, 1000.0, 200.0, 1000.0),
        _pad("SER_B", "R1", "2", 240.0, 1000.0),
        _pad("SER_B", "U2", "8", 540.0, 1000.0),
        _track("SER_B", 240.0, 1000.0, 540.0, 1000.0),
    ]
    prims += _straight_net("G100", 1100.0, 100.0, "9")
    prims += _straight_net("G97", 1200.0, 97.0, "10")
    prims += _straight_net("G90", 1300.0, 90.0, "11")
    prims += _straight_net("DP_P", 1400.0, 500.0, "12")
    prims += _straight_net("DP_N", 1450.0, 494.5, "13")
    # A track running straight over a pad (no endpoint in it).
    prims += [
        _pad("FLY", "U1", "14", 0.0, 1500.0),
        _pad("FLY", "U2", "14", 1000.0, 1500.0),
        _pad("FLY", "U3", "14", 500.0, 1500.0, size=20.0),
        _track("FLY", 0.0, 1500.0, 1000.0, 1500.0),
    ]
    # Blind vias (Top..L3) in SMD pads, routed on L3.
    prims += [
        _pad("VIP", "U1", "15", 0.0, 1600.0, size=20.0),
        _via("VIP", 0.0, 1600.0, layer_to=3),
        _track("VIP", 0.0, 1600.0, 200.0, 1600.0, layer=3),
        _via("VIP", 200.0, 1600.0, layer_to=3),
        _pad("VIP", "U2", "15", 200.0, 1600.0, size=20.0),
    ]
    # A long diagonal with a branch landing mid-way (grid-hashed split).
    prims += [
        _pad("DIAG", "U1", "16", 0.0, 2000.0),
        _pad("DIAG", "U2", "16", 3000.0, 5000.0),
        _pad("DIAG", "U3", "16", 1500.0, 3700.0),
        _track("DIAG", 0.0, 2000.0, 3000.0, 5000.0),
        _track("DIAG", 1500.0, 3500.0, 1500.0, 3700.0),
    ]
    prims += [
        _pad("NOCOPPER", "U1", "17", 0.0, 2100.0),
        _pad("NOCOPPER", "U2", "17", 400.0, 2100.0),
    ]
    return prims


def _rule(name, kind, scope, priority=1, enabled=True, **props):
    return PcbRule(
        name=name, kind=kind, kind_id=3 if kind == "Length" else 4,
        enabled=enabled, priority=priority, scope1=scope, scope2="All",
        net_scope="AnyNet", comment="", unique_id=None, from_schematic=False,
        properties=props,
    )


def _matched(name, scope, tolerance="5mil", within=False, priority=1, **props):
    group = "FALSE" if within else "TRUE"
    return _rule(
        name, "MatchedLengths", scope, priority=priority,
        TOLERANCE=tolerance,
        CHECKNETSINDIFFPAIR="TRUE" if within else "FALSE",
        CHECKDIFFPAIRVSDIFFPAIR=group, CHECKXSIGNALS=group, CHECKOTHERS=group,
        **props,
    )


def _class(name, kind, members):
    return PcbClass(
        name=name, kind=kind, members=list(members), superclass=False,
        auto_generated=False, unique_id=None,
    )


@pytest.fixture
def length_data():
    return PcbData(
        board=_board(),
        nets=list(NETS),
        components=[
            PcbComponent(
                index=index, refdes=name, source_uid_tail=None,
                channel_path=None, x=0.0, y=0.0, rotation=0.0, side="top",
                designator=name,
            )
            for index, name in enumerate(COMPONENTS)
        ],
        primitives=_primitives(),
        pours=[],
        layer_name_to_v6={
            "top layer": 1, "l2": 2, "l3": 3, "bottom layer": 32,
            "top": 1, "bottom": 32,
        },
        xsignals=[
            XSignal("X100", ["U1-9", "U2-9"]),
            XSignal("X97", ["U1-10", "U2-10"]),
            XSignal("X90", ["U1-11", "U2-11"]),
            XSignal("XSER", ["U1-8", "R1-1", "R1-2", "U2-8"]),
        ],
        classes=[
            _class("XG", "xsignal", ["X100", "X97", "X90"]),
            _class("GN", "net", ["G100", "G97", "G90"]),
            _class("PC", "diff_pair", ["DP"]),
        ],
        diff_pairs=[DiffPair("DP", "DP_P", "DP_N", None)],
        drc_rule_kinds={3, 4},
    )


def _index(data, rules=()):
    data = deepcopy(data)
    data.rules = list(rules)
    return PcbIndex(data, None)


@pytest.fixture
def index(length_data):
    return _index(length_data)


def _floats_outside_mil_keys(value, inside_mil=False, path="$"):
    """Paths of float values that _convert_units would not convert."""
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            found += _floats_outside_mil_keys(
                item, inside_mil or key.endswith("_mil"), f"{path}.{key}"
            )
    elif isinstance(value, list):
        for position, item in enumerate(value):
            found += _floats_outside_mil_keys(item, inside_mil, f"{path}[{position}]")
    elif isinstance(value, float) and not inside_mil:
        found.append(path)
    return found


# ---------- stackup ----------


def test_stack_z_copper_centres(index):
    z = stack_z(index)
    assert z == pytest.approx({1: 0.7, 2: 7.0, 3: 48.2, 32: 54.5})


def test_stack_z_none_when_a_thickness_is_missing(length_data):
    length_data.board.layers[3].diel_height_mil = None
    index = _index(length_data)
    assert stack_z(index) is None
    result = pad_to_pad(index, "VIA_L3", "U1-3", "J1-1")
    assert result["status"] == "ok"
    assert result["via_barrel_mil"] is None
    assert result["length_mil"] == pytest.approx(300.0)
    rules = [_matched("M", "InNetClass('GN')")]
    report = check_length_rules(_index(length_data, rules))
    assert any("barrel" in item for item in report["unknown"])


def test_stack_z_none_for_legacy_stackup(length_data):
    for layer in length_data.board.layers:
        layer.stack_order = None
    assert stack_z(_index(length_data)) is None


# ---------- paths ----------


def test_straight_two_pad_net_path_equals_routed(index):
    result = net_length(index, "STRAIGHT")
    assert result["measure"] == "signal_path"
    assert result["length_mil"] == pytest.approx(1000.0)
    assert result["routed_mil"] == pytest.approx(1000.0)
    assert result["status"] == "ok"
    assert result["pads"] == ["U1-1", "U2-1"]


def test_in_pad_wiggle_is_ignored(index):
    result = net_length(index, "WIGGLE")
    assert result["routed_mil"] == pytest.approx(1010.0)
    assert result["length_mil"] == pytest.approx(1000.0)


def test_via_barrel_counts_only_the_traversed_span(index):
    result = pad_to_pad(index, "VIA_L3", "U1-3", "J1-1")
    assert result["via_barrel_mil"] == pytest.approx(Z_TOP_TO_L3)
    assert result["copper_mil"] == pytest.approx(300.0)
    assert result["length_mil"] == pytest.approx(300.0 + Z_TOP_TO_L3)
    assert result["vias"] == 1
    assert result["layers_mil"] == {"Top Layer": 100.0, "L3": 200.0}


def test_full_via_traversal_is_layers_between_plus_half_outer_copper(index):
    result = pad_to_pad(index, None, "U1-4", "U3-1")
    # 5 + 1.2 + 40 + 1.2 + 5 between, plus 0.7 + 0.7 of the outer copper.
    assert result["via_barrel_mil"] == pytest.approx(Z_TOP_TO_BOTTOM)
    assert result["net"] == "VIA_FULL"


def test_t_junction_stub_is_excluded_from_the_path(index):
    assert pad_to_pad(index, "TEE", "U1-5", "U2-5")["length_mil"] == pytest.approx(1000.0)
    assert pad_to_pad(index, "TEE", "U1-5", "U3-5")["length_mil"] == pytest.approx(800.0)
    whole = net_length(index, "TEE")
    assert whole["measure"] == "routed_plus_vias"
    assert whole["pad_count"] == 3
    assert whole["length_mil"] == pytest.approx(1300.0)


def test_t_junction_found_on_a_long_diagonal(index):
    # The branch lands 2121 mil along a 4243 mil diagonal.
    result = pad_to_pad(index, "DIAG", "U1-16", "U3-16")
    assert result["status"] == "ok"
    assert result["length_mil"] == pytest.approx(1500.0 * math.sqrt(2) + 200.0)


def test_no_path_reports_unrouted_manhattan(index):
    result = pad_to_pad(index, "GAP", "U1-6", "U2-6")
    assert result["status"] == "no_path"
    assert result["length_mil"] is None
    assert result["unrouted_manhattan_mil"] == pytest.approx(230.0)
    whole = net_length(index, "GAP")
    assert whole["status"] == "incomplete"
    assert whole["unrouted_manhattan_mil"] == pytest.approx(230.0)


def test_hatched_pour_copper_is_not_in_the_graph(length_data):
    # GAP's hatched pour bridges its gap with tracks: copper, not routing, so
    # the path stays open, the routed length is unchanged, and the pour
    # (unless shelved: no copper) may complete the connection.
    for shelved in (False, True):
        data = deepcopy(length_data)
        data.pours = [PolygonPour(
            0, N["GAP"], 1, "Horizontal",
            [(350.0, 750.0), (650.0, 750.0), (650.0, 880.0), (350.0, 880.0)],
            shelved=shelved,
        )]
        data.primitives += [
            replace(_track("GAP", *ends), poured_by=0)
            for ends in ((400.0, 800.0, 600.0, 800.0), (600.0, 800.0, 600.0, 830.0))
        ]
        index = _index(data)
        assert pad_to_pad(index, "GAP", "U1-6", "U2-6")["status"] == "no_path"
        whole = net_length(index, "GAP")
        assert whole["status"] == "incomplete"
        assert whole["routed_mil"] == pytest.approx(800.0)
        assert ("note" in whole) is not shelved


def test_net_without_copper_is_unrouted(index):
    result = net_length(index, "NOCOPPER")
    assert result["status"] == "unrouted"
    assert result["unrouted_manhattan_mil"] == pytest.approx(400.0)


def test_arc_uses_true_length_not_chords(index):
    result = pad_to_pad(index, "ARC", "U1-7", "U2-7")
    assert result["length_mil"] == pytest.approx(10.0 * math.pi, abs=0.01)
    assert net_length(index, "ARC")["routed_mil"] == pytest.approx(31.42, abs=0.01)


def test_track_running_over_a_pad_connects_it(index):
    assert pad_to_pad(index, "FLY", "U1-14", "U3-14")["length_mil"] == pytest.approx(500.0)
    assert pad_to_pad(index, "FLY", "U3-14", "U2-14")["length_mil"] == pytest.approx(500.0)


def test_rounded_rectangle_pad_contains_keeps_its_corners():
    pad = {"x": 0.0, "y": 0.0, "w": 40.0, "h": 60.0, "rot": 0.0, "shape": 9,
           "corner": 10.0}
    # Inside the sharp corner, 3.4 mil outside the rounded one.
    assert not pcb_length.pad_contains(pad, 19.5, 29.5, 0.0)
    assert pcb_length.pad_contains(pad, 19.5, 29.5, 3.5)
    assert pcb_length.pad_contains(pad, 19.5, 0.0, 0.0)
    assert pcb_length.pad_contains(pad, 21.0, 0.0, 1.0)
    assert not pcb_length.pad_contains(pad, 21.0, 0.0, 0.5)
    turned = {**pad, "rot": 90.0}
    assert pcb_length.pad_contains(turned, 29.5, 0.0, 0.0)
    assert not pcb_length.pad_contains(turned, 29.5, 19.5, 0.0)
    # Without a corner radius it is the rectangle; past half the short side,
    # the oblong.
    assert pcb_length.pad_contains({**pad, "corner": None}, 19.5, 29.5, 0.0)
    oblong = {**pad, "shape": 1}
    for x, y in [(19.0, 15.0), (19.0, 21.0), (5.0, 29.0), (0.0, 30.5)]:
        assert pcb_length.pad_contains(
            {**pad, "corner": 35.0}, x, y, 0.5
        ) == pcb_length.pad_contains(oblong, x, y, 0.5)


def test_track_end_in_a_rounded_pad_corner_does_not_connect(length_data):
    data = deepcopy(length_data)
    data.nets.append("CORNER")
    net = len(data.nets) - 1
    pad = _pad("STRAIGHT", "U1", "20", 0.0, 2000.0, size=40.0, shape=9)
    pad.net, pad.height, pad.corner_radius = net, 60.0, 10.0
    far = _pad("STRAIGHT", "U2", "20", 1000.0, 2000.0)
    far.net = net
    # Ends 3.4 mil outside the rounded corner (inside the sharp one).
    track = _track("STRAIGHT", 19.5, 2029.5, 1000.0, 2000.0, width=0.5)
    track.net = net
    data.primitives += [pad, far, track]
    assert pad_to_pad(_index(data), "CORNER", "U1-20", "U2-20")["status"] != "ok"
    track.x1, track.y1 = 15.0, 2025.0
    assert pad_to_pad(_index(data), "CORNER", "U1-20", "U2-20")["status"] == "ok"


def test_pad_contains_uses_the_copper_on_the_layer():
    pad = {"x": 0.0, "y": 0.0, "w": 20.0, "h": 20.0, "rot": 0.0, "shape": 1,
           "copper": {PAD_INNER_LAYERS: PadCopper(60.0, 60.0, PAD_SHAPE_ROUND),
                      32: PadCopper(30.0, 80.0, PAD_SHAPE_RECT)}}
    assert not pcb_length.pad_contains(pad, 25.0, 0.0, 0.0)
    assert not pcb_length.pad_contains(pad, 25.0, 0.0, 0.0, 1)
    assert pcb_length.pad_contains(pad, 25.0, 0.0, 0.0, 2)
    assert not pcb_length.pad_contains(pad, 25.0, 0.0, 0.0, 32)
    assert pcb_length.pad_contains(pad, 0.0, 35.0, 0.0, 32)


def test_through_hole_pad_connects_with_its_copper_on_the_track_layer(length_data):
    data = deepcopy(length_data)
    data.nets.append("STACK")
    net = len(data.nets) - 1
    near = _pad("STRAIGHT", "U1", "30", 0.0, 3000.0, layer=MULTI, size=20.0,
                shape=1, hole=10.0)
    near.layer_copper = {PAD_INNER_LAYERS: PadCopper(60.0, 60.0, PAD_SHAPE_ROUND)}
    far = _pad("STRAIGHT", "U2", "30", 1000.0, 3000.0, layer=MULTI, hole=5.0)
    # On L2 the track ends 25 mil from the centre: inside the 60 mil inner
    # copper, outside the 20 mil top-layer size.
    track = _track("STRAIGHT", 25.0, 3000.0, 1000.0, 3000.0, layer=2)
    for primitive in (near, far, track):
        primitive.net = net
    data.primitives += [near, far, track]
    assert pad_to_pad(_index(data), "STACK", "U1-30", "U2-30")["status"] == "ok"
    # The other way round (60 mil on top, 20 mil inside) it misses.
    near.width = near.height = 60.0
    near.layer_copper = {PAD_INNER_LAYERS: PadCopper(20.0, 20.0, PAD_SHAPE_ROUND)}
    assert pad_to_pad(_index(data), "STACK", "U1-30", "U2-30")["status"] != "ok"


def test_via_in_pad_connects_without_a_track(index):
    result = pad_to_pad(index, "VIP", "U1-15", "U2-15")
    assert result["status"] == "ok"
    assert result["vias"] == 2
    assert result["via_barrel_mil"] == pytest.approx(2 * Z_TOP_TO_L3)
    assert result["length_mil"] == pytest.approx(200.0 + 2 * Z_TOP_TO_L3)


def test_routed_plus_vias_counts_each_via_over_its_connected_span(length_data):
    # A third pad turns VIA_L3 into a multi-pad net.
    length_data.primitives.append(_pad("VIA_L3", "U2", "99", 0.0, 260.0))
    length_data.primitives.append(_track("VIA_L3", 0.0, 200.0, 0.0, 260.0))
    result = net_length(_index(length_data), "VIA_L3")
    assert result["measure"] == "routed_plus_vias"
    assert result["via_barrel_mil"] == pytest.approx(Z_TOP_TO_L3)
    assert result["length_mil"] == pytest.approx(360.0 + Z_TOP_TO_L3)
    assert result["vias"] == 1


def test_net_graph_is_cached_on_the_index(index):
    first = net_graph(index, index.net_id("STRAIGHT"))
    assert net_graph(index, index.net_id("STRAIGHT")) is first


# ---------- xSignals and pairs ----------


def test_xsignal_series_hop_counts_zero(index):
    result = xsignal_length(index, "xser")
    assert result["status"] == "ok"
    assert result["length_mil"] == pytest.approx(500.0)
    assert result["series_hops"] == ["R1"]
    assert [leg["net"] for leg in result["legs"]] == ["SER_A", "SER_B"]
    assert [leg["length_mil"] for leg in result["legs"]] == [200.0, 300.0]


def test_xsignal_pin_not_found_and_broken_chain(length_data):
    length_data.xsignals += [
        XSignal("XMISSING", ["U1-1", "U9-1"]),
        XSignal("XBROKEN", ["U1-1", "U2-9"]),
    ]
    index = _index(length_data)
    missing = xsignal_length(index, "XMISSING")
    assert missing["status"] == "pin_not_found"
    assert missing["missing_pins"] == ["U9-1"]
    assert missing["length_mil"] is None
    broken = xsignal_length(index, "XBROKEN")
    assert broken["status"] == "broken_chain"
    assert broken["broken_between"] == ["U1-1", "U2-9"]


def test_pair_lengths(index):
    result = pair_lengths(index, "dp")
    assert result["positive"]["length_mil"] == pytest.approx(500.0)
    assert result["negative"]["length_mil"] == pytest.approx(494.5)
    # The pair length is the P/N average (Altium's DRC); max_mil the longer leg.
    assert result["length_mil"] == pytest.approx(497.25)
    assert result["max_mil"] == pytest.approx(500.0)
    assert "avg_mil" not in result
    assert "average" in result["length_basis"]
    assert result["skew_mil"] == pytest.approx(5.5)
    assert result["status"] == "ok"


def test_lookup_errors(index):
    assert net_length(index, "NOPE")["error"] == "net_not_found"
    assert xsignal_length(index, "X1")["error"] == "xsignal_not_found"
    assert pair_lengths(index, "NOPE")["error"] == "pair_not_found"
    assert pad_to_pad(index, None, "U1-1", "U9-9")["error"] == "pin_not_found"
    assert pad_to_pad(index, "STRAIGHT", "U1-1", "U2-2")["error"] == "pin_not_on_net"
    assert pad_to_pad(index, "NOPE", "U1-1", "U2-1")["error"] == "net_not_found"


# ---------- rules ----------


def _only_rule(report):
    assert report["rule_count"] == 1
    return report["rules"][0]


def _by_object(rows):
    return {row["object"]: row for row in rows}


def _group(rule, section):
    return rule["groups"][section]


def test_group_reference_is_the_longest_member(length_data):
    index = _index(length_data, [_matched("ML", "InxSignalClass('XG')")])
    rule = _only_rule(check_length_rules(index, rule="ml"))
    assert rule["mode"] == "group"
    assert rule["member_kinds"] == ["xsignal"]
    assert set(rule["groups"]) == {"xsignals"}
    group = _group(rule, "xsignals")
    assert group["reference"]["object"] == "X100"
    assert group["reference"]["source"] == "longest_member"
    assert group["reference"]["band_min_mil"] == pytest.approx(95.0)
    rows = _by_object(group["members"])
    assert rows["X90"]["status"] == "too_short"
    assert rows["X90"]["delta_to_target_mil"] == pytest.approx(-10.0)
    assert rows["X90"]["outside_band_mil"] == pytest.approx(5.0)
    assert rows["X97"]["status"] == "ok"
    assert rule["summary"]["violations"] == 1
    assert group["summary"]["spread_mil"] == pytest.approx(10.0)
    # Problems first.
    assert group["members"][0]["object"] == "X90"


def test_group_target_source_band(length_data):
    rules = [_matched("ML", "InxSignalClass('XG')", TARGETSOURCENAME="X97")]
    rule = _only_rule(check_length_rules(_index(length_data, rules), rule="ML"))
    group = _group(rule, "xsignals")
    assert group["reference"]["source"] == "target_source"
    assert group["reference"]["band_min_mil"] == pytest.approx(92.0)
    assert group["reference"]["band_max_mil"] == pytest.approx(102.0)
    rows = _by_object(group["members"])
    assert rows["X90"]["status"] == "too_short"
    assert rows["X100"]["status"] == "ok"


def test_missing_target_source_falls_back_to_longest(length_data):
    rules = [_matched("ML", "InxSignalClass('XG')", TARGETSOURCENAME="GONE")]
    report = check_length_rules(_index(length_data, rules), rule="ML")
    rule = _only_rule(report)
    assert _group(rule, "xsignals")["reference"]["source"] == "longest_member"
    assert "targetsource('GONE')" in rule["stale_references"]
    assert any("GONE" in item for item in report["unknown"])


def test_group_of_nets_and_pairs(length_data):
    rules = [_matched("ML", "InNetClass('GN') or InDifferentialPairClass('PC')")]
    rule = _only_rule(check_length_rules(_index(length_data, rules), rule="ML"))
    assert rule["member_kinds"] == ["diff_pair", "net"]
    assert set(rule["groups"]) == {"diff_pairs", "nets"}
    # Nets are checked against the longest net, the pair's own legs included.
    nets = _group(rule, "nets")
    rows = _by_object(nets["members"])
    assert set(rows) == {"DP_P", "DP_N", "G100", "G97", "G90"}
    assert nets["reference"]["object"] == "DP_P"
    assert rows["DP_N"]["status"] == "too_short"
    assert rows["G100"]["status"] == "too_short"
    # Pairs are checked against the longest pair, by their P/N average.
    pairs = _group(rule, "diff_pairs")
    pair = pairs["members"][0]
    assert pair["object"] == "DP"
    assert pair["length_mil"] == pytest.approx(497.25)
    assert pair["max_mil"] == pytest.approx(500.0)
    assert pairs["reference"]["object"] == "DP"
    assert pair["status"] == "ok"
    # The rule summary adds the sections up.
    assert rule["summary"]["members"] == 6
    assert rule["summary"]["violations"] == (
        nets["summary"]["violations"] + pairs["summary"]["violations"]
    ) == 4


def test_within_pair_skew(length_data):
    rules = [_matched("WP", "InDifferentialPairClass('PC')", within=True)]
    rule = _only_rule(check_length_rules(_index(length_data, rules), rule="WP"))
    assert rule["mode"] == "within_pair"
    pair = rule["pairs"][0]
    assert pair["skew_mil"] == pytest.approx(5.5)
    assert pair["status"] == "skew_exceeded"
    assert pair["outside_band_mil"] == pytest.approx(0.5)
    assert pair["marginal"] is True
    assert rule["pair_summary"]["violations"] == 1


def test_delay_units_are_not_evaluated(length_data):
    rules = [
        _rule("LD", "Length", "InNet('STRAIGHT')", USEDELAYUNITS="TRUE",
              MINDELAY="100", MAXDELAY="200"),
    ]
    report = check_length_rules(_index(length_data, rules))
    rule = _only_rule(report)
    assert rule["status"] == "not_evaluated_delay_units"
    assert rule["member_count"] == 1
    assert any("delay" in item for item in report["unknown"])


def test_stale_scope_is_reported(length_data):
    rules = [_matched("ML", "InxSignalClass('Gone') or InNetClass('Old')")]
    rule = _only_rule(check_length_rules(_index(length_data, rules)))
    assert rule["status"] == "no_members"
    assert rule["stale_references"] == [
        "inxsignalclass('Gone')", "innetclass('Old')",
    ]


def test_length_rule_range_and_priority(length_data):
    rules = [
        _rule("L1", "Length", "InNet('STRAIGHT')", priority=1,
              MINLIMIT="900mil", MAXLIMIT="950mil"),
        _rule("L2", "Length", "InNetClass('GN') or InNet('STRAIGHT')",
              priority=2, MINLIMIT="95mil", MAXLIMIT="2000mil"),
        _rule("LX", "Length", "InxSignal('X100')", priority=3,
              MINLIMIT="99mil", MAXLIMIT="101mil"),
    ]
    report = check_length_rules(_index(length_data, rules), problems_only=False)
    rules_out = {rule["name"]: rule for rule in report["rules"]}
    straight = rules_out["L1"]["members"][0]
    assert straight["status"] == "too_long"
    assert straight["outside_band_mil"] == pytest.approx(50.0)
    second = rules_out["L2"]
    assert second["overridden"]["objects"] == [
        {"object": "STRAIGHT", "kind": "net", "by_rule": "L1"}
    ]
    assert set(_by_object(second["members"])) == {"G100", "G97", "G90"}
    assert _by_object(second["members"])["G90"]["status"] == "too_short"
    xsig = rules_out["LX"]
    assert xsig["member_kinds"] == ["xsignal"]
    assert xsig["members"][0]["status"] == "ok"


def test_pagination_and_problem_listing(length_data):
    index = _index(length_data, [_matched("ML", "InxSignalClass('XG')")])
    overview = _only_rule(check_length_rules(index))
    assert overview["listing"] == "problems_only"
    assert [row["object"] for row in _group(overview, "xsignals")["members"]] == ["X90"]
    assert overview["summary"]["members"] == 3
    first = _group(_only_rule(check_length_rules(index, rule="ML", limit=2)), "xsignals")
    assert first["members_page"] == {
        "total": 3, "offset": 0, "has_more": True, "next_offset": 2,
    }
    second = _group(
        _only_rule(check_length_rules(index, rule="ML", offset=2, limit=2)), "xsignals"
    )
    assert len(second["members"]) == 1
    assert second["members_page"]["has_more"] is False


def test_target_filter(length_data):
    rules = [
        _matched("ML", "InxSignalClass('XG')"),
        _matched("WP", "InDifferentialPairClass('PC')", within=True),
    ]
    index = _index(length_data, rules)
    by_net = check_length_rules(index, target="G97")
    assert by_net["target"] == {"name": "G97", "kind": "net"}
    rule = _only_rule(by_net)
    assert rule["name"] == "ML"
    assert [row["object"] for row in _group(rule, "xsignals")["members"]] == ["X97"]
    by_pair = check_length_rules(index, target="DP_N")
    assert [rule["name"] for rule in by_pair["rules"]] == ["WP"]


def test_conditional_members_are_listed(length_data):
    rules = [_matched("ML", "InNetClass('GN') and OnLayer('Top Layer')")]
    rule = _only_rule(check_length_rules(_index(length_data, rules)))
    assert rule["status"] == "no_members"
    assert rule["conditional_count"] == 3
    assert rule["conditional"][0]["conditions"] == ["OnLayer('Top Layer')"]


def test_disabled_and_unknown_rules(length_data):
    rules = [_matched("OFF", "All", enabled=False)]
    index = _index(length_data, rules)
    assert _only_rule(check_length_rules(index))["status"] == "disabled"
    assert check_length_rules(index, rule="nope")["error"] == "rule_not_found"
    assert check_length_rules(index, target="nope")["error"] == "target_not_found"
    assert "note" in check_length_rules(_index(length_data))


def test_outputs_keep_lengths_under_mil_keys(length_data):
    rules = [
        _matched("ML", "InxSignalClass('XG') or InNetClass('GN')"),
        _matched("WP", "InDifferentialPairClass('PC')", within=True),
        _rule("L", "Length", "InNet('VIA_L3')", MINLIMIT="1mil", MAXLIMIT="2mil"),
    ]
    index = _index(length_data, rules)
    outputs = [
        check_length_rules(index, problems_only=False),
        net_length(index, "VIA_L3"),
        net_length(index, "TEE"),
        net_length(index, "GAP"),
        xsignal_length(index, "XSER"),
        pair_lengths(index, "DP"),
        pad_to_pad(index, "VIA_L3", "U1-3", "J1-1"),
    ]
    for output in outputs:
        assert _floats_outside_mil_keys(output) == []


def test_corpus_shaped_rule_values_are_interpreted(length_data):
    rules = [_matched("ML", "InxSignalClass('XG')", tolerance="3.937mil")]
    report = check_length_rules(_index(length_data, rules))
    rule = _only_rule(report)
    assert rule["tolerance_mil"] == pytest.approx(3.937)
    assert rule["group_checks"] == {"xsignals": True, "diff_pairs": True, "other_nets": True}
    assert rule["in_batch_drc"] is True
    assert report["unknown"] == []
    assert report["measure"] == pcb_length.MEASURE


# ---------- pin/package length ----------


def _set_pin_package(data, values):
    """Give pads ('<designator>-<pad>' -> mil) a Pin/Package Length."""
    for primitive in data.primitives:
        if primitive.kind == "pad":
            label = f"{COMPONENTS[primitive.component]}-{primitive.pad_name}"
            if label in values:
                primitive.pin_package = values[label]
    return data


def test_pin_package_is_stored_per_pad(length_data):
    index = _index(_set_pin_package(length_data, {"U1-1": 30.0}))
    rows = index.db.execute(
        "SELECT pin_package FROM prims WHERE kind = 'pad' AND component = ? "
        "AND pad_name = '1'",
        (C["U1"],),
    ).fetchall()
    assert [row["pin_package"] for row in rows] == [30.0]


def test_pin_package_adds_to_two_pad_signal_paths(length_data):
    index = _index(_set_pin_package(length_data, {"U1-1": 30.0, "U2-1": 20.0}))
    result = net_length(index, "STRAIGHT")
    assert result["measure"] == "signal_path"
    assert result["copper_mil"] == pytest.approx(1000.0)
    assert result["pin_package_mil"] == pytest.approx(50.0)
    assert result["length_mil"] == pytest.approx(1050.0)
    path = pad_to_pad(index, "STRAIGHT", "U2-1", "U1-1")
    assert path["pin_package_mil"] == pytest.approx(50.0)
    assert path["length_mil"] == pytest.approx(1050.0)
    assert net_length(index, "WIGGLE")["pin_package_mil"] == 0.0


def test_pin_package_is_not_added_to_routed_length(length_data):
    index = _index(_set_pin_package(length_data, {"U1-5": 40.0, "U3-5": 2.5}))
    result = net_length(index, "TEE")
    assert result["measure"] == "routed_plus_vias"
    assert result["length_mil"] == pytest.approx(1300.0)
    assert result["pin_package_mil"] == 0.0
    assert result["pin_package_excluded_mil"] == pytest.approx(42.5)
    # A pad-to-pad path on the same net includes its own end pads.
    path = pad_to_pad(index, "TEE", "U1-5", "U3-5")
    assert path["length_mil"] == pytest.approx(842.5)


def test_pin_package_in_pair_legs_and_skew(length_data):
    index = _index(_set_pin_package(length_data, {"U1-12": 5.0}))
    result = pair_lengths(index, "DP")
    assert result["positive"]["pin_package_mil"] == pytest.approx(5.0)
    assert result["positive"]["length_mil"] == pytest.approx(505.0)
    assert result["skew_mil"] == pytest.approx(10.5)
    assert result["length_mil"] == pytest.approx(499.75)
    assert result["max_mil"] == pytest.approx(505.0)


def test_xsignal_pin_package_at_end_and_series_pins(length_data):
    index = _index(_set_pin_package(
        length_data, {"U1-8": 100.0, "R1-1": 7.0, "U2-8": 3.0}
    ))
    result = xsignal_length(index, "XSER")
    assert result["copper_mil"] == pytest.approx(500.0)
    assert result["pin_package_mil"] == pytest.approx(110.0)
    assert result["length_mil"] == pytest.approx(610.0)
    # Legs stay the routed copper between their pins.
    assert [leg["length_mil"] for leg in result["legs"]] == [200.0, 300.0]


def test_xsignal_fly_by_pin_adds_no_pin_package(length_data):
    length_data.xsignals.append(XSignal("XFLY", ["U1-5", "U3-5", "U2-5"]))
    index = _index(_set_pin_package(
        length_data, {"U1-5": 10.0, "U3-5": 1000.0, "U2-5": 20.0}
    ))
    result = xsignal_length(index, "XFLY")
    assert result["status"] == "ok"
    assert result["pin_package_mil"] == pytest.approx(30.0)
    assert result["length_mil"] == pytest.approx(800.0 + 800.0 + 30.0)


def test_length_rule_counts_pin_package(length_data):
    rules = [_rule("L", "Length", "InNet('STRAIGHT')", MINLIMIT="1040mil", MAXLIMIT="1060mil")]
    plain = _only_rule(check_length_rules(_index(length_data, rules), rule="L"))
    assert plain["members"][0]["status"] == "too_short"
    tuned = _index(_set_pin_package(length_data, {"U1-1": 30.0, "U2-1": 20.0}), rules)
    row = _only_rule(check_length_rules(tuned, rule="L"))["members"][0]
    assert row["status"] == "ok"
    assert row["pin_package_mil"] == pytest.approx(50.0)


def test_approximations_no_longer_exclude_pin_package():
    text = " ".join(pcb_length.APPROXIMATIONS)
    assert "not included (the pad property is not decoded)" not in text
    assert "Pin/Package" in pcb_length.MEASURE
    assert "average" in pcb_length.MEASURE


# ---------- group sections ----------


def _straight_data(lengths, pairs=(), classes=()):
    """Straight 2-pad nets U1-n -> U2-n of the given lengths."""
    names = list(lengths)
    prims = []
    for number, name in enumerate(names):
        y = 100.0 * number
        for component, x in ((0, 0.0), (1, lengths[name])):
            prims.append(Primitive(
                kind="pad", layer=1, net=number, component=component,
                x1=x, y1=y, x2=x, y2=y, width=10.0, height=10.0, shape=2,
                hole=0.0, pad_name=str(number + 1),
            ))
        prims.append(Primitive(
            kind="track", layer=1, net=number, component=None,
            x1=0.0, y1=y, x2=lengths[name], y2=y, width=4.0,
        ))
    return PcbData(
        board=_board(),
        nets=names,
        components=[
            PcbComponent(
                index=index, refdes=name, source_uid_tail=None,
                channel_path=None, x=0.0, y=0.0, rotation=0.0, side="top",
                designator=name,
            )
            for index, name in enumerate(("U1", "U2"))
        ],
        primitives=prims,
        pours=[],
        layer_name_to_v6={"top layer": 1, "bottom layer": 32},
        classes=list(classes),
        diff_pairs=[DiffPair(name, positive, negative, None) for name, positive, negative in pairs],
        drc_rule_kinds={3, 4},
    )


def test_group_checks_pair_legs_as_nets():
    # Both pair lengths are in band, but PD_P is short against the longest net.
    data = _straight_data(
        {"PC_P": 1000.0, "PC_N": 1008.0, "PD_P": 995.0, "PD_N": 1000.0},
        pairs=[("PC", "PC_P", "PC_N"), ("PD", "PD_P", "PD_N")],
        classes=[_class("PAIRS", "diff_pair", ["PC", "PD"])],
    )
    rules = [_matched("ML", "InDifferentialPairClass('PAIRS')", tolerance="10mil")]
    index = _index(data, rules)
    rule = _only_rule(check_length_rules(index, rule="ML"))
    nets = _group(rule, "nets")
    assert nets["reference"]["object"] == "PC_N"
    rows = _by_object(nets["members"])
    assert rows["PD_P"]["status"] == "too_short"
    assert rows["PD_P"]["outside_band_mil"] == pytest.approx(3.0)
    assert {name for name, row in rows.items() if row["status"] == "ok"} == {"PC_P", "PC_N", "PD_N"}
    pairs = _group(rule, "diff_pairs")
    assert pairs["reference"]["object"] == "PC"
    assert pairs["reference"]["length_mil"] == pytest.approx(1004.0)
    assert {row["status"] for row in pairs["members"]} == {"ok"}
    assert rule["summary"]["violations"] == 1
    # A leg as target lists its own net row (and the pair it belongs to).
    by_leg = _only_rule(check_length_rules(index, target="PD_P"))
    assert [row["object"] for row in _group(by_leg, "nets")["members"]] == ["PD_P"]
    assert [row["object"] for row in _group(by_leg, "diff_pairs")["members"]] == ["PD"]


def test_group_pair_length_is_the_average():
    data = _straight_data(
        {"P": 1000.0, "N": 1010.0, "REF": 1012.0},
        pairs=[("DPX", "P", "N")],
        classes=[_class("PX", "diff_pair", ["DPX"])],
    )
    rules = [_matched("ML", "InDifferentialPairClass('PX')", TARGETSOURCENAME="REF")]
    rule = _only_rule(check_length_rules(_index(data, rules), rule="ML"))
    pair = _group(rule, "diff_pairs")["members"][0]
    # avg 1005 is below the 1007..1017 band; max(P, N) = 1010 would pass.
    assert pair["length_mil"] == pytest.approx(1005.0)
    assert pair["max_mil"] == pytest.approx(1010.0)
    assert pair["status"] == "too_short"
    assert pair["outside_band_mil"] == pytest.approx(2.0)


def test_group_sections_follow_the_enabled_checks(length_data):
    rules = [_matched("ML", "InNetClass('GN') or InDifferentialPairClass('PC')")]
    rules[0].properties["CHECKDIFFPAIRVSDIFFPAIR"] = "FALSE"
    rule = _only_rule(check_length_rules(_index(length_data, rules), rule="ML"))
    assert set(rule["groups"]) == {"nets"}
    assert rule["summary"]["members"] == 5


# ---------- class targets ----------


def test_xsignal_class_target(length_data):
    length_data.classes.append(PcbClass(
        name="All xSignals", kind="xsignal", members=[], superclass=True,
        auto_generated=False, unique_id=None,
    ))
    rules = [
        _matched("ML", "InxSignalClass('XG')"),
        _matched("WP", "InDifferentialPairClass('PC')", within=True),
    ]
    index = _index(length_data, rules)
    report = check_length_rules(index, target="xg")
    assert report["target"] == {"name": "XG", "kind": "xsignal_class"}
    rule = _only_rule(report)
    assert rule["name"] == "ML"
    members = {row["object"] for row in _group(rule, "xsignals")["members"]}
    assert members == {"X100", "X97", "X90"}
    every = check_length_rules(index, target="All xSignals")
    assert [item["name"] for item in every["rules"]] == ["ML"]


def test_net_and_pair_class_targets(length_data):
    rules = [
        _matched("ML", "InNetClass('GN')"),
        _matched("WP", "InDifferentialPairClass('PC')", within=True),
    ]
    index = _index(length_data, rules)
    by_net_class = check_length_rules(index, target="GN")
    assert by_net_class["target"]["kind"] == "net_class"
    assert [item["name"] for item in by_net_class["rules"]] == ["ML"]
    by_pair_class = check_length_rules(index, target="PC")
    assert by_pair_class["target"]["kind"] == "diff_pair_class"
    assert [item["name"] for item in by_pair_class["rules"]] == ["WP"]
    missing = check_length_rules(index, target="nope")
    assert missing["error"] == "target_not_found"
    assert "xSignal" in missing["message"] and "class" in missing["message"]


# ---------- copper connectivity ----------


def _graph_row(**values):
    row = dict(
        id=0, kind="track", layer=1, layer_to=None, component=None, x1=0.0,
        y1=0.0, x2=0.0, y2=0.0, width=0.0, height=None, rotation=0.0,
        shape=None, hole=None, pad_name=None, length=None, pin_package=None,
    )
    row.update(values)
    return row


def _graph_pad(pid, x, y, component):
    return _graph_row(
        id=pid, kind="pad", x1=x, y1=y, x2=x, y2=y, width=10.0, height=10.0,
        shape=2, component=component, pad_name="1",
    )


@pytest.mark.parametrize("dx, dy", [(0.0, 0.0), (3.0, 0.0), (0.0, 3.0), (6.0, 0.0), (0.0, 8.0)])
def test_narrow_track_ending_inside_a_wide_track_end_connects(dx, dy):
    rows = [
        _graph_pad(1, 0.0, 0.0, 0),
        _graph_pad(2, 1000.0, 0.0, 1),
        _graph_row(id=3, x1=0.0, y1=0.0, x2=500.0, y2=0.0, width=20.0),
        _graph_row(id=4, x1=500.0 + dx, y1=dy, x2=1000.0, y2=0.0, width=4.0),
    ]
    result = NetGraph(rows, [1, 32], {1: 0.7, 32: 54.5}).shortest([1], [2])
    assert result.status == "ok"
    # Along the axis the ends are linked by their distance; off it the wide
    # end lands on the narrow track's body. Either way about the 1000 mil run.
    assert result.length == pytest.approx(1000.0, abs=0.1)


def test_track_ends_outside_each_others_copper_stay_apart():
    rows = [
        _graph_pad(1, 0.0, 0.0, 0),
        _graph_pad(2, 1000.0, 0.0, 1),
        # Beyond the 20 mil track's round end.
        _graph_row(id=3, x1=0.0, y1=0.0, x2=500.0, y2=0.0, width=20.0),
        _graph_row(id=4, x1=512.0, y1=0.0, x2=1000.0, y2=0.0, width=4.0),
    ]
    assert NetGraph(rows, [1, 32], None).shortest([1], [2]).status == "no_path"
    # Equal widths keep the half-width merge: no shortcut between meander arms.
    rows[2] = _graph_row(id=3, x1=0.0, y1=0.0, x2=500.0, y2=0.0, width=4.0)
    rows[3] = _graph_row(id=4, x1=503.0, y1=0.0, x2=1000.0, y2=0.0, width=4.0)
    assert NetGraph(rows, [1, 32], None).shortest([1], [2]).status == "no_path"


def test_wide_track_ending_on_a_narrow_track_body_connects():
    rows = [
        _graph_pad(1, 0.0, 0.0, 0),
        _graph_pad(2, 1000.0, 0.0, 1),
        _graph_pad(5, 500.0, 300.0, 2),
        _graph_row(id=3, x1=0.0, y1=0.0, x2=1000.0, y2=0.0, width=4.0),
        # Its end is 6 mil off the narrow centreline, inside its own round end.
        _graph_row(id=4, x1=500.0, y1=6.0, x2=500.0, y2=300.0, width=20.0),
    ]
    result = NetGraph(rows, [1, 32], None).shortest([1], [5])
    assert result.status == "ok"
    assert result.length == pytest.approx(500.0 + 294.0)


# ---------- Altium example board (skipped when absent) ----------

MINIPC = Path(
    r"C:\Users\Public\Documents\Altium\AD25\Examples\Mini PC"
    r"\Mini PC - Motherboard\MiniPC.PcbDoc"
)


@pytest.fixture(scope="module")
def minipc():
    if not MINIPC.is_file():
        pytest.skip("Altium example boards not installed")
    from server.parsers.pcb_doc import parse_pcb_doc

    return PcbIndex(parse_pcb_doc(str(MINIPC)), None)


def test_minipc_tuned_groups_include_pin_package(minipc):
    enet = _only_rule(check_length_rules(minipc, rule="MatchedLengths_ENET_HPS"))
    assert enet["summary"]["violations"] == 0
    assert _group(enet, "xsignals")["summary"]["spread_mil"] < 20.0
    usb = _only_rule(check_length_rules(minipc, rule="MatchedLengths_USB3"))
    assert usb["summary"]["violations"] <= 2
    assert _group(usb, "nets")["summary"]["spread_mil"] < 50.0
    byte = _only_rule(check_length_rules(minipc, rule="MatchedLengths_BYTELINE_IC_DDR4"))
    assert _group(byte, "nets")["summary"]["members"] == 44
    assert _group(byte, "diff_pairs")["summary"]["members"] == 4


def test_track_on_a_layer_missing_from_the_stack_reaches_through_hole_pads(length_data):
    data = deepcopy(length_data)
    for primitive in data.primitives:
        if primitive.net != N["STRAIGHT"]:
            continue
        if primitive.kind == "pad":
            primitive.layer = 74
            primitive.hole = 8.0
        else:
            primitive.layer = 5  # a signal layer the stack table does not list
    result = net_length(_index(data), "STRAIGHT")
    assert result["status"] == "ok"
    assert result["length_mil"] == pytest.approx(1000.0, abs=1.0)
