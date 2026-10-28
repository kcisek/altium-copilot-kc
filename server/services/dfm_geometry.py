"""Copper geometry for check_dfm: copper spacing, hole to hole, copper to board edge and
the walls of holes without copper around them (copper to the wall, the wall to the edge)
over a PcbIndex. The engine holds no fabrication limits: the caller passes each distance.
check_hv_clearance uses the same model through net_gaps (the copper of chosen nets
against every other net's, each pair within its own reach) and openings_near.

Pure logic, no MCP. Lengths are mils and coordinates absolute (the caller makes them
origin-relative). What does not depend on a distance is built once per index, when a pass
first needs it, and cached on it (PcbIndex._dfm_geometry): the per-layer copper items, the
area ring chunks, the board edge and the holes. The model holds the index weakly, so a
replaced index is freed as soon as nothing else holds it. Results are cached per distance
as well: a complete result of a larger distance serves every smaller one (filtered), so a
few are kept, not one per distance.

Copper is what the fabricator etches, per signal layer: tracks and arcs on the layer, a
pad's copper on the layer (pad_copper_on; an octagonal pad is an octagon whose corners are
cut by a quarter of its short side), a via's pad on the layer (ViaStack.pad_diameter) or
its barrel where the pad is removed, and area copper (PcbData.areas: solid-pour islands,
free regions, fills). A pour's outline rows are not copper. A non-plated pad whose copper
is no larger than its hole has none on that layer: its hole wall is measured instead, as
are bare holes' (hole_to_copper, hole_to_edge). A plated pad keeps its barrel there.
Shelved pours have no copper (the parser drops it). A pour modified since it was poured
(copper_invalid) is measured as saved and its records are marked.

Owners: a hatched pour's tracks and arcs are one object, ('pour', poured_by); an arc's
chord rows are one, ('arc', source_id); each area is one, ('area', index in pcb.areas);
every other row is ('prim', id). Results hold one record per owner pair (spacing) or per
owner (copper to edge), with the smallest distance and where it occurs.

Spacing skips copper of one net, no-net against no-net copper (counted) and net ties
(ComponentKind 3 or 4): a pair where one side belongs to the part and the other belongs to
it too or carries one of its pads' nets (counted per part). It keeps, and marks, records
of copper_invalid pours, pads of one footprint, no-net footprint copper touching a netted
pad of its own part, and touching copper.

The passes follow the measured prototypes of the check_dfm design: per-layer grids of the
copper items with their geometry cached, a pair kept only in the cell holding the low
corner of the overlap of its grown boxes; area rings cut into runs of CHUNK_EDGES edges in
a shared grid; round copper against area edges measured as capsules; area against area
only in cells holding chunks of two areas. Copper wholly inside other-net area copper
(no area edge near it) is found by containment: objects inside free regions, fills and
stale pours (a valid pour clears other nets' copper when it is poured: testing it costs
more than the rest, VALID_POUR_ITEMS), and areas inside any area by a sweep over their
boxes. Copper to edge runs edge first: each piece of the outline or a cutout queries the
R*Tree and the chunk grid. Work is bounded: past `cap` work units (exact distances, box
comparisons of the containment sweep) a pass stops and says so.
"""

import math
import weakref
from bisect import bisect_left
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace

try:
    from parsers.pcb_doc import (
        MULTI_LAYER,
        PAD_SHAPE_OCTAGONAL,
        pad_copper_on,
        read_board_cutouts,
    )
    from services.connectivity import _closest_on, _closest_pair, _toward
    from services.pcb_compliance import CLEARANCE_TOLERANCE_MIL, EPS_MIL
    from services.pcb_geometry import (
        in_arc_span,
        oriented_rect_vertices,
        point_arc_distance,
        point_polygon_distance,
        point_seg_distance,
        seg_arc_distance,
        seg_seg_distance,
    )
    from services.pcb_health import SNAP_MIL, contour
    from services.pcb_index import PcbIndex, layer_copper
except ImportError:
    from server.parsers.pcb_doc import (
        MULTI_LAYER,
        PAD_SHAPE_OCTAGONAL,
        pad_copper_on,
        read_board_cutouts,
    )
    from server.services.connectivity import _closest_on, _closest_pair, _toward
    from server.services.pcb_compliance import CLEARANCE_TOLERANCE_MIL, EPS_MIL
    from server.services.pcb_geometry import (
        in_arc_span,
        oriented_rect_vertices,
        point_arc_distance,
        point_polygon_distance,
        point_seg_distance,
        seg_arc_distance,
        seg_seg_distance,
    )
    from server.services.pcb_health import SNAP_MIL, contour
    from server.services.pcb_index import PcbIndex, layer_copper

# Copper this close touches (gap 0 within the grid slack of a geometric comparison).
TOUCH_MIL = CLEARANCE_TOLERANCE_MIL
# Work units per pass before it stops (the reach drives the cost: MiniPC takes about
# 0.3M exact distances at 0.127 mm).
MAX_DISTANCE_CALLS = 3_000_000
# Item grid cell (at least 4x the reach), and the area chunk grid shared by every pass.
# Area chunks are runs of at most CHUNK_EDGES ring edges and CHUNK_SPAN_MIL across. On
# MiniPC 80-mil cells take the time of 40-mil ones (2.0 s) and keep 97 MB, not 120.
ITEM_CELL_MIL = 64.0
AREA_CELL_MIL = 80.0
CHUNK_EDGES = 16
CHUNK_SPAN_MIL = AREA_CELL_MIL
# Chunks enter the area grid grown by this much, so one grid serves any spacing reach up
# to twice it (a larger reach builds another; a layer keeps this one and the last).
AREA_GROW_MIL = 5.0
BAND_MIL = 10.0  # y band of the point-in-polygon indexes
# An area with more ring points answers a few points from the chunk grid, not an index.
_BANDS_MAX_POINTS = 2000
# Board edge pieces longer than this are split, so their R*Tree boxes stay small.
EDGE_PIECE_MIL = 100.0
# Edge-connector fingers (gold fingers of a card edge): at least FINGER_PADS SMD pads of
# one part on an outer layer along one edge, each at least FINGER_MIN_LENGTH_MIL long and
# FINGER_ASPECT times as long as wide, the long axis within FINGER_ANGLE_DEG of
# perpendicular to the edge, and at least FINGER_SHARE of all the part's pads such
# fingers within the reach. An IC or a connector placed at the edge fails one of these:
# its pads stay edge records.
FINGER_PADS = 5
FINGER_ANGLE_DEG = 20.0
FINGER_MIN_LENGTH_MIL = 1.5 / 0.0254
FINGER_ASPECT = 2.0
FINGER_SHARE = 0.9
# An octagonal pad (shape 3) is its rectangle with the corners cut by this share of the
# short side, rotated with the pad (KiCad's Altium importer: chamfer ratio 0.25).
OCTAGON_CHAMFER = 0.25
# Objects of other nets wholly inside a valid pour's copper: one point in polygon per
# object in the pour's box costs about 0.55 s on MiniPC (128k objects in 202 pour
# islands), more than the rest of spacing, so it is off; SpacingResult says so
# (valid_pour_items_checked). Areas inside a valid pour are always tested.
VALID_POUR_ITEMS = False
# As pcb_doc.shape_points: an arc edge of a smaller sweep is a straight edge.
_MIN_SWEEP_DEG = 0.2
# Default reaches of board_minimums when the caller has no limit of its own.
DEFAULT_GAP_REACH_MIL = 0.127 / 0.0254
DEFAULT_EDGE_REACH_MIL = 1.0 / 0.0254
DEFAULT_HOLE_REACH_MIL = 0.5 / 0.0254
_RESULTS_KEPT = 4
_NET_TIE_KINDS = (3, 4)
_GEO = PcbIndex._geometry_distance
_BOUNDS = PcbIndex._geometry_bounds
_ITEM_SQL = (
    "SELECT id, kind, net_id, layer, layer_to, component, poured_by, x1, y1, x2, y2, "
    "width, height, rotation, shape, corner_radius, layer_copper, source_id, hole, "
    "arc_x, arc_y, arc_radius, minx, miny, maxx, maxy FROM prims "
    "WHERE kind IN ('track', 'arc', 'via', 'pad') AND pour_id IS NULL ORDER BY id"
)
# CROSS JOIN keeps SQLite on the R*Tree (see via_checks._near).
_RTREE_SQL = (
    "SELECT p.id FROM prims_rtree r CROSS JOIN prims p ON p.id = r.id "
    "WHERE r.minx <= ? AND r.maxx >= ? AND r.miny <= ? AND r.maxy >= ? "
    "AND p.pour_id IS NULL AND p.kind IN ('track', 'arc', 'via', 'pad')"
)
_BOX_SQL = (
    "SELECT p.id FROM prims p "
    "WHERE p.minx <= ? AND p.maxx >= ? AND p.miny <= ? AND p.maxy >= ? "
    "AND p.pour_id IS NULL AND p.kind IN ('track', 'arc', 'via', 'pad')"
)

# A copper item is a tuple (one per object and signal layer; layers with the same copper
# share it): min x, min y, max x, max y (its copper's box on the layer), net, owner,
# geometry (PcbIndex's (segment, radius, polygon, arc)), prims row id, kind, component,
# pour. kind is track, arc, via, pad, or pour for a hatched pour's copper. Area copper
# uses the same layout with no geometry and no row (_AreaCopper.side).


# ---------- results ----------


@dataclass
class Copper:
    """One side of a record: a copper owner and the object of it that is closest."""

    owner: tuple  # ('prim', id) | ('arc', source_id) | ('pour', pour index) | ('area', i)
    kind: str  # track | arc | via | pad | pour | region | fill (pour: pour copper)
    net: int | None
    component: int | None
    pour: int | None = None  # Polygons6 index of the pour it is copper of
    row: int | None = None  # prims row id (index.pcb.primitives[row - 1]); None for areas
    area: int | None = None  # index into pcb.areas for area copper

    @property
    def is_area(self) -> bool:
        """Area copper: a pour's (solid or hatched), a free region or a fill."""
        return self.kind in ("pour", "region", "fill")


@dataclass
class GapRecord:
    """Copper of two owners closer than the reach on one or more signal layers."""

    first: Copper
    second: Copper
    gap_mil: float  # edge to edge, 0 when they touch or one lies inside the other
    layer: int  # the layer of the smallest gap
    layers: tuple[int, ...]  # every layer where they are closer than the reach
    # touching: gap 0; same_footprint: two pads of one part; footprint_copper: no-net
    # copper of a part touching one of its netted pads; stale_pour: copper of a pour
    # modified since it was poured (its pours in stale_pours).
    marks: frozenset[str] = frozenset()
    stale_pours: tuple[int, ...] = ()
    # The gap on each of `layers` when they differ (None: gap_mil on every one), so a
    # result for a smaller reach can drop the layers past it.
    _layer_gaps: tuple | None = field(default=None, repr=False, compare=False)
    _geometries: tuple = field(default=(), repr=False, compare=False)
    _at: tuple | None = field(default=None, repr=False, compare=False)

    @property
    def at(self) -> tuple[float, float]:
        """Where the two coppers meet or come closest (computed on first use)."""
        if self._at is None:
            self._at = _meeting_point(*self._geometries, self.gap_mil)
        return self._at

    @property
    def ends(self) -> tuple[tuple[float, float], tuple[float, float]]:
        """The point of each copper nearest the other (first's, second's): the closest
        points of their cores, arcs on the true arc (unlike `at`, which takes an arc's
        chord), past each one's copper; one point twice when they touch (where first's
        copper meets second's). Their midpoint is where the gap is."""
        first, second = self._geometries
        distance, on_first, on_second = nearest_true_cores(first, second)
        if distance == 0.0:
            return on_first, on_first
        if self.gap_mil <= 0.0:
            point = _toward(on_first, on_second, first[1])
            return point, point
        return (_toward(on_first, on_second, first[1]),
                _toward(on_second, on_first, second[1]))


@dataclass
class SpacingResult:
    reach_mil: float
    records: list[GapRecord]  # smallest gap first
    no_net_pairs: int  # no-net against no-net owner pairs closer than the reach (skipped)
    net_tie_pairs: dict[int, int]  # component -> owner pairs skipped as its net tie
    distance_calls: int
    stopped: bool  # the cap was reached: the records are incomplete
    # False: objects of other nets wholly inside a valid pour's copper were not looked
    # for (VALID_POUR_ITEMS); areas inside a valid pour and objects inside free regions,
    # fills and stale pours were.
    valid_pour_items_checked: bool = False
    _state: tuple | None = field(default=None, repr=False, compare=False)

    def minimum(self) -> GapRecord | None:
        """The smallest gap the fabricator must etch: touching copper, stale pour
        copper and footprint copper touching its pad left out."""
        return next(
            (
                record for record in self.records
                if not record.marks & {"touching", "stale_pour", "footprint_copper"}
            ),
            None,
        )


@dataclass
class EdgeRecord:
    """Copper of one owner closer than the reach to the board edge."""

    copper: Copper
    distance_mil: float  # 0 when the copper reaches or crosses the edge
    layer: int
    layers: tuple[int, ...]  # the layers with that copper
    edge: str  # outline | cutout
    cutout: int | None = None  # PcbRegion.index of the cutout
    cutout_component: int | None = None  # the part owning the cutout (its own edge)
    marks: frozenset[str] = frozenset()  # crosses (distance 0), stale_pour
    _geometries: tuple = field(default=(), repr=False, compare=False)
    _at: tuple | None = field(default=None, repr=False, compare=False)

    @property
    def at(self) -> tuple[float, float]:
        """The point of the copper nearest the edge (computed on first use)."""
        if self._at is None:
            self._at = _near_point(*self._geometries)
        return self._at


@dataclass
class OffBoardRecord:
    """Copper of one owner wholly outside the board (or inside a board cutout)."""

    copper: Copper
    layers: tuple[int, ...]
    objects: int  # its pieces off the board (a hatched pour's tracks, ...)
    inside_cutout: bool
    at: tuple[float, float]


@dataclass
class FingerGroup:
    """SMD pads of one part along one board edge, long axis across it: an edge
    connector's fingers, reported as a group instead of per pad."""

    component: int
    pads: list[Copper]  # nearest first
    distance_mil: float  # the nearest pad's
    layer: int  # the nearest pad's
    layers: tuple[int, ...]
    edge: str  # the nearest pad's edge: outline | cutout
    cutout: int | None
    at: tuple[float, float]  # the nearest pad's centre


@dataclass
class Hole:
    """A drilled hole: a via's, a pad's or a bare hole's (PcbData.bare_holes)."""

    kind: str  # via | pad | bare_hole
    x: float
    y: float
    hole_mil: float  # diameter, or slot / square hole width
    plated: bool | None
    hole_shape: int = 0  # 0 round, 1 square, 2 slot
    slot_length_mil: float | None = None
    angle: float = 0.0  # absolute angle of a slot or square hole (pad rotation + slot)
    row: int | None = None  # prims row id of the via or pad
    bare: int | None = None  # index into pcb.bare_holes
    component: int | None = None
    net: int | None = None
    span: tuple[int, int] | None = None  # stack positions drilled; None: unknown
    hole_offset: bool = False  # offset from the pad centre (position not decoded)

    @property
    def geometry(self) -> tuple:
        """The hole as a PcbIndex geometry: a circle, a capsule (slot) or a polygon."""
        return _hole_geometry(
            self.x, self.y, self.hole_mil, self.hole_shape, self.slot_length_mil, self.angle
        )


@dataclass
class EdgeHole:
    """A plated hole cut by the board edge: an edge-plated or castellated hole."""

    hole: Hole
    copper: Copper | None  # its pad or via; None for a bare hole
    edge: str
    cutout: int | None
    at: tuple[float, float]


@dataclass
class EdgeResult:
    reach_mil: float
    has_outline: bool  # False: no outline, nothing measured
    cutouts: int
    footprint_cutouts: int
    records: list[EdgeRecord]  # smallest distance first; fingers and edge holes left out
    off_board: list[OffBoardRecord]
    fingers: list[FingerGroup]
    edge_holes: list[EdgeHole]
    distance_calls: int
    stopped: bool
    # Board cutouts with no edge of non-zero length (every vertex on one spot): not
    # part of the edge, and not counted in `cutouts`.
    degenerate_cutouts: int = 0
    _state: tuple | None = field(default=None, repr=False, compare=False)

    def minimum(self) -> "EdgeRecord | FingerGroup | None":
        """The copper nearest the edge that the fabricator must keep clear: fingers
        counted; stale pour copper, edge-plated holes and off-board copper not."""
        candidates: list = [record for record in self.records if "stale_pour" not in record.marks]
        candidates += self.fingers
        return min(candidates, key=lambda found: found.distance_mil, default=None)


@dataclass
class HolePair:
    first: Hole
    second: Hole
    gap_mil: float  # edge to edge, 0 when they overlap
    # touching: overlapping holes; coincident: the same hole drilled twice;
    # hole_offset: a pad hole whose offset from its pad is not decoded.
    marks: frozenset[str] = frozenset()

    @property
    def pair_class(self) -> str:
        """'via' when both holes are vias (the via limit applies), else 'pad'."""
        return "via" if self.first.kind == self.second.kind == "via" else "pad"

    @property
    def at(self) -> tuple[float, float]:
        return _meeting_point(self.first.geometry, self.second.geometry, self.gap_mil)


@dataclass
class HoleResult:
    reach_mil: float
    holes: int
    records: list[HolePair]  # smallest gap first
    distance_calls: int
    stopped: bool
    _state: object = field(default=None, repr=False, compare=False)

    def minimum(self) -> HolePair | None:
        """The smallest gap between two holes that do not overlap."""
        return next(
            (record for record in self.records if "touching" not in record.marks), None
        )


@dataclass
class HoleCopperRecord:
    """Copper of one owner closer than the reach to a hole wall (Model.walls) on one or
    more signal layers."""

    hole: Hole
    copper: Copper
    gap_mil: float  # wall to copper, 0 when the copper touches, crosses or covers the hole
    layer: int  # the layer of the smallest gap
    layers: tuple[int, ...]  # every layer where the copper is closer than the reach
    # touching: gap 0 (the drill cuts copper); same_part: copper of the hole's own part;
    # stale_pour: copper of a pour modified since it was poured; hole_offset: a pad hole
    # whose offset from its pad is not decoded (its position is approximate).
    marks: frozenset[str] = frozenset()
    _layer_gaps: tuple | None = field(default=None, repr=False, compare=False)
    _geometries: tuple = field(default=(), repr=False, compare=False)
    _at: tuple | None = field(default=None, repr=False, compare=False)

    @property
    def at(self) -> tuple[float, float]:
        """Where the copper meets the wall or comes closest (computed on first use)."""
        if self._at is None:
            self._at = _meeting_point(*self._geometries, self.gap_mil)
        return self._at


@dataclass
class HoleCopperResult:
    reach_mil: float
    walls: int  # holes measured (Model.walls)
    records: list[HoleCopperRecord]  # smallest gap first
    distance_calls: int
    stopped: bool
    _state: object = field(default=None, repr=False, compare=False)

    def minimum(self) -> HoleCopperRecord | None:
        """The smallest wall-to-copper gap the fabricator must keep: touching copper (the
        drill cuts it) and stale pour copper left out."""
        return next(
            (record for record in self.records if not record.marks & {"touching", "stale_pour"}),
            None,
        )


@dataclass
class HoleEdgeRecord:
    """A hole without copper of its own around it (a non-plated pad's hole, a bare hole)
    closer than the reach to the board edge."""

    hole: Hole
    distance_mil: float  # wall to edge, 0 when the hole reaches or crosses the edge
    edge: str  # outline | cutout
    cutout: int | None = None
    cutout_component: int | None = None
    marks: frozenset[str] = frozenset()  # crosses (distance 0), hole_offset
    _geometries: tuple = field(default=(), repr=False, compare=False)
    _at: tuple | None = field(default=None, repr=False, compare=False)

    @property
    def at(self) -> tuple[float, float]:
        """The point of the hole's wall nearest the edge (computed on first use)."""
        if self._at is None:
            self._at = _near_point(*self._geometries)
        return self._at


@dataclass
class HoleEdgeResult:
    reach_mil: float
    has_outline: bool
    records: list[HoleEdgeRecord]  # smallest distance first
    distance_calls: int
    stopped: bool
    _state: object = field(default=None, repr=False, compare=False)

    def minimum(self) -> HoleEdgeRecord | None:
        return self.records[0] if self.records else None


# ---------- geometry helpers ----------


def _hole_geometry(x, y, hole, shape, slot_length, angle) -> tuple:
    """A hole as a PcbIndex geometry: a square hole (shape 1) a rectangle of its length
    by its width, a slot (shape 2) a capsule along `angle`, else a circle."""
    radius = hole / 2.0
    length = max(slot_length or 0.0, hole)
    if shape == 1:
        return None, 0.0, oriented_rect_vertices(x, y, length, hole, angle), None
    if shape == 2 and length > hole:
        half = (length - hole) / 2.0
        dx = math.cos(math.radians(angle)) * half
        dy = math.sin(math.radians(angle)) * half
        return (x - dx, y - dy, x + dx, y + dy), radius, None, None
    return (x, y, x, y), radius, None, None


def _octagon(x: float, y: float, width: float, height: float, rotation: float) -> list:
    """The 8 corners of an octagonal pad: its rectangle with each corner cut by
    OCTAGON_CHAMFER of the short side along both sides, rotated with the pad."""
    cut = OCTAGON_CHAMFER * min(width, height)
    hw, hh = width / 2.0, height / 2.0
    local = (
        (-hw + cut, -hh), (hw - cut, -hh), (hw, -hh + cut), (hw, hh - cut),
        (hw - cut, hh), (-hw + cut, hh), (-hw, hh - cut), (-hw, -hh + cut),
    )
    cosine, sine = math.cos(math.radians(rotation)), math.sin(math.radians(rotation))
    return [(x + lx * cosine - ly * sine, y + lx * sine + ly * cosine) for lx, ly in local]


def _core(geometry) -> list[tuple]:
    """The segments a geometry grows from: its centreline, an arc's chord, or a
    polygon's edges."""
    segment, _, polygon, arc = geometry
    if polygon is not None:
        closed = list(polygon) + [polygon[0]]
        return [(a[0], a[1], b[0], b[1]) for a, b in zip(closed, closed[1:])]
    if arc is not None:
        return [(arc[3], arc[4], arc[5], arc[6])]
    return [segment]


def _nearest_cores(first, second) -> tuple[float, tuple, tuple]:
    """(distance, point on first, point on second) of two geometries' nearest cores."""
    return min(
        (_closest_pair(a, b) for a in _core(first) for b in _core(second)),
        key=lambda item: item[0],
    )


def _meeting_point(first, second, gap: float) -> tuple[float, float]:
    """Where two coppers meet or come closest: from the closest points of their cores,
    past the first one's copper by half the gap (arcs measured on their chords)."""
    distance, on_first, on_second = _nearest_cores(first, second)
    if distance == 0.0:
        return on_first
    return _toward(on_first, on_second, first[1] + gap / 2.0)


def _near_point(copper, edge) -> tuple[float, float]:
    """The point of copper nearest an edge piece (arcs measured on their chords)."""
    _, on_copper, on_edge = _nearest_cores(copper, edge)
    return _toward(on_copper, on_edge, copper[1])


def _on_arc(px: float, py: float, arc: tuple) -> tuple[float, float]:
    """The point of an arc piece (pcb_geometry's) nearest a point."""
    cx, cy, radius, x1, y1, x2, y2 = arc
    if in_arc_span(px, py, arc):
        apart = math.hypot(px - cx, py - cy)
        if apart:
            return cx + (px - cx) * radius / apart, cy + (py - cy) * radius / apart
        return x1, y1
    if math.hypot(px - x1, py - y1) <= math.hypot(px - x2, py - y2):
        return x1, y1
    return x2, y2


def _on_piece(px: float, py: float, piece: tuple) -> tuple[float, float]:
    return _on_arc(px, py, piece) if len(piece) == 7 else _closest_on(px, py, *piece)


def _closest_pieces(a: tuple, b: tuple) -> tuple[float, tuple, tuple]:
    """(distance, point on a, point on b) of two core pieces, each a segment (x1, y1,
    x2, y2) or an arc piece (cx, cy, r, x1, y1, x2, y2), on the true arc. The candidates
    are pcb_geometry's seg_arc_distance and arc_arc_distance ones: an end of either,
    the foot of the perpendicular from an arc's centre, the line through two centres,
    and crossings."""
    if len(a) == 4 and len(b) == 4:
        return _closest_pair(a, b)
    if len(a) == 4:
        distance, on_b, on_a = _closest_pieces(b, a)
        return distance, on_a, on_b
    ends_b = ((b[0], b[1]), (b[2], b[3])) if len(b) == 4 else ((b[3], b[4]), (b[5], b[6]))
    candidates = []
    for point in ((a[3], a[4]), (a[5], a[6])):
        other = _on_piece(*point, b)
        candidates.append((math.dist(point, other), point, other))
    for point in ends_b:
        other = _on_arc(*point, a)
        candidates.append((math.dist(other, point), other, point))
    cx, cy, radius = a[0], a[1], a[2]
    if len(b) == 4:
        x1, y1, x2, y2 = b
        dx, dy = x2 - x1, y2 - y1
        length_squared = dx * dx + dy * dy
        if length_squared > 0:
            fx, fy = x1 - cx, y1 - cy
            along = -(fx * dx + fy * dy) / length_squared
            if 0 < along < 1:
                foot = (x1 + along * dx, y1 + along * dy)
                on = _on_arc(*foot, a)
                candidates.append((math.dist(on, foot), on, foot))
            discriminant = along * along - (fx * fx + fy * fy - radius * radius) / (
                length_squared)
            if discriminant >= 0:
                root = math.sqrt(discriminant)
                for t in (along - root, along + root):
                    point = (x1 + t * dx, y1 + t * dy)
                    if 0 <= t <= 1 and in_arc_span(*point, a):
                        return 0.0, point, point
    else:
        c2x, c2y, radius_b = b[0], b[1], b[2]
        dx, dy = c2x - cx, c2y - cy
        apart = math.hypot(dx, dy)
        if apart > 0:
            ux, uy = dx / apart, dy / apart
            for sign in (1.0, -1.0):
                point = (cx + sign * radius * ux, cy + sign * radius * uy)
                if in_arc_span(*point, a):
                    other = _on_arc(*point, b)
                    candidates.append((math.dist(point, other), point, other))
                point = (c2x + sign * radius_b * ux, c2y + sign * radius_b * uy)
                if in_arc_span(*point, b):
                    other = _on_arc(*point, a)
                    candidates.append((math.dist(other, point), other, point))
            if abs(radius - radius_b) <= apart <= radius + radius_b:
                along = (radius * radius - radius_b * radius_b + apart * apart) / (2.0 * apart)
                height = math.sqrt(max(0.0, radius * radius - along * along))
                for sign in (1.0, -1.0):
                    point = (cx + along * ux - sign * height * uy,
                             cy + along * uy + sign * height * ux)
                    if in_arc_span(*point, a) and in_arc_span(*point, b):
                        return 0.0, point, point
    return min(candidates, key=lambda item: item[0])


def _true_core(geometry) -> list[tuple]:
    """_core with an arc as its arc piece, not its chord."""
    arc = geometry[3]
    return [arc] if arc is not None and geometry[2] is None else _core(geometry)


def nearest_true_cores(first, second) -> tuple[float, tuple, tuple]:
    """_nearest_cores on the true arcs."""
    return min(
        (_closest_pieces(a, b) for a in _true_core(first) for b in _true_core(second)),
        key=lambda item: item[0],
    )


def point_distance(px: float, py: float, geometry) -> float:
    """Distance from a point to the copper (0 inside it)."""
    segment, radius, polygon, arc = geometry
    if arc is not None:
        distance = point_arc_distance(px, py, arc)
    elif polygon is not None:
        distance = point_polygon_distance(px, py, polygon)
    else:
        distance = point_seg_distance(px, py, *segment)
    return max(0.0, distance - radius)


def _ref_point(geometry) -> tuple[float, float]:
    """A point of the copper (its centreline's start, an arc's first end, a polygon
    corner)."""
    segment, _, polygon, arc = geometry
    if polygon is not None:
        return polygon[0]
    if arc is not None:
        return arc[3], arc[4]
    return segment[0], segment[1]


def _point(x: float, y: float) -> tuple:
    return (x, y, x, y), 0.0, None, None


def _cells(x0: float, y0: float, x1: float, y1: float, size: float):
    floor = math.floor
    return (
        range(floor(x0 / size), floor(x1 / size) + 1),
        range(floor(y0 / size), floor(y1 / size) + 1),
    )


def _least(store: dict, key, value: float) -> None:
    """Keep the smallest value per key."""
    if value < store.get(key, math.inf):
        store[key] = value


def _layer_split(gaps: dict, gap: float, position: dict) -> tuple[tuple, tuple | None]:
    """(layers in stack order, their gaps when they differ from `gap`, else None) of a
    record's per-layer gaps."""
    layers = tuple(sorted(gaps, key=position.get))
    if all(value == gap for value in gaps.values()):
        return layers, None
    return layers, tuple(gaps[layer] for layer in layers)


def _narrow_layers(record, cut: float):
    """A record (GapRecord, HoleCopperRecord) of a wider result for a smaller reach:
    itself, or a copy without the layers whose gap is not below `cut`."""
    gaps = record._layer_gaps
    if gaps is None or max(gaps) < cut:
        return record
    kept = [(layer, gap) for layer, gap in zip(record.layers, gaps) if gap < cut]
    spread = tuple(gap for _, gap in kept)
    return replace(
        record, layers=tuple(layer for layer, _ in kept),
        _layer_gaps=None if all(gap == record.gap_mil for gap in spread) else spread,
    )


class Bands:
    """Even-odd point in polygon over several rings, edges indexed by y band."""

    def __init__(self, rings, band: float = BAND_MIL):
        self.band = band
        self.edges: list[tuple[float, float, float, float]] = []
        self.bands: dict[int, list[int]] = defaultdict(list)
        floor = math.floor
        for ring in rings:
            count = len(ring)
            for number in range(count):
                (x1, y1), (x2, y2) = ring[number], ring[(number + 1) % count]
                if y1 == y2:
                    continue
                edge = len(self.edges)
                self.edges.append((x1, y1, x2, y2))
                for key in range(floor(min(y1, y2) / band), floor(max(y1, y2) / band) + 1):
                    self.bands[key].append(edge)

    def contains(self, x: float, y: float) -> bool:
        inside = False
        edges = self.edges
        for number in self.bands.get(math.floor(y / self.band), ()):
            x1, y1, x2, y2 = edges[number]
            if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
                inside = not inside
        return inside


class _AreaCopper:
    """One area of pcb.areas on a signal layer."""

    __slots__ = ("number", "layer", "net", "kind", "pour", "component", "stale", "bbox",
                 "rings", "points", "side", "_bands")

    def __init__(self, number: int, area, stale: bool):
        self.number = number
        self.layer = area.layer
        self.net = area.net
        self.kind = "pour" if area.pour_id is not None else area.kind
        self.pour = area.pour_id
        self.component = area.component
        self.stale = stale
        self.rings = [list(area.outline), *(list(hole) for hole in area.holes if len(hole) >= 3)]
        self.points = sum(len(ring) for ring in self.rings)
        xs = [point[0] for point in area.outline]
        ys = [point[1] for point in area.outline]
        self.bbox = (min(xs), min(ys), max(xs), max(ys))
        self.side = (*self.bbox, self.net, ("area", number), None, None, self.kind,
                     self.component, self.pour)
        self._bands = None

    @property
    def checks_containment(self) -> bool:
        """Free regions, fills and stale pour copper may lie over other copper; a
        valid pour's copper does not (it is poured around other nets' copper)."""
        return self.pour is None or self.stale

    def contains(self, x: float, y: float) -> bool:
        minx, miny, maxx, maxy = self.bbox
        if not (minx <= x <= maxx and miny <= y <= maxy):
            return False
        if self._bands is None:
            self._bands = Bands(self.rings)
        return self._bands.contains(x, y)


# ---------- the cached model ----------


class _Model:
    """Everything that does not depend on a distance, built once per index when a pass
    first needs it: the copper items (spacing, edge, hole walls), the area chunks, the
    board edge, the holes (holes() alone builds no copper)."""

    def __init__(self, index):
        # Weak: index._dfm_geometry holds the model, and a reference back would keep a
        # replaced index (and this model) alive until a full garbage collection.
        self._index = weakref.ref(index)
        self.pcb = pcb = index.pcb
        self.order = index._copper_layers_in_stack_order()
        self.signal = set(index.signal_layers())
        self.position = {layer: number for number, layer in enumerate(self.order)}
        self.stack_signal = [layer for layer in self.order if layer in self.signal]
        self.outer = {self.stack_signal[0], self.stack_signal[-1]} if self.stack_signal else set()
        self.stale_pours = {
            pour.pour_id for pour in pcb.pours if pour.copper_invalid and not pour.shelved
        }
        self._ties: dict[int, set[int]] | None = None
        self._items: dict[int, list[tuple]] | None = None
        # prims row id -> [(geometry, item, layers)], one entry per distinct copper.
        self._row_items: dict[int, list[tuple]] = {}
        # How far an item's copper reaches past its row's box in the index (a via's pad
        # larger than its nominal diameter, a plated pad's barrel past its copper): the
        # edge and wall passes grow their R*Tree queries by it.
        self._overhang = 0.0
        self._areas: list[_AreaCopper] | None = None
        self._area_layers: dict[int, list[_AreaCopper]] = defaultdict(list)
        self._chunks: dict[int, list[tuple]] = defaultdict(list)
        self._grids: dict[tuple, tuple] = {}
        self._edge = None
        self._holes: list[Hole] | None = None
        self._walls: list[tuple] | None = None
        self._part_pads: Counter | None = None
        self.results: dict[str, dict] = defaultdict(dict)

    @property
    def index(self):
        return self._index()

    @property
    def ties(self) -> dict[int, set[int]]:
        """Net-tie part -> the nets of its pads."""
        if self._ties is None:
            ties: dict[int, set[int]] = {
                component.index: set() for component in self.pcb.components
                if component.kind in _NET_TIE_KINDS
            }
            for primitive in self.pcb.primitives:
                if primitive.kind == "pad" and primitive.component in ties and (
                    primitive.net is not None
                ):
                    ties[primitive.component].add(primitive.net)
            self._ties = ties
        return self._ties

    @property
    def items(self) -> dict[int, list[tuple]]:
        if self._items is None:
            self._build_items()
        return self._items

    @property
    def row_items(self) -> dict[int, list[tuple]]:
        if self._items is None:
            self._build_items()
        return self._row_items

    @property
    def overhang(self) -> float:
        if self._items is None:
            self._build_items()
        return self._overhang

    @property
    def areas(self) -> list[_AreaCopper]:
        if self._areas is None:
            self._build_areas()
        return self._areas

    @property
    def area_layers(self) -> dict[int, list[_AreaCopper]]:
        if self._areas is None:
            self._build_areas()
        return self._area_layers

    @property
    def chunks(self) -> dict[int, list[tuple]]:
        if self._areas is None:
            self._build_areas()
        return self._chunks

    @property
    def part_pads(self) -> Counter:
        """Part -> its pad count."""
        if self._part_pads is None:
            self._part_pads = Counter(
                primitive.component for primitive in self.pcb.primitives
                if primitive.kind == "pad" and primitive.component is not None
            )
        return self._part_pads

    def _build_items(self) -> None:
        index = self.index
        items: dict[int, list[tuple]] = defaultdict(list)
        row_items: dict[int, list[tuple]] = {}
        primitives = self.pcb.primitives
        signal, position = self.signal, self.position
        stack_signal = self.stack_signal
        overhang = 0.0
        for row in index.db.execute(_ITEM_SQL):
            kind, layer, number = row["kind"], row["layer"], row["id"]
            poured = row["poured_by"]
            if poured is not None:
                owner, label = ("pour", poured), "pour"
            elif kind == "arc" and row["source_id"] is not None:
                owner, label = ("arc", row["source_id"]), kind
            else:
                owner, label = ("prim", number), kind
            base = (row["net_id"], owner, number, label, row["component"], poured)
            # copper key -> [geometry, item, layers]: layers with the same copper share it.
            groups: dict = {}
            if kind == "via":
                low, high = position.get(layer), position.get(row["layer_to"])
                if low is None or high is None:
                    layers = [x for x in (layer, row["layer_to"]) if x in signal]
                else:
                    low, high = sorted((low, high))
                    layers = [x for x in stack_signal if low <= position[x] <= high]
                stack, width = primitives[number - 1].via, row["width"]
                for layer_ in layers:
                    size = stack.pad_diameter(layer_, width) if stack is not None else width
                    # Where the pad is removed the barrel is the via's copper.
                    size = size or row["hole"]
                    if not size:
                        continue
                    centre = (row["x1"], row["y1"], row["x1"], row["y1"])
                    _group(groups, size, (centre, size / 2.0, None, None), layer_, base, items)
            elif kind == "pad":
                if layer == MULTI_LAYER:
                    layers = stack_signal
                else:
                    layers = [layer] if layer in signal else []
                copper_map = layer_copper(row)
                primitive = primitives[number - 1] if (row["hole"] or 0.0) > 0 else None
                bare: set = set()  # copper keys of layers with no copper
                for layer_ in layers:
                    copper = pad_copper_on(copper_map, layer_) if copper_map else None
                    key = id(copper) if copper is not None else None
                    if key in bare:
                        continue
                    geometry = None
                    if key not in groups:
                        geometry = self.pad_geometry(index, row, copper, primitive)
                        if geometry is None:
                            bare.add(key)
                            continue
                    _group(groups, key, geometry, layer_, base, items)
            elif layer in signal:
                _group(groups, None, index._copper_geometry(row, None), layer, base, items)
            if groups:
                row_items[number] = [tuple(group) for group in groups.values()]
                for _, item, _ in row_items[number]:
                    overhang = max(overhang, row["minx"] - item[0], row["miny"] - item[1],
                                   item[2] - row["maxx"], item[3] - row["maxy"])
        self._items, self._row_items, self._overhang = items, row_items, overhang

    @staticmethod
    def pad_geometry(index, row, copper, primitive):
        """A pad's copper on a layer (copper None: its main copper). Where the copper is
        no larger than the hole, a plated hole's barrel, or None (no copper: the hole's
        wall, Model.walls). An octagonal pad is an octagon."""
        if copper is not None:
            width, height, shape = copper.width, copper.height, copper.shape
        else:
            width, height, shape = row["width"], row["height"] or row["width"], row["shape"]
        if primitive is not None:
            hole = row["hole"]
            if max(width, height) <= hole + EPS_MIL:
                if primitive.plated is False:
                    return None
                return _hole_geometry(
                    row["x1"], row["y1"], hole, primitive.hole_shape, primitive.slot_length,
                    (primitive.rotation or 0.0) + (primitive.slot_rotation or 0.0),
                )
        if shape == PAD_SHAPE_OCTAGONAL and width > 0 and height > 0:
            return None, 0.0, _octagon(row["x1"], row["y1"], width, height,
                                       row["rotation"] or 0.0), None
        return index._copper_geometry(row, copper)

    def _build_areas(self) -> None:
        self._areas = []
        for number, area in enumerate(self.pcb.areas):
            if area.layer not in self.signal or len(area.outline) < 3:
                continue
            info = _AreaCopper(number, area, area.pour_id in self.stale_pours)
            self._areas.append(info)
            self._area_layers[info.layer].append(info)
            chunks = self._chunks[info.layer]
            for ring in info.rings:
                for x0, y0, x1, y1, run in _ring_chunks(ring):
                    chunks.append((x0, y0, x1, y1, info, run))

    def grid(self, layer: int, grow: float) -> tuple[dict, set]:
        """(cell -> chunk numbers, cells holding chunks of two or more areas) of a
        layer's area chunks, each in the AREA_CELL_MIL cells its box grown by `grow`
        covers. A layer keeps the grid of the default grow and of the last other one."""
        key = (layer, grow)
        if key not in self._grids:
            for old in [old for old in self._grids
                        if old[0] == layer and old[1] not in (AREA_GROW_MIL, grow)]:
                del self._grids[old]
            grid: dict[tuple[int, int], list[int]] = defaultdict(list)
            first: dict[tuple[int, int], object] = {}
            multi: set[tuple[int, int]] = set()
            size = AREA_CELL_MIL
            for number, chunk in enumerate(self.chunks.get(layer, ())):
                info = chunk[4]
                xs, ys = _cells(chunk[0] - grow, chunk[1] - grow, chunk[2] + grow,
                                chunk[3] + grow, size)
                for gx in xs:
                    for gy in ys:
                        cell = (gx, gy)
                        grid[cell].append(number)
                        if first.setdefault(cell, info) is not info:
                            multi.add(cell)
            self._grids[key] = (grid, multi)
        return self._grids[key]

    def any_grid(self, layer: int) -> dict:
        """A chunk grid of the layer, any grow (a query grown by its reach finds every
        chunk within it): an existing one, else the default."""
        for (grid_layer, _), (grid, _) in self._grids.items():
            if grid_layer == layer:
                return grid
        return self.grid(layer, AREA_GROW_MIL)[0]

    def area_contains(self, area: _AreaCopper, x: float, y: float) -> bool:
        """Is (x, y) in the area's copper (even-odd over its rings)? For a few points of a
        large area: a small one (or one with its band index built) asks the index; a
        large one counts the ring edges its chunks in the grid row hold toward the nearer
        side of its box, so a big pour builds no index (tens of MB on MiniPC)."""
        minx, miny, maxx, maxy = area.bbox
        if not (minx <= x <= maxx and miny <= y <= maxy):
            return False
        if area._bands is not None or area.points <= _BANDS_MAX_POINTS:
            return area.contains(x, y)
        floor, size = math.floor, AREA_CELL_MIL
        grid, chunks = self.any_grid(area.layer), self.chunks[area.layer]
        right = maxx - x <= x - minx
        first, last = (x, maxx) if right else (minx, x)
        row = floor(y / size)
        seen: set[int] = set()
        inside = False
        for gx in range(floor(first / size), floor(last / size) + 1):
            for number in grid.get((gx, row), ()):
                if number in seen:
                    continue
                seen.add(number)
                cx0, cy0, cx1, cy1, owner, points = chunks[number]
                if owner is not area or cy0 > y or cy1 < y or cx1 < first or cx0 > last:
                    continue
                px, py = points[0]
                for qx, qy in points[1:]:
                    if (py > y) != (qy > y):
                        cross = px + (y - py) * (qx - px) / (qy - py)
                        if (x < cross) if right else (cross < x):
                            inside = not inside
                    px, py = qx, qy
        return inside

    @property
    def edge(self) -> "_Edge":
        if self._edge is None:
            self._edge = _Edge(self.pcb)
        return self._edge

    @property
    def holes(self) -> list[Hole]:
        if self._holes is None:
            self._holes = self._build_holes()
        return self._holes

    @property
    def walls(self) -> list[tuple[Hole, tuple[int, ...]]]:
        """(hole, signal layers in stack order) of the hole walls no copper of the hole
        covers: a non-plated pad's hole on the layers where the pad's copper is no larger
        than the hole, and bare holes (plated or not, their plating is no ring) on every
        layer."""
        if self._walls is None:
            walls = []
            primitives = self.pcb.primitives
            for hole in self.holes:
                if hole.kind == "bare_hole":
                    layers = tuple(self.stack_signal)
                elif hole.kind == "pad" and hole.plated is False:
                    layers = self._bare_pad_layers(primitives[hole.row - 1])
                else:
                    continue
                if layers:
                    walls.append((hole, layers))
            self._walls = walls
        return self._walls

    def _bare_pad_layers(self, primitive) -> tuple[int, ...]:
        """The signal layers where a pad's copper is no larger than its hole."""
        if primitive.layer == MULTI_LAYER:
            layers = self.stack_signal
        else:
            layers = [primitive.layer] if primitive.layer in self.signal else []
        copper_map = layer_copper(primitive)
        bare = []
        for layer in layers:
            copper = pad_copper_on(copper_map, layer) if copper_map else None
            size = (max(copper.width, copper.height) if copper is not None
                    else max(primitive.width, primitive.height or primitive.width))
            if size <= primitive.hole + EPS_MIL:
                bare.append(layer)
        return tuple(bare)

    def _build_holes(self) -> list[Hole]:
        holes: list[Hole] = []
        position, last = self.position, len(self.order) - 1
        through = (0, last) if last >= 0 else None
        for number, primitive in enumerate(self.pcb.primitives, start=1):
            hole = primitive.hole or 0.0
            if hole <= 0 or primitive.pour_id is not None:
                continue
            if primitive.kind == "via":
                low, high = position.get(primitive.layer), position.get(primitive.layer_to)
                span = tuple(sorted((low, high))) if low is not None and high is not None else None
                holes.append(Hole(
                    "via", primitive.x1, primitive.y1, hole, True, row=number,
                    component=primitive.component, net=primitive.net, span=span,
                ))
            elif primitive.kind == "pad":
                holes.append(Hole(
                    "pad", primitive.x1, primitive.y1, hole, primitive.plated,
                    hole_shape=primitive.hole_shape if primitive.hole_shape in (1, 2) else 0,
                    slot_length_mil=primitive.slot_length,
                    angle=(primitive.rotation or 0.0) + (primitive.slot_rotation or 0.0),
                    row=number, component=primitive.component, net=primitive.net,
                    span=through, hole_offset=primitive.hole_offset,
                ))
        for number, bare in enumerate(getattr(self.pcb, "bare_holes", None) or []):
            if bare.hole <= 0:
                continue
            holes.append(Hole(
                "bare_hole", bare.x, bare.y, bare.hole, bare.plated,
                hole_shape=bare.hole_shape if bare.hole_shape in (1, 2) else 0,
                slot_length_mil=bare.slot_length,
                angle=(bare.rotation or 0.0) + (bare.slot_rotation or 0.0),
                bare=number, component=bare.component, net=bare.net, span=through,
                hole_offset=bare.hole_offset,
            ))
        return holes

    def cached(self, name: str, reach: float, cap: int, compute, narrow=None):
        """The result of pass `name` for (reach, cap). A complete computed result (one
        with a _state) of a larger reach whose work fits the cap serves a smaller reach
        through narrow(result, reach); a new complete computed result replaces those of
        smaller reaches. At most _RESULTS_KEPT per name, the widest complete one kept
        longest. The same call returns the same object while it is kept."""
        store = self.results[name]
        key = (round(reach, 6), cap)
        if key in store:
            return store[key]
        base = None
        if narrow is not None:
            for (wide, _), result in store.items():
                if result._state is not None and not result.stopped and wide >= key[0] \
                        and result.distance_calls <= cap and (base is None or wide < base[0]):
                    base = (wide, result)
        if base is not None:
            result = narrow(base[1], reach)
        else:
            result = compute()
            if result._state is not None and not result.stopped:
                for old in [old for old, kept in store.items()
                            if old[0] <= key[0] and not kept.stopped]:
                    del store[old]
        while len(store) >= _RESULTS_KEPT:
            complete = [old for old, kept in store.items()
                        if kept._state is not None and not kept.stopped]
            widest = max(complete, key=lambda old: old[0], default=None)
            victim = next((old for old in store if old != widest), None)
            if victim is None:
                break
            del store[victim]
        store[key] = result
        return result


def _ring_chunks(ring) -> list[tuple]:
    """A closed ring as runs of points (min x, min y, max x, max y, points): at most
    CHUNK_EDGES edges, at most CHUNK_SPAN_MIL across. A longer edge is split into parts
    of at most one cell (the same copper edge), so no chunk covers many cells."""
    chunks = []
    run = [ring[0]]
    x0 = x1 = ring[0][0]
    y0 = y1 = ring[0][1]
    cell, span, most = AREA_CELL_MIL, CHUNK_SPAN_MIL, CHUNK_EDGES
    count = len(ring)
    for number in range(count):
        px, py = ring[number]
        following = ring[(number + 1) % count]  # the ring's own vertex, not a copy
        qx, qy = following
        dx, dy = qx - px, qy - py
        if dx * dx + dy * dy <= cell * cell:
            points = (following,)
        else:
            steps = math.ceil(math.hypot(dx, dy) / cell)
            points = [(px + dx * step / steps, py + dy * step / steps) for step in range(1, steps)]
            points.append(following)
        for point in points:
            x, y = point
            if x == px and y == py:
                continue
            nx0 = x if x < x0 else x0
            nx1 = x if x > x1 else x1
            ny0 = y if y < y0 else y0
            ny1 = y if y > y1 else y1
            if len(run) > most or nx1 - nx0 > span or ny1 - ny0 > span:
                chunks.append((x0, y0, x1, y1, run))
                lx, ly = run[-1]
                run = [run[-1]]
                nx0, nx1 = (x, lx) if x < lx else (lx, x)
                ny0, ny1 = (y, ly) if y < ly else (ly, y)
            run.append(point)
            x0, x1, y0, y1 = nx0, nx1, ny0, ny1
            px, py = x, y
    if len(run) > 1:
        chunks.append((x0, y0, x1, y1, run))
    return chunks


def _group(groups: dict, key, geometry, layer: int, base: tuple, items: dict) -> None:
    """Add a row's copper on a layer: a new item for new copper (geometry given), the
    row's earlier item for copper it already has on another layer."""
    group = groups.get(key)
    if group is None:
        box = _BOUNDS(geometry)
        net, owner, number, label, component, pour = base
        item = (box[0], box[1], box[2], box[3], net, owner, geometry, number, label,
                component, pour)
        group = groups[key] = [geometry, item, []]
    group[2].append(layer)
    items[layer].append(group[1])


def copper_model(index) -> _Model:
    """The index's cached model (built on first use): per-layer copper items, area copper,
    the board edge and the holes, shared by check_dfm, its assembly checks and
    check_hv_clearance."""
    model = getattr(index, "_dfm_geometry", None)
    if model is None:
        model = index._dfm_geometry = _Model(index)
    return model


def _net_tie(ties: dict[int, set[int]], first, second) -> int | None:
    """The net-tie part (Model.ties) two copper sides belong to as its tie: one side is
    the part's own copper and the other is too or carries one of its pads' nets."""
    for mine, other in ((first, second), (second, first)):
        nets = ties.get(mine[9])
        if nets is not None and (
            other[9] == mine[9] or (other[4] is not None and other[4] in nets)
        ):
            return mine[9]
    return None


def _copper(side) -> Copper:
    owner = side[5]
    return Copper(
        owner=owner,
        kind=side[8],
        net=side[4],
        component=side[9],
        pour=side[10],
        row=side[7],
        area=owner[1] if owner[0] == "area" else None,
    )


# ---------- spacing ----------


class _Spacing:
    """One spacing run: pairs found so far and what was skipped."""

    def __init__(self, model: _Model, reach: float, cap: int):
        self.model = model
        self.reach = reach
        self.limit = reach - CLEARANCE_TOLERANCE_MIL
        self.cap = cap
        self.calls = 0
        self.stopped = False
        # (owner, owner) -> [gap, layer, {layer: gap}, first side, second side, geometries]
        self.pairs: dict[tuple, list] = {}
        # Skipped pairs with their smallest gap (a smaller reach counts those below it).
        self.no_net: dict[tuple, float] = {}
        self.tie_pairs: dict[int, dict] = defaultdict(dict)
        self.item_grids: dict[int, tuple] = {}

    def _tie(self, first, second) -> int | None:
        return _net_tie(self.model.ties, first, second)

    def found(self, first, second, layer: int, gap: float, first_geometry, second_geometry):
        """first and second are closer than the limit on layer."""
        a, b = first[5], second[5]
        key = (a, b) if a <= b else (b, a)
        if first[4] is None and second[4] is None:
            _least(self.no_net, key, gap)
            return
        ties = self.model.ties
        if ties and (first[9] in ties or second[9] in ties):
            component = self._tie(first, second)
            if component is not None:
                _least(self.tie_pairs[component], key, gap)
                return
        if a > b:
            first, second = second, first
            first_geometry, second_geometry = second_geometry, first_geometry
        entry = self.pairs.get(key)
        if entry is None:
            self.pairs[key] = [gap, layer, {layer: gap}, first, second,
                               (first_geometry, second_geometry)]
            return
        _least(entry[2], layer, gap)
        if gap < entry[0] or (gap == entry[0] and layer < entry[1]):
            entry[0], entry[1], entry[3], entry[4] = gap, layer, first, second
            entry[5] = (first_geometry, second_geometry)

    def over_cap(self) -> bool:
        if self.calls > self.cap:
            self.stopped = True
        return self.stopped

    # --- passes ---

    def prims_vs_prims(self) -> None:
        reach, limit = self.reach, self.limit
        half = reach / 2.0
        size = max(ITEM_CELL_MIL, 4.0 * reach)
        floor = math.floor
        geo = _GEO
        for layer, items in self.model.items.items():
            grid: dict[tuple[int, int], list[int]] = defaultdict(list)
            for number, item in enumerate(items):
                xs, ys = _cells(item[0] - half, item[1] - half, item[2] + half,
                                item[3] + half, size)
                for gx in xs:
                    for gy in ys:
                        grid[(gx, gy)].append(number)
            self.item_grids[layer] = (grid, size)
            if self.stopped:
                continue
            for (gx, gy), members in grid.items():
                count = len(members)
                if count < 2:
                    continue
                if self.over_cap():
                    break
                for i in range(count):
                    a = items[members[i]]
                    na = a[4]
                    ax0, ay0, ax1, ay1 = a[0] - half, a[1] - half, a[2] + half, a[3] + half
                    for j in range(i + 1, count):
                        b = items[members[j]]
                        if na is not None and na == b[4]:
                            continue
                        bx0, by0 = b[0] - half, b[1] - half
                        if bx0 > ax1 or ax0 > b[2] + half or by0 > ay1 or ay0 > b[3] + half:
                            continue
                        # The pair belongs to the cell holding the low corner of the
                        # overlap of the grown boxes (both boxes are in that cell).
                        if floor(max(ax0, bx0) / size) != gx or floor(max(ay0, by0) / size) != gy:
                            continue
                        if a[5] == b[5]:
                            continue
                        self.calls += 1
                        gap = geo(a[6], b[6])
                        if gap < limit:
                            self.found(a, b, layer, gap, a[6], b[6])

    def prims_vs_areas(self) -> None:
        model, reach, limit = self.model, self.reach, self.limit
        grow = _grid_grow(reach)
        query = max(0.0, reach - grow)
        size = AREA_CELL_MIL
        floor = math.floor
        geo = _GEO
        for layer, chunks in model.chunks.items():
            if self.stopped:
                return
            grid, _ = model.grid(layer, grow)
            for item in model.items.get(layer, ()):
                if self.over_cap():
                    return
                net = item[4]
                x0, y0, x1, y1 = item[0] - reach, item[1] - reach, item[2] + reach, item[3] + reach
                gx0, gx1 = floor((item[0] - query) / size), floor((item[2] + query) / size)
                gy0, gy1 = floor((item[1] - query) / size), floor((item[3] + query) / size)
                if gx0 == gx1 and gy0 == gy1:
                    candidates = grid.get((gx0, gy0), ())
                else:
                    candidates = set()
                    for gx in range(gx0, gx1 + 1):
                        for gy in range(gy0, gy1 + 1):
                            cell = grid.get((gx, gy))
                            if cell:
                                candidates.update(cell)
                if not candidates:
                    continue
                geometry = item[6]
                segment, radius, polygon, arc = geometry
                capsule = polygon is None and arc is None
                if capsule:
                    sx1, sy1, sx2, sy2 = segment
                    point = sx1 == sx2 and sy1 == sy2
                best: dict = {}
                tested = 0
                for number in candidates:
                    cx0, cy0, cx1, cy1, area, points = chunks[number]
                    if cx0 > x1 or cx1 < x0 or cy0 > y1 or cy1 < y0:
                        continue
                    if net is not None and net == area.net:
                        continue
                    known = best.get(area)
                    least = known[0] if known else math.inf
                    if least == 0.0:
                        continue
                    edge = None
                    px, py = points[0]
                    for qx, qy in points[1:]:
                        if (px < x0 and qx < x0) or (px > x1 and qx > x1) or \
                                (py < y0 and qy < y0) or (py > y1 and qy > y1):
                            px, py = qx, qy
                            continue
                        tested += 1
                        if capsule:
                            if point:
                                distance = point_seg_distance(sx1, sy1, px, py, qx, qy) - radius
                            else:
                                distance = seg_seg_distance(
                                    sx1, sy1, sx2, sy2, px, py, qx, qy) - radius
                        else:
                            distance = geo(geometry, ((px, py, qx, qy), 0.0, None, None))
                        if distance < least:
                            least, edge = max(0.0, distance), (px, py, qx, qy)
                        px, py = qx, qy
                    if edge is not None:
                        best[area] = (least, edge)
                self.calls += tested
                for area, (gap, edge) in best.items():
                    if gap < limit:
                        self.found(item, area.side, layer, gap, geometry,
                                   (edge, 0.0, None, None))

    def areas_vs_areas(self) -> None:
        model, reach, limit = self.model, self.reach, self.limit
        grow = _grid_grow(reach)
        for layer, chunks in model.chunks.items():
            if self.stopped:
                return
            nets = {area.net for area in model.area_layers[layer]}
            if len(nets) < 2 and None not in nets:
                continue  # one net: nothing to measure
            grid, multi = model.grid(layer, grow)
            done: set[tuple[int, int]] = set()
            for cell in multi:
                if self.over_cap():
                    return
                members = grid[cell]
                count = len(members)
                for i in range(count):
                    first = members[i]
                    ca = chunks[first]
                    area_a = ca[4]
                    na = area_a.net
                    for j in range(i + 1, count):
                        second = members[j]
                        cb = chunks[second]
                        area_b = cb[4]
                        if area_a is area_b:
                            continue
                        nb = area_b.net
                        if na is not None and na == nb:
                            continue
                        if cb[0] > ca[2] + reach or ca[0] > cb[2] + reach or \
                                cb[1] > ca[3] + reach or ca[1] > cb[3] + reach:
                            continue
                        key = (first, second) if first < second else (second, first)
                        if key in done:
                            continue
                        done.add(key)
                        self._chunk_pair(layer, ca, cb, limit, reach)

    def _chunk_pair(self, layer, ca, cb, limit, reach) -> None:
        # Only the edges within the window both chunks can reach matter.
        ox0, oy0 = max(ca[0], cb[0]) - reach, max(ca[1], cb[1]) - reach
        ox1, oy1 = min(ca[2], cb[2]) + reach, min(ca[3], cb[3]) + reach

        def near(points):
            return [
                (p[0], p[1], q[0], q[1]) for p, q in zip(points, points[1:])
                if not ((p[0] < ox0 and q[0] < ox0) or (p[0] > ox1 and q[0] > ox1)
                        or (p[1] < oy0 and q[1] < oy0) or (p[1] > oy1 and q[1] > oy1))
            ]

        first = near(ca[5])
        if not first:
            return
        second = near(cb[5])
        best, pair = math.inf, None
        for a in first:
            for b in second:
                distance = seg_seg_distance(*a, *b)
                if distance < best:
                    best, pair = distance, (a, b)
        self.calls += len(first) * len(second)
        if pair is not None and best < limit:
            self.found(ca[4].side, cb[4].side, layer, best,
                       (pair[0], 0.0, None, None), (pair[1], 0.0, None, None))

    def containment(self) -> None:
        """Copper wholly inside other-net area copper, which no edge pass sees: objects
        inside free regions, fills and stale pours (valid pours too with
        VALID_POUR_ITEMS), and areas inside any area. (An area inside an object needs no
        test: objects are measured as filled copper, so the area's edges are at 0 from
        it.)"""
        model = self.model
        if self.limit <= 0:
            return
        for layer, areas in model.area_layers.items():
            if self.over_cap():
                return
            self._items_in_areas(layer, [
                area for area in areas if VALID_POUR_ITEMS or area.checks_containment
            ])
            self._areas_in_areas(layer, areas)

    def _items_in_areas(self, layer: int, candidates: list) -> None:
        items = self.model.items.get(layer, ())
        grid, size = self.item_grids.get(layer, ({}, ITEM_CELL_MIL))
        for area in candidates:
            if self.over_cap():
                return
            minx, miny, maxx, maxy = area.bbox
            seen: set[int] = set()
            xs, ys = _cells(minx, miny, maxx, maxy, size)
            if len(xs) * len(ys) <= len(grid):
                for gx in xs:
                    for gy in ys:
                        seen.update(grid.get((gx, gy), ()))
            else:  # an area larger than the layer's copper: walk the copper's cells
                for (gx, gy), members in grid.items():
                    if gx in xs and gy in ys:
                        seen.update(members)
            for number in seen:
                item = items[number]
                if item[4] is not None and item[4] == area.net:
                    continue
                # Copper crossing the area's edge was measured against it; wholly
                # inside, any point of it is inside.
                if not (minx <= item[0] and item[2] <= maxx
                        and miny <= item[1] and item[3] <= maxy):
                    continue
                self.calls += 1
                x, y = _ref_point(item[6])
                if area.contains(x, y):
                    self.found(item, area.side, layer, 0.0, item[6], _point(x, y))

    def _areas_in_areas(self, layer: int, areas: list) -> None:
        """An area inside another of another net: their boxes nest and the inner one's
        first vertex lies in the outer one's copper. A sweep along the longer spread of
        the boxes compares a box with the earlier ones still open; each comparison is a
        work unit."""
        if len(areas) < 2:
            return
        spread_x = max(area.bbox[2] for area in areas) - min(area.bbox[0] for area in areas)
        spread_y = max(area.bbox[3] for area in areas) - min(area.bbox[1] for area in areas)
        axis = 0 if spread_x >= spread_y else 1
        order = sorted(areas, key=lambda area: (area.bbox[axis], -area.bbox[axis + 2]))
        active: list[_AreaCopper] = []
        for area in order:
            if self.over_cap():
                return
            low = area.bbox[axis]
            active = [other for other in active if other.bbox[axis + 2] >= low]
            self.calls += len(active)
            net = area.net
            for other in active:
                if net is not None and net == other.net:
                    continue
                for inner, outer in ((area, other), (other, area)):
                    ib, ob = inner.bbox, outer.bbox
                    if not (ob[0] <= ib[0] and ib[2] <= ob[2]
                            and ob[1] <= ib[1] and ib[3] <= ob[3]):
                        continue
                    x, y = inner.rings[0][0]
                    self.calls += 1
                    if self.model.area_contains(outer, x, y):
                        point = _point(x, y)
                        self.found(inner.side, outer.side, layer, 0.0, point, point)
                        break
            active.append(area)

    def result(self) -> SpacingResult:
        stale = self.model.stale_pours
        position = self.model.position
        records = []
        for entry in self.pairs.values():
            gap, layer, gaps, first, second, geometries = entry
            marks = set()
            if gap <= TOUCH_MIL:
                marks.add("touching")
            if first[8] == "pad" and second[8] == "pad" and first[9] is not None \
                    and first[9] == second[9]:
                marks.add("same_footprint")
            if gap <= TOUCH_MIL and first[9] is not None and first[9] == second[9]:
                for mine, other in ((first, second), (second, first)):
                    if mine[4] is None and other[4] is not None and other[8] == "pad":
                        marks.add("footprint_copper")
            pours = tuple(sorted({side[10] for side in (first, second) if side[10] in stale}))
            if pours:
                marks.add("stale_pour")
            layers, spread = _layer_split(gaps, gap, position)
            records.append(GapRecord(
                first=_copper(first), second=_copper(second), gap_mil=gap, layer=layer,
                layers=layers, marks=frozenset(marks), stale_pours=pours,
                _layer_gaps=spread, _geometries=geometries,
            ))
        records.sort(key=lambda record: (record.gap_mil, record.first.owner, record.second.owner))
        return SpacingResult(
            reach_mil=self.reach,
            records=records,
            no_net_pairs=len(self.no_net),
            net_tie_pairs={component: len(pairs) for component, pairs in self.tie_pairs.items()},
            distance_calls=self.calls,
            stopped=self.stopped,
            valid_pour_items_checked=VALID_POUR_ITEMS,
            _state=(sorted(self.no_net.values()),
                    {component: sorted(pairs.values())
                     for component, pairs in self.tie_pairs.items()}),
        )


def _grid_grow(reach: float) -> float:
    """The grow of the area grid a reach needs: AREA_GROW_MIL up to twice it."""
    return AREA_GROW_MIL if reach <= 2.0 * AREA_GROW_MIL else float(math.ceil(reach / 2.0))


def _narrow_spacing(base: SpacingResult, reach: float) -> SpacingResult:
    """A complete spacing result of a larger reach cut to `reach`."""
    cut = reach - CLEARANCE_TOLERANCE_MIL
    records = []
    for record in base.records:
        if record.gap_mil >= cut:
            break
        records.append(_narrow_layers(record, cut))
    no_net, ties = base._state
    pairs = {component: bisect_left(gaps, cut) for component, gaps in ties.items()}
    return SpacingResult(
        reach_mil=reach,
        records=records,
        no_net_pairs=bisect_left(no_net, cut),
        net_tie_pairs={component: count for component, count in pairs.items() if count},
        distance_calls=base.distance_calls,
        stopped=False,
        valid_pour_items_checked=base.valid_pour_items_checked,
    )


def spacing(index, reach_mil: float, cap: int = MAX_DISTANCE_CALLS) -> SpacingResult:
    """Every pair of copper owners of different nets closer than
    reach_mil - CLEARANCE_TOLERANCE_MIL on a signal layer (see the module docstring
    for what is skipped and marked). Cached on the index (Model.cached)."""
    model = copper_model(index)

    def compute():
        run = _Spacing(model, reach_mil, cap)
        run.prims_vs_prims()
        run.prims_vs_areas()
        run.areas_vs_areas()
        run.containment()
        return run.result()

    return model.cached("spacing", reach_mil, cap, compute, _narrow_spacing)


# ---------- copper of chosen nets (check_hv_clearance) ----------


@dataclass
class NetGapResult:
    """net_gaps: the copper of chosen (source) nets against other nets' copper."""

    # The smallest gap per (net pair, layer, whether a pad is one side), smallest first;
    # first.net < second.net.
    records: list[GapRecord]
    no_net_owners: int  # no-net copper owners within a source net's reach (not measured)
    net_tie_pairs: int  # owner pairs skipped as a net tie's own copper
    distance_calls: int
    stopped: bool  # the cap was reached: the records are incomplete


def _grid_near(grid: dict, size: float, x0: float, y0: float, x1: float, y1: float) -> set:
    """The members of the grid cells a box touches."""
    found: set[int] = set()
    xs, ys = _cells(x0, y0, x1, y1, size)
    for gx in xs:
        for gy in ys:
            cell = grid.get((gx, gy))
            if cell:
                found.update(cell)
    return found


def _edge_near(geometry, points, window, least: float) -> tuple[tuple | None, float, int]:
    """(edge, distance, edges measured) of the ring edges in `points` within the window
    nearest the copper geometry, when nearer than `least` (edge None otherwise)."""
    x0, y0, x1, y1 = window
    segment, radius, polygon, arc = geometry
    capsule = polygon is None and arc is None
    edge, tested = None, 0
    px, py = points[0]
    for qx, qy in points[1:]:
        if (px < x0 and qx < x0) or (px > x1 and qx > x1) or \
                (py < y0 and qy < y0) or (py > y1 and qy > y1):
            px, py = qx, qy
            continue
        tested += 1
        if capsule:
            distance = seg_seg_distance(*segment, px, py, qx, qy) - radius
        else:
            distance = _GEO(geometry, ((px, py, qx, qy), 0.0, None, None))
        if distance < least:
            least, edge = max(0.0, distance), (px, py, qx, qy)
        px, py = qx, qy
    return edge, least, tested


def _chunk_gap(ca, cb, reach: float, least: float) -> tuple[float, tuple | None, tuple | None, int]:
    """(distance, edge of ca, edge of cb, edges compared) of the nearest edges of two
    area chunks within the window both can reach, when nearer than `least`."""
    ox0, oy0 = max(ca[0], cb[0]) - reach, max(ca[1], cb[1]) - reach
    ox1, oy1 = min(ca[2], cb[2]) + reach, min(ca[3], cb[3]) + reach

    def near(points):
        return [
            (p[0], p[1], q[0], q[1]) for p, q in zip(points, points[1:])
            if not ((p[0] < ox0 and q[0] < ox0) or (p[0] > ox1 and q[0] > ox1)
                    or (p[1] < oy0 and q[1] < oy0) or (p[1] > oy1 and q[1] > oy1))
        ]

    first = near(ca[5])
    second = near(cb[5]) if first else []
    best, pair = least, (None, None)
    for a in first:
        for b in second:
            distance = seg_seg_distance(*a, *b)
            if distance < best:
                best, pair = distance, (a, b)
    return best, pair[0], pair[1], len(first) * len(second)


class _NetGaps:
    """One net_gaps run: the source nets' copper on each layer against every other net's,
    each pair within its own reach. A pair of two source nets is measured from one side."""

    def __init__(self, model: _Model, reach_by_net: dict[int, float], pair_reach, cap: int):
        self.model = model
        self.reach_by_net = reach_by_net
        self.sources = set(reach_by_net)
        self.pair_reach = pair_reach
        self.cap = cap
        self.calls = 0
        self.stopped = False
        # (net a, net b, layer, pad) -> [gap, first side, second side, geometries]
        self.pairs: dict[tuple, list] = {}
        self.no_net: set = set()
        self.ties: set = set()
        self._reaches: dict[tuple, float | None] = {}

    def reach(self, a: int, b: int) -> float | None:
        """The pair's reach (pair_reach, never past its source sides' own reach), or None
        when the pair is not wanted."""
        key = (a, b) if a <= b else (b, a)
        if key not in self._reaches:
            widest = max(self.reach_by_net.get(a, 0.0), self.reach_by_net.get(b, 0.0))
            value = widest if self.pair_reach is None else self.pair_reach(*key)
            self._reaches[key] = min(value, widest) if value and widest else None
        return self._reaches[key]

    def over_cap(self) -> bool:
        if self.calls > self.cap:
            self.stopped = True
        return self.stopped

    def found(self, first, second, layer: int, gap: float, first_geometry, second_geometry):
        ties = self.model.ties
        if ties and (first[9] in ties or second[9] in ties) and \
                _net_tie(ties, first, second) is not None:
            a, b = first[5], second[5]
            self.ties.add((a, b) if a <= b else (b, a))
            return
        if first[4] > second[4]:
            first, second = second, first
            first_geometry, second_geometry = second_geometry, first_geometry
        key = (first[4], second[4], layer, first[8] == "pad" or second[8] == "pad")
        entry = self.pairs.get(key)
        if entry is None or gap < entry[0]:
            self.pairs[key] = [gap, first, second, (first_geometry, second_geometry)]

    def run(self, layers=None) -> None:
        for layer in self.model.stack_signal:
            if layers is not None and layer not in layers:
                continue
            if self.over_cap():
                return
            self._layer(layer)

    def _layer(self, layer: int) -> None:
        model, sources = self.model, self.sources
        items = model.items.get(layer, [])
        areas = model.area_layers.get(layer, [])
        source_items = [number for number, item in enumerate(items) if item[4] in sources]
        source_areas = [area for area in areas if area.net in sources]
        if not source_items and not source_areas:
            return
        widest = max([self.reach_by_net[items[number][4]] for number in source_items]
                     + [self.reach_by_net[area.net] for area in source_areas])
        size = max(ITEM_CELL_MIL, widest / 4.0)
        grid: dict[tuple[int, int], list[int]] = defaultdict(list)
        for number, item in enumerate(items):
            xs, ys = _cells(item[0], item[1], item[2], item[3], size)
            for gx in xs:
                for gy in ys:
                    grid[(gx, gy)].append(number)
        self._items_vs_items(layer, items, grid, size, source_items)
        chunks = model.chunks.get(layer, [])
        if chunks:
            chunk_grid = model.grid(layer, AREA_GROW_MIL)[0]
            own: dict = defaultdict(list)  # a source area -> its chunks
            for number, chunk in enumerate(chunks):
                if chunk[4].net in sources:
                    own[chunk[4]].append(number)
            self._items_vs_areas(layer, items, chunks, chunk_grid, source_items)
            self._areas_vs_items(layer, items, grid, size, chunks, own)
            self._areas_vs_areas(layer, chunks, chunk_grid, own)
        self._containment(layer, items, grid, size, areas, source_items, source_areas)

    def _limit(self, net_a: int, net_b: int, layer: int, pad: bool, reach: float) -> float:
        """How near a candidate must come to matter: the smallest gap found so far for
        its (pair, layer, pad) record, else the pair's reach."""
        key = (net_a, net_b, layer, pad) if net_a < net_b else (net_b, net_a, layer, pad)
        entry = self.pairs.get(key)
        return reach if entry is None or entry[0] >= reach else entry[0]

    def _items_vs_items(self, layer, items, grid, size, source_items) -> None:
        sources, geo = self.sources, _GEO
        for number in source_items:
            if self.over_cap():
                return
            a = items[number]
            net = a[4]
            r = self.reach_by_net[net]
            pad = a[8] == "pad"
            near = []
            for other in _grid_near(grid, size, a[0] - r, a[1] - r, a[2] + r, a[3] + r):
                b = items[other]
                nb = b[4]
                if nb == net or b[5] == a[5]:
                    continue
                dx = max(0.0, b[0] - a[2], a[0] - b[2])
                dy = max(0.0, b[1] - a[3], a[1] - b[3])
                box = dx * dx + dy * dy
                if nb is None:
                    if box < r * r:
                        self.no_net.add(b[5])
                    continue
                near.append((box, other))
            # Nearest boxes first: the first gaps found prune the rest.
            near.sort()
            for box, other in near:
                b = items[other]
                nb = b[4]
                reach = self.reach(net, nb)
                if reach is None or (nb in sources and other < number
                                     and self.reach_by_net[nb] >= reach):
                    continue  # not wanted, or measured from the other side
                limit = self._limit(net, nb, layer, pad or b[8] == "pad", reach)
                if box >= limit * limit:
                    continue
                self.calls += 1
                gap = geo(a[6], b[6])
                if gap < limit:
                    self.found(a, b, layer, gap, a[6], b[6])

    def _items_vs_areas(self, layer, items, chunks, chunk_grid, source_items) -> None:
        for number in source_items:
            if self.over_cap():
                return
            item = items[number]
            net = item[4]
            r = self.reach_by_net[net]
            pad = item[8] == "pad"
            query = max(0.0, r - AREA_GROW_MIL)
            near = []
            for chunk in _grid_near(chunk_grid, AREA_CELL_MIL, item[0] - query,
                                    item[1] - query, item[2] + query, item[3] + query):
                cx0, cy0, cx1, cy1, area, _ = chunks[chunk]
                if area.net == net:
                    continue
                dx = max(0.0, cx0 - item[2], item[0] - cx1)
                dy = max(0.0, cy0 - item[3], item[1] - cy1)
                box = dx * dx + dy * dy
                if area.net is None:
                    if box < r * r:
                        self.no_net.add(area.side[5])
                    continue
                near.append((box, chunk))
            near.sort()
            for box, chunk in near:
                area, points = chunks[chunk][4], chunks[chunk][5]
                reach = self.reach(net, area.net)
                if reach is None:
                    continue
                limit = self._limit(net, area.net, layer, pad, reach)
                if box >= limit * limit:
                    continue
                window = (item[0] - limit, item[1] - limit, item[2] + limit, item[3] + limit)
                edge, gap, tested = _edge_near(item[6], points, window, limit)
                self.calls += tested
                if edge is not None:
                    self.found(item, area.side, layer, gap, item[6], (edge, 0.0, None, None))

    def _areas_vs_items(self, layer, items, grid, size, chunks, own) -> None:
        sources = self.sources
        for area, numbers in own.items():
            net = area.net
            r = self.reach_by_net[net]
            for chunk in numbers:
                if self.over_cap():
                    return
                cx0, cy0, cx1, cy1, _, points = chunks[chunk]
                near = []
                for other in _grid_near(grid, size, cx0 - r, cy0 - r, cx1 + r, cy1 + r):
                    item = items[other]
                    nb = item[4]
                    if nb == net:
                        continue
                    dx = max(0.0, cx0 - item[2], item[0] - cx1)
                    dy = max(0.0, cy0 - item[3], item[1] - cy1)
                    box = dx * dx + dy * dy
                    if nb is None:
                        if box < r * r:
                            self.no_net.add(item[5])
                        continue
                    near.append((box, other))
                near.sort()
                for box, other in near:
                    item = items[other]
                    nb = item[4]
                    reach = self.reach(net, nb)
                    if reach is None or (nb in sources and self.reach_by_net[nb] >= reach):
                        continue  # not wanted, or the item measured itself against this area
                    limit = self._limit(net, nb, layer, item[8] == "pad", reach)
                    if box >= limit * limit:
                        continue
                    window = (item[0] - limit, item[1] - limit, item[2] + limit,
                              item[3] + limit)
                    edge, gap, tested = _edge_near(item[6], points, window, limit)
                    self.calls += tested
                    if edge is not None:
                        self.found(item, area.side, layer, gap, item[6],
                                   (edge, 0.0, None, None))

    def _areas_vs_areas(self, layer, chunks, chunk_grid, own) -> None:
        sources = self.sources
        for area, numbers in own.items():
            net = area.net
            r = self.reach_by_net[net]
            query = max(0.0, r - AREA_GROW_MIL)
            for chunk in numbers:
                if self.over_cap():
                    return
                ca = chunks[chunk]
                near = []
                for other in _grid_near(chunk_grid, AREA_CELL_MIL, ca[0] - query,
                                        ca[1] - query, ca[2] + query, ca[3] + query):
                    cb = chunks[other]
                    area_b = cb[4]
                    nb = area_b.net
                    if area_b is area or nb == net:
                        continue
                    dx = max(0.0, cb[0] - ca[2], ca[0] - cb[2])
                    dy = max(0.0, cb[1] - ca[3], ca[1] - cb[3])
                    box = dx * dx + dy * dy
                    if nb is None:
                        if box < r * r:
                            self.no_net.add(area_b.side[5])
                        continue
                    near.append((box, other))
                near.sort()
                for box, other in near:
                    cb = chunks[other]
                    area_b = cb[4]
                    nb = area_b.net
                    reach = self.reach(net, nb)
                    if reach is None or (nb in sources and area_b.number < area.number
                                         and self.reach_by_net[nb] >= reach):
                        continue  # not wanted, or measured from the other side
                    limit = self._limit(net, nb, layer, False, reach)
                    if box >= limit * limit:
                        continue
                    gap, edge_a, edge_b, tested = _chunk_gap(ca, cb, limit, limit)
                    self.calls += tested
                    if edge_a is not None:
                        self.found(area.side, area_b.side, layer, gap,
                                   (edge_a, 0.0, None, None), (edge_b, 0.0, None, None))

    def _containment(self, layer, items, grid, size, areas, source_items, source_areas) -> None:
        """Copper wholly inside another net's area copper, which no edge pass sees: source
        objects inside free regions, fills and stale pours (a valid pour is poured around
        other nets' copper) and other nets' objects inside the source nets' ones, and
        areas inside areas."""
        model, sources = self.model, self.sources
        holders = [area for area in areas if area.checks_containment and area.net is not None]
        cell = 4.0 * AREA_CELL_MIL
        holder_grid: dict[tuple[int, int], list] = defaultdict(list)
        for area in holders:
            xs, ys = _cells(*area.bbox, cell)
            for gx in xs:
                for gy in ys:
                    holder_grid[(gx, gy)].append(area)
        for number in source_items:
            if self.over_cap():
                return
            item = items[number]
            x, y = _ref_point(item[6])
            for area in holder_grid.get((math.floor(x / cell), math.floor(y / cell)), ()):
                minx, miny, maxx, maxy = area.bbox
                if area.net == item[4] or not (minx <= item[0] and item[2] <= maxx
                                               and miny <= item[1] and item[3] <= maxy):
                    continue
                if self.reach(item[4], area.net) is None:
                    continue
                self.calls += 1
                if model.area_contains(area, x, y):
                    self.found(item, area.side, layer, 0.0, item[6], _point(x, y))
        for area in source_areas:
            if self.over_cap():
                return
            minx, miny, maxx, maxy = area.bbox
            if area.checks_containment:
                for other in _grid_near(grid, size, minx, miny, maxx, maxy):
                    item = items[other]
                    nb = item[4]
                    if nb is None or nb == area.net or nb in sources or not (
                            minx <= item[0] and item[2] <= maxx
                            and miny <= item[1] and item[3] <= maxy):
                        continue
                    if self.reach(area.net, nb) is None:
                        continue
                    self.calls += 1
                    x, y = _ref_point(item[6])
                    if model.area_contains(area, x, y):
                        self.found(item, area.side, layer, 0.0, item[6], _point(x, y))
            for other in areas:
                nb = other.net
                if other is area or nb is None or nb == area.net or (
                        nb in sources and other.number < area.number):
                    continue
                if self.reach(area.net, nb) is None:
                    continue
                for inner, outer in ((area, other), (other, area)):
                    ib, ob = inner.bbox, outer.bbox
                    if not (ob[0] <= ib[0] and ib[2] <= ob[2] and ob[1] <= ib[1]
                            and ib[3] <= ob[3]):
                        continue
                    x, y = inner.rings[0][0]
                    self.calls += 1
                    if model.area_contains(outer, x, y):
                        point = _point(x, y)
                        self.found(inner.side, outer.side, layer, 0.0, point, point)
                        break

    def result(self) -> NetGapResult:
        stale = self.model.stale_pours
        records = []
        for (_, _, layer, _), (gap, first, second, geometries) in self.pairs.items():
            marks = set()
            if gap <= TOUCH_MIL:
                marks.add("touching")
            pours = tuple(sorted({side[10] for side in (first, second) if side[10] in stale}))
            if pours:
                marks.add("stale_pour")
            records.append(GapRecord(
                first=_copper(first), second=_copper(second), gap_mil=gap, layer=layer,
                layers=(layer,), marks=frozenset(marks), stale_pours=pours,
                _geometries=geometries,
            ))
        records.sort(key=lambda record: (record.gap_mil, record.first.net, record.second.net,
                                         record.layer, record.first.owner, record.second.owner))
        return NetGapResult(records=records, no_net_owners=len(self.no_net),
                            net_tie_pairs=len(self.ties), distance_calls=self.calls,
                            stopped=self.stopped)


def net_gaps(index, reach_by_net: dict[int, float], pair_reach=None, layers=None,
             cap: int = MAX_DISTANCE_CALLS) -> NetGapResult:
    """The copper of the source nets (reach_by_net: net id -> its search reach in mil) on
    each signal layer against every other net's copper (tracks, arcs, vias' pads on the
    layer, pads' copper on the layer, hatched pour copper, poured regions, free regions and
    fills; shelved pours hold none), as spacing measures it: the smallest gap per net pair,
    layer and whether a pad is one side, below the pair's reach. pair_reach(a, b) (a < b,
    net ids) gives a pair's own reach, or None to skip the pair; it is never wider than
    its source sides' reach. layers narrows to those signal layers. No-net copper is
    counted, not measured; a net tie's own copper is skipped (counted). Not cached."""
    run = _NetGaps(copper_model(index), reach_by_net, pair_reach, cap)
    run.run(layers)
    return run.result()


def openings_near(index, start, end, near_mil: float) -> list[dict]:
    """What a surface path between two points crosses or passes: the board outline, board
    cutouts, and holes without copper of their own (non-plated pad holes and slots, bare
    holes) within near_mil of the straight segment start -> end. Each: kind (board_edge,
    board_cutout, slot, hole), in_path (it crosses or touches the segment), distance_mil
    and where (a hole's centre, the nearest point of an edge), absolute mil; nearest
    first."""
    model = copper_model(index)
    segment = ((start[0], start[1], end[0], end[1]), 0.0, None, None)
    x0, y0, x1, y1 = _BOUNDS(segment)
    x0, y0, x1, y1 = x0 - near_mil, y0 - near_mil, x1 + near_mil, y1 + near_mil
    found: dict[tuple, tuple] = {}
    for geometry, box, _, kind, cutout, _ in model.edge.pieces:
        if box[0] > x1 or box[2] < x0 or box[1] > y1 or box[3] < y0:
            continue
        distance = _GEO(segment, geometry)
        key = ("board_cutout", cutout) if kind == "cutout" else ("board_edge", None)
        if distance < near_mil and (key not in found or distance < found[key][0]):
            found[key] = (distance, _near_point(geometry, segment))
    for number, hole in enumerate(model.holes):
        if hole.kind != "bare_hole" and hole.plated is not False:
            continue
        geometry = hole.geometry
        box = _BOUNDS(geometry)
        if box[0] > x1 or box[2] < x0 or box[1] > y1 or box[3] < y0:
            continue
        distance = _GEO(segment, geometry)
        if distance < near_mil:
            slot = hole.hole_shape == 2 and (hole.slot_length_mil or 0.0) > hole.hole_mil
            found[("slot" if slot else "hole", number)] = (distance, (hole.x, hole.y))
    out = [
        {"kind": key[0], "in_path": distance <= TOUCH_MIL, "distance_mil": distance,
         "at": point}
        for key, (distance, point) in found.items()
    ]
    out.sort(key=lambda entry: (entry["distance_mil"], entry["kind"]))
    return out


# ---------- the board edge ----------


class _Edge:
    """The board edge: the outline's pieces with exact arcs, and each board cutout's
    (ShapeBasedRegions6 shapes, Regions6 without it; never BoardRegions). A cutout or an
    outline with no edge of non-zero length is no edge."""

    def __init__(self, pcb):
        outline = pcb.board.outline_shape
        regions, self.unread = read_board_cutouts(pcb)
        self.cutouts = []
        self.degenerate_cutouts = 0
        cutout_pieces = []
        for region in regions:
            pieces = _shape_pieces(region.vertices) if len(region.vertices) >= 3 else []
            if not pieces:
                self.degenerate_cutouts += 1
                continue
            self.cutouts.append(region)
            cutout_pieces.append(pieces)
        outline_pieces = _shape_pieces(outline) if len(outline) >= 3 else []
        self.has_outline = bool(outline_pieces)
        # (geometry, box, edge key, edge kind, cutout index, cutout component)
        self.pieces: list[tuple] = []
        self.outline: Bands | None = None
        self.box: tuple | None = None  # the outline's box
        self.point: tuple | None = None  # a point of the outline
        self.cutout_shapes: list[tuple] = []  # (bands, box, contour, region)
        if not self.has_outline:
            return
        for geometry, vertex in outline_pieces:
            self.pieces.append((geometry, _BOUNDS(geometry), (-1, vertex), "outline", None, None))
        points = contour(outline, max_sagitta=SNAP_MIL)
        self.outline = Bands([points])
        self.box = (min(point[0] for point in points), min(point[1] for point in points),
                    max(point[0] for point in points), max(point[1] for point in points))
        self.point = points[0]
        for number, (region, pieces) in enumerate(zip(self.cutouts, cutout_pieces)):
            for geometry, vertex in pieces:
                self.pieces.append((geometry, _BOUNDS(geometry), (number, vertex), "cutout",
                                    region.index, region.component))
            points = contour(region.vertices, max_sagitta=SNAP_MIL)
            xs = [point[0] for point in points]
            ys = [point[1] for point in points]
            self.cutout_shapes.append(
                (Bands([points]), (min(xs), min(ys), max(xs), max(ys)), points, region)
            )

    def in_cutout(self, x: float, y: float):
        for bands, (minx, miny, maxx, maxy), _, region in self.cutout_shapes:
            if minx <= x <= maxx and miny <= y <= maxy and bands.contains(x, y):
                return region
        return None

    def on_board(self, x: float, y: float) -> bool:
        return self.outline is not None and self.outline.contains(x, y) \
            and self.in_cutout(x, y) is None

    def nearest(self, geometry, reach: float, limit: float) -> tuple[int, tuple | None]:
        """(exact distances computed, (distance, piece) of the edge piece nearest the
        geometry when it is closer than `limit`, else None)."""
        x0, y0, x1, y1 = _BOUNDS(geometry)
        x0, y0, x1, y1 = x0 - reach, y0 - reach, x1 + reach, y1 + reach
        calls, best = 0, None
        for piece in self.pieces:
            px0, py0, px1, py1 = piece[1]
            if px0 > x1 or px1 < x0 or py0 > y1 or py1 < y0:
                continue
            calls += 1
            distance = _GEO(geometry, piece[0])
            if distance < limit and (best is None or distance < best[0]):
                best = (distance, piece)
        return calls, best

    def cut(self, hole: Hole):
        """The edge piece a hole's circle (or slot) crosses, if any."""
        geometry = hole.geometry
        x0, y0, x1, y1 = _BOUNDS(geometry)
        core = (geometry[0], 0.0, geometry[2], geometry[3])
        for piece in self.pieces:
            px0, py0, px1, py1 = piece[1]
            if px0 > x1 or px1 < x0 or py0 > y1 or py1 < y0:
                continue
            if _GEO(core, piece[0]) < geometry[1] - TOUCH_MIL:
                return piece
        return None


def _shape_pieces(vertices) -> list[tuple[tuple, int]]:
    """(geometry, vertex index) pieces of a closed ShapeVertex shape: straight edges as
    segments (split at EDGE_PIECE_MIL), arc edges as exact arc pieces of at most 90
    degrees. As pcb_doc.shape_points, an arc runs counter-clockwise from its start to its
    end angle, drawn from the end nearer its vertex; short joins close the grid rounding
    between an arc's ends and the vertices."""
    pieces: list[tuple[tuple, int]] = []
    count = len(vertices)

    def straight(start, end, number):
        length = math.dist(start, end)
        if length < 1e-9:
            return
        steps = max(1, math.ceil(length / EDGE_PIECE_MIL))
        for step in range(steps):
            a = (start[0] + (end[0] - start[0]) * step / steps,
                 start[1] + (end[1] - start[1]) * step / steps)
            b = (start[0] + (end[0] - start[0]) * (step + 1) / steps,
                 start[1] + (end[1] - start[1]) * (step + 1) / steps)
            pieces.append((((a[0], a[1], b[0], b[1]), 0.0, None, None), number))

    for number, vertex in enumerate(vertices):
        here = (vertex.x, vertex.y)
        following = vertices[(number + 1) % count]
        there = (following.x, following.y)
        if vertex.arc is None or (vertex.arc[4] - vertex.arc[3]) % 360.0 < _MIN_SWEEP_DEG:
            straight(here, there, number)
            continue
        cx, cy, radius, start, end = vertex.arc
        sweep = (end - start) % 360.0 or 360.0
        steps = max(math.ceil(sweep / 90.0),
                    math.ceil(radius * math.radians(sweep) / EDGE_PIECE_MIL))
        points = [
            (cx + radius * math.cos(math.radians(start + sweep * step / steps)),
             cy + radius * math.sin(math.radians(start + sweep * step / steps)))
            for step in range(steps + 1)
        ]
        if math.dist(points[-1], here) < math.dist(points[0], here):
            points.reverse()
        straight(here, points[0], number)
        for a, b in zip(points, points[1:]):
            pieces.append((((a[0], a[1], b[0], b[1]), 0.0, None,
                            (cx, cy, radius, a[0], a[1], b[0], b[1])), number))
        straight(points[-1], there, number)
    return pieces


def _piece_distance(segment, piece) -> float:
    """Distance from a zero-width segment to an edge piece."""
    if piece[3] is not None:
        return seg_arc_distance(*segment, piece[3])
    return seg_seg_distance(*segment, *piece[0])


class _EdgeRun:
    def __init__(self, model: _Model, reach: float, cap: int):
        self.model = model
        self.edge = model.edge
        self.reach = reach
        self.limit = reach - CLEARANCE_TOLERANCE_MIL
        self.cap = cap
        self.calls = 0
        self.stopped = False
        # owner -> [distance, layer, layers, side, piece, item geometry]
        self.best: dict[tuple, list] = {}
        self.crossing: set[int] = set()  # id() of items that reach the edge
        # Owners that reach the edge (kept when a smaller reach is cut from this run).
        self.crossing_owners: set[tuple] = set()
        self._board: dict[int, bool] = {}
        self._area_board: dict[int, bool] = {}

    def item_on_board(self, item) -> bool:
        key = id(item)
        if key not in self._board:
            self._board[key] = self.edge.on_board((item[0] + item[2]) / 2.0,
                                                  (item[1] + item[3]) / 2.0)
        return self._board[key]

    def area_on_board(self, area) -> bool:
        """Some of the area's copper lies on the board: a vertex of its outline does.
        (An area whose edges do not reach the board's lies wholly on or off it, or covers
        the whole board: covered_outline.)"""
        known = self._area_board.get(area.number)
        if known is None:
            on_board = self.edge.on_board
            known = self._area_board[area.number] = any(
                on_board(x, y) for x, y in area.rings[0]
            )
        return known

    def note(self, side, layers, distance, piece, geometry) -> None:
        owner = side[5]
        if distance <= TOUCH_MIL:
            self.crossing_owners.add(owner)
        entry = self.best.get(owner)
        if entry is None or distance < entry[0]:
            self.best[owner] = [distance, layers[0], tuple(layers), side, piece, geometry]

    def prims(self) -> None:
        index = self.model.index
        sql = _RTREE_SQL if index._has_rtree else _BOX_SQL
        row_items = self.model.row_items
        reach, limit = self.reach, self.limit
        grow = reach + self.model.overhang
        for piece in self.edge.pieces:
            if self.calls > self.cap:
                self.stopped = True
                return
            geometry, (x0, y0, x1, y1) = piece[0], piece[1]
            for (number,) in index.db.execute(sql, (x1 + grow, x0 - grow, y1 + grow, y0 - grow)):
                for copper, item, layers in row_items.get(number, ()):
                    self.calls += 1
                    distance = _GEO(copper, geometry)
                    if distance >= limit:
                        continue
                    if distance <= TOUCH_MIL:
                        self.crossing.add(id(item))
                    elif not self.item_on_board(item):
                        continue  # off the board: copper_off_board, not an edge gap
                    self.note(item, layers, distance, piece, copper)

    def areas(self) -> None:
        model, reach, limit = self.model, self.reach, self.limit
        size = AREA_CELL_MIL
        grids = {layer: model.any_grid(layer) for layer in model.chunks}
        area_side: dict[int, list] = {}
        for piece in self.edge.pieces:
            if self.calls > self.cap:
                self.stopped = True
                return
            x0, y0, x1, y1 = piece[1]
            x0, y0, x1, y1 = x0 - reach, y0 - reach, x1 + reach, y1 + reach
            xs, ys = _cells(x0, y0, x1, y1, size)
            for layer, chunks in model.chunks.items():
                grid = grids[layer]
                seen: set[int] = set()
                for gx in xs:
                    for gy in ys:
                        seen.update(grid.get((gx, gy), ()))
                for number in seen:
                    cx0, cy0, cx1, cy1, area, points = chunks[number]
                    if cx0 > x1 or cx1 < x0 or cy0 > y1 or cy1 < y0:
                        continue
                    for p, q in zip(points, points[1:]):
                        if (p[0] < x0 and q[0] < x0) or (p[0] > x1 and q[0] > x1) or \
                                (p[1] < y0 and q[1] < y0) or (p[1] > y1 and q[1] > y1):
                            continue
                        self.calls += 1
                        segment = (p[0], p[1], q[0], q[1])
                        distance = _piece_distance(segment, piece[0])
                        if distance >= limit:
                            continue
                        entry = area_side.get(area.number)
                        if entry is None or distance < entry[0]:
                            area_side[area.number] = [distance, area, piece, segment]
        for distance, area, piece, segment in area_side.values():
            if distance > TOUCH_MIL and not self.area_on_board(area):
                continue
            self.note(area.side, [area.layer], distance, piece, (segment, 0.0, None, None))

    def covered_cutouts(self) -> None:
        """A cutout wholly inside copper: no copper edge comes near the cutout's."""
        index = self.model.index
        sql = _RTREE_SQL if index._has_rtree else _BOX_SQL
        for number, (_, box, points, _) in enumerate(self.edge.cutout_shapes):
            piece = next((p for p in self.edge.pieces if p[2][0] == number), None)
            if piece is None:
                continue
            x, y = points[0]
            point = _point(x, y)
            for area in self.model.areas:
                bbox = area.bbox
                if bbox[0] <= box[0] and box[2] <= bbox[2] and bbox[1] <= box[1] \
                        and box[3] <= bbox[3] and self.model.area_contains(area, x, y):
                    self.note(area.side, [area.layer], 0.0, piece, point)
            for (row,) in index.db.execute(sql, (x, x, y, y)):
                for copper, item, layers in self.model.row_items.get(row, ()):
                    if point_distance(x, y, copper) == 0.0:
                        self.crossing.add(id(item))
                        self.note(item, layers, 0.0, piece, copper)

    def covered_outline(self) -> None:
        """Area copper over the whole board (a fill or region drawn larger than it): no
        edge of it comes near the outline, so it crosses the edge everywhere."""
        edge = self.edge
        box, (x, y) = edge.box, edge.point
        piece = next(p for p in edge.pieces if p[3] == "outline")
        for area in self.model.areas:
            if area.side[5] in self.crossing_owners:
                continue
            bbox = area.bbox
            if bbox[0] <= box[0] and box[2] <= bbox[2] and bbox[1] <= box[1] \
                    and box[3] <= bbox[3] and self.model.area_contains(area, x, y):
                self.note(area.side, [area.layer], 0.0, piece, _point(x, y))

    def result(self, keep: bool = False) -> EdgeResult:
        """The run's records; keep: hold its state, so smaller reaches are cut from it."""
        model, edge = self.model, self.edge
        stale = model.stale_pours
        edge_holes, cut_owners = self._edge_holes()
        fingers, finger_owners = self._fingers(cut_owners)
        records = []
        for owner, entry in self.best.items():
            if owner in cut_owners or owner in finger_owners:
                continue
            distance, layer, layers, side, piece, geometry = entry
            marks = set()
            if distance <= TOUCH_MIL:
                marks.add("crosses")
            if side[10] in stale:
                marks.add("stale_pour")
            records.append(EdgeRecord(
                copper=_copper(side), distance_mil=distance, layer=layer, layers=layers,
                edge=piece[3], cutout=piece[4], cutout_component=piece[5],
                marks=frozenset(marks), _geometries=(geometry, piece[0]),
            ))
        records.sort(key=lambda record: (record.distance_mil, record.copper.owner))
        return EdgeResult(
            reach_mil=self.reach,
            has_outline=edge.has_outline,
            cutouts=len(edge.cutouts),
            footprint_cutouts=sum(1 for region in edge.cutouts if region.component is not None),
            records=records,
            off_board=self._off_board(cut_owners),
            fingers=fingers,
            edge_holes=edge_holes,
            distance_calls=self.calls,
            stopped=self.stopped,
            degenerate_cutouts=edge.degenerate_cutouts,
            _state=(self.best, self.crossing, self.crossing_owners) if keep else None,
        )

    def _edge_holes(self) -> tuple[list[EdgeHole], set]:
        """Plated holes the edge cuts (edge-plated or castellated): their pads and vias
        reach the edge on purpose, so they are not edge gaps."""
        row_items, crossing = self.model.row_items, self.crossing
        found: list[EdgeHole] = []
        owners: set[tuple] = set()
        for hole in self.model.holes:
            if hole.plated is False:
                continue
            side = None
            if hole.row is not None:
                side = next((item for _, item, _ in row_items.get(hole.row, ())
                             if id(item) in crossing), None)
                if side is None:
                    continue  # its copper does not reach the edge
            piece = self.edge.cut(hole)
            if piece is None:
                continue
            if side is not None:
                owners.add(side[5])
            found.append(EdgeHole(hole, _copper(side) if side is not None else None, piece[3],
                                  piece[4], (hole.x, hole.y)))
        return found, owners

    def _fingers(self, cut_owners: set) -> tuple[list[FingerGroup], set]:
        """Edge-connector fingers (see FINGER_PADS): a part's SMD pads within the reach of
        one edge (one straight or arc edge of the outline or a cutout), on an outer
        layer, long and narrow, long axis across the edge, at least FINGER_PADS of them
        along that edge, and fingers making up at least FINGER_SHARE of the part's pads.
        One group per part, whatever edges its fingers line."""
        model = self.model
        primitives = model.pcb.primitives
        outer = model.outer
        limit = math.cos(math.radians(FINGER_ANGLE_DEG))
        candidates: dict[tuple, list] = defaultdict(list)
        for owner, entry in self.best.items():
            side, piece = entry[3], entry[4]
            if side[8] != "pad" or side[9] is None or owner in cut_owners:
                continue
            primitive = primitives[side[7] - 1]
            if primitive.layer not in outer or (primitive.hole or 0.0) > 0:
                continue
            width, height = primitive.width, primitive.height or primitive.width
            long, short = max(width, height), min(width, height)
            if short <= 0 or long < FINGER_MIN_LENGTH_MIL or long < FINGER_ASPECT * short:
                continue
            axis = (primitive.rotation or 0.0) + (90.0 if height > width else 0.0)
            px1, py1, px2, py2 = piece[0][0]
            direction = math.degrees(math.atan2(py2 - py1, px2 - px1))
            if abs(math.sin(math.radians(axis - direction))) < limit:
                continue
            candidates[(side[9], piece[2])].append(owner)
        by_component: dict[int, list] = defaultdict(list)
        for (component, _), owners in candidates.items():
            if len(owners) >= FINGER_PADS:
                by_component[component].extend(owners)
        groups: list[FingerGroup] = []
        members: set[tuple] = set()
        for component, owners in sorted(by_component.items()):
            if len(owners) < FINGER_SHARE * model.part_pads[component]:
                continue  # other pads too: an IC or a connector at the edge
            members.update(owners)
            entries = sorted((self.best[owner] for owner in owners),
                             key=lambda entry: (entry[0], entry[3][5]))
            nearest = entries[0]
            pad = primitives[nearest[3][7] - 1]
            groups.append(FingerGroup(
                component=component,
                pads=[_copper(entry[3]) for entry in entries],
                distance_mil=nearest[0],
                layer=nearest[1],
                layers=tuple(sorted({entry[1] for entry in entries}, key=model.position.get)),
                edge=nearest[4][3],
                cutout=nearest[4][4],
                at=(pad.x1, pad.y1),
            ))
        return groups, members

    def _off_board(self, cut_owners: set) -> list[OffBoardRecord]:
        """Owners with copper wholly off the board: an object whose box centre is outside
        the outline or inside a cutout and that does not reach the edge; an area that
        reaches no edge with no vertex on the board (one that covers the board crosses
        its edge: covered_outline)."""
        model, edge = self.model, self.edge
        found: dict[tuple, list] = {}  # owner -> [side, layers, inside a cutout, pieces]

        def add(side, layers, x, y):
            outside = not edge.outline.contains(x, y)
            cutout = None if outside else edge.in_cutout(x, y)
            if outside or cutout is not None:
                entry = found.setdefault(side[5], [side, set(), False, 0])
                entry[1].update(layers)
                entry[2] = entry[2] or cutout is not None
                entry[3] += 1

        for groups in model.row_items.values():
            for _, item, layers in groups:
                if id(item) not in self.crossing and item[5] not in cut_owners:
                    add(item, layers, (item[0] + item[2]) / 2.0, (item[1] + item[3]) / 2.0)
        for area in model.areas:
            if area.side[5] in self.crossing_owners or self.area_on_board(area):
                continue
            add(area.side, [area.layer], *area.rings[0][0])
        records = [
            OffBoardRecord(
                copper=_copper(side),
                layers=tuple(sorted(layers, key=model.position.get)),
                objects=pieces,
                inside_cutout=inside_cutout,
                at=((side[0] + side[2]) / 2.0, (side[1] + side[3]) / 2.0),
            )
            for side, layers, inside_cutout, pieces in found.values()
        ]
        records.sort(key=lambda record: record.copper.owner)
        return records


def _narrow_edge(model: _Model, base: EdgeResult, reach: float, cap: int) -> EdgeResult:
    """A complete copper-to-edge result of a larger reach cut to `reach`: its owners
    closer than the reach, with fingers judged again at this reach."""
    run = _EdgeRun(model, reach, cap)
    best, run.crossing, run.crossing_owners = base._state
    run.best = {owner: entry for owner, entry in best.items() if entry[0] < run.limit}
    result = run.result()
    result.distance_calls = base.distance_calls
    return result


# The smallest reach a copper-to-edge search runs at: it finds the copper that reaches
# the edge (a smaller reach, 0 included, is cut from it), so off-board copper and
# edge-plated holes are judged alike at every reach.
_EDGE_FLOOR_MIL = CLEARANCE_TOLERANCE_MIL + 2.0 * TOUCH_MIL


def copper_to_edge(index, reach_mil: float, cap: int = MAX_DISTANCE_CALLS) -> EdgeResult:
    """Copper closer than reach_mil - CLEARANCE_TOLERANCE_MIL to the board edge (the
    outline and board cutouts), one record per owner; copper wholly off the board;
    edge-connector fingers; plated holes the edge cuts. Cached on the index
    (Model.cached)."""
    model = copper_model(index)

    def compute():
        edge = model.edge
        if not edge.has_outline:
            return EdgeResult(
                reach_mil=reach_mil, has_outline=False, cutouts=len(edge.cutouts),
                footprint_cutouts=sum(
                    1 for region in edge.cutouts if region.component is not None
                ),
                records=[], off_board=[], fingers=[], edge_holes=[], distance_calls=0,
                stopped=False, degenerate_cutouts=edge.degenerate_cutouts,
            )
        search = max(reach_mil, _EDGE_FLOOR_MIL)
        run = _EdgeRun(model, search, cap)
        run.prims()
        run.areas()
        run.covered_cutouts()
        run.covered_outline()
        result = run.result(keep=True)
        if search == reach_mil:
            return result
        cut = _narrow_edge(model, result, reach_mil, cap)
        cut.stopped = result.stopped
        return cut

    def narrow(base, reach):
        return _narrow_edge(model, base, reach, cap)

    return model.cached("copper_to_edge", reach_mil, cap, compute, narrow)


# ---------- holes ----------


def holes(index) -> list[Hole]:
    """Every drilled hole: vias, pad holes (plated or not) and bare holes. Builds no
    copper model."""
    return copper_model(index).holes


def hole_to_hole(index, reach_mil: float, cap: int = MAX_DISTANCE_CALLS) -> HoleResult:
    """Pairs of holes closer than reach_mil - CLEARANCE_TOLERANCE_MIL edge to edge (slots
    as capsules) whose drilled spans overlap. Cached on the index (Model.cached)."""
    model = copper_model(index)

    def compute():
        return _hole_pairs(model, reach_mil, cap)

    def narrow(base, reach):
        cut = reach - CLEARANCE_TOLERANCE_MIL
        records = base.records[:bisect_left([record.gap_mil for record in base.records], cut)]
        return HoleResult(reach, base.holes, records, base.distance_calls, False)

    return model.cached("hole_to_hole", reach_mil, cap, compute, narrow)


def _hole_pairs(model: _Model, reach: float, cap: int) -> HoleResult:
    limit = reach - CLEARANCE_TOLERANCE_MIL
    half = reach / 2.0
    size = max(ITEM_CELL_MIL / 2.0, 4.0 * reach)
    floor = math.floor
    all_holes = model.holes
    entries = []
    grid: dict[tuple[int, int], list[int]] = defaultdict(list)
    for number, hole in enumerate(all_holes):
        geometry = hole.geometry
        box = _BOUNDS(geometry)
        entries.append((box[0] - half, box[1] - half, box[2] + half, box[3] + half, geometry))
        xs, ys = _cells(*entries[-1][:4], size)
        for gx in xs:
            for gy in ys:
                grid[(gx, gy)].append(number)
    records: list[HolePair] = []
    calls = 0
    stopped = False
    for (gx, gy), members in grid.items():
        if calls > cap:
            stopped = True
            break
        for i, first in enumerate(members):
            ax0, ay0, ax1, ay1, ga = entries[first]
            a = all_holes[first]
            for second in members[i + 1:]:
                bx0, by0, bx1, by1, gb = entries[second]
                if bx0 > ax1 or ax0 > bx1 or by0 > ay1 or ay0 > by1:
                    continue
                if floor(max(ax0, bx0) / size) != gx or floor(max(ay0, by0) / size) != gy:
                    continue
                b = all_holes[second]
                if a.span is not None and b.span is not None:
                    # Holes meet in depth when their drilled spans share a dielectric:
                    # stacked microvias meeting at one copper layer do not.
                    low, high = max(a.span[0], b.span[0]), min(a.span[1], b.span[1])
                    if high < low or (high == low and a.span[0] < a.span[1]
                                      and b.span[0] < b.span[1]):
                        continue
                calls += 1
                gap = _GEO(ga, gb)
                if gap >= limit:
                    continue
                marks = set()
                if gap <= TOUCH_MIL:
                    marks.add("touching")
                    if math.dist((a.x, a.y), (b.x, b.y)) <= TOUCH_MIL \
                            and abs(a.hole_mil - b.hole_mil) <= EPS_MIL:
                        marks.add("coincident")
                if a.hole_offset or b.hole_offset:
                    marks.add("hole_offset")
                records.append(HolePair(a, b, gap, frozenset(marks)))
    records.sort(key=lambda record: (record.gap_mil, record.first.x, record.first.y))
    return HoleResult(reach, len(all_holes), records, calls, stopped, _state=True)


# ---------- hole walls ----------


def _wall_skips(hole: Hole, net: int | None) -> bool:
    """Copper a wall does not concern: a plated wall (a plated bare hole's barrel) is
    copper of its net, so copper of that net, or no-net copper when it has none, is no
    gap. A non-plated wall concerns copper of every net."""
    return hole.plated is not False and net == hole.net


class _WallRun:
    """One hole_to_copper run over Model.walls."""

    def __init__(self, model: _Model, reach: float, cap: int):
        self.model = model
        self.reach = reach
        self.limit = reach - CLEARANCE_TOLERANCE_MIL
        self.cap = cap
        self.calls = 0
        self.stopped = False
        # (wall number, owner) -> [gap, layer, {layer: gap}, hole, side, geometries]
        self.pairs: dict[tuple, list] = {}

    def note(self, number, hole, side, layer, gap, wall, geometry) -> None:
        key = (number, side[5])
        entry = self.pairs.get(key)
        if entry is None:
            self.pairs[key] = [gap, layer, {layer: gap}, hole, side, (wall, geometry)]
            return
        _least(entry[2], layer, gap)
        position = self.model.position
        if gap < entry[0] or (gap == entry[0] and position[layer] < position[entry[1]]):
            entry[0], entry[1], entry[4], entry[5] = gap, layer, side, (wall, geometry)

    def run(self) -> None:
        model = self.model
        if self.limit <= 0 or not model.walls:
            return
        index = model.index
        sql = _RTREE_SQL if index._has_rtree else _BOX_SQL
        grow = self.reach + model.overhang
        for number, (hole, layers) in enumerate(model.walls):
            if self.calls > self.cap:
                self.stopped = True
                return
            wall = hole.geometry
            x0, y0, x1, y1 = _BOUNDS(wall)
            wanted = set(layers)
            for (row,) in index.db.execute(sql, (x1 + grow, x0 - grow, y1 + grow, y0 - grow)):
                if row == hole.row:
                    continue
                for copper, item, item_layers in model.row_items.get(row, ()):
                    common = [layer for layer in item_layers if layer in wanted]
                    if not common or _wall_skips(hole, item[4]):
                        continue
                    self.calls += 1
                    gap = _GEO(wall, copper)
                    if gap < self.limit:
                        for layer in common:
                            self.note(number, hole, item, layer, gap, wall, copper)
            for layer in layers:
                self._areas(number, hole, wall, layer)

    def _areas(self, number: int, hole: Hole, wall, layer: int) -> None:
        """Area copper of the layer near the wall (its ring edges), and areas the hole
        lies wholly inside (its centre in their copper)."""
        model, reach, limit = self.model, self.reach, self.limit
        chunks = model.chunks.get(layer)
        if not chunks:
            return
        bx0, by0, bx1, by1 = _BOUNDS(wall)
        x0, y0, x1, y1 = bx0 - reach, by0 - reach, bx1 + reach, by1 + reach
        grid = model.any_grid(layer)
        seen: set[int] = set()
        xs, ys = _cells(x0, y0, x1, y1, AREA_CELL_MIL)
        for gx in xs:
            for gy in ys:
                seen.update(grid.get((gx, gy), ()))
        best: dict = {}
        for chunk in seen:
            cx0, cy0, cx1, cy1, area, points = chunks[chunk]
            if cx0 > x1 or cx1 < x0 or cy0 > y1 or cy1 < y0 or _wall_skips(hole, area.net):
                continue
            known = best.get(area)
            least, edge = (known if known else (math.inf, None))
            for p, q in zip(points, points[1:]):
                if (p[0] < x0 and q[0] < x0) or (p[0] > x1 and q[0] > x1) or \
                        (p[1] < y0 and q[1] < y0) or (p[1] > y1 and q[1] > y1):
                    continue
                self.calls += 1
                segment = (p[0], p[1], q[0], q[1])
                distance = _GEO(wall, (segment, 0.0, None, None))
                if distance < least:
                    least, edge = distance, segment
            if edge is not None:
                best[area] = (least, edge)
        for area, (gap, segment) in best.items():
            if gap < limit:
                self.note(number, hole, area.side, layer, gap, wall, (segment, 0.0, None, None))
        for area in model.area_layers.get(layer, ()):
            box = area.bbox
            if not (box[0] <= bx0 and bx1 <= box[2] and box[1] <= by0 and by1 <= box[3]) \
                    or _wall_skips(hole, area.net):
                continue
            self.calls += 1
            if model.area_contains(area, hole.x, hole.y):
                self.note(number, hole, area.side, layer, 0.0, wall, _point(hole.x, hole.y))

    def result(self) -> HoleCopperResult:
        stale = self.model.stale_pours
        position = self.model.position
        records = []
        for entry in self.pairs.values():
            gap, layer, gaps, hole, side, geometries = entry
            marks = set()
            if gap <= TOUCH_MIL:
                marks.add("touching")
            if hole.component is not None and side[9] == hole.component:
                marks.add("same_part")
            if side[10] in stale:
                marks.add("stale_pour")
            if hole.hole_offset:
                marks.add("hole_offset")
            layers, spread = _layer_split(gaps, gap, position)
            records.append(HoleCopperRecord(
                hole=hole, copper=_copper(side), gap_mil=gap, layer=layer, layers=layers,
                marks=frozenset(marks), _layer_gaps=spread, _geometries=geometries,
            ))
        records.sort(key=lambda record: (record.gap_mil, record.hole.x, record.hole.y,
                                         record.copper.owner))
        return HoleCopperResult(
            reach_mil=self.reach, walls=len(self.model.walls), records=records,
            distance_calls=self.calls, stopped=self.stopped, _state=True,
        )


def hole_to_copper(index, reach_mil: float, cap: int = MAX_DISTANCE_CALLS) -> HoleCopperResult:
    """Copper closer than reach_mil - CLEARANCE_TOLERANCE_MIL to a hole wall that no
    copper of the hole covers (Model.walls: a non-plated pad's hole where the pad has no
    copper on the layer, a bare hole): copper of any net, no-net copper included, to a
    non-plated wall; copper of other nets to a plated bare hole's barrel. One record per
    (hole, copper owner). Cached on the index (Model.cached)."""
    model = copper_model(index)

    def compute():
        run = _WallRun(model, reach_mil, cap)
        run.run()
        return run.result()

    def narrow(base, reach):
        cut = reach - CLEARANCE_TOLERANCE_MIL
        records = [_narrow_layers(record, cut) for record in base.records if record.gap_mil < cut]
        return HoleCopperResult(reach, base.walls, records, base.distance_calls, False)

    return model.cached("hole_to_copper", reach_mil, cap, compute, narrow)


def hole_to_edge(index, reach_mil: float, cap: int = MAX_DISTANCE_CALLS) -> HoleEdgeResult:
    """Holes without copper of their own around them (non-plated pad holes, bare holes)
    closer than reach_mil - CLEARANCE_TOLERANCE_MIL to the board edge or crossing it:
    their wall to the nearest outline or cutout piece. Holes wholly off the board are
    left out, and so are plated bare holes the edge cuts (copper_to_edge's edge_holes).
    Cached on the index (Model.cached)."""
    model = copper_model(index)

    def compute():
        edge = model.edge
        limit = reach_mil - CLEARANCE_TOLERANCE_MIL
        records: list[HoleEdgeRecord] = []
        calls, stopped = 0, False
        if edge.has_outline and limit > 0:
            for hole in model.holes:
                if hole.kind != "bare_hole" and not (hole.kind == "pad" and hole.plated is False):
                    continue
                if calls > cap:
                    stopped = True
                    break
                wall = hole.geometry
                tested, near = edge.nearest(wall, reach_mil, limit)
                calls += tested
                if near is None:
                    continue
                distance, piece = near
                crosses = distance <= TOUCH_MIL
                if not crosses and not edge.on_board(hole.x, hole.y):
                    continue  # off the board
                if hole.plated is not False and edge.cut(hole) is not None:
                    continue  # an edge-plated hole
                marks = {"crosses"} if crosses else set()
                if hole.hole_offset:
                    marks.add("hole_offset")
                records.append(HoleEdgeRecord(
                    hole=hole, distance_mil=distance, edge=piece[3], cutout=piece[4],
                    cutout_component=piece[5], marks=frozenset(marks),
                    _geometries=(wall, piece[0]),
                ))
        records.sort(key=lambda record: (record.distance_mil, record.hole.x, record.hole.y))
        return HoleEdgeResult(reach_mil, edge.has_outline, records, calls, stopped,
                              _state=True if edge.has_outline else None)

    def narrow(base, reach):
        cut = reach - CLEARANCE_TOLERANCE_MIL
        records = [record for record in base.records if record.distance_mil < cut]
        return HoleEdgeResult(reach, base.has_outline, records, base.distance_calls, False)

    return model.cached("hole_to_edge", reach_mil, cap, compute, narrow)


# ---------- board minimums ----------


def _widest(model: _Model, name: str, reach: float):
    """The cached result of a name with the largest reach of at least `reach`."""
    best = None
    for (cached_reach, _), result in model.results[name].items():
        if cached_reach >= reach - 1e-9 and (best is None or cached_reach > best[0]):
            best = (cached_reach, result)
    return best[1] if best else None


def board_minimums(
    index,
    gap_reach_mil: float = DEFAULT_GAP_REACH_MIL,
    edge_reach_mil: float = DEFAULT_EDGE_REACH_MIL,
    hole_reach_mil: float = DEFAULT_HOLE_REACH_MIL,
) -> dict:
    """What the board needs from a fabricator, independent of any profile: the smallest
    copper gap, copper-to-edge distance and hole-to-hole gap, each searched up to its
    reach (value_mil None: nothing closer than the reach). A cached result of a larger
    reach is used when there is one."""
    model = copper_model(index)
    gaps = _widest(model, "spacing", gap_reach_mil) or spacing(index, gap_reach_mil)
    edges = (_widest(model, "copper_to_edge", edge_reach_mil)
             or copper_to_edge(index, edge_reach_mil))
    drills = _widest(model, "hole_to_hole", hole_reach_mil) or hole_to_hole(index, hole_reach_mil)
    gap = gaps.minimum()
    edge = edges.minimum()
    hole_gap = drills.minimum()
    return {
        "min_gap": {
            "value_mil": gap.gap_mil if gap else None,
            "layer": gap.layer if gap else None,
            "reach_mil": gaps.reach_mil,
            "touching_pairs": sum(1 for record in gaps.records if "touching" in record.marks),
            "stopped": gaps.stopped,
        },
        "min_copper_to_edge": {
            "value_mil": edge.distance_mil if edge else None,
            "layer": edge.layer if edge else None,
            "reach_mil": edges.reach_mil,
            "has_outline": edges.has_outline,
        },
        "min_hole_to_hole": {
            "value_mil": hole_gap.gap_mil if hole_gap else None,
            "reach_mil": drills.reach_mil,
            "overlapping_pairs": sum(1 for record in drills.records if "touching" in record.marks),
        },
    }
