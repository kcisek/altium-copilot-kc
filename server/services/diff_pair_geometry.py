"""Differential-pair routing geometry (roadmap #28): per pair and signal layer, the coupled
gap and the widths of the two nets' tracks and arcs, the uncoupled length and the layer
changes, against the DiffPairsRouting rule or an impedance profile, offline.

- Pairs: the PCB's differential pairs, and nets named as complementary legs that are not
  one (diff_pair_checks.inferred_pairs: X_P / X_N, D+ / D-, ...), marked inferred. No
  DiffPairsRouting rule applies to an inferred pair, so its findings are info.
- Coupling: each leg's tracks and arcs on a signal layer, chained where they meet
  (return_path.chains), are sampled every SAMPLE_STEP_MIL along their centreline (an arc on
  its true arc, not its chords). A sample is coupled when a track or arc of the other leg on
  the same layer runs parallel to it (tangents within ANGLE_TOLERANCE_DEG), the sample's
  perpendicular foot falls on it (or past one of its corners by at most CORNER_EXTENSION of
  the centre distance: the inner leg of a bend turns first) and the edge-to-edge gap (centre
  distance minus both half widths) is at most the coupling distance: COUPLING_GAP_FACTOR x
  the layer's max gap (else its preferred gap), at least COUPLING_WIDTH_FACTOR x the pair's
  width there (COUPLING_WIDTH_ONLY_FACTOR x without a gap limit), or coupling_distance=.
  Concentric arcs couple along their common angle, an arc and a track where their tangents
  agree. A coupled stretch is a run of coupled samples along a chain.
- Gap: along each stretch, the gap against the layer's min and max gap (GAP_TOLERANCE_MIL
  allowed); runs below or above are gap_below_min / gap_above_max, a warning when at least
  min_stretch long (DEFAULT_MIN_STRETCH_MIL); info when shorter, at a breakout (at most
  BREAKOUT_MAX_MIL long, starting at most BREAKOUT_MAX_MIL along the leg from the copper
  edge of the pad or via its chain ends on, before the first stretch within the range),
  marginal (at most MARGINAL_GAP_MIL beyond the limit) or judged against limits assumed
  from a profile no rule links to the pair. gap_varies (info): the 5th to 95th percentile
  spread of the coupled gap on a layer above GAP_VARY_MIN_MIL and GAP_VARY_FRACTION of its
  median.
- Width: each track and arc of both legs against the layer's min and max width
  (width_out_of_range, warning, an arc counted once; check_rule_compliance makes the same
  per-object check under DiffPairsRouting, without the profile fallbacks, so a finding
  against the rule's own width says also_reported_by check_rule_compliance).
- Uncoupled length: per leg, its routed length on signal layers less the length coupled
  within the gap constraints (a gap within min..max, as Altium's "Uncoupled Length using the
  Gap Constraints"), or less its coupled length when no gap limit is known. Split into
  at_pads / at_vias (an uncoupled run from a chain end on a pad or via, inside its copper
  and up to BREAKOUT_MAX_MIL beyond its edge: its breakout) and elsewhere (the rest).
  uncoupled_length_high: the longer leg above the rule's Max Uncoupled Length (warning), or,
  without one, above UNCOUPLED_FRACTION of its routed length and UNCOUPLED_MIN_MIL (info).
- Layer changes: a via whose copper joins a leg's routing on two or more signal layers.
  Each via of one leg is paired with the nearest via of the other leg joining at least two
  of the same layers within via_distance (DEFAULT_VIA_DISTANCE_MIL); one left over is
  unmatched_layer_change (warning).
- Routing: pair_not_routed (neither leg has a track) and partially_routed (one leg has
  none, or a leg's copper does not join all its pads), info.

Limits: for each track and arc, the DiffPairsRouting rule that applies to it (scope and
priority, decided per object as check_rule_compliance does) with its per-layer min,
preferred and max width and gap and its Max Uncoupled Length; a profile-driven rule's
impedance profile fills what the rule leaves out on a layer. Without a rule, the board's only
differential impedance profile solving the layer gives its min and max width and gap, as an
assumption (its findings are info). Else the pair is measured only. Pure logic over a
PcbIndex, no MCP; lengths in mil, locations relative to the board origin.
"""

import math
from collections import Counter, defaultdict

try:
    from services.diff_pair_checks import inferred_pairs
    from services.impedance import impedance_profiles
    from services.pcb_compliance import EPS_MIL, object_id
    from services.pcb_compliance import _Checker as RuleChecker
    from services.pcb_geometry import in_arc_span
    from services.pcb_length import net_graph, net_length, pad_contains
    from services.reference_planes import Grid
    from services.return_path import SNAP_MIL, ChainSegment, chains, length_text
except ImportError:
    from server.services.diff_pair_checks import inferred_pairs
    from server.services.impedance import impedance_profiles
    from server.services.pcb_compliance import EPS_MIL, object_id
    from server.services.pcb_compliance import _Checker as RuleChecker
    from server.services.pcb_geometry import in_arc_span
    from server.services.pcb_length import net_graph, net_length, pad_contains
    from server.services.reference_planes import Grid
    from server.services.return_path import SNAP_MIL, ChainSegment, chains, length_text

MIL_PER_MM = 1 / 0.0254
CHECKS = ("gap_below_min", "gap_above_max", "width_out_of_range", "gap_varies",
          "uncoupled_length_high", "unmatched_layer_change", "pair_not_routed",
          "partially_routed")
_CHECK_ALIASES = {
    "gap": ("gap_below_min", "gap_above_max", "gap_varies"),
    "gaps": ("gap_below_min", "gap_above_max", "gap_varies"),
    "width": ("width_out_of_range",),
    "widths": ("width_out_of_range",),
    "uncoupled": ("uncoupled_length_high",),
    "vias": ("unmatched_layer_change",),
    "layer_changes": ("unmatched_layer_change",),
    "routing": ("pair_not_routed", "partially_routed"),
}
_CHECK_RANK = {check: rank for rank, check in enumerate(CHECKS)}
_SEVERITY_RANK = {"warning": 0, "info": 1}

SAMPLE_STEP_MIL = 2.0
ANGLE_TOLERANCE_DEG = 10.0
_SIN_TOLERANCE = math.sin(math.radians(ANGLE_TOLERANCE_DEG))
# The foot of a sample may fall this share of the centre distance past the end of the
# other leg's track where that track turns into another (tan 22.5 deg = 0.414: the inner
# leg of a 45 degree bend turns that much earlier); the gap is then measured to the
# track's line.
CORNER_EXTENSION = 0.45
BEND_MAX_DEG = 50.0  # a turn of the routing up to this is a bend (a 45 degree corner)
_COS_BEND = math.cos(math.radians(BEND_MAX_DEG))
COUPLING_GAP_FACTOR = 3.0
COUPLING_WIDTH_FACTOR = 2.0
COUPLING_WIDTH_ONLY_FACTOR = 3.0  # x the width, when no gap limit is known
COUPLING_MIN_MIL = 5.0
GAP_TOLERANCE_MIL = 0.1
# A stretch whose worst gap is at most this far beyond the limit is marginal (info): a
# rounding of the routed gap (0.125 mm against 0.12 mm), not a defect of the impedance; it
# still counts as uncoupled.
MARGINAL_GAP_MIL = 0.25
DEFAULT_MIN_STRETCH_MIL = 20.0
MIN_DEVIATION_MIL = SAMPLE_STEP_MIL  # shorter runs outside the gap range are not listed
# Breakouts, where the legs fan out to the pins' or vias' pitch: a run outside the gap range
# at most this long, starting at most this far along the leg beyond the copper edge of the
# pad or via its chain ends on and before the first stretch within the range, is info; and
# an uncoupled run at a pad or via, inside its copper and up to this far beyond, counts as
# that pad's or via's breakout (at_pads / at_vias).
BREAKOUT_MAX_MIL = 50.0
GAP_VARY_MIN_MIL = 1.0
GAP_VARY_FRACTION = 0.2
GAP_VARY_MIN_LENGTH_MIL = 200.0  # coupled length in long stretches a spread is judged on
UNCOUPLED_FRACTION = 0.25
UNCOUPLED_MIN_MIL = 200.0
DEFAULT_VIA_DISTANCE_MIL = 2.0 * MIL_PER_MM
STRETCHES_LISTED = 10
PAIRS_LISTED = 50
CACHE_SIZE = 4
_LIMIT_FIELDS = ("min_width", "preferred_width", "max_width", "min_gap", "preferred_gap",
                 "max_gap")
_PROFILE_FIELDS = {"min_width": "min_width_mil", "preferred_width": "width_mil",
                   "max_width": "max_width_mil", "min_gap": "min_gap_mil",
                   "preferred_gap": "gap_mil", "max_gap": "max_gap_mil"}
_SEGMENT_SQL = (
    "SELECT * FROM prims WHERE net_id = ? AND kind IN ('track', 'arc') "
    "AND pour_id IS NULL AND poured_by IS NULL AND layer BETWEEN 1 AND 32 ORDER BY id"
)

def _r(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(value, digits)


def _approximations(units: str) -> list[str]:
    return [
        f"Tracks and arcs are sampled every {length_text(SAMPLE_STEP_MIL, units)} (arcs on "
        "the true arc): stretch lengths and ends are good to about one step.",
        "A sample is coupled when the other leg runs parallel (within "
        f"{ANGLE_TOLERANCE_DEG:g} degrees) beside it with its perpendicular foot on that "
        f"leg's track or arc; at a bend of up to {BEND_MAX_DEG:g} degrees the inner leg's "
        "track is extended past its corner, so 45 degree bends stay coupled. Sharper "
        "corners, neck-downs and fan-outs are uncoupled for about one gap.",
        "Uncoupled length is the leg's routed length on signal layers (its tracks and arcs, "
        "stubs included) less its length coupled within the rule's gap range; Altium's DRC "
        "tolerance for 'within the gap constraints' is not documented (here "
        f"{length_text(GAP_TOLERANCE_MIL, units)}), so a marginal stretch (at most "
        f"{length_text(MARGINAL_GAP_MIL, units)} beyond the limit, info) still counts as "
        "uncoupled. The longer leg is compared with Max Uncoupled Length. at_pads and "
        "at_vias hold the part of an uncoupled run at a pad or via inside its copper and up "
        f"to {length_text(BREAKOUT_MAX_MIL, units)} beyond its edge (its breakout); the "
        "rest of the run is elsewhere.",
        "A layer change is a via whose copper joins the leg's tracks on two or more signal "
        "layers (via-in-pad included); through-hole pads are not counted.",
    ]


# ---------- geometry ----------


class _Seg:
    """One track, or one chord of an arc measured on its arc, of a leg on one layer."""

    __slots__ = ("row", "layer", "x1", "y1", "x2", "y2", "width", "half", "length",
                 "arc", "a1", "sweep", "ux", "uy", "planar", "corner_start", "corner_end",
                 "limits")

    def __init__(self, row):
        self.row = row
        self.layer = row["layer"]
        self.x1, self.y1, self.x2, self.y2 = row["x1"], row["y1"], row["x2"], row["y2"]
        self.width = row["width"] or 0.0
        self.half = self.width / 2.0
        dx, dy = self.x2 - self.x1, self.y2 - self.y1
        self.planar = math.hypot(dx, dy)
        self.ux, self.uy = (dx / self.planar, dy / self.planar) if self.planar else (1.0, 0.0)
        self.arc = None
        self.a1 = self.sweep = 0.0
        radius = row["arc_radius"] if row["kind"] == "arc" else None
        if radius and row["arc_x"] is not None and row["arc_y"] is not None:
            cx, cy = row["arc_x"], row["arc_y"]
            self.arc = (cx, cy, radius, self.x1, self.y1, self.x2, self.y2)
            self.a1 = math.atan2(self.y1 - cy, self.x1 - cx)
            a2 = math.atan2(self.y2 - cy, self.x2 - cx)
            self.sweep = (a2 - self.a1 + math.pi) % (2.0 * math.pi) - math.pi
            self.length = abs(self.sweep) * radius
        else:
            self.length = self.planar
        self.corner_start = self.corner_end = False
        self.limits: dict = {}

    def point(self, fraction: float) -> tuple[float, float, float, float]:
        """(x, y, tangent x, tangent y) at a fraction of the way from end 1 to end 2."""
        if self.arc is not None:
            cx, cy, radius = self.arc[0], self.arc[1], self.arc[2]
            angle = self.a1 + self.sweep * fraction
            cosine, sine = math.cos(angle), math.sin(angle)
            return cx + radius * cosine, cy + radius * sine, -sine, cosine
        return (self.x1 + (self.x2 - self.x1) * fraction,
                self.y1 + (self.y2 - self.y1) * fraction, self.ux, self.uy)

    def heading(self, at_end: bool) -> tuple[float, float]:
        """Unit direction of travel from end 1 to end 2, at end 2 (at_end) or at end 1."""
        if self.arc is None:
            return self.ux, self.uy
        angle = self.a1 + (self.sweep if at_end else 0.0)
        sign = 1.0 if self.sweep >= 0.0 else -1.0
        return -math.sin(angle) * sign, math.cos(angle) * sign

    def foot(self, px: float, py: float):
        """(x, y, tangent x, tangent y, distance, extended) of the foot of the perpendicular
        from (px, py) on this centreline: on the track or arc, or on a track's line up to
        CORNER_EXTENSION of the distance past an end where the leg bends into another track
        or arc (extended). None when the sample is beside no part of it."""
        if self.arc is not None:
            cx, cy, radius = self.arc[0], self.arc[1], self.arc[2]
            vx, vy = px - cx, py - cy
            apart = math.hypot(vx, vy)
            if apart > 0 and in_arc_span(px, py, self.arc):
                return (cx + radius * vx / apart, cy + radius * vy / apart, -vy / apart,
                        vx / apart, abs(apart - radius), False)
            return None
        ux, uy = self.ux, self.uy
        rx, ry = px - self.x1, py - self.y1
        along = rx * ux + ry * uy
        across = abs(rx * uy - ry * ux)
        extended = False
        if along < 0.0:
            if not (self.corner_start and -along <= CORNER_EXTENSION * across):
                return None
            extended = True
        elif along > self.planar:
            if not (self.corner_end and along - self.planar <= CORNER_EXTENSION * across):
                return None
            extended = True
        return self.x1 + along * ux, self.y1 + along * uy, ux, uy, across, extended


def _mark_corners(segments: list[_Seg]) -> None:
    """Flag each end of a leg's track where the leg bends into another of its tracks or
    arcs by at most BEND_MAX_DEG (a bend of the routing, not a branch or a breakout)."""
    ends: dict[tuple[int, int], list[tuple[_Seg, bool]]] = defaultdict(list)

    def key(x: float, y: float) -> tuple[int, int]:
        return round(x / SNAP_MIL), round(y / SNAP_MIL)

    for segment in segments:
        if segment.length >= SNAP_MIL:
            ends[key(segment.x1, segment.y1)].append((segment, False))
            ends[key(segment.x2, segment.y2)].append((segment, True))
    for segment in segments:
        flags = []
        for at_end in (False, True):
            point = key(segment.x2, segment.y2) if at_end else key(segment.x1, segment.y1)
            hx, hy = segment.heading(at_end)
            if not at_end:  # leaving through end 1 is travelling backwards
                hx, hy = -hx, -hy
            bend = False
            for other, other_end in ends.get(point, ()):
                if other is segment:
                    continue
                ox, oy = other.heading(other_end)
                if other_end:  # entering through end 2 is travelling backwards
                    ox, oy = -ox, -oy
                if hx * ox + hy * oy >= _COS_BEND:
                    bend = True
            flags.append(bend)
        segment.corner_start, segment.corner_end = flags


def _grid(segments: list[_Seg], reach: float) -> Grid:
    """The other leg's segments by cell (reference_planes.Grid), each in every cell its
    copper can reach a sample within reach of."""
    grid = Grid(max(10.0, reach))
    grow = reach * (1.0 + CORNER_EXTENSION)
    for segment in segments:
        xs, ys = [segment.x1, segment.x2], [segment.y1, segment.y2]
        if segment.arc is not None:
            for step in range(1, 4):
                x, y, _, _ = segment.point(step / 4.0)
                xs.append(x)
                ys.append(y)
        grid.add((min(xs) - grow, min(ys) - grow, max(xs) + grow, max(ys) + grow), segment)
    return grid


def _couple(px, py, tx, ty, half, candidates, reach) -> tuple[float, _Seg, bool] | None:
    """(edge-to-edge gap, segment, extended) of the closest candidate running parallel
    beside the sample, its gap at most reach; None when none does. A foot on a track or arc
    wins over one past a corner (extended)."""
    best = None
    for other in candidates:
        foot = other.foot(px, py)
        if foot is None:
            continue
        _, _, ox, oy, distance, extended = foot
        if abs(tx * oy - ty * ox) > _SIN_TOLERANCE:
            continue
        gap = distance - half - other.half
        if gap > reach:
            continue
        if best is None or (extended, gap) < (best[2], best[0]):
            best = (gap, other, extended)
    return best


class _Walked:
    """One chain of a leg on a layer: its samples, what its two ends lie on (a pad or via
    of the leg, else None) and how much of the chain lies inside that pad or via."""

    __slots__ = ("chain", "samples", "start", "end", "before", "total", "start_inside",
                 "end_inside")

    def __init__(self, chain, samples, start, end, start_inside=0.0, end_inside=0.0):
        self.chain, self.samples, self.start, self.end = chain, samples, start, end
        self.start_inside, self.end_inside = start_inside, end_inside
        # Length of the chain's samples before each sample, and all of it.
        self.before = []
        running = 0.0
        for sample in samples:
            self.before.append(running)
            running += sample.weight
        self.total = running

    def from_start(self, number: int) -> float:
        """Length along the chain from its start to where sample number begins."""
        return self.before[number]

    def to_end(self, number: int) -> float:
        """Length along the chain from where sample number ends to the chain's end."""
        return self.total - self.before[number] - self.samples[number].weight

    def within_span(self) -> tuple[int, int]:
        """Indices of the first and last sample coupled within the gap constraints
        (len, -1 when none is)."""
        numbers = [number for number, sample in enumerate(self.samples) if sample.within]
        return (numbers[0], numbers[-1]) if numbers else (len(self.samples), -1)


class _Sample:
    __slots__ = ("x", "y", "weight", "segment", "gap", "partner", "within")

    def __init__(self, x, y, weight, segment):
        self.x, self.y, self.weight, self.segment = x, y, weight, segment
        self.gap = None
        self.partner = None
        self.within = False  # coupled within the gap constraints


def _inside(samples: list[_Sample], kind: str | None, feature: dict | None,
            layer: int) -> float:
    """Length of the samples, from the first on, whose centre lies on the pad's or via's
    copper (a breakout is measured from that copper's edge)."""
    if kind is None:
        return 0.0
    total = 0.0
    for sample in samples:
        if kind == "pad":
            inside = pad_contains(feature, sample.x, sample.y, 0.01, layer)
        else:
            inside = math.hypot(sample.x - feature["x"], sample.y - feature["y"]) <= \
                feature["r"] + 0.01
        if not inside:
            break
        total += sample.weight
    return total


# ---------- percentiles ----------


def _weighted_percentile(values: list[tuple[float, float]], fraction: float) -> float:
    """values: (value, weight), sorted by value."""
    total = sum(weight for _, weight in values)
    target = total * fraction
    running = 0.0
    for value, weight in values:
        running += weight
        if running >= target:
            return value
    return values[-1][0]


# ---------- pairs and selection ----------


class _Pair:
    __slots__ = ("name", "positive", "negative", "inferred", "style", "classes", "key")

    def __init__(self, name, positive, negative, inferred=False, style=None, classes=()):
        self.name, self.positive, self.negative = name, positive, negative
        self.inferred, self.style, self.classes = inferred, style, list(classes)
        self.key = name.lower()


def _board_pairs(index) -> tuple[list[_Pair], Counter]:
    """Declared pairs whose two legs are different board nets, then inferred pairs."""
    names = {name.lower() for name in index.pcb.nets if name}
    pairs: list[_Pair] = []
    skipped: Counter = Counter()
    for pair in index.pcb.diff_pairs:
        if pair.positive_net.lower() not in names or pair.negative_net.lower() not in names:
            skipped["leg_not_on_board"] += 1
            continue
        if pair.positive_net.lower() == pair.negative_net.lower():
            skipped["same_net"] += 1
            continue
        pairs.append(_Pair(pair.name, pair.positive_net, pair.negative_net,
                           classes=index.pair_classes_of(pair.name)))
    for candidate in inferred_pairs(index):
        positive, negative = candidate["positive"], candidate["negative"]
        pairs.append(_Pair(f"{positive} / {negative}", positive, negative, inferred=True,
                           style=candidate["style"]))
    return pairs, skipped


def _select(index, pairs: list[_Pair], names, classes) -> tuple[list[_Pair], dict | None]:
    if not names and not classes:
        return pairs, None
    ctx = index.rule_context
    chosen: dict[str, _Pair] = {}
    unknown = []
    for name in names or []:
        key = name.lower()
        found = [pair for pair in pairs
                 if key in (pair.key, pair.positive.lower(), pair.negative.lower())]
        if not found:
            unknown.append(name)
        for pair in found:
            chosen.setdefault(pair.key, pair)
    if unknown:
        suggestions = sorted({
            pair.name for pair in pairs for name in unknown
            if name.lower() in pair.key or name.lower() in pair.positive.lower()
            or name.lower() in pair.negative.lower()
        }, key=str.lower)[:10]
        return [], {"error": "pair_not_found",
                    "message": "No differential pair (declared, or nets named as one) "
                    "named or with a net named " + ", ".join(f"'{name}'" for name in unknown)
                    + ".", "suggestions": suggestions}
    unknown_classes = []
    for name in classes or []:
        key = name.lower()
        if key in ctx.superclasses.get("diff_pair", set()):
            members = [pair for pair in pairs if not pair.inferred]
        elif key in ctx.superclasses.get("net", set()):
            members = list(pairs)
        elif key in ctx.pair_classes:
            names_in = ctx.pair_classes[key]
            members = [pair for pair in pairs if pair.key in names_in]
        elif key in ctx.net_classes:
            nets_in = ctx.net_classes[key]
            members = [pair for pair in pairs if pair.positive.lower() in nets_in
                       and pair.negative.lower() in nets_in]
        else:
            unknown_classes.append(name)
            continue
        for pair in members:
            chosen.setdefault(pair.key, pair)
    if unknown_classes:
        return [], {"error": "net_class_not_found",
                    "message": "No differential-pair or net class named "
                    + ", ".join(f"'{name}'" for name in unknown_classes) + ".",
                    "classes": sorted({item.name for item in index.pcb.classes
                                       if item.kind in ("diff_pair", "net")},
                                      key=str.lower)[:50]}
    return [pair for pair in pairs if pair.key in chosen], None


def _parse_checks(checks) -> tuple[list[str] | None, dict | None]:
    if not checks:
        return list(CHECKS), None
    if isinstance(checks, str):
        checks = [part for part in checks.replace(",", " ").split() if part]
    selected: list[str] = []
    invalid = []
    for check in checks:
        name = str(check).strip().lower()
        expanded = _CHECK_ALIASES.get(name, (name,))
        for item in expanded:
            if item not in CHECKS:
                invalid.append(str(check))
            elif item not in selected:
                selected.append(item)
    if invalid:
        return None, {"error": "invalid_checks",
                      "message": f"Unknown checks: {', '.join(invalid)}.",
                      "valid_checks": list(CHECKS), "aliases": sorted(_CHECK_ALIASES)}
    return [check for check in CHECKS if check in selected], None


# ---------- the check ----------


class _Check:
    def __init__(self, index, layer: int | None, checks: set[str],
                 coupling_mil: float | None, min_stretch: float, via_distance: float,
                 units: str):
        self.index = index
        self.layer = layer
        self.checks = checks
        self.coupling_mil = coupling_mil
        self.min_stretch = min_stretch
        self.via_distance = via_distance
        self.units = units
        board = index.pcb.board
        self.origin = (board.origin_x or 0.0, board.origin_y or 0.0)
        self.signal = index.signal_layers()
        self.order = {layer_id: number for number, layer_id in enumerate(self.signal)}
        self.rules = RuleChecker(index, 1)
        self.has_rules = any(rule.enabled for rule in self.rules.rules.get(
            "DiffPairsRouting", []))
        self.profiles = [profile for profile in impedance_profiles(index)
                         if profile["type"] == "differential"]
        self.rule_profiles = {
            entry["rule"]: profile for profile in self.profiles
            for entry in profile["used_by_rules"] if entry["kind"] == "DiffPairsRouting"}
        self._limits: dict = {}
        self.findings: list[dict] = []
        self.rows: list[dict] = []
        self.stats: Counter = Counter()
        self.limit_sources: Counter = Counter()
        self.undecided = 0
        self.requested = False  # pairs named by the caller (pair= or net_class=)

    # -- helpers --

    def at(self, x: float, y: float) -> dict:
        return {"x_mil": round(x - self.origin[0], 2), "y_mil": round(y - self.origin[1], 2)}

    def layer_name(self, layer: int) -> str:
        return self.signal.get(layer) or self.index.layer_name(layer)

    def text(self, mil: float) -> str:
        return length_text(mil, self.units)

    def profile_layer(self, profile: dict, layer_name: str) -> dict | None:
        rows = [row for row in profile["layers"] if row["layer"].lower() == layer_name.lower()]
        plain = [row for row in rows if "substack" not in row]
        return (plain or rows or [None])[0]

    def limits(self, rule, layer: int) -> dict:
        """Width and gap limits on a layer under a rule (None: no rule applies)."""
        key = (None if rule is None else id(rule), layer)
        if key in self._limits:
            return self._limits[key]
        name = self.layer_name(layer)
        out: dict = {}
        source = "none"
        if rule is not None:
            values = self.rules.values(rule)
            entry = (values.get("layers_mil") or {}).get(name) or {}
            out = {field: entry[field] for field in _LIMIT_FIELDS if field in entry}
            out["rule"] = rule.name
            source = "rule"
            if values.get("max_uncoupled_length_mil") is not None:
                out["max_uncoupled_length"] = values["max_uncoupled_length_mil"]
            profile = self.rule_profiles.get(rule.name) if values.get(
                "impedance_profile_driven") else None
            if profile is not None:
                solved = self.profile_layer(profile, name) or {}
                filled = []
                for field, profile_key in _PROFILE_FIELDS.items():
                    if field not in out and solved.get(profile_key) is not None:
                        out[field] = solved[profile_key]
                        filled.append(field)
                out["profile"] = profile["name"]
                if filled:
                    source = "rule_and_profile"
                    out["from_profile"] = filled
        else:
            solving = [(profile, self.profile_layer(profile, name)) for profile in
                       self.profiles]
            solving = [(profile, row) for profile, row in solving if row is not None]
            if len(solving) == 1:
                profile, solved = solving[0]
                out = {field: solved[profile_key] for field, profile_key in
                       _PROFILE_FIELDS.items() if solved.get(profile_key) is not None}
                out["profile"] = profile["name"]
                source = "layer_profile"
        out["source"] = source
        self._limits[key] = out
        return out

    def pair_rule(self, pair: _Pair):
        """The DiffPairsRouting rule of the pair as a whole (its scope decided without an
        object), or None."""
        if pair.inferred or not self.has_rules:
            return None
        return self.rules.resolver("DiffPairsRouting", pair.positive, pair.name).resolve(
            None)[0]

    def resolve(self, pair: _Pair, net: str, segments: list[_Seg]) -> None:
        """The limits of each segment from the rule applying to it."""
        if pair.inferred or not self.has_rules:
            for segment in segments:
                segment.limits = self.limits(None, segment.layer)
            return
        resolver = self.rules.resolver("DiffPairsRouting", net, pair.name)
        per_object = "conditional" in resolver.net_level
        for segment in segments:
            facts = self.index.object_facts(segment.row) if per_object else None
            rule, undecided = resolver.resolve(facts)
            if undecided:
                self.undecided += 1
            segment.limits = self.limits(rule, segment.layer)

    @staticmethod
    def _within(sample: _Sample) -> bool:
        limits = sample.segment.limits
        low, high = limits.get("min_gap"), limits.get("max_gap")
        if low is None and high is None:
            return True
        if low is not None and sample.gap < low - GAP_TOLERANCE_MIL:
            return False
        return high is None or sample.gap <= high + GAP_TOLERANCE_MIL

    def reach(self, layer_limits: dict, widths: list[float]) -> float:
        if self.coupling_mil is not None:
            return self.coupling_mil
        width = layer_limits.get("preferred_width") or layer_limits.get("max_width") or max(
            widths, default=0.0)
        gap = layer_limits.get("max_gap") or layer_limits.get("preferred_gap")
        if gap:
            return max(COUPLING_GAP_FACTOR * gap, COUPLING_WIDTH_FACTOR * width,
                       COUPLING_MIN_MIL)
        return max(COUPLING_WIDTH_ONLY_FACTOR * width, COUPLING_MIN_MIL)

    def walk(self, own: list[_Seg], other: list[_Seg], reach: float, graph,
             layer: int) -> list[_Walked]:
        """Samples of one leg on a layer, chain by chain, each coupled to the other leg or
        not."""
        if not own:
            return []
        by_row = {segment.row["id"]: segment for segment in own}
        grid = _grid(other, reach + max((segment.half for segment in own), default=0.0)
                     + max((segment.half for segment in other), default=0.0)) if other else None
        walked = []
        # A track shorter than the snap (a dot) has no direction to couple along, and would
        # break the chain it sits on (as in return_path).
        for chain in chains([ChainSegment(segment.row) for segment in own
                             if segment.length > SNAP_MIL]):
            samples: list[_Sample] = []
            for part, backwards in chain.parts:
                segment = by_row[part.row]
                count = max(1, math.ceil(segment.length / SAMPLE_STEP_MIL))
                weight = segment.length / count
                for step in range(count):
                    fraction = (step + 0.5) / count
                    if backwards:
                        fraction = 1.0 - fraction
                    x, y, tx, ty = segment.point(fraction)
                    sample = _Sample(x, y, weight, segment)
                    if grid is not None:
                        found = _couple(x, y, tx, ty, segment.half, grid.at(x, y), reach)
                        if found is not None:
                            sample.gap = max(0.0, found[0])
                            sample.partner = found[1]
                            sample.within = self._within(sample)
                    samples.append(sample)
            first, first_back = chain.parts[0]
            last, last_back = chain.parts[-1]
            start = (first.x2, first.y2) if first_back else (first.x1, first.y1)
            end = (last.x1, last.y1) if last_back else (last.x2, last.y2)
            start_kind, start_feature = self._end_kind(graph, start, by_row[first.row].half,
                                                       layer)
            end_kind, end_feature = self._end_kind(graph, end, by_row[last.row].half, layer)
            walked.append(_Walked(
                chain, samples, start_kind, end_kind,
                _inside(samples, start_kind, start_feature, layer),
                _inside(samples[::-1], end_kind, end_feature, layer)))
        return walked

    # -- one pair --

    def pair(self, pair: _Pair) -> None:
        legs = {}
        for side, net in (("positive", pair.positive), ("negative", pair.negative)):
            net_id = self.index.net_id(net)
            rows = self.index.db.execute(_SEGMENT_SQL, (net_id,)).fetchall() \
                if net_id is not None else []
            legs[side] = {"net": net, "net_id": net_id,
                          "segments": [_Seg(row) for row in rows if row["layer"] in self.signal]}
        routed = {side: sum(segment.length for segment in leg["segments"])
                  for side, leg in legs.items()}
        if pair.inferred and not any(routed.values()) and not self.requested:
            # Named like a pair, routed nowhere: the naming check's business.
            self.stats["inferred_unrouted"] += 1
            return
        for leg in legs.values():
            self.resolve(pair, leg["net"], leg["segments"])
            leg["graph"] = (net_graph(self.index, leg["net_id"]) if leg["net_id"] is not None
                            else None)
        layers = sorted({segment.layer for leg in legs.values() for segment in leg["segments"]},
                        key=lambda layer: self.order.get(layer, 99))
        row: dict = {"pair": pair.name, "positive_net": pair.positive,
                     "negative_net": pair.negative}
        if pair.inferred:
            row["inferred"] = True
            row["style"] = pair.style
        if pair.classes:
            row["classes"] = pair.classes
        found: list[dict] = []
        coupled = dict.fromkeys(legs, 0.0)
        within = dict.fromkeys(legs, 0.0)
        breakdown = {side: Counter() for side in legs}
        rule_length: Counter = Counter()
        gap_known = False
        layer_rows = []
        for layer in layers:
            name = self.layer_name(layer)
            own = {side: [segment for segment in leg["segments"] if segment.layer == layer]
                   for side, leg in legs.items()}
            for segments in own.values():
                _mark_corners(segments)
            weights: Counter = Counter()
            for segments in own.values():
                for segment in segments:
                    weights[segment.limits.get("rule")] += segment.length
                    rule_length[segment.limits.get("rule")] += segment.length
            main_rule = weights.most_common(1)[0][0]
            layer_limits = next(segment.limits for segments in own.values()
                                for segment in segments
                                if segment.limits.get("rule") == main_rule)
            if layer_limits.get("min_gap") is not None or layer_limits.get(
                    "max_gap") is not None:
                gap_known = True
            widths = [segment.width for segments in own.values() for segment in segments]
            reach = self.reach(layer_limits, widths)
            walked = {
                "positive": self.walk(own["positive"], own["negative"], reach,
                                      legs["positive"]["graph"], layer),
                "negative": self.walk(own["negative"], own["positive"], reach,
                                      legs["negative"]["graph"], layer),
            }
            for side, chains_walked in walked.items():
                for item in chains_walked:
                    for sample in item.samples:
                        if sample.gap is not None:
                            coupled[side] += sample.weight
                            if sample.within:
                                within[side] += sample.weight
                    self._uncoupled_runs(item, breakdown[side])
            layer_row = self._layer_row(pair, name, layer, own, walked, layer_limits, reach,
                                        found)
            layer_rows.append(layer_row)
        # Uncoupled length: what is not coupled within the gap constraints (on a layer
        # without gap limits every coupled sample is within them).
        uncoupled = {side: max(0.0, routed[side] - within[side]) for side in legs}
        if rule_length:
            # The rule over most of the pair's copper.
            main_rule = rule_length.most_common(1)[0][0]
            pair_limits = next(segment.limits for leg in legs.values()
                               for segment in leg["segments"]
                               if segment.limits.get("rule") == main_rule)
        else:
            rule = self.pair_rule(pair)
            pair_limits = (self.limits(rule, next(iter(self.signal))) if rule is not None
                           else {"source": "none"})
        row["rule"] = pair_limits.get("rule")
        row["limit_source"] = pair_limits.get("source", "none")
        if pair_limits.get("profile"):
            row["profile"] = pair_limits["profile"]
        if len([rule for rule in rule_length if rule is not None]) > 1:
            row["rules"] = sorted(rule for rule in rule_length if rule is not None)
        max_uncoupled = pair_limits.get("max_uncoupled_length")
        if max_uncoupled is not None:
            row["max_uncoupled_length_mil"] = _r(max_uncoupled)
        row["routed_mil"] = {side: _r(value) for side, value in routed.items()}
        row["coupled_mil"] = {side: _r(value) for side, value in coupled.items()}
        # Coupled within the gap range: routed = this + uncoupled.
        row["coupled_in_gap_range_mil"] = {side: _r(value) for side, value in within.items()}
        row["uncoupled_mil"] = {side: _r(value) for side, value in uncoupled.items()}
        row["uncoupled_basis"] = "gap_constraints" if gap_known else "coupling_distance"
        row["uncoupled_breakdown_mil"] = {
            side: {kind: _r(breakdown[side].get(kind, 0.0)) for kind in
                   ("at_pads", "at_vias", "elsewhere")} for side in legs}
        changes = self._layer_changes(pair, legs, found)
        row["layer_changes"] = changes
        row["layers"] = layer_rows
        self._routing(pair, legs, routed, found)
        self._uncoupled(pair, routed, coupled, uncoupled, max_uncoupled, gap_known,
                        row, found)
        if pair.inferred:
            for finding in found:
                if finding["severity"] == "warning":
                    finding["severity"] = "info"
                    finding["inferred_pair"] = True
        if self.layer is not None:
            name = self.layer_name(self.layer)
            found = [finding for finding in found if (
                finding["layer"] == name if "layer" in finding
                else name in finding["layers_touched"] if "layers_touched" in finding
                else self.layer in layers)]
            row["layers"] = [entry for entry in layer_rows if entry["layer"] == name]
        found = [finding for finding in found if finding["check"] in self.checks]
        for finding in found:
            if finding.pop("_key", None) is not None:
                self._gap_message(finding)
        row["findings"] = dict(Counter(finding["severity"] for finding in found))
        self.findings.extend(found)
        self.rows.append(row)
        self.stats["pairs"] += 1
        self.stats["inferred" if pair.inferred else "declared"] += 1
        if any(routed.values()):
            self.stats["routed"] += 1
        self.stats["coupled"] += sum(coupled.values())
        self.stats["within"] += sum(within.values())
        self.stats["routed_length"] += sum(routed.values())
        self.stats["uncoupled"] += sum(uncoupled.values())
        self.stats["vias"] += changes["positive"] + changes["negative"]
        self.stats["vias_matched"] += 2 * changes["matched"]
        self.limit_sources[row["limit_source"]] += 1

    @staticmethod
    def _uncoupled_runs(item: _Walked, breakdown: Counter) -> None:
        """Add each run of samples not coupled within the gap constraints to breakdown: a
        run from a chain end on one of the leg's pads or vias, inside its copper and up to
        BREAKOUT_MAX_MIL beyond its edge, is that pad's or via's breakout (at_pads /
        at_vias); the rest is elsewhere."""
        samples = item.samples
        runs = []
        start = None
        for number, sample in enumerate(samples):
            free = not sample.within
            if free and start is None:
                start = number
            elif not free and start is not None:
                runs.append((start, number - 1))
                start = None
        if start is not None:
            runs.append((start, len(samples) - 1))
        bucket = {"pad": "at_pads", "via": "at_vias"}
        for low, high in runs:
            length = sum(sample.weight for sample in samples[low:high + 1])
            if low == 0 and item.start in bucket:
                part = min(length, item.start_inside + BREAKOUT_MAX_MIL)
                breakdown[bucket[item.start]] += part
                length -= part
            if high == len(samples) - 1 and item.end in bucket:
                part = min(length, item.end_inside + BREAKOUT_MAX_MIL)
                breakdown[bucket[item.end]] += part
                length -= part
            breakdown["elsewhere"] += length

    @staticmethod
    def _end_kind(graph, point, half: float, layer: int) -> tuple[str | None, dict | None]:
        """("pad" or "via", its record) of the leg's pad or via a chain end touches, else
        (None, None)."""
        if graph is None:
            return None, None
        x, y = point
        for pad in graph.pads.values():
            if layer in pad["layer_set"] and pad_contains(pad, x, y, half + 0.01, layer):
                return "pad", pad
        for via in graph.vias.values():
            if layer in via["layer_set"] and math.hypot(via["x"] - x, via["y"] - y) <= \
                    via["r"] + half + 0.01:
                return "via", via
        return None, None

    # -- per layer --

    def _layer_row(self, pair, name, layer, own, walked, layer_limits, reach, found) -> dict:
        positive = [sample for item in walked["positive"] for sample in item.samples]
        negative = [sample for item in walked["negative"] for sample in item.samples]
        coupled_samples = [sample for sample in positive if sample.gap is not None]
        row: dict = {
            "layer": name,
            "routed_mil": {
                "positive": _r(sum(segment.length for segment in own["positive"])),
                "negative": _r(sum(segment.length for segment in own["negative"]))},
            "coupled_mil": {
                "positive": _r(sum(sample.weight for sample in coupled_samples)),
                "negative": _r(sum(sample.weight for sample in negative
                                   if sample.gap is not None))},
        }
        stretches = []  # (run, inside the routing between its first and last sample
        # within the gap range: not a breakout)
        for item in walked["positive"]:
            first, last = item.within_span()
            run: list[_Sample] = []
            for number, sample in enumerate(item.samples + [None]):
                if sample is not None and sample.gap is not None:
                    if not run:
                        start = number
                    run.append(sample)
                elif run:
                    stretches.append((run, start >= first and number - 1 <= last))
                    run = []
        row["coupled_stretches"] = len(stretches)
        if coupled_samples:
            gaps = sorted((sample.gap, sample.weight) for sample in coupled_samples)
            total = sum(weight for _, weight in gaps)
            row["gap_mil"] = {
                "min": _r(gaps[0][0], 3),
                "mean": _r(sum(gap * weight for gap, weight in gaps) / total, 3),
                "max": _r(gaps[-1][0], 3)}
            # Variation along the routing: stretches of at least min_stretch between the
            # first and last sample within the gap range (bends and breakouts left out),
            # within the gap range (what is outside it is gap_below_min / gap_above_max).
            long_gaps = sorted(
                (sample.gap, sample.weight) for run, inside in stretches
                if inside and sum(sample.weight for sample in run) >= self.min_stretch - 1e-9
                for sample in run if sample.within)
            long_total = sum(weight for _, weight in long_gaps)
            if long_total >= GAP_VARY_MIN_LENGTH_MIL:
                self._gap_varies(pair, name, long_gaps, long_total, found)
        widths = {}
        for side, segments in own.items():
            if segments:
                widths[side] = {"min": _r(min(segment.width for segment in segments), 3),
                                "max": _r(max(segment.width for segment in segments), 3)}
        row["width_mil"] = widths
        limits = {f"{field}_mil": _r(layer_limits[field], 4) for field in _LIMIT_FIELDS
                  if layer_limits.get(field) is not None}
        limits["source"] = layer_limits.get("source", "none")
        if layer_limits.get("rule"):
            limits["rule"] = layer_limits["rule"]
        if layer_limits.get("profile"):
            limits["profile"] = layer_limits["profile"]
        row["limits"] = limits
        row["coupling_distance_mil"] = _r(reach, 3)
        for item in walked["positive"]:
            self._gap_runs(pair, name, item, found)
        for side, segments in own.items():
            self._widths(pair, side, name, segments, found)
        return row

    def _gap_runs(self, pair, layer_name, item: _Walked, found) -> None:
        """gap_below_min / gap_above_max: runs of coupled samples outside the gap range. A
        run between a chain end on a pad or via and the first sample within the gap range
        is a breakout when at most BREAKOUT_MAX_MIL long and starting at most
        BREAKOUT_MAX_MIL along the chain from the pad's or via's copper edge."""
        samples = item.samples
        first, last = item.within_span()

        def side(sample):
            if sample.gap is None:
                return None
            limits = sample.segment.limits
            low, high = limits.get("min_gap"), limits.get("max_gap")
            if low is not None and sample.gap < low - GAP_TOLERANCE_MIL:
                return "gap_below_min"
            if high is not None and sample.gap > high + GAP_TOLERANCE_MIL:
                return "gap_above_max"
            return None

        run: list[_Sample] = []
        current = None
        start = 0
        for number, sample in enumerate(samples + [None]):
            kind = side(sample) if sample is not None else None
            if kind != current and run:
                breakout = None
                if sum(sample.weight for sample in run) <= BREAKOUT_MAX_MIL:
                    if item.start and number - 1 < first and item.from_start(start) <= \
                            item.start_inside + BREAKOUT_MAX_MIL:
                        breakout = item.start
                    elif item.end and start > last and item.to_end(number - 1) <= \
                            item.end_inside + BREAKOUT_MAX_MIL:
                        breakout = item.end
                self._gap_finding(pair, layer_name, current, run, breakout, found)
                run = []
            if kind is not None and not run:
                start = number
            current = kind
            if kind is not None:
                run.append(sample)

    def _gap_finding(self, pair, layer_name, check, run, breakout, found) -> None:
        length = sum(sample.weight for sample in run)
        if length < MIN_DEVIATION_MIL:
            return  # below the sampling's resolution (still uncoupled)
        limits = run[0].segment.limits
        limit = limits.get("min_gap") if check == "gap_below_min" else limits.get("max_gap")
        gaps = [sample.gap for sample in run]
        worst = min(gaps) if check == "gap_below_min" else max(gaps)
        excess = abs(worst - limit)
        marginal = excess <= MARGINAL_GAP_MIL + 1e-9
        # Limits from a profile no rule links to the pair are an assumption (info).
        assumed = limits.get("source") == "layer_profile"
        severity = "warning" if (length >= self.min_stretch - 1e-9 and not breakout
                                 and not marginal and not assumed) else "info"
        stretch = {
            "from": self.at(run[0].x, run[0].y), "to": self.at(run[-1].x, run[-1].y),
            "length_mil": _r(length),
            "gap_mil": {"min": _r(min(gaps), 3), "max": _r(max(gaps), 3)},
            "width_mil": {"positive": _r(run[0].segment.width, 3),
                          "negative": _r(run[0].partner.width, 3)},
        }
        if breakout:
            stretch["breakout"] = breakout
        if marginal:
            stretch["marginal"] = True
        key = (pair.name, layer_name, check, severity)
        group = next((finding for finding in found if finding.get("_key") == key), None)
        if group is None:
            group = {"_key": key, "check": check, "severity": severity, "pair": pair.name,
                     "layer": layer_name, "rule": limits.get("rule"),
                     "limit_mil": _r(limit, 4), "limit_source": limits.get("source"),
                     "stretch_count": 0, "length_mil": 0.0,
                     "worst_gap_mil": None, "excess_mil": 0.0, "stretches": []}
            if assumed:
                group["assumed_limits"] = True
            found.append(group)
        group["stretch_count"] += 1
        group["length_mil"] = _r(group["length_mil"] + length)
        if group["worst_gap_mil"] is None or (
                worst < group["worst_gap_mil"] if check == "gap_below_min"
                else worst > group["worst_gap_mil"]):
            group["worst_gap_mil"] = _r(worst, 3)
        group["excess_mil"] = max(group["excess_mil"], _r(excess, 3))
        group["stretches"].append(stretch)

    def _gap_message(self, finding: dict) -> None:
        stretches = finding["stretches"]
        stretches.sort(key=lambda stretch: -stretch["length_mil"])
        below = finding["check"] == "gap_below_min"
        message = (
            f"{finding['stretch_count']} stretch(es) of {finding['pair']} on "
            f"{finding['layer']} ({self.text(finding['length_mil'])} in all) run "
            f"{'closer than the minimum' if below else 'farther apart than the maximum'} gap "
            f"{self.text(finding['limit_mil'])} (worst {self.text(finding['worst_gap_mil'])}, "
            f"{self.text(finding['excess_mil'])} beyond it)")
        if finding["severity"] == "warning":
            finding["message"] = message + "."
            return
        reasons = []
        short = sum(1 for stretch in stretches
                    if stretch["length_mil"] < self.min_stretch - 1e-9)
        if short:
            reasons.append(f"{short} shorter than {self.text(self.min_stretch)}")
        breakouts = sum(1 for stretch in stretches if stretch.get("breakout"))
        if breakouts:
            reasons.append(f"{breakouts} where the legs leave a pad or via (breakout)")
        marginal = sum(1 for stretch in stretches if stretch.get("marginal"))
        if marginal:
            reasons.append(f"{marginal} marginal (at most {self.text(MARGINAL_GAP_MIL)} "
                           "beyond the limit)")
        if finding.get("assumed_limits"):
            reasons.append("the limits are assumed from the board's only differential "
                           "impedance profile, which no DiffPairsRouting rule links to "
                           "the pair")
        if finding.get("inferred_pair"):
            reasons.append("the pair is only inferred from its net names")
        finding["message"] = (message + ("; info: " + "; ".join(reasons) if reasons else "")
                              + ".")

    def _gap_varies(self, pair, layer_name, gaps, total, found) -> None:
        low = _weighted_percentile(gaps, 0.05)
        median = _weighted_percentile(gaps, 0.5)
        high = _weighted_percentile(gaps, 0.95)
        spread = high - low
        if spread <= max(GAP_VARY_MIN_MIL, GAP_VARY_FRACTION * median):
            return
        found.append({
            "check": "gap_varies", "severity": "info", "pair": pair.name,
            "layer": layer_name, "coupled_length_mil": _r(total),
            "gap_mil": {"p5": _r(low, 3), "median": _r(median, 3), "p95": _r(high, 3),
                        "min": _r(gaps[0][0], 3), "max": _r(gaps[-1][0], 3)},
            "spread_mil": _r(spread, 3),
            "message": f"The coupled gap of {pair.name} on {layer_name} varies by "
                       f"{self.text(spread)} (5th to 95th percentile, median "
                       f"{self.text(median)}): the impedance varies along the pair.",
        })

    def _widths(self, pair, side, layer_name, segments, found) -> None:
        """width_out_of_range: tracks and arcs (an arc once, however many chords it is
        stored as, as check_rule_compliance counts it) outside the layer's width range."""
        groups: dict = {}
        for segment in segments:
            limits = segment.limits
            low, high = limits.get("min_width"), limits.get("max_width")
            if low is None and high is None:
                continue
            if low is not None and segment.width < low - EPS_MIL:
                direction = "below_min"
            elif high is not None and segment.width > high + EPS_MIL:
                direction = "above_max"
            else:
                continue
            key = (round(segment.width, 4), direction, limits.get("rule"))
            group = groups.get(key)
            if group is None:
                x, y, _, _ = segment.point(0.5)
                assumed = limits.get("source") == "layer_profile"
                group = groups[key] = {
                    "check": "width_out_of_range",
                    "severity": "info" if assumed else "warning",
                    "pair": pair.name, "net": pair.positive if side == "positive"
                    else pair.negative, "layer": layer_name, "rule": limits.get("rule"),
                    "limit_source": limits.get("source"),
                    "width_mil": _r(segment.width, 4), "side": direction,
                    "min_mil": _r(low, 4), "max_mil": _r(high, 4), "tracks": 0, "arcs": 0,
                    "length_mil": 0.0, "at": self.at(x, y), "_objects": set()}
                if assumed:
                    group["assumed_limits"] = True
                elif limits.get("rule") and ("min_width" if direction == "below_min"
                                             else "max_width") not in limits.get(
                                                 "from_profile", ()):
                    # The rule's own per-layer width: check_rule_compliance reports the
                    # same DiffPairsRouting violation (this is the pair's view of it).
                    group["also_reported_by"] = "check_rule_compliance"
            identity = object_id(segment.row)
            if identity not in group["_objects"]:
                group["_objects"].add(identity)
                group["arcs" if identity[0] == "arc" else "tracks"] += 1
            group["length_mil"] = _r(group["length_mil"] + segment.length)
        for group in groups.values():
            del group["_objects"]
            limit = group["min_mil"] if group["side"] == "below_min" else group["max_mil"]
            objects = " and ".join(
                f"{count} {kind}{'s' if count > 1 else ''}"
                for kind, count in (("track", group["tracks"]), ("arc", group["arcs"]))
                if count)
            group["message"] = (
                f"{objects} of {group['net']} on {layer_name} "
                f"{'are' if group['tracks'] + group['arcs'] > 1 else 'is'} "
                f"{self.text(group['width_mil'])} wide, "
                f"{'below the minimum' if group['side'] == 'below_min' else 'above the maximum'}"
                f" {self.text(limit)} ({self.text(group['length_mil'])} in all)"
                + ("; info: the limits are assumed from the board's only differential "
                   "impedance profile, which no DiffPairsRouting rule links to the pair."
                   if group.get("assumed_limits") else
                   "; check_rule_compliance reports the same DiffPairsRouting violation."
                   if group.get("also_reported_by") else "."))
            found.append(group)

    # -- whole pair --

    def _transitions(self, graph) -> list[tuple]:
        if graph is None:
            return []
        out = []
        for pid, nodes in graph.via_nodes.items():
            layers = sorted((layer for layer in nodes if layer in self.signal),
                            key=lambda layer: self.order.get(layer, 99))
            if len(layers) >= 2:
                via = graph.vias[pid]
                out.append((pid, via["x"], via["y"], tuple(layers)))
        return out

    def _layer_changes(self, pair, legs, found) -> dict:
        positive = self._transitions(legs["positive"]["graph"])
        negative = self._transitions(legs["negative"]["graph"])
        candidates = []
        for p_number, (_, px, py, p_layers) in enumerate(positive):
            for n_number, (_, nx, ny, n_layers) in enumerate(negative):
                distance = math.hypot(px - nx, py - ny)
                # Both vias join the legs on at least two of the same layers: a stub one
                # leg has on a third layer at its via does not break the match.
                if distance <= self.via_distance and len(set(p_layers) & set(n_layers)) >= 2:
                    candidates.append((distance, p_layers != n_layers, p_number, n_number))
        candidates.sort()
        used_p, used_n, spacing = set(), set(), []
        for distance, _, p_number, n_number in candidates:
            if p_number in used_p or n_number in used_n:
                continue
            used_p.add(p_number)
            used_n.add(n_number)
            spacing.append(distance)
        result: dict = {"positive": len(positive), "negative": len(negative),
                        "matched": len(spacing)}
        if spacing:
            result["via_spacing_mil"] = {"min": _r(min(spacing)), "max": _r(max(spacing))}
        unmatched = []
        for side, own, other, used in (("positive", positive, negative, used_p),
                                       ("negative", negative, positive, used_n)):
            net = pair.positive if side == "positive" else pair.negative
            for number, (_, x, y, layers) in enumerate(own):
                if number in used:
                    continue
                place = {"net": net, "at": self.at(x, y),
                         "layers": [self.layer_name(layer) for layer in layers]}
                nearest = min(((math.hypot(x - ox, y - oy), o_layers)
                               for _, ox, oy, o_layers in other), default=None)
                if nearest is not None:
                    place["nearest_other_via"] = {
                        "distance_mil": _r(nearest[0]),
                        "layers": [self.layer_name(layer) for layer in nearest[1]]}
                unmatched.append(place)
        if unmatched:
            layers_touched = {name for place in unmatched for name in place["layers"]}
            found.append({
                "check": "unmatched_layer_change", "severity": "warning", "pair": pair.name,
                "count": len(unmatched), "vias": unmatched,
                "layers_touched": sorted(layers_touched),
                "message": f"{len(unmatched)} layer change(s) of {pair.name} have no via of "
                           f"the other leg joining two of the same layers within "
                           f"{self.text(self.via_distance)}: the two legs change layer at "
                           "different places (or onto different layers).",
            })
        return result

    def _routing(self, pair, legs, routed, found) -> None:
        if not any(routed.values()):
            found.append({
                "check": "pair_not_routed", "severity": "info", "pair": pair.name,
                "message": f"Neither leg of {pair.name} has a track on a signal layer.",
            })
            return
        statuses = {}
        for side, leg in legs.items():
            if not routed[side]:
                statuses[side] = "no_tracks"
                continue
            measured = net_length(self.index, leg["net"])
            status = measured.get("status", "ok")
            if status != "ok":
                statuses[side] = status
        if statuses:
            found.append({
                "check": "partially_routed", "severity": "info", "pair": pair.name,
                "legs": {pair.positive if side == "positive" else pair.negative: status
                         for side, status in statuses.items()},
                "message": f"{pair.name} is not fully routed: "
                           + ", ".join(f"{pair.positive if side == 'positive' else pair.negative}"
                                       f" {status.replace('_', ' ')}"
                                       for side, status in statuses.items())
                           + ". Its coupling and uncoupled length cover the copper so far.",
            })

    def _uncoupled(self, pair, routed, coupled, uncoupled, max_uncoupled, gap_known, row,
                   found) -> None:
        if not all(routed.values()) or (pair.inferred and not any(coupled.values())):
            return
        side = max(uncoupled, key=lambda key: uncoupled[key])
        longest = uncoupled[side]
        net = pair.positive if side == "positive" else pair.negative
        finding = {"check": "uncoupled_length_high", "pair": pair.name,
                   "uncoupled_mil": dict(row["uncoupled_mil"]),
                   "uncoupled_breakdown_mil": row["uncoupled_breakdown_mil"][side],
                   "basis": row["uncoupled_basis"]}
        if max_uncoupled is not None:
            if longest <= max_uncoupled + EPS_MIL:
                return
            finding.update({
                "severity": "warning", "rule": row.get("rule"),
                "limit_mil": _r(max_uncoupled),
                "message": f"{net} of {pair.name} has {self.text(longest)} not coupled "
                           f"within the gap range, over the rule's Max Uncoupled Length "
                           f"{self.text(max_uncoupled)}.",
            })
        else:
            threshold = max(UNCOUPLED_FRACTION * routed[side], UNCOUPLED_MIN_MIL)
            if longest <= threshold:
                return
            finding.update({
                "severity": "info", "share": _r(longest / routed[side], 3),
                "message": f"{net} of {pair.name} has {self.text(longest)} uncoupled, "
                           f"{longest / routed[side]:.0%} of its routed length; no Max "
                           "Uncoupled Length applies to the pair.",
            })
        found.append(finding)


def _finish(findings: list[dict]) -> list[dict]:
    findings.sort(key=lambda finding: (
        _SEVERITY_RANK[finding["severity"]], _CHECK_RANK[finding["check"]],
        -(finding.get("length_mil") or 0.0), str(finding.get("pair")),
        str(finding.get("layer") or "")))
    return findings


def prepare_diff_pair_geometry(index, pairs: list[str] | None = None,
                               net_classes: list[str] | None = None, layer: str | None = None,
                               checks=None, coupling_distance_mil: float | None = None,
                               min_stretch_mil: float | None = None,
                               via_distance_mil: float | None = None,
                               units: str = "mil") -> dict:
    """The whole check, unpaged (render_diff_pair_geometry pages it), or an error."""
    selected_checks, error = _parse_checks(checks)
    if error:
        return error
    signal = index.signal_layers()
    layer_id = None
    if layer:
        layer_id = index.resolve_layer(layer)
        if layer_id is None or layer_id not in signal:
            return {"error": "layer_not_found",
                    "message": f"No signal layer named '{layer}'.",
                    "layers": list(signal.values())}
    board_pairs, skipped = _board_pairs(index)
    selected, error = _select(index, board_pairs, pairs, net_classes)
    if error:
        return error
    check = _Check(index, layer_id, set(selected_checks), coupling_distance_mil,
                   DEFAULT_MIN_STRETCH_MIL if min_stretch_mil is None else min_stretch_mil,
                   DEFAULT_VIA_DISTANCE_MIL if via_distance_mil is None else via_distance_mil,
                   units)
    check.requested = bool(pairs or net_classes)
    for pair in selected:
        check.pair(pair)
    findings = _finish(check.findings)
    rows = check.rows
    if layer_id is not None:
        rows = [row for row in rows if row["layers"]]
    rank = {pair: number for number, pair in enumerate(
        dict.fromkeys(finding["pair"] for finding in findings))}
    rows.sort(key=lambda row: (rank.get(row["pair"], len(rank)), row.get("inferred", False),
                               row["pair"].lower()))
    stats = check.stats
    summary = {
        "pairs_analysed": stats["pairs"],
        "declared_pairs": stats["declared"],
        "inferred_pairs": stats["inferred"],
        "inferred_pairs_not_routed": stats["inferred_unrouted"],
        "pairs_routed": stats["routed"],
        "limit_sources": dict(check.limit_sources),
        "routed_length_mil": _r(stats["routed_length"]),
        "coupled_length_mil": _r(stats["coupled"]),
        "coupled_in_gap_range_length_mil": _r(stats["within"]),
        "uncoupled_length_mil": _r(stats["uncoupled"]),
        "layer_changes": {"vias": stats["vias"], "vias_matched": stats["vias_matched"]},
        "severity_counts": dict(Counter(finding["severity"] for finding in findings)),
        "findings_by_check": dict(Counter(finding["check"] for finding in findings)),
    }
    if skipped:
        summary["declared_pairs_skipped"] = dict(skipped)
    if check.undecided:
        summary["segments_with_undecided_rules"] = check.undecided
    notes = []
    if not board_pairs:
        notes.append("The board has no differential pair and no nets named as one.")
    if any(row.get("inferred") for row in rows):
        notes.append(
            "Pairs marked inferred are nets named as complementary legs that are not a PCB "
            "differential pair: no DiffPairsRouting rule applies to them, so their findings "
            "are info. Declare them as a pair to apply the routing rule.")
    if check.limit_sources.get("none"):
        notes.append(
            "Pairs with limit_source none have no DiffPairsRouting rule or unambiguous "
            "differential impedance profile: their gap and width are measured, not judged.")
    if check.limit_sources.get("layer_profile"):
        notes.append(
            "Pairs with limit_source layer_profile have no DiffPairsRouting rule: their gap "
            "and width are judged against the board's only differential impedance profile "
            "on the layer, which no rule links to them, so those findings are info.")
    notes.append(
        "Gaps and widths are read from the saved tracks and arcs. Altium's batch DRC checks "
        "DiffPairsRouting widths and the uncoupled length; gap stretches, gap variation and "
        "unmatched layer changes are this tool's own screen. A field solver or the "
        "fabricator's impedance report is the final word on impedance.")
    parameters = {
        "coupling_distance_mil": _r(coupling_distance_mil, 4),
        "coupling_distance_default": (
            f"per layer, {COUPLING_GAP_FACTOR:g}x the max gap (else the preferred gap), at "
            f"least {COUPLING_WIDTH_FACTOR:g}x the width; {COUPLING_WIDTH_ONLY_FACTOR:g}x the "
            "width without a gap limit"),
        "min_stretch_mil": _r(check.min_stretch, 4),
        "via_distance_mil": _r(check.via_distance, 4),
        "gap_tolerance_mil": GAP_TOLERANCE_MIL,
        "angle_tolerance_deg": ANGLE_TOLERANCE_DEG,
    }
    if layer_id is not None:
        parameters["layer"] = check.layer_name(layer_id)
    return {
        "checks_run": selected_checks,
        "parameters": parameters,
        "summary": summary,
        "pairs": rows,
        "findings": findings,
        "approximations": _approximations(units),
        "notes": notes,
    }


def prepare_diff_pair_geometry_cached(index, **kwargs) -> dict:
    """prepare_diff_pair_geometry, cached on the index per arguments (the last CACHE_SIZE),
    so paging reuses it: treat the result as read-only."""
    key = repr(sorted(kwargs.items()))
    cache = index.__dict__.setdefault("_diff_pair_geometry_cache", {})
    held = cache.get(key)
    if held is not None:
        return held
    prepared = prepare_diff_pair_geometry(index, **kwargs)
    if "error" not in prepared:
        if len(cache) >= CACHE_SIZE:
            cache.pop(next(iter(cache)))
        cache[key] = prepared
    return prepared


def render_diff_pair_geometry(prepared: dict, offset: int = 0, limit: int = 100,
                              stretches: int = STRETCHES_LISTED,
                              pairs: int = PAIRS_LISTED, pairs_offset: int = 0) -> dict:
    """One page: findings from offset (each with at most stretches stretches or vias) and
    pairs rows from pairs_offset, at most pairs of them (those with findings first)."""
    offset = max(0, int(offset or 0))
    pairs_offset = max(0, int(pairs_offset or 0))
    findings = prepared["findings"]
    page = []
    for finding in findings[offset:offset + limit]:
        for key in ("stretches", "vias"):
            listed = finding.get(key)
            if listed is not None and len(listed) > stretches:
                finding = {**finding, key: listed[:stretches], f"{key}_listed": stretches}
        page.append(finding)
    result = {key: value for key, value in prepared.items()
              if key not in ("findings", "pairs", "approximations", "notes")}
    rows = prepared["pairs"]
    result["pairs"] = rows[pairs_offset:pairs_offset + pairs]
    if pairs_offset or len(rows) > pairs:
        result["pairs_total"] = len(rows)
        result["pairs_offset"] = pairs_offset
        result["pairs_listed"] = len(result["pairs"])
        if pairs_offset + pairs < len(rows):
            result["next_pairs_offset"] = pairs_offset + pairs
    result["findings"] = page
    result["total"] = len(findings)
    result["offset"] = offset
    result["has_more"] = offset + limit < len(findings)
    if result["has_more"]:
        result["next_offset"] = offset + limit
    result["approximations"] = prepared["approximations"]
    result["notes"] = prepared["notes"]
    return result


def check_diff_pair_geometry(index, offset: int = 0, limit: int = 100,
                             pairs_offset: int = 0, **kwargs) -> dict:
    """The check in one call: prepare_diff_pair_geometry and one page of it."""
    prepared = prepare_diff_pair_geometry(index, **kwargs)
    if "error" in prepared:
        return prepared
    return render_diff_pair_geometry(prepared, offset, limit, pairs_offset=pairs_offset)
