"""Assembly facts of check_dfm (services/dfm_assembly): parts, fiducials, placements,
part spacing, part to edge and tombstoning on synthetic boards, and on Altium's examples
(skipped when absent)."""

import gc
import math
import weakref
from collections import Counter
from pathlib import Path

import pytest

from server.parsers.pcb_doc import (
    PAD_SHAPE_RECT,
    PAD_SHAPE_ROUND,
    ComponentBody,
    CopperArea,
    PcbComponent,
    PcbData,
    PolygonPour,
    Primitive,
    parse_pcb_doc,
)
from server.services import dfm_assembly as assembly
from server.services import dfm_geometry as geometry
from server.services.pcb_geometry import oriented_rect_vertices
from server.services.pcb_index import PcbIndex
from tests.test_dfm_geometry import OUTLINE
from tests.test_pcb_health import _region, _square
from tests.test_pcb_index import _board

MM = 1 / 0.0254
GND, A, B, C = range(4)
NETS = ["GND", "A", "B", "C"]
LAYERS = {"top layer": 1, "l2-gnd": 2, "l3-sig": 3, "bottom layer": 32, "top": 1, "bottom": 32}
EXAMPLES = Path(r"C:\Users\Public\Documents\Altium\AD25\Examples")


def _part(index, designator, x=0.0, y=0.0, rotation=0.0, side="top", footprint=None,
          comment=None, kind=0):
    return PcbComponent(
        index=index, refdes=designator, source_uid_tail=None, channel_path=None, x=x, y=y,
        rotation=rotation, side=side, footprint=footprint, designator=designator,
        comment=comment, kind=kind,
    )


def _smd(component, x, y, width=20.0, height=None, net=None, layer=1, shape=PAD_SHAPE_RECT,
         rotation=0.0, name=None):
    """An SMD pad; no name (the default) makes it a terminal of its own unless its copper
    touches another pad's."""
    return Primitive(kind="pad", layer=layer, net=net, component=component, x1=x, y1=y, x2=x,
                     y2=y, width=width, height=height or width, shape=shape, rotation=rotation,
                     hole=0.0, pad_name=name)


def _tht(component, x, y, size=60.0, hole=35.0, plated=True, net=None):
    return Primitive(kind="pad", layer=74, net=net, component=component, x1=x, y1=y, x2=x,
                     y2=y, width=size, height=size, shape=PAD_SHAPE_ROUND, hole=hole,
                     pad_name="1", plated=plated)


def _track(net, x1, y1, x2, y2, width=6.0, layer=1, poured_by=None):
    return Primitive(kind="track", layer=layer, net=net, component=None, x1=x1, y1=y1, x2=x2,
                     y2=y2, width=width, poured_by=poured_by)


def _body(component, outline, height=40.0, projection="top"):
    return ComponentBody(component=component, layer=69, outline=list(outline),
                         overall_height_mil=height, standoff_height_mil=0.0,
                         projection=projection, model_type=1)


def _rect(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def _area(net, outline, holes=(), layer=1):
    return CopperArea(kind="region", layer=layer, net=net, component=None, outline=outline,
                      holes=[list(hole) for hole in holes])


def _index(components=(), primitives=(), bodies=(), areas=(), outline=None, regions=b"",
           pours=(), bare_holes=()):
    board = _board()
    if outline is not None:
        board.outline_shape = list(outline)
    pcb = PcbData(
        board=board, nets=list(NETS), components=list(components),
        primitives=list(primitives), pours=list(pours), layer_name_to_v6=dict(LAYERS),
        raw={"ShapeBasedRegions6/Data": regions} if regions else {}, areas=list(areas),
        bodies=list(bodies), bare_holes=list(bare_holes),
    )
    return PcbIndex(pcb, None)


def _sides(polygon):
    closed = list(polygon) + [polygon[0]]
    return [round(math.dist(a, b), 6) for a, b in zip(closed, closed[1:])]


# ---------- parts ----------


def test_mount_classes_pitch_and_bodies():
    components = [
        _part(0, "U1"), _part(1, "J1"), _part(2, "J2"), _part(3, "MH1", kind=5),
        _part(4, "LOGO1", kind=2), _part(5, "Q1"),
    ]
    primitives = [_smd(0, x, y, 10.0, 30.0) for x in (100, 120, 140, 160) for y in (100, 160)]
    primitives += [_tht(1, 300, 100), _tht(1, 400, 100), _tht(1, 350, 200, plated=False)]
    primitives += [_smd(2, x, 500) for x in (100, 150, 200, 250)]
    primitives += [_tht(2, 50, 520, plated=False), _tht(2, 300, 520, plated=False)]
    primitives += [_tht(3, 600, 600, size=120.0, hole=120.0, plated=False)]
    primitives += [_smd(5, 800, 100), _tht(5, 900, 100, plated=None)]
    bodies = [
        _body(0, _rect(80, 110, 180, 150), height=40.0),
        _body(0, _rect(90, 115, 170, 145), height=55.0),
        _body(0, _rect(80, 110, 180, 150), height=99.0, projection="bottom"),  # other side
        _body(0, [(0, 0), (1, 1)], height=200.0),  # no area
        _body(None, _rect(0, 0, 10, 10)),  # a free body
    ]
    index = _index(components, primitives, bodies)
    parts = assembly.parts(index)
    assert [(part.designator, part.mount) for part in parts] == [
        ("U1", "smd"), ("J1", "tht"), ("J2", "smd"), ("MH1", "mechanical"),
        ("LOGO1", "no_pads"), ("Q1", "mixed"),
    ]
    u1, j1, j2, _, _, q1 = parts
    assert (u1.smd_pads, u1.pitch_mil, len(u1.pads), u1.side) == (8, 20.0, 8, "top")
    assert [body.overall_height_mil for body in u1.bodies] == [40.0, 55.0]
    assert u1.height_mil == 55.0 and u1.body_box == (80, 110, 180, 150)
    assert (j1.plated_pads, j1.npth_pads, j1.pitch_mil) == (2, 1, None)
    assert j2.pitch_mil == 50.0  # its SMD pads; the non-plated pegs are no soldering
    assert (q1.smd_pads, q1.plated_pads) == (1, 1)  # unknown plating counts as plated
    assert u1.pads == tuple(range(1, 9))  # prims row ids
    assert index.pcb.primitives[u1.pads[0] - 1].component == 0
    assert assembly.parts(index) is parts  # cached


def test_pad_extent_is_a_rectangle_in_the_part_frame():
    angle = 30.0
    cosine, sine = math.cos(math.radians(angle)), math.sin(math.radians(angle))
    pads = [_smd(0, 500 + dx * cosine, 500 + dx * sine, 10.0, rotation=angle) for dx in (-20, 20)]
    (part,) = assembly.parts(_index([_part(0, "R1", 500, 500, rotation=angle)], pads))
    assert _sides(part.pad_rect) == [50.0, 10.0, 50.0, 10.0]
    assert part.centre == pytest.approx((500.0, 500.0))
    assert part.span_mil == pytest.approx(50.0)


def test_chip_size_codes():
    def chip(index, designator, centre, half, size, footprint=None, rotation=0.0, body=None):
        cosine, sine = math.cos(math.radians(rotation)), math.sin(math.radians(rotation))
        pads = [_smd(index, centre[0] + d * cosine, centre[1] + d * sine, size, rotation=rotation)
                for d in (-half, half)]
        bodies = [] if body is None else [_body(index, oriented_rect_vertices(
            centre[0], centre[1], body[0] * MM, body[1] * MM, rotation))]
        return _part(index, designator, *centre, rotation, footprint=footprint), pads, bodies

    entries = [
        chip(0, "R1", (0, 0), 20.0, 20.0, "RESC1005X40N"),  # IPC metric 1005 = 0402
        chip(1, "C1", (200, 0), 30.0, 24.0, "CAP_X", 90.0, body=(1.6, 0.8)),
        chip(2, "R4", (400, 0), 20.0, 20.0, "RES_X", 30.0, body=(1.0, 0.5)),
        chip(3, "C2", (600, 0), 0.75 * MM, 0.9 * MM, "C_SMD"),  # span 2.4 mm
        chip(4, "R3", (800, 0), 2.0 * MM, 1.0 * MM, "R_BIG"),  # span 5 mm
        chip(5, "L1", (1000, 0), 40.0, 30.0, "IND_0805"),
        chip(6, "D1", (1200, 0), 20.0, 20.0, "SOD_0402"),  # no passive designator
        chip(7, "FB1", (1400, 0), 10.0, 10.0, "FB_0201"),
    ]
    index = _index([entry[0] for entry in entries], [pad for entry in entries for pad in entry[1]],
                   [body for entry in entries for body in entry[2]])
    found = [(part.designator, part.passive, part.size_code, part.size_basis, part.small)
             for part in assembly.parts(index)]
    assert found == [
        ("R1", True, "0402", "footprint", True),
        ("C1", True, "0603", "body", True),
        ("R4", True, "0402", "body", True),
        ("C2", True, "0603", "pad_span", True),
        ("R3", True, None, None, False),
        ("L1", True, "0805", "footprint", False),
        ("D1", False, None, None, False),
        ("FB1", True, "0201", "footprint", True),
    ]
    parts = assembly.parts(index)
    assert parts[0].span_mil == pytest.approx(60.0)
    assert parts[3].span_mil == pytest.approx(2.4 * MM)
    assert assembly.footprint_size("CAPC2013X90N") is None  # metric 2013: no imperial code
    assert assembly.footprint_size("C0603_HandSolder") == "0603"


def test_terminals_merge_composite_pads_for_pitch_and_passives():
    components = [_part(0, "L1", footprint="IND_X"), _part(1, "U1"), _part(2, "U2"),
                  _part(3, "U3"), _part(4, "J1"), _part(5, "U4")]
    # L1: each terminal two overlapping pads of one name (its pins 108 mil apart).
    primitives = [_smd(0, x, 500, 30.0, 80.0, net=net, name=name) for x, name, net in (
        (440, "1", A), (452, "1", A), (548, "2", B), (560, "2", B))]
    # U1: four pins at a 20 mil pitch, each drawn as two pads 10 mil apart along it.
    primitives += [_smd(1, 1000 + 20 * n, 500 + dy, 8.0, 20.0, net=A, name=str(n + 1))
                   for n in range(4) for dy in (0, 10)]
    # U2: unnamed pads, two of them touching (one terminal): four terminals 25 mil apart.
    primitives += [_smd(2, 2000 + 25 * n, 500, 10.0) for n in range(4)]
    primitives += [_smd(2, 2085, 500, 10.0)]
    # U3: pin 1 drawn twice on the same spot under two names: the pitch is not 0.
    primitives += [_smd(3, 3000 + 30 * n, 500, 10.0, name=str(n + 1)) for n in range(4)]
    primitives += [_smd(3, 3000, 500, 10.0, name="1A")]
    # J1: pads on both outer layers at the same places: layers are not compared.
    primitives += [_smd(4, 4000 + 50 * n, 500, 20.0, layer=layer, name=f"{layer}.{n}")
                   for n in range(4) for layer in (1, 32)]
    # U4: four pins 30 mil apart and an exposed pad split in four apart pads named 5.
    primitives += [_smd(5, 5000 + 30 * n, 500, 10.0, name=str(n + 1)) for n in range(4)]
    primitives += [_smd(5, 5030 + 12 * n, 600, 8.0, name="5") for n in range(4)]
    l1, u1, u2, u3, j1, u4 = assembly.parts(_index(components, primitives))
    assert (l1.terminals, l1.smd_pads, l1.pitch_mil, l1.passive) == (((1, 2), (3, 4)), 4, None,
                                                                     True)
    assert l1.span_mil == pytest.approx(150.0)  # outer edge to outer edge of its four pads
    assert (len(u1.terminals), u1.pitch_mil) == (4, 20.0)  # not the 10 mil within a pin
    assert (len(u2.terminals), u2.pitch_mil) == (4, 25.0)
    assert (len(u3.terminals), u3.pitch_mil) == (4, 30.0)
    assert (len(j1.terminals), j1.pitch_mil) == (8, 50.0)
    assert (len(u4.terminals), u4.pitch_mil) == (5, 30.0)  # not the 12 mil within pad 5
    assert not (u1.passive or u2.passive)


# ---------- fiducials ----------


def test_fiducial_parts_free_pads_and_local_fiducials():
    one = 1.0 * MM
    components = [
        _part(0, "FID1"), _part(1, "M1", side="bottom", footprint="Fiducial_1mm"),
        _part(2, "X5"), _part(3, "X6"), _part(4, "X7"), _part(5, "X8"), _part(6, "U9"),
        _part(7, "U10", footprint="RFID_QFN16"), _part(8, "FID2"),
        _part(9, "L5", footprint="RFID_COIL"),
    ]
    primitives = [
        _smd(0, 100, 100, one, shape=PAD_SHAPE_ROUND),
        _smd(1, 100, 300, one, layer=32, shape=PAD_SHAPE_ROUND),
        _smd(2, 3000, 100, one, shape=PAD_SHAPE_ROUND),  # one unnetted round pad
        _smd(3, 3000, 300, one, net=A, shape=PAD_SHAPE_ROUND),  # netted: no fiducial
        _smd(4, 3000, 500, 0.3 * MM, shape=PAD_SHAPE_ROUND),  # too small
        _smd(5, 3000, 700, one, 2 * one, shape=PAD_SHAPE_ROUND),  # oblong
        _smd(None, 200, 2000, one, shape=PAD_SHAPE_ROUND),  # free pads
        _smd(None, 400, 2000, 1.6 * MM, layer=32, shape=PAD_SHAPE_ROUND),
        _smd(None, 600, 2000, one, net=A, shape=PAD_SHAPE_ROUND),
        _tht(None, 800, 2000, size=one, hole=10.0),
        _smd(None, 1000, 2000, one, shape=PAD_SHAPE_RECT),
        _smd(None, 1200, 2000, 4.0 * MM, shape=PAD_SHAPE_ROUND),
    ]
    primitives += [_smd(6, 1000 + 0.5 * MM * n, 100, 10.0) for n in range(4)]  # 0.5 mm pitch
    # Far away: an RFID chip (no fiducial word), FID2 with four pads (more than a
    # fiducial has) and an oblong round free pad.
    primitives += [_smd(7, 10000 + 0.508 * MM * n, 100, 10.0, net=A) for n in range(16)]
    primitives += [_smd(8, 10000 + 100 * n, 1000, one, shape=PAD_SHAPE_ROUND) for n in range(4)]
    primitives += [_smd(None, 10000, 3000, one, 2 * one, shape=PAD_SHAPE_ROUND)]
    primitives += [_smd(9, 12000, 100, net=A), _smd(9, 12100, 100, net=B)]  # an RFID coil
    index = _index(components, primitives)
    found = [(f.reason, f.side, f.component, round(f.size_mil / MM, 2))
             for f in assembly.fiducials(index)]
    assert found == [
        ("designator", "top", 0, 1.0), ("footprint", "bottom", 1, 1.0),
        ("single_pad", "top", 2, 1.0), ("free_pad", "top", None, 1.0),
        ("free_pad", "bottom", None, 1.6),
    ]
    assert assembly.fiducial_counts(index) == {"top": 3, "bottom": 2}
    parts = assembly.parts(index)
    assert [part.fiducial for part in parts] == [
        "designator", "footprint", "single_pad", None, None, None, None, None, None, None]
    assert assembly.placements(index).components == [3, 4, 5, 6, 7, 8, 9]  # fiducials are not
    assert parts[7].pitch_mil == pytest.approx(0.508 * MM)
    u9 = parts[6]
    assert u9.pitch_mil == pytest.approx(0.5 * MM)
    # Within 50 mm on its side: FID1 (22.9 mm); X5 (50.8 mm) and the free pad (52.4 mm)
    # are farther, the bottom ones on the other side.
    assert u9.near_fiducials == 1
    assert parts[0].near_fiducials is None  # no pitch


# ---------- placements ----------


def test_placements_dnp_and_unique_parts():
    components = [
        _part(0, "R1", footprint="R0402", comment="10k"),
        _part(1, "R2", footprint="r0402 ", comment="10K"),  # the same part, case-folded
        _part(2, "C1", side="bottom", footprint="C0402", comment="1u"),
        _part(3, "J1", footprint="HDR", comment="hdr"),
        _part(4, "Q1", footprint="NT", comment="tie", kind=3),  # net tie in the BOM
        _part(5, "MH1", kind=5), _part(6, "LOGO1", kind=2), _part(7, "FID1"),
        _part(8, "U5", kind=1), _part(9, "W1", footprint="JMP", comment="0R", kind=6),
        _part(10, "MH2", kind=1, side="bottom"),  # a Mechanical part's plated hole
    ]
    primitives = [  # netted: a lone unnetted pad of fiducial size is a fiducial
        _smd(0, 0, 0, net=A), _smd(1, 100, 0, net=A), _smd(2, 200, 0, layer=32, net=A),
        _tht(3, 300, 0), _smd(4, 400, 0), _tht(4, 450, 0), _tht(5, 500, 0, plated=False),
        _smd(6, 600, 0), _smd(7, 700, 0, 1.0 * MM, shape=PAD_SHAPE_ROUND),
        _smd(9, 900, 0, net=A), _tht(10, 1000, 0, size=120.0, hole=100.0, net=GND),
    ]
    index = _index(components, primitives)
    assert assembly.parts(index)[10].mount == "mechanical"  # a mounting hole: not placed
    placed = assembly.placements(index)
    assert placed.components == [0, 1, 2, 3, 4, 9]
    assert placed.counts == {"top": {"smd": 3, "tht": 1, "mixed": 1},
                             "bottom": {"smd": 1, "tht": 0, "mixed": 0}}
    assert placed.unique == 5 and placed.not_fitted == []
    dnp = assembly.placements(index, lambda name: name in {"R2", "W1"})
    assert dnp.components == [0, 2, 3, 4] and dnp.not_fitted == [1, 9]
    assert dnp.counts["top"]["smd"] == 1

    def broken(name):
        raise ValueError(name)

    assert assembly.placements(index, broken).components == placed.components
    # The netlist name wins over the designator.
    index.db.execute("UPDATE components SET sch_refdes = 'R20' WHERE id = 0")
    index._dfm_assembly = None
    assert assembly.placements(index, lambda name: name == "R20").not_fitted == [0]
    assert assembly.parts(index)[0].name == "R20"


# ---------- spacing ----------


def _spacing_board():
    diamond = 10.0 * math.sqrt(2.0)  # a square on its corner, 10 mil from centre to corner
    components = [
        _part(0, "A"), _part(1, "B"), _part(2, "C"), _part(3, "D"), _part(4, "E"),
        _part(5, "F"), _part(6, "G", kind=1), _part(7, "H"), _part(8, "I"), _part(9, "J"),
        _part(10, "K"), _part(11, "L"), _part(12, "M"), _part(13, "N", side="bottom"),
        _part(14, "O", kind=2),
    ]
    primitives = [
        _smd(0, 0, 0, 4.0), _smd(1, 25, 25, 4.0),
        _smd(2, 1000, 0), _smd(2, 1040, 0), _smd(3, 1080, 0), _smd(3, 1120, 0),
        _smd(4, 2000, 0), _smd(4, 2040, 0), _smd(5, 2080, 0), _smd(5, 2120, 0),
        # Netted: a lone unnetted pad of fiducial size is a fiducial (not placed).
        _smd(7, 3130, 50, net=A),
        _smd(8, 4050, 50, net=A), _smd(9, 4100, 50, net=A), _smd(10, 4250, 50, net=A),
        _smd(11, 5050, 50, net=A), _smd(12, 5150, 50, net=A),
        _smd(13, 0, 0, 4.0, layer=32), _smd(14, 1060, 30),
    ]
    bodies = [
        _body(0, oriented_rect_vertices(0, 0, diamond, diamond, 45.0)),
        _body(1, oriented_rect_vertices(25, 25, diamond, diamond, 45.0)),
        _body(4, _rect(1980, -30, 2060, 30)),
        _body(6, _rect(3000, 0, 3100, 100)),
        _body(8, _rect(4000, 0, 4100, 100)), _body(9, _rect(4050, 0, 4150, 100)),
        _body(10, _rect(4200, 0, 4300, 100)),
        _body(11, _rect(5000, 0, 5100, 100)), _body(12, _rect(5100, 0, 5200, 100)),
        _body(13, oriented_rect_vertices(0, 0, diamond, diamond, 45.0), projection="bottom"),
    ]
    return _index(components, primitives, bodies)


def test_nearest_neighbours_bases_and_overlaps():
    index = _spacing_board()
    spacing = assembly.part_spacing(index)
    assert spacing.members == 14  # the graphical part is left out
    found = {part: (gap.second if gap.first == part else gap.first, round(gap.gap_mil, 3),
                    gap.basis, gap.overlap)
             for part, gap in spacing.nearest.items()}
    assert found == {
        0: (1, 21.213, "body", False),  # exact outlines: their boxes are 7.07 apart
        1: (0, 21.213, "body", False),
        2: (3, 20.0, "pads", False), 3: (2, 20.0, "pads", False),
        # E's body reaches 10 mil past its pads towards F (no body: its pad extent).
        4: (5, 10.0, "mixed", False), 5: (4, 10.0, "mixed", False),
        6: (7, 20.0, "mixed", False), 7: (6, 20.0, "mixed", False),
        8: (10, 100.0, "body", False),  # J overlaps it
        9: (10, 50.0, "body", False), 10: (9, 50.0, "body", False),
        11: (12, 0.0, "body", False), 12: (11, 0.0, "body", False),  # touching
    }
    (overlap,) = spacing.overlaps
    assert (overlap.first, overlap.second, overlap.gap_mil, overlap.basis, overlap.overlap) == (
        8, 9, 0.0, "body", True)
    at = spacing.nearest[0].at  # midway between the parallel edges facing each other
    assert at[0] + at[1] == pytest.approx(25.0) and 5.0 <= at[0] <= 20.0
    minimum = spacing.minimum()
    assert (minimum.first, minimum.second, minimum.gap_mil) == (11, 12, 0.0)
    assert assembly.part_spacing(index) is spacing


def test_close_pairs():
    index = _spacing_board()
    pairs = assembly.close_pairs(index, 25.0)
    assert [(gap.first, gap.second, round(gap.gap_mil, 3), gap.overlap) for gap in pairs] == [
        (8, 9, 0.0, True), (11, 12, 0.0, False), (4, 5, 10.0, False), (2, 3, 20.0, False),
        (6, 7, 20.0, False), (0, 1, 21.213, False),
    ]
    # Closer than the limit itself (20 mil is not below 20).
    assert [(gap.first, gap.second) for gap in assembly.close_pairs(index, 20.0)] == [
        (8, 9), (11, 12), (4, 5)]
    assert assembly.close_pairs(index, 25.0) is pairs


def _chip(component, x, y=500.0, body=True):
    """A chip at (x, y): pads 60 mil apart (20 mil square) and, with body, a 3D body
    between them (40 x 20 mil): its pads reach 20 mil past the body."""
    pads = [_smd(component, x - 30, y, net=A), _smd(component, x + 30, y, net=B)]
    bodies = [_body(component, _rect(x - 20, y - 10, x + 20, y + 10))] if body else []
    return pads, bodies


def test_spacing_takes_the_pads_past_the_bodies_and_a_body_over_pads():
    components = [_part(0, "C1"), _part(1, "C2"), _part(2, "U1"), _part(3, "C3"),
                  _part(4, "C4"), _part(5, "C5")]
    primitives, bodies = [], []
    for component, x, body in ((0, 500, True), (1, 600, True), (4, 2000, True),
                               (5, 2120, False)):
        pads, shapes = _chip(component, x, body=body)
        primitives += pads
        bodies += shapes
    # U1's body covers C3, which has none: a body over another part's pads.
    primitives += [_smd(2, 960 + 20 * n, 500, 10.0, net=A) for n in range(5)]
    bodies += [_body(2, _rect(930, 470, 1070, 540))]
    primitives += [_smd(3, 1020, 525, 10.0, net=A), _smd(3, 1040, 525, 10.0, net=B)]
    index = _index(components, primitives, bodies)
    parts = assembly.parts(index)
    model = assembly._model(index)
    pads = model.pair(parts[0], parts[1])  # bodies 60 mil apart, pads 20
    assert (round(pads.gap_mil, 3), pads.basis, pads.overlap) == (20.0, "pads", False)
    assert pads.at[0] == pytest.approx(550.0) and 490.0 <= pads.at[1] <= 510.0
    covered = model.pair(parts[2], parts[3])
    assert (covered.gap_mil, covered.basis, covered.overlap) == (0.0, "mixed", True)
    # C4's pad copper against C5's pad extent (C5 has no body).
    plain = model.pair(parts[4], parts[5])
    assert (round(plain.gap_mil, 3), plain.basis) == (40.0, "pads")
    spacing = assembly.part_spacing(index)
    assert [(gap.first, gap.second, gap.basis) for gap in spacing.overlaps] == [(2, 3, "mixed")]
    assert spacing.minimum().gap_mil == pytest.approx(20.0)


def _dnp_board():
    """R1 with R2 2 mil to its right and R3 10 mil to its left, R4 2 mil left of R3 (pad
    extents, no bodies)."""
    components = [_part(n, f"R{n + 1}") for n in range(4)]
    primitives = []
    for component, x0 in ((0, 1000), (1, 1042), (2, 950), (3, 908)):
        primitives += [_smd(component, x0 + 5, 500, 10.0, net=A),
                       _smd(component, x0 + 35, 500, 10.0, net=B)]
    return _index(components, primitives)


def test_spacing_leaves_out_the_parts_asked_and_parts_off_the_board():
    index = _dnp_board()
    every = assembly.part_spacing(index)
    assert {part: (gap.first, gap.second, gap.gap_mil) for part, gap in every.nearest.items()} \
        == {0: (0, 1, 2.0), 1: (0, 1, 2.0), 2: (2, 3, 2.0), 3: (2, 3, 2.0)}
    # R2 and R4 Not Fitted: R1 and R3 are each other's nearest neighbour.
    fitted = assembly.part_spacing(index, [1, 3])
    assert fitted.members == 2 and fitted.minimum().gap_mil == pytest.approx(10.0)
    assert {part: (gap.first, gap.second) for part, gap in fitted.nearest.items()} == {
        0: (0, 2), 2: (0, 2)}
    assert assembly.part_spacing(index, {3, 1}) is fitted
    assert assembly.part_spacing(index) is every
    # Parts parked off the board are not measured (part_to_edge lists them).
    components = [_part(0, "P1"), _part(1, "P2"), _part(2, "R1")]
    primitives = [_smd(0, 2000, 500, net=A), _smd(1, 2032, 500, net=A),
                  _smd(2, 500, 500, net=A)]
    parked = _index(components, primitives, outline=OUTLINE)
    spacing = assembly.part_spacing(parked)
    assert spacing.members == 1 and spacing.minimum() is None
    assert assembly.close_pairs(parked, 50.0) == []
    assert assembly.part_to_edge(parked).off_board == [0, 1]


def test_overlap_of_outlines():
    square = _rect(0, 0, 10, 10)
    assert assembly.outlines_overlap(square, _rect(5, 0, 15, 10))  # half over it, corners on edges
    assert assembly.outlines_overlap(square, list(square))  # the same outline
    assert not assembly.outlines_overlap(square, _rect(10, 0, 20, 10))  # sharing an edge
    assert assembly.outlines_overlap(_rect(0, 4, 20, 6), _rect(9, -5, 11, 15))  # a cross
    concave = [(0, 0), (30, 0), (30, 30), (20, 30), (20, 10), (10, 10), (10, 30), (0, 30)]
    assert assembly.outlines_overlap(concave, _rect(2, 2, 8, 8))
    assert not assembly.outlines_overlap(concave, _rect(12, 12, 18, 28))  # in the notch


# ---------- part to edge ----------


def _edge_board():
    components = [
        _part(0, "P0"), _part(1, "P1"), _part(2, "P2"), _part(3, "P3"), _part(4, "P4"),
        _part(5, "P5"), _part(6, "P6"),
    ]
    primitives = [
        _smd(0, 100, 17, net=A), _smd(0, 140, 17, net=A),  # 7 mil from the bottom edge
        _smd(1, 20, 520, 10.0, net=A),  # 15 mil from the left edge, its body over it
        _smd(2, 998, 500, net=A),  # across the right edge
        _smd(3, 2000, 2000, net=A),  # off the board
        _smd(4, 500, 500, net=A),  # 190 mil from a cutout
        _smd(5, 450, 690, 10.0, net=A),  # 5 mil from its own cutout
        _smd(6, 650, 690, 10.0, net=A),  # 5 mil from another cutout
    ]
    bodies = [_body(0, _rect(80, 2, 160, 40)), _body(1, _rect(-20, 500, 40, 540)),
              _body(5, _rect(440, 680, 460, 720))]  # into its own cutout: no overhang
    regions = (_region(56, _square(400, 700, 100), kind=3, component=5)
               + _region(56, _square(600, 700, 100), kind=3))
    return _index(components, primitives, bodies, outline=OUTLINE, regions=regions)


def test_part_to_edge():
    index = _edge_board()
    edge = assembly.part_to_edge(index)
    assert edge.has_outline and edge.reach_mil == assembly.DEFAULT_EDGE_REACH_MIL
    assert edge.off_board == [3] and edge.fingers == []
    found = {
        part: (None if e.pad_distance_mil is None else round(e.pad_distance_mil, 3), e.pad_edge,
               None if e.body_distance_mil is None else round(e.body_distance_mil, 3),
               e.crosses, e.overhang)
        for part, e in edge.parts.items()
    }
    assert found == {
        0: (7.0, "outline", 2.0, False, False),
        1: (15.0, "outline", 0.0, False, True),
        2: (0.0, "outline", None, True, False),
        4: (190.0, "cutout", None, False, False),
        5: (145.086, "cutout", 140.0, False, False),  # its own cutout is no edge
        6: (5.0, "cutout", None, False, False),
    }
    assert edge.parts[0].pad_at == pytest.approx((100.0, 7.0), abs=10.0)
    assert edge.parts[0].pad_at[1] == pytest.approx(7.0)
    minimum = edge.minimum()
    assert (minimum.component, minimum.pad_distance_mil) == (6, pytest.approx(5.0))
    near = assembly.part_to_edge(index, 10.0)
    assert sorted(near.parts) == [0, 1, 2, 6]
    assert near.parts[1].pad_distance_mil is None and near.parts[1].overhang
    assert assembly.part_to_edge(index) is edge


def test_a_pad_off_the_board_is_crossing_however_far_out():
    # P0's centre is on the board; one of its pads lies wholly 20 mil past the right edge.
    index = _index([_part(0, "P0")], [_smd(0, 900, 500, net=A), _smd(0, 1030, 500, net=A)],
                   outline=OUTLINE)
    edge = assembly.part_to_edge(index).parts[0]
    assert (edge.crosses, edge.pad_distance_mil, edge.pad_edge) == (True, 0.0, "outline")


def test_part_to_edge_without_outline():
    index = _index([_part(0, "R1")], [_smd(0, 0, 0)])
    edge = assembly.part_to_edge(index)
    assert not edge.has_outline and edge.parts == {} and edge.minimum() is None


def test_edge_fingers_are_their_own_class(monkeypatch):
    finger = dict(width=20.0, height=60.0)
    primitives = [_smd(1, x, 35, **finger) for x in range(100, 400, 50)]  # 6 fingers
    primitives += [_smd(2, x, 35, **finger) for x in range(600, 800, 50)]  # 4: no connector
    primitives += [_smd(2, 700, 300, **finger)]
    index = _index([_part(0, "R1"), _part(1, "X1"), _part(2, "J2")], primitives,
                   outline=OUTLINE)
    edge = assembly.part_to_edge(index)
    (group,) = edge.fingers
    assert group.component == 1 and len(group.pads) == 6
    assert edge.parts[1].fingers and not edge.parts[2].fingers
    assert edge.minimum().component == 2  # fingers left out
    # Fingers are searched at their own reach whatever the reach asked (5 mil from it).
    near = assembly.part_to_edge(index, 5.0)
    assert [group.component for group in near.fingers] == [1] and 1 not in near.parts

    def no_search(*_):
        raise AssertionError("copper_to_edge was asked")

    monkeypatch.setattr(geometry, "copper_to_edge", no_search)
    index = _index([_part(0, "J2")], [_smd(0, x, 35, **finger) for x in range(600, 800, 50)]
                   + [_smd(0, 700, 300, **finger)], outline=OUTLINE)
    assert not assembly.part_to_edge(index).fingers


# ---------- tombstoning ----------


def _tomb_board():
    components = [
        _part(0, "R1", footprint="R0402"), _part(1, "C1", footprint="C0402"),
        _part(2, "C2", footprint="C0402", rotation=90.0), _part(3, "R5", footprint="R0603"),
        _part(4, "R6", footprint="R0603"), _part(5, "R7", footprint="R0402"),
        _part(6, "R8", footprint="R0805"),
    ]
    primitives = [
        _smd(0, 100, 100, 30.0, net=GND), _smd(0, 160, 100, 30.0, net=A),
        _smd(1, 300, 100, 30.0, net=GND), _smd(1, 360, 100, 30.0, net=B),
        _smd(2, 500, 100, 30.0, 20.0, net=GND, shape=PAD_SHAPE_ROUND, rotation=90.0),
        _smd(2, 500, 160, 30.0, 20.0, net=A, shape=PAD_SHAPE_ROUND, rotation=90.0),
        _smd(3, 700, 100, 30.0, net=A), _smd(3, 760, 100, 30.0, net=B),
        _smd(4, 1100, 100, 30.0, net=A), _smd(4, 1160, 100, 30.0, net=B),
        _smd(5, 900, 100, 30.0, net=GND), _smd(5, 960, 100, 30.0, net=B),
        _smd(6, 1300, 100, 30.0), _smd(6, 1360, 100, 30.0, net=A),
        # R5: a 20 mil (0.508 mm) track on one pad, 8 mil on the other: imbalance.
        _track(A, 700, 100, 700, 300, width=20.0), _track(B, 760, 100, 760, 300, width=8.0),
        _track(A, 700, 100, 700, 300, width=40.0, layer=32),  # another layer
        _track(A, 670, 140, 730, 140, width=30.0),  # 5 mil from the pad: not attached
        # R6: 20 against 12 mil: less than twice.
        _track(A, 1100, 100, 1100, 300, width=20.0), _track(B, 1160, 100, 1160, 300, width=12.0),
    ]
    # R7's GND pad under a hatched pour: its tracks are pour copper, not attachments.
    primitives += [_track(GND, x, 50, x, 150, width=4.0, poured_by=0) for x in (890, 900, 910)]
    areas = [
        _area(GND, _rect(0, 0, 130, 200)),  # R1 pad 1: covered all round (direct)
        _area(A, _rect(150, 97, 250, 103)),  # R1 pad 2: one spoke (relief)
        _area(GND, _rect(250, 50, 350, 150), holes=[_rect(270, 70, 330, 130)]),  # C1: a void
        _area(B, _rect(330, 0, 400, 100)),  # C1 pad 2: its top edge through the pad centre
        _area(GND, _rect(497, 0, 503, 100)),  # C2 pad 1: a spoke from below
    ]
    pours = [PolygonPour(pour_id=0, net=GND, layer=1, hatch_style="Hatched",
                         vertices=_rect(850, 50, 950, 150))]
    return _index(components, primitives, areas=areas, pours=pours)


def test_tombstoning_ring_sampling_and_tracks():
    index = _tomb_board()
    found = {
        index.designator(tomb.component): [
            (pad.style, round(pad.coverage, 4), pad.widest_track_mil) for pad in tomb.pads
        ] + [tomb.causes, tomb.small]
        for tomb in assembly.tombstoning(index)
    }
    assert found["R1"] == [("direct", 1.0, 0.0), ("relief", 3 / 48, 0.0), ("direct",), True]
    c1 = found["C1"]
    assert c1[0] == ("none", 0.0, 0.0) and c1[1][0] == "relief"
    assert 0.3 < c1[1][1] < 0.7  # a horizontal copper edge across the ring
    assert found["C2"] == [("relief", 3 / 48, 0.0), ("none", 0.0, 0.0), (), True]
    assert found["R5"] == [("none", 0.0, 20.0), ("none", 0.0, 8.0), ("track",), True]
    assert found["R6"] == [("none", 0.0, 20.0), ("none", 0.0, 12.0), (), True]
    r7 = found["R7"]
    assert r7[0][0] == "relief" and 0.1 < r7[0][1] < 0.3 and r7[0][2] == 0.0
    assert found["R8"] == [("none", 0.0, 0.0), ("none", 0.0, 0.0), (), False]
    assert assembly.tombstoning(index) is assembly.tombstoning(index)
    plain = _tomb_board()
    plain._has_rtree = False  # the box index finds the same tracks
    assert assembly.tombstoning(plain) == assembly.tombstoning(index)
    # Any pads: a pad without a net or on no copper layer of its own is none.
    (connection,) = assembly.pad_connections(index, [1])
    assert (connection.row, connection.layer, connection.net, connection.style) == (
        1, 1, GND, "direct")


def test_tombstoning_tracks_only_where_no_area_copper_and_terminals():
    components = [_part(0, "C1", footprint="C0402"), _part(1, "C2", footprint="C0402"),
                  _part(2, "C3", footprint="C0402"), _part(3, "L1", footprint="L0603")]
    primitives = [
        # C1: its GND pad relief-connected to a plane, the other on a 20 mil track.
        _smd(0, 100, 100, 30.0, net=GND), _smd(0, 160, 100, 30.0, net=A),
        _track(A, 160, 100, 160, 300, width=20.0),
        # C2: its GND pad in a solid region (direct), the other on a 20 mil track.
        _smd(1, 400, 100, 30.0, net=GND), _smd(1, 460, 100, 30.0, net=A),
        _track(A, 460, 100, 460, 300, width=20.0),
        # C3: a 20 mil arc on one pad, an 8 mil track on the other.
        _smd(2, 700, 100, 30.0, net=A), _smd(2, 760, 100, 30.0, net=B),
        Primitive(kind="arc", layer=1, net=A, component=None, x1=700, y1=100, x2=700, y2=300,
                  width=20.0, source_id=9),
        _track(B, 760, 100, 760, 300, width=8.0),
        # L1: each terminal two pads; 20 mil on the second pad of one (not touching its
        # first pad), 8 mil on the other.
        _smd(3, 1000, 100, 30.0, net=A, name="1"), _smd(3, 1025, 100, 30.0, net=A, name="1"),
        _smd(3, 1100, 100, 30.0, net=B, name="2"), _smd(3, 1110, 100, 30.0, net=B, name="2"),
        _track(A, 1040, 100, 1040, 300, width=20.0), _track(B, 1110, 100, 1110, 300, width=8.0),
    ]
    areas = [_area(GND, _rect(0, 97, 85, 103)), _area(GND, _rect(300, 0, 430, 200))]
    index = _index(components, primitives, areas=areas)
    assert index._has_rtree
    found = {index.designator(tomb.component): (
        [(pad.style, pad.widest_track_mil) for pad in tomb.pads], tomb.causes)
        for tomb in assembly.tombstoning(index)}
    assert found == {
        "C1": ([("relief", 0.0), ("none", 20.0)], ()),  # the plane outweighs the track
        "C2": ([("direct", 0.0), ("none", 20.0)], ("direct",)),  # never both causes
        "C3": ([("none", 20.0), ("none", 8.0)], ("track",)),  # the arc is attached copper
        "L1": ([("none", 20.0), ("none", 8.0)], ("track",)),  # per terminal
    }
    (l1,) = [tomb for tomb in assembly.tombstoning(index) if tomb.component == 3]
    assert [pad.row for pad in l1.pads] == [assembly.parts(index)[3].pads[0],
                                            assembly.parts(index)[3].pads[2]]
    plain = _index(components, primitives, areas=areas)
    plain._has_rtree = False
    assert assembly.tombstoning(plain) == assembly.tombstoning(index)


# ---------- pick and place ----------

# Altium's files write 4 decimals in mm: the map's oracle agrees within 0.0001 mm.
PNP_TOLERANCE = 0.001 * MM


def _approx(point):
    return pytest.approx(point, abs=PNP_TOLERANCE)


def test_pick_and_place_centre_is_the_box_of_pad_centres():
    components = [
        _part(0, "Q1", 100, 100),  # two pads of different size
        _part(1, "J1", 400, 100),  # mixed: SMD pads and through-hole pegs
        _part(2, "U1", 700, 700, side="bottom", rotation=270.0),
        _part(3, "LOGO1", 300, 300, kind=0),  # no pads
        _part(4, "P1", 900, 100),  # a through-hole header with a pin-1 origin
    ]
    primitives = [
        _smd(0, 100, 100, 10.0), _smd(0, 200, 100, 60.0),
        _smd(1, 380, 100), _smd(1, 420, 100), _tht(1, 300, 160), _tht(1, 520, 160),
        _smd(2, 680, 700, layer=32), _smd(2, 720, 700, layer=32), _smd(2, 700, 760, layer=32),
        _tht(4, 900, 100), _tht(4, 1000, 100), _tht(4, 1100, 100),
    ]
    index = _index(components, primitives)
    q1, j1, u1, logo, p1 = assembly.centroids(index)
    # The midpoint of the pad centres, not of the copper extent (Part.centre).
    assert q1.centre == _approx((150.0, 100.0))
    assert assembly.parts(index)[0].centre == _approx((162.5, 100.0))
    assert (q1.extent_basis, q1.extent_centre) == ("pads", _approx((162.5, 100.0)))
    assert q1.extent_offset_mil == pytest.approx(12.5)
    assert q1.reference_offset_mil == pytest.approx(50.0)
    # Every pad of a mixed part counts, through-hole pegs too.
    assert j1.centre == _approx((410.0, 130.0))
    # A bottom part: the same box, no mirroring.
    assert u1.centre == _approx((700.0, 730.0)) and u1.unverified == ()
    # No pads: Altium writes the absolute origin.
    assert (logo.centre, logo.reference, logo.extent_offset_mil) == (None, (300, 300), None)
    # Pin-1 origin: Ref-X/Y is off Center-X/Y; the extent agrees with the centre.
    assert p1.centre == _approx((1000.0, 100.0))
    assert p1.reference_offset_mil == pytest.approx(100.0)
    assert p1.extent_offset_mil == pytest.approx(0.0, abs=1e-9)
    assert assembly.centroids(index) is assembly.centroids(index)  # cached


def test_pick_and_place_asymmetric_parts_and_unverified_cases():
    from server.parsers.pcb_doc import BareHole

    components = [
        _part(0, "J1", 200, 200),  # an edge connector: pads in a row, the body beyond
        _part(1, "U2", 500, 500, rotation=30.0),  # off the 90-degree grid
        _part(2, "U3", 800, 200),  # pads on both sides
        _part(3, "M1", 800, 800, kind=1),  # copper pads and a bare-hole pad
        _part(4, "R9", 900, 500, rotation=30.0),  # off the grid, symmetric: no case
        _part(5, "U4", 1200, 200),  # a bottom pad inside its top pads' box: no case
    ]
    primitives = [_smd(0, x, 200) for x in (100, 150, 200, 250, 300)]
    cosine, sine = math.cos(math.radians(30.0)), math.sin(math.radians(30.0))
    for dx, dy in ((-40, -20), (40, -20), (-40, 60)):  # an L of pads
        primitives.append(_smd(1, 500 + dx * cosine - dy * sine, 500 + dx * sine + dy * cosine))
    primitives += [_smd(2, 780, 200), _smd(2, 820, 200), _smd(2, 800, 300, layer=32)]
    primitives += [_tht(3, 780, 800), _tht(3, 820, 800)]
    primitives += [_smd(4, 900 + dx * cosine, 500 + dx * sine) for dx in (-20, 20)]
    primitives += [_smd(5, 1180, 200), _smd(5, 1220, 200), _smd(5, 1200, 300),
                   _smd(5, 1200, 220, layer=32)]
    bodies = [_body(0, _rect(80, 180, 320, 400))]
    holes = [BareHole(x=900, y=800, hole=60.0, layer=74, net=None, component=3, pad_name="M")]
    index = _index(components, primitives, bodies, bare_holes=holes)
    j1, u2, u3, m1, r9, u4 = assembly.centroids(index)
    assert (j1.centre, j1.extent_basis) == (_approx((200.0, 200.0)), "body")
    assert j1.extent_centre == _approx((200.0, 290.0))
    assert j1.extent_offset_mil == pytest.approx(90.0)
    assert j1.reference_offset_mil == pytest.approx(0.0, abs=1e-9)
    # The box is taken in the board's frame; the part's frame gives another centre here.
    assert "rotation" in u2.unverified and r9.unverified == ()
    # Pads on both sides are all counted (the top pads alone give y = 200).
    assert u3.centre == _approx((800.0, 250.0)) and u3.unverified == ("both_sides",)
    # The part's own side decides: its top pads alone give the same centre.
    assert u4.centre == _approx((1200.0, 250.0)) and u4.unverified == ()
    # A bare-hole pad counts as a pad.
    assert m1.centre == _approx((840.0, 800.0)) and m1.unverified == ("bare_holes",)


def _shifted_outline(dx, dy):
    return [type(vertex)(vertex.x + dx, vertex.y + dy,
                         None if vertex.arc is None else
                         (vertex.arc[0] + dx, vertex.arc[1] + dy, *vertex.arc[2:]))
            for vertex in OUTLINE]


def test_board_origin_against_the_outline():
    index = _index(outline=_shifted_outline(500, 500))
    board = index.pcb.board
    # Within 0.1 mm (3.94 mil) of a corner is at it: 2.2 mil (0.057 mm) off the lower-left
    # is at it, 6 mil (0.15 mm) off is not.
    cases = [((0.0, 0.0), "not_set"), ((500.0, 500.0), "lower_left"),
             ((500.2, 500.1), "lower_left"), ((502.0, 501.0), "lower_left"),
             ((506.0, 500.0), "inside"), ((1500.0, 500.0), "other_corner"),
             ((1498.0, 1502.0), "other_corner"), ((494.0, 500.0), "outside"),
             ((800.0, 900.0), "inside"), ((2000.0, 0.0), "outside"), ((None, None), "not_set")]
    for (x, y), position in cases:
        board.origin_x, board.origin_y = x, y
        facts = assembly.origin(index)
        assert facts.position == position, (x, y)
        assert facts.box == (500, 500, 1500, 1500) and not facts.rigid_flex
    board.origin_x, board.origin_y = 0.0, 0.0
    assert assembly.origin(index).to_lower_left_mil == pytest.approx(math.hypot(500, 500))
    board.substacks = [{"id": "1", "name": "Rigid", "is_flex": False},
                       {"id": "2", "name": "Flex", "is_flex": True}]
    assert assembly.origin(index).rigid_flex
    # The outline's corner at the absolute origin: an origin left there is at the corner,
    # also when the corner is 0.03 mm off it (a corpus case: not a finding).
    assert assembly.origin(_index(outline=OUTLINE)).position == "lower_left"
    assert assembly.origin(_index(outline=_shifted_outline(1.0, 0.5))).position == "lower_left"
    assert assembly.origin(_index()).position == "no_outline"


# ---------- the cache ----------


def test_a_replaced_index_is_freed_without_a_collection():
    index = _tomb_board()
    for part in assembly.parts(index):
        part.fitted(lambda name: False)
    assembly.part_spacing(index)
    assembly.part_spacing(index, [0])
    assembly.close_pairs(index, 30.0)
    assembly.part_to_edge(index)
    assembly.tombstoning(index)
    # check_dfm's board_minimums is the only one (placed parts, bodies, the variant).
    assert not hasattr(assembly, "board_minimums")
    assert next(iter(assembly.part_spacing(index).nearest.values())).at
    index_ref, model_ref = weakref.ref(index), weakref.ref(index._dfm_assembly)
    gc.disable()
    try:
        del index
        assert index_ref() is None and model_ref() is None
    finally:
        gc.enable()


# ---------- Altium's examples ----------


def _example(name):
    path = next(EXAMPLES.rglob(name), None) if EXAMPLES.is_dir() else None
    if path is None:
        pytest.skip("Altium example boards not installed")
    return PcbIndex(parse_pcb_doc(str(path)), None)


def _smd_styles(index):
    rows = [row for row, pad in enumerate(index.pcb.primitives, start=1)
            if pad.kind == "pad" and pad.net is not None and not (pad.hole or 0.0) > 0]
    return Counter(connection.style for connection in assembly.pad_connections(index, rows))


def _small_styles(index):
    return Counter(tuple(sorted(pad.style for pad in tomb.pads))
                   for tomb in assembly.tombstoning(index) if tomb.small)


@pytest.fixture(scope="module")
def minipc():
    return _example("MiniPC.PcbDoc")


def test_minipc(minipc):
    """MiniPC's PolygonConnect rules connect power nets direct and GND by relief: 293 SMD
    pads read as direct; 7 small passives have one pad direct and the other not."""
    assert _smd_styles(minipc)["direct"] == 293
    assert _small_styles(minipc) == {("none", "none"): 487, ("none", "relief"): 324,
                                     ("relief", "relief"): 7, ("direct", "relief"): 6,
                                     ("direct", "none"): 1}
    causes = Counter(cause for tomb in assembly.tombstoning(minipc) if tomb.small
                     for cause in tomb.causes)
    # Track imbalance only where neither pad touches area copper.
    assert causes == {"direct": 7, "track": 3}
    spacing = assembly.part_spacing(minipc)
    assert len(spacing.overlaps) == 25 and {gap.basis for gap in spacing.overlaps} == {"body"}
    assert spacing.distance_calls < 5000
    # Pad copper reaches past the bodies: the closest pair is set by pads.
    assert (round(spacing.minimum().gap_mil, 3), spacing.minimum().basis) == (3.15, "pads")
    assert assembly.fiducial_counts(minipc) == {"top": 0, "bottom": 0}
    parts = assembly.parts(minipc)
    assert min(part.pitch_mil for part in parts if part.pitch_mil) == pytest.approx(
        0.4 * MM, abs=0.01)
    # MH1-MH4 (Mechanical, a plated hole each) are mounting holes, not THT placements.
    assert assembly.placements(minipc).counts == {"top": {"smd": 346, "tht": 16, "mixed": 1},
                                                  "bottom": {"smd": 677, "tht": 1, "mixed": 0}}
    edge = assembly.part_to_edge(minipc)
    assert edge.fingers == [] and edge.off_board == []


def test_sodimm_free_pad_fiducials_and_fingers():
    index = _example("PC4-SODIMM_V200_RC_A1_20141015.PcbDoc")
    found = Counter((f.reason, f.side, round(f.size_mil / MM, 1))
                    for f in assembly.fiducials(index))
    assert found == {("free_pad", "top", 1.0): 2, ("free_pad", "top", 1.6): 1,
                     ("free_pad", "bottom", 1.0): 2, ("free_pad", "bottom", 1.6): 1}
    edge = assembly.part_to_edge(index)
    (group,) = edge.fingers
    assert index.designator(group.component) == "X1" and len(group.pads) == 260
    assert edge.parts[group.component].fingers
    assert _smd_styles(index) == {"none": 1119, "relief": 134}
    assert _small_styles(index) == {("none", "none"): 119, ("none", "relief"): 63}


@pytest.mark.parametrize("name, relief, small", [
    ("SL1 Xilinx Spartan-IIE PQ208 Rev1.02.PcbDoc", 67, {("none", "relief"): 5}),
    ("WiFi.PcbDoc", 36, {("none", "none"): 9, ("none", "relief"): 9}),
])
def test_relief_boards_have_no_direct_pads(name, relief, small):
    """SL1 (every PolygonConnect rule Relief) and WiFi (SMD pads Relief)."""
    index = _example(name)
    styles = _smd_styles(index)
    assert styles["relief"] == relief and styles["direct"] == 0
    assert _small_styles(index) == small


def test_a_terminal_connects_as_its_heaviest_pad():
    # Two pads of one terminal: the direct one decides the style, the widest track of
    # either pad stays the terminal's.
    none = assembly.PadConnection(row=1, layer=1, net=0, style="none", coverage=0.0,
                                  widest_track_mil=20.0)
    direct = assembly.PadConnection(row=2, layer=1, net=0, style="direct", coverage=1.0,
                                    widest_track_mil=0.0)
    relief = assembly.PadConnection(row=3, layer=1, net=0, style="relief", coverage=0.3,
                                    widest_track_mil=8.0)
    for order in ([none, direct, relief], [relief, none, direct]):
        joined = assembly._terminal_connection(order)
        assert (joined.style, joined.row, joined.widest_track_mil) == ("direct", 2, 20.0)
    assert assembly._terminal_connection([none, relief]).style == "relief"
