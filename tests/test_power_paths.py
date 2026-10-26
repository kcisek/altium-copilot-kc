"""Power paths (services/power_paths, parsers/power_analyzer, check_power_paths): the
IPC-2221 formula at published points, track, arc and via resistance, parallel vias, the
copper graph and its least-resistance paths, pour-fed rails, currents and their sources,
the Power Analyzer files, paging and the tool. Synthetic boards with generic names."""

import json
import math
from pathlib import Path

import pytest

import server.main as main_mod
import server.services.power_paths as pp
from server.parsers import power_analyzer as pa
from server.parsers.pcb_doc import (
    BoardInfo, CopperArea, PcbComponent, PcbData, PcbLayer, Primitive,
)
from server.parsers.prj_pcb import VariantDefinition, VariantState
from server.services.derating import parse_rail_map
from server.services.pcb_index import PcbIndex
from tests.test_pull_resistors import _netlist

NETS = ["GND", "VIN", "3V3", "5V", "SW1", "FB1", "3V3_A", "SIG", "VCAP1"]
GND, VIN, P3V3, P5V, SW1, FBN, P3A, SIG, VCAP = range(len(NETS))
PARTS = [
    ("J1", {"1": ("1", "VIN", "Passive"), "2": ("2", "GND", "Passive")},
     {"description": "Header 2 pin, power input"}),
    ("U1", {"1": ("VIN", "VIN", "Power"), "2": ("VOUT", "3V3", "Power"),
            "3": ("GND", "GND", "Power")}, {"description": "LDO regulator 3.3 V"}),
    ("U2", {"1": ("VDD", "3V3", "Power"), "2": ("GND", "GND", "Power"),
            "3": ("PA0", "SIG", "IO")}, {"description": "Microcontroller"}),
    ("U3", {"1": ("VIN", "VIN", "Power"), "2": ("SW", "SW1", "Power"),
            "3": ("FB", "FB1", "Input"), "4": ("GND", "GND", "Power")},
     {"description": "Synchronous step-down converter"}),
    ("L1", {"1": ("1", "SW1"), "2": ("2", "5V")}, {"description": "Inductor 4.7uH"}),
    ("R1", {"1": ("1", "5V"), "2": ("2", "FB1")}, {"description": "Resistor", "value": "100k"}),
    ("R2", {"1": ("1", "FB1"), "2": ("2", "GND")}, {"description": "Resistor", "value": "22k"}),
    ("U4", {"1": ("VCC", "5V", "Power"), "2": ("GND", "GND", "Power")},
     {"description": "Motor driver"}),
    ("FB1", {"1": ("1", "3V3"), "2": ("2", "3V3_A")}, {"description": "Ferrite bead"}),
    ("U5", {"1": ("AVDD", "3V3_A", "Power"), "2": ("GND", "GND", "Power")},
     {"description": "ADC"}),
]
DESIGNATORS = ["J1", "U1", "U2", "U3", "L1", "U4", "FB1", "U5"]
C = {name: number for number, name in enumerate(DESIGNATORS)}
ONE_OZ = 1.378  # mil
INNER = 0.689
# Copper centres: Top 0.689, Bottom 1.378 + 10 + 0.689 + 40 + 0.689 + 10 + 0.689.
TOP_TO_BOTTOM = 1.378 + 10 + 0.689 + 40 + 0.689 + 10 + 0.689 - 0.689
PLATING = 25 / 25.4  # mil
RHO = 1.72e-8
M = 25.4e-6
PAD = 10.0  # half of the 20 mil square pads: track copper inside a pad is the pad's
VIA = 12.0  # radius of the 24 mil vias


def _ohms_track(length, width, thickness=ONE_OZ, rho=RHO):
    return rho * length * M / (width * M * thickness * M)


def _ohms_via(length, drill, plating=PLATING, rho=RHO):
    area = math.pi * ((drill / 2 + plating) ** 2 - (drill / 2) ** 2)
    return rho * length * M / (area * M * M)


def _layer(v6, name, order, kind="copper", **kwargs):
    return PcbLayer(v6_id=v6, name=name, kind=kind, stack_order=order, **kwargs)


def _board(heights=True):
    copper = {"copper_thick_mil": ONE_OZ} if heights else {}
    inner = {"copper_thick_mil": INNER} if heights else {}
    return BoardInfo(
        origin_x=0.0, origin_y=0.0, sheet_x=0.0, sheet_y=0.0, sheet_width=5000.0,
        sheet_height=5000.0, display_unit="mil", stackup_source="v9",
        layers=[
            _layer(1, "Top Layer", 0, **copper),
            _layer(None, "D1", 1, kind="dielectric", diel_height_mil=10.0),
            _layer(2, "L2", 2, **inner),
            _layer(None, "D2", 3, kind="dielectric", diel_height_mil=40.0),
            _layer(3, "L3", 4, **inner),
            _layer(None, "D3", 5, kind="dielectric", diel_height_mil=10.0),
            _layer(32, "Bottom Layer", 6, **copper),
        ])


def _track(net, x1, y1, x2, y2, width=10.0, layer=1, kind="track", **extra):
    return Primitive(kind=kind, layer=layer, net=net, component=None, x1=x1, y1=y1, x2=x2,
                     y2=y2, width=width, **extra)


def _pad(net, part, name, x, y, layer=1, size=20.0):
    return Primitive(kind="pad", layer=layer, net=net, component=C.get(part), x1=x, y1=y,
                     x2=x, y2=y, width=size, height=size, shape=2, pad_name=name)


def _via(net, x, y, hole=12.0, diameter=24.0, layer_to=32):
    return Primitive(kind="via", layer=1, layer_to=layer_to, net=net, component=None, x1=x,
                     y1=y, x2=x, y2=y, width=diameter, hole=hole)


def _arc(net, cx, cy, radius, start_deg, end_deg, steps=8, width=8.0):
    sweep = end_deg - start_deg
    points = [(cx + radius * math.cos(math.radians(start_deg + sweep * k / steps)),
               cy + radius * math.sin(math.radians(start_deg + sweep * k / steps)))
              for k in range(steps + 1)]
    return [Primitive(kind="arc", layer=1, net=net, component=None, x1=a[0], y1=a[1],
                      x2=b[0], y2=b[1], width=width, source_id=7, arc_x=cx, arc_y=cy,
                      arc_radius=radius, length=radius * math.radians(abs(sweep)) / steps)
            for a, b in zip(points, points[1:])]


def _copper(three_v3=None, vias=1, five_v=None, three_v3_a=None):
    """The board: VIN from J1 to U1 (20 mil, 1000) and U3 (10 mil, 1000); 3V3 from U1 up
    500 mil on top, a via, 1000 mil on the bottom to U2 (a bottom pad), with FB1 on a
    100 mil branch; 3V3_A from FB1 to U5; 5V from L1 to U4 (15 mil, 2000)."""
    prims = [
        _pad(VIN, "J1", "1", 0, 0), _pad(VIN, "U1", "1", 1000, 0),
        _pad(VIN, "U3", "1", 0, -1000),
        _track(VIN, 0, 0, 1000, 0, width=20), _track(VIN, 0, 0, 0, -1000, width=10),
        _pad(SW1, "U3", "2", -200, -1000), _pad(SW1, "L1", "1", -100, 2000),
        _pad(P5V, "L1", "2", 0, 2000), _pad(P5V, "U4", "1", 2000, 2000),
        _pad(P3A, "FB1", "2", 1300, 200), _pad(P3A, "U5", "1", 1800, 200),
    ]
    prims += three_v3 if three_v3 is not None else [
        _pad(P3V3, "U1", "2", 1100, 0), _track(P3V3, 1100, 0, 1100, 500),
        *[_via(P3V3, 1100 + 30 * number, 500) for number in range(vias)],
        _track(P3V3, 1100, 500, 2100, 500, layer=32),
        # Several vias: a wide track joins them on each side.
        *([_track(P3V3, 1100, 500, 1100 + 30 * (vias - 1), 500, width=100)]
          if vias > 1 else []),
        *([_track(P3V3, 1100, 500, 1100 + 30 * (vias - 1), 500, width=100, layer=32)]
          if vias > 1 else []),
        _pad(P3V3, "U2", "1", 2100, 500, layer=32),
        _pad(P3V3, "FB1", "1", 1200, 200), _track(P3V3, 1100, 200, 1200, 200),
    ]
    prims += five_v if five_v is not None else [_track(P5V, 0, 2000, 2000, 2000, width=15)]
    prims += three_v3_a if three_v3_a is not None else [
        _track(P3A, 1300, 200, 1800, 200, width=8)]
    return prims


def _index(prims=None, areas=(), heights=True, plane=None, netlist=None,
           designators=DESIGNATORS):
    board = _board(heights)
    if plane:
        board.layers.append(_layer(39, "Plane1", 7))
        board.plane_nets = {39: plane}
    components = [PcbComponent(index=number, refdes=name, source_uid_tail=None,
                               channel_path=None, x=0.0, y=0.0, rotation=0.0, side="top",
                               designator=name) for number, name in enumerate(designators)]
    pcb = PcbData(board=board, nets=list(NETS), components=components,
                  primitives=list(prims if prims is not None else _copper()), pours=[],
                  layer_name_to_v6={"top layer": 1, "l2": 2, "l3": 3, "bottom layer": 32},
                  areas=list(areas), area_streams=("Regions6", "Fills6"))
    return PcbIndex(pcb, netlist)


def _parts(**params):
    """PARTS with parameters added per designator."""
    out = []
    for ref, pins, info in PARTS:
        if ref in params:
            info = {**info, "params": params[ref]}
        out.append((ref, pins, info))
    return out


def _run(index=None, parts=None, current=None, eta=None, **kwargs):
    kwargs.setdefault("limit", 1000)
    if current is not None:
        current, error = pp.parse_currents(current)
        assert error is None
    if eta is not None:
        eta, error = pp.parse_eta(eta)
        assert error is None
    if isinstance(kwargs.get("rails"), str):
        kwargs["rails"], error = parse_rail_map(kwargs["rails"])
        assert error is None
    netlist = _netlist(parts or PARTS)
    return pp.run_power_paths_check(netlist, index=index or _index(netlist=netlist),
                                    current=current, eta=eta, **kwargs)


def _rail(result, name):
    return next(row for row in result["rails"] if row["rail"] == name)


def _findings(result, check=None):
    return [f for f in result["findings"] if check is None or f["check"] == check]


# ---------- IPC-2221 ----------

def test_ipc2221_at_published_calculator_points():
    # Trace-width calculators implementing IPC-2221 (Advanced Circuits' calculator,
    # Altium's 'IPC-2221 Calculator for PCB Trace Current and Heating'): 1 A at a 10 C
    # rise in 1 oz copper needs 11.8 mil outside and about 30.7 mil inside.
    assert pp.ipc2221_width(1.0, 10.0, ONE_OZ, True) == pytest.approx(11.8, abs=0.05)
    assert pp.ipc2221_width(1.0, 10.0, ONE_OZ, False) == pytest.approx(30.75, abs=0.1)
    # So 1 A on a 10 mil, 1 oz external track rises (1 / (0.048 x 13.78^0.725))^(1/0.44).
    rise = pp.ipc2221_rise(1.0, 10 * ONE_OZ, True)
    assert rise == pytest.approx(13.2, abs=0.05)
    # The formula and its inverse agree, internal layers carry about half.
    area = 10 * ONE_OZ
    assert pp.ipc2221_current(rise, area, True) == pytest.approx(1.0)
    assert pp.ipc2221_current(10.0, area, False) == pytest.approx(
        pp.ipc2221_current(10.0, area, True) / 2)
    assert pp.ipc2221_current(10.0, 11.8 * ONE_OZ, True) == pytest.approx(1.0, abs=0.005)
    assert pp.ipc2221_rise(0.0, area, True) == 0.0


# ---------- resistance ----------

def test_track_resistance_and_temperature():
    # 1 oz copper is about 0.49 mOhm per square: 100 squares (1000 x 10 mil) 49 mOhm.
    assert pp.track_resistance(1000, 10, ONE_OZ) == pytest.approx(0.04914, rel=1e-3)
    assert pp.resistivity(20) == pytest.approx(1.72e-8)
    assert pp.resistivity(70) == pytest.approx(1.72e-8 * (1 + 0.00393 * 50))
    assert pp.track_resistance(1000, 10, ONE_OZ, pp.resistivity(70)) == pytest.approx(
        0.04914 * 1.1965, rel=1e-3)
    assert pp.track_resistance(1000, 0, ONE_OZ) == math.inf


def test_via_resistance_and_parallel():
    # A 12 mil drill with 25 um plating through a 62 mil board: about 1 mOhm.
    ohms = pp.via_resistance(62.0, 12.0, PLATING)
    assert ohms == pytest.approx(_ohms_via(62.0, 12.0))
    assert ohms == pytest.approx(0.00103, rel=0.02)
    assert pp.barrel_area_mil2(12.0, PLATING) == pytest.approx(
        math.pi * ((6 + PLATING) ** 2 - 36))
    assert pp.parallel([ohms] * 4) == pytest.approx(ohms / 4)
    assert pp.parallel([1.0, 3.0]) == pytest.approx(0.75)
    assert pp.parallel([]) == math.inf


# ---------- arguments ----------

def test_parsers():
    assert pp.parse_currents("VIN=3,U5=300mA,U2@3V3=0.1") == (
        {"VIN": 3.0, "U5": 0.3, "U2@3V3": 0.1}, None)
    assert pp.parse_currents("VIN")[1]["error"] == "invalid_current"
    assert pp.parse_currents("VIN=-1")[1]["error"] == "invalid_current"
    assert pp.parse_copper_oz("2") == (2.0, None)
    assert pp.parse_copper_oz("outer=2,Inner=0.5oz") == ({"outer": 2.0, "inner": 0.5}, None)
    assert pp.parse_copper_oz("Top Layer=2") == ({"top layer": 2.0}, None)
    for bad in ("x", "20", "2,3", "outer=0"):
        assert pp.parse_copper_oz(bad)[1]["error"] == "invalid_copper_oz", bad
    assert pp.parse_eta("0.9") == (0.9, None)


@pytest.mark.parametrize("params, amps", [
    ({"Current Consumption": "150mA"}, 0.15),
    ({"Supply Current": "15 mA typ, 30 mA max"}, 0.03),
    ({"Current Consumption (mA)": "250"}, 0.25),
    ({"IDD": "2A"}, 2.0),
    ({"Current Consumption": "250"}, None),  # no unit anywhere
    ({"Current Rating": "3A"}, None),  # a rating, not a consumption
])
def test_param_current(params, amps):
    from server.services.derating import merged_params
    found = pp.param_current(merged_params({"params": params}, None, "U9"))
    assert (found[0] if found else None) == (pytest.approx(amps) if amps else None)


# ---------- the graph and paths ----------

def test_path_resistance_through_tracks_and_a_via():
    result = _run(current="3V3=5")
    row = _rail(result, "3V3")
    assert row["status"] == "estimated" and row["sources"] == ["U1 output"]
    path = next(p for p in row["paths"] if p["load"] == "U2")
    # Pad to via (the copper inside U1-2's pad and the via's ring left out), via to pad.
    tracks = _ohms_track(500 - PAD - VIA, 10) + _ohms_track(1000 - VIA - PAD, 10)
    via = _ohms_via(TOP_TO_BOTTOM, 12.0)
    assert path["resistance_mohm"] == pytest.approx((tracks + via) * 1000, abs=1e-3)
    assert path["vias_mohm"] == pytest.approx(via * 1000, abs=1e-3)
    assert path["track_mil"] == pytest.approx(1500 - 2 * PAD - 2 * VIA)
    assert path["vias"] == 1 and path["layers"] == ["Top Layer", "Bottom Layer"]
    assert path["layer_changes"][0]["from_layer"] == "Top Layer"
    # Only the rail's current is known: the piece up to the junction (both loads' paths
    # use it) carries all 5 A, the rest at most that: a range.
    trunk = _ohms_track(200 - PAD, 10)
    assert path["drop_low_mv"] == pytest.approx(5 * trunk * 1000, rel=1e-3)
    assert path["drop_high_mv"] == pytest.approx(5 * (tracks + via) * 1000, rel=1e-3)
    # The feed to 3V3_A through FB1: 200 mil up the track (a T-junction) and 100 mil across.
    feed = next(p for p in row["paths"] if p["load"] == "FB1")
    assert feed["role"] == "feeds"
    assert feed["resistance_mohm"] == pytest.approx(_ohms_track(300 - 2 * PAD, 10) * 1000,
                                                    abs=1e-3)


def test_vin_paths_and_the_narrowest_track():
    row = _rail(_run(), "VIN")
    by_load = {p["load"]: p for p in row["paths"]}
    assert by_load["U1"]["resistance_mohm"] == pytest.approx(
        _ohms_track(1000 - 2 * PAD, 20) * 1000, abs=1e-3)
    assert by_load["U3"]["resistance_mohm"] == pytest.approx(
        _ohms_track(1000 - 2 * PAD, 10) * 1000, abs=1e-3)
    assert row["narrowest"]["width_mil"] == 10.0 and row["narrowest"]["layer"] == "Top Layer"
    assert row["narrowest"]["copper_um"] == pytest.approx(35.0, abs=0.01)
    capacity = row["narrowest"]["capacity_a"]
    assert capacity["10C"] == pytest.approx(pp.ipc2221_current(10, 10 * ONE_OZ, True), rel=1e-3)
    assert row["copper"]["track_pieces"] == 2 and row["copper"]["vias"] == 0
    assert row["copper"]["by_layer"][0]["routed_mil"] == 2000.0


def test_arcs_measure_their_true_length():
    arc = _arc(P3A, 1300, 300, 100, -90, 0)  # from FB1-2 (1300, 200) to (1400, 300)
    copper = _copper(three_v3_a=[*arc, _track(P3A, 1400, 300, 1800, 300, width=8),
                                 _track(P3A, 1800, 300, 1800, 200, width=8)])
    row = _rail(_run(index=_index(copper, netlist=_netlist(PARTS))), "3V3_A")
    (path,) = row["paths"]
    # The arc's first chord leaves FB1-2's pad (x = 1310) part way along; U5-1's 10 mil.
    chord_x = 100 * math.sin(math.radians(90 / 8))
    inside = PAD / chord_x * (100 * math.pi / 2 / 8)
    expected = _ohms_track(100 * math.pi / 2 - inside, 8) + _ohms_track(500 - PAD, 8)
    assert path["resistance_mohm"] == pytest.approx(expected * 1000, abs=1e-3)


@pytest.mark.parametrize("vias", [1, 2, 4])
def test_parallel_vias_between_the_same_copper(vias):
    # Vias in one pad on top and one on the bottom are in parallel.
    copper = [_pad(P3V3, "U1", "2", 1100, 0), _track(P3V3, 1100, 0, 1100, 500),
              _pad(P3V3, None, "1", 1100, 500, size=200),
              _pad(P3V3, None, "1", 1100, 500, layer=32, size=200),
              *[_via(P3V3, 1050 + 30 * number, 500) for number in range(vias)],
              _track(P3V3, 1100, 500, 2100, 500, layer=32),
              _pad(P3V3, "U2", "1", 2100, 500, layer=32)]
    netlist = _netlist(PARTS)
    row = _rail(_run(index=_index(_copper(three_v3=copper), netlist=netlist)), "3V3")
    path = next(p for p in row["paths"] if p["load"] == "U2")
    assert path["vias_mohm"] == pytest.approx(_ohms_via(TOP_TO_BOTTOM, 12.0) / vias * 1000,
                                              abs=1e-3)
    assert path["vias"] == vias and path["layer_changes"][0]["vias"] == vias


def test_resistance_graph_combines_and_finds_the_least_resistance_path():
    netlist = _netlist(PARTS)
    index = _index(netlist=netlist)
    board = pp._Board(index, {"copper_oz": None}, [])
    from server.services.pcb_length import net_graph
    graph = net_graph(index, VIN)
    rgraph = pp.ResistanceGraph(graph, board.thickness, RHO, PLATING, index.pcb.primitives,
                                board.barrel_mil)
    pads = {graph.pads[pid]["name"] + str(graph.pads[pid]["component"]): pid
            for pid in graph.pads}
    start = rgraph.pad_nodes([pads["10"]])  # J1-1
    dist, _ = rgraph.dijkstra(start)
    (u1,) = rgraph.pad_nodes([pads["11"]])
    (u3,) = rgraph.pad_nodes([pads["13"]])
    assert dist[u1] == pytest.approx(_ohms_track(1000 - 2 * PAD, 20))
    assert dist[u3] == pytest.approx(_ohms_track(1000 - 2 * PAD, 10))
    assert rgraph.inside_pads_mil == pytest.approx(4 * PAD)
    assert len(rgraph.edges) == 2  # pad copper and track ends merged into nodes


def test_barrels_without_stack_heights_assume_a_1_6_mm_board():
    result = _run(index=_index(heights=False, netlist=_netlist(PARTS)))
    assert result["assumptions"]["barrel_lengths"].startswith("assumed")
    assert result["assumptions"]["copper"]["Top Layer"]["source"] == "assumed 1 oz"
    path = next(p for p in _rail(result, "3V3")["paths"] if p["load"] == "U2")
    assert path["vias_mohm"] == pytest.approx(
        _ohms_via(1.6 / 0.0254, 12.0) * 1000, abs=1e-3)
    assert any("1 oz" in note for note in result["notes"])


def test_copper_oz_overrides_the_stack():
    row = _rail(_run(copper_oz=2.0), "VIN")
    by_load = {p["load"]: p for p in row["paths"]}
    assert by_load["U3"]["resistance_mohm"] == pytest.approx(
        _ohms_track(1000 - 2 * PAD, 10, 2 * ONE_OZ) * 1000, abs=1e-3)
    inner = _run(copper_oz={"outer": 0.5})["assumptions"]["copper"]
    assert inner["Top Layer"] == {"um": 17.5, "source": "copper_oz"}
    assert inner["L2"]["source"] == "stackup"


# ---------- stubs ----------

def test_stubs_into_pads_are_left_out_of_the_narrowest_width():
    neck = [_pad(P5V, "U4", "1", 2000, 2000), _track(P5V, 0, 2000, 1980, 2000, width=15),
            _track(P5V, 1980, 2000, 2000, 2000, width=4)]  # a 20 mil neck into U4-1
    copper = _copper(five_v=neck)
    copper = [prim for prim in copper if not (prim.kind == "pad" and prim.net == P5V and
                                              prim.component == C["U4"])] + [neck[0]]
    netlist = _netlist(PARTS)
    row = _rail(_run(index=_index(copper, netlist=netlist)), "5V")
    assert row["narrowest"]["width_mil"] == 15.0
    assert row["stubs_ignored"] == {"count": 1, "narrowest_mil": 4.0, "shorter_than_mil": 39.4}
    kept = _rail(_run(index=_index(copper, netlist=netlist), stub_mil=0.0), "5V")
    assert kept["narrowest"]["width_mil"] == 4.0 and "stubs_ignored" not in kept
    # The neck is not judged for its rise either (IPC-2221 assumes a long conductor): the
    # 15 mil track is, and the neck is listed apart.
    hot = _run(index=_index(copper, netlist=netlist), current="5V=2")
    (rise,) = _findings(hot, "temp_rise_high")
    assert rise["width_mil"] == 15.0 and rise["severity"] == "warning"
    neck_row = _rail(hot, "5V")["hottest_neck"]
    assert neck_row["width_mil"] == 4.0 and "not judged" in neck_row["note"]
    judged = _run(index=_index(copper, netlist=netlist), current="5V=2", stub_mil=0.0)
    (rise,) = _findings(judged, "temp_rise_high")
    assert rise["width_mil"] == 4.0


# ---------- pours ----------

def test_pour_fed_rails_are_not_estimated():
    region = CopperArea(kind="region", layer=1, net=P5V, component=None,
                        outline=[(0, 1900), (2000, 1900), (2000, 2100), (0, 2100)])
    netlist = _netlist(PARTS)
    result = _run(index=_index(areas=[region], netlist=netlist), current="5V=2")
    row = _rail(result, "5V")
    assert row["status"] == "pour_fed" and "paths" not in row
    assert row["copper"]["pour_mm2"] == pytest.approx(2000 * 200 * 0.0254 ** 2, rel=1e-3)
    assert row["copper"]["pour_share"] == pytest.approx(400000 / 430000, abs=1e-3)
    assert row["narrowest"]["width_mil"] == 15.0
    assert row["narrowest"]["rise_at_rail_current_c"] == pytest.approx(
        pp.ipc2221_rise(2, 15 * ONE_OZ, True), rel=1e-3)
    (finding,) = _findings(result, "pour_fed_not_estimated")
    assert finding["rails"][0]["rail"] == "5V" and finding["severity"] == "info"
    assert not _findings(result, "temp_rise_high")


def test_plane_fed_rails_are_not_estimated():
    netlist = _netlist(PARTS)
    row = _rail(_run(index=_index(plane="3V3", netlist=netlist)), "3V3")
    assert row["status"] == "pour_fed" and row["copper"]["planes"] == ["Plane1"]
    assert "the plane Plane1" in row["not_estimated"]


def test_rails_that_cannot_be_estimated_say_why():
    no_source = _rail(_run(parts=[part for part in PARTS if part[0] != "U1"]), "3V3")
    assert no_source["status"] == "no_source" and "get_power_tree" in no_source["not_estimated"]
    no_loads = _rail(_run(parts=[part for part in PARTS if part[0] not in ("U2", "FB1")]),
                     "3V3")
    assert no_loads["status"] == "no_loads"
    unrouted = _rail(_run(nets=["SW1"]), "SW1")
    assert unrouted["status"] == "no_routing"
    # A part's own regulator output (VCAP) feeding its own pins.
    parts = [part for part in PARTS if part[0] != "U2"] + [
        ("U2", {"1": ("VDD", "3V3", "Power"), "2": ("GND", "GND", "Power"),
                "4": ("VCAP", "VCAP1", "Power")}, {"description": "Microcontroller"}),
        ("C9", {"1": ("1", "VCAP1"), "2": ("2", "GND")}, {"description": "Capacitor",
                                                         "value": "1uF"})]
    copper = _copper() + [_pad(VCAP, "U2", "4", 2100, 600, layer=32),
                          _track(VCAP, 2100, 600, 2200, 600, layer=32)]
    own = _rail(_run(index=_index(copper, netlist=_netlist(parts)), parts=parts), "VCAP1")
    assert own["status"] == "own_supply" and own["sources"] == ["internal U2"]
    region = CopperArea(kind="region", layer=1, net=VIN, component=None,
                        outline=[(0, 100), (100, 100), (100, 200), (0, 200)])
    netlist = _netlist(PARTS)
    vin = _rail(_run(index=_index(areas=[region], netlist=netlist)), "VIN")
    assert vin["status"] == "estimated" and "upper bound" in vin["pour_note"]


# ---------- currents ----------

def test_currents_flow_up_the_power_tree():
    result = _run(current="3V3=0.5,5V=1", eta="U3=0.8", rails="VIN=12")
    vin = _rail(result, "VIN")["current"]
    assert vin["source"] == "sum_of_loads" and "partial" not in vin
    # U1 (an LDO) draws its output current; U3 (a buck) 5 V x 1 A / (0.8 x 12 V).
    assert vin["amps"] == pytest.approx(0.5 + 5 * 1 / (0.8 * 12), rel=1e-3)
    assert _rail(result, "3V3")["current"] == {"amps": 0.5, "source": "user",
                                              "basis": "current argument"}
    paths = {p["load"]: p for p in _rail(result, "VIN")["paths"]}
    assert paths["U3"]["load_current"]["source"] == "converter_input"
    assert paths["U3"]["load_current"]["amps"] == pytest.approx(0.5208, rel=1e-3)


def test_rails_are_given_as_arguments():
    # rails= feeds check_derating's voltages.
    result = _run(current="5V=1", eta="U3=0.8", rails="VIN=24")
    assert _rail(result, "VIN")["current"]["amps"] == pytest.approx(5 / (0.8 * 24), rel=1e-3)


def test_a_switcher_needs_its_efficiency():
    vin = _rail(_run(current="5V=1,3V3=0.5"), "VIN")["current"]
    assert vin["partial"] is True and vin["amps"] == pytest.approx(0.5)
    assert any("eta" in need for need in vin["needs"])


def test_designators_name_what_they_feed_and_draw():
    # A converter's designator is its output: the rail it makes carries it, and its input
    # rail its input current; 'U1@VIN' is its input current only.
    out = _run(current="U1=0.7")
    assert _rail(out, "3V3")["current"]["amps"] == pytest.approx(0.7)
    assert _rail(out, "3V3")["current"]["basis"].startswith("current argument (U1")
    assert _rail(out, "VIN")["current"]["amps"] == pytest.approx(0.7)
    given_in = _run(current="U1@VIN=0.4")
    assert _rail(given_in, "3V3")["current"]["amps"] is None
    paths = {p["load"]: p for p in _rail(given_in, "VIN")["paths"]}
    assert paths["U1"]["load_current"]["amps"] == pytest.approx(0.4)
    # An input connector's designator is the rail's supply; a series part's its current.
    assert _rail(_run(current="J1=2"), "VIN")["current"]["amps"] == pytest.approx(2.0)
    through = _run(current="FB1=0.25")
    assert _rail(through, "3V3_A")["current"]["amps"] == pytest.approx(0.25)
    feed = {p["load"]: p for p in _rail(through, "3V3")["paths"]}["FB1"]
    assert feed["load_current"]["amps"] == pytest.approx(0.25)
    # A part the power tree does not list as a load, named with a pin on the rail.
    parts = PARTS + [("R9", {"1": ("1", "3V3"), "2": ("2", "SIG")},
                      {"description": "Resistor", "value": "1k"})]
    extra = _run(parts=parts, current="R9@3V3=0.05,U2=0.1,U5=0")
    assert _rail(extra, "3V3")["current"]["amps"] == pytest.approx(0.15)
    # A part on no rail it is named for: noted, not silently dropped.
    idle = _run(current="U2@5V=1")
    assert any("applied to no rail: ['U2@5V']" in note for note in idle["notes"])


def test_outlet_connectors_draw_from_their_rail():
    # J2's power pin sits on 3V3, a rail made on the board: an outlet, so a load.
    parts = PARTS + [("J2", {"1": ("1", "3V3"), "2": ("2", "GND")},
                      {"description": "Header 2 pin"})]
    j2 = Primitive(kind="pad", layer=1, net=P3V3, component=len(DESIGNATORS), x1=1100,
                   y1=-300, x2=1100, y2=-300, width=20, height=20, shape=2, pad_name="1")
    netlist = _netlist(parts)
    index = _index(_copper() + [j2, _track(P3V3, 1100, 0, 1100, -300)], netlist=netlist,
                   designators=DESIGNATORS + ["J2"])
    result = _run(index=index, parts=parts, current="J2=0.5,U2=0.1,U5=0")
    row = _rail(result, "3V3")
    paths = {p["load"]: p for p in row["paths"]}
    assert paths["J2"]["role"] == "outlet"
    assert paths["J2"]["resistance_mohm"] == pytest.approx(
        _ohms_track(300 - 2 * PAD, 10) * 1000, abs=1e-3)
    assert row["current"]["amps"] == pytest.approx(0.6)
    unknown = _rail(_run(index=index, parts=parts, current="U2=0.1,U5=0"), "3V3")
    assert unknown["current"]["partial"] is True


def test_drops_without_a_voltage_ask_for_it():
    # VIN's name gives no voltage: its drops are in mV only, and the finding asks for it.
    result = _run(current="U1@VIN=1,U3@VIN=0.5")
    (finding,) = _findings(result, "needs_voltage")
    assert finding["rails"][0]["rail"] == "VIN" and finding["severity"] == "info"
    expected = max(_ohms_track(1000 - 2 * PAD, 20) * 1, _ohms_track(1000 - 2 * PAD, 10) * 0.5)
    assert finding["rails"][0]["highest_drop_mv"] == pytest.approx(expected * 1000, rel=1e-3)
    assert not _findings(_run(current="U1@VIN=1,U3@VIN=0.5", rails="VIN=12"),
                         "needs_voltage")


def test_load_currents_from_designators_and_parameters():
    result = _run(parts=_parts(U2={"Current Consumption": "150mA"}), current="U5=0.05")
    three = _rail(result, "3V3")["current"]
    # U2's parameter and the feed to 3V3_A (U5's current through FB1).
    assert three["amps"] == pytest.approx(0.2) and three["source"] == "sum_of_loads"
    paths = {p["load"]: p for p in _rail(result, "3V3")["paths"]}
    assert paths["U2"]["load_current"]["source"] == "parameter"
    assert "Current Consumption" in paths["U2"]["load_current"]["basis"]
    assert paths["FB1"]["load_current"]["source"] == "fed_rail"
    # A converter's designator is its output current.
    assert _rail(_run(current="U1=0.7"), "VIN")["current"]["amps"] == pytest.approx(0.7)
    # 'part@rail' picks the rail.
    picked = _run(current="U2@3V3=0.4")
    assert _rail(picked, "3V3")["current"]["amps"] == pytest.approx(0.4)


def test_unknown_currents_ask_for_them():
    result = _run()
    assert all(row["current"]["amps"] is None for row in result["rails"])
    (finding,) = _findings(result, "needs_current")
    assert {row["rail"] for row in finding["rails"]} >= {"VIN", "3V3", "5V", "3V3_A"}
    assert not _findings(result, "ir_drop_high") and not _findings(result, "temp_rise_high")
    assert any("current=" in note for note in result["notes"])
    partial = _run(current="U5=0.1")
    assert _rail(partial, "3V3")["current"]["partial"] is True
    assert _run(current="NOPE=1")["notes"][0].startswith("current= names no rail or part")


# ---------- findings ----------

OHMS_U2 = _ohms_track(1500 - 2 * PAD - 2 * VIA, 10) + _ohms_via(TOP_TO_BOTTOM, 12.0)
TRUNK = _ohms_track(200 - PAD, 10)  # U1-2 to the junction with FB1's branch


def test_ir_drop_prefers_the_loads_own_current():
    # Only the rail's current: U2's drop lies between the trunk's share and the whole
    # 5 A through its path; the upper figure alone gives at most info.
    result = _run(current="3V3=5", rails="3V3=3.3")
    (drop,) = [f for f in _findings(result, "ir_drop_high") if f["load"] == "U2"]
    assert drop["severity"] == "info" and drop["current_basis"].startswith("a range")
    assert drop["drop_high_mv"] == pytest.approx(OHMS_U2 * 5 * 1000, rel=1e-3)
    assert drop["drop_low_mv"] == pytest.approx(TRUNK * 5 * 1000, rel=1e-3)
    assert drop["drop_pct"] == pytest.approx(OHMS_U2 * 5 / 3.3 * 100, rel=1e-3)
    assert "judged info until" in drop["issue"]
    # Every load's current known: the drop is exact and keeps its warning.
    known = _run(current="U2=5,U5=0", rails="3V3=3.3")
    (drop,) = [f for f in _findings(known, "ir_drop_high") if f["load"] == "U2"]
    assert drop["severity"] == "warning" and drop["drop_mv"] == pytest.approx(
        OHMS_U2 * 5 * 1000, rel=1e-3)
    # With U2's own current known, the trunk carries the rail's 5 A and the rest 0.15 A.
    quiet = _run(parts=_parts(U2={"Current Consumption": "150mA"}), current="3V3=5")
    assert not [f for f in _findings(quiet, "ir_drop_high") if f["load"] == "U2"]
    path = next(p for p in _rail(quiet, "3V3")["paths"] if p["load"] == "U2")
    assert path["drop_mv"] == pytest.approx((TRUNK * 5 + (OHMS_U2 - TRUNK) * 0.15) * 1000,
                                            rel=1e-3)
    # Thresholds move the severity.
    relaxed = _run(current="U2=5,U5=0", rails="3V3=3.3", drop_info_pct=20,
                   drop_warning_pct=30)
    assert not _findings(relaxed, "ir_drop_high")
    info = _run(current="U2=5,U5=0", rails="3V3=3.3", drop_info_pct=2, drop_warning_pct=15)
    assert {f["severity"] for f in _findings(info, "ir_drop_high") if f["load"] == "U2"} == {
        "info"}


def test_shared_pieces_carry_the_sum_of_their_loads():
    # U6 sits on the bottom track after the via: the via and the bottom track up to U6
    # carry U2's and U6's currents together, so U2's drop is the sum of R x I per piece.
    parts = PARTS + [("U6", {"1": ("VDD", "3V3", "Power"), "2": ("GND", "GND", "Power")},
                      {"description": "Sensor"})]
    u6 = Primitive(kind="pad", layer=32, net=P3V3, component=len(DESIGNATORS), x1=1600,
                   y1=500, x2=1600, y2=500, width=20, height=20, shape=2, pad_name="1")
    netlist = _netlist(parts)
    index = _index(_copper() + [u6], netlist=netlist, designators=DESIGNATORS + ["U6"])
    result = _run(index=index, parts=parts, current="U2=1,U6=2,U5=0", rails="3V3=3.3")
    row = _rail(result, "3V3")
    path = next(p for p in row["paths"] if p["load"] == "U2")
    via = _ohms_via(TOP_TO_BOTTOM, 12.0)
    # U1-2 to the via and on to U6 (U6's pad copper 0 ohm) carry 3 A; U6 to U2 1 A.
    shared = _ohms_track(500 - PAD - VIA, 10) + via + _ohms_track(500 - VIA - PAD, 10)
    alone = _ohms_track(500 - 2 * PAD, 10)
    assert path["drop_mv"] == pytest.approx((shared * 3 + alone * 1) * 1000, rel=1e-3)
    assert "drop_basis" in path and "drop_low_mv" not in path
    # The via is judged at the 3 A it carries, not at either load's own current.
    (vias,) = _findings(result, "path_through_few_vias")
    assert vias["current_a"] == pytest.approx(3.0) and vias["severity"] == "warning"
    assert vias["loads_through"] == ["U2", "U6"]


def test_temperature_rise_on_the_power_path():
    # VIN=3 with its two loads' currents unknown: each branch carries at most 3 A, an
    # upper bound judged at most info.
    result = _run(current="VIN=3")
    (rise,) = _findings(result, "temp_rise_high")
    assert rise["rail"] == "VIN" and rise["width_mil"] == 10.0 and rise["severity"] == "info"
    assert "upper bound" in rise["issue"]
    # 3 A on 10 mil rises past IPC-2221's 100 C charts: said so, not as a figure.
    assert pp.ipc2221_rise(3, 10 * ONE_OZ, True) > 100
    assert rise["beyond_chart"] is True and "rise_c" not in rise
    assert "beyond its charts" in rise["issue"]
    # A single load's rail current is its own: a warning, with the rise as a figure.
    single = _run(current="5V=2")
    (rise,) = _findings(single, "temp_rise_high")
    assert rise["severity"] == "warning" and rise["rise_c"] == pytest.approx(
        pp.ipc2221_rise(2, 15 * ONE_OZ, True), rel=1e-3)
    # With every load's current known, each track carries only its own loads'.
    known = _run(current="3V3=0.5,5V=1", eta="U3=0.8", rails="VIN=12")
    row = _rail(known, "VIN")
    assert row["hottest_track"]["current_basis"] == "the currents of the loads it feeds"
    assert not _findings(known, "temp_rise_high")
    info = _run(current="VIN=1.2")
    assert {f["severity"] for f in _findings(info, "temp_rise_high")} == {"info"}


def test_a_track_split_at_a_junction_carries_a_path_once():
    # U2's path runs up both pieces of the 3V3 track the branch to FB1 splits; FB1's
    # path (U5's current) uses the first: that piece carries 1 + 0.5 A, not 2 + 0.5.
    row = _rail(_run(current="U2=1,U5=0.5"), "3V3")
    assert row["current"]["amps"] == pytest.approx(1.5)
    hottest = row["hottest_track"]
    assert hottest["current_a"] == pytest.approx(1.5) and hottest["width_mil"] == 10.0
    assert hottest["current_basis"] == "the currents of the loads it feeds"


def test_few_vias_for_the_current():
    result = _run(current="U2=5,U5=0")
    (vias,) = [f for f in _findings(result, "path_through_few_vias") if f["load"] == "U2"]
    per_via = pp.ipc2221_current(10, pp.barrel_area_mil2(12.0, PLATING), True)
    assert vias["severity"] == "warning" and vias["vias"] == 1
    assert vias["vias_needed"] == math.ceil(5 / per_via) == 3
    assert vias["per_via_a"]["10C"] == pytest.approx(per_via, rel=1e-3)
    # Only the rail's current, and the via carries U2's share of it: at most info.
    bound = _run(current="3V3=5")
    (vias,) = [f for f in _findings(bound, "path_through_few_vias") if f["load"] == "U2"]
    assert vias["severity"] == "info" and "upper bound" in vias["issue"]
    netlist = _netlist(PARTS)
    four = _run(index=_index(_copper(vias=4), netlist=netlist), current="U2=5,U5=0")
    assert not [f for f in _findings(four, "path_through_few_vias") if f["load"] == "U2"]
    path = next(p for p in _rail(four, "3V3")["paths"] if p["load"] == "U2")
    (change,) = path["layer_changes"]
    # The wide tracks' ends cover all four vias: they join the same copper.
    assert change["vias"] == 4 and change["vias_nearby"] == 4
    assert (change["from_layer"], change["to_layer"]) == ("Top Layer", "Bottom Layer")


def test_vias_shared_by_several_paths_are_reported_once():
    # U6 sits on the bottom track after the via: U2's and U6's paths share it.
    parts = PARTS + [("U6", {"1": ("VDD", "3V3", "Power"), "2": ("GND", "GND", "Power")},
                      {"description": "Sensor"})]
    u6 = Primitive(kind="pad", layer=32, net=P3V3, component=len(DESIGNATORS), x1=1600,
                   y1=500, x2=1600, y2=500, width=20, height=20, shape=2, pad_name="1")
    netlist = _netlist(parts)
    index = _index(_copper() + [u6], netlist=netlist, designators=DESIGNATORS + ["U6"])
    result = _run(index=index, parts=parts, current="3V3=5")
    (vias,) = _findings(result, "path_through_few_vias")
    assert vias["loads_through"] == ["U2", "U6"] and vias["vias"] == 1


def test_vias_nearby_share_the_current():
    # Two vias 150 mil apart on 100 mil wide copper: the second is within one via's
    # resistance of the path on both layers, so it counts as parallel; a third 300 mil
    # away on 10 mil copper does not.
    copper = [_pad(P3V3, "U1", "2", 1100, 0), _track(P3V3, 1100, 0, 1100, 500),
              _via(P3V3, 1100, 500), _via(P3V3, 1250, 500), _via(P3V3, 1100, 800),
              _track(P3V3, 1100, 500, 1250, 500, width=100),
              _track(P3V3, 1100, 500, 1250, 500, width=100, layer=32),
              _track(P3V3, 1100, 500, 1100, 800), _track(P3V3, 1100, 500, 1100, 800, layer=32),
              _track(P3V3, 1250, 500, 2100, 500, layer=32),
              _pad(P3V3, "U2", "1", 2100, 500, layer=32)]
    netlist = _netlist(PARTS)
    result = _run(index=_index(_copper(three_v3=copper), netlist=netlist), current="3V3=5")
    path = next(p for p in _rail(result, "3V3")["paths"] if p["load"] == "U2")
    (change,) = path["layer_changes"]
    assert change["vias"] == 1 and change["vias_nearby"] == 2
    (vias,) = [f for f in _findings(result, "path_through_few_vias") if f["load"] == "U2"]
    assert vias["vias_nearby"] == 2 and vias["severity"] == "info"
    known = _run(index=_index(_copper(three_v3=copper), netlist=netlist),
                 current="U2=6,U5=0")
    (vias,) = [f for f in _findings(known, "path_through_few_vias") if f["load"] == "U2"]
    # Two vias take 2 x 1.9 A at 10 C and 2 x 2.6 A at 20 C: 6 A, known, is a warning.
    assert vias["vias_nearby"] == 2 and vias["severity"] == "warning"


def test_a_via_crossed_in_two_barrel_pieces_is_one_layer_change():
    # A stub on L2 gives the via a node there: U2's path crosses the via from the top to
    # L2 and on to the bottom, one physical via and one layer change.
    netlist = _netlist(PARTS)
    copper = _copper() + [_track(P3V3, 1100, 500, 1200, 500, layer=2)]
    result = _run(index=_index(copper, netlist=netlist), current="U2=5,U5=0")
    path = next(p for p in _rail(result, "3V3")["paths"] if p["load"] == "U2")
    assert path["vias"] == 1
    (change,) = path["layer_changes"]
    assert (change["from_layer"], change["to_layer"]) == ("Top Layer", "Bottom Layer")
    assert path["vias_mohm"] == pytest.approx(_ohms_via(TOP_TO_BOTTOM, 12.0) * 1000, abs=1e-3)
    (vias,) = _findings(result, "path_through_few_vias")
    assert vias["vias"] == 1


def test_a_through_hole_load_pad_is_reached_at_its_nearest_layer():
    # U2's through-hole pad is reached on the top; a bottom track carries on to U6. U2's
    # path ends on the top: no pad barrel and no layer change in it.
    parts = PARTS + [("U6", {"1": ("VDD", "3V3", "Power"), "2": ("GND", "GND", "Power")},
                      {"description": "Sensor"})]
    tht = Primitive(kind="pad", layer=74, net=P3V3, component=C["U2"], x1=2100, y1=500,
                    x2=2100, y2=500, width=60, height=60, shape=1, pad_name="1", hole=35.0,
                    plated=True)
    u6 = Primitive(kind="pad", layer=32, net=P3V3, component=len(DESIGNATORS), x1=2600,
                   y1=500, x2=2600, y2=500, width=20, height=20, shape=2, pad_name="1")
    copper = [_pad(P3V3, "U1", "2", 1100, 0), _track(P3V3, 1100, 0, 1100, 500),
              _track(P3V3, 1100, 500, 2100, 500, width=20), tht,
              _track(P3V3, 2100, 500, 2600, 500, layer=32), u6]
    netlist = _netlist(parts)
    index = _index(_copper(three_v3=copper), netlist=netlist, designators=DESIGNATORS + ["U6"])
    result = _run(index=index, parts=parts, current="U2=1,U6=0.1")
    paths = {p["load"]: p for p in _rail(result, "3V3")["paths"]}
    expected = _ohms_track(500 - PAD, 10) + _ohms_track(1000 - 30, 20)
    assert paths["U2"]["resistance_mohm"] == pytest.approx(expected * 1000, abs=1e-3)
    assert "layer_changes" not in paths["U2"] and paths["U2"]["vias_mohm"] == 0.0
    assert paths["U6"]["layer_changes"][0]["through_pad"] is True


def test_copper_touching_exactly_joins():
    # U4-1 is a round pad whose edge the 5V track's round end just touches: the stored
    # coordinates put the end a hair (float rounding) past the touching distance.
    radius, half = 25.5906 / 2, 11.811 / 2
    end = 2000 - (radius + half) - 5e-13
    assert 2000 - end > radius + half  # past the exact limit, by float noise only
    pad = Primitive(kind="pad", layer=1, net=P5V, component=C["U4"], x1=2000, y1=2000,
                    x2=2000, y2=2000, width=25.5906, height=25.5906, shape=1, pad_name="1")
    copper = [prim for prim in _copper(five_v=[_track(P5V, 0, 2000, end, 2000, width=11.811)])
              if not (prim.kind == "pad" and prim.component == C["U4"])] + [pad]
    netlist = _netlist(PARTS)
    row = _rail(_run(index=_index(copper, netlist=netlist), current="5V=1"), "5V")
    assert row["status"] == "estimated" and [p["load"] for p in row["paths"]] == ["U4"]


def test_a_track_touching_a_pad_and_running_over_it_counts_from_its_far_edge():
    # The 5V track ends 5 mil right of U4-1's edge (its round end touches the pad) and runs
    # left across the pad: the pad's copper is at its potential, so the track counts from
    # the pad's far edge (x = 1990) to L1-2's edge (x = 10).
    copper = [prim for prim in _copper(five_v=[_track(P5V, 0, 2000, 2015, 2000, width=10)])]
    netlist = _netlist(PARTS)
    row = _rail(_run(index=_index(copper, netlist=netlist), current="5V=1"), "5V")
    (path,) = row["paths"]
    assert path["resistance_mohm"] == pytest.approx(_ohms_track(1990 - PAD, 10) * 1000,
                                                    abs=1e-3)


def test_loads_reached_only_through_pour_are_listed():
    # U6 has a 3V3 pad but no track to it, only a small region: no path, and the
    # summary finding names it.
    parts = PARTS + [("U6", {"1": ("VDD", "3V3", "Power"), "2": ("GND", "GND", "Power")},
                      {"description": "Sensor"})]
    u6 = Primitive(kind="pad", layer=1, net=P3V3, component=len(DESIGNATORS), x1=3000,
                   y1=3000, x2=3000, y2=3000, width=20, height=20, shape=2, pad_name="1")
    region = CopperArea(kind="region", layer=1, net=P3V3, component=None,
                        outline=[(2990, 2990), (3010, 2990), (3010, 3010), (2990, 3010)])
    netlist = _netlist(parts)
    index = _index(_copper() + [u6], areas=[region], netlist=netlist,
                   designators=DESIGNATORS + ["U6"])
    result = _run(index=index, parts=parts, current="3V3=1")
    row = _rail(result, "3V3")
    assert row["status"] == "estimated" and row["loads_without_track_path"] == ["U6"]
    (finding,) = _findings(result, "pour_fed_not_estimated")
    assert finding["loads_only_through_pour"] == [{"rail": "3V3", "loads": ["U6"]}]
    assert "no track path" in finding["issue"]


def test_checks_and_filters():
    result = _run(current="VIN=3", checks=["needs_current"])
    assert {f["check"] for f in result["findings"]} == {"needs_current"}
    assert result["checks_run"] == ["needs_current"]
    only = _run(rail_names=["vin"])
    assert [row["rail"] for row in only["rails"]] == ["VIN"]
    pcb_net = _run(nets=["SIG"])
    assert [row["rail"] for row in pcb_net["rails"]] == ["SIG"]
    assert _run(rail_names=["NOPE"])["error"] == "rail_not_found"
    assert _run(checks=["nope"])["error"] == "invalid_checks"
    assert _run(rise_info_c=30, rise_warning_c=20)["error"] == "invalid_thresholds"


# ---------- Power Analyzer ----------

REPORT = """<html><head><title>Power Analysis Report</title></head><body>
<h1>Full Report</h1>
<table><tr><th>Net</th><th>Status</th><th>Vdrop</th><th>Idensity</th><th>Ivia_max</th></tr>
<tr><td>3V3</td><td>Pass</td><td>12.5 mV</td><td>15.2 A/mm2</td><td>0.8 A</td></tr>
<tr><td>VIN</td><td>Fail</td><td>0.31 V</td><td>55 A/mm2</td><td>2.5 A</td></tr>
</table></body></html>"""
NET_PAGE = """<html><head><title>3V3</title></head><body><h1>3V3</h1>
<p>Status: Pass</p>
<table><tr><td>Vdrop</td><td>12.5 mV</td></tr><tr><td>Idensity</td><td>15.2 A/mm2</td></tr>
<tr><td>Ivia_max</td><td>0.8 A</td></tr></table>
<h2>Power Net Members</h2>
<table><tr><th>Designator</th><th>Type</th><th>Net</th><th>Current</th></tr>
<tr><td>U1</td><td>Source</td><td>3V3</td><td></td></tr>
<tr><td>U2</td><td>Load</td><td>3V3</td><td>150 mA</td></tr></table>
<h2>Least Margins</h2>
<table><tr><th>Parameter</th><th>Margin</th><th>Margin (%)</th></tr>
<tr><td>Non-Via Current Density</td><td>4.8 A/mm2</td><td>24 %</td></tr></table>
</body></html>"""


def _report(root: Path) -> Path:
    folder = root / "PowerAnalyzerByKeysight_Output" / "HTMLReport" / "Demo [PDNA]_2026-01-01_10-00"
    folder.mkdir(parents=True)
    (folder / "Report.html").write_text(REPORT, encoding="utf-8")
    (folder / "3V3.html").write_text(NET_PAGE, encoding="utf-8")
    return folder


def test_report_pages_are_parsed():
    page = pa.parse_report_page(REPORT, single_net=False)
    assert page["results"]["3V3"]["vdrop"] == {"text": "12.5 mV", "value": pytest.approx(0.0125),
                                               "unit": "V"}
    assert page["results"]["VIN"]["status"] == "fail"
    assert page["results"]["VIN"]["ivia"]["value"] == 2.5
    assert page["results"]["3V3"]["idensity"]["unit"] == "A/mm2"
    net = pa.parse_report_page(NET_PAGE)
    assert net["net"] == "3V3" and net["results"]["3V3"]["status"] == "pass"
    assert net["results"]["3V3"]["vdrop"]["value"] == pytest.approx(0.0125)
    assert [member["designator"] for member in net["members"]] == ["U1", "U2"]
    assert net["members"][1]["current"] == "150 mA"
    assert [table["heading"] for table in net["tables"]][-1] == "Least Margins"
    assert pa.parse_value("1.2 kV")["value"] is not None
    assert pa.parse_value("abc") is None


def test_reports_replace_the_estimate(tmp_path):
    folder = _report(tmp_path)
    found = pa.find_files(tmp_path)
    assert found["reports"] == [folder] and found["setups"] == []
    report = pa.read_report(folder)
    assert report["report_html"] and set(report["results"]) == {"3V3", "VIN"}
    result = _run(current="3V3=5", root_dir=str(tmp_path))
    row = _rail(result, "3V3")
    assert row["status"] == "power_analyzer" and row["estimate_status"] == "estimated"
    assert row["power_analyzer"]["vdrop"] == "12.5 mV"
    assert row["power_analyzer"]["vdrop_v"] == pytest.approx(0.0125)
    assert row["power_analyzer"]["status"] == "pass"
    assert not [f for f in result["findings"] if f.get("rail") == "3V3"]
    assert result["summary"]["rails"]["by_status"]["power_analyzer"] == 2
    assert result["power_analyzer"]["reports"][0]["folder"] == folder.name
    # A failing net is a warning of its own, counted; the estimate's findings it replaces
    # are counted in its row.
    (failed,) = _findings(result, "power_analyzer_fail")
    assert failed["rail"] == "VIN" and failed["severity"] == "warning"
    assert failed["vdrop"] == "0.31 V" and failed["ivia"] == "2.5 A"
    assert result["summary"]["severity_counts"]["warning"] >= 1
    assert result["summary"]["findings"]["power_analyzer_fail"] == {"warning": 1}
    vin = _rail(_run(current="VIN=3", root_dir=str(tmp_path)), "VIN")
    assert vin["estimate_findings"] == {"temp_rise_high info": 1}


def test_reports_must_name_this_project(tmp_path):
    folder = _report(tmp_path)
    ours = _run(current="3V3=5", root_dir=str(tmp_path), project_name="Demo",
                pcb_doc="Board.PcbDoc")
    assert _rail(ours, "3V3")["status"] == "power_analyzer"
    # Another project's report folder: listed, not applied.
    other = folder.parent / "Other [PDNA]_2026-01-01_10-00"
    folder.rename(other)
    theirs = _run(current="3V3=5", root_dir=str(tmp_path), project_name="Demo",
                  pcb_doc="Board.PcbDoc")
    assert _rail(theirs, "3V3")["status"] == "estimated"
    assert theirs["power_analyzer"]["reports"][0]["applies"] is False
    assert any("name neither this project" in note for note in theirs["notes"])
    assert not _findings(theirs, "power_analyzer_fail")
    # A page naming the PcbDoc applies it whatever its folder is called.
    (other / "Report.html").write_text(REPORT.replace("<h1>Full Report</h1>",
                                                      "<h1>Board.PcbDoc</h1>"),
                                       encoding="utf-8")
    named = _run(current="3V3=5", root_dir=str(tmp_path), project_name="Demo",
                 pcb_doc="Board.PcbDoc")
    assert _rail(named, "3V3")["status"] == "power_analyzer"
    assert pa.report_names("Demo [PDNA]_2026-01-01_10-00", ["demo"])
    assert not pa.report_names("Demo2 [PDNA]_2026-01-01_10-00", ["Demo"])


SETUP_XML = """<SimulatorManager xmlns="http://schemas.datacontract.org/2004/07/PDN"
 xmlns:i="http://www.w3.org/2001/XMLSchema-instance"><Name>Nominal</Name><Networks>
<NetworkModel><Name>RAIL</Name><PinModels>
<CompPinModel i:type="BatterySourceModelData"><PowerTerminal><Component>
<Designator>J1</Designator></Component><Net><Name>VIN</Name></Net><Pins><PinObject>
<Descriptor>J1-1</Descriptor></PinObject></Pins></PowerTerminal>
<VoltageParameter>12</VoltageParameter></CompPinModel>
<CompPinModel i:type="CurrentLoadModelData"><PowerTerminal><Component>
<Designator>U2</Designator></Component><Net><Name>3V3</Name></Net></PowerTerminal>
<LoadCurrentParameter>0.25</LoadCurrentParameter><MinVoltageCriteria>3.1</MinVoltageCriteria>
</CompPinModel></PinModels></NetworkModel></Networks>
<VoltageRegulators><VRMModelData i:type="LinearVRMModelData"><InNetwork>RAIL</InNetwork>
<OutNetwork>OUT</OutNetwork></VRMModelData></VoltageRegulators></SimulatorManager>"""
SETUP_JSON = {"SourcePowerNet": "3V3", "SourceModel": {
    "PowerNet": "3V3", "PowerComponent": "U1", "PowerPins": ["U1-2"],
    "SourceProperties": {"MinDCVoltage": 3.1, "MaxDCCurrent": 2.0}},
    "LoadModels": [{"PowerNet": "3V3", "PowerComponent": "U2", "PowerPins": ["U2-1"],
                    "LoadProperties": {"UseCurrent": True, "MaxDCCurrent": 0.4}}]}


def test_setups_are_read_and_listed(tmp_path):
    (tmp_path / "Demo.pdnaK").write_bytes(b"\x00\x01binary\x00" * 10)
    (tmp_path / "Nominal.pdna").write_text(SETUP_XML, encoding="utf-8")
    keysight = pa.read_setup(tmp_path / "Demo.pdnaK")
    assert keysight == {"file": "Demo.pdnaK", "kind": "pdnak", "read": False,
                        "format": "binary"}
    setup = pa.read_setup(tmp_path / "Nominal.pdna")
    assert setup["format"] == "xml" and setup["networks"] == ["RAIL"]
    assert setup["sources"] == [{"type": "BatterySourceModelData", "network": "RAIL",
                                 "part": "J1", "net": "VIN", "pins": ["J1-1"],
                                 "voltage_v": 12.0}]
    assert setup["loads"][0]["current_a"] == 0.25 and setup["loads"][0]["part"] == "U2"
    assert setup["vrms"] == [{"type": "LinearVRMModelData", "in_network": "RAIL",
                              "out_network": "OUT"}]
    # The only setup with loads gives U2's current.
    result = _run(current="3V3=5", root_dir=str(tmp_path))
    assert result["power_analyzer"]["currents_from_setup"] == "Nominal.pdna"
    kinds = {entry["file"]: entry for entry in result["power_analyzer"]["setups"]}
    assert "not plain text" in kinds["Demo.pdnaK"]["note"]
    path = next(p for p in _rail(result, "3V3")["paths"] if p["load"] == "U2")
    assert path["load_current"]["source"] == "setup"
    assert path["load_current"]["amps"] == 0.25
    # Two setups with loads: neither is used unless setup= names one.
    (tmp_path / "Other.pidc_config").write_text(json.dumps(SETUP_JSON), encoding="utf-8")
    two = _run(current="3V3=5", root_dir=str(tmp_path))
    assert "currents_from_setup" not in two["power_analyzer"]
    assert any("setup= names the one" in note for note in two["notes"])
    listed = {entry["file"]: entry for entry in two["power_analyzer"]["setups"]}
    assert listed["Other.pidc_config"]["loads_total"] == 1
    assert "loads" not in listed["Other.pidc_config"]  # counts only, none is in use
    named = _run(current="3V3=5", root_dir=str(tmp_path), setup="Other")
    path = next(p for p in _rail(named, "3V3")["paths"] if p["load"] == "U2")
    assert path["load_current"]["amps"] == 0.4


def test_a_setup_is_used_unasked_only_for_this_board(tmp_path):
    folder = tmp_path / "Config"
    folder.mkdir()
    named = SETUP_XML.replace("<Name>Nominal</Name>",
                              r"<BoardFileName>C:\Work\Board.PcbDoc</BoardFileName>"
                              "<Name>Nominal</Name>")
    (folder / "Nominal.pdna").write_text(named, encoding="utf-8")
    assert pa.read_setup(folder / "Nominal.pdna")["board"] == "Board.PcbDoc"
    ours = _run(current="3V3=5", root_dir=str(tmp_path), pcb_doc="board.pcbdoc")
    assert ours["power_analyzer"]["currents_from_setup"] == "Nominal.pdna"
    other = _run(current="3V3=5", root_dir=str(tmp_path), pcb_doc="Other.PcbDoc")
    assert "currents_from_setup" not in other["power_analyzer"]
    assert any("names another board" in note for note in other["notes"])
    # Without a board name, only a setup in the project folder itself is used.
    (folder / "Nominal.pdna").write_text(SETUP_XML, encoding="utf-8")
    nested = _run(current="3V3=5", root_dir=str(tmp_path), pcb_doc="Board.PcbDoc")
    assert "currents_from_setup" not in nested["power_analyzer"]


def test_json_setup():
    setup = pa._read_json_setup(json.dumps(SETUP_JSON))
    assert setup["networks"] == ["3V3"]
    assert setup["entries"][0] == {"kind": "source", "type": "SourceModel", "part": "U1",
                                   "net": "3V3", "pins": ["U1-2"], "min_voltage_v": 3.1,
                                   "max_current_a": 2.0}
    assert setup["entries"][1]["current_a"] == 0.4
    # Some setups name a part '<unique id>|<x>|<y>|U1': its last field.
    composite = {"SourcePowerNet": "3V3", "LoadModels": [
        {"PowerNet": "3V3", "PowerComponent": "ABCDEFGH|1234|5678|U2",
         "PowerPins": ["IJKLMNOP|1234|5678|U2-1"],
         "LoadProperties": {"UseCurrent": True, "MaxDCCurrent": 0.4}}]}
    (load,) = pa._read_json_setup(json.dumps(composite))["entries"]
    assert load["part"] == "U2" and load["pins"] == ["U2-1"]


def test_setup_currents_on_converters_resistors_and_other_parts(tmp_path):
    # A setup lists U1 (an LDO) on its input VIN, a 330 ohm resistive load on 3V3 and U2
    # by a composite name: each is used, the resistor as V / R; U99 is in no design.
    setup = {"SourcePowerNet": "VIN", "BoardFile": "Board.PcbDoc", "LoadModels": [
        {"PowerNet": "VIN", "PowerComponent": "U1",
         "LoadProperties": {"UseCurrent": True, "MaxDCCurrent": 0.6}},
        {"PowerNet": "3V3", "PowerComponent": "QWERTYUI|1|2|U2",
         "LoadProperties": {"UseCurrent": True, "MaxDCCurrent": 0.2}},
        {"PowerNet": "3V3", "PowerComponent": "R9",
         "LoadProperties": {"UseCurrent": False, "MinResistance": 330}},
        {"PowerNet": "3V3", "PowerComponent": "U99",
         "LoadProperties": {"UseCurrent": True, "MaxDCCurrent": 0.1}}]}
    (tmp_path / "Rails.pidc_config").write_text(json.dumps(setup), encoding="utf-8")
    parts = PARTS + [("R9", {"1": ("1", "3V3"), "2": ("2", "SIG")},
                      {"description": "Resistor", "value": "330"})]
    result = _run(parts=parts, root_dir=str(tmp_path), pcb_doc="Board.PcbDoc",
                  rails="3V3=3.3")
    vin = _rail(result, "VIN")
    paths = {p["load"]: p for p in vin["paths"]}
    assert paths["U1"]["load_current"] == {"amps": 0.6, "source": "setup",
                                           "basis": "Power Analyzer setup Rails.pidc_config"}
    three = _rail(result, "3V3")["current"]
    # U2 0.2 A and R9 3.3 V / 330 ohm; FB1's feed (U5) unknown.
    assert three["amps"] == pytest.approx(0.2 + 0.01) and three["partial"] is True
    assert any("U99@3V3" in note for note in result["notes"])


# ---------- paging and the tool ----------

def test_render_pages_rails_and_findings_together():
    prepared = pp.prepare_power_paths(_netlist(PARTS), index=_index(netlist=_netlist(PARTS)),
                                      current={"VIN": 3.0})
    first = pp.render_power_paths(prepared, offset=0, limit=2)
    assert len(first["rails"]) == 2 and first["has_more"] and first["next_offset"] == 2
    second = pp.render_power_paths(prepared, offset=2, limit=2)
    assert second["rails"][0]["rail"] not in {row["rail"] for row in first["rails"]}
    assert first["sources"].startswith("IPC-2221") and "IPC-2152" in first["sources"]
    assert "estimate" in first["estimate"].lower()


@pytest.fixture
def board(monkeypatch, tmp_path: Path):
    netlist = _netlist(PARTS)
    index = _index(netlist=netlist)
    pcb_file = tmp_path / "Board.PcbDoc"
    pcb_file.write_bytes(b"fake")
    monkeypatch.setattr(main_mod, "_project", {
        "name": "Demo", "root_dir": str(tmp_path), "prj_pcb_path": str(tmp_path / "D.PrjPcb"),
        "sheets": [], "pcb_doc_paths": [str(pcb_file)],
    })
    monkeypatch.setattr(main_mod._altium, "_netlist", netlist)
    monkeypatch.setattr(main_mod, "_variant_state",
                        VariantState([VariantDefinition(name="Default")]))
    monkeypatch.setattr(main_mod._pcb_session, "_load", lambda path, netlist: index)
    main_mod._pcb_session._index = None
    return netlist


def test_check_power_paths_tool(board):
    result = json.loads(main_mod.check_power_paths(current="3V3=5", rails="3V3=3.3"))
    assert result["pcb_doc"] == "Board.PcbDoc" and result["units"] == "mm"
    row = next(r for r in result["rails"] if r["rail"] == "3V3")
    path = next(p for p in row["paths"] if p["load"] == "U2")
    assert path["track_mm"] == pytest.approx(1456 * 0.0254, abs=1e-3)
    assert "resistance_mohm" in path
    assert result["assumptions"]["stub_length_mm"] == pytest.approx(1.0, abs=1e-3)
    mil = json.loads(main_mod.check_power_paths(current="3V3=5", units="mil",
                                                stub_length=10))
    assert mil["assumptions"]["stub_length_mil"] == 10.0
    hot = json.loads(main_mod.check_power_paths(current="3V3=5", temperature=70,
                                                plating=35, copper_oz="2"))
    assert hot["assumptions"]["temperature_c"] == 70.0 and hot["assumptions"]["plating_um"] == 35
    assert hot["assumptions"]["copper"]["L2"]["source"] == "copper_oz"
    one = json.loads(main_mod.check_power_paths(rail="VIN", net="SIG"))
    assert {r["rail"] for r in one["rails"]} == {"VIN", "SIG"}
    # One threshold past the other's default moves that default.
    moved = json.loads(main_mod.check_power_paths(rise_info=25))
    assert moved["assumptions"]["thresholds"]["rise_warning_c"] == 25.0
    lowered = json.loads(main_mod.check_power_paths(drop_warning=1))
    assert lowered["assumptions"]["thresholds"]["drop_info_pct"] == 1.0


def test_check_power_paths_argument_errors(board):
    errors = {
        "invalid_units": {"units": "inch"}, "invalid_plating": {"plating": 0},
        "invalid_temperature": {"temperature": 500}, "invalid_stub_length": {"stub_length": -1},
        "invalid_rise_info": {"rise_info": 0}, "invalid_drop_warning": {"drop_warning": 200},
        "invalid_current": {"current": "VIN"}, "invalid_eta": {"eta": "2"},
        "invalid_copper_oz": {"copper_oz": "x"}, "invalid_rails": {"rails": "A=x"},
        "invalid_checks": {"checks": ["nope"]}, "rail_not_found": {"rail": "NOPE"},
        "invalid_thresholds": {"rise_info": 30, "rise_warning": 20},
    }
    for code, kwargs in errors.items():
        assert json.loads(main_mod.check_power_paths(**kwargs))["error"] == code, code


def test_check_power_paths_needs_a_board(board, monkeypatch):
    monkeypatch.setitem(main_mod._project, "pcb_doc_paths", [])
    result = json.loads(main_mod.check_power_paths())
    assert result["error"] == "no_pcb_document" and "PcbDoc" in result["message"]


def test_check_power_paths_pages_and_shrinks(board, monkeypatch):
    first = json.loads(main_mod.check_power_paths(current="VIN=3"))
    assert "page_limited_to" not in first and first["rails_total"] == 4
    monkeypatch.setattr(main_mod, "QUERY_NET_MAX_RESULT_SIZE_CHARS", 9000)
    small = main_mod.check_power_paths(current="VIN=3")
    result = json.loads(small)
    assert len(small) <= 9000 and result["page_limited_to"] < 100
    assert result["next_offset"] == result["page_limited_to"]
    following = json.loads(main_mod.check_power_paths(current="VIN=3",
                                                      offset=result["next_offset"]))
    assert following["offset"] == result["next_offset"]


def test_check_power_paths_needs_a_project(monkeypatch):
    monkeypatch.setattr(main_mod, "_project", None)
    assert json.loads(main_mod.check_power_paths())["error"] == "no_project"
    assert "next_offset" in main_mod.check_power_paths.__doc__


def test_instructions_name_the_tool_after_the_cut():
    assert main_mod.SERVER_INSTRUCTIONS.index("check_power_paths") > 4096
    assert "check_power_paths" in main_mod.NETLIST_TOOLS
