"""Design-for-manufacture (DFM) review of a PcbIndex against a fabricator's capability
profile (fab_profiles.Profile): trace width, copper spacing (hole walls to copper
included), hole sizes, annular rings, aspect ratios, hole to hole, copper and holes to
the board edge, layer count, copper weight, board size, drill sizes, and the board's own
rules against the profile; and assembly (DFA) for the active variant over
dfm_assembly's facts: placements and unique parts, the sides assembled, part spacing,
parts near the edge, fiducials, tombstoning of small passives, fine pitch and the
pick-and-place data (centres as Altium writes them, and the board origin).

Pure logic, no MCP. Lengths are mils under *_mil keys, so callers can convert units;
ratios and counts carry no unit. Issue texts are written in the units asked for, with
enough decimals that a value and its limit differ. Coordinates (at_mil) are relative to
the board origin (origin_mil).

review_dfm computes the whole classified result once and caches it on the index (the
copper geometry is dfm_geometry's, cached there); page_dfm pages it. A finding is a class
of objects fixed together, with its count, the worst value (and the range), sample nets
and the worst examples (one per place): trace width per layer and width, spacing per
family, routed copper (tracks, vias, pads) or area copper (pours, regions, fills),
counted in places (the distinct spots where two nets' copper comes too close, however
many object pairs meet there), hole walls to copper per copper kind, hole size per drill
row, annular ring and aspect ratio per via type or pad hole, hole to hole per pair kind,
copper and holes to the edge per object kind and edge. results gives every check's
status, severity, limit and worst value; board_minimums what the board needs from any
fabricator, whatever the profile.

Status: fail when a miss of severity error or warning is not marginal, marginal when
every such miss is, advisory when the only misses are info (a service limit of Altimade
while it is only the default, more drill sizes than the profile's), else pass; and
not_checked with a reason.

Comparisons: scalars (width, drill, ring, size) miss a limit by more than EPS_MIL,
geometric gaps by more than CLEARANCE_TOLERANCE_MIL (grid rounding). A miss within the
profile's tolerance_percent of the limit (for a length at most MARGINAL_CAP_MIL) is
marginal, a warning; the text names mm/mil rounding only when the limit's round value in
the other unit lies inside that band (a 0.1 mm ring against "4 mil (0.1016mm)"). Aspect
ratios are the copper-to-copper depth over the hole as drawn (Altium's finished hole),
compared at one decimal as fabricators quote them (check_vias' default).

Left out of spacing, and said so: net ties (ComponentKind 3/4) against the nets they join,
no-net against no-net copper, touching nets an enabled ShortCircuit rule allows, copper
lying wholly inside a valid pour (dfm_geometry.VALID_POUR_ITEMS). Copper of pours
modified since they were poured becomes one warning per pour; no-net footprint copper
touching its own part's pad is an info finding. Pads of one footprint form their own
class: only the land pattern fixes them. Overlapping and coincident holes are reported
whatever the profile (they need no limit). Annular rings skip pads whose hole is offset
(the offset is not decoded) and inner layers where the copper is no larger than the hole
(a removed, non-functional pad). Back drills are not in the drill table.

Assembly: the parts the assembler places are the fitted parts (AssemblyContext.is_dnp)
of a BOM kind with soldered pads, fiducials and mounting holes left out. Not Fitted parts
are left out of every assembly check (a DNP alternate footprint may overlap the fitted
one on purpose), and part spacing searches each part's nearest neighbour among the
fitted ones: it measures every fitted part on the board with pads or a body (a mounting
hole's screw head needs room too); a part the assembler does not place overlapping
others is its own info class. Part to edge, tombstoning and pitch take the placed parts.
A limit the profile does not set is measured, not judged: part spacing and part to edge
are then not_checked, unless their own classes found something (overlapping parts are
advisory; pads crossing the edge, bodies past it and edge connectors are info), fine
pitch is advisory for parts at FINE_PITCH_MIL or finer, and fiducials are compared with
typical counts (GLOBAL_FIDUCIALS per assembled side, LOCAL_FIDUCIALS near parts at
LOCAL_FIDUCIAL_PITCH_MIL or finer) as info. Max placements and unique parts are
Altimade's ordering limits: info while Altimade is only the default. Fiducials report
their misses as missing_global and local_parts, not one objects count. Pick and place
needs no limit: fitted parts without pads (Altium writes their centre at the absolute
origin) are a warning when they are in the BOM or named fiducials; asymmetric parts
(Center-X/Y off the body), an origin away from the outline's lower-left corner and
Ref-X/Y far from Center-X/Y (not a miss) are info.
"""

import heapq
import itertools
import json
import math
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable

try:
    from parsers.pcb_doc import MULTI_LAYER, pad_copper_on
    from services import dfm_assembly as assembly
    from services import dfm_geometry as geometry
    from services.bom import KIND_NAMES, natural_key
    from services.fab_profiles import MM
    from services.pcb_compliance import (
        CLEARANCE_TOLERANCE_MIL,
        EPS_MIL,
        _Checker as RuleChecker,
        resolve_clearance_rule,
        smallest_via_pad,
    )
    from services.via_checks import _Stack, is_microvia, via_depth, via_span
except ImportError:
    from server.parsers.pcb_doc import MULTI_LAYER, pad_copper_on
    from server.services import dfm_assembly as assembly
    from server.services import dfm_geometry as geometry
    from server.services.bom import KIND_NAMES, natural_key
    from server.services.fab_profiles import MM
    from server.services.pcb_compliance import (
        CLEARANCE_TOLERANCE_MIL,
        EPS_MIL,
        _Checker as RuleChecker,
        resolve_clearance_rule,
        smallest_via_pad,
    )
    from server.services.via_checks import _Stack, is_microvia, via_depth, via_span

CHECKS = (
    "trace_width",
    "spacing",
    "hole_size",
    "annular_ring",
    "aspect_ratio",
    "hole_to_hole",
    "copper_to_edge",
    "layer_count",
    "copper_weight",
    "board_size",
    "drill_count",
    "rules_vs_profile",
    # assembly (DFA)
    "part_counts",
    "assembly_sides",
    "part_spacing",
    "part_to_edge",
    "fiducials",
    "tombstoning",
    "fine_pitch",
    "pick_and_place",
)
VALID_CHECKS = CHECKS
_ASSEMBLY = CHECKS[CHECKS.index("part_counts"):]
EXAMPLES = 5  # worst examples kept per finding (page_dfm lists fewer)
NETS_LISTED = 5
PAGE_NETS = 3  # page_dfm keeps this many sample nets, or as many as examples
LAYERS_LISTED = 4
RULES_LISTED = 5
KINDS_LISTED = 4  # object kinds named in a spacing class's issue text
# check_dfm's shrink loop: (findings per page, examples per finding), then the drill
# table cut to DRILL_ROWS_LISTED rows.
SHRINK_STEPS = ((100, 5), (40, 3), (15, 1), (5, 0))
DRILL_ROWS_LISTED = 20
CACHE_SIZE = 4
# Altium's Layer Stack Manager counts 1 oz copper as 1.4 mil.
OZ_MIL = 1.4
# Altium's default HoleSize rule minimum: a rule left at it checks nothing.
HOLE_SIZE_DEFAULT_MIL = 1.0
# The marginal band of a length: tolerance_percent of the limit, at most this (a 2% band
# of a 5 mm edge limit or a 378 mm board would be 0.1 mm or 7.6 mm, not rounding).
MARGINAL_CAP_MIL = 1.0
# Places: a spot is the meeting point rounded to this (mil), per layer and object pair.
PLACE_MIL = 0.1
# Typical DFA values used when the profile sets no limit (texts and info findings only,
# never a fail; map_limits: Altium's fiducial article and the common rule of thumb):
# global fiducials per assembled side, local fiducials near a fine-pitch part and the
# pitch at or below which a part wants them, and the pitch fine_pitch lists as info.
GLOBAL_FIDUCIALS = 3
LOCAL_FIDUCIALS = 2
LOCAL_FIDUCIAL_PITCH_MIL = 0.5 * MM
FINE_PITCH_MIL = 0.5 * MM
# Altium's DFA article: top-side SMD parts 125 mil from the edge without breakaway rails
# (guidance in a reason text, not a default).
PART_EDGE_GUIDANCE_MIL = 125.0
# Pick and place (map_pnp: a survey of 7867 placed parts on 83 boards of a private set;
# no assembler publishes a number). Asymmetric parts, info: Center-X/Y at least
# ASYMMETRIC_SMD_MIL from the body's box centre (the pad copper extent's without a body)
# for an SMD or mixed part, ASYMMETRIC_THT_MIL for a through-hole one, whose bodies
# often overhang their pins (map: 79 SMD-only parts past 0.5 mm, 31 past 1 mm; 72
# through-hole or mixed parts past 2 mm; through the check, 83 SMD or mixed parts on 53
# boards and 69 through-hole parts on 27). Ref-X/Y more than REFERENCE_OFFSET_MIL from
# Center-X/Y is listed for assemblers who place from Ref-X/Y, never a miss (corpus: 1616
# parts on 78 of 83 boards, 1154 of them through-hole with a pin-1 origin).
ASYMMETRIC_SMD_MIL = 0.5 * MM
ASYMMETRIC_THT_MIL = 2.0 * MM
REFERENCE_OFFSET_MIL = 0.5 * MM
# Pad-less parts of these kinds (Standard, Net Tie in the BOM, Jumper) are in the BOM and
# the pick-and-place file: a warning; Mechanical, Graphical and No-BOM ones are info.
PNP_BOM_KINDS = (0, 3, 6)

_SEVERITY_RANK = {"error": 0, "warning": 1, "info": 2}
_SEQUENCE = itertools.count()
_AREA_KINDS = ("pour", "region", "fill")
_KIND_NAMES = {
    "track": ("track", "tracks"), "arc": ("arc", "arcs"), "via": ("via", "vias"),
    "pad": ("pad", "pads"), "pour": ("piece of pour copper", "pieces of pour copper"),
    "region": ("region", "regions"),
    "fill": ("fill", "fills"), "bare_hole": ("bare hole", "bare holes"),
}
_HOLE_LIMITS = {"via": "min_via_drill", "pth": "min_pth_drill", "npth": "min_npth_drill"}
_DRILL_NAMES = {"via": "via", "pth": "plated (PTH)", "npth": "non-plated (NPTH)"}

NOT_CHECKED = {
    "solder_mask": "Solder mask slivers, mask expansion and mask dams are not checked.",
    "silkscreen": "Silkscreen to mask, silkscreen to silkscreen and text size are not "
                  "checked.",
    "acute_angles": "Acute angles (acid traps) in copper are not checked.",
    "copper_text": "Copper text (strings on signal layers) is not read: its width, "
                   "spacing and edge distance are not checked.",
    "plane_pullback": "Internal planes: pullback from the board edge, plane clearances and "
                      "split gaps are not read.",
    "same_net_gaps": "Gaps between copper of one net (thermal relief air gaps, slivers) "
                     "are not checked.",
    "panels": "Embedded board arrays (panels) are not read.",
    "courtyards": "Courtyard outlines are not read: part spacing uses the 3D bodies' "
                  "outlines and the pads' copper (a part without a body: a rectangle around "
                  "its pads).",
    "other_side_pins": "Part spacing compares parts on one side: through-hole pins and "
                       "bodies reaching through to the other side are not checked against "
                       "that side's parts (a bottom SMD part on a top connector's pins).",
    "component_heights": "Component heights against an enclosure or a height limit are "
                         "not checked.",
    "pick_and_place_rotation": "Pick-and-place rotations against the assembler's zero "
                               "orientation (IPC-7351 or the tape's) are not checked: "
                               "Altium writes each part's own rotation. Ref-X/Y, Pad-X/Y "
                               "and the Y-flip option were not compared with Altium's "
                               "files.",
    "paste_stencil": "Paste mask and stencil apertures (paste reduction, step stencils) "
                     "are not checked.",
}
_SIDES = ("top", "bottom")


def side_counts(counts: dict) -> dict[str, dict[str, int]]:
    """Placements per side from dfm_assembly.placements' counts, as the assembly checks
    count them: SMD (mixed parts included), THT (mixed included) and all."""
    return {side: {"smd": counts[side]["smd"] + counts[side]["mixed"],
                   "tht": counts[side]["tht"] + counts[side]["mixed"],
                   "all": sum(counts[side].values())} for side in _SIDES}


@dataclass(frozen=True)
class AssemblyContext:
    """The active variant for the assembly checks. is_dnp: VariantState.is_dnp, applied
    to a part's netlist name, else its PCB designator (None: every part fitted);
    variant: its name; dnp_key: a hashable copy of its Not Fitted list (the cache key);
    unique_parts: the BOM line count of the fitted parts when a netlist is loaded (as
    get_bom groups them), None without one (then distinct footprint and Comment,
    approximate); netlist_free: no netlist is loaded."""

    variant: str | None = None
    is_dnp: Callable[[str], bool] | None = field(default=None, compare=False)
    dnp_key: tuple = ()
    unique_parts: int | None = None
    netlist_free: bool = False

    def key(self) -> tuple:
        return (self.variant, self.dnp_key, self.unique_parts, self.netlist_free)


def stack_total_thickness(layers, substack: str | None = None) -> float | None:
    """The board's thickness from its layer stack (mil): every stack layer's copper or
    dielectric height, solder mask and coverlay layers included; None when a stack layer
    has no height (or there is none). substack (a layer stack id, upper case) counts only
    the layers that stack holds (PcbLayer.substacks; None when a layer does not say)."""
    total = 0.0
    for layer in layers:
        if layer.stack_order is None or layer.kind == "non-stack":
            continue
        if substack is not None:
            if layer.substacks is None:
                return None
            if substack not in layer.substacks:
                continue
        height = layer.copper_thick_mil if layer.kind == "copper" else layer.diel_height_mil
        if height is None:
            return None
        total += height
    return total or None


def _round(value, digits: int = 4):
    return None if value is None else round(value, digits)


def _text(mil: float, to_mm: bool, digits: int | None = None) -> str:
    """A length in the output unit, 3 decimals in mil and 4 in mm (or digits), trailing
    zeros dropped."""
    if to_mm:
        value, unit, digits = mil / MM, "mm", 4 if digits is None else digits
    else:
        value, unit, digits = mil, "mil", 3 if digits is None else digits
    text = f"{value:.{digits}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return f"{'0' if text in ('-0', '') else text} {unit}"


def _pair(value: float, limit: float, to_mm: bool) -> tuple[str, str]:
    """value and limit as texts with enough decimals (up to 3 more) to differ."""
    base = 4 if to_mm else 3
    for digits in range(base, base + 4):
        first, second = _text(value, to_mm, digits), _text(limit, to_mm, digits)
        if first != second:
            break
    return first, second


def _plural(count: int, kind: str) -> str:
    one, many = _KIND_NAMES.get(kind, (kind, kind + "s"))
    return f"{count} {one if count == 1 else many}"


def _ratio(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _hole_name(plated: bool, count: int) -> str:
    """A hole wall's name: a non-plated hole, or a plated hole with no pad around it."""
    if plated:
        return "plated hole without a pad" if count == 1 else "plated holes without pads"
    return "non-plated hole" if count == 1 else "non-plated holes"


# ---------- finding classes ----------


class _Group:
    """One finding: a class of objects with its count, sample nets and layers, and the
    EXAMPLES worst objects (a bounded heap, one per place). miss: the objects miss a
    profile limit (they set the check's status); margin: how far past the limit,
    relative, for the order of findings. by_place: count the distinct places added, not
    the objects (spacing: object pairs meeting at one spot are one place; pairs counts
    the objects). low and high: the smallest and largest order added (a gap range)."""

    def __init__(self, check: str, severity: str, *, marginal: bool = False,
                 miss: bool = True, margin: float = 0.0, by_place: bool = False, **fields):
        self.check = check
        self.severity = severity
        self.marginal = marginal
        self.miss = miss
        self.margin = margin
        self.by_place = by_place
        self.fields = fields
        self.count = 0
        self.pairs = 0
        self.low = math.inf
        self.high = -math.inf
        self.places: set = set()
        self.kinds: Counter | None = None  # places per object kinds (spacing families)
        self.nets: dict[str, None] = {}
        self.layers: dict[int, None] = {}
        self.examples: list[tuple] = []
        self.board_rule: dict | None = None
        # (value key, range key): the finding gives low as the value and [low, high] as
        # the range when they differ (gap_mil, gap_range_mil).
        self.range_keys: tuple[str, str] | None = None
        self.describe = None  # group -> issue text
        # hole_size: min, max or route (which board rule fits it); "hole": a hole-wall
        # class of spacing or copper_to_edge (no copper rule rides on it).
        self.side = None

    def add(self, order: float, example, nets=(), layers=(), place=None) -> bool:
        """One object; order: smaller is worse; example: a callable giving its example
        entry (only the kept ones are built); place: a hashable spot. An object at a
        place already added gives no second example (and, by_place, no second count):
        added worst first, the place keeps its worst object. True for a new place."""
        self.pairs += 1
        self.low = min(self.low, order)
        self.high = max(self.high, order)
        for net in nets:
            if net is not None and net not in self.nets and len(self.nets) < NETS_LISTED:
                self.nets[net] = None
        for layer in layers:
            self.layers[layer] = None
        new = place is None or place not in self.places
        if new or not self.by_place:
            self.count += 1
        if not new:
            return False
        if place is not None:
            self.places.add(place)
        item = (-order, -next(_SEQUENCE), example)
        if len(self.examples) < EXAMPLES:
            heapq.heappush(self.examples, item)
        elif item > self.examples[0]:
            heapq.heapreplace(self.examples, item)
        return True


def _place(layer, at, *keys) -> tuple:
    """A spot: the layer, the point rounded to PLACE_MIL and what meets there."""
    return (layer, round(at[0] / PLACE_MIL), round(at[1] / PLACE_MIL), *keys)


# ---------- the review ----------


class _Review:
    def __init__(self, index, profile, checks: tuple[str, ...], to_mm: bool,
                 variant: AssemblyContext | None = None):
        self.index = index
        self.variant = variant or AssemblyContext()
        self.pcb = pcb = index.pcb
        self.profile = profile
        self.checks = checks
        self.to_mm = to_mm
        self.stack = _Stack(index)
        self.signal = index.signal_layers()
        order = list(self.signal)
        self.outer = {order[0], order[-1]} if order else set()
        self.layer_names = {
            layer.v6_id: layer.name for layer in pcb.board.layers if layer.v6_id is not None
        }
        self.origin = (pcb.board.origin_x or 0.0, pcb.board.origin_y or 0.0)
        self.tolerance = (profile.get("tolerance_percent") or 0.0) / 100.0
        self.checker = RuleChecker(index, 1)
        self.holes = geometry.holes(index)
        self.groups: list[_Group] = []
        self.results: dict[str, dict] = {}
        self.minimums: dict = {}
        self.not_checked: dict[str, str] = {}
        self.notes: list[str] = []
        # dfm_geometry results of the checks run (board_minimums reads them).
        self.spacing_result = None
        self.edge_result = None
        self.hole_result = None
        self.wall_result = None  # hole walls to copper (spacing)
        self.hole_edge_result = None  # hole walls to the edge (copper_to_edge)
        # Assembly: the active variant's placements and Not Fitted parts (built on first
        # use), and the measured part gap and part edge of the checks run.
        self._placements = None
        self._placed: list | None = None
        self._not_fitted: set[int] | None = None
        self.part_gap = None  # (nearest PartGap | None, overlapping pairs, reach)
        self.part_edge = None  # (distance, basis, reach) | (None, None, reach)
        self._position_names: dict[int, str] = {}
        self._short_rules: dict[tuple, bool] = {}

    # --- names and formatting ---

    def len(self, mil: float) -> str:
        return _text(mil, self.to_mm)

    def pair(self, value: float, limit: float) -> tuple[str, str]:
        return _pair(value, limit, self.to_mm)

    def at(self, x: float, y: float) -> list[float]:
        return [round(x - self.origin[0], 3), round(y - self.origin[1], 3)]

    def net(self, net: int | None) -> str | None:
        nets = self.pcb.nets
        return nets[net] if net is not None and 0 <= net < len(nets) else None

    def designator(self, component: int | None) -> str | None:
        if component is None or not 0 <= component < len(self.pcb.components):
            return None
        part = self.pcb.components[component]
        return part.designator or part.refdes or f"#{component}"

    def layer(self, v6_id: int | None) -> str:
        if v6_id is None:
            return "no layer"
        return self.signal.get(v6_id) or self.layer_names.get(v6_id) or f"layer {v6_id}"

    def position_name(self, position: int) -> str:
        if position not in self._position_names:
            self._position_names[position] = self.layer(self.stack.order[position])
        return self._position_names[position]

    def span_label(self, span) -> str | None:
        if span is None:
            return None
        return f"{self.position_name(span[0])} - {self.position_name(span[1])}"

    def copper_label(self, copper) -> str:
        """'Pad U3-12 (GND)', 'Track (CLK)', 'Region of U5 (no net)'."""
        kind = copper.kind
        owner = self.designator(copper.component)
        if kind == "pad":
            name = self.pcb.primitives[copper.row - 1].pad_name or ""
            label = f"Pad {owner}-{name}" if owner else f"Pad {name}".strip()
        elif kind == "pour":
            label = "Pour"
        else:
            label = kind.capitalize() + (f" of {owner}" if owner else "")
        return f"{label} ({self.net(copper.net) or 'no net'})"

    def pour_label(self, number: int) -> str:
        pour = next((pour for pour in self.pcb.pours if pour.pour_id == number), None)
        if pour is None:
            return f"Polygon #{number}"
        net = self.net(pour.net)
        return f"Polygon on {self.layer(pour.layer)}" + (f" ({net})" if net else " (no net)")

    def layers_field(self, group: _Group) -> list[str] | None:
        """The group's layers in stack order, when there are at most LAYERS_LISTED."""
        order = {layer: number for number, layer in enumerate(self.stack.order)}
        layers = sorted(group.layers, key=lambda layer: (order.get(layer, len(order)), layer))
        names = [self.layer(layer) for layer in layers]
        return names if 0 < len(names) <= LAYERS_LISTED else None

    def where(self, group: _Group) -> str:
        """' on L2', ' on 2 outer layers', ' on 14 inner layers', ' on 16 layers (outer
        and inner)', or ''."""
        count = len(group.layers)
        if count == 1:
            return f" on {self.layer(next(iter(group.layers)))}"
        if not count:
            return ""
        outer = sum(1 for layer in group.layers if layer in self.outer)
        if outer == count:
            return f" on {count} outer layers"
        return f" on {count} inner layers" if not outer else f" on {count} layers (outer and inner)"

    # --- limits ---

    def band(self, limit: float, length: bool = True) -> float:
        """How far past a limit a miss is marginal: tolerance_percent of it, for a length
        at most MARGINAL_CAP_MIL."""
        band = abs(limit) * self.tolerance
        return min(band, MARGINAL_CAP_MIL) if length else band

    def below(self, value: float, limit: float, eps: float, length: bool = True) -> str | None:
        """None when value meets the minimum limit, 'marginal' when it misses by no more
        than the band, else 'miss'."""
        if value >= limit - eps:
            return None
        return "marginal" if value >= limit - self.band(limit, length) - eps else "miss"

    def above(self, value: float, limit: float, eps: float, length: bool = True) -> str | None:
        if value <= limit + eps:
            return None
        return "marginal" if value <= limit + self.band(limit, length) + eps else "miss"

    def rounding(self, key: str, limit: float, side: str = "min") -> str | None:
        """The limit's round value in the other unit (0.01 mm or 0.1 mil) when it misses
        the limit inside the marginal band: a published limit the designer may have
        rounded ('4 mil (0.1016mm)' read as 0.1 mm). None otherwise."""
        if not self.profile.published(key):
            return None
        band = self.band(limit)
        mm = round(limit / MM, 2)
        mil = round(limit, 1)
        for value, text in ((mm * MM, f"{mm:g} mm"), (mil, f"{mil:g} mil")):
            off = limit - value if side == "min" else value - limit
            if EPS_MIL < off <= band + EPS_MIL:
                return text
        return None

    def marginal_text(self, key: str, limit: float, side: str = "min",
                      length: bool = True, maker: str = "fabricator") -> str:
        """The marginal sentence: within tolerance_percent of the limit (or within
        MARGINAL_CAP_MIL, when that is the smaller), the published text when the profile
        has one, and mm/mil rounding only when it explains a miss (rounding)."""
        if length and self.band(limit) < abs(limit) * self.tolerance:
            within = self.len(MARGINAL_CAP_MIL)
        else:
            within = f"{self.tolerance * 100:g}%"
        text = f" Marginal: within {within} of the limit"
        published = self.profile.published(key)
        if published:
            rounded = self.rounding(key, limit, side) if length else None
            text += f" ('{published}' as published"
            if rounded:
                text += (f"; rounded to {rounded} the limit is missed, so mm/mil rounding may "
                         "explain it")
            text += ")"
        return text + f"; confirm with the {maker}."

    def group(self, check: str, severity: str, verdict: str = "miss", **fields) -> _Group:
        marginal = verdict == "marginal"
        if marginal and severity == "error":
            severity = "warning"
        group = _Group(check, severity, marginal=marginal, **fields)
        self.groups.append(group)
        return group

    def not_run(self, check: str, reason: str) -> None:
        self.results[check] = {"status": "not_checked", "reason": reason}

    def status(self, check: str) -> str | None:
        """The check's status from its findings so far: fail when a miss of severity
        error or warning is not marginal, marginal when every such miss is, advisory when
        its only misses are info, else pass; not_checked as recorded; None when the check
        was not asked for. A looser rule of rules_vs_profile is a warning miss even when
        it rides on the measured check's finding (covered_by)."""
        entry = self.results.get(check)
        if entry is None or entry["status"] == "not_checked":
            return None if entry is None else "not_checked"
        misses = [(group.severity, group.marginal) for group in self.groups
                  if group.check == check and group.miss]
        if check == "rules_vs_profile":
            misses += [("warning", bool(row.get("marginal"))) for row in entry.get("rules", ())
                       if row.get("covered_by")]
        real = [marginal for severity, marginal in misses if severity != "info"]
        if real:
            return "marginal" if all(real) else "fail"
        return "advisory" if misses else "pass"

    def no_limit(self, check: str, *keys: str) -> bool:
        """Record the check as not run when the profile sets none of keys."""
        if any(self.profile.is_set(key) for key in keys):
            return False
        names = " or ".join(keys)
        self.not_run(check, f"the profile sets no {names}.")
        return True

    # --- trace width ---

    def check_trace_width(self) -> None:
        """Tracks and arcs on signal layers (routing, footprint copper and a hatched
        pour's tracks): one object per arc (its chords) and per hatched pour."""
        rows = self.index.db.execute(
            "SELECT id, kind, net_id, layer, poured_by, source_id, x1, y1, x2, y2, width "
            "FROM prims WHERE kind IN ('track', 'arc') AND pour_id IS NULL AND width > 0 "
            "ORDER BY id"
        ).fetchall()
        signal = self.signal
        widths: dict[tuple, tuple] = {}  # owner -> (width, layer, row)
        for row in rows:
            if row["layer"] not in signal:
                continue
            if row["poured_by"] is not None:
                owner = ("pour", row["poured_by"])
            elif row["kind"] == "arc" and row["source_id"] is not None:
                owner = ("arc", row["source_id"])
            else:
                owner = ("prim", row["id"])
            known = widths.get(owner)
            if known is None or row["width"] < known[0]:
                widths[owner] = (row["width"], row["layer"], row)
        if widths:
            narrowest = min(width for width, _, _ in widths.values())
            at_min = [entry for entry in widths.values() if entry[0] <= narrowest + EPS_MIL]
            self.minimums["min_trace"] = {
                "value_mil": _round(narrowest),
                "layer": self.layer(min(at_min, key=lambda entry: entry[2]["id"])[1]),
                "count": len(at_min),
            }
        else:
            self.minimums["min_trace"] = {"value_mil": None, "layer": None, "count": 0}
        if "trace_width" not in self.checks or self.no_limit("trace_width", "min_trace"):
            return
        limit = self.profile.get("min_trace")
        self.results["trace_width"] = {
            "status": "pass", "limit_mil": _round(limit),
            "worst_mil": self.minimums["min_trace"]["value_mil"],
        }
        classes: dict[tuple, _Group] = {}
        for owner, (width, layer, row) in sorted(widths.items(), key=lambda item: item[1][2]["id"]):
            verdict = self.below(width, limit, EPS_MIL)
            if verdict is None:
                continue
            pour = owner[1] if owner[0] == "pour" else None
            key = (layer, round(width, 4), pour, verdict)
            group = classes.get(key)
            if group is None:
                group = classes[key] = self.group(
                    "trace_width", "error", verdict, margin=(limit - width) / limit,
                    width_mil=_round(width),
                    layer=self.layer(layer),
                )
                group.describe = self._describe_width(width, limit, layer, pour)
            x, y = (row["x1"] + row["x2"]) / 2.0, (row["y1"] + row["y2"]) / 2.0
            net = self.net(row["net_id"])
            group.add(width, self._example(x, y, net=net), nets=(net,))

    def _describe_width(self, width, limit, layer, pour):
        def describe(group: _Group) -> str:
            value, minimum = self.pair(width, limit)
            if pour is not None:
                text = (f"{self.pour_label(pour)} is a hatched pour drawn with {value} "
                        f"tracks, below the profile's minimum trace {minimum}: widen the "
                        "pour's track width.")
            else:
                kinds = "track or arc" if group.count == 1 else "tracks and arcs"
                text = (f"{group.count} {kinds} {value} wide on {self.layer(layer)}, below "
                        f"the profile's minimum trace {minimum}.")
            if group.marginal:
                text += self.marginal_text("min_trace", limit)
            return text
        return describe

    def _example(self, x: float, y: float, **fields):
        at = self.at(x, y)

        def make() -> dict:
            entry = {"at_mil": at}
            entry.update({key: value for key, value in fields.items() if value is not None})
            return entry
        return make

    # --- spacing ---

    def clearance_floor(self) -> tuple | None:
        """(rule, value_mil) of the enabled Clearance rule with the smallest copper
        clearance (its generic value or an object-matrix entry; hole and text entries
        are not copper to copper)."""
        best = None
        for rule in self.checker.enabled("Clearance"):
            values = self.checker.values(rule)
            candidates = [values.get("clearance_mil")] + [
                value for pair, value in (values.get("object_clearances_mil") or {}).items()
                if "Hole" not in pair and "Text" not in pair
            ]
            for value in candidates:
                if isinstance(value, (int, float)) and value > 0 and (
                    best is None or value < best[1]
                ):
                    best = (rule, value)
        return best

    def board_rule(self, rule, value: float) -> dict:
        return {
            "name": rule.name,
            "value_mil": _round(value),
            "checked_by_batch_drc": self.checker.batch_drc(rule.kind),
        }

    def short_allowed(self, first, second) -> bool:
        """An enabled ShortCircuit rule with Allowed set covers the two nets."""
        rules = self.checker.rules.get("ShortCircuit")
        if not rules:
            return False
        key = tuple(sorted((first.net, second.net), key=lambda net: (net is None, net)))
        if key not in self._short_rules:
            rule, _ = resolve_clearance_rule(
                rules, self.checker.target(self.net(key[0])),
                self.checker.target(self.net(key[1])), self.checker.ctx,
            )
            allowed = False
            if rule is not None:
                values = self.checker.values(rule)
                allowed = values.get("allowed", (values.get("other") or {}).get("allowed"))
            self._short_rules[key] = allowed is True
        return self._short_rules[key]

    def check_spacing(self) -> None:
        """Copper of different nets closer than min_spacing, and copper closer than
        min_hole_to_copper (falls back to min_spacing) to a hole wall: a non-plated hole
        or a plated hole without a pad (dfm_geometry.hole_to_copper)."""
        limit = self.profile.get("min_spacing")
        hole_limit = self.profile.get("min_hole_to_copper")
        default = geometry.DEFAULT_GAP_REACH_MIL
        result = geometry.spacing(self.index, max(limit or 0.0, default))
        walls = geometry.hole_to_copper(self.index, max(hole_limit or 0.0, default))
        self.spacing_result, self.wall_result = result, walls
        self._spacing_notes(result, walls)
        if self.no_limit("spacing", "min_spacing", "min_hole_to_copper"):
            return
        floor = self.clearance_floor()
        board_rule = (self.board_rule(*floor) if floor and limit is not None
                      and floor[1] < limit - EPS_MIL else None)
        entry = {"status": "pass", "limit_mil": _round(limit)}
        minimum = result.minimum() if limit is not None else None
        entry["worst_mil"] = _round(minimum.gap_mil) if minimum else None
        if board_rule:
            entry["board_rule"] = board_rule
        self.results["spacing"] = entry
        classes: dict[tuple, _Group] = {}
        if limit is not None:
            self._copper_gaps(result, limit, floor, board_rule, classes)
        else:
            self.not_checked["copper_gaps"] = (
                "The profile sets no min_spacing: gaps between copper of different nets "
                "were not compared, only hole walls to copper (min_hole_to_copper)."
            )
        if hole_limit is not None:
            nearest = walls.minimum()
            entry["hole_to_copper"] = {
                "limit_mil": _round(hole_limit),
                "worst_mil": _round(nearest.gap_mil) if nearest else None,
            }
            self._wall_gaps(walls, hole_limit, classes)
        else:
            self.not_checked["hole_walls"] = (
                "The profile removes min_hole_to_copper: copper near non-plated hole walls "
                "and plated holes without a pad was not compared."
            )

    def _copper_gaps(self, result, limit, floor, board_rule, classes) -> None:
        ignore_pads = [
            bool(self.checker.values(rule).get("ignore_pad_to_pad_in_footprint"))
            for rule in self.checker.enabled("Clearance")
        ]
        cutoff = limit - CLEARANCE_TOLERANCE_MIL
        allowed = 0
        for record in result.records:
            if record.gap_mil >= cutoff:
                break
            if "touching" in record.marks and not record.marks & {
                "stale_pour", "footprint_copper"
            } and self.short_allowed(record.first, record.second):
                allowed += 1
                continue
            key, make = self._gap_class(record, limit, floor, board_rule, ignore_pads)
            group = classes.get(key)
            if group is None:
                group = classes[key] = make()
            nets = (record.first.net, record.second.net)
            new = group.add(record.gap_mil, self._record_example(record),
                            nets=[self.net(net) for net in nets], layers=record.layers,
                            place=_place(record.layer, record.at, frozenset(nets)))
            if new and group.kinds is not None:
                group.kinds["-".join(sorted((record.first.kind, record.second.kind)))] += 1
        if allowed:
            self.notes.append(
                f"{allowed} touching pair(s) of nets an enabled ShortCircuit rule allows "
                "were not reported."
            )

    def _gap_class(self, record, limit, floor, board_rule, ignore_pads) -> tuple:
        """(class key, a function making the class's group) of a spacing record:
        a stale pour, unnetted footprint copper, touching copper (per object kinds), pads
        of one footprint, else routed copper (tracks, arcs, vias, pads) or area copper
        (a pour, region or fill on one side), marginal, and whether every Clearance rule
        of the board is broken too. A class holds every gap of its family (gap_mil the
        smallest, gap_range_mil the spread, kinds the object kinds by places): one fix,
        the rule and the routing or the pours' clearance and a repour, covers it."""
        marks, first, second = record.marks, record.first, record.second
        kinds = "-".join(sorted((first.kind, second.kind)))
        if "stale_pour" in marks:
            pour = record.stale_pours[0]
            return ("stale", pour), lambda: self._stale_group("spacing", pour, "to other copper")
        if "footprint_copper" in marks:
            def make():
                group = self.group("spacing", "info", miss=False, by_place=True)
                group.describe = self._describe_footprint_copper(first.component)
                return group
            return ("footprint_copper", first.component), make
        if "touching" in marks:
            def make():
                group = self.group("spacing", "error", margin=1.0, by_place=True, gap_mil=0.0,
                                   kinds=kinds)
                group.describe = self._describe_touching(kinds)
                return group
            return ("touching", kinds), make
        gap = record.gap_mil
        verdict = self.below(gap, limit, CLEARANCE_TOLERANCE_MIL) or "marginal"
        if "same_footprint" in marks:
            part = self.pcb.components[first.component]
            footprint = part.footprint or self.designator(first.component)

            def make():
                group = self.group("spacing", "warning", verdict, margin=(limit - gap) / limit,
                                   by_place=True, gap_mil=_round(gap), kinds=kinds,
                                   footprint=footprint)
                group.range_keys = ("gap_mil", "gap_range_mil")
                group.describe = self._describe_pad_gap(footprint, limit, ignore_pads)
                return group
            return ("footprint", footprint, verdict), make
        family = "area" if first.is_area or second.is_area else "routed"
        below_rule = floor is not None and gap < floor[1] - CLEARANCE_TOLERANCE_MIL

        def make():
            group = self.group("spacing", "error", verdict, margin=(limit - gap) / limit,
                               by_place=True, gap_mil=_round(gap), kinds=None)
            group.kinds = Counter()
            group.range_keys = ("gap_mil", "gap_range_mil")
            if below_rule:
                group.fields["drc_violation"] = True
            elif board_rule:
                group.board_rule = board_rule
            group.describe = self._describe_gap(family, limit, below_rule, floor)
            return group
        return ("gap", family, verdict, below_rule), make

    def _record_example(self, record):
        def make() -> dict:
            # Kinds' order (as in "pad-track"), so a pair prints the same either way.
            sides = sorted((record.first, record.second),
                           key=lambda copper: (copper.kind, self.copper_label(copper)))
            return {
                "at_mil": self.at(*record.at),
                "objects": " / ".join(self.copper_label(copper) for copper in sides),
                "layer": self.layer(record.layer),
            }
        return make

    def _wall_gaps(self, walls, limit, classes) -> None:
        """Hole walls (dfm_geometry.hole_to_copper) closer than limit to copper: the drill
        cuts or crowds it. Classes: stale pours (with spacing's), copper of the hole's own
        part, touching copper, else the copper's kind, the wall's plating and marginal."""
        cutoff = limit - CLEARANCE_TOLERANCE_MIL
        generic = self.profile.resolved("min_hole_to_copper") != "min_hole_to_copper"
        for record in walls.records:
            if record.gap_mil >= cutoff:
                break
            copper, hole, marks = record.copper, record.hole, record.marks
            kind = "track" if copper.kind == "arc" else copper.kind
            plated = hole.plated is not False
            if "stale_pour" in marks and copper.pour is not None:
                key = ("stale", copper.pour)
                if key not in classes:
                    classes[key] = self._stale_group("spacing", copper.pour, "to other copper")
            else:
                touching = "touching" in marks
                own = "same_part" in marks
                verdict = "miss" if touching else (
                    self.below(record.gap_mil, limit, CLEARANCE_TOLERANCE_MIL) or "marginal")
                key = ("wall", kind, plated, touching, hole.component if own else None,
                       verdict)
                if key not in classes:
                    # Below the min_spacing stand-in (the profile sets no hole-to-copper
                    # limit) or inside the part's own footprint: a warning; touching: an error.
                    severity = "warning" if (own or generic) and not touching else "error"
                    group = classes[key] = self.group(
                        "spacing", severity, verdict,
                        margin=(limit - record.gap_mil) / limit, by_place=True,
                        gap_mil=_round(record.gap_mil),
                        kinds="-".join(sorted(("hole", kind))), plated_hole=plated,
                    )
                    group.side = "hole"
                    group.fields["min_hole_to_copper_mil"] = _round(limit)
                    if own:
                        group.fields["footprint"] = self.footprint(hole.component)
                    group.range_keys = ("gap_mil", "gap_range_mil")
                    group.describe = self._describe_wall(kind, plated, touching, own, hole,
                                                         limit, generic)
            classes[key].add(record.gap_mil, self._wall_example(record),
                             nets=(self.net(copper.net), self.net(hole.net)),
                             layers=record.layers,
                             place=_place(record.layer, record.at, "wall", copper.net))

    def footprint(self, component: int | None) -> str | None:
        if component is None or not 0 <= component < len(self.pcb.components):
            return None
        return self.pcb.components[component].footprint or self.designator(component)

    def hole_label(self, hole) -> str:
        """'Pad U3-M1', 'Hole of U3', 'Hole', 'Via'."""
        owner = self.designator(hole.component)
        if hole.kind == "pad":
            name = self.pcb.primitives[hole.row - 1].pad_name or ""
            return f"Pad {owner}-{name}" if owner else f"Pad {name}".strip()
        if hole.kind == "bare_hole":
            return "Hole" + (f" of {owner}" if owner else "")
        return "Via"

    def _wall_example(self, record):
        def make() -> dict:
            plating = "non-plated hole" if record.hole.plated is False else "plated hole"
            return {
                "at_mil": self.at(*record.at),
                "objects": f"{self.hole_label(record.hole)} ({plating} "
                           f"{self.len(record.hole.hole_mil)}) / "
                           f"{self.copper_label(record.copper)}",
                "layer": self.layer(record.layer),
            }
        return make

    def _spacing_notes(self, result, walls) -> None:
        if result.net_tie_pairs:
            parts = sorted(self.designator(part) for part in result.net_tie_pairs)
            count = sum(result.net_tie_pairs.values())
            shown = ", ".join(parts[:NETS_LISTED]) + (" ..." if len(parts) > NETS_LISTED else "")
            self.notes.append(
                f"Net ties ({shown}): {count} close pair(s) between a net tie and the nets "
                "it joins are intended and were not reported."
            )
        if result.no_net_pairs:
            self.not_checked["no_net_gaps"] = (
                f"{result.no_net_pairs} pair(s) of no-net copper closer than "
                f"{self.len(result.reach_mil)} were not checked: no-net copper against "
                "no-net copper is not a short."
            )
        if not result.valid_pour_items_checked:
            self.not_checked["inside_valid_pours"] = (
                "Pads, vias and tracks of another net lying wholly inside a pour's copper "
                "(no pour edge near them) are not looked for in pours saved as poured: "
                "Altium pours around other nets. Regions and fills inside pours, and pours "
                "modified since they were poured, are checked."
            )
        stopped = [what for what, part in (("spacing", result), ("hole-to-copper", walls))
                   if part.stopped]
        if stopped:
            calls = result.distance_calls + walls.distance_calls
            self.not_checked["spacing_incomplete"] = (
                f"The {' and '.join(stopped)} search stopped after {calls} computations: "
                "findings and minimums are incomplete."
            )

    def gap_range(self, group: _Group, limit: float) -> tuple[str, str]:
        """('0.09 mm' or '0.09 mm to 0.12 mm', the limit) for a class's gaps."""
        low, minimum = self.pair(group.low, limit)
        high, _ = self.pair(group.high, limit)
        return (low if high == low else f"{low} to {high}"), minimum

    @staticmethod
    def places(group: _Group) -> str:
        """'3 places' (spots where copper meets; by_place groups count them)."""
        return f"{group.count} place{'' if group.count == 1 else 's'}"

    @staticmethod
    def object_pairs(group: _Group) -> str:
        """' (5 object pairs)' when more object pairs than places were added."""
        return f" ({group.pairs} object pairs)" if group.pairs > group.count else ""

    def _describe_gap(self, family, limit, below_rule, floor):
        def describe(group: _Group) -> str:
            span, minimum = self.gap_range(group, limit)
            listed = group.kinds.most_common()
            kinds = ", ".join(f"{kind} {count}" for kind, count in listed[:KINDS_LISTED])
            kinds += ", ..." if len(listed) > KINDS_LISTED else ""
            if family == "routed":
                what = f"tracks, vias or pads of two nets are {span} apart ({kinds})"
            else:
                what = (f"pour, region or fill copper is {span} from copper of another net "
                        f"({kinds})")
            text = (f"{self.places(group)} where {what}{self.where(group)}, below the "
                    f"profile's minimum spacing {minimum}{self.object_pairs(group)}.")
            if group.marginal:
                text += self.marginal_text("min_spacing", limit)
            if below_rule:
                text += " Below every Clearance rule of the board: an Altium DRC violation too."
            elif group.board_rule:
                text += (f" The board's Clearance rules go down to {self.len(floor[1])}: "
                         "Altium's DRC may pass them (board_rule).")
            return text
        return describe

    def _describe_touching(self, kinds):
        def describe(group: _Group) -> str:
            return (f"{self.places(group)} where {kinds} copper of two nets touches (gap 0)"
                    f"{self.where(group)}{self.object_pairs(group)}: a short unless intended.")
        return describe

    def _describe_pad_gap(self, footprint, limit, ignore_pads):
        def describe(group: _Group) -> str:
            span, minimum = self.gap_range(group, limit)
            gaps = "gap" if group.count == 1 else "gaps"
            text = (f"{group.count} pad {gaps} inside footprint {footprint}, {span}, below "
                    f"the profile's minimum spacing {minimum}: only the land pattern or the "
                    "part fixes them.")
            if group.marginal:
                text += self.marginal_text("min_spacing", limit)
            if ignore_pads and all(ignore_pads):
                text += (" The board's Clearance rules ignore pad-to-pad gaps inside a "
                         "footprint, so Altium's DRC does not flag them.")
            return text
        return describe

    def _describe_wall(self, kind, plated, touching, own, hole, limit, generic):
        """A hole-wall class: '2 tracks on L2 0.08 mm from non-plated holes (wall to
        copper), below ...'; touching: the drill cuts the copper (a plated hole: a short
        through its barrel)."""
        rule = "minimum spacing" if generic else "minimum hole-to-copper clearance"
        note = " (it sets no min_hole_to_copper)" if generic else ""
        owner = self.designator(hole.component)

        def describe(group: _Group) -> str:
            holes = (f"a {_hole_name(plated, 1)}" if group.count == 1
                     else _hole_name(plated, 2))
            if touching:
                text = (f"{self.places(group)} where {kind} copper touches or covers {holes}"
                        f"{self.where(group)}: " + (
                            "a short through the hole's barrel unless the nets are the same."
                            if plated else "the drill cuts the copper."))
                if own:
                    text += f" The copper is {owner}'s own: fix the footprint."
                return text
            span, minimum = self.gap_range(group, limit)
            text = (f"{self.places(group)} where {kind} copper is {span} from {holes} (wall "
                    f"to copper){self.where(group)}, below the profile's {rule} {minimum}"
                    f"{note}.")
            if own:
                text += f" The copper is {owner}'s own: only the land pattern fixes it."
            if group.marginal:
                text += self.marginal_text("min_hole_to_copper", limit)
            return text
        return describe

    def _describe_footprint_copper(self, component):
        def describe(group: _Group) -> str:
            nets = ", ".join(group.nets) or "a net"
            return (f"Unnetted copper of {self.designator(component)} touches its own pad "
                    f"on {nets} ({self.places(group)}): give the copper the pad's net, or "
                    "confirm it is intended.")
        return describe

    def _stale_group(self, check: str, pour: int, where: str) -> _Group:
        """One warning per pour modified since it was poured: its copper's places closer
        than the limit (where: 'to other copper', 'to the board edge')."""
        group = self.group(check, "warning", miss=False, by_place=True)

        def describe(group: _Group) -> str:
            places = "place" if group.count == 1 else "places"
            return (f"{self.pour_label(pour)} was modified since it was poured, so its saved "
                    f"copper is out of date ({group.count} {places} closer than the limit "
                    f"{where}). Repour and save before fabrication (check_pcb_health "
                    "shelved_modified_polygons).")
        group.describe = describe
        return group

    # --- holes ---

    def hole_class(self, hole) -> str:
        """via, pth or npth: the drill limit a hole takes (unknown plating: plated)."""
        if hole.kind == "via":
            return "via"
        return "npth" if hole.plated is False else "pth"

    @staticmethod
    def slot_length(hole) -> float | None:
        length = hole.slot_length_mil
        return length if hole.hole_shape in (1, 2) and length and length > hole.hole_mil else None

    def hole_span(self, hole) -> str | None:
        if hole.span is None and hole.kind == "via" and hole.row is not None:
            prim = self.pcb.primitives[hole.row - 1]
            return f"{self.layer(prim.layer)} - {self.layer(prim.layer_to)}"
        return self.span_label(hole.span)

    def hole_example(self, hole, **fields):
        return self._example(hole.x, hole.y, object=self.hole_label(hole),
                             net=self.net(hole.net), **fields)

    def drill_table(self) -> list[dict]:
        rows: Counter = Counter()
        for hole in self.holes:
            slot = self.slot_length(hole)
            rows[(hole.kind, round(hole.hole_mil, 4), _round(slot), hole.plated,
                  self.hole_span(hole))] += 1
        order = {"via": 0, "pad": 1, "bare_hole": 2}
        table = []
        for (kind, hole, slot, plated, span), count in sorted(
            rows.items(), key=lambda item: (order[item[0][0]], item[0][1], item[0][2] or 0.0,
                                            str(item[0][3]), str(item[0][4]))
        ):
            row = {"kind": kind, "hole_mil": hole}
            if slot is not None:
                row["slot_length_mil"] = slot
            row.update({"plated": plated, "span": span, "count": count})
            table.append(row)
        return table

    def check_hole_size(self) -> None:
        smallest: dict[str, float] = {}
        for hole in self.holes:
            kind = self.hole_class(hole)
            if hole.hole_mil < smallest.get(kind, math.inf):
                smallest[kind] = hole.hole_mil
        for kind in ("via", "pth", "npth"):
            self.minimums[f"min_{kind}_hole_mil"] = _round(smallest.get(kind))
        if "hole_size" not in self.checks or self.no_limit(
            "hole_size", "min_drill", "min_via_drill", "min_pth_drill", "min_npth_drill",
            "max_drill", "min_route_size",
        ):
            return
        limits = {kind: self.profile.get(key) for kind, key in _HOLE_LIMITS.items()}
        largest = self.profile.get("max_drill")
        route = self.profile.get("min_route_size")
        entry = {"status": "pass"}
        limit_fields = {kind: _round(value) for kind, value in limits.items() if value is not None}
        if largest is not None:
            limit_fields["max"] = _round(largest)
        entry["limit_mil"] = limit_fields
        entry["worst_mil"] = {kind: _round(value) for kind, value in smallest.items()}
        if largest is not None and self.holes:
            entry["worst_mil"]["max"] = _round(max(hole.hole_mil for hole in self.holes))
        self.results["hole_size"] = entry
        classes: dict[tuple, _Group] = {}
        for hole in self.holes:
            kind = self.hole_class(hole)
            slot = self.slot_length(hole)
            span = self.hole_span(hole)
            row = (hole.kind, round(hole.hole_mil, 4), _round(slot), hole.plated, span)
            tests = []
            if limits[kind] is not None:
                verdict = self.below(hole.hole_mil, limits[kind], EPS_MIL)
                if verdict:
                    tests.append(("min", verdict, limits[kind], _HOLE_LIMITS[kind]))
            if largest is not None:
                verdict = self.above(hole.hole_mil, largest, EPS_MIL)
                if verdict:
                    tests.append(("max", verdict, largest, "max_drill"))
            if route is not None and slot is not None and hole.hole_mil < route - EPS_MIL:
                tests.append(("route", "info", route, "min_route_size"))
            for side, verdict, limit, key in tests:
                class_key = (side, row, verdict)
                group = classes.get(class_key)
                if group is None:
                    if side == "route":
                        group = self.group("hole_size", "info", miss=False)
                    else:
                        margin = (abs(limit - hole.hole_mil) / limit) if limit else 0.0
                        group = self.group("hole_size", "error", verdict, margin=margin)
                    group.side = side
                    group.fields.update(kind=hole.kind, hole_mil=_round(hole.hole_mil))
                    if slot is not None:
                        group.fields["slot_length_mil"] = _round(slot)
                    group.fields["plated"] = hole.plated
                    group.fields[f"{key}_mil"] = _round(limit)
                    group.describe = self._describe_hole_size(side, kind, hole, slot, limit, key)
                    classes[class_key] = group
                group.add(hole.hole_mil if side != "max" else -hole.hole_mil,
                          self.hole_example(hole), nets=(self.net(hole.net),))

    def _describe_hole_size(self, side, kind, hole, slot, limit, key):
        def describe(group: _Group) -> str:
            value, bound = self.pair(hole.hole_mil, limit)
            what = _plural(group.count, hole.kind)
            if slot is not None:
                size = f"slot {value} x {self.len(slot)}"
            else:
                size = f"hole {value}"
            if side == "route":
                return (f"{what} with a {size}: narrower than the profile's minimum route "
                        f"size {bound} (read as the narrowest routed slot); confirm the "
                        "fabricator can make the slot.")
            if side == "max":
                text = f"{what} with a {size}, above the profile's maximum drill {bound}."
            else:
                text = (f"{what} with a {_DRILL_NAMES[kind]} {size}, below the profile's "
                        f"minimum {_DRILL_NAMES[kind]} drill {bound}.")
            text += " Holes are as drawn (finished); the fabricator drills larger."
            if group.marginal:
                text += self.marginal_text(key, limit, side)
            return text
        return describe

    # --- annular ring ---

    def pad_ring(self, prim, hole) -> tuple:
        """(ring, layer, (width, height)) of a plated pad hole's smallest ring over the
        signal layers, and the inner layers skipped as having no pad there."""
        if prim.layer == MULTI_LAYER:
            layers = list(self.signal)
        else:
            layers = [prim.layer] if prim.layer in self.signal else []
        slot = self.slot_length(hole)
        worst = None
        skipped = 0
        for layer in layers:
            copper = pad_copper_on(prim.layer_copper, layer)
            width, height = (
                (copper.width, copper.height) if copper is not None
                else (prim.width, prim.height or prim.width)
            )
            if max(width, height) <= hole.hole_mil + EPS_MIL and layer not in self.outer:
                skipped += 1  # a removed (non-functional) inner pad
                continue
            if slot is not None:
                turn = (prim.slot_rotation or 0.0) % 180.0
                if min(turn, 180.0 - turn) < 1.0:
                    along, across = width, height
                elif abs(turn - 90.0) < 1.0:
                    along, across = height, width
                else:
                    along, across = max(width, height), min(width, height)
                ring = min((across - hole.hole_mil) / 2.0, (along - slot) / 2.0)
            else:
                ring = (min(width, height) - hole.hole_mil) / 2.0
            if worst is None or ring < worst[0] - EPS_MIL:
                worst = (ring, layer, (width, height))
        return worst, skipped

    def check_annular_ring(self) -> None:
        primitives = self.pcb.primitives
        via_limit = self.profile.get("min_via_annular_ring")
        pad_limit = self.profile.get("min_pth_annular_ring")
        run = "annular_ring" in self.checks and not self.no_limit(
            "annular_ring", "min_annular_ring", "min_via_annular_ring", "min_pth_annular_ring")
        classes: dict[tuple, _Group] = {}
        smallest = {"via": None, "pth": None}  # rings above 0 (board_minimums)
        worst = {"via": None, "pth": None}  # no ring (0) included (results)
        offsets = skipped_layers = no_ring = 0
        for hole in self.holes:
            if hole.plated is False:
                continue
            if hole.kind == "via":
                prim = primitives[hole.row - 1]
                layers = self.stack.order[hole.span[0]: hole.span[1] + 1] if hole.span else []
                diameter, layer = smallest_via_pad(prim, layers)
                ring = (diameter - hole.hole_mil) / 2.0
                kind, pad = "via", (diameter,)
            elif hole.kind == "pad":
                if hole.hole_offset:
                    offsets += 1
                    continue
                prim = primitives[hole.row - 1]
                found, skipped = self.pad_ring(prim, hole)
                skipped_layers += skipped
                if found is None:
                    continue
                ring, layer, pad = found
                kind = "pth"
            else:  # a plated bare hole: no copper at all
                ring, layer, pad, kind = 0.0, None, None, "pth"
            if ring <= EPS_MIL:
                no_ring += 1
            elif smallest[kind] is None or ring < smallest[kind]:
                smallest[kind] = ring
            if worst[kind] is None or max(ring, 0.0) < worst[kind]:
                worst[kind] = max(ring, 0.0)
            limit = via_limit if kind == "via" else pad_limit
            if not run or limit is None:
                continue
            if ring <= EPS_MIL and kind == "pth":
                verdict = "miss"
                key = ("no_ring", hole.kind, round(hole.hole_mil, 4))
            else:
                verdict = self.below(ring, limit, EPS_MIL)
                if verdict is None:
                    continue
                key = ("ring", hole.kind, self.hole_span(hole), round(hole.hole_mil, 4),
                       _round(self.slot_length(hole)), tuple(round(size, 4) for size in pad),
                       layer, round(ring, 4), verdict)
            group = classes.get(key)
            if group is None:
                group = classes[key] = self._ring_group(key, hole, kind, ring, layer, pad, limit,
                                                        verdict)
            group.add(ring, self.hole_example(hole), nets=(self.net(hole.net),))
        self.minimums["min_via_ring_mil"] = _round(smallest["via"])
        self.minimums["min_pth_ring_mil"] = _round(smallest["pth"])
        if no_ring:
            self.minimums["plated_holes_without_ring"] = no_ring
        if offsets:
            self.not_checked["pad_hole_offsets"] = (
                f"{offsets} plated pad(s) have a hole offset from the pad centre, which is not "
                "decoded: their annular ring was not checked."
            )
        if skipped_layers:
            self.notes.append(
                f"{skipped_layers} inner pad layer(s) with copper no larger than the hole were "
                "read as having no pad (non-functional pads removed): their ring is not "
                "checked."
            )
        if run:
            # worst_mil: a plated hole without a ring counts as 0, as its finding does.
            entry = {"status": "pass", "limit_mil": {}, "worst_mil": {}}
            for kind, limit in (("via", via_limit), ("pth", pad_limit)):
                if limit is not None:
                    entry["limit_mil"][kind] = _round(limit)
                    entry["worst_mil"][kind] = _round(worst[kind])
            self.results["annular_ring"] = entry

    def _ring_group(self, key, hole, kind, ring, layer, pad, limit, verdict) -> _Group:
        limit_key = "min_via_annular_ring" if kind == "via" else "min_pth_annular_ring"
        if key[0] == "no_ring":
            group = self.group("annular_ring", "warning", margin=1.0,
                               kind=hole.kind, hole_mil=_round(hole.hole_mil), ring_mil=0.0)
            group.fields[f"{limit_key}_mil"] = _round(limit)

            def describe(group: _Group) -> str:
                what = "plated hole" if group.count == 1 else "plated holes"
                return (f"{group.count} {what} {self.len(hole.hole_mil)} without an annular "
                        "ring (no pad copper larger than the hole on an outer layer): a "
                        "plated hole needs a pad on both outer layers, or make it "
                        "non-plated.")
            group.describe = describe
            return group
        span = self.hole_span(hole)
        group = self.group("annular_ring", "error", verdict, margin=(limit - ring) / limit,
                           kind=hole.kind, hole_mil=_round(hole.hole_mil))
        if kind == "via":
            group.fields["diameter_mil"] = _round(pad[0])
            group.fields["span"] = span
        else:
            group.fields["pad_mil"] = [_round(pad[0]), _round(pad[1])]
        group.fields["ring_mil"] = _round(ring)
        group.fields["layer"] = self.layer(layer) if layer is not None else None
        group.fields[f"{limit_key}_mil"] = _round(limit)
        slot = self.slot_length(hole)

        def describe(group: _Group) -> str:
            value, minimum = self.pair(ring, limit)
            where = f" on {self.layer(layer)}" if layer is not None else ""
            if kind == "via":
                what = (f"{_plural(group.count, 'via')} ({span}, hole "
                        f"{self.len(hole.hole_mil)}, pad {self.len(pad[0])})")
                name = "via"
            else:
                size = (f"slot {self.len(hole.hole_mil)} x {self.len(slot)}" if slot
                        else f"hole {self.len(hole.hole_mil)}")
                what = (f"{group.count} plated pad {'hole' if group.count == 1 else 'holes'} "
                        f"({size} in a {self.len(pad[0])} x {self.len(pad[1])} pad)")
                name = "PTH"
            text = (f"{what}: annular ring {value}{where} (pad minus the hole as drawn), "
                    f"below the profile's minimum {name} ring {minimum}; drilling oversize "
                    "for plating leaves less.")
            if group.marginal:
                text += self.marginal_text(limit_key, limit)
            return text
        group.describe = describe
        return group

    # --- aspect ratio ---

    def check_aspect_ratio(self) -> None:
        stack = self.stack
        primitives = self.pcb.primitives
        limit = self.profile.get("max_aspect_ratio")
        micro_limit = self.profile.get("max_microvia_aspect_ratio")
        allowance = self.profile.get("drill_allowance") or 0.0
        run = "aspect_ratio" in self.checks
        if run and not stack.has_heights:
            self.not_run("aspect_ratio", "the layer stack has no copper and dielectric "
                                         "heights, so depths are unknown.")
            run = False
        elif run and self.no_limit("aspect_ratio", "max_aspect_ratio",
                                   "max_microvia_aspect_ratio"):
            run = False
        worst = {"drilled": None, "microvia": None}
        classes: dict[tuple, _Group] = {}
        microvias_unchecked = 0
        for hole in self.holes:
            if hole.plated is False or hole.hole_mil <= 0 or not stack.has_heights:
                continue
            micro = False
            if hole.kind == "via":
                prim = primitives[hole.row - 1]
                low, high, kind = via_span(stack, prim)
                micro = is_microvia(stack, kind, low, high, hole.hole_mil)
                depth = via_depth(stack, low, high, micro)
            else:
                depth = stack.thickness_mil
            if depth is None:
                continue
            ratio = depth / hole.hole_mil
            which = "microvia" if micro else "drilled"
            if worst[which] is None or ratio > worst[which]:
                worst[which] = ratio
            bound = micro_limit if micro else limit
            if not run:
                continue
            if bound is None:
                microvias_unchecked += micro
                continue
            judged = round(ratio, 1)
            verdict = self.above(judged, bound, 1e-9, length=False)
            if verdict is None:
                continue
            key = (hole.kind, self.hole_span(hole), round(hole.hole_mil, 4),
                   _round(self.slot_length(hole)), round(depth, 3), micro, verdict)
            group = classes.get(key)
            if group is None:
                group = classes[key] = self._aspect_group(hole, depth, ratio, bound, micro,
                                                          allowance, verdict)
            group.add(-ratio, self.hole_example(hole), nets=(self.net(hole.net),))
        # The board's worst ratios (worst_*: the profile's limits are max_*).
        self.minimums["worst_aspect_ratio"] = _round(worst["drilled"], 2)
        if worst["microvia"] is not None:
            self.minimums["worst_microvia_aspect_ratio"] = _round(worst["microvia"], 2)
        if microvias_unchecked:
            self.not_checked["microvia_aspect_ratio"] = (
                f"{microvias_unchecked} microvia(s): the profile sets no "
                "max_microvia_aspect_ratio, so their aspect ratio was not checked."
            )
        if run:
            entry = {"status": "pass", "limit": limit, "worst": _round(worst["drilled"], 2)}
            if micro_limit is not None:
                entry["microvia_limit"] = micro_limit
                entry["microvia_worst"] = _round(worst["microvia"], 2)
            self.results["aspect_ratio"] = entry

    def _aspect_group(self, hole, depth, ratio, bound, micro, allowance, verdict) -> _Group:
        key = "max_microvia_aspect_ratio" if micro else "max_aspect_ratio"
        group = self.group("aspect_ratio", "warning", verdict, margin=(ratio - bound) / bound,
                           kind=hole.kind, hole_mil=_round(hole.hole_mil),
                           depth_mil=_round(depth), aspect_ratio=_round(ratio, 2))
        group.fields[key] = bound
        if hole.kind == "via":
            group.fields["span"] = self.hole_span(hole)
        if micro:
            group.fields["microvia"] = True
        drilled = depth / (hole.hole_mil + allowance) if allowance > 0 else None
        if drilled is not None:
            group.fields["aspect_ratio_drilled"] = _round(drilled, 2)

        def describe(group: _Group) -> str:
            if hole.kind == "via":
                what = (f"{group.count} {'microvia' if micro else 'via'}"
                        f"{'' if group.count == 1 else 's'} ({self.hole_span(hole)}, hole "
                        f"{self.len(hole.hole_mil)})")
            else:
                what = (f"{group.count} plated {'hole' if group.count == 1 else 'holes'} of "
                        f"{self.len(hole.hole_mil)} through the board")
            depth_name = "capture depth" if micro else "copper-to-copper depth"
            text = (f"{what}: aspect ratio {_ratio(ratio)}:1 ({depth_name} {self.len(depth)} "
                    f"over the hole as drawn), above the profile's {bound:g}:1.")
            if drilled is not None:
                text += (f" Drilled {self.len(allowance)} larger, the ratio is "
                         f"{_ratio(drilled)}:1.")
            if group.marginal:
                text += (f" Marginal: within {self.tolerance * 100:g}% of {bound:g}:1; "
                         "confirm how the fabricator measures it.")
            return text
        group.describe = describe
        return group

    # --- hole to hole ---

    def check_hole_to_hole(self) -> None:
        """Hole pairs closer than the via or pad hole-to-hole limit, edge to edge.
        Overlapping and coincident holes are reported whatever the profile: a drill
        overlap needs no limit."""
        via_limit = self.profile.get("min_via_hole_to_hole")
        pad_limit = self.profile.get("min_pad_hole_to_hole")
        reach = max(via_limit or 0.0, pad_limit or 0.0, geometry.DEFAULT_HOLE_REACH_MIL)
        result = geometry.hole_to_hole(self.index, reach)
        self.hole_result = result
        unlimited = [name for name, value in (("via", via_limit), ("pad", pad_limit))
                     if value is None]
        overlaps = sum(1 for record in result.records if "touching" in record.marks)
        note = None
        if unlimited:
            keys = ("min_hole_to_hole (min_via_hole_to_hole, min_pad_hole_to_hole)"
                    if len(unlimited) == 2 else f"min_{unlimited[0]}_hole_to_hole")
            pairs = "holes" if len(unlimited) == 2 else f"{unlimited[0]} holes"
            note = f"the profile sets no {keys}: gaps between {pairs} were not compared"
            rules = [
                self.checker.values(rule).get("clearance_mil")
                for rule in self.checker.enabled("HoleToHoleClearance")
            ]
            rules = [value for value in rules if value is not None]
            tail = (f" The board's HoleToHoleClearance rule asks for {self.len(min(rules))}."
                    if rules else "")
            if len(unlimited) == 2 and not overlaps:
                self.not_run("hole_to_hole", f"{note}; no holes overlap.{tail}")
                return
            note += (f"; overlapping and coincident holes were checked all the same (they "
                     f"need no limit).{tail}")
        minimum = result.minimum()
        limits = {key: _round(value) for key, value in (("via", via_limit), ("pad", pad_limit))
                  if value is not None}
        entry = {"status": "pass", "limit_mil": limits,
                 "worst_mil": _round(minimum.gap_mil) if minimum else None}
        if note:
            entry["note"] = note[0].upper() + note[1:]
        self.results["hole_to_hole"] = entry
        classes: dict[tuple, _Group] = {}
        for record in result.records:
            limit = via_limit if record.pair_class == "via" else pad_limit
            touching = "touching" in record.marks
            if not touching and (limit is None or
                                 record.gap_mil >= limit - CLEARANCE_TOLERANCE_MIL):
                continue
            kinds = "-".join(sorted((record.first.kind, record.second.kind)))
            offset = "hole_offset" in record.marks
            if "coincident" in record.marks:
                key, severity, verdict = ("coincident", kinds), "warning", "miss"
            elif touching:
                key, severity, verdict = ("touching", kinds), "error", "miss"
            else:
                verdict = self.below(record.gap_mil, limit, CLEARANCE_TOLERANCE_MIL) or "marginal"
                key, severity = ("gap", record.pair_class, kinds, offset, verdict), "error"
            group = classes.get(key)
            if group is None:
                limit_key = f"min_{record.pair_class}_hole_to_hole"
                group = classes[key] = self.group(
                    "hole_to_hole", severity, verdict,
                    margin=1.0 if touching else (limit - record.gap_mil) / limit,
                    kinds=kinds, gap_mil=_round(record.gap_mil),
                )
                if limit is not None:
                    group.fields[f"{limit_key}_mil"] = _round(limit)
                if key[0] == "gap":
                    group.range_keys = ("gap_mil", "gap_range_mil")
                group.describe = self._describe_holes(key[0], kinds, record, limit, limit_key,
                                                      offset)
            nets = (record.first.net, record.second.net)
            group.add(record.gap_mil, self._hole_pair_example(record),
                      nets=[self.net(net) for net in nets],
                      place=_place(None, record.at, frozenset(nets)))

    def _hole_pair_example(self, record):
        def make() -> dict:
            return {"at_mil": self.at(*record.at),
                    "objects": f"{self.hole_label(record.first)} / "
                               f"{self.hole_label(record.second)}"}
        return make

    def _describe_holes(self, what, kinds, record, limit, limit_key, offset):
        def describe(group: _Group) -> str:
            pairs = "pair" if group.count == 1 else "pairs"
            if what == "coincident":
                return (f"{group.count} {kinds} hole {pairs} drilled twice at one spot (same "
                        "size and centre): drill it once, or check the footprint.")
            if what == "touching":
                return (f"{group.count} {kinds} hole {pairs} overlap: overlapping drills "
                        "break bits and tear the plating.")
            span, minimum = self.gap_range(group, limit)
            name = "via" if record.pair_class == "via" else "pad"
            text = (f"{group.count} {kinds} hole {pairs} {span} apart (edge to edge), below "
                    f"the profile's minimum {name} hole-to-hole {minimum}.")
            if offset:
                text += " A pad hole is offset from its pad (not decoded): its position is approximate."
            if group.marginal:
                text += self.marginal_text(limit_key, limit)
            return text
        return describe

    # --- copper to edge ---

    def check_copper_to_edge(self) -> None:
        limit = self.profile.get("min_copper_to_edge")
        hole_limit = self.profile.get("min_hole_to_edge")
        default = geometry.DEFAULT_EDGE_REACH_MIL
        widest = geometry.copper_to_edge(self.index, max(limit or 0.0, default))
        holes = geometry.hole_to_edge(self.index, max(hole_limit or 0.0, default))
        self.edge_result, self.hole_edge_result = widest, holes
        if widest.degenerate_cutouts:
            self.notes.append(
                f"{widest.degenerate_cutouts} board cutout(s) have no edge of non-zero length "
                "(every vertex on one spot): they are not part of the board edge."
            )
        if not widest.has_outline:
            self.not_run("copper_to_edge", "the board has no outline.")
            return
        if self.no_limit("copper_to_edge", "min_copper_to_edge", "min_hole_to_edge"):
            return
        stopped = [what for what, part in (("copper-to-edge", widest), ("hole-to-edge", holes))
                   if part.stopped]
        if stopped:
            self.not_checked["copper_to_edge_incomplete"] = (
                f"The {' and '.join(stopped)} search stopped after "
                f"{widest.distance_calls + holes.distance_calls} computations: findings and "
                "minimums are incomplete."
            )
        # worst_mil from the widest search, as board_minimums (fingers count).
        nearest = widest.minimum() if limit is not None else None
        entry = {"status": "pass", "limit_mil": _round(limit),
                 "worst_mil": _round(nearest.distance_mil) if nearest else None}
        self.results["copper_to_edge"] = entry
        classes: dict[tuple, _Group] = {}
        if limit is not None:
            self._edge_copper(widest, limit, classes)
        else:
            self.not_checked["copper_edge"] = (
                "The profile sets no min_copper_to_edge: copper to the board edge was not "
                "compared, only holes (min_hole_to_edge)."
            )
        if hole_limit is not None:
            near = holes.minimum()
            entry["hole_to_edge"] = {
                "limit_mil": _round(hole_limit),
                "worst_mil": _round(near.distance_mil) if near else None,
            }
            self._edge_walls(holes, hole_limit, classes)
        else:
            self.not_checked["holes_to_edge"] = (
                "The profile removes min_hole_to_edge: non-plated and bare holes near the "
                "board edge were not compared."
            )

    def _edge_copper(self, widest, limit, classes) -> None:
        # Edge-connector fingers depend on the distance searched: the profile's own.
        default = geometry.DEFAULT_EDGE_REACH_MIL
        result = geometry.copper_to_edge(self.index, limit) if limit < default else widest
        cutoff = limit - CLEARANCE_TOLERANCE_MIL
        for record in result.records:
            if record.distance_mil >= cutoff:
                break
            copper = record.copper
            kind = "track" if copper.kind == "arc" else copper.kind
            edge = self.edge_name(record)
            if "stale_pour" in record.marks:
                key = ("stale", copper.pour)
                if key not in classes:
                    classes[key] = self._stale_group("copper_to_edge", copper.pour,
                                                     "to the board edge")
            else:
                crosses = "crosses" in record.marks
                verdict = "miss" if crosses else (
                    self.below(record.distance_mil, limit, CLEARANCE_TOLERANCE_MIL) or "marginal")
                key = ("edge", kind, edge, crosses, verdict)
                if key not in classes:
                    severity = "warning" if copper.is_area else "error"
                    group = classes[key] = self.group(
                        "copper_to_edge", severity, verdict,
                        margin=(limit - record.distance_mil) / limit, kind=kind,
                        distance_mil=_round(record.distance_mil),
                    )
                    if not crosses:
                        group.range_keys = ("distance_mil", "distance_range_mil")
                    group.describe = self._describe_edge(kind, edge, crosses, limit)
            classes[key].add(record.distance_mil, self._edge_example(record),
                             nets=(self.net(copper.net),), layers=record.layers,
                             place=_place(record.layer, record.at, copper.net))
        for finger in result.fingers:
            if finger.distance_mil >= cutoff:
                continue
            group = self.group("copper_to_edge", "warning", miss=False, kind="pad",
                               distance_mil=_round(finger.distance_mil),
                               designator=self.designator(finger.component))
            group.describe = self._describe_fingers(finger, limit)
            for pad in finger.pads:
                group.add(0.0, lambda: None, nets=(self.net(pad.net),))
            group.examples = [(0.0, 0, self._example(*finger.at, layer=self.layer(finger.layer)))]
        self._edge_holes(result)
        self._off_board(result)

    @staticmethod
    def edge_name(record) -> str:
        if record.cutout_component is not None:
            return "the part's own cutout"
        return "a board cutout" if record.edge == "cutout" else "the board outline"

    def _edge_example(self, record):
        def make() -> dict:
            return {
                "at_mil": self.at(*record.at),
                "object": self.copper_label(record.copper),
                "layer": self.layer(record.layer),
                "distance_mil": _round(record.distance_mil),
            }
        return make

    def _describe_edge(self, kind, edge, crosses, limit):
        def describe(group: _Group) -> str:
            what = _plural(group.count, kind)
            if crosses:
                text = f"{what} reach or cross {edge}{self.where(group)}."
            else:
                span, minimum = self.gap_range(group, limit)
                text = (f"{what}{self.where(group)} closer to {edge} than the profile's "
                        f"copper-to-edge clearance {minimum}: {span} from it.")
                if group.marginal:
                    text += self.marginal_text("min_copper_to_edge", limit)
            if kind in _AREA_KINDS:
                text += " Pull the copper back from the edge (a rule or a repour)."
            return text
        return describe

    def _edge_walls(self, holes, limit, classes) -> None:
        """Holes without copper of their own around them (non-plated pad holes, bare
        holes: dfm_geometry.hole_to_edge) closer than limit to the edge, or crossing it."""
        cutoff = limit - CLEARANCE_TOLERANCE_MIL
        generic = self.profile.resolved("min_hole_to_edge") != "min_hole_to_edge"
        for record in holes.records:
            if record.distance_mil >= cutoff:
                break
            hole = record.hole
            plated = hole.plated is not False
            edge = self.edge_name(record)
            crosses = "crosses" in record.marks
            verdict = "miss" if crosses else (
                self.below(record.distance_mil, limit, CLEARANCE_TOLERANCE_MIL) or "marginal")
            key = ("hole_edge", plated, edge, crosses, verdict)
            group = classes.get(key)
            if group is None:
                group = classes[key] = self.group(
                    "copper_to_edge", "warning" if generic and not crosses else "error",
                    verdict,
                    margin=1.0 if crosses else (limit - record.distance_mil) / limit,
                    kind="hole", plated_hole=plated, distance_mil=_round(record.distance_mil),
                    min_hole_to_edge_mil=_round(limit),
                )
                group.side = "hole"
                if not crosses:
                    group.range_keys = ("distance_mil", "distance_range_mil")
                group.describe = self._describe_hole_edge(plated, edge, crosses, limit, generic)
            group.add(record.distance_mil, self._hole_edge_example(record),
                      nets=(self.net(hole.net),))

    def _hole_edge_example(self, record):
        def make() -> dict:
            hole = record.hole
            plating = "non-plated hole" if hole.plated is False else "plated hole"
            return {
                "at_mil": self.at(*record.at),
                "object": f"{self.hole_label(hole)} ({plating} {self.len(hole.hole_mil)})",
                "distance_mil": _round(record.distance_mil),
            }
        return make

    def _describe_hole_edge(self, plated, edge, crosses, limit, generic):
        rule = "copper-to-edge clearance" if generic else "hole-to-edge clearance"
        note = " (it sets no min_hole_to_edge)" if generic else ""

        def describe(group: _Group) -> str:
            what = f"{group.count} {_hole_name(plated, group.count)}"
            if crosses:
                return (f"{what} reach or cross {edge}: the drill runs off the board; move "
                        "the hole, or confirm the fabricator can make the notch.")
            span, minimum = self.gap_range(group, limit)
            text = (f"{what} closer to {edge} than the profile's {rule} {minimum}{note} "
                    f"(wall to edge): {span} from it.")
            if group.marginal:
                text += self.marginal_text("min_hole_to_edge", limit)
            return text
        return describe

    def _describe_fingers(self, finger, limit):
        def describe(group: _Group) -> str:
            return (f"Edge-connector fingers of {self.designator(finger.component)}: "
                    f"{len(finger.pads)} pads, the nearest {self.len(finger.distance_mil)} from "
                    f"the edge, inside the profile's copper-to-edge clearance "
                    f"{self.len(limit)}. Fingers need the fabricator's edge-connector option: "
                    "confirm the finger plating, bevel and clearance with the fabricator.")
        return describe

    def _edge_holes(self, result) -> None:
        groups: dict[int | None, _Group] = {}
        for found in result.edge_holes:
            hole = found.hole
            group = groups.get(hole.component)
            if group is None:
                group = groups[hole.component] = self.group(
                    "copper_to_edge", "info", miss=False, kind="plated_hole",
                    designator=self.designator(hole.component),
                )
                group.describe = self._describe_edge_holes(hole.component)
            group.add(0.0, self.hole_example(hole), nets=(self.net(hole.net),))

    def _describe_edge_holes(self, component):
        def describe(group: _Group) -> str:
            owner = self.designator(component)
            what = "plated hole" if group.count == 1 else "plated holes"
            return (f"{group.count} {what}{f' of {owner}' if owner else ''} cut by the board "
                    "edge (edge-plated or castellated): tell the fabricator.")
        return describe

    def _off_board(self, result) -> None:
        groups: dict[tuple, _Group] = {}
        for record in result.off_board:
            copper = record.copper
            key = (copper.component, record.inside_cutout)
            group = groups.get(key)
            if group is None:
                group = groups[key] = self.group(
                    "copper_to_edge", "info", miss=False, kind="off_board",
                    designator=self.designator(copper.component),
                )
                group.describe = self._describe_off_board(copper.component, record.inside_cutout)
            group.add(0.0, self._example(*record.at, object=self.copper_label(copper)),
                      nets=(self.net(copper.net),), layers=record.layers)

    def _describe_off_board(self, component, inside_cutout):
        def describe(group: _Group) -> str:
            where = "inside a board cutout" if inside_cutout else "outside the board"
            owner = self.designator(component)
            what = "object" if group.count == 1 else "objects"
            if owner:
                return (f"Copper of {owner} lies {where} ({group.count} {what}): a part "
                        "parked off the board? check_pcb_health's components_outside_board "
                        "lists such parts.")
            return (f"{group.count} free copper {what} {where}: delete them or move them "
                    "onto the board.")
        return describe

    # --- layer count and copper weight ---

    def check_layer_count(self) -> None:
        """The stack's copper layer count against layer_counts (copper weight is
        copper_weight's)."""
        counts = self.profile.get("layer_counts")
        if self.no_limit("layer_count", "layer_counts"):
            return
        if self.stack.legacy:
            self.not_run("layer_count", "legacy layer stack (not a v9 stackup): the copper "
                                        "layer count is unknown.")
            return
        copper = len(self.stack.order)
        self.results["layer_count"] = {"status": "pass", "limit": list(counts), "value": copper}
        if copper not in counts:
            severity = self.profile.severity("layer_counts")
            group = self.group("layer_count", severity, copper_layers=copper,
                               layer_counts=list(counts))
            group.count = 1
            group.describe = self._describe_layers(copper, counts, severity)

    def check_copper_weight(self) -> None:
        """Each copper layer's thickness in oz (1 oz = OZ_MIL), compared at 0.1 oz as
        weights are quoted, against copper_oz."""
        ounces = self.profile.get("copper_oz")
        if self.no_limit("copper_weight", "copper_oz"):
            return
        thick = [
            (layer.copper_thick_mil, layer.name) for layer in self.pcb.board.layers
            if layer.kind == "copper" and layer.copper_thick_mil
        ]
        if self.stack.legacy or not thick:
            self.not_run("copper_weight", "legacy layer stack (not a v9 stackup): the copper "
                                          "weights are unknown." if self.stack.legacy else
                         "the layer stack gives no copper thickness.")
            return
        heaviest, name = max(thick)
        # limit: the profile's copper_oz; worst: the board's heaviest copper (oz).
        self.results["copper_weight"] = {"status": "pass", "limit": ounces,
                                         "worst": round(heaviest / OZ_MIL, 2)}
        # Weights compare at 0.1 oz, as they are quoted (1.42 mil is 1 oz).
        verdicts = [self.above(round(item[0] / OZ_MIL, 1), ounces, 1e-6, length=False)
                    for item in thick]
        over = [item for item, verdict in zip(thick, verdicts) if verdict]
        if over:
            verdict = "miss" if "miss" in verdicts else "marginal"
            severity = self.profile.severity("copper_oz")
            group = self.group("copper_weight", severity, verdict,
                               margin=(heaviest / OZ_MIL - ounces) / ounces,
                               copper_mil=_round(heaviest),
                               copper_oz=round(heaviest / OZ_MIL, 2),
                               max_copper_oz=ounces)
            group.count = len(over)
            group.describe = self._describe_copper(heaviest, name, ounces, len(over), severity)

    def default_service(self, severity: str, maker: str = "a fabricator can build") -> str:
        if severity != "info":
            return ""
        return (" Altimade is only the default profile here: this is what Altimade orders, "
                f"not what {maker} (advisory).")

    def _describe_layers(self, copper, counts, severity):
        def describe(group: _Group) -> str:
            allowed = ", ".join(str(count) for count in counts)
            return (f"{copper} copper layers; the profile builds {allowed}."
                    + self.default_service(severity))
        return describe

    def _describe_copper(self, heaviest, name, ounces, layers, severity):
        def describe(group: _Group) -> str:
            text = (f"Copper {self.len(heaviest)} ({heaviest / OZ_MIL:.2f} oz) on {name}"
                    f"{f' and {layers - 1} more layer(s)' if layers > 1 else ''}, above the "
                    f"profile's {ounces:g} oz (1 oz = 1.4 mil as Altium's stack counts it; "
                    "outer copper in the stack may be finished, plated copper).")
            if group.marginal:
                text += self.marginal_text("copper_oz", ounces, "max", length=False)
            return text + self.default_service(severity)
        return describe

    # --- board size ---

    def outline_box(self) -> tuple | None:
        """(min x, min y, max x, max y) of the outline, arcs bulging included."""
        return assembly.outline_box(self.pcb)

    def check_board_size(self) -> None:
        box = self.outline_box()
        smallest = self.profile.get("min_board_size")
        largest = self.profile.get("max_board_size")
        if self.no_limit("board_size", "min_board_size", "max_board_size"):
            return
        if box is None:
            self.not_run("board_size", "the board has no outline.")
            return
        width, height = box[2] - box[0], box[3] - box[1]
        short, long = sorted((width, height))
        entry = {"status": "pass", "value_mil": [_round(width), _round(height)]}
        self.results["board_size"] = entry
        for key, bound, test in (("min_board_size", smallest, "below"),
                                 ("max_board_size", largest, "above")):
            if bound is None:
                continue
            entry[f"{key}_mil"] = [_round(bound[0]), _round(bound[1])]
            check = self.below if test == "below" else self.above
            verdicts = [check(short, bound[0], EPS_MIL), check(long, bound[1], EPS_MIL)]
            if not any(verdicts):
                continue
            verdict = "miss" if "miss" in verdicts else "marginal"
            severity = self.profile.severity(key)
            group = self.group("board_size", severity, verdict, size_mil=entry["value_mil"])
            group.fields[f"{key}_mil"] = entry[f"{key}_mil"]
            group.count = 1
            group.describe = self._describe_size(width, height, bound, test, severity, key)

    def _describe_size(self, width, height, bound, test, severity, key):
        def describe(group: _Group) -> str:
            word = "below the minimum" if test == "below" else "above the maximum"
            text = (f"Board {self.len(width)} x {self.len(height)} (outline extent), {word} "
                    f"{self.len(bound[0])} x {self.len(bound[1])} of the profile (either "
                    "orientation).")
            if group.marginal:
                side = "min" if test == "below" else "max"
                text += self.marginal_text(key, max(bound), side)
            return text + self.default_service(severity)
        return describe

    # --- drill count ---

    def check_drill_count(self) -> None:
        sizes = {
            (round(hole.hole_mil, 2), _round(self.slot_length(hole), 2)) for hole in self.holes
        }
        limit = self.profile.get("max_drill_sizes")
        if self.no_limit("drill_count", "max_drill_sizes"):
            return
        self.results["drill_count"] = {"status": "pass", "limit": limit, "value": len(sizes)}
        if len(sizes) > limit:
            group = self.group("drill_count", "info", drill_sizes=len(sizes),
                               max_drill_sizes=limit, margin=(len(sizes) - limit) / limit)
            group.count = len(sizes)

            def describe(group: _Group) -> str:
                return (f"{len(sizes)} distinct drill sizes (slots counted apart), above the "
                        f"profile's {limit}: merging sizes saves tool changes and cost.")
            group.describe = describe

    # --- rules against the profile ---

    def _rule_values(self, kind: str, read) -> list[tuple]:
        """(rule, value) of the enabled rules of a kind that hold a value."""
        found = []
        for rule in self.checker.enabled(kind):
            for value in read(self.checker.values(rule)):
                if isinstance(value, (int, float)):
                    found.append((rule, float(value)))
        return found

    def check_rules_vs_profile(self) -> None:
        def clearance(values):
            yield values.get("clearance_mil")
            for pair, value in (values.get("object_clearances_mil") or {}).items():
                if "Hole" not in pair and "Text" not in pair and value > 0:
                    yield value

        def widths(values):
            yield values.get("min_width_mil")
            for layer in (values.get("layers_mil") or {}).values():
                yield layer.get("min_width")
            for layers in (values.get("substack_layers_mil") or {}).values():
                for layer in layers.values():
                    yield layer.get("min_width")

        def absolute(name):
            def read(values):
                if values.get("absolute_values", True) is not False:
                    yield values.get(name)
            return read

        drills = [self.profile.get(key) for key in _HOLE_LIMITS.values()]
        drills = [value for value in drills if value is not None]
        rings = [self.profile.get(key) for key in ("min_via_annular_ring", "min_pth_annular_ring")]
        rings = [value for value in rings if value is not None]
        h2h = [self.profile.get(key) for key in ("min_via_hole_to_hole", "min_pad_hole_to_hole")]
        h2h = [value for value in h2h if value is not None]
        comparisons = (
            # (rule kind, what the rule sets, read values, profile key, limit, min/max,
            #  measured check, the rule absent: info)
            ("Clearance", "minimum clearance", clearance, "min_spacing",
             self.profile.get("min_spacing"), "min", "spacing", False),
            ("Width", "minimum width", widths, "min_trace", self.profile.get("min_trace"),
             "min", "trace_width", False),
            ("DiffPairsRouting", "minimum width", widths, "min_trace",
             self.profile.get("min_trace"), "min", "trace_width", False),
            ("RoutingVias", "minimum hole", lambda values: [values.get("min_hole_mil")],
             "min_via_drill", self.profile.get("min_via_drill"), "min", "hole_size", False),
            ("HoleSize", "minimum hole", absolute("min_hole_mil"), "min_drill",
             min(drills) if drills else None, "min", "hole_size", True),
            ("HoleSize", "maximum hole", absolute("max_hole_mil"), "max_drill",
             self.profile.get("max_drill"), "max", "hole_size", False),
            ("MinimumAnnularRing", "minimum ring", lambda values: [values.get("min_ring_mil")],
             "min_annular_ring", min(rings) if rings else None, "min", "annular_ring", True),
            ("BoardOutlineClearance", "minimum clearance",
             lambda values: [values.get("clearance_mil")], "min_copper_to_edge",
             self.profile.get("min_copper_to_edge"), "min", "copper_to_edge", True),
            ("HoleToHoleClearance", "minimum clearance",
             lambda values: [values.get("clearance_mil")], "min_hole_to_hole",
             min(h2h) if h2h else None, "min", "hole_to_hole", True),
        )
        self.results["rules_vs_profile"] = entry = {"status": "pass", "rules": []}
        for kind, what, read, key, limit, side, measured, absent_info in comparisons:
            if limit is None:
                continue
            found = self._rule_values(kind, read)
            if kind == "HoleSize" and side == "min":
                found = self._hole_size_default(found, limit, absent_info)
                if found is None:
                    continue
            if not found:
                if absent_info and kind != "HoleSize":
                    self._rule_absent(kind, what, limit)
                continue
            check = self.below if side == "min" else self.above
            looser = [(rule, value, check(value, limit, EPS_MIL)) for rule, value in found]
            looser = [item for item in looser if item[2]]
            if not looser:
                continue
            looser.sort(key=lambda item: item[1] if side == "min" else -item[1])
            rule, value, verdict = looser[0]
            board_rule = self.board_rule(rule, value)
            copper = self.status(measured)
            fails = self._attach_rule(measured, board_rule, kind, side)
            # sets: which side of the rule ("maximum hole"); covered_by: the measured check
            # whose failing findings carry the rule (board_rule) instead of a finding here.
            row = {
                "kind": kind, "rule": rule.name, "sets": what, "value_mil": _round(value),
                "limit_mil": _round(limit),
                "copper": "fails" if fails else "not_checked" if copper in (
                    None, "not_checked") else "passes",
            }
            if fails:
                row["covered_by"] = measured
            if verdict == "marginal":
                row["marginal"] = True
            entry["rules"].append(row)
            if fails:
                continue
            group = self.group("rules_vs_profile", "warning", verdict,
                               margin=abs(limit - value) / limit, kind=kind,
                               rule=rule.name, sets=what, rule_value_mil=_round(value))
            group.fields[f"{key}_mil"] = _round(limit)
            group.count = len(looser)
            group.board_rule = board_rule
            group.fields["rules"] = [
                {"rule": item[0].name, "value_mil": _round(item[1]), "scope": item[0].scope1}
                for item in looser[:RULES_LISTED]
            ]
            group.describe = self._describe_rule(kind, what, rule, value, limit, key, side,
                                                 row["copper"], len(looser))

    def _hole_size_default(self, found, limit, absent_info):
        """HoleSize minimums above Altium's untouched default (1 mil); one info finding
        when every rule sits at it (None: nothing more to compare)."""
        real = [(rule, value) for rule, value in found if value > HOLE_SIZE_DEFAULT_MIL + EPS_MIL]
        if real:
            return real
        group = self.group("rules_vs_profile", "info", miss=False, kind="HoleSize")
        group.count = 1
        default = bool(found)

        def describe(group: _Group) -> str:
            if default:
                return (f"The HoleSize rule's minimum ({self.len(found[0][1])}) is no more "
                        f"than Altium's default {self.len(HOLE_SIZE_DEFAULT_MIL)}: Altium's "
                        "DRC does not check holes against the profile's minimum drill "
                        f"{self.len(limit)}.")
            return (f"No enabled HoleSize rule with an absolute minimum: Altium's DRC does "
                    f"not check holes against the profile's minimum drill {self.len(limit)}.")
        group.describe = describe
        return None

    def _rule_absent(self, kind, what, limit) -> None:
        group = self.group("rules_vs_profile", "info", miss=False, kind=kind)
        group.count = 1

        def describe(group: _Group) -> str:
            return (f"No enabled {kind} rule: Altium's DRC does not check the {what} the "
                    f"profile asks for ({self.len(limit)}).")
        group.describe = describe

    def _attach_rule(self, check: str, board_rule: dict, kind: str, side: str) -> bool:
        """Put a looser board rule on the measured check's findings that miss the profile
        too (RoutingVias: via holes below the minimum; HoleSize: holes of its side) and on
        its results. False when no such finding exists: the copper passes. Spacing's
        findings carry the board's lowest Clearance (this rule) already."""
        matching = [
            group for group in self.groups
            if group.check == check and group.miss and group.side != "hole" and not (
                check == "hole_size" and (group.side != side or (
                    kind == "RoutingVias" and group.fields.get("kind") != "via"))
            )
        ]
        if not matching:
            return False
        result = self.results[check]
        if "board_rule" not in result:
            result["board_rule"] = board_rule
        if check != "spacing":
            for group in matching:
                if group.board_rule is None:
                    group.board_rule = board_rule
        return True

    def _describe_rule(self, kind, what, rule, value, limit, key, side, copper, count):
        def describe(group: _Group) -> str:
            shown, bound = self.pair(value, limit)
            verb = "Raise" if side == "min" else "Lower"
            if copper == "passes":
                state = ("the copper meets the profile today, but Altium's DRC would pass a "
                         "later edit that does not")
            else:
                state = "the copper was not checked against the profile"
            more = f" ({count} rules; the loosest named)" if count > 1 else ""
            text = (f"Rule '{rule.name}' ({kind}) sets the {what} to {shown}, looser than the "
                    f"profile's {bound}{more}: {state}. {verb} the rule to {bound}.")
            if group.marginal:
                text += self.marginal_text(key, limit, side)
            return text
        return describe

    # --- assembly (DFA) ---

    def placements(self):
        """dfm_assembly's placements for the active variant (computed once)."""
        if self._placements is None:
            self._placements = assembly.placements(self.index, self.variant.is_dnp)
        return self._placements

    def not_fitted(self) -> set[int]:
        """Components Not Fitted in the active variant (any kind)."""
        if self._not_fitted is None:
            is_dnp = self.variant.is_dnp
            self._not_fitted = {part.component for part in assembly.parts(self.index)
                                if not part.fitted(is_dnp)}
        return self._not_fitted

    def placed_parts(self) -> list:
        """The parts the assembler places, in designator order (computed once)."""
        if self._placed is None:
            parts = assembly.parts(self.index)
            placed = [parts[component] for component in self.placements().components]
            self._placed = sorted(placed, key=lambda part: natural_key(part.designator or ""))
        return self._placed

    def unique_parts(self) -> tuple[int, bool]:
        """(unique parts, approximate): the BOM's line count with a netlist, else
        distinct footprint and Comment of the placements."""
        if self.variant.unique_parts is not None:
            return self.variant.unique_parts, False
        return self.placements().unique, True

    def side_counts(self) -> dict[str, dict[str, int]]:
        """Placements per side: SMD (mixed parts included) and THT (mixed included)."""
        return side_counts(self.placements().counts)

    def part_example(self, part, at=None, **fields):
        """An example naming a part: its designator, side and a point (its centre)."""
        x, y = at if at is not None else part.centre
        return self._example(x, y, object=self.designator(part.component), side=part.side,
                             **fields)

    def finest_pitch(self) -> float | None:
        pitches = [part.pitch_mil for part in self.placed_parts() if part.pitch_mil is not None]
        return min(pitches) if pitches else None

    def check_part_counts(self) -> None:
        """Placements (fitted parts of a BOM kind with soldered pads, fiducials left out)
        and unique parts (the BOM's lines with a netlist, else distinct footprint and
        Comment) against max_placements and max_unique_parts: Altimade's ordering
        limits, info while Altimade is only the default."""
        if self.no_limit("part_counts", "max_placements", "max_unique_parts"):
            return
        total = len(self.placements().components)
        unique, approximate = self.unique_parts()
        limits = {"placements": self.profile.get("max_placements"),
                  "unique_parts": self.profile.get("max_unique_parts")}
        entry = {"status": "pass", "limit": limits,
                 "value": {"placements": total, "unique_parts": unique}}
        if approximate:
            entry["unique_parts_approximate"] = True
        self.results["part_counts"] = entry
        for key, name, value in (("max_placements", "placements", total),
                                 ("max_unique_parts", "unique_parts", unique)):
            limit = limits[name]
            if limit is None or value <= limit:
                continue
            severity = self.profile.severity(key)
            group = self.group("part_counts", severity, margin=(value - limit) / limit,
                               **{name: value, key: limit})
            group.count = 1
            group.describe = self._describe_counts(name, value, limit, severity, approximate)

    def _describe_counts(self, name, value, limit, severity, approximate):
        def describe(group: _Group) -> str:
            if name == "placements":
                sides = self.side_counts()
                text = (f"{value} placements ({sides['top']['all']} top, "
                        f"{sides['bottom']['all']} bottom), above the profile's maximum of "
                        f"{limit}. Counted: fitted parts of the active variant in the BOM "
                        "with soldered pads, fiducials left out; ask the assembler how it "
                        "counts.")
            elif approximate:
                text = (f"About {value} unique parts (distinct footprint and Comment: no "
                        "netlist is loaded, so the BOM's lines cannot be counted), above the "
                        f"profile's maximum of {limit}.")
            else:
                text = (f"{value} unique parts (the BOM lines of the active variant's fitted "
                        "parts, grouped as get_bom groups them), above the profile's maximum "
                        f"of {limit}.")
            return text + self.default_service(severity, "an assembler can place")
        return describe

    def check_assembly_sides(self) -> None:
        """Info whatever the profile: SMD parts on both sides (a second reflow; the count
        is the bottom side's SMD parts) and through-hole parts on the bottom (selective,
        wave or hand soldering). Counts are parts (a mixed part on the bottom is in
        both)."""
        sides = self.side_counts()
        self.results["assembly_sides"] = {
            "status": "pass", "sides": [side for side in _SIDES if sides[side]["all"]],
        }
        if sides["top"]["smd"] and sides["bottom"]["smd"]:
            smd = {side: sides[side]["smd"] for side in _SIDES}
            group = self.group("assembly_sides", "info", smd_parts=smd)
            group.count = smd["bottom"]

            def describe(group: _Group) -> str:
                parts = "part" if smd["bottom"] == 1 else "parts"
                return (f"{smd['bottom']} SMD {parts} on the bottom side and {smd['top']} on "
                        "the top: two reflow passes, and the side reflowed first goes through "
                        "the oven twice (heavy parts there may need glue). Confirm the "
                        "assembler quotes double-sided assembly.")
            group.describe = describe
        bottom = [part for part in self.placed_parts()
                  if part.side == "bottom" and part.mount in ("tht", "mixed")]
        if bottom:
            group = self.group("assembly_sides", "info", side="bottom", mount="tht")
            for part in bottom:
                group.add(0.0, self.part_example(part))

            def describe_tht(group: _Group) -> str:
                what, them = ("part", "it") if group.count == 1 else ("parts", "them")
                return (f"{group.count} through-hole {what} on the bottom side: selective "
                        "soldering, a wave pallet or hand soldering after reflow (an extra "
                        f"step). Move {them} to the top if the design allows.")
            group.describe = describe_tht

    def check_part_spacing(self) -> None:
        """Fitted parts closer than min_part_spacing to a same-side neighbour (the
        smallest of body, body-to-pad and pad gaps) and placed parts whose outlines
        overlap: a warning with the limit, advisory (info) without one. A part the
        assembler does not place (a mechanical model, a mounting hole, a fiducial) over
        fitted parts is its own info class, whatever the limit."""
        out = self.not_fitted()
        facts = assembly.part_spacing(self.index, out)  # each part's nearest fitted one
        limit = self.profile.get("min_part_spacing")
        placed = set(self.placements().components)
        overlaps = [gap for gap in facts.overlaps
                    if gap.first in placed and gap.second in placed]
        unplaced = [gap for gap in facts.overlaps
                    if gap.first not in placed or gap.second not in placed]
        close = []
        if limit is not None and limit > 0:
            close = [gap for gap in assembly.close_pairs(self.index, limit)
                     if gap.first not in out and gap.second not in out and not gap.overlap]
        nearest = min([gap for gap in (facts.minimum(),) if gap] + close,
                      key=lambda gap: (gap.gap_mil, gap.first, gap.second), default=None)
        self.part_gap = (nearest, len(overlaps), facts.reach_mil)
        if limit is None and not overlaps and not unplaced:
            self.not_run("part_spacing", "the profile sets no min_part_spacing, and no "
                                         "fitted parts' outlines overlap.")
            return
        entry = {"status": "pass", "limit_mil": _round(limit),
                 "worst_mil": _round(nearest.gap_mil) if nearest else None}
        if overlaps:
            entry["overlapping_pairs"] = len(overlaps)
        self.results["part_spacing"] = entry
        classes: dict[tuple, _Group] = {}
        severity = "info" if limit is None else "warning"
        for gap in overlaps:
            key = ("overlap", gap.basis)
            if key not in classes:
                group = classes[key] = self.group("part_spacing", severity, margin=1.0,
                                                  basis=gap.basis, gap_mil=0.0)
                group.describe = self._describe_overlap(gap.basis, limit)
            classes[key].add(0.0, self._gap_example(gap))
        self._unplaced_overlaps(unplaced, placed)
        for gap in close:
            verdict = self.below(gap.gap_mil, limit, CLEARANCE_TOLERANCE_MIL) or "marginal"
            key = ("gap", gap.basis, verdict)
            if key not in classes:
                group = classes[key] = self.group(
                    "part_spacing", "warning", verdict, margin=(limit - gap.gap_mil) / limit,
                    basis=gap.basis, gap_mil=_round(gap.gap_mil),
                )
                group.range_keys = ("gap_mil", "gap_range_mil")
                group.describe = self._describe_part_gap(gap.basis, limit)
            classes[key].add(gap.gap_mil, self._gap_example(gap))

    def _unplaced_overlaps(self, gaps, placed: set) -> None:
        """One info class (not a miss) per part the assembler does not place whose
        outline overlaps fitted parts: a 3D model without pads, a mounting hole, a
        fiducial or a part not in the BOM."""
        parts = assembly.parts(self.index)
        classes: dict[int, _Group] = {}

        def order(component: int) -> tuple:  # the unplaced part, else the first by name
            return component in placed, natural_key(self.designator(component) or "")

        owned = sorted(((min(gap.first, gap.second, key=order), gap) for gap in gaps),
                       key=lambda item: (order(item[0]), item[1].first, item[1].second))
        for owner, gap in owned:
            if owner not in classes:
                name, reason = self.designator(owner), self._unplaced_reason(parts[owner])
                group = classes[owner] = self.group("part_spacing", "info", miss=False,
                                                    object=name, not_placed=reason)
                group.describe = self._describe_unplaced(name or "A part without a designator",
                                                         reason)
            classes[owner].add(0.0, self._gap_example(gap))

    @staticmethod
    def _unplaced_reason(part) -> str:
        if part.fiducial:
            return "a fiducial"
        if part.mount == "no_pads":
            return "a 3D model without pads"
        if part.mount == "mechanical":
            return "a mounting hole"
        if part.kind not in assembly.BOM_KINDS:
            return "not in the BOM"
        return "not placed"

    def _describe_unplaced(self, name, reason):
        def describe(group: _Group) -> str:
            parts = "part" if group.count == 1 else "parts"
            return (f"{name}, which the assembler does not place ({reason}), overlaps the "
                    f"outlines of {group.count} other {parts}: confirm it is meant to (a fan, "
                    "heatsink or shield above them, a screw head's room) or fix its 3D body.")
        return describe

    def _gap_example(self, gap):
        def make() -> dict:
            first, second = sorted((self.designator(gap.first) or "", self.designator(
                gap.second) or ""), key=natural_key)
            return {"at_mil": self.at(*gap.at), "objects": f"{first} / {second}",
                    "side": gap.side, "gap_mil": _round(gap.gap_mil)}
        return make

    def _describe_overlap(self, basis, limit):
        def describe(group: _Group) -> str:
            pairs = "pair" if group.count == 1 else "pairs"
            if basis == "body":
                text = (f"{group.count} {pairs} of placed parts whose 3D bodies overlap: they "
                        "collide unless one sits over the other on purpose (a shield can, a "
                        "part under a connector) or a body outline is wrong.")
            elif basis == "mixed":
                text = (f"{group.count} {pairs} of placed parts where one part's 3D body lies "
                        "over the other's pads: the pads cannot be soldered under it unless it "
                        "stands off on purpose; check the placement and the body.")
            else:
                text = (f"{group.count} {pairs} of placed parts whose pads overlap (pad copper, "
                        "or a rectangle around the pads of a part without a 3D body): check "
                        "the placement.")
            if limit is None:
                text += " The profile sets no min_part_spacing, so this is advisory."
            return text
        return describe

    def _describe_part_gap(self, basis, limit):
        what = {"body": "between their 3D body outlines",
                "pads": "between their pads (pad copper, or a rectangle around the pads of a "
                        "part without a 3D body)",
                "mixed": "from one part's 3D body to the other's pads"}[basis]

        def describe(group: _Group) -> str:
            pairs = "pair" if group.count == 1 else "pairs"
            span, minimum = self.gap_range(group, limit)
            text = (f"{group.count} {pairs} of same-side parts closer than the "
                    f"profile's minimum part spacing {minimum}: {span} {what}.")
            if group.marginal:
                text += self.marginal_text("min_part_spacing", limit, maker="assembler")
            return text
        return describe

    def check_part_to_edge(self) -> None:
        """Placed parts whose pad copper or 3D body is closer than min_part_to_edge to the
        board outline or a cutout (a warning). Their own info classes, whatever the
        limit: pads reaching or crossing the edge, bodies past it, edge connectors."""
        facts = assembly.part_to_edge(self.index)
        if not facts.has_outline:
            self.not_run("part_to_edge", "the board has no outline.")
            return
        limit = self.profile.get("min_part_to_edge")
        placed = set(self.placements().components)
        parts = assembly.parts(self.index)
        edges = sorted((edge for component, edge in facts.parts.items() if component in placed),
                       key=lambda edge: natural_key(self.designator(edge.component) or ""))
        measured = []  # (distance, basis, edge kind, point, part)
        crossing, overhang, fingers = [], [], []
        for edge in edges:
            part = parts[edge.component]
            if edge.fingers:
                fingers.append(edge)
                continue
            if edge.crosses:
                crossing.append(edge)
                continue
            if edge.overhang:
                overhang.append(edge)
            options = []
            if edge.pad_distance_mil is not None:
                options.append((edge.pad_distance_mil, "pads", edge.pad_edge, edge.pad_at))
            if edge.body_distance_mil is not None and not edge.overhang:
                options.append((edge.body_distance_mil, "body", edge.body_edge, edge.body_at))
            if options:
                measured.append((*min(options, key=lambda option: option[0]), part))
        measured.sort(key=lambda item: (item[0], item[4].component))
        nearest = measured[0] if measured else None
        self.part_edge = ((nearest[0], nearest[1]) if nearest else (None, None),
                          facts.reach_mil)
        off = [component for component in facts.off_board if component in placed]
        if off:
            self.notes.append(
                f"{len(off)} part(s) lie outside the board outline (parked?): part to edge "
                "skips them; check_pcb_health's components_outside_board lists them."
            )
        self._edge_info(crossing, "crossing", lambda edge: edge.pad_at)
        self._edge_info(overhang, "overhang", lambda edge: edge.body_at)
        self._edge_info(fingers, "fingers", lambda edge: edge.pad_at)
        if limit is None:
            where = (f" the nearest placed part's {nearest[1]} {self.len(nearest[0])} from "
                     "it (board_minimums)" if nearest else " no placed part within "
                     f"{self.len(facts.reach_mil)} of it")
            reason = ("the profile sets no min_part_to_edge (Altium's DFA guidance: "
                      f"{self.len(PART_EDGE_GUIDANCE_MIL)} for top-side SMD without breakaway "
                      f"rails; ask the assembler);{where}.")
            if not (crossing or overhang or fingers):
                self.not_run("part_to_edge", reason)
                return
            # Its own classes ran whatever the limit: a run entry, the distance unjudged.
            self.results["part_to_edge"] = {
                "status": "pass", "limit_mil": None,
                "worst_mil": _round(nearest[0]) if nearest else None,
                "note": "Not judged: " + reason,
            }
            return
        self.results["part_to_edge"] = {
            "status": "pass", "limit_mil": _round(limit),
            "worst_mil": _round(nearest[0]) if nearest else None,
        }
        cutoff = limit - CLEARANCE_TOLERANCE_MIL
        classes: dict[tuple, _Group] = {}
        for distance, basis, kind, point, part in measured:
            if distance >= cutoff:
                break
            verdict = self.below(distance, limit, CLEARANCE_TOLERANCE_MIL) or "marginal"
            key = (basis, kind, verdict)
            if key not in classes:
                group = classes[key] = self.group(
                    "part_to_edge", "warning", verdict, margin=(limit - distance) / limit,
                    basis=basis, distance_mil=_round(distance),
                )
                group.range_keys = ("distance_mil", "distance_range_mil")
                group.describe = self._describe_part_edge(basis, kind, limit)
            classes[key].add(distance, self.part_example(part, point,
                                                         distance_mil=_round(distance)))

    def _edge_info(self, edges, kind, point) -> None:
        """An info class of parts (not a miss): pads crossing the edge, bodies past it or
        edge connectors."""
        if not edges:
            return
        parts = assembly.parts(self.index)
        group = self.group("part_to_edge", "info", miss=False, kind=kind)
        for edge in edges:
            part = parts[edge.component]
            group.add(0.0, self.part_example(part, point(edge) or part.centre))
        texts = {
            "crossing": "whose pad copper reaches or crosses the board edge (or a pad lies "
                        "off it): castellated or edge-mounted on purpose? Otherwise move "
                        "{them} onto the board.",
            "overhang": "whose 3D body reaches past the board edge (connectors usually do on "
                        "purpose): confirm the assembler's rails and the panel allow it.",
            "fingers": "with card-edge fingers (an edge connector): left out of part to edge; "
                       "copper_to_edge reports the fingers' clearance.",
        }

        def describe(group: _Group) -> str:
            what, them = ("placed part", "it") if group.count == 1 else ("placed parts", "them")
            return f"{group.count} {what} {texts[kind].replace('{them}', them)}"
        group.describe = describe

    def _describe_part_edge(self, basis, kind, limit):
        edge = "a board cutout" if kind == "cutout" else "the board outline"
        what = "pad copper" if basis == "pads" else "3D body"

        def describe(group: _Group) -> str:
            parts, them = ("part", "it") if group.count == 1 else ("parts", "them")
            span, minimum = self.gap_range(group, limit)
            text = (f"{group.count} placed {parts} whose {what} is closer to {edge} than the "
                    f"profile's minimum part to edge {minimum}: {span} from it. Move {them} "
                    "in, or ask the assembler about breakaway rails.")
            if group.marginal:
                text += self.marginal_text("min_part_to_edge", limit, maker="assembler")
            return text
        return describe

    def check_fiducials(self) -> None:
        """Global fiducials per side with SMD placements against min_global_fiducials
        (typical GLOBAL_FIDUCIALS as info when unset), and fine-pitch parts (pitch at or
        below local_fiducial_pitch, typical LOCAL_FIDUCIAL_PITCH_MIL as info) with fewer
        than LOCAL_FIDUCIALS fiducials within dfm_assembly.LOCAL_FIDUCIAL_MIL."""
        counts = assembly.fiducial_counts(self.index)
        minimum = self.profile.get("min_global_fiducials")
        wanted = GLOBAL_FIDUCIALS if minimum is None else minimum
        pitch_limit = self.profile.get("local_fiducial_pitch")
        pitch = LOCAL_FIDUCIAL_PITCH_MIL if pitch_limit is None else pitch_limit
        sides = self.side_counts()
        # Two counts, not one objects: global fiducials missing per side, and fine-pitch
        # parts short of local ones.
        missing = {side: 0 for side in _SIDES}
        entry = self.results["fiducials"] = {
            "status": "pass", "limit": minimum,
            "value": {side: counts.get(side, 0) for side in _SIDES},
            "local_fiducial_pitch_mil": _round(pitch_limit),
            "missing_global": missing, "local_parts": 0,
        }
        wanted_key = "typical_global_fiducials" if minimum is None else "min_global_fiducials"
        for side in _SIDES:
            have = counts.get(side, 0)
            if not sides[side]["smd"] or have >= wanted:
                continue
            severity = "info" if minimum is None else "warning"
            group = self.group("fiducials", severity, margin=(wanted - have) / wanted,
                               side=side, fiducials=have, **{wanted_key: wanted})
            group.count = missing[side] = wanted - have
            group.describe = self._describe_global(side, have, wanted, sides[side]["smd"],
                                                   minimum is None)
        if pitch <= 0:
            return
        fine = [part for part in self.placed_parts()
                if part.pitch_mil is not None and part.pitch_mil <= pitch + EPS_MIL
                and (part.near_fiducials or 0) < LOCAL_FIDUCIALS]
        entry["local_parts"] = len(fine)
        if not fine:
            return
        pitch_key = ("typical_local_fiducial_pitch_mil" if pitch_limit is None
                     else "local_fiducial_pitch_mil")
        group = self.group("fiducials", "info" if pitch_limit is None else "warning",
                           **{pitch_key: _round(pitch)})
        group.range_keys = ("pitch_mil", "pitch_range_mil")
        for part in sorted(fine, key=lambda part: part.pitch_mil):
            group.add(part.pitch_mil, self.part_example(
                part, pitch_mil=_round(part.pitch_mil), fiducials_nearby=part.near_fiducials))
        group.describe = self._describe_local(pitch, pitch_limit is None)

    def _describe_global(self, side, have, wanted, smd, typical):
        def describe(group: _Group) -> str:
            found = ("No global fiducials" if not have else "1 global fiducial" if have == 1
                     else f"{have} global fiducials")
            placements = "placement" if smd == 1 else "placements"
            text = f"{found} on the {side} side, which has {smd} SMD {placements}; "
            if typical:
                return text + (f"assemblers typically want {wanted}, far apart near the "
                               "board corners (the profile sets no min_global_fiducials). "
                               "Fiducials on the panel's rails may cover it: confirm with "
                               "the assembler.")
            return text + f"the profile asks for {wanted} per assembled side."
        return describe

    def _describe_local(self, pitch, typical):
        reach = _text(assembly.LOCAL_FIDUCIAL_MIL, self.to_mm, 0)

        def describe(group: _Group) -> str:
            parts, have, its = (("part", "has", "its") if group.count == 1
                                else ("parts", "have", "their"))
            text = (f"{group.count} fitted {parts} with a pad pitch of {self.len(pitch)} or "
                    f"finer (finest {self.len(group.low)}) {have} fewer than "
                    f"{LOCAL_FIDUCIALS} fiducials within {reach} on {its} side: add two local "
                    "fiducials diagonally outside each part, or confirm with the assembler "
                    "that the global ones suffice.")
            if typical:
                text += (f" {self.len(pitch)} is a common rule of thumb, not the profile's "
                         "(it sets no local_fiducial_pitch).")
            return text
        return describe

    def check_tombstoning(self) -> None:
        """Fitted small two-terminal passives (0603 or smaller, or a pad span within
        0603's) whose terminals meet their copper unequally: one direct-connected to area
        copper and the other not (a warning), or, neither touching area copper, one on a
        much wider track (info: an imbalance to review)."""
        placed = set(self.placements().components)
        parts = assembly.parts(self.index)
        found = [tomb for tomb in assembly.tombstoning(self.index)
                 if tomb.small and tomb.component in placed]
        self.results["tombstoning"] = {"status": "pass", "small_passives": len(found)}
        classes: dict[tuple, _Group] = {}
        found.sort(key=lambda tomb: natural_key(self.designator(tomb.component) or ""))
        for tomb in found:
            if not tomb.causes:
                continue
            key = (tomb.size_code, tomb.causes)
            if key not in classes:
                severity = "warning" if "direct" in tomb.causes else "info"
                group = classes[key] = self.group(
                    "tombstoning", severity, size_code=tomb.size_code,
                    cause=" and ".join(tomb.causes),
                )
                group.describe = self._describe_tombstone(tomb.size_code, tomb.causes)
            fields = {"connections": [pad.style for pad in tomb.pads]}
            if "track" in tomb.causes:
                fields["tracks_mil"] = [_round(pad.widest_track_mil) for pad in tomb.pads]
            classes[key].add(0.0, self.part_example(parts[tomb.component], **fields),
                             nets=[self.net(pad.net) for pad in tomb.pads])

    def _describe_tombstone(self, size, causes):
        def describe(group: _Group) -> str:
            noun = "two-terminal passive" if group.count == 1 else "two-terminal passives"
            what = (f"{group.count} fitted {size} {noun}" if size else
                    f"{group.count} fitted small {noun} (size unknown, pad span within "
                    "0603's)")
            texts = []
            if "direct" in causes:
                texts.append(
                    "one pad connected directly to a pour, region or fill of its net and the "
                    "other not: the direct pad heats more slowly in reflow, so the part can "
                    "tombstone. Give the pour thermal reliefs (PolygonConnect rule) or "
                    "connect both pads alike."
                )
            if "track" in causes:
                texts.append(
                    "no area copper at either pad, and one pad on a track at least "
                    f"{self.len(assembly.TRACK_IMBALANCE_MIL)} wide and "
                    f"{assembly.TRACK_IMBALANCE_RATIO:g} times the other pad's widest track: "
                    "a thermal imbalance to review (a tombstoning risk, not a certainty); "
                    "neck the track down at the pad."
                )
            return f"{what} with " + " Also ".join(texts)
        return describe

    def check_fine_pitch(self) -> None:
        """Fitted parts whose pad pitch (the nearest pad centres of two terminals, for 4
        or more SMD terminals) is below min_pitch (an error); without the limit, parts at
        FINE_PITCH_MIL or finer as info."""
        limit = self.profile.get("min_pitch")
        pitched = sorted((part for part in self.placed_parts() if part.pitch_mil is not None),
                         key=lambda part: part.pitch_mil)
        finest = pitched[0].pitch_mil if pitched else None
        altimade = "altimade" in (self.profile.key, self.profile.based_on)
        unpublished = (" (Altimade checks component pitch but publishes no number)"
                       if altimade else "")
        if limit is None:
            fine = [part for part in pitched if part.pitch_mil <= FINE_PITCH_MIL + EPS_MIL]
            if not fine:
                where = (f"the finest pitch is {self.len(finest)}" if finest is not None
                         else "no fitted part has 4 or more SMD terminals")
                self.not_run("fine_pitch", f"the profile sets no min_pitch{unpublished}; "
                                           f"{where}.")
                return
            self.results["fine_pitch"] = {"status": "pass", "limit_mil": None,
                                          "worst_mil": _round(finest)}
            group = self.group("fine_pitch", "info",
                               typical_fine_pitch_mil=_round(FINE_PITCH_MIL))
            group.range_keys = ("pitch_mil", "pitch_range_mil")
            for part in fine:
                group.add(part.pitch_mil, self.part_example(part,
                                                            pitch_mil=_round(part.pitch_mil)))

            def describe(group: _Group) -> str:
                parts = "part" if group.count == 1 else "parts"
                return (f"{group.count} fitted {parts} with a pad pitch of "
                        f"{self.len(FINE_PITCH_MIL)} or finer (finest {self.len(group.low)}): "
                        f"the profile sets no min_pitch{unpublished}, so confirm the "
                        "assembler places this pitch (stencil and paste too).")
            group.describe = describe
            return
        self.results["fine_pitch"] = {"status": "pass", "limit_mil": _round(limit),
                                      "worst_mil": _round(finest)}
        classes: dict[tuple, _Group] = {}
        for part in pitched:
            verdict = self.below(part.pitch_mil, limit, EPS_MIL)
            if verdict is None:
                break
            key = (round(part.pitch_mil, 1), verdict)
            if key not in classes:
                group = classes[key] = self.group(
                    "fine_pitch", "error", verdict, margin=(limit - part.pitch_mil) / limit,
                    pitch_mil=_round(part.pitch_mil), min_pitch_mil=_round(limit),
                )
                group.describe = self._describe_pitch(part.pitch_mil, limit)
            classes[key].add(part.pitch_mil, self.part_example(
                part, pitch_mil=_round(part.pitch_mil)))

    def _describe_pitch(self, pitch, limit):
        def describe(group: _Group) -> str:
            value, minimum = self.pair(pitch, limit)
            parts = "part" if group.count == 1 else "parts"
            text = (f"{group.count} fitted {parts} with a pad pitch of {value}, below the "
                    f"profile's minimum pitch {minimum}.")
            if group.marginal:
                text += self.marginal_text("min_pitch", limit, maker="assembler")
            return text
        return describe

    def check_pick_and_place(self) -> None:
        """Altium's pick-and-place data (Center-X/Y: the midpoint of the box of a part's
        pad centres, over every pad; the file counts from the board's relative origin),
        whatever the profile. Fitted parts without pads, which Altium writes at the
        absolute origin: a warning for a part in the BOM (Standard, Net Tie in the BOM,
        Jumper) or one named a fiducial, else info. Placed parts whose Center-X/Y is off
        their footprint's extent (the 3D bodies' box, else the pad copper's) by
        ASYMMETRIC_SMD_MIL (SMD or mixed) or ASYMMETRIC_THT_MIL (through-hole): info.
        The relative origin anywhere but the outline's lower-left corner: info (Altium's
        own files were made from such boards). Ref-X/Y more than REFERENCE_OFFSET_MIL
        from Center-X/Y: an info class that is not a miss."""
        centroids = assembly.centroids(self.index)
        parts = assembly.parts(self.index)
        out = self.not_fitted()
        placed = self.placed_parts()
        origin = assembly.origin(self.index)
        pad_less = sorted(
            (part for part in parts if part.component not in out and part.x is not None
             and centroids[part.component].centre is None),
            key=lambda part: natural_key(self.designator(part.component) or ""))
        offsets = [centroids[part.component].extent_offset_mil for part in placed]
        offsets = [offset for offset in offsets if offset is not None]
        entry = self.results["pick_and_place"] = {
            "status": "pass", "parts": len(placed), "pad_less": len(pad_less),
            "origin": origin.position,
            "worst_offset_mil": _round(max(offsets)) if offsets else None,
        }
        if origin.to_lower_left_mil is not None:
            entry["origin_to_lower_left_mil"] = _round(origin.to_lower_left_mil)
        unverified = Counter(reason for part in placed
                             for reason in centroids[part.component].unverified)
        if unverified:
            entry["unverified"] = dict(sorted(unverified.items()))
            entry["note"] = (
                "Center-X/Y of these parts depends on a case Altium's own files did not "
                "settle (bare-hole pads counted, pads on both sides counted, the box taken "
                "in the board's frame for a rotation off 90 degrees): compare with the "
                "file Altium writes.")
        self._pnp_pad_less(pad_less)
        self._pnp_origin(origin)
        self._pnp_asymmetric(placed, centroids)
        self._pnp_reference(placed, centroids)

    def _pnp_pad_less(self, pad_less) -> None:
        """Fitted parts without pads: Center-X/Y falls back to the absolute origin."""
        classes: dict[str, _Group] = {}
        written = self.at(0.0, 0.0)
        for part in pad_less:
            reason = ("fiducial" if assembly.named_fiducial(part) else
                      "bom" if part.kind in PNP_BOM_KINDS else "not_placed")
            if reason not in classes:
                group = classes[reason] = self.group(
                    "pick_and_place", "info" if reason == "not_placed" else "warning",
                    pad_less=reason, centre_in_file_mil=written)
                group.describe = self._describe_pad_less(reason, written)
            classes[reason].add(0.0, self.part_example(
                part, (part.x, part.y), kind=KIND_NAMES.get(part.kind, str(part.kind))))

    def _describe_pad_less(self, reason, written):
        where = f"({self.len(written[0])}, {self.len(written[1])})"

        def describe(group: _Group) -> str:
            one = group.count == 1
            what = {
                "bom": "fitted part in the BOM" if one else "fitted parts in the BOM",
                "fiducial": "fiducial" if one else "fiducials",
                "not_placed": ("fitted part not placed by machine (Mechanical, Graphical or "
                               "Standard (No BOM))" if one else "fitted parts not placed by "
                               "machine (Mechanical, Graphical or Standard (No BOM))"),
            }[reason]
            text = (f"{group.count} {what} without pads: Altium's pick-and-place file has "
                    "no centre for a part without pads and writes Center-X/Y at the absolute "
                    f"origin, {where} from the board origin in the file. ")
            if reason == "fiducial":
                return text + ("The assembler would take a wrong fiducial position: make "
                               "each fiducial a single SMD pad, or give the assembler "
                               "Ref-X/Y (its reference point) for it.")
            if reason == "bom":
                return text + ("Give the assembler Ref-X/Y for it with the reference point "
                               "at the body centre, add the pads, or make it Standard (No "
                               "BOM) or Mechanical if no machine places it.")
            return text + ("If the file lists them (No BOM parts only with 'Include "
                           "Standard (No BOM) Items'), leave them out or tell the "
                           "assembler.")
        return describe

    def _pnp_origin(self, origin) -> None:
        """The relative origin anywhere but the outline's lower-left corner: info."""
        if origin.position in ("lower_left", "no_outline"):
            return
        box = origin.box
        group = self.group("pick_and_place", "info", origin=origin.position,
                           origin_to_lower_left_mil=_round(origin.to_lower_left_mil),
                           lower_left_mil=self.at(box[0], box[1]))
        group.count = 1
        reach = self.len(origin.to_lower_left_mil)

        def describe(group: _Group) -> str:
            text = {
                "not_set": "The board's relative origin is not set (it is the absolute "
                           f"origin), {reach} from the outline's lower-left corner",
                "other_corner": "The board's relative origin is at a corner of the outline "
                                f"other than the lower-left one, {reach} from it",
                "inside": "The board's relative origin is inside the board, "
                          f"{reach} from the outline's lower-left corner",
                "outside": "The board's relative origin is outside the outline, "
                           f"{reach} from its lower-left corner",
            }[origin.position]
            text += (": the pick-and-place file (and ODB++) count from it, so the board's "
                     "corner is not 0,0 there. Assemblers usually expect the lower-left "
                     "corner: set it there (Edit > Origin > Set) before making the outputs, "
                     "or confirm the assembler aligns the file with the board data. Gerbers "
                     "line up with the pick-and-place file and ODB++ only with Gerber Setup "
                     "> Advanced > 'Reference to relative origin'.")
            if origin.rigid_flex:
                text += (" The stack is rigid-flex: the outline read may be one region of the "
                         "board, so check the origin against the whole board.")
            return text
        group.describe = describe

    def _pnp_asymmetric(self, placed, centroids) -> None:
        """Placed parts whose Center-X/Y is off their footprint's extent: info."""
        classes: dict[tuple, _Group] = {}
        found = []
        for part in placed:
            facts = centroids[part.component]
            if facts.extent_offset_mil is None:
                continue
            mount = "tht" if part.mount == "tht" else "smd"
            limit = ASYMMETRIC_THT_MIL if mount == "tht" else ASYMMETRIC_SMD_MIL
            if facts.extent_offset_mil >= limit - EPS_MIL:
                found.append((facts.extent_offset_mil, mount, facts.extent_basis, limit,
                              part, facts))
        # Largest offset first: the class's offset_mil is its largest, examples the worst.
        found.sort(key=lambda item: (-item[0], item[4].component))
        for offset, mount, basis, limit, part, facts in found:
            reference = math.hypot(facts.reference[0] - facts.extent_centre[0],
                                   facts.reference[1] - facts.extent_centre[1])
            key = (mount, basis)
            group = classes.get(key)
            if group is None:
                group = classes[key] = self.group(
                    "pick_and_place", "info", mount=mount, basis=basis,
                    threshold_mil=_round(limit), offset_mil=_round(offset),
                    reference_at_centre=0)
                group.describe = self._describe_asymmetric(mount, basis, limit, offset)
            group.fields["reference_at_centre"] += reference < limit - EPS_MIL
            group.add(-offset, self.part_example(
                part, facts.centre, offset_mil=_round(offset),
                extent_at_mil=self.at(*facts.extent_centre),
                reference_offset_mil=_round(reference)))

    def _describe_asymmetric(self, mount, basis, limit, largest):
        extent = "3D body's centre" if basis == "body" else "pad copper extent's centre"
        kind = "through-hole" if mount == "tht" else "SMD or mixed"

        def describe(group: _Group) -> str:
            one = group.count == 1
            parts, these = ("part", "this part") if one else ("parts", "these parts")
            there = group.fields["reference_at_centre"]
            text = (f"{group.count} placed {kind} {parts} whose Center-X/Y (the midpoint of "
                    f"the outermost pad centres, as Altium writes it) is {self.len(limit)} or "
                    f"more from the footprint's {extent} (up to {self.len(largest)}): an "
                    "asymmetric footprint, such as an edge connector or a part with a tab. A "
                    "machine placing at Center-X/Y puts the body off by that much unless the "
                    "assembler's package data corrects it. Give the assembler Ref-X/Y for "
                    f"{these} with the footprint's reference point at the body centre")
            if there:
                has = "it already has" if one else (
                    f"{there} of them already {'has' if there == 1 else 'have'}")
                text += f" ({has} it there, within {self.len(limit)})"
            return text + ", or confirm the assembler corrects the offset."
        return describe

    def _pnp_reference(self, placed, centroids) -> None:
        """Ref-X/Y far from Center-X/Y: info, not a miss."""
        found = []
        for part in placed:
            facts = centroids[part.component]
            offset = facts.reference_offset_mil
            if offset is not None and offset > REFERENCE_OFFSET_MIL + EPS_MIL:
                found.append((offset, part, facts))
        if not found:
            return
        through = sum(1 for _, part, _ in found if part.mount == "tht")
        group = self.group("pick_and_place", "info", miss=False, kind="reference_offset",
                           threshold_mil=_round(REFERENCE_OFFSET_MIL), through_hole=through)
        found.sort(key=lambda item: (-item[0], item[1].component))
        for offset, part, facts in found:
            group.add(-offset, self.part_example(
                part, facts.centre, offset_mil=_round(offset),
                reference_at_mil=self.at(*facts.reference)))
        largest = found[0][0]
        group.fields["offset_mil"] = _round(largest)

        def describe(group: _Group) -> str:
            parts, their = ("placed part", "its") if group.count == 1 else ("placed parts",
                                                                            "their")
            text = (f"{group.count} {parts} whose reference point (Ref-X/Y) is more than "
                    f"{self.len(REFERENCE_OFFSET_MIL)} from {their} Center-X/Y (up to "
                    f"{self.len(largest)}")
            if through:
                text += f"; {through} through-hole, where a pin-1 origin is common"
            return text + ("). No problem when the assembler places from Center-X/Y, the "
                           "default columns; an assembler who places from Ref-X/Y must know.")
        group.describe = describe

    def assembly_minimums(self, out: dict, not_measured: list) -> None:
        """What the board asks of any assembler for the active variant: placements per
        side (mixed parts counted as THT), unique parts, fiducials per side, the finest
        pitch of the fitted parts, and the part gap and part to edge when their check
        ran."""
        counts = self.placements().counts
        out["placements"] = {}
        for side in _SIDES:
            out["placements"][f"{side}_smd"] = counts[side]["smd"]
            out["placements"][f"{side}_tht"] = counts[side]["tht"] + counts[side]["mixed"]
        unique, approximate = self.unique_parts()
        out["unique_parts"] = {"value": unique, "approximate": approximate}
        fiducials = assembly.fiducial_counts(self.index)
        out["fiducials"] = {side: fiducials.get(side, 0) for side in _SIDES}
        out["min_pitch_mil"] = _round(self.finest_pitch())
        if self.part_gap is not None:
            gap, overlapping, reach = self.part_gap
            out["min_part_gap"] = {
                "value_mil": _round(gap.gap_mil) if gap else None,
                "basis": gap.basis if gap else None, "reach_mil": _round(reach),
            }
            if overlapping:
                out["min_part_gap"]["overlapping_pairs"] = overlapping
        else:
            not_measured.append("min_part_gap")
        if self.part_edge is not None:
            (distance, basis), reach = self.part_edge
            out["min_part_to_edge"] = {"value_mil": _round(distance), "basis": basis,
                                       "reach_mil": _round(reach)}
        else:
            not_measured.append("min_part_to_edge")

    def assembly_notes(self) -> list[str]:
        if not any(check in self.checks for check in _ASSEMBLY):
            return []
        name = self.variant.variant
        text = (f"Assembly checks are for the active variant '{name}': its Not Fitted parts "
                "are left out (placements, unique parts, spacing, part to edge, the parts "
                "wanting local fiducials, tombstoning, pitch, pick and place and their board "
                "minimums). "
                "Fiducials are counted whatever the variant: their copper is on the bare "
                "board." if name else
                "Assembly checks count every part as fitted (no variant given).")
        if self.variant.netlist_free:
            text += (" No netlist is loaded: Not Fitted parts come from the designators "
                     "stored in the project's variants, and unique parts are approximate.")
        notes = [text]
        if "part_spacing" in self.checks or "part_to_edge" in self.checks:
            notes.append("Part outlines are the 3D bodies on the part's side and its pads' "
                         "copper (a part without a body: a rectangle around its pads): a "
                         "missing or oversized body changes part spacing and part to edge.")
        return notes

    # --- board facts ---

    def board(self) -> dict:
        stack = self.stack
        board = {
            "copper_layers": len(stack.order),
            "stack_thickness_mil": _round(stack.thickness_mil, 2),
        }
        total = stack_total_thickness(self.pcb.board.layers)
        board["total_thickness_mil"] = _round(total, 2) if total else None
        box = self.outline_box()
        board["size_mil"] = [_round(box[2] - box[0]), _round(box[3] - box[1])] if box else None
        board["embedded_boards"] = getattr(self.pcb, "embedded_boards", 0) or 0
        if not stack.has_heights:
            board["stack_heights"] = "missing: depths and aspect ratios not computed"
        if stack.legacy:
            board["stack_order"] = "legacy stackup: layer order assumed from layer ids"
        return board

    def board_minimums(self) -> dict:
        """What the board needs from any fabricator, whatever the profile. The gap,
        hole-to-copper, edge, hole-to-edge and hole-to-hole searches reach the larger of
        the profile's limit and dfm_geometry's default (value_mil None: nothing closer);
        they are measured only when their check runs (spacing, copper_to_edge,
        hole_to_hole; else listed in not_measured). worst_aspect_ratio is the board's
        (the profile's limit is max_aspect_ratio)."""
        minimums = self.minimums
        out = {"min_trace": minimums["min_trace"]}
        not_measured = []
        gaps = self.spacing_result
        if gaps is not None:
            gap = gaps.minimum()
            out["min_gap"] = {
                "value_mil": _round(gap.gap_mil) if gap else None,
                "layer": self.layer(gap.layer) if gap else None,
                "reach_mil": _round(gaps.reach_mil),
            }
            touching = sum(1 for record in gaps.records if "touching" in record.marks)
            if touching:
                out["min_gap"]["touching_pairs"] = touching
        else:
            not_measured.append("min_gap")
        walls = self.wall_result
        if walls is not None:
            wall = walls.minimum()
            out["min_hole_to_copper"] = {
                "value_mil": _round(wall.gap_mil) if wall else None,
                "layer": self.layer(wall.layer) if wall else None,
                "reach_mil": _round(walls.reach_mil),
            }
            touching = sum(1 for record in walls.records if "touching" in record.marks)
            if touching:
                out["min_hole_to_copper"]["touching_pairs"] = touching
        else:
            not_measured.append("min_hole_to_copper")
        for name in ("min_via_hole_mil", "min_pth_hole_mil", "min_npth_hole_mil",
                     "min_via_ring_mil", "min_pth_ring_mil", "plated_holes_without_ring"):
            if name in minimums:
                out[name] = minimums[name]
        drills = self.hole_result
        if drills is not None:
            pair = drills.minimum()
            out["min_hole_to_hole"] = {
                "value_mil": _round(pair.gap_mil) if pair else None,
                "reach_mil": _round(drills.reach_mil),
            }
            overlapping = sum(1 for record in drills.records if "touching" in record.marks)
            if overlapping:
                out["min_hole_to_hole"]["overlapping_pairs"] = overlapping
        else:
            not_measured.append("min_hole_to_hole")
        edges = self.edge_result
        if edges is not None and edges.has_outline:
            nearest = edges.minimum()
            out["min_copper_to_edge"] = {
                "value_mil": _round(nearest.distance_mil) if nearest else None,
                "layer": self.layer(nearest.layer) if nearest else None,
                "reach_mil": _round(edges.reach_mil),
            }
        else:
            not_measured.append("min_copper_to_edge")
        holes = self.hole_edge_result
        if holes is not None and holes.has_outline:
            near = holes.minimum()
            out["min_hole_to_edge"] = {
                "value_mil": _round(near.distance_mil) if near else None,
                "reach_mil": _round(holes.reach_mil),
            }
        else:
            not_measured.append("min_hole_to_edge")
        for name in ("worst_aspect_ratio", "worst_microvia_aspect_ratio"):
            if name in minimums:
                out[name] = minimums[name]
        self.assembly_minimums(out, not_measured)
        if not_measured:
            out["not_measured"] = not_measured
        return out

    # --- the result ---

    def finding(self, group: _Group) -> dict:
        finding = {"check": group.check, "severity": group.severity}
        if group.marginal:
            finding["marginal"] = True
        finding["issue"] = group.describe(group)
        finding["count"] = group.count
        if group.by_place and group.pairs > group.count:
            finding["object_pairs"] = group.pairs  # count is places
        fields = dict(group.fields)
        if group.kinds is not None:  # most places first
            fields["kinds"] = ", ".join(kind for kind, _ in group.kinds.most_common())
        finding.update({key: value for key, value in fields.items() if value is not None})
        if group.range_keys and group.pairs:
            value_key, range_key = group.range_keys
            finding[value_key] = _round(group.low)
            if group.high - group.low > 1e-4:
                finding[range_key] = [_round(group.low), _round(group.high)]
        if "layer" not in finding:
            layers = self.layers_field(group)
            if layers:
                finding["layers"] = layers
        if group.nets:
            finding["nets"] = list(group.nets)
        if group.board_rule:
            finding["board_rule"] = group.board_rule
        examples = [make() for _, _, make in sorted(group.examples, reverse=True)]
        examples = [example for example in examples if example]
        if len(group.layers) == 1:
            for example in examples:
                example.pop("layer", None)  # the finding's layers say it
        if examples:
            finding["examples"] = examples
        return finding

    def run(self) -> dict:
        for check in CHECKS:
            if check in ("trace_width", "hole_size", "annular_ring", "aspect_ratio") or (
                check in self.checks
            ):
                getattr(self, f"check_{check}")()
        groups = sorted(
            self.groups,
            key=lambda group: (_SEVERITY_RANK[group.severity], -group.margin,
                               CHECKS.index(group.check)),
        )
        findings = [self.finding(group) for group in groups]
        for check in self.checks:
            entry = self.results.get(check)
            if entry is None or entry["status"] == "not_checked":
                continue
            entry["status"] = self.status(check)
            mine = [group for group in self.groups if group.check == check]
            severities = [group.severity for group in mine]
            if check == "rules_vs_profile":
                # objects: the looser rules; one riding on a measured check's finding
                # (covered_by) is a warning all the same.
                entry["objects"] = len(entry.get("rules", ()))
                if entry["objects"]:
                    severities.append("warning")
            elif check != "fiducials":  # fiducials: missing_global and local_parts
                entry["objects"] = sum(group.count for group in mine if group.miss)
            entry["groups"] = len(mine)
            if severities:
                entry["severity"] = min(severities, key=_SEVERITY_RANK.get)
        results = {check: self.results[check] for check in self.checks if check in self.results}
        if not results.get("rules_vs_profile", {}).get("rules", True):
            results["rules_vs_profile"].pop("rules")
        profile = self.profile.echo(self.to_mm)
        profile.pop("scope", None)  # the result's own scope
        result = {
            "checks_run": list(self.checks),
            "scope": self.profile.scope,
            "profile": profile,
            "origin_mil": [round(self.origin[0], 3), round(self.origin[1], 3)],
            "board": self.board(),
            "board_minimums": self.board_minimums(),
            "results": results,
            "severity_counts": {
                level: sum(1 for finding in findings if finding["severity"] == level)
                for level in ("error", "warning", "info")
            },
            "drills": self.drill_table(),
            "findings": findings,
            "not_checked": self.not_checked_items(),
            "notes": self.final_notes(),
        }
        return result

    def not_checked_items(self) -> dict:
        items = dict(NOT_CHECKED)
        boards = getattr(self.pcb, "embedded_boards", 0) or 0
        if boards:
            items["panels"] = (f"This PcbDoc holds {boards} embedded board array(s) (a panel): "
                               "the panel's boards are not read, so they are not checked.")
        back_drills = [pair for pair in getattr(self.pcb.board, "drill_pairs", []) or []
                       if pair.back_drill]
        if back_drills:
            items["back_drills"] = (f"The layer stack defines {len(back_drills)} back "
                                    "drill(s): back-drilled holes are not in the drill table "
                                    "or the checks (check_vias reports them).")
        items.update(self.not_checked)
        items.update(self.profile.not_checked)
        return items

    def final_notes(self) -> list[str]:
        notes = list(self.notes)
        stale = sum(1 for pour in self.pcb.pours if pour.copper_invalid and not pour.shelved)
        shelved = sum(1 for pour in self.pcb.pours if pour.shelved)
        if stale or shelved:
            parts = []
            if stale:
                parts.append(f"{stale} pour(s) modified since they were poured are read as "
                             "last poured")
            if shelved:
                parts.append(f"{shelved} shelved pour(s) have no copper here")
            notes.append("; ".join(parts) + ": repour and save (check_pcb_health "
                         "shelved_modified_polygons).")
        notes += self.assembly_notes()
        if self.profile.chosen_by == "default":
            notes.append("No fabricator was chosen, so Altimade's published limits were "
                         "used; its service-only limits (layer counts, board size, copper "
                         "weight, placements and unique parts) are info, and a check "
                         "missing only those is advisory. Other fabricators differ: pass "
                         "profile or limits.")
        notes.append(
            "Holes are as drawn (Altium's finished hole); fabricators drill larger for "
            "plating. Example locations are relative to the board origin (origin_mil, or "
            "origin_mm in mm): add it for query_pcb_region."
        )
        return notes


# ---------- entry points ----------


def _selected(checks) -> tuple[tuple[str, ...] | None, dict | None]:
    requested = [str(check).strip().lower() for check in checks or [] if str(check).strip()]
    invalid = [check for check in requested if check not in VALID_CHECKS]
    if invalid:
        return None, {
            "error": "invalid_checks",
            "message": f"Unknown checks: {', '.join(invalid)}.",
            "valid_checks": list(VALID_CHECKS),
        }
    if not requested:
        return CHECKS, None
    return tuple(check for check in CHECKS if check in requested), None


def review_dfm(index, profile, checks: list[str] | None = None, to_mm: bool = False,
               variant: AssemblyContext | None = None) -> dict:
    """The whole DFM review of the board against profile, unpaged (page_dfm pages it), or
    {"error": "invalid_checks", ...}. variant: the active variant for the assembly checks
    and board minimums (None: every part fitted, unique parts approximate). Cached on the
    index per (checks, profile, units, variant): treat the result as read-only."""
    selected, error = _selected(checks)
    if error:
        return error
    variant = variant or AssemblyContext()
    key = (selected, to_mm, profile.cache_key(),
           json.dumps(profile.echo(to_mm), sort_keys=True, default=str), variant.key())
    cache = getattr(index, "_dfm_cache", None)
    if cache is None:
        cache = index._dfm_cache = {}
    if key not in cache:
        if len(cache) >= CACHE_SIZE:
            cache.pop(next(iter(cache)))
        review = _Review(index, profile, selected, to_mm, variant)
        try:
            cache[key] = review.run()
        finally:
            # The groups' describe closures hold the review, which holds the index: drop
            # them so a replaced index is freed without waiting for a garbage collection.
            review.groups = []
    return cache[key]


def page_dfm(full: dict, offset: int = 0, limit: int = 100, examples: int = EXAMPLES,
             drill_rows: int | None = None) -> dict:
    """A page of review_dfm's result: findings from offset (limit of them), each with at
    most `examples` examples (0: none) and max(PAGE_NETS, examples) sample nets, and the
    drill table cut to drill_rows rows when given (drills_total then counts them all).
    full is not changed."""
    result = {key: value for key, value in full.items() if key not in ("findings", "notes",
                                                                        "not_checked")}
    offset = max(0, int(offset or 0))
    findings = full["findings"]
    page = []
    nets = max(PAGE_NETS, examples)
    for finding in findings[offset: offset + limit]:
        listed = finding.get("examples")
        if listed is not None and len(listed) > examples:
            finding = dict(finding)
            if examples > 0:
                finding["examples"] = listed[:examples]
            else:
                finding.pop("examples")
        if len(finding.get("nets", ())) > nets:
            finding = dict(finding)
            finding["nets"] = finding["nets"][:nets]
        page.append(finding)
    drills = full["drills"]
    if drill_rows is not None and len(drills) > drill_rows:
        result["drills"] = drills[:drill_rows]
        result["drills_total"] = len(drills)
    result["findings"] = page
    result["total"] = len(findings)
    result["offset"] = offset
    result["has_more"] = offset + limit < len(findings)
    if result["has_more"]:
        result["next_offset"] = offset + limit
    result["not_checked"] = full["not_checked"]
    result["notes"] = full["notes"]
    return result
