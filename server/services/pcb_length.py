"""Signal lengths over a per-net copper graph, and Length / MatchedLengths checks.

Each net's copper (tracks, true-length arcs, vias, pads; pours excluded, a
hatched pour's tracks and arcs too) is a graph. Track endpoints merge when they
overlap, an endpoint landing on another track's body splits it (T-junction),
and endpoints inside a pad or via attach to its centre with an edge as long as
the distance to that centre. Vias and
through-hole pads get one node per copper layer they connect on, joined by
barrel edges as long as the stackup z distance between those layers.

A pad-to-pad length is the shortest path between the two pad centres: in-pad
wandering does not count, straight fanout copper does, and a via counts the
barrel over the layers actually traversed. The end pads' Pin/Package Length
(pad to die) is added, as Altium adds it to Signal Length. That reproduces the
example boards' tuned groups far better than whole-net routed length.

Graphs are built lazily and cached on the PcbIndex instance; everything else
is pure functions over the index. Lengths are float mils under *_mil keys.
"""

import heapq
import math
import re
from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass, field

try:
    from parsers.pcb_doc import PAD_SHAPE_ROUND, PAD_SHAPE_ROUNDRECT, pad_copper_on
    from services.pcb_index import layer_copper
    from services.pcb_rules import (
        MULTI_LAYER,
        ScopeSyntaxError,
        evaluate_scope,
        parse_scope,
        rule_header,
        rule_values,
        stale_references,
    )
except ImportError:
    from server.parsers.pcb_doc import (
        PAD_SHAPE_ROUND, PAD_SHAPE_ROUNDRECT, pad_copper_on,
    )
    from server.services.pcb_index import layer_copper
    from server.services.pcb_rules import (
        MULTI_LAYER,
        ScopeSyntaxError,
        evaluate_scope,
        parse_scope,
        rule_header,
        rule_values,
        stale_references,
    )

LENGTH_RULE_KINDS = ("Length", "MatchedLengths")
DEFAULT_LIMIT = 50
# With pin/package length included, tuned groups come out within a few mil of
# Altium's DRC figures; paths run to pad centres (copper wandering inside pads
# or off the path is left out) and via barrels follow the stackup, so
# violations this close to a band edge are flagged marginal rather than
# certain.
MARGINAL_MIL = 5.0
_CONDITIONAL_LIMIT = 20
_MST_ISLAND_LIMIT = 16

MEASURE = (
    "Signal path: shortest copper path from pad centre to pad centre "
    "(tracks, true arc lengths, attach to pad and via centres) plus via barrel "
    "over the stackup span actually traversed plus both end pads' Pin/Package "
    "Length (pin_package_mil). An xSignal sums its routed legs plus the "
    "Pin/Package Length of its first and last pins and of any pin only one "
    "routed leg reaches (not of fly-by pins the route passes through); a "
    "series part between legs counts 0. A net with exactly 2 pads uses its "
    "signal path (like Altium's Signal Length); other nets use routed copper "
    "plus each via's barrel over the layers its copper connects, without "
    "Pin/Package Length (like Altium's Routed Length). A differential pair's "
    "length is the P/N average (as Altium's DRC reports it); max_mil is the "
    "longer leg."
)

APPROXIMATIONS = [
    "Pin/package length is included where Altium includes it (2-pad signal "
    "paths and pair legs, pad-to-pad paths, xSignal end pins) and not on "
    "multi-pad nets; it is read from one pad-record field, and pad records "
    "too old to carry it count 0.",
    "A series part between xSignal legs counts 0 length.",
    "Polygon pours (solid or hatched), fills and regions are not part of the "
    "copper graph; a connection made only through them is reported incomplete.",
    "Pad shapes: round/oblong pads are capsules, rounded rectangles keep their "
    "corners, octagonal pads are rectangles, and through-hole pads have each "
    "layer's size and shape; paths run to the pad or via centre, so "
    "copper wandering inside a pad and stubs off the path are not counted "
    "(Altium's track total may be longer).",
    "Nets without exactly 2 pads use routed copper plus via barrels; Altium "
    "shows no Signal Length for them (use xSignals).",
    f"Violations within {MARGINAL_MIL:g} mil of the band are marked marginal: "
    "pad entry and via barrel conventions can differ from Altium's; on Altium's MiniPC example the Length-rule nets come out 6-13 mil from their windows.",
]

_GRAPH_SQL = (
    "SELECT id, kind, layer, layer_to, component, x1, y1, x2, y2, width, "
    "height, rotation, shape, corner_radius, layer_copper, hole, pad_name, "
    "length, pin_package FROM prims "
    "WHERE net_id = ? AND kind IN ('track', 'arc', 'via', 'pad') "
    "AND pour_id IS NULL AND poured_by IS NULL ORDER BY id"
)
_SPLIT_EPS_MIL = 0.01
_MERGE_MIN_MIL = 0.01
# Copper exactly touching (tangent end caps, a track end on a pad's edge) joins: the
# containment tests allow this much so float rounding of the stored coordinates does not
# decide it.
_TOUCH_EPS_MIL = 0.01
_XSIGNAL_FUNCTIONS = frozenset(
    {"inxsignal", "inxsignalclass", "inanyxsignal", "isxsignal"}
)
_MIL_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*mil\s*$")
# Raw MatchedLengths / Length keys this module interprets itself, beyond what
# rule_values maps.
_HANDLED_RAW_KEYS = {
    "MINLIMIT", "MAXLIMIT", "TOLERANCE", "CHECKNETSINDIFFPAIR",
    "CHECKDIFFPAIRVSDIFFPAIR", "CHECKXSIGNALS", "CHECKOTHERS",
    "USEDELAYUNITS", "TARGETSOURCENAME", "STYLE", "AMPLITUDE", "GAP",
}


def _r(value: float | None) -> float | None:
    return None if value is None else round(value, 2)


def _parse_mil(raw) -> float | None:
    if raw is None:
        return None
    match = _MIL_RE.match(str(raw))
    return float(match.group(1)) if match else None


def _cache(index) -> dict:
    return index.__dict__.setdefault("_length_cache", {})


def _column(row, key: str):
    """row[key], or None when the row (sqlite3.Row or dict) lacks the column."""
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


# ---------- stackup ----------


def stack_z(index) -> dict[int, float] | None:
    """z (mil, from the top copper's top face) of each copper layer's centre.

    Only layers between the outermost copper layers count. None when any
    thickness there is missing (legacy stackups): barrels are then unknown,
    but planar lengths still compute.
    """
    cache = _cache(index)
    if "z" in cache:
        return cache["z"]
    rows = index.db.execute(
        "SELECT v6_id, kind, copper_thick_mil, diel_height_mil FROM layers "
        "WHERE stack_order IS NOT NULL ORDER BY stack_order"
    ).fetchall()
    copper = [position for position, row in enumerate(rows) if row["kind"] == "copper"]
    z: dict[int, float] | None = None
    if copper:
        z = {}
        depth = 0.0
        for row in rows[copper[0] : copper[-1] + 1]:
            is_copper = row["kind"] == "copper"
            thickness = row["copper_thick_mil"] if is_copper else row["diel_height_mil"]
            if thickness is None:
                z = None
                break
            if is_copper and row["v6_id"] is not None:
                z[row["v6_id"]] = depth + thickness / 2.0
            depth += thickness
    cache["z"] = z
    return z


# ---------- geometry helpers ----------


def pad_contains(
    pad: dict, x: float, y: float, tolerance: float, layer: int | None = None
) -> bool:
    """Is (x, y) inside the pad's copper on layer grown by tolerance? Round/
    oblong pads are capsules, rounded rectangles keep their corners, every
    other shape is its rotated rectangle. A multi-layer pad's copper can
    differ per layer; without a layer it has its main (top-layer) copper."""
    dx, dy = x - pad["x"], y - pad["y"]
    width, height = pad["w"], pad["h"]
    shape, corner = pad["shape"], pad.get("corner") or 0.0
    copper = (
        pad_copper_on(pad.get("copper"), layer) if layer is not None else None
    )
    if copper is not None:
        width, height, shape = copper.width, copper.height, copper.shape
        corner = copper.corner_radius or 0.0
    if shape == PAD_SHAPE_ROUND:
        minor, major = min(width, height), max(width, height)
        if major - minor < 1e-9:
            return dx * dx + dy * dy <= (width / 2.0 + tolerance) ** 2
        angle = math.radians(pad["rot"] + (90.0 if height > width else 0.0))
        ux, uy = math.cos(angle), math.sin(angle)
        half = (major - minor) / 2.0
        along = max(-half, min(half, dx * ux + dy * uy))
        px, py = dx - along * ux, dy - along * uy
        return px * px + py * py <= (minor / 2.0 + tolerance) ** 2
    angle = math.radians(pad["rot"])
    cosine, sine = math.cos(angle), math.sin(angle)
    local_x = dx * cosine + dy * sine
    local_y = -dx * sine + dy * cosine
    if shape == PAD_SHAPE_ROUNDRECT and corner > 0.0:
        corner = min(corner, width / 2.0, height / 2.0)
        out_x = max(0.0, abs(local_x) - (width / 2.0 - corner))
        out_y = max(0.0, abs(local_y) - (height / 2.0 - corner))
        return out_x * out_x + out_y * out_y <= (corner + tolerance) ** 2
    # Grown by the tolerance, the rectangle keeps round corners: copper off a
    # corner diagonally is as far as its distance to that corner.
    out_x = max(0.0, abs(local_x) - width / 2.0)
    out_y = max(0.0, abs(local_y) - height / 2.0)
    return out_x * out_x + out_y * out_y <= tolerance * tolerance


def _project(segment: tuple, x: float, y: float):
    """(t, px, py, distance) of (x, y) onto a segment's interior, else None."""
    x1, y1, x2, y2 = segment[0], segment[1], segment[2], segment[3]
    planar = segment[6]
    if planar <= 0.0:
        return None
    dx, dy = x2 - x1, y2 - y1
    t = ((x - x1) * dx + (y - y1) * dy) / (planar * planar)
    if t * planar <= _SPLIT_EPS_MIL or (1.0 - t) * planar <= _SPLIT_EPS_MIL:
        return None
    px, py = x1 + t * dx, y1 + t * dy
    return t, px, py, math.hypot(x - px, y - py)


def _cells(x: float, y: float, reach: float, size: float):
    for gx in range(math.floor((x - reach) / size), math.floor((x + reach) / size) + 1):
        for gy in range(math.floor((y - reach) / size), math.floor((y + reach) / size) + 1):
            yield gx, gy


def _min_manhattan(points_a: list, points_b: list) -> float | None:
    """Smallest |dx| + |dy| between two point sets (sorted sweep on x)."""
    if not points_a or not points_b:
        return None
    ordered = sorted(points_b)
    xs = [point[0] for point in ordered]
    best = math.inf
    for x, y in points_a:
        start = bisect_left(xs, x)
        cursor = start - 1
        while cursor >= 0 and x - xs[cursor] < best:
            best = min(best, x - xs[cursor] + abs(y - ordered[cursor][1]))
            cursor -= 1
        cursor = start
        while cursor < len(xs) and xs[cursor] - x < best:
            best = min(best, xs[cursor] - x + abs(y - ordered[cursor][1]))
            cursor += 1
    return best


# ---------- net graph ----------


@dataclass
class PathResult:
    """One pad-to-pad measurement; raw floats (rounded only for output)."""

    status: str  # "ok" | "no_path"
    length: float | None = None
    copper: float | None = None
    barrel: float | None = None  # None when the stackup is unknown
    vias: int = 0
    layers: dict[int, float] = field(default_factory=dict)
    gap: float | None = None  # unrouted Manhattan distance when no_path
    end_pads: tuple[int, int] | None = None  # (source, target) pad ids when ok


class NetGraph:
    """Copper connectivity graph of one net.

    Nodes are (x, y, layer) points; edges are track pieces ("seg"), attach
    edges from an endpoint to a pad or via centre ("attach") and barrels
    between per-layer nodes of a via or through-hole pad ("barrel").
    """

    def __init__(self, rows, order: list[int], z: dict[int, float] | None):
        self.order = list(order)
        self.order_pos = {layer: position for position, layer in enumerate(order)}
        self.z = z
        self.pos: list[tuple[float, float, int]] = []
        self.adj: list[list[tuple[int, float, int]]] = []
        # (kind, layer, layer_b, owner, barrel_known, weight)
        self.edges: list[tuple] = []
        self.pads: dict[int, dict] = {}
        self.pad_nodes: dict[int, dict[int, int]] = {}
        self.vias: dict[int, dict] = {}
        self.via_nodes: dict[int, dict[int, int]] = {}
        self.routed_mil = 0.0
        self.routed_by_layer: dict[int, float] = defaultdict(float)
        self.segment_count = 0
        # Track and arc-chord rows: (x1, y1, x2, y2, layer, half width,
        # planar length, true length, prim id), and each row's end nodes.
        self.segments: list[tuple] = []
        self.track_ends: dict[int, tuple[int, int]] = {}
        self._attached: dict[tuple[int, int], float] = {}
        # Track ends lying inside another track's round end cap (overlapping
        # copper that the endpoint merge leaves apart): node pair -> distance.
        self._cap_links: dict[tuple[int, int], float] = {}
        self._islands: list[int] | None = None
        self._node_pads: dict[int, int] | None = None
        self._build(rows)

    # -- construction --

    def _node(self, x: float, y: float, layer: int) -> int:
        self.pos.append((x, y, layer))
        self.adj.append([])
        return len(self.pos) - 1

    def _edge(self, a: int, b: int, weight: float, meta: tuple) -> None:
        number = len(self.edges)
        self.edges.append(meta + (weight,))
        self.adj[a].append((b, weight, number))
        self.adj[b].append((a, weight, number))

    def _attach(self, end_node: int, owner_node: int, distance: float) -> None:
        """Record an attach edge; merged endpoints may offer several, the
        shortest wins. Edges are created once construction is done."""
        if end_node == owner_node:
            return
        key = (end_node, owner_node)
        if distance < self._attached.get(key, math.inf):
            self._attached[key] = distance

    def _owner_node(self, nodes: dict[int, int], x: float, y: float, layer: int) -> int:
        node = nodes.get(layer)
        if node is None:
            node = self._node(x, y, layer)
            nodes[layer] = node
        return node

    def _span(self, row) -> list[int]:
        layer, layer_to = row["layer"], row["layer_to"]
        if layer in self.order_pos and layer_to in self.order_pos:
            low, high = sorted((self.order_pos[layer], self.order_pos[layer_to]))
            span = self.order[low : high + 1]
            if self.order and span[0] == self.order[0] and span[-1] == self.order[-1]:
                # A through via also reaches copper on layers missing from the
                # stack table (their barrel length is unknown).
                span = span + self.unstacked
            return span
        return list(dict.fromkeys(item for item in (layer, layer_to) if item is not None))

    def _build(self, rows) -> None:
        # x1, y1, x2, y2, layer, half width, planar, true, prim id
        segments: list[tuple] = self.segments
        vias: list[dict] = []
        pads: list[dict] = []
        # Signal layers carrying tracks but absent from the stack table: through
        # vias and multi-layer pads still connect them (barrel unknown).
        self.unstacked = sorted({
            row["layer"]
            for row in rows
            if row["kind"] in ("track", "arc")
            and row["layer"] not in self.order_pos
            and 1 <= row["layer"] <= 32
        }) if self.order else []
        for row in rows:
            kind = row["kind"]
            if kind in ("track", "arc"):
                planar = math.hypot(row["x2"] - row["x1"], row["y2"] - row["y1"])
                true = row["length"] if row["length"] is not None else planar
                segments.append((
                    row["x1"], row["y1"], row["x2"], row["y2"], row["layer"],
                    (row["width"] or 0.0) / 2.0, planar, true, row["id"],
                ))
                self.routed_mil += true
                self.routed_by_layer[row["layer"]] += true
            elif kind == "via":
                via = {
                    "pid": row["id"], "x": row["x1"], "y": row["y1"],
                    "r": (row["width"] or 0.0) / 2.0, "layers": self._span(row),
                }
                via["layer_set"] = set(via["layers"])
                vias.append(via)
                self.vias[row["id"]] = via
                self.via_nodes[row["id"]] = {}
            else:
                width = row["width"] or 0.0
                pad = {
                    "pid": row["id"], "x": row["x1"], "y": row["y1"], "w": width,
                    "h": row["height"] or width, "rot": row["rotation"] or 0.0,
                    "shape": row["shape"], "corner": _column(row, "corner_radius"),
                    "copper": layer_copper(row),
                    "component": row["component"],
                    "name": row["pad_name"],
                    "ppl": _column(row, "pin_package") or 0.0,
                    "layers": (
                        list(self.order) + self.unstacked
                        if row["layer"] == MULTI_LAYER and self.order
                        else [row["layer"]]
                    ),
                }
                pad["layer_set"] = set(pad["layers"])
                # A multi-layer pad's inner copper can be the larger.
                pad["reach"] = max(
                    [math.hypot(pad["w"], pad["h"])]
                    + [
                        math.hypot(copper.width, copper.height)
                        for copper in (pad["copper"] or {}).values()
                    ]
                ) / 2.0
                pads.append(pad)
                self.pads[row["id"]] = pad
                nodes: dict[int, int] = {}
                if len(pad["layers"]) == 1:
                    nodes[pad["layers"][0]] = self._node(pad["x"], pad["y"], pad["layers"][0])
                self.pad_nodes[row["id"]] = nodes
        self.segment_count = len(segments)

        # Endpoints, merged when they overlap (union-find over a grid hash).
        count = 2 * len(segments)
        end_x = [0.0] * count
        end_y = [0.0] * count
        end_layer = [0] * count
        end_half = [0.0] * count
        for number, segment in enumerate(segments):
            for side in (0, 1):
                end = 2 * number + side
                end_x[end] = segment[0] if side == 0 else segment[2]
                end_y[end] = segment[1] if side == 0 else segment[3]
                end_layer[end] = segment[4]
                end_half[end] = segment[5]
        max_half = max(end_half, default=0.0)
        end_cell = max(5.0, max_half)
        end_grid: dict[tuple[int, int], list[int]] = defaultdict(list)
        for end in range(count):
            end_grid[(math.floor(end_x[end] / end_cell), math.floor(end_y[end] / end_cell))].append(end)
        parent = list(range(count))

        def find(item: int) -> int:
            while parent[item] != item:
                parent[item] = parent[parent[item]]
                item = parent[item]
            return item

        for end in range(count):
            gx = math.floor(end_x[end] / end_cell)
            gy = math.floor(end_y[end] / end_cell)
            for cx in (gx - 1, gx, gx + 1):
                for cy in (gy - 1, gy, gy + 1):
                    for other in end_grid.get((cx, cy), ()):
                        if other <= end or end_layer[other] != end_layer[end]:
                            continue
                        tolerance = max(_MERGE_MIN_MIL, min(end_half[end], end_half[other]))
                        dx = end_x[other] - end_x[end]
                        dy = end_y[other] - end_y[end]
                        if dx * dx + dy * dy <= tolerance * tolerance:
                            root_a, root_b = find(end), find(other)
                            if root_a != root_b:
                                parent[root_a] = root_b
        node_of_end = [0] * count
        root_nodes: dict[int, int] = {}
        for end in range(count):
            root = find(end)
            node = root_nodes.get(root)
            if node is None:
                node = self._node(end_x[root], end_y[root], end_layer[root])
                root_nodes[root] = node
            node_of_end[end] = node
        for number, segment in enumerate(segments):
            self.track_ends[segment[8]] = (
                node_of_end[2 * number], node_of_end[2 * number + 1],
            )

        # An endpoint inside another track's round end cap (a neck-down that
        # does not snap) overlaps it without merging, since the merge keeps to
        # the narrower half-width so meanders get no shortcuts: link the two
        # ends with an edge as long as their distance.
        for end in range(count):
            gx = math.floor(end_x[end] / end_cell)
            gy = math.floor(end_y[end] / end_cell)
            for cx in (gx - 1, gx, gx + 1):
                for cy in (gy - 1, gy, gy + 1):
                    for other in end_grid.get((cx, cy), ()):
                        if (
                            other <= end
                            or other // 2 == end // 2
                            or end_layer[other] != end_layer[end]
                        ):
                            continue
                        node_a, node_b = node_of_end[end], node_of_end[other]
                        if node_a == node_b:
                            continue
                        distance = math.hypot(
                            end_x[other] - end_x[end], end_y[other] - end_y[end]
                        )
                        if distance <= max(end_half[end], end_half[other]) + _TOUCH_EPS_MIL:
                            key = (min(node_a, node_b), max(node_a, node_b))
                            if distance < self._cap_links.get(key, math.inf):
                                self._cap_links[key] = distance

        # Segments in a grid (cells along their length, with neighbours) so a
        # point's own cell lists every segment whose copper can reach it.
        seg_cell = max(10.0, 2.0 * max_half)
        seg_grid: dict[tuple[int, int, int], list[int]] = defaultdict(list)
        for number, segment in enumerate(segments):
            steps = int(segment[6] / seg_cell) + 1
            cells: set[tuple[int, int]] = set()
            for step in range(steps + 1):
                fraction = step / steps
                gx = math.floor((segment[0] + (segment[2] - segment[0]) * fraction) / seg_cell)
                gy = math.floor((segment[1] + (segment[3] - segment[1]) * fraction) / seg_cell)
                for cx in (gx - 1, gx, gx + 1):
                    for cy in (gy - 1, gy, gy + 1):
                        cells.add((cx, cy))
            for cx, cy in cells:
                seg_grid[(segment[4], cx, cy)].append(number)

        def segments_at(layer: int, x: float, y: float) -> list[int]:
            return seg_grid.get(
                (layer, math.floor(x / seg_cell), math.floor(y / seg_cell)), []
            )

        # T-junctions: an endpoint on another segment's body splits it. The
        # copper overlaps when the endpoint lies within the host's half-width
        # or, for a wider track ending on a narrow one, the host's centreline
        # lies within the end's round cap.
        splits: dict[int, list[tuple[float, int]]] = defaultdict(list)
        for end in range(count):
            node = node_of_end[end]
            own = end // 2
            for host in segments_at(end_layer[end], end_x[end], end_y[end]):
                if host == own or node in (node_of_end[2 * host], node_of_end[2 * host + 1]):
                    continue
                hit = _project(segments[host], end_x[end], end_y[end])
                if hit is not None and hit[3] <= max(segments[host][5], end_half[end]) + _TOUCH_EPS_MIL:
                    splits[host].append((hit[0], node))

        def ends_near(x: float, y: float, reach: float):
            for cell in _cells(x, y, reach, end_cell):
                yield from end_grid.get(cell, ())

        def body_attach(owner_nodes: dict[int, int], x, y, layers, fits) -> None:
            """Attach segments running over the owner without an endpoint on it."""
            for layer in layers:
                for host in segments_at(layer, x, y):
                    owner_node = owner_nodes.get(layer)
                    if owner_node is not None and (
                        (node_of_end[2 * host], owner_node) in self._attached
                        or (node_of_end[2 * host + 1], owner_node) in self._attached
                    ):
                        continue
                    segment = segments[host]
                    hit = _project(segment, x, y)
                    if hit is None or not fits(hit[1], hit[2], hit[3], segment[5], layer):
                        continue
                    owner_node = self._owner_node(owner_nodes, x, y, layer)
                    split_node = self._node(hit[1], hit[2], layer)
                    splits[host].append((hit[0], split_node))
                    self._attach(split_node, owner_node, hit[3])

        for via in vias:
            nodes = self.via_nodes[via["pid"]]
            x, y, radius = via["x"], via["y"], via["r"]
            for end in ends_near(x, y, radius + max_half):
                if end_layer[end] not in via["layer_set"]:
                    continue
                distance = math.hypot(end_x[end] - x, end_y[end] - y)
                if distance <= radius + end_half[end] + _TOUCH_EPS_MIL:
                    owner = self._owner_node(nodes, x, y, end_layer[end])
                    self._attach(node_of_end[end], owner, distance)
            body_attach(
                nodes, x, y, via["layers"],
                lambda px, py, distance, half, layer, radius=radius: (
                    distance <= radius + half + _TOUCH_EPS_MIL
                ),
            )
        for pad in pads:
            nodes = self.pad_nodes[pad["pid"]]
            x, y = pad["x"], pad["y"]
            for end in ends_near(x, y, pad["reach"] + max_half):
                if end_layer[end] not in pad["layer_set"]:
                    continue
                if pad_contains(
                    pad, end_x[end], end_y[end], end_half[end] + _TOUCH_EPS_MIL, end_layer[end]
                ):
                    owner = self._owner_node(nodes, x, y, end_layer[end])
                    self._attach(
                        node_of_end[end], owner,
                        math.hypot(end_x[end] - x, end_y[end] - y),
                    )
            body_attach(
                nodes, x, y, pad["layers"],
                lambda px, py, distance, half, layer, pad=pad: pad_contains(
                    pad, px, py, half + _TOUCH_EPS_MIL, layer
                ),
            )

        # Vias touching pads (via-in-pad) or other vias (stacked microvias).
        if vias:
            via_cell = 50.0
            via_grid: dict[tuple[int, int], list[int]] = defaultdict(list)
            max_radius = max(via["r"] for via in vias)
            for number, via in enumerate(vias):
                via_grid[(math.floor(via["x"] / via_cell), math.floor(via["y"] / via_cell))].append(number)
            for pad in pads:
                for cell in _cells(pad["x"], pad["y"], pad["reach"] + max_radius, via_cell):
                    for number in via_grid.get(cell, ()):
                        via = vias[number]
                        common = [
                            layer for layer in via["layers"]
                            if layer in pad["layer_set"]
                            and pad_contains(pad, via["x"], via["y"], via["r"] + _TOUCH_EPS_MIL, layer)
                        ]
                        if not common:
                            continue
                        distance = math.hypot(via["x"] - pad["x"], via["y"] - pad["y"])
                        for layer in common:
                            self._attach(
                                self._owner_node(self.via_nodes[via["pid"]], via["x"], via["y"], layer),
                                self._owner_node(self.pad_nodes[pad["pid"]], pad["x"], pad["y"], layer),
                                distance,
                            )
            for number, via in enumerate(vias):
                for cell in _cells(via["x"], via["y"], via["r"] + max_radius, via_cell):
                    for other_number in via_grid.get(cell, ()):
                        if other_number <= number:
                            continue
                        other = vias[other_number]
                        distance = math.hypot(other["x"] - via["x"], other["y"] - via["y"])
                        common = [layer for layer in via["layers"] if layer in other["layer_set"]]
                        if not common or distance > via["r"] + other["r"] + _TOUCH_EPS_MIL:
                            continue
                        for layer in common:
                            self._attach(
                                self._owner_node(self.via_nodes[via["pid"]], via["x"], via["y"], layer),
                                self._owner_node(self.via_nodes[other["pid"]], other["x"], other["y"], layer),
                                distance,
                            )

        # Track pieces between endpoints and split points.
        for number, segment in enumerate(segments):
            points = [(0.0, node_of_end[2 * number])]
            seen = {node_of_end[2 * number]}
            for t, node in sorted(splits.get(number, ())):
                if node not in seen:
                    seen.add(node)
                    points.append((t, node))
            points.append((1.0, node_of_end[2 * number + 1]))
            for (t0, node0), (t1, node1) in zip(points, points[1:]):
                if node0 != node1:
                    self._edge(
                        node0, node1, (t1 - t0) * segment[7],
                        ("seg", segment[4], None, ("seg", segment[8]), True),
                    )

        for (end_node, owner_node), distance in self._attached.items():
            self._edge(
                end_node, owner_node, distance,
                ("attach", self.pos[owner_node][2], None, None, True),
            )
        for (node_a, node_b), distance in self._cap_links.items():
            self._edge(
                node_a, node_b, distance,
                ("seg", self.pos[node_a][2], None, None, True),
            )

        # Barrels between the per-layer nodes of each via / through-hole pad.
        for pid, nodes in self.via_nodes.items():
            self._barrels(("via", pid), nodes)
        for pid, nodes in self.pad_nodes.items():
            if not nodes:
                pad = self.pads[pid]
                layer = pad["layers"][0] if pad["layers"] else 0
                nodes[layer] = self._node(pad["x"], pad["y"], layer)
            self._barrels(("pad", pid), nodes)

    def _ordered_layers(self, layers) -> list[int]:
        return sorted(layers, key=lambda layer: self.order_pos.get(layer, 1000 + layer))

    def _z_gap(self, layer_a: int, layer_b: int) -> tuple[float, bool]:
        if self.z is not None and layer_a in self.z and layer_b in self.z:
            return abs(self.z[layer_a] - self.z[layer_b]), True
        return 0.0, False

    def _barrels(self, owner: tuple, nodes: dict[int, int]) -> None:
        layers = self._ordered_layers(nodes)
        for layer_a, layer_b in zip(layers, layers[1:]):
            weight, known = self._z_gap(layer_a, layer_b)
            self._edge(
                nodes[layer_a], nodes[layer_b], weight,
                ("barrel", layer_a, layer_b, owner, known),
            )

    # -- queries --

    def nodes_of(self, pad_ids) -> list[int]:
        return [node for pid in pad_ids for node in self.pad_nodes.get(pid, {}).values()]

    def pad_of_node(self, node: int) -> int | None:
        if self._node_pads is None:
            self._node_pads = {
                pad_node: pid
                for pid, nodes in self.pad_nodes.items()
                for pad_node in nodes.values()
            }
        return self._node_pads.get(node)

    def pin_package(self, pad_ids) -> float:
        """Sum of the pads' Pin/Package Length (mil)."""
        return sum(self.pads[pid]["ppl"] for pid in pad_ids if pid in self.pads)

    def shortest(self, pad_ids_a, pad_ids_b) -> PathResult:
        sources = self.nodes_of(pad_ids_a)
        targets = set(self.nodes_of(pad_ids_b))
        if not sources or not targets:
            return PathResult("no_path")
        dist = {node: 0.0 for node in sources}
        previous: dict[int, tuple[int, int]] = {}
        heap = [(0.0, node) for node in sources]
        heapq.heapify(heap)
        while heap:
            distance, node = heapq.heappop(heap)
            if distance > dist.get(node, math.inf):
                continue
            if node in targets:
                target_pad = self.pad_of_node(node)
                path_edges = []
                while node in previous:
                    node, edge = previous[node]
                    path_edges.append(edge)
                result = self._summarise(path_edges)
                result.end_pads = (self.pad_of_node(node), target_pad)
                return result
            for neighbour, weight, edge in self.adj[node]:
                candidate = distance + weight
                if candidate < dist.get(neighbour, math.inf):
                    dist[neighbour] = candidate
                    previous[neighbour] = (node, edge)
                    heapq.heappush(heap, (candidate, neighbour))
        reach_a = self.reachable(sources)
        reach_b = self.reachable(list(targets))
        return PathResult("no_path", gap=self.gap(reach_a, reach_b))

    def _summarise(self, path_edges: list[int]) -> PathResult:
        copper = barrel = 0.0
        known = self.z is not None
        vias: set[int] = set()
        layers: dict[int, float] = defaultdict(float)
        for edge in path_edges:
            kind, layer, _layer_b, owner, edge_known, weight = self.edges[edge]
            if kind == "barrel":
                barrel += weight
                known = known and edge_known
                if owner[0] == "via":
                    vias.add(owner[1])
            else:
                copper += weight
                layers[layer] += weight
        return PathResult(
            "ok",
            length=copper + barrel,
            copper=copper,
            barrel=barrel if known else None,
            vias=len(vias),
            layers=dict(layers),
        )

    def reachable(self, sources) -> set[int]:
        seen = set(sources)
        stack = list(sources)
        while stack:
            node = stack.pop()
            for neighbour, _, _ in self.adj[node]:
                if neighbour not in seen:
                    seen.add(neighbour)
                    stack.append(neighbour)
        return seen

    def gap(self, nodes_a, nodes_b) -> float | None:
        return _min_manhattan(
            [self.pos[node][:2] for node in nodes_a],
            [self.pos[node][:2] for node in nodes_b],
        )

    def islands(self) -> list[int]:
        """Connected-component id of every node."""
        if self._islands is None:
            labels = [-1] * len(self.pos)
            current = 0
            for start in range(len(self.pos)):
                if labels[start] >= 0:
                    continue
                labels[start] = current
                stack = [start]
                while stack:
                    node = stack.pop()
                    for neighbour, _, _ in self.adj[node]:
                        if labels[neighbour] < 0:
                            labels[neighbour] = current
                            stack.append(neighbour)
                current += 1
            self._islands = labels
        return self._islands

    def via_spans(self) -> tuple[float, int, bool]:
        """(barrel total, vias used, known): each via's barrel over the
        layers its copper connects on (Altium's routed-length via term)."""
        total = 0.0
        used = 0
        known = self.z is not None
        for nodes in self.via_nodes.values():
            if len(nodes) < 2:
                continue
            layers = self._ordered_layers(nodes)
            weight, edge_known = self._z_gap(layers[0], layers[-1])
            total += weight
            known = known and edge_known
            used += 1
        return total, used, known


def net_graph(index, net_id: int) -> NetGraph:
    """The cached copper graph of a net (built on first use)."""
    graphs = index.__dict__.setdefault("_net_graphs", {})
    graph = graphs.get(net_id)
    if graph is None:
        rows = index.db.execute(_GRAPH_SQL, (net_id,)).fetchall()
        graph = NetGraph(rows, index._copper_layers_in_stack_order(), stack_z(index))
        graphs[net_id] = graph
    return graph


# ---------- lookups ----------


def _pin_map(index) -> dict[str, list[tuple[int, int, int | None]]]:
    """'<designator>-<pad>' (lower) -> [(prim id, component, net id)]."""
    cache = _cache(index)
    if "pins" not in cache:
        pins: dict[str, list] = defaultdict(list)
        for row in index.db.execute(
            "SELECT id, component, pad_name, net_id FROM prims WHERE kind = 'pad' "
            "AND component IS NOT NULL AND pad_name IS NOT NULL ORDER BY id"
        ):
            component = row["component"]
            if not 0 <= component < len(index.pcb.components):
                continue
            designator = index.designator(component)
            if designator:
                pins[f"{designator}-{row['pad_name']}".lower()].append(
                    (row["id"], component, row["net_id"])
                )
        cache["pins"] = dict(pins)
    return cache["pins"]


def _pad_pin_package(index) -> dict[int, float]:
    """prim id -> Pin/Package Length (mil) of every pad that has one."""
    cache = _cache(index)
    if "pin_package" not in cache:
        cache["pin_package"] = {
            row["id"]: row["pin_package"]
            for row in index.db.execute(
                "SELECT id, pin_package FROM prims "
                "WHERE kind = 'pad' AND pin_package > 0"
            )
        }
    return cache["pin_package"]


def _layer_names(index, layers: dict[int, float]) -> dict[str, float]:
    """{layer display name: mil} in stack order."""
    cache = _cache(index)
    names = cache.setdefault("layer_names", {})
    if "layer_order" not in cache:
        cache["layer_order"] = {
            layer: position
            for position, layer in enumerate(index._copper_layers_in_stack_order())
        }
    order = cache["layer_order"]
    out: dict[str, float] = {}
    for layer in sorted(layers, key=lambda item: order.get(item, 1000 + item)):
        if layer not in names:
            names[layer] = index.layer_name(layer)
        out[names[layer]] = round(layers[layer], 2)
    return out


def _pad_label(index, pad: dict) -> str:
    component = pad["component"]
    if component is not None and 0 <= component < len(index.pcb.components):
        designator = index.designator(component)
        if designator and pad["name"]:
            return f"{designator}-{pad['name']}"
    return f"free pad at ({pad['x']:.1f}, {pad['y']:.1f})"


def _net_name(index, net_id: int | None) -> str | None:
    if net_id is None or not 0 <= net_id < len(index.pcb.nets):
        return None
    return index.pcb.nets[net_id]


def _path_fields(index, result: PathResult, pin_package: float | None = None) -> dict:
    """Output fields of a path; pin_package (mil), when given, is added to the
    length and reported as pin_package_mil."""
    length = result.length
    if length is not None and pin_package is not None:
        length += pin_package
    fields: dict = {
        "length_mil": _r(length),
        "copper_mil": _r(result.copper),
        "via_barrel_mil": _r(result.barrel),
    }
    if pin_package is not None:
        fields["pin_package_mil"] = _r(pin_package)
    fields["vias"] = result.vias
    if result.status == "ok":
        fields["layers_mil"] = _layer_names(index, result.layers)
    if result.gap is not None:
        fields["unrouted_manhattan_mil"] = _r(result.gap)
    fields["status"] = result.status
    return fields


# ---------- public measurements ----------


def pad_to_pad(index, net: str | None, pin_a: str, pin_b: str) -> dict:
    """Shortest copper path between two pads ('<PCB designator>-<pad>').

    net may be None: the first pin's net is used.
    """
    pins = _pin_map(index)
    rows_a, rows_b = pins.get(pin_a.lower()), pins.get(pin_b.lower())
    for pin, rows in ((pin_a, rows_a), (pin_b, rows_b)):
        if not rows:
            return {"error": "pin_not_found", "message": f"No pad '{pin}' ('<designator>-<pad>')."}
    if net is None:
        net_id = rows_a[0][2]
    else:
        net_id = index.net_id(net)
        if net_id is None:
            return {"error": "net_not_found", "message": f"No PCB net named '{net}'."}
    ids_a = [pid for pid, _, row_net in rows_a if row_net == net_id]
    ids_b = [pid for pid, _, row_net in rows_b if row_net == net_id]
    for pin, ids in ((pin_a, ids_a), (pin_b, ids_b)):
        if not ids:
            return {
                "error": "pin_not_on_net",
                "message": f"Pad '{pin}' is not on net '{_net_name(index, net_id)}'.",
            }
    graph = net_graph(index, net_id)
    result = graph.shortest(ids_a, ids_b)
    pin_package = graph.pin_package(result.end_pads) if result.end_pads else None
    return {
        "net": _net_name(index, net_id),
        "from": pin_a,
        "to": pin_b,
        **_path_fields(index, result, pin_package),
    }


def _net_measure(index, net_id: int) -> dict:
    cache = _cache(index).setdefault("net_lengths", {})
    if net_id in cache:
        return cache[net_id]
    graph = net_graph(index, net_id)
    pad_ids = list(graph.pads)
    has_copper = graph.segment_count > 0 or bool(graph.vias)
    result: dict = {"net": _net_name(index, net_id)}
    if len(pad_ids) == 2:
        path = graph.shortest([pad_ids[0]], [pad_ids[1]])
        result["measure"] = "signal_path"
        result["pads"] = [_pad_label(index, graph.pads[pid]) for pid in pad_ids]
        result.update(_path_fields(index, path, graph.pin_package(pad_ids)))
        if path.status != "ok":
            result["status"] = "incomplete" if has_copper else "unrouted"
    else:
        barrel, used, known = graph.via_spans()
        result["measure"] = "routed_plus_vias"
        result["length_mil"] = _r(graph.routed_mil + barrel)
        result["copper_mil"] = _r(graph.routed_mil)
        result["via_barrel_mil"] = _r(barrel) if known else None
        # Routed length has no pin/package term (as in Altium); say when the
        # pads carry one that is therefore left out.
        result["pin_package_mil"] = 0.0
        excluded = graph.pin_package(pad_ids)
        if excluded:
            result["pin_package_excluded_mil"] = _r(excluded)
        result["vias"] = used
        result["layers_mil"] = _layer_names(index, graph.routed_by_layer)
        result["status"] = "ok"
        if len(pad_ids) >= 2:
            if not has_copper:
                result["status"] = "unrouted"
            else:
                islands, gap = _pad_islands(graph, pad_ids)
                if islands > 1:
                    result["status"] = "incomplete"
                    result["pad_islands"] = islands
                    if gap is not None:
                        result["unrouted_manhattan_mil"] = _r(gap)
    if result["status"] != "ok" and index.db.execute(
        "SELECT 1 FROM pours WHERE net_id = ? AND NOT shelved LIMIT 1", (net_id,)
    ).fetchone():
        result["note"] = (
            "The net has polygon pours, which are not part of the copper "
            "graph; they may complete the connection."
        )
    result["pad_count"] = len(pad_ids)
    result["routed_mil"] = _r(graph.routed_mil)
    cache[net_id] = result
    return result


def _pad_islands(graph: NetGraph, pad_ids: list[int]) -> tuple[int, float | None]:
    """(number of copper islands holding the net's pads, Manhattan length of
    a minimum spanning tree joining them). The tree is only computed for a
    few islands; nets split into many (planes and pours are not modelled)
    report None."""
    labels = graph.islands()
    islands: set[int] = set()
    for pid in pad_ids:
        for node in graph.pad_nodes[pid].values():
            islands.add(labels[node])
    if len(islands) < 2 or len(islands) > _MST_ISLAND_LIMIT:
        return len(islands), None
    members: dict[int, list[int]] = defaultdict(list)
    for node, label in enumerate(labels):
        if label in islands:
            members[label].append(node)
    ordered = sorted(islands)
    distance = {
        (first, second): graph.gap(members[first], members[second])
        for position, first in enumerate(ordered)
        for second in ordered[position + 1 :]
    }
    joined = {ordered[0]}
    total = 0.0
    while len(joined) < len(ordered):
        best, best_label = math.inf, None
        for label in ordered:
            if label in joined:
                continue
            for done in joined:
                gap = distance[(min(label, done), max(label, done))]
                if gap is not None and gap < best:
                    best, best_label = gap, label
        if best_label is None:
            return len(islands), None
        total += best
        joined.add(best_label)
    return len(islands), total


def net_length(index, net: str) -> dict:
    """Length of a net: the pad-to-pad signal path when it has exactly 2
    pads, otherwise routed copper plus via barrels."""
    net_id = index.net_id(net)
    if net_id is None:
        return {"error": "net_not_found", "message": f"No PCB net named '{net}'."}
    return dict(_net_measure(index, net_id))


def _xsignal_measure(index, xsignal) -> dict:
    cache = _cache(index).setdefault("xsignal_lengths", {})
    key = xsignal.name.lower()
    if key in cache:
        return cache[key]
    pins = _pin_map(index)
    result: dict = {"xsignal": xsignal.name, "pins": list(xsignal.pins)}
    missing = [pin for pin in xsignal.pins if pin.lower() not in pins]
    legs: list[dict] = []
    hops: list[str] = []
    nets: list[str] = []
    status = "ok"
    copper = barrel = 0.0
    barrel_known = True
    vias = 0
    layers: dict[int, float] = defaultdict(float)
    # Routed legs reaching each pin position, and the pad a leg ended on.
    leg_ends = [0] * len(xsignal.pins)
    end_pad: dict[int, int] = {}
    if missing:
        status = "pin_not_found"
        result["missing_pins"] = missing
    else:
        for position, (pin_a, pin_b) in enumerate(zip(xsignal.pins, xsignal.pins[1:])):
            rows_a, rows_b = pins[pin_a.lower()], pins[pin_b.lower()]
            common = (
                {row[2] for row in rows_a} & {row[2] for row in rows_b}
            ) - {None}
            if common:
                net_id = min(common)
                net_name = _net_name(index, net_id)
                if net_name not in nets:
                    nets.append(net_name)
                path = net_graph(index, net_id).shortest(
                    [row[0] for row in rows_a if row[2] == net_id],
                    [row[0] for row in rows_b if row[2] == net_id],
                )
                leg = {"from": pin_a, "to": pin_b, "net": net_name}
                leg.update(_path_fields(index, path))
                legs.append(leg)
                leg_ends[position] += 1
                leg_ends[position + 1] += 1
                if path.status != "ok":
                    status = "incomplete"
                    continue
                if path.end_pads:
                    end_pad.setdefault(position, path.end_pads[0])
                    end_pad.setdefault(position + 1, path.end_pads[1])
                copper += path.copper
                if path.barrel is None:
                    barrel_known = False
                else:
                    barrel += path.barrel
                vias += path.vias
                for layer, amount in path.layers.items():
                    layers[layer] += amount
            elif shared := {row[1] for row in rows_a} & {row[1] for row in rows_b}:
                hops.append(index.designator(min(shared)) or pin_a.rsplit("-", 1)[0])
            else:
                status = "broken_chain"
                result["broken_between"] = [pin_a, pin_b]
                break
    complete = status == "ok"
    pin_package = None
    if complete:
        # Pin/Package Length at the first and last pins and at pins only one
        # routed leg reaches (series-part pins); a fly-by pin the route passes
        # through (a leg in, a leg out) adds none.
        ppl = _pad_pin_package(index)
        last = len(xsignal.pins) - 1
        pin_package = 0.0
        for position, pin in enumerate(xsignal.pins):
            if position not in (0, last) and leg_ends[position] != 1:
                continue
            if position in end_pad:
                pin_package += ppl.get(end_pad[position], 0.0)
            else:
                pin_package += max(
                    (ppl.get(row[0], 0.0) for row in pins[pin.lower()]), default=0.0
                )
    result.update({
        "length_mil": _r(copper + barrel + pin_package) if complete else None,
        "copper_mil": _r(copper) if complete else None,
        "via_barrel_mil": _r(barrel) if complete and barrel_known else None,
        "pin_package_mil": _r(pin_package),
        "vias": vias if complete else None,
        "layers_mil": _layer_names(index, layers) if complete else {},
        "nets": nets,
        "legs": legs,
        "series_hops": hops,
        "status": status,
    })
    cache[key] = result
    return result


def xsignal_length(index, name: str) -> dict:
    """Length of an xSignal: its routed legs summed (series hops as 0) plus
    the Pin/Package Length of its end pins."""
    xsignal = index.xsignals.get(name.lower())
    if xsignal is None:
        suggestions = [
            item.name for item in index.pcb.xsignals if name.lower() in item.name.lower()
        ][:10]
        return {
            "error": "xsignal_not_found",
            "message": f"No xSignal named '{name}'.",
            "suggestions": suggestions,
        }
    return dict(_xsignal_measure(index, xsignal))


def _pair_measure(index, pair_key: str) -> dict:
    cache = _cache(index).setdefault("pair_lengths", {})
    if pair_key in cache:
        return cache[pair_key]
    display, positive, negative = index.rule_context.pairs[pair_key]
    sides = {}
    for side, net in (("positive", positive), ("negative", negative)):
        net_id = index.net_id(net)
        if net_id is None:
            sides[side] = {"net": net, "length_mil": None, "status": "net_not_found"}
            continue
        measured = _net_measure(index, net_id)
        sides[side] = {
            key: measured[key]
            for key in (
                "net", "measure", "length_mil", "copper_mil", "via_barrel_mil",
                "pin_package_mil", "vias", "pad_count", "status",
                "unrouted_manhattan_mil",
            )
            if key in measured
        }
    result: dict = {"pair": display, **sides}
    p_len, n_len = sides["positive"]["length_mil"], sides["negative"]["length_mil"]
    complete = (
        sides["positive"]["status"] == "ok" and sides["negative"]["status"] == "ok"
        and p_len is not None and n_len is not None
    )
    if complete:
        # Altium's DRC compares pairs by their P/N average; the longer leg is
        # what interactive tuning matches.
        result["length_mil"] = _r((p_len + n_len) / 2.0)
        result["max_mil"] = max(p_len, n_len)
        result["skew_mil"] = _r(abs(p_len - n_len))
        result["status"] = "ok"
    else:
        result["length_mil"] = None
        result["max_mil"] = None
        result["skew_mil"] = None
        statuses = {sides["positive"]["status"], sides["negative"]["status"]}
        result["status"] = "net_not_found" if "net_not_found" in statuses else "incomplete"
    result["length_basis"] = (
        "average of the positive and negative legs (the longer leg is reported as max)"
    )
    cache[pair_key] = result
    return result


def pair_lengths(index, pair: str) -> dict:
    """Both nets of a differential pair, the pair length (P/N average, as
    Altium's DRC uses it), the longer leg (max_mil) and skew."""
    key = pair.lower()
    if key not in index.rule_context.pairs:
        suggestions = [
            item.name for item in index.pcb.diff_pairs if key in item.name.lower()
        ][:10]
        return {
            "error": "pair_not_found",
            "message": f"No differential pair named '{pair}'.",
            "suggestions": suggestions,
        }
    return dict(_pair_measure(index, key))


# ---------- rule checks ----------


def _rule_settings(index, rule) -> tuple[dict, list[str]]:
    """Interpreted values of a Length / MatchedLengths rule, and the keys
    left uninterpreted."""
    values = rule_values(rule, index.signal_layers(), index.pcb.board.substacks)
    raw = {key.upper(): value for key, value in rule.properties.items()}

    def flag(name: str, key: str) -> bool:
        if name in values:
            return bool(values[name])
        return str(raw.get(key, "")).strip().upper() == "TRUE"

    def mil(name: str, key: str) -> float | None:
        if values.get(name) is not None:
            return values[name]
        return _parse_mil(raw.get(key))

    settings: dict = {"use_delay_units": flag("use_delay_units", "USEDELAYUNITS")}
    if rule.kind == "Length":
        settings["min_mil"] = mil("min_length_mil", "MINLIMIT")
        settings["max_mil"] = mil("max_length_mil", "MAXLIMIT")
    else:
        settings["tolerance_mil"] = mil("tolerance_mil", "TOLERANCE")
        settings["within_pair"] = flag("within_pair", "CHECKNETSINDIFFPAIR")
        settings["group_diff_pairs"] = flag("group_diff_pairs", "CHECKDIFFPAIRVSDIFFPAIR")
        settings["group_xsignals"] = flag("group_xsignals", "CHECKXSIGNALS")
        settings["group_other_nets"] = flag("group_other_nets", "CHECKOTHERS")
        source = values.get("target_source", raw.get("TARGETSOURCENAME"))
        settings["target_source"] = str(source).strip() if source and str(source).strip() else None
    unhandled = [
        key for key, value in raw.items()
        if key not in _HANDLED_RAW_KEYS and str(value).strip() not in ("", "FALSE")
    ]
    return settings, unhandled


def _scope_calls(text: str) -> set[str] | None:
    try:
        node = parse_scope(text)
    except ScopeSyntaxError:
        return None
    calls: set[str] = set()

    def walk(current):
        if current[0] == "call":
            calls.add(current[1])
        elif current[0] == "cmp":
            calls.add("?cmp")
        elif current[0] == "not":
            walk(current[1])
        elif current[0] in ("and", "or", "xor"):
            walk(current[1])
            walk(current[2])

    walk(node)
    return calls


def _rule_target(index, kind: str, name: str):
    targets = _cache(index).setdefault("targets", {})
    key = (kind, name.lower())
    if key not in targets:
        targets[key] = index.rule_target(name, kind)
    return targets[key]


def _candidates(index, kind: str) -> list[str]:
    if kind == "xsignal":
        return [xsignal.name for xsignal in index.pcb.xsignals]
    if kind == "diff_pair":
        return [pair.name for pair in index.pcb.diff_pairs]
    return [net for net in index.pcb.nets if net]


def _object_nets(index, kind: str, name: str) -> set[str]:
    ctx = index.rule_context
    if kind == "net":
        return {name.lower()}
    if kind == "diff_pair":
        entry = ctx.pairs.get(name.lower())
        return {entry[1].lower(), entry[2].lower()} if entry else set()
    if kind in _CLASS_KINDS:
        member_kind = _CLASS_KINDS[kind]
        nets: set[str] = set()
        for member in _class_members(index, kind, name):
            nets |= _object_nets(index, member_kind, member)
        return nets
    return set(ctx.xsignals.get(name.lower(), set()))


# Class target kinds -> (member kind, RuleContext table of the classes).
_CLASS_KINDS = {
    "xsignal_class": "xsignal", "net_class": "net", "diff_pair_class": "diff_pair",
}
_CLASS_TABLES = {
    "xsignal_class": "xsignal_classes", "net_class": "net_classes",
    "diff_pair_class": "pair_classes",
}


def _class_members(index, kind: str, name: str) -> set[str]:
    """Lower-case member names of a class; a superclass (All Nets, All
    xSignals...) holds every object of its kind."""
    ctx = index.rule_context
    member_kind = _CLASS_KINDS[kind]
    key = name.lower()
    if key in ctx.superclasses.get(member_kind, set()):
        return {item.lower() for item in _candidates(index, member_kind)}
    return set(getattr(ctx, _CLASS_TABLES[kind]).get(key, set()))


def _membership(index, rule, kinds: list[str]) -> dict:
    """Objects of the given kinds whose scope matches: yes / conditional."""
    ctx = index.rule_context
    yes: dict[str, list[str]] = {kind: [] for kind in kinds}
    conditional: list[dict] = []
    for kind in kinds:
        for name in _candidates(index, kind):
            target = _rule_target(index, kind, name)
            if target is None:
                continue
            verdict = evaluate_scope(rule.scope1, target, ctx)
            if verdict["matches"] == "yes":
                yes[kind].append(name)
            elif verdict["matches"] == "conditional":
                conditional.append({
                    "object": name, "kind": kind,
                    "conditions": verdict.get("conditions", []),
                })
    return {"yes": yes, "conditional": conditional}


def _member_row(index, kind: str, name: str) -> dict:
    """Measured length of one rule member, with its measurement status."""
    if kind == "xsignal":
        measured = _xsignal_measure(index, index.xsignals[name.lower()])
        row = {
            "object": measured["xsignal"], "kind": "xsignal",
            "length_mil": measured["length_mil"],
            "copper_mil": measured["copper_mil"],
            "via_barrel_mil": measured["via_barrel_mil"],
            "vias": measured["vias"],
            "nets": measured["nets"],
        }
        if measured["pin_package_mil"]:
            row["pin_package_mil"] = measured["pin_package_mil"]
        if measured["series_hops"]:
            row["series_hops"] = measured["series_hops"]
        if measured["status"] == "incomplete":
            row["incomplete_legs"] = [
                {key: leg[key] for key in ("from", "to", "net", "unrouted_manhattan_mil") if key in leg}
                for leg in measured["legs"]
                if leg["status"] != "ok"
            ]
        elif measured["status"] in ("pin_not_found", "broken_chain"):
            row["reason"] = measured["status"]
            for key in ("missing_pins", "broken_between"):
                if key in measured:
                    row[key] = measured[key]
        measure_status = measured["status"]
    elif kind == "diff_pair":
        measured = _pair_measure(index, name.lower())
        row = {
            "object": measured["pair"], "kind": "diff_pair",
            "length_mil": measured["length_mil"],
            "max_mil": measured["max_mil"],
            "positive_mil": measured["positive"]["length_mil"],
            "negative_mil": measured["negative"]["length_mil"],
            "skew_mil": measured["skew_mil"],
            "nets": [measured["positive"]["net"], measured["negative"]["net"]],
        }
        measure_status = measured["status"]
    else:
        measured = _net_measure(index, index.net_id(name))
        row = {
            "object": measured["net"], "kind": "net",
            "measure": measured["measure"],
            "length_mil": measured["length_mil"],
            "copper_mil": measured["copper_mil"],
            "via_barrel_mil": measured["via_barrel_mil"],
            "vias": measured["vias"],
            "pad_count": measured["pad_count"],
        }
        for key in ("pin_package_mil", "pin_package_excluded_mil"):
            if measured.get(key):
                row[key] = measured[key]
        if "unrouted_manhattan_mil" in measured:
            row["unrouted_manhattan_mil"] = measured["unrouted_manhattan_mil"]
        measure_status = measured["status"]
    if measure_status in ("ok",):
        row["status"] = "ok"
    elif measure_status in ("incomplete", "unrouted", "no_path"):
        row["status"] = "incomplete"
    else:
        row["status"] = "unknown"
        row.setdefault("reason", measure_status)
    return row


def _classify(row: dict, low: float | None, high: float | None) -> None:
    length = row["length_mil"]
    if row["status"] != "ok" or length is None:
        return
    outside = 0.0
    if low is not None and length < low - 1e-9:
        row["status"], outside = "too_short", low - length
    elif high is not None and length > high + 1e-9:
        row["status"], outside = "too_long", length - high
    if outside:
        row["outside_band_mil"] = _r(outside)
        if outside <= MARGINAL_MIL:
            row["marginal"] = True


_STATUS_RANK = {
    "too_short": 0, "too_long": 0, "skew_exceeded": 0,
    "incomplete": 1, "unknown": 2, "ok": 3,
}


def _sort_rows(rows: list[dict]) -> list[dict]:
    return sorted(
        rows,
        key=lambda row: (
            _STATUS_RANK.get(row["status"], 2),
            -(row.get("outside_band_mil") or 0.0),
            -(row.get("length_mil") or row.get("skew_mil") or 0.0),
            row["object"].lower(),
        ),
    )


def _summary(rows: list[dict], length_key: str = "length_mil") -> dict:
    measured = [
        row[length_key] for row in rows
        if row[length_key] is not None and row["status"] not in ("incomplete", "unknown")
    ]
    violations = [row for row in rows if _STATUS_RANK.get(row["status"]) == 0]
    summary = {
        "members": len(rows),
        "ok": sum(row["status"] == "ok" for row in rows),
        "violations": len(violations),
        "marginal_violations": sum(bool(row.get("marginal")) for row in violations),
        "incomplete": sum(row["status"] == "incomplete" for row in rows),
        "unknown": sum(row["status"] == "unknown" for row in rows),
    }
    if measured:
        summary["min_mil"] = min(measured)
        summary["max_mil"] = max(measured)
        summary["spread_mil"] = _r(max(measured) - min(measured))
    return summary


def _page(rows: list[dict], offset: int, limit: int) -> tuple[list[dict], dict]:
    offset = max(0, offset)
    limit = max(1, limit)
    page = rows[offset : offset + limit]
    meta = {"total": len(rows), "offset": offset, "has_more": offset + limit < len(rows)}
    if meta["has_more"]:
        meta["next_offset"] = offset + limit
    return page, meta


def _find_object(index, name: str, classes: bool = False) -> tuple[str, str] | None:
    """(kind, display name) of an xSignal, net or pair called name; with
    classes, then of an xSignal, net or differential-pair class."""
    key = name.lower()
    if key in index.xsignals:
        return "xsignal", index.xsignals[key].name
    if key in index.rule_context.net_names:
        return "net", index.rule_context.net_names[key]
    if key in index.rule_context.pairs:
        return "diff_pair", index.rule_context.pairs[key][0]
    if classes:
        for kind, member_kind in _CLASS_KINDS.items():
            for pcb_class in index.pcb.classes:
                if pcb_class.kind == member_kind and pcb_class.name.lower() == key:
                    return kind, pcb_class.name
    return None


def _member_kinds(rule, settings: dict, calls: set[str]) -> list[str]:
    uses_xsignals = bool(calls & _XSIGNAL_FUNCTIONS)
    only_xsignals = uses_xsignals and calls <= _XSIGNAL_FUNCTIONS
    kinds: list[str] = []
    if rule.kind == "Length":
        if uses_xsignals:
            kinds.append("xsignal")
        if not only_xsignals:
            kinds.append("net")
        return kinds
    if settings["group_xsignals"] and uses_xsignals:
        kinds.append("xsignal")
    if (settings["group_diff_pairs"] or settings["within_pair"]) and not only_xsignals:
        kinds.append("diff_pair")
    if settings["group_other_nets"] and not only_xsignals:
        kinds.append("net")
    return kinds


def _evaluate_rule(index, rule, memo: dict) -> dict:
    """Settings and scope membership of one rule (memoised per check call)."""
    key = id(rule)
    if key in memo:
        return memo[key]
    settings, unhandled = _rule_settings(index, rule)
    calls = _scope_calls(rule.scope1)
    entry: dict = {
        "settings": settings, "unhandled": unhandled, "calls": calls,
        "kinds": [], "members": [], "conditional": [],
    }
    memo[key] = entry
    if calls is None:
        return entry
    kinds = _member_kinds(rule, settings, calls)
    membership = _membership(index, rule, kinds)
    yes = membership["yes"]
    # Every matching object is a member, a pair's own nets included: Altium
    # checks nets against nets (pair legs among them), pairs against pairs
    # and xSignals against xSignals.
    members: list[tuple[str, str]] = []
    covered: set[str] = set()
    for kind in ("xsignal", "diff_pair", "net"):
        for name in yes.get(kind, []):
            members.append((kind, name))
            covered |= _object_nets(index, kind, name)
    conditional = [
        item for item in membership["conditional"]
        if not _object_nets(index, item["kind"], item["object"]) <= covered
    ]
    entry.update({"kinds": kinds, "members": members, "conditional": conditional})
    return entry


def _length_winners(index, memo: dict) -> dict[tuple[str, str], str]:
    """Length is a unary rule: per object only the highest-priority enabled
    rule whose scope matches it applies."""
    winners: dict[tuple[str, str], str] = {}
    for rule in sorted(
        (item for item in index.pcb.rules if item.kind == "Length" and item.enabled),
        key=lambda item: item.priority,
    ):
        for kind, name in _evaluate_rule(index, rule, memo)["members"]:
            winners.setdefault((kind, name.lower()), rule.name)
    return winners


def _check_rule(index, rule, memo: dict, winners: dict, target, offset, limit, problems_only) -> dict:
    out = rule_header(rule)
    drc_kinds = index.pcb.drc_rule_kinds
    if drc_kinds is not None:
        out["in_batch_drc"] = rule.kind_id in drc_kinds
    settings, unhandled = _rule_settings(index, rule)
    unknown: list[str] = [
        f"{rule.name}: rule key {key} is not interpreted." for key in unhandled
    ]
    if rule.kind == "Length":
        out["mode"] = "range"
        out["min_mil"] = settings["min_mil"]
        out["max_mil"] = settings["max_mil"]
    else:
        modes = []
        if settings["within_pair"]:
            modes.append("within_pair")
        if settings["group_diff_pairs"] or settings["group_xsignals"] or settings["group_other_nets"]:
            modes.append("group")
        out["mode"] = modes[0] if modes else None
        if len(modes) > 1:
            out["modes"] = modes
        out["tolerance_mil"] = settings["tolerance_mil"]
        out["group_checks"] = {
            "xsignals": settings["group_xsignals"],
            "diff_pairs": settings["group_diff_pairs"],
            "other_nets": settings["group_other_nets"],
        }
        if settings["target_source"]:
            out["target_source"] = settings["target_source"]
    if not rule.enabled:
        out["status"] = "disabled"
        return {"rule": out, "unknown": unknown, "involved": False}
    if rule.kind == "MatchedLengths" and out["mode"] is None:
        out["status"] = "no_checks_enabled"
        return {"rule": out, "unknown": unknown, "involved": False}
    entry = _evaluate_rule(index, rule, memo)
    stale = stale_references(rule.scope1, index.rule_context)
    if entry["calls"] is None:
        out["status"] = "scope_parse_error"
        unknown.append(f"{rule.name}: scope could not be parsed.")
        return {"rule": out, "unknown": unknown, "involved": False}
    out["member_kinds"] = entry["kinds"]
    members = entry["members"]
    if rule.kind == "Length":
        overridden = [
            (kind, name) for kind, name in members
            if winners.get((kind, name.lower())) not in (None, rule.name)
        ]
        if overridden:
            out["overridden"] = {
                "count": len(overridden),
                "objects": [
                    {"object": name, "kind": kind, "by_rule": winners[(kind, name.lower())]}
                    for kind, name in overridden[:_CONDITIONAL_LIMIT]
                ],
            }
            members = [item for item in members if item not in overridden]

    def involved(row_nets: set[str], name: str) -> bool:
        return (
            target is None
            or name.lower() == target["name"].lower()
            or name.lower() in target["members"]
            or bool(row_nets & target["nets"])
        )

    conditional = entry["conditional"]
    if target is not None:
        conditional = [
            item for item in conditional
            if involved(_object_nets(index, item["kind"], item["object"]), item["object"])
        ]
    if conditional:
        out["conditional_count"] = len(conditional)
        out["conditional"] = conditional[:_CONDITIONAL_LIMIT]
    if stale:
        out["stale_references"] = stale
    if settings["use_delay_units"]:
        out["status"] = "not_evaluated_delay_units"
        out["member_count"] = len(members)
        unknown.append(f"{rule.name}: delay-unit limits are not evaluated.")
        any_member = any(
            involved(_object_nets(index, kind, name), name) for kind, name in members
        )
        return {"rule": out, "unknown": unknown, "involved": any_member or bool(conditional)}
    if not members:
        out["status"] = "no_members"
        out["summary"] = {"members": 0}
        return {"rule": out, "unknown": unknown, "involved": bool(conditional)}

    # (container, key, rows): rows listed under container[key], paginated.
    sections: list[tuple[dict, str, list[dict]]] = []
    if rule.kind == "Length":
        rows = [_member_row(index, kind, name) for kind, name in members]
        for row in rows:
            _classify(row, settings["min_mil"], settings["max_mil"])
        out["summary"] = _summary(rows)
        sections.append((out, "members", rows))
    else:
        tolerance = settings["tolerance_mil"] or 0.0
        if "group" in (out.get("modes") or [out["mode"]]):
            source_reference = None
            if settings["target_source"]:
                found = _find_object(index, settings["target_source"])
                if found is None:
                    unknown.append(
                        f"{rule.name}: target source '{settings['target_source']}' "
                        "does not exist; the longest member is used."
                    )
                    out.setdefault("stale_references", []).append(
                        f"targetsource('{settings['target_source']}')"
                    )
                else:
                    source_row = _member_row(index, *found)
                    if source_row["status"] == "ok" and source_row["length_mil"] is not None:
                        length = source_row["length_mil"]
                        source_reference = {
                            "object": source_row["object"], "kind": source_row["kind"],
                            "length_mil": length, "source": "target_source",
                            "band_min_mil": _r(length - tolerance),
                            "band_max_mil": _r(length + tolerance),
                        }
                    else:
                        unknown.append(
                            f"{rule.name}: target source '{settings['target_source']}' "
                            "could not be measured; the longest member is used."
                        )
            # Altium compares each object type separately: nets (pair legs
            # included) against the longest net, pairs against the longest
            # pair, xSignals against the longest xSignal.
            groups: dict[str, dict] = {}
            for section, kind, enabled in (
                ("xsignals", "xsignal", settings["group_xsignals"]),
                ("diff_pairs", "diff_pair", settings["group_diff_pairs"]),
                ("nets", "net", settings["group_other_nets"]),
            ):
                names = [name for member_kind, name in members if member_kind == kind]
                if not enabled or not names:
                    continue
                rows = [_member_row(index, kind, name) for name in names]
                reference = dict(source_reference) if source_reference else None
                measured = [
                    row for row in rows
                    if row["status"] == "ok" and row["length_mil"] is not None
                ]
                if reference is None and measured:
                    longest = max(measured, key=lambda row: row["length_mil"])
                    reference = {
                        "object": longest["object"], "kind": longest["kind"],
                        "length_mil": longest["length_mil"], "source": "longest_member",
                        "band_min_mil": _r(longest["length_mil"] - tolerance),
                        "band_max_mil": longest["length_mil"],
                    }
                if reference is not None:
                    for row in rows:
                        if row["status"] == "ok" and row["length_mil"] is not None:
                            row["delta_to_target_mil"] = _r(
                                row["length_mil"] - reference["length_mil"]
                            )
                        _classify(row, reference["band_min_mil"], reference["band_max_mil"])
                groups[section] = {"reference": reference, "summary": _summary(rows)}
                sections.append((groups[section], "members", rows))
            out["groups"] = groups
            out["summary"] = {
                key: sum(group["summary"][key] for group in groups.values())
                for key in ("members", "ok", "violations", "marginal_violations",
                            "incomplete", "unknown")
            }
        if "within_pair" in (out.get("modes") or [out["mode"]]):
            pair_rows = []
            for kind, name in members:
                if kind != "diff_pair":
                    continue
                measured_pair = _pair_measure(index, name.lower())
                row = {
                    "object": measured_pair["pair"], "kind": "diff_pair",
                    "positive_net": measured_pair["positive"]["net"],
                    "negative_net": measured_pair["negative"]["net"],
                    "positive_mil": measured_pair["positive"]["length_mil"],
                    "negative_mil": measured_pair["negative"]["length_mil"],
                    "skew_mil": measured_pair["skew_mil"],
                    "nets": [measured_pair["positive"]["net"], measured_pair["negative"]["net"]],
                }
                if measured_pair["status"] != "ok":
                    row["status"] = "incomplete" if measured_pair["status"] == "incomplete" else "unknown"
                elif row["skew_mil"] > tolerance + 1e-9:
                    row["status"] = "skew_exceeded"
                    outside = row["skew_mil"] - tolerance
                    row["outside_band_mil"] = _r(outside)
                    if outside <= MARGINAL_MIL:
                        row["marginal"] = True
                else:
                    row["status"] = "ok"
                pair_rows.append(row)
            pair_summary = _summary(pair_rows, "skew_mil")
            pair_summary["pairs"] = pair_summary.pop("members")
            for key in ("min_mil", "spread_mil"):
                pair_summary.pop(key, None)
            if "max_mil" in pair_summary:
                pair_summary["max_skew_mil"] = pair_summary.pop("max_mil")
            out["pair_summary"] = pair_summary
            sections.append((out, "pairs", pair_rows))
            if not members or not pair_rows:
                out.setdefault("note", "Within-pair checks need differential pairs in scope.")
    out["status"] = "evaluated"
    any_involved = bool(conditional)
    for container, key, rows in sections:
        if target is not None:
            rows = [row for row in rows if involved({net.lower() for net in row.get("nets") or [row["object"]]}, row["object"])]
            any_involved = any_involved or bool(rows)
        if problems_only:
            rows = [row for row in rows if row["status"] != "ok"]
        page, meta = _page(_sort_rows(rows), offset, limit)
        for row in page:
            if row["kind"] == "net":
                row.pop("nets", None)
        container[key] = page
        container[f"{key}_page"] = meta
    if problems_only:
        out["listing"] = "problems_only"
    return {"rule": out, "unknown": unknown, "involved": any_involved or target is None}


def check_length_rules(
    index,
    rule: str | None = None,
    target: str | None = None,
    offset: int = 0,
    limit: int = DEFAULT_LIMIT,
    problems_only: bool | None = None,
) -> dict:
    """Check Length and MatchedLengths rules against measured lengths.

    rule names one rule; target (a net, differential pair, xSignal, or an
    xSignal, net or differential-pair class) keeps only the rules and members
    that involve it. A MatchedLengths group is checked per object type, each
    under groups (xsignals, diff_pairs, nets) with its own reference; the
    rule summary adds them up. Members are paginated per rule and group with
    offset/limit. problems_only (default: when neither rule nor target is
    given) lists only violations, incomplete and unknown members; summaries
    always count every member.
    """
    length_rules = sorted(
        (item for item in index.pcb.rules if item.kind in LENGTH_RULE_KINDS),
        key=lambda item: (item.kind, item.priority, item.name.lower()),
    )
    selected = length_rules
    if rule is not None:
        selected = [item for item in length_rules if item.name.lower() == rule.lower()]
        if not selected:
            return {
                "error": "rule_not_found",
                "message": f"No Length or MatchedLengths rule named '{rule}'.",
                "suggestions": [
                    item.name for item in length_rules if rule.lower() in item.name.lower()
                ][:10] or [item.name for item in length_rules][:10],
            }
    target_info = None
    if target is not None:
        found = _find_object(index, target, classes=True)
        if found is None:
            return {
                "error": "target_not_found",
                "message": (
                    f"No net, differential pair, xSignal or xSignal, net or "
                    f"differential-pair class named '{target}'."
                ),
            }
        kind, display = found
        target_info = {
            "name": display, "kind": kind, "nets": _object_nets(index, kind, display),
            "members": (
                _class_members(index, kind, display) if kind in _CLASS_KINDS else set()
            ),
        }
    if problems_only is None:
        problems_only = rule is None and target is None
    memo: dict = {}
    winners = _length_winners(index, memo)
    rules_out = []
    unknown: list[str] = []
    for item in selected:
        checked = _check_rule(
            index, item, memo, winners, target_info, offset, limit, problems_only
        )
        if target_info is not None and not checked["involved"]:
            continue
        rules_out.append(checked["rule"])
        unknown.extend(checked["unknown"])
    if stack_z(index) is None:
        unknown.append(
            "Via barrel length is unknown: the stackup has no layer "
            "thicknesses (via_barrel_mil is null; lengths exclude barrels)."
        )
    result: dict = {"rule_count": len(rules_out)}
    if target_info is not None:
        result["target"] = {"name": target_info["name"], "kind": target_info["kind"]}
    result.update({
        "rules": rules_out,
        "measure": MEASURE,
        "approximations": list(APPROXIMATIONS),
        "unknown": list(dict.fromkeys(unknown)),
    })
    if not length_rules:
        result["note"] = "The board has no Length or MatchedLengths rules."
    return result
