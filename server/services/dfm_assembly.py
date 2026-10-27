"""Assembly (DFA) facts for check_dfm over a PcbIndex: per part its pads and terminals,
mount class, side, pad pitch, chip size, 3D bodies and heights and fiducial flag;
free-pad fiducials; placements per side; the nearest same-side neighbour of each part;
each part's distance to the board edge; fiducials near fine-pitch parts; how the
terminals of two-terminal passives meet their copper (tombstoning); and each part's
pick-and-place centre as Altium writes it, with the board origin against the outline.
The engine holds no limits: dfm.py compares the facts with a profile.

Pure logic, no MCP. Lengths are mils and coordinates absolute (the caller makes them
origin-relative). The facts are built once per index, each pass when it is first asked
for, and cached on it (PcbIndex._dfm_assembly): the parts and fiducials, the spacing
search per set of left-out parts and the close pairs and edge search per reach (a few
kept each), the tombstoning facts and the centroids. The model holds the index weakly, so a replaced
index is freed as soon as nothing else holds it. DNP depends on the variant, so it is
applied per call (Part.fitted, placements, part_spacing's exclude).

Parts: a part's pads are its Pads6 pads (prims row ids). Its SMD pads form terminals: pads
on one layer with the same pad name (not blank), or whose copper touches or overlaps, are
one terminal (a pin drawn as several pads). Mount class from the pads that are soldered:
smd (no hole), tht (plated holes; unknown plating counts as plated), mixed, mechanical
(only non-plated holes, or a Mechanical-kind part whose pads are all holes: a mounting
hole) or no_pads. Pitch: the nearest centre distance between pads of two terminals on one
layer, for parts with at least PITCH_MIN_TERMINALS SMD terminals. A two-terminal passive
(only SMD pads, two terminals, designator R/C/L/FB + digit) gets a size code from its
footprint name (bom_checks' patterns, IPC-7351 names are metric), else from its body
outline in the part's frame (EIA body sizes within BODY_SLACK), else from the span of its
pads (outer edge to edge). Bodies: the ComponentBodies6 outlines with at least 3 vertices
projected on the part's side. A fiducial: a part of at most FIDUCIAL_MAX_PADS pads whose
designator starts with FID or whose footprint names fid or fiducial as a word (RFID is
not one), or with exactly one pad, no hole, no net, round or square, FIDUCIAL_MIN_MIL to
FIDUCIAL_MAX_MIL; or a free pad (no part) with no hole and no net, round, in that size
range, on the top or bottom layer.

Spacing: each part (graphical parts and net ties left out, and parts whose centre is off
the board) against its same-side neighbours. A part is its 3D body outlines on that side
and its pads' copper, or the pad extent (its pads as one rectangle in the part's frame)
when it has no body. The gap is the smallest of the body-to-body, body-to-pad and
pad-to-pad distances, and its basis says which set it (body, mixed or pads). Outlines that
share area overlap (bodies placed on top of each other, a body over another part's pads);
the overlap's basis is body before mixed before pads. part_spacing leaves out the parts
it is given (a variant's Not Fitted ones), so each part's nearest neighbour is searched
among the others. A grid prefilter and the box distance as a lower bound keep the exact
distances few.

Edge: each part's pad copper (main copper, as dfm_geometry measures it) and its bodies
against dfm_geometry's board edge (the outline with exact arcs and the board cutouts; a
part's own cutouts are not its edge). A part whose centre is off the board is skipped
(check_pcb_health covers it); pad copper reaching or crossing the edge, bodies past it
and edge-connector fingers (dfm_geometry.copper_to_edge's FingerGroups, asked for only
when a part could be one) are marked.

Tombstoning: each pad of a two-terminal passive is sampled on a ring RING_MIL outside its
edge (RING_POINTS points) against same-net area copper on its layer (regions, fills, solid
pour islands and a hatched pour's tracks and arcs): at least DIRECT_COVERAGE covered is a
direct connection, any is a relief, none is none (Altium's saved relief copper runs its
spokes into the pad, so the centre cannot tell). The widest track or arc of the pad's net
touching its copper is its widest attached track. A terminal of several pads takes its
heaviest pad's connection and its widest track.

Pick and place: a part's Center-X/Y as Altium's pick-and-place file writes it, the
midpoint of the box of its pad centres over every pad (SMD and through-hole, any layer;
pad size plays no part), and the absolute origin (0, 0) for a part without pads; the file
counts from the board's relative origin (Board6 ORIGINX/Y). Map: 40 of Altium's own files,
for 25 boards of a private set, agree on 2571 of 2571 rows of unchanged parts within
0.0001 mm (the copper extent's centre misses 304 of them, by up to 1.4 mm), bottom-side
parts unmirrored, and all 12 pad-less rows sit at the absolute origin (2 of them on boards
whose relative origin is set, so not at the file's 0,0). Bare-hole pads count (they are
Pads6 pads). Cases the files
did not settle are marked per part where they change the centre (Centroid.unverified):
bare-hole pads, pads on both sides (all are counted), a rotation off the 90-degree grid
(the box is taken in the board's frame). The origin is classed against the outline's
extent (arcs included).
"""

import math
import re
import weakref
from collections import defaultdict
from dataclasses import dataclass, field

try:
    from parsers.pcb_doc import (
        PAD_SHAPE_RECT,
        PAD_SHAPE_ROUND,
        PAD_SHAPE_ROUNDRECT,
    )
    from services import dfm_geometry as geometry
    from services.bom import BOM_KINDS
    from services.bom_checks import IPC_SIZE, METRIC_SIZE, _PASSIVE, _SIZE
    from services.fab_profiles import MM
    from services.pcb_compliance import CLEARANCE_TOLERANCE_MIL
    from services.pcb_geometry import (
        oriented_rect_vertices,
        point_in_polygon,
        point_seg_distance,
    )
except ImportError:
    from server.parsers.pcb_doc import (
        PAD_SHAPE_RECT,
        PAD_SHAPE_ROUND,
        PAD_SHAPE_ROUNDRECT,
    )
    from server.services import dfm_geometry as geometry
    from server.services.bom import BOM_KINDS
    from server.services.bom_checks import IPC_SIZE, METRIC_SIZE, _PASSIVE, _SIZE
    from server.services.fab_profiles import MM
    from server.services.pcb_compliance import CLEARANCE_TOLERANCE_MIL
    from server.services.pcb_geometry import (
        oriented_rect_vertices,
        point_in_polygon,
        point_seg_distance,
    )

# Pitch: the nearest pad-centre distance between terminals, for parts with at least this
# many SMD terminals.
PITCH_MIN_TERMINALS = 4
# Fiducials: a single unnetted SMD pad (or a free pad) this size, round or square; a part
# named a fiducial has at most FIDUCIAL_MAX_PADS pads (its copper dot, a mask or keepout
# pad).
FIDUCIAL_MIN_MIL = 0.5 * MM
FIDUCIAL_MAX_MIL = 3.0 * MM
FIDUCIAL_MAX_PADS = 3
_SQUARE_SHARE = 0.01  # width and height within this share count as square or round
# Local fiducials: same-side fiducials this close to a part's centre.
LOCAL_FIDUCIAL_MIL = 50.0 * MM
# Spacing: each part's nearest neighbour is looked for this far (box to box).
NEIGHBOUR_REACH_MIL = 5.0 * MM
# Outlines sharing more area than this overlap; less is touching (grid rounding).
OVERLAP_AREA_MIL2 = 1.0
# Edge: the default search reach of part_to_edge; fingers are judged at dfm_geometry's
# default edge reach (1 mm), where check_dfm's own copper_to_edge result is cached.
DEFAULT_EDGE_REACH_MIL = 10.0 * MM
FINGER_REACH_MIL = geometry.DEFAULT_EDGE_REACH_MIL
# Tombstoning (map_components: validated on the examples' PolygonConnect rules).
RING_MIL = 2.0
RING_POINTS = 48
DIRECT_COVERAGE = 0.85
TRACK_IMBALANCE_MIL = 0.5 * MM
TRACK_IMBALANCE_RATIO = 2.0
# Chip sizes: the codes tombstoning looks at, the EIA body (length, width) of each code
# and how far a body may differ, and the largest outer pad span of the small codes.
SMALL_SIZES = ("01005", "0201", "0402", "0603")
BODY_SIZES_MM = {
    "01005": (0.4, 0.2), "0201": (0.6, 0.3), "0402": (1.0, 0.5), "0603": (1.6, 0.8),
    "0805": (2.0, 1.25), "1206": (3.2, 1.6), "1210": (3.2, 2.5), "1812": (4.5, 3.2),
    "2010": (5.0, 2.5), "2512": (6.3, 3.2),
}
BODY_SLACK = 0.25
# The largest outer pad span (mm) per size code; corpus spans of named chips (5% to
# 95%): 0201 1.1, 0402 1.4-2.0, 0603 2.2-2.5, 0805 2.6-3.0, 1206 and 1210 alike
# (3.7-4.4), so larger codes are not told by their span.
SPAN_SIZES_MM = (("0201", 1.2), ("0402", 2.05), ("0603", 2.55), ("0805", 3.1))
SMALL_SPAN_MIL = 2.55 * MM
# Pick and place: the file's resolution (4 decimals in mm), below which two centres are
# one; the relative origin within ORIGIN_TOLERANCE_MIL of a corner of the outline's extent
# is at that corner (0.1 mm, well inside any placement tolerance: 4 boards of the private
# set have it 0.01 to 0.05 mm from the lower-left corner and none 0.05 to 0.5 mm);
# rotations within ORTHOGONAL_DEG of the 90-degree grid are on it.
PNP_RESOLUTION_MIL = 0.0001 * MM
ORIGIN_TOLERANCE_MIL = 0.1 * MM
ORTHOGONAL_DEG = 0.001
# Graphical parts and net ties (not in the BOM) have no assembly geometry.
_NO_ASSEMBLY_KINDS = (2, 4)
_MECHANICAL_KIND = 1
_FIDUCIAL_DESIGNATOR = re.compile(r"^FID", re.IGNORECASE)
# fid or fiducial(s) as a word: Fiducial_1mm, FID1, TP_FID; not RFID or NFID.
_FIDUCIAL_FOOTPRINT = re.compile(r"(?:^|[^a-z])fid(?:ucials?)?(?:[^a-z]|$)", re.IGNORECASE)
_STYLE_RANK = {"none": 0, "relief": 1, "direct": 2}
_TOP, _BOTTOM = 1, 32
_GRID_MIL = 200.0
_RESULTS_KEPT = 4
_GEO = geometry._GEO
_BOUNDS = geometry._BOUNDS
_TRACK_SQL = (
    "SELECT p.id, p.kind, p.x1, p.y1, p.x2, p.y2, p.width, p.arc_x, p.arc_y, p.arc_radius, "
    "p.poured_by FROM prims_rtree r CROSS JOIN prims p ON p.id = r.id "
    "WHERE r.minx <= ? AND r.maxx >= ? AND r.miny <= ? AND r.maxy >= ? "
    "AND p.kind IN ('track', 'arc') AND p.pour_id IS NULL AND p.layer = ? AND p.net_id = ?"
)
_TRACK_BOX_SQL = (
    "SELECT p.id, p.kind, p.x1, p.y1, p.x2, p.y2, p.width, p.arc_x, p.arc_y, p.arc_radius, "
    "p.poured_by FROM prims p "
    "WHERE p.minx <= ? AND p.maxx >= ? AND p.miny <= ? AND p.maxy >= ? "
    "AND p.kind IN ('track', 'arc') AND p.pour_id IS NULL AND p.layer = ? AND p.net_id = ?"
)


# ---------- facts ----------


@dataclass
class Body:
    """A 3D body of a part on its side: the outline (absolute) and heights."""

    outline: list[tuple[float, float]]  # at least 3 vertices
    overall_height_mil: float | None
    standoff_height_mil: float | None
    model_type: int | None  # 0 extruded, 1 STEP, 2 cylinder, 3 sphere
    layer: int  # the mechanical layer it is drawn on


@dataclass
class Part:
    """The assembly facts of one PCB component (pcb.components[component])."""

    component: int
    designator: str | None  # the PCB designator (PcbIndex.designator)
    sch_refdes: str | None  # its netlist name; None without a netlist or unmatched
    side: str  # top | bottom
    kind: int  # ComponentKind
    footprint: str | None
    comment: str | None
    x: float | None  # placement origin
    y: float | None
    rotation: float
    pads: tuple[int, ...]  # prims row ids (pcb.primitives[row - 1])
    terminals: tuple[tuple[int, ...], ...]  # the SMD pads (row ids) per terminal
    smd_pads: int
    plated_pads: int  # pads with a plated hole (unknown plating counts as plated)
    npth_pads: int  # pads with a non-plated hole
    mount: str  # smd | tht | mixed | mechanical | no_pads
    pitch_mil: float | None  # nearest pad centres of two terminals (>= PITCH_MIN_TERMINALS)
    passive: bool  # only SMD pads, two terminals and a R/C/L/FB designator
    span_mil: float | None  # passives: outer pad edge to outer pad edge
    size_code: str | None  # passives: "0402", ...
    size_basis: str | None  # footprint | body | pad_span
    small: bool  # passives of SMALL_SIZES, or of unknown size within SMALL_SPAN_MIL
    bodies: tuple[Body, ...]
    height_mil: float | None  # the tallest body's overall height
    fiducial: str | None  # designator | footprint | single_pad; None: not a fiducial
    centre: tuple[float, float]  # of the pad extent, else of the bodies, else the origin
    near_fiducials: int | None = None  # parts with a pitch: same-side fiducials nearby
    pad_rect: list | None = field(default=None, repr=False)  # pad extent, part's frame
    pad_box: tuple | None = field(default=None, repr=False)
    body_box: tuple | None = field(default=None, repr=False)

    @property
    def name(self) -> str | None:
        """The name a variant's DNP list uses: the netlist name, else the designator."""
        return self.sch_refdes or self.designator

    @property
    def solderable(self) -> bool:
        return self.mount in ("smd", "tht", "mixed")

    def fitted(self, is_dnp=None) -> bool:
        """Not DNP in the active variant (is_dnp: VariantState.is_dnp or None). A name
        is_dnp cannot judge counts as fitted."""
        if is_dnp is None or not self.name:
            return True
        try:
            return not is_dnp(self.name)
        except Exception:
            return True


@dataclass
class Fiducial:
    x: float
    y: float
    side: str
    size_mil: float  # the pad's larger side
    reason: str  # designator | footprint | single_pad | free_pad
    component: int | None  # None for a free pad
    row: int | None  # prims row id of its pad


@dataclass
class Placements:
    """Assembled parts: fitted, ComponentKind in the BOM, solderable pads, not fiducials."""

    components: list[int]
    counts: dict[str, dict[str, int]]  # {"top": {smd, tht, mixed}, "bottom": {...}}
    not_fitted: list[int]  # parts that would be placed but are DNP
    unique: int  # distinct (footprint, comment), case-folded: approximate unique parts


@dataclass
class PartGap:
    """Two same-side parts and the gap between them."""

    first: int  # component indices, first < second
    second: int
    side: str
    gap_mil: float  # 0 when they touch or overlap
    basis: str  # body | pads | mixed
    overlap: bool  # the outlines share area (more than OVERLAP_AREA_MIL2)
    _geometries: tuple = field(default=(), repr=False, compare=False)
    _at: tuple | None = field(default=None, repr=False, compare=False)

    @property
    def at(self) -> tuple[float, float]:
        """Where the outlines meet or come closest (computed on first use)."""
        if self._at is None:
            self._at = geometry._meeting_point(*self._geometries, self.gap_mil)
        return self._at


@dataclass
class SpacingFacts:
    reach_mil: float  # NEIGHBOUR_REACH_MIL
    members: int  # parts measured (the parts left out and those off the board are not)
    nearest: dict[int, PartGap]  # part -> its nearest same-side neighbour within reach
    overlaps: list[PartGap]  # every pair whose outlines overlap
    distance_calls: int

    def minimum(self) -> PartGap | None:
        """The smallest gap between parts whose outlines do not overlap."""
        return min(
            (gap for gap in self.nearest.values() if not gap.overlap),
            key=lambda gap: (gap.gap_mil, gap.first, gap.second), default=None,
        )


@dataclass
class PartEdge:
    """A part's pad copper and bodies against the board edge (within the reach)."""

    component: int
    pad_distance_mil: float | None  # None: no pads, or none within the reach
    pad_edge: str | None  # outline | cutout
    pad_at: tuple[float, float] | None  # the point of the copper nearest the edge
    body_distance_mil: float | None
    body_edge: str | None
    body_at: tuple[float, float] | None
    crosses: bool = False  # pad copper reaches or crosses the edge
    overhang: bool = False  # a body reaches or crosses the edge
    fingers: bool = False  # an edge connector's fingers (dfm_geometry.FingerGroup)


@dataclass
class EdgeFacts:
    reach_mil: float
    has_outline: bool  # False: nothing measured
    parts: dict[int, PartEdge]  # parts with pads or bodies closer than the reach
    off_board: list[int]  # parts whose centre is off the board (skipped)
    fingers: list  # dfm_geometry.FingerGroup of the finger parts

    def minimum(self) -> PartEdge | None:
        """The part whose pad copper is nearest the edge: fingers and pads crossing the
        edge (their own classes) left out."""
        return min(
            (edge for edge in self.parts.values()
             if edge.pad_distance_mil is not None and not edge.fingers and not edge.crosses),
            key=lambda edge: (edge.pad_distance_mil, edge.component), default=None,
        )


@dataclass
class PadConnection:
    row: int  # prims row id
    layer: int
    net: int | None
    style: str  # direct | relief | none
    coverage: float  # share of the ring points in same-net area copper
    widest_track_mil: float  # 0.0 when no track touches the pad


@dataclass
class Tombstone:
    """A two-terminal passive's terminal connections; causes name an imbalance."""

    component: int
    size_code: str | None
    size_basis: str | None
    small: bool
    pads: tuple[PadConnection, PadConnection]  # per terminal, in pad order
    # direct: one terminal direct-connected to area copper and the other not; track:
    # neither touches area copper, and one's widest track is at least
    # TRACK_IMBALANCE_MIL and TRACK_IMBALANCE_RATIO times the other's.
    causes: tuple[str, ...]


@dataclass
class Centroid:
    """A part's pick-and-place facts (absolute; the file subtracts the board origin)."""

    component: int
    # Center-X/Y: the midpoint of the box of its pad centres; None: no pads, and Altium
    # writes the absolute origin (0, 0).
    centre: tuple[float, float] | None
    reference: tuple[float, float]  # Ref-X/Y: the footprint's reference point
    reference_offset_mil: float | None  # centre to reference point
    # The footprint's extent: its 3D bodies' box centre (body), else its pad copper
    # extent's (pads); and the centre's distance from it.
    extent_centre: tuple[float, float] | None
    extent_basis: str | None  # body | pads
    extent_offset_mil: float | None
    # Cases Altium's files did not settle that move the centre here (by more than
    # PNP_RESOLUTION_MIL): bare_holes, both_sides, rotation.
    unverified: tuple[str, ...] = ()


@dataclass
class OriginFacts:
    """The board's relative origin (Edit > Origin > Set: Board6 ORIGINX/Y, (0, 0) when
    never set) against the outline's extent."""

    origin: tuple[float, float]  # absolute
    box: tuple[float, float, float, float] | None  # the outline's extent; None: no outline
    # lower_left | not_set (the absolute origin, the outline elsewhere) | other_corner |
    # inside (the extent) | outside | no_outline
    position: str
    to_lower_left_mil: float | None  # origin to the extent's lower-left corner
    rigid_flex: bool  # the stack has a flex region: the outline read may be one region


# ---------- helpers ----------


def footprint_size(footprint: str | None) -> str | None:
    """The imperial chip size code a footprint name gives (IPC-7351 names are metric:
    RESC1005X40N is an 0402), as bom_checks reads it; None when it names none."""
    text = footprint or ""
    ipc = IPC_SIZE.search(text)
    if ipc:
        return METRIC_SIZE.get(ipc[1])
    match = _SIZE.search(text)
    return match[1] if match else None


def _body_size(length: float, width: float) -> str | None:
    """The size code whose EIA body is within BODY_SLACK of length x width (mil)."""
    best = None
    for code, (nominal_length, nominal_width) in BODY_SIZES_MM.items():
        error = max(abs(length / (nominal_length * MM) - 1.0),
                    abs(width / (nominal_width * MM) - 1.0))
        if error <= BODY_SLACK and (best is None or error < best[0]):
            best = (error, code)
    return best[1] if best else None


def _span_size(span: float) -> str | None:
    for code, most in SPAN_SIZES_MM:
        if span <= most * MM:
            return code
    return None


def points_box(points) -> tuple[float, float, float, float]:
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return min(xs), min(ys), max(xs), max(ys)


def _middle(points) -> tuple[float, float]:
    """The centre of the points' box."""
    box = points_box(points)
    return (box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0


def _moved(first, second) -> bool:
    return math.hypot(first[0] - second[0], first[1] - second[1]) > PNP_RESOLUTION_MIL


def outline_box(pcb) -> tuple[float, float, float, float] | None:
    """(min x, min y, max x, max y) of the board outline, arcs bulging included; None
    without an outline."""
    shape = pcb.board.outline_shape
    if len(shape) < 3:
        return None
    xs = [vertex.x for vertex in shape]
    ys = [vertex.y for vertex in shape]
    for vertex in shape:
        if vertex.arc is None:
            continue
        cx, cy, radius, start, end = vertex.arc
        sweep = (end - start) % 360.0 or 360.0
        if sweep < 0.2:
            continue
        for angle in (start, end, 0.0, 90.0, 180.0, 270.0):
            if (angle - start) % 360.0 <= sweep + 1e-9:
                xs.append(cx + radius * math.cos(math.radians(angle)))
                ys.append(cy + radius * math.sin(math.radians(angle)))
    return min(xs), min(ys), max(xs), max(ys)


def named_fiducial(part) -> bool:
    """The part's designator or footprint names a fiducial (pads or not)."""
    return bool(_FIDUCIAL_DESIGNATOR.match(part.designator or "")
                or _FIDUCIAL_FOOTPRINT.search(part.footprint or ""))


def _centroid(pcb, part, holes) -> Centroid:
    """Altium's pick-and-place facts of one part; holes: its bare-hole pads (x, y)."""
    pads = [pcb.primitives[row - 1] for row in part.pads]
    reference = (part.x if part.x is not None else 0.0, part.y if part.y is not None else 0.0)
    copper = [(pad.x1, pad.y1) for pad in pads]
    points = copper + list(holes)
    if not points:
        return Centroid(part.component, None, reference, None, None, None, None)
    centre = _middle(points)
    unverified = []
    if holes and (not copper or _moved(_middle(copper), centre)):
        unverified.append("bare_holes")
    layers = {pad.layer for pad in pads if not (pad.hole or 0.0) > 0}
    if _TOP in layers and _BOTTOM in layers:
        other = _BOTTOM if part.side == "top" else _TOP
        own = [(pad.x1, pad.y1) for pad in pads
               if (pad.hole or 0.0) > 0 or pad.layer != other] + list(holes)
        if own and _moved(_middle(own), centre):
            unverified.append("both_sides")
    turn = part.rotation % 90.0
    if min(turn, 90.0 - turn) > ORTHOGONAL_DEG:
        ox, oy = reference
        local = _middle(rotate_points(points, -part.rotation, ox, oy))
        if _moved(rotate_points([local], part.rotation, ox, oy)[0], centre):
            unverified.append("rotation")
    box = part.body_box or part.pad_box
    extent = basis = offset = None
    if box is not None:
        extent = ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0)
        basis = "body" if part.body_box is not None else "pads"
        offset = math.hypot(centre[0] - extent[0], centre[1] - extent[1])
    return Centroid(
        part.component, centre, reference,
        math.hypot(centre[0] - reference[0], centre[1] - reference[1]),
        extent, basis, offset, tuple(unverified),
    )


def _union(first, second):
    if first is None:
        return second
    if second is None:
        return first
    return (min(first[0], second[0]), min(first[1], second[1]),
            max(first[2], second[2]), max(first[3], second[3]))


def _union_all(boxes):
    found = None
    for box in boxes:
        found = _union(found, box)
    return found


def _box_gap(first, second) -> float:
    dx = max(first[0] - second[2], second[0] - first[2], 0.0)
    dy = max(first[1] - second[3], second[1] - first[3], 0.0)
    return math.hypot(dx, dy)


def rotate_points(points, angle: float, ox: float, oy: float) -> list[tuple[float, float]]:
    cosine, sine = math.cos(math.radians(angle)), math.sin(math.radians(angle))
    return [(ox + (x - ox) * cosine - (y - oy) * sine, oy + (x - ox) * sine + (y - oy) * cosine)
            for x, y in points]


def _pad_corners(primitive) -> list[tuple[float, float]]:
    """The pad's main copper as its rotated rectangle."""
    return oriented_rect_vertices(primitive.x1, primitive.y1, primitive.width,
                                  primitive.height or primitive.width, primitive.rotation or 0.0)


def _support(primitive, ux: float, uy: float) -> float:
    """Half the extent of a pad's rectangle along the unit direction (ux, uy)."""
    angle = math.radians(primitive.rotation or 0.0)
    along = ux * math.cos(angle) + uy * math.sin(angle)
    across = -ux * math.sin(angle) + uy * math.cos(angle)
    return (abs(primitive.width * along) + abs((primitive.height or primitive.width) * across)) / 2.0


def _terminals(index, pads) -> list[int]:
    """Each pad's terminal (the number of one of its pads): pads on one layer with the same
    pad name (not blank), or whose copper touches or overlaps, are one terminal."""
    parent = list(range(len(pads)))

    def root(number: int) -> int:
        while parent[number] != number:
            parent[number] = parent[parent[number]]
            number = parent[number]
        return number

    named: dict[tuple, int] = {}
    for number, pad in enumerate(pads):
        name = (pad.pad_name or "").strip()
        if name:
            first = named.setdefault((pad.layer, name), number)
            parent[root(number)] = root(first)
    shapes = [index._copper_geometry(pad, None) for pad in pads]
    boxes = [_BOUNDS(shape) for shape in shapes]
    order = sorted(range(len(pads)), key=lambda number: boxes[number][0])
    for place, number in enumerate(order):
        box = boxes[number]
        for other in order[place + 1:]:
            if boxes[other][0] > box[2] + geometry.TOUCH_MIL:
                break
            if pads[other].layer == pads[number].layer and root(other) != root(number) \
                    and _box_gap(box, boxes[other]) <= geometry.TOUCH_MIL \
                    and _GEO(shapes[number], shapes[other]) <= geometry.TOUCH_MIL:
                parent[root(other)] = root(number)
    return [root(number) for number in range(len(pads))]


def _pitch(pads, terminals) -> float | None:
    """The nearest distance between the centres of two pads of different terminals on one
    layer (a sweep by x); terminals[n]: the terminal of pads[n]."""
    points = sorted((pad.x1, pad.y1, pad.layer, terminal)
                    for pad, terminal in zip(pads, terminals))
    best = math.inf
    for number, (x, y, layer, terminal) in enumerate(points):
        for other in points[number + 1:]:
            dx = other[0] - x
            if dx >= best:
                break
            if other[2] == layer and other[3] != terminal:
                best = min(best, math.hypot(dx, other[1] - y))
    return None if best == math.inf else best


def _span(groups) -> float | None:
    """Outer pad edge to outer pad edge of two terminals (lists of pads) along the line
    through their centres (the mean of their pads' centres)."""
    centres = [(sum(pad.x1 for pad in pads) / len(pads), sum(pad.y1 for pad in pads) / len(pads))
               for pads in groups]
    dx, dy = centres[1][0] - centres[0][0], centres[1][1] - centres[0][1]
    distance = math.hypot(dx, dy)
    if distance <= 1e-6:
        return None
    ux, uy = dx / distance, dy / distance
    pads = [pad for group in groups for pad in group]
    return (max(pad.x1 * ux + pad.y1 * uy + _support(pad, ux, uy) for pad in pads)
            - min(pad.x1 * ux + pad.y1 * uy - _support(pad, ux, uy) for pad in pads))


def _squarish(primitive) -> bool:
    width, height = primitive.width, primitive.height or primitive.width
    return abs(width - height) <= _SQUARE_SHARE * max(width, height)


def _fiducial_size(primitive) -> bool:
    return FIDUCIAL_MIN_MIL <= max(primitive.width, primitive.height or 0.0) <= FIDUCIAL_MAX_MIL


def polygon_area(polygon) -> float:
    closed = list(polygon) + [polygon[0]]
    return abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(closed, closed[1:]))) / 2.0


def _orientation(polygon) -> float:
    """The sign of the polygon's turns: 1 or -1 when it is convex, else 0."""
    count = len(polygon)
    sign = 0.0
    for number in range(count):
        ax, ay = polygon[number]
        bx, by = polygon[(number + 1) % count]
        cx, cy = polygon[(number + 2) % count]
        cross = (bx - ax) * (cy - by) - (by - ay) * (cx - bx)
        if abs(cross) < 1e-9:
            continue
        if sign == 0.0:
            sign = math.copysign(1.0, cross)
        elif cross * sign < 0:
            return 0.0
    return sign


def _clip(subject, clip, sign: float) -> list[tuple[float, float]]:
    """Sutherland-Hodgman: the part of `subject` inside the convex `clip` (turning
    with `sign`)."""
    output = list(subject)
    count = len(clip)
    for number in range(count):
        if not output:
            break
        ax, ay = clip[number]
        bx, by = clip[(number + 1) % count]

        def inside(point):
            return sign * ((bx - ax) * (point[1] - ay) - (by - ay) * (point[0] - ax)) >= 0.0

        def cut(p, q):
            dx, dy = q[0] - p[0], q[1] - p[1]
            denominator = (bx - ax) * dy - (by - ay) * dx
            if denominator == 0:
                return q
            t = ((bx - ax) * (ay - p[1]) - (by - ay) * (ax - p[0])) / denominator
            return p[0] + t * dx, p[1] + t * dy

        points, output = output, []
        previous = points[-1]
        for point in points:
            if inside(point):
                if not inside(previous):
                    output.append(cut(previous, point))
                output.append(point)
            elif inside(previous):
                output.append(cut(previous, point))
            previous = point
    return output


def boundary_distance(x: float, y: float, polygon) -> float:
    closed = list(polygon) + [polygon[0]]
    return min(point_seg_distance(x, y, a[0], a[1], b[0], b[1]) for a, b in zip(closed, closed[1:]))


def outlines_overlap(first, second) -> bool:
    """Do two outlines share area (more than OVERLAP_AREA_MIL2), not just an edge?
    Exact when one is convex (clipping); else a vertex or centre of one well inside the
    other."""
    for subject, clip in ((first, second), (second, first)):
        sign = _orientation(clip)
        if sign:
            part = _clip(subject, clip, sign)
            return len(part) >= 3 and polygon_area(part) > OVERLAP_AREA_MIL2
    for polygon, other in ((first, second), (second, first)):
        centre = (sum(x for x, _ in polygon) / len(polygon), sum(y for _, y in polygon) / len(polygon))
        for x, y in [*polygon, centre]:
            if point_in_polygon(x, y, other) and boundary_distance(x, y, other) > geometry.TOUCH_MIL:
                return True
    return False


def _ring(primitive) -> list[tuple[float, float]]:
    """RING_POINTS points RING_MIL outside a pad's main copper: an ellipse for a round or
    oblong pad, the rectangle's perimeter for the others."""
    half_width = primitive.width / 2.0 + RING_MIL
    half_height = (primitive.height or primitive.width) / 2.0 + RING_MIL
    angle = math.radians(primitive.rotation or 0.0)
    cosine, sine = math.cos(angle), math.sin(angle)
    points = []
    for number in range(RING_POINTS):
        if primitive.shape == PAD_SHAPE_ROUND:
            t = 2.0 * math.pi * number / RING_POINTS
            x, y = half_width * math.cos(t), half_height * math.sin(t)
        else:
            u = 4.0 * number / RING_POINTS
            side, f = int(u), u - int(u)
            x, y = (
                (-half_width + 2 * half_width * f, -half_height),
                (half_width, -half_height + 2 * half_height * f),
                (half_width - 2 * half_width * f, half_height),
                (-half_width, half_height - 2 * half_height * f),
            )[side]
        points.append((primitive.x1 + x * cosine - y * sine, primitive.y1 + x * sine + y * cosine))
    return points


def _cells(box, size: float = _GRID_MIL):
    floor = math.floor
    return [(gx, gy) for gx in range(floor(box[0] / size), floor(box[2] / size) + 1)
            for gy in range(floor(box[1] / size), floor(box[3] / size) + 1)]


def _grow(box, by: float):
    return box[0] - by, box[1] - by, box[2] + by, box[3] + by


def _polygon(points) -> tuple:
    return None, 0.0, list(points), None


class _RingCopper:
    """One area's copper for ring sampling: its rings and box, and a y-band index of
    every edge (built on first use)."""

    __slots__ = ("bbox", "rings", "_edges", "_bands")

    def __init__(self, area):
        self.rings = [area.outline, *(hole for hole in area.holes if len(hole) >= 3)]
        self.bbox = points_box(area.outline)
        self._edges: list[tuple] | None = None
        self._bands: dict[int, list[int]] = {}

    def _index(self) -> None:
        self._edges = []
        bands, floor = defaultdict(list), math.floor
        for ring in self.rings:
            count = len(ring)
            for number in range(count):
                (x1, y1), (x2, y2) = ring[number], ring[(number + 1) % count]
                edge = len(self._edges)
                self._edges.append((x1, y1, x2, y2))
                for key in range(floor(min(y1, y2) / geometry.BAND_MIL),
                                 floor(max(y1, y2) / geometry.BAND_MIL) + 1):
                    bands[key].append(edge)
        self._bands = bands

    def contains(self, x: float, y: float) -> bool:
        """Even-odd over the rings (a horizontal edge never crosses the ray)."""
        minx, miny, maxx, maxy = self.bbox
        if not (minx <= x <= maxx and miny <= y <= maxy):
            return False
        if self._edges is None:
            self._index()
        inside, edges = False, self._edges
        for number in self._bands.get(math.floor(y / geometry.BAND_MIL), ()):
            x1, y1, x2, y2 = edges[number]
            if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
                inside = not inside
        return inside

    def crosses(self, box) -> bool:
        """Does an edge of the copper (by its box) reach into `box`?"""
        if _box_gap(self.bbox, box) > 0.0:
            return False
        if self._edges is None:
            self._index()
        x0, y0, x1, y1 = box
        edges, floor = self._edges, math.floor
        for key in range(floor(y0 / geometry.BAND_MIL), floor(y1 / geometry.BAND_MIL) + 1):
            for number in self._bands.get(key, ()):
                ax, ay, bx, by = edges[number]
                if (ax <= x1 or bx <= x1) and (ax >= x0 or bx >= x0) \
                        and (ay <= y1 or by <= y1) and (ay >= y0 or by >= y0):
                    return True
        return False


# ---------- the cached model ----------


class _Model:
    """The parts and fiducials (built on first use) and each pass's results."""

    def __init__(self, index):
        # Weak: index._dfm_assembly holds the model (see dfm_geometry._Model).
        self._index = weakref.ref(index)
        self._parts: list[Part] | None = None
        self._fiducials: list[Fiducial] | None = None
        self._on_board: list[Part] | None = None
        self._shapes: dict[int, tuple] = {}
        self._spacing: dict[frozenset, SpacingFacts] = {}
        self._pairs: dict[tuple[int, int], PartGap | None] = {}
        self._close: dict[float, list[PartGap]] = {}
        self._edges: dict[float, EdgeFacts] = {}
        self._tombstones: list[Tombstone] | None = None
        self._centroids: list[Centroid] | None = None
        self.distance_calls = 0

    @property
    def index(self):
        return self._index()

    @property
    def parts(self) -> list[Part]:
        if self._parts is None:
            self._build()
        return self._parts

    @property
    def fiducials(self) -> list[Fiducial]:
        if self._parts is None:
            self._build()
        return self._fiducials

    def _build(self) -> None:
        index = self.index
        pcb = index.pcb
        sch = {row["id"]: row["sch_refdes"]
               for row in index.db.execute("SELECT id, sch_refdes FROM components")}
        pads: dict[int, list[tuple[int, object]]] = defaultdict(list)
        free: list[tuple[int, object]] = []
        for row, primitive in enumerate(pcb.primitives, start=1):
            if primitive.kind != "pad":
                continue
            if primitive.component is None:
                free.append((row, primitive))
            else:
                pads[primitive.component].append((row, primitive))
        bodies: dict[int, list] = defaultdict(list)
        for body in getattr(pcb, "bodies", None) or ():
            if body.component is not None and len(body.outline) >= 3:
                bodies[body.component].append(body)
        parts = [self._part(index, component, index.designator(component.index),
                            sch.get(component.index), pads.get(component.index, []),
                            bodies.get(component.index, []))
                 for component in pcb.components]
        fiducials = []
        for part in parts:
            if part.fiducial:
                row, pad = next((row, pad) for row, pad in pads[part.component]
                                if not (pad.hole or 0.0) > 0)
                side = ("top" if pad.layer == _TOP else "bottom" if pad.layer == _BOTTOM
                        else part.side)
                fiducials.append(Fiducial(pad.x1, pad.y1, side, max(pad.width, pad.height or 0.0),
                                          part.fiducial, part.component, row))
        for row, pad in free:
            if (pad.hole or 0.0) > 0 or pad.net is not None or pad.layer not in (_TOP, _BOTTOM):
                continue
            if pad.shape == PAD_SHAPE_ROUND and _squarish(pad) and _fiducial_size(pad):
                fiducials.append(Fiducial(pad.x1, pad.y1, "top" if pad.layer == _TOP else "bottom",
                                          max(pad.width, pad.height or 0.0), "free_pad", None, row))
        for part in parts:
            if part.pitch_mil is not None:
                cx, cy = part.centre
                part.near_fiducials = sum(
                    1 for fiducial in fiducials
                    if fiducial.side == part.side and fiducial.component != part.component
                    and math.hypot(fiducial.x - cx, fiducial.y - cy) <= LOCAL_FIDUCIAL_MIL
                )
        self._parts, self._fiducials = parts, fiducials

    @staticmethod
    def _part(index, component, designator, sch_refdes, pads, bodies) -> Part:
        primitives = [pad for _, pad in pads]
        smd_rows = [(row, pad) for row, pad in pads if not (pad.hole or 0.0) > 0]
        smd = [pad for _, pad in smd_rows]
        npth = sum(1 for pad in primitives if (pad.hole or 0.0) > 0 and pad.plated is False)
        plated = len(primitives) - len(smd) - npth
        if not primitives:
            mount = "no_pads"
        elif smd and plated:
            mount = "mixed"
        elif smd:
            mount = "smd"
        elif plated and component.kind != _MECHANICAL_KIND:
            mount = "tht"
        else:
            mount = "mechanical"  # non-plated holes, or a Mechanical part's mounting hole
        owners = _terminals(index, smd) if smd else []
        groups: dict[int, list[int]] = {}
        for (row, _), owner in zip(smd_rows, owners):
            groups.setdefault(owner, []).append(row)
        terminals = tuple(tuple(rows) for rows in groups.values())
        rotation = component.rotation or 0.0
        ox = component.x if component.x is not None else 0.0
        oy = component.y if component.y is not None else 0.0
        pad_rect = pad_box = None
        if primitives:
            corners = [point for pad in primitives for point in _pad_corners(pad)]
            local = points_box(rotate_points(corners, -rotation, ox, oy))
            pad_rect = rotate_points([(local[0], local[1]), (local[2], local[1]), (local[2], local[3]),
                                (local[0], local[3])], rotation, ox, oy)
            pad_box = points_box(pad_rect)  # holds every pad's copper (and the rectangle)
        kept = tuple(
            Body(list(body.outline), body.overall_height_mil, body.standoff_height_mil,
                 body.model_type, body.layer)
            for body in bodies if body.projection in (None, component.side)
        )
        body_box = None
        for body in kept:
            body_box = _union(body_box, points_box(body.outline))
        heights = [body.overall_height_mil for body in kept if body.overall_height_mil is not None]
        if pad_box is not None:
            centre = ((pad_box[0] + pad_box[2]) / 2.0, (pad_box[1] + pad_box[3]) / 2.0)
        elif body_box is not None:
            centre = ((body_box[0] + body_box[2]) / 2.0, (body_box[1] + body_box[3]) / 2.0)
        else:
            centre = (ox, oy)
        name = designator or ""
        passive = (len(smd) == len(primitives) and len(terminals) == 2
                   and bool(_PASSIVE.match(name)))
        span = size = basis = None
        if passive:
            by_row = dict(pads)
            span = _span([[by_row[row] for row in rows] for rows in terminals])
            size = footprint_size(component.footprint)
            basis = "footprint" if size else None
            if size is None and kept:
                outline = [point for body in kept for point in body.outline]
                local = points_box(rotate_points(outline, -rotation, ox, oy))
                length, width = local[2] - local[0], local[3] - local[1]
                size = _body_size(max(length, width), min(length, width))
                basis = "body" if size else None
            if size is None and span is not None:
                size = _span_size(span)
                basis = "pad_span" if size else None
        small = passive and (size in SMALL_SIZES if size else (
            span is not None and span <= SMALL_SPAN_MIL))
        fiducial = None
        if smd and len(primitives) <= FIDUCIAL_MAX_PADS:
            if _FIDUCIAL_DESIGNATOR.match(name):
                fiducial = "designator"
            elif _FIDUCIAL_FOOTPRINT.search(component.footprint or ""):
                fiducial = "footprint"
            elif len(primitives) == 1 and smd[0].net is None and smd[0].shape in (
                PAD_SHAPE_ROUND, PAD_SHAPE_RECT, PAD_SHAPE_ROUNDRECT
            ) and _squarish(smd[0]) and _fiducial_size(smd[0]):
                fiducial = "single_pad"
        return Part(
            component=component.index, designator=designator, sch_refdes=sch_refdes,
            side=component.side, kind=component.kind, footprint=component.footprint,
            comment=component.comment, x=component.x, y=component.y, rotation=rotation,
            pads=tuple(row for row, _ in pads), terminals=terminals, smd_pads=len(smd),
            plated_pads=plated, npth_pads=npth, mount=mount,
            pitch_mil=(_pitch(smd, owners) if len(terminals) >= PITCH_MIN_TERMINALS
                       else None),
            passive=passive, span_mil=span, size_code=size, size_basis=basis, small=small,
            bodies=kept, height_mil=max(heights) if heights else None, fiducial=fiducial,
            centre=centre, pad_rect=pad_rect, pad_box=pad_box, body_box=body_box,
        )

    # ---------- spacing ----------

    def members(self) -> list[Part]:
        """Parts with assembly geometry: graphical parts and net ties left out."""
        return [part for part in self.parts if part.kind not in _NO_ASSEMBLY_KINDS
                and (part.pad_rect is not None or part.bodies)]

    def on_board(self) -> list[Part]:
        """The members whose centre is on the board (all without an outline): spacing's
        parts (a part parked off the board is part_to_edge's off_board)."""
        if self._on_board is None:
            edge = geometry.copper_model(self.index).edge
            self._on_board = [part for part in self.members()
                              if not edge.has_outline or edge.outline.contains(*part.centre)]
        return self._on_board

    def shapes(self, part: Part) -> tuple[tuple, tuple]:
        """(bodies, pads) of a part for spacing, each a list of (box, geometry, outline)
        and its box: its 3D bodies, and its pads' main copper (outline: the pad's
        rectangle), or its pad extent when it has no body (cached)."""
        found = self._shapes.get(part.component)
        if found is None:
            bodies = [(points_box(body.outline), _polygon(body.outline), body.outline)
                      for body in part.bodies]
            pads = []
            if not part.bodies:
                if part.pad_rect is not None:
                    pads = [(part.pad_box, _polygon(part.pad_rect), part.pad_rect)]
            else:
                index = self.index
                for row in part.pads:
                    pad = index.pcb.primitives[row - 1]
                    shape = index._copper_geometry(pad, None)
                    pads.append((_BOUNDS(shape), shape, _pad_corners(pad)))
            found = self._shapes[part.component] = (
                (bodies, part.body_box), (pads, _union_all(box for box, _, _ in pads)))
        return found

    def pair(self, first: Part, second: Part) -> PartGap:
        """The gap between two parts: the smallest of the body-to-body, body-to-pad and
        pad-to-pad distances, its basis the shapes that set it (cached per pair)."""
        if first.component > second.component:
            first, second = second, first
        key = (first.component, second.component)
        found = self._pairs.get(key)
        if found is not None:
            return found
        first_bodies, first_pads = self.shapes(first)
        second_bodies, second_pads = self.shapes(second)
        # Body before mixed before pads: an overlap takes the basis of the first found.
        sets = (("body", first_bodies, second_bodies), ("mixed", first_bodies, second_pads),
                ("mixed", first_pads, second_bodies), ("pads", first_pads, second_pads))
        best, closest, overlap, basis = math.inf, None, False, None
        for kind, (shapes, _), (others, around) in sets:
            if not shapes or not others:
                continue
            for box, shape, outline in shapes:
                lower = _box_gap(box, around)
                # A pair that cannot come closer is skipped, unless its boxes meet and
                # no overlap is known yet.
                if lower >= best and (overlap or lower > 0.0):
                    continue
                for other_box, other, other_outline in others:
                    lower = _box_gap(box, other_box)
                    if lower >= best and (overlap or lower > 0.0):
                        continue
                    self.distance_calls += 1
                    gap = _GEO(shape, other)
                    if gap < best:
                        best = gap
                        if not overlap:
                            closest, basis = (shape, other), kind
                    if not overlap and gap <= geometry.TOUCH_MIL and outlines_overlap(
                        outline, other_outline
                    ):
                        overlap, closest, basis = True, (shape, other), kind
        if best <= geometry.TOUCH_MIL:
            best = 0.0
        found = PartGap(first.component, second.component, first.side, best, basis, overlap,
                        _geometries=closest)
        self._pairs[key] = found
        return found

    def _neighbours(self, members: list[Part], reach: float):
        """(part, [(box gap, other part)] of same-side parts whose boxes are closer than
        reach, nearest first) per member."""
        boxes = {part.component: _union(part.pad_box, part.body_box) for part in members}
        grids: dict[str, dict] = defaultdict(lambda: defaultdict(list))
        for part in members:
            grid = grids[part.side]
            for cell in _cells(boxes[part.component]):
                grid[cell].append(part)
        for part in members:
            box, grid = boxes[part.component], grids[part.side]
            seen: dict[int, Part] = {}
            for cell in _cells(_grow(box, reach)):
                for other in grid.get(cell, ()):
                    if other.component != part.component:
                        seen[other.component] = other
            candidates = []
            for other in seen.values():
                lower = _box_gap(box, boxes[other.component])
                if lower < reach:
                    candidates.append((lower, other.component, other))
            candidates.sort(key=lambda entry: entry[:2])
            yield part, [(lower, other) for lower, _, other in candidates]

    def spacing(self, exclude: frozenset = frozenset()) -> SpacingFacts:
        """Each part's nearest same-side neighbour it does not overlap, within
        NEIGHBOUR_REACH_MIL; every overlapping pair. The parts in exclude (components)
        take no part (the last few sets kept)."""
        if exclude in self._spacing:
            return self._spacing[exclude]
        members = [part for part in self.on_board() if part.component not in exclude]
        nearest: dict[int, PartGap] = {}
        overlaps: list[PartGap] = []
        for part, candidates in self._neighbours(members, NEIGHBOUR_REACH_MIL):
            best = None
            for lower, other in candidates:
                if lower == 0.0 and other.component > part.component:
                    gap = self.pair(part, other)  # boxes meet: every such pair is measured
                    if gap.overlap:
                        overlaps.append(gap)
                if best is not None and lower >= best.gap_mil:
                    if lower > 0.0:
                        break
                    continue
                gap = self.pair(part, other)
                if not gap.overlap and (best is None or gap.gap_mil < best.gap_mil):
                    best = gap
            if best is not None:
                nearest[part.component] = best
        overlaps.sort(key=lambda gap: (gap.first, gap.second))
        while len(self._spacing) >= _RESULTS_KEPT:
            del self._spacing[next(iter(self._spacing))]
        found = self._spacing[exclude] = SpacingFacts(
            NEIGHBOUR_REACH_MIL, len(members), nearest, overlaps, self.distance_calls)
        return found

    def close_pairs(self, reach: float) -> list[PartGap]:
        """Every same-side pair of on-board parts closer than reach -
        CLEARANCE_TOLERANCE_MIL (overlaps included), smallest gap first. The last few
        reaches are kept."""
        key = round(reach, 6)
        if key not in self._close:
            limit = reach - CLEARANCE_TOLERANCE_MIL
            found = []
            for part, candidates in self._neighbours(self.on_board(), max(limit, 0.0) + 1e-9):
                for lower, other in candidates:
                    if other.component < part.component or lower >= limit:
                        continue
                    gap = self.pair(part, other)
                    if gap.gap_mil < limit or gap.overlap:
                        found.append(gap)
            found.sort(key=lambda gap: (gap.gap_mil, not gap.overlap, gap.first, gap.second))
            while len(self._close) >= _RESULTS_KEPT:
                del self._close[next(iter(self._close))]
            self._close[key] = found
        return self._close[key]

    # ---------- edge ----------

    def _nearest_piece(self, grid, box, limit: float, component: int, shapes):
        """(distance, piece, geometry) of the edge piece nearest the shapes (a list of
        (box, geometry), built only when a piece is near `box`) when closer than limit,
        else None. The part's own cutouts are not its edge."""
        pieces: dict[int, tuple] = {}
        for cell in _cells(_grow(box, limit)):
            for piece in grid.get(cell, ()):
                if piece[5] is None or piece[5] != component:
                    pieces[id(piece)] = piece
        candidates = sorted(
            ((lower, number, piece) for number, piece in enumerate(pieces.values())
             if (lower := _box_gap(box, piece[1])) < limit),
            key=lambda entry: entry[:2],
        )
        if not candidates:
            return None
        shapes = shapes()
        best = None
        for lower, _, piece in candidates:
            if best is not None and lower >= best[0]:
                break
            for shape_box, shape in shapes:
                if best is not None and _box_gap(shape_box, piece[1]) >= best[0]:
                    continue
                self.distance_calls += 1
                distance = _GEO(shape, piece[0])
                if distance < limit and (best is None or distance < best[0]):
                    best = (distance, piece, shape)
        return best

    def _finger_like(self, index, grid, part: Part, pads, outer) -> bool:
        """Could the part be an edge connector? dfm_geometry's finger rule on its main
        copper: long, narrow SMD pads on an outer layer within FINGER_REACH_MIL of an
        edge, long axis across it, at least FINGER_PADS along one edge and at least
        FINGER_SHARE of the part's pads. Only then is copper_to_edge asked (it builds
        the whole copper model)."""
        long = _long_pads(pads, outer)
        if len(long) < max(geometry.FINGER_PADS, geometry.FINGER_SHARE * len(pads)):
            return False
        across = math.cos(math.radians(geometry.FINGER_ANGLE_DEG))
        limit = FINGER_REACH_MIL - CLEARANCE_TOLERANCE_MIL
        edges: dict[tuple, int] = defaultdict(int)
        for pad in long:
            shape = index._copper_geometry(pad, None)
            box = _BOUNDS(shape)
            near = self._nearest_piece(grid, box, limit, part.component,
                                       lambda box=box, shape=shape: [(box, shape)])
            if near is None:
                continue
            x1, y1, x2, y2 = near[1][0][0]
            axis = (pad.rotation or 0.0) + (90.0 if (pad.height or pad.width) > pad.width else 0.0)
            direction = math.degrees(math.atan2(y2 - y1, x2 - x1))
            if abs(math.sin(math.radians(axis - direction))) >= across:
                edges[near[1][2]] += 1
        fingers = sum(count for count in edges.values() if count >= geometry.FINGER_PADS)
        return fingers >= geometry.FINGER_PADS and fingers >= geometry.FINGER_SHARE * len(pads)

    def part_to_edge(self, reach: float) -> EdgeFacts:
        key = round(reach, 6)
        if key in self._edges:
            return self._edges[key]
        index = self.index
        edge = geometry.copper_model(index).edge
        if not edge.has_outline:
            result = EdgeFacts(reach, False, {}, [], [])
        else:
            result = self._edge_run(index, edge, reach)
        while len(self._edges) >= _RESULTS_KEPT:
            del self._edges[next(iter(self._edges))]
        self._edges[key] = result
        return result

    def _edge_run(self, index, edge, reach: float) -> EdgeFacts:
        limit = reach - CLEARANCE_TOLERANCE_MIL
        # Searched at least at the finger reach, so a finger part is seen at any reach.
        search = max(limit, FINGER_REACH_MIL - CLEARANCE_TOLERANCE_MIL)
        grid: dict[tuple, list] = defaultdict(list)
        for piece in edge.pieces:
            for cell in _cells(piece[1]):
                grid[cell].append(piece)
        primitives = index.pcb.primitives
        outer = geometry.copper_model(index).outer
        found: dict[int, PartEdge] = {}
        off: list[int] = []
        finger_parts = False
        for part in self.members():
            if not edge.outline.contains(*part.centre):
                off.append(part.component)  # parked off the board (a cutout is no reason)
                continue
            pads = [primitives[row - 1] for row in part.pads]
            near_pad = near_body = None
            if part.pad_box is not None:
                def pad_shapes(pads=pads):
                    return [(_BOUNDS(shape), shape)
                            for shape in (index._copper_geometry(pad, None) for pad in pads)]
                near_pad = self._nearest_piece(grid, part.pad_box, search, part.component,
                                               pad_shapes)
            if part.bodies:
                def body_shapes(part=part):
                    return [(points_box(body.outline), _polygon(body.outline)) for body in part.bodies]
                near_body = self._nearest_piece(grid, part.body_box, search, part.component,
                                                body_shapes)
            if near_pad is None and near_body is None:
                continue
            crosses = near_pad is not None and (
                near_pad[0] <= geometry.TOUCH_MIL
                or not all(_on_board(edge, pad.x1, pad.y1, part.component) for pad in pads)
            )
            overhang = near_body is not None and (
                near_body[0] <= geometry.TOUCH_MIL
                or not all(_on_board(edge, x, y, part.component)
                           for body in part.bodies for x, y in body.outline)
            )
            pad_distance = 0.0 if crosses else near_pad[0] if near_pad else None
            body_distance = 0.0 if overhang else near_body[0] if near_body else None
            if not finger_parts and pad_distance is not None and self._finger_like(
                index, grid, part, pads, outer
            ):
                finger_parts = True  # copper_to_edge judges it
            if pad_distance is not None and pad_distance >= limit:
                pad_distance = near_pad = None
            if body_distance is not None and body_distance >= limit:
                body_distance = near_body = None
            if pad_distance is None and body_distance is None:
                continue
            found[part.component] = PartEdge(
                component=part.component,
                pad_distance_mil=pad_distance,
                pad_edge=near_pad[1][3] if near_pad else None,
                pad_at=geometry._near_point(near_pad[2], near_pad[1][0]) if near_pad else None,
                body_distance_mil=body_distance,
                body_edge=near_body[1][3] if near_body else None,
                body_at=geometry._near_point(near_body[2], near_body[1][0]) if near_body else None,
                crosses=crosses and pad_distance is not None,
                overhang=overhang and body_distance is not None,
            )
        fingers = []
        if finger_parts:
            fingers = geometry.copper_to_edge(index, FINGER_REACH_MIL).fingers
            for group in fingers:
                if group.component in found:
                    found[group.component].fingers = True
        return EdgeFacts(reach, True, dict(sorted(found.items())), off, fingers)

    # ---------- tombstoning ----------

    def tombstones(self) -> list[Tombstone]:
        if self._tombstones is not None:
            return self._tombstones
        passives = [part for part in self.parts if part.passive]
        rows = [row for part in passives for terminal in part.terminals for row in terminal]
        connections = iter(self.connections(rows))
        found = []
        for part in passives:
            first, second = (_terminal_connection([next(connections) for _ in terminal])
                             for terminal in part.terminals)
            causes = []
            if (first.style == "direct") != (second.style == "direct"):
                causes.append("direct")
            elif first.style == second.style == "none":
                # Tracks only matter where no area copper draws the heat.
                wide = max(first.widest_track_mil, second.widest_track_mil)
                narrow = min(first.widest_track_mil, second.widest_track_mil)
                if wide >= TRACK_IMBALANCE_MIL - CLEARANCE_TOLERANCE_MIL and (
                    wide >= TRACK_IMBALANCE_RATIO * narrow - CLEARANCE_TOLERANCE_MIL
                ):
                    causes.append("track")
            found.append(Tombstone(part.component, part.size_code, part.size_basis, part.small,
                                   (first, second), tuple(causes)))
        self._tombstones = found
        return found

    # ---------- pick and place ----------

    def centroids(self) -> list[Centroid]:
        if self._centroids is None:
            pcb = self.index.pcb
            holes: dict[int, list[tuple[float, float]]] = defaultdict(list)
            for hole in getattr(pcb, "bare_holes", None) or ():
                if hole.component is not None:
                    holes[hole.component].append((hole.x, hole.y))
            self._centroids = [_centroid(pcb, part, holes.get(part.component, ()))
                               for part in self.parts]
        return self._centroids

    def connections(self, rows) -> list[PadConnection]:
        """The connection of each pad (prims row ids) to its copper, in order."""
        index = self.index
        primitives = index.pcb.primitives
        pads = [(row, primitives[row - 1]) for row in rows]
        keys = {(pad.net, pad.layer) for _, pad in pads if pad.net is not None}
        # Transient: the band indexes of large pours are not kept with the facts.
        areas: dict[tuple, list] = defaultdict(list)
        for area in index.pcb.areas:
            if (area.net, area.layer) in keys and len(area.outline) >= 3:
                areas[(area.net, area.layer)].append(_RingCopper(area))
        sql = _TRACK_SQL if index._has_rtree else _TRACK_BOX_SQL
        return [self._connection(index, sql, row, pad, areas) for row, pad in pads]

    @staticmethod
    def _connection(index, sql, row: int, pad, areas) -> PadConnection:
        if pad.net is None:
            return PadConnection(row, pad.layer, None, "none", 0.0, 0.0)
        ring_box = _grow(points_box(oriented_rect_vertices(
            pad.x1, pad.y1, pad.width + 2.0 * RING_MIL,
            (pad.height or pad.width) + 2.0 * RING_MIL, pad.rotation or 0.0,
        )), geometry.TOUCH_MIL)
        near = [area for area in areas.get((pad.net, pad.layer), ())
                if _box_gap(area.bbox, ring_box) == 0.0]
        shape = index._copper_geometry(pad, None)
        x0, y0, x1, y1 = _union(ring_box, _BOUNDS(shape))
        grow = geometry.TOUCH_MIL
        pours, widest = [], 0.0
        for track in index.db.execute(sql, (x1 + grow, x0 - grow, y1 + grow, y0 - grow,
                                            pad.layer, pad.net)):
            copper = index._copper_geometry(track, None)
            if track["poured_by"] is not None:
                pours.append((_BOUNDS(copper), copper))
            elif track["width"] > widest and _GEO(copper, shape) <= geometry.TOUCH_MIL:
                widest = track["width"]
        covered = 0
        if pours or any(area.crosses(ring_box) for area in near):
            for x, y in _ring(pad):
                if any(area.contains(x, y) for area in near) or any(
                    box[0] <= x <= box[2] and box[1] <= y <= box[3]
                    and geometry.point_distance(x, y, copper) == 0.0
                    for box, copper in pours
                ):
                    covered += 1
        elif near:
            # No copper edge passes the ring: its points are all in the copper or none.
            x, y = _ring(pad)[0]
            covered = RING_POINTS if any(area.contains(x, y) for area in near) else 0
        coverage = covered / RING_POINTS
        style = "direct" if coverage >= DIRECT_COVERAGE else "relief" if covered else "none"
        return PadConnection(row, pad.layer, pad.net, style, coverage, widest)


def _terminal_connection(connections: list[PadConnection]) -> PadConnection:
    """A terminal's connection from its pads': the heaviest pad's (direct, relief, none;
    then coverage) with the widest track of any."""
    heaviest = max(connections, key=lambda pad: (_STYLE_RANK[pad.style], pad.coverage))
    widest = max(pad.widest_track_mil for pad in connections)
    if widest == heaviest.widest_track_mil:
        return heaviest
    return PadConnection(heaviest.row, heaviest.layer, heaviest.net, heaviest.style,
                         heaviest.coverage, widest)


def _on_board(edge, x: float, y: float, component: int) -> bool:
    """Inside the outline and outside every cutout but the part's own."""
    if not edge.outline.contains(x, y):
        return False
    cutout = edge.in_cutout(x, y)
    return cutout is None or cutout.component == component


def _long_pads(pads, outer) -> list:
    """The pads that may be fingers by their shape: SMD, on an outer layer, at least
    FINGER_MIN_LENGTH_MIL long and FINGER_ASPECT times as long as wide."""
    found = []
    for pad in pads:
        if (pad.hole or 0.0) > 0 or pad.layer not in outer:
            continue
        width, height = pad.width, pad.height or pad.width
        long, short = max(width, height), min(width, height)
        if short > 0 and long >= geometry.FINGER_MIN_LENGTH_MIL \
                and long >= geometry.FINGER_ASPECT * short:
            found.append(pad)
    return found


def _model(index) -> _Model:
    model = getattr(index, "_dfm_assembly", None)
    if model is None:
        model = index._dfm_assembly = _Model(index)
    return model


# ---------- API ----------


def parts(index) -> list[Part]:
    """The assembly facts of every PCB component, in pcb.components order (cached)."""
    return _model(index).parts


def fiducials(index) -> list[Fiducial]:
    """Global fiducials: fiducial parts (their pad) and free-pad fiducials (cached)."""
    return _model(index).fiducials


def fiducial_counts(index) -> dict[str, int]:
    """Fiducials per side: {"top": n, "bottom": n}."""
    counts = {"top": 0, "bottom": 0}
    for fiducial in fiducials(index):
        counts[fiducial.side] = counts.get(fiducial.side, 0) + 1
    return counts


def placements(index, is_dnp=None) -> Placements:
    """Parts the assembler places: ComponentKind in the BOM, solderable pads (smd, tht or
    mixed: not a mounting hole), not a fiducial, fitted (is_dnp: VariantState.is_dnp,
    applied to the netlist name, else the designator; None: every part fitted)."""
    counts = {side: {"smd": 0, "tht": 0, "mixed": 0} for side in ("top", "bottom")}
    placed, not_fitted = [], []
    for part in parts(index):
        if part.kind not in BOM_KINDS or not part.solderable or part.fiducial:
            continue
        if not part.fitted(is_dnp):
            not_fitted.append(part.component)
            continue
        placed.append(part)
        counts.setdefault(part.side, {"smd": 0, "tht": 0, "mixed": 0})[part.mount] += 1
    unique = len({((part.footprint or "").strip().lower(), (part.comment or "").strip().lower())
                  for part in placed})
    return Placements([part.component for part in placed], counts, not_fitted, unique)


def part_spacing(index, exclude=()) -> SpacingFacts:
    """Each on-board part's nearest same-side neighbour (within NEIGHBOUR_REACH_MIL,
    overlapping ones left out) and every overlapping pair; the parts in exclude
    (components: a variant's Not Fitted parts) take no part, so a part next to one is
    measured to its nearest other neighbour (cached per exclude, the last few kept)."""
    return _model(index).spacing(frozenset(exclude))


def close_pairs(index, reach_mil: float) -> list[PartGap]:
    """Every same-side pair of on-board parts closer than reach_mil -
    CLEARANCE_TOLERANCE_MIL, overlaps included, smallest gap first (pass the profile
    limit itself; filter out the Not Fitted parts)."""
    return _model(index).close_pairs(reach_mil)


def part_to_edge(index, reach_mil: float = DEFAULT_EDGE_REACH_MIL) -> EdgeFacts:
    """Each part's pad copper and bodies closer than reach_mil -
    CLEARANCE_TOLERANCE_MIL to the board edge; parts off the board; edge-connector
    fingers (cached per reach, the last few kept)."""
    return _model(index).part_to_edge(reach_mil)


def tombstoning(index) -> list[Tombstone]:
    """The terminal connections of every two-terminal passive, with the imbalance causes
    (filter by Tombstone.small and Part.fitted for the check; cached)."""
    return _model(index).tombstones()


def pad_connections(index, rows) -> list[PadConnection]:
    """How each pad (prims row ids, in order) meets its copper: ring style and coverage,
    widest touching track (not cached)."""
    return _model(index).connections(list(rows))


def centroids(index) -> list[Centroid]:
    """Every part's pick-and-place facts, in pcb.components order (cached)."""
    return _model(index).centroids()


def origin(index) -> OriginFacts:
    """The board's relative origin against the outline's extent."""
    board = index.pcb.board
    point = (board.origin_x or 0.0, board.origin_y or 0.0)
    box = outline_box(index.pcb)
    rigid_flex = any(substack.get("is_flex") for substack in board.substacks or ())
    if box is None:
        return OriginFacts(point, None, "no_outline", None, rigid_flex)
    reach = math.hypot(point[0] - box[0], point[1] - box[1])
    tolerance = ORIGIN_TOLERANCE_MIL
    corners = [(x, y) for x in (box[0], box[2]) for y in (box[1], box[3])]
    if reach <= tolerance:
        position = "lower_left"
    elif math.hypot(*point) <= tolerance:
        position = "not_set"
    elif any(math.hypot(point[0] - x, point[1] - y) <= tolerance for x, y in corners):
        position = "other_corner"
    elif (box[0] - tolerance <= point[0] <= box[2] + tolerance
          and box[1] - tolerance <= point[1] <= box[3] + tolerance):
        position = "inside"
    else:
        position = "outside"
    return OriginFacts(point, box, position, reach, rigid_flex)
