"""Copper connectivity of the saved board, offline: per net, whether its copper
joins all its pads (Altium's Un-Routed Net rule), copper of a net that reaches no
pad, and open-ended tracks (Altium's Net Antennae rule).

A net's copper starts as the length engine's graph (pcb_length.NetGraph: tracks,
arc chords, vias with barrels, pads), here with a hatched pour's tracks and arcs.
Same-net copper that touches is joined too (crossing tracks, overlapping edges),
as Altium's Un-Routed Net rule counts touching copper as connected, and so is area
copper: the regions a solid pour was poured into as saved in the file (Regions6,
thermal spokes included), free copper regions and fills, and an internal plane
assigned to the net. A net is routed when its pads are in one piece; each missing
connection is a spanning-tree edge between islands, nearest pads first (Altium's
connection lines). Pads join only through copper, or through a Jumper component's
jumper; net ties and pad Jumper IDs are not modelled.

Results are cached on the PcbIndex instance per net. Lengths are mils under *_mil
keys.
"""

import math
import re
from collections import Counter, defaultdict
from dataclasses import replace

try:
    from parsers.drc_report import RULE
    from parsers.pcb_doc import parse_dim_mil
    from services.drc_results import rule_kind
    from services.pcb_index import PcbIndex
    from services.pcb_length import NetGraph, stack_z
    from services.pcb_rules import (
        ObjectFacts, ObjectResolver, RuleTarget, evaluate_scope, scope_name,
    )
except ImportError:
    from server.parsers.drc_report import RULE
    from server.parsers.pcb_doc import parse_dim_mil
    from server.services.drc_results import rule_kind
    from server.services.pcb_index import PcbIndex
    from server.services.pcb_length import NetGraph, stack_z
    from server.services.pcb_rules import (
        ObjectFacts, ObjectResolver, RuleTarget, evaluate_scope, scope_name,
    )

CHECKS = ("unrouted", "isolated", "antennae", "weak")
# weak_connection findings only matter with the Un-Routed Net rule's 'Check for
# incomplete connections' option on: listed when asked for, or when the rule of
# a checked net has (or may have) it on; the summary counts them always.
DEFAULT_CHECKS = ("unrouted", "isolated", "antennae")
# The findings' own names select their check too.
_CHECK_ALIASES = {
    "isolated_copper": "isolated", "antenna": "antennae", "weak_connection": "weak",
}
# Copper this close counts as touching (coordinates are stored in 1e-4 mil).
TOUCH_MIL = 0.01
# Centrelines (and centres) this close count as meeting for weak_connection:
# Altium's 'coincide' tolerance is not documented, and sub-mil snapping
# offsets are common on real boards.
CENTRE_MIL = 0.5
# Copper of two islands this close is reported as a near miss (copper_gap_mil).
NEAR_MIL = 25.0
CONNECTIONS_LISTED = 20
NEAR_MISSES_LISTED = 5
_CELL_MIL = 50.0
_UNROUTED_NET = 16
_NET_ANTENNAE = 56
_JUMPER = 6  # PcbComponent.kind of a Jumper component
_SEVERITY_ORDER = {"warning": 0, "info": 1}
_CHECK_ORDER = {"unrouted": 0, "isolated_copper": 1, "antenna": 2, "weak_connection": 3}
_ROWS_SQL = (
    "SELECT * FROM prims WHERE net_id = ? AND kind IN ('track', 'arc', 'via', 'pad') "
    "AND pour_id IS NULL ORDER BY id"
)
_WALK_LIMIT = 100_000

APPROXIMATIONS = [
    f"Copper within {TOUCH_MIL:g} mil touches and connects, as in Altium's Un-Routed "
    "Net rule. weak_connection lists the joins that Altium's 'Check for incomplete "
    "connections' option rejects (tracks whose centrelines do not meet, a track whose "
    "centreline misses the pad, via or pour copper it touches, a via whose centre is "
    "off the pad or via, a pad or via connected directly to a pour whose centre is "
    "outside the pour's outline; centrelines and centres within "
    f"{CENTRE_MIL:g} mil count as meeting) where a net needs them: with that option on, "
    "its nets_joined_only_by_weak_connections would read un-routed.",
    "The option is that of the Un-Routed Net rule that applies to each net: the first "
    "enabled rule by priority whose scope matches the net, every rule that may come "
    "first counting when a scope is not decided for the net "
    "(incomplete_connections_checked null when they differ). A rule that does not save "
    "it has it off: Altium's help gives no default, and boards save it only when on "
    "(CHECKBADCONNECTIONS=TRUE) while other options are saved as FALSE too. Where it is "
    "or may be on, weak_connection findings are listed by default. Where it is on, they "
    "are warnings, and a net routed here only through weak connections reads un-routed "
    "in Altium (nets_unrouted_in_altium). Its status here and the routing counts still "
    "follow touching copper; altium_drc counts it as not routed here, and its "
    "connections joined only through weak connections as missing.",
    "A pad or via touching a pour's copper is tested when its Polygon Connect Style is "
    "Direct (Altium tests the centre 'when the Pad/Via uses a direct connection'), "
    "against the pour's outline with its arcs, not the poured copper: Altium's fix for "
    "this violation is to extend the polygon over the centre (a track's centreline is "
    "tested against the poured copper). The style is the first enabled rule by priority "
    "whose first scope matches the pad or via and second the pour. Whether Altium also "
    "matches them the other way round is not documented, a pour without a saved name "
    "leaves name terms open, and a room term on a pour is decided only when the pour "
    "lies clear of the room, so every rule that may come first counts: the pad or via "
    "is tested only when they all say Direct.",
    "Pour copper is the copper saved in the file (a solid pour's regions, a hatched "
    "pour's tracks and arcs): a pour changed since its last repour is checked as "
    "saved; a shelved pour, and one with no saved copper (never poured, or poured to "
    "nothing), connects nothing and is named beside the net's missing connections.",
    "An internal plane assigned to a net connects every pad and via of that net that "
    "crosses the layer, except inside another net's split plane (its outline) and "
    "where a Power Plane Connect Style rule says No Connect. Plane clearances, voids, "
    "plane islands and non-plated holes are not checked; Altium checks planes with the "
    "Batch DRC options Report Broken Planes, Dead Copper and Starved Thermals.",
    "Pads join through copper. A Jumper component's pads on one net are joined by its "
    "jumper; pad Jumper IDs on other parts are not read, so pads Altium joins that way "
    "read as missing connections (same_component when both pads are on one part). "
    "Net ties are not modelled.",
    "Connections are counted as Altium documents routing completion: a net of N pads "
    "needs N - 1. Each missing one is named by the nearest pads of two copper islands "
    "and their centre distance; Altium draws its connection lines between the nearest "
    "objects of the sub-nets (often a track end or via), so copper_gap gives the "
    "islands' closest copper approach when it is within "
    f"{NEAR_MIL:g} mil.",
    "Copper of a net that reaches none of its pads (isolated_copper) is not counted as "
    "a missing connection, as Altium's definition counts pads; Altium may still draw a "
    "connection line to it. Its open track ends are antennae (in_isolated_copper), "
    "as Altium's Net Antennae rule flags any open-ended track.",
    "An antenna's stub is the track and arc length from its open end (or the via it "
    "ends on) back to the first branch, pad, via or area copper; the open track is the "
    "open-ended track or arc alone. Altium's tolerance is for 'the stub of an "
    "open-ended track/arc primitive' and its documentation does not say which length "
    "is meant: over_tolerance compares the open track (its violations name one "
    "primitive, and its Bluetooth Sentinel example allows 30 mm against a 36 mm stub "
    "whose open track is 4.4 mm), stub_over_tolerance the stub.",
]


def _r(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(value, digits)


def _pct(part: int, whole: int) -> float | None:
    return round(100.0 * part / whole, 1) if whole else None


def _incomplete_option(rule) -> bool | None:
    """An Un-Routed Net rule's 'Check for incomplete connections' as saved
    (CHECKBADCONNECTIONS); None when the rule does not save it (off)."""
    properties = {key.upper(): value for key, value in rule.properties.items()}
    value = properties.get("CHECKBADCONNECTIONS")
    return None if value is None else value.strip().upper() == "TRUE"


class _UnionFind:
    def __init__(self, size: int = 0):
        self.parent = list(range(size))

    def add(self) -> int:
        self.parent.append(len(self.parent))
        return len(self.parent) - 1

    def find(self, item: int) -> int:
        parent = self.parent
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    def union(self, first: int, second: int) -> bool:
        """Join two sets; False when they were one already."""
        root_a, root_b = self.find(first), self.find(second)
        if root_a == root_b:
            return False
        self.parent[root_a] = root_b
        return True


def _box(row) -> tuple[float, float, float, float]:
    box = row.get("_box") if isinstance(row, dict) else None
    return box or (row["minx"], row["miny"], row["maxx"], row["maxy"])


def _boxes_within(first, second, margin: float) -> bool:
    return (
        first[0] <= second[2] + margin and second[0] <= first[2] + margin
        and first[1] <= second[3] + margin and second[1] <= first[3] + margin
    )


def _box_gap(first, second) -> float:
    dx = max(0.0, first[0] - second[2], second[0] - first[2])
    dy = max(0.0, first[1] - second[3], second[1] - first[3])
    return math.hypot(dx, dy)


def _core(row) -> tuple[float, float, float, float]:
    """A row's centreline (a pad's or via's centre as a point segment)."""
    if row["kind"] in ("track", "arc"):
        return row["x1"], row["y1"], row["x2"], row["y2"]
    return row["x1"], row["y1"], row["x1"], row["y1"]


def _closest_on(px, py, x1, y1, x2, y2) -> tuple[float, float]:
    dx, dy = x2 - x1, y2 - y1
    length = dx * dx + dy * dy
    t = 0.0 if not length else max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / length))
    return x1 + t * dx, y1 + t * dy


def _closest_pair(a, b) -> tuple[float, tuple, tuple]:
    """(distance, point on a, point on b) of two segments' closest points; a
    point is a zero-length segment."""
    candidates = []
    for point, segment, first in (
        ((a[0], a[1]), b, True), ((a[2], a[3]), b, True),
        ((b[0], b[1]), a, False), ((b[2], b[3]), a, False),
    ):
        other = _closest_on(*point, *segment)
        pair = (point, other) if first else (other, point)
        candidates.append((math.dist(point, other), *pair))
    # Crossing centrelines meet where they cross.
    ax, ay, bx, by = a
    cx, cy, ex, ey = b
    denominator = (bx - ax) * (ey - cy) - (by - ay) * (ex - cx)
    if denominator:
        t = ((cx - ax) * (ey - cy) - (cy - ay) * (ex - cx)) / denominator
        u = ((cx - ax) * (by - ay) - (cy - ay) * (bx - ax)) / denominator
        if 0.0 <= t <= 1.0 and 0.0 <= u <= 1.0:
            point = (ax + t * (bx - ax), ay + t * (by - ay))
            candidates.append((0.0, point, point))
    return min(candidates, key=lambda item: item[0])


def _reach(row) -> float:
    """How far a track's or via's copper reaches from its centreline or
    centre; a pad's edge is not known here (0)."""
    return 0.0 if row["kind"] == "pad" else (row["width"] or 0.0) / 2.0


def _toward(start, end, distance: float) -> tuple[float, float]:
    """The point distance along start -> end, never past end."""
    length = math.dist(start, end)
    if length <= distance or not length:
        return end
    return (
        start[0] + (end[0] - start[0]) * distance / length,
        start[1] + (end[1] - start[1]) * distance / length,
    )


def _meeting_point(first, second, gap: float = 0.0) -> tuple[float, float]:
    """Where two rows' copper meets or comes closest (gap apart): from the
    closest points of their centrelines (or centres), past the track or via
    copper and half the gap. Two pads meet halfway between their centres."""
    _, on_first, on_second = _closest_pair(_core(first), _core(second))
    if first["kind"] == "pad" and second["kind"] == "pad":
        return _toward(on_first, on_second, math.dist(on_first, on_second) / 2.0)
    if first["kind"] == "pad":
        return _toward(on_second, on_first, _reach(second) + gap / 2.0)
    return _toward(on_first, on_second, _reach(first) + gap / 2.0)


def _grid_range(low: float, high: float):
    return range(math.floor(low / _CELL_MIL), math.floor(high / _CELL_MIL) + 1)


def _segment_cells(x1, y1, x2, y2, reach: float) -> set[tuple[int, int]]:
    """Grid cells within reach of a segment: samples along it at most a cell
    apart (every point of it within half a cell of one), each with the cells
    around it that half a cell plus reach can get to."""
    length = math.hypot(x2 - x1, y2 - y1)
    steps = max(1, math.ceil(length / _CELL_MIL))
    ring = math.ceil((0.5 * _CELL_MIL + reach) / _CELL_MIL)
    cells: set[tuple[int, int]] = set()
    for step in range(steps + 1):
        fraction = step / steps
        gx = math.floor((x1 + (x2 - x1) * fraction) / _CELL_MIL)
        gy = math.floor((y1 + (y2 - y1) * fraction) / _CELL_MIL)
        for cx in range(gx - ring, gx + ring + 1):
            for cy in range(gy - ring, gy + ring + 1):
                cells.add((cx, cy))
    return cells


def _row_cells(row, reach: float) -> set[tuple[int, int]]:
    """Grid cells the copper of a prims row can reach within reach."""
    if row["kind"] in ("track", "arc"):
        half = (row["width"] or 0.0) / 2.0
        if row["kind"] == "arc" and row["arc_radius"]:
            # A chord lies up to its sagitta inside the arc piece.
            half += 0.5
        return _segment_cells(row["x1"], row["y1"], row["x2"], row["y2"], half + reach)
    minx, miny, maxx, maxy = _box(row)
    return {
        (gx, gy)
        for gx in _grid_range(minx - reach, maxx + reach)
        for gy in _grid_range(miny - reach, maxy + reach)
    }


def _grids(rows, row_layers, reach: float) -> dict[int, dict]:
    """Per layer, grid cell -> positions of the rows whose copper on that
    layer can come within reach of the cell."""
    grids: dict[int, dict] = defaultdict(lambda: defaultdict(list))
    for number, row in enumerate(rows):
        cells = _row_cells(row, reach)
        for layer in row_layers[number]:
            grid = grids[layer]
            for cell in cells:
                grid[cell].append(number)
    return grids


class _Area:
    """Area copper of one net on one layer: an outline with holes, or a whole
    plane layer (outline None) less other nets' split planes."""

    def __init__(
        self, kind: str, layer: int, net_id: int, outline=None, holes=(),
        pour_id: int | None = None, assumed: bool = False, excluded=(),
    ):
        self.kind = kind
        self.layer = layer
        self.net_id = net_id
        self.pour_id = pour_id
        self.assumed = assumed
        self.excluded = list(excluded)
        self.outline = list(outline) if outline is not None else None
        self.holes = [list(hole) for hole in holes]
        self.bbox: tuple[float, float, float, float] | None = None
        self._edges: list[tuple[float, float, float, float]] | None = None
        self._bands: dict[int, list[int]] = defaultdict(list)
        self._grid: dict[tuple[int, int], list[int]] = defaultdict(list)
        if self.outline is not None:
            xs = [point[0] for point in self.outline]
            ys = [point[1] for point in self.outline]
            self.bbox = (min(xs), min(ys), max(xs), max(ys))

    @property
    def edges(self) -> list[tuple[float, float, float, float]]:
        """The outline's and holes' edges, indexed on first use: by y band
        (point in polygon) and by the grid cells they pass (edges near a box)."""
        if self._edges is None:
            self._edges = []
            for ring in (self.outline or [], *self.holes):
                closed = list(ring) + [ring[0]] if ring else []
                for (x1, y1), (x2, y2) in zip(closed, closed[1:]):
                    if (x1, y1) != (x2, y2):
                        self._edges.append((x1, y1, x2, y2))
            for number, (x1, y1, x2, y2) in enumerate(self._edges):
                for band in _grid_range(min(y1, y2), max(y1, y2)):
                    self._bands[band].append(number)
                start = (math.floor(x1 / _CELL_MIL), math.floor(y1 / _CELL_MIL))
                end = (math.floor(x2 / _CELL_MIL), math.floor(y2 / _CELL_MIL))
                if start == end:
                    self._grid[start].append(number)
                    continue
                steps = max(1, math.ceil(2.0 * math.hypot(x2 - x1, y2 - y1) / _CELL_MIL))
                for cell in {
                    (
                        math.floor((x1 + (x2 - x1) * step / steps) / _CELL_MIL),
                        math.floor((y1 + (y2 - y1) * step / steps) / _CELL_MIL),
                    )
                    for step in range(steps + 1)
                }:
                    self._grid[cell].append(number)
        return self._edges

    @property
    def whole_layer(self) -> bool:
        return self.outline is None

    def contains(self, x: float, y: float) -> bool:
        """Is the point in the copper (outline less holes, even-odd)?"""
        if self.outline is None:
            return not any(area.contains(x, y) for area in self.excluded)
        minx, miny, maxx, maxy = self.bbox
        if not (minx <= x <= maxx and miny <= y <= maxy):
            return False
        edges = self.edges
        inside = False
        for number in self._bands.get(math.floor(y / _CELL_MIL), ()):
            x1, y1, x2, y2 = edges[number]
            if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
                inside = not inside
        return inside

    def edges_near(self, minx: float, miny: float, maxx: float, maxy: float) -> set[int]:
        self.edges  # index on first use
        found: set[int] = set()
        for gx in range(math.floor(minx / _CELL_MIL) - 1, math.floor(maxx / _CELL_MIL) + 2):
            for gy in range(math.floor(miny / _CELL_MIL) - 1, math.floor(maxy / _CELL_MIL) + 2):
                found.update(self._grid.get((gx, gy), ()))
        return found

    def touches(self, geometry, ref: tuple[float, float], box) -> bool:
        """Does copper (a PcbIndex geometry tuple, ref a point of it, box its
        bounds) touch this area?"""
        if self.contains(*ref):
            return True
        if self.outline is None or not _boxes_within(box, self.bbox, TOUCH_MIL):
            return False
        for number in self.edges_near(
            box[0] - TOUCH_MIL, box[1] - TOUCH_MIL, box[2] + TOUCH_MIL, box[3] + TOUCH_MIL
        ):
            edge = (self.edges[number], 0.0, None, None)
            if PcbIndex._geometry_distance(geometry, edge) <= TOUCH_MIL:
                return True
        return False


def _areas_touch(first: _Area, second: _Area) -> bool:
    if first.layer != second.layer:
        return False
    if first.whole_layer or second.whole_layer:
        whole, other = (first, second) if first.whole_layer else (second, first)
        if other.whole_layer:
            return True
        return any(whole.contains(x, y) for x, y in other.outline)
    if not _boxes_within(first.bbox, second.bbox, TOUCH_MIL):
        return False
    if second.contains(*first.outline[0]) or first.contains(*second.outline[0]):
        return True
    for x1, y1, x2, y2 in first.edges:
        box = (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
        if not _boxes_within(box, second.bbox, TOUCH_MIL):
            continue
        segment = ((x1, y1, x2, y2), 0.0, None, None)
        for number in second.edges_near(*box):
            if PcbIndex._geometry_distance(
                segment, (second.edges[number], 0.0, None, None)
            ) <= TOUCH_MIL:
                return True
    return False


def _spanning_edges(points: list[tuple[float, float]], groups: list[int]) -> list[tuple]:
    """(i, j, distance) edges of a minimum spanning tree joining the groups,
    each between the nearest points of two groups (Boruvka over a grid; the
    largest group is never searched from, which keeps one big island cheap)."""
    group_ids = sorted(set(groups))
    if len(group_ids) < 2:
        return []
    position = {group: number for number, group in enumerate(group_ids)}
    sets = _UnionFind(len(group_ids))
    member = [position[group] for group in groups]
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    span = max(max(xs) - min(xs), max(ys) - min(ys), 1.0)
    cell = max(span / math.sqrt(len(points)), 1.0)
    grid: dict[tuple[int, int], list[int]] = defaultdict(list)
    for number, (x, y) in enumerate(points):
        grid[(math.floor(x / cell), math.floor(y / cell))].append(number)
    max_ring = math.ceil(span / cell) + 1
    edges: list[tuple] = []
    while True:
        sizes = Counter(sets.find(item) for item in member)
        if len(sizes) < 2:
            break
        largest = sizes.most_common(1)[0][0]
        best: dict[int, tuple] = {}
        for number, (x, y) in enumerate(points):
            root = sets.find(member[number])
            if root == largest:
                continue
            found = best.get(root, (math.inf,))[0]
            gx, gy = math.floor(x / cell), math.floor(y / cell)
            for ring in range(max_ring + 1):
                if (ring - 1) * cell > found:
                    break
                if ring == 0:
                    cells = [(gx, gy)]
                else:
                    cells = [(gx + d, gy - ring) for d in range(-ring, ring + 1)]
                    cells += [(gx + d, gy + ring) for d in range(-ring, ring + 1)]
                    cells += [(gx - ring, gy + d) for d in range(-ring + 1, ring)]
                    cells += [(gx + ring, gy + d) for d in range(-ring + 1, ring)]
                for key in cells:
                    for other in grid.get(key, ()):
                        if sets.find(member[other]) == root:
                            continue
                        distance = math.hypot(points[other][0] - x, points[other][1] - y)
                        if distance < found:
                            found = distance
                            best[root] = (distance, number, other)
        if not best:
            break
        for distance, first, second in sorted(best.values()):
            if sets.union(member[first], member[second]):
                edges.append((first, second, distance))
    return edges


class Connectivity:
    """Connectivity of every net of one board, computed per net on demand."""

    def __init__(self, index):
        self.index = index
        self.pcb = index.pcb
        self.order = index._copper_layers_in_stack_order()
        self.copper = set(self.order)
        self.z = stack_z(index)
        self.model: Counter = Counter()
        self.areas_by_net: dict[int, list[_Area]] = defaultdict(list)
        self.unpoured: dict[int, list] = defaultdict(list)
        self.results: dict[int, dict] = {}
        self._layer_names: dict[int, str] = {}
        self.rules = {
            kind: sorted(
                (rule for rule in self.pcb.rules if rule.kind == kind),
                key=lambda rule: rule.priority,
            )
            for kind in ("UnRoutedNet", "NetAntennae", "PlaneConnect", "PolygonConnect")
        }
        self._resolvers: dict[tuple[str, str], ObjectResolver] = {}
        self._pour_outlines: dict[int, _Area | None] = {}
        self._pour_centres: dict[tuple[int, int], bool] = {}
        self._targets: dict[str, RuleTarget] = {}
        self._incomplete: dict[str, bool | None] = {}
        self.shelved: dict[int, list] = defaultdict(list)
        # Nets with a plane or split plane on a layer the stack does not list.
        self.planes_off_stack: set[int] = set()
        self._build_areas()

    # ---------- board level ----------

    def _build_areas(self) -> None:
        pcb, index = self.pcb, self.index
        streams = getattr(pcb, "area_streams", ()) or ()
        self.regions_read = "Regions6" in streams
        self.fills_read = "Fills6" in streams
        poured = set()
        for area in getattr(pcb, "areas", None) or []:
            if area.pour_id is not None:
                poured.add(area.pour_id)
            if area.net is None or area.layer not in self.copper:
                if area.net is None:
                    self.model["areas_without_net"] += 1
                continue
            kind = "pour_copper" if area.pour_id is not None else area.kind
            self.areas_by_net[area.net].append(
                _Area(kind, area.layer, area.net, area.outline, area.holes, area.pour_id)
            )
            self.model[kind] += 1
        hatched = {
            row[0]
            for row in index.db.execute(
                "SELECT DISTINCT poured_by FROM prims WHERE poured_by IS NOT NULL"
            )
        }
        self.model["hatched_pour_primitives"] = index.db.execute(
            "SELECT COUNT(*) FROM prims WHERE poured_by IS NOT NULL"
        ).fetchone()[0]
        split_planes: dict[int, list[_Area]] = defaultdict(list)
        for pour in pcb.pours:
            if pour.shelved and pour.net is not None and pour.layer in self.copper:
                self.shelved[pour.net].append(pour)
            if pour.shelved or pour.net is None or pour.layer is None or len(pour.vertices) < 3:
                continue
            if (getattr(pour, "polygon_type", None) or "").strip().lower() == "split plane":
                if pour.layer not in self.copper:
                    self.model["planes_not_in_stack"] += 1
                    self.planes_off_stack.add(pour.net)
                    continue
                area = _Area(
                    "split_plane", pour.layer, pour.net, pour.vertices,
                    pour_id=pour.pour_id, assumed=True,
                )
                split_planes[pour.layer].append(area)
                self.areas_by_net[pour.net].append(area)
                self.model["split_plane"] += 1
                continue
            if pour.layer not in self.copper or pour.pour_id in poured or pour.pour_id in hatched:
                continue
            if (pour.hatch_style or "").strip().lower() == "solid" and not self.regions_read:
                # Regions6 was not read: the outline stands in for the copper.
                self.areas_by_net[pour.net].append(_Area(
                    "pour_outline", pour.layer, pour.net, pour.vertices,
                    pour_id=pour.pour_id, assumed=True,
                ))
                self.model["pour_outline"] += 1
            else:
                self.unpoured[pour.net].append(pour)
                self.model["unpoured_pours"] += 1
        for layer, net_name in (getattr(pcb.board, "plane_nets", None) or {}).items():
            net_id = index.net_id(net_name)
            if net_id is None:
                continue
            if layer not in self.copper:
                # A legacy stack without the plane: its connections are unknown.
                self.model["planes_not_in_stack"] += 1
                self.planes_off_stack.add(net_id)
                continue
            self.areas_by_net[net_id].append(_Area(
                "plane", layer, net_id, None, assumed=True,
                excluded=[area for area in split_planes.get(layer, []) if area.net_id != net_id],
            ))
            self.model["plane"] += 1

    def layer_name(self, layer: int | None) -> str:
        if layer not in self._layer_names:
            self._layer_names[layer] = self.index.layer_name(layer)
        return self._layer_names[layer]

    def copper_model(self) -> dict:
        model = self.model
        return {
            "regions_read": self.regions_read,
            "fills_read": self.fills_read,
            "pour_regions": model["pour_copper"],
            "hatched_pour_primitives": model["hatched_pour_primitives"],
            "free_regions": model["region"],
            "fills": model["fill"],
            "planes": model["plane"],
            "split_planes": model["split_plane"],
            "planes_not_in_stack": model["planes_not_in_stack"],
            "pour_outlines_assumed": model["pour_outline"],
            "pours_without_copper": model["unpoured_pours"],
            "shelved_pours": sum(len(pours) for pours in self.shelved.values()),
            "area_copper_without_net": model["areas_without_net"],
        }

    def rule_summary(self) -> dict:
        kinds = getattr(self.pcb, "drc_rule_kinds", None)
        out: dict = {}
        for kind, kind_id in (("UnRoutedNet", _UNROUTED_NET), ("NetAntennae", _NET_ANTENNAE)):
            rules = []
            for rule in self.rules[kind]:
                entry = {
                    "name": rule.name, "enabled": rule.enabled,
                    "priority": rule.priority, "scope": rule.scope1,
                }
                if kind == "NetAntennae":
                    entry["tolerance_mil"] = parse_dim_mil(
                        rule.properties.get("NETANTENNAETOLERANCE")
                    )
                else:
                    entry["check_incomplete_connections"] = _incomplete_option(rule)
                rules.append(entry)
            out[kind] = {
                "rules": rules,
                "checked_by_batch_drc": None if kinds is None else kind_id in kinds,
            }
        return out

    def incomplete_connections(self, net: str) -> bool | None:
        """Does the Un-Routed Net rule that applies to a net have 'Check for
        incomplete connections' on? The rule is the first enabled one by
        priority whose scope matches the net; a rule whose scope is not decided
        for the net may come first, so every rule that may counts: True or
        False when they all agree (False when none applies, as Altium then
        does not check the net), else None. Cached per net."""
        if net in self._incomplete:
            return self._incomplete[net]
        rules = [rule for rule in self.rules["UnRoutedNet"] if rule.enabled]
        flags = set()
        if not any(_incomplete_option(rule) for rule in rules):
            rules = []
        for rule in rules:
            verdict = "yes" if rule.scope1.strip().lower() == "all" else evaluate_scope(
                rule.scope1, self._net_target(net), self.index.rule_context
            )["matches"]
            if verdict == "no":
                continue
            flags.add(bool(_incomplete_option(rule)))
            if verdict == "yes":
                break
        else:
            flags.add(False)
        self._incomplete[net] = flags.pop() if len(flags) == 1 else None
        return self._incomplete[net]

    def _net_target(self, net: str) -> RuleTarget:
        if net not in self._targets:
            self._targets[net] = self.index.rule_target(net, kind="net") or RuleTarget(
                kind="net", name=net.lower(), nets={net.lower()}, pairs=set()
            )
        return self._targets[net]

    def _object_rule(self, kind: str, net: str, row):
        """The enabled rule of a kind that applies to an object of a net, by
        priority and scope (first scope only), or None."""
        rules = [rule for rule in self.rules[kind] if rule.enabled]
        if not rules:
            return None
        key = (kind, net)
        resolver = self._resolvers.get(key)
        if resolver is None:
            target = self.index.rule_target(net, kind="net") or RuleTarget(
                kind="net", name=net.lower(), nets={net.lower()}, pairs=set()
            )
            resolver = ObjectResolver(rules, target, self.index.rule_context)
            self._resolvers[key] = resolver
        rule, _ = resolver.resolve(self.index.object_facts(row))
        return rule

    def _antenna_rule(self, net: str, row):
        """(rule, tolerance mil) of the Net Antennae rule for an object."""
        rule = self._object_rule("NetAntennae", net, row)
        if rule is None:
            return None, None
        return rule, parse_dim_mil(rule.properties.get("NETANTENNAETOLERANCE"))

    def _plane_connect_style(self, net: str, row) -> str | None:
        """Lower-case style (relief, direct, noconnect) of the Power Plane
        Connect Style rule for a pad or via: the rule's VIA. or PAD. style when it
        sets one for the object's kind, else its PLANECONNECTSTYLE."""
        rule = self._object_rule("PlaneConnect", net, row)
        if rule is None:
            return None
        properties = {key.upper(): value for key, value in rule.properties.items()}
        prefix = "VIA." if row["kind"] == "via" else "PAD."
        style = properties.get(prefix + "PLANECONNECTSTYLE") or properties.get("PLANECONNECTSTYLE")
        return style.strip().lower().replace(" ", "") if style else None

    def _polygon_connect_style(self, net: str, row, pour_id: int, layer: int) -> str | None:
        """Lower-case style (relief, direct, noconnect) of the Polygon Connect
        Style rule for a pad or via and a pour of its net that meet on a layer:
        the rule's VIA., THPAD. or SMDPAD. style when it sets one per object
        kind, else its CONNECTSTYLE. The rule is the first enabled one by
        priority whose first scope matches the pad or via and second the pour.
        Altium's help does not say whether the scopes also match the other way
        round, and some terms cannot be decided on a pour (how much of it lies
        in a room, a pour without NAME), so every rule that may come first
        counts: the style is known only when they all agree, else None (as
        without a rule)."""
        target = self._net_target(net)
        pour = self.pcb.pours[pour_id]
        sides = (
            replace(target, obj=self.index.object_facts(row)),
            replace(target, obj=ObjectFacts(
                kind="polygon", layer=layer, span=frozenset({layer}),
                points=tuple(pour.vertices),
                polygon=scope_name(pour.name),
            )),
        )
        prefix = "VIA." if row["kind"] == "via" else "THPAD." if row["hole"] else "SMDPAD."

        def matches(scope: str, side: int) -> str:
            verdict = evaluate_scope(scope, sides[side], self.index.rule_context)["matches"]
            return verdict if verdict in ("yes", "no") else "unknown"

        def both(first: str, second: str) -> str:
            if "no" in (first, second):
                return "no"
            return "yes" if first == second == "yes" else "unknown"

        def style(rule) -> str | None:
            properties = {key.upper(): value for key, value in rule.properties.items()}
            value = properties.get(prefix + "CONNECTSTYLE") or properties.get("CONNECTSTYLE")
            return value.strip().lower().replace(" ", "") if value else None

        styles = set()
        for rule in self.rules["PolygonConnect"]:
            if not rule.enabled:
                continue
            forward = both(matches(rule.scope1, 0), matches(rule.scope2, 1))
            backward = both(matches(rule.scope1, 1), matches(rule.scope2, 0))
            if forward == backward == "no":
                continue
            styles.add(style(rule))
            if forward == "yes":
                break
        else:
            styles.add(None)
        return styles.pop() if len(styles) == 1 else None

    def _centre_in_pour(self, row, pour_id: int) -> bool:
        """Is a pad's or via's centre inside the pour's outline (or within
        CENTRE_MIL of it)? True when the pour has no outline."""
        if pour_id not in self._pour_outlines:
            pours = self.pcb.pours
            pour = pours[pour_id] if 0 <= pour_id < len(pours) else None
            self._pour_outlines[pour_id] = (
                _Area("pour_outline", pour.layer, pour.net, pour.vertices)
                if pour is not None and len(pour.vertices) >= 3 else None
            )
        outline = self._pour_outlines[pour_id]
        x, y = row["x1"], row["y1"]
        if outline is None or outline.contains(x, y):
            return True
        return any(
            math.dist((x, y), _closest_on(x, y, *outline.edges[number])) <= CENTRE_MIL
            for number in outline.edges_near(
                x - CENTRE_MIL, y - CENTRE_MIL, x + CENTRE_MIL, y + CENTRE_MIL
            )
        )

    def _pad_meets_pour(self, net: str, row, pour_id: int, layer: int) -> bool:
        """Does a pad or via touching a pour's copper on a layer meet it as
        'Check for incomplete connections' wants? With a direct connection
        (Polygon Connect Style) its centre must be inside the pour's outline;
        one connected by relief or no connect, or whose style is not known, is
        not tested. Cached per pad or via and pour."""
        key = (row["id"], pour_id)
        if key not in self._pour_centres:
            self._pour_centres[key] = (
                self._centre_in_pour(row, pour_id)
                or self._polygon_connect_style(net, row, pour_id, layer) != "direct"
            )
        return self._pour_centres[key]

    def _pour_contact_strong(self, row, layer: int, area, net: str) -> bool:
        """Does an object touching a solid pour's copper (or its outline, when
        that stands in for unread copper) meet it as 'Check for incomplete
        connections' wants? A pad or via as _pad_meets_pour says; a routing
        track must reach the saved copper with its centreline (not tested
        against an outline)."""
        if row["poured_by"] is not None:
            return True
        if row["kind"] in ("pad", "via"):
            return area.pour_id is None or self._pad_meets_pour(
                net, row, area.pour_id, layer
            )
        if area.kind != "pour_copper":
            return True
        segment, _, _, arc = self.index._primitive_geometry(row, layer)
        box = _box(row)
        grown = (box[0] - CENTRE_MIL, box[1] - CENTRE_MIL, box[2] + CENTRE_MIL, box[3] + CENTRE_MIL)
        return area.touches((segment, CENTRE_MIL, None, arc), (row["x1"], row["y1"]), grown)

    # ---------- per net ----------

    def net(self, net_id: int) -> dict:
        if net_id not in self.results:
            self.results[net_id] = self._analyse(net_id)
        return self.results[net_id]

    def _pad_label(self, row) -> str:
        component = row["component"]
        if component is not None and 0 <= component < len(self.pcb.components):
            designator = self.index.designator(component)
            if designator and row["pad_name"]:
                return f"{designator}-{row['pad_name']}"
        return f"free pad at ({row['x1']:.1f}, {row['y1']:.1f})"

    def _describe(self, row, layer: int) -> str:
        kind = row["kind"]
        if kind == "pad":
            return f"Pad {self._pad_label(row)}"
        if kind == "via":
            return "Via"
        name = "Arc" if kind == "arc" else "Track"
        if row["poured_by"] is not None:
            name = f"Pour {name.lower()}"
        return f"{name} on {self.layer_name(layer)}"

    def _strong(self, first, second, layer: int, net: str) -> bool:
        """Do two touching objects also meet as Altium's 'Check for incomplete
        connections' wants (within CENTRE_MIL): track to track by centreline,
        a track's centreline on the pad, via or pour copper it touches, a via's
        centre on the pad or via, a pad or via on a hatched pour's copper as
        _pad_meets_pour says? Pad to pad and pour copper to pour copper are
        not tested."""
        index = self.index

        def core(row):
            if row["kind"] in ("pad", "via"):
                return ((row["x1"], row["y1"], row["x1"], row["y1"]), 0.0, None, None)
            segment, _, _, arc = index._primitive_geometry(row, layer)
            return (segment, 0.0, None, arc)

        def copper(row):
            return index._primitive_geometry(row, layer)

        def meets(a, b) -> bool:
            return PcbIndex._geometry_distance(a, b) <= CENTRE_MIL

        def routing_track(row) -> bool:
            return row["kind"] in ("track", "arc") and row["poured_by"] is None

        poured = [row for row in (first, second) if row["poured_by"] is not None]
        if len(poured) == 2 or (first["kind"] == "pad" and second["kind"] == "pad"):
            return True
        if poured:
            other = second if poured[0] is first else first
            if other["kind"] in ("pad", "via"):
                return self._pad_meets_pour(net, other, poured[0]["poured_by"], layer)
            return not routing_track(other) or meets(core(other), copper(poured[0]))
        a_track, b_track = routing_track(first), routing_track(second)
        if a_track and b_track:
            return meets(core(first), core(second))
        if a_track or b_track:
            track, other = (first, second) if a_track else (second, first)
            return meets(core(track), copper(other))
        if first["kind"] == "via" and second["kind"] == "pad":
            return meets(core(first), copper(second))
        if first["kind"] == "pad" and second["kind"] == "via":
            return meets(core(second), copper(first))
        return meets(core(first), copper(second)) or meets(core(second), copper(first))

    def _analyse(self, net_id: int) -> dict:
        index = self.index
        name = index.pcb.nets[net_id]
        rows = [dict(row) for row in index.db.execute(_ROWS_SQL, (net_id,))]
        for row in rows:
            row["_box"] = (row["minx"], row["miny"], row["maxx"], row["maxy"])
        graph = NetGraph(rows, self.order, self.z)
        labels = graph.islands()
        sets = _UnionFind(max(labels) + 1 if labels else 0)
        element: list[int] = []
        for row in rows:
            pid, kind = row["id"], row["kind"]
            if kind == "pad":
                nodes = graph.pad_nodes[pid]
            elif kind == "via":
                nodes = graph.via_nodes[pid]
            else:
                ends = graph.track_ends.get(pid)
                nodes = {0: ends[0]} if ends else {}
            element.append(labels[next(iter(nodes.values()))] if nodes else sets.add())
        # The graph's own layers: through vias and pads also reach signal layers
        # missing from the stack table.
        row_layers = [
            graph.pads[row["id"]]["layers"] if row["kind"] == "pad"
            else graph.vias[row["id"]]["layers"] if row["kind"] == "via"
            else index._primitive_layers(row)
            for row in rows
        ]
        layer_rows: dict[int, list[int]] = defaultdict(list)
        for number in range(len(rows)):
            for layer in row_layers[number]:
                layer_rows[layer].append(number)
        grids = _grids(rows, row_layers, TOUCH_MIL)
        areas = self.areas_by_net.get(net_id, [])
        area_element = [sets.add() for _ in areas]
        # strict joins only what Altium's 'Check for incomplete connections'
        # accepts: rows are 0..n-1, areas follow.
        strict = _UnionFind(len(rows) + len(areas))

        def join(first: int, second: int) -> None:
            sets.union(element[first], element[second])
            strict.union(first, second)

        # A Jumper component's pads on one net are joined by its jumper wire.
        jumper_pads: dict[int, int] = {}
        for number, row in enumerate(rows):
            component = row["component"]
            if (
                row["kind"] == "pad" and component is not None
                and 0 <= component < len(self.pcb.components)
                and getattr(self.pcb.components[component], "kind", 0) == _JUMPER
            ):
                join(jumper_pads.setdefault(component, number), number)

        # Every pair of touching rows the strict joins have not tied yet: the
        # graph may hold them already, but only touching that Altium's option
        # accepts joins strictly; the rest wait as weak.
        weak_rows: list[tuple[int, int, int]] = []
        for layer, grid in grids.items():
            seen: set[tuple[int, int]] = set()
            for members in grid.values():
                if len(members) < 2:
                    continue
                roots = [strict.find(number) for number in members]
                if len(set(roots)) < 2:
                    continue
                count = len(members)
                for first_at in range(count - 1):
                    first = members[first_at]
                    for second_at in range(first_at + 1, count):
                        second = members[second_at]
                        if roots[first_at] == roots[second_at] or strict.find(
                            first
                        ) == strict.find(second):
                            continue
                        key = (first, second) if first < second else (second, first)
                        if key in seen:
                            continue
                        seen.add(key)
                        row_a, row_b = rows[first], rows[second]
                        if not _boxes_within(_box(row_a), _box(row_b), TOUCH_MIL):
                            continue
                        if index._primitive_distance(row_a, row_b, [layer]) > TOUCH_MIL:
                            continue
                        if self._strong(row_a, row_b, layer, name):
                            join(first, second)
                        else:
                            sets.union(element[first], element[second])
                            weak_rows.append((first, second, layer))

        area_rows: dict[int, list[int]] = defaultdict(list)
        joins = Counter()
        weak_areas: list[tuple[int, int]] = []
        for number, area in enumerate(areas):
            for position in layer_rows.get(area.layer, ()):
                row = rows[position]
                box = _box(row)
                if area.bbox is not None and not _boxes_within(box, area.bbox, TOUCH_MIL):
                    continue
                if area.kind in ("plane", "split_plane") and row["kind"] in ("pad", "via") and (
                    self._plane_connect_style(name, row) == "noconnect"
                ):
                    continue
                if not area.touches(
                    index._primitive_geometry(row, area.layer), (row["x1"], row["y1"]), box
                ):
                    continue
                area_rows[number].append(position)
                if sets.union(area_element[number], element[position]):
                    joins[number] += 1
                if area.kind in ("pour_copper", "pour_outline") and not self._pour_contact_strong(
                    row, area.layer, area, name
                ):
                    weak_areas.append((position, number))
                else:
                    strict.union(len(rows) + number, position)
        by_layer: dict[int, list[int]] = defaultdict(list)
        for number, area in enumerate(areas):
            by_layer[area.layer].append(number)
        for numbers in by_layer.values():
            for at, first in enumerate(numbers):
                for second in numbers[at + 1 :]:
                    if strict.find(len(rows) + first) == strict.find(len(rows) + second):
                        continue
                    box_a, box_b = areas[first].bbox, areas[second].bbox
                    if box_a is not None and box_b is not None and not _boxes_within(
                        box_a, box_b, TOUCH_MIL
                    ):
                        continue
                    if _areas_touch(areas[first], areas[second]):
                        strict.union(len(rows) + first, len(rows) + second)
                        if sets.union(area_element[first], area_element[second]):
                            joins[first] += 1
                            joins[second] += 1
        strict_pad_islands = len({
            strict.find(number) for number, row in enumerate(rows) if row["kind"] == "pad"
        })
        # The weak joins a strict reading needs: those that still tie two
        # strictly joined parts.
        weak = [
            (first, second, layer)
            for first, second, layer in weak_rows
            if strict.union(first, second)
        ]
        weak_pours = [
            (position, number)
            for position, number in weak_areas
            if strict.union(len(rows) + number, position)
        ]

        # Islands.
        comps: dict[int, dict] = {}

        def comp_of(root: int) -> dict:
            if root not in comps:
                comps[root] = {"rows": [], "pads": [], "areas": []}
            return comps[root]

        for number, row in enumerate(rows):
            comp = comp_of(sets.find(element[number]))
            comp["rows"].append(number)
            if row["kind"] == "pad":
                comp["pads"].append(number)
        for number, area in enumerate(areas):
            if area.whole_layer and not area_rows.get(number):
                continue  # a plane nothing reaches is not an island of this net
            comp_of(sets.find(area_element[number]))["areas"].append(number)
        pad_comps = [root for root, comp in comps.items() if comp["pads"]]
        pad_count = sum(len(comps[root]["pads"]) for root in pad_comps)

        result: dict = {
            "net": name,
            "net_id": net_id,
            "pads": pad_count,
            "pad_islands": len(pad_comps),
            "strict_pad_islands": strict_pad_islands,
            "missing": [],
            "isolated": [],
            "antennae": [],
            "weak": [],
            "assumed": sorted({
                self._area_label(areas[number])
                for number, count in joins.items()
                if areas[number].assumed and count >= 2
            }),
            "pours_without_copper": [
                self.layer_name(pour.layer) for pour in self.unpoured.get(net_id, [])
            ],
            "shelved_pours": [
                self.layer_name(pour.layer) for pour in self.shelved.get(net_id, [])
            ],
        }
        if pad_count >= 2:
            if len(pad_comps) == 1:
                result["status"] = "routed"
            elif len(pad_comps) == pad_count:
                result["status"] = "unrouted"
            else:
                result["status"] = "partly_routed"
        else:
            result["status"] = "single_pad" if pad_count == 1 else "no_pads"

        near = (
            self._near_gaps(
                rows, _grids(rows, row_layers, NEAR_MIL / 2.0), sets, element,
                areas, area_element, layer_rows,
            )
            if len(comps) > 1 else {}
        )
        result["near_misses"] = []
        if len(pad_comps) > 1:
            result["missing"] = self._missing(rows, comps, pad_comps, sets, element, near)
            result["near_misses"] = self._near_misses(rows, comps, pad_comps, near)
        for root, comp in comps.items():
            if not comp["pads"]:
                result["isolated"].append(
                    self._island(rows, areas, comp, root, row_layers, near, pad_comps)
                )
        if rows:
            result["antennae"] = self._antennae(
                name, rows, graph, areas, area_rows, grids, sets, element, pad_comps
            )
        for first, second, layer in weak:
            row_a, row_b = rows[first], rows[second]
            x, y = _meeting_point(row_a, row_b)
            result["weak"].append({
                "layer": layer,
                "objects": [self._describe(row_a, layer), self._describe(row_b, layer)],
                "x": x, "y": y,
            })
        for position, number in weak_pours:
            row, area = rows[position], areas[number]
            found = self._edge_nearest(row, area.layer, area, _reach(row) + TOUCH_MIL)
            x, y = found[1] if found is not None else (row["x1"], row["y1"])
            result["weak"].append({
                "layer": area.layer,
                "objects": [
                    self._describe(row, area.layer),
                    ("Pour outline" if area.kind == "pour_outline" else "Pour copper")
                    + f" on {self.layer_name(area.layer)}",
                ],
                "x": x, "y": y,
            })
        return result

    @staticmethod
    def _area_label(area: _Area) -> str:
        return {
            "plane": "internal plane",
            "split_plane": "split plane (outline)",
            "pour_outline": "pour outline (Regions6 not read)",
        }.get(area.kind, area.kind)

    def _edge_nearest(self, row, layer: int, area, reach: float):
        """(distance between the copper, point on the area's edge) for the area
        edge nearest a row on a layer, among edges within reach; None if none."""
        box = _box(row)
        geometry = self.index._primitive_geometry(row, layer)
        best = None
        for number in area.edges_near(
            box[0] - reach, box[1] - reach, box[2] + reach, box[3] + reach
        ):
            edge = area.edges[number]
            distance = PcbIndex._geometry_distance(geometry, (edge, 0.0, None, None))
            if best is None or distance < best[0]:
                best = (distance, _closest_pair(_core(row), edge)[2])
        return best

    def _near_gaps(self, rows, grids, sets, element, areas, area_element, layer_rows) -> dict:
        """Closest approach (distance, layer, x, y) of the copper of every two
        islands that come within NEAR_MIL: rows against rows and against area
        copper (areas against areas are not measured). {(root a, root b): ...}."""
        index = self.index
        near: dict[tuple[int, int], tuple] = {}
        for number, area in enumerate(areas):
            if area.whole_layer:
                continue
            root_a = sets.find(area_element[number])
            for position in layer_rows.get(area.layer, ()):
                root_b = sets.find(element[position])
                if root_a == root_b:
                    continue
                pair = (root_a, root_b) if root_a < root_b else (root_b, root_a)
                best = near.get(pair, (NEAR_MIL,))[0]
                row = rows[position]
                if _box_gap(_box(row), area.bbox) > best:
                    continue
                found = self._edge_nearest(row, area.layer, area, best)
                if found is not None and found[0] <= best:
                    distance, on_edge = found
                    near[pair] = (
                        distance, area.layer,
                        _toward(on_edge, (row["x1"], row["y1"]), distance / 2.0),
                    )
        for layer, grid in grids.items():
            seen: set[tuple[int, int]] = set()
            for members in grid.values():
                roots = [sets.find(element[number]) for number in members]
                if len(set(roots)) < 2:
                    continue
                for first_at in range(len(members) - 1):
                    first, root_a = members[first_at], roots[first_at]
                    for second_at in range(first_at + 1, len(members)):
                        second, root_b = members[second_at], roots[second_at]
                        if root_a == root_b:
                            continue
                        key = (first, second) if first < second else (second, first)
                        if key in seen:
                            continue
                        seen.add(key)
                        pair = (root_a, root_b) if root_a < root_b else (root_b, root_a)
                        best = near.get(pair, (NEAR_MIL,))[0]
                        if _box_gap(_box(rows[first]), _box(rows[second])) > best:
                            continue
                        distance = index._primitive_distance(rows[first], rows[second], [layer])
                        if distance <= best:
                            near[pair] = (distance, layer, (first, second))
        # Area entries carry their point, row pairs are located once here.
        return {
            pair: (
                distance, layer,
                *(where if isinstance(where[0], float) else _meeting_point(
                    rows[where[0]], rows[where[1]], distance
                )),
            )
            for pair, (distance, layer, where) in near.items()
        }

    def _missing(self, rows, comps, pad_comps, sets, element, near) -> list[dict]:
        points, groups, pad_rows = [], [], []
        for root in pad_comps:
            for number in comps[root]["pads"]:
                points.append((rows[number]["x1"], rows[number]["y1"]))
                groups.append(root)
                pad_rows.append(number)
        missing = []
        for first, second, distance in _spanning_edges(points, groups):
            row_a, row_b = rows[pad_rows[first]], rows[pad_rows[second]]
            if self._pad_label(row_b) < self._pad_label(row_a):
                first, second, row_a, row_b = second, first, row_b, row_a
            entry = {
                "from": self._pad_label(row_a),
                "to": self._pad_label(row_b),
                "distance_mil": _r(distance),
                **(
                    {"same_component": True}
                    if row_a["component"] is not None
                    and row_a["component"] == row_b["component"] else {}
                ),
                "from_layer": self.layer_name(row_a["layer"]),
                "to_layer": self.layer_name(row_b["layer"]),
                "layers": sorted({row_a["layer"], row_b["layer"]}),
            }
            root_a, root_b = groups[first], groups[second]
            gap = near.get((root_a, root_b) if root_a < root_b else (root_b, root_a))
            if gap is not None:
                entry["copper_gap_mil"] = _r(gap[0], 3)
                entry["copper_gap_layer"] = self.layer_name(gap[1])
                entry["copper_gap_at_mil"] = {"x_mil": _r(gap[2], 3), "y_mil": _r(gap[3], 3)}
            missing.append(entry)
        missing.sort(key=lambda entry: entry["distance_mil"])
        return missing

    def _near_misses(self, rows, comps, pad_comps, near) -> list[dict]:
        """Island pairs holding pads whose copper comes within NEAR_MIL, closest
        first, each named by one pad per island."""
        pad_roots = set(pad_comps)
        label = {
            root: min(self._pad_label(rows[number]) for number in comps[root]["pads"])
            for root in pad_comps
        }
        misses = []
        for (root_a, root_b), (distance, layer, x, y) in sorted(
            near.items(), key=lambda item: item[1][0]
        ):
            if root_a in pad_roots and root_b in pad_roots:
                misses.append({
                    "islands_of": sorted((label[root_a], label[root_b])),
                    "copper_gap_mil": _r(distance, 3),
                    "layer": self.layer_name(layer),
                    "at_mil": {"x_mil": _r(x, 3), "y_mil": _r(y, 3)},
                })
        return misses

    def _island(self, rows, areas, comp, root, row_layers, near, pad_comps) -> dict:
        kinds = Counter()
        arcs = set()
        routed = 0.0
        layers: set[int] = set()
        boxes = []
        for number in comp["rows"]:
            row = rows[number]
            if row["kind"] in ("track", "arc"):
                key = "pour_primitives" if row["poured_by"] is not None else row["kind"] + "s"
                if row["kind"] == "arc" and row["poured_by"] is None:
                    arcs.add(row["source_id"] if row["source_id"] is not None else row["id"])
                else:
                    kinds[key] += 1
                routed += row["length"] if row["length"] is not None else math.hypot(
                    row["x2"] - row["x1"], row["y2"] - row["y1"]
                )
            else:
                kinds[row["kind"] + "s"] += 1
            layers.update(row_layers[number])
            boxes.append(_box(row))
        if arcs:
            kinds["arcs"] = len(arcs)
        for number in comp["areas"]:
            area = areas[number]
            kinds[{"pour_copper": "pour_regions", "region": "regions", "fill": "fills"}.get(
                area.kind, area.kind + "s"
            )] += 1
            layers.add(area.layer)
            if area.bbox is not None:
                boxes.append(area.bbox)
        island: dict = {
            "objects": dict(sorted(kinds.items())),
            "routed_mil": _r(routed),
            "layers": sorted(layers),
            "pour_only": not any(
                rows[number]["poured_by"] is None for number in comp["rows"]
            ) and all(areas[number].kind == "pour_copper" for number in comp["areas"]),
        }
        if boxes:
            island["bbox"] = (
                min(box[0] for box in boxes), min(box[1] for box in boxes),
                max(box[2] for box in boxes), max(box[3] for box in boxes),
            )
        best = None
        for (root_a, root_b), gap in near.items():
            if root in (root_a, root_b) and (best is None or gap[0] < best[0]):
                other = root_b if root == root_a else root_a
                best = gap + (other in pad_comps,)
        if best is not None:
            island["nearest_copper"] = best
        return island

    def _antennae(
        self, net, rows, graph, areas, area_rows, grids, sets, element, pad_comps
    ) -> list[dict]:
        """Open-ended tracks and tracks ending on a via that connects nothing
        else, once per track or arc; those in copper that reaches no pad are
        marked in_isolated_copper."""
        index = self.index
        pad_roots = set(pad_comps)
        reported: dict[int, dict] = {}
        position_of = {row["id"]: number for number, row in enumerate(rows)}
        on_area = {number for numbers in area_rows.values() for number in numbers}
        areas_on: dict[int, list[_Area]] = defaultdict(list)
        for area in areas:
            areas_on[area.layer].append(area)
        via_nodes = {node for nodes in graph.via_nodes.values() for node in nodes.values()}
        pad_nodes = {node for nodes in graph.pad_nodes.values() for node in nodes.values()}
        arc_lengths: dict[int, float] = defaultdict(float)
        for row in rows:
            if row["kind"] == "arc" and row["source_id"] is not None:
                arc_lengths[row["source_id"]] += row["length"] or 0.0

        def primitive_length(row) -> float:
            """Length of the track, or of the whole arc an arc chord belongs to."""
            if row["kind"] == "arc" and row["source_id"] in arc_lengths:
                return arc_lengths[row["source_id"]]
            if row["length"] is not None:
                return row["length"]
            return math.hypot(row["x2"] - row["x1"], row["y2"] - row["y1"])

        def same_object(row, other) -> bool:
            return other["id"] == row["id"] or (
                row["kind"] == "arc" and other["kind"] == "arc"
                and row["source_id"] is not None and other["source_id"] == row["source_id"]
            )

        def touches_other(geometry, ref, box, layer, skip) -> bool:
            """Does copper (geometry, a point ref of it, bounds box) touch an
            area or a row other than those skip names?"""
            for area in areas_on.get(layer, ()):
                if area.touches(geometry, ref, box):
                    return True
            grid = grids.get(layer, {})
            candidates = set()
            for gx in _grid_range(box[0] - TOUCH_MIL, box[2] + TOUCH_MIL):
                for gy in _grid_range(box[1] - TOUCH_MIL, box[3] + TOUCH_MIL):
                    candidates.update(grid.get((gx, gy), ()))
            for number in candidates:
                other = rows[number]
                if skip(other) or not _boxes_within(box, _box(other), TOUCH_MIL):
                    continue
                if PcbIndex._geometry_distance(
                    geometry, index._primitive_geometry(other, layer)
                ) <= TOUCH_MIL:
                    return True
            return False

        def in_area(node: int) -> bool:
            x, y, layer = graph.pos[node]
            return any(area.contains(x, y) for area in areas_on.get(layer, ()))

        def stub(start: int, ignored: frozenset = frozenset()) -> float:
            """Track length from an open end (start) back to the first branch,
            pad, via or area copper; ignored edges (the attachments of the via a
            track ends on) count as absent."""
            length, node, previous, steps = 0.0, start, None, 0
            while steps < _WALK_LIMIT:
                steps += 1
                edges = [item for item in graph.adj[node] if item[2] not in ignored]
                if node != start and (
                    len(edges) != 2 or node in pad_nodes
                    or node in via_nodes or in_area(node)
                ):
                    break
                onward = [item for item in edges if item[2] != previous]
                if len(onward) != 1:
                    break
                neighbour, weight, edge = onward[0]
                if graph.edges[edge][0] != "seg":
                    break
                length += weight
                node, previous = neighbour, edge
            return length

        found = []
        for segment in graph.segments:
            pid = segment[8]
            number = position_of[pid]
            row = rows[number]
            if row["poured_by"] is not None:
                continue
            isolated = sets.find(element[number]) not in pad_roots
            ends = graph.track_ends[pid]
            if ends[0] == ends[1]:
                continue
            for side, node in enumerate(ends):
                if len(graph.adj[node]) != 1:
                    continue
                x, y = (segment[0], segment[1]) if side == 0 else (segment[2], segment[3])
                half, layer = segment[5], segment[4]
                cap = ((x, y, x, y), half, None, None)
                box = (x - half, y - half, x + half, y + half)
                if touches_other(
                    cap, (x, y), box, layer, lambda other, row=row: same_object(row, other)
                ):
                    continue
                key = (
                    row["source_id"] if row["kind"] == "arc" and row["source_id"] is not None
                    else -1 - row["id"]
                )
                if key in reported:
                    reported[key]["open_ends"] = reported[key].get("open_ends", 1) + 1
                    continue
                entry = self._antenna_entry(
                    net, row, layer, x, y, "nothing", stub(node), primitive_length(row),
                )
                if isolated:
                    entry["in_isolated_copper"] = True
                reported[key] = entry
                found.append(entry)

        for number, row in enumerate(rows):
            if row["kind"] != "via" or number in on_area:
                continue
            isolated = sets.find(element[number]) not in pad_roots
            nodes = graph.via_nodes[row["id"]]
            attached = [
                (neighbour, edge)
                for node in nodes.values()
                for neighbour, _, edge in graph.adj[node]
                if graph.edges[edge][0] != "barrel"
            ]
            if not attached or any(
                neighbour in pad_nodes or neighbour in via_nodes for neighbour, _ in attached
            ):
                continue
            # The tracks and arcs the via touches: one primitive (an arc's
            # chords count once) that ends on it, or no stub.
            ignored = frozenset(edge for _, edge in attached)
            touched = {}
            for neighbour, _ in attached:
                for _, _, item in graph.adj[neighbour]:
                    owner = graph.edges[item][3]
                    if graph.edges[item][0] == "seg" and owner:
                        track_row = rows[position_of[owner[1]]]
                        key = (
                            track_row["source_id"]
                            if track_row["kind"] == "arc" and track_row["source_id"] is not None
                            else -1 - track_row["id"]
                        )
                        touched[key] = track_row
            if len(touched) != 1:
                continue
            ends = [
                neighbour for neighbour, _ in attached
                if len([item for item in graph.adj[neighbour] if item[2] not in ignored]) == 1
            ]
            if not ends:
                continue  # the via sits on the track's body or a junction
            (track,) = touched.values()
            touching = False
            for layer in index._primitive_layers(row):
                geometry = index._primitive_geometry(row, layer)
                if touches_other(
                    geometry, (row["x1"], row["y1"]), _box(row), layer,
                    lambda other, row=row, track=track: other["id"] == row["id"]
                    or same_object(track, other),
                ):
                    touching = True
                    break
            if touching:
                continue
            key = (
                track["source_id"] if track["kind"] == "arc" and track["source_id"] is not None
                else -1 - track["id"]
            )
            if key in reported:
                reported[key]["open_ends"] = reported[key].get("open_ends", 1) + 1
                continue
            entry = self._antenna_entry(
                net, track, graph.pos[ends[0]][2], row["x1"], row["y1"], "via",
                stub(ends[0], ignored), primitive_length(track),
            )
            reported[key] = entry
            if isolated:
                entry["in_isolated_copper"] = True
            found.append(entry)
        return found

    def _antenna_entry(self, net, track, layer, x, y, ends_in, stub, open_track) -> dict:
        """An antenna: track is the open-ended track or arc (it decides the
        Net Antennae rule), stub the copper back to the first branch."""
        rule, tolerance = self._antenna_rule(net, track)
        return {
            "layer": layer,
            "ends_in": ends_in,
            "x": x, "y": y,
            "stub_mil": stub,
            "open_track_mil": open_track,
            "rule": rule.name if rule is not None else None,
            "tolerance_mil": tolerance,
            # Altium's tolerance is on 'the stub of an open-ended track/arc
            # primitive': the open track itself is compared. Without an enabled
            # rule Altium checks nothing: None.
            "over_tolerance": None if tolerance is None else open_track > tolerance + 1e-6,
            "stub_over_tolerance": None if tolerance is None else stub > tolerance + 1e-6,
        }


def connectivity(index) -> Connectivity:
    """The board's Connectivity, cached on the index."""
    cached = index.__dict__.get("_connectivity")
    if cached is None:
        cached = Connectivity(index)
        index.__dict__["_connectivity"] = cached
    return cached


# ---------- the check ----------


def select_nets(index, nets: list[str] | None, net_classes: list[str] | None):
    """(net ids, unknown names, unknown classes): nets named (also differential
    pairs, whose two nets count) and members of net classes; every net without
    either."""
    ctx = index.rule_context
    if not nets and not net_classes:
        return [net_id for net_id, name in enumerate(index.pcb.nets) if name], [], []
    names: set[str] = set()
    unknown, unknown_classes = [], []
    for name in nets or []:
        key = name.lower()
        if key in ctx.net_names:
            names.add(key)
        elif key in ctx.pairs:
            names.update(net.lower() for net in ctx.pairs[key][1:])
        else:
            unknown.append(name)
    for name in net_classes or []:
        key = name.lower()
        if key in ctx.superclasses.get("net", set()):
            names.update(net.lower() for net in index.pcb.nets if net)
        elif key in ctx.net_classes:
            names.update(ctx.net_classes[key])
        else:
            unknown_classes.append(name)
    ids = sorted(
        net_id for net_id in (index.net_id(ctx.net_names.get(key, key)) for key in names)
        if net_id is not None
    )
    return ids, unknown, unknown_classes


def _location(x: float, y: float) -> dict:
    return {"x_mil": _r(x, 3), "y_mil": _r(y, 3)}


def _findings(board: Connectivity, result: dict, checks: set, layer: int | None) -> list[dict]:
    """The findings of one net, narrowed to a layer when given."""
    net = result["net"]
    out = []

    def on_layer(layers) -> bool:
        return layer is None or layer in layers

    if "unrouted" in checks and result["missing"]:
        missing = [
            entry for entry in result["missing"]
            if layer is None or layer in entry["layers"] or 74 in entry["layers"]
        ]
        if missing:
            finding = {
                "check": "unrouted",
                "severity": "warning",
                "net": net,
                "status": result["status"],
                "pads": result["pads"],
                "pad_islands": result["pad_islands"],
                "missing_connections": len(result["missing"]),
                "connections": [
                    {key: value for key, value in entry.items() if key != "layers"}
                    for entry in missing
                ],
            }
            if result["near_misses"]:
                finding["near_misses"] = result["near_misses"][:NEAR_MISSES_LISTED]
                if len(result["near_misses"]) > NEAR_MISSES_LISTED:
                    finding["near_misses_not_listed"] = (
                        len(result["near_misses"]) - NEAR_MISSES_LISTED
                    )
            hints = []
            if any(entry.get("same_component") for entry in missing):
                hints.append(
                    "Pads of one part: Altium joins them only through copper or a pad "
                    "Jumper ID, which is not read here."
                )
            if result["pours_without_copper"]:
                finding["pours_without_copper"] = dict(Counter(result["pours_without_copper"]))
                hints.append(
                    "The net has pours with no saved copper (never poured, or poured to "
                    "nothing): they connect nothing as saved."
                )
            if result["net_id"] in board.planes_off_stack:
                hints.append(
                    "The net has an internal plane on a layer the board's layer stack "
                    "does not list: its plane connections are not checked."
                )
            if result["shelved_pours"]:
                finding["shelved_pours"] = dict(Counter(result["shelved_pours"]))
                hints.append("The net has shelved pours: they connect nothing until restored.")
            if hints:
                finding["hint"] = " ".join(hints)
            out.append(finding)
    if "isolated" in checks:
        for island in result["isolated"]:
            if not on_layer(island["layers"]):
                continue
            finding = {
                "check": "isolated_copper",
                "severity": "info" if island["pour_only"] else "warning",
                "net": net,
                "objects": island["objects"],
                "routed_mil": island["routed_mil"],
                "layers": [board.layer_name(item) for item in island["layers"]],
            }
            if result["pads"] == 0:
                finding["net_has_pads"] = False
            if "bbox" in island:
                minx, miny, maxx, maxy = island["bbox"]
                finding["location_mil"] = _location((minx + maxx) / 2.0, (miny + maxy) / 2.0)
                finding["bbox_mil"] = {
                    "minx_mil": _r(minx, 3), "miny_mil": _r(miny, 3),
                    "maxx_mil": _r(maxx, 3), "maxy_mil": _r(maxy, 3),
                }
            if "nearest_copper" in island:
                distance, gap_layer, x, y, to_pads = island["nearest_copper"]
                finding["nearest_copper_mil"] = _r(distance, 3)
                finding["nearest_copper_layer"] = board.layer_name(gap_layer)
                finding["nearest_copper_reaches_pads"] = to_pads
                finding["nearest_copper_at_mil"] = _location(x, y)
            out.append(finding)
    if "antennae" in checks:
        for antenna in result["antennae"]:
            if not on_layer([antenna["layer"]]):
                continue
            finding = {
                "check": "antenna",
                "severity": "warning" if antenna["over_tolerance"] else "info",
                "over_tolerance": antenna["over_tolerance"],
                "net": net,
                "ends_in": antenna["ends_in"],
                "layer": board.layer_name(antenna["layer"]),
                "location_mil": _location(antenna["x"], antenna["y"]),
                "stub_mil": _r(antenna["stub_mil"]),
                "open_track_mil": _r(antenna["open_track_mil"]),
                "rule": antenna["rule"],
                "tolerance_mil": antenna["tolerance_mil"],
                "stub_over_tolerance": antenna["stub_over_tolerance"],
            }
            if antenna.get("open_ends", 1) > 1:
                finding["open_ends"] = antenna["open_ends"]
            if antenna.get("in_isolated_copper"):
                finding["in_isolated_copper"] = True
            out.append(finding)
    if "weak" in checks:
        checked = board.incomplete_connections(net)
        for weak in result["weak"]:
            if not on_layer([weak["layer"]]):
                continue
            out.append({
                "check": "weak_connection",
                "severity": "warning" if checked else "info",
                "net": net,
                "layer": board.layer_name(weak["layer"]),
                "objects": weak["objects"],
                "location_mil": _location(weak["x"], weak["y"]),
                "incomplete_connections_checked": checked,
            })
    return out


def check_connectivity(
    index,
    nets: list[str] | None = None,
    net_classes: list[str] | None = None,
    layer: str | None = None,
    checks: list[str] | None = None,
) -> dict:
    """The whole check, unpaged (page_result pages it), or an error."""
    selected = (
        list(dict.fromkeys(_CHECK_ALIASES.get(check, check) for check in checks))
        if checks else list(DEFAULT_CHECKS)
    )
    invalid = [check for check in selected if check not in CHECKS]
    if invalid:
        return {
            "error": "invalid_checks",
            "message": f"Unknown checks: {', '.join(invalid)}.",
            "valid_checks": list(CHECKS),
        }
    net_ids, unknown, unknown_classes = select_nets(index, nets, net_classes)
    if unknown:
        return {
            "error": "net_not_found",
            "message": "No PCB net or differential pair named "
            + ", ".join(f"'{name}'" for name in unknown) + ".",
        }
    if unknown_classes:
        return {
            "error": "net_class_not_found",
            "message": "No net class named "
            + ", ".join(f"'{name}'" for name in unknown_classes) + ".",
            "net_classes": sorted(
                {item.name for item in index.pcb.classes if item.kind == "net"},
                key=str.lower,
            )[:50],
        }
    layer_id = None
    if layer:
        layer_id = index.resolve_layer(layer)
        if layer_id is None:
            return {
                "error": "layer_not_found",
                "message": f"No copper layer named '{layer}'.",
                "layers": [
                    index.layer_name(item) for item in index._copper_layers_in_stack_order()
                ],
            }
    board = connectivity(index)
    # 'Check for incomplete connections' per net; where it is (or may be) on,
    # weak joins are listed unless checks says otherwise.
    options = {net_id: board.incomplete_connections(index.pcb.nets[net_id]) for net_id in net_ids}
    if not checks and any(option is not False for option in options.values()):
        selected.append("weak")
    statuses = Counter()
    connections_total = connections_routed = 0
    antennae = over = no_rule = in_isolated = isolated = weak = strict_nets = 0
    assumed: list[dict] = []
    not_routed: list[str] = []
    # Nets routed here that Altium reads un-routed: joined only by weak
    # connections, with the option on (or, maybe_unrouted, possibly on); and
    # the connections Altium then misses on top of ours.
    weak_not_routed: list[str] = []
    maybe_unrouted = weak_missing = 0
    findings: list[dict] = []
    per_net: list[dict] = []
    for net_id in net_ids:
        result = board.net(net_id)
        statuses[result["status"]] += 1
        if result["pads"] >= 2:
            connections_total += result["pads"] - 1
            connections_routed += result["pads"] - result["pad_islands"]
        antennae += len(result["antennae"])
        over += sum(1 for item in result["antennae"] if item["over_tolerance"])
        no_rule += sum(1 for item in result["antennae"] if item["over_tolerance"] is None)
        in_isolated += sum(1 for item in result["antennae"] if item.get("in_isolated_copper"))
        isolated += len(result["isolated"])
        weak += len(result["weak"])
        if result["status"] in ("partly_routed", "unrouted"):
            not_routed.append(result["net"])
        if result["assumed"]:
            assumed.append({"net": result["net"], "through": result["assumed"]})
        findings.extend(_findings(board, result, set(selected), layer_id))
        weak_only = False
        if result["pads"] >= 2 and result["strict_pad_islands"] > result["pad_islands"]:
            strict_nets += 1
            if options[net_id]:
                weak_missing += result["strict_pad_islands"] - result["pad_islands"]
            if result["status"] == "routed":
                if options[net_id]:
                    weak_not_routed.append(result["net"])
                    weak_only = True
                elif options[net_id] is None:
                    maybe_unrouted += 1
        if nets or net_classes:
            entry = {
                "net": result["net"],
                "status": result["status"],
                "pads": result["pads"],
                "pad_islands": result["pad_islands"],
            }
            if weak_only:
                entry["unrouted_in_altium"] = True
            if result["assumed"]:
                entry["connected_through_assumed_copper"] = result["assumed"]
            per_net.append(entry)
    routable = statuses["routed"] + statuses["partly_routed"] + statuses["unrouted"]
    states = set(options.values())
    checked = states.pop() if len(states) == 1 else None if states else False
    summary = {
        "nets_checked": len(net_ids),
        "nets_with_2_or_more_pads": routable,
        "routed": statuses["routed"],
        "partly_routed": statuses["partly_routed"],
        "unrouted": statuses["unrouted"],
        "routed_pct": _pct(statuses["routed"], routable),
        "partly_routed_pct": _pct(statuses["partly_routed"], routable),
        "unrouted_pct": _pct(statuses["unrouted"], routable),
        "single_pad_nets": statuses["single_pad"],
        "nets_without_pads": statuses["no_pads"],
        "connections": {
            "total": connections_total,
            "routed": connections_routed,
            "missing": connections_total - connections_routed,
            "completion_pct": _pct(connections_routed, connections_total),
        },
        "isolated_copper_islands": isolated,
        "antennae": antennae,
        "antennae_over_tolerance": over,
        "antennae_without_enabled_rule": no_rule,
        "antennae_in_isolated_copper": in_isolated,
        "weak_connections": weak,
        "nets_joined_only_by_weak_connections": strict_nets,
        "incomplete_connections_checked": checked,
        "nets_connected_through_assumed_copper": len(assumed),
    }
    if checked is not False:
        count = len(weak_not_routed)
        summary["nets_routed_here_unrouted_in_altium"] = count
        summary["incomplete_connections_note"] = (
            (
                "The Un-Routed Net rule has 'Check for incomplete connections' on"
                if checked else
                "'Check for incomplete connections' is on in an Un-Routed Net rule that "
                "applies to some of the nets checked, or may apply "
                "(incomplete_connections_checked on each weak_connection, null where a "
                "rule's scope is not decided for the net). Where it is on"
            )
            + ": Altium does not count weak connections as connected, so "
            + f"{count} net{'' if count == 1 else 's'} routed here only through them "
            + f"read{'s' if count == 1 else ''} un-routed in Altium (nets_unrouted_in_altium)"
            + (
                f", and {maybe_unrouted} more may, where the rule is not decided"
                if maybe_unrouted else ""
            )
            + (
                ". The weak_connection findings show where (warnings where it is on)."
                if "weak" in selected else ". Name weak in checks to list the weak connections."
            )
        )
    findings.sort(key=lambda finding: (
        _SEVERITY_ORDER[finding["severity"]],
        _CHECK_ORDER[finding["check"]],
        -finding.get("missing_connections", 0),
        finding["net"].lower(),
    ))
    result: dict = {
        "checks_run": selected,
        "summary": summary,
        "copper_model": board.copper_model(),
        "rules": board.rule_summary(),
    }
    if layer_id is not None:
        result["layer"] = board.layer_name(layer_id)
        result["layer_note"] = (
            "The summary covers every selected net on all layers; findings are those "
            "with copper (or, for missing connections, a pad) on the layer."
        )
    if per_net:
        result["nets"] = per_net
    if checked is not False:
        result["nets_unrouted_in_altium"] = sorted(weak_not_routed, key=str.lower)
    if assumed:
        result["assumed_copper_nets"] = assumed
    result["severity_counts"] = dict(Counter(finding["severity"] for finding in findings))
    result["findings_by_check"] = dict(Counter(finding["check"] for finding in findings))
    result["findings"] = findings
    # For drc_comparison: the nets checked (None: the whole board) and those
    # not routed, whatever the checks and layer filters show.
    result["_checked_nets"] = (
        {board.pcb.nets[net_id].lower() for net_id in net_ids}
        if nets or net_classes else None
    )
    result["_not_routed_nets"] = not_routed
    result["_weak_not_routed_nets"] = weak_not_routed
    result["_weak_missing_connections"] = weak_missing
    result["approximations"] = list(APPROXIMATIONS)
    if not board.regions_read and board.model["pour_outline"]:
        result["approximations"].insert(0, (
            "The file's Regions6 stream (solid pour copper) was not read: solid pours "
            "connect what their outline covers on their layer (assumed)."
        ))
    return result


def page_result(
    full: dict,
    offset: int = 0,
    limit: int = 100,
    net_limit: int = 200,
    connection_limit: int = CONNECTIONS_LISTED,
) -> dict:
    """A page of check_connectivity's result: findings from offset, each
    unrouted finding with up to connection_limit connections, and up to
    net_limit entries of the per-net lists."""
    result = {key: value for key, value in full.items() if not key.startswith("_")}
    offset = max(0, offset)
    findings = full["findings"]
    approximations = result.pop("approximations")
    for key in ("nets", "nets_unrouted_in_altium", "assumed_copper_nets"):
        if key in full and len(full[key]) > net_limit:
            result[key] = full[key][:net_limit]
            result[f"{key}_not_listed"] = len(full[key]) - net_limit
            result[f"{key}_hint"] = "Narrow net or net_class to see the rest."
    page = []
    for finding in findings[offset : offset + limit]:
        connections = finding.get("connections")
        if connections is not None and len(connections) > connection_limit:
            finding = dict(finding)
            finding["connections"] = connections[:connection_limit]
            finding["connections_not_listed"] = len(connections) - connection_limit
        page.append(finding)
    result["findings"] = page
    result["total"] = len(findings)
    result["offset"] = offset
    result["has_more"] = offset + limit < len(findings)
    if result["has_more"]:
        result["next_offset"] = offset + limit
    result["approximations"] = approximations
    return result


# ---------- Altium's own Batch DRC report ----------

_REPORT_NET = re.compile(r"\bNet (\S+) Between\b")
_NAMES_LISTED = 20


def _listed(names: list[str], key: str) -> dict:
    """{key: up to _NAMES_LISTED names} and, when there are more, how many."""
    out: dict = {key: names[:_NAMES_LISTED]}
    if len(names) > _NAMES_LISTED:
        out[f"{key}_not_listed"] = len(names) - _NAMES_LISTED
    return out


def drc_comparison(report, full: dict) -> dict:
    """Altium's Un-Routed Net and Net Antennae violations in a Batch DRC report
    against this check. The net lists cover the nets checked, whatever the
    checks and layer filters; the report's counts are always the whole board's.
    Where a net's rule checks incomplete connections, the connections joined
    here only through weak connections count as missing, and a net routed here
    only through them as not routed."""
    unrouted_nets: Counter = Counter()
    unrouted = antennae = connection_rows = other_rows = 0
    antenna_tables = 0
    for table in report.tables:
        if table.type != RULE:
            continue
        kind = rule_kind(table.title)[0]
        if kind == "NetAntennae":
            antennae += table.count
            antenna_tables += 1
        elif kind == "UnRoutedNet":
            unrouted += table.count
            for violation in table.violations:
                # 'Net X Between A And B' is a missing connection; other rows are
                # dead copper without a net and broken planes.
                match = _REPORT_NET.search(violation.message)
                if match:
                    connection_rows += 1
                    unrouted_nets[match[1]] += 1
                else:
                    other_rows += 1
    checked = full.get("_checked_nets")
    # A net routed here only through weak connections is not routed in Altium
    # when its rule checks incomplete connections.
    weak_only = list(full.get("_weak_not_routed_nets", []))
    here = list(full.get("_not_routed_nets", [])) + weak_only
    there = [net for net in unrouted_nets if checked is None or net.lower() in checked]
    here_lower = {net.lower() for net in here}
    there_lower = {net.lower() for net in there}
    summary = full["summary"]
    rules = full.get("rules", {}).get("NetAntennae", {})
    weak_missing = full.get("_weak_missing_connections", 0)
    weak_block = (
        {
            "missing_connections_here_only_by_weak_connections": weak_missing,
            "nets_not_routed_here_only_by_weak_connections": len(weak_only),
        }
        if "nets_routed_here_unrouted_in_altium" in summary else {}
    )
    return {
        "unrouted": {
            "report_violations": unrouted,
            "report_missing_connections": connection_rows,
            "report_other_rows": other_rows,
            "report_nets": len(there),
            "missing_connections_here": summary["connections"]["missing"] + weak_missing,
            "nets_not_routed_here": len(here),
            **weak_block,
            **_listed(
                sorted(net for net in there if net.lower() not in here_lower),
                "report_nets_routed_here",
            ),
            **_listed(
                sorted(net for net in here if net.lower() not in there_lower),
                "not_routed_here_not_in_report",
            ),
        },
        "net_antennae": {
            "report_violations": antennae,
            "report_checked_net_antennae": antenna_tables > 0,
            "enabled_rules": sum(1 for rule in rules.get("rules", []) if rule["enabled"]),
            "checked_by_batch_drc": rules.get("checked_by_batch_drc"),
            "antennae_here": summary["antennae"],
            "over_tolerance_here": summary["antennae_over_tolerance"],
            "without_enabled_rule_here": summary["antennae_without_enabled_rule"],
        },
    }
