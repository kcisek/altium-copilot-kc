"""Mechanical fit and the mechanical handoff (roadmap #34), offline from the saved PcbDoc:
part heights against height limits, keepouts, mounting holes and their free radius, tall
parts on the in-circuit test (ICT) probe side and connectors against the board edge
(check_mechanical_fit); and, for the mechanical team, the board outline, holes,
connectors, regions and per-side summaries (get_mechanical_handoff, and package_for_xfn's
mech_* tables) with an optional DXF (R12 ASCII) and SVG drawing. Plan-view outlines and
heights only: not a replacement for a STEP or IDF / IDX exchange.

- Heights: each placed part's tallest 3D body projected on its side (ComponentBodies6
  OVERALLHEIGHT with its STANDOFFHEIGHT and model; a flat body of height 0 gives no
  height), else the footprint's Height (the fallback 3D height Altium uses without a
  body), else the tallest body of its library footprint when the caller read the
  libraries (library_heights), else none. A part without a body is missing_3d_body (info;
  a warning when it has no height at all and a limit the user or a drawing gives applies
  to it). Graphical parts, net ties, fiducials, parts without pads, TP parts with SMD
  pads only and card-edge fingers (CARD_EDGE_PADS or more SMD pads only, each elongated
  and within CARD_EDGE_MIL of the board edge: board copper) are not expected to have a
  body. Not Fitted parts (is_dnp) are listed apart and take part in nothing else (their
  mounting holes stay: the hole is drilled).
- Plan-view shapes: a part's body outlines, else its courtyard (the box, in the part's
  frame, of its tracks and arcs on a mechanical layer named courtyard or placement
  outline), else its pad box (dfm_assembly's pad extent). Heights and regions use the
  bodies (else the courtyard, else the pads); keepouts use the bodies and the courtyard.
- Limits: per side (height_limit_top / height_limit_bottom); per region: user regions
  (polygons or rectangles), free 3D bodies (no part: an enclosure part placed on the board;
  its standoff is the limit under it, and it is a no-parts keepout when it reaches the
  board), areas on a mechanical layer named for a height restriction with a length in its
  name; Altium's Height rules (MAXHEIGHT, MINHEIGHT; the rule that applies to each part by
  scope and priority, decided per component as Altium does: InComponent,
  InComponentClass, HasFootprint, WithinRoom on the part's extent, OnTopLayer ...); and the
  maximum component height Draftsman notes state (only when every such note agrees for a
  side; the note is quoted; a note for one place or with exceptions is quoted, not
  applied). The tightest applicable limit is checked: height_over_limit (warning, with
  the margin) and height_under_min (a Height rule's minimum, warning).
- Keepouts: a part's bodies or courtyard (else its pad box) overlapping a component
  keepout on its side is part_in_keepout (warning): a user region with keepout or a
  max_height of 0, a free 3D body reaching the board, an area on a mechanical layer named
  as a component keepout. Areas on the Keep-Out layer and keepouts on the outer copper
  layers restrict copper (Altium's DRC checks them): they are regions listed with the
  tallest part over them, not judged. Rooms likewise (Altium checks their confinement).
- Mounting holes: free pads and bare holes with a hole of at least MIN_HOLE_MIL, and the
  holes (of that size) of parts that are mounting holes: holes (SMD pads only as rings
  centred on them), and a designator H, MH, MTG ... followed by a digit, a footprint named
  for a mounting hole or screw, a Mechanical part (dfm_assembly's mount 'mechanical') or
  pads on at most one net. A hole in a board cutout or off the outline is off the board.
  Each: position, drill (slot length), plated, net, pad size (its own pad or ring), the
  screw it takes (the largest metric thread that passes with ISO 273 clearance: SCREWS)
  and its free radius per board face: from the hole centre to the nearest copper of
  another net (or none) on that face (tracks, arcs, pads, vias, pour and region copper;
  the hole's own pad, its part's copper and, for a free hole, free pads centred on it
  excepted), another fitted part's 3D body (else its pad box) on that face, or the
  board edge (outline or cutout); the nearest copper of the hole's own net is given apart
  (a grounded ring is no obstacle), and a part without pads whose body covers the hole is
  its hardware (a standoff, screw or nut model), no obstacle. mounting_hole_clearance: a
  part or exposed copper (pads, vias that are not tented) closer than half the larger of
  the head and washer diameters plus the margin (the screw's ISO 7045 pan head and ISO
  7089 washer and DEFAULT_MARGIN_MIL by default: info; the user's: a warning; the head
  alone, without a washer, when only the head is given); copper under the solder mask
  only (tracks, pours, tented vias) within it is one grouped info finding for the board.
  The board edge alone is no obstacle for it: hole_near_edge, the hole wall closer to the
  edge than the board thickness (at most EDGE_WALL_MAX_MIL; info, half of it a warning).
- ICT: the probe side check_testpoints decides from the probe sites (the side reaching
  more nets), or probe_side. Fitted parts at least tall_part_height tall on it (TP parts
  excepted) are tall_part_on_probe_side (info; a warning when probe_side is given), with
  the probe points within SHADOW_MIL of their outline (check_testpoints' tall_parts names
  those sites).
- Connectors: parts with a connector designator prefix whose text names no crystal,
  holder, jumper or test point (interfaces.is_interface_connector), external or internal
  by their text. Per connector: position, rotation, side, the nearest board edge (the
  outline's left, right, top or bottom, another outline edge, or a cutout), the distance
  of its bodies and of its pads to it, the overhang past the outline, and the direction it
  faces (the edge's outward normal; on an arc, the radial direction at the point nearest
  the part) when it overhangs or comes within EDGE_NEAR_MIL.

Pure logic over a PcbIndex, no MCP. Lengths in mil under *_mil keys; locations relative to
the board origin (origin_mil); user inputs in the caller's units, relative to the origin.
"""

import json
import math
import re
import weakref
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

try:
    from parsers.pcb_doc import (
        decode_fill_records, decode_region_records, fill_outline, shape_points,
    )
    from services import dfm_assembly as assembly
    from services import dfm_geometry as geometry
    from services.decoupling import designator_prefix
    from services.dfm import stack_total_thickness
    from services.fab_drawing import LENGTH, MAX_WORDS, length_mm
    from services.interfaces import is_interface_connector, text_exposure
    from services.pcb_geometry import (
        in_arc_span, oriented_rect_vertices, point_in_polygon, point_polygon_distance,
    )
    from services.pcb_rules import (
        ROOM_KIND, ObjectFacts, ObjectResolver, RuleTarget, room_vertices, rule_values,
    )
    from services.testpoints import PROFILES as TEST_PROFILES
    from services.testpoints import probe_sides, tp_components
except ImportError:
    from server.parsers.pcb_doc import (
        decode_fill_records, decode_region_records, fill_outline, shape_points,
    )
    from server.services import dfm_assembly as assembly
    from server.services import dfm_geometry as geometry
    from server.services.decoupling import designator_prefix
    from server.services.dfm import stack_total_thickness
    from server.services.fab_drawing import LENGTH, MAX_WORDS, length_mm
    from server.services.interfaces import is_interface_connector, text_exposure
    from server.services.pcb_geometry import (
        in_arc_span, oriented_rect_vertices, point_in_polygon, point_polygon_distance,
    )
    from server.services.pcb_rules import (
        ROOM_KIND, ObjectFacts, ObjectResolver, RuleTarget, room_vertices, rule_values,
    )
    from server.services.testpoints import PROFILES as TEST_PROFILES
    from server.services.testpoints import probe_sides, tp_components

MM = 1 / 0.0254  # mil per mm
EPS_MIL = 0.05
CHECKS = (
    "height_over_limit", "height_under_min", "part_in_keepout", "missing_3d_body",
    "mounting_hole_clearance", "hole_near_edge", "tall_part_on_probe_side",
)
_HEIGHT_CHECKS = ("height_over_limit", "height_under_min", "missing_3d_body")
_HOLE_CHECKS = ("mounting_hole_clearance", "hole_near_edge")
_CHECK_ALIASES = {
    "height": _HEIGHT_CHECKS, "heights": _HEIGHT_CHECKS, "bodies": ("missing_3d_body",),
    "keepout": ("part_in_keepout",), "keepouts": ("part_in_keepout",),
    "holes": _HOLE_CHECKS, "mounting_holes": _HOLE_CHECKS, "mounting": _HOLE_CHECKS,
    "ict": ("tall_part_on_probe_side",), "probe": ("tall_part_on_probe_side",),
    "test": ("tall_part_on_probe_side",),
}
_CHECK_RANK = {check: rank for rank, check in enumerate(CHECKS)}
_SEVERITY_RANK = {"warning": 0, "info": 1}
SECTIONS = ("board", "outline", "mounting_holes", "connectors", "regions", "rigid_flex",
            "sides", "parts")
SIDES = ("top", "bottom")

# Mounting holes: free pads and bare holes, and parts' holes, of at least this diameter.
MIN_HOLE_MIL = 1.5 * MM
# Free radius: obstacles are looked for this far from a hole's centre.
SEARCH_MIL = 15.0 * MM
DEFAULT_MARGIN_MIL = 0.5 * MM
# Metric screws: (thread, nominal, ISO 273 clearance hole fine, medium, coarse, ISO 7045
# pan head diameter, ISO 7089 washer outer diameter), mm.
SCREWS = (
    ("M1.6", 1.6, 1.7, 1.8, 2.0, 3.2, 4.0),
    ("M2", 2.0, 2.2, 2.4, 2.6, 4.0, 5.0),
    ("M2.5", 2.5, 2.7, 2.9, 3.1, 5.0, 6.0),
    ("M3", 3.0, 3.2, 3.4, 3.6, 5.6, 7.0),
    ("M4", 4.0, 4.3, 4.5, 4.8, 8.0, 9.0),
    ("M5", 5.0, 5.3, 5.5, 5.8, 9.5, 10.0),
    ("M6", 6.0, 6.4, 6.6, 7.0, 12.0, 12.0),
)
SCREW_MIN_PLAY_MM = 0.1  # the thread passes a hole at least this much larger
SCREW_MAX_PLAY_MM = 0.6  # a hole larger than the coarse clearance by more is not its hole
# hole_near_edge: the wall closer to the edge than the board thickness, at most
# EDGE_WALL_MAX_MIL (1.6 mm without a stack thickness); half of it is a warning.
DEFAULT_EDGE_WALL_MIL = 1.6 * MM
EDGE_WALL_MAX_MIL = 1.0 * MM
HOLES_SAMPLE = 10  # holes listed in the grouped masked-copper finding
# ICT: testpoints' ict profile (a fixture keeps its probes this far from parts this tall).
TALL_PART_MIL = TEST_PROFILES["ict"]["tall_part_height_mil"]
SHADOW_MIL = TEST_PROFILES["ict"]["tall_part_clearance_mil"]
# Connectors: a body (or pad box) this close to an edge faces it.
EDGE_NEAR_MIL = 2.0 * MM
# Card-edge fingers: a bodiless part of at least CARD_EDGE_PADS SMD pads only, each
# elongated (CARD_EDGE_ASPECT) and within CARD_EDGE_MIL of the board edge.
CARD_EDGE_PADS = 4
CARD_EDGE_ASPECT = 2.0
CARD_EDGE_MIL = 0.5 * MM
TALLEST_LISTED = 5
DEFAULT_HEIGHT_RULE_MIL = 1000.0  # Altium's default Height rule maximum: no enclosure
CONNECTORS_LISTED = 60
REGIONS_LISTED = 40
HOLES_LISTED = 40
PARTS_LISTED = 100
FINDINGS_LISTED = 100
CACHE_SIZE = 4
HEIGHT_CLASSES_MM = (1.0, 2.0, 3.0, 5.0, 10.0, 20.0)
_HEIGHT_LABELS = ("<1", "1-2", "2-3", "3-5", "5-10", "10-20", ">=20")

_KEEP_OUT_LAYER = 56
_MECHANICAL = range(57, 73)
_GRAPHICAL, _NET_TIES = 2, (3, 4)
_COURTYARD_LAYER = re.compile(r"courtyard|placement\s*outline|place\s*bound", re.I)
_KEEPOUT_LAYER = re.compile(r"keep[\s_-]*out|no[\s_-]*(?:components?|parts?)\b|"
                            r"component[\s_-]*keep|placement[\s_-]*restrict", re.I)
_HEIGHT_LAYER = re.compile(r"height|\btall\b|z[\s_-]*restrict", re.I)
_MOUNT_DESIGNATOR = re.compile(r"^(?:MH|MTG|MNT|MT|HOLE|H|M)\d", re.I)
_MOUNT_FOOTPRINT = re.compile(r"mount|mtg|screw|standoff|stand[\s_-]off|\bhole|hole[\s_-]*\d|"
                              r"^MH|[\s_-]M[2-6](?:[._]\d)?(?:$|[\s_-])", re.I)
# Draftsman notes: a maximum component height.
_HEIGHT_WORD = re.compile(r"\bheights?\b|\btall(?:est)?\b", re.I)
_NOT_PART_HEIGHT = re.compile(
    r"text|font|character|letter|silk|legend|marking|stencil|paste|solder|copper|thick|"
    r"\bvia|hole|drill|bow|twist|warp|\bstack|dielectric|impedance|panel|\bbump", re.I)
_PART_WORDS = re.compile(r"component|\bparts?\b|assembl|populat|placement|\bSMD\b|\bSMT\b|"
                         r"keep[\s-]?out|enclosure|clearance", re.I)
# A limit for one place or with exceptions, not the whole side: quoted, not applied.
_LOCAL_WORDS = re.compile(
    r"\bunder\s+(?:the|a|an|this|that|each|any)\b|\bbeneath\b|\bbelow\s+the\b|\bexcept|"
    r"\bexcluding\b|\bzones?\b|\bareas?\b|\bregions?\b|\bnear\b|\baround\b", re.I)
_TOP_WORDS = re.compile(r"\btop\b|\bprimary\b|component\s+side", re.I)
_BOTTOM_WORDS = re.compile(r"\bbottom\b|\bsecondary\b|solder\s+side", re.I)
_RTREE_SQL = (
    "SELECT p.* FROM prims_rtree r CROSS JOIN prims p ON p.id = r.id "
    "WHERE r.minx <= ? AND r.maxx >= ? AND r.miny <= ? AND r.maxy >= ? "
    "AND p.pour_id IS NULL AND p.kind IN ('track', 'arc', 'via', 'pad')"
)
_BOX_SQL = (
    "SELECT p.* FROM prims p WHERE p.minx <= ? AND p.maxx >= ? AND p.miny <= ? "
    "AND p.maxy >= ? AND p.pour_id IS NULL AND p.kind IN ('track', 'arc', 'via', 'pad')"
)


def _r(value: float | None, digits: int = 3) -> float | None:
    return None if value is None else round(value, digits)


def _mm_text(mil: float | None, units: str) -> str:
    if mil is None:
        return "?"
    if units == "mm":
        return f"{mil / MM:.2f} mm"
    return f"{mil:.1f} mil"


def _box(points) -> tuple[float, float, float, float]:
    return assembly.points_box(points)


def _box_gap(first, second) -> float:
    dx = max(first[0] - second[2], second[0] - first[2], 0.0)
    dy = max(first[1] - second[3], second[1] - first[3], 0.0)
    return math.hypot(dx, dy)


def _union(boxes):
    boxes = [box for box in boxes if box is not None]
    if not boxes:
        return None
    return (min(box[0] for box in boxes), min(box[1] for box in boxes),
            max(box[2] for box in boxes), max(box[3] for box in boxes))


def _height_class(mil: float) -> str:
    value = mil / MM
    for edge, label in zip(HEIGHT_CLASSES_MM, _HEIGHT_LABELS):
        if value < edge:
            return label
    return _HEIGHT_LABELS[-1]


def parse_checks(checks) -> tuple[list[str] | None, dict | None]:
    """(selected checks in CHECKS order, None) or (None, error); aliases expand."""
    if not checks:
        return list(CHECKS), None
    wanted: set[str] = set()
    unknown = []
    for raw in checks:
        name = str(raw).strip().lower()
        if name in CHECKS:
            wanted.add(name)
        elif name in _CHECK_ALIASES:
            wanted.update(_CHECK_ALIASES[name])
        else:
            unknown.append(str(raw))
    if unknown:
        return None, {"error": "invalid_checks",
                      "message": f"Unknown checks: {', '.join(unknown)}.",
                      "valid_checks": list(CHECKS), "aliases": sorted(_CHECK_ALIASES)}
    return [check for check in CHECKS if check in wanted], None


# ---------- screws ----------


def screw_for_hole(hole_mil: float) -> dict | None:
    """The largest metric screw whose thread passes the hole with at least
    SCREW_MIN_PLAY_MM to spare, unless the hole is more than SCREW_MAX_PLAY_MM larger than
    its coarse ISO 273 clearance hole: {thread, head_mil, washer_mil}; None otherwise. The
    hole is taken to 0.001 mm first: Altium stores 2.6 mm as 102.3622 mil, a hair under
    it."""
    hole = round(hole_mil / MM, 3)
    for thread, nominal, _fine, _medium, coarse, head, washer in reversed(SCREWS):
        if nominal + SCREW_MIN_PLAY_MM <= hole + 1e-6:
            if hole > coarse + SCREW_MAX_PLAY_MM + 1e-6:
                return None
            return {"thread": thread, "head_mil": head * MM, "washer_mil": washer * MM}
    return None


# ---------- regions ----------


@dataclass
class Region:
    """A plan-view area that limits, forbids or groups parts. side: top, bottom or both."""

    kind: str  # user | free_body | keepout_layer | copper_keepout | named_layer | room | board_region
    name: str
    side: str
    polygon: list[tuple[float, float]]
    max_height_mil: float | None = None
    keepout: bool = False  # no parts may overlap it
    source: str = ""
    data: dict = field(default_factory=dict)
    owner: int | None = None  # the part whose footprint holds it: never in it itself
    box: tuple | None = None

    def __post_init__(self):
        if self.box is None and self.polygon:
            self.box = _box(self.polygon)

    def on(self, side: str) -> bool:
        return self.side in ("both", side)

    def applies(self, part) -> bool:
        return self.on(part.side) and part.component != self.owner


def parse_user_regions(raw, units: str, origin: tuple[float, float]) -> tuple[list[Region], dict | None]:
    """User regions ([{name, polygon: [[x, y] ...] | rect: [x1, y1, x2, y2], side,
    max_height, keepout}], lengths in units relative to the board origin) as Regions in
    absolute mil, or an error."""
    if raw is None:
        return [], None
    if isinstance(raw, str):  # a client that sends the list as JSON text
        try:
            raw = json.loads(raw)
        except ValueError:
            return [], _region_error("regions must be a list of objects.")
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return [], _region_error("regions must be a list of objects.")
    scale = MM if units == "mm" else 1.0
    regions = []
    for number, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            return [], _region_error(f"Region {number} must be an object.")
        unknown = set(item) - {"name", "polygon", "rect", "side", "max_height", "keepout"}
        if unknown:
            return [], _region_error(f"Region {number}: unknown keys {sorted(unknown)}.")
        name = str(item.get("name") or f"region {number}")
        try:
            if "polygon" in item and item["polygon"] is not None:
                points = [(float(x), float(y)) for x, y in item["polygon"]]
                if len(points) < 3:
                    raise ValueError
            elif "rect" in item and item["rect"] is not None:
                x1, y1, x2, y2 = (float(value) for value in item["rect"])
                if x1 == x2 or y1 == y2:
                    raise ValueError
                points = [(min(x1, x2), min(y1, y2)), (max(x1, x2), min(y1, y2)),
                          (max(x1, x2), max(y1, y2)), (min(x1, x2), max(y1, y2))]
            else:
                return [], _region_error(f"Region {number} needs a polygon or a rect.")
        except (TypeError, ValueError):
            return [], _region_error(
                f"Region {number}: polygon is at least 3 [x, y] points, rect is [x1, y1, "
                "x2, y2] with an area.")
        if not all(math.isfinite(value) for point in points for value in point):
            return [], _region_error(f"Region {number}: coordinates must be finite.")
        side = str(item.get("side") or "both").strip().lower()
        if side not in ("top", "bottom", "both"):
            return [], _region_error(f"Region {number}: side is top, bottom or both.")
        limit = item.get("max_height")
        if limit is not None:
            try:
                limit = float(limit)
            except (TypeError, ValueError):
                limit = -1.0
            if not math.isfinite(limit) or limit < 0:
                return [], _region_error(f"Region {number}: max_height must be 0 or more.")
            limit *= scale
        keepout = bool(item.get("keepout")) or limit == 0.0
        polygon = [(x * scale + origin[0], y * scale + origin[1]) for x, y in points]
        regions.append(Region("user", name, side, polygon,
                              max_height_mil=None if keepout else limit, keepout=keepout,
                              source="user"))
    return regions, None


def _region_error(message: str) -> dict:
    return {"error": "invalid_regions", "message": message,
            "expected": "[{name, polygon: [[x, y], ...] or rect: [x1, y1, x2, y2], side: "
                        "top|bottom|both, max_height, keepout}] in units, relative to the "
                        "board origin"}


# ---------- Draftsman notes ----------


def note_height_limits(documents) -> dict:
    """Maximum component heights stated in Draftsman notes. documents: [(file name,
    [fab_drawing.NoteLine])]. A line is a candidate when it names a height (height, tall,
    tallest) with a maximum word (max, not exceed, <=, up to ...), is about parts
    (component, part, assembly, placement, keep-out, enclosure, clearance), not text,
    copper, holes or the board's thickness, and holds a length with a unit. One length:
    top / bottom words pick the side (neither or both: both sides); two lengths: each
    takes the one side word before it (else after it), one top and one bottom, else the
    line is unclear. A line naming a place or an exception (under the ..., except, zone,
    area, near ...) is local: quoted, not applied. {"limits": {side: {max_mil, source,
    quote}}, "conflicts": [side], "candidates": n, "local": [{source, quote}], "unclear":
    n}: a side whose lines disagree is a conflict, not a limit."""
    found: dict[str, list[tuple[float, str, str]]] = defaultdict(list)
    candidates = unclear = 0
    local: list[dict] = []
    for document, lines in documents:
        for line in lines:
            text = line.text
            if not _HEIGHT_WORD.search(text) or not MAX_WORDS.search(text):
                continue
            if _NOT_PART_HEIGHT.search(text) or not _PART_WORDS.search(text):
                continue
            lengths = []
            for match in LENGTH.finditer(text):
                if match.group(2):
                    value_mm, _ = length_mm(float(match.group(1).replace(",", ".")),
                                            match.group(2))
                    if 0 < value_mm < 500:
                        lengths.append((match, value_mm))
            if not lengths:
                continue
            candidates += 1
            where = f"{document}, {line.where}"
            quote = text if len(text) <= 110 else text[:107] + "..."
            if _LOCAL_WORDS.search(text):
                local.append({"source": where, "quote": quote})
                continue
            assigned = _note_sides(text, lengths)
            if not assigned:
                unclear += 1
                continue
            for side, value_mm in assigned:
                found[side].append((value_mm * MM, where, quote))
    limits, conflicts = {}, []
    for side in SIDES:
        values = found.get(side, [])
        if not values:
            continue
        if max(v for v, _, _ in values) - min(v for v, _, _ in values) > 0.01 * MM:
            conflicts.append(side)
            continue
        value, where, quote = values[0]
        limits[side] = {"max_mil": value, "source": where, "quote": quote,
                        "lines": len(values)}
    return {"limits": limits, "conflicts": conflicts, "candidates": candidates,
            "local": local, "unclear": unclear}


def _note_sides(text: str, lengths: list[tuple[re.Match, float]]) -> list[tuple[str, float]]:
    """(side, mm) of a note line's lengths: one length on the side its words name (both
    when they name neither or both); two lengths each on the one side word before it
    (else after it), one top and one bottom; [] when that is unclear."""
    if len(lengths) == 1:
        top, bottom = bool(_TOP_WORDS.search(text)), bool(_BOTTOM_WORDS.search(text))
        sides = ("top",) if top and not bottom else ("bottom",) if bottom and not top \
            else SIDES
        return [(side, lengths[0][1]) for side in sides]
    if len(lengths) != 2:
        return []
    bounds = [0, *(match.end() for match, _ in lengths)]
    starts = [match.start() for match, _ in lengths] + [len(text)]
    for spans in ([(bounds[n], starts[n]) for n in range(2)],  # the words before each
                  [(bounds[n + 1], starts[n + 1]) for n in range(2)]):  # ... after each
        assigned = []
        for (start, end), (_, value_mm) in zip(spans, lengths):
            segment = text[start:end]
            top, bottom = bool(_TOP_WORDS.search(segment)), bool(_BOTTOM_WORDS.search(segment))
            if top == bottom:
                break
            assigned.append(("top" if top else "bottom", value_mm))
        if len(assigned) == 2 and assigned[0][0] != assigned[1][0]:
            return assigned
    return []


# ---------- parts ----------


@dataclass
class PartFit:
    component: int
    designator: str
    name: str | None  # the netlist name (dfm_assembly.Part.name)
    side: str
    kind: int
    footprint: str | None
    comment: str | None
    x: float
    y: float
    rotation: float
    fitted: bool
    height_mil: float | None
    standoff_mil: float | None
    source: str  # body | footprint_height | library_body | none
    bodies: list[list[tuple[float, float]]]
    flat_bodies: int
    model: str | None
    library: str | None
    courtyard: list[tuple[float, float]] | None
    pad_rect: list[tuple[float, float]] | None
    expects_body: bool
    tp: bool
    mount: str
    hole_mil: float
    pads: tuple[int, ...]
    card_edge: bool = False  # gold fingers on the board edge: board copper, no body

    @property
    def shape_basis(self) -> str:
        return "body" if self.bodies else "courtyard" if self.courtyard else \
            "pads" if self.pad_rect else "none"

    @property
    def height_shapes(self) -> list[list[tuple[float, float]]]:
        if self.bodies:
            return self.bodies
        if self.courtyard:
            return [self.courtyard]
        return [self.pad_rect] if self.pad_rect else []

    @property
    def keepout_shapes(self) -> list[list[tuple[float, float]]]:
        shapes = list(self.bodies) + ([self.courtyard] if self.courtyard else [])
        return shapes or ([self.pad_rect] if self.pad_rect else [])

    @property
    def obstacle_shapes(self) -> list[list[tuple[float, float]]]:
        """What a screw head or washer would touch: bodies, else the pad box."""
        return self.bodies or ([self.pad_rect] if self.pad_rect else [])

    @property
    def box(self):
        return _union([_box(shape) for shape in [*self.height_shapes, *self.keepout_shapes]])

    @property
    def label(self) -> str:
        return self.designator or f"#{self.component}"


def _mechanical_names(pcb) -> dict[int, str]:
    """Mechanical layer number (1..16) -> its name."""
    return {layer.v6_id - 56: layer.name for layer in pcb.board.layers
            if layer.v6_id is not None and layer.v6_id in _MECHANICAL}


def _mechanical_number(primitive) -> int | None:
    """A primitive's mechanical layer number; None off the mechanical layers. A layer
    above 16 shares v6 layer 72 with Mechanical 16 (its number from the V7 id)."""
    if primitive.layer not in _MECHANICAL:
        return None
    if primitive.mechanical is not None:
        return primitive.mechanical
    return primitive.layer - 56


def _courtyards(pcb, names: dict[int, str]) -> dict[int, list[tuple[float, float]]]:
    """Part -> the ends of its tracks and arcs on courtyard-named mechanical layers."""
    layers = {number for number, name in names.items() if _COURTYARD_LAYER.search(name)}
    points: dict[int, list[tuple[float, float]]] = defaultdict(list)
    if not layers:
        return points
    for primitive in pcb.primitives:
        if primitive.component is None or primitive.kind not in ("track", "arc"):
            continue
        if _mechanical_number(primitive) in layers:
            points[primitive.component] += [(primitive.x1, primitive.y1),
                                             (primitive.x2, primitive.y2)]
    return points


def _frame_rect(points, rotation: float, ox: float, oy: float):
    """The box of the points in a part's frame, as a polygon in the board's frame."""
    local = _box(assembly.rotate_points(points, -rotation, ox, oy))
    if local[2] - local[0] <= 0 or local[3] - local[1] <= 0:
        return None
    return assembly.rotate_points([(local[0], local[1]), (local[2], local[1]),
                                   (local[2], local[3]), (local[0], local[3])], rotation, ox, oy)


def _build_parts(index, is_dnp, library_heights: dict | None) -> list[PartFit]:
    pcb = index.pcb
    names = _mechanical_names(pcb)
    courtyards = _courtyards(pcb, names)
    tp = tp_components(index)
    bodies: dict[int, list] = defaultdict(list)
    for body in pcb.bodies or ():
        if body.component is not None and len(body.outline) >= 3:
            bodies[body.component].append(body)
    library_heights = library_heights or {}
    parts = []
    for part in assembly.parts(index):
        component = pcb.components[part.component]
        own = [body for body in bodies.get(part.component, ())
               if body.projection in (None, part.side)]
        tall = [body for body in own if (body.overall_height_mil or 0.0) > EPS_MIL]
        height = standoff = model = library = None
        if tall:
            tallest = max(tall, key=lambda body: body.overall_height_mil)
            height, standoff, model = (tallest.overall_height_mil, tallest.standoff_height_mil,
                                       tallest.model_name)
            source = "body"
        elif (component.height_mil or 0.0) > EPS_MIL:
            height, source = component.height_mil, "footprint_height"
        elif part.component in library_heights:
            entry = library_heights[part.component]
            height, standoff = entry.get("height_mil"), entry.get("standoff_mil")
            library, source = entry.get("library"), "library_body"
        else:
            source = "none"
        ox = component.x if component.x is not None else 0.0
        oy = component.y if component.y is not None else 0.0
        courtyard = None
        if part.component in courtyards:
            courtyard = _frame_rect(courtyards[part.component], part.rotation, ox, oy)
        primitives = [pcb.primitives[row - 1] for row in part.pads]
        hole = max((pad.hole or 0.0 for pad in primitives), default=0.0)
        expects = (part.kind not in (_GRAPHICAL, *_NET_TIES) and not part.fiducial
                   and part.mount in ("smd", "tht", "mixed")
                   and not (part.component in tp and part.mount == "smd"))
        parts.append(PartFit(
            component=part.component, designator=part.designator or "", name=part.name,
            side=part.side, kind=part.kind, footprint=part.footprint, comment=part.comment,
            x=ox, y=oy, rotation=part.rotation, fitted=part.fitted(is_dnp),
            height_mil=height, standoff_mil=standoff, source=source,
            bodies=[list(body.outline) for body in tall], flat_bodies=len(own) - len(tall),
            model=model, library=library, courtyard=courtyard, pad_rect=part.pad_rect,
            expects_body=expects, tp=part.component in tp, mount=part.mount,
            hole_mil=hole, pads=part.pads,
        ))
    return parts


def _object_facts(part: PartFit) -> ObjectFacts:
    """The part as a component object for Height and placement rule scopes."""
    box = part.box
    points = ((box[0], box[1]), (box[2], box[1]), (box[2], box[3]), (box[0], box[3])) \
        if box else ((part.x, part.y),)
    layer = 32 if part.side == "bottom" else 1
    return ObjectFacts(kind="component", layer=layer, span=frozenset({layer}), points=points,
                       component=part.designator.lower() or None, footprint=part.footprint,
                       hole=part.hole_mil)


# ---------- the board model ----------


@dataclass
class Hole:
    x: float
    y: float
    hole_mil: float
    slot_length_mil: float | None
    plated: bool | None
    net: int | None
    pad_mil: float | None
    component: int | None
    source: str  # free_pad | bare_hole | part
    geometry_hole: object  # dfm_geometry.Hole
    designator: str | None = None
    # side -> (distance, obstacle) of the nearest copper, body or pad box on that face;
    # exposed: of the nearest part or copper the solder mask leaves open (pads, vias that
    # are not tented), what a screw head or washer would touch
    free: dict = field(default_factory=dict)
    exposed: dict = field(default_factory=dict)
    same_net: tuple | None = None  # (distance, obstacle) of the nearest own-net copper
    edge: tuple | None = None  # (wall distance, edge kind)
    edge_centre: float | None = None  # the hole centre's distance to that edge
    # Parts without pads whose body covers the hole: its standoff, screw or nut models.
    hardware: list = field(default_factory=list)
    on_board: bool = True
    in_cutout: bool = False  # off the board: in a board cutout
    fitted: bool = True  # False: its part is Not Fitted (the hole is drilled all the same)
    # The widest pad concentric with the hole (its own ring: the part's, or a free pad's
    # for a free hole), from _measure.
    ring_mil: float | None = None

    def face(self, side: str) -> tuple | None:
        """(distance, obstacle) of the nearest obstacle on a face, the board edge
        included: its free radius."""
        found = self.free.get(side)
        if self.edge_centre is not None and self.edge_centre <= SEARCH_MIL and (
                found is None or self.edge_centre < found[0]):
            return self.edge_centre, {"kind": self.edge[1]}
        return found


class _Model:
    """What does not depend on the user's limits, built once per index and variant."""

    def __init__(self, index, is_dnp, library_heights, texts):
        # Weak: the index holds the model (build_model), and a reference back would keep a
        # replaced index alive until a full garbage collection (as dfm_geometry's model).
        self._index = weakref.ref(index)
        self.pcb = pcb = index.pcb
        self.origin = (pcb.board.origin_x or 0.0, pcb.board.origin_y or 0.0)
        self.copper = geometry.copper_model(index)
        self.edge = self.copper.edge
        self.parts = _build_parts(index, is_dnp, library_heights)
        self.by_component = {part.component: part for part in self.parts}
        self.texts = texts or {}
        stack = self.copper.stack_signal
        self.faces = {"top": stack[0], "bottom": stack[-1]} if stack else {}
        self.thickness_mil = stack_total_thickness(pcb.board.layers)
        self._mark_card_edges()
        self.regions = self._board_regions()
        self.substacks = {str(item["id"]).upper(): item for item in pcb.board.substacks}
        # A rigid-flex board's layer stacks each have their own thickness (the layers the
        # stack holds); one stack: the board's.
        self.substack_thickness = {
            stack_id: stack_total_thickness(pcb.board.layers, stack_id)
            for stack_id in self.substacks} if len(self.substacks) > 1 else {}
        self.board_regions = self._rigid_flex()
        self.holes = self._mounting_holes()
        self.connectors = self._connectors()

    # --- helpers ---

    @property
    def index(self):
        return self._index()

    def at(self, x: float, y: float) -> list[float]:
        return [_r(x - self.origin[0], 4), _r(y - self.origin[1], 4)]

    def polygon(self, points) -> list[list[float]]:
        return [self.at(x, y) for x, y in points]

    def net_name(self, net) -> str | None:
        nets = self.pcb.nets
        return nets[net] if net is not None and 0 <= net < len(nets) else None

    def designator(self, component) -> str | None:
        if component is None or not 0 <= component < len(self.pcb.components):
            return None
        return self.index.designator(component)

    def fitted_parts(self) -> list[PartFit]:
        return [part for part in self.parts if part.fitted]

    def _mark_card_edges(self) -> None:
        """Card-edge fingers (a bodiless part of at least CARD_EDGE_PADS SMD pads only,
        each elongated and within CARD_EDGE_MIL of the board edge) are the board's own
        copper: no body is expected of them."""
        if not self.edge.has_outline:
            return
        for part in self.parts:
            if not part.expects_body or part.bodies or part.mount != "smd" \
                    or len(part.pads) < CARD_EDGE_PADS:
                continue
            pads = [self.pcb.primitives[row - 1] for row in part.pads]
            if any(max(pad.width, pad.height or pad.width) < CARD_EDGE_ASPECT * min(
                    pad.width, pad.height or pad.width) for pad in pads):
                continue
            if all((self._nearest_piece([oriented_rect_vertices(
                    pad.x1, pad.y1, pad.width, pad.height or pad.width, pad.rotation or 0.0)])
                    or (math.inf,))[0] <= CARD_EDGE_MIL for pad in pads):
                part.card_edge = True
                part.expects_body = False

    # --- regions read from the board ---

    def _board_regions(self) -> list[Region]:
        pcb = self.pcb
        names = _mechanical_names(pcb)
        regions: list[Region] = []
        raw = pcb.raw or {}
        shape_based = "ShapeBasedRegions6/Data" in raw
        # Only the layers that can hold a keepout or a named area are read: a poured
        # board's copper regions are most of the stream.
        wanted = {_KEEP_OUT_LAYER, *_MECHANICAL, *(layer for layer in self.faces.values())}
        records, _ = decode_region_records(
            raw.get("ShapeBasedRegions6/Data") or raw.get("Regions6/Data") or b"", shape_based,
            layers=wanted)
        fills, _ = decode_fill_records(raw.get("Fills6/Data") or b"")
        areas = []
        for region in records:
            if region.board_cutout or region.kind != 0 \
                    or region.polygon not in (None, 0xFFFE, 0xFFFF):
                continue  # cutouts and pour copper
            points = shape_points(region.vertices)  # arc edges as chords
            if len(points) >= 3:
                areas.append((region.layer, region.keepout, region.mechanical, points,
                              region.name or "", region.component))
        for fill in fills:
            outline = fill_outline(fill)
            if outline:
                areas.append((fill.layer, fill.keepout, fill.mechanical, outline, "",
                              fill.component))
        faces = {self.faces.get("top"): "top", self.faces.get("bottom"): "bottom"}
        counts: Counter = Counter()
        for layer, keepout, mechanical, points, name, owner in areas:
            # A footprint's own area keeps its part's name (and applies to the others).
            part = self.designator(owner) if owner is not None else None
            prefix = f"{part} " if part else ""
            data = {"part": part} if part else {}
            if layer == _KEEP_OUT_LAYER:
                counts["keepout_layer"] += 1
                regions.append(Region(
                    "keepout_layer", name or f"{prefix}Keep-Out area {counts['keepout_layer']}",
                    "both", points, source="Keep-Out layer", data=data, owner=owner))
            elif keepout and layer in faces and faces[layer]:
                counts["copper_keepout"] += 1
                regions.append(Region(
                    "copper_keepout", name or f"{prefix}copper keepout {counts['copper_keepout']}",
                    faces[layer], points, source=f"keepout on {self.index.layer_name(layer)}",
                    data=data, owner=owner))
            elif keepout:
                continue  # an inner layer's copper keepout
            elif layer in _MECHANICAL:
                number = mechanical if mechanical is not None else layer - 56
                layer_name = names.get(number) if number <= 16 else None
                if not layer_name:
                    continue
                height = _named_height(layer_name)
                if height is None and not _KEEPOUT_LAYER.search(layer_name):
                    continue
                side = "bottom" if re.search(r"\bbot(?:tom)?\b", layer_name, re.I) else \
                    "top" if re.search(r"\btop\b", layer_name, re.I) else "both"
                counts["named_layer"] += 1
                regions.append(Region(
                    "named_layer", name or f"{prefix}{layer_name} area {counts['named_layer']}",
                    side, points, max_height_mil=height, keepout=height is None,
                    source=f"mechanical layer '{layer_name}'", data=data, owner=owner))
        for rule in pcb.rules:
            if rule.kind != ROOM_KIND or not rule.enabled:
                continue
            points = room_vertices(rule)
            if len(points) < 3:
                continue
            values = rule_values(rule, {})
            regions.append(Region(
                "room", rule.name, values.get("side", "top"), points, source="room",
                data={"confinement": rule.properties.get("CONFINEMENTSTYLE") or None,
                      "scope": rule.scope1}))
        for number, body in enumerate(pcb.bodies or ()):
            if body.component is not None or len(body.outline) < 3:
                continue
            top = body.overall_height_mil or 0.0
            bottom = body.standoff_height_mil or 0.0
            reaches = bottom <= EPS_MIL
            regions.append(Region(
                "free_body", body.model_name or f"3D body {number + 1}", body.projection or "top",
                list(body.outline), max_height_mil=None if reaches else bottom, keepout=reaches,
                source="free 3D body",
                data={"standoff_mil": _r(bottom), "overall_height_mil": _r(top)}))
        return regions

    def _rigid_flex(self) -> list[dict]:
        records, _ = decode_region_records((self.pcb.raw or {}).get("BoardRegions/Data") or b"",
                                           False)
        found = []
        for region in records:
            points = shape_points(region.vertices)
            if len(points) < 3:
                continue
            stack_id = str(region.layer_stack_id or "").upper()
            stack = self.substacks.get(stack_id) or {}
            found.append({"name": region.name or "board region", "polygon": points,
                          "layer_stack": stack.get("name"), "is_flex": bool(stack.get("is_flex")),
                          "bending_lines": region.bending_lines,
                          "thickness_mil": self.substack_thickness.get(stack_id)
                          if self.substack_thickness else self.thickness_mil})
        return found

    # --- mounting holes ---

    def _mounting_parts(self) -> set[int]:
        found = set()
        pcb = self.pcb
        for part in self.parts:
            bare = self._bare_owners().get(part.component, [])
            if not part.pads and not bare:
                continue
            pads = [pcb.primitives[row - 1] for row in part.pads]
            drilled = [(pad.x1, pad.y1, pad.hole) for pad in pads if (pad.hole or 0.0) > 0]
            centres = drilled + bare
            # SMD pads are allowed only as rings around the part's holes (an NPTH with a
            # copper ring on each face); anywhere else the part is no mounting hole.
            if any(not (pad.hole or 0.0) > 0 and not any(
                    math.hypot(pad.x1 - x, pad.y1 - y) <= size / 2.0 for x, y, size in centres)
                    for pad in pads):
                continue
            holes = [size for _, _, size in centres]
            if not holes or max(holes) < MIN_HOLE_MIL - EPS_MIL:
                continue
            nets = {pad.net for pad in pads if pad.net is not None}
            if (_MOUNT_DESIGNATOR.match(part.designator)
                    or _MOUNT_FOOTPRINT.search(part.footprint or "")
                    or part.mount == "mechanical" or len(nets) <= 1) \
                    and designator_prefix(part.designator) not in ("J", "P", "X", "CN", "CON",
                                                                   "CONN", "U", "Q", "D",
                                                                   "LED", "K", "SW", "F"):
                found.add(part.component)
        return found

    def _bare_owners(self) -> dict[int, list[tuple[float, float, float]]]:
        """Part -> (x, y, size) of its bare holes."""
        if not hasattr(self, "_bare_cache"):
            owners: dict[int, list[tuple[float, float, float]]] = defaultdict(list)
            for bare in self.pcb.bare_holes or ():
                if bare.component is not None:
                    owners[bare.component].append((bare.x, bare.y, bare.hole))
            self._bare_cache = owners
        return self._bare_cache

    def _mounting_holes(self) -> list[Hole]:
        pcb = self.pcb
        parts = self._mounting_parts()
        holes = []
        for hole in self.copper.holes:
            if hole.kind == "via" or hole.hole_mil < MIN_HOLE_MIL - EPS_MIL:
                continue
            if hole.component is not None and hole.component not in parts:
                continue
            # A Not Fitted mounting-hole part's hole is drilled all the same: listed and
            # measured, its screw not checked.
            owner = self.by_component.get(hole.component)
            pad_mil = None
            if hole.kind == "pad":
                pad = pcb.primitives[hole.row - 1]
                pad_mil = max(pad.width, pad.height or pad.width)
                if pad_mil <= hole.hole_mil + EPS_MIL:
                    pad_mil = None
            source = "part" if hole.component is not None else \
                "bare_hole" if hole.kind == "bare_hole" else "free_pad"
            item = Hole(hole.x, hole.y, hole.hole_mil, hole.slot_length_mil
                        if hole.hole_shape == 2 else None, hole.plated, hole.net, pad_mil,
                        hole.component, source, hole, designator=self.designator(hole.component),
                        fitted=owner is None or owner.fitted)
            if self.edge.has_outline and not self.edge.on_board(hole.x, hole.y):
                item.on_board = False  # off the outline, or in a board cutout
                item.in_cutout = self.edge.outline.contains(hole.x, hole.y)
            else:
                self._measure(item)
                if item.ring_mil and item.ring_mil > max(item.pad_mil or 0.0,
                                                         item.hole_mil + EPS_MIL):
                    item.pad_mil = item.ring_mil
            holes.append(item)
        holes.sort(key=lambda item: (item.designator or "~", item.x, item.y))
        return holes

    def _measure(self, hole: Hole) -> None:
        index, x, y, reach = self.index, hole.x, hole.y, SEARCH_MIL
        best: dict[str, tuple | None] = {side: None for side in SIDES}
        exposed: dict[str, tuple | None] = {side: None for side in SIDES}
        same: tuple | None = None

        def note(sides, distance, obstacle, net, bare: bool):
            """bare: the obstacle is a part, or copper the solder mask leaves open."""
            nonlocal same
            if hole.net is not None and net == hole.net:
                if same is None or distance < same[0]:
                    same = (distance, obstacle)
                return
            for side in sides:
                if best[side] is None or distance < best[side][0]:
                    best[side] = (distance, obstacle)
                if bare and (exposed[side] is None or distance < exposed[side][0]):
                    exposed[side] = (distance, obstacle)

        faces = self.faces
        if faces:
            sql = _RTREE_SQL if index._has_rtree else _BOX_SQL
            rows = index.db.execute(sql, (x + reach, x - reach, y + reach, y - reach)).fetchall()
            for row in rows:
                if row["id"] == hole.geometry_hole.row:
                    continue
                if row["kind"] == "pad" and row["component"] == hole.component and math.hypot(
                        row["x1"] - x, row["y1"] - y) <= max(hole.hole_mil / 2.0, EPS_MIL):
                    # The hole's own ring: a pad of its part (or, for a free hole, a free
                    # pad) centred within the hole.
                    pad = self.pcb.primitives[row["id"] - 1]
                    hole.ring_mil = max(hole.ring_mil or 0.0, pad.width, pad.height or 0.0)
                    continue
                if hole.component is not None and row["component"] == hole.component:
                    continue
                layers = index._primitive_layers(row)
                sides = [side for side, layer in faces.items() if layer in layers]
                if not sides:
                    continue
                kind = "pour" if row["poured_by"] is not None else row["kind"]
                for side in sides:
                    layer = faces[side]
                    bare = row["kind"] == "pad"
                    if row["kind"] == "via":
                        stack = self.pcb.primitives[row["id"] - 1].via
                        size = stack.pad_diameter(layer, row["width"]) if stack else row["width"]
                        size = size or row["hole"] or 0.0
                        distance = max(0.0, math.hypot(row["x1"] - x, row["y1"] - y) - size / 2.0)
                        bare = stack is None or not (
                            stack.tent_top if side == "top" else stack.tent_bottom)
                    else:
                        distance = min(d for d, _ in index._point_layer_distances(
                            x, y, row, [layer] if row["kind"] == "pad" else None))
                    if distance > reach:
                        continue
                    obstacle = {"kind": kind, "side": side}
                    net = self.net_name(row["net_id"])
                    if net:
                        obstacle["net"] = net
                    owner = self.designator(row["component"])
                    if owner:
                        obstacle["part"] = owner
                    if row["kind"] == "via":
                        obstacle["tented"] = not bare
                    note([side], distance, obstacle, row["net_id"], bare)
            for area in self.copper.areas:
                side = next((side for side, layer in faces.items() if layer == area.layer), None)
                if side is None or (hole.component is not None and area.component == hole.component):
                    continue
                if _box_gap(area.bbox, (x, y, x, y)) > reach:
                    continue
                if area.contains(x, y):
                    distance = 0.0
                else:
                    distance = min(assembly.boundary_distance(x, y, ring) for ring in area.rings)
                if distance > reach:
                    continue
                obstacle = {"kind": area.kind, "side": side}
                net = self.net_name(area.net)
                if net:
                    obstacle["net"] = net
                note([side], distance, obstacle, area.net, False)
        for part in self.fitted_parts():
            if part.component == hole.component:
                continue
            if not part.pads and any(point_in_polygon(x, y, body) for body in part.bodies):
                hole.hardware.append(part.label)  # a standoff, screw or nut placed on it
                continue
            for shape in part.obstacle_shapes:
                if _box_gap(_box(shape), (x, y, x, y)) > reach:
                    continue
                distance = point_polygon_distance(x, y, shape)
                obstacle = {"kind": "body" if part.bodies else "part_pads", "side": part.side,
                            "part": part.label}
                note([part.side], distance, obstacle, None, True)
        if self.edge.has_outline:
            wall = hole.geometry_hole.geometry
            nearest = self._nearest_piece([[(x, y)]], exclude=hole.component)
            if nearest is not None:
                kind = "board_edge" if nearest[1][3] == "outline" else "cutout"
                hole.edge_centre = nearest[0]
                hole.edge = (geometry._GEO(wall, nearest[1][0]), kind)
        hole.free = best
        hole.exposed = exposed
        hole.same_net = same

    # --- connectors ---

    def _connectors(self) -> list[dict]:
        found = []
        for part in self.parts:
            prefix = designator_prefix(part.designator)
            text = self.texts.get(part.component) or " ".join(filter(None, (
                part.comment, part.footprint, self.pcb.components[part.component].lib_reference)))
            if not is_interface_connector(prefix, text):
                continue
            exposure, labels = text_exposure(text)
            entry = {"part": part, "exposure": exposure, "basis": labels}
            if self.edge.has_outline and part.fitted:
                entry.update(self._edge_facts(part))
            found.append(entry)
        return found

    def _nearest_piece(self, shapes, exclude: int | None = None):
        """(distance, piece) of the edge piece nearest the polygons (a polygon of one point
        is that point; 0 where they cross), the cutouts of part `exclude` left out."""
        best = None
        boxes = [(_box(shape), shape) for shape in shapes]
        candidates = []
        for number, piece in enumerate(self.edge.pieces):
            if exclude is not None and piece[5] == exclude:
                continue
            lower = min(_box_gap(box, piece[1]) for box, _ in boxes)
            candidates.append((lower, number, piece))
        candidates.sort(key=lambda item: item[:2])
        for lower, _, piece in candidates:
            if best is not None and lower >= best[0]:
                break
            for _, shape in boxes:
                if len(shape) == 1:
                    x, y = shape[0]
                    shape_geometry = ((x, y, x, y), 0.0, None, None)
                else:
                    shape_geometry = (None, 0.0, shape, None)
                distance = geometry._GEO(shape_geometry, piece[0])
                if best is None or distance < best[0]:
                    best = (distance, piece)
        return best

    def _outline_distance(self, x: float, y: float) -> float:
        point = ((x, y, x, y), 0.0, None, None)
        return min((geometry._GEO(point, piece[0]) for piece in self.edge.pieces
                    if piece[3] == "outline"), default=0.0)

    def _edge_facts(self, part: PartFit) -> dict:
        facts: dict = {}
        body = self._nearest_piece(part.bodies) if part.bodies else None
        # Each pad's rectangle: the pads' box would span a card edge's key notch.
        pad_shapes = [oriented_rect_vertices(pad.x1, pad.y1, pad.width, pad.height or pad.width,
                                             pad.rotation or 0.0)
                      for pad in (self.pcb.primitives[row - 1] for row in part.pads)]
        pads = self._nearest_piece(pad_shapes) if pad_shapes else None
        nearest = body or pads
        if nearest is None:
            return facts
        piece = nearest[1]
        point, normal = self._edge_normal(piece, part.bodies if body else pad_shapes)
        facts["edge"] = self._edge_label(piece, point, normal)
        facts["body_to_edge_mil"] = _r(body[0]) if body else None
        facts["pads_to_edge_mil"] = _r(pads[0]) if pads else None
        overhang = 0.0
        for shape in part.bodies or pad_shapes:
            for x, y in shape:
                if not self.edge.outline.contains(x, y):
                    overhang = max(overhang, self._outline_distance(x, y))
        facts["overhang_mil"] = _r(overhang)
        facts["edge_offset_mil"] = _r(-overhang if overhang > EPS_MIL else nearest[0])
        if overhang > EPS_MIL or nearest[0] <= EDGE_NEAR_MIL:
            facts["facing"] = _facing(normal)
        return facts

    def _edge_normal(self, piece, shapes) -> tuple[tuple[float, float], tuple | None]:
        """(the point of the edge piece nearest the shapes, the edge's outward unit normal
        there; None when the piece has no length). An arc piece: the radial direction at
        that point, outward as a step off the true arc's middle says; where the shapes
        cross the arc, at their vertex deepest past it (over the whole arc edge), else the
        middle of their crossings. A straight piece: its perpendicular, outward as a step
        off its middle says."""
        segment, _, _, arc = piece[0]
        step = 2.0
        if arc is None:
            x1, y1, x2, y2 = segment
            length = math.hypot(x2 - x1, y2 - y1)
            mx, my = (x1 + x2) / 2.0, (y1 + y2) / 2.0
            if length < 1e-9:
                return (mx, my), None
            nx, ny = (y2 - y1) / length, -(x2 - x1) / length
            if self.edge.on_board(mx + nx * step, my + ny * step):
                nx, ny = -nx, -ny
            return (mx, my), (nx, ny)
        cx, cy, radius = arc[0], arc[1], arc[2]
        mx, my = (arc[3] + arc[5]) / 2.0 - cx, (arc[4] + arc[6]) / 2.0 - cy
        apart = math.hypot(mx, my)
        if apart < 1e-9 or radius < 1e-9:
            return (arc[3], arc[4]), None
        mx, my = cx + mx / apart * radius, cy + my / apart * radius  # on the true arc
        # +1: the board is inside the arc's circle (outward is away from the centre).
        sense = -1.0 if self.edge.on_board(mx + (mx - cx) / radius * step,
                                           my + (my - cy) / radius * step) else 1.0
        near = [(geometry.nearest_true_cores((None, 0.0, shape, None), piece[0]), shape)
                for shape in shapes if len(shape) >= 2]
        point = min(near, key=lambda item: item[0][0])[0][2] if near else (arc[3], arc[4])
        crossing = [shape for found, shape in near if found[0] <= EPS_MIL]
        if crossing:
            arcs = [other[0][3] for other in self.edge.pieces
                    if other[2] == piece[2] and other[0][3] is not None]
            deepest = max(((sense * (math.hypot(x - cx, y - cy) - radius), x, y)
                           for shape in crossing for x, y in shape
                           if any(in_arc_span(x, y, other) for other in arcs)),
                          default=None)
            if deepest is not None and deepest[0] > EPS_MIL:
                x, y = deepest[1], deepest[2]
            else:  # edges cross it, no vertex past it: the middle of the crossings
                points = [xy for shape in crossing for xy in _circle_crossings(shape, arc)]
                x = sum(px for px, _ in points) / len(points) if points else point[0]
                y = sum(py for _, py in points) / len(points) if points else point[1]
            apart = math.hypot(x - cx, y - cy)
            if apart > 1e-9:
                point = (cx + (x - cx) / apart * radius, cy + (y - cy) / apart * radius)
        nx, ny = point[0] - cx, point[1] - cy
        length = math.hypot(nx, ny)
        if length < 1e-9:
            return point, None
        return point, (sense * nx / length, sense * ny / length)

    def _edge_label(self, piece, point, normal) -> str:
        """left / right / bottom / top: a piece of the outline within the tolerance of
        that side of the outline's box whose outward normal leans that way (an arc piece
        judged at its point nearest the part); else outline, or cutout."""
        if piece[3] != "outline":
            return "cutout"
        if normal is None:
            return "outline"
        box = self.edge.box
        tolerance = max(0.5 * MM, 0.02 * max(box[2] - box[0], box[3] - box[1]))
        x, y = point
        nx, ny = normal
        if abs(nx) > abs(ny):
            if nx < 0 and abs(x - box[0]) <= tolerance:
                return "left"
            if nx > 0 and abs(x - box[2]) <= tolerance:
                return "right"
        else:
            if ny < 0 and abs(y - box[1]) <= tolerance:
                return "bottom"
            if ny > 0 and abs(y - box[3]) <= tolerance:
                return "top"
        return "outline"


def _circle_crossings(polygon, arc) -> list[tuple[float, float]]:
    """Where a closed polygon's edges cross an arc piece (pcb_geometry's (cx, cy, r, x1,
    y1, x2, y2))."""
    cx, cy, radius = arc[0], arc[1], arc[2]
    found = []
    for (x1, y1), (x2, y2) in zip(polygon, [*polygon[1:], polygon[0]]):
        dx, dy = x2 - x1, y2 - y1
        a = dx * dx + dy * dy
        if a < 1e-12:
            continue
        b = 2.0 * (dx * (x1 - cx) + dy * (y1 - cy))
        c = (x1 - cx) ** 2 + (y1 - cy) ** 2 - radius * radius
        root = b * b - 4.0 * a * c
        if root < 0:
            continue
        for t in ((-b - math.sqrt(root)) / (2.0 * a), (-b + math.sqrt(root)) / (2.0 * a)):
            x, y = x1 + t * dx, y1 + t * dy
            if 0.0 <= t <= 1.0 and in_arc_span(x, y, arc):
                found.append((x, y))
    return found


def _facing(normal) -> str:
    """An outward normal as +X, -X, +Y, -Y (within 22.5 degrees) or an angle."""
    if normal is None:
        return "?"
    angle = math.degrees(math.atan2(normal[1], normal[0]))
    for label, axis in (("+X", 0.0), ("+Y", 90.0), ("-X", 180.0), ("-Y", -90.0),
                        ("-X", -180.0)):
        if abs(angle - axis) <= 22.5:
            return label
    return f"{angle:.0f} deg"


def build_model(index, is_dnp=None, library_heights: dict | None = None,
                texts: dict | None = None) -> _Model:
    """The board model, cached on the index per variant, library heights and texts (the
    last CACHE_SIZE)."""
    not_fitted = ()
    if is_dnp is not None:
        not_fitted = tuple(sorted(part.component for part in assembly.parts(index)
                                  if not part.fitted(is_dnp)))
    key = (not_fitted, tuple(sorted((library_heights or {}).items(), key=lambda kv: kv[0]))
           .__repr__(), tuple(sorted((texts or {}).items())).__repr__())
    cache = index.__dict__.setdefault("_mechanical_fit_cache", {})
    model = cache.get(key)
    if model is None:
        model = _Model(index, is_dnp, library_heights, texts)
        if len(cache) >= CACHE_SIZE:
            cache.pop(next(iter(cache)))
        cache[key] = model
    return model


def _named_height(layer_name: str) -> float | None:
    """A height (mil) a mechanical layer's name states with a unit ('Height 3mm'), when
    the name is about height."""
    if not _HEIGHT_LAYER.search(layer_name):
        return None
    lengths = [match for match in LENGTH.finditer(layer_name) if match.group(2)]
    if len(lengths) != 1:
        return None
    value, _ = length_mm(float(lengths[0].group(1).replace(",", ".")), lengths[0].group(2))
    return value * MM if value > 0 else None


# ---------- the check ----------


@dataclass
class _Limit:
    max_mil: float | None
    min_mil: float | None
    source: str  # user | user_region | free_body | named_layer | height_rule | draftsman_note
    label: str
    given: bool  # by the user or a drawing: a part with no height is then a warning
    data: dict = field(default_factory=dict)


class _Check:
    def __init__(self, model: _Model, selected: list[str], units: str, side_limits: dict,
                 user_regions: list[Region], notes: dict, probe_side: str | None,
                 tall_mil: float, head_mil: float | None, washer_mil: float | None,
                 margin_mil: float):
        self.model = model
        self.index = model.index
        self.selected = set(selected)
        self.units = units
        self.side_limits = side_limits
        self.regions = model.regions + list(user_regions)
        self.notes = notes
        self.probe_side = probe_side
        self.tall_mil = tall_mil
        self.head_mil = head_mil
        self.washer_mil = washer_mil
        self.margin_mil = margin_mil
        self.findings: list[dict] = []
        self.rule_stats: Counter = Counter()
        self.part_limits: dict[int, _Limit] = {}
        self.heights_run = False
        self.limited = 0  # parts a real height limit applies to (prepare's summary)
        self._rules = [rule for rule in model.pcb.rules if rule.kind == "Height" and rule.enabled]
        self._rule_values = {id(rule): rule_values(rule, {}) for rule in self._rules}
        self._resolver = ObjectResolver(
            self._rules, RuleTarget(kind="net", name="", nets=set(), pairs=set()),
            self.index.rule_context) if self._rules else None
        self.batch = getattr(model.pcb, "drc_rule_kinds", None)

    def finding(self, check: str, severity: str, issue: str, **fields) -> None:
        if check not in self.selected:
            return
        record = {"check": check, "severity": severity}
        record.update({key: value for key, value in fields.items() if value is not None})
        record["issue"] = issue
        self.findings.append(record)

    def length(self, mil: float | None) -> str:
        return _mm_text(mil, self.units)

    def point(self, x: float, y: float) -> str:
        rx, ry = self.model.at(x, y)
        if self.units == "mm":
            return f"({rx / MM:.2f}, {ry / MM:.2f}) mm"
        return f"({rx:.1f}, {ry:.1f}) mil"

    # --- heights ---

    def _overlaps(self, shapes, region: Region) -> bool:
        for shape in shapes:
            box = _box(shape)
            if _box_gap(box, region.box) > 0.0:
                continue
            if assembly.outlines_overlap(shape, region.polygon):
                return True
        return False

    def limits(self, part: PartFit) -> list[_Limit]:
        found = []
        if part.side in self.side_limits:
            found.append(_Limit(self.side_limits[part.side], None, "user",
                                f"height_limit_{part.side}", True))
        note = self.notes.get("limits", {}).get(part.side)
        if note:
            found.append(_Limit(note["max_mil"], None, "draftsman_note", note["source"], True,
                                {"quote": note["quote"]}))
        for region in self.regions:
            if region.max_height_mil is None or not region.applies(part):
                continue
            if self._overlaps(part.height_shapes, region):
                found.append(_Limit(region.max_height_mil, None,
                                    "user_region" if region.kind == "user" else region.kind,
                                    region.name, region.kind == "user"))
        if self._resolver is not None:
            rule, undecided = self._resolver.resolve(_object_facts(part))
            if rule is not None:
                values = self._rule_values[id(rule)]
                self.rule_stats[rule.name] += 1
                data = {"conditional": [item["rule"] for item in undecided]} if undecided else {}
                found.append(_Limit(values.get("max_height_mil"), values.get("min_height_mil"),
                                    "height_rule", rule.name, False, data))
            elif undecided:
                self.rule_stats["(undecided)"] += 1
        return found

    def heights(self) -> None:
        self.heights_run = True
        for part in self.model.fitted_parts():
            if not part.height_shapes and part.source == "none":
                continue
            limits = self.limits(part)
            maxima = sorted((limit for limit in limits if limit.max_mil is not None),
                            key=lambda limit: limit.max_mil)
            if maxima:
                self.part_limits[part.component] = maxima[0]
            if any(limit.source != "height_rule" or limit.max_mil < DEFAULT_HEIGHT_RULE_MIL
                   - EPS_MIL for limit in maxima):
                self.limited += 1
            given = any(limit.given for limit in limits)
            if not part.bodies and part.expects_body:
                self._missing(part, given, maxima)
            if part.height_mil is None:
                continue
            over = [limit for limit in maxima if part.height_mil > limit.max_mil + EPS_MIL]
            if over:
                limit = over[0]
                fields = self._part_fields(part)
                extra = ""
                if limit.source == "height_rule":
                    rule = next(rule for rule in self._rules if rule.name == limit.label)
                    if self.batch is not None and rule.kind_id in self.batch:
                        fields["also_reported_by"] = "Altium batch DRC (Height rule)"
                    if limit.data.get("conditional"):
                        fields["rule_undecided_above"] = limit.data["conditional"]
                    extra = f" (Height rule '{limit.label}')"
                elif limit.source == "draftsman_note":
                    fields["note"] = limit.data.get("quote")
                    extra = f" (Draftsman note: {limit.label})"
                else:
                    extra = f" ({limit.source.replace('_', ' ')}: {limit.label})"
                if len(over) > 1:
                    fields["also_over"] = [f"{item.source}: {item.label}" for item in over[1:]]
                self.finding(
                    "height_over_limit", "warning",
                    f"{part.label} is {self.length(part.height_mil)} tall ({part.source}) on "
                    f"the {part.side}: over the {self.length(limit.max_mil)} limit{extra} by "
                    f"{self.length(part.height_mil - limit.max_mil)}.",
                    limit_mil=_r(limit.max_mil), limit_source=limit.source,
                    limit=limit.label, margin_mil=_r(limit.max_mil - part.height_mil), **fields)
            for limit in limits:
                if limit.source == "height_rule" and limit.min_mil and \
                        part.height_mil < limit.min_mil - EPS_MIL:
                    self.finding(
                        "height_under_min", "warning",
                        f"{part.label} is {self.length(part.height_mil)} tall: below the "
                        f"minimum {self.length(limit.min_mil)} of Height rule '{limit.label}'.",
                        min_height_mil=_r(limit.min_mil), rule=limit.label,
                        **self._part_fields(part))

    def _missing(self, part: PartFit, given: bool, maxima: list[_Limit]) -> None:
        fields = self._part_fields(part)
        if part.source == "none":
            severity = "warning" if given else "info"
            issue = (f"{part.label} has no 3D body and no Height: nothing says how tall it "
                     "is")
            if given:
                issue += (f", and the {self.length(maxima[0].max_mil)} limit "
                          f"({maxima[0].source}: {maxima[0].label}) applies to it")
            issue += ". Add a 3D body (or at least the footprint's Height)."
        else:
            severity = "info"
            basis = ("the footprint's Height" if part.source == "footprint_height"
                     else f"its library footprint's body ({part.library})")
            issue = (f"{part.label} has no 3D body on the board: its height "
                     f"{self.length(part.height_mil)} is {basis}; 3D clearance checks and the "
                     "STEP export do not see it.")
        if part.flat_bodies:
            fields["flat_bodies"] = part.flat_bodies
        self.finding("missing_3d_body", severity, issue, **fields)

    def _part_fields(self, part: PartFit) -> dict:
        fields = {"part": part.label, "side": part.side,
                  "height_mil": _r(part.height_mil), "height_source": part.source,
                  "at_mil": self.model.at(part.x, part.y)}
        if part.name and part.name != part.designator:
            fields["sch_refdes"] = part.name
        if part.footprint:
            fields["footprint"] = part.footprint
        return fields

    # --- keepouts ---

    def keepouts(self) -> None:
        keepouts = [region for region in self.regions if region.keepout]
        if not keepouts:
            return
        for part in self.model.fitted_parts():
            shapes = part.keepout_shapes
            if not shapes:
                continue
            for region in keepouts:
                if region.applies(part) and self._overlaps(shapes, region):
                    self.finding(
                        "part_in_keepout", "warning",
                        f"{part.label} ({part.shape_basis}) overlaps {region.kind.replace('_', ' ')} "
                        f"'{region.name}' on the {part.side}, where no part may sit.",
                        region=region.name, region_kind=region.kind,
                        **self._part_fields(part))

    # --- mounting holes ---

    def required(self, hole: Hole) -> tuple[float | None, str, bool]:
        """(required free radius, basis, given by the user)."""
        screw = screw_for_hole(hole.hole_mil)
        given = self.head_mil is not None or self.washer_mil is not None
        head = self.head_mil if self.head_mil is not None else (screw or {}).get("head_mil")
        washer = self.washer_mil if self.washer_mil is not None else (
            (screw or {}).get("washer_mil") if not given else None)
        sizes = [size for size in (head, washer) if size]
        if not sizes:
            return None, "the screw is not known from the hole (pass screw_head_diameter)", given
        widest = max(sizes)
        what = "washer" if washer and widest == washer else "head"
        if (self.washer_mil if what == "washer" else self.head_mil) is not None:
            origin = "given"
        else:
            origin = f"{screw['thread']} " + (
                "ISO 7089 washer" if what == "washer" else "ISO 7045 pan head")
        basis = (f"{what} {self.length(widest)} ({origin}) / 2 + margin "
                 f"{self.length(self.margin_mil)}")
        return widest / 2.0 + self.margin_mil, basis, given

    def holes(self) -> None:
        wall = min(self.model.thickness_mil or DEFAULT_EDGE_WALL_MIL, EDGE_WALL_MAX_MIL)
        self.masked: list[dict] = []
        for hole in self.model.holes:
            if not hole.on_board:
                continue
            label = hole.designator or f"The hole at {self.point(hole.x, hole.y)}"
            required, basis, given = self.required(hole)
            fields = {"hole": hole.designator, "at_mil": self.model.at(hole.x, hole.y),
                      "hole_mil": _r(hole.hole_mil)}
            if required is not None and hole.fitted:  # a Not Fitted part takes no screw
                short = [(side, hole.exposed[side]) for side in SIDES if hole.exposed.get(side)
                         and hole.exposed[side][0] < required - EPS_MIL]
                masked = [(side, hole.free[side]) for side in SIDES if hole.free.get(side)
                          and hole.free[side][0] < required - EPS_MIL]
                if short:
                    side, nearest = min(short, key=lambda item: item[1][0])
                    obstacle = nearest[1]
                    what = _what(obstacle)
                    faces = " and ".join(item[0] for item in short)
                    self.finding(
                        "mounting_hole_clearance", "warning" if given else "info",
                        f"{label}: on the {faces} face{'s' if len(short) > 1 else ''} a "
                        f"{self.length(2 * required)} circle around the hole is not free: "
                        f"{what} at {self.length(nearest[0])} from its centre ({side}), less "
                        f"than {self.length(required)} ({basis}).",
                        sides=[item[0] for item in short], free_radius_mil=_r(nearest[0]),
                        required_radius_mil=_r(required), limited_by=obstacle, **fields)
                elif masked:
                    side, nearest = min(masked, key=lambda item: item[1][0])
                    self.masked.append({**fields, "side": side,
                                        "free_radius_mil": _r(nearest[0]),
                                        "required_radius_mil": _r(required),
                                        "limited_by": nearest[1]})
            if hole.edge is not None and hole.edge[0] < wall - EPS_MIL:
                thin = hole.edge[0] < wall / 2.0 - EPS_MIL
                self.finding(
                    "hole_near_edge", "warning" if thin else "info",
                    f"{label}: its wall is {self.length(hole.edge[0])} from the "
                    f"{hole.edge[1].replace('_', ' ')}, less than {'half of ' if thin else ''}"
                    f"{self.length(wall)} (the board thickness, at most 1 mm): little material "
                    "is left around the screw.",
                    edge_distance_mil=_r(hole.edge[0]), edge=hole.edge[1],
                    min_distance_mil=_r(wall), **fields)
        if self.masked:
            examples = sorted(self.masked, key=lambda item: item["free_radius_mil"])
            plural = len(examples) > 1
            self.finding(
                "mounting_hole_clearance", "info",
                f"{len(examples)} mounting hole{'s have' if plural else ' has'} copper of "
                "another net under the screw head or washer, covered by solder mask (tracks, "
                "pours, tented vias): fine unless the screw must not touch it or the mask may "
                "be scraped; keep that circle free of copper otherwise.",
                count=len(examples), masked_copper_only=True, holes=examples[:HOLES_SAMPLE])

    # --- ICT ---

    def ict(self) -> dict:
        side, chosen_by, probes = probe_sides(self.index, self.probe_side)
        info: dict = {"probe_side": side, "chosen_by": chosen_by,
                      "probe_points": len(probes), "tall_part_height_mil": _r(self.tall_mil),
                      "shadow_mil": _r(SHADOW_MIL)}
        if side is None:
            return info
        sides = SIDES if side == "both" else (side,)
        tall = []
        for part in self.model.fitted_parts():
            if part.side not in sides or part.tp or part.height_mil is None or \
                    part.height_mil < self.tall_mil - EPS_MIL:
                continue
            shapes = part.height_shapes
            near = 0
            for probe in probes:
                if probe.side != part.side or probe.component == part.component:
                    continue
                if any(point_polygon_distance(probe.x, probe.y, shape) < SHADOW_MIL
                       for shape in shapes):
                    near += 1
            if side == "both" and not near:
                continue
            tall.append(part.designator)
            severity = "warning" if chosen_by == "given" else "info"
            # check_testpoints' tall_parts names the probe sites near it.
            see_also = "check_testpoints (tall_parts)" if near else None
            self.finding(
                "tall_part_on_probe_side", severity,
                f"{part.label} is {self.length(part.height_mil)} tall on the {part.side}, the "
                f"side the fixture probes ({chosen_by}): parts at least "
                f"{self.length(self.tall_mil)} tall belong on the other side; {near} probe "
                f"points lie within {self.length(SHADOW_MIL)} of it.",
                probe_points_near=near, see_also=see_also, **self._part_fields(part))
        info["tall_parts"] = len(tall)
        return info

    def result(self) -> list[dict]:
        self.findings.sort(key=lambda finding: (
            _SEVERITY_RANK[finding["severity"]], _CHECK_RANK[finding["check"]],
            -(finding.get("height_mil") or 0.0), str(finding.get("part") or finding.get("hole"))))
        return self.findings


def _what(obstacle: dict) -> str:
    return obstacle["kind"].replace("_", " ") + "".join(
        f" {obstacle[key]}" for key in ("part", "net") if obstacle.get(key))


def _region_summary(model: _Model, regions: list[Region]) -> list[dict]:
    rows = []
    fitted = model.fitted_parts()
    for region in regions:
        members = []
        for part in fitted:
            if not region.applies(part):
                continue
            for shape in part.height_shapes:
                if _box_gap(_box(shape), region.box) <= 0.0 and \
                        assembly.outlines_overlap(shape, region.polygon):
                    members.append(part)
                    break
        known = [part for part in members if part.height_mil is not None]
        tallest = max(known, key=lambda part: part.height_mil, default=None)
        row = {"kind": region.kind, "name": region.name, "side": region.side,
               "source": region.source, "parts": len(members)}
        if region.max_height_mil is not None:
            row["max_height_mil"] = _r(region.max_height_mil)
        if region.keepout:
            row["keepout"] = True
        if tallest is not None:
            row["tallest"] = {"part": tallest.label, "height_mil": _r(tallest.height_mil),
                              "height_source": tallest.source}
        row.update({key: value for key, value in region.data.items() if value is not None})
        rows.append(row)
    return rows


def _side_summary(model: _Model, side: str) -> dict:
    parts = [part for part in model.fitted_parts() if part.side == side
             and (part.height_shapes or part.height_mil is not None)]
    known = sorted((part for part in parts if part.height_mil is not None),
                   key=lambda part: -part.height_mil)
    classes = Counter(_height_class(part.height_mil) for part in known)
    area = sum(assembly.polygon_area(shape) for part in parts for shape in part.height_shapes)
    return {
        "parts": len(parts),
        "height_sources": dict(Counter(part.source for part in parts)),
        "tallest": [{"part": part.label, "height_mil": _r(part.height_mil),
                     "height_source": part.source} for part in known[:TALLEST_LISTED]],
        "height_classes_mm": {label: classes[label] for label in _HEIGHT_LABELS if classes[label]},
        "area_used_mm2": round(area / (MM * MM), 1),
    }


def _hole_row(model: _Model, hole: Hole, required=None) -> dict:
    row = {"hole": hole.designator, "source": hole.source, "at_mil": model.at(hole.x, hole.y),
           "hole_mil": _r(hole.hole_mil)}
    if hole.slot_length_mil:
        row["slot_length_mil"] = _r(hole.slot_length_mil)
    row["plated"] = hole.plated
    net = model.net_name(hole.net)
    if net:
        row["net"] = net
    if hole.pad_mil:
        row["pad_mil"] = _r(hole.pad_mil)
    screw = screw_for_hole(hole.hole_mil)
    row["screw"] = screw["thread"] if screw else None
    if not hole.fitted:
        row["fitted"] = False  # its part is Not Fitted: drilled, no screw checked
    if not hole.on_board:
        row["off_board"] = True
        if hole.in_cutout:
            row["in_cutout"] = True
        return row
    nearest = [hole.face(side) for side in SIDES if hole.face(side)]
    if nearest:
        least = min(nearest, key=lambda item: item[0])
        row["free_radius_mil"] = _r(least[0])
        row["limited_by"] = least[1]
    else:
        row["free_radius_mil"] = _r(SEARCH_MIL)
        row["free_radius_at_least"] = True
    for side in SIDES:
        item = hole.face(side)
        row[f"free_radius_{side}_mil"] = _r(item[0]) if item else _r(SEARCH_MIL)
    bare = [hole.exposed[side] for side in SIDES if hole.exposed.get(side)]
    if bare:
        least = min(bare, key=lambda item: item[0])
        row["exposed_free_radius_mil"] = _r(least[0])
        row["exposed_limited_by"] = least[1]
    if hole.same_net is not None:
        row["own_net_copper_mil"] = _r(hole.same_net[0])
    if hole.hardware:
        row["hardware"] = hole.hardware
    if hole.edge is not None:
        row["edge_distance_mil"] = _r(hole.edge[0])
    if required is not None:
        row["required_radius_mil"] = _r(required[0])
        row["required_basis"] = required[1]
    return row


def _connector_row(model: _Model, entry: dict) -> dict:
    part = entry["part"]
    row = {"part": part.label, "at_mil": model.at(part.x, part.y), "rotation": _r(part.rotation, 2),
           "side": part.side}
    if part.name and part.name != part.designator:
        row["sch_refdes"] = part.name
    if part.footprint:
        row["footprint"] = part.footprint
    row["kind"] = entry["exposure"] or "unknown"
    if entry["basis"]:
        row["kind_basis"] = entry["basis"]
    row["basis"] = "body" if part.bodies else "pads"
    for key in ("edge", "edge_offset_mil", "body_to_edge_mil", "pads_to_edge_mil",
                "overhang_mil", "facing"):
        if entry.get(key) is not None:
            row[key] = entry[key]
    if part.height_mil is not None:
        row["height_mil"] = _r(part.height_mil)
    if not part.fitted:
        row["fitted"] = False
    return row


def _part_row(model: _Model, part: PartFit, limit: _Limit | None = None) -> dict:
    row = {"part": part.label, "side": part.side, "at_mil": model.at(part.x, part.y),
           "rotation": _r(part.rotation, 2), "height_mil": _r(part.height_mil),
           "height_source": part.source}
    if part.standoff_mil:
        row["standoff_mil"] = _r(part.standoff_mil)
    if part.name and part.name != part.designator:
        row["sch_refdes"] = part.name
    if part.footprint:
        row["footprint"] = part.footprint
    if part.model:
        row["model"] = part.model
    if part.library:
        row["library"] = part.library
    if part.card_edge:
        row["card_edge"] = True
    box = _union([_box(shape) for shape in part.height_shapes])
    if box:
        row["extent_mil"] = [*model.at(box[0], box[1]), *model.at(box[2], box[3])]
        row["extent_basis"] = part.shape_basis
    if limit is not None:
        row["limit_mil"] = _r(limit.max_mil)
        row["limit_source"] = limit.source
    return row


def prepare_mechanical_fit(index, *, is_dnp=None, library_heights: dict | None = None,
                           texts: dict | None = None, height_limit_top_mil: float | None = None,
                           height_limit_bottom_mil: float | None = None, regions=None,
                           probe_side: str | None = None, tall_part_height_mil: float | None = None,
                           screw_head_mil: float | None = None, washer_mil: float | None = None,
                           margin_mil: float | None = None, checks=None,
                           note_documents=None, units: str = "mil") -> dict:
    """The whole check, unpaged (render_mechanical_fit pages it), or an error. regions are
    the user's (parse_user_regions input, in units relative to the origin)."""
    selected, error = parse_checks(checks)
    if error:
        return error
    model = build_model(index, is_dnp, library_heights, texts)
    user_regions, error = parse_user_regions(regions, units, model.origin)
    if error:
        return error
    side_limits = {side: value for side, value in (("top", height_limit_top_mil),
                                                   ("bottom", height_limit_bottom_mil))
                   if value is not None}
    notes = note_height_limits(note_documents or [])
    check = _Check(model, selected, units, side_limits, user_regions, notes, probe_side,
                   TALL_PART_MIL if tall_part_height_mil is None else tall_part_height_mil,
                   screw_head_mil, washer_mil,
                   DEFAULT_MARGIN_MIL if margin_mil is None else margin_mil)
    if set(selected) & {"height_over_limit", "height_under_min", "missing_3d_body"}:
        check.heights()
    if "part_in_keepout" in selected:
        check.keepouts()
    if set(selected) & set(_HOLE_CHECKS):
        check.holes()
    ict = check.ict() if "tall_part_on_probe_side" in selected else None
    findings = check.result()
    parts = model.parts
    fitted = model.fitted_parts()
    counted = [part for part in fitted if part.height_shapes or part.height_mil is not None]
    summary = {
        "parts": len(parts),
        "fitted": len(fitted),
        "not_fitted": len(parts) - len(fitted),
        "height_sources": dict(Counter(part.source for part in counted)),
        "missing_3d_body": sum(1 for part in counted if part.expects_body and not part.bodies),
        "no_height": sum(1 for part in counted if part.expects_body and part.height_mil is None),
        "flat_bodies": sum(part.flat_bodies for part in parts),
        "sides": {side: _side_summary(model, side) for side in SIDES},
        "mounting_holes": len(model.holes),
        "connectors": len(model.connectors),
        "regions": dict(Counter(region.kind for region in check.regions)),
        "height_rules": [{"rule": name, "parts": count}
                         for name, count in sorted(check.rule_stats.items())],
        "severity_counts": dict(Counter(finding["severity"] for finding in findings)),
        "findings_by_check": dict(Counter(finding["check"] for finding in findings)),
    }
    if check.heights_run:
        # Parts a real height limit applies to (yours, a region, a free 3D body, a named
        # layer, a drawing note, a Height rule tighter than Altium's 1000 mil default):
        # without any, no height finding says the board fits the enclosure.
        summary["parts_with_height_limit"] = check.limited
    limits = {}
    for side, value in side_limits.items():
        limits[f"height_limit_{side}_mil"] = _r(value)
    if notes["limits"] or notes["conflicts"]:
        limits["draftsman_notes"] = {
            side: {"max_height_mil": _r(item["max_mil"]), "source": item["source"],
                   "quote": item["quote"]} for side, item in notes["limits"].items()}
        if notes["conflicts"]:
            limits["draftsman_conflicts"] = notes["conflicts"]
    if notes["local"]:
        limits["draftsman_local_notes"] = notes["local"][:HOLES_SAMPLE]
    rules = [rule for rule in model.pcb.rules if rule.kind == "Height"]
    if rules:
        limits["height_rules"] = [
            {"rule": rule.name, "scope": rule.scope1, "priority": rule.priority,
             "enabled": rule.enabled,
             **{key: _r(value) for key, value in rule_values(rule, {}).items()
                if key in ("min_height_mil", "preferred_height_mil", "max_height_mil")}}
            for rule in sorted(rules, key=lambda rule: rule.priority)]
    holes = []
    for hole in model.holes:
        required = check.required(hole)
        holes.append(_hole_row(model, hole, required if required[0] is not None else None))
    connectors = [_connector_row(model, entry) for entry in model.connectors
                  if entry["part"].fitted]
    connectors.sort(key=lambda row: (row.get("edge_offset_mil") is None,
                                     row.get("edge_offset_mil") or 0.0, row["part"]))
    rows = [_part_row(model, part, check.part_limits.get(part.component)) for part in counted]
    rows.sort(key=lambda row: (-(row["height_mil"] or -1.0), row["part"]))
    parameters = {
        "tall_part_height_mil": _r(check.tall_mil),
        "screw_head_diameter_mil": _r(screw_head_mil),
        "washer_diameter_mil": _r(washer_mil),
        "clearance_margin_mil": _r(check.margin_mil),
        "min_hole_mil": _r(MIN_HOLE_MIL),
        "search_radius_mil": _r(SEARCH_MIL),
        "screw_defaults": "the largest metric screw passing the hole (ISO 273 clearance); "
                          "head: ISO 7045 pan head (M2 4.0, M2.5 5.0, M3 5.6, M4 8.0 mm); "
                          "washer: ISO 7089 (M2 5, M2.5 6, M3 7, M4 9 mm)",
        "edge_wall_min_mil": _r(min(model.thickness_mil or DEFAULT_EDGE_WALL_MIL,
                                    EDGE_WALL_MAX_MIL)),
    }
    result = {
        "checks_run": selected,
        "origin_mil": [_r(model.origin[0], 4), _r(model.origin[1], 4)],
        "parameters": parameters,
        "limits": limits,
        "summary": summary,
        "findings": findings,
        "parts": rows,
        "not_fitted": sorted(part.label for part in parts if not part.fitted),
        "mounting_holes": holes,
        "connectors": connectors,
        "regions": _region_summary(model, check.regions),
        "notes": _notes(model, check, ict, notes),
    }
    if ict is not None:
        result["ict"] = ict
    return result


def _notes(model: _Model, check: _Check, ict, notes: dict) -> list[str]:
    found = []
    if not model.edge.has_outline:
        found.append("The board has no outline: edge distances, connector edges and hole "
                     "edge checks are not measured.")
    if check._rules:
        found.append(
            "Height rules are applied per part as Altium resolves them (scope and priority on "
            "the component); Altium's batch DRC checks them too, from the 3D bodies when a "
            "part has any (believed), else the footprint Height.")
    if notes.get("conflicts"):
        found.append("Draftsman notes state different maximum heights for the "
                     f"{' and '.join(notes['conflicts'])} side: not used.")
    if notes.get("local"):
        found.append("Draftsman notes state a height limit for one place or with exceptions "
                     "(limits.draftsman_local_notes, quoted): not applied; give it as a "
                     "region (regions=) to check it.")
    if ict is not None and ict.get("probe_side") is None:
        found.append("No test points (Fabrication Testpoint pads or vias, TP parts): the ICT "
                     "probe side is not known; pass probe_side to check tall parts against "
                     "it.")
    found.append(
        "Heights and outlines are the saved 3D bodies' (overall height above the board face, "
        "plan-view projection): the enclosure itself is not read, so give its limits "
        "(height_limit_top / height_limit_bottom, regions) or add it as a free 3D body.")
    found.append(
        "Free radius: the nearest obstacle on each board face to the hole centre (copper of "
        "another net or none on the outer layer, other parts' 3D bodies or pad boxes, the "
        "board edge); copper of the hole's own net is given apart as own_net_copper_mil. "
        "Solder mask and silkscreen are not obstacles here.")
    return found


def prepare_mechanical_fit_cached(index, **kwargs) -> dict:
    """prepare_mechanical_fit cached on the index per arguments (the last CACHE_SIZE), so
    paging reuses it: treat the result as read-only. is_dnp is keyed by its Not Fitted set."""
    is_dnp = kwargs.get("is_dnp")
    key_args = {key: value for key, value in kwargs.items() if key not in ("is_dnp",)}
    dnp = ()
    if is_dnp is not None:
        dnp = tuple(sorted(part.component for part in assembly.parts(index)
                           if not part.fitted(is_dnp)))
    key = repr((sorted(key_args.items(), key=lambda kv: kv[0]), dnp))
    cache = index.__dict__.setdefault("_mechanical_fit_results", {})
    held = cache.get(key)
    if held is not None:
        return held
    prepared = prepare_mechanical_fit(index, **kwargs)
    if "error" not in prepared:
        if len(cache) >= CACHE_SIZE:
            cache.pop(next(iter(cache)))
        cache[key] = prepared
    return prepared


def render_mechanical_fit(prepared: dict, offset: int = 0, limit: int = FINDINGS_LISTED,
                          parts_offset: int = 0, parts_limit: int = PARTS_LISTED,
                          connectors_limit: int = CONNECTORS_LISTED,
                          regions_limit: int = REGIONS_LISTED, holes_limit: int = HOLES_LISTED,
                          side: str | None = None) -> dict:
    """One page: findings from offset, part rows (tallest first) from parts_offset, and
    the connectors, regions and mounting holes, each list cut to its limit; side narrows
    the findings, parts, connectors and regions to one side."""
    offset = max(0, int(offset or 0))
    parts_offset = max(0, int(parts_offset or 0))

    def on_side(row) -> bool:
        return side is None or row.get("side") in (None, side, "both")

    findings = [finding for finding in prepared["findings"] if on_side(finding)]
    rows = [row for row in prepared["parts"] if on_side(row)]
    connectors = [row for row in prepared["connectors"] if on_side(row)]
    regions = [row for row in prepared["regions"] if on_side(row)]
    result = {key: value for key, value in prepared.items()
              if key not in ("findings", "parts", "connectors", "regions", "notes",
                             "mounting_holes")}
    holes = prepared["mounting_holes"]
    result["mounting_holes"] = holes[:holes_limit]
    if len(holes) > holes_limit:
        result["mounting_holes_total"] = len(holes)
    if side:
        result["side"] = side
    result["findings"] = findings[offset:offset + limit]
    result["total"] = len(findings)
    result["offset"] = offset
    result["has_more"] = offset + limit < len(findings)
    if result["has_more"]:
        result["next_offset"] = offset + limit
    result["parts"] = rows[parts_offset:parts_offset + parts_limit]
    result["parts_total"] = len(rows)
    if parts_offset:
        result["parts_offset"] = parts_offset
    if parts_offset + parts_limit < len(rows):
        result["next_parts_offset"] = parts_offset + parts_limit
    result["connectors"] = connectors[:connectors_limit]
    if len(connectors) > connectors_limit:
        result["connectors_total"] = len(connectors)
    result["regions"] = regions[:regions_limit]
    if len(regions) > regions_limit:
        result["regions_total"] = len(regions)
    result["notes"] = prepared["notes"]
    return result


# ---------- the handoff ----------


def _outline(model: _Model) -> dict:
    board = model.pcb.board
    points = board.outline_vertices
    outline: dict = {"polygon_mil": model.polygon(points), "vertices": len(points),
                     "arcs": sum(1 for vertex in board.outline_shape if vertex.arc)}
    cutouts = []
    for region in model.edge.cutouts if model.edge.has_outline else ():
        polygon = shape_points(region.vertices)  # arc edges as chords, as the outline's
        cutout = {"polygon_mil": model.polygon(polygon)}
        owner = model.designator(region.component)
        if owner:
            cutout["part"] = owner
        cutouts.append(cutout)
    outline["cutouts"] = cutouts
    if getattr(model.pcb, "embedded_boards", 0):
        outline["embedded_boards"] = model.pcb.embedded_boards
    return outline


def prepare_handoff(index, *, is_dnp=None, library_heights: dict | None = None,
                    texts: dict | None = None) -> dict:
    """The mechanical handoff, unpaged (render_handoff pages it): board, outline, mounting
    holes, connectors, regions, rigid-flex regions, per-side summaries and part rows."""
    model = build_model(index, is_dnp, library_heights, texts)
    box = model.edge.box if model.edge.has_outline else None
    area = None
    if model.edge.has_outline:
        area = assembly.polygon_area(model.pcb.board.outline_vertices) - sum(
            assembly.polygon_area(shape_points(region.vertices))
            for region in model.edge.cutouts)
    board = {
        "thickness_mil": _r(model.thickness_mil),
        "thickness_source": "layer stack" if model.thickness_mil else None,
        "size_mil": [_r(box[2] - box[0]), _r(box[3] - box[1])] if box else None,
        "extent_mil": [*model.at(box[0], box[1]), *model.at(box[2], box[3])] if box else None,
        "area_mm2": round(area / (MM * MM), 1) if area else None,
        "copper_layers": len(model.copper.stack_signal),
    }
    sides = {}
    for side in SIDES:
        summary = _side_summary(model, side)
        if area:
            summary["area_share"] = round(summary["area_used_mm2"] / (area / (MM * MM)), 3)
        sides[side] = summary
    regions = []
    for region in model.regions:
        row = {"kind": region.kind, "name": region.name, "side": region.side,
               "source": region.source, "polygon_mil": model.polygon(region.polygon)}
        if region.max_height_mil is not None:
            row["max_height_mil"] = _r(region.max_height_mil)
        if region.keepout:
            row["keepout"] = True
        row.update({key: value for key, value in region.data.items() if value is not None})
        regions.append(row)
    if model.substack_thickness:
        # Every stack layer: the rigid regions'; each board region has its stack's own.
        board["thickness_basis"] = "the whole layer stack, the rigid regions' thickness; " + (
            "rigid_flex.board_regions give each region's"
            if any(value is not None for value in model.substack_thickness.values())
            else "the stack does not record which layers the flex regions hold")
    rigid = [{"name": item["name"], "layer_stack": item["layer_stack"],
              "is_flex": item["is_flex"], "bending_lines": item["bending_lines"],
              "thickness_mil": _r(item["thickness_mil"]),
              "polygon_mil": model.polygon(item["polygon"])} for item in model.board_regions]
    parts = [_part_row(model, part) for part in model.fitted_parts()
             if part.height_shapes or part.height_mil is not None]
    parts.sort(key=lambda row: (row["side"], -(row["height_mil"] or -1.0), row["part"]))
    connectors = [_connector_row(model, entry) for entry in model.connectors
                  if entry["part"].fitted]
    connectors.sort(key=lambda row: row["part"])
    shapes = {
        "connectors": {entry["part"].label: [model.polygon(shape) for shape in
                                             (entry["part"].bodies or [entry["part"].pad_rect])
                                             if shape]
                       for entry in model.connectors if entry["part"].fitted},
        "parts": {side: [model.polygon(shape) for part in model.fitted_parts()
                         if part.side == side for shape in part.height_shapes]
                  for side in SIDES},
    }
    notes = []
    if not model.edge.has_outline:
        notes.append("The board has no outline.")
    if any(item["is_flex"] for item in model.board_regions):
        notes.append("Rigid-flex: the outline is the board's shape as saved; each board "
                     "region names its layer stack (flex or rigid), its thickness (the "
                     "layers that stack holds) and bending lines.")
    notes.append("Coordinates are relative to the board origin (origin_mil, absolute), "
                 "viewed from the top; bottom-side outlines are as seen from the top.")
    notes.append("Plan-view outlines and heights: not a replacement for a STEP or IDF / IDX "
                 "exchange.")
    return {
        "origin_mil": [_r(model.origin[0], 4), _r(model.origin[1], 4)],
        "board": board,
        "outline": _outline(model),
        "mounting_holes": [_hole_row(model, hole) for hole in model.holes],
        "connectors": connectors,
        "regions": regions,
        "rigid_flex": {"substacks": [
            {"name": item.get("name"), "is_flex": bool(item.get("is_flex")),
             **({"thickness_mil": _r(model.substack_thickness.get(str(item["id"]).upper()))}
                if model.substack_thickness else {})}
            for item in model.pcb.board.substacks],
            "board_regions": rigid},
        "sides": sides,
        "parts": parts,
        "not_fitted": sorted(part.label for part in model.parts if not part.fitted),
        # The snapshot's refdes of the Not Fitted parts (the netlist name, as mech_parts').
        "_not_fitted_refdes": sorted(part.name or part.label for part in model.parts
                                     if not part.fitted),
        "_shapes": shapes,
        "notes": notes,
    }


def parse_sections(sections) -> tuple[list[str] | None, dict | None]:
    if not sections:
        return list(SECTIONS), None
    wanted = [str(name).strip().lower() for name in sections]
    unknown = [name for name in wanted if name not in SECTIONS]
    if unknown:
        return None, {"error": "invalid_sections",
                      "message": f"Unknown sections: {', '.join(unknown)}.",
                      "valid_sections": list(SECTIONS)}
    return [name for name in SECTIONS if name in wanted], None


def render_handoff(prepared: dict, sections: list[str], parts_offset: int = 0,
                   parts_limit: int = PARTS_LISTED, connectors_limit: int = CONNECTORS_LISTED,
                   regions_limit: int = REGIONS_LISTED, holes_limit: int = HOLES_LISTED,
                   polygons: bool = True) -> dict:
    """One page of the handoff: the sections asked for, part rows from parts_offset, the
    connector and region lists cut to their limits; polygons False leaves the region and
    outline polygons out (their counts stay)."""
    parts_offset = max(0, int(parts_offset or 0))
    result: dict = {"origin_mil": prepared["origin_mil"], "sections": sections}
    for name in sections:
        value = prepared[name]
        if name == "parts":
            rows = value
            result["parts"] = rows[parts_offset:parts_offset + parts_limit]
            result["parts_total"] = len(rows)
            if parts_offset:
                result["parts_offset"] = parts_offset
            if parts_offset + parts_limit < len(rows):
                result["next_parts_offset"] = parts_offset + parts_limit
            result["not_fitted"] = prepared["not_fitted"]
        elif name == "connectors":
            result["connectors"] = value[:connectors_limit]
            if len(value) > connectors_limit:
                result["connectors_total"] = len(value)
        elif name == "mounting_holes":
            result["mounting_holes"] = value[:holes_limit]
            if len(value) > holes_limit:
                result["mounting_holes_total"] = len(value)
        elif name == "regions":
            rows = value[:regions_limit]
            if not polygons:
                rows = [{key: item for key, item in row.items() if key != "polygon_mil"}
                        for row in rows]
            result["regions"] = rows
            if len(value) > regions_limit:
                result["regions_total"] = len(value)
        elif name == "outline" and not polygons:
            result["outline"] = {key: item for key, item in value.items()
                                 if key not in ("polygon_mil", "cutouts")}
            result["outline"]["cutouts"] = len(value["cutouts"])
            result["outline"]["polygons_omitted"] = True
        elif name == "rigid_flex" and not polygons:
            result["rigid_flex"] = {
                "substacks": value["substacks"],
                "board_regions": [{key: item for key, item in row.items() if key != "polygon_mil"}
                                  for row in value["board_regions"]]}
        else:
            result[name] = value
    result["notes"] = prepared["notes"]
    return result


# ---------- drawings ----------


_DXF_LAYERS = (("OUTLINE", 7), ("CUTOUTS", 7), ("HOLES", 1), ("CONNECTORS", 3),
               ("KEEPOUTS", 6), ("ROOMS", 5), ("BOARD_REGIONS", 4), ("PARTS_TOP", 2),
               ("PARTS_BOTTOM", 8), ("LABELS", 7))


def _mm(points) -> list[tuple[float, float]]:
    return [(x / MM, y / MM) for x, y in points]


def _drawing_items(handoff: dict) -> list[tuple]:
    """(layer, kind, data) items of the drawing in mm, origin-relative: kind polyline
    (closed points), circle (x, y, r) or text (x, y, height, text)."""
    items: list[tuple] = []
    outline = handoff["outline"]
    if len(outline["polygon_mil"]) >= 3:
        items.append(("OUTLINE", "polyline", _mm(outline["polygon_mil"])))
    for cutout in outline["cutouts"]:
        items.append(("CUTOUTS", "polyline", _mm(cutout["polygon_mil"])))
    for hole in handoff["mounting_holes"]:
        x, y = hole["at_mil"]
        items.append(("HOLES", "circle", (x / MM, y / MM, hole["hole_mil"] / MM / 2.0)))
        if hole.get("pad_mil"):
            items.append(("HOLES", "circle", (x / MM, y / MM, hole["pad_mil"] / MM / 2.0)))
        if hole.get("hole"):
            items.append(("LABELS", "text", (x / MM + hole["hole_mil"] / MM, y / MM, 1.0,
                                             hole["hole"])))
    for name, shapes in handoff["_shapes"]["connectors"].items():
        for shape in shapes:
            items.append(("CONNECTORS", "polyline", _mm(shape)))
        if shapes:
            x = min(point[0] for point in shapes[0]) / MM
            y = max(point[1] for point in shapes[0]) / MM
            items.append(("LABELS", "text", (x, y + 0.3, 1.0, name)))
    for region in handoff["regions"]:
        layer = {"room": "ROOMS"}.get(region["kind"], "KEEPOUTS")
        items.append((layer, "polyline", _mm(region["polygon_mil"])))
    for region in handoff["rigid_flex"]["board_regions"]:
        if len(handoff["rigid_flex"]["board_regions"]) > 1 or region["is_flex"]:
            items.append(("BOARD_REGIONS", "polyline", _mm(region["polygon_mil"])))
    for side in SIDES:
        for shape in handoff["_shapes"]["parts"][side]:
            items.append((f"PARTS_{side.upper()}", "polyline", _mm(shape)))
    return items


def _number(value: float) -> str:
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def dxf_text(handoff: dict) -> str:
    """The handoff as an AutoCAD R12 ASCII DXF in mm (origin-relative): closed POLYLINEs,
    CIRCLEs and TEXT on the _DXF_LAYERS layers."""
    lines: list[str] = []

    def put(code: int, value) -> None:
        lines.append(str(code))
        lines.append(value if isinstance(value, str) else _number(value))

    put(0, "SECTION")
    put(2, "HEADER")
    put(9, "$ACADVER")
    put(1, "AC1009")
    put(0, "ENDSEC")
    put(0, "SECTION")
    put(2, "TABLES")
    put(0, "TABLE")
    put(2, "LTYPE")
    put(70, "1")
    put(0, "LTYPE")
    put(2, "CONTINUOUS")
    put(70, "0")
    put(3, "Solid line")
    put(72, "65")
    put(73, "0")
    put(40, 0.0)
    put(0, "ENDTAB")
    put(0, "TABLE")
    put(2, "LAYER")
    put(70, str(len(_DXF_LAYERS)))
    for name, colour in _DXF_LAYERS:
        put(0, "LAYER")
        put(2, name)
        put(70, "0")
        put(62, str(colour))
        put(6, "CONTINUOUS")
    put(0, "ENDTAB")
    put(0, "ENDSEC")
    put(0, "SECTION")
    put(2, "ENTITIES")
    for layer, kind, data in _drawing_items(handoff):
        if kind == "polyline":
            put(0, "POLYLINE")
            put(8, layer)
            put(66, "1")
            put(10, 0.0)
            put(20, 0.0)
            put(30, 0.0)
            put(70, "1")
            for x, y in data:
                put(0, "VERTEX")
                put(8, layer)
                put(10, x)
                put(20, y)
                put(30, 0.0)
            put(0, "SEQEND")
            put(8, layer)
        elif kind == "circle":
            x, y, radius = data
            put(0, "CIRCLE")
            put(8, layer)
            put(10, x)
            put(20, y)
            put(30, 0.0)
            put(40, radius)
        else:
            x, y, height, text = data
            put(0, "TEXT")
            put(8, layer)
            put(10, x)
            put(20, y)
            put(30, 0.0)
            put(40, height)
            put(1, _dxf_safe(text))
    put(0, "ENDSEC")
    put(0, "EOF")
    return "\n".join(lines) + "\n"


def _dxf_safe(text: str) -> str:
    """Text a DXF value line can carry: one line, ASCII (others as \\U+XXXX)."""
    text = " ".join(str(text).split())
    return "".join(char if 32 <= ord(char) < 127 else f"\\U+{ord(char):04X}" for char in text)


_SVG_STYLE = {
    "OUTLINE": "fill:none;stroke:#000;stroke-width:0.15",
    "CUTOUTS": "fill:none;stroke:#000;stroke-width:0.15",
    "HOLES": "fill:none;stroke:#c00;stroke-width:0.1",
    "CONNECTORS": "fill:none;stroke:#080;stroke-width:0.12",
    "KEEPOUTS": "fill:none;stroke:#a0a;stroke-width:0.1;stroke-dasharray:0.5,0.3",
    "ROOMS": "fill:none;stroke:#00a;stroke-width:0.08;stroke-dasharray:1,0.5",
    "BOARD_REGIONS": "fill:none;stroke:#088;stroke-width:0.1",
    "PARTS_TOP": "fill:none;stroke:#b80;stroke-width:0.05",
    "PARTS_BOTTOM": "fill:none;stroke:#888;stroke-width:0.05",
    "LABELS": "fill:#000;font-family:sans-serif",
}


def _xml(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def svg_text(handoff: dict) -> str:
    """The handoff as an SVG in mm (origin-relative, y up as on the board: drawn with y
    negated), one group per _DXF_LAYERS layer."""
    items = _drawing_items(handoff)
    xs, ys = [], []
    for _, kind, data in items:
        if kind == "polyline":
            xs += [x for x, _ in data]
            ys += [y for _, y in data]
        elif kind == "circle":
            xs += [data[0] - data[2], data[0] + data[2]]
            ys += [data[1] - data[2], data[1] + data[2]]
        else:
            xs.append(data[0])
            ys.append(data[1])
    if not xs:
        xs, ys = [0.0, 1.0], [0.0, 1.0]
    pad = 2.0
    minx, maxx, miny, maxy = min(xs) - pad, max(xs) + pad, min(ys) - pad, max(ys) + pad
    width, height = maxx - minx, maxy - miny
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           f'<svg xmlns="http://www.w3.org/2000/svg" width="{_number(width)}mm" '
           f'height="{_number(height)}mm" viewBox="{_number(minx)} {_number(-maxy)} '
           f'{_number(width)} {_number(height)}">']
    groups: dict[str, list[str]] = defaultdict(list)
    for layer, kind, data in items:
        if kind == "polyline":
            points = " ".join(f"{_number(x)},{_number(-y)}" for x, y in data)
            groups[layer].append(f'<polygon points="{points}"/>')
        elif kind == "circle":
            groups[layer].append(f'<circle cx="{_number(data[0])}" cy="{_number(-data[1])}" '
                                 f'r="{_number(data[2])}"/>')
        else:
            groups[layer].append(f'<text x="{_number(data[0])}" y="{_number(-data[1])}" '
                                 f'font-size="{_number(data[2])}">{_xml(data[3])}</text>')
    for layer, _ in _DXF_LAYERS:
        if groups.get(layer):
            out.append(f'<g id="{layer.lower()}" style="{_SVG_STYLE[layer]}">')
            out += groups[layer]
            out.append("</g>")
    out.append("</svg>")
    return "\n".join(out) + "\n"


def write_drawings(handoff: dict, output_dir: str, stem: str, formats=("dxf", "svg")) -> dict:
    """Write the drawings into output_dir (an existing folder) as <stem>-mechanical.dxf /
    .svg, a file already there kept (the new one numbered: -mechanical-2.dxf ...);
    {format: path}. Raises OSError when a file cannot be written."""
    folder = Path(output_dir)
    written = {}
    for kind in formats:
        path = folder / f"{stem}-mechanical.{kind}"
        number = 2
        while path.exists():
            path = folder / f"{stem}-mechanical-{number}.{kind}"
            number += 1
        text = dxf_text(handoff) if kind == "dxf" else svg_text(handoff)
        path.write_text(text, encoding="ascii" if kind == "dxf" else "utf-8", newline="\n")
        written[kind] = str(path)
    return written


# ---------- package_for_xfn ----------


def _mm_points(points) -> list[list[float]]:
    return [[round(x / MM, 4), round(y / MM, 4)] for x, y in points]


def _mm_value(mil: float | None) -> float | None:
    return None if mil is None else round(mil / MM, 4)


def export_rows(handoff: dict, pcb_doc: str | None = None) -> dict[str, list[dict]]:
    """Rows of the snapshot's mech_* tables (mm, origin-relative) from prepare_handoff."""
    board = handoff["board"]
    origin = handoff["origin_mil"]
    rows: dict[str, list[dict]] = {
        "mech_board": [{
            "pcb_doc": pcb_doc, "thickness_mm": _mm_value(board["thickness_mil"]),
            "width_mm": _mm_value((board["size_mil"] or [None, None])[0]),
            "height_mm": _mm_value((board["size_mil"] or [None, None])[1]),
            "area_mm2": board["area_mm2"], "origin_x_mm": _mm_value(origin[0]),
            "origin_y_mm": _mm_value(origin[1]),
            "data": {"sides": {side: {key: value for key, value in summary.items()}
                               for side, summary in handoff["sides"].items()},
                     "copper_layers": board["copper_layers"],
                     **({"thickness_basis": board["thickness_basis"]}
                        if board.get("thickness_basis") else {})},
        }],
        "mech_outline": [],
        "mech_mounting_holes": [],
        "mech_connectors": [],
        "mech_regions": [],
        "mech_parts": [],
    }
    outline = handoff["outline"]
    if outline["polygon_mil"]:
        rows["mech_outline"].append({"kind": "outline", "name": None,
                                     "polygon": _mm_points(outline["polygon_mil"]), "data": {}})
    for cutout in outline["cutouts"]:
        rows["mech_outline"].append({"kind": "cutout", "name": cutout.get("part"),
                                     "polygon": _mm_points(cutout["polygon_mil"]), "data": {}})
    for region in handoff["rigid_flex"]["board_regions"]:
        rows["mech_outline"].append({
            "kind": "board_region", "name": region["name"],
            "polygon": _mm_points(region["polygon_mil"]),
            "data": {"layer_stack": region["layer_stack"], "is_flex": region["is_flex"],
                     "bending_lines": region["bending_lines"],
                     **({"thickness_mm": _mm_value(region["thickness_mil"])}
                        if region.get("thickness_mil") is not None else {})}})
    for hole in handoff["mounting_holes"]:
        limited = hole.get("limited_by")
        rows["mech_mounting_holes"].append({
            "refdes": hole.get("hole"), "x_mm": _mm_value(hole["at_mil"][0]),
            "y_mm": _mm_value(hole["at_mil"][1]), "drill_mm": _mm_value(hole["hole_mil"]),
            "slot_length_mm": _mm_value(hole.get("slot_length_mil")),
            "plated": None if hole.get("plated") is None else int(bool(hole["plated"])),
            "net_name": hole.get("net"), "pad_mm": _mm_value(hole.get("pad_mil")),
            "screw": hole.get("screw"), "free_radius_mm": _mm_value(hole.get("free_radius_mil")),
            "limited_by": None if not limited else " ".join(
                str(limited[key]) for key in ("kind", "part", "net", "side") if limited.get(key)),
            "edge_distance_mm": _mm_value(hole.get("edge_distance_mil")),
            "fitted": 0 if hole.get("fitted") is False else 1,
        })
    for row in handoff["connectors"]:
        rows["mech_connectors"].append({
            "refdes": row.get("sch_refdes") or row["part"], "x_mm": _mm_value(row["at_mil"][0]),
            "y_mm": _mm_value(row["at_mil"][1]), "rotation": row["rotation"], "side": row["side"],
            "edge": row.get("edge"), "edge_offset_mm": _mm_value(row.get("edge_offset_mil")),
            "overhang_mm": _mm_value(row.get("overhang_mil")), "facing": row.get("facing"),
            "kind": row.get("kind"),
            "data": {"designator": row["part"], "footprint": row.get("footprint"),
                     "basis": row.get("basis"),
                     "body_to_edge_mm": _mm_value(row.get("body_to_edge_mil")),
                     "pads_to_edge_mm": _mm_value(row.get("pads_to_edge_mil"))},
        })
    for region in handoff["regions"]:
        rows["mech_regions"].append({
            "kind": region["kind"], "name": region["name"], "side": region["side"],
            "polygon": _mm_points(region["polygon_mil"]),
            "max_height_mm": _mm_value(region.get("max_height_mil")),
            "data": {key: value for key, value in region.items()
                     if key not in ("kind", "name", "side", "polygon_mil", "max_height_mil")},
        })
    for row in handoff["parts"]:
        extent = row.get("extent_mil")
        rows["mech_parts"].append({
            "refdes": row.get("sch_refdes") or row["part"], "side": row["side"],
            "x_mm": _mm_value(row["at_mil"][0]), "y_mm": _mm_value(row["at_mil"][1]),
            "rotation": row["rotation"], "height_mm": _mm_value(row["height_mil"]),
            "standoff_mm": _mm_value(row.get("standoff_mil")),
            "height_source": row["height_source"], "fitted": 1,
            "extent": [round(value / MM, 4) for value in extent] if extent else None,
        })
    for name in handoff.get("_not_fitted_refdes", handoff["not_fitted"]):
        rows["mech_parts"].append({"refdes": name, "side": None, "x_mm": None, "y_mm": None,
                                   "rotation": None, "height_mm": None, "standoff_mm": None,
                                   "height_source": "not_fitted", "fitted": 0, "extent": None})
    return rows
