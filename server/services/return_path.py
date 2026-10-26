"""Return-path check (roadmap #29, stage B): whether the fast nets of a board keep reference
copper under them, offline, on the reference copper model of services/reference_planes.

- Layers: each signal layer's reference layers (an impedance profile's, else its stack
  neighbours) with their kind (internal plane, large pour, signal layer without one), the
  dielectric to each and the edge distance used against it; no_adjacent_reference flags a
  routed signal layer with no plane or large pour among its references (a warning when it
  carries selected nets).
- Segments: the tracks and arcs (as their chords) of the selected nets, chained where they
  meet, against the copper of each plane or pour reference of their layer
  (reference_planes.segment_coverage). Reference copper is a plane region's net, or on a
  pour layer a net listed for it (a share of at least reference_planes.NET_SHARE_LISTED)
  or a supply or ground net with area copper there; anything else (no copper, copper
  without a net, another net's fill or routing) is a gap. The net's own vias and pads (and
  its differential partner's) crossing the reference layer are left out with their
  clearance (the anti-pad on a plane, the pour's clearance measured on rays from the hole
  on a signal layer: _measured), and so is the cut-out measured under its own SMD pads.
  segment_crosses_split: the reference net under the run changes (GND to PWR) across a
  gap of at most SPLIT_GAP_MAX_MIL; segment_over_void: any other gap of at least
  VOID_MIN_MIL (between copper of one net, of two nets farther apart, or at the end of a
  run); segment_near_plane_edge: reference copper of another net, or none, within the
  edge distance of the track (3x the dielectric to that reference, edge_distance=
  overrides) on either side, sampled on the two lines offset by it. A gap or edge within
  one other via's or pad's clearance is that hole's: info for a void, not an edge; a gap
  within several (with the neck a pour removes between them) is hole_clearances_merged.
  A stripline whose other reference stays one net's copper across the spot is info. A
  reference net's routing copper with a gap on both sides (a GND via's pad in a PWR pour)
  is part of the gap (_islands), not a crossing there and back.
- plane_split: internal planes split among several nets, and signal layers serving as a
  reference whose area copper is split among several nets (each with a share of at least
  reference_planes.NET_SHARE_LISTED), with the nets and areas.
- Stitching: each layer change of a selected net (a via or through-hole pad its tracks end
  on in two signal layers) whose two layers share no reference layer needs the return
  current to change layers too: a via (or through-hole pad) of the reference net drilled
  through a reference layer of each side within stitch_radius (centre to centre), or, when
  the reference nets on the two sides differ (a reference net change), a capacitor
  (decoupling.is_capacitor) between each pair of them with a pad within stitch_radius. The
  reference net at the via is the most common reference-net copper on two rings around it.
  A differential pair's two vias of one layer change are judged together.
  missing_stitching_via: info, a warning for a reference net change without a capacitor.

Severity: warning for crossings and voids on the selected nets (they are the fast nets, or
the ones asked for), info for edges, stitching, plane splits and voids that are one hole's
clearance or that the other reference covers. Results that rest on approximated geometry
(anti-pad sizes, default pullback, bridged hatch openings, assumed pour outlines, a plane
whose split lines are not partitioned) are marked likely.

Default selection: the members of differential pairs, of net classes with a high-speed
name, of xSignals, of the scopes of impedance-profile-driven Width and DiffPairsRouting
rules and of Length and MatchedLengths rules (scopes other than All, or than one taking in
SCOPE_ALL_SHARE of the routed nets), and nets whose name suggests a clock or a high-speed
bus but not a sideband net (enables, resets, I2C, debug); power and ground names are left
out. Pure logic over a PcbIndex, no MCP; lengths in mil, locations relative to the board
origin.
"""

import bisect
import math
import re
from collections import Counter, defaultdict

try:
    from services import reference_planes as rp
    from services.connectivity import select_nets
    from services.decoupling import designator_prefix, is_capacitor
    from services.dfm_geometry import point_distance
    from services.pcb_rules import evaluate_scope
except ImportError:
    from server.services import reference_planes as rp
    from server.services.connectivity import select_nets
    from server.services.decoupling import designator_prefix, is_capacitor
    from server.services.dfm_geometry import point_distance
    from server.services.pcb_rules import evaluate_scope

MIL_PER_MM = 1 / 0.0254
CHECKS = ("no_adjacent_reference", "segment_crosses_split", "segment_over_void",
          "segment_near_plane_edge", "plane_split", "missing_stitching_via")
_CHECK_ALIASES = {"layers": "no_adjacent_reference", "splits": "segment_crosses_split",
                  "crossings": "segment_crosses_split", "voids": "segment_over_void",
                  "edges": "segment_near_plane_edge", "plane_splits": "plane_split",
                  "stitching": "missing_stitching_via"}
_CHECK_RANK = {check: rank for rank, check in enumerate(CHECKS)}
_SEVERITY_RANK = {"warning": 0, "info": 1}
_SEGMENT_CHECKS = frozenset({"segment_crosses_split", "segment_over_void",
                             "segment_near_plane_edge"})

DEFAULT_STITCH_RADIUS_MIL = 1.0 * MIL_PER_MM
EDGE_FACTOR = 3.0  # edge distance: 3x the dielectric height to the reference
DEFAULT_EDGE_MIL = 20.0  # when the stack gives no dielectric height
# A pour's clearance around a via or pad is looked for this far past its copper (the rays
# measuring it are this long; past it a hole has no clearance of its own there).
POUR_CLEARANCE_REACH_MIL = 40.0
HOLE_REACH_MARGIN_MIL = 2.0
HOLE_SEARCH_MIL = 100.0  # holes are looked for this far past their copper (any clearance)
VOID_MIN_MIL = 2.0  # shorter gaps are not reported
# A change of reference net across a longer gap is a void between two nets' copper, not a
# split crossing.
SPLIT_GAP_MAX_MIL = 100.0
EDGE_MIN_RUN_MIL = 10.0  # shorter stretches near an edge are not reported
SAMPLE_STEP_MIL = 5.0  # gaps are tested against hole clearances this often
SNAP_MIL = 0.01  # track ends this close meet (chaining)
STITCH_SEARCH_FACTOR = 5.0  # the nearest stitching via is reported up to this x radius
PAIR_VIA_FACTOR = 3.0  # a pair's two vias of one layer change lie within this x radius
PAIR_VIA_MIN_MIL = 100.0
LOCAL_RING_MIL = (5.0, 25.0)  # rings past a via's clearance sampled for its reference net
OCCURRENCES_LISTED = 10
NETS_LISTED = 50
CACHE_SIZE = 4
# A rule scope matching this share of the routed nets (of at least SCOPE_ALL_MIN_NETS) is
# read as All.
SCOPE_ALL_SHARE = 0.9
SCOPE_ALL_MIN_NETS = 20
# A pour's clearance around a hole (or the cut-out under a pad) is measured on this many
# rays from its centre: the smallest gap between its copper and reference-net copper.
CLEARANCE_RAYS = 8
CONTACT_MIL = 0.01  # reference copper this close to a hole's copper touches it
# On a pour, copper narrower than this between two clearances is removed (Altium's default
# neck width): gaps this far past the clearances of several holes are still theirs.
NECK_MIL = 5.0
_APPROXIMATE_SOURCES = frozenset({"hatched_pour_bridged", "hatched_pour_outlines",
                                  "pour_outlines_assumed",
                                  "whole_plane_split_lines_unpartitioned"})

APPROXIMATIONS = [
    *rp.APPROXIMATIONS,
    "Arcs are walked as the chords Altium's arcs are read as (at most a fraction of a mil "
    "inside the arc).",
    "segment_near_plane_edge samples the reference copper on the two lines offset from the "
    "track by the edge distance; an edge lying wholly between them and the track is seen "
    "only where it reaches the track (a void). Clearances around one via or pad are not "
    "edges.",
    "A via's or pad's clearance on a reference layer is its anti-pad on a plane and, on a "
    f"pour, the smallest gap to reference copper on {CLEARANCE_RAYS} rays from its centre "
    f"(looked for up to {POUR_CLEARANCE_REACH_MIL:g} mil past its copper). A gap within one "
    "other net's via or pad clearance is that hole's (info); a gap within several, or the "
    f"copper narrower than {NECK_MIL:g} mil a pour removes between them, is merged "
    "clearances. The net's own vias and pads are left out with their clearance, and its "
    "own SMD pads with the cut-out measured around them on the reference layer.",
    "Layer changes are the vias and through-hole pads that tracks of the net end on in two "
    "or more signal layers (via-in-pad to an SMD pad included). The reference net at a via "
    "is the most common reference-net copper sampled on two rings around it; a stitching "
    "via is a via or through-hole pad of that net drilled through a reference layer of "
    "each side, and a stitching capacitor a capacitor (designator C and a number, or a "
    "C-prefixed part whose comment or footprint says capacitor or farads) with pads on "
    "both reference nets, within stitch_radius of the via (centre to centre, pad centre); "
    "every pair of reference nets that changes needs one.",
    "A pour layer's reference copper is the nets listed for it (at least "
    f"{rp.NET_SHARE_LISTED:.0%} of the board) and supply or ground nets with area copper "
    "there; another net's copper under a fast net is a gap.",
]

# ---------- net selection ----------

_TOKEN_SPLIT = re.compile(r"[^A-Z0-9]+")
_POWER_TOKEN = re.compile(
    r"[A-Z]{0,2}GND\w*|EARTH|CHASSIS|VSS\w*|VCC\w*|VDD\w*|VEE\w*|VBAT\w*|VBUS\w*|VIN\w*|"
    r"VREF\w*|VTT\w*|VPP\w*|PWR\w*|POWER\w*|\d+V\d*|\d+V\d+\w*|V\d+(?:V\d*|P\d+)?|\d+P\d+V?|"
    # prefixed supplies (AVDD, DVCC, IOVDD, PLLVDD), aux and core rails, P3V3-style names
    r"(?:[ADPS]|IO|PLL)V(?:DD|CC|SS|EE)\w*|VAUX\w*|AUX\d+V\d*\w*|VCORE\w*|P\d+V\d+\w*|VIO\d*")
# Name-only matches skip control and sideband nets of a fast interface (enables, resets,
# wake and clock-request lines, I2C/SMBus, debug ports, LEDs): slow edges.
_SIDEBAND_TOKEN = re.compile(
    r"EN|ENA|ENABLE|I2C\d*|SCL\d*|SDA\d*|SMB\w*|PERST\w*|WAKE\w*|CLKREQ\w*|PRSNT\w*|"
    r"RST\w*|RESET\w*|INT|IRQ\w*|ALERT\w*|SWCLK|SWDCLK|SWDIO|TCK|TMS|TDI|TDO|JTAG\w*|PG|"
    r"PGOOD|PWRGD|PWROK|LED\w*")
_CLOCK_TOKEN = re.compile(r"[A-Z]*CLK[A-Z0-9]*|CLOCK[A-Z0-9]*|SCK\d*|CK[TCPN]?\d*")
_BUS_TOKEN = re.compile(
    r"PCIE\w*|PCI|PE[TR][PN]\d*|SATA\w*|HDMI\w*|TMDS\w*|LVDS\w*|MIPI\w*|CSI\d*|DSI\d*|"
    r"Q?SGMII\d*|RGMII\w*|RMII\w*|XAUI\w*|SERDES\w*|MDI\d*[PN]?|TRD\d*[PN]?|"
    r"GT[XHPYM][A-Z0-9]*|MGT\w*|LPDDR\d*|DDR\d*|DQ\d*|DQS\w*|DM\d+|EMMC\w*|SDIO\w*|"
    r"SDMMC\w*|QSPI\w*|OSPI\w*|SSTX\w*|SSRX\w*|HSTX\w*|HSRX\w*|HS|RF\d*|RFIN\w*|"
    r"RFOUT\w*|ANT\d*")
_USB_DATA_TOKEN = re.compile(r"D[PMN]?\d*|DATA[PN]?|USB\d*D[PMN]|S?S[TR]X\d*[PN]?|[TR]X\d*[PN]?")
_ETH_DATA_TOKEN = re.compile(r"[TR]XD?\d*[PN]?|[TR]D\d*[PN]?|MDI\w*|TRD\w*|TXEN|RXDV")
_HS_CLASS = re.compile(r"HIGH.?SPEED|IMPED|(?<![A-Z0-9])\d+\s*(?:R|OHMS?)(?![A-Z0-9])|DIFF|"
                       r"(?<![A-Z0-9])(?:HS|FAST|Z0|ZDIFF|SE\d+)(?![A-Z0-9])")


def _tokens(name: str) -> list[str]:
    return [token for token in _TOKEN_SPLIT.split(name.upper()) if token]


def power_name(name: str) -> bool:
    """A net name that reads as a supply or ground rail."""
    return any(_POWER_TOKEN.fullmatch(token) for token in _tokens(name))


def high_speed_name(name: str) -> str | None:
    """Why a net or class name suggests a fast signal: clock, high_speed_bus, or None (also
    for an interface's sideband: enables, resets, I2C, debug and the like)."""
    tokens = _tokens(name)
    if any(_SIDEBAND_TOKEN.fullmatch(token) for token in tokens):
        return None
    if any(_CLOCK_TOKEN.fullmatch(token) for token in tokens):
        return "clock"
    if any(_BUS_TOKEN.fullmatch(token) for token in tokens):
        return "high_speed_bus"
    upper = name.upper()
    if any(token.startswith("USB") for token in tokens) and (
            "D+" in upper or "D-" in upper
            or any(_USB_DATA_TOKEN.fullmatch(token) for token in tokens)):
        return "high_speed_bus"
    if any(token.startswith("ETH") for token in tokens) and any(
            _ETH_DATA_TOKEN.fullmatch(token) for token in tokens):
        return "high_speed_bus"
    return None


def _high_speed_class(name: str) -> bool:
    return not power_name(name) and (
        bool(_HS_CLASS.search(name.upper())) or high_speed_name(name) is not None)


def _scope_all(text: str | None) -> bool:
    return (text or "").strip().lower() in ("", "all")


def default_selection(index, routed: set[str]) -> tuple[dict[str, list[str]], dict]:
    """The fast nets among routed (lower-case names): net -> reasons, and what was left out."""
    ctx = index.rule_context
    reasons: dict[str, list[str]] = defaultdict(list)

    def add(net: str, reason: str) -> None:
        key = net.lower()
        if key in routed and reason not in reasons[key]:
            reasons[key].append(reason)

    for pair in index.pcb.diff_pairs:
        add(pair.positive_net, "diff_pair")
        add(pair.negative_net, "diff_pair")
    for name, members in ctx.net_classes.items():
        if _high_speed_class(name):
            for net in members:
                add(net, "high_speed_class")
    for name, members in ctx.pair_classes.items():
        if _high_speed_class(name):
            for pair in members:
                for net in ctx.pairs.get(pair, (None, None, None))[1:]:
                    if net:
                        add(net, "high_speed_class")
    for nets in ctx.xsignals.values():
        for net in nets:
            add(net, "xsignal")
    skipped = Counter()
    targets: dict[tuple, object] = {}

    def target(name: str, kind: str):
        key = (name, kind)
        if key not in targets:
            targets[key] = index.rule_target(name, kind=kind)
        return targets[key]

    for rule in index.pcb.rules:
        if not rule.enabled:
            continue
        properties = {key.upper(): value for key, value in rule.properties.items()}
        if rule.kind in ("Width", "DiffPairsRouting") and \
                (properties.get("IMPEDANCEPROFILEDRIVEN") or "").strip().upper() == "TRUE":
            reason = "impedance_rule"
        elif rule.kind in ("Length", "MatchedLengths"):
            reason = "length_rule"
        else:
            continue
        if _scope_all(rule.scope1):
            skipped[f"{reason}_scoped_all"] += 1
            continue
        matched = []
        for pair in index.pcb.diff_pairs:
            pair_target = target(pair.name, "diff_pair")
            if pair_target is not None and \
                    evaluate_scope(rule.scope1, pair_target, ctx)["matches"] == "yes":
                matched += [pair.positive_net.lower(), pair.negative_net.lower()]
        if rule.kind != "DiffPairsRouting":
            for net in routed:
                net_target = target(ctx.net_names.get(net, net), "net")
                if net_target is not None and \
                        evaluate_scope(rule.scope1, net_target, ctx)["matches"] == "yes":
                    matched.append(net)
        # A scope that takes in (nearly) every routed net, e.g. InNetClass('All Nets'),
        # says nothing about which nets are fast.
        if len(routed) >= SCOPE_ALL_MIN_NETS and \
                len(set(matched) & routed) >= SCOPE_ALL_SHARE * len(routed):
            skipped[f"{reason}_scoped_all"] += 1
            continue
        for net in matched:
            add(net, reason)
    for net in routed:
        reason = high_speed_name(net)
        if reason:
            add(net, reason)
    selected = {}
    for net, why in reasons.items():
        if power_name(net):
            skipped["power_or_ground_name"] += 1
            continue
        selected[net] = why
    return selected, dict(skipped)


# ---------- geometry helpers ----------


def _disc_interval(ax, ay, dx, dy, cx, cy, radius) -> tuple[float, float] | None:
    """The t range (clipped to 0..1) of A + tD inside a circle."""
    a = dx * dx + dy * dy
    if a == 0.0:
        return None
    fx, fy = ax - cx, ay - cy
    b = fx * dx + fy * dy
    c = fx * fx + fy * fy - radius * radius
    disc = b * b - a * c
    if disc <= 0.0:
        return None
    root = math.sqrt(disc)
    t0, t1 = max(0.0, (-b - root) / a), min(1.0, (-b + root) / a)
    return (t0, t1) if t1 > t0 else None


def _subtract(pieces: list[list], zones: list[tuple[float, float]]) -> list[list]:
    """Pieces [cls, label, t0, t1] with their gap parts inside zones made own_zone."""
    if not zones:
        return pieces
    zones = sorted(zones)
    merged: list[list[float]] = []
    for t0, t1 in zones:
        if merged and t0 <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], t1)
        else:
            merged.append([t0, t1])
    out: list[list] = []
    for cls, label, t0, t1 in pieces:
        if cls != "gap":
            out.append([cls, label, t0, t1])
            continue
        cursor = t0
        for z0, z1 in merged:
            if z1 <= cursor or z0 >= t1:
                continue
            if z0 > cursor:
                out.append(["gap", label, cursor, z0])
            out.append(["own_zone", None, max(cursor, z0), min(t1, z1)])
            cursor = min(t1, z1)
        if cursor < t1:
            out.append(["gap", label, cursor, t1])
    return out


def _merge(items: list[list], tolerance: float = 1e-9) -> list[list]:
    out: list[list] = []
    for item in items:
        if item[3] - item[2] <= 0.0:
            continue
        if out and out[-1][0] == item[0] and out[-1][1] == item[1] and \
                item[2] - out[-1][3] <= tolerance:
            out[-1][3] = max(out[-1][3], item[3])
        else:
            out.append(list(item))
    return out


def _length_text(mil: float, units: str) -> str:
    return f"{mil * 0.0254:.3g} mm" if units == "mm" else f"{mil:.3g} mil"


# ---------- the check ----------


class _Segment:
    __slots__ = ("row", "kind", "layer", "x1", "y1", "x2", "y2", "length")

    def __init__(self, row):
        self.row = row["id"]
        self.kind = row["kind"]
        self.layer = row["layer"]
        self.x1, self.y1, self.x2, self.y2 = row["x1"], row["y1"], row["x2"], row["y2"]
        self.length = math.hypot(self.x2 - self.x1, self.y2 - self.y1)


class _Chain:
    """Segments of one net on one layer joined end to end, each with its direction."""

    def __init__(self, parts: list[tuple[_Segment, bool]]):
        self.parts = parts
        self.starts = []
        total = 0.0
        for segment, _ in parts:
            self.starts.append(total)
            total += segment.length
        self.length = total

    def point(self, s: float) -> tuple[float, float]:
        number = max(0, bisect.bisect_right(self.starts, s) - 1)
        segment, backwards = self.parts[number]
        t = 0.0 if segment.length == 0 else min(1.0, max(0.0, (s - self.starts[number]) /
                                                         segment.length))
        if backwards:
            t = 1.0 - t
        return (segment.x1 + (segment.x2 - segment.x1) * t,
                segment.y1 + (segment.y2 - segment.y1) * t)


def _chains(segments: list[_Segment]) -> list[_Chain]:
    def key(x: float, y: float) -> tuple[int, int]:
        return round(x / SNAP_MIL), round(y / SNAP_MIL)

    ends: dict[tuple, list[tuple[int, int]]] = defaultdict(list)
    for number, segment in enumerate(segments):
        ends[key(segment.x1, segment.y1)].append((number, 0))
        ends[key(segment.x2, segment.y2)].append((number, 1))
    used = [False] * len(segments)

    def walk(number: int, end: int) -> _Chain:
        parts = []
        while True:
            used[number] = True
            segment = segments[number]
            parts.append((segment, end == 1))
            exit_key = key(segment.x1, segment.y1) if end == 1 else key(segment.x2, segment.y2)
            joined = ends[exit_key]
            following = [item for item in joined if not used[item[0]]]
            if len(joined) != 2 or not following:
                return _Chain(parts)
            number, end = following[0]

    chains = []
    for node in sorted(ends):
        if len(ends[node]) != 2:
            for number, end in ends[node]:
                if not used[number]:
                    chains.append(walk(number, end))
    for number in range(len(segments)):
        if not used[number]:
            chains.append(walk(number, 0))
    return chains


class _Reference:
    """One plane or pour reference of a signal layer."""

    def __init__(self, check: "_Check", layer: int, entry: dict, edge_mil: float,
                 edge_basis: str):
        self.layer = layer
        self.name = entry["layer"]
        self.kind = entry["kind"]
        self.dielectric = entry.get("dielectric_mil")
        self.edge = edge_mil
        self.edge_basis = edge_basis
        self.plane = 39 <= layer <= 54
        # Reference copper: a plane's region nets; on a pour layer the nets listed for it
        # (a share of at least NET_SHARE_LISTED) and supply or ground nets with area copper.
        # Any other net's copper (a signal net's fill and its routing) is a gap.
        nets = check.model.area_nets(layer)
        if not self.plane:
            nets = (set(entry.get("nets") or ()) - {rp.NO_NET}) | {
                net for net in nets if power_name(net)}
        self.nets = nets
        self.position = check.model.position[layer]


class _Check:
    def __init__(self, index, selected: dict[str, list[str]], requested: bool,
                 layer_filter: int | None, checks: set[str], edge_mil: float | None,
                 stitch_mil: float, units: str):
        self.index = index
        self.model = rp.reference_planes(index)
        self.pcb = index.pcb
        self.selected = selected
        self.requested = requested
        self.layer_filter = layer_filter
        self.checks = checks
        self.edge_override = edge_mil
        self.stitch = stitch_mil
        self.units = units
        board = self.pcb.board
        self.origin = (board.origin_x or 0.0, board.origin_y or 0.0)
        self.raw_findings: list[dict] = []
        self.counts = Counter()
        self.transitions = Counter()
        self.pair_transitions = Counter()
        self.layer_rows: list[dict] = []
        self.references: dict[int, list[_Reference]] = {}
        self.partner: dict[str, tuple[str, str]] = {}  # net (lower) -> (pair, partner)
        for pair in self.pcb.diff_pairs:
            self.partner[pair.positive_net.lower()] = (pair.name, pair.negative_net)
            self.partner[pair.negative_net.lower()] = (pair.name, pair.positive_net)
        self.net_stats: dict[str, Counter] = defaultdict(Counter)
        self.groups: dict[tuple, dict] = {}
        self.net_transitions: dict[str, list[dict]] = {}
        self.selected_segments: Counter = Counter()
        self._centres: dict[tuple, list] = {}
        self._own_holes: dict[str, list[dict]] | None = None
        self._own_pads: dict[tuple, object] = {}
        self._clearances: dict[tuple, float | None] = {}
        self._extents: dict[tuple, float] = {}

    # ---------- helpers ----------

    def holes_of(self, net: str) -> list[dict]:
        """holes() records of a net (lower-case name)."""
        if self._own_holes is None:
            self._own_holes = defaultdict(list)
            for record in self.model.holes():
                if record["net"] is not None:
                    self._own_holes[record["net"].lower()].append(record)
        return self._own_holes.get(net, [])

    def at(self, point) -> dict:
        return {"x_mil": round(point[0] - self.origin[0], 2),
                "y_mil": round(point[1] - self.origin[1], 2)}

    def net_name(self, key: str) -> str:
        return self.index.rule_context.net_names.get(key, key)

    def _covers(self, record: dict, position: int) -> bool:
        span = record["span"]
        return span is not None and span[0] <= position <= span[1]

    def _measured(self, key, geometry, x: float, y: float, reference: _Reference):
        """The smallest gap between copper (a hole's, or a pad's seen from the reference
        layer) and reference copper of the reference layer, on CLEARANCE_RAYS rays from
        (x, y) up to POUR_CLEARANCE_REACH_MIL past the copper: a pour's clearance as saved,
        a connected hole's thermal relief air gap, the cut-out under a pad. A ray that meets
        other copper first (a neighbour whose clearance merges with this one), reference
        copper running on from the copper (a spoke, a direct connection) or no copper at all
        adds no gap. None where no ray finds a gap, or where two opposite rays meet no
        copper at all: the copper sits in a channel or an unpoured area, not in a clearance
        of its own."""
        cache_key = (key, reference.layer)
        if cache_key in self._clearances:
            return self._clearances[cache_key]
        length = rp.geometry_extent(geometry, x, y) + POUR_CLEARANCE_REACH_MIL
        nets, best = reference.nets, None
        open_rays = set()
        for ray in range(CLEARANCE_RAYS):
            # Half a step off the axes and diagonals, where thermal relief spokes run.
            angle = 2.0 * math.pi * (ray + 0.5) / CLEARANCE_RAYS
            ex, ey = x + math.cos(angle) * length, y + math.sin(angle) * length
            met = False
            for label, t0, t1 in self.model.segment_pieces(reference.layer, (x, y, ex, ey)):
                if label is None:
                    continue
                gap = point_distance(x + (ex - x) * t0, y + (ey - y) * t0, geometry)
                if gap <= CONTACT_MIL:
                    if label not in nets or point_distance(
                            x + (ex - x) * t1, y + (ey - y) * t1, geometry) <= CONTACT_MIL:
                        continue  # the copper itself
                    met = True  # reference copper runs on from the copper: no gap here
                    break
                met = True
                if label in nets:
                    best = gap if best is None else min(best, gap)
                break
            if not met:
                if (ray + CLEARANCE_RAYS // 2) % CLEARANCE_RAYS in open_rays:
                    best = None
                    break
                open_rays.add(ray)
        self._clearances[cache_key] = best
        return best

    def _clearance(self, record: dict, reference: _Reference) -> float | None:
        """A hole's clearance on a reference layer: its anti-pad's on a plane (the Power
        Plane Clearance rule, or the default: the plane model voids exactly that), the
        measured one on a pour (None: no reference copper within reach apart from it)."""
        if reference.plane:
            return record["clearance"]
        return self._measured(("hole", id(record)), record["copper"](reference.layer),
                              record["x"], record["y"], reference)

    def _own_zones(self, own_holes, reference: _Reference, ax, ay, bx, by) -> list:
        """t ranges of a line within the net's own holes' copper and clearance (plus
        HOLE_REACH_MARGIN_MIL) on the reference layer."""
        zones = []
        dx, dy = bx - ax, by - ay
        extents = self._extents
        for record in own_holes:
            if not self._covers(record, reference.position):
                continue
            key = (id(record), reference.layer)
            extent = extents.get(key)
            if extent is None:
                extent = extents[key] = rp.geometry_extent(
                    record["copper"](reference.layer), record["x"], record["y"])
            if _disc_interval(ax, ay, dx, dy, record["x"], record["y"],
                              extent + POUR_CLEARANCE_REACH_MIL + HOLE_REACH_MARGIN_MIL) is None:
                continue  # too far to matter whatever its clearance
            radius = extent + (self._clearance(record, reference) or 0.0) + \
                HOLE_REACH_MARGIN_MIL
            zone = _disc_interval(ax, ay, dx, dy, record["x"], record["y"], radius)
            if zone:
                zones.append(zone)
        return zones

    def _classify(self, coverage, reference: _Reference) -> list[list]:
        pieces = []
        for label, t0, t1 in coverage:
            cls = "ref" if label is not None and label in reference.nets else "gap"
            pieces.append([cls, label, t0, t1])
        return pieces

    def _hole_attribution(self, reference: _Reference, points, own: set[str]):
        """(the one hole of another net whose clearance holds every point, or None; whether
        every point lies in some hole's clearance, the net's own holes included). The
        clearance is the anti-pad's on a plane and the pour's measured one on a signal layer
        (_clearance), plus HOLE_REACH_MARGIN_MIL; for the second answer on a pour also plus
        NECK_MIL (the copper Altium removes between two clearances)."""
        xs = [point[0] for point in points]
        ys = [point[1] for point in points]
        cx, cy = (min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0
        half = math.hypot(max(xs) - min(xs), max(ys) - min(ys)) / 2.0
        neck = 0.0 if reference.plane else NECK_MIL
        candidates = []  # (record, distance of each point to its copper, bound, own net)
        for record in self.model.holes_near(cx, cy, half + HOLE_SEARCH_MIL):
            if not self._covers(record, reference.position):
                continue
            geometry = record["copper"](reference.layer)
            # The most its clearance can hold: the anti-pad on a plane, the reach of the
            # rays measuring it on a pour.
            bound = HOLE_REACH_MARGIN_MIL + (
                record["clearance"] if reference.plane else POUR_CLEARANCE_REACH_MIL)
            distances = [point_distance(x, y, geometry) for x, y in points]
            if min(distances) > bound + neck:
                continue  # too far to matter whatever its clearance
            net = record["net"]
            candidates.append((record, distances, bound,
                               net is not None and net.lower() in own))
        if not candidates:
            return None, False

        def reach(record) -> float | None:  # measured lazily: the nearest holes first
            clearance = self._clearance(record, reference)
            return None if clearance is None else clearance + HOLE_REACH_MARGIN_MIL

        for record, distances, bound, mine in sorted(candidates, key=lambda item: max(item[1])):
            farthest = max(distances)
            if mine or farthest > bound:
                continue
            limit = reach(record)
            if limit is not None and farthest <= limit:
                return record, True
        for number in range(len(points)):
            for record, distances, bound, _ in sorted(candidates,
                                                      key=lambda item: item[1][number]):
                if distances[number] > bound + neck:
                    continue
                limit = reach(record)
                if limit is not None and distances[number] <= limit + neck:
                    break
            else:
                return None, False
        return None, True

    def _samples(self, chain: _Chain, s0: float, s1: float) -> list[tuple[float, float]]:
        steps = max(1, math.ceil((s1 - s0) / SAMPLE_STEP_MIL))
        return [chain.point(s0 + (s1 - s0) * step / steps) for step in range(steps + 1)]

    # ---------- layers ----------

    def layers(self) -> None:
        model, index = self.model, self.index
        routed = Counter()
        for row in index.db.execute(
            "SELECT layer, COUNT(*) n FROM prims WHERE kind IN ('track', 'arc') "
            "AND pour_id IS NULL AND poured_by IS NULL AND net_id IS NOT NULL "
            "AND layer BETWEEN 1 AND 32 GROUP BY layer"
        ):
            routed[row["layer"]] = row["n"]
        self.routed = routed
        for entry in model.reference_layers():
            layer = model.layer_id(entry["layer"])
            references = []
            listed = []
            for ref in entry["references"]:
                ref_layer = model.layer_id(ref["layer"])
                if self.edge_override is not None:
                    edge, basis = self.edge_override, "edge_distance"
                elif ref.get("dielectric_mil"):
                    edge, basis = EDGE_FACTOR * ref["dielectric_mil"], "3x_dielectric"
                else:
                    edge, basis = DEFAULT_EDGE_MIL, "default_no_dielectric"
                row = {"layer": ref["layer"], "side": ref["side"], "kind": ref["kind"],
                       "dielectric_mil": ref.get("dielectric_mil")}
                if ref.get("nets"):
                    row["nets"] = ref["nets"][:10]
                if ref.get("copper_layers_between"):
                    row["copper_layers_between"] = ref["copper_layers_between"]
                if ref["kind"] in ("plane", "pour"):
                    row["edge_distance_mil"] = round(edge, 3)
                    row["edge_distance_basis"] = basis
                    references.append(_Reference(self, ref_layer, ref, edge, basis))
                listed.append(row)
            self.references[layer] = references
            self.layer_rows.append({
                "layer": entry["layer"], "layer_id": layer, "basis": entry["basis"],
                "references": listed, "has_reference_copper": entry["has_reference_copper"],
                "neighbours": {side: None if entry[side] is None else
                               {"layer": entry[side]["layer"], "kind": entry[side]["kind"]}
                               for side in ("above", "below")},
                "routing_segments": routed.get(layer, 0),
            })

    def layer_findings(self, selected_segments: Counter) -> None:
        for row in self.layer_rows:
            layer = row.pop("layer_id")
            row["selected_segments"] = selected_segments.get(layer, 0)
            if row["has_reference_copper"] or not row["routing_segments"]:
                continue
            if self.layer_filter is not None and self.layer_filter != layer:
                continue
            carries = row["selected_segments"] > 0
            kinds = ", ".join(f"{side} {value['layer']} ({value['kind']})"
                              for side, value in row["neighbours"].items() if value) or "none"
            self.finding(
                "no_adjacent_reference", "warning" if carries else "info",
                f"{row['layer']} has no plane or large pour among its reference layers "
                f"(basis {row['basis']}; neighbours: {kinds})"
                + (f": {row['selected_segments']} segments of selected nets have no "
                   "continuous return path under them." if carries else "."),
                layer=row["layer"], basis=row["basis"],
                selected_segments=row["selected_segments"],
                routing_segments=row["routing_segments"],
            )

    def plane_splits(self) -> list[dict]:
        model = self.model
        used = {reference.layer for references in self.references.values()
                for reference in references}
        report = []
        for layer in model.order:
            if 39 <= layer <= 54:
                summary = model.plane_partition(layer)
                if not summary["split_among_nets"]:
                    continue
                entry = {"layer": summary["layer"], "type": "plane",
                         "nets": summary["nets"], "regions": summary["regions"],
                         "split_lines": summary["split_lines"]}
            elif layer in used:
                summary = model.plane_partition(layer)
                nets = [row for row in summary["copper"]
                        if row.get("share", 0.0) >= rp.NET_SHARE_LISTED]
                if len(nets) < 2:
                    continue
                entry = {"layer": summary["layer"], "type": "signal_layer_pours",
                         "nets": [row["net"] for row in nets], "copper": nets}
            else:
                continue
            entry["used_as_reference_by"] = sorted(
                (row["layer"] for row in self.layer_rows
                 if any(ref["layer"] == entry["layer"] for ref in row["references"])),
                key=lambda name: model.position[model.layer_id(name)])
            report.append(entry)
            if self.layer_filter is None or self.layer_filter == layer:
                self.finding(
                    "plane_split", "info",
                    f"{entry['layer']} ({'internal plane' if entry['type'] == 'plane' else 'pours'}"
                    f") is split among {len(entry['nets'])} nets: "
                    f"{', '.join(entry['nets'][:8])}. Fast signals routed over it must not "
                    "cross from one net's copper to another's.",
                    layer=entry["layer"], nets=entry["nets"][:20],
                    used_as_reference_by=entry["used_as_reference_by"],
                )
        return report

    # ---------- segments ----------

    def nets(self) -> None:
        index = self.index
        selected_segments = Counter()
        for key in sorted(self.selected):
            name = self.net_name(key)
            net_id = index.net_id(name)
            if net_id is None:
                continue
            rows = index.db.execute(
                "SELECT id, kind, layer, x1, y1, x2, y2 FROM prims WHERE net_id = ? "
                "AND kind IN ('track', 'arc') AND pour_id IS NULL AND poured_by IS NULL "
                "AND layer BETWEEN 1 AND 32 ORDER BY id", (net_id,)).fetchall()
            segments = [_Segment(row) for row in rows]
            # A track shorter than the snap (a dot) would break the chain it sits on.
            segments = [segment for segment in segments if segment.length > SNAP_MIL]
            stats = self.net_stats[key]
            stats["segments"] = len(segments)
            stats["length"] = sum(segment.length for segment in segments)
            own = {key}
            pair = self.partner.get(key)
            if pair:
                own.add(pair[1].lower())
            own_holes = [record for net in own for record in self.holes_of(net)]
            by_layer: dict[int, list[_Segment]] = defaultdict(list)
            for segment in segments:
                by_layer[segment.layer].append(segment)
            for layer, items in sorted(by_layer.items()):
                selected_segments[layer] += len(items)
                if not self.checks & _SEGMENT_CHECKS:
                    continue
                references = self.references.get(layer, [])
                in_filter = self.layer_filter is None or self.layer_filter == layer or any(
                    reference.layer == self.layer_filter for reference in references)
                if not in_filter:
                    continue
                if not references:
                    self.counts["segments_without_reference"] += len(items)
                    continue
                self.counts["segments_checked"] += len(items)
                self.counts["length_checked"] += sum(segment.length for segment in items)
                self._check_layer(key, name, layer, items, references, own, own_holes)
            if "missing_stitching_via" in self.checks:
                self._transitions(key, name, segments, own, own_holes)
        self.selected_segments = selected_segments
        self._pair_transitions()

    def _coverage(self, segment: _Segment, reference: _Reference, own_holes, offset=0.0):
        """(classified pieces with own zones, the line walked) of a segment's centre line
        (cached) or of the line offset from it by offset (left of its direction)."""
        key = (segment.row, reference.layer)
        if not offset and key in self._centres:
            return self._centres[key]
        x1, y1, x2, y2 = segment.x1, segment.y1, segment.x2, segment.y2
        if offset:
            nx = -(y2 - y1) / segment.length * offset
            ny = (x2 - x1) / segment.length * offset
            x1, y1, x2, y2 = x1 + nx, y1 + ny, x2 + nx, y2 + ny
        coverage = self.model.segment_coverage(reference.layer, (x1, y1, x2, y2))
        pieces = self._islands(
            lambda t: (x1 + (x2 - x1) * t, y1 + (y2 - y1) * t), reference,
            self._classify(coverage, reference), whole=False)
        result = (_subtract(pieces, self._own_zones(own_holes, reference, x1, y1, x2, y2)),
                  (x1, y1, x2, y2))
        if not offset:
            self._centres[key] = result
        return result

    def _check_layer(self, key, name, layer, segments, references, own, own_holes) -> None:
        layer_name = self.model.layer_name(layer)
        for chain in _chains(segments):
            per_reference = {}
            for reference in references:
                items = []
                for (segment, backwards), start in zip(chain.parts, chain.starts):
                    pieces, _ = self._coverage(segment, reference, own_holes)
                    if backwards:
                        pieces = [[cls, label, 1.0 - t1, 1.0 - t0]
                                  for cls, label, t0, t1 in reversed(pieces)]
                    for cls, label, t0, t1 in pieces:
                        items.append([cls, label, start + t0 * segment.length,
                                      start + t1 * segment.length])
                per_reference[reference.layer] = self._islands(
                    chain.point, reference, _merge(items, 1e-6))
            for reference in references:
                others = [other for other in references if other is not reference]
                self._analyse(key, name, layer, layer_name, chain, reference,
                              per_reference[reference.layer],
                              [(other, per_reference[other.layer]) for other in others], own)
                if "segment_near_plane_edge" in self.checks:
                    self._edges(key, name, layer_name, chain, reference, own, own_holes)

    def _islands(self, point_at, reference: _Reference, items: list[list],
                 whole: bool = True) -> list[list]:
        """Reference-net routing copper (a via's or pad's copper, a track) with a gap on
        both sides and no copper of its net beyond either gap is no plane to return on:
        part of the gap (a GND via's pad in a PWR pour is that via's clearance, not two
        split crossings; a GND pad relieved into a GND pour stays copper). Items [cls,
        label, start, end] along a line whose point_at(position) gives the point; on a
        piece of a line (whole False) only between reference copper on both sides."""
        changed = False
        refs = [number for number, item in enumerate(items) if item[0] == "ref"]
        for place, number in enumerate(refs):
            item = items[number]
            if not 0 < number < len(items) - 1 or items[number - 1][0] != "gap" or \
                    items[number + 1][0] != "gap":
                continue
            if not whole and not 0 < place < len(refs) - 1:
                continue  # what lies past the piece's ends is not known here
            if any(0 <= other < len(refs) and items[refs[other]][1] == item[1]
                   for other in (place - 1, place + 1)):
                continue  # its net's copper lies beyond a gap: the pad is joined to it
            x, y = point_at((item[2] + item[3]) / 2.0)
            if self.model.copper_source(reference.layer, x, y) == "routing":
                item[0] = "gap"
                item[1] = items[number - 1][1]
                changed = True
        if not changed:
            return items
        out: list[list] = []
        for item in items:  # gaps joined whatever lies under them
            if out and out[-1][0] == "gap" and item[0] == "gap" and item[2] - out[-1][3] <= 1e-6:
                out[-1][3] = max(out[-1][3], item[3])
            else:
                out.append(item)
        return out

    def _source_likely(self, reference: _Reference, chain: _Chain, item) -> bool:
        s = (item[2] + item[3]) / 2.0
        x, y = chain.point(s)
        return self.model.copper_source(reference.layer, x, y) in _APPROXIMATE_SOURCES

    def _covered_by(self, others, s0: float, s1: float) -> dict | None:
        """The other reference whose one net's copper runs under all of [s0, s1]."""
        for other, items in others:
            labels = set()
            whole = True
            for cls, label, t0, t1 in items:
                if t1 <= s0 or t0 >= s1:
                    continue
                if cls == "own_zone":
                    continue
                if cls != "ref":
                    whole = False
                    break
                labels.add(label)
            if whole and len(labels) == 1:
                return {"layer": other.name, "net": labels.pop()}
        return None

    def _analyse(self, key, name, layer, layer_name, chain, reference, items, others,
                 own) -> None:
        refs = [number for number, item in enumerate(items) if item[0] == "ref"]
        in_crossing = set()
        if "segment_crosses_split" in self.checks or "segment_over_void" in self.checks:
            for a, b in zip(refs, refs[1:]):
                s0, s1 = items[a][3], items[b][2]
                if items[a][1] == items[b][1] or s1 - s0 > SPLIT_GAP_MAX_MIL:
                    continue
                in_crossing.update(range(a + 1, b))
                if "segment_crosses_split" not in self.checks:
                    continue
                point = chain.point((s0 + s1) / 2.0)
                likely = self._source_likely(reference, chain, items[a]) or \
                    self._source_likely(reference, chain, items[b])
                occurrence = {"at": self.at(point), "from_net": items[a][1],
                              "to_net": items[b][1], "gap_mil": round(s1 - s0, 2)}
                causes = Counter()
                for item in items[a + 1:b]:
                    if item[0] == "gap":
                        middle = chain.point((item[2] + item[3]) / 2.0)
                        cause = self.model.void_cause(reference.layer, *middle) or \
                            {"cause": "other_net_copper"}
                        causes[cause["cause"]] += 1
                        likely = likely or bool(cause.get("approximate"))
                if causes:
                    occurrence["gap_causes"] = dict(causes)
                covered = self._covered_by(others, s0 - 0.5, s1 + 0.5)
                if covered:
                    occurrence["other_reference_continuous"] = covered
                if likely:
                    occurrence["likely"] = True
                self.counts["crossings"] += 1
                self.add_occurrence(
                    "segment_crosses_split", "info" if covered else "warning", key, name,
                    layer_name, reference, tuple(sorted((items[a][1], items[b][1]))),
                    occurrence,
                    max(s1 - s0, 0.0))
        if "segment_over_void" not in self.checks:
            return
        run: list[int] = []
        for number in range(len(items) + 1):
            item = items[number] if number < len(items) else None
            if item is not None and item[0] == "gap" and number not in in_crossing:
                run.append(number)
                continue
            if run:
                self._void(key, name, layer, layer_name, chain, reference, items, run, others,
                           own)
                run = []

    def _void(self, key, name, layer, layer_name, chain, reference, items, run, others,
              own) -> None:
        s0, s1 = items[run[0]][2], items[run[-1]][3]
        if s1 - s0 < VOID_MIN_MIL:
            return
        # The cut-out under the net's own SMD pad (often made on purpose) at either end of
        # the gap is left out of it; a gap wholly under one is info.
        under = self._own_pad_test(key, own, layer, reference)
        under_pad = False
        if under is not None:
            start, end = chain.point(s0), chain.point(s1)
            if under(*start) or under(*end):
                points = self._samples(chain, s0, s1)
                if all(under(x, y) for x, y in points):
                    under_pad = True
                else:
                    if under(*start):
                        s0 = _boundary(chain, s0, s1, under)
                    if under(*end):
                        s1 = _boundary(chain, s1, s0, under)
                    if s1 - s0 < VOID_MIN_MIL:
                        return
        points = self._samples(chain, s0, s1)
        hole, inside = (None, False) if under_pad else \
            self._hole_attribution(reference, points, own)
        middle = chain.point((s0 + s1) / 2.0)
        occurrence = {"from": self.at(chain.point(s0)), "to": self.at(chain.point(s1)),
                      "length_mil": round(s1 - s0, 2)}
        likely = False
        if under_pad:
            occurrence["cause"] = "under_own_pad"
        elif hole is not None:
            occurrence["cause"] = "hole_clearance"
            occurrence["hole"] = self._hole_brief(hole)
            likely = reference.plane
        elif inside:
            occurrence["cause"] = "hole_clearances_merged"
            likely = reference.plane
        else:
            cause = self.model.void_cause(reference.layer, *middle)
            if cause is None:
                net = self.model.copper_at(reference.layer, *middle)
                # A reference net's routing cut off from its copper (_islands), or another's.
                occurrence["cause"] = "reference_net_island" if net in reference.nets \
                    else "other_net_copper"
                occurrence["copper_net"] = net
            else:
                occurrence["cause"] = cause["cause"]
                likely = bool(cause.get("approximate"))
        for neighbour in (run[0] - 1, run[-1] + 1):
            if 0 <= neighbour < len(items) and items[neighbour][0] == "ref":
                likely = likely or self._source_likely(reference, chain, items[neighbour])
        covered = self._covered_by(others, s0, s1)
        if covered:
            occurrence["other_reference_continuous"] = covered
        if likely:
            occurrence["likely"] = True
        sides = [items[number][1] for number in (run[0] - 1, run[-1] + 1)
                 if 0 <= number < len(items) and items[number][0] == "ref"]
        if len(sides) == 2 and sides[0] != sides[1]:
            occurrence["from_net"], occurrence["to_net"] = sides
        nets = sorted(set(sides))
        severity = "info" if covered or hole is not None or under_pad else "warning"
        self.counts["voids"] += 1
        self.add_occurrence("segment_over_void", severity, key, name, layer_name, reference,
                            tuple(nets), occurrence, s1 - s0)

    def _own_pad_test(self, key: str, own: set[str], layer: int, reference: _Reference):
        """A test (x, y) -> bool of points under an SMD pad of the net (or its partner) on
        the signal layer or within the cut-out around it on the reference layer (its
        measured margin, _measured, plus HOLE_REACH_MARGIN_MIL); None without such pads."""
        cache_key = (key, layer, reference.layer)
        if cache_key in self._own_pads:
            return self._own_pads[cache_key]
        pads = []
        for net in sorted(own):
            net_id = self.index.net_id(self.net_name(net))
            if net_id is None:
                continue
            for row in self.index.db.execute(
                    "SELECT * FROM prims WHERE kind = 'pad' AND net_id = ? AND layer = ?",
                    (net_id, layer)):
                geometry = self.index._primitive_geometry(row, layer)
                margin = self._measured(("pad", row["id"]), geometry, row["x1"], row["y1"],
                                        reference)
                pads.append((geometry, (margin or 0.0) + HOLE_REACH_MARGIN_MIL))
        test = None
        if pads:
            def test(x: float, y: float) -> bool:
                return any(point_distance(x, y, geometry) <= reach for geometry, reach in pads)
        self._own_pads[cache_key] = test
        return test

    def _hole_brief(self, record: dict) -> dict:
        brief = {"kind": record["kind"], "at": self.at((record["x"], record["y"])),
                 "net": record["net"]}
        if record["kind"] != "via" and record.get("component") is not None:
            brief["component"] = self.index.designator(record["component"])
        return brief

    def _edges(self, key, name, layer_name, chain, reference, own, own_holes) -> None:
        distance = reference.edge
        runs: list[list] = []  # [s0, s1, side, cause, likely]
        for (segment, backwards), start in zip(chain.parts, chain.starts):
            if segment.length < 1e-6:
                continue
            centre, _ = self._coverage(segment, reference, own_holes)
            for side in (1.0, -1.0):
                pieces, line = self._coverage(segment, reference, own_holes,
                                              offset=side * distance)
                for t0, t1, label in _off_edge(centre, pieces):
                    a = (line[0] + (line[2] - line[0]) * t0, line[1] + (line[3] - line[1]) * t0)
                    b = (line[0] + (line[2] - line[0]) * t1, line[1] + (line[3] - line[1]) * t1)
                    steps = max(1, math.ceil((t1 - t0) * segment.length / SAMPLE_STEP_MIL))
                    points = [(a[0] + (b[0] - a[0]) * step / steps,
                               a[1] + (b[1] - a[1]) * step / steps) for step in range(steps + 1)]
                    hole, merged = self._hole_attribution(reference, points, own)
                    if hole is not None or merged:
                        continue
                    middle = ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
                    cause = self.model.void_cause(reference.layer, *middle)
                    if cause is None:
                        cause = {"cause": "reference_net_change", "net": label} \
                            if label in reference.nets else {"cause": "other_net_copper",
                                                             "net": label}
                    if backwards:
                        t0, t1 = 1.0 - t1, 1.0 - t0
                    runs.append([start + t0 * segment.length, start + t1 * segment.length,
                                 side, cause])
        if not runs:
            return
        runs.sort(key=lambda run: run[0])
        merged: list[list] = []
        for run in runs:
            if merged and run[0] <= merged[-1][1] + 0.5:
                merged[-1][1] = max(merged[-1][1], run[1])
                merged[-1][2].add(run[2])
                merged[-1][3].append(run[3])
            else:
                merged.append([run[0], run[1], {run[2]}, [run[3]]])
        for s0, s1, sides, causes in merged:
            if s1 - s0 < EDGE_MIN_RUN_MIL:
                continue
            kinds = Counter(cause["cause"] for cause in causes)
            occurrence = {"from": self.at(chain.point(s0)), "to": self.at(chain.point(s1)),
                          "length_mil": round(s1 - s0, 2), "both_sides": len(sides) == 2,
                          "edge": kinds.most_common(1)[0][0]}
            other = sorted({cause["net"] for cause in causes if cause.get("net")})
            if other:
                occurrence["other_nets"] = other[:5]
            if any(cause.get("approximate") for cause in causes):
                occurrence["likely"] = True
            self.counts["edges"] += 1
            self.add_occurrence("segment_near_plane_edge", "info", key, name, layer_name,
                                reference, (), occurrence, s1 - s0)

    # ---------- stitching ----------

    def _transitions(self, key, name, segments, own, own_holes) -> None:
        model, index = self.model, self.index
        ends = defaultdict(list)
        for segment in segments:
            ends[segment.layer].extend(((segment.x1, segment.y1), (segment.x2, segment.y2)))
        net_id = index.net_id(name)
        pads = index.db.execute(
            "SELECT * FROM prims WHERE net_id = ? AND kind = 'pad' AND layer BETWEEN 1 AND 32 "
            "AND (hole IS NULL OR hole = 0)", (net_id,)).fetchall()
        records = []
        for record in own_holes:
            if record["net"].lower() != key or record["kind"] not in ("via", "pad") or \
                    record["span"] is None:
                continue
            low, high = record["span"]
            layers = set()
            for layer, points in ends.items():
                position = model.position.get(layer)
                if position is None or not low <= position <= high:
                    continue
                geometry = record["copper"](layer)
                if any(point_distance(x, y, geometry) <= 0.5 for x, y in points):
                    layers.add(layer)
            for pad in pads:
                position = model.position.get(pad["layer"])
                if position is None or not low <= position <= high or pad["layer"] in layers:
                    continue
                if point_distance(record["x"], record["y"],
                                  index._primitive_geometry(pad)) == 0.0:
                    layers.add(pad["layer"])
            ordered = sorted(layers, key=lambda layer: model.position[layer])
            for a, b in zip(ordered, ordered[1:]):
                records.append(self._transition(key, name, record, a, b))
        self.net_transitions[key] = records

    def _local_net(self, reference: _Reference, record: dict) -> tuple[str | None, bool]:
        """(the most common reference net on rings around a hole on a layer, whether a
        sample read assumed copper)."""
        extent = rp.geometry_extent(record["copper"](reference.layer), record["x"], record["y"])
        base = extent + (record["clearance"] if reference.plane else POUR_CLEARANCE_REACH_MIL)
        labels = Counter()
        assumed = False
        for ring in LOCAL_RING_MIL:
            for step in range(8):
                angle = math.pi / 4.0 * step
                x = record["x"] + math.cos(angle) * (base + ring)
                y = record["y"] + math.sin(angle) * (base + ring)
                label = self.model.copper_at(reference.layer, x, y)
                if label is not None and label in reference.nets:
                    labels[label] += 1
                    if self.model.copper_source(reference.layer, x, y) in _APPROXIMATE_SOURCES:
                        assumed = True
        if not labels:
            return None, assumed
        return labels.most_common(1)[0][0], assumed

    def _transition(self, key, name, record, a: int, b: int) -> dict:
        refs_a, refs_b = self.references.get(a, []), self.references.get(b, [])
        out = {"net": key, "name": name, "record": record, "layers": (a, b), "status": None}
        if not refs_a or not refs_b:
            out["status"] = "no_reference"
            return out
        local = {}
        likely = False
        for reference in {reference.layer: reference for reference in refs_a + refs_b}.values():
            net, assumed = self._local_net(reference, record)
            local[reference.layer] = net
            likely = likely or (assumed and net is not None)
        out["local"] = local
        out["likely"] = likely
        layers_b = {reference.layer for reference in refs_b}
        if any(reference.layer in layers_b and local[reference.layer] for reference in refs_a):
            out["status"] = "shared_reference"
            return out
        pairs = [(ra, rb) for ra in refs_a for rb in refs_b
                 if local[ra.layer] and local[rb.layer]]
        if not pairs:
            out["status"] = "no_reference_copper_at_via"
            return out
        same = [(ra, rb) for ra, rb in pairs if local[ra.layer] == local[rb.layer]]
        out["refs"] = (refs_a, refs_b)
        if same:
            best = None
            for ra, rb in same:
                found = self._stitch_via(record, local[ra.layer], ra, rb)
                if found and (best is None or found[0] < best[0]):
                    best = found
            out["nets"] = sorted({local[ra.layer] for ra, _ in same})
            out["nearest"] = best
            out["status"] = "stitched" if best and best[0] <= self.stitch else "missing"
            return out
        nets = sorted({local[ra.layer] for ra, _ in pairs} | {local[rb.layer] for _, rb in pairs})
        out["nets"] = nets
        capacitors, unbridged = self._stitch_capacitors(record, pairs, local)
        out["capacitors"], out["unbridged"] = capacitors, unbridged
        out["status"] = "reference_net_change" if unbridged else "stitched_by_capacitor"
        return out

    def _stitch_via(self, record, net: str, ra: _Reference, rb: _Reference):
        best = None
        for other in self.model.holes_near(record["x"], record["y"],
                                           STITCH_SEARCH_FACTOR * self.stitch):
            if other is record or other["net"] != net or other["kind"] == "bare_hole":
                continue
            if not (self._covers(other, ra.position) and self._covers(other, rb.position)):
                continue
            distance = math.hypot(other["x"] - record["x"], other["y"] - record["y"])
            if distance <= STITCH_SEARCH_FACTOR * self.stitch and (
                    best is None or distance < best[0]):
                best = (distance, other)
        return best

    def _stitch_capacitors(self, record, pairs, local) -> tuple[list[dict], list[list[str]]]:
        """(capacitors bridging the pairs of reference nets that change, one per pair, with
        a pad within stitch_radius of the via; the pairs none bridges). A capacitor is a part
        decoupling.is_capacitor accepts: designator C and a number, or another C-prefixed
        part whose comment or footprint says capacitor or farads (never CN, CON or CR)."""
        index = self.index
        wanted = {frozenset((local[ra.layer], local[rb.layer])) for ra, rb in pairs}
        x, y, reach = record["x"], record["y"], self.stitch
        rows = index.db.execute(
            "SELECT p.component, p.net_id, p.x1, p.y1 FROM prims p WHERE p.kind = 'pad' "
            "AND p.component IS NOT NULL AND p.minx <= ? AND p.maxx >= ? AND p.miny <= ? "
            "AND p.maxy >= ?", (x + reach, x - reach, y + reach, y - reach)).fetchall()
        components = {row["component"] for row in rows
                      if math.hypot(row["x1"] - x, row["y1"] - y) <= reach}
        bridged: dict[frozenset, str] = {}
        for component in sorted(components):
            if not 0 <= component < len(index.pcb.components):
                continue
            part = index.pcb.components[component]
            designator = index.designator(component) or ""
            text = " ".join(filter(None, (part.comment, part.footprint)))
            if not is_capacitor(designator_prefix(designator), text):
                continue
            nets = {self.model.label(row["net_id"]) for row in index.db.execute(
                "SELECT DISTINCT net_id FROM prims WHERE component = ? AND kind = 'pad'",
                (component,))}
            for pair in wanted:
                if pair not in bridged and pair <= nets:
                    bridged[pair] = designator
        found = [{"designator": designator, "nets": sorted(pair)}
                 for pair, designator in sorted(bridged.items(), key=lambda item: item[1])]
        missing = sorted(sorted(pair) for pair in wanted if pair not in bridged)
        return found, missing

    def _pair_transitions(self) -> None:
        """Count every transition, pair a differential pair's two vias of one layer change,
        and report what is not stitched."""
        done = set()
        limit = max(PAIR_VIA_FACTOR * self.stitch, PAIR_VIA_MIN_MIL)
        for key, records in sorted(self.net_transitions.items()):
            pair = self.partner.get(key)
            partner_records = self.net_transitions.get(pair[1].lower(), []) if pair else []
            for transition in records:
                if id(transition) in done:
                    continue
                done.add(id(transition))
                mate = None
                if pair:
                    best = None
                    for other in partner_records:
                        if id(other) in done or set(other["layers"]) != set(transition["layers"]):
                            continue
                        distance = math.hypot(other["record"]["x"] - transition["record"]["x"],
                                              other["record"]["y"] - transition["record"]["y"])
                        if distance <= limit and (best is None or distance < best[0]):
                            best = (distance, other)
                    if best:
                        mate = best[1]
                        done.add(id(mate))
                self._report_transition(transition, mate, pair)

    def _report_transition(self, transition, mate, pair) -> None:
        group = [transition] + ([mate] if mate else [])
        rank = ("stitched", "shared_reference", "stitched_by_capacitor", "missing",
                "reference_net_change", "no_reference_copper_at_via", "no_reference")
        status = min((item["status"] for item in group), key=rank.index)
        self.transitions[status] += 1 if mate is None else 2
        if mate is not None:
            self.pair_transitions[status] += 1
        a, b = transition["layers"]
        if self.layer_filter is not None:
            touched = {a, b} | {reference.layer for layer in (a, b)
                                for reference in self.references.get(layer, [])}
            if self.layer_filter not in touched:
                return
        if status not in ("missing", "reference_net_change"):
            return
        worst = next(item for item in group if item["status"] == status)
        layers = [self.model.layer_name(layer) for layer in (a, b)]
        refs_a, refs_b = worst["refs"]
        occurrence = {
            "at": self.at((transition["record"]["x"], transition["record"]["y"])),
            "layers": layers,
            "reference_layers": {self.model.layer_name(a): [ref.name for ref in refs_a],
                                 self.model.layer_name(b): [ref.name for ref in refs_b]},
            "reference_nets": worst["nets"],
        }
        if mate is not None:
            occurrence["pair_via"] = self.at((mate["record"]["x"], mate["record"]["y"]))
        nearest = worst.get("nearest")
        if nearest:
            occurrence["nearest_stitching_via_mil"] = round(nearest[0], 2)
            occurrence["nearest_stitching_via"] = self._hole_brief(nearest[1])
        if worst.get("unbridged"):
            occurrence["pairs_without_capacitor"] = worst["unbridged"]
            if worst.get("capacitors"):
                occurrence["capacitors"] = worst["capacitors"]
        if any(item.get("likely") for item in group):
            occurrence["likely"] = True
        change = status == "reference_net_change"
        occurrence["reference_net_change"] = change
        severity = "warning" if change else "info"
        net_key = transition["net"]
        self.add_stitch(severity, net_key, transition["name"], pair if mate else None,
                        occurrence, change)

    # ---------- findings ----------

    def finding(self, check: str, severity: str, issue: str, **fields) -> None:
        if check not in self.checks:
            return
        record = {"check": check, "severity": severity}
        record.update({key: value for key, value in fields.items() if value is not None})
        record["issue"] = issue
        self.raw_findings.append(record)

    def add_occurrence(self, check, severity, key, name, layer_name, reference, nets,
                       occurrence, length) -> None:
        group = (check, severity, key, layer_name, reference.name, nets)
        entry = self.groups.get(group)
        if entry is None:
            entry = self.groups[group] = {"check": check, "severity": severity, "net": name,
                                          "layer": layer_name,
                                          "reference_layer": reference.name,
                                          "reference_kind": reference.kind,
                                          "nets": list(nets), "occurrences": [],
                                          "length": 0.0}
            if check == "segment_near_plane_edge":
                entry["edge_distance_mil"] = round(reference.edge, 3)
        entry["occurrences"].append((length, occurrence))
        entry["length"] += length

    def add_stitch(self, severity, key, name, pair, occurrence, change) -> None:
        group = ("missing_stitching_via", severity, pair[0] if pair else key, change)
        entry = self.groups.get(group)
        if entry is None:
            entry = self.groups[group] = {"check": "missing_stitching_via", "severity": severity,
                                          "net": name, "occurrences": [], "length": 0.0,
                                          "reference_net_change": change}
            if pair:
                entry["pair"] = pair[0]
                entry["net"] = None
        entry["occurrences"].append((0.0, occurrence))

    def grouped(self) -> list[dict]:
        out = []
        for entry in self.groups.values():
            occurrences = sorted(entry.pop("occurrences"), key=lambda item: -item[0])
            length = entry.pop("length")
            finding = {key: value for key, value in entry.items()
                       if value is not None and value != []}
            finding["count"] = len(occurrences)
            check = finding["check"]
            if check != "missing_stitching_via":
                finding["length_mil"] = round(length, 2)
            likely = sum(1 for _, occurrence in occurrences if occurrence.get("likely"))
            if likely:
                finding["likely"] = True
                finding["likely_occurrences"] = likely
            finding["occurrences"] = [occurrence for _, occurrence in occurrences]
            finding["issue"] = self._issue(finding)
            out.append(finding)
        return out

    def _issue(self, finding: dict) -> str:
        units = self.units
        check, count = finding["check"], finding["count"]
        times = f"{count} place{'s' if count != 1 else ''}"
        prefix = "Likely: " if finding.get("likely") else ""
        if check == "missing_stitching_via":
            who = f"Pair {finding['pair']}" if finding.get("pair") else f"Net {finding['net']}"
            if finding["reference_net_change"]:
                return (f"{prefix}{who} changes layers at {times} where its reference net "
                        "changes too, with no capacitor between the reference nets "
                        f"(pairs_without_capacitor) within {_length_text(self.stitch, units)}: "
                        "the return current has no short path between them.")
            return (f"{prefix}{who} changes layers at {times} with no via of the reference net "
                    f"joining both reference layers within {_length_text(self.stitch, units)}.")
        where = (f"{finding['net']} on {finding['layer']} over {finding['reference_layer']}")
        length = _length_text(finding["length_mil"], units)
        if check == "segment_crosses_split":
            nets = " / ".join(finding["nets"])
            return (f"{prefix}{where} crosses from one reference net to another ({nets}) at "
                    f"{times}: the return current must detour around the split.")
        if check == "segment_over_void":
            return (f"{prefix}{where} runs over a gap in its reference copper at {times} "
                    f"({length} in all).")
        return (f"{prefix}{where} runs within {_length_text(finding['edge_distance_mil'], units)}"
                f" of the reference copper's edge at {times} ({length} in all).")


def _boundary(chain: _Chain, inside: float, toward: float, test) -> float:
    """Where a chain leaves the region test holds, walking from inside (where it holds)
    toward the other end: the first sample out, refined by bisection to 0.01 mil; toward
    itself when the walk never leaves it."""
    steps = max(1, math.ceil(abs(toward - inside) / SAMPLE_STEP_MIL))
    previous = inside
    for step in range(1, steps + 1):
        s = inside + (toward - inside) * step / steps
        if not test(*chain.point(s)):
            low, high = previous, s
            while abs(high - low) > 0.01:
                middle = (low + high) / 2.0
                if test(*chain.point(middle)):
                    low = middle
                else:
                    high = middle
            return high
        previous = s
    return toward


def _off_edge(centre: list[list], offset: list[list]) -> list[tuple[float, float, str | None]]:
    """t ranges where the centre line is over reference copper and the offset line is not
    over that net's copper (own zones on either line left out)."""
    out = []
    i = j = 0
    while i < len(centre) and j < len(offset):
        c, o = centre[i], offset[j]
        t0, t1 = max(c[2], o[2]), min(c[3], o[3])
        if t1 > t0 and c[0] == "ref" and o[0] != "own_zone" and not (
                o[0] == "ref" and o[1] == c[1]):
            if out and abs(out[-1][1] - t0) <= 1e-9 and out[-1][2] == o[1]:
                out[-1] = (out[-1][0], t1, o[1])
            else:
                out.append((t0, t1, o[1]))
        if c[3] <= o[3]:
            i += 1
        else:
            j += 1
    return out


# ---------- the API ----------


def _parse_checks(checks: list[str] | None) -> tuple[list[str] | None, dict | None]:
    if not checks:
        return list(CHECKS), None
    selected = list(dict.fromkeys(_CHECK_ALIASES.get(check, check) for check in checks))
    invalid = [check for check in selected if check not in CHECKS]
    if invalid:
        return None, {"error": "invalid_checks",
                      "message": f"Unknown checks: {', '.join(invalid)}.",
                      "valid_checks": list(CHECKS)}
    return selected, None


def prepare_return_path(index, nets: list[str] | None = None,
                        net_classes: list[str] | None = None, layer: str | None = None,
                        checks: list[str] | None = None, edge_distance_mil: float | None = None,
                        stitch_radius_mil: float | None = None, units: str = "mil") -> dict:
    """The whole check, unpaged (render_return_path pages it), or an error."""
    selected_checks, error = _parse_checks(checks)
    if error:
        return error
    layer_id = None
    model = rp.reference_planes(index)
    if layer:
        layer_id = index.resolve_layer(layer)
        if layer_id is None or layer_id not in model.position:
            return {"error": "layer_not_found",
                    "message": f"No copper layer named '{layer}' in the stack.",
                    "layers": [model.layer_name(item) for item in model.order]}
    routed = {
        index.pcb.nets[row["net_id"]].lower()
        for row in index.db.execute(
            "SELECT DISTINCT net_id FROM prims WHERE kind IN ('track', 'arc') "
            "AND pour_id IS NULL AND poured_by IS NULL AND net_id IS NOT NULL "
            "AND layer BETWEEN 1 AND 32")
        if 0 <= row["net_id"] < len(index.pcb.nets) and index.pcb.nets[row["net_id"]]
    }
    skipped: dict = {}
    if nets or net_classes:
        net_ids, unknown, unknown_classes = select_nets(index, nets, net_classes)
        if unknown:
            return {"error": "net_not_found",
                    "message": "No PCB net or differential pair named "
                    + ", ".join(f"'{name}'" for name in unknown) + "."}
        if unknown_classes:
            return {"error": "net_class_not_found",
                    "message": "No net class named "
                    + ", ".join(f"'{name}'" for name in unknown_classes) + ".",
                    "net_classes": sorted({item.name for item in index.pcb.classes
                                           if item.kind == "net"}, key=str.lower)[:50]}
        selected = {index.pcb.nets[net_id].lower(): ["requested"] for net_id in net_ids}
        requested = True
    else:
        selected, skipped = default_selection(index, routed)
        requested = False
    check = _Check(index, selected, requested, layer_id, set(selected_checks),
                   edge_distance_mil,
                   DEFAULT_STITCH_RADIUS_MIL if stitch_radius_mil is None else stitch_radius_mil,
                   units)
    check.layers()
    check.nets()
    check.layer_findings(check.selected_segments)
    plane_splits = check.plane_splits()
    findings = check.raw_findings + check.grouped()
    findings.sort(key=lambda finding: (
        _SEVERITY_RANK[finding["severity"]], _CHECK_RANK[finding["check"]],
        -finding.get("length_mil", 0.0), -finding.get("count", 1),
        str(finding.get("net") or finding.get("pair") or finding.get("layer") or "")))
    by_reason = Counter(reason for reasons in selected.values() for reason in reasons)
    nets_out = []
    for key in sorted(selected, key=lambda item: (-check.net_stats[item]["length"], item)):
        stats = check.net_stats[key]
        row = {"net": check.net_name(key), "reasons": selected[key],
               "segments": stats["segments"], "length_mil": round(stats["length"], 2)}
        pair = check.partner.get(key)
        if pair:
            row["pair"] = pair[0]
        nets_out.append(row)
    transitions = dict(check.transitions)
    summary = {
        "nets_selected": len(selected),
        "selection": "requested" if requested else "default",
        "segments_checked": check.counts["segments_checked"],
        "length_checked_mil": round(check.counts["length_checked"], 2),
        "segments_without_reference": check.counts["segments_without_reference"],
        "signal_layers": len(check.layer_rows),
        "signal_layers_without_reference": sum(
            1 for row in check.layer_rows if not row["has_reference_copper"]),
        "plane_layers": len(model.planes()),
        "plane_splits": len(plane_splits),
        "layer_changes": {"total": sum(transitions.values()), **transitions},
        "severity_counts": dict(Counter(finding["severity"] for finding in findings)),
        "findings_by_check": dict(Counter(finding["check"] for finding in findings)),
    }
    if check.pair_transitions:
        summary["pair_layer_changes"] = dict(check.pair_transitions)
    selection = {"by_reason": dict(by_reason), "nets": nets_out}
    if skipped:
        selection["left_out"] = skipped
    notes = []
    if not selected:
        notes.append(
            "No net was selected: none is in a differential pair, a high-speed net class, an "
            "xSignal or an impedance or length rule's scope, and no net name suggests a clock "
            "or a high-speed bus. Name the nets to check with net= or net_class=.")
    elif not requested:
        notes.append(
            "Nets were selected by reasons (selection.by_reason): confirm they are the fast "
            "ones, or name others with net= or net_class=.")
    if check.counts["segments_without_reference"]:
        notes.append(
            f"{check.counts['segments_without_reference']} segments lie on signal layers "
            "with no plane or large pour reference and were not walked (no_adjacent_reference).")
    notes.append(
        "The copper is read as saved: repour polygons in Altium and save the PcbDoc first. A "
        "field solver or Altium's own Return Path rule (not decoded here) is the final word.")
    return {
        "checks_run": selected_checks,
        "parameters": {
            "stitch_radius_mil": round(check.stitch, 3),
            "edge_distance_mil": None if edge_distance_mil is None else round(edge_distance_mil, 3),
            "edge_distance_default": f"{EDGE_FACTOR:g}x the dielectric height to each "
                                     f"reference ({DEFAULT_EDGE_MIL:g} mil without one)",
            **({"layer": model.layer_name(layer_id)} if layer_id is not None else {}),
        },
        "summary": summary,
        "selection": selection,
        "layers": check.layer_rows,
        "plane_splits": plane_splits,
        "findings": findings,
        "approximations": APPROXIMATIONS,
        "notes": notes,
    }


def prepare_return_path_cached(index, **kwargs) -> dict:
    """prepare_return_path, cached on the index per arguments (the last CACHE_SIZE), so
    paging reuses it: treat the result as read-only."""
    key = repr(sorted(kwargs.items()))
    cache = index.__dict__.setdefault("_return_path_cache", {})
    held = cache.get(key)
    if held is not None:
        return held
    prepared = prepare_return_path(index, **kwargs)
    if "error" not in prepared:
        if len(cache) >= CACHE_SIZE:
            cache.pop(next(iter(cache)))
        cache[key] = prepared
    return prepared


def render_return_path(prepared: dict, offset: int = 0, limit: int = 100,
                       occurrences: int = OCCURRENCES_LISTED, nets: int = NETS_LISTED) -> dict:
    """One page: findings from offset (each with at most occurrences places), the selected
    nets listed up to nets, the rest whole."""
    offset = max(0, int(offset or 0))
    findings = prepared["findings"]
    page = []
    for finding in findings[offset:offset + limit]:
        listed = finding.get("occurrences")
        if listed is not None and len(listed) > occurrences:
            finding = {**finding, "occurrences": listed[:occurrences],
                       "occurrences_listed": occurrences}
        page.append(finding)
    selection = dict(prepared["selection"])
    if len(selection["nets"]) > nets:
        selection["nets_total"] = len(selection["nets"])
        selection["nets"] = selection["nets"][:nets]
    result = {key: value for key, value in prepared.items()
              if key not in ("findings", "selection", "approximations", "notes")}
    result["selection"] = selection
    result["findings"] = page
    result["total"] = len(findings)
    result["offset"] = offset
    result["has_more"] = offset + limit < len(findings)
    if result["has_more"]:
        result["next_offset"] = offset + limit
    result["approximations"] = prepared["approximations"]
    result["notes"] = prepared["notes"]
    return result


def check_return_path(index, offset: int = 0, limit: int = 100, **kwargs) -> dict:
    """The check in one call: prepare_return_path and one page of it."""
    prepared = prepare_return_path(index, **kwargs)
    if "error" in prepared:
        return prepared
    return render_return_path(prepared, offset, limit)
