"""Mechanical fit and the mechanical handoff (services/mechanical_fit, roadmap #34) on
synthetic boards: heights and their sources, limits per side, region, Height rule and
Draftsman note, keepouts, mounting holes and their free radius, the ICT probe side,
connectors against the board edge, Not Fitted parts, the DXF / SVG drawings, the
package_for_xfn tables, paging and the MCP tools."""

import json
import math
import sqlite3
import struct
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

import server.main as main
from export import MECHANICAL_TABLES, export_project
from parsers.pcb_doc import (
    PAD_SHAPE_RECT,
    PAD_SHAPE_ROUND,
    BoardInfo,
    ComponentBody,
    CopperArea,
    PcbClass,
    PcbComponent,
    PcbData,
    PcbLayer,
    PcbRule,
    Primitive,
    ShapeVertex,
    shape_points,
)
from parsers.prj_pcb import VariantState
from services import mechanical_fit as mf
from services.fab_drawing import NoteLine
from services.pcb_geometry import oriented_rect_vertices
from services.pcb_index import PcbIndex
from services.pcb_rules import ObjectFacts, RuleContext, RuleTarget, evaluate_scope

MM = 1 / 0.0254
UNITS = 10000
GND, NET_A, NET_B = range(3)
NETS = ["GND", "SIG_A", "SIG_B"]
LAYERS = {"top layer": 1, "bottom layer": 32, "top": 1, "bottom": 32}
WIDTH, HEIGHT = 4000.0, 3000.0
THICKNESS = 62.8  # 1.4 + 60 + 1.4 mil


# ---------- builders ----------

def _rect(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def _board_info():
    layers = [
        PcbLayer(1, "Top Layer", "copper", 0, copper_thick_mil=1.4),
        PcbLayer(None, "Core", "dielectric", 1, diel_height_mil=60.0),
        PcbLayer(32, "Bottom Layer", "copper", 2, copper_thick_mil=1.4),
        PcbLayer(56, "Keep-Out Layer", "non-stack", None),
        PcbLayer(66, "Component Keepout", "non-stack", None),  # mechanical 10
        PcbLayer(67, "Height 3mm Zone", "non-stack", None),  # mechanical 11
        PcbLayer(71, "Top Courtyard", "non-stack", None),  # mechanical 15
    ]
    board = BoardInfo(origin_x=0.0, origin_y=0.0, sheet_x=0.0, sheet_y=0.0,
                      sheet_width=10000.0, sheet_height=10000.0, display_unit="mil",
                      layers=layers, stackup_source="v9")
    board.outline_shape = [ShapeVertex(x, y) for x, y in _rect(0, 0, WIDTH, HEIGHT)]
    board.outline_vertices = shape_points(board.outline_shape)
    board.substacks = [{"id": "{RIGID}", "name": "Rigid", "is_flex": False},
                       {"id": "{FLEX}", "name": "Flex", "is_flex": True}]
    return board


def _part(index, name, x, y, side="top", rotation=0.0, footprint=None, height=None, kind=0,
          comment=None):
    return PcbComponent(index=index, refdes=name, source_uid_tail=None, channel_path=None,
                        x=x, y=y, rotation=rotation, side=side, footprint=footprint,
                        designator=name, comment=comment, kind=kind, height_mil=height)


def _smd(component, x, y, size=30.0, net=None, layer=1, name=None, rotation=0.0, **flags):
    return Primitive(kind="pad", layer=layer, net=net, component=component, x1=x, y1=y, x2=x,
                     y2=y, width=size, height=size, shape=PAD_SHAPE_RECT, rotation=rotation,
                     hole=0.0, pad_name=name, **flags)


def _tht(component, x, y, size, hole, net=None, plated=True, name="1"):
    return Primitive(kind="pad", layer=74, net=net, component=component, x1=x, y1=y, x2=x,
                     y2=y, width=size, height=size, shape=PAD_SHAPE_ROUND, hole=hole,
                     pad_name=name, plated=plated)


def _two_pads(component, x, y, layer=1, net=NET_A):
    return [_smd(component, x - 50, y, net=net, layer=layer, name="1"),
            _smd(component, x + 50, y, net=net, layer=layer, name="2")]


def _body(component, outline, height, standoff=0.0, side="top", model="part.step"):
    return ComponentBody(component=component, layer=69, outline=list(outline),
                         overall_height_mil=height, standoff_height_mil=standoff,
                         projection=side, model_type=1, model_name=model)


def _track(net, x1, y1, x2, y2, width=10.0, layer=1, component=None):
    return Primitive(kind="track", layer=layer, net=net, component=component, x1=x1, y1=y1,
                     x2=x2, y2=y2, width=width)


def _via(net, x, y, size=30.0, hole=12.0):
    return Primitive(kind="via", layer=1, layer_to=32, net=net, component=None, x1=x, y1=y,
                     x2=x, y2=y, width=size, hole=hole)


def _rule(kind, priority, scope="All", name=None, enabled=True, **props):
    return PcbRule(name=name or f"{kind}{priority}", kind=kind, kind_id=50, enabled=enabled,
                   priority=priority, scope1=scope, scope2="All", net_scope="AnyNet",
                   comment="", unique_id=None, from_schematic=False, properties=props)


def _height_rule(priority, scope, max_mil, min_mil=0.0, name=None):
    return _rule("Height", priority, scope, name=name, MINHEIGHT=f"{min_mil}mil",
                 MAXHEIGHT=f"{max_mil}mil", PREFHEIGHT=f"{max_mil}mil")


def _room(name, outline, side="TOP"):
    props = {"LAYER": side}
    for number, (x, y) in enumerate(outline):
        props[f"VX{number}"] = f"{x}mil"
        props[f"VY{number}"] = f"{y}mil"
    return _rule("RoomDefinition", 1, "All", name=name, **props)


def _region(layer, vertices, *, component=0xFFFF, keepout=False, kind=0, extra=""):
    """A ShapeBasedRegions6 record (or, with extra text, a BoardRegions one: Regions6
    layout)."""
    text = f"|KIND={kind}{extra}".encode() + b"\x00"
    head = (bytes([layer, 0, 2 if keepout else 0])
            + struct.pack("<HHH", 0xFFFF, 0xFFFF, component) + b"\x00" * 5
            + struct.pack("<H", 0) + b"\x00\x00" + struct.pack("<I", len(text)) + text)
    if extra:
        closed = [*vertices, vertices[0]]
        body = struct.pack("<I", len(closed)) + b"".join(
            struct.pack("<dd", x * UNITS, y * UNITS) for x, y in closed)
    else:
        body = struct.pack("<I", len(vertices))
        for x, y in [*vertices, vertices[0]]:
            body += struct.pack("<B5i2d", 0, round(x * UNITS), round(y * UNITS), 0, 0, 0,
                                0.0, 0.0)
    payload = head + body
    return bytes([11]) + struct.pack("<I", len(payload)) + payload


def _fill(layer, x1, y1, x2, y2, keepout=False):
    payload = (bytes([layer, 0, 2 if keepout else 0])
               + struct.pack("<HHH", 0xFFFF, 0xFFFF, 0xFFFF) + b"\x00" * 4
               + struct.pack("<iiii", *(round(v * UNITS) for v in (x1, y1, x2, y2)))
               + struct.pack("<d", 0.0))
    return bytes([6]) + struct.pack("<I", len(payload)) + payload


def _pcb(components=(), primitives=(), bodies=(), rules=(), classes=(), regions=(), fills=(),
         board_regions=(), areas=()):
    raw = {}
    if regions:
        raw["ShapeBasedRegions6/Data"] = b"".join(regions)
    if fills:
        raw["Fills6/Data"] = b"".join(fills)
    if board_regions:
        raw["BoardRegions/Data"] = b"".join(board_regions)
    return PcbData(board=_board_info(), nets=list(NETS), components=list(components),
                   primitives=list(primitives), pours=[], layer_name_to_v6=dict(LAYERS),
                   rules=list(rules), classes=list(classes), raw=raw, areas=list(areas),
                   bodies=list(bodies))


def _index(**kwargs):
    return PcbIndex(_pcb(**kwargs), None)


def _check(index, **kwargs):
    result = mf.prepare_mechanical_fit(index, **kwargs)
    assert "error" not in result, result
    return result


def _findings(result, check=None):
    return [finding for finding in result["findings"]
            if check is None or finding["check"] == check]


def _by_part(result, check):
    return {finding["part"]: finding for finding in _findings(result, check)}


# ---------- heights ----------

def _height_board(**extra):
    components = [
        _part(0, "U1", 1000, 1000),
        _part(1, "U2", 1500, 1000, height=50.0),
        _part(2, "U3", 2000, 1000),
        _part(3, "U4", 2500, 1000, rotation=45.0),
        _part(4, "C1", 1000, 2000, side="bottom"),
        _part(5, "U5", 1200, 1500),
        _part(6, "FID1", 3000, 2000),
        _part(7, "R1", 3000, 1000),
    ]
    primitives = [*_two_pads(0, 1000, 1000), *_two_pads(1, 1500, 1000), *_two_pads(2, 2000, 1000),
                  *_two_pads(4, 1000, 2000, layer=32), *_two_pads(5, 1200, 1500),
                  *_two_pads(7, 3000, 1000)]
    corners = oriented_rect_vertices(2500, 1000, 140, 60, 45.0)
    primitives += [_smd(3, *corners[0], name="1"), _smd(3, *corners[2], name="2")]
    primitives.append(_smd(6, 3000, 2000, size=40.0))  # a fiducial: one unnetted pad
    bodies = [
        _body(0, _rect(930, 970, 1070, 1030), 80.0, standoff=4.0, model="u1.step"),
        _body(0, _rect(930, 970, 1070, 1030), 999.0, side="bottom"),  # the other side
        _body(3, oriented_rect_vertices(2500, 1000, 200, 100, 45.0), 200.0),
        _body(4, _rect(930, 1970, 1070, 2030), 40.0, side="bottom"),
        _body(7, _rect(2930, 970, 3070, 1030), 300.0),
        _body(2, _rect(1950, 990, 2050, 1010), 0.0),  # a flat body gives no height
    ]
    return _index(components=components, primitives=primitives, bodies=bodies, **extra)


def test_heights_sources_standoff_and_not_fitted():
    index = _height_board()
    result = _check(index, library_heights={5: {"height_mil": 120.0, "standoff_mil": 2.0,
                                                "library": "Parts.PcbLib"}},
                    is_dnp=lambda name: name == "R1")
    rows = {row["part"]: row for row in result["parts"]}
    assert rows["U1"]["height_mil"] == 80.0 and rows["U1"]["height_source"] == "body"
    assert rows["U1"]["standoff_mil"] == 4.0 and rows["U1"]["model"] == "u1.step"
    assert rows["U1"]["extent_mil"] == [930.0, 970.0, 1070.0, 1030.0]
    assert rows["U2"]["height_source"] == "footprint_height" and rows["U2"]["height_mil"] == 50.0
    assert rows["U3"]["height_source"] == "none" and rows["U3"]["height_mil"] is None
    assert rows["U5"]["height_source"] == "library_body" and rows["U5"]["library"] == "Parts.PcbLib"
    assert rows["C1"]["side"] == "bottom" and rows["C1"]["height_mil"] == 40.0
    # Rotated: the body's extent is the rotated rectangle's box.
    half = (200 + 100) / 2 / math.sqrt(2)
    assert rows["U4"]["extent_mil"] == pytest.approx([2500 - half, 1000 - half,
                                                      2500 + half, 1000 + half], abs=1e-3)
    assert rows["U4"]["rotation"] == 45.0
    assert "R1" not in rows and result["not_fitted"] == ["R1"]
    assert result["summary"]["not_fitted"] == 1
    # Tallest first; FID1 has neither body nor height and is not expected to.
    assert [row["part"] for row in result["parts"]][:2] == ["U4", "U5"]
    missing = _by_part(result, "missing_3d_body")
    assert set(missing) == {"U2", "U3", "U5"}
    assert all(finding["severity"] == "info" for finding in missing.values())
    assert missing["U3"]["flat_bodies"] == 1 and "no 3D body and no Height" in missing["U3"]["issue"]
    assert "footprint's Height" in missing["U2"]["issue"]
    summary = result["summary"]
    assert summary["height_sources"] == {"body": 3, "footprint_height": 1, "none": 2,
                                         "library_body": 1}
    assert summary["missing_3d_body"] == 3 and summary["no_height"] == 1
    top = summary["sides"]["top"]
    assert top["tallest"][0] == {"part": "U4", "height_mil": 200.0, "height_source": "body"}
    assert top["height_classes_mm"] == {"1-2": 1, "2-3": 1, "3-5": 1, "5-10": 1}


def test_per_side_limits_and_no_height_warning():
    result = _check(_height_board(), height_limit_top_mil=100.0, height_limit_bottom_mil=30.0)
    over = _by_part(result, "height_over_limit")
    assert set(over) == {"U4", "C1", "R1"}  # R1 is fitted without a variant
    assert over["U4"]["limit_mil"] == 100.0 and over["U4"]["margin_mil"] == -100.0
    assert over["U4"]["limit_source"] == "user" and over["U4"]["limit"] == "height_limit_top"
    assert over["C1"]["limit_mil"] == 30.0 and over["C1"]["severity"] == "warning"
    missing = _by_part(result, "missing_3d_body")
    # U3 has no height at all and a limit applies: a warning.
    assert missing["U3"]["severity"] == "warning" and missing["U2"]["severity"] == "info"
    rows = {row["part"]: row for row in result["parts"]}
    assert rows["U1"]["limit_mil"] == 100.0 and rows["U1"]["limit_source"] == "user"
    assert result["limits"]["height_limit_top_mil"] == 100.0


def test_height_rules_resolved_per_component():
    classes = [PcbClass(name="Tall", kind="component", members=["U1"], superclass=False,
                        auto_generated=False, unique_id=None)]
    rules = [
        _height_rule(3, "All", 1000.0, name="Default"),
        _height_rule(2, "OnBottomLayer", 30.0, name="BottomRule"),
        _height_rule(1, "InComponentClass('Tall')", 60.0, name="ClassRule"),
        _height_rule(0, "WithinRoom('RoomA')", 40.0, name="RoomRule"),
        _height_rule(0, "IsSMTComponent And (Name = 'U4')", 150.0, min_mil=190.0,
                     name="NameRule"),
        _room("RoomA", _rect(1400, 900, 1600, 1100)),
    ]
    result = _check(_height_board(rules=rules, classes=classes))
    over = _by_part(result, "height_over_limit")
    assert over["U1"]["limit"] == "ClassRule" and over["U1"]["limit_mil"] == 60.0
    assert over["U2"]["limit"] == "RoomRule"  # 50 mil footprint Height in the room
    assert over["C1"]["limit"] == "BottomRule"
    assert over["U4"]["limit"] == "NameRule" and over["U4"]["limit_source"] == "height_rule"
    under = _findings(result, "height_under_min")
    assert not under  # U4 is 200 mil, above NameRule's 190 minimum
    stats = {row["rule"]: row["parts"] for row in result["summary"]["height_rules"]}
    assert stats["ClassRule"] == 1 and stats["RoomRule"] == 1 and stats["NameRule"] == 1
    assert {row["rule"] for row in result["limits"]["height_rules"]} >= {"ClassRule", "Default"}
    rows = {row["part"]: row for row in result["parts"]}
    assert rows["U3"]["limit_mil"] == 1000.0  # the default rule


def test_height_rule_minimum():
    rules = [_height_rule(1, "All", 1000.0, min_mil=60.0, name="MinRule")]
    result = _check(_height_board(rules=rules))
    under = {finding["part"] for finding in _findings(result, "height_under_min")}
    assert under == {"U2", "C1"}  # 50 and 40 mil; U3 has no height


def test_component_scopes_in_the_rule_evaluator():
    ctx = RuleContext()
    ctx.rooms = {"rooma"}
    ctx.room_shapes = {"rooma": _rect(0, 0, 100, 100)}
    ctx.component_classes = {"tall": {"u1"}}
    target = RuleTarget(kind="net", name="", nets=set(), pairs=set())

    def matches(scope, **facts):
        obj = ObjectFacts(**{"kind": "component", "layer": 1, "component": "u1",
                             "points": ((10, 10), (90, 10), (90, 90), (10, 90)), **facts})
        return evaluate_scope(scope, RuleTarget(**{**target.__dict__, "obj": obj}), ctx)["matches"]

    assert matches("IsComponent") == "yes"
    assert matches("IsSMTComponent") == "yes" and matches("IsThruComponent") == "no"
    assert matches("IsThruComponent", hole=30.0) == "yes"
    assert matches("IsVia") == "no" and matches("IsBGA") == "conditional"
    assert matches("WithinRoom('RoomA')") == "yes"
    assert matches("WithinRoom('RoomA')", points=((50, 50), (150, 50))) == "no"
    assert matches("TouchesRoom('RoomA')", points=((50, 50), (150, 50))) == "yes"
    assert matches("Name = 'U1'") == "yes" and matches("Name <> 'U1'") == "no"
    assert matches("InComponentClass('Tall')") == "yes" and matches("OnBottomLayer") == "no"
    assert matches("InNet('GND')") == "no"


# ---------- regions and keepouts ----------

def test_user_regions_free_bodies_and_named_layers():
    regions = [_region(67, _rect(1100, 1400, 1300, 1600))]  # 'Height 3mm Zone' around U5
    bodies_extra = [
        ComponentBody(component=None, layer=69, outline=_rect(2400, 900, 2600, 1100),
                      overall_height_mil=900.0, standoff_height_mil=150.0, projection="top",
                      model_type=1, model_name="lid.step"),
    ]
    index = _height_board(regions=regions)
    index.pcb.bodies.extend(bodies_extra)
    user = [{"name": "rib", "rect": [900, 950, 1100, 1050], "side": "top", "max_height": 70},
            {"polygon": [[1900, 900], [2100, 900], [2000, 1100]], "keepout": True}]
    result = _check(index, regions=user,
                    library_heights={5: {"height_mil": 120.0, "library": "Parts.PcbLib"}})
    over = _by_part(result, "height_over_limit")
    assert over["U1"]["limit_source"] == "user_region" and over["U1"]["limit"] == "rib"
    assert over["U4"]["limit_source"] == "free_body" and over["U4"]["limit_mil"] == 150.0
    assert over["U5"]["limit_source"] == "named_layer"
    assert over["U5"]["limit_mil"] == pytest.approx(3 * MM, abs=1e-3)
    keepout = _by_part(result, "part_in_keepout")
    assert set(keepout) == {"U3"} and keepout["U3"]["region"] == "region 2"
    summary = {row["name"]: row for row in result["regions"]}
    assert summary["rib"]["tallest"]["part"] == "U1" and summary["rib"]["max_height_mil"] == 70.0
    assert summary["lid.step"]["kind"] == "free_body" and summary["lid.step"]["standoff_mil"] == 150.0
    assert result["summary"]["regions"] == {"named_layer": 1, "free_body": 1, "user": 2}


def test_keepout_layer_copper_keepouts_rooms_and_named_keepouts():
    courtyard = [_track(None, *a, *b, width=2.0, layer=71, component=0) for a, b in (
        ((900, 940), (1100, 940)), ((1100, 940), (1100, 1060)), ((1100, 1060), (900, 1060)),
        ((900, 1060), (900, 940)))]
    regions = [
        _region(66, _rect(1080, 1000, 1250, 1100)),  # 'Component Keepout': U1's courtyard only
        _region(56, _rect(1900, 950, 2100, 1050)),  # Keep-Out layer area over U3
        _region(32, _rect(900, 1900, 1100, 2100), keepout=True),  # bottom copper keepout
        # U4's footprint's own copper keepout over its body: U4 is never in it.
        _region(1, _rect(2400, 950, 2600, 1050), keepout=True, component=3),
        _region(2, _rect(0, 0, 50, 50), keepout=True),  # an inner layer's: not mechanical
    ]
    rules = [_room("RoomB", _rect(2800, 900, 3200, 1100), side="TOP")]
    index = _height_board(regions=regions, rules=rules)
    index.pcb.primitives.extend(courtyard)
    index = PcbIndex(index.pcb, None)
    result = _check(index)
    keepout = _by_part(result, "part_in_keepout")
    assert set(keepout) == {"U1"} and keepout["U1"]["region_kind"] == "named_layer"
    named = {row["name"]: row for row in result["regions"]}
    assert set(named) == {"Component Keepout area 1", "Keep-Out area 1", "copper keepout 1",
                          "U4 copper keepout 2", "RoomB"}
    # U3 under the Keep-Out area has no height: counted, no tallest.
    keep_out = named["Keep-Out area 1"]
    assert keep_out["kind"] == "keepout_layer" and keep_out["parts"] == 1
    assert "tallest" not in keep_out
    bottom = named["copper keepout 1"]
    assert bottom["side"] == "bottom" and bottom["tallest"]["part"] == "C1"
    own = named["U4 copper keepout 2"]
    assert own["part"] == "U4" and own["side"] == "top" and own["parts"] == 0
    room = named["RoomB"]
    assert room["tallest"]["part"] == "R1" and "confinement" not in room
    rows = {row["part"]: row for row in result["parts"]}
    assert rows["U1"]["extent_basis"] == "body"


def test_courtyard_is_the_shape_without_a_body_and_keepout_fills():
    components = [_part(0, "U1", 1000, 1000, rotation=90.0)]
    primitives = [*_two_pads(0, 1000, 1000)]
    primitives += [_track(None, *a, *b, width=2.0, layer=71, component=0) for a, b in (
        ((940, 900), (1060, 900)), ((1060, 900), (1060, 1100)))]
    fills = [_fill(66, 1040, 1080, 1200, 1200)]
    index = _index(components=components, primitives=primitives, fills=fills)
    result = _check(index)
    row = result["parts"][0]
    assert row["extent_basis"] == "courtyard"
    assert row["extent_mil"] == pytest.approx([940, 900, 1060, 1100], abs=1e-6)
    assert _by_part(result, "part_in_keepout")["U1"]["region_kind"] == "named_layer"


def test_user_region_errors():
    index = _height_board()
    for regions, words in (
            ("x", "list"), ([{"rect": [0, 0, 0, 5]}], "rect"), ([{"name": "a"}], "polygon or"),
            ([{"rect": [0, 0, 1, 1], "side": "left"}], "side"),
            ([{"rect": [0, 0, 1, 1], "max_height": -1}], "max_height"),
            ([{"rect": [0, 0, 1, 1], "colour": 1}], "unknown keys"),
            ([{"polygon": [[0, 0], [1, 1]]}], "polygon")):
        result = mf.prepare_mechanical_fit(index, regions=regions)
        assert result["error"] == "invalid_regions" and words in result["message"], regions
    regions, error = mf.parse_user_regions([{"rect": [1, 2, 3, 4], "max_height": 1.0}], "mm",
                                           (100.0, 200.0))
    assert error is None
    assert regions[0].polygon[0] == pytest.approx((100 + MM, 200 + 2 * MM))
    assert regions[0].max_height_mil == pytest.approx(MM) and regions[0].side == "both"
    assert mf.parse_user_regions([{"rect": [0, 0, 1, 1], "max_height": 0}], "mil",
                                 (0, 0))[0][0].keepout


# ---------- Draftsman notes ----------

def _lines(*texts):
    return [NoteLine(text, f"page 1, note line {n}", "note") for n, text in enumerate(texts, 1)]


def test_draftsman_note_limits():
    notes = mf.note_height_limits([("Fab.PCBDwf", _lines(
        "Maximum component height on the bottom side: 1.0 mm.",
        "Max text height 2 mm",  # text, not a part
        "Board thickness 1.6 mm max",  # no height word
        "Components max height 5 mm"))])
    # The bottom gets both 1 mm and 5 mm (the last line names no side): a conflict; the
    # top only 5 mm. Text heights and the board's thickness are no part heights.
    assert notes["candidates"] == 2 and notes["conflicts"] == ["bottom"]
    assert notes["limits"]["top"]["max_mil"] == pytest.approx(5 * MM)
    assert notes["limits"]["top"]["source"] == "Fab.PCBDwf, page 1, note line 4"
    notes = mf.note_height_limits([("Fab.PCBDwf", _lines(
        "Max component height top side 3 mm", "Maximum component height bottom: 120 mil"))])
    assert notes["conflicts"] == []
    assert notes["limits"]["top"]["max_mil"] == pytest.approx(3 * MM)
    assert notes["limits"]["bottom"]["max_mil"] == pytest.approx(120.0)
    conflict = mf.note_height_limits([("A.PCBDwf", _lines("Max component height 3 mm")),
                                      ("B.PCBDwf", _lines("Max component height 4 mm"))])
    assert conflict["limits"] == {} and conflict["conflicts"] == ["top", "bottom"]


def test_draftsman_limit_is_applied_and_quoted():
    documents = [("Fab.PCBDwf", _lines("Max component height bottom side 1.0 mm"))]
    result = _check(_height_board(), note_documents=documents)
    over = _by_part(result, "height_over_limit")
    assert over["C1"]["limit_source"] == "draftsman_note"
    assert over["C1"]["note"] == "Max component height bottom side 1.0 mm"
    assert result["limits"]["draftsman_notes"]["bottom"]["source"].startswith("Fab.PCBDwf")
    conflict = [("A.PCBDwf", _lines("Max component height 3 mm")),
                ("B.PCBDwf", _lines("Max component height 4 mm"))]
    result = _check(_height_board(), note_documents=conflict)
    assert any("not used" in note for note in result["notes"])


# ---------- mounting holes ----------

def _hole_board(**extra):
    components = [
        _part(0, "MH1", 300, 300, footprint="MountingHole_3.2mm"),
        _part(1, "U9", 2200, 2560),  # its pad near hole C
        _part(2, "U8", 3600, 1800),  # its body near hole E
        _part(3, "J5", 600, 1200),  # a connector's big pins are not mounting holes
    ]
    primitives = [
        _tht(0, 300, 300, size=236.0, hole=126.0, net=GND),
        _via(GND, 450, 300),  # own net: no obstacle
        _track(NET_A, 300, 500, 700, 500),  # 200 - 5 = 195 from MH1 (top)
        # Free non-plated holes, each with one obstacle.
        _tht(None, 600, 2400, size=126.0, hole=126.0, plated=False, name=""),
        _track(NET_A, 500, 2600, 700, 2600),  # hole A: 195
        _tht(None, 1400, 2400, size=126.0, hole=126.0, plated=False, name=""),
        _via(NET_A, 1580, 2400),  # hole B: 180 - 15 = 165
        _tht(None, 2200, 2400, size=126.0, hole=126.0, plated=False, name=""),
        _smd(1, 2200, 2560, size=40.0, net=NET_B),  # hole C: 160 - 20 = 140
        _tht(None, 3000, 2400, size=126.0, hole=126.0, plated=False, name=""),
        # hole D: a bottom copper region 120 away (CopperArea below)
        _tht(None, 3400, 1800, size=126.0, hole=126.0, plated=False, name=""),
        # hole E: U8's body (below) 130 away
        _tht(None, 3900, 2850, size=126.0, hole=126.0, plated=False, name=""),  # edge 100
        _tht(3, 600, 1200, size=200.0, hole=150.0, net=NET_A, name="1"),
        _tht(3, 900, 1200, size=200.0, hole=150.0, net=NET_B, name="2"),
        _smd(2, 3700, 1800, size=20.0, net=NET_A),
    ]
    areas = [CopperArea(kind="region", layer=32, net=NET_B, component=None,
                        outline=_rect(3120, 2300, 3300, 2500))]
    bodies = [_body(2, _rect(3530, 1700, 3800, 1900), 50.0)]
    return _index(components=components, primitives=primitives, areas=areas, bodies=bodies,
                  **extra)


def _holes(result):
    return {(row["at_mil"][0], row["at_mil"][1]): row for row in result["mounting_holes"]}


def test_mounting_holes_found_with_their_free_radius():
    result = _check(_hole_board())
    holes = _holes(result)
    assert set(holes) == {(300, 300), (600, 2400), (1400, 2400), (2200, 2400), (3000, 2400),
                          (3400, 1800), (3900, 2850)}
    mh1 = holes[(300, 300)]
    assert mh1["hole"] == "MH1" and mh1["source"] == "part" and mh1["net"] == "GND"
    assert mh1["plated"] is True and mh1["pad_mil"] == 236.0 and mh1["screw"] == "M3"
    assert mh1["free_radius_mil"] == pytest.approx(195.0)
    assert mh1["limited_by"] == {"kind": "track", "side": "top", "net": "SIG_A"}
    assert mh1["own_net_copper_mil"] == pytest.approx(135.0)  # the GND via
    assert mh1["free_radius_bottom_mil"] == pytest.approx(300.0)  # the board edge
    expected = {(600, 2400): (195.0, "track"), (1400, 2400): (165.0, "via"),
                (2200, 2400): (140.0, "pad"), (3000, 2400): (120.0, "region"),
                (3400, 1800): (130.0, "body"), (3900, 2850): (100.0, "board_edge")}
    for at, (radius, kind) in expected.items():
        row = holes[at]
        assert row["free_radius_mil"] == pytest.approx(radius, abs=1e-6), at
        assert row["limited_by"]["kind"] in (kind, "part_pads"), at
        assert row["source"] == "free_pad" and row["plated"] is False and "pad_mil" not in row
    assert holes[(2200, 2400)]["limited_by"]["part"] == "U9"
    assert holes[(3400, 1800)]["limited_by"] == {"kind": "body", "side": "top", "part": "U8"}
    assert holes[(3000, 2400)]["limited_by"]["side"] == "bottom"
    assert holes[(3000, 2400)]["free_radius_top_mil"] > 500
    assert holes[(3900, 2850)]["edge_distance_mil"] == pytest.approx(37.0)


def test_mounting_hole_clearance_defaults_and_given_sizes():
    result = _check(_hole_board())
    findings = _findings(result, "mounting_hole_clearance")
    clearance = {tuple(f["at_mil"]): f for f in findings if "at_mil" in f}
    # M3 defaults: washer 7.0 mm / 2 + 0.5 mm = 4.0 mm = 157.5 mil. Parts and exposed
    # copper (pads, vias that are not tented) within it are findings; the edge alone is not
    # an obstacle for the washer (hole_near_edge covers it). Hole B's via at 165 mil clears
    # it; MH1's GND via is its own net.
    assert set(clearance) == {(2200, 2400), (3400, 1800)}
    assert all(f["severity"] == "info" for f in clearance.values())
    assert clearance[(2200, 2400)]["required_radius_mil"] == pytest.approx(4.0 * MM, abs=1e-3)
    assert clearance[(2200, 2400)]["limited_by"]["kind"] in ("pad", "part_pads")
    assert clearance[(3400, 1800)]["limited_by"]["part"] == "U8"
    # Copper under the mask only (hole D's bottom region): one grouped info finding.
    (masked,) = [f for f in findings if f.get("masked_copper_only")]
    assert masked["count"] == 1 and masked["holes"][0]["at_mil"] == [3000.0, 2400.0]
    assert masked["holes"][0]["side"] == "bottom" and masked["severity"] == "info"
    row = _holes(result)[(1400, 2400)]
    assert "M3 ISO 7089 washer" in row["required_basis"]
    assert row["exposed_free_radius_mil"] == pytest.approx(165.0)
    assert "exposed_free_radius_mil" not in _holes(result)[(3000, 2400)]  # none in 15 mm
    given = _check(_hole_board(), screw_head_mil=5.5 * MM, margin_mil=0.0)
    # No washer when only the head is given: 2.75 mm = 108.3 mil clears every obstacle.
    assert not _findings(given, "mounting_hole_clearance")
    assert "head" in _holes(given)[(1400, 2400)]["required_basis"]
    given = _check(_hole_board(), washer_mil=9.0 * MM)  # 5.0 mm: 196.9 mil
    findings = _findings(given, "mounting_hole_clearance")
    per_hole = {tuple(f["at_mil"]): f["severity"] for f in findings if "at_mil" in f}
    assert per_hole == {(1400, 2400): "warning", (2200, 2400): "warning",
                        (3400, 1800): "warning"}
    (masked,) = [f for f in findings if f.get("masked_copper_only")]
    assert masked["count"] == 3 and masked["severity"] == "info"  # MH1, A and D


def test_hole_near_edge_and_off_board_holes():
    result = _check(_hole_board())
    near = _findings(result, "hole_near_edge")
    assert [tuple(f["at_mil"]) for f in near] == [(3900, 2850)]
    # The board thickness (1.6 mm), at most 1 mm.
    assert near[0]["severity"] == "info"
    assert near[0]["min_distance_mil"] == pytest.approx(MM, abs=1e-3)
    index = _hole_board()
    index.pcb.primitives.append(_tht(None, 3980, 1500, size=100.0, hole=100.0, plated=False))
    index = PcbIndex(index.pcb, None)
    near = {tuple(f["at_mil"]): f for f in _findings(_check(index), "hole_near_edge")}
    assert near[(3980, 1500)]["severity"] == "warning"  # the wall crosses the edge
    index.pcb.primitives.append(_tht(None, 5000, 1500, size=100.0, hole=100.0, plated=False))
    index = PcbIndex(index.pcb, None)
    holes = _holes(_check(index))
    assert holes[(5000, 1500)]["off_board"] is True


def test_screw_sizes():
    assert mf.screw_for_hole(3.2 * MM)["thread"] == "M3"
    assert mf.screw_for_hole(3.1 * MM)["thread"] == "M3"
    assert mf.screw_for_hole(2.2 * MM)["thread"] == "M2"
    assert mf.screw_for_hole(4.3 * MM)["thread"] == "M4"
    assert mf.screw_for_hole(1.0 * MM) is None
    assert mf.screw_for_hole(8.5 * MM) is None  # larger than an M6's coarse hole + play
    m3 = mf.screw_for_hole(3.4 * MM)
    assert m3["head_mil"] == pytest.approx(5.6 * MM) and m3["washer_mil"] == pytest.approx(7 * MM)


# ---------- ICT ----------

def _ict_board(**extra):
    components = [_part(0, "TP1", 1000, 1000, side="bottom"),
                  _part(1, "TP2", 2000, 1000, side="bottom"),
                  _part(2, "E1", 1100, 1100, side="bottom"),
                  _part(3, "E2", 3000, 2500, side="top")]
    primitives = [
        _smd(0, 1000, 1000, layer=32, net=NET_A, size=40.0),
        _smd(1, 2000, 1000, layer=32, net=NET_B, size=40.0),
        _smd(2, 1100, 1100, layer=32, net=NET_A), _smd(2, 1200, 1100, layer=32, net=GND),
        _smd(3, 3000, 2500, net=NET_A), _smd(3, 3100, 2500, net=GND),
    ]
    bodies = [_body(2, _rect(1080, 1080, 1220, 1120), 250.0, side="bottom"),
              _body(3, _rect(2980, 2480, 3120, 2520), 300.0)]
    return _index(components=components, primitives=primitives, bodies=bodies, **extra)


def test_tall_parts_on_the_probe_side():
    result = _check(_ict_board())
    assert result["ict"]["probe_side"] == "bottom" and result["ict"]["chosen_by"] == "auto"
    tall = _by_part(result, "tall_part_on_probe_side")
    assert set(tall) == {"E1"} and tall["E1"]["severity"] == "info"
    assert tall["E1"]["probe_points_near"] == 1  # TP1; TP2 is 780 mil away
    given = _check(_ict_board(), probe_side="top")
    tall = _by_part(given, "tall_part_on_probe_side")
    assert set(tall) == {"E2"} and tall["E2"]["severity"] == "warning"
    assert given["ict"]["chosen_by"] == "given"
    low = _check(_ict_board(), tall_part_height_mil=400.0)
    assert not _findings(low, "tall_part_on_probe_side")
    none = _check(_height_board())
    assert none["ict"]["probe_side"] is None
    assert any("probe_side" in note for note in none["notes"])


# ---------- connectors ----------

def _connector_board():
    components = [_part(0, "J1", 100, 1500, rotation=90.0, comment="USB Type-C receptacle"),
                  _part(1, "J2", 2000, 2950, side="bottom", footprint="HDR-2x5"),
                  _part(2, "J3", 2000, 1500, comment="Crystal 25MHz"),  # not a connector
                  _part(3, "P1", 3500, 300, comment="Header")]
    primitives = [_smd(0, 150, 1450, net=NET_A), _smd(0, 150, 1550, net=GND),
                  _smd(1, 1950, 2940, layer=32, net=NET_A), _smd(1, 2050, 2940, layer=32),
                  _smd(2, 1950, 1500), _smd(2, 2050, 1500),
                  _smd(3, 3450, 300), _smd(3, 3550, 300)]
    bodies = [_body(0, _rect(-50, 1400, 200, 1600), 130.0),
              _body(1, _rect(1900, 2900, 2100, 2990), 100.0, side="bottom")]
    return _index(components=components, primitives=primitives, bodies=bodies)


def test_connectors_edges_offsets_and_overhang():
    result = _check(_connector_board())
    rows = {row["part"]: row for row in result["connectors"]}
    assert set(rows) == {"J1", "J2", "P1"}
    j1 = rows["J1"]
    assert j1["edge"] == "left" and j1["overhang_mil"] == pytest.approx(50.0)
    assert j1["edge_offset_mil"] == pytest.approx(-50.0) and j1["facing"] == "-X"
    assert j1["body_to_edge_mil"] == 0.0 and j1["pads_to_edge_mil"] == pytest.approx(135.0)
    assert j1["kind"] == "external" and j1["rotation"] == 90.0 and j1["basis"] == "body"
    j2 = rows["J2"]
    assert j2["side"] == "bottom" and j2["edge"] == "top" and j2["facing"] == "+Y"
    assert j2["edge_offset_mil"] == pytest.approx(10.0) and j2["overhang_mil"] == 0.0
    p1 = rows["P1"]
    assert p1["basis"] == "pads" and p1["edge"] == "bottom"
    assert p1["edge_offset_mil"] == pytest.approx(285.0) and "facing" not in p1
    # Ordered by offset: the overhanging one first.
    assert [row["part"] for row in result["connectors"]] == ["J1", "J2", "P1"]


# ---------- paging ----------

def test_render_pages_and_side_filter():
    result = _check(_height_board(), height_limit_top_mil=10.0, height_limit_bottom_mil=10.0)
    total = len(result["findings"])
    page = mf.render_mechanical_fit(result, offset=0, limit=2, parts_limit=3)
    assert len(page["findings"]) == 2 and page["has_more"] and page["next_offset"] == 2
    assert len(page["parts"]) == 3 and page["next_parts_offset"] == 3
    assert page["total"] == total and page["parts_total"] == len(result["parts"])
    last = mf.render_mechanical_fit(result, offset=total - 1, limit=2, parts_offset=6,
                                    parts_limit=3)
    assert not last["has_more"] and "next_parts_offset" not in last
    bottom = mf.render_mechanical_fit(result, side="bottom")
    assert {row["side"] for row in bottom["parts"]} == {"bottom"}
    assert all(f.get("side") in (None, "bottom") for f in bottom["findings"])
    assert mf.prepare_mechanical_fit(_height_board(), checks=["nope"])["error"] == "invalid_checks"
    only = _check(_height_board(), checks=["holes", "keepout"])
    assert only["checks_run"] == ["part_in_keepout", "mounting_hole_clearance", "hole_near_edge"]


def test_results_are_cached_per_arguments_and_variant():
    index = _height_board()
    first = mf.prepare_mechanical_fit_cached(index, height_limit_top_mil=100.0)
    assert mf.prepare_mechanical_fit_cached(index, height_limit_top_mil=100.0) is first
    other = mf.prepare_mechanical_fit_cached(index, height_limit_top_mil=100.0,
                                             is_dnp=lambda name: name == "R1")
    assert other is not first and other["not_fitted"] == ["R1"]


# ---------- handoff, drawings and the snapshot ----------

def _handoff_board():
    regions = [_region(56, _rect(1900, 950, 2100, 1050)),
               _region(74, _rect(3500, 2000, 3700, 2200), kind=3),  # a board cutout
               ]
    board_regions = [
        _region(56, _rect(0, 0, 2000, 3000), extra="|NAME=Main|LAYERSTACKID={RIGID}"),
        _region(56, _rect(2000, 0, 4000, 3000),
                extra="|NAME=Tail|LAYERSTACKID={FLEX}|BENDINGLINECOUNT=2"),
    ]
    index = _height_board(regions=regions, board_regions=board_regions)
    return index


def test_handoff_sections():
    handoff = mf.prepare_handoff(_handoff_board(), is_dnp=lambda name: name == "R1")
    board = handoff["board"]
    assert board["thickness_mil"] == pytest.approx(THICKNESS)
    assert board["size_mil"] == [WIDTH, HEIGHT]
    assert board["area_mm2"] == pytest.approx((WIDTH * HEIGHT - 200 * 200) / MM / MM, abs=0.1)
    outline = handoff["outline"]
    assert len(outline["polygon_mil"]) == 4 and len(outline["cutouts"]) == 1
    rigid = handoff["rigid_flex"]["board_regions"]
    assert [(r["name"], r["layer_stack"], r["is_flex"], r["bending_lines"]) for r in rigid] == [
        ("Main", "Rigid", False, 0), ("Tail", "Flex", True, 2)]
    assert handoff["regions"][0]["kind"] == "keepout_layer"
    assert handoff["not_fitted"] == ["R1"]
    sides = handoff["sides"]
    assert sides["top"]["parts"] == 6 and sides["bottom"]["parts"] == 1
    assert 0 < sides["top"]["area_share"] < 0.05
    page = mf.render_handoff(handoff, ["board", "outline"], polygons=False)
    assert page["outline"]["polygons_omitted"] and "polygon_mil" not in page["outline"]
    assert set(page) >= {"board", "outline", "origin_mil", "notes"} and "parts" not in page
    page = mf.render_handoff(handoff, list(mf.SECTIONS), parts_limit=2)
    assert len(page["parts"]) == 2 and page["next_parts_offset"] == 2
    assert mf.parse_sections(["board", "nope"])[1]["error"] == "invalid_sections"


def _dxf_pairs(text):
    lines = text.splitlines()
    assert len(lines) % 2 == 0
    return [(int(lines[n].strip()), lines[n + 1]) for n in range(0, len(lines), 2)]


def test_dxf_and_svg_written_only_to_the_folder_given(tmp_path):
    handoff = mf.prepare_handoff(_hole_board())
    written = mf.write_drawings(handoff, str(tmp_path), "Board")
    assert sorted(Path(path).name for path in written.values()) == [
        "Board-mechanical.dxf", "Board-mechanical.svg"]
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "Board-mechanical.dxf", "Board-mechanical.svg"]
    pairs = _dxf_pairs(Path(written["dxf"]).read_text(encoding="ascii"))
    assert pairs[:4] == [(0, "SECTION"), (2, "HEADER"), (9, "$ACADVER"), (1, "AC1009")]
    assert pairs[-1] == (0, "EOF")
    entities = [value for code, value in pairs if code == 0]
    assert entities.count("POLYLINE") == entities.count("SEQEND") >= 1
    layers = {value for code, value in pairs if code == 8}
    assert {"OUTLINE", "HOLES"} <= layers
    circles = entities.count("CIRCLE")
    assert circles == 7 + 1  # seven holes, MH1's pad ring
    # The outline polyline: 4 vertices in mm.
    xs = [float(value) for code, value in pairs if code == 10]
    assert max(xs) == pytest.approx(WIDTH / MM, abs=1e-3)
    root = ET.parse(written["svg"]).getroot()
    assert root.tag.endswith("svg") and root.get("width").endswith("mm")
    groups = {element.get("id") for element in root if element.tag.endswith("g")}
    assert {"outline", "holes"} <= groups
    only = mf.write_drawings(handoff, str(tmp_path / "."), "Other", formats=("svg",))
    assert list(only) == ["svg"]
    # A second write keeps the first files and numbers the new ones.
    again = mf.write_drawings(handoff, str(tmp_path), "Board")
    assert Path(again["dxf"]).name == "Board-mechanical-2.dxf"
    assert Path(written["dxf"]).read_text(encoding="ascii") == \
        Path(again["dxf"]).read_text(encoding="ascii")


def test_xfn_tables_rows_and_snapshot(tmp_path):
    handoff = mf.prepare_handoff(_handoff_board(), is_dnp=lambda name: name == "R1")
    rows = mf.export_rows(handoff, "Board.PcbDoc")
    assert set(rows) == set(MECHANICAL_TABLES)
    assert rows["mech_board"][0]["thickness_mm"] == pytest.approx(THICKNESS / MM, abs=1e-4)
    kinds = [row["kind"] for row in rows["mech_outline"]]
    assert kinds == ["outline", "cutout", "board_region", "board_region"]
    parts = {row["refdes"]: row for row in rows["mech_parts"]}
    assert parts["R1"]["fitted"] == 0 and parts["U4"]["height_mm"] == pytest.approx(200 / MM, abs=1e-4)
    project = {"name": "B", "root_dir": str(tmp_path), "sheets": []}
    path = export_project(project, {"nets": {}, "components": {}}, VariantState([]), "0.0.1",
                          mechanical=rows)
    conn = sqlite3.connect(path)
    try:
        counts = {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                  for table in MECHANICAL_TABLES}
        polygon = json.loads(conn.execute(
            "SELECT polygon FROM mech_outline WHERE kind = 'outline'").fetchone()[0])
        flex = json.loads(conn.execute(
            "SELECT data FROM mech_outline WHERE name = 'Tail'").fetchone()[0])
        height = conn.execute("SELECT height_mm, height_source FROM mech_parts "
                              "WHERE refdes = 'U2'").fetchone()
    finally:
        conn.close()
    assert counts["mech_board"] == 1 and counts["mech_parts"] == len(rows["mech_parts"])
    assert polygon[2] == [pytest.approx(WIDTH / MM, abs=1e-3), pytest.approx(HEIGHT / MM, abs=1e-3)]
    assert flex == {"layer_stack": "Flex", "is_flex": True, "bending_lines": 2}
    assert height[1] == "footprint_height"
    # A snapshot without a PcbDoc keeps the tables, empty.
    path = export_project({"name": "C", "root_dir": str(tmp_path), "sheets": []},
                          {"nets": {}, "components": {}}, VariantState([]), "0.0.1")
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT COUNT(*) FROM mech_parts").fetchone() == (0,)
    conn.close()


# ---------- the MCP tools ----------

@contextmanager
def _loaded(tmp_path, index, prj_extra=""):
    """main loaded on a project whose PcbDoc parses to the given index's data."""
    from tests.test_netlist_free_load import _environment

    gen = _environment(tmp_path, index.pcb, compiles=False)
    root, _, _ = next(gen)
    try:
        main.set_project_dir(str(root))
        yield root
    finally:
        gen.close()


def test_tools_end_to_end(tmp_path):
    with _loaded(tmp_path, _hole_board()) as root:
        result = json.loads(main.check_mechanical_fit(units="mm", height_limit_top=1.0))
        assert result["units"] == "mm" and result["pcb_doc"] == "Board.PcbDoc"
        assert result["parameters"]["clearance_margin_mm"] == pytest.approx(0.5)
        assert result["mounting_holes"][0]["free_radius_mm"] > 0
        assert result["limits"]["height_limit_top_mm"] == pytest.approx(1.0)
        for name, value in (("height_limit_top", -1), ("washer_diameter", float("nan")),
                            ("screw_head_diameter", 2000.0)):
            error = json.loads(main.check_mechanical_fit(units="mm", **{name: value}))
            assert error["error"] == f"invalid_{name}"
        assert json.loads(main.check_mechanical_fit(regions="x"))["error"] == "invalid_regions"
        handoff = json.loads(main.get_mechanical_handoff(sections=["board"], units="mm"))
        assert handoff["board"]["thickness_mm"] == pytest.approx(THICKNESS * 0.0254, abs=1e-4)
        assert "drawings" not in handoff and not list(root.glob("*.dxf"))
        out = tmp_path / "out"
        missing = json.loads(main.get_mechanical_handoff(output_dir=str(out)))
        assert missing["error"] == "output_dir_not_found"
        out.mkdir()
        written = json.loads(main.get_mechanical_handoff(output_dir=str(out), formats=["dxf"]))
        assert Path(written["drawings"]["dxf"]).parent == out
        assert [path.name for path in out.iterdir()] == ["Board-mechanical.dxf"]
        assert not list(root.glob("*mechanical*"))
        bad = json.loads(main.get_mechanical_handoff(sections=["nope"]))
        assert bad["error"] == "invalid_sections"


def test_tools_shrink_to_the_result_size(tmp_path, monkeypatch):
    with _loaded(tmp_path, _height_board()):
        text = main.check_mechanical_fit(height_limit_top=1.0)
        full = json.loads(text)
        assert "page_limited_to" not in full and full["total"] > 5
        handoff_text = main.get_mechanical_handoff()
        assert "page_limited_to" not in json.loads(handoff_text)
        limit = len(text) * 2 // 3
        monkeypatch.setattr(main, "QUERY_NET_MAX_RESULT_SIZE_CHARS", limit)
        small = main.check_mechanical_fit(height_limit_top=1.0)
        result = json.loads(small)
        assert result["page_limited_to"] < 100 and len(small) <= limit
        assert result["total"] == full["total"] and result["has_more"]
        monkeypatch.setattr(main, "QUERY_NET_MAX_RESULT_SIZE_CHARS", len(handoff_text) // 2)
        handoff = json.loads(main.get_mechanical_handoff())
        assert handoff["page_limited_to"] < 100


def test_package_for_xfn_writes_the_mechanical_tables(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "mark_xfn_exported", lambda name: None)
    project = {"name": "Board", "root_dir": str(tmp_path), "prj_pcb_path":
               str(tmp_path / "Board.PrjPcb"), "sheets": [],
               "pcb_doc_paths": [str(tmp_path / "Board.PcbDoc")]}
    netlist = {"nets": {}, "components": {}}
    out = main._package_for_xfn_impl(project, netlist, VariantState([]), "0.0.1",
                                     pcb_index=_hole_board())
    assert "Mechanical handoff: the board outline, 7 mounting holes" in out
    conn = sqlite3.connect(str(tmp_path / "Board-pcb-copilot.db"))
    assert conn.execute("SELECT COUNT(*) FROM mech_mounting_holes").fetchone() == (7,)
    assert conn.execute("SELECT pcb_doc FROM mech_board").fetchone() == ("Board.PcbDoc",)
    conn.close()

    def broken(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(main, "prepare_handoff", broken)
    out = main._package_for_xfn_impl(project, netlist, VariantState([]), "0.0.1",
                                     pcb_index=_hole_board())
    assert "Exported to:" in out
    assert "the mechanical tables could not be built (RuntimeError: boom)" in out


def test_no_outline_board():
    index = _height_board()
    index.pcb.board.outline_shape = []
    index.pcb.board.outline_vertices = []
    index = PcbIndex(index.pcb, None)
    result = _check(index)
    assert any("no outline" in note for note in result["notes"])
    handoff = mf.prepare_handoff(index)
    assert handoff["board"]["size_mil"] is None and handoff["outline"]["polygon_mil"] == []
    assert mf.dxf_text(handoff).endswith("EOF\n")
    with patch.object(mf, "SEARCH_MIL", 10.0):
        assert mf.svg_text(handoff).startswith("<?xml")


def test_tool_registration_manifest_readme_and_instructions():
    import asyncio

    from services.review_checklist import EVIDENCE_TOOLS

    repo = Path(__file__).parents[1]
    tools = {tool.name: tool for tool in asyncio.run(main.mcp.list_tools())}
    check = tools["check_mechanical_fit"]
    assert check.annotations.readOnlyHint is True and check.title == "Check Mechanical Fit"
    assert check.meta["anthropic/maxResultSizeChars"] == main.QUERY_NET_MAX_RESULT_SIZE_CHARS
    assert set(check.inputSchema["properties"]) == {
        "height_limit_top", "height_limit_bottom", "regions", "probe_side",
        "tall_part_height", "screw_head_diameter", "washer_diameter", "clearance_margin",
        "checks", "side", "units", "offset", "parts_offset"}
    handoff = tools["get_mechanical_handoff"]
    # It writes drawings into the folder the user names, never over a file.
    assert handoff.annotations.readOnlyHint is False
    assert handoff.annotations.destructiveHint is False
    assert set(handoff.inputSchema["properties"]) == {
        "sections", "output_dir", "formats", "units", "parts_offset"}
    manifest = json.loads((repo / "manifest.json").read_text("utf-8"))
    names = {entry["name"] for entry in manifest["tools"]}
    readme = (repo / "README.md").read_text("utf-8")
    changelog = (repo / "CHANGELOG.md").read_text("utf-8")
    for name in ("check_mechanical_fit", "get_mechanical_handoff"):
        assert name in names and f"| `{name}` |" in readme and f"`{name}`" in changelog
        assert main.SERVER_INSTRUCTIONS.index(name) > 4096
    assert "check_mechanical_fit" in EVIDENCE_TOOLS


def test_bare_holes_cutout_edges_and_hole_parts():
    from parsers.pcb_doc import BareHole

    components = [_part(0, "H2", 1000, 1000), _part(1, "SP1", 2000, 1000, kind=1)]
    primitives = [_tht(0, 1000, 1000, size=200.0, hole=126.0, net=GND),
                  # SP1: a Mechanical part of one plated hole with no net
                  _tht(1, 2000, 1000, size=160.0, hole=90.0, plated=True),
                  # too small to take a screw: left out
                  _tht(None, 3000, 1000, size=40.0, hole=40.0, plated=False)]
    cutout = _region(74, _rect(3300, 2000, 3500, 2200), kind=3)
    index = _index(components=components, primitives=primitives, regions=[cutout])
    index.pcb.bare_holes = [BareHole(x=3200.0, y=2100.0, hole=100.0, layer=74, net=None,
                                     component=None, pad_name="", plated=False)]
    index = PcbIndex(index.pcb, None)
    holes = _holes(_check(index))
    assert set(holes) == {(1000, 1000), (2000, 1000), (3200, 2100)}
    assert holes[(1000, 1000)]["hole"] == "H2" and holes[(1000, 1000)]["net"] == "GND"
    assert holes[(2000, 1000)]["hole"] == "SP1" and holes[(2000, 1000)]["screw"] == "M2"
    bare = holes[(3200, 2100)]
    assert bare["source"] == "bare_hole" and bare["plated"] is False
    assert bare["limited_by"] == {"kind": "cutout"} and bare["free_radius_mil"] == pytest.approx(100.0)
    assert bare["edge_distance_mil"] == pytest.approx(50.0)
    handoff = mf.prepare_handoff(index)
    assert len(handoff["outline"]["cutouts"]) == 1
    assert handoff["board"]["area_mm2"] == pytest.approx(
        (WIDTH * HEIGHT - 200 * 200) / MM / MM, abs=0.1)


def test_tools_without_a_pcbdoc(tmp_path):
    with _loaded(tmp_path, _height_board()) as root:
        prj = root / "Board.PrjPcb"
        prj.write_text(prj.read_text(encoding="utf-8").replace(
            "[Document3]\nDocumentPath=Board.PcbDoc\n", ""), encoding="utf-8")
        main.set_project_dir(str(root))
        for call in (main.check_mechanical_fit, main.get_mechanical_handoff):
            assert json.loads(call())["error"] == "no_pcb_document"


def test_regions_as_json_text_and_an_unreadable_pcbdoc_in_the_snapshot(tmp_path, monkeypatch):
    regions, error = mf.parse_user_regions('[{"rect": [0, 0, 10, 10], "keepout": true}]',
                                           "mil", (0.0, 0.0))
    assert error is None and regions[0].keepout and regions[0].kind == "user"
    monkeypatch.setattr(main, "mark_xfn_exported", lambda name: None)
    project = {"name": "Board", "root_dir": str(tmp_path), "prj_pcb_path":
               str(tmp_path / "Board.PrjPcb"), "sheets": []}
    out = main._package_for_xfn_impl(project, {"nets": {}, "components": {}},
                                     VariantState([]), "0.0.1", pcb_error="not an OLE file")
    assert "the mechanical tables could not be built (not an OLE file)" in out


def test_a_replaced_index_is_freed_with_its_model():
    import gc
    import weakref

    index = _hole_board()
    mf.prepare_mechanical_fit_cached(index)
    mf.prepare_handoff(index)
    index_ref = weakref.ref(index)
    model_ref = weakref.ref(next(iter(index._mechanical_fit_cache.values())))
    gc.disable()
    try:
        del index
        assert index_ref() is None and model_ref() is None
    finally:
        gc.enable()


# ---------- review fixes ----------

def _arc_region(layer, cx, cy, radius, kind=0, arcs=4):
    """A circle as `arcs` arc vertices (ShapeBasedRegions6)."""
    text = f"|KIND={kind}".encode() + b"\x00"
    head = (bytes([layer, 0, 0]) + struct.pack("<HHH", 0xFFFF, 0xFFFF, 0xFFFF) + b"\x00" * 5
            + struct.pack("<H", 0) + b"\x00\x00" + struct.pack("<I", len(text)) + text)
    step = 360.0 / arcs
    vertices = [(cx + radius * math.cos(math.radians(n * step)),
                 cy + radius * math.sin(math.radians(n * step)), n * step, (n + 1) * step)
                for n in range(arcs)]
    body = struct.pack("<I", len(vertices))
    for x, y, start, end in [*vertices, vertices[0]]:
        body += struct.pack("<B5i2d", 1, round(x * UNITS), round(y * UNITS), round(cx * UNITS),
                            round(cy * UNITS), round(radius * UNITS), start, end)
    payload = head + body
    return bytes([11]) + struct.pack("<I", len(payload)) + payload


def _rounded(index, radius):
    """The board's outline with a radius at each corner (arc vertices), a fresh index."""
    w, h, r = WIDTH, HEIGHT, radius
    index.pcb.board.outline_shape = [
        ShapeVertex(r, 0), ShapeVertex(w - r, 0, (w - r, r, r, -90.0, 0.0)),
        ShapeVertex(w, r), ShapeVertex(w, h - r, (w - r, h - r, r, 0.0, 90.0)),
        ShapeVertex(w - r, h), ShapeVertex(r, h, (r, h - r, r, 90.0, 180.0)),
        ShapeVertex(0, h - r), ShapeVertex(0, r, (r, r, r, 180.0, 270.0))]
    index.pcb.board.outline_vertices = shape_points(index.pcb.board.outline_shape)
    return PcbIndex(index.pcb, None)


def test_connector_facing_on_an_arc_corner():
    # The corner arc (r 500) is cut into pieces of about 100 mil whose chords lie more
    # than 2 mil inside the arc: the facing is the radial direction at the part.
    r = 500.0
    diagonal = (r - r / math.sqrt(2), r - r / math.sqrt(2))
    near_left = (r + r * math.cos(math.radians(-170)), r + r * math.sin(math.radians(-170)))
    components = [_part(0, "J1", *diagonal, comment="USB receptacle"),
                  _part(1, "J2", near_left[0] + 30, near_left[1], comment="USB receptacle")]
    primitives = [_smd(0, diagonal[0] + 60, diagonal[1] + 60, net=NET_A),
                  _smd(1, near_left[0] + 90, near_left[1], net=NET_A)]
    bodies = [_body(0, oriented_rect_vertices(*diagonal, 100, 100, 0.0), 100.0),  # a corner out
              _body(1, _rect(near_left[0] - 20, near_left[1] - 40, near_left[0] + 80,
                             near_left[1] + 40), 100.0)]
    index = _rounded(_index(components=components, primitives=primitives, bodies=bodies), r)
    rows = {row["part"]: row for row in _check(index)["connectors"]}
    assert rows["J1"]["facing"] == "-135 deg" and rows["J1"]["edge"] == "outline"
    assert rows["J1"]["overhang_mil"] > 0
    assert rows["J2"]["facing"] == "-X" and rows["J2"]["edge"] == "left"


def test_screw_sizes_of_holes_on_altiums_grid():
    # Altium keeps 0.0001 mil: 2.6 mm is 102.3622 mil, a hair under 2.6 mm.
    for size, thread in ((1.7, "M1.6"), (2.6, "M2.5"), (3.1, "M3"), (4.1, "M4"), (5.1, "M5"),
                         (7.6, "M6")):
        assert mf.screw_for_hole(round(size * MM, 4))["thread"] == thread, size


def test_mounting_hole_rings_of_smd_pads():
    # An NPTH with an SMD copper ring on each face: a mounting hole all the same, its ring
    # its pad; a free hole's free ring pads are its own, no obstacle.
    components = [_part(0, "MH1", 1000, 1000, footprint="MountingHole_3.2mm")]
    primitives = [_tht(0, 1000, 1000, size=126.0, hole=126.0, plated=False),
                  _smd(0, 1000, 1000, size=236.0, net=GND, layer=1, name="1"),
                  _smd(0, 1000, 1000, size=236.0, net=GND, layer=32, name="1"),
                  _track(NET_A, 800, 1180, 1200, 1180),
                  _tht(None, 2500, 1000, size=126.0, hole=126.0, plated=False, name=""),
                  _smd(None, 2500, 1000, size=220.0, layer=1),
                  _smd(None, 2500, 1000, size=220.0, layer=32),
                  _track(NET_A, 2300, 1300, 2700, 1300)]
    holes = _holes(_check(_index(components=components, primitives=primitives)))
    mh1 = holes[(1000, 1000)]
    assert mh1["hole"] == "MH1" and mh1["source"] == "part" and mh1["pad_mil"] == 236.0
    assert mh1["free_radius_mil"] == pytest.approx(175.0) and mh1["plated"] is False
    free = holes[(2500, 1000)]
    assert free["pad_mil"] == 220.0 and free["free_radius_mil"] == pytest.approx(295.0)
    assert free["limited_by"]["kind"] == "track"
    # SMD pads away from the hole: not a mounting hole.
    primitives[1] = _smd(0, 1300, 1000, size=60.0, net=NET_B, name="2")
    holes = _holes(_check(_index(components=components, primitives=primitives)))
    assert (1000, 1000) not in holes


def test_card_edge_fingers_need_no_body():
    def board(gap):
        components = [_part(0, "P1", 2000, 10 + gap + 50)]
        primitives = [_smd(0, 1800 + 50 * n, 10 + gap + 50, layer=layer, net=NET_A,
                           name=str(n))
                      for n in range(8) for layer in (1, 32)]
        for pad in primitives:
            pad.width, pad.height = 20.0, 100.0
        return _index(components=components, primitives=primitives)

    fingers = _check(board(0.0), height_limit_top_mil=100.0)
    assert not _findings(fingers, "missing_3d_body")
    assert fingers["parts"][0]["card_edge"] is True
    assert fingers["summary"]["missing_3d_body"] == 0
    inside = _check(board(300.0), height_limit_top_mil=100.0)
    assert _by_part(inside, "missing_3d_body")["P1"]["severity"] == "warning"


def test_draftsman_notes_local_split_and_tallest():
    notes = mf.note_height_limits([("Fab.PCBDwf", _lines(
        "Max component height 5mm, except connectors",
        "Component height 4 mm max under the shield",
        "Max height 3 mm"))])  # no part word: not a component height
    assert notes["limits"] == {} and notes["candidates"] == 2
    assert [item["quote"] for item in notes["local"]] == [
        "Max component height 5mm, except connectors",
        "Component height 4 mm max under the shield"]
    split = mf.note_height_limits([("Fab.PCBDwf", _lines(
        "Maximum part height: top side 6.5 mm, bottom side 2 mm"))])
    assert split["limits"]["top"]["max_mil"] == pytest.approx(6.5 * MM)
    assert split["limits"]["bottom"]["max_mil"] == pytest.approx(2 * MM)
    after = mf.note_height_limits([("Fab.PCBDwf", _lines(
        "Component height: 6 mm max on top, 2 mm on the bottom"))])
    assert after["limits"]["bottom"]["max_mil"] == pytest.approx(2 * MM)
    unclear = mf.note_height_limits([("Fab.PCBDwf", _lines(
        "Component height 3 mm max, 4 mm with a heatsink"))])
    assert unclear["limits"] == {} and unclear["unclear"] == 1
    tallest = mf.note_height_limits([("Fab.PCBDwf", _lines(
        "Tallest component must not exceed 8 mm"))])
    assert tallest["limits"]["top"]["max_mil"] == pytest.approx(8 * MM)
    enclosure = mf.note_height_limits([("Fab.PCBDwf", _lines(
        "Max component height within the enclosure: 12 mm"))])
    assert enclosure["limits"]["bottom"]["max_mil"] == pytest.approx(12 * MM)
    result = _check(_height_board(), note_documents=[("Fab.PCBDwf", _lines(
        "Max component height 1 mm except near the connector"))])
    assert not _findings(result, "height_over_limit")
    assert result["limits"]["draftsman_local_notes"][0]["source"].startswith("Fab.PCBDwf")
    assert any("draftsman_local_notes" in note for note in result["notes"])


def test_rigid_flex_thickness_per_layer_stack():
    from parsers.pcb_doc import decode_board
    from services.dfm import stack_total_thickness

    properties = {"V9_SUBSTACK0_ID": "{R}", "V9_SUBSTACK0_NAME": "Rigid",
                  "V9_SUBSTACK1_ID": "{F}", "V9_SUBSTACK1_NAME": "Flex",
                  "V9_SUBSTACK1_ISFLEX": "TRUE"}
    for order, (name, key, value, flex) in enumerate((
            ("Top Layer", "COPTHICK", "1.4mil", "1"), ("Prepreg", "DIELHEIGHT", "30mil", "1"),
            ("Polyimide", "DIELHEIGHT", "4mil", "0"),
            ("Bottom Layer", "COPTHICK", "1.4mil", "0"))):
        prefix = f"V9_STACK_LAYER{order}_"
        properties.update({f"{prefix}NAME": name, f"{prefix}{key}": value,
                           f"{prefix}{{R}}CONTEXT": "0", f"{prefix}{{F}}CONTEXT": flex})
    board = decode_board([properties])
    assert [layer.substacks for layer in board.layers[:4]] == [
        frozenset({"{R}"}), frozenset({"{R}"}), frozenset({"{R}", "{F}"}),
        frozenset({"{R}", "{F}"})]
    assert stack_total_thickness(board.layers) == pytest.approx(36.8)
    assert stack_total_thickness(board.layers, "{F}") == pytest.approx(5.4)
    assert stack_total_thickness(board.layers, "{X}") is None  # a stack holding no layer
    # The handoff: each board region its stack's thickness.
    index = _handoff_board()
    for layer in index.pcb.board.layers:
        if layer.stack_order is not None:
            layer.substacks = frozenset({"{RIGID}", "{FLEX}"}) if layer.v6_id == 32 \
                else frozenset({"{RIGID}"})
    handoff = mf.prepare_handoff(index)
    regions = {row["name"]: row for row in handoff["rigid_flex"]["board_regions"]}
    assert regions["Main"]["thickness_mil"] == pytest.approx(THICKNESS)
    assert regions["Tail"]["thickness_mil"] == pytest.approx(1.4)
    assert "rigid regions" in handoff["board"]["thickness_basis"]
    stacks = {row["name"]: row["thickness_mil"] for row in handoff["rigid_flex"]["substacks"]}
    assert stacks == {"Rigid": pytest.approx(THICKNESS), "Flex": pytest.approx(1.4)}
    rows = mf.export_rows(handoff)
    tail = next(row for row in rows["mech_outline"] if row["name"] == "Tail")
    assert tail["data"]["thickness_mm"] == pytest.approx(1.4 / MM, abs=1e-4)
    assert "rigid regions" in rows["mech_board"][0]["data"]["thickness_basis"]


def test_round_cutouts_and_holes_in_cutouts():
    regions = [_arc_region(74, 3000, 1500, 150, kind=3),
               _region(74, _rect(1900, 1400, 2100, 1600), kind=3)]
    primitives = [_tht(None, 2000, 1500, size=126.0, hole=126.0, plated=False, name="")]
    index = _index(primitives=primitives, regions=regions)
    handoff = mf.prepare_handoff(index)
    round_cut = max(handoff["outline"]["cutouts"], key=lambda cut: len(cut["polygon_mil"]))
    assert len(round_cut["polygon_mil"]) > 16  # the arcs as chords, not 4 corners
    assert all(math.hypot(x - 3000, y - 1500) == pytest.approx(150, abs=0.5)
               for x, y in round_cut["polygon_mil"])
    expected = (WIDTH * HEIGHT - math.pi * 150 ** 2 - 200 * 200) / MM / MM
    assert handoff["board"]["area_mm2"] == pytest.approx(expected, abs=0.2)
    result = _check(index)
    (hole,) = result["mounting_holes"]
    assert hole["off_board"] is True and hole["in_cutout"] is True
    assert not _findings(result, "hole_near_edge")


def test_not_fitted_mounting_hole_parts_keep_their_hole():
    given = _check(_hole_board(), washer_mil=9.0 * MM, is_dnp=lambda name: name == "MH1")
    mh1 = _holes(given)[(300, 300)]
    assert mh1["fitted"] is False and mh1["free_radius_mil"] == pytest.approx(195.0)
    (masked,) = [f for f in _findings(given, "mounting_hole_clearance")
                 if f.get("masked_copper_only")]
    assert masked["count"] == 2  # A and D; MH1 takes no screw
    handoff = mf.prepare_handoff(_hole_board(), is_dnp=lambda name: name == "MH1")
    assert "MH1" in [row["hole"] for row in handoff["mounting_holes"]]
    # mech_parts names Not Fitted parts by the snapshot's refdes (the netlist name).
    handoff["_not_fitted_refdes"] = ["MH1_SCH"]
    rows = mf.export_rows(handoff)
    assert [row["refdes"] for row in rows["mech_parts"] if not row["fitted"]] == ["MH1_SCH"]
    fitted = {row["refdes"]: row["fitted"] for row in rows["mech_mounting_holes"]}
    assert fitted["MH1"] == 0 and set(fitted.values()) == {0, 1}


def test_checklist_leans_only_with_a_real_height_limit():
    from services import review_checklist as rc

    bundled = rc.load_bundled(Path(main.__file__).parent / "checklists")
    item = next(item for checklist in bundled.values() for item in checklist["items"]
                if item["id"] == "PCB-PLC-07")
    rules = [_height_rule(1, "All", 1000.0, name="Default")]
    plain = _check(_height_board(rules=rules), checks=["heights", "keepout"])
    assert plain["summary"]["parts_with_height_limit"] == 0  # the default rule is no limit
    findings = [f for f in plain["findings"] if f["check"] != "missing_3d_body"]
    row = rc.evaluate_item(item, {"data": {**plain, "findings": findings,
                                           "pcb_doc": "Board.PcbDoc"}})
    assert row["status"] == "needs_judgment" and "lean" not in row
    limited = _check(_height_board(rules=rules), checks=["heights", "keepout"],
                     height_limit_top_mil=5000.0, height_limit_bottom_mil=5000.0)
    assert limited["summary"]["parts_with_height_limit"] > 0
    findings = [f for f in limited["findings"] if f["check"] != "missing_3d_body"]
    row = rc.evaluate_item(item, {"data": {**limited, "findings": findings,
                                           "pcb_doc": "Board.PcbDoc"}})
    assert row.get("lean") == "yes"


def test_tall_part_on_the_probe_side_points_to_check_testpoints():
    tall = _by_part(_check(_ict_board()), "tall_part_on_probe_side")
    assert tall["E1"]["see_also"] == "check_testpoints (tall_parts)"
    given = _by_part(_check(_ict_board(), probe_side="top"), "tall_part_on_probe_side")
    assert "see_also" not in given["E2"]  # no probe point near it
