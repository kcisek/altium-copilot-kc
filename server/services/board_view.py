"""Board view (roadmap #63): the locator get_board_view answers with, and the compact
board geometry the MCP Apps view (server/ui/board.html) draws.

Two tools in server/main.py use this module:
- get_board_view (model-visible, linked to the "board" view) says where nets, parts, pads,
  spots and the objects findings name lie: a small JSON locator text, the same for every
  host. It never builds the geometry; geometry_rev names the board it answered on.
- get_board_view_geometry (app-only) gives the view the geometry in pages of at most
  PAGE_CHARS characters of compact JSON. The pages are built on the first call for a
  loaded board and kept on its PcbIndex (index._board_view), so a reload drops them.

Payload format 1 (geometry: 1):
- Every coordinate and length is an integer in 0.1 mil (q = 10 per mil) from base_mil, the
  floor of the smallest x and y exported, so no coordinate is negative; the view reads
  x_mil = base_x + v / 10. Rotations are integers in 0.01 degree, arc start angles and
  sweeps in 0.001 degree (counter-clockwise, 360000 a whole circle). -1 is no net or no part.
- Page 0 holds the header (base, origin, layers, the page plan, counts, notes), the
  per-call variant and Not Fitted part indices, then chunks. Later pages hold chunks
  only. The header has a bounded size: the outline, the cutouts and the schematic
  designators come as chunks, so a long outline or many cutouts spill onto later pages.
- A chunk is {k: kind, l: v6 layer or null, first (nets, stacks, via types, parts), s:
  stride (flat kinds), rows}. Flat kinds list s values per row in one array; the others
  hold one array per row. A ring is [x0, y0, dx1, dy1, ...] and closes on its first
  point; a long ring (outline, cutout, area, pour) comes in pieces, each later piece
  starting at the previous piece's last point.
- Pages are filled in order: the board's outline and cutouts, nets, pad stacks, via
  types, schematic designators and parts; pads and vias; per copper layer (top, bottom,
  inner in stack order, planes) routing tracks chained into polylines, arcs, free areas
  and pour outlines; silkscreen; then poured copper (solid pours' islands and hatched
  pours' tracks and arcs), each layer on pages of its own. The poured copper is read
  automatically only when the whole board fits AUTO_PAGES pages; otherwise the view
  loads it per layer when asked (plan[].auto says which). At most SESSION_PAGES pages are
  sent (the view reads no more for one answer): what lies beyond is named in a note.

The findings reader (FINDING_SOURCES) places the objects a finding names: the frame of
each tool's coordinates was checked in its code (DRC locations are absolute, as the DRC
linker adds the board origin; check_dfm and check_testpoints give example locations
relative to the origin), and the names it reads. DEFERRED_SOURCES records the frame of
the other tools that report coordinates, whose coordinates are not placed yet.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import math
import re
from collections import defaultdict
from dataclasses import dataclass, field

try:
    from parsers.pcb_doc import read_board_cutouts, shape_points
    from services.bom import natural_key
    from services.pcb_geometry import point_in_polygon
except ImportError:
    from server.parsers.pcb_doc import read_board_cutouts, shape_points
    from server.services.bom import natural_key
    from server.services.pcb_geometry import point_in_polygon

FORMAT = 1
Q = 10  # integer units per mil
PAGE_CHARS = 55_000  # a page's text at most (the tools' result cap is 60,000)
AUTO_PAGES = 16  # the pages the view reads without being asked
SESSION_PAGES = 128  # the pages the view reads in all for one result
NEW_PAGE_BELOW = 2_000  # a chunk starts on a new page when less room than this is left
PAGE_HEAD = 160  # room kept for a later page's header
MAX_CHAIN_POINTS = 1_500  # a track polyline's points per row (a longer one continues)
MAX_RING_POINTS = 2_000  # an area ring's points per piece
SEPARATORS = (",", ":")

TOP, BOTTOM, TOP_OVERLAY, BOTTOM_OVERLAY, MULTI_LAYER = 1, 32, 33, 34, 74
OVERLAYS = (TOP_OVERLAY, BOTTOM_OVERLAY)
STANDARD_NAMES = {TOP_OVERLAY: "Top Overlay", BOTTOM_OVERLAY: "Bottom Overlay",
                  MULTI_LAYER: "Multi-Layer"}

# Chunk kinds: their stride (flat kinds) or None (one array per row).
STRIDES = {
    "board": None, "nets": None, "stacks": 8, "viatypes": 4, "sch": 2, "parts": 9, "pads": 6,
    "vias": 4,
    "tracks": None, "arcs": 7, "areas": None, "pours": None, "silk": None, "silkarcs": 6,
    "poured_areas": None, "poured_tracks": None, "poured_arcs": 7,
}
POURED_KINDS = ("poured_areas", "poured_tracks", "poured_arcs")
HATCH = {"solid": 0, "none": 2}  # any other hatch style is hatched (1)

POURED_COPPER_NOTE = ("drawn when the whole board fits 16 pages, else pour outlines, with "
                      "poured copper loaded per layer on request")
NOT_DRAWN = [
    "free texts (designators are drawn at part centres)",
    "mechanical layers, courtyards, 3D bodies",
    "solder mask, paste, keepouts",
    "internal plane copper (plane primitives and pours only)",
    "per-layer pad stacks (main copper on every layer)",
    "embedded boards of a panel",
    "poured copper past 16 pages, until loaded per layer",
    "pour copper newer than the last save (repour)",
]
FRAME = ("x/y are absolute (the frame query_pcb_region takes); from_origin values are "
         "relative to the board origin, as Altium's status bar shows them")
VIEW_NOTE = ("Hosts that show MCP Apps draw the board next to this result; the view reads "
             "the geometry itself. Elsewhere this text is the answer.")


def is_copper(layer: int | None) -> bool:
    """A signal layer (1-32) or an internal plane (39-54)."""
    return layer is not None and (1 <= layer <= 32 or 39 <= layer <= 54)


def is_plane(layer: int | None) -> bool:
    return layer is not None and 39 <= layer <= 54


def geometry_rev(path: str, signature: tuple[int, int]) -> str:
    """The loaded board's geometry revision: the payload format, the file and its saved
    time and size, hashed (it names nothing)."""
    mtime_ns, size = signature
    return hashlib.sha1(f"{FORMAT}|{path}|{mtime_ns}|{size}".encode("utf-8")).hexdigest()[:12]


def _dumps(value) -> str:
    return json.dumps(value, separators=SEPARATORS, ensure_ascii=False)


def counted(count: int, singular: str, plural: str | None = None) -> str:
    """'1 pad', '3 pads' (plural given when it is not singular + 's')."""
    return f"{count} {singular if count == 1 else plural or singular + 's'}"


# A JSON string (no raw newline inside), number, true, false or null.
_SCALAR = r'(?:"(?:[^"\\\n]|\\.)*"|-?\d[\d.eE+-]*|true|false|null)'
_SCALAR_LIST = re.compile(r"\[\n\s+(" + _SCALAR + r"(?:,\n\s+" + _SCALAR + r")*)\n\s*\]")


def dumps_text(value) -> str:
    """The locator text: JSON indented by 2, a list of names or numbers on one line."""
    text = json.dumps(value, indent=2)
    return _SCALAR_LIST.sub(lambda match: "[" + re.sub(r",\n\s+", ", ", match.group(1)) + "]",
                            text)


# ---------- the geometry export ----------

@dataclass
class Export:
    """A board's pages: page 0's header (without the per-call variant and dnp) and
    chunks, the later pages as their text, the plan and each part's designator for
    is_dnp (its schematic designator, else the PCB one)."""

    rev: str
    head: dict
    first_chunks: list[str]
    pages: list[str | None]
    plan: list[dict]
    dnp_names: list[str]
    reserve: int

    @property
    def page_count(self) -> int:
        return len(self.pages)

    def page(self, offset: int, variant: str | None = None, dnp: list[int] | None = None) -> str:
        """Page offset as text; page 0 with the call's variant and Not Fitted parts."""
        if offset:
            return self.pages[offset]
        head = {**self.head, "variant": variant, "dnp": list(dnp or [])}
        chunks = ',"chunks":[' + ",".join(self.first_chunks) + "]}"
        text = _dumps(head)
        over = len(text) - 1 + len(chunks) - PAGE_CHARS
        if over > 0 and isinstance(variant, str) and variant:
            # Only a variant name longer than the reserve can pass it: it is shortened.
            head["variant"] = variant[:max(1, len(variant) - over - 2)] + "…"
            text = _dumps(head)
        return text[:-1] + chunks


@dataclass
class _Section:
    kind: str
    layer: int | None
    rows: list[str]
    first: int | None = None  # the index of the first row (nets, parts)
    new_page: bool = False  # starts on a page of its own (a poured layer)
    poured: bool = False


class _Quant:
    """Board coordinates (mil) to the integers sent: 0.1 mil from base_mil."""

    def __init__(self, base_x: int, base_y: int):
        self.bx, self.by = base_x, base_y

    def point(self, x: float, y: float) -> tuple[int, int]:
        return int(round((x - self.bx) * Q)), int(round((y - self.by) * Q))


def length(value: float | None) -> int:
    return int(round((value or 0.0) * Q))


def rotation(value: float | None) -> int:
    if value is None or not math.isfinite(value):
        return 0
    return int(round(value * 100)) % 36000


def ring(points: list[tuple[int, int]]) -> list[int]:
    """[x0, y0, dx1, dy1, ...] of quantized points."""
    if not points:
        return []
    out = [points[0][0], points[0][1]]
    px, py = points[0]
    for x, y in points[1:]:
        out.append(x - px)
        out.append(y - py)
        px, py = x, y
    return out


def _pieces(points: list, size: int) -> list[list]:
    """Points cut into runs of at most size points, each run after the first starting
    at the previous run's last point."""
    if len(points) <= size:
        return [points]
    out = []
    start = 0
    while start < len(points) - 1:
        out.append(points[start:start + size])
        start += size - 1
    return out


def chains(segments: list[tuple[int, int, int, int]]) -> list[list[tuple[int, int]]]:
    """Segments joined into polylines both ways through exactly shared ends (every
    segment once; a point where several meet ends some polylines)."""
    ends: dict[tuple[int, int], list[int]] = defaultdict(list)
    for number, (a, b, c, d) in enumerate(segments):
        ends[(a, b)].append(number)
        ends[(c, d)].append(number)
    used = bytearray(len(segments))

    def grow(point, out):
        while True:
            waiting = ends.get(point)
            while waiting and used[waiting[-1]]:
                waiting.pop()
            if not waiting:
                return
            number = waiting.pop()
            used[number] = 1
            a, b, c, d = segments[number]
            point = (c, d) if (a, b) == point else (a, b)
            out.append(point)

    lines = []
    for number, (a, b, c, d) in enumerate(segments):
        if used[number]:
            continue
        used[number] = 1
        forward = [(c, d)]
        grow((c, d), forward)
        backward: list[tuple[int, int]] = []
        grow((a, b), backward)
        lines.append(list(reversed(backward)) + [(a, b)] + forward)
    return lines


def _arc_row(chords: list, quant: _Quant) -> tuple[list[int], tuple[float, float, float]]:
    """[cx, cy, r, a0, sweep] of an arc rebuilt from its chord rows (centre and radius
    from the rows, the start from the first chord, the sweep summed over the chords)."""
    first = chords[0]
    cx, cy, radius = first.arc_x, first.arc_y, first.arc_radius
    if cx is None or cy is None or not radius:
        # A row without its arc (a hand-built one): the chord's own circle is unknown.
        cx, cy = (first.x1 + chords[-1].x2) / 2.0, (first.y1 + chords[-1].y2) / 2.0
        radius = max(math.dist((first.x1, first.y1), (chords[-1].x2, chords[-1].y2)) / 2.0, 1e-6)
    sweep = 0.0
    for chord in chords:
        a = math.degrees(math.atan2(chord.y1 - cy, chord.x1 - cx))
        b = math.degrees(math.atan2(chord.y2 - cy, chord.x2 - cx))
        sweep += (b - a) % 360.0
    # The centre and radius on the 0.1 mil grid that keep both ends nearest the arc's own
    # (a rounded centre alone can move an end by 0.12 mil), the angles taken from it.
    begin, finish = (first.x1, first.y1), (chords[-1].x2, chords[-1].y2)
    x0, y0 = quant.point(cx, cy)
    best = None
    for x in (value for value in (x0 - 1, x0, x0 + 1) if value >= 0):
        for y in (value for value in (y0 - 1, y0, y0 + 1) if value >= 0):
            centre = (quant.bx + x / Q, quant.by + y / Q)
            to_begin, to_finish = math.dist(centre, begin), math.dist(centre, finish)
            for r in {math.floor(radius * Q), math.ceil(radius * Q)}:
                error = max(abs(to_begin - r / Q), abs(to_finish - r / Q), abs(radius - r / Q))
                if best is None or error < best[0]:
                    best = (error, x, y, r, centre)
    _, x, y, r, centre = best
    start = math.degrees(math.atan2(begin[1] - centre[1], begin[0] - centre[0]))
    end = math.degrees(math.atan2(finish[1] - centre[1], finish[0] - centre[0]))
    sweep += ((end - start) - sweep + 180.0) % 360.0 - 180.0
    start_q = int(round(start * 1000)) % 360000
    sweep_q = min(360000, max(1, int(round(sweep * 1000))))
    return [x, y, max(r, 1), start_q, sweep_q], (cx, cy, radius)


def _arcs(primitives) -> list[list]:
    """Arc chord rows grouped per arc (consecutive rows of one source record)."""
    groups: list[list] = []
    key = None
    for primitive in primitives:
        here = (primitive.source_id, primitive.layer, primitive.arc_x, primitive.arc_y,
                primitive.arc_radius, primitive.net)
        if primitive.source_id is None or here != key or not groups:
            groups.append([primitive])
        else:
            groups[-1].append(primitive)
        key = here
    return groups


def _hatch(style: str | None) -> int:
    return HATCH.get((style or "solid").strip().lower(), 1)


def board_layers(index) -> dict:
    """The board's layers for the view: copper in stack order (then any other copper
    layer that holds primitives), the overlays and Multi-Layer, each {id, name, kind,
    order}."""
    copper = [layer for layer in index._copper_layers_in_stack_order() if is_copper(layer)]
    used = {row[0] for row in index.db.execute("SELECT DISTINCT layer FROM prims")}
    used |= {area.layer for area in index.pcb.areas}
    used |= {pour.layer for pour in index.pcb.pours if pour.layer is not None}
    for layer in sorted(used):
        if is_copper(layer) and layer not in copper:
            copper.append(layer)
    out = []

    def name(layer: int) -> str:
        found = index.layer_name(layer)
        return STANDARD_NAMES.get(layer, found) if found == f"layer{layer}" else found

    known = {row[0] for row in index.db.execute("SELECT v6_id FROM layers")}
    if TOP_OVERLAY in used or TOP_OVERLAY in known:
        out.append({"id": TOP_OVERLAY, "name": name(TOP_OVERLAY), "kind": "overlay", "order": -1})
    for order, layer in enumerate(copper):
        kind = ("top" if layer == TOP else "bottom" if layer == BOTTOM
                else "plane" if is_plane(layer) else "inner")
        out.append({"id": layer, "name": name(layer), "kind": kind, "order": order})
    if BOTTOM_OVERLAY in used or BOTTOM_OVERLAY in known:
        out.append({"id": BOTTOM_OVERLAY, "name": name(BOTTOM_OVERLAY), "kind": "overlay",
                    "order": len(copper)})
    out.append({"id": MULTI_LAYER, "name": name(MULTI_LAYER), "kind": "multi", "order": None})
    return {"layers": out, "copper": copper}


def copper_layer_names(index) -> list[str]:
    """The copper layers' names, in stack order."""
    layers = board_layers(index)
    by_id = {layer["id"]: layer["name"] for layer in layers["layers"]}
    return [by_id[layer] for layer in layers["copper"]]


def _base(index, outline, cutouts) -> tuple[int, int]:
    db = index.db
    xs: list[float] = []
    ys: list[float] = []
    for row in (
        db.execute("SELECT MIN(minx), MIN(miny) FROM prims").fetchone(),
        db.execute("SELECT MIN(arc_x), MIN(arc_y) FROM prims WHERE kind = 'arc'").fetchone(),
        db.execute("SELECT MIN(x), MIN(y) FROM components").fetchone(),
        db.execute("SELECT MIN(minx), MIN(miny) FROM components").fetchone(),
    ):
        if row[0] is not None:
            xs.append(row[0])
        if row[1] is not None:
            ys.append(row[1])
    for points in [outline, *cutouts, *(pour.vertices for pour in index.pcb.pours),
                   *(area.outline for area in index.pcb.areas)]:
        if points:
            xs.append(min(point[0] for point in points))
            ys.append(min(point[1] for point in points))
    return (math.floor(min(xs)) if xs else 0), (math.floor(min(ys)) if ys else 0)


def _flat(values) -> str:
    return ",".join(map(str, values))


def _array(values) -> str:
    return "[" + ",".join(map(str, values)) + "]"


def build_export(index, rev: str) -> Export:
    """Every page of the board's geometry (payload format 1)."""
    pcb = index.pcb
    db = index.db
    notes: list[str] = []
    outline_points = list(pcb.board.outline_vertices or [])
    cutouts, unread = read_board_cutouts(pcb)
    cutout_points = [shape_points(region.vertices) for region in cutouts
                     if len(region.vertices) >= 3]
    if unread:
        notes.append(f"{counted(unread, 'region record')} could not be read: board cutouts "
                     "among them are not drawn.")
    base_x, base_y = _base(index, outline_points, cutout_points)
    quant = _Quant(base_x, base_y)
    layers = board_layers(index)
    copper = layers["copper"]
    counts: dict[str, int] = defaultdict(int)

    # Parts (dense by component index), their schematic designators where they differ.
    rows = {row["id"]: row for row in db.execute("SELECT * FROM components")}
    part_rows: list[str] = []
    sch_rows: list[str] = []
    dnp_names: list[str] = []
    for number in range(max(rows, default=-1) + 1):
        row = rows.get(number)
        if row is None:
            part_rows.append(_flat([_dumps(""), 0, 0, 0, 2, 0, 0, 0, 0]))
            dnp_names.append("")
            continue
        designator = row["refdes"] or ""
        placed = row["x"] is not None and row["y"] is not None
        side = {"top": 0, "bottom": 1}.get(row["side"], 2) if placed else 2
        x, y = quant.point(row["x"], row["y"]) if placed else (0, 0)
        if row["minx"] is not None and row["miny"] is not None:
            box = [*quant.point(row["minx"], row["miny"]), *quant.point(row["maxx"], row["maxy"])]
        else:
            box = [x, y, x, y]
        part_rows.append(_flat([_dumps(designator), x, y, rotation(row["rotation"]), side, *box]))
        if row["sch_refdes"] and row["sch_refdes"] != designator:
            sch_rows.append(_flat([number, _dumps(row["sch_refdes"])]))
        dnp_names.append(row["sch_refdes"] or designator)
    counts["parts"] = len(rows)

    stacks: dict[tuple, int] = {}
    viatypes: dict[tuple, int] = {}
    pads: dict[int, list[str]] = defaultdict(list)
    vias: list[str] = []
    tracks: dict[int, dict[tuple, list]] = defaultdict(lambda: defaultdict(list))
    silk: dict[int, dict[int, list]] = defaultdict(lambda: defaultdict(list))
    poured_tracks: dict[int, dict[tuple, list]] = defaultdict(lambda: defaultdict(list))
    arcs: dict[int, list[str]] = defaultdict(list)
    silk_arcs: dict[int, list[str]] = defaultdict(list)
    poured_arcs: dict[int, list[str]] = defaultdict(list)
    arc_chords: list = []
    off_copper_pads = 0
    pin_text: dict[str, str] = {}
    for primitive in pcb.primitives:
        kind = primitive.kind
        layer = primitive.layer
        net = -1 if primitive.net is None else primitive.net
        if kind == "pad":
            if layer != MULTI_LAYER and not is_copper(layer):
                off_copper_pads += 1
                continue
            hole = length(primitive.hole) if primitive.hole and primitive.hole > 0 else 0
            key = (primitive.shape or 1, length(primitive.width),
                   length(primitive.height if primitive.height is not None else primitive.width),
                   rotation(primitive.rotation), length(primitive.corner_radius),
                   hole, (primitive.hole_shape or 0) if hole else 0,
                   length(primitive.slot_length) if hole else 0)
            stack = stacks.setdefault(key, len(stacks))
            name = primitive.pad_name or ""
            if name not in pin_text:
                pin_text[name] = _dumps(name)
            x, y = quant.point(primitive.x1, primitive.y1)
            part = -1 if primitive.component is None else primitive.component
            pads[layer].append(f"{x},{y},{stack},{net},{part},{pin_text[name]}")
            counts["pads"] += 1
        elif kind == "via":
            key = (length(primitive.width), length(primitive.hole), layer,
                   primitive.layer_to if primitive.layer_to is not None else layer)
            kind_of = viatypes.setdefault(key, len(viatypes))
            x, y = quant.point(primitive.x1, primitive.y1)
            vias.append(f"{x},{y},{kind_of},{net}")
            counts["vias"] += 1
        elif kind == "track":
            if primitive.pour_id is not None:
                continue  # a pour's outline edge: the pours chunk draws it
            segment = (*quant.point(primitive.x1, primitive.y1),
                       *quant.point(primitive.x2, primitive.y2))
            width = length(primitive.width)
            if layer in OVERLAYS:
                silk[layer][width].append(segment)
                counts["silk"] += 1
            elif is_copper(layer):
                target = tracks if primitive.poured_by is None else poured_tracks
                target[layer][(net, width)].append(segment)
                counts["tracks" if primitive.poured_by is None else "poured_tracks"] += 1
        elif kind == "arc" and (layer in OVERLAYS or is_copper(layer)):
            arc_chords.append(primitive)
    for chords in _arcs(arc_chords):
        first = chords[0]
        values, _ = _arc_row(chords, quant)
        width = length(first.width)
        net = -1 if first.net is None else first.net
        if first.layer in OVERLAYS:
            silk_arcs[first.layer].append(_flat([*values, width]))
            counts["silkarcs"] += 1
        elif first.poured_by is None:
            arcs[first.layer].append(_flat([*values, width, net]))
            counts["arcs"] += 1
        else:
            poured_arcs[first.layer].append(_flat([*values, width, net]))
            counts["poured_arcs"] += 1
    if off_copper_pads:
        notes.append(f"{counted(off_copper_pads, 'pad')} on layers other than copper "
                     f"{'is' if off_copper_pads == 1 else 'are'} not drawn.")

    def polylines(groups: dict, with_net: bool) -> list[str]:
        out = []
        for key in sorted(groups):
            prefix = [key[1], key[0]] if with_net else [key]
            for line in chains(groups[key]):
                for piece in _pieces(line, MAX_CHAIN_POINTS):
                    out.append(_array([*prefix, *ring(piece)]))
        return out

    def ring_rows(head: list, points) -> list[str]:
        """A ring's rows: head, the piece number and count, then the piece's points."""
        pieces = _pieces([quant.point(x, y) for x, y in points], MAX_RING_POINTS)
        return [_array([*head, piece_number, len(pieces), *ring(piece)])
                for piece_number, piece in enumerate(pieces)]

    def area_rows(number: int, area, part: int) -> list[str]:
        rings = [area.outline, *area.holes]
        net = -1 if area.net is None else area.net
        return [row for ring_number, points in enumerate(rings)
                for row in ring_rows([number, net, part, len(rings), ring_number], points)]

    free_areas: dict[int, list[str]] = defaultdict(list)
    poured_areas: dict[int, list[str]] = defaultdict(list)
    free_number = poured_number = 0
    off_copper_areas = 0
    for area in pcb.areas:
        if not is_copper(area.layer) or len(area.outline) < 3:
            off_copper_areas += 1
            continue
        if area.pour_id is None:
            component = -1 if area.component is None else area.component
            free_areas[area.layer].extend(area_rows(free_number, area, component))
            free_number += 1
        else:
            poured_areas[area.layer].extend(area_rows(poured_number, area, area.pour_id))
            poured_number += 1
    counts["areas"] = free_number
    counts["poured_areas"] = poured_number
    if off_copper_areas:
        notes.append(f"{counted(off_copper_areas, 'copper area')} off the copper layers "
                     f"{'is' if off_copper_areas == 1 else 'are'} not drawn.")

    pours: dict[int, list[str]] = defaultdict(list)
    skipped_pours = 0
    for pour in pcb.pours:
        if pour.shelved:
            continue
        if not is_copper(pour.layer) or len(pour.vertices) < 3:
            skipped_pours += 1
            continue
        net = -1 if pour.net is None else pour.net
        pours[pour.layer].extend(
            ring_rows([pour.pour_id, net, _hatch(pour.hatch_style)], pour.vertices))
        counts["pours"] += 1
    if skipped_pours:
        notes.append(f"{counted(skipped_pours, 'pour')} on no copper layer or without an "
                     "outline " + ("is" if skipped_pours == 1 else "are") + " not drawn.")

    counts["nets"] = len(pcb.nets)
    counts["stacks"] = len(stacks)
    counts["viatypes"] = len(viatypes)

    # The board: its outline (role 0) and cutouts (role 1, numbered), as rings in pieces.
    board_rows = ring_rows([0, 0], outline_points) if len(outline_points) >= 3 else []
    for number, points in enumerate(cutout_points):
        board_rows += ring_rows([1, number], points)
    counts["cutouts"] = len(cutout_points)
    sections: list[_Section] = [
        _Section("board", None, board_rows),
        _Section("nets", None, [_dumps(name) for name in pcb.nets], first=0),
        _Section("stacks", None, [_flat(key) for key in sorted(stacks, key=stacks.get)], first=0),
        _Section("viatypes", None, [_flat(key) for key in sorted(viatypes, key=viatypes.get)],
                 first=0),
        _Section("sch", None, sch_rows),
        _Section("parts", None, part_rows, first=0),
    ]
    pad_layers = sorted(pads, key=lambda layer: (layer != MULTI_LAYER, layer != TOP,
                                                 layer != BOTTOM, layer))
    sections += [_Section("pads", layer, pads[layer]) for layer in pad_layers]
    sections.append(_Section("vias", None, vias))
    view_order = [layer for layer in copper if layer == TOP] + \
        [layer for layer in copper if layer == BOTTOM] + \
        [layer for layer in copper if layer not in (TOP, BOTTOM) and not is_plane(layer)] + \
        [layer for layer in copper if is_plane(layer)]
    for layer in view_order:
        sections += [
            _Section("tracks", layer, polylines(tracks.get(layer, {}), True)),
            _Section("arcs", layer, arcs.get(layer, [])),
            _Section("areas", layer, free_areas.get(layer, [])),
            _Section("pours", layer, pours.get(layer, [])),
        ]
    for layer in OVERLAYS:
        sections += [_Section("silk", layer, polylines(silk.get(layer, {}), False)),
                     _Section("silkarcs", layer, silk_arcs.get(layer, []))]
    for layer in view_order:
        group = [
            _Section("poured_areas", layer, poured_areas.get(layer, []), poured=True),
            _Section("poured_tracks", layer, polylines(poured_tracks.get(layer, {}), True),
                     poured=True),
            _Section("poured_arcs", layer, poured_arcs.get(layer, []), poured=True),
        ]
        group = [section for section in group if section.rows]
        if group:
            group[0].new_page = True
            sections += group
    sections = [section for section in sections if section.rows]

    origin = [round(pcb.board.origin_x or 0.0, 4), round(pcb.board.origin_y or 0.0, 4)]
    if len(outline_points) < 3:
        notes.append("The board has no outline: its copper extents stand in for it.")
    if pcb.embedded_boards:
        notes.append(f"{counted(pcb.embedded_boards, 'embedded board array')} of a panel: "
                     "their boards are not drawn.")
    head = {
        "geometry": FORMAT, "rev": rev, "page": 0, "pages": 0, "auto_pages": 0,
        "has_more": False, "next_offset": None, "q": Q, "base_mil": [base_x, base_y],
        "origin_mil": origin, "layers": layers["layers"], "plan": [], "counts": dict(counts),
        "variant": None, "dnp": [], "notes": notes + NOT_DRAWN,
        "embedded_boards": pcb.embedded_boards,
    }
    reserve = 128 + 6 * len(part_rows)
    return _paginate(rev, head, sections, dnp_names, reserve)


def _chunk_prefix(section: _Section, first: int | None) -> str:
    text = f'{{"k":"{section.kind}","l":{"null" if section.layer is None else section.layer}'
    if first is not None:
        text += f',"first":{first}'
    stride = STRIDES[section.kind]
    if stride:
        text += f',"s":{stride}'
    return text + ',"rows":['


def _pack(sections: list[_Section], first_room: int,
          split_poured: bool) -> tuple[list[list[tuple]], int]:
    """Chunks per page: [(kind, layer, poured, text)] each, filled in order. A chunk is
    split at a row boundary when it does not fit the room left; a row too long for an
    empty page is left out (counted). With split_poured each poured layer starts a page."""
    pages: list[list[tuple]] = [[]]
    used = [0]
    dropped = 0

    def room() -> int:
        limit = first_room if len(pages) == 1 else PAGE_CHARS - PAGE_HEAD
        return limit - used[-1]

    def new_page():
        pages.append([])
        used.append(0)

    for section in sections:
        if split_poured and section.new_page and pages[-1]:
            new_page()
        rows = section.rows
        start = 0
        while start < len(rows):
            if pages[-1] and room() < NEW_PAGE_BELOW:
                new_page()
            first = None if section.first is None else section.first + start
            prefix = _chunk_prefix(section, first)
            available = room() - len(prefix) - 3
            end = start
            size = 0
            while end < len(rows) and size + len(rows[end]) + (end > start) <= available:
                size += len(rows[end]) + (end > start)
                end += 1
            if end == start:
                if pages[-1] or len(pages) == 1:  # page 0 holds the header: a fresh page
                    new_page()
                    continue
                dropped += 1
                start += 1
                continue
            text = prefix + ",".join(rows[start:end]) + "]}"
            pages[-1].append((section.kind, section.layer, section.poured, text))
            used[-1] += len(text) + 1
            start = end
    return pages, dropped


def _paginate(rev: str, head: dict, sections: list[_Section], dnp_names: list[str],
              reserve: int) -> Export:
    total = sum(len(row) + 1 for section in sections for row in section.rows)
    plan_room = 200 + 120 * (total // (PAGE_CHARS - PAGE_HEAD - NEW_PAGE_BELOW) + 2)
    for _ in range(8):
        # Room kept on page 0 beyond its header as built: the plan, the notes added
        # below (the paging note and dropped rows) and the numbers filled in (pages,
        # auto_pages, chars), and the per-call variant and Not Fitted parts (reserve).
        head_size = len(_dumps(head)) + len(',"chunks":[]}')
        first_room = PAGE_CHARS - head_size - plan_room - reserve - 1200
        # Everything in order when the board fits the automatic pages; else again with
        # each poured layer on pages of its own, read when asked.
        pages, dropped = _pack(sections, first_room, split_poured=False)
        auto_all = len(pages) <= AUTO_PAGES
        if not auto_all:
            pages, dropped = _pack(sections, first_room, split_poured=True)
        # The view reads at most SESSION_PAGES pages for one answer: the rest is not sent.
        cut = (max(0, len(pages) - SESSION_PAGES),
               {(kind, layer) for page in pages[SESSION_PAGES:] for kind, layer, _, _ in page})
        pages = pages[:SESSION_PAGES]
        count = len(pages)
        texts: list[str | None] = [None]
        for number, page in enumerate(pages[1:], start=1):
            more = number < count - 1
            texts.append(
                f'{{"geometry":{FORMAT},"rev":"{rev}","page":{number},"pages":{count},'
                f'"has_more":{"true" if more else "false"},'
                f'"next_offset":{number + 1 if more else "null"},"chunks":['
                + ",".join(chunk[3] for chunk in page) + "]}")
        plan = []
        for number, page in enumerate(pages):
            poured = bool(page) and all(chunk[2] for chunk in page)
            chunk_list = []
            for kind, layer, _, _ in page:
                if not chunk_list or chunk_list[-1] != [kind, layer]:
                    chunk_list.append([kind, layer])
            plan.append({"page": number, "auto": auto_all or not poured,
                         "chars": len(texts[number]) if number else 0, "chunks": chunk_list})
        if len(_dumps(plan)) <= plan_room:
            break
        plan_room = len(_dumps(plan)) + 400
    notes = list(head["notes"])
    if dropped:
        notes.insert(0, f"{counted(dropped, 'row')} too long for a page "
                     f"{'is' if dropped == 1 else 'are'} not drawn.")
    head = {**head, "pages": count, "auto_pages": sum(1 for entry in plan if entry["auto"]),
            "has_more": count > 1, "next_offset": 1 if count > 1 else None, "plan": plan,
            "notes": notes, "paging": _paging_note(plan, cut, head["layers"])}
    first_chunks = [chunk[3] for chunk in pages[0]]
    export = Export(rev=rev, head=head, first_chunks=first_chunks, pages=texts, plan=plan,
                    dnp_names=dnp_names, reserve=reserve)
    plan[0]["chars"] = len(export.page(0))
    export.head["plan"] = plan
    return export


KIND_WORDS = {"board": "the outline", "nets": "net names", "stacks": "pad shapes",
              "viatypes": "via types", "sch": "schematic designators", "parts": "parts",
              "pads": "pads", "vias": "vias", "tracks": "tracks", "arcs": "arcs",
              "areas": "copper areas", "pours": "pour outlines", "silk": "silkscreen",
              "silkarcs": "silkscreen", "poured_areas": "poured copper",
              "poured_tracks": "poured copper", "poured_arcs": "poured copper"}


def _paging_note(plan: list[dict], cut: tuple[int, set], layers: list[dict]) -> str | None:
    """What the view reads by itself and what waits (None when it reads every page)."""
    count = len(plan)
    auto = sum(1 for entry in plan if entry["auto"])
    on_request = count - auto
    parts = []
    if auto > AUTO_PAGES:
        parts.append(f"the view reads {AUTO_PAGES} of its {auto} routing pages by itself and "
                     "the rest when asked")
    if on_request:
        parts.append("its poured copper is drawn as pour outlines until loaded per layer "
                     f"({counted(on_request, 'page')})")
    if cut[0]:
        names = {layer["id"]: layer["name"] for layer in layers}
        what: dict[str, list[str]] = defaultdict(list)
        for kind, layer in sorted(cut[1], key=lambda item: (item[1] is None, item[1] or 0)):
            word = KIND_WORDS.get(kind, kind)
            name = names.get(layer, f"layer {layer}") if layer is not None else None
            if name and name not in what[word]:
                what[word].append(name)
            elif not name:
                what.setdefault(word, [])
        listed = "; ".join(word + (f" on {', '.join(on)}" if on else "") for word, on in what.items())
        parts.append(f"{counted(cut[0], 'more page')} ({listed}) "
                     f"{'is' if cut[0] == 1 else 'are'} not sent: the view reads at most "
                     f"{SESSION_PAGES} pages for one answer")
    if not parts:
        return None
    return f"The board takes {counted(count + cut[0], 'page')}: " + "; ".join(parts) + "."


def export_for(index, rev: str) -> Export:
    """The board's export, built once per index and revision (kept on the index, as the
    DFM review is)."""
    cached = getattr(index, "_board_view", None)
    if cached is not None and cached[0] == rev:
        return cached[1]
    export = build_export(index, rev)
    index._board_view = (rev, export)
    return export


# ---------- the locator ----------

@dataclass(frozen=True)
class SourceSpec:
    """How to read one tool's findings: the frame of its coordinates (absolute, board
    origin or names only), the point keys (a key's stem: 'at', 'location', or '*_at' for
    any stem ending in _at), the name keys and what they name, the layer keys, the
    nested keys whose dicts (or lists of dicts) are read too, a measure and the label's
    short name."""

    frame: str
    points: tuple[str, ...] = ()
    names: dict = field(default_factory=dict)
    layers: tuple[str, ...] = ()
    nested: tuple[str, ...] = ()
    measure: str | None = None
    short: str = "Finding"


FINDING_SOURCES = {
    "get_drc_report": SourceSpec(
        "absolute", points=("location",),
        names={"components": "parts", "component": "parts", "nets": "nets", "net": "nets",
               "diff_pairs": "pairs"},
        nested=("examples", "violations"), short="DRC"),
    "check_dfm": SourceSpec(
        "board origin", points=("at", "*_at"),
        names={"objects": "objects", "object": "objects", "designators": "parts", "net": "nets",
               "nets": "nets"},
        layers=("layer", "layers"), nested=("examples",), short="DFM"),
    "check_testpoints": SourceSpec(
        "board origin", points=("at",),
        names={"net": "nets", "nets": "nets", "pad": "pads", "designators": "parts",
               "part": "parts"},
        layers=("layer",), nested=("examples", "first", "second"), short="Testpoints"),
    "check_decoupling": SourceSpec(
        "names only",
        names={"designators": "parts", "ic": "parts", "rail": "nets",
               "nearest": "designator_of", "nearest_under_1uF": "designator_of",
               "capacitors": "designator_of"},
        measure="decoupling", short="Decoupling"),
    "check_interfaces": SourceSpec(
        "names only",
        names={"designators": "parts", "device": "parts", "connector": "parts"},
        measure="interfaces", short="Interfaces"),
}

# The other PCB tools that report coordinates, and the frame their code uses: their
# names are read generically for now; placing their coordinates needs a FINDING_SOURCES
# entry and a round-trip test each.
DEFERRED_SOURCES = {
    "check_connectivity": "absolute",
    "check_rule_compliance": "absolute",
    "check_vias": "absolute",
    "check_pcb_health": "board origin",
    "check_mechanical_fit": "board origin",
    "check_hv_clearance": "board origin",
    "check_return_path": "board origin",
    "check_power_paths": "board origin",
    "check_diff_pair_geometry": "board origin",
    "get_mechanical_handoff": "board origin",
    "package_for_xfn": "board origin",  # its mechanical handoff
    "verify_outputs": "board origin",
    "get_assembly_guide": "board origin",
}

GENERIC_NAMES = {
    "designators": "parts", "designator": "parts", "components": "parts", "parts": "parts",
    "refdes": "parts", "ic": "parts", "device": "parts", "net": "nets", "nets": "nets",
    "rail": "nets", "pad": "pads", "pads": "pads",
}
POINT_STEMS = ("at", "location", "position", "centre", "center", "point", "xy")

MAX_NETS = 20
MAX_PARTS = 50
MAX_PADS = 50
MAX_MARKS = 50
MAX_MEASURES = 20
MAX_FINDINGS = 20
NEAR_LISTED = 5
SUGGESTIONS = 5
DEFAULT_RADIUS_MIL = 50.0
MERGE_MIL = 0.5
LISTED = {"nets": 20, "parts": 50, "pads": 50, "marks": 50, "measures": 20, "findings": 20,
          "not_found": 50}
NET_PARTS_LISTED = 20
SHRINK_STEPS = (None, 15, 5)
MM = 25.4 / 1000.0  # mm per mil


def parse_findings(value) -> list[dict] | None:
    """One finding object, a list of them, or a JSON string holding either; None when
    it is anything else."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list) and all(isinstance(item, dict) for item in value):
        return list(value)
    return None


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _stem(key: str) -> tuple[str, str] | None:
    """(stem, unit) of a key ending in _mil or _mm."""
    if key.endswith("_mil"):
        return key[:-4], "mil"
    if key.endswith("_mm"):
        return key[:-3], "mm"
    return None


def _point_value(key: str, value) -> tuple[float, float] | None:
    """A point in mil: [x, y], or {x_*, y_*}; a key or sub-key ending _mm is in mm."""
    stem = _stem(key)
    scale = 1.0 / MM if stem and stem[1] == "mm" else 1.0
    if isinstance(value, (list, tuple)) and len(value) == 2 and all(map(_number, value)):
        return float(value[0]) * scale, float(value[1]) * scale
    if isinstance(value, dict):
        for x_key, y_key, sub_scale in (("x_mil", "y_mil", 1.0), ("x_mm", "y_mm", 1.0 / MM)):
            if _number(value.get(x_key)) and _number(value.get(y_key)):
                return float(value[x_key]) * sub_scale, float(value[y_key]) * sub_scale
        if _number(value.get("x")) and _number(value.get("y")):
            return float(value["x"]) * scale, float(value["y"]) * scale
    return None


def _looks_like_point(key: str, value) -> bool:
    stem = _stem(key)
    if stem is None:
        return False
    if isinstance(value, dict):
        return _point_value(key, value) is not None
    return (stem[0] in POINT_STEMS or stem[0].endswith("_at")) and \
        _point_value(key, value) is not None


def _names(value) -> list[str]:
    """A name key's value: a str, a list of str, or a dict (its keys)."""
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, dict):
        return [str(key) for key in value if str(key).strip()]
    if isinstance(value, (list, tuple)):
        return [item for item in value if isinstance(item, str) and item.strip()]
    return []


def _layer_names(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [item for item in value if isinstance(item, str)]
    return []


_OBJECT = re.compile(r"^(?P<head>.*?)(?: \((?P<paren>[^()]*(?:\([^()]*\)[^()]*)*)\))?$")


@dataclass
class Located:
    """What get_board_view found, in mil, before the lists are capped."""

    highlight: dict
    totals: dict
    side: str
    layers_on: list[str]
    window: dict
    notes: list[str]


class Locator:
    """Resolves get_board_view's arguments on a PcbIndex: nets, parts, pads, a spot and
    the objects findings name, with where each lies."""

    def __init__(self, index, schematic: set[str] | None = None, is_dnp=None,
                 radius_mil: float = DEFAULT_RADIUS_MIL):
        self.index = index
        self.pcb = index.pcb
        self.db = index.db
        self.schematic = schematic
        self.is_dnp = is_dnp or (lambda name: False)
        self.radius = radius_mil
        board = self.pcb.board
        self.origin = (board.origin_x or 0.0, board.origin_y or 0.0)
        self.outline = list(board.outline_vertices or [])
        self.extents = board_extents(index)
        layers = board_layers(index)
        self.copper = layers["copper"]
        self.stack = {layer: number for number, layer in enumerate(self.copper)}
        self.nets_by_name: dict[str, int] = {}
        for number, name in enumerate(self.pcb.nets):
            self.nets_by_name.setdefault(name.lower(), number)
        self.nets: dict[int, dict] = {}
        self.parts: dict[int, dict] = {}
        self.pads: dict[int, dict] = {}
        self.marks: list[dict] = []
        self.measures: list[dict] = []
        self.findings: list[dict] = []
        self.not_found: list[dict] = []
        self.finding_layers: set[int] = set()
        self.notes: list[str] = []
        self.overflow = {"marks": 0, "measures": 0}
        # The nets, parts and pads a cap left out, each once (they count in *_total).
        self.refused: dict[str, set] = {"nets": set(), "parts": set(), "pads": set()}

    # ----- where things are -----

    def on_board(self, x: float, y: float) -> bool:
        if len(self.outline) >= 3:
            return point_in_polygon(x, y, self.outline)
        minx, miny, maxx, maxy = self.extents
        return minx is not None and minx <= x <= maxx and miny <= y <= maxy

    def where(self, x: float, y: float) -> str:
        """A plain word for a point: the thirds of the board extents (+Y up), or outside."""
        minx, miny, maxx, maxy = self.extents
        if minx is None:
            return "unknown"
        horizontal = "left" if x < minx else "right" if x > maxx else ""
        vertical = "top" if y > maxy else "bottom" if y < miny else ""
        if horizontal or vertical:
            return f"outside the board ({' '.join(filter(None, (vertical, horizontal)))})"
        third_x = (maxx - minx) / 3.0
        third_y = (maxy - miny) / 3.0
        column = "left" if x < minx + third_x else "right" if x > maxx - third_x else ""
        row = "bottom" if y < miny + third_y else "top" if y > maxy - third_y else ""
        return " ".join(filter(None, (row, column))) or "centre"

    def at(self, x: float, y: float) -> dict:
        return {"x_mil": round(x, 4), "y_mil": round(y, 4)}

    def from_origin(self, x: float, y: float) -> dict:
        return {"x_mil": round(x - self.origin[0], 4), "y_mil": round(y - self.origin[1], 4)}

    def designator(self, component: int | None) -> str | None:
        if component is None or not 0 <= component < len(self.pcb.components):
            return None
        return self.index.designator(component)

    def layer_name(self, layer: int | None) -> str:
        if layer is None:
            return "?"
        name = self.index.layer_name(layer)
        return STANDARD_NAMES.get(layer, name) if name == f"layer{layer}" else name

    def stack_key(self, layer: int) -> tuple:
        return (self.stack.get(layer, len(self.stack)), layer)

    # ----- nets -----

    def add_net(self, net_id: int, source: str) -> bool:
        if net_id in self.nets:
            return True
        if len(self.nets) >= MAX_NETS:
            self.refused["nets"].add(net_id)
            return False
        self.nets[net_id] = {"id": net_id, "from": source}
        return True

    def ask_nets(self, text: str) -> None:
        for entry in _split(text):
            self._ask_net(entry, "asked")

    def _ask_net(self, entry: str, source: str, regex: bool = True) -> None:
        if _placeholder(entry):
            return
        key = entry.lower()
        if key in self.nets_by_name:
            self._take_nets([self.nets_by_name[key]], entry, source)
            return
        pair = next((pair for pair in self.pcb.diff_pairs if pair.name.lower() == key), None)
        if pair is not None:
            ids = [self.nets_by_name[name.lower()] for name in (pair.positive_net, pair.negative_net)
                   if name.lower() in self.nets_by_name]
            if ids:
                self._take_nets(ids, entry, source)
                return
        if regex:
            try:
                pattern = re.compile(entry, re.IGNORECASE)
            except re.error:
                pattern = None
            if pattern is not None:
                matched = [number for number, name in enumerate(self.pcb.nets)
                           if pattern.search(name)]
                if len(matched) > MAX_NETS:
                    self.missing("net", entry, f"matches {len(matched)} nets: be more specific",
                                 source=source)
                    return
                if matched:
                    self._take_nets(matched, entry, source)
                    return
        self.missing("net", entry, "no net of this board has this name",
                     self._suggest(entry, self.pcb.nets), source=source)

    def _take_nets(self, ids: list[int], entry: str, source: str) -> None:
        refused = [number for number in ids if not self.add_net(number, source)]
        if refused:
            self.too_many("net", entry, MAX_NETS, len(refused), len(ids), source)

    def too_many(self, kind: str, entry: str, cap: int, refused: int, of: int,
                 source: str) -> None:
        """A not_found entry for what a cap left out (refused of the of items entry names)."""
        what = {"net": "nets", "part": "parts", "pad": "pads"}[kind]
        left = ("it is not" if of == 1 else
                f"none of its {of} is" if refused == of else f"{refused} of its {of} are not")
        self.missing(kind, entry, f"too many: at most {cap} {what} are highlighted, and {left}",
                     source=source)

    def _net_entry(self, net_id: int, source: str) -> dict:
        db = self.db
        row = db.execute(
            "SELECT SUM(kind='track' AND pour_id IS NULL AND poured_by IS NULL) tracks, "
            "COUNT(DISTINCT CASE WHEN kind='arc' AND poured_by IS NULL THEN source_id END) arcs, "
            "SUM(kind='pad') pads, SUM(kind='via') vias, "
            "MIN(minx) a, MIN(miny) b, MAX(maxx) c, MAX(maxy) d "
            "FROM prims WHERE net_id = ?", (net_id,)).fetchone()
        layers = [layer for (layer,) in db.execute(
            "SELECT DISTINCT layer FROM prims WHERE net_id = ? AND kind IN ('track','arc') "
            "AND pour_id IS NULL AND poured_by IS NULL", (net_id,))]
        layers.sort(key=self.stack_key)
        components = [component for (component,) in db.execute(
            "SELECT DISTINCT component FROM prims WHERE net_id = ? AND kind = 'pad' "
            "AND component IS NOT NULL", (net_id,))]
        designators = sorted({name for name in map(self.designator, components) if name},
                             key=natural_key)
        pours = db.execute("SELECT COUNT(*) FROM pours WHERE net_id = ? AND NOT shelved",
                           (net_id,)).fetchone()[0]
        box = [row["a"], row["b"], row["c"], row["d"]]
        areas = 0
        for area in self.pcb.areas:
            if area.net != net_id or area.pour_id is not None:
                continue
            areas += 1
            xs = [x for x, _ in area.outline]
            ys = [y for _, y in area.outline]
            box = _union(box, [min(xs), min(ys), max(xs), max(ys)])
        entry = {
            "net": self.pcb.nets[net_id], "from": source,
            "layers": [self.layer_name(layer) for layer in layers],
            "pads": row["pads"] or 0, "vias": row["vias"] or 0, "tracks": row["tracks"] or 0,
            "arcs": row["arcs"] or 0, "areas": areas, "pours": pours,
            "parts_total": len(designators), "parts": designators[:NET_PARTS_LISTED],
        }
        if box[0] is not None:
            entry["extents_mil"] = _box(box)
            entry["where"] = self.where((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0)
        else:
            entry["where"] = "no copper on the board"
        entry["_layers"] = layers
        entry["_box"] = box if box[0] is not None else None
        return entry

    # ----- parts and pads -----

    def ask_parts(self, text: str) -> None:
        for entry in _split(text):
            self._ask_part(entry, "asked")

    def _ask_part(self, entry: str, source: str, pads_too: bool = True) -> None:
        if _placeholder(entry):
            return
        rows = self.index.find_components(entry)
        if rows:
            self._take_parts(rows, entry, source)
            return
        if pads_too and self._ask_pad(entry, source):
            return
        why = "no part of this board has this designator"
        if self.schematic is not None and entry.lower() in self.schematic:
            why = "in the schematic, not on the board (check_sch_pcb_sync)"
        names = [row[0] for row in self.db.execute(
            "SELECT DISTINCT refdes FROM components WHERE refdes IS NOT NULL")]
        self.missing("part", entry, why, self._suggest(entry, names), source=source)

    def _add_part(self, row, source: str) -> bool:
        number = row["id"]
        if number in self.parts:
            return True
        if len(self.parts) >= MAX_PARTS:
            self.refused["parts"].add(number)
            return False
        self.parts[number] = {"row": dict(row), "from": source}
        return True

    def _pad_rows(self, designator: str, pin: str | None = None, net: int | None = None) -> list:
        ids = [row["id"] for row in self.index.find_components(designator)]
        if not ids:
            return []
        query = (f"SELECT * FROM prims WHERE kind = 'pad' AND component IN "
                 f"({','.join('?' * len(ids))})")
        rows = self.db.execute(query, ids).fetchall()
        if pin is not None:
            rows = [row for row in rows if (row["pad_name"] or "").lower() == pin.lower()]
        if net is not None:
            rows = [row for row in rows if row["net_id"] == net]
        return rows

    def _take_parts(self, rows: list, entry: str, source: str) -> None:
        refused = [row for row in rows if not self._add_part(row, source)]
        if refused:
            self.too_many("part", entry, MAX_PARTS, len(refused), len(rows), source)

    def _take_pads(self, rows: list, entry: str, source: str, nets: bool = False) -> None:
        """Highlight pads (and with nets, the nets they are on); what the caps leave out
        is counted and named."""
        refused = 0
        for row in rows:
            if not self._add_pad(row, source):
                refused += 1
            elif nets and row["net_id"] is not None and not self.add_net(row["net_id"], source):
                self.too_many("net", self.pcb.nets[row["net_id"]], MAX_NETS, 1, 1, source)
        if refused:
            self.too_many("pad", entry, MAX_PADS, refused, len(rows), source)

    def split_pad(self, entry: str) -> tuple[str, str, list] | None:
        """(designator, pin, pad rows) of a pad written 'U3-4' or 'U3.4'. A designator or
        pin name may hold '-' or '.' too: each split is tried, the last separator first.
        The rows are empty when the part exists but has no such pin; None when no split
        names a part of the board."""
        known = None
        for cut in sorted((n for n, char in enumerate(entry) if char in "-."), reverse=True):
            designator, pin = entry[:cut], entry[cut + 1:]
            if not designator or not pin or not self.index.find_components(designator):
                continue
            rows = self._pad_rows(designator, pin)
            if rows:
                return designator, pin, rows
            known = known or (designator, pin, [])
        return known

    def _ask_pad(self, entry: str, source: str) -> bool:
        found = self.split_pad(entry)
        if found is None:
            return False
        designator, pin, rows = found
        if not rows:
            pins = sorted({row["pad_name"] for row in self._pad_rows(designator)
                           if row["pad_name"]}, key=natural_key)
            self.missing("pad", entry, f"{designator} has no pad {pin}",
                         self._suggest(pin, pins, prefix=f"{designator}-"), source=source)
            return True
        self._take_pads(rows, entry, source)
        return True

    def _add_pad(self, row, source: str) -> bool:
        if row["id"] in self.pads:
            return True
        if len(self.pads) >= MAX_PADS:
            self.refused["pads"].add(row["id"])
            return False
        self.pads[row["id"]] = {"row": row, "from": source}
        return True

    def _part_entry(self, number: int, item: dict) -> dict:
        row = item["row"]
        pads = self.db.execute("SELECT COUNT(*) FROM prims WHERE kind = 'pad' AND component = ?",
                               (number,)).fetchone()[0]
        entry = {"designator": row["refdes"]}
        if row["sch_refdes"] and row["sch_refdes"] != row["refdes"]:
            entry["sch_refdes"] = row["sch_refdes"]
        entry.update({"from": item["from"], "side": row["side"]})
        placed = row["x"] is not None and row["y"] is not None
        if placed:
            entry["at_mil"] = self.at(row["x"], row["y"])
            entry["from_origin_mil"] = self.from_origin(row["x"], row["y"])
        entry["rotation"] = row["rotation"]
        entry["pads"] = pads
        entry["dnp"] = bool(self.is_dnp(row["sch_refdes"] or row["refdes"] or ""))
        box = [row["minx"], row["miny"], row["maxx"], row["maxy"]]
        if box[0] is not None:
            entry["extents_mil"] = _box(box)
        if placed:
            entry["where"] = self.where(row["x"], row["y"])
        elif box[0] is not None:
            entry["where"] = self.where((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0)
        else:
            entry["where"] = "not placed"
        entry["_box"] = box if box[0] is not None else None
        entry["_side"] = row["side"]
        return entry

    def _pad_entry(self, item: dict) -> dict:
        row = item["row"]
        designator = self.designator(row["component"]) or "?"
        entry = {"pad": f"{designator}-{row['pad_name'] or ''}", "from": item["from"],
                 "net": self.pcb.nets[row["net_id"]] if row["net_id"] is not None else None,
                 "layer": self.layer_name(row["layer"]),
                 "at_mil": self.at(row["x1"], row["y1"]),
                 "where": self.where(row["x1"], row["y1"])}
        entry["_box"] = [row["minx"], row["miny"], row["maxx"], row["maxy"]]
        side = None
        if row["layer"] == BOTTOM:
            side = "bottom"
        elif row["layer"] == TOP:
            side = "top"
        elif row["component"] is not None and 0 <= row["component"] < len(self.pcb.components):
            side = self.pcb.components[row["component"]].side
        entry["_side"] = side
        entry["_layer"] = row["layer"]
        return entry

    # ----- marks -----

    def add_mark(self, x: float, y: float, label: str, source: str, layer: str | None = None,
                 raw: tuple[float, float] | None = None, frame: str = "absolute") -> bool:
        for mark in self.marks:
            if math.dist((mark["_x"], mark["_y"]), (x, y)) <= MERGE_MIL:
                return False
        if len(self.marks) >= MAX_MARKS:
            self.overflow["marks"] += 1
            return False
        on_board = self.on_board(x, y)
        mark = {"label": label, "from": source, "at_mil": self.at(x, y),
                "from_origin_mil": self.from_origin(x, y)}
        if layer:
            mark["layer"] = layer
        mark["on_board"] = on_board
        if not on_board and self.origin != (0.0, 0.0):
            if frame == "board origin" and raw is not None and self.on_board(*raw):
                mark["frame_hint"] = "looks absolute: check finding_tool"
            elif frame != "board origin" and \
                    self.on_board(x + self.origin[0], y + self.origin[1]):
                mark["frame_hint"] = ("looks origin-relative: check finding_tool"
                                      if source.startswith("finding") else
                                      "looks origin-relative: x and y are absolute, add origin_*")
        mark["_x"], mark["_y"] = x, y
        self.marks.append(mark)
        return True

    def _near(self, mark: dict) -> dict:
        found = self.index.region_query(mark["_x"], mark["_y"], self.radius)
        return {"nets": [item["net"] for item in found["nets"][:NEAR_LISTED]],
                "parts": [item["refdes"] for item in found["components"][:NEAR_LISTED]]}

    # ----- findings -----

    def read_findings(self, findings: list[dict], tool: str | None) -> None:
        if len(findings) > MAX_FINDINGS:
            self.notes.append(f"{len(findings)} findings given: the first {MAX_FINDINGS} are read.")
        spec = FINDING_SOURCES.get(tool or "")
        for number, finding in enumerate(findings[:MAX_FINDINGS], start=1):
            source = f"finding {number}"
            record = {"finding": number, "tool": tool,
                      "frame": spec.frame if spec else "names only",
                      "read": [], "ignored": [], "marks": 0}
            marks_before = len(self.marks)
            self._read(finding, spec, source, number, record, finding, depth=1)
            if spec and spec.measure:
                self._measure(finding, spec, source)
            if not spec and record["ignored"]:
                why = (f"{tool}'s coordinates are not placed yet: pass x and y (absolute)"
                       if tool else "name finding_tool so its coordinates can be placed")
                self.missing("location", ", ".join(record["ignored"]), why, source=source)
            if not record["read"] and not record["ignored"] and len(self.marks) == marks_before:
                whole = any(isinstance(finding.get(key), list) for key in ("findings", "pairs"))
                self.missing("finding", source, "no names or locations were read from it: " + (
                    "it looks like a whole result; pass its findings (or pairs) themselves"
                    if whole else "pass a finding (or one of its examples) as "
                    f"{tool or 'the tool'} gave it"), source=source)
            record["marks"] = len(self.marks) - marks_before
            record["read"] = sorted(set(record["read"]))
            record["ignored"] = sorted(set(record["ignored"]))
            self.findings.append(record)

    def _label(self, spec: SourceSpec | None, number: int, item: dict, finding: dict) -> str:
        short = spec.short if spec else "Finding"
        detail = None
        for source in (item, finding):
            detail = next((source[key] for key in ("check", "rule", "rule_kind")
                           if isinstance(source.get(key), str) and source[key]), None)
            if detail:
                break
        return f"{short} {number}" + (f": {detail.replace('_', ' ')}" if detail else "")

    def _read(self, item: dict, spec: SourceSpec | None, source: str, number: int,
              record: dict, finding: dict, depth: int, layer: str | None = None) -> None:
        """Read one finding dict (and the dicts in its nested lists, to depth 3): names,
        points as marks (in the tool's frame), layers."""
        names = spec.names if spec else GENERIC_NAMES
        nested = spec.nested if spec else None
        if isinstance(item.get("layer"), str):
            layer = item["layer"]  # an example's own layer, else its finding's
        for key in (spec.layers if spec else ()):
            for name in _layer_names(item.get(key)):
                resolved = self.index.resolve_layer(name)
                if resolved is not None:
                    self.finding_layers.add(resolved)
                    record["read"].append(key)
        for key, value in item.items():
            what = names.get(key)
            if what is not None:
                if self._read_name(what, key, value, item, source):
                    record["read"].append(key)
                continue
            stem = _stem(key)
            if stem is None:
                continue
            placed = spec is not None and spec.frame != "names only" and \
                self._spec_point(spec, stem[0])
            point = _point_value(key, value) if placed or _looks_like_point(key, value) else None
            if point is None:
                continue
            if not placed:
                record["ignored"].append(key)
                continue
            x, y = point
            if spec.frame == "board origin":
                x, y = x + self.origin[0], y + self.origin[1]
            self.add_mark(x, y, self._label(spec, number, item, finding), source,
                          layer=layer, raw=point, frame=spec.frame)
            record["read"].append(key)
        if depth >= 3:
            return
        # The dicts of nested lists (examples, violations) and of nested dicts (a spacing
        # example's first and second probe); a tool's own entry names the keys.
        for key, value in item.items():
            if (nested is not None and key not in nested) or key in names:
                continue
            for child in value if isinstance(value, list) else [value] if nested else []:
                if isinstance(child, dict):
                    self._read(child, spec, source, number, record, finding, depth + 1, layer)

    @staticmethod
    def _spec_point(spec: SourceSpec, stem: str) -> bool:
        return stem in spec.points or ("*_at" in spec.points and stem.endswith("_at"))

    def _read_name(self, what: str, key: str, value, item: dict, source: str) -> bool:
        if what == "designator_of":
            values = value if isinstance(value, list) else [value]
            names = [entry.get("designator") for entry in values if isinstance(entry, dict)]
            names = [name for name in names if isinstance(name, str) and name]
            for name in names:
                self._ask_part(name, source, pads_too=False)
            return bool(names)
        if what == "objects":
            return self._read_objects(value, source)
        names = [name for name in _names(value) if not _placeholder(name)]
        for name in names:
            if what == "parts":
                self._ask_part(name, source, pads_too=False)
            elif what == "nets":
                self._ask_net(name, source, regex=False)
            elif what == "pads":
                self._ask_part(name, source)
            elif what == "pairs":
                self._ask_net(name, source, regex=False)
        if key == "connector" and names and isinstance(item.get("pins"), list):
            for pin in item["pins"]:
                if isinstance(pin, (str, int)):
                    rows = self._pad_rows(names[0], str(pin))
                    self._take_pads(rows, f"{names[0]}-{pin}", source, nets=True)
        return bool(names)

    def _read_objects(self, value, source: str) -> bool:
        """check_dfm's objects: 'Pad U3-12 (GND) / Track (CLK)', 'Region of U5 (no net)'."""
        if not isinstance(value, str):
            return False
        read = False
        for piece in value.split(" / "):
            piece = piece.strip()
            match = _OBJECT.match(piece)
            head, paren = (match.group("head"), match.group("paren")) if match else (piece, None)
            if paren and paren.lower() in self.nets_by_name:
                self._take_nets([self.nets_by_name[paren.lower()]], paren, source)
                read = True
            if head.startswith("Pad ") and ("-" in head or "." in head):
                found = self.split_pad(head[4:].strip())
                if found and found[2]:
                    self._take_pads(found[2], head[4:].strip(), source)
                    read = True
            elif " of " in head:
                designator = head.split(" of ", 1)[1].strip()
                rows = self.index.find_components(designator)
                if rows:
                    self._take_parts(rows, designator, source)
                    read = True
            elif head and " " not in head and self.index.find_components(head):
                self._take_parts(self.index.find_components(head), head, source)
                read = True
        return read

    # ----- measures -----

    def _measure(self, finding: dict, spec: SourceSpec, source: str) -> None:
        if spec.measure == "decoupling":
            ic, rail, nearest = finding.get("ic"), finding.get("rail"), finding.get("nearest")
            cap = nearest.get("designator") if isinstance(nearest, dict) else None
            if not (isinstance(ic, str) and isinstance(rail, str) and isinstance(cap, str)):
                return
            net = self.nets_by_name.get(rail.lower())
            if net is None:
                return
            # check_decoupling measures from the IC's power pins on the rail (pins, in its
            # pairs), else from every pad of it on the rail.
            on_rail = self._pad_rows(ic, net=net)
            pins = finding.get("pins")
            named = {str(pin) for pin in pins} if isinstance(pins, list) else set()
            first = [row for row in on_rail if row["pad_name"] in named] or on_rail
            second = self._pad_rows(cap, net=net) or self._pad_rows(cap)
            if not named and len(first) > 1:
                # A finding names no pins: the IC pad whose nearest capacitor pad lies at
                # the finding's own distance is the power pad the tool measured from.
                told = _distance_mil(nearest)
                if told is not None and second:
                    def gap(row) -> float:
                        return min(self.index._primitive_distance(row, other) for other in second)
                    first = [min(first, key=lambda row: (abs(gap(row) - told), gap(row)))]
            self._add_measure(first, second, "check_decoupling", source)
        elif spec.measure == "interfaces":
            device, connector = finding.get("device"), finding.get("connector")
            pins = finding.get("pins")
            if not (isinstance(device, str) and isinstance(connector, str) and
                    isinstance(pins, list)):
                return
            device_pads = self._pad_rows(device)
            for pin in pins:
                for pad in self._pad_rows(connector, str(pin)):
                    same = [row for row in device_pads
                            if row["net_id"] is not None and row["net_id"] == pad["net_id"]]
                    self._add_measure(same or device_pads, [pad], "check_interfaces", source)

    def _add_measure(self, first: list, second: list, tool: str, source: str) -> None:
        if not first or not second:
            return
        best = None
        for a in first:
            for b in second:
                distance = self.index._primitive_distance(a, b)
                if best is None or distance < best[0]:
                    best = (distance, a, b)
        if len(self.measures) >= MAX_MEASURES:
            self.overflow["measures"] += 1
            return
        distance, a, b = best
        entry = {"from_pad": self._pad_name(a), "to_pad": self._pad_name(b),
                 "distance_mil": round(distance, 2)}
        if a["net_id"] is not None and a["net_id"] == b["net_id"]:
            entry["net"] = self.pcb.nets[a["net_id"]]
        entry.update({"from_at_mil": self.at(a["x1"], a["y1"]),
                      "to_at_mil": self.at(b["x1"], b["y1"]),
                      "basis": f"straight line, pad edge to pad edge, as {tool} measures",
                      "from": source})
        self.measures.append(entry)

    def _pad_name(self, row) -> str:
        return f"{self.designator(row['component']) or '?'}-{row['pad_name'] or ''}"

    # ----- not found -----

    def missing(self, kind: str, asked: str, why: str, suggestions=None,
                source: str = "asked") -> None:
        entry = {"kind": kind, "asked": asked, "why": why}
        if suggestions:
            entry["suggestions"] = list(suggestions)[:SUGGESTIONS]
        if source != "asked":
            entry["from"] = source
        if entry not in self.not_found:
            self.not_found.append(entry)

    @staticmethod
    def _suggest(entry: str, names, prefix: str = "") -> list[str]:
        lowered = {}
        for name in names:
            lowered.setdefault(str(name).lower(), str(name))
        close = difflib.get_close_matches(entry.lower(), list(lowered), n=SUGGESTIONS, cutoff=0.6)
        # The closest first; names as close as each other in natural order.
        close.sort(key=lambda name: (-difflib.SequenceMatcher(None, entry.lower(), name).ratio(),
                                     natural_key(lowered[name])))
        return [prefix + lowered[name] for name in close]

    # ----- layers and side -----

    def ask_layers(self, text: str) -> set[int]:
        chosen: set[int] = set()
        for word in _split(text):
            key = word.lower()
            if key == "all":
                chosen |= set(self.copper)
            elif key == "top" and TOP in self.copper:
                chosen.add(TOP)
            elif key == "bottom" and BOTTOM in self.copper:
                chosen.add(BOTTOM)
            elif key == "inner":
                chosen |= {layer for layer in self.copper if layer not in (TOP, BOTTOM)}
            else:
                resolved = self.index.resolve_layer(word)
                if resolved is None:
                    names = [self.layer_name(layer) for layer in self.copper]
                    self.missing("layer", word, "not a copper layer of this board",
                                 self._suggest(word, names) or names[:SUGGESTIONS])
                else:
                    chosen.add(resolved)
        return chosen


def _split(text: str | None) -> list[str]:
    return [item.strip() for item in (text or "").split(",") if item.strip()]


_PLACEHOLDER = re.compile(r"^\((?:no|none)\b[^()]*\)$", re.IGNORECASE)


def _distance_mil(item: dict) -> float | None:
    """distance_mil, or distance_mm in mil, of a finding's dict."""
    if _number(item.get("distance_mil")):
        return float(item["distance_mil"])
    if _number(item.get("distance_mm")):
        return float(item["distance_mm"]) / MM
    return None


def _placeholder(name: str) -> bool:
    """A tool's stand-in for a missing name, such as get_drc_report's '(no component)'
    and '(no net)' groups, or check_dfm's 'no net': nothing to look for."""
    text = name.strip()
    return bool(_PLACEHOLDER.match(text)) or text.lower() == "no net"


def _union(box: list, other: list) -> list:
    if box[0] is None:
        return list(other)
    return [min(box[0], other[0]), min(box[1], other[1]), max(box[2], other[2]),
            max(box[3], other[3])]


def _box(box: list) -> dict:
    return {"minx_mil": round(box[0], 4), "miny_mil": round(box[1], 4),
            "maxx_mil": round(box[2], 4), "maxy_mil": round(box[3], 4)}


def board_extents(index) -> tuple:
    """(minx, miny, maxx, maxy) of the board outline, else of its copper (as
    get_board_info's board_extents)."""
    outline = index.pcb.board.outline_vertices or []
    if outline:
        xs = [point[0] for point in outline]
        ys = [point[1] for point in outline]
        return min(xs), min(ys), max(xs), max(ys)
    row = index.db.execute("SELECT MIN(minx), MIN(miny), MAX(maxx), MAX(maxy) FROM prims").fetchone()
    return tuple(row)


def locate(index, *, nets: str | None = None, parts: str | None = None,
           findings: list[dict] | None = None, finding_tool: str | None = None,
           point: tuple[float, float] | None = None, radius_mil: float = DEFAULT_RADIUS_MIL,
           layers: str | None = None, side: str = "auto", schematic: set[str] | None = None,
           is_dnp=None, point_label: str | None = None) -> Located:
    """Everything get_board_view highlights, in mil (lists not capped yet). point is
    absolute, in mil; point_label names it as asked (in the call's units)."""
    locator = Locator(index, schematic, is_dnp, radius_mil)
    if nets:
        locator.ask_nets(nets)
    if parts:
        locator.ask_parts(parts)
    if findings:
        locator.read_findings(findings, finding_tool)
    if point is not None:
        x, y = point
        locator.add_mark(x, y, point_label or f"{_short(x)}, {_short(y)}", "asked")
    chosen = locator.ask_layers(layers) if layers else set()

    net_entries = [locator._net_entry(number, item["from"]) for number, item in locator.nets.items()]
    part_entries = [locator._part_entry(number, item) for number, item in locator.parts.items()]
    pad_entries = [locator._pad_entry(item) for item in locator.pads.values()]
    for mark in locator.marks:
        mark["near"] = locator._near(mark)
        mark["where"] = locator.where(mark["_x"], mark["_y"])

    # The side the view starts on: bottom when every part and pad asked for is there.
    sides = [entry["_side"] for entry in part_entries + pad_entries]
    if side not in ("top", "bottom"):
        side = "bottom" if sides and all(value == "bottom" for value in sides) else "top"

    # The layers the view shows first.
    copper = locator.copper
    if chosen:
        on = set(chosen)
    elif len(copper) <= 4:
        on = {layer for layer in copper if not is_plane(layer)}
    else:
        on = {layer for layer in copper if layer in (TOP, BOTTOM)}
        for entry in net_entries:
            on |= set(entry["_layers"])
        on |= locator.finding_layers
        on |= {entry["_layer"] for entry in pad_entries if is_copper(entry["_layer"])}
        for mark in locator.marks:
            resolved = index.resolve_layer(mark["layer"]) if mark.get("layer") else None
            if resolved is not None:
                on.add(resolved)
        on = {layer for layer in on if layer in copper and not is_plane(layer)}
    layers_on = [locator.layer_name(layer) for layer in sorted(on, key=locator.stack_key)]

    # The window: everything highlighted, grown 15% (at least 100 mil).
    box = [None, None, None, None]
    for entry in net_entries + part_entries + pad_entries:
        if entry.get("_box"):
            box = _union(box, entry["_box"])
    for mark in locator.marks:
        box = _union(box, [mark["_x"], mark["_y"], mark["_x"], mark["_y"]])
        if not mark["on_board"] and locator.extents[0] is not None:
            box = _union(box, list(locator.extents))  # a mark off the board: the board too
    for measure in locator.measures:
        for key in ("from_at_mil", "to_at_mil"):
            at = measure[key]
            box = _union(box, [at["x_mil"], at["y_mil"], at["x_mil"], at["y_mil"]])
    if box[0] is None:
        box = list(locator.extents) if locator.extents[0] is not None else [0.0, 0.0, 0.0, 0.0]
    else:
        grow_x = max((box[2] - box[0]) * 0.15, 100.0) / 2.0
        grow_y = max((box[3] - box[1]) * 0.15, 100.0) / 2.0
        box = [box[0] - grow_x, box[1] - grow_y, box[2] + grow_x, box[3] + grow_y]

    for entries in (net_entries, part_entries, pad_entries):
        for entry in entries:
            for key in [key for key in entry if key.startswith("_")]:
                del entry[key]
    for mark in locator.marks:
        del mark["_x"], mark["_y"]

    totals = {"nets": len(net_entries) + len(locator.refused["nets"]),
              "parts": len(part_entries) + len(locator.refused["parts"]),
              "pads": len(pad_entries) + len(locator.refused["pads"]),
              "marks": len(locator.marks) + locator.overflow["marks"],
              "measures": len(locator.measures) + locator.overflow["measures"],
              "findings": len(locator.findings), "not_found": len(locator.not_found)}
    highlight = {"nets": net_entries, "parts": part_entries, "pads": pad_entries,
                 "marks": locator.marks, "measures": locator.measures,
                 "findings": locator.findings, "not_found": locator.not_found}
    return Located(highlight=highlight, totals=totals, side=side, layers_on=layers_on,
                   window=_box(box), notes=locator.notes)


def _short(value: float) -> str:
    return f"{round(value, 4):g}"


def highlight_block(located: Located, listed: int | None) -> dict:
    """The highlight lists, each capped (LISTED, or listed for every list when the text
    must shrink), with their totals."""
    out: dict = {}
    for key in ("nets", "parts", "pads", "marks", "measures", "findings", "not_found"):
        cap = LISTED[key] if listed is None else min(LISTED[key], listed)
        out[key] = located.highlight[key][:cap]
        out[f"{key}_total"] = located.totals[key]
    return out
