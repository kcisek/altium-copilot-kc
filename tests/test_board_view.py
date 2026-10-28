"""The board view's server side (services/board_view.py and get_board_view /
get_board_view_geometry in server/main.py): the compact geometry export rebuilt by a
reference decoder against the PcbIndex on synthetic boards and Altium's public examples,
paging, budgets and the cache, get_board_view's locator text, the findings reader per
FINDING_SOURCES entry, and the tools through the protocol."""

import asyncio
import json
import math
import os
import re
import shutil
import time
from collections import Counter, defaultdict
from copy import deepcopy
from functools import cache
from pathlib import Path

import pytest
from mcp import types

import server.main as main_mod
import server.services.board_view as bv
import server.services.ui_views as uv
from server.parsers.pcb_doc import (
    CopperArea,
    DiffPair,
    PolygonPour,
    Primitive,
    ShapeVertex,
    parse_pcb_doc,
    shape_points,
)
from server.parsers.prj_pcb import VariantDefinition, VariantState
from server.services.pcb_index import PcbIndex
from tests.test_pcb_index import NETLIST, decoded_arc, pcb_data  # noqa: F401

ROOT = Path(__file__).parents[1]
EXAMPLES = Path(r"C:\Users\Public\Documents\Altium\AD25\Examples")
BOARDS = {
    "MiniPC": EXAMPLES / "Mini PC" / "Mini PC - Motherboard" / "MiniPC.PcbDoc",
    "SODIMM": EXAMPLES / "Mini PC" / "Mini PC - SODIMM" / "PC4-SODIMM_V200_RC_A1_20141015.PcbDoc",
    "SL1": EXAMPLES / "SpiritLevel-SL1" / "SL1 Xilinx Spartan-IIE PQ208 Rev1.02.PcbDoc",
    "Sentinel": EXAMPLES / "Bluetooth Sentinel" / "Bluetooth_Sentinel.PcbDoc",
    "WiFi": EXAMPLES / "Mini PC" / "Mini PC - WiFi" / "WiFi.PcbDoc",
    "WiFi_panel": EXAMPLES / "Mini PC" / "Mini PC - WiFi" / "WiFi_panel.PcbDoc",
}
LIMIT = main_mod.QUERY_NET_MAX_RESULT_SIZE_CHARS
COPPER = set(range(1, 33)) | set(range(39, 55))
OVERLAYS = {33, 34}


def _handle(request):
    handler = main_mod.mcp._mcp_server.request_handlers[type(request)]
    result = asyncio.run(handler(request))
    return result.model_dump(mode="json", by_alias=True, exclude_none=True)


def _tools() -> dict:
    return {tool["name"]: tool
            for tool in _handle(types.ListToolsRequest(method="tools/list"))["tools"]}


def _call(name: str, args: dict) -> dict:
    return _handle(types.CallToolRequest(
        method="tools/call", params=types.CallToolRequestParams(name=name, arguments=args)))


def _need(name: str) -> Path:
    path = BOARDS[name]
    if not path.is_file():
        pytest.skip(f"{path.name} is not installed")
    return path


@cache
def _public(name: str) -> PcbIndex:
    return PcbIndex(parse_pcb_doc(str(BOARDS[name])), None)


@cache
def _public_export(name: str) -> bv.Export:
    return bv.build_export(_public(name), "rev")


# ---------- a richer synthetic board ----------

def _rich(pcb_data):
    """test_pcb_index's board with an outline (one arc edge), arcs on copper and
    silkscreen, a free area with a hole, a pour poured solid and one hatched, silkscreen
    tracks, a pad on a non-copper layer and a component without a position."""
    data = deepcopy(pcb_data)
    board = data.board
    board.outline_shape = [ShapeVertex(0, 0), ShapeVertex(900, 0, (900, 100, 100, 270, 360)),
                           ShapeVertex(1000, 100), ShapeVertex(1000, 1000), ShapeVertex(0, 1000)]
    board.outline_vertices = shape_points(board.outline_shape)
    board.origin_x, board.origin_y = 100.0, 200.0
    arcs = decoded_arc(1, 400, 400, 120, 10, 200, 8)
    silk_arcs = decoded_arc(0xFFFF, 700, 300, 40, 0, 360, 6, layer=33)
    for row in silk_arcs:
        row.source_id = 99
    data.primitives += arcs + silk_arcs + [
        Primitive(kind="track", layer=33, net=None, component=0, x1=90, y1=90, x2=110, y2=90, width=5),
        Primitive(kind="track", layer=33, net=None, component=0, x1=110, y1=90, x2=110, y2=120, width=5),
        Primitive(kind="track", layer=2, net=0, component=None, x1=10, y1=900, x2=990, y2=900,
                  width=8, poured_by=1),
        Primitive(kind="pad", layer=57, net=None, component=None, x1=5, y1=5, x2=5, y2=5,
                  width=10, height=10, shape=1, pad_name="M"),
    ]
    data.pours.append(PolygonPour(1, 0, 2, "45Degree", [(0, 850), (1000, 850), (1000, 950), (0, 950)]))
    data.areas = [
        CopperArea("region", 1, 2, None, [(600, 600), (800, 600), (800, 800), (600, 800)],
                   holes=[[(650, 650), (700, 650), (700, 700)]]),
        CopperArea("fill", 32, None, 2, [(50, 50), (80, 50), (80, 80), (50, 80)]),
        CopperArea("region", 2, 0, None, [(0, 0), (1000, 0), (1000, 800), (0, 800)], pour_id=0),
    ]
    data.diff_pairs = [DiffPair(name="CLK", positive_net="CLK_A", negative_net="CLK_B", unique_id=None)]
    return data


@pytest.fixture
def rich(pcb_data):
    return PcbIndex(_rich(pcb_data), NETLIST)


# ---------- the reference decoder ----------

def _pages(export: bv.Export, **page0) -> list[dict]:
    return [json.loads(export.page(k, **page0) if k == 0 else export.page(k))
            for k in range(export.page_count)]


def _points(values: list[int]) -> list[tuple[int, int]]:
    x, y = values[0], values[1]
    out = [(x, y)]
    for i in range(2, len(values), 2):
        x += values[i]
        y += values[i + 1]
        out.append((x, y))
    return out


def _decode(pages: list[dict]) -> dict:
    """Every object of the pages, coordinates as the integers sent (0.1 mil from base)."""
    out: dict = {"nets": {}, "stacks": {}, "viatypes": {}, "parts": {}, "sch": {}, "pads": Counter(),
                 "vias": Counter(), "tracks": defaultdict(Counter), "poured_tracks": defaultdict(Counter),
                 "silk": defaultdict(Counter), "arcs": defaultdict(list), "poured_arcs": defaultdict(list),
                 "silkarcs": defaultdict(list), "areas": defaultdict(dict), "poured_areas": defaultdict(dict),
                 "pours": defaultdict(list), "outline": [], "cutouts": []}
    pieces = defaultdict(dict)
    singles = defaultdict(dict)  # pours and the board's rings: {key: {piece: (count, points)}}

    def joined(parts: dict) -> list:
        total = next(iter(parts.values()))[0]
        assert sorted(parts) == list(range(total))
        points = []
        for piece in range(total):
            run = parts[piece][1]
            if piece:
                assert run[0] == points[-1]  # a later piece starts at the last point
                run = run[1:]
            points += run
        return points

    for page in pages:
        for chunk in page["chunks"]:
            kind, layer, rows = chunk["k"], chunk["l"], chunk["rows"]
            stride = bv.STRIDES[kind]
            assert ("s" in chunk) == bool(stride) and chunk.get("s") == stride
            if kind == "nets":
                for number, name in enumerate(rows):
                    out["nets"][chunk["first"] + number] = name
                continue
            if stride:
                assert len(rows) % stride == 0
                groups = [rows[i:i + stride] for i in range(0, len(rows), stride)]
                if kind in ("stacks", "viatypes", "parts"):
                    for number, row in enumerate(groups):
                        out[kind][chunk["first"] + number] = row
                elif kind == "sch":
                    for number, name in groups:
                        out["sch"][str(number)] = name
                elif kind == "pads":
                    for x, y, stack, net, part, pin in groups:
                        out["pads"][(layer, x, y, tuple(out["stacks"][stack]), net, part, pin)] += 1
                elif kind == "vias":
                    for x, y, kind_of, net in groups:
                        out["vias"][(x, y, tuple(out["viatypes"][kind_of]), net)] += 1
                else:
                    out[kind][layer].extend(groups)
                continue
            for row in rows:
                if kind in ("tracks", "poured_tracks", "silk"):
                    head = (row[1], row[0]) if kind != "silk" else (row[0],)  # (net,) width
                    points = _points(row[len(head):])
                    for a, b in zip(points, points[1:]):
                        out[kind][layer][(*head, *sorted((a, b)))] += 1
                elif kind in ("areas", "poured_areas"):
                    number, net, part, rings, ring, piece, count = row[:7]
                    pieces[(kind, layer, number)].setdefault(ring, {})[piece] = (count, _points(row[7:]))
                    out[kind][layer][number] = (net, part, rings)
                elif kind == "pours":
                    pour, net, style, piece, count = row[:5]
                    singles[("pours", layer, pour, net, style)][piece] = (count, _points(row[5:]))
                elif kind == "board":
                    role, number, piece, count = row[:4]
                    singles[("board", role, number)][piece] = (count, _points(row[4:]))
    rings = {}
    for (kind, layer, number), by_ring in pieces.items():
        net, part, count = out[kind][layer][number]
        assert sorted(by_ring) == list(range(count))
        rings[(kind, layer, number)] = (net, part, [joined(by_ring[ring]) for ring in range(count)])
    out["rings"] = rings
    for key, parts in singles.items():
        if key[0] == "pours":
            out["pours"][key[1]].append((key[2], key[3], key[4], joined(parts)))
        elif key[1] == 0:
            out["outline"] = joined(parts)
        else:
            out["cutouts"].append((key[2], joined(parts)))
    out["cutouts"] = [points for _, points in sorted(out["cutouts"], key=lambda item: item[0])]
    return out


def _q(value: float, base: int) -> int:
    return int(round((value - base) * bv.Q))


def _expected_stack(p) -> tuple:
    hole = bv.length(p.hole) if p.hole and p.hole > 0 else 0
    return (p.shape or 1, bv.length(p.width), bv.length(p.height if p.height is not None else p.width),
            bv.rotation(p.rotation), bv.length(p.corner_radius), hole,
            (p.hole_shape or 0) if hole else 0, bv.length(p.slot_length) if hole else 0)


def _check_round_trip(index: PcbIndex, export: bv.Export) -> dict:
    """Rebuild every primitive from the pages and compare it with the board."""
    pcb = index.pcb
    db = index.db
    pages = _pages(export)
    first = pages[0]
    bx, by = first["base_mil"]
    decoded = _decode(pages)

    def q(x, y):
        return _q(x, bx), _q(y, by)

    # Nothing is negative: every coordinate counts from base_mil.
    for row in decoded["parts"].values():
        assert min(row[1:3] + row[5:]) >= 0
    for key in decoded["pads"]:
        assert key[1] >= 0 and key[2] >= 0
    assert [decoded["nets"][number] for number in range(len(pcb.nets))] == pcb.nets
    # Parts, by component index.
    rows = {row["id"]: row for row in db.execute("SELECT * FROM components")}
    for number, row in rows.items():
        sent = decoded["parts"][number]
        assert sent[0] == (row["refdes"] or "")
        if row["x"] is None:
            assert sent[4] == 2
            continue
        assert sent[4] == {"top": 0, "bottom": 1}.get(row["side"], 2)
        assert abs(bx + sent[1] / bv.Q - row["x"]) <= 0.05 + 1e-9
        assert abs(by + sent[2] / bv.Q - row["y"]) <= 0.05 + 1e-9
        assert sent[3] == bv.rotation(row["rotation"])
        assert sent[5:] == [*q(row["minx"], row["miny"]), *q(row["maxx"], row["maxy"])]
        if row["sch_refdes"] and row["sch_refdes"] != row["refdes"]:
            assert decoded["sch"][str(number)] == row["sch_refdes"]
        else:
            assert str(number) not in decoded["sch"]
    pads, vias = Counter(), Counter()
    tracks, poured, silk = defaultdict(Counter), defaultdict(Counter), defaultdict(Counter)
    off_copper = 0
    for p in pcb.primitives:
        net = -1 if p.net is None else p.net
        if p.kind == "pad":
            if p.layer != 74 and p.layer not in COPPER:
                off_copper += 1
                continue
            x, y = q(p.x1, p.y1)
            assert abs(bx + x / bv.Q - p.x1) <= 0.05 + 1e-9 and abs(by + y / bv.Q - p.y1) <= 0.05 + 1e-9
            pads[(p.layer, x, y, _expected_stack(p), net,
                  -1 if p.component is None else p.component, p.pad_name or "")] += 1
        elif p.kind == "via":
            vias[(*q(p.x1, p.y1), (bv.length(p.width), bv.length(p.hole), p.layer,
                                   p.layer if p.layer_to is None else p.layer_to), net)] += 1
        elif p.kind == "track" and p.pour_id is None:
            ends = tuple(sorted((q(p.x1, p.y1), q(p.x2, p.y2))))
            for value, original in zip(ends, sorted(((p.x1, p.y1), (p.x2, p.y2)))):
                assert abs(bx + value[0] / bv.Q - original[0]) <= 0.05 + 1e-9
            if p.layer in OVERLAYS:
                silk[p.layer][(bv.length(p.width), *ends)] += 1
            elif p.layer in COPPER:
                (tracks if p.poured_by is None else poured)[p.layer][(net, bv.length(p.width), *ends)] += 1
    assert decoded["pads"] == pads
    assert decoded["vias"] == vias
    assert dict(decoded["tracks"]) == dict(tracks)
    assert dict(decoded["poured_tracks"]) == dict(poured)
    assert dict(decoded["silk"]) == dict(silk)
    if off_copper:
        assert any(f"{bv.counted(off_copper, 'pad')} on layers other than copper" in note
                   for note in first["notes"])
    # Arcs: one row per arc, in the board's order per layer; their ends meet the chords'.
    groups = defaultdict(list)
    for chords in bv._arcs([p for p in pcb.primitives if p.kind == "arc"
                            and (p.layer in OVERLAYS or p.layer in COPPER)]):
        lead = chords[0]
        kind = "silkarcs" if lead.layer in OVERLAYS else "arcs" if lead.poured_by is None else "poured_arcs"
        groups[(kind, lead.layer)].append(chords)
    worst = 0.0
    for kind in ("arcs", "poured_arcs", "silkarcs"):
        assert {layer for layer in decoded[kind]} == {layer for k, layer in groups if k == kind}
        for layer, sent in decoded[kind].items():
            assert len(sent) == len(groups[(kind, layer)])
            for row, chords in zip(sent, groups[(kind, layer)]):
                cx, cy, r = bx + row[0] / bv.Q, by + row[1] / bv.Q, row[2] / bv.Q
                a0, a1 = math.radians(row[3] / 1000), math.radians((row[3] + row[4]) / 1000)
                begin = (cx + r * math.cos(a0), cy + r * math.sin(a0))
                end = (cx + r * math.cos(a1), cy + r * math.sin(a1))
                worst = max(worst, math.dist(begin, (chords[0].x1, chords[0].y1)),
                            math.dist(end, (chords[-1].x2, chords[-1].y2)))
                assert row[5] == bv.length(chords[0].width)
                if kind != "silkarcs":
                    assert row[6] == (-1 if chords[0].net is None else chords[0].net)
    assert worst <= 0.1
    # Areas (rings with holes, pieces rejoined) and pours.
    free = defaultdict(list)
    for area in pcb.areas:
        if area.layer in COPPER and len(area.outline) >= 3:
            free[("areas" if area.pour_id is None else "poured_areas", area.layer)].append(area)
    sent_rings = defaultdict(list)
    for (kind, layer, _number), (net, part, rings) in sorted(decoded["rings"].items()):
        sent_rings[(kind, layer)].append((net, part, rings))
    for (kind, layer), areas in free.items():
        expected = [(-1 if a.net is None else a.net,
                     (-1 if a.component is None else a.component) if kind == "areas" else a.pour_id,
                     [[q(x, y) for x, y in ring] for ring in [a.outline, *a.holes]]) for a in areas]
        assert sent_rings[(kind, layer)] == expected
    pours = defaultdict(list)
    for pour in pcb.pours:
        if not pour.shelved and pour.layer in COPPER and len(pour.vertices) >= 3:
            pours[pour.layer].append((pour.pour_id, -1 if pour.net is None else pour.net,
                                      bv._hatch(pour.hatch_style), [q(x, y) for x, y in pour.vertices]))
    assert dict(decoded["pours"]) == dict(pours)
    # The outline and cutouts (board chunks), never in the header.
    assert not {"outline", "cutouts", "sch"} & set(first)
    outline = pcb.board.outline_vertices
    assert decoded["outline"] == ([q(x, y) for x, y in outline] if len(outline) >= 3 else [])
    cutouts, _ = bv.read_board_cutouts(pcb)
    expected_cutouts = [[q(x, y) for x, y in shape_points(c.vertices)] for c in cutouts if len(c.vertices) >= 3]
    assert decoded["cutouts"] == expected_cutouts
    assert first["counts"]["cutouts"] == len(expected_cutouts)
    # Counts per kind and layer, as the PcbIndex holds them.

    def sql(query: str) -> dict:
        return {row[0]: row[1] for row in db.execute(query)}

    routing = sql("SELECT layer, COUNT(*) FROM prims WHERE kind = 'track' AND pour_id IS NULL "
                  "AND poured_by IS NULL GROUP BY layer")
    assert {layer: sum(c.values()) for layer, c in decoded["tracks"].items()} == \
        {layer: n for layer, n in routing.items() if layer in COPPER}
    assert {layer: sum(c.values()) for layer, c in decoded["silk"].items()} == \
        {layer: n for layer, n in routing.items() if layer in OVERLAYS}
    arcs = sql("SELECT layer, COUNT(DISTINCT source_id) FROM prims WHERE kind = 'arc' "
               "AND poured_by IS NULL GROUP BY layer")
    assert {layer: len(rows_) for layer, rows_ in decoded["arcs"].items()} == \
        {layer: n for layer, n in arcs.items() if layer in COPPER}
    assert {layer: len(rows_) for layer, rows_ in decoded["silkarcs"].items()} == \
        {layer: n for layer, n in arcs.items() if layer in OVERLAYS}
    hatched = sql("SELECT layer, COUNT(*) FROM prims WHERE kind = 'track' AND poured_by IS NOT NULL "
                  "GROUP BY layer")
    assert {layer: sum(c.values()) for layer, c in decoded["poured_tracks"].items()} == \
        {layer: n for layer, n in hatched.items() if layer in COPPER}
    pad_count = sql("SELECT layer, COUNT(*) FROM prims WHERE kind = 'pad' GROUP BY layer")
    assert Counter(key[0] for key in decoded["pads"].elements()) == \
        Counter({layer: n for layer, n in pad_count.items() if layer in COPPER or layer == 74})
    assert sum(decoded["vias"].values()) == index.counts()["vias"]
    counts = first["counts"]
    assert counts.get("tracks", 0) == sum(n for layer, n in routing.items() if layer in COPPER)
    assert counts.get("pads", 0) == sum(pads.values()) and counts.get("vias", 0) == sum(vias.values())
    assert counts["nets"] == len(pcb.nets) and counts["parts"] == len(rows)
    return {"decoded": decoded, "pages": pages, "worst_arc_end": worst}


# ---------- the export: round trip ----------

def test_the_pages_rebuild_every_object_of_a_synthetic_board(rich):
    export = bv.build_export(rich, "rev")
    checked = _check_round_trip(rich, export)
    decoded = checked["decoded"]
    first = checked["pages"][0]
    # Every kind is exercised: arcs on copper and silkscreen, a free area with its hole,
    # a solid and a hatched pour's copper, both pours' outlines and the outline's arc.
    assert decoded["arcs"][1] and decoded["silkarcs"][33] and decoded["silk"][33]
    assert decoded["rings"][("areas", 1, 0)][2][1]  # the hole
    assert decoded["poured_areas"][2] and decoded["poured_tracks"][2]
    assert len(decoded["pours"][2]) == 2 and {row[2] for row in decoded["pours"][2]} == {0, 1}
    assert len(decoded["outline"]) > 4  # the arc edge as chords
    assert decoded["sch"] == {"0": "U3A", "1": "U3B"}
    assert first["origin_mil"] == [100.0, 200.0] and first["embedded_boards"] == 0
    layers = {layer["id"]: layer for layer in first["layers"]}
    assert layers[1]["kind"] == "top" and layers[32]["kind"] == "bottom" and layers[2]["kind"] == "inner"
    assert layers[33]["kind"] == "overlay" and layers[74]["kind"] == "multi"
    assert [layer["id"] for layer in first["layers"] if layer["kind"] not in ("overlay", "multi")] == [1, 2, 3, 32]
    assert "1 pad on layers other than copper is not drawn." in first["notes"]
    # A two-way chain: the two silkscreen tracks share an end, one row holds both.
    silk_rows = [row for page in checked["pages"] for chunk in page["chunks"]
                 if chunk["k"] == "silk" for row in chunk["rows"]]
    assert len(silk_rows) == 1 and len(silk_rows[0]) == 1 + 2 + 2 * 2


@pytest.mark.parametrize("name", list(BOARDS))
def test_the_pages_rebuild_every_object_of_the_public_examples(name):
    _need(name)
    checked = _check_round_trip(_public(name), _public_export(name))
    assert checked["worst_arc_end"] <= 0.1


def test_long_polylines_and_rings_are_cut_and_rejoin(pcb_data, monkeypatch):
    monkeypatch.setattr(bv, "MAX_CHAIN_POINTS", 5)
    monkeypatch.setattr(bv, "MAX_RING_POINTS", 4)
    data = deepcopy(pcb_data)
    data.primitives += [Primitive(kind="track", layer=1, net=1, component=None, x1=10 * i, y1=500,
                                  x2=10 * i + 10, y2=500, width=4) for i in range(17)]
    data.areas = [CopperArea("region", 1, 0, None, [(x, 600 + (x % 20)) for x in range(0, 200, 10)] +
                             [(190, 700), (0, 700)], holes=[[(20, 650), (40, 650), (40, 670), (30, 680), (20, 670)]])]
    index = PcbIndex(data, NETLIST)
    checked = _check_round_trip(index, bv.build_export(index, "rev"))
    area_rows = [row for page in checked["pages"] for chunk in page["chunks"] if chunk["k"] == "areas"
                 for row in chunk["rows"]]
    assert max(row[6] for row in area_rows) > 3  # the outline came in pieces
    chain_rows = [row for page in checked["pages"] for chunk in page["chunks"] if chunk["k"] == "tracks"
                  for row in chunk["rows"] if row[1] == 1]
    assert len(chain_rows) >= 4


def _cutout(x: float, y: float, size: float) -> bytes:
    from tests.test_connectivity import _region_record
    square = [(x, y), (x + size, y), (x + size, y + size), (x, y + size)]
    return _region_record(56, 0xFFFF, 0xFFFF, square, extra="|ISBOARDCUTOUT=TRUE")


def test_a_long_outline_many_cutouts_and_a_long_pour_keep_every_page_in_budget(pcb_data):
    # The header holds no ring: the outline, 400 cutouts and a pour outline longer than a
    # page come as chunks in pieces, and page 0 stays within PAGE_CHARS even with every
    # part Not Fitted (a header of 66,000 and 70,000 characters before).
    data = _rich(pcb_data)
    data.board.outline_vertices = [(500 + 480 * math.cos(2 * math.pi * n / 12_000),
                                    500 + 480 * math.sin(2 * math.pi * n / 12_000)) for n in range(12_000)]
    data.raw = {"Regions6/Data": b"".join(_cutout(100 + 20 * (n % 40), 100 + 20 * (n // 40), 7.5)
                                          for n in range(400))}
    data.pours.append(PolygonPour(5, 1, 3, "Solid", [(10 + 0.06 * n, 20 + (7 if n % 2 else 0))
                                                     for n in range(15_000)] + [(910, 60), (10, 60)]))
    index = PcbIndex(data, NETLIST)
    export = bv.build_export(index, "rev")
    pages = _check_pages(export)
    checked = _check_round_trip(index, export)
    assert len(checked["decoded"]["cutouts"]) == 400 and len(checked["decoded"]["outline"]) == 12_000
    assert len(pages[0]["chunks"]) and len(json.dumps(pages[0]["chunks"])) > 10_000  # room for chunks
    pour_rows = [row for page in pages for chunk in page["chunks"] if chunk["k"] == "pours" for row in chunk["rows"]]
    assert max(row[4] for row in pour_rows) > 1  # the long pour came in pieces
    assert not any("too long" in note for note in pages[0]["notes"])


def test_split_stacks_via_types_and_parts_keep_their_numbers(pcb_data, monkeypatch):
    # Many pad stacks on small pages: a chunk split over pages carries the number of its
    # first row, so the pads still name their own stack.
    data = deepcopy(pcb_data)
    data.primitives += [Primitive(kind="pad", layer=1, net=0, component=2, x1=20 + 3 * n, y1=900,
                                  x2=20 + 3 * n, y2=900, width=1 + n / 10, height=2, shape=2, pad_name=f"S{n}")
                        for n in range(300)]
    data.primitives += [Primitive(kind="via", layer=1, layer_to=32, net=1, component=None, x1=5 * n, y1=950,
                                  x2=5 * n, y2=950, width=10 + n / 10, hole=4.0) for n in range(500)]
    monkeypatch.setattr(bv, "PAGE_CHARS", 3_000)
    monkeypatch.setattr(bv, "NEW_PAGE_BELOW", 200)
    index = PcbIndex(data, NETLIST)
    export = bv.build_export(index, "rev")
    pages = _check_pages(export)
    split = Counter(chunk["k"] for page in pages for chunk in page["chunks"])
    assert split["stacks"] > 1 and split["viatypes"] > 1
    _check_round_trip(index, export)


def test_a_board_beyond_the_session_pages_says_what_is_not_sent(pcb_data, monkeypatch):
    data = _rich(pcb_data)
    data.primitives += [Primitive(kind="track", layer=(1, 32)[n % 2], net=n % 3, component=None,
                                  x1=7 * n, y1=10 + n, x2=7 * n + 5, y2=20 + 2 * n, width=4 + n % 5)
                        for n in range(400)]
    data.areas += [CopperArea("region", 3, 0, None, [(10 * n, 0), (10 * n + 8, 0), (10 * n + 8, 40)],
                              pour_id=0) for n in range(80)]
    monkeypatch.setattr(bv, "PAGE_CHARS", 3_000)
    monkeypatch.setattr(bv, "NEW_PAGE_BELOW", 200)
    monkeypatch.setattr(bv, "AUTO_PAGES", 3)
    monkeypatch.setattr(bv, "SESSION_PAGES", 6)
    export = bv.build_export(PcbIndex(data, NETLIST), "rev")
    pages = _check_pages(export)
    assert len(pages) == 6 and pages[0]["pages"] == 6
    paging = pages[0]["paging"]
    # The routing alone passes the automatic pages, and the poured copper is not sent.
    assert paging.startswith("The board takes ") and "the view reads 3 of its" in paging
    assert "not sent: the view reads at most 6 pages for one answer" in paging
    assert "2 more pages (poured copper on L2-GND, L3-SIG) are not sent" in paging
    assert bv.build_export(_rich_index(pcb_data), "rev").head["paging"] is None


def _rich_index(pcb_data) -> PcbIndex:
    return PcbIndex(_rich(pcb_data), NETLIST)


# ---------- paging ----------

def _check_pages(export: bv.Export) -> list[dict]:
    texts = [export.page(k) for k in range(export.page_count)]
    pages = [json.loads(text) for text in texts]
    assert all(len(text) <= bv.PAGE_CHARS for text in texts)
    # Page 0 stays in its budget when every part is Not Fitted, in a long-named variant.
    parts = len(export.dnp_names)
    worst = export.page(0, "V" * 60, list(range(parts)))
    assert len(worst) <= bv.PAGE_CHARS and json.loads(worst)["dnp"] == list(range(parts))
    plan = pages[0]["plan"]
    assert [entry["page"] for entry in plan] == list(range(len(pages))) == [page["page"] for page in pages]
    for entry, page, text in zip(plan, pages, texts):
        chunks = []
        for chunk in page["chunks"]:
            if not chunks or chunks[-1] != [chunk["k"], chunk["l"]]:
                chunks.append([chunk["k"], chunk["l"]])
        assert entry["chunks"] == chunks
        if entry["page"]:
            assert entry["chars"] == len(text)
            assert page["pages"] == len(pages) and page["rev"] == pages[0]["rev"]
            assert page["has_more"] == (entry["page"] < len(pages) - 1)
            assert page["next_offset"] == (entry["page"] + 1 if page["has_more"] else None)
    assert pages[0]["auto_pages"] == sum(entry["auto"] for entry in plan)
    poured = [entry for entry in plan if entry["chunks"] and
              all(kind.startswith("poured_") for kind, _ in entry["chunks"])]
    if len(pages) <= bv.AUTO_PAGES:
        assert all(entry["auto"] for entry in plan)
    else:
        # Poured copper on request, each layer from a page of its own; nothing else waits.
        assert all(not entry["auto"] for entry in poured)
        assert all(entry["auto"] for entry in plan if entry not in poured)
        assert not any(kind.startswith("poured_") for entry in plan if entry not in poured
                       for kind, _ in entry["chunks"])
        started = set()
        for entry in poured:
            layer = entry["chunks"][0][1]
            if layer not in started:
                started.add(layer)
                previous = plan[entry["page"] - 1]
                assert not any(kind.startswith("poured_") and l_ == layer for kind, l_ in previous["chunks"])
    return pages


def test_the_plan_and_pages_of_a_synthetic_board(rich, pcb_data, monkeypatch):
    pages = _check_pages(bv.build_export(rich, "rev"))
    assert len(pages) == 1 and pages[0]["has_more"] is False and pages[0]["next_offset"] is None
    # A small page budget: many pages, chunks split at row boundaries, poured copper on
    # two layers read on request.
    data = _rich(pcb_data)
    data.primitives += [Primitive(kind="track", layer=(1, 3, 32)[n % 3], net=n % 3, component=None,
                                  x1=7 * n, y1=10 + n, x2=7 * n + 5, y2=20 + 2 * n, width=4 + n % 5)
                        for n in range(120)]
    data.areas += [CopperArea("region", layer, 0, None, [(10 * n, 0), (10 * n + 8, 0), (10 * n + 8, 40)],
                              pour_id=0) for layer in (1, 32) for n in range(40)]
    index = PcbIndex(data, NETLIST)
    monkeypatch.setattr(bv, "PAGE_CHARS", 3_500)
    monkeypatch.setattr(bv, "NEW_PAGE_BELOW", 200)
    monkeypatch.setattr(bv, "AUTO_PAGES", 2)
    export = bv.build_export(index, "rev")
    pages = _check_pages(export)
    assert len(pages) > 2 and any(not entry["auto"] for entry in pages[0]["plan"])
    poured_layers = {layer for entry in pages[0]["plan"] if not entry["auto"] for _, layer in entry["chunks"]}
    assert poured_layers == {1, 2, 32}
    _check_round_trip(index, export)


@pytest.mark.parametrize("name, pages, auto_all", [
    ("SODIMM", 13, True), ("SL1", 7, True), ("Sentinel", 6, True), ("WiFi", 1, True),
    ("WiFi_panel", 1, True),
])
def test_the_public_examples_fit_their_page_budgets(name, pages, auto_all):
    _need(name)
    export = _public_export(name)
    checked = _check_pages(export)
    assert len(checked) <= pages
    assert all(entry["auto"] for entry in checked[0]["plan"]) == auto_all
    if name == "Sentinel":  # hatched pours: their tracks and arcs come automatically
        kinds = {kind for entry in checked[0]["plan"] for kind, _ in entry["chunks"]}
        assert {"poured_tracks", "poured_arcs"} <= kinds
    if name == "WiFi_panel":
        assert checked[0]["embedded_boards"] == 1
        assert any("embedded board" in note for note in checked[0]["notes"])


def test_the_mini_pc_reads_its_default_pages_and_its_poured_copper_on_request():
    _need("MiniPC")
    export = _public_export("MiniPC")
    pages = _check_pages(export)
    plan = pages[0]["plan"]
    assert len(pages) > bv.AUTO_PAGES
    assert sum(entry["auto"] for entry in plan) <= bv.AUTO_PAGES
    assert "poured copper is drawn as pour outlines until loaded per layer" in pages[0]["paging"]
    assert "routing pages" not in pages[0]["paging"]  # its routing fits the automatic pages
    requested = [entry for entry in plan if not entry["auto"]]
    assert requested and all(kind == "poured_areas" or kind.startswith("poured_")
                             for entry in requested for kind, _ in entry["chunks"])


def test_the_export_is_built_once_per_board_and_fast():
    _need("MiniPC")
    index = PcbIndex(parse_pcb_doc(str(BOARDS["MiniPC"])), None)
    started = time.perf_counter()
    export = bv.export_for(index, "a")
    assert time.perf_counter() - started <= 3.0  # about 0.7 s measured (1.5 s budget)
    assert bv.export_for(index, "a") is export
    started = time.perf_counter()
    for offset in range(1, export.page_count):
        export.page(offset)
    assert (time.perf_counter() - started) / (export.page_count - 1) <= 0.05
    assert bv.export_for(index, "b") is not export  # another revision is built again


# ---------- the tools on a loaded board ----------

def _load(monkeypatch, index: PcbIndex, path: Path, netlist=NETLIST, variants=None):
    """index as the loaded project's board (path is its file on disk)."""
    session = main_mod.PcbSession()
    session._load = lambda pcb_path, netlist_: index
    monkeypatch.setattr(main_mod, "_pcb_session", session)
    monkeypatch.setattr(main_mod, "_project", {
        "name": "Demo", "root_dir": str(path.parent), "prj_pcb_path": str(path.with_suffix(".PrjPcb")),
        "sheets": [], "pcb_doc_paths": [str(path)],
    })
    monkeypatch.setattr(main_mod._altium, "_netlist", netlist)
    monkeypatch.setattr(main_mod, "_variant_state",
                        VariantState(variants or [VariantDefinition(name="Default")]))
    return index


@pytest.fixture
def loaded(rich, monkeypatch, tmp_path):
    path = tmp_path / "Board.PcbDoc"
    path.write_bytes(b"fake")
    return _load(monkeypatch, rich, path)


def _view(**args) -> dict:
    return json.loads(main_mod.get_board_view(**args))


def test_the_text_answers_where_nets_parts_and_spots_are(loaded):
    data = _view(nets="CLK_A", parts="R1", x=300, y=300, units="mil")
    assert data["pcb_doc"] == "Board.PcbDoc" and data["units"] == "mil"
    assert data["frame"] == bv.FRAME and data["note"] == bv.VIEW_NOTE
    assert data["origin_mil"] == {"x_mil": 100.0, "y_mil": 200.0}
    assert data["board_extents_mil"] == {"minx_mil": 0, "miny_mil": 0, "maxx_mil": 1000, "maxy_mil": 1000}
    assert data["copper_layers"] == ["Top Layer", "L2-GND", "L3-SIG", "Bottom Layer"]
    assert data["variant"] == "Default"
    assert data["asked"] == {"nets": "CLK_A", "parts": "R1", "x": 300, "y": 300, "side": "auto", "units": "mil"}
    h = data["highlight"]
    (net,) = h["nets"]
    assert net == {"net": "CLK_A", "from": "asked", "layers": ["Top Layer", "L3-SIG"], "pads": 1, "vias": 1,
                   "tracks": 2, "arcs": 1, "areas": 0, "pours": 0, "parts_total": 1, "parts": ["U3"],
                   "extents_mil": net["extents_mil"], "where": net["where"]}
    assert net["where"] in ("bottom left", "left", "bottom")
    (part,) = h["parts"]
    assert part["designator"] == "R1" and part["side"] == "bottom" and part["pads"] == 1
    assert part["at_mil"] == {"x_mil": 300.0, "y_mil": 300.0}
    assert part["from_origin_mil"] == {"x_mil": 200.0, "y_mil": 100.0}
    assert part["rotation"] == 90.0 and part["dnp"] is False and "sch_refdes" not in part
    (mark,) = h["marks"]
    assert mark["label"] == "300, 300" and mark["on_board"] is True and "frame_hint" not in mark
    assert mark["near"] == {"nets": ["GND"], "parts": ["R1"]} and mark["where"] == "bottom left"
    assert h["nets_total"] == 1 and h["parts_total"] == 1 and h["marks_total"] == 1
    assert h["not_found"] == [] and h["not_found_total"] == 0
    view = data["view"]
    assert view["layers_on"] == ["Top Layer", "L2-GND", "L3-SIG", "Bottom Layer"]  # four layers: all
    assert view["geometry_rev"] == bv.geometry_rev(*main_mod._pcb_session.loaded)
    assert view["poured_copper"] == bv.POURED_COPPER_NOTE and view["not_drawn"] == bv.NOT_DRAWN
    window = view["window_mil"]
    assert window["minx_mil"] < net["extents_mil"]["minx_mil"] and window["maxx_mil"] > 300


def test_names_resolve_by_regex_pair_channel_and_pad(loaded):
    h = _view(nets="CLK_.*")["highlight"]
    assert [row["net"] for row in h["nets"]] == ["CLK_A", "CLK_B"]
    h = _view(nets="clk")["highlight"]  # the differential pair: both of its nets
    assert [row["net"] for row in h["nets"]] == ["CLK_A", "CLK_B"]
    h = _view(nets="CLKX, gnd")["highlight"]
    assert [row["net"] for row in h["nets"]] == ["GND"]
    assert h["not_found"] == [{"kind": "net", "asked": "CLKX", "why": "no net of this board has this name",
                               "suggestions": ["CLK_A", "CLK_B"]}]
    # The logical designator gives every channel instance; the netlist's names one.
    h = _view(parts="U3")["highlight"]
    assert [(row["designator"], row["sch_refdes"]) for row in h["parts"]] == [("U3", "U3A"), ("U3", "U3B")]
    h = _view(parts="U3B")["highlight"]
    assert [row["sch_refdes"] for row in h["parts"]] == ["U3B"]
    # Pads as U3-2 and U3.2: one per channel instance.
    for text in ("U3-2", "U3.2"):
        h = _view(parts=text)["highlight"]
        assert [row["pad"] for row in h["pads"]] == ["U3-2", "U3-2"] and h["pads_total"] == 2
        assert {row["net"] for row in h["pads"]} == {"GND"}
    h = _view(parts="U3-9")["highlight"]
    assert h["not_found"] == [{"kind": "pad", "asked": "U3-9", "why": "U3 has no pad 9"}]
    h = _view(parts="Q7")["highlight"]
    assert h["not_found"][0]["why"] == "no part of this board has this designator"


def test_too_many_nets_and_a_part_only_in_the_schematic(loaded, monkeypatch):
    netlist = deepcopy(NETLIST)
    netlist["components"]["Q7"] = {"pins": {}}
    monkeypatch.setattr(main_mod._altium, "_netlist", netlist)
    main_mod._pcb_session.invalidate()
    h = _view(parts="Q7")["highlight"]
    assert h["not_found"][0]["why"] == "in the schematic, not on the board (check_sch_pcb_sync)"
    index = loaded
    index.pcb.nets.extend(f"N{n}" for n in range(30))
    index.__init__(index.pcb, NETLIST)
    h = _view(nets=r"^N\d+")["highlight"]
    assert h["nets"] == [] and h["not_found"][0]["why"] == "matches 30 nets: be more specific"
    h = _view(nets=",".join(f"N{n}" for n in range(21)))["highlight"]
    assert h["nets_total"] == 21 and len(h["nets"]) == 20  # the one left out counts
    assert h["not_found"] == [{"kind": "net", "asked": "N20",
                               "why": "too many: at most 20 nets are highlighted, and it is not"}]


def test_millimetres_dnp_side_and_layers(loaded, monkeypatch):
    monkeypatch.setattr(main_mod, "_variant_state", VariantState([
        VariantDefinition(name="Lite", dnp_refdes=["R1"])]))
    data = _view(parts="R1", x=7.62, y=7.62, units="mm", layers="top, nope")
    assert data["units"] == "mm" and "origin_mm" in data and "board_extents_mm" in data
    (part,) = data["highlight"]["parts"]
    assert part["dnp"] is True and part["at_mm"] == {"x_mm": 7.62, "y_mm": 7.62}
    (mark,) = data["highlight"]["marks"]
    assert mark["label"] == "7.62, 7.62" and mark["at_mm"] == {"x_mm": 7.62, "y_mm": 7.62}
    assert data["view"]["side"] == "bottom"  # every part asked for is on the bottom
    assert data["view"]["layers_on"] == ["Top Layer"]
    assert data["highlight"]["not_found"] == [{
        "kind": "layer", "asked": "nope", "why": "not a copper layer of this board",
        "suggestions": ["Top Layer", "L2-GND", "L3-SIG", "Bottom Layer"]}]
    assert "window_mm" in data["view"] and data["variant"] == "Lite"
    assert _view(parts="R1, U3")["view"]["side"] == "top"
    assert _view(parts="R1", side="top")["view"]["side"] == "top"
    assert _view(layers="inner")["view"]["layers_on"] == ["L2-GND", "L3-SIG"]
    assert _view(layers="all, bottom")["view"]["layers_on"] == data["copper_layers"]


def test_frame_hints_both_ways_and_marks_off_the_board(loaded):
    # x and y relative to the origin by mistake: off the board, on it with the origin added.
    (mark,) = _view(x=150, y=150, units="mil")["highlight"]["marks"]
    assert mark["on_board"] is True
    (mark,) = _view(x=-50, y=-150, units="mil")["highlight"]["marks"]
    assert mark["on_board"] is False and mark["where"] == "outside the board (bottom left)"
    assert mark["frame_hint"] == "looks origin-relative: x and y are absolute, add origin_*"
    # A finding read as origin-relative whose point is absolute: the hint says so.
    finding = {"check": "spacing", "at_mil": [950.0, 950.0]}
    (mark,) = _view(findings=finding, finding_tool="check_dfm", units="mil")["highlight"]["marks"]
    assert mark["at_mil"] == {"x_mil": 1050.0, "y_mil": 1150.0} and mark["on_board"] is False
    assert mark["frame_hint"] == "looks absolute: check finding_tool"
    assert mark["label"] == "DFM 1: spacing" and mark["from"] == "finding 1"
    # An absolute finding point that is origin-relative.
    finding = {"rule": "Clearance", "location_mil": {"x_mil": -60.0, "y_mil": -100.0}}
    (mark,) = _view(findings=[finding], finding_tool="get_drc_report", units="mil")["highlight"]["marks"]
    assert mark["frame_hint"] == "looks origin-relative: check finding_tool"
    data = _view(x=5000, y=5000, units="mil")
    assert data["view"]["window_mil"]["maxx_mil"] > 5000  # the window takes it in


def test_the_errors(loaded, monkeypatch, tmp_path):
    def error(**args):
        return _view(**args)["error"]
    assert error(units="inch") == "invalid_units"
    assert error(x=1.0) == "invalid_coordinate" and error(y=1.0) == "invalid_coordinate"
    assert error(x=math.nan, y=1.0) == "invalid_coordinate"
    assert error(x=1.0, y=math.inf) == "invalid_coordinate"
    assert error(radius=-1.0) == "invalid_radius" and error(radius=math.inf) == "invalid_radius"
    for findings in ("not json", [1, 2], 5, '"x"'):
        assert error(findings=findings) == "invalid_findings"
    assert "error" not in _view(findings='{"net": "GND"}')  # a JSON string holding one
    monkeypatch.setitem(main_mod._project, "pcb_doc_paths", [])
    assert error() == "no_pcb_document"
    assert json.loads(main_mod.get_board_view_geometry("x"))["error"] == "no_pcb_document"
    monkeypatch.setitem(main_mod._project, "pcb_doc_paths", [str(tmp_path / "gone.PcbDoc")])
    assert error() == "pcb_file_missing"
    monkeypatch.setattr(main_mod, "_project", None)
    assert error() == "no_project"
    assert json.loads(main_mod.get_board_view_geometry("x"))["error"] == "no_project"


def test_a_board_that_fails_to_parse_is_an_error(monkeypatch, tmp_path):
    path = tmp_path / "Broken.PcbDoc"
    path.write_bytes(b"not an OLE file")
    monkeypatch.setattr(main_mod, "_pcb_session", main_mod.PcbSession())
    monkeypatch.setattr(main_mod, "_project", {"name": "Demo", "root_dir": str(tmp_path),
                                               "prj_pcb_path": str(tmp_path / "D.PrjPcb"),
                                               "sheets": [], "pcb_doc_paths": [str(path)]})
    monkeypatch.setattr(main_mod._altium, "_netlist", None)
    monkeypatch.setattr(main_mod, "_variant_state", VariantState([]))
    assert _view()["error"] == "pcb_parse_failed"


def test_a_netlist_free_load_and_several_boards(loaded, monkeypatch, tmp_path):
    monkeypatch.setattr(main_mod._altium, "_netlist", None)
    second = tmp_path / "Other.PcbDoc"
    monkeypatch.setitem(main_mod._project, "pcb_doc_paths", [*main_mod._project["pcb_doc_paths"], str(second)])
    data = _view(parts="U3, Q7")
    assert main_mod.NETLIST_FREE_PCB_NOTE in data["notes"]
    assert any("Project lists 2 PcbDocs; using 'Board.PcbDoc'" in note for note in data["notes"])
    assert [row["designator"] for row in data["highlight"]["parts"]] == ["U3", "U3"]
    assert data["highlight"]["not_found"][0]["why"] == "no part of this board has this designator"


def test_the_lists_shrink_to_fit_and_keep_their_totals(loaded, monkeypatch):
    findings = [{"rule": f"R{n}", "location_mil": {"x_mil": 10.0 * n, "y_mil": 500.0},
                 "components": ["U3", "R1"], "nets": ["GND", "CLK_A"]} for n in range(25)]
    full = _view(findings=findings, finding_tool="get_drc_report", units="mil")
    h = full["highlight"]
    assert h["findings_total"] == 20 and h["marks_total"] == 20 and len(h["marks"]) == 20
    assert "25 findings given: the first 20 are read." in full["notes"]
    assert full["asked"]["findings"] == {"count": 25, "finding_tool": "get_drc_report"}
    text = main_mod.get_board_view(findings=findings, finding_tool="get_drc_report", units="mil")
    monkeypatch.setattr(main_mod, "QUERY_NET_MAX_RESULT_SIZE_CHARS", len(text) - 1)
    shrunk = _view(findings=findings, finding_tool="get_drc_report", units="mil")["highlight"]
    assert len(shrunk["marks"]) == 15 and shrunk["marks_total"] == 20 and len(shrunk["findings"]) == 15
    monkeypatch.setattr(main_mod, "QUERY_NET_MAX_RESULT_SIZE_CHARS", 6_000)
    smallest = _view(findings=findings, finding_tool="get_drc_report", units="mil")["highlight"]
    assert len(smallest["marks"]) == 5 and smallest["marks_total"] == 20
    assert smallest["findings_total"] == 20 and smallest["nets_total"] == 2


def test_the_text_on_the_mini_pc_is_small_and_quick(monkeypatch, tmp_path):
    _need("MiniPC")
    index = _load(monkeypatch, _public("MiniPC"), BOARDS["MiniPC"], netlist=None)
    busiest = index.db.execute(
        "SELECT n.name FROM nets n JOIN prims p ON p.net_id = n.id GROUP BY n.id "
        "ORDER BY COUNT(*) DESC LIMIT 1").fetchone()[0]
    largest = index.db.execute(
        "SELECT c.refdes FROM components c JOIN prims p ON p.component = c.id AND p.kind = 'pad' "
        "GROUP BY c.id ORDER BY COUNT(*) DESC LIMIT 1").fetchone()[0]
    small = [row[0] for row in index.db.execute(
        "SELECT n.name FROM nets n JOIN prims p ON p.net_id = n.id AND p.kind = 'pad' "
        "GROUP BY n.id HAVING COUNT(*) = 2 LIMIT 2")]
    main_mod.get_board_view()  # the board is read once
    finding = {"rule": "Clearance", "nets": small, "location_mil": {"x_mil": 9000.0, "y_mil": 8000.0}}
    for args in ({"nets": busiest}, {"parts": largest},
                 {"findings": finding, "finding_tool": "get_drc_report"}):
        text = main_mod.get_board_view(**args)
        assert len(text) <= 4_000, args
    marks = [{"location_mil": {"x_mil": 8500.0 + 37 * n, "y_mil": 7000.0 + 23 * n}} for n in range(50)]
    started = time.perf_counter()
    data = json.loads(main_mod.get_board_view(nets=busiest, findings={"rule": "r", "examples": marks},
                                              finding_tool="get_drc_report"))
    assert time.perf_counter() - started <= 0.6  # 300 ms budget, measured far below
    assert data["highlight"]["marks_total"] == 50


# ---------- the geometry tool ----------

def test_the_geometry_tool_pages_and_its_errors(monkeypatch, tmp_path):
    _need("WiFi")
    copy = tmp_path / "WiFi.PcbDoc"
    shutil.copy(BOARDS["WiFi"], copy)
    monkeypatch.setattr(main_mod, "_pcb_session", main_mod.PcbSession())
    monkeypatch.setattr(main_mod, "_project", {"name": "WiFi", "root_dir": str(tmp_path),
                                               "prj_pcb_path": str(tmp_path / "WiFi.PrjPcb"),
                                               "sheets": [], "pcb_doc_paths": [str(copy)]})
    monkeypatch.setattr(main_mod._altium, "_netlist", None)
    monkeypatch.setattr(main_mod, "_variant_state", VariantState([VariantDefinition(name="Default")]))
    rev = _view()["view"]["geometry_rev"]
    first = json.loads(main_mod.get_board_view_geometry(rev))
    assert first["geometry"] == 1 and first["page"] == 0 and first["rev"] == rev
    assert first["pages"] == 1 and first["variant"] == "Default" and first["dnp"] == []
    bad = json.loads(main_mod.get_board_view_geometry(rev, 5))
    assert bad == {"error": "invalid_offset", "pages": 1, "message": "offset is a page number from 0 to 0."}
    assert json.loads(main_mod.get_board_view_geometry(rev, -1))["error"] == "invalid_offset"
    os.utime(copy, (2_000_000_000, 2_000_000_000))  # saved again
    changed = json.loads(main_mod.get_board_view_geometry(rev))
    assert changed["error"] == "board_changed" and changed["rev"] != rev
    assert re.fullmatch(r"[0-9a-f]{12}", changed["rev"])
    assert json.loads(main_mod.get_board_view_geometry(changed["rev"]))["page"] == 0
    assert changed["rev"] == _view()["view"]["geometry_rev"]


def test_page_zero_carries_the_active_variants_not_fitted_parts(loaded, monkeypatch):
    monkeypatch.setattr(main_mod, "_variant_state", VariantState([
        VariantDefinition(name="Lite", dnp_refdes=["U3B", "R1"])]))
    rev = _view()["view"]["geometry_rev"]
    page = json.loads(main_mod.get_board_view_geometry(rev, 0))
    assert page["variant"] == "Lite" and page["dnp"] == [1, 2]  # by schematic designator


def test_the_geometry_tool_is_app_only_without_structured_content(loaded):
    tool = _tools()["get_board_view_geometry"]
    assert tool["_meta"] == uv.app_only_meta({"anthropic/maxResultSizeChars": LIMIT}) == {
        "ui": {"visibility": ["app"]}, "anthropic/maxResultSizeChars": LIMIT}
    assert "outputSchema" not in tool and "ui/resourceUri" not in tool["_meta"]
    assert tool["annotations"]["readOnlyHint"] is True and tool["title"] == "Board View Geometry"
    assert tool["inputSchema"]["required"] == ["rev"]
    rev = _view()["view"]["geometry_rev"]
    direct = main_mod.get_board_view_geometry(rev, 0)
    result = _call("get_board_view_geometry", {"rev": rev, "offset": 0})
    assert result["content"] == [{"type": "text", "text": direct}]
    assert "structuredContent" not in result and not result.get("isError")


def test_get_board_view_through_the_protocol_is_the_direct_text(loaded):
    args = {"nets": "GND", "parts": "U3", "findings": [{"net": "CLK_A"}], "finding_tool": "check_testpoints"}
    direct = main_mod.get_board_view(**args)
    result = _call("get_board_view", args)
    assert result["content"] == [{"type": "text", "text": direct}]
    assert result["structuredContent"] == {"result": direct} and not result.get("isError")
    tool = _tools()["get_board_view"]
    view = uv.view_for("get_board_view")
    assert tool["_meta"] == {"anthropic/maxResultSizeChars": LIMIT, "ui": {"resourceUri": view.uri},
                             "ui/resourceUri": view.uri}
    assert tool["outputSchema"]["properties"] == {"result": {"title": "Result", "type": "string"}}
    assert tool["annotations"]["readOnlyHint"] is True and tool["title"] == "Board View"
    findings = tool["inputSchema"]["properties"]["findings"]
    assert findings["type"] == ["object", "array"] and findings["maxItems"] == 20
    assert tool["inputSchema"]["properties"]["side"]["enum"] == ["auto", "top", "bottom"]


# ---------- findings: one round trip per FINDING_SOURCES entry ----------

def _marks_at(marks: list[dict], x: float, y: float, units: str = "mil") -> float:
    scale = 1 / 0.0254 if units == "mm" else 1.0
    return min(math.dist((mark[f"at_{units}"][f"x_{units}"] * scale, mark[f"at_{units}"][f"y_{units}"] * scale),
                         (x, y)) for mark in marks)


def test_drc_findings_land_on_their_absolute_locations(monkeypatch, tmp_path):
    _need("SL1")
    shutil.copy(BOARDS["SL1"], tmp_path / BOARDS["SL1"].name)
    pcb = tmp_path / BOARDS["SL1"].name
    out = tmp_path / "Project Outputs for SL1"
    out.mkdir()
    report = out / f"Design Rule Check - {pcb.stem}.html"
    shutil.copy(ROOT / "tests" / "fixtures" / "drc" / "Design Rule Check - Demo.html", report)
    for item in (pcb, report):
        os.utime(item, (2_000_000_000, 2_000_000_000))
    index = _load(monkeypatch, _public("SL1"), pcb, netlist=None)
    monkeypatch.setitem(main_mod._project, "name", "SL1")
    monkeypatch.setitem(main_mod._project, "output_path", "")
    monkeypatch.setitem(main_mod._project, "log_folder_path", "")
    monkeypatch.setitem(main_mod._project, "prj_pcb_path", str(tmp_path / "SL1.PrjPcb"))
    drc = json.loads(main_mod.get_drc_report(group_by="rule", units="mil"))
    groups = [group for group in drc["groups"] if group.get("examples")][:6]
    located = [(example["location_mil"]["x_mil"], example["location_mil"]["y_mil"])
               for group in groups for example in group["examples"] if "location_mil" in example]
    assert located
    data = _view(findings=groups, finding_tool="get_drc_report", units="mil")
    marks = data["highlight"]["marks"]
    for x, y in located:
        assert _marks_at(marks, x, y) <= 1.0
    assert all(record["frame"] == "absolute" and "examples" not in record["ignored"]
               for record in data["highlight"]["findings"])
    named = {name for group in groups for example in group["examples"] for name in example.get("components", [])}
    found = {row["designator"] for row in data["highlight"]["parts"]}
    for name in named:
        assert (name in found) == bool(index.find_components(name))


def test_dfm_findings_land_on_their_origin_relative_locations(monkeypatch):
    _need("SL1")
    index = _load(monkeypatch, _public("SL1"), BOARDS["SL1"], netlist=None)
    origin = (index.pcb.board.origin_x, index.pcb.board.origin_y)
    dfm = json.loads(main_mod.check_dfm(units="mm"))
    findings = [finding for finding in dfm["findings"] if finding.get("examples")][:5]
    assert findings
    points = [(example["at_mm"][0] / 0.0254 + origin[0], example["at_mm"][1] / 0.0254 + origin[1])
              for finding in findings for example in finding["examples"] if "at_mm" in example]
    data = _view(findings=findings, finding_tool="check_dfm", units="mm")
    marks = data["highlight"]["marks"]
    for x, y in points[:50]:
        assert _marks_at(marks, x, y, "mm") <= 1.0
    assert not any("frame_hint" in mark for mark in marks)
    # object(s): 'LCD1 / R11', 'Pad U3-12 (GND) / Track (CLK)': the parts and pads named.
    pieces = [piece.split(" (")[0] for finding in findings for example in finding["examples"]
              for text in (example.get("objects"), example.get("object")) if text
              for piece in text.split(" / ")]
    pads = {piece[4:] for piece in pieces if piece.startswith("Pad ") and "-" in piece}
    parts = {piece for piece in pieces if " " not in piece and index.find_components(piece)}
    assert pads | parts
    assert pads <= {row["pad"] for row in data["highlight"]["pads"]}
    assert parts <= {row["designator"] for row in data["highlight"]["parts"]}


def test_testpoint_findings_land_on_their_origin_relative_locations(monkeypatch, tmp_path):
    from tests import test_testpoints as tt
    pcb = tt._pcb()
    pcb.board.origin_x, pcb.board.origin_y = 100.0, 200.0
    index = PcbIndex(pcb, None)
    path = tmp_path / "Board.PcbDoc"
    path.write_bytes(b"fake")
    _load(monkeypatch, index, path, netlist={"components": {}, "nets": {}})
    result = json.loads(main_mod.check_testpoints(units="mil"))
    findings = [finding for finding in result["findings"] if finding.get("examples")][:10]
    # An example is a probe, or two (first and second) for a spacing finding.
    probes = [probe for finding in findings for example in finding["examples"]
              for probe in ([example["first"], example["second"]] if "first" in example else [example])]
    points = [(probe["at_mil"][0] + 100.0, probe["at_mil"][1] + 200.0) for probe in probes]
    assert any("first" in example for finding in findings for example in finding["examples"])
    data = _view(findings=findings, finding_tool="check_testpoints", units="mil")
    for x, y in points:
        assert _marks_at(data["highlight"]["marks"], x, y) <= 1.0
    nets = {finding["net"] for finding in findings if finding.get("net")}
    assert nets <= {row["net"] for row in data["highlight"]["nets"]}
    pads = {probe["pad"] for probe in probes if "-" in probe.get("pad", "")}
    assert pads <= {row["pad"] for row in data["highlight"]["pads"]}
    parts = {finding["part"] for finding in findings if finding.get("part")}
    assert parts <= {row["designator"] for row in data["highlight"]["parts"]}


def test_decoupling_findings_name_parts_and_measure_as_the_tool(monkeypatch, tmp_path):
    from tests import test_decoupling as td
    netlist = td._netlist(td.BOARD_PARTS)
    path = tmp_path / "Board.PcbDoc"
    path.write_bytes(b"fake")
    _load(monkeypatch, PcbIndex(td._pcb(td._placements()), netlist), path, netlist=netlist)
    result = json.loads(main_mod.check_decoupling(units="mm"))
    (far,) = [finding for finding in result["findings"] if finding["check"] == "far_capacitor"]
    data = _view(findings=far, finding_tool="check_decoupling", units="mm")
    h = data["highlight"]
    assert {row["designator"] for row in h["parts"]} == {"U1", "C1"}
    assert [row["net"] for row in h["nets"]] == ["+3V3"]
    (measure,) = h["measures"]
    assert abs(measure["distance_mm"] - far["nearest"]["distance_mm"]) <= 0.1 * 0.0254
    assert measure["from_pad"].startswith("U1-") and measure["to_pad"].startswith("C1-")
    assert measure["basis"] == "straight line, pad edge to pad edge, as check_decoupling measures"
    assert h["findings"] == [{"finding": 1, "tool": "check_decoupling", "frame": "names only",
                              "read": ["designators", "ic", "nearest", "rail"], "ignored": [], "marks": 0}]
    # The whole pairs list (nearest of each) measures the same way.
    pairs = result["pairs"]
    data = _view(findings=pairs, finding_tool="check_decoupling", units="mm")
    assert abs(data["highlight"]["measures"][0]["distance_mm"] - pairs[0]["nearest"]["distance_mm"]) <= 0.0026


def test_tvs_findings_name_the_pads_and_measure_as_the_tool(monkeypatch, tmp_path):
    from tests import test_interfaces as ti
    netlist = ti._netlist(ti.PARTS)
    path = tmp_path / "Board.PcbDoc"
    path.write_bytes(b"fake")
    _load(monkeypatch, PcbIndex(ti._pcb(ti._placements(3000.0)), netlist), path, netlist=netlist)
    result = json.loads(main_mod.check_interfaces())
    (tvs,) = [finding for finding in result["findings"] if finding["check"] == "tvs_far_from_connector"]
    data = _view(findings=tvs, finding_tool="check_interfaces", units="mm")
    h = data["highlight"]
    assert {"J1", "D1"} <= {row["designator"] for row in h["parts"]}
    assert {row["pad"] for row in h["pads"]} == {f"J1-{pin}" for pin in tvs["pins"]}
    assert {row["net"] for row in h["pads"]} <= {row["net"] for row in h["nets"]}
    distances = [row["distance_mm"] for row in h["measures"]]
    assert len(distances) == len(tvs["pins"])
    assert min(abs(value - tvs["to_connector_mm"]) for value in distances) <= 0.1 * 0.0254
    assert all(row["to_pad"].startswith("J1-") and row["from_pad"].startswith("D1-") for row in h["measures"])


def test_decoupling_measures_from_the_power_pins_the_tool_measures_from(monkeypatch, tmp_path):
    # U1's EN pin is on the rail too, nearer C1 than its VDD pins: check_decoupling
    # measures from the power pins only (its pairs name them), and so does the view, for a
    # pairs entry (by its pins) and for a finding (which names none: by its own distance).
    from tests import test_decoupling as td
    parts = [(name, {**pins, "4": ("EN", "+3V3")} if name == "U1" else pins, info)
             for name, pins, info in td.BOARD_PARTS]
    placements = td._placements()
    placements[0] = (*placements[0][:4], {**placements[0][4], "4": ("+3V3", 300, 0)})
    netlist = td._netlist(parts)
    path = tmp_path / "Board.PcbDoc"
    path.write_bytes(b"fake")
    _load(monkeypatch, PcbIndex(td._pcb(placements), netlist), path, netlist=netlist)
    result = json.loads(main_mod.check_decoupling(units="mil"))
    (pair,) = result["pairs"]
    assert pair["pins"] == ["1", "2"] and pair["nearest"]["distance_mil"] == pytest.approx(380.0)
    (far,) = [finding for finding in result["findings"] if finding["check"] == "far_capacitor"]
    for finding in (pair, far):
        (measure,) = _view(findings=finding, finding_tool="check_decoupling", units="mil")["highlight"]["measures"]
        assert measure["from_pad"] == "U1-1" and measure["distance_mil"] == pytest.approx(380.0, abs=0.1)


def test_decoupling_pairs_of_a_public_example_measure_as_the_tool(monkeypatch):
    prj = EXAMPLES / "Bluetooth Sentinel" / "Bluetooth_Sentinel.PrjPcb"
    _need("Sentinel")
    netlist = main_mod.AltiumClient().compile_offline(str(prj))
    _load(monkeypatch, PcbIndex(parse_pcb_doc(str(BOARDS["Sentinel"])), netlist), BOARDS["Sentinel"],
          netlist=netlist)
    result = json.loads(main_mod.check_decoupling(units="mil"))
    measured = [entry for entry in result["pairs"] + result["findings"] if isinstance(entry.get("nearest"), dict)]
    assert len(measured) >= 5
    for entry in measured:
        measures = _view(findings=entry, finding_tool="check_decoupling", units="mil")["highlight"]["measures"]
        assert abs(measures[0]["distance_mil"] - entry["nearest"]["distance_mil"]) <= 0.1, entry["ic"]


def test_pads_named_with_a_dash_are_found_asked_or_in_dfm_objects(loaded):
    loaded.pcb.primitives.append(Primitive(kind="pad", layer=32, net=2, component=2, x1=320, y1=300,
                                           x2=320, y2=300, width=10, height=10, shape=1, pad_name="A-1"))
    loaded.__init__(loaded.pcb, NETLIST)
    h = _view(parts="R1-A-1, R1.A-1")["highlight"]
    assert [row["pad"] for row in h["pads"]] == ["R1-A-1"] and h["not_found"] == []
    h = _view(findings={"objects": "Pad R1-A-1 (CLK_B) / Track (GND)"}, finding_tool="check_dfm")["highlight"]
    assert [row["pad"] for row in h["pads"]] == ["R1-A-1"]
    assert {row["net"] for row in h["nets"]} == {"CLK_B", "GND"}
    h = _view(parts="R1-B-2")["highlight"]
    assert h["not_found"][0]["why"] == "R1 has no pad B-2"


def test_what_the_caps_leave_out_counts_and_is_named(loaded, monkeypatch):
    index = loaded
    index.pcb.components += [deepcopy(index.pcb.components[2]) for _ in range(60)]
    for number, component in enumerate(index.pcb.components[3:], start=3):
        component.index, component.refdes, component.source_uid_tail = number, f"R{number + 10}", None
    index.pcb.primitives += [Primitive(kind="pad", layer=32, net=0, component=2, x1=300 + n, y1=350, x2=300 + n,
                                       y2=350, width=1, height=1, shape=1, pad_name=f"P{n}") for n in range(60)]
    index.pcb.nets.extend(f"N{n}" for n in range(30))
    index.__init__(index.pcb, NETLIST)
    pads = ", ".join(f"R1-P{n}" for n in range(60))
    h = _view(parts=pads)["highlight"]
    assert len(h["pads"]) == 50 and h["pads_total"] == 60
    assert sum(row["why"].startswith("too many: at most 50 pads") for row in h["not_found"]) == 10
    # Names inside findings count the same way: DFM objects naming 60 parts and 25 nets.
    objects = [{"objects": " / ".join(f"R{n + 10}" for n in range(3 + 3 * k, 6 + 3 * k))} for k in range(20)]
    h = _view(findings=objects, finding_tool="check_dfm")["highlight"]
    assert len(h["parts"]) == 50 and h["parts_total"] == 60
    assert sum(row["kind"] == "part" and row["why"].startswith("too many") for row in h["not_found"]) == 10
    nets = [{"objects": f"Track (N{2 * k}) / Track (N{2 * k + 1})"} for k in range(13)]
    h = _view(findings=nets, finding_tool="check_dfm")["highlight"]
    assert len(h["nets"]) == 20 and h["nets_total"] == 26
    assert h["not_found"][0]["why"] == "too many: at most 20 nets are highlighted, and it is not"


def test_placeholders_are_skipped_and_an_empty_finding_says_so(loaded):
    # get_drc_report's '(no component)' and '(no net)' groups name nothing to look for.
    finding = {"components": {"(no component)": 3, "U3": 1}, "nets": ["(no net)", "GND"]}
    h = _view(findings=finding, finding_tool="get_drc_report")["highlight"]
    assert h["not_found"] == [] and [row["net"] for row in h["nets"]] == ["GND"]
    # A whole result passed as a finding: nothing read, and the answer says so.
    h = _view(findings={"findings": [{"check": "spacing"}], "summary": {}}, finding_tool="check_dfm")["highlight"]
    assert h["findings"][0]["read"] == [] and h["findings"][0]["marks"] == 0
    assert h["not_found"] == [{"kind": "finding", "asked": "finding 1", "from": "finding 1",
                               "why": "no names or locations were read from it: it looks like a whole "
                                      "result; pass its findings (or pairs) themselves"}]


def test_an_unknown_tools_coordinates_are_named_not_placed(loaded):
    finding = {"designators": ["U3"], "net": "GND", "at_mil": [10, 20],
               "location_mil": {"x_mil": 1.0, "y_mil": 2.0}, "gap_range_mil": [1, 2]}
    data = _view(findings=finding, finding_tool="check_vias")
    h = data["highlight"]
    assert h["marks"] == [] and {row["designator"] for row in h["parts"]} == {"U3"}
    assert [row["net"] for row in h["nets"]] == ["GND"]
    (record,) = h["findings"]
    assert record["frame"] == "names only" and record["ignored"] == ["at_mil", "location_mil"]
    assert record["read"] == ["designators", "net"]
    assert h["not_found"] == [{"kind": "location", "asked": "at_mil, location_mil",
                               "why": "check_vias's coordinates are not placed yet: pass x and y (absolute)",
                               "from": "finding 1"}]
    h = _view(findings=finding)["highlight"]
    assert h["not_found"][0]["why"] == "name finding_tool so its coordinates can be placed"


def test_every_tool_that_reports_board_locations_has_a_frame():
    sources = set(bv.FINDING_SOURCES)
    deferred = set(bv.DEFERRED_SOURCES)
    assert not sources & deferred
    tools = {tool.name: tool for tool in asyncio.run(main_mod.mcp.list_tools())}
    pattern = re.compile(r"relative to (?:the board )?origin|from the board origin|\bis absolute\b")
    # get_board_view takes locations (it is the reader); the others report them.
    mentions = {name for name, tool in tools.items() if name != "get_board_view"
                and pattern.search(" ".join((tool.description or "").split()))}
    assert {"get_drc_report", "check_dfm", "check_testpoints", "check_pcb_health"} <= mentions
    assert mentions <= sources | deferred, mentions - sources - deferred
    assert (sources | deferred) <= set(tools)
    assert bv.FINDING_SOURCES["get_drc_report"].frame == "absolute"
    assert {name for name, spec in bv.FINDING_SOURCES.items() if spec.frame == "board origin"} == \
        {"check_dfm", "check_testpoints"}
    # The frames as the tools' own code writes them: the DRC linker adds the origin, the
    # DFM and testpoint reviews subtract it.
    services = ROOT / "server" / "services"
    assert "+ self.origin[0]" in (services / "drc_results.py").read_text(encoding="utf-8")
    for module in ("dfm.py", "testpoints.py", "pcb_health.py", "mechanical_fit.py"):
        assert re.search(r"x - self\.origin\[0\]", (services / module).read_text(encoding="utf-8")), module


# ---------- the existing PCB tools are unchanged ----------

@pytest.mark.parametrize("name, args", [
    ("get_board_info", {}), ("get_net_pcb", {"net": "GND"}),
    ("query_pcb_region", {"x": 5000.0, "y": 7000.0, "units": "mil"}),
    ("get_component_placement", {"refdes": "U1"}),
])
def test_existing_pcb_tools_give_the_same_text_through_the_protocol(monkeypatch, name, args):
    _need("SL1")
    _load(monkeypatch, _public("SL1"), BOARDS["SL1"], netlist=None)
    direct = getattr(main_mod, name)(**args)
    result = _call(name, args)
    assert result["content"] == [{"type": "text", "text": direct}]
    assert result["structuredContent"] == {"result": direct}
    assert "error" not in json.loads(direct)
