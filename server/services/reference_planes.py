"""Reference copper of a board's layers, offline (roadmap #29, stage A of the return-path
check): for each signal layer the layers it references, and on any copper layer of the stack
the copper at a point or along a segment, per net.

Reference layers (reference_layers): the layers an impedance profile of the Layer Stack
Manager names for the signal layer (REF_TOP / REF_BOT of its enabled entries), else its
nearest copper layers above and below in the stack. Each comes with the dielectric between
(the heights of the dielectric layers between, summed; None when one is missing or the stack
has none) and what it is: an internal plane (a negative layer), a signal layer carrying a
large pour (one net's area copper covering at least LARGE_POUR_SHARE of the board), or a
signal layer without one; the stack neighbours are listed whatever the basis.

Internal planes are negative: what is drawn on them is a void. A plane's copper is
- per net, the split plane polygons Altium saves (Polygons6 with POLYGONTYPE Split Plane):
  each region that the split lines and the board edge less the pullback cut the plane into,
  with its net; the smallest polygon holding a point gives its net, and a point in none has
  no copper. A plane with none saved (an older file) is one region of the plane's net
  (Board6 PLANEnNETNAME, NO_NET without one) over the whole board;
- less the pullback along the board outline and its cutouts (BoardInfo.plane_pullbacks,
  DEFAULT_PULLBACK_MIL when the file has none) and everything off the board;
- less what is drawn on the layer: tracks and arcs (split lines, and the boundary lines
  Altium draws along the board edge, twice the pullback wide), regions and fills (keepouts
  and board cutouts excepted);
- less the anti-pad of each via, through-hole pad and bare hole crossing the layer that does
  not connect to the region it lies in (another net, no net, or a Power Plane Connect Style
  of No Connect): its copper on the layer (a via's diameter, a pad's inner-layer copper, the
  wall of a hole without copper) grown by the clearance of the Power Plane Clearance rule
  that applies to it (DEFAULT_PLANE_CLEARANCE_MIL without one). A connected one is copper of
  the region: thermal relief air gaps are not voided (their spokes join the plane).

Signal layers are positive: their copper is what is saved on them: tracks, arcs, pads and
vias as check_dfm's copper model holds them (dfm_geometry.copper_model), solid pours'
poured regions, free regions and fills, a hatched pour's hatch tracks and arcs with the
openings of its grid bridged inside its outline (at a return path's scale its hatch is one
conductor; the channels it leaves out are not bridged), its outline when the pour record
gives no grid or track width, and a solid pour's outline when Regions6 was not read. A
shelved pour, an outline-only pour (hatch None: its border tracks are its copper) and a pour
with no saved copper have no area copper.

copper_at(layer, x, y) names the net of the copper at a point: a net name, NO_NET for copper
without a net, None where there is none. Overlapping copper of two nets (a short) reads as
one of them: routing and pads before area copper. segment_coverage(layer, segment) cuts a
segment into intervals of one answer: every crossing of the segment with a boundary of the
shapes above is found exactly (circles, offset edges and arcs of the grown shapes, polygon
edges), each interval between two crossings is named by copper_at at its middle, and
equal neighbours are merged (pieces under MIN_PIECE_MIL go to a neighbour).
plane_partition(layer) summarises a layer's copper per net and how it was read.

Pure logic over a PcbIndex, no MCP. Lengths are mils, coordinates absolute (the frame of the
prims rows: a tool makes them origin-relative). Layers are built on first use and cached on
the index (reference_planes). The return-path check (stage B) builds on this API.
"""

import math
import weakref
from collections import Counter, defaultdict

try:
    from parsers.pcb_doc import (
        MULTI_LAYER,
        decode_fill_records,
        decode_region_records,
        fill_outline,
        pad_copper_on,
        parse_dim_mil,
        shape_points,
    )
    from services.dfm_geometry import AREA_CELL_MIL, Bands, copper_model, point_distance
    from services.pcb_health import area as polygon_area
    from services.pcb_index import PcbIndex, layer_copper
    from services.pcb_rules import ObjectFacts, ObjectResolver, RuleTarget
except ImportError:
    from server.parsers.pcb_doc import (
        MULTI_LAYER,
        decode_fill_records,
        decode_region_records,
        fill_outline,
        pad_copper_on,
        parse_dim_mil,
        shape_points,
    )
    from server.services.dfm_geometry import (
        AREA_CELL_MIL,
        Bands,
        copper_model,
        point_distance,
    )
    from server.services.pcb_health import area as polygon_area
    from server.services.pcb_index import PcbIndex, layer_copper
    from server.services.pcb_rules import ObjectFacts, ObjectResolver, RuleTarget

# copper_at's answer for copper that carries no net (Altium's own label).
NO_NET = "(No Net)"
# Altium's defaults: the Layer Stack Manager's plane pullback and the Power Plane Clearance
# rule a new board gets.
DEFAULT_PULLBACK_MIL = 20.0
DEFAULT_PLANE_CLEARANCE_MIL = 20.0
# A signal layer whose largest one-net area copper covers this share of the board carries a
# large pour (a reference); nets from NET_SHARE_LISTED up are named as its nets. On the
# examples and a private set (279 signal layers of 69 boards) no inner layer lies between
# 0.10 and 0.20 (below: 16, above: 125); outer layers spread, 15 of 138 between 0.10 and 0.20.
LARGE_POUR_SHARE = 0.15
NET_SHARE_LISTED = 0.05
# A plane track or arc whose ends and middle lie this close to the board edge is a boundary
# line (the pullback Altium draws), not a split line.
EDGE_SNAP_MIL = 1.0
# segment_coverage pieces shorter than this are given to a neighbour.
MIN_PIECE_MIL = 1e-3
REGIONS_LISTED = 20
NETS_LISTED = 20
SQ_MIL_PER_MM2 = (1.0 / 0.0254) ** 2
# A hatched pour's copper is its saved hatch tracks and arcs, and inside its outline what
# lies within half an opening (GRIDSIZE less TRACKWIDTH, halved) plus HATCH_JOIN_MIL of
# them: the openings are bridged, the channels the hatch leaves out (around other nets'
# copper, past its clearances) are not. Openings narrower than HATCH_OPEN_MIL (a hatch as
# wide as its grid) leave the reading exact.
HATCH_JOIN_MIL = 0.05
HATCH_OPEN_MIL = 0.01
_CELL_MIL = 50.0  # point and segment grid of the shapes of a layer
_AREA_GRID_MIL = 250.0  # grid of the area copper boxes of a signal layer
_HOLE_CELL_MIL = 100.0  # grid of the holes (holes_near)

APPROXIMATIONS = [
    "An internal plane's regions are the split plane polygons Altium saves with their nets "
    "(set back from the split lines and the board edge as Altium pours them); a plane "
    "without them is one region of the plane's net over the board less its pullback. The "
    f"pullback is read from the Layer Stack Manager ({DEFAULT_PULLBACK_MIL:g} mil when the "
    "file has none) and also applies along board cutouts.",
    "A via, through-hole pad or bare hole crossing a plane that does not connect to the "
    "region it lies in has an anti-pad: its copper on the layer (the via's diameter, the "
    "pad's inner-layer copper, the wall of a hole without copper) grown by its Power Plane "
    f"Clearance rule's clearance ({DEFAULT_PLANE_CLEARANCE_MIL:g} mil without a rule). "
    "Measuring from the pad's edge rather than the hole's gives the larger, more cautious "
    "void; the files do not save the anti-pads Altium draws. "
    "Thermal relief air gaps of connected ones are not voids.",
    "Tracks, arcs, regions and fills on a plane are voids (split lines and drawn cutouts); "
    "keepouts are not.",
    "A hatched pour is its saved hatch tracks and arcs, with the openings of its grid "
    "bridged inside its outline (the channels its hatch leaves out stay voids); one whose "
    "grid or track width is not saved is its outline. An outline-only pour is its border "
    "tracks; a solid pour is its saved copper, its outline when Regions6 was not read; "
    "shelved pours and pours with no saved copper have none.",
    "A signal layer carries a large pour when one net's area copper covers at least "
    f"{LARGE_POUR_SHARE:.0%} of the board (outline less cutouts); routing copper does not "
    "count toward it.",
]


# ---------- geometry ----------


def _circle_hits(ax, ay, dx, dy, cx, cy, radius, out) -> None:
    """Parameters t in (0, 1) where A + tD crosses the circle."""
    a = dx * dx + dy * dy
    if a == 0.0 or radius <= 0.0:
        return
    fx, fy = ax - cx, ay - cy
    b = fx * dx + fy * dy
    c = fx * fx + fy * fy - radius * radius
    disc = b * b - a * c
    if disc < 0.0:
        return
    root = math.sqrt(disc)
    for t in ((-b - root) / a, (-b + root) / a):
        if 0.0 < t < 1.0:
            out.append(t)


def _segment_hits(ax, ay, dx, dy, px, py, qx, qy, out) -> None:
    """Parameters t in (0, 1) where A + tD crosses the segment PQ (its ends' projections
    when the two are parallel and in line)."""
    ex, ey = qx - px, qy - py
    wx, wy = px - ax, py - ay
    denominator = dx * ey - dy * ex
    scale = math.hypot(dx, dy) * math.hypot(ex, ey)
    if scale == 0.0:
        return
    if abs(denominator) <= 1e-12 * scale:
        a = dx * dx + dy * dy
        if abs(wx * dy - wy * dx) <= 1e-9 * a:
            for x, y in ((px, py), (qx, qy)):
                t = ((x - ax) * dx + (y - ay) * dy) / a
                if 0.0 < t < 1.0:
                    out.append(t)
        return
    t = (wx * ey - wy * ex) / denominator
    u = (wx * dy - wy * dx) / denominator
    if 0.0 < t < 1.0 and -1e-9 <= u <= 1.0 + 1e-9:
        out.append(t)


def _capsule_hits(ax, ay, dx, dy, px, py, qx, qy, radius, out) -> None:
    """Crossings with the boundary of a segment grown by radius (a superset: its end
    circles and both offset edges whole)."""
    _circle_hits(ax, ay, dx, dy, px, py, radius, out)
    _circle_hits(ax, ay, dx, dy, qx, qy, radius, out)
    length = math.hypot(qx - px, qy - py)
    if length == 0.0:
        return
    nx, ny = -(qy - py) / length * radius, (qx - px) / length * radius
    _segment_hits(ax, ay, dx, dy, px + nx, py + ny, qx + nx, qy + ny, out)
    _segment_hits(ax, ay, dx, dy, px - nx, py - ny, qx - nx, qy - ny, out)


def _geometry_hits(ax, ay, dx, dy, geometry, out) -> None:
    """Crossings with the boundary of a PcbIndex geometry (segment, radius, polygon, arc):
    a superset (every circle, offset edge and polygon edge the boundary is made of)."""
    segment, radius, polygon, arc = geometry
    if arc is not None:
        cx, cy, big, x1, y1, x2, y2 = arc
        _circle_hits(ax, ay, dx, dy, cx, cy, big + radius, out)
        if big - radius > 0.0:
            _circle_hits(ax, ay, dx, dy, cx, cy, big - radius, out)
        _circle_hits(ax, ay, dx, dy, x1, y1, radius, out)
        _circle_hits(ax, ay, dx, dy, x2, y2, radius, out)
        return
    if polygon is not None:
        count = len(polygon)
        for number in range(count):
            (px, py), (qx, qy) = polygon[number], polygon[(number + 1) % count]
            _segment_hits(ax, ay, dx, dy, px, py, qx, qy, out)
            if radius > 0.0:
                _capsule_hits(ax, ay, dx, dy, px, py, qx, qy, radius, out)
        return
    px, py, qx, qy = segment
    if radius > 0.0:
        _capsule_hits(ax, ay, dx, dy, px, py, qx, qy, radius, out)
    else:
        _segment_hits(ax, ay, dx, dy, px, py, qx, qy, out)


def _ring_hits(ax, ay, dx, dy, ring, out) -> None:
    count = len(ring)
    for number in range(count):
        (px, py), (qx, qy) = ring[number], ring[(number + 1) % count]
        _segment_hits(ax, ay, dx, dy, px, py, qx, qy, out)


def _meets_box(ax, ay, bx, by, box) -> bool:
    """Whether segment AB meets a box (grown by a hair): boundaries whose box it misses
    cannot cross it."""
    x0, y0, x1, y1 = box[0] - 1e-6, box[1] - 1e-6, box[2] + 1e-6, box[3] + 1e-6
    if max(ax, bx) < x0 or min(ax, bx) > x1 or max(ay, by) < y0 or min(ay, by) > y1:
        return False
    t0, t1 = 0.0, 1.0
    dx, dy = bx - ax, by - ay
    for p, q in ((-dx, ax - x0), (dx, x1 - ax), (-dy, ay - y0), (dy, y1 - ay)):
        if p == 0.0:
            if q < 0.0:
                return False
            continue
        r = q / p
        if p < 0.0:
            if r > t1:
                return False
            t0 = max(t0, r)
        else:
            if r < t0:
                return False
            t1 = min(t1, r)
    return True


def _grown(geometry, grow: float) -> tuple:
    segment, radius, polygon, arc = geometry
    return segment, radius + grow, polygon, arc


def _inside_box(box, x: float, y: float) -> bool:
    return box[0] <= x <= box[2] and box[1] <= y <= box[3]


def _ring_box(ring) -> tuple[float, float, float, float]:
    xs = [point[0] for point in ring]
    ys = [point[1] for point in ring]
    return min(xs), min(ys), max(xs), max(ys)


def _cells_along(x1, y1, x2, y2, cell: float):
    """Grid cells within one cell of a segment (samples a cell apart, each with its eight
    neighbours)."""
    floor = math.floor
    gx1, gx2 = sorted((floor(x1 / cell), floor(x2 / cell)))
    gy1, gy2 = sorted((floor(y1 / cell), floor(y2 / cell)))
    if gx2 - gx1 <= 1 and gy2 - gy1 <= 1:  # a short one: its box's cells and their neighbours
        return {(gx, gy) for gx in range(gx1 - 1, gx2 + 2) for gy in range(gy1 - 1, gy2 + 2)}
    steps = max(1, math.ceil(math.hypot(x2 - x1, y2 - y1) / cell))
    found = set()
    for step in range(steps + 1):
        gx = floor((x1 + (x2 - x1) * step / steps) / cell)
        gy = floor((y1 + (y2 - y1) * step / steps) / cell)
        for ox in (-1, 0, 1):
            for oy in (-1, 0, 1):
                found.add((gx + ox, gy + oy))
    return found


class Grid:
    """Uniform grid of numbered shapes by their boxes."""

    def __init__(self, cell: float):
        self.cell = cell
        self.cells: dict[tuple[int, int], list] = defaultdict(list)

    def add(self, box, value) -> None:
        cell, floor = self.cell, math.floor
        for gx in range(floor(box[0] / cell), floor(box[2] / cell) + 1):
            for gy in range(floor(box[1] / cell), floor(box[3] / cell) + 1):
                self.cells[(gx, gy)].append(value)

    def at(self, x: float, y: float):
        return self.cells.get((math.floor(x / self.cell), math.floor(y / self.cell)), ())

    def along(self, x1, y1, x2, y2) -> list:
        found = set()
        for key in _cells_along(x1, y1, x2, y2, self.cell):
            found.update(self.cells.get(key, ()))
        return sorted(found)


class _Edges:
    """Polygon ring edges in a grid (edges longer than a cell cut into parts), so a segment
    meets only the edges near it."""

    def __init__(self):
        self.grid = Grid(_CELL_MIL)
        self.edges: list[tuple[float, float, float, float]] = []

    def add_ring(self, ring) -> None:
        count = len(ring)
        for number in range(count):
            (px, py), (qx, qy) = ring[number], ring[(number + 1) % count]
            parts = max(1, math.ceil(math.hypot(qx - px, qy - py) / _CELL_MIL))
            for part in range(parts):
                x1 = px + (qx - px) * part / parts
                y1 = py + (qy - py) * part / parts
                x2 = qx if part + 1 == parts else px + (qx - px) * (part + 1) / parts
                y2 = qy if part + 1 == parts else py + (qy - py) * (part + 1) / parts
                if (x1, y1) == (x2, y2):
                    continue
                self.grid.add((min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)),
                              len(self.edges))
                self.edges.append((x1, y1, x2, y2))

    def hits(self, ax, ay, bx, by, out) -> None:
        dx, dy = bx - ax, by - ay
        edges = self.edges
        for number in self.grid.along(ax, ay, bx, by):
            x1, y1, x2, y2 = edges[number]
            if _meets_box(ax, ay, bx, by, (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))):
                _segment_hits(ax, ay, dx, dy, x1, y1, x2, y2, out)


# ---------- internal planes ----------


class _Plane:
    """One internal plane layer: its regions per net, voids and anti-pads."""

    kind = "plane"

    def __init__(self, owner: "ReferencePlanes", layer: int):
        index, pcb = owner.index, owner.pcb
        self.owner = owner
        self.layer = layer
        board = pcb.board
        self.net = (getattr(board, "plane_nets", None) or {}).get(layer)
        saved = (getattr(board, "plane_pullbacks", None) or {}).get(layer)
        self.pullback, self.pullback_basis = saved if saved else (DEFAULT_PULLBACK_MIL, "default")
        self.edge = owner.edge
        self.on_outline = self.edge.has_outline
        self.grid = Grid(_CELL_MIL)
        # (kind, box, shape, data): edge (piece geometry grown by the pullback), line
        # (geometry), void ((bands, rings)), hole (anti-pad geometry, hole record).
        self.shapes: list[tuple] = []
        self.counts: Counter = Counter()
        for piece in self.edge.pieces if self.on_outline else ():
            geometry = _grown(piece[0], self.pullback)
            box = PcbIndex._geometry_bounds(_grown(piece[0], max(self.pullback, EDGE_SNAP_MIL)))
            self._add("edge", box, geometry, piece[0])
        self._read_lines(index)
        self._read_voids(owner, layer)
        # Split plane polygons, smallest first (the innermost region holds a point).
        self.splits: list[tuple] = []
        for pour in pcb.pours:
            if pour.layer != layer or not _split_plane(pour):
                continue
            if pour.shelved:
                self.counts["split_planes_shelved"] += 1
                continue
            if len(pour.vertices) < 3:
                self.counts["split_planes_degenerate"] += 1
                continue
            ring = list(pour.vertices)
            self.splits.append((owner.label(pour.net), Bands([ring]), ring, _ring_box(ring),
                                polygon_area(ring)))
        self.splits.sort(key=lambda split: split[4])
        self.split_edges = _Edges()
        for split in self.splits:
            self.split_edges.add_ring(split[2])
        self._read_holes(owner)

    def _add(self, kind: str, box, shape, data=None) -> None:
        self.grid.add(box, len(self.shapes))
        self.shapes.append((kind, box, shape, data))

    def _read_lines(self, index) -> None:
        arcs = set()
        for row in index.db.execute(
            "SELECT * FROM prims WHERE layer = ? AND kind IN ('track', 'arc') "
            "AND pour_id IS NULL ORDER BY id", (self.layer,)
        ):
            geometry = index._copper_geometry(row, None)
            boundary = self.on_outline and all(
                self._near_edge(x, y, EDGE_SNAP_MIL) for x, y in (
                    (row["x1"], row["y1"]), (row["x2"], row["y2"]),
                    ((row["x1"] + row["x2"]) / 2.0, (row["y1"] + row["y2"]) / 2.0),
                )
            )
            self._add("line", PcbIndex._geometry_bounds(geometry), geometry, boundary)
            if row["kind"] == "arc" and row["source_id"] is not None:
                if (row["source_id"], boundary) in arcs:
                    continue
                arcs.add((row["source_id"], boundary))
            self.counts["boundary_lines" if boundary else "split_lines"] += 1

    def _read_voids(self, owner, layer: int) -> None:
        for region in owner.plane_regions:
            if region.layer != layer or region.keepout or region.board_cutout or region.kind:
                continue
            if region.polygon is not None and region.polygon != 0xFFFE:
                continue  # copper of a pour, not a drawn void
            points = shape_points(region.vertices)
            if len(points) < 3:
                self.counts["void_regions_degenerate"] += 1
                continue
            rings = [points, *(hole for hole in region.holes if len(hole) >= 3)]
            self._add("void", _ring_box(points), (Bands(rings), rings))
            self.counts["void_regions"] += 1
        for fill in owner.plane_fills:
            if fill.layer != layer or fill.keepout:
                continue
            outline = fill_outline(fill)
            if outline is None:
                continue
            self._add("void", _ring_box(outline), (Bands([outline]), [outline]))
            self.counts["void_fills"] += 1

    def _read_holes(self, owner) -> None:
        position = owner.position.get(self.layer)
        self.holes: list[tuple] = []  # (hole record, region label at its centre, connected)
        for record in owner.holes():
            low, high = record["span"] if record["span"] is not None else (None, None)
            if low is None or position is None:
                self.counts["holes_span_unknown"] += 1
                continue
            if not low <= position <= high:
                continue
            region = self.region(record["x"], record["y"])
            connected = self._connects(record, region)
            self.holes.append((record, region, connected))
            geometry = record["copper"](self.layer)
            antipad = _grown(geometry, record["clearance"])
            self._add("hole", PcbIndex._geometry_bounds(antipad), antipad, record)

    @staticmethod
    def _connects(record, label: str | None) -> bool:
        return (
            label is not None and label != NO_NET and record["net"] is not None
            and record["net"].casefold() == label.casefold() and record["style"] != "noconnect"
        )

    def _near_edge(self, x: float, y: float, reach: float) -> bool:
        probe = ((x, y, x, y), 0.0, None, None)
        for number in self.grid.at(x, y):
            shape_kind, _, _, piece = self.shapes[number]
            if shape_kind == "edge" and PcbIndex._geometry_distance(probe, piece) <= reach:
                return True
        return False

    def region(self, x: float, y: float) -> str | None:
        """The region's label at a point before voids: its split plane polygon's net, the
        plane's net on a plane without them, None outside every split plane polygon."""
        if not self.splits:
            return self.net or NO_NET
        for label, bands, _, box, _ in self.splits:
            if _inside_box(box, x, y) and bands.contains(x, y):
                return label
        return None

    def copper_at(self, x: float, y: float) -> str | None:
        label, _ = self._read(x, y)
        return label

    def _read(self, x: float, y: float) -> tuple[str | None, dict | None]:
        """(copper_at's answer, why there is no copper: None where there is)."""
        if self.on_outline and not self.edge.on_board(x, y):
            return None, {"cause": "off_board"}
        label = self.region(x, y)
        if label is None:
            return None, {"cause": "outside_split_planes"}
        for number in self.grid.at(x, y):
            shape_kind, box, shape, data = self.shapes[number]
            if not _inside_box(box, x, y):
                continue
            if shape_kind == "edge":
                if self.pullback > 0.0 and point_distance(x, y, shape) == 0.0:
                    return None, {"cause": "pullback",
                                  "approximate": self.pullback_basis == "default"}
            elif shape_kind == "line":
                if point_distance(x, y, shape) == 0.0:
                    return None, {"cause": "boundary_line" if data else "split_line"}
            elif shape_kind == "void":
                if shape[0].contains(x, y):
                    return None, {"cause": "drawn_void"}
            elif point_distance(x, y, shape) == 0.0 and not self._connects(data, label):
                return None, {"cause": "anti_pad", "approximate": True, "hole": data}
        return label, None

    def void_cause(self, x: float, y: float) -> dict | None:
        return self._read(x, y)[1]

    def source(self, x: float, y: float) -> str | None:
        """What the copper at a point is: a split plane polygon's region or the whole
        plane (assumed: split lines without split plane polygons do not partition it)."""
        if self.copper_at(x, y) is None:
            return None
        if self.splits:
            return "split_plane_polygon"
        return "whole_plane_split_lines_unpartitioned" if self.counts["split_lines"] else \
            "whole_plane"

    def area_nets(self) -> set[str]:
        """The nets of the plane's regions (its net without split plane polygons)."""
        if not self.splits:
            return {self.net or NO_NET} - {NO_NET}
        return {split[0] for split in self.splits} - {NO_NET}

    def crossings(self, ax, ay, bx, by) -> list[float]:
        dx, dy = bx - ax, by - ay
        out: list[float] = []
        self.split_edges.hits(ax, ay, bx, by, out)
        for number in self.grid.along(ax, ay, bx, by):
            shape_kind, box, shape, data = self.shapes[number]
            if not _meets_box(ax, ay, bx, by, box):
                continue
            if shape_kind == "void":
                for ring in shape[1]:
                    _ring_hits(ax, ay, dx, dy, ring, out)
                continue
            _geometry_hits(ax, ay, dx, dy, shape, out)
            if shape_kind == "edge":
                _geometry_hits(ax, ay, dx, dy, data, out)  # the edge itself
        return out

    def summary(self) -> dict:
        owner = self.owner
        if self.splits:
            regions = [
                {"net": label, "area_mm2": round(size / SQ_MIL_PER_MM2, 2),
                 "basis": "split plane polygon"}
                for label, _, _, _, size in sorted(self.splits, key=lambda split: -split[4])
            ]
        else:
            regions = [{"net": self.net or NO_NET,
                        "basis": "whole plane (no split plane polygons saved)"}]
        nets = sorted({region["net"] for region in regions}, key=str.casefold)
        connections: Counter = Counter()
        anti_pads: Counter = Counter()
        clearances: Counter = Counter()
        reliefs = []
        for record, _, connected in self.holes:
            if connected:
                connections[record["style"] or "unknown"] += 1
                relief = record["relief"]
                if record["style"] == "relief" and relief and relief not in reliefs:
                    reliefs.append(relief)
            else:
                anti_pads[record["kind"]] += 1
                clearances[(record["clearance"], record["clearance_basis"])] += 1
        out = {
            "layer": owner.layer_name(self.layer),
            "type": "plane",
            "net": self.net,
            "net_on_board": None if self.net is None else owner.index.net_id(self.net) is not None,
            "pullback_mil": round(self.pullback, 3),
            "pullback_basis": self.pullback_basis,
            "regions": regions[:REGIONS_LISTED],
            "region_count": len(regions),
            "nets": nets,
            "split_among_nets": len(nets) > 1,
            "split_lines": self.counts["split_lines"],
            "boundary_lines": self.counts["boundary_lines"],
            "voids": {"regions": self.counts["void_regions"], "fills": self.counts["void_fills"]},
            "connections": dict(sorted(connections.items())),
            "anti_pads": dict(sorted(anti_pads.items())),
            "anti_pad_clearances": [
                {"clearance_mil": round(value, 3), "basis": basis, "count": count}
                for (value, basis), count in sorted(clearances.items())
            ],
        }
        if reliefs:
            out["thermal_reliefs"] = reliefs[:5]
        if not self.on_outline:
            out["no_board_outline"] = True
        for key in ("split_planes_shelved", "split_planes_degenerate", "void_regions_degenerate",
                    "holes_span_unknown"):
            if self.counts[key]:
                out[key] = self.counts[key]
        return out


def _split_plane(pour) -> bool:
    return (getattr(pour, "polygon_type", None) or "").strip().lower() == "split plane"


# ---------- signal layers ----------


class _Hatch:
    """A hatched pour's copper inside its outline: its hatch tracks and arcs (items, the
    copper model's items of the pour) grown by reach (half an opening of its grid plus
    HATCH_JOIN_MIL)."""

    def __init__(self, owner: "ReferencePlanes", pour, reach: float, items: list):
        self.label = owner.label(pour.net)
        self.ring = list(pour.vertices)
        self.bands = Bands([self.ring])
        self.box = _ring_box(self.ring)
        self.reach = reach
        self.source = "hatched_pour_bridged" if reach > HATCH_JOIN_MIL + HATCH_OPEN_MIL / 2.0 \
            else "hatched_pour"
        self.geometries: list = []
        self.grown: list[tuple] = []  # (grown geometry, its box)
        self.grid = Grid(_CELL_MIL)
        for item in items:
            grown = _grown(item[6], reach)
            box = PcbIndex._geometry_bounds(grown)
            self.grid.add(box, len(self.geometries))
            self.geometries.append(item[6])
            self.grown.append((grown, box))
        self.edges = _Edges()
        self.edges.add_ring(self.ring)

    def covers(self, x: float, y: float) -> bool:
        if not _inside_box(self.box, x, y):
            return False
        near = self.grid.at(x, y)
        if not near or not self.bands.contains(x, y):
            return False
        reach, geometries = self.reach, self.geometries
        return any(point_distance(x, y, geometries[number]) <= reach for number in near)

    def crossings(self, ax, ay, bx, by, out) -> None:
        if not _meets_box(ax, ay, bx, by, self.box):
            return
        self.edges.hits(ax, ay, bx, by, out)
        dx, dy = bx - ax, by - ay
        for number in self.grid.along(ax, ay, bx, by):
            grown, box = self.grown[number]
            if _meets_box(ax, ay, bx, by, box):
                _geometry_hits(ax, ay, dx, dy, grown, out)


class _Signal:
    """One signal layer: its routing, pads, vias and area copper."""

    kind = "signal"

    def __init__(self, owner: "ReferencePlanes", layer: int):
        self.owner = owner
        self.layer = layer
        model = owner.model
        self.model = model
        outline_pours = owner.outline_pours
        self.items = [
            item for item in model.items.get(layer, ())
            if not (item[8] == "pour" and item[10] in outline_pours)
        ]
        self.item_grid = Grid(_CELL_MIL)
        for number, item in enumerate(self.items):
            self.item_grid.add(item[:4], number)
        self.areas = list(model.area_layers.get(layer, ()))
        # (label, bands, ring, box, kind) of the pours whose outline stands for their copper.
        self.outlines: list[tuple] = []
        for pour_id, kind in outline_pours.items():
            pour = owner.pcb.pours[pour_id]
            if pour.layer == layer:
                ring = list(pour.vertices)
                self.outlines.append((owner.label(pour.net), Bands([ring]), ring,
                                      _ring_box(ring), kind))
        self.area_grid = Grid(_AREA_GRID_MIL)
        for number, area in enumerate(self.areas):
            self.area_grid.add(area.bbox, ("area", number))
        self.outline_edges = _Edges()
        for number, outline in enumerate(self.outlines):
            self.area_grid.add(outline[3], ("outline", number))
            self.outline_edges.add_ring(outline[2])
        hatch_items: dict[int, list] = defaultdict(list)
        for item in self.items:
            if item[8] == "pour" and item[10] in owner.hatched_pours:
                hatch_items[item[10]].append(item)
        self.hatches = [
            _Hatch(owner, owner.pcb.pours[pour_id], reach, hatch_items[pour_id])
            for pour_id, reach in sorted(owner.hatched_pours.items())
            if owner.pcb.pours[pour_id].layer == layer
        ]
        self._copper: dict | None = None

    def copper_at(self, x: float, y: float) -> str | None:
        return self._read(x, y)[0]

    def _read(self, x: float, y: float) -> tuple[str | None, str | None]:
        """(copper_at's answer, what the copper is: routing, area or an outline kind)."""
        items = self.items
        hatched = self.owner.hatched_pours
        for number in self.item_grid.at(x, y):
            item = items[number]
            if item[0] <= x <= item[2] and item[1] <= y <= item[3] and \
                    point_distance(x, y, item[6]) == 0.0:
                return self.owner.label(item[4]), \
                    "hatched_pour" if item[8] == "pour" and item[10] in hatched else "routing"
        for kind, number in self.area_grid.at(x, y):
            if kind == "area":
                area = self.areas[number]
                if self.model.area_contains(area, x, y):
                    return self.owner.label(area.net), "area"
            else:
                label, bands, _, box, outline_kind = self.outlines[number]
                if _inside_box(box, x, y) and bands.contains(x, y):
                    return label, outline_kind
        for hatch in self.hatches:
            if hatch.covers(x, y):
                return hatch.label, hatch.source
        return None, None

    def void_cause(self, x: float, y: float) -> dict | None:
        label = self.copper_at(x, y)
        if label is None:
            return {"cause": "no_copper"}
        if label == NO_NET:
            return {"cause": "no_net_copper"}
        return None

    def source(self, x: float, y: float) -> str | None:
        return self._read(x, y)[1]

    def area_nets(self) -> set[str]:
        """The nets with area copper (pours, regions, fills) on the layer."""
        return set(self.copper_by_net()) - {NO_NET}

    def crossings(self, ax, ay, bx, by) -> list[float]:
        dx, dy = bx - ax, by - ay
        out: list[float] = []
        for number in self.item_grid.along(ax, ay, bx, by):
            item = self.items[number]
            if _meets_box(ax, ay, bx, by, item):
                _geometry_hits(ax, ay, dx, dy, item[6], out)
        if self.areas:
            grid = self.model.any_grid(self.layer)
            chunks = self.model.chunks.get(self.layer, ())
            seen = set()
            for key in _cells_along(ax, ay, bx, by, AREA_CELL_MIL):
                for number in grid.get(key, ()):
                    if number in seen:
                        continue
                    seen.add(number)
                    chunk = chunks[number]
                    if not _meets_box(ax, ay, bx, by, chunk):
                        continue
                    points = chunk[5]
                    for (px, py), (qx, qy) in zip(points, points[1:]):
                        _segment_hits(ax, ay, dx, dy, px, py, qx, qy, out)
        self.outline_edges.hits(ax, ay, bx, by, out)
        for hatch in self.hatches:
            hatch.crossings(ax, ay, bx, by, out)
        return out

    def copper_by_net(self) -> dict[str, dict]:
        """Area copper per net label: area (sq mil) and what it is made of."""
        if self._copper is None:
            copper: dict[str, dict] = defaultdict(lambda: {"area": 0.0, "sources": Counter()})
            for area in self.areas:
                size = polygon_area(area.rings[0]) - sum(
                    polygon_area(ring) for ring in area.rings[1:])
                entry = copper[self.owner.label(area.net)]
                entry["area"] += max(0.0, size)
                entry["sources"]["pour_regions" if area.pour is not None else
                                 "free_regions" if area.kind == "region" else "fills"] += 1
            for label, _, ring, _, kind in self.outlines:
                entry = copper[label]
                entry["area"] += polygon_area(ring)
                entry["sources"][kind] += 1
            for hatch in self.hatches:  # a hatch's area: its outline's
                entry = copper[hatch.label]
                entry["area"] += polygon_area(hatch.ring)
                entry["sources"]["hatched_pours"] += 1
            self._copper = dict(copper)
        return self._copper

    def largest_pour(self) -> tuple[str | None, float | None]:
        """(net label, share of the board) of the largest one-net area copper."""
        board = self.owner.board_area()
        copper = self.copper_by_net()
        if not copper or not board:
            return None, None
        label, entry = max(copper.items(), key=lambda item: item[1]["area"])
        return label, min(1.0, entry["area"] / board)

    def pour_nets(self) -> list[dict]:
        board = self.owner.board_area()
        if not board:
            return []
        rows = sorted(
            ((label, entry["area"] / board) for label, entry in self.copper_by_net().items()),
            key=lambda row: -row[1],
        )
        return [{"net": label, "share": round(min(1.0, share), 3)}
                for label, share in rows if share >= NET_SHARE_LISTED]

    def summary(self) -> dict:
        owner = self.owner
        board = owner.board_area()
        copper = sorted(self.copper_by_net().items(), key=lambda item: -item[1]["area"])
        label, share = self.largest_pour()
        sources: Counter = Counter()
        for _, entry in copper:
            sources.update(entry["sources"])
        sources.update(owner.pour_counts.get(self.layer, {}))
        out = {
            "layer": owner.layer_name(self.layer),
            "type": "signal",
            "kind": "pour" if share is not None and share >= LARGE_POUR_SHARE else "signal",
            "board_area_mm2": None if not board else round(board / SQ_MIL_PER_MM2, 2),
            "board_area_basis": owner.board_area_basis,
            "largest_pour": None if label is None else {"net": label, "share": round(share, 3)},
            "copper": [
                {"net": name, "area_mm2": round(entry["area"] / SQ_MIL_PER_MM2, 2),
                 **({"share": round(min(1.0, entry["area"] / board), 3)} if board else {})}
                for name, entry in copper[:NETS_LISTED]
            ],
            "nets_with_area_copper": len(copper),
            "sources": dict(sorted(sources.items())),
            "routing_objects": len(self.items),
        }
        return out


# ---------- the board ----------


class ReferencePlanes:
    """The reference copper model of one board, built per layer on first use."""

    def __init__(self, index):
        # Weak: the index holds this model (index.__dict__), as dfm_geometry's.
        self._index = weakref.ref(index)
        self.pcb = pcb = index.pcb
        self.order = index._copper_layers_in_stack_order()
        self.position = {layer: number for number, layer in enumerate(self.order)}
        self.model = copper_model(index)
        self.edge = self.model.edge
        self._layers: dict[int, _Plane | _Signal] = {}
        self._names: dict[int, str] = {}
        self._holes: list[dict] | None = None
        self._hole_grid: Grid | None = None
        self._resolvers: dict[tuple, ObjectResolver] = {}
        self._rules = {
            kind: sorted((rule for rule in pcb.rules if rule.kind == kind and rule.enabled),
                         key=lambda rule: rule.priority)
            for kind in ("PlaneClearance", "PlaneConnect")
        }
        self._board_area: float | None = None
        self.board_area_basis: str | None = None
        self._plane_streams: tuple | None = None
        # Pours whose outline stands for their copper: pour id -> what it is (a source key).
        self.outline_pours: dict[int, str] = {}
        # Hatched pours read from their hatch: pour id -> the reach that bridges its grid.
        self.hatched_pours: dict[int, float] = {}
        self.pour_counts: dict[int, Counter] = defaultdict(Counter)
        self._classify_pours(index)

    @property
    def index(self):
        return self._index()

    # ---------- helpers ----------

    def _classify_pours(self, index) -> None:
        pcb = self.pcb
        signal = set(index.signal_layers())
        poured = {area.pour_id for area in pcb.areas if area.pour_id is not None}
        hatched = {row[0] for row in index.db.execute(
            "SELECT DISTINCT poured_by FROM prims WHERE poured_by IS NOT NULL")}
        regions_read = "Regions6" in (getattr(pcb, "area_streams", ()) or ())
        for pour in pcb.pours:
            if pour.layer not in signal or _split_plane(pour):
                continue
            counts = self.pour_counts[pour.layer]
            if pour.shelved:
                counts["shelved_pours"] += 1
                continue
            style = (pour.hatch_style or "").strip().lower()
            if len(pour.vertices) < 3:
                continue
            if pour.pour_id in hatched:
                grid = getattr(pour, "grid_mil", None)
                width = getattr(pour, "track_width_mil", None)
                if style == "none":
                    counts["outline_only_pours"] += 1  # its border tracks are its copper
                elif grid and width and grid > 0.0 and width > 0.0:
                    self.hatched_pours[pour.pour_id] = \
                        max(0.0, grid - width) / 2.0 + HATCH_JOIN_MIL
                else:
                    self.outline_pours[pour.pour_id] = "hatched_pour_outlines"
            elif pour.pour_id in poured:
                continue
            elif style == "solid" and not regions_read:
                self.outline_pours[pour.pour_id] = "pour_outlines_assumed"
            else:
                counts["pours_without_copper"] += 1

    def label(self, net_id: int | None) -> str:
        nets = self.pcb.nets
        if net_id is None or not 0 <= net_id < len(nets):
            return NO_NET
        return nets[net_id]

    def layer_name(self, layer: int) -> str:
        if layer not in self._names:
            self._names[layer] = self.index.layer_name(layer)
        return self._names[layer]

    def layer_id(self, layer: int | str) -> int:
        """The v6 id of a copper layer of the stack, by id or name; ValueError otherwise."""
        resolved = layer if isinstance(layer, int) else self.index.resolve_layer(layer)
        if resolved not in self.position:
            raise ValueError(f"not a copper layer of the stack: {layer!r}")
        return resolved

    def _layer(self, layer: int | str):
        layer = self.layer_id(layer)
        if layer not in self._layers:
            self._layers[layer] = _Plane(self, layer) if 39 <= layer <= 54 else _Signal(self, layer)
        return self._layers[layer]

    @property
    def plane_regions(self):
        return self._plane_data()[0]

    @property
    def plane_fills(self):
        return self._plane_data()[1]

    def _plane_data(self) -> tuple:
        """(regions, fills) on the internal plane layers, decoded once."""
        if self._plane_streams is None:
            raw = self.pcb.raw or {}
            shape_based = "ShapeBasedRegions6/Data" in raw
            data = raw.get("ShapeBasedRegions6/Data") or raw.get("Regions6/Data") or b""
            regions, _ = decode_region_records(data, shape_based)
            fills, _ = decode_fill_records(raw.get("Fills6/Data") or b"")
            self._plane_streams = (
                [region for region in regions if 39 <= region.layer <= 54],
                [fill for fill in fills if 39 <= fill.layer <= 54],
            )
        return self._plane_streams

    def board_area(self) -> float | None:
        """The board's area (sq mil): its outline less its cutouts, else the box of its
        copper objects; None for a board with neither."""
        if self.board_area_basis is None:
            edge = self.edge
            if edge.has_outline:
                points = shape_points(self.pcb.board.outline_shape)
                size = polygon_area(points) - sum(
                    polygon_area(shape[2]) for shape in edge.cutout_shapes)
                self._board_area, self.board_area_basis = max(0.0, size), "outline"
            else:
                row = self.index.db.execute(
                    "SELECT MIN(minx), MIN(miny), MAX(maxx), MAX(maxy) FROM prims "
                    "WHERE pour_id IS NULL").fetchone()
                if row[0] is None:
                    self._board_area, self.board_area_basis = None, "none"
                else:
                    self._board_area = (row[2] - row[0]) * (row[3] - row[1])
                    self.board_area_basis = "copper box (no board outline)"
        return self._board_area

    def _rule(self, kind: str, net: str | None, facts: ObjectFacts):
        rules = self._rules.get(kind)
        if not rules:
            return None
        key = (kind, net)
        resolver = self._resolvers.get(key)
        if resolver is None:
            index = self.index
            target = index.rule_target(net, kind="net") if net else None
            if target is None:
                target = RuleTarget(kind="net", name=(net or "").lower(),
                                    nets={net.lower()} if net else set(), pairs=set())
            resolver = self._resolvers[key] = ObjectResolver(rules, target, index.rule_context)
        return resolver.resolve(facts)[0]

    def holes(self) -> list[dict]:
        """Every via, pad hole and bare hole with what a plane needs of it: its net, span
        (positions in the copper stack order), copper on a plane layer, the Power Plane
        Clearance and Connect Style that apply."""
        if self._holes is not None:
            return self._holes
        index, model = self.index, self.model
        rows = {row["id"]: row for row in index.db.execute(
            "SELECT * FROM prims WHERE kind IN ('via', 'pad') AND hole > 0")}
        holes = []
        for hole in model.holes:
            row = rows.get(hole.row) if hole.row is not None else None
            if row is not None:
                facts = index.object_facts(row)
                net = None if row["net_id"] is None else self.label(row["net_id"])
            else:
                facts = ObjectFacts(kind="pad", layer=MULTI_LAYER, span=frozenset(self.order),
                                    points=((hole.x, hole.y),), hole=hole.hole_mil)
                net = None if hole.net is None else self.label(hole.net)
            clearance_rule = self._rule("PlaneClearance", net, facts)
            clearance = None
            if clearance_rule is not None:
                properties = {key.upper(): value for key, value in clearance_rule.properties.items()}
                clearance = parse_dim_mil(properties.get("CLEARANCE"))
            style, relief = _connect_style(
                self._rule("PlaneConnect", net, facts), "VIA." if hole.kind == "via" else "PAD.")
            holes.append({
                "kind": hole.kind, "x": hole.x, "y": hole.y, "net": net, "span": hole.span,
                "row": hole.row, "component": hole.component,
                "clearance": clearance if clearance is not None else DEFAULT_PLANE_CLEARANCE_MIL,
                "clearance_basis": "rule" if clearance is not None else "default",
                "style": style, "relief": relief,
                "copper": _hole_copper(index, model, hole, row),
            })
        self._holes = holes
        return holes

    # ---------- the API ----------

    def planes(self) -> list[int]:
        """The internal plane layers of the stack, in stack order."""
        return [layer for layer in self.order if 39 <= layer <= 54]

    def copper_at(self, layer: int | str, x: float, y: float) -> str | None:
        """The net of the copper at (x, y) on a copper layer of the stack: its name, NO_NET
        for copper without a net, None where there is no copper."""
        return self._layer(layer).copper_at(float(x), float(y))

    def segment_coverage(self, layer: int | str, segment) -> list[tuple]:
        """(copper_at answer, start t, end t) intervals along a segment (x1, y1, x2, y2)
        on a layer, t from 0 at its start to 1 at its end, neighbours different."""
        model = self._layer(layer)
        x1, y1, x2, y2 = (float(value) for value in segment)
        length = math.hypot(x2 - x1, y2 - y1)
        if length < MIN_PIECE_MIL:
            return [(model.copper_at((x1 + x2) / 2.0, (y1 + y2) / 2.0), 0.0, 1.0)]
        pieces = _merged([list(piece) for piece in self.segment_pieces(layer, segment)])
        shortest = MIN_PIECE_MIL / length
        if len(pieces) > 1 and any(piece[2] - piece[1] < shortest for piece in pieces):
            kept: list[list] = []
            for number, piece in enumerate(pieces):
                if piece[2] - piece[1] >= shortest:
                    kept.append(piece)
                elif kept:
                    kept[-1][2] = piece[2]
                elif number + 1 < len(pieces):
                    pieces[number + 1][1] = piece[1]
            pieces = _merged(kept) or pieces[:1]
        pieces[0][1], pieces[-1][2] = 0.0, 1.0
        return [tuple(piece) for piece in pieces]

    def segment_pieces(self, layer: int | str, segment):
        """(copper_at answer, start t, end t) between consecutive boundary crossings along a
        segment, in order and unmerged, each read when it is reached: for a walk that stops
        at the first piece it wants (segment_coverage merges them)."""
        model = self._layer(layer)
        x1, y1, x2, y2 = (float(value) for value in segment)
        cuts = [0.0]
        for t in sorted(model.crossings(x1, y1, x2, y2)):
            if t - cuts[-1] > 1e-12:
                cuts.append(t)
        if 1.0 - cuts[-1] > 1e-12:
            cuts.append(1.0)
        else:
            cuts[-1] = 1.0
        for t0, t1 in zip(cuts, cuts[1:]):
            middle = (t0 + t1) / 2.0
            yield model.copper_at(x1 + (x2 - x1) * middle, y1 + (y2 - y1) * middle), t0, t1

    def plane_partition(self, layer: int | str) -> dict:
        """A layer's copper per net and how it was read (see _Plane.summary and
        _Signal.summary)."""
        return self._layer(layer).summary()

    def void_cause(self, layer: int | str, x: float, y: float) -> dict | None:
        """Why a point of a layer has no copper with a net, or None where it has: cause
        off_board, outside_split_planes, pullback, split_line, boundary_line, drawn_void
        or anti_pad (with its hole record) on a plane; no_copper on a signal layer;
        no_net_copper on both. approximate: True where the model assumed the geometry."""
        return self._layer(layer).void_cause(float(x), float(y))

    def copper_source(self, layer: int | str, x: float, y: float) -> str | None:
        """What the copper at a point is, None without copper: on a signal layer routing,
        area, hatched_pour (its hatch, or a hatch as wide as its grid), hatched_pour_bridged,
        hatched_pour_outlines or pour_outlines_assumed (the last three assumed); on a plane
        split_plane_polygon, whole_plane or whole_plane_split_lines_unpartitioned
        (assumed)."""
        return self._layer(layer).source(float(x), float(y))

    def area_nets(self, layer: int | str) -> set[str]:
        """The nets of a layer's area copper: a plane's region nets, a signal layer's
        pour, region and fill nets (any size), NO_NET left out."""
        return self._layer(layer).area_nets()

    def holes_near(self, x: float, y: float, reach: float) -> list[dict]:
        """holes() records whose centre lies within reach plus their largest copper extent
        of (x, y): a superset for a distance test to their copper."""
        if self._hole_grid is None:
            grid = Grid(_HOLE_CELL_MIL)
            for number, record in enumerate(self.holes()):
                extent = hole_extent(record, self.order)
                record["extent"] = extent
                grid.add((record["x"] - extent, record["y"] - extent,
                          record["x"] + extent, record["y"] + extent), number)
            self._hole_grid = grid
        grid, holes = self._hole_grid, self._holes
        found = set()
        cell = grid.cell
        for gx in range(math.floor((x - reach) / cell), math.floor((x + reach) / cell) + 1):
            for gy in range(math.floor((y - reach) / cell), math.floor((y + reach) / cell) + 1):
                found.update(grid.cells.get((gx, gy), ()))
        return [holes[number] for number in sorted(found)
                if math.hypot(holes[number]["x"] - x, holes[number]["y"] - y)
                <= reach + holes[number]["extent"]]

    def reference_layers(self) -> list[dict]:
        """Per signal layer in stack order: its stack neighbours above and below, its
        references (the impedance profiles', else the neighbours) and their basis."""
        index = self.index
        board = self.pcb.board
        v7_to_v6 = {layer.v7_id: layer.v6_id for layer in board.layers
                    if layer.kind == "copper" and layer.v7_id is not None
                    and layer.v6_id in self.position}
        profiles: dict[int, list[dict]] = defaultdict(list)
        for profile in getattr(board, "impedance_profiles", None) or []:
            for entry in profile.layers:
                layer = v7_to_v6.get(entry.layer_v7)
                if not entry.enabled or layer is None:
                    continue
                profiles[layer].append({
                    "profile": profile.name,
                    "above": v7_to_v6.get(entry.ref_top_v7),
                    "below": v7_to_v6.get(entry.ref_bottom_v7),
                })
        result = []
        for layer in index.signal_layers():
            position = self.position[layer]
            above = self.order[position - 1] if position > 0 else None
            below = self.order[position + 1] if position + 1 < len(self.order) else None
            entry = {
                "layer": self.layer_name(layer),
                "above": None if above is None else self._reference(layer, above),
                "below": None if below is None else self._reference(layer, below),
            }
            named = []
            listed = []
            for row in profiles.get(layer, ()):
                listed.append({
                    "profile": row["profile"],
                    "above": None if row["above"] is None else self.layer_name(row["above"]),
                    "below": None if row["below"] is None else self.layer_name(row["below"]),
                })
                for side in ("above", "below"):
                    if row[side] is not None and row[side] != layer and row[side] not in named:
                        named.append(row[side])
            if named:
                entry["basis"] = "impedance_profile"
                entry["profiles"] = listed
                references = [self._reference(layer, other) for other in
                              sorted(named, key=lambda other: self.position[other])]
            else:
                entry["basis"] = "stackup_neighbours"
                references = [entry[side] for side in ("above", "below") if entry[side]]
            entry["references"] = references
            entry["has_reference_copper"] = any(
                reference["kind"] in ("plane", "pour") for reference in references)
            result.append(entry)
        return result

    def _reference(self, layer: int, other: int) -> dict:
        """What a copper layer is as a reference of a signal layer, and the dielectric
        between them."""
        dielectric, between = self._between(layer, other)
        out = {
            "layer": self.layer_name(other),
            "side": "above" if self.position[other] < self.position[layer] else "below",
            "dielectric_mil": None if dielectric is None else round(dielectric, 3),
        }
        if between:
            out["copper_layers_between"] = between
        model = self._layer(other)
        if model.kind == "plane":
            regions = [model.net or NO_NET] if not model.splits else \
                [split[0] for split in model.splits]
            out["kind"] = "plane"
            out["nets"] = sorted(set(regions), key=str.casefold)
        else:
            label, share = model.largest_pour()
            out["kind"] = "pour" if share is not None and share >= LARGE_POUR_SHARE else "signal"
            out["largest_pour"] = None if label is None else {"net": label,
                                                              "share": round(share, 3)}
            if out["kind"] == "pour":
                out["nets"] = [row["net"] for row in model.pour_nets()]
        return out

    def _between(self, layer: int, other: int) -> tuple[float | None, int]:
        """(dielectric height between two copper layers, copper layers between them)."""
        rows = self.index.db.execute(
            "SELECT v6_id, kind, diel_height_mil, stack_order FROM layers "
            "WHERE stack_order IS NOT NULL ORDER BY stack_order").fetchall()
        places = {row["v6_id"]: number for number, row in enumerate(rows)
                  if row["kind"] == "copper" and row["v6_id"] is not None}
        if layer not in places or other not in places:
            return None, 0
        low, high = sorted((places[layer], places[other]))
        middle = rows[low + 1:high]
        between = sum(1 for row in middle if row["kind"] == "copper")
        dielectrics = [row for row in middle if row["kind"] == "dielectric"]
        if not dielectrics or any(row["diel_height_mil"] is None for row in dielectrics):
            return None, between
        return sum(row["diel_height_mil"] for row in dielectrics), between


def _merged(pieces: list[list]) -> list[list]:
    out: list[list] = []
    for label, t0, t1 in pieces:
        if out and out[-1][0] == label:
            out[-1][2] = t1
        else:
            out.append([label, t0, t1])
    return out


def _connect_style(rule, prefix: str) -> tuple[str | None, dict | None]:
    """(style: relief | direct | noconnect | None, relief settings) of a Power Plane Connect
    Style rule for a via (prefix VIA.) or pad (PAD.): the object kind's own keys when the
    rule sets them, else its plain ones."""
    if rule is None:
        return None, None
    properties = {key.upper(): value for key, value in rule.properties.items()}

    def value(key: str):
        return properties.get(prefix + key) or properties.get(key)

    raw = value("PLANECONNECTSTYLE")
    style = raw.strip().lower().replace(" ", "") if raw else None
    relief = None
    if style == "relief":
        relief = {
            "air_gap_mil": parse_dim_mil(value("RELIEFAIRGAP")),
            "conductor_width_mil": parse_dim_mil(value("RELIEFCONDUCTORWIDTH")),
            "expansion_mil": parse_dim_mil(value("RELIEFEXPANSION")),
            "entries": (value("RELIEFENTRIES") or "").strip() or None,
        }
    return style, relief


def _hole_copper(index, model, hole, row):
    """layer -> the hole's copper on a plane layer as a PcbIndex geometry: a via's disc of
    its diameter, a pad's inner-layer copper (its hole's wall where the pad has no copper
    there), a bare hole's wall."""
    if hole.kind == "via" and row is not None:
        disc = ((hole.x, hole.y, hole.x, hole.y), (row["width"] or hole.hole_mil) / 2.0, None, None)
        return lambda layer: disc
    if hole.kind == "pad" and row is not None:
        primitive = index.pcb.primitives[hole.row - 1]
        copper_map = layer_copper(row)
        cache: dict = {}

        def pad(layer):
            copper = pad_copper_on(copper_map, layer) if copper_map else None
            key = id(copper) if copper is not None else None
            if key not in cache:
                geometry = model.pad_geometry(index, row, copper, primitive)
                cache[key] = geometry if geometry is not None else hole.geometry
            return cache[key]

        return pad
    return lambda layer: hole.geometry


def geometry_extent(geometry, x: float, y: float) -> float:
    """The largest distance from (x, y) to a PcbIndex geometry's copper (no arcs)."""
    segment, radius, polygon, _ = geometry
    points = polygon if polygon is not None else (segment[:2], segment[2:])
    return max(math.hypot(px - x, py - y) for px, py in points) + radius


def hole_extent(record: dict, order) -> float:
    """A holes() record's largest copper extent from its centre over the stack's layers."""
    seen: dict[int, float] = {}
    for layer in order:
        geometry = record["copper"](layer)
        key = id(geometry)
        if key not in seen:
            seen[key] = geometry_extent(geometry, record["x"], record["y"])
    return max(seen.values(), default=0.0)


# ---------- module API ----------


def reference_planes(index) -> ReferencePlanes:
    """The board's reference copper model, cached on the index."""
    cached = index.__dict__.get("_reference_planes")
    if cached is None:
        cached = ReferencePlanes(index)
        index.__dict__["_reference_planes"] = cached
    return cached


def reference_layers(index) -> list[dict]:
    return reference_planes(index).reference_layers()


def copper_at(index, layer: int | str, x: float, y: float) -> str | None:
    return reference_planes(index).copper_at(layer, x, y)


def segment_coverage(index, layer: int | str, segment) -> list[tuple]:
    return reference_planes(index).segment_coverage(layer, segment)


def plane_partition(index, layer: int | str) -> dict:
    return reference_planes(index).plane_partition(layer)
