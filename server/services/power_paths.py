"""Power path check (check_power_paths), offline: per rail, the current it carries, its
copper on the PcbDoc, the IPC-2221 temperature rise of its tracks and an estimate of the
DC resistance and IR drop from its source to each load.

Currents per rail. current= wins: amps per rail ('VIN=3,3V3=0.5') or per part: a load's
current on its supply rail ('U5=0.3', the rail holding most of its supply pins; 'U5@3V3'
picks the rail), a converter's output current ('U3=0.7', as check_thermal's load= does;
'U3@3V3' names the output of a converter with several, 'U3@5V' on its input rail is its
input current), a connector's or battery's supply current on the rail it feeds, an outlet
connector's draw, the current through a series part. Otherwise a rail's current is the sum
of what draws from it: the power tree's loads (fitted parts with a supply pin on it), its
outlet connectors (connector power pins on a rail made on the board), and the parts that
current= or the setup name with a pin on it. A load's own current comes from current=, from
a Power Analyzer or PDN Analyzer setup (used when there is exactly one for this board, or
setup= names it; a resistive load draws V/R; a converter listed there draws that on its
input), or from a 'Current Consumption' (or similar) part parameter; a converter on the rail
draws its outputs' power: a linear regulator or switch its output current, a switcher
Vout x Iout / (eta x Vin) with eta from eta= (check_thermal's parsing) and the rails'
voltages (check_derating's: rails=, names, the power tree); a rail fed through a series
part (ferrite, fuse, 0 ohm link, diode, FET) carries the current of the rail it feeds.
Some loads unknown: the sum of the known ones is a lower bound (partial). Each current
carries its source; current= and setup entries that apply to no rail are listed in notes.

Copper per rail net: tracks and arcs with widths and lengths per layer (hatched pours'
tracks are pour copper), vias with drills, pads; the area of pour, region and fill copper
of the net and the internal planes assigned to it. A rail whose pours, regions or planes
hold most of its copper area (POUR_SHARE) carries its current through them, which this
phase-1 estimate does not model: its resistance and drop are 'not estimated'
(pour_fed_not_estimated) and its narrowest track is still reported.

Widths. The narrowest routed width per layer and overall, leaving out stubs into pads (a
track or arc shorter than stub_length with an end on a pad: fanout necks), with the copper
thickness of its layer from the stackup (Altium stores one thickness per layer: whether the
outer layers' includes plating is the stack's own figure), 1 oz (35 um) labelled assumed
when the stack gives none, copper_oz= overriding.

IPC-2221 (section 6.2, the curve fit of its conductor charts): I = k x dT^0.44 x A^0.725,
A in mil^2 (width x thickness), k 0.048 on external and 0.024 on internal layers; so
dT = (I / (k x A^0.725))^(1/0.44), and the current at a rise. Its charts end at a 100 C
rise: a rise beyond it is reported as beyond the charts, not as a figure. IPC-2152 replaced
the charts with ones measured with board thickness, planes and copper weight; it is
chart-based (no formula is applied here) and is often less conservative, especially on
internal layers. The fit assumes a long conductor: stubs into pads (above) are not judged.

DC resistance: a track or arc piece R = rho(T) x L / (w x t), rho(T) = 1.72e-8 ohm m x
(1 + 0.00393 x (T - 20 C)) (temperature= T, default 20 C), L the piece's length outside
the pads and vias it ends on; a via's barrel R = rho x h / (pi x ((d/2 + p)^2 - (d/2)^2)),
h its length between the layers the current enters and leaves (from the stackup; a 1.6 mm
board when the stack has no heights, labelled assumed), d the drill, p the plating (25 um,
plating=); a through-hole pad's barrel alike (none when not plated). Pad and via copper and
the joins inside them are taken as 0 ohm. Copper joined by several barrels or tracks
between the same two nodes is in parallel.

IR drop (phase 1, no resistor-network solver): the net's copper graph (pcb_length's
NetGraph: track and arc pieces, vias with their barrels, pads, joined where they touch);
from the rail's source pads (converter outputs or the inductor, diode or FET a switcher's
output passes through, connector and battery power pins, the series part feeding it) to
each load's supply pads, the least-resistance path (Dijkstra) to the load's farthest pad
(each pad reached at its nearest layer). The paths form a tree from the sources: each
piece carries the currents of the loads downstream of it, and the drop to a load is the
sum of its pieces' R x I. With every such load's current known that is the drop (within
this model); otherwise it lies between the known loads' currents and the rail's current
through every piece whose loads are unknown (a piece every load's path uses carries the
whole rail current). Parallel copper off the paths (more tracks, pours) only lowers the
true resistance. A load reached only through pour, region or plane copper has no path here
(listed). Findings: ir_drop_high (the drop above drop_info_pct (2%) of the rail voltage:
info, above drop_warning_pct (5%): warning), temp_rise_high (a track's rise above
rise_info_c (10 C): info, above rise_warning_c (20 C): warning), path_through_few_vias (a
layer change whose vias, counting the ones within one via's resistance of the path as
parallel, carry more than IPC-2221's external formula on their barrel cross-section allows
at those rises; one per set of vias), each judged at the currents the pieces carry: where
only the upper bound (the rail's current on a piece some loads' currents are unknown on)
crosses a threshold the finding is at most info. power_analyzer_fail (a report's failing
or warning status), needs_current, needs_voltage (a drop in mV with no rail voltage to
judge it against) and pour_fed_not_estimated (info). A rail's status says what was done:
estimated, pour_fed, power_analyzer, or why not: no_source (the power tree finds none),
no_source_pads, no_loads, own_supply (a part's own regulator feeding its own pins),
no_load_pads, no_routing.

Power Analyzer by Keysight (parsers.power_analyzer): a <Project>.pdnaK setup is reported
(read only when it is plain text); the earlier PDN Analyzer's .pdna and .pidc_config
setups are listed with their sources, loads and VRMs; the HTML reports under
PowerAnalyzerByKeysight_Output\\HTMLReport are parsed, and those whose folder or pages
name this project or its PcbDoc replace the estimate for the rails they cover (its
findings are not raised there, a failing status is).

Everything is an estimate: the copper is as saved, currents come from the user, the
setup or part parameters, and a field solver (Power Analyzer) or a measurement decides.
"""

import heapq
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

try:
    from parsers import power_analyzer as pa
    from services.bom import natural_key
    from services.decoupling import pcb_components
    from services.derating import merged_params, net_voltages, read_parts
    from services.pcb_health import area as polygon_area
    from services.pcb_length import net_graph, pad_contains, stack_z
    from services.power_tree import SWITCHING_KINDS, build_tree, tree_data
    from services.thermal import parse_values
except ImportError:
    from server.parsers import power_analyzer as pa
    from server.services.bom import natural_key
    from server.services.decoupling import pcb_components
    from server.services.derating import merged_params, net_voltages, read_parts
    from server.services.pcb_health import area as polygon_area
    from server.services.pcb_length import net_graph, pad_contains, stack_z
    from server.services.power_tree import SWITCHING_KINDS, build_tree, tree_data
    from server.services.thermal import parse_values

CHECKS = ("temp_rise_high", "ir_drop_high", "path_through_few_vias", "power_analyzer_fail",
          "needs_current", "needs_voltage", "pour_fed_not_estimated")
_CHECK_RANK = {check: rank for rank, check in enumerate(CHECKS)}
_SEVERITY_RANK = {"warning": 0, "info": 1}
_LEVEL = {None: 0, "info": 1, "warning": 2}
PAGE_LIMIT = 100
CACHE_SIZE = 4
PATHS_LISTED = 10  # paths listed per rail, highest drop (else resistance) first
RAILS_LISTED = 25  # rails listed in a summary finding
SETUP_ENTRIES_LISTED = 12  # sources, loads, series parts and VRMs listed per setup
REPORTS_READ = 3  # Power Analyzer report folders read, newest first
REPORT_ROWS_LISTED = 40  # members and pages listed per report

MIL_PER_MM = 1 / 0.0254
M_PER_MIL = 25.4e-6
UM_PER_MIL = 25.4
RHO_20C = 1.72e-8  # ohm m, copper at 20 C (IACS annealed copper 1.7241e-8)
TEMPCO = 0.00393  # per C, copper's temperature coefficient of resistance at 20 C
OZ_MIL = 1.378  # 1 oz/ft^2 copper (35 um) in mil, as IPC-2221 calculators take it
DEFAULT_OZ = 1.0
DEFAULT_PLATING_UM = 25.0
DEFAULT_STUB_MIL = 1.0 * MIL_PER_MM
ASSUMED_BOARD_MIL = 1.6 * MIL_PER_MM
POUR_SHARE = 0.5  # pour, region and plane copper above this share of the net's area
K_EXTERNAL, K_INTERNAL = 0.048, 0.024
EXPONENT_RISE, EXPONENT_AREA = 0.44, 0.725
CHART_MAX_RISE_C = 100.0  # IPC-2221's conductor charts end at a 100 C rise
ZERO_OHM = 1e-12
DEFAULT_RISE_INFO, DEFAULT_RISE_WARNING = 10.0, 20.0  # C
DEFAULT_DROP_INFO, DEFAULT_DROP_WARNING = 2.0, 5.0  # % of the rail voltage
DEFAULTS = {"rise_info_c": DEFAULT_RISE_INFO, "rise_warning_c": DEFAULT_RISE_WARNING,
            "drop_info_pct": DEFAULT_DROP_INFO, "drop_warning_pct": DEFAULT_DROP_WARNING,
            "stub_mil": DEFAULT_STUB_MIL}

ESTIMATE = (
    "Estimates, not a simulation: currents come from current=, a Power Analyzer setup or "
    "part parameters; resistance is the least-resistance copper path over tracks, arcs and "
    "vias (pad and via copper 0 ohm; pours, regions and planes in parallel only lower it; "
    "rails fed mainly by pours are not estimated); each piece of the paths carries the "
    "currents of the loads downstream of it, and where those are unknown the drop is a "
    "range up to the rail's current. Temperature rise is IPC-2221's chart fit for an "
    "isolated long conductor, up to its charts' 100 C. Power Analyzer by Keysight (a field "
    "solver), the datasheets and a measurement decide."
)
SOURCES = (
    "IPC-2221 section 6.2 conductor current capacity, as its charts' curve fit "
    "I = k x dT^0.44 x A^0.725 (A in mil^2, k 0.048 external, 0.024 internal; the charts "
    "span 10 to 100 C rise), the formula trace-width calculators implement (Advanced "
    "Circuits' calculator, Altium's 'IPC-2221 Calculator for PCB Trace Current and "
    "Heating': 1 A at 10 C rise in 1 oz copper needs 11.8 mil outside, about 30.7 mil "
    "inside). IPC-2152 (2009) replaced those charts with measured ones that depend on board "
    "thickness, planes and copper weight; it is chart-based (no formula is applied here) and "
    "often less conservative, mainly for internal layers. Copper resistivity 1.72e-8 ohm m "
    "at 20 C with 0.00393 /C; a via's current capacity is IPC-2221's external formula on "
    "its barrel's cross-section, a common estimate."
)


# ---------- formulas ----------

def ipc2221_rise(current_a: float, area_mil2: float, external: bool) -> float:
    """Temperature rise (C) of a conductor of cross-section area_mil2 carrying current_a:
    dT = (I / (k x A^0.725))^(1/0.44)."""
    if current_a <= 0 or area_mil2 <= 0:
        return 0.0 if current_a <= 0 else math.inf
    k = K_EXTERNAL if external else K_INTERNAL
    return (current_a / (k * area_mil2 ** EXPONENT_AREA)) ** (1.0 / EXPONENT_RISE)


def ipc2221_current(rise_c: float, area_mil2: float, external: bool) -> float:
    """The current (A) a conductor carries at a temperature rise: k x dT^0.44 x A^0.725."""
    if rise_c <= 0 or area_mil2 <= 0:
        return 0.0
    k = K_EXTERNAL if external else K_INTERNAL
    return k * rise_c ** EXPONENT_RISE * area_mil2 ** EXPONENT_AREA


def ipc2221_width(current_a: float, rise_c: float, thickness_mil: float, external: bool) -> float:
    """The width (mil) IPC-2221 asks for a current at a rise in copper of a thickness."""
    k = K_EXTERNAL if external else K_INTERNAL
    area = (current_a / (k * rise_c ** EXPONENT_RISE)) ** (1.0 / EXPONENT_AREA)
    return area / thickness_mil


def resistivity(temperature_c: float = 20.0) -> float:
    """Copper's resistivity (ohm m) at a temperature."""
    return RHO_20C * (1.0 + TEMPCO * (temperature_c - 20.0))


def track_resistance(length_mil: float, width_mil: float, thickness_mil: float,
                     rho: float = RHO_20C) -> float:
    """R = rho x L / (w x t) of a track or arc piece (ohm)."""
    if width_mil <= 0 or thickness_mil <= 0:
        return math.inf
    return rho * length_mil * M_PER_MIL / (width_mil * M_PER_MIL * thickness_mil * M_PER_MIL)


def barrel_area_mil2(drill_mil: float, plating_mil: float) -> float:
    """The plated barrel's annular cross-section (mil^2)."""
    radius = drill_mil / 2.0
    return math.pi * ((radius + plating_mil) ** 2 - radius ** 2)


def via_resistance(length_mil: float, drill_mil: float, plating_mil: float,
                   rho: float = RHO_20C) -> float:
    """R = rho x h / (pi x ((d/2 + p)^2 - (d/2)^2)) of a plated barrel (ohm)."""
    area = barrel_area_mil2(drill_mil, plating_mil)
    if area <= 0:
        return math.inf
    return rho * length_mil * M_PER_MIL / (area * M_PER_MIL * M_PER_MIL)


def parallel(resistances) -> float:
    """Resistances in parallel (inf for none)."""
    conductance = sum(1.0 / value for value in resistances if value and math.isfinite(value))
    zero = any(value == 0 for value in resistances)
    if zero:
        return 0.0
    return 1.0 / conductance if conductance > 0 else math.inf


def rise_fields(rise: float, key: str = "rise_c") -> dict:
    """{key: the rise} within IPC-2221's charts, else {key: None, beyond_chart: True}: the
    fit says nothing meaningful past a 100 C rise."""
    if rise <= CHART_MAX_RISE_C:
        return {key: _r(rise)}
    return {key: None, "beyond_chart" if key == "rise_c" else f"{key[:-2]}_beyond_chart": True}


def _rise_text(rise: float, limit: float) -> str:
    if rise <= CHART_MAX_RISE_C:
        return f"puts its rise at {rise:.0f} C (above {limit:g} C)"
    return f"puts its rise beyond its charts (above {CHART_MAX_RISE_C:g} C)"


# ---------- arguments ----------

def parse_currents(text: str | None) -> tuple[dict[str, float] | None, dict | None]:
    """current= -> {name: amps} (a rail, a designator, or 'designator@rail')."""
    return parse_values(text, "current")


def parse_eta(text: str | None):
    return parse_values(text, "eta")


_OZ_ITEM = re.compile(r"\s*(?:(.+?)\s*[=:]\s*)?(\d+(?:\.\d+)?)\s*(?:oz)?\s*", re.I)


def parse_copper_oz(text: str | None) -> tuple[float | dict | None, dict | None]:
    """'2' (every signal layer) or 'outer=2,inner=0.5' or 'Top Layer=2,L3=1' (layer
    names): ounces of copper, 0.1 to 10."""
    error = {"error": "invalid_copper_oz",
             "message": "copper_oz takes ounces of copper per square foot (1 oz = 35 um): one "
                        "for every layer ('2'), or per layer: 'outer=2,inner=1' or "
                        "'Top Layer=2,Bottom Layer=2', each 0.1 to 10."}
    if text is None or not str(text).strip():
        return None, None
    items = [item for item in str(text).split(",") if item.strip()]
    out: dict[str, float] = {}
    for item in items:
        match = _OZ_ITEM.fullmatch(item)
        if not match:
            return None, error
        ounces = float(match[2])
        if not 0.1 <= ounces <= 10.0:
            return None, error
        if match[1] is None:
            if len(items) > 1:
                return None, error
            return ounces, None
        out[match[1].strip().casefold()] = ounces
    return out, None


# Part parameters giving a load's supply current (names normalised as check_derating
# reads them: lower case, words separated by spaces; a unit word at the end).
_CURRENT_PARAMS = frozenset({
    "current consumption", "consumption current", "consumption", "current draw",
    "supply current", "operating current", "load current", "input current",
    "power supply current", "supply current typ", "supply current max",
    "current consumption typ", "current consumption max", "max current consumption",
    "icc", "idd", "i cc", "i dd", "i supply", "isupply",
})
_UNIT_WORDS = {"a": 1.0, "amp": 1.0, "amps": 1.0, "ma": 1e-3, "ua": 1e-6, "µa": 1e-6,
               "μa": 1e-6}
_AMPS = re.compile(r"([-+]?\d+(?:[.,]\d+)?)\s*(mA|uA|µA|μA|A|amps?)?(?![A-Za-z])", re.I)


def param_current(params: dict[str, tuple[str, str]]) -> tuple[float, str, str] | None:
    """A load's current from its parameters (merged_params' map): (amps, parameter name,
    value), the largest current the value states ('15mA typ, 30mA max' -> 0.03); a value
    without a unit counts only when the name ends in one ('Current Consumption (mA)')."""
    for name, (raw_name, value) in params.items():
        words = name.split()
        unit_scale = None
        if len(words) > 1 and words[-1] in _UNIT_WORDS:
            unit_scale = _UNIT_WORDS[words[-1]]
            name = " ".join(words[:-1])
        if name not in _CURRENT_PARAMS:
            continue
        found = []
        for number, unit in _AMPS.findall(str(value)):
            scale = _UNIT_WORDS.get(unit.lower()) if unit else unit_scale
            if scale is None:
                continue
            amps = float(number.replace(",", ".")) * scale
            if 0 < amps <= 1000:
                found.append(amps)
        if found:
            return max(found), raw_name, value
    return None


# ---------- currents ----------

@dataclass
class Current:
    """A current with where it comes from."""

    amps: float | None
    source: str  # user, setup, parameter, converter_input, fed_rail, sum_of_loads
    basis: str
    partial: bool = False  # a sum of the known loads only: a lower bound
    known: int = 0
    total: int = 0
    needs: list[str] = field(default_factory=list)

    def row(self) -> dict:
        out: dict = {"amps": _r(self.amps), "source": self.source, "basis": self.basis}
        if self.partial:
            out["partial"] = True
            out["loads_known"] = f"{self.known} of {self.total}"
        if self.needs:
            out["needs"] = list(dict.fromkeys(self.needs))[:6]
        return out


class _Currents:
    """Rail and load currents over the power tree, memoised."""

    def __init__(self, tree, volts: dict, user: dict[str, float] | None, eta,
                 setup_loads: dict | None, plan, netlist: dict, notes: list[str]):
        self.tree = tree
        self.volts = volts
        self.eta = eta
        self.notes = notes
        self.components = netlist.get("components") or {}
        self.plan = plan
        self.folded_nets = {net.casefold(): net for net in tree.design.nets}
        self.folded_parts = {ref.casefold(): ref for ref in tree.design.part_pins}
        self.user_rails: dict[str, float] = {}
        self.user_parts: dict[tuple[str, str | None], float] = {}
        self.user_names: dict[tuple[str, str | None], str] = {}
        self.used: set[tuple[str, str | None]] = set()
        self.unknown_names: list[str] = []
        for name, amps in (user or {}).items():
            part, _, rail = name.partition("@")
            if not rail and part.casefold() in self.folded_nets and \
                    part.casefold() not in self.folded_parts:
                self.user_rails[self.folded_nets[part.casefold()]] = amps
                continue
            ref = self.folded_parts.get(part.strip().casefold())
            if ref is None:
                if not rail and part.casefold() in self.folded_nets:
                    self.user_rails[self.folded_nets[part.casefold()]] = amps
                else:
                    self.unknown_names.append(name)
                continue
            net = self.folded_nets.get(rail.strip().casefold()) if rail else None
            if rail and net is None:
                self.unknown_names.append(name)
                continue
            self.user_parts[(ref, net)] = amps
            self.user_names[(ref, net)] = name
        # What makes each rail: converter outputs, input connectors and batteries; what
        # passes it on: outlet connectors.
        self.makers: dict[str, list] = defaultdict(list)
        for conv in tree.converters.values():
            if conv.fitted:
                for out in conv.outputs:
                    self.makers[out.net].append((conv, out))
        self.inputs: dict[str, list[str]] = defaultdict(list)
        self.outlets: dict[str, list[tuple[str, list[str]]]] = defaultdict(list)
        for source in tree.sources:
            if source.kind == "connector" and source.role == "outlet":
                if source.part not in (part for part, _ in self.outlets[source.net]):
                    self.outlets[source.net].append((source.part, list(source.pins)))
            elif source.kind == "battery" or (source.kind == "connector" and
                                              source.role == "input"):
                if source.part not in self.inputs[source.net]:
                    self.inputs[source.net].append(source.part)
        # Setup loads by the design's own names; the ones naming no part or net are noted.
        self.setup: dict[tuple[str, str], dict] = {}
        self.setup_used: set[tuple[str, str]] = set()
        self.setup_unmatched: list[str] = []
        for (part, net), entry in (setup_loads or {}).items():
            ref, real = self.folded_parts.get(part), self.folded_nets.get(net)
            if ref is None or real is None:
                self.setup_unmatched.append(f"{entry['part']}@{entry['net']}")
                continue
            self.setup[(ref, real)] = entry
        self._rails: dict[str, Current] = {}
        self._loads: dict[tuple[str, str], Current | None] = {}
        self._visiting: set[str] = set()
        self._supply: dict[str, str | None] = {}
        self._downstream = self._feeds()
        self.series = {part for feeds in self._downstream.values() for part, _, _ in feeds}
        self._extra = self._extra_loads()

    # -- the tree --

    def _feeds(self) -> dict[str, list[tuple[str, str, str]]]:
        """Rail -> [(part, kind, rail it feeds)] through non-converter series edges, the
        downstream side by the edge's direction or the power tree's depth."""
        depth = self.tree.depth
        out: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
        for edge in self.tree.edges:
            if edge.converter:
                continue
            src, dst = edge.src, edge.dst
            if not edge.directed:
                a, b = depth.get(src), depth.get(dst)
                if a is None or b is None or a == b:
                    continue
                if a > b:
                    src, dst = dst, src
            out[src].append((edge.part, edge.kind, dst))
        return out

    def feeders(self, net: str) -> list[str]:
        """The series parts feeding a rail."""
        return list(dict.fromkeys(part for feeds in self._downstream.values()
                                  for part, _, dst in feeds if dst == net))

    def supply_rail(self, ref: str) -> str | None:
        """The rail a part draws from: the one with most of its supply pins (a power-tree
        load), else its outlet's rail, else the power-tree rail with most of its pins."""
        if ref in self._supply:
            return self._supply[ref]
        rails = [(len(pins), net) for net, loads in self.tree.loads.items()
                 for load, pins in loads if load == ref]
        if not rails:
            rails = [(len(pins) or 1, net) for net, outlets in self.outlets.items()
                     for part, pins in outlets if part == ref]
        if not rails:
            nodes = self.tree.nodes()
            count = Counter(net for net in (self.tree.design.part_pins.get(ref) or {}).values()
                            if net in nodes and net not in self.tree.grounds)
            rails = [(number, net) for net, number in count.items()]
        found = None
        if rails:
            rails.sort(key=lambda item: (-item[0], natural_key(item[1])))
            found = rails[0][1]
            if len(rails) > 1 and rails[0][0] == rails[1][0] and \
                    any(key[0] == ref and key[1] is None for key in self.user_parts):
                self.notes.append(f"current= gives {ref} without a rail and it draws from "
                                  f"{len(rails)} rails: taken on {found} ('{ref}@RAIL=amps' "
                                  "picks one).")
        self._supply[ref] = found
        return found

    def _extra_loads(self) -> dict[str, list[str]]:
        """Parts current= or the setup name drawing from a rail that the power tree does
        not list as its loads (a resistor, a crystal, an LED): rail -> designators."""
        extra: dict[str, list[str]] = defaultdict(list)
        makers = {conv.ref for convs in self.makers.values() for conv, _ in convs}
        nodes = self.tree.nodes()

        def add(ref: str, net: str | None) -> None:
            if net is None or net not in nodes or net in self.tree.grounds:
                return
            if ref in makers or ref in self.series or not self.tree.fitted(ref):
                return
            if net not in set((self.tree.design.part_pins.get(ref) or {}).values()):
                return
            if any(load == ref for load, _ in self.tree.loads.get(net, ())) or \
                    ref in self.inputs.get(net, ()) or \
                    any(part == ref for part, _ in self.outlets.get(net, ())):
                return
            if ref not in extra[net]:
                extra[net].append(ref)

        for ref, rail in self.user_parts:
            if ref in makers or ref in self.series or (
                    rail is None and any(ref in parts for parts in self.inputs.values())):
                continue  # a supply's or series part's designator: the rail it feeds
            add(ref, rail or self.supply_rail(ref))
        for ref, net in self.setup:
            add(ref, net)
        return extra

    def volt(self, net: str | None) -> float | None:
        found = self.volts.get(net) if net else None
        return None if found is None else found.volts

    def _eta(self, ref: str) -> float | None:
        if isinstance(self.eta, (int, float)):
            return float(self.eta)
        folded = {name.casefold(): value for name, value in (self.eta or {}).items()}
        return folded.get(ref.casefold())

    def _given(self, *keys) -> tuple[float, str] | None:
        """The first of keys current= gives: (amps, its name as given)."""
        for key in keys:
            if key in self.user_parts:
                self.used.add(key)
                return self.user_parts[key], self.user_names[key]
        return None

    # -- loads --

    def load_current(self, ref: str, net: str) -> Current | None:
        """The current a load draws from a rail, with its source (memoised)."""
        key = (ref, net)
        if key not in self._loads:
            self._loads[key] = self._load_current(ref, net)
        return self._loads[key]

    def _setup_current(self, ref: str, net: str, entry: dict) -> Current:
        self.setup_used.add((ref, net))
        basis = f"Power Analyzer setup {entry['file']}"
        amps = entry.get("amps") or 0.0
        if entry.get("siemens"):
            volts = self.volt(net)
            if volts is None:
                if not amps:
                    return Current(None, "setup", basis + ": a resistive load, its current "
                                   "needs the rail's voltage",
                                   needs=[f"rails: {net}'s voltage (rails='{net}=volts')"])
            else:
                amps += abs(volts) * entry["siemens"]
                basis += f" (resistive: V / R at {abs(volts):g} V)"
        return Current(amps, "setup", basis)

    def _load_current(self, ref: str, net: str) -> Current | None:
        if (ref, net) in self.setup:  # consulted (current= may still win over it)
            self.setup_used.add((ref, net))
        conv = self.tree.converters.get(ref)
        if conv is not None and conv.fitted:
            if net not in {out.net for out in conv.outputs}:
                given = self._given((ref, net))  # its input current, given
                if given is not None:
                    return Current(given[0], "user", f"current argument ({given[1]}: its input)")
                entry = self.setup.get((ref, net))
                if entry is not None and (ref, None) not in self.user_parts:
                    return self._setup_current(ref, net, entry)
            return self.converter_input(conv, net)
        if (ref, net) in self.user_parts or ((ref, None) in self.user_parts and
                                             self.supply_rail(ref) == net):
            given = self._given((ref, net), (ref, None))
            return Current(given[0], "user", f"current argument ({given[1]})")
        entry = self.setup.get((ref, net))
        if entry is not None:
            return self._setup_current(ref, net, entry)
        meta = self.components.get(ref) or {}
        found = param_current(merged_params(meta, self.plan, ref))
        if found is not None and self.supply_rail(ref) == net:
            amps, name, value = found
            return Current(amps, "parameter", f"{ref}'s parameter {name} = {value}")
        return None

    def converter_output(self, conv, out) -> Current | None:
        keys = [(conv.ref, out.net)] + ([(conv.ref, None)] if len(conv.outputs) == 1 else [])
        given = self._given(*keys)
        if given is not None:
            return Current(given[0], "user", f"current argument ({given[1]}: its output)")
        return self.rail(out.net)

    def converter_input(self, conv, net: str) -> Current:
        """The current a converter draws on its input: Iout for a linear regulator or a
        switch, Vout x Iout / (eta x Vin) for a switcher."""
        outputs = list(conv.outputs)
        if conv.inputs and net != conv.inputs[0]:
            channel = next((name for _, name, pin_net in conv.pins.get("in", ())
                            if pin_net == net), None)
            outputs = [out for out in outputs if out.channel and channel and
                       out.channel in channel]
        if not outputs:
            return Current(None, "converter_input", f"{conv.ref}'s input: no output read")
        total, known, needs, parts = 0.0, 0, [], []
        lower = False  # an output's current is itself a partial sum
        vin = self.volt(net)
        for out in outputs:
            iout = self.converter_output(conv, out)
            lower = lower or bool(iout is not None and iout.amps is not None and iout.partial)
            switching = (conv.kind in SWITCHING_KINDS or conv.kind == "charge_pump"
                         or (conv.kind == "pmic" and out.kind != "ldo")
                         or (conv.kind in ("charger", "regulator") and bool(conv.switch_nodes)))
            if iout is None or iout.amps is None:
                needs.append(f"current: {out.net}'s current (current='{out.net}=amps')")
                continue
            if not switching:  # a linear regulator or a switch passes its output current
                total += iout.amps
                known += 1
                parts.append(f"{out.net} {iout.amps:.3g} A")
                continue
            eta = self._eta(conv.ref)
            vout = self.volt(out.net)
            missing = []
            if eta is None:
                missing.append(f"eta: {conv.ref}'s efficiency (eta='{conv.ref}=0.85')")
            if vout is None:
                missing.append(f"rails: {out.net}'s voltage (rails='{out.net}=volts')")
            if vin is None:
                missing.append(f"rails: {net}'s voltage (rails='{net}=volts')")
            if missing:
                needs += missing
                continue
            amps = abs(vout) * iout.amps / (eta * abs(vin)) if vin else None
            if amps is None:
                continue
            total += amps
            known += 1
            parts.append(f"{out.net} {abs(vout):g} V x {iout.amps:.3g} A / ({eta:g} x "
                         f"{abs(vin):g} V)")
        if not known:
            return Current(None, "converter_input", f"{conv.ref}'s input", needs=needs)
        return Current(total, "converter_input",
                       f"{conv.ref}'s input: " + " + ".join(parts),
                       partial=known < len(outputs) or lower, known=known,
                       total=len(outputs), needs=needs)

    # -- rails --

    def loads(self, net: str) -> list[tuple[str, str, list[str]]]:
        """(designator, role, pins) of what draws from a rail: its loads (load), the
        series parts to the rails it feeds (feeds), its outlet connectors (outlet) and the
        parts current= or the setup name on it (load)."""
        out = [(ref, "load", pins) for ref, pins in self.tree.loads.get(net, ())]
        seen = {ref for ref, _, _ in out}
        for part, _, _ in self._downstream.get(net, ()):
            if part not in seen:
                out.append((part, "feeds", []))
                seen.add(part)
        for part, pins in self.outlets.get(net, ()):
            if part not in seen:
                out.append((part, "outlet", pins))
                seen.add(part)
        for ref in self._extra.get(net, ()):
            if ref not in seen:
                out.append((ref, "load", []))
                seen.add(ref)
        return out

    def draw(self, ref: str, role: str, net: str) -> Current | None:
        return self.fed_current(ref, net) if role == "feeds" else self.load_current(ref, net)

    def fed_current(self, part: str, net: str) -> Current | None:
        given = self._given((part, net), (part, None))
        if given is not None:
            return Current(given[0], "user", f"current argument ({given[1]}: through it)")
        entry = self.setup.get((part, net))
        if entry is not None:
            return self._setup_current(part, net, entry)
        rails = [dst for name, _, dst in self._downstream.get(net, ()) if name == part]
        currents = [self.rail(dst) for dst in rails]
        known = [current for current in currents if current is not None and
                 current.amps is not None]
        if not known:
            return None
        return Current(sum(current.amps for current in known), "fed_rail",
                       f"feeds {', '.join(rails)} through {part}",
                       partial=any(current.partial for current in known) or
                       len(known) < len(currents))

    def _given_supply(self, net: str) -> Current | None:
        """A rail's current given as what feeds it: a converter's output, a connector's or
        battery's supply, the current through the series part feeding it."""
        total, names, suppliers = 0.0, [], 0
        for conv, _ in self.makers.get(net, ()):
            suppliers += 1
            keys = [(conv.ref, net)] + ([(conv.ref, None)] if len(conv.outputs) == 1 else [])
            if (given := self._given(*keys)) is not None:
                total += given[0]
                names.append(f"{given[1]}: its output")
        for part, what in [*((part, "its supply") for part in self.inputs.get(net, ())),
                           *((part, "through it") for part in self.feeders(net))]:
            suppliers += 1
            if (given := self._given((part, net), (part, None))) is not None:
                total += given[0]
                names.append(f"{given[1]}: {what}")
        if not names:
            return None
        # Several supplies and only some given: what they give is a lower bound.
        return Current(total, "user", f"current argument ({'; '.join(names)})",
                       partial=len(names) < suppliers, known=len(names), total=suppliers)

    def rail(self, net: str) -> Current | None:
        if net in self._rails:
            return self._rails[net]
        if net in self.user_rails:
            current = Current(self.user_rails[net], "user", "current argument")
            self._rails[net] = current
            return current
        if net in self._visiting:
            return None  # a loop in the power tree
        given = self._given_supply(net)
        if given is not None:
            self._rails[net] = given
            return given
        self._visiting.add(net)
        total, known, count, partial, needs = 0.0, 0, 0, False, []
        sources = Counter()
        for ref, role, _ in self.loads(net):
            count += 1
            current = self.draw(ref, role, net)
            if current is None or current.amps is None:
                if current is not None:
                    needs += current.needs
                continue
            total += current.amps
            known += 1
            partial = partial or current.partial
            needs += current.needs
            sources[current.source] += 1
        self._visiting.discard(net)
        if not known:
            result = Current(None, "unknown", "no load current known", total=count,
                             needs=needs)
        else:
            basis = "sum of loads (" + ", ".join(
                f"{count_} {source.replace('_', ' ')}" for source, count_ in
                sorted(sources.items())) + ")"
            if partial and known == count:
                basis += "; some of them are partial sums"
            result = Current(total, "sum_of_loads", basis,
                             partial=partial or known < count, known=known, total=count,
                             needs=needs)
        self._rails[net] = result
        return result

    def unapplied(self) -> list[str]:
        """Notes on current= and setup entries that no rail or load used (every rail and
        load is evaluated first)."""
        for net in sorted(self.tree.nodes(), key=natural_key):
            if net in self.tree.grounds:
                continue
            self.rail(net)
            for ref, role, _ in self.loads(net):
                self.draw(ref, role, net)
        out = []
        unused = [self.user_names[key] for key in self.user_parts if key not in self.used]
        if unused:
            out.append(f"current= entries applied to no rail: {unused} (the part draws from "
                       "no power-tree rail or is not on the one named; 'PART@RAIL=amps' "
                       "names the rail, 'RAIL=amps' gives a rail's current).")
        idle = sorted(f"{ref}@{net}" for ref, net in self.setup if (ref, net) not in
                      self.setup_used) + self.setup_unmatched
        if idle:
            out.append(f"Setup load currents applied to no rail: {idle[:12]} (the part or "
                       "net is not in this design, or the part is not on that rail).")
        return out


# ---------- the copper graph ----------

class _UnionFind:
    def __init__(self, size: int):
        self.parent = list(range(size))

    def find(self, item: int) -> int:
        parent = self.parent
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    def union(self, a: int, b: int) -> None:
        a, b = self.find(a), self.find(b)
        if a != b:
            self.parent[a] = b


@dataclass
class _Member:
    """One conductor of an edge: a track or arc piece, or a via or pad barrel."""

    kind: str  # track | via | pad_barrel
    owner: int  # prims row id
    ohms: float
    layer: int | None = None  # tracks
    layers: tuple[int, int] | None = None  # barrels
    length_mil: float = 0.0  # piece length outside pads and vias, or barrel length
    width_mil: float | None = None
    drill_mil: float | None = None


class ResistanceGraph:
    """A net's copper as resistances: NetGraph's nodes joined by 0 ohm edges (pad copper,
    endpoint joins) merged into one node, track and arc pieces (their copper inside the
    pads and vias they end on left out: that copper is the pad's) and barrels as
    resistive edges, parallel ones between the same two nodes combined."""

    def __init__(self, graph, thickness: dict[int, float], rho: float, plating_mil: float,
                 primitives, barrel_mil):
        self.graph = graph
        size = len(graph.pos)
        joins = _UnionFind(size)
        members: list[tuple[int, int, _Member]] = []
        seg_by_prim = {segment[8]: segment for segment in graph.segments}
        copper_at = self._copper_at(graph)
        self.no_barrel = 0
        self.inside_pads_mil = 0.0
        for a, adjacency in enumerate(graph.adj):
            for b, _, number in adjacency:
                if b <= a:
                    continue
                kind, layer, layer_b, owner, known, weight = graph.edges[number]
                if kind == "attach" or (kind == "seg" and owner is None):
                    joins.union(a, b)
                    continue
                if kind == "seg":
                    segment = seg_by_prim[owner[1]]
                    width = segment[5] * 2.0
                    inside = min(1.0, self._inside_share(copper_at, a, b) +
                                 self._inside_share(copper_at, b, a))
                    length = weight * (1.0 - inside)
                    self.inside_pads_mil += weight - length
                    ohms = track_resistance(length, width, thickness.get(layer, OZ_MIL), rho)
                    member = _Member("track", owner[1], ohms, layer=layer, length_mil=length,
                                     width_mil=width)
                else:
                    pid = owner[1]
                    prim = primitives[pid - 1]
                    drill = prim.hole or 0.0
                    if owner[0] == "pad" and prim.plated is False:
                        continue  # a non-plated hole joins nothing
                    length = weight if known else barrel_mil(layer, layer_b)
                    ohms = via_resistance(length, drill, plating_mil, rho) if drill > 0 \
                        else math.inf
                    if not math.isfinite(ohms):
                        self.no_barrel += 1
                        continue
                    member = _Member("via" if owner[0] == "via" else "pad_barrel", pid, ohms,
                                     layers=(layer, layer_b), length_mil=length,
                                     drill_mil=drill)
                if member.ohms <= ZERO_OHM:
                    joins.union(a, b)
                    continue
                members.append((a, b, member))
        self.find = joins.find
        self.edges: dict[tuple[int, int], list[_Member]] = defaultdict(list)
        for a, b, member in members:
            ra, rb = joins.find(a), joins.find(b)
            if ra == rb:
                continue
            self.edges[(min(ra, rb), max(ra, rb))].append(member)
        self.ohms = {key: parallel([member.ohms for member in group])
                     for key, group in self.edges.items()}
        self.adj: dict[int, list[tuple[int, tuple[int, int]]]] = defaultdict(list)
        for key in self.edges:
            self.adj[key[0]].append((key[1], key))
            self.adj[key[1]].append((key[0], key))
        self.via_layers = {pid: {layer: self.find(node) for layer, node in nodes.items()}
                           for pid, nodes in graph.via_nodes.items()}
        self.layer_of: dict[int, int] = {}
        for node, (_, _, layer) in enumerate(graph.pos):
            self.layer_of.setdefault(self.find(node), layer)

    @staticmethod
    def _copper_at(graph) -> dict[int, list[tuple[str, int]]]:
        """Node -> the pads and vias (kind, prim id) its attach edges join it to."""
        pad_of = {node: pid for pid, nodes in graph.pad_nodes.items() for node in nodes.values()}
        via_of = {node: pid for pid, nodes in graph.via_nodes.items() for node in nodes.values()}
        out: dict[int, list[tuple[str, int]]] = defaultdict(list)
        for a, adjacency in enumerate(graph.adj):
            for b, _, number in adjacency:
                if graph.edges[number][0] != "attach":
                    continue
                if b in pad_of:
                    out[a].append(("pad", pad_of[b]))
                elif b in via_of:
                    out[a].append(("via", via_of[b]))
        return out

    def _inside(self, kind: str, pid: int, layer: int, x: float, y: float) -> bool:
        if kind == "pad":
            return pad_contains(self.graph.pads[pid], x, y, 0.0, layer)
        via = self.graph.vias[pid]
        return math.hypot(x - via["x"], y - via["y"]) <= via["r"]

    def _inside_share(self, copper_at, start: int, end: int) -> float:
        """The share of the piece from start to end up to where it leaves the pad or via
        copper start is joined to: that copper is at start's potential (0 ohm), so the
        piece counts from the far edge on, also when start itself only touches the pad and
        the piece then runs over it. Pads are convex: bisection finds the far edge from
        the last point found inside."""
        owners = copper_at.get(start)
        if not owners:
            return 0.0
        x0, y0, layer = self.graph.pos[start]
        x1, y1, _ = self.graph.pos[end]
        dx, dy = x1 - x0, y1 - y0
        span = dx * dx + dy * dy
        best = 0.0
        for kind, pid in owners:
            if self._inside(kind, pid, layer, x1, y1):
                return 1.0
            copper = self.graph.pads[pid] if kind == "pad" else self.graph.vias[pid]
            centre = ((copper["x"] - x0) * dx + (copper["y"] - y0) * dy) / span if span else 0.0
            tried = [step / 32.0 for step in range(32)] + [min(1.0, max(0.0, centre))]
            inside = [t for t in tried if self._inside(kind, pid, layer, x0 + dx * t,
                                                       y0 + dy * t)]
            if not inside:
                continue  # it only touches the pad beside the piece
            low, high = max(inside), 1.0
            for _ in range(30):
                middle = (low + high) / 2.0
                if self._inside(kind, pid, layer, x0 + (x1 - x0) * middle,
                                y0 + (y1 - y0) * middle):
                    low = middle
                else:
                    high = middle
            best = max(best, low)
        return best

    def pad_nodes(self, pad_ids) -> set[int]:
        return {self.find(node) for pid in pad_ids
                for node in self.graph.pad_nodes.get(pid, {}).values()}

    def dijkstra(self, sources) -> tuple[dict[int, float], dict[int, tuple]]:
        dist = {node: 0.0 for node in sources}
        previous: dict[int, tuple] = {}
        heap = [(0.0, node) for node in dist]
        heapq.heapify(heap)
        while heap:
            distance, node = heapq.heappop(heap)
            if distance > dist.get(node, math.inf):
                continue
            for other, key in self.adj.get(node, ()):
                candidate = distance + self.ohms[key]
                if candidate < dist.get(other, math.inf) - 1e-15:
                    dist[other] = candidate
                    previous[other] = (node, key)
                    heapq.heappush(heap, (candidate, other))
        return dist, previous

    def near(self, seeds, limit: float) -> set[int]:
        """Nodes within `limit` ohms of any seed over track edges only (no barrel)."""
        dist = {seed: 0.0 for seed in seeds}
        heap = [(0.0, seed) for seed in dist]
        while heap:
            distance, node = heapq.heappop(heap)
            if distance > dist.get(node, math.inf):
                continue
            for other, key in self.adj.get(node, ()):
                if any(member.kind != "track" for member in self.edges[key]):
                    continue
                candidate = distance + self.ohms[key]
                if candidate <= limit and candidate < dist.get(other, math.inf):
                    dist[other] = candidate
                    heapq.heappush(heap, (candidate, other))
        return set(dist)

    def parallel_vias(self, before, after, layer_a: int, layer_b: int,
                      via_ohms: float) -> int:
        """The vias from layer_a to layer_b whose ends lie within one via's resistance of
        the path's copper before (on layer_a) and after (on layer_b) the layer change:
        they share its current (the used ones included)."""
        near_before, near_after = self.near(before, via_ohms), self.near(after, via_ohms)
        count = 0
        for layers in self.via_layers.values():
            top, bottom = layers.get(layer_a), layers.get(layer_b)
            if top is not None and bottom is not None and top in near_before and \
                    bottom in near_after:
                count += 1
        return count


def _length(mil: float, units: str) -> str:
    return f"{mil:.1f} mil" if units == "mil" else f"{mil / MIL_PER_MM:.3g} mm"


def _r(value, digits: int = 4):
    if value is None or not math.isfinite(value):
        return None
    return float(f"{value:.{digits}g}")


def _mohm(ohms: float | None):
    return None if ohms is None or not math.isfinite(ohms) else round(ohms * 1000.0, 3)


def _worse(a: str | None, b: str | None) -> str | None:
    return a if _LEVEL[a] >= _LEVEL[b] else b


def _judged(low_level: str | None, high_level: str | None, exact: bool) -> str | None:
    """A finding's severity from its lower and upper bound: the upper bound alone gives at
    most info."""
    if exact or high_level is None:
        return low_level
    return _worse(low_level, "info")


def _bound_text(exact: bool, low_level: str | None, severity: str | None, low: str) -> str:
    """How a finding's figure relates to the truth: exact (within the model), already past
    the threshold at its lower bound, or an upper bound judged at most info."""
    if exact:
        return ""
    if severity and _LEVEL[low_level] >= _LEVEL[severity]:
        return f"; at least {low} with the known loads' currents alone"
    return "; an upper bound, so at most info until the loads' currents are given"


# ---------- the board ----------

class _Board:
    """The PCB side: nets, layers, thickness, areas, parts."""

    def __init__(self, index, options: dict, notes: list[str]):
        self.index = index
        self.pcb = index.pcb
        self.names = list(self.pcb.nets)
        self.ids: dict[str, int] = {}
        for number, name in enumerate(self.names):
            self.ids.setdefault(name.casefold(), number)
        self.origin = (self.pcb.board.origin_x or 0.0, self.pcb.board.origin_y or 0.0)
        self.order = index._copper_layers_in_stack_order()
        self.signal = [layer for layer in self.order if 1 <= layer <= 32]
        self.outer = {self.signal[0], self.signal[-1]} if self.signal else set()
        self._layer_names: dict[int, str] = {}
        self.thickness: dict[int, float] = {}
        self.thickness_source: dict[int, str] = {}
        stack = {row["v6_id"]: row["copper_thick_mil"] for row in index.db.execute(
            "SELECT v6_id, copper_thick_mil FROM layers WHERE kind = 'copper'")}
        override = options.get("copper_oz")
        for layer in self.signal:
            given = None
            if isinstance(override, (int, float)):
                given = float(override)
            elif isinstance(override, dict):
                name = self.layer_name(layer).casefold()
                kind = "outer" if layer in self.outer else "inner"
                given = override.get(name, override.get(kind))
            if given is not None:
                self.thickness[layer] = given * OZ_MIL
                self.thickness_source[layer] = "copper_oz"
            elif stack.get(layer):
                self.thickness[layer] = float(stack[layer])
                self.thickness_source[layer] = "stackup"
            else:
                self.thickness[layer] = DEFAULT_OZ * OZ_MIL
                self.thickness_source[layer] = "assumed 1 oz"
        if isinstance(override, dict):
            known = {self.layer_name(layer).casefold() for layer in self.signal} | {
                "outer", "inner"}
            if idle := sorted(name for name in override if name not in known):
                notes.append(f"copper_oz names no signal layer {idle}: ignored.")
        # Areas: per net and layer, pour/region/fill copper (mm^2) and hatched pour tracks.
        self.area_by_net: dict[int, dict[int, float]] = defaultdict(lambda: defaultdict(float))
        signal = set(self.signal)
        for area in self.pcb.areas:
            if area.net is None or area.layer not in signal or len(area.outline) < 3:
                continue
            size = polygon_area(list(area.outline)) - sum(
                polygon_area(list(hole)) for hole in area.holes if len(hole) >= 3)
            self.area_by_net[area.net][area.layer] += max(0.0, size) / MIL_PER_MM ** 2
        for row in index.db.execute(
                "SELECT net_id, layer, width, x1, y1, x2, y2, length FROM prims "
                "WHERE poured_by IS NOT NULL AND kind IN ('track', 'arc') "
                "AND net_id IS NOT NULL"):
            length = row["length"] if row["length"] is not None else math.hypot(
                row["x2"] - row["x1"], row["y2"] - row["y1"])
            self.area_by_net[row["net_id"]][row["layer"]] += (
                length * (row["width"] or 0.0) / MIL_PER_MM ** 2)
        self.planes: dict[str, list[str]] = defaultdict(list)
        for layer, name in (getattr(self.pcb.board, "plane_nets", None) or {}).items():
            self.planes[name.casefold()].append(self.layer_name(layer))
        # Parts: schematic designator (else PCB designator) -> PCB component indexes.
        self.parts: dict[str, set[int]] = defaultdict(set)
        for name, rows in pcb_components(index).items():
            for row in rows:
                self.parts[name.casefold()].add(row["id"])
        for number, _ in enumerate(self.pcb.components):
            designator = index.designator(number)
            if designator and designator.casefold() not in self.parts:
                self.parts[designator.casefold()].add(number)
        self.z_known = stack_z(index) is not None

    def layer_name(self, layer: int) -> str:
        if layer not in self._layer_names:
            self._layer_names[layer] = self.index.layer_name(layer)
        return self._layer_names[layer]

    def layer_type(self, layer: int) -> str:
        return "outer" if layer in self.outer else "inner"

    def at(self, x: float, y: float) -> dict:
        return {"x_mil": round(x - self.origin[0], 2), "y_mil": round(y - self.origin[1], 2)}

    def barrel_mil(self, layer_a: int, layer_b: int) -> float:
        """A barrel's length between two copper layers on a board of the assumed thickness
        (the stack has no heights): its share of the copper layer gaps."""
        position = {layer: number for number, layer in enumerate(self.order)}
        gaps = max(1, len(self.order) - 1)
        steps = abs(position.get(layer_a, 0) - position.get(layer_b, gaps))
        return ASSUMED_BOARD_MIL * steps / gaps


# ---------- one rail ----------

def _part_pads(board: _Board, graph, ref: str) -> list[int]:
    components = board.parts.get(ref.casefold(), set())
    return [pid for pid, pad in graph.pads.items() if pad["component"] in components]


def _pad_label(board: _Board, graph, pid: int) -> str:
    pad = graph.pads[pid]
    component = pad["component"]
    designator = board.index.designator(component) if component is not None and \
        0 <= component < len(board.pcb.components) else None
    return f"{designator}-{pad['name']}" if designator and pad["name"] else f"pad {pid}"


def _source_parts(tree, currents: _Currents, net: str) -> list[tuple[str, str]]:
    """(designator, what) of the parts a rail's current comes from: converter outputs
    (the part, or the inductor, diode or FET its output passes through), connector and
    battery inputs, internal regulators; else the series parts feeding it."""
    out: list[tuple[str, str]] = []
    members = {ref for ref, _ in tree.members(net)}
    for conv in tree.converters.values():
        if not conv.fitted:
            continue
        for output in conv.outputs:
            if output.net != net:
                continue
            if output.pins:
                out.append((conv.ref, f"{conv.ref} output"))
            named = [ref for ref in members if re.search(
                rf"(?<![\w-]){re.escape(ref)}(?![\w-])", output.via)]
            out += [(ref, f"{conv.ref} output through {ref}") for ref in named]
    for source in tree.sources:
        if source.net != net or source.kind == "converter":
            continue
        if source.kind == "connector" and source.role != "input":
            continue
        out.append((source.part, f"{source.kind} {source.part}"))
    if not out:
        for rail, feeds in currents._downstream.items():
            for part, kind, dst in feeds:
                if dst == net:
                    out.append((part, f"{kind.replace('_', ' ')} {part} from {rail}"))
    seen, unique = set(), []
    for ref, what in out:
        if ref not in seen:
            seen.add(ref)
            unique.append((ref, what))
    return unique


@dataclass
class _Traced:
    """One load's least-resistance path from the rail's sources."""

    ref: str
    role: str
    pid: int  # its farthest pad
    pads: int
    current: Current | None
    ohms: float
    keys: list[tuple[int, int]]
    nodes: list[int]
    origin: tuple | None

    @property
    def amps(self) -> float | None:
        return self.current.amps if self.current is not None else None


def _stubs(graph, stub: float) -> set[int]:
    """Prim ids of the stubs into pads: track or arc rows shorter than stub with an end
    on a pad."""
    pad_ends: set[int] = set()
    for a, adjacency in enumerate(graph.adj):
        for b, _, number in adjacency:
            if graph.edges[number][0] == "attach" and graph.pad_of_node(b) is not None:
                pad_ends.add(a)
    return {segment[8] for segment in graph.segments
            if segment[7] < stub and any(node in pad_ends
                                         for node in graph.track_ends.get(segment[8], ()))}


def _analyse(board: _Board, tree, currents: _Currents, net: str, pcb_id: int,
             rail_current: Current | None, volts: float | None, options: dict, rho: float,
             plating_mil: float) -> tuple[dict, list[dict]]:
    """A rail's row and its findings."""
    graph = net_graph(board.index, pcb_id)
    stub = options["stub_mil"]
    row: dict = {}
    # --- copper ---
    stub_ids = _stubs(graph, stub)
    layers: dict[int, dict] = {}
    narrowest = None
    stubs = 0
    narrowest_stub = None
    track_area = 0.0
    for x1, y1, x2, y2, layer, half, _, true, pid in graph.segments:
        width = half * 2.0
        if width <= 0:
            continue
        entry = layers.setdefault(layer, {"pieces": 0, "length": 0.0, "narrowest": None,
                                          "widest": 0.0, "stubs": 0})
        entry["pieces"] += 1
        entry["length"] += true
        entry["widest"] = max(entry["widest"], width)
        track_area += true * width / MIL_PER_MM ** 2
        item = (width, -true, layer, pid, (x1 + x2) / 2.0, (y1 + y2) / 2.0, true)
        if pid in stub_ids:
            stubs += 1
            entry["stubs"] += 1
            if narrowest_stub is None or item < narrowest_stub:
                narrowest_stub = item
            continue
        if entry["narrowest"] is None or item < entry["narrowest"]:
            entry["narrowest"] = item
        if narrowest is None or item < narrowest:
            narrowest = item
    pours = board.area_by_net.get(pcb_id, {})
    pour_area = sum(pours.values())
    planes = board.planes.get(board.names[pcb_id].casefold(), [])
    share = pour_area / (pour_area + track_area) if pour_area + track_area > 0 else 0.0
    drills = Counter()
    for pid in graph.vias:
        drills[round(board.pcb.primitives[pid - 1].hole or 0.0, 2)] += 1
    by_layer = []
    for layer in sorted(set(layers) | set(pours), key=lambda item: board.order.index(item)
                        if item in board.order else 1000 + item):
        entry = layers.get(layer, {})
        line = {"layer": board.layer_name(layer), "layer_type": board.layer_type(layer),
                "copper_um": round(board.thickness.get(layer, OZ_MIL) * UM_PER_MIL, 1),
                "copper_source": board.thickness_source.get(layer, "assumed 1 oz")}
        if entry:
            line.update(track_pieces=entry["pieces"], routed_mil=round(entry["length"], 1),
                        widest_mil=round(entry["widest"], 2))
            if entry["narrowest"] is not None:
                line["narrowest_mil"] = round(entry["narrowest"][0], 2)
            if entry["stubs"]:
                line["stubs_ignored"] = entry["stubs"]
        if pours.get(layer):
            line["pour_mm2"] = round(pours[layer], 2)
        by_layer.append(line)
    row["copper"] = {
        "track_pieces": sum(entry["pieces"] for entry in layers.values()),
        "routed_mil": round(graph.routed_mil, 1),
        "vias": len(graph.vias),
        "via_drills": [{"drill_mil": drill, "vias": count}
                       for drill, count in sorted(drills.items())],
        "pads": len(graph.pads),
        "track_mm2": round(track_area, 2),
        "pour_mm2": round(pour_area, 2),
        "pour_share": round(share, 3),
        **({"planes": planes} if planes else {}),
        "by_layer": by_layer,
    }
    rail_amps = rail_current.amps if rail_current is not None else None
    only_stubs = narrowest is None and narrowest_stub is not None
    if only_stubs:
        narrowest = narrowest_stub  # every piece is a stub: the narrowest of them
    if narrowest is not None:
        width, _, layer, pid, x, y, length = narrowest
        external = layer in board.outer
        area = width * board.thickness.get(layer, OZ_MIL)
        entry = {"width_mil": round(width, 2), "layer": board.layer_name(layer),
                 "layer_type": board.layer_type(layer), "length_mil": round(length, 1),
                 "copper_um": round(board.thickness.get(layer, OZ_MIL) * UM_PER_MIL, 1),
                 "at": board.at(x, y),
                 "capacity_a": {f"{options['rise_info_c']:g}C": _r(ipc2221_current(
                     options["rise_info_c"], area, external)),
                     f"{options['rise_warning_c']:g}C": _r(ipc2221_current(
                         options["rise_warning_c"], area, external))}}
        if rail_amps:
            entry.update(rise_fields(ipc2221_rise(rail_amps, area, external),
                                     "rise_at_rail_current_c"))
        if only_stubs:
            entry["only_stubs"] = True
        row["narrowest"] = entry
    if stubs and not only_stubs:
        row["stubs_ignored"] = {"count": stubs, "narrowest_mil": round(narrowest_stub[0], 2),
                                "shorter_than_mil": round(stub, 1)}
    findings: list[dict] = []
    if planes or share > POUR_SHARE:
        carrier = f"the plane {', '.join(planes)}" if planes else "pour, region or fill copper"
        row["status"] = "pour_fed"
        row["not_estimated"] = (f"carried mainly by {carrier} ({share:.0%} of its copper "
                                "area): resistance and drop are not estimated")
        return row, findings
    if not graph.segments and not graph.vias:
        row["status"] = "no_routing"
        row["not_estimated"] = "no track, arc or via on the net"
        return row, findings
    if pour_area > 0:
        row["pour_note"] = (f"{share:.0%} of its copper area is pour, region or fill copper, "
                            "not modelled: it carries part of the current, so the paths' "
                            "resistance is an upper bound")
    # --- paths ---
    sources = _source_parts(tree, currents, net)
    source_pads = {pid: what for ref, what in sources for pid in _part_pads(board, graph, ref)}
    loads = []
    off_board = 0
    source_refs = {ref for ref, _ in sources}
    for ref, role, _ in currents.loads(net):
        if ref in source_refs:
            continue
        pads = _part_pads(board, graph, ref)
        if not pads:
            off_board += 1
            continue
        loads.append((ref, role, pads, currents.draw(ref, role, net)))
    if sources:
        row["sources"] = [what for _, what in sources][:6]
    if not source_pads:
        row["status"] = "no_source_pads" if sources else "no_source"
        row["not_estimated"] = (
            "its source parts have no pad on this net on the board" if sources else
            "the power tree finds no source for it (a converter output, a connector or "
            "battery power input, or a series part from a supplied rail): get_power_tree")
        return row, findings
    if not loads:
        drawn = [ref for ref, _, _ in currents.loads(net)]
        if not drawn:
            row["status"] = "no_loads"
            row["not_estimated"] = ("the power tree lists no load on it (capacitors and "
                                    "resistors are not loads; outlet connectors and parts "
                                    "current= names are): current='PART@RAIL=amps' adds one")
        elif all(ref in source_refs for ref in drawn):
            row["status"] = "own_supply"
            row["not_estimated"] = ("only its source part draws from it (a part's own "
                                    "regulator feeding its own pins)")
        else:
            row["status"] = "no_load_pads"
            row["not_estimated"] = "its loads have no pad on this net on the board"
        return row, findings
    rgraph = ResistanceGraph(graph, board.thickness, rho, plating_mil, board.pcb.primitives,
                             board.barrel_mil)
    start = rgraph.pad_nodes(source_pads)
    dist, previous = rgraph.dijkstra(start)
    node_source = {}
    for pid, what in source_pads.items():
        for node in rgraph.pad_nodes([pid]):
            node_source.setdefault(node, (pid, what))
    traced: list[_Traced] = []
    no_path = []
    for ref, role, pads, current in loads:
        reached = []
        for pid in pads:
            # A pad is reached at its nearest node (a through-hole pad's other layers
            # hang off it).
            nearest = min(((dist[node], node) for node in rgraph.pad_nodes([pid])
                           if node in dist), default=None)
            if nearest is not None:
                reached.append((nearest[0], pid, nearest[1]))
        if not reached:
            no_path.append(ref)
            continue
        ohms, pid, node = max(reached)
        keys = []
        cursor = node
        while cursor in previous:
            cursor, key = previous[cursor]
            keys.append(key)
        keys.reverse()
        nodes = [cursor]
        for key in keys:
            nodes.append(key[1] if key[0] == nodes[-1] else key[0])
        traced.append(_Traced(ref, role, pid, len(pads), current, ohms, keys, nodes,
                              node_source.get(cursor)))
    # --- currents on the pieces: each carries the loads downstream of it ---
    users: dict[tuple, list[str]] = defaultdict(list)
    known: dict[tuple, float] = defaultdict(float)
    unknown: set[tuple] = set()
    lower: set[tuple] = set()  # pieces feeding a load whose current is a partial sum
    for path in traced:
        for key in path.keys:
            users[key].append(path.ref)
            if path.amps is None:
                unknown.add(key)
            else:
                known[key] += path.amps
                if path.current.partial:
                    lower.add(key)
    # The whole rail current (not a partial sum) reaches every path's shared pieces when
    # every load has a path on this board.
    whole = rail_amps if rail_current is not None and not rail_current.partial else None
    complete = not no_path and not off_board

    def bounds(key) -> tuple[float, float | None, str]:
        """(low, high, basis) of the current a piece carries."""
        if key not in unknown and key not in lower:
            return known[key], known[key], "the currents of the loads it feeds"
        if key not in unknown:
            return known[key], None, ("the currents of the loads it feeds, some of them "
                                      "partial sums (a lower bound)")
        if whole is not None and complete and len(users[key]) == len(traced):
            return whole, whole, "the rail's current (every load's path uses it)"
        if whole is not None:
            return known[key], max(whole, known[key]), (
                "up to the rail's current (the currents of loads it feeds are unknown)")
        return known[key], None, "the known loads' currents only (a lower bound)"

    edge_bounds = {key: bounds(key) for key in users}

    def level_drop(pct: float | None) -> str | None:
        if pct is None:
            return None
        return "warning" if pct > options["drop_warning_pct"] else \
            "info" if pct > options["drop_info_pct"] else None

    # Layer changes: each barrel piece once, oriented from the source.
    barrel_info: dict[tuple, dict] = {}
    for path in traced:
        for step, key in enumerate(path.keys):
            group = rgraph.edges[key]
            if key in barrel_info or not all(member.kind != "track" for member in group):
                continue
            node_at, other = path.nodes[step], path.nodes[step + 1]
            through_vias = [member for member in group if member.kind == "via"]
            layer_a, layer_b = rgraph.layer_of[node_at], rgraph.layer_of[other]
            nearby = rgraph.parallel_vias(path.nodes[:step + 1], path.nodes[step + 1:],
                                          layer_a, layer_b,
                                          max(member.ohms for member in group))
            pads_in = len(group) - len(through_vias)
            barrel_info[key] = {
                "from_layer": board.layer_name(layer_a), "to_layer": board.layer_name(layer_b),
                "vias": len(group), "vias_nearby": max(nearby, len(through_vias)) + pads_in,
                "drill_mil": round(min(member.drill_mil or 0.0 for member in group), 2),
                "through_pad": pads_in > 0,
                "owners": tuple(sorted(member.owner for member in group))}
    paths = []
    for path in traced:
        tracks_ohm = vias_ohm = length = 0.0
        via_owners: set[int] = set()
        changes: list[dict] = []
        path_narrowest = None
        layers_used: list[str] = []
        low = 0.0
        high: float | None = 0.0
        exact = True
        previous_barrel = False
        for key in path.keys:
            group = rgraph.edges[key]
            combined = rgraph.ohms[key]
            piece_low, piece_high, _ = edge_bounds[key]
            low += combined * piece_low
            high = None if high is None or piece_high is None else high + combined * piece_high
            exact = exact and piece_high is not None and piece_high == piece_low
            if key in barrel_info:
                vias_ohm += combined
                info = barrel_info[key]
                via_owners |= {member.owner for member in group if member.kind == "via"}
                if previous_barrel:  # one layer change through several barrels in a row
                    merged = changes[-1]
                    merged["to_layer"] = info["to_layer"]
                    merged["vias"] = min(merged["vias"], info["vias"])
                    merged["vias_nearby"] = min(merged["vias_nearby"], info["vias_nearby"])
                    merged["drill_mil"] = min(merged["drill_mil"], info["drill_mil"])
                    merged["through_pad"] = merged["through_pad"] or info["through_pad"]
                else:
                    changes.append({name: info[name] for name in (
                        "from_layer", "to_layer", "vias", "vias_nearby", "drill_mil",
                        "through_pad")})
                previous_barrel = True
                continue
            previous_barrel = False
            tracks_ohm += combined
            conductance = sum(1.0 / member.ohms for member in group)
            for member in group:
                if member.kind != "track":
                    continue
                length += member.length_mil * (1.0 / member.ohms) / conductance
                if path_narrowest is None or member.width_mil < path_narrowest[0]:
                    path_narrowest = (member.width_mil, member.layer)
                name = board.layer_name(member.layer)
                if name not in layers_used:
                    layers_used.append(name)
        entry = {"load": path.ref, "role": path.role, "pad": _pad_label(board, graph, path.pid),
                 "load_pads": path.pads, "from": path.origin[1] if path.origin else None,
                 "resistance_mohm": _mohm(path.ohms), "tracks_mohm": _mohm(tracks_ohm),
                 "vias_mohm": _mohm(vias_ohm), "track_mil": round(length, 1),
                 "vias": len(via_owners), "layers": layers_used}
        if path_narrowest is not None:
            entry["narrowest_mil"] = round(path_narrowest[0], 2)
        if changes:
            entry["layer_changes"] = changes
        if path.current is not None and path.amps is not None:
            entry["load_current"] = path.current.row()
        low_pct = high_pct = None
        if not path.keys:
            exact = True
        if exact and (low > 0 or path.amps is not None):
            entry["drop_mv"] = _r(low * 1000.0, 4)
            entry["drop_basis"] = ("each piece carries the currents of the loads downstream "
                                   "of it")
            if volts:
                low_pct = high_pct = low / abs(volts) * 100.0
                entry["drop_pct"] = _r(low_pct)
        elif not exact and (high is not None or low > 0):
            entry["drop_low_mv"] = _r(low * 1000.0, 4)
            if high is not None:
                entry["drop_high_mv"] = _r(high * 1000.0, 4)
            entry["drop_basis"] = (
                "low: the known loads' currents; high: the rail's current through every "
                "piece whose loads' currents are unknown" if high is not None else
                "the known loads' currents only (a lower bound)")
            if volts:
                low_pct = low / abs(volts) * 100.0
                entry["drop_low_pct"] = _r(low_pct)
                if high is not None:
                    high_pct = high / abs(volts) * 100.0
                    entry["drop_high_pct"] = _r(high_pct)
        judged_pct = high_pct if high_pct is not None else low_pct
        entry["_pct"] = judged_pct
        entry["_drop"] = high if high is not None and (high > 0 or path.amps is not None) \
            else (low if low > 0 else None)
        low_level = level_drop(low_pct)
        severity = _judged(low_level, level_drop(high_pct), exact)
        if severity:
            from_low = exact or _LEVEL[low_level] >= _LEVEL[severity]
            limit = options["drop_warning_pct" if severity == "warning" else "drop_info_pct"]
            if exact:
                said = (f"with each piece carrying the currents of the loads downstream of it "
                        f"it drops {low * 1000:.0f} mV, {low_pct:.1f}% of {abs(volts):g} V "
                        f"(above {limit:g}%)")
                basis = "the loads' currents along the path"
            elif high is None:
                said = (f"with the known loads' currents alone it drops at least "
                        f"{low * 1000:.0f} mV, {low_pct:.1f}% of {abs(volts):g} V (above "
                        f"{limit:g}%)")
                basis = "the known loads' currents (a lower bound)"
            else:
                said = (f"it drops {low * 1000:.0f} to {high * 1000:.0f} mV, {low_pct:.1f}% to "
                        f"{high_pct:.1f}% of {abs(volts):g} V")
                said += (f": even the lower figure is above {limit:g}%" if from_low else
                         f": the upper figure puts the rail's {whole:.3g} A through the pieces "
                         "whose loads' currents are unknown, so it is judged info until they "
                         "are given (current='PART=amps')")
                basis = "a range: the known loads' currents to the rail's current"
            findings.append(_finding(
                "ir_drop_high", severity,
                f"{net}: the path from {entry['from']} to {entry['pad']} measures "
                f"{path.ohms * 1000:.1f} mOhm; {said}.",
                rail=net, load=path.ref, pad=entry["pad"], resistance_mohm=_mohm(path.ohms),
                current_basis=basis, drop_mv=_r(low * 1000, 4) if exact else None,
                drop_low_mv=None if exact else _r(low * 1000, 4),
                drop_high_mv=None if exact or high is None else _r(high * 1000, 4),
                drop_pct=_r(judged_pct), voltage_v=volts,
                ratio=_r(judged_pct / options["drop_warning_pct"], 3)))
        paths.append((entry, path))
    # --- layer changes on too few vias: each set of vias once, at the current it carries ---
    few_vias: dict[tuple, tuple] = {}
    for key, info in barrel_info.items():
        piece_low, piece_high, basis = edge_bounds[key]
        area = barrel_area_mil2(info["drill_mil"], plating_mil)
        info_cap = ipc2221_current(options["rise_info_c"], area, True)
        warn_cap = ipc2221_current(options["rise_warning_c"], area, True)
        if not info_cap:
            continue
        count = max(info["vias"], info["vias_nearby"])

        def level_vias(amps, count=count, info_cap=info_cap, warn_cap=warn_cap):
            if amps is None:
                return None
            return "warning" if amps > count * warn_cap else \
                "info" if amps > count * info_cap else None

        exact = piece_high is not None and piece_high == piece_low
        low_level = level_vias(piece_low)
        severity = _judged(low_level, level_vias(piece_high), exact)
        if severity is None:
            continue
        amps = piece_high if piece_high is not None else piece_low
        needed = math.ceil(amps / info_cap)
        loads_through = sorted(set(users[key]), key=natural_key)
        rank = (_LEVEL[severity], amps / (count * warn_cap))
        held = few_vias.get(info["owners"])
        if held is not None and held[0] >= rank:
            continue
        bound = _bound_text(exact, low_level, severity, f"{piece_low:.3g} A")
        few_vias[info["owners"]] = (rank, _finding(
            "path_through_few_vias", severity,
            f"{net}: {info['vias']} via(s) ({count} counting those in parallel nearby, "
            f"{_length(info['drill_mil'], options['units'])} drill) carry the current to "
            f"{_names(loads_through)} from {info['from_layer']} to {info['to_layer']}: "
            f"{amps:.3g} A ({basis}{bound}) is above the {count * info_cap:.2g} A they "
            f"take at a {options['rise_info_c']:g} C rise; about {needed} would.",
            rail=net, load=loads_through[0], loads_through=loads_through,
            from_layer=info["from_layer"], to_layer=info["to_layer"], vias=info["vias"],
            vias_nearby=info["vias_nearby"], drill_mil=info["drill_mil"],
            current_a=_r(amps), current_basis=basis,
            per_via_a={f"{options['rise_info_c']:g}C": _r(info_cap),
                       f"{options['rise_warning_c']:g}C": _r(warn_cap)},
            vias_needed=needed, ratio=_r(amps / (count * warn_cap), 3)))
    findings += [finding for _, finding in few_vias.values()]
    # --- temperature rise on the power path (stubs into pads are not judged) ---
    worst = None
    worst_neck = None
    hot: set[int] = set()

    def level_rise(rise):
        if rise is None:
            return None
        return "warning" if rise > options["rise_warning_c"] else \
            "info" if rise > options["rise_info_c"] else None

    for key, (piece_low, piece_high, basis) in edge_bounds.items():
        group = rgraph.edges[key]
        conductance = sum(1.0 / member.ohms for member in group)
        for member in group:
            if member.kind != "track":
                continue
            part = (1.0 / member.ohms) / conductance
            thickness = board.thickness.get(member.layer, OZ_MIL)
            external = member.layer in board.outer
            area = member.width_mil * thickness
            low_amps = piece_low * part
            high_amps = None if piece_high is None else piece_high * part
            amps = high_amps if high_amps is not None else low_amps
            if amps <= 0:
                continue
            rise = ipc2221_rise(amps, area, external)
            exact = high_amps is not None and high_amps == low_amps
            low_level = level_rise(ipc2221_rise(low_amps, area, external))
            severity = _judged(low_level, level_rise(None if high_amps is None else rise),
                               exact)
            bound = _bound_text(exact, low_level, severity, f"{low_amps:.3g} A")
            item = ((_LEVEL[severity], rise, -member.width_mil, -member.owner), member, amps,
                    rise, basis, severity, bound)
            if member.owner in stub_ids:
                if worst_neck is None or item[0] > worst_neck[0]:
                    worst_neck = item
                continue
            if severity:
                hot.add(member.owner)
            if worst is None or item[0] > worst[0]:
                worst = item
    seg_by_prim = {segment[8]: segment for segment in graph.segments}
    for which, picked in (("hottest_track", worst), ("hottest_neck", worst_neck)):
        if picked is None:
            continue
        _, member, amps, rise, basis, severity, bound = picked
        segment = seg_by_prim[member.owner]
        thickness = board.thickness.get(member.layer, OZ_MIL)
        external = member.layer in board.outer
        width = member.width_mil
        x, y = (segment[0] + segment[2]) / 2.0, (segment[1] + segment[3]) / 2.0
        row[which] = {
            "width_mil": round(width, 2), "layer": board.layer_name(member.layer),
            "layer_type": board.layer_type(member.layer), "length_mil": round(segment[7], 1),
            "copper_um": round(thickness * UM_PER_MIL, 1),
            "current_a": _r(amps), "current_basis": basis, **rise_fields(rise),
            "at": board.at(x, y),
            "width_for_rise_mil": {f"{options['rise_info_c']:g}C": _r(ipc2221_width(
                amps, options["rise_info_c"], thickness, external), 4)}}
        row[which]["_rise"] = rise
        if which == "hottest_neck":
            row[which]["note"] = (f"a stub into a pad shorter than {_length(stub, options['units'])}"
                                  ": IPC-2221 assumes a long conductor and the pads sink its "
                                  "heat, so it is not judged")
            continue
        if severity:
            limit = options["rise_warning_c" if severity == "warning" else "rise_info_c"]
            needed = ipc2221_width(amps, options["rise_info_c"], thickness, external)
            findings.append(_finding(
                "temp_rise_high", severity,
                f"{net}: a {_length(width, options['units'])} track on "
                f"{board.layer_name(member.layer)} ({board.layer_type(member.layer)}, "
                f"{thickness * UM_PER_MIL:.0f} um) carries {amps:.3g} A ({basis}{bound}): "
                f"IPC-2221 {_rise_text(rise, limit)}; "
                f"{_length(needed, options['units'])} would rise {options['rise_info_c']:g} C.",
                rail=net, layer=board.layer_name(member.layer),
                layer_type=board.layer_type(member.layer), width_mil=round(width, 2),
                current_a=_r(amps), current_basis=basis, **rise_fields(rise),
                width_for_rise_mil=_r(needed, 4), tracks_above_info=len(hot),
                at=board.at(x, y),
                ratio=_r(amps / ipc2221_current(options["rise_warning_c"],
                                                width * thickness, external), 3)))
    paths.sort(key=lambda item: (-(item[0].get("_pct") or 0.0),
                                 -(item[0].get("_drop") or 0.0), -item[1].ohms,
                                 natural_key(item[1].ref)))
    row["status"] = "estimated"
    row["paths_total"] = len(paths)
    row["paths"] = [{key: value for key, value in entry.items() if not key.startswith("_")}
                    for entry, _ in paths[:PATHS_LISTED]]
    if paths:
        row["highest_resistance_mohm"] = max(_mohm(path.ohms) for _, path in paths)
        drops = [entry["_pct"] for entry, _ in paths if entry.get("_pct") is not None]
        if drops:
            row["highest_drop_pct"] = _r(max(drops))
        drops_mv = [entry["_drop"] for entry, _ in paths if entry.get("_drop") is not None]
        if drops_mv:
            row["highest_drop_mv"] = _r(max(drops_mv) * 1000.0, 4)
    if no_path:
        row["loads_without_track_path"] = sorted(no_path, key=natural_key)[:10]
        row["loads_without_track_path_total"] = len(no_path)
    if rgraph.no_barrel:
        row["barrels_skipped"] = rgraph.no_barrel
    return row, findings


def _names(items, limit: int = 4) -> str:
    items = list(items)
    shown = ", ".join(items[:limit])
    return shown + (f" and {len(items) - limit} more" if len(items) > limit else "")


def _finding(check: str, severity: str, issue: str, **fields) -> dict:
    out: dict = {"check": check, "severity": severity}
    out.update({key: value for key, value in fields.items() if value not in (None, [], {})})
    out["issue"] = issue
    return out


# ---------- Power Analyzer ----------

def _power_analyzer(root_dir, setup_name: str | None, notes: list[str],
                    pcb_doc: str | None = None, project_name: str | None = None
                    ) -> tuple[dict, dict]:
    """(the block reported, {(part, net) casefolded: {part, net, amps, siemens, file}} of
    the load currents of the setup used). A setup is used unasked only when it is the one
    setup with load currents and it names this PcbDoc (or names no board and sits in the
    project folder), so a neighbouring project's setup is never taken for this one's. A
    report applies only when its folder or pages name this project or PcbDoc."""
    found = pa.find_files(root_dir)
    block: dict = {"setups": [], "reports": []}
    readable = []
    for path in found["setups"]:
        try:
            setup = pa.read_setup(path)
        except Exception as error:  # a damaged file must not stop the check
            setup = {"file": path.name, "read": False, "error": type(error).__name__}
        brief = {key: setup.get(key) for key in ("file", "kind", "format", "read", "board",
                                                 "networks") if setup.get(key) is not None}
        for kind in ("sources", "loads", "series", "vrms"):
            if setup.get(kind):
                brief[f"{kind}_total"] = len(setup[kind])
                brief[kind] = setup[kind][:SETUP_ENTRIES_LISTED]
        if setup.get("kind") == "pdnak" and not setup.get("read"):
            brief["note"] = ("a Power Analyzer by Keysight document; its format is not "
                             "plain text, so only its presence is reported")
        block["setups"].append(brief)
        if setup.get("read") and setup.get("loads"):
            setup["_folder"] = path.parent
            readable.append(setup)
    loads: dict = {}
    chosen = None
    if setup_name:
        wanted = setup_name.strip().casefold()
        chosen = next((setup for setup in readable if setup["file"].casefold() == wanted or
                       Path(setup["file"]).stem.casefold() == wanted), None)
        if chosen is None:
            notes.append(f"setup names no readable Power Analyzer setup with loads "
                         f"('{setup_name}'): its currents are not used.")
    elif len(readable) == 1:
        named = readable[0].get("board")
        if (named and pcb_doc and named.casefold() == pcb_doc.casefold()) or (
                not named and root_dir and Path(readable[0]["_folder"]) == Path(root_dir)):
            chosen = readable[0]
        else:
            notes.append(f"The Power Analyzer / PDN Analyzer setup {readable[0]['file']} "
                         "lists load currents but names another board (or none): it is not "
                         "used unless setup= names it.")
    elif len(readable) > 1:
        notes.append(f"{len(readable)} Power Analyzer / PDN Analyzer setups list load "
                     "currents: none is used (setup= names the one to use).")
    if len(block["setups"]) > 1:
        # Several setups: only the one used lists its entries, the others their counts.
        for brief in block["setups"]:
            if chosen is None or brief["file"] != chosen["file"]:
                for kind in ("sources", "loads", "series", "vrms"):
                    brief.pop(kind, None)
    if chosen is not None:
        block["currents_from_setup"] = chosen["file"]
        for load in chosen["loads"]:
            amps, ohms = load.get("current_a"), load.get("resistance_ohm")
            if not load.get("part") or not load.get("net") or (amps is None and not ohms):
                continue
            key = (load["part"].casefold(), load["net"].casefold())
            held = loads.setdefault(key, {"part": load["part"], "net": load["net"],
                                          "amps": 0.0, "siemens": 0.0, "file": chosen["file"]})
            if amps is not None:
                held["amps"] += amps
            elif ohms and ohms > 0:
                held["siemens"] += 1.0 / ohms
    # A report folder is '<Project> [PDNA]_<date>'; a page may name the project or the
    # PcbDoc file.
    folder_names = [name for name in (project_name, Path(pcb_doc).stem if pcb_doc else None)
                    if name]
    text_names = [name for name in (project_name, pcb_doc) if name]
    foreign = []
    for folder in found["reports"][:REPORTS_READ]:
        try:
            report = pa.read_report(folder, text_names)
        except Exception as error:
            block["reports"].append({"folder": folder.name, "error": type(error).__name__})
            continue
        report["applies"] = not folder_names or pa.report_names(folder.name, folder_names) or \
            bool(report.get("names_found"))
        if not report["applies"]:
            foreign.append(folder.name)
        for key in ("members", "pages"):
            if len(report[key]) > REPORT_ROWS_LISTED:
                report[f"{key}_total"] = len(report[key])
                report[key] = report[key][:REPORT_ROWS_LISTED]
        block["reports"].append(report)
    if foreign:
        notes.append(f"Power Analyzer report(s) {foreign} name neither this project nor its "
                     "PcbDoc (folder '<Project> [PDNA]_<date>' or the pages' text): listed, "
                     "not applied.")
    if len(found["reports"]) > REPORTS_READ:
        block["older_reports_not_read"] = len(found["reports"]) - REPORTS_READ
    if not block["setups"] and not block["reports"]:
        return {}, loads
    return block, loads


def _report_results(block: dict) -> dict[str, dict]:
    """net (casefolded) -> {net, report, metrics} of the newest applying report naming
    it."""
    out: dict[str, dict] = {}
    for report in block.get("reports", ()):
        if not report.get("applies"):
            continue
        for net, metrics in (report.get("results") or {}).items():
            key = net.casefold()
            if key in out:
                continue
            row = {"report": report["folder"]}
            for role in ("vdrop", "idensity", "ivia"):
                if role in metrics:
                    row[role] = metrics[role].get("text")
                    if metrics[role].get("unit") in ("V", "A"):
                        row[f"{role}_{'v' if metrics[role]['unit'] == 'V' else 'a'}"] = \
                            _r(metrics[role]["value"], 4)
            if metrics.get("status"):
                row["status"] = metrics["status"]
            out[key] = {"net": net, **row}
    return out


def _analyzer_finding(net: str, covered: dict) -> dict | None:
    """power_analyzer_fail for a report row whose status is fail (warning) or warning
    (info)."""
    status = covered.get("status")
    if status not in ("fail", "warning"):
        return None
    values = ", ".join(f"{label} {covered[role]}" for role, label in (
        ("vdrop", "Vdrop"), ("idensity", "Idensity"), ("ivia", "Ivia_max")) if covered.get(role))
    return _finding(
        "power_analyzer_fail", "warning" if status == "fail" else "info",
        f"{net}: Power Analyzer's report {covered['report']} gives it status {status}"
        f"{f' ({values})' if values else ''}: its List of Violations and Failed Via Summary "
        "say where.",
        rail=net, report=covered["report"], status=status, vdrop=covered.get("vdrop"),
        idensity=covered.get("idensity"), ivia=covered.get("ivia"),
        ratio=2.0 if status == "fail" else 1.0)


# ---------- the check ----------

def prepare_power_paths(
    netlist: dict,
    plan=None,
    project_params: dict | None = None,
    *,
    index,
    rails: dict[str, float] | None = None,
    current: dict[str, float] | None = None,
    eta=None,
    copper_oz=None,
    plating_um: float = DEFAULT_PLATING_UM,
    temperature_c: float = 20.0,
    stub_mil: float = DEFAULT_STUB_MIL,
    rise_info_c: float = DEFAULT_RISE_INFO,
    rise_warning_c: float = DEFAULT_RISE_WARNING,
    drop_info_pct: float = DEFAULT_DROP_INFO,
    drop_warning_pct: float = DEFAULT_DROP_WARNING,
    rail_names: list[str] | None = None,
    nets: list[str] | None = None,
    checks: list[str] | None = None,
    setup: str | None = None,
    root_dir=None,
    pcb_doc: str | None = None,
    project_name: str | None = None,
    units: str = "mm",
) -> dict:
    """Analyse every rail once; render_power_paths pages the result. index is the board's
    PcbIndex (required). An {"error": ...} dict for an unknown check, rail or net."""
    requested = [check.strip().lower() for check in checks or [] if check.strip()]
    if invalid := [check for check in requested if check not in CHECKS]:
        return {"error": "invalid_checks",
                "message": f"Unknown checks {invalid}; use {list(CHECKS)}."}
    if rise_warning_c < rise_info_c or drop_warning_pct < drop_info_pct:
        return {"error": "invalid_thresholds",
                "message": "The warning thresholds must not be below the info ones "
                           "(rise_warning_c >= rise_info_c, drop_warning_pct >= "
                           "drop_info_pct)."}
    wanted = set(requested or CHECKS)
    notes: list[str] = []
    options = {"units": units, "copper_oz": copper_oz, "stub_mil": stub_mil,
               "rise_info_c": rise_info_c,
               "rise_warning_c": rise_warning_c, "drop_info_pct": drop_info_pct,
               "drop_warning_pct": drop_warning_pct}
    rho = resistivity(temperature_c)
    plating_mil = plating_um / UM_PER_MIL
    board = _Board(index, options, notes)

    # --- the schematic: rails, voltages, currents ---
    tree = build_tree(netlist, plan, project_params)
    data = tree_data(tree)
    parts, _, _ = read_parts(tree, netlist, plan)
    volts, unknown_rails = net_voltages(tree, data, rails, parts)
    if unknown_rails:
        notes.append(f"rails names no net {unknown_rails}: ignored (query_net finds the real "
                     "name).")
    analyzer, setup_loads = _power_analyzer(root_dir, setup, notes, pcb_doc, project_name)
    currents = _Currents(tree, volts, current, eta, setup_loads, plan, netlist, notes)
    if currents.unknown_names:
        notes.append(f"current= names no rail or part {currents.unknown_names}: ignored.")

    # --- which nets ---
    nodes = sorted((net for net in tree.nodes() if net not in tree.grounds), key=natural_key)
    selected = nodes
    if rail_names or nets:
        selected = []
        missing = []
        folded_nodes = {net.casefold(): net for net in tree.design.nets}
        for name in rail_names or ():
            real = folded_nodes.get(name.strip().casefold())
            if real is None and name.strip().casefold() in board.ids:
                real = board.names[board.ids[name.strip().casefold()]]
            if real is None:
                missing.append(name)
            elif real not in selected:
                selected.append(real)
        for name in nets or ():
            folded = name.strip().casefold()
            if folded not in board.ids:
                missing.append(name)
                continue
            real = folded_nodes.get(folded) or board.names[board.ids[folded]]
            if real not in selected:
                selected.append(real)
        if not selected:
            return {"error": "rail_not_found",
                    "message": f"No rail or PCB net {missing}; get_power_tree lists the rails "
                               "and query_net or get_net_pcb finds a net's real name."}
        if missing:
            notes.append(f"Not found: {', '.join(missing)}.")
    results = _report_results(analyzer) if analyzer else {}

    rows, findings = [], []
    counts = Counter()
    current_sources = Counter()
    not_on_pcb = []
    for net in selected:
        pcb_id = board.ids.get(net.casefold())
        if pcb_id is None:
            not_on_pcb.append(net)
            continue
        rail_current = currents.rail(net)
        volt = currents.volts.get(net)
        row: dict = {"rail": net, "voltage_v": _r(volt.volts) if volt else None,
                     "voltage_source": volt.source if volt else None,
                     "current": rail_current.row() if rail_current else None}
        if rail_current is not None:
            current_sources["partial" if rail_current.partial else rail_current.source] += 1
        row_data, row_findings = _analyse(
            board, tree, currents, net, pcb_id, rail_current, volt.volts if volt else None,
            options, rho, plating_mil)
        row.update(row_data)
        covered = results.get(net.casefold())
        if covered is not None:
            row["power_analyzer"] = covered
            row["estimate_status"] = row.get("status")  # the estimate, kept beside it
            row["status"] = "power_analyzer"
            if row_findings:  # the field solver's result replaces the estimate's
                row["estimate_findings"] = dict(Counter(
                    f"{finding['check']} {finding['severity']}" for finding in row_findings))
            row_findings = [finding for finding in (_analyzer_finding(net, covered),)
                            if finding is not None]
        counts[row["status"]] += 1
        rows.append(row)
        findings += [finding for finding in row_findings if finding["check"] in wanted]

    # --- summary findings ---
    unknown = [row for row in rows if row.get("status") == "estimated" and (
        row["current"] is None or row["current"]["amps"] is None)]
    if "needs_current" in wanted and unknown:
        listed = sorted(unknown, key=lambda row: (-(row.get("highest_resistance_mohm") or 0),
                                                  natural_key(row["rail"])))
        needs = sorted({need for row in unknown for need in
                        (row["current"] or {}).get("needs", ())})
        findings.append(_finding(
            "needs_current", "info",
            f"{len(unknown)} rail(s) have copper paths but no known current, so their "
            "temperature rise and drop are not judged: give the rails' or loads' currents in "
            "current= ('3V3=0.5' or 'U5=0.3'), from the datasheets or the user.",
            rails=[{"rail": row["rail"],
                    "highest_resistance_mohm": row.get("highest_resistance_mohm")}
                   for row in listed[:RAILS_LISTED]],
            rails_total=len(unknown), needs=needs[:12]))
    no_volts = [row for row in rows if row.get("status") == "estimated" and
                row.get("voltage_v") is None and row.get("highest_drop_mv")]
    if "needs_voltage" in wanted and no_volts:
        listed = sorted(no_volts, key=lambda row: (-row["highest_drop_mv"],
                                                   natural_key(row["rail"])))
        findings.append(_finding(
            "needs_voltage", "info",
            f"{len(no_volts)} rail(s) have a drop in mV but no known voltage, so the drop is "
            "not judged against drop_info / drop_warning: give their voltages in rails= "
            "('NET=volts').",
            rails=[{"rail": row["rail"], "highest_drop_mv": row["highest_drop_mv"]}
                   for row in listed[:RAILS_LISTED]],
            rails_total=len(no_volts)))
    pour_fed = [row for row in rows if row.get("status") == "pour_fed"]
    through_pour = [row for row in rows if row.get("status") == "estimated" and
                    row.get("loads_without_track_path")]
    if "pour_fed_not_estimated" in wanted and (pour_fed or through_pour):
        said = []
        if pour_fed:
            said.append(f"{len(pour_fed)} rail(s) carry their current mainly in pours, "
                        "regions or planes, which this estimate does not model: their "
                        "resistance and drop are not estimated (Power Analyzer by Keysight "
                        "computes them); the narrowest track is listed with its rise if the "
                        "whole rail current used it")
        if through_pour:
            said.append(f"{'On' if not said else 'on'} {len(through_pour)} estimated "
                        "rail(s) some loads have no track path from the source (reached "
                        "through pour or plane copper, or not routed: check_connectivity), "
                        "so their drop is not estimated")
        findings.append(_finding(
            "pour_fed_not_estimated", "info", "; ".join(said) + ".",
            rails=[{"rail": row["rail"], "pour_share": row["copper"]["pour_share"],
                    **({"planes": row["copper"]["planes"]} if row["copper"].get("planes")
                       else {}),
                    **({"narrowest_mil": row["narrowest"]["width_mil"],
                        **{key: value for key, value in row["narrowest"].items()
                           if key.startswith("rise_at_rail_current")}}
                       if row.get("narrowest") else {})}
                   for row in pour_fed[:RAILS_LISTED]],
            rails_total=len(pour_fed),
            loads_only_through_pour=[{"rail": row["rail"],
                                      "loads": row["loads_without_track_path"]}
                                     for row in through_pour[:RAILS_LISTED]]))
    findings.sort(key=lambda finding: (_SEVERITY_RANK[finding["severity"]],
                                       _CHECK_RANK[finding["check"]],
                                       -(finding.get("ratio") or 0.0)))
    rank = {}
    for number, finding in enumerate(findings):
        rank.setdefault(finding.get("rail"), number)
    status_rank = {"estimated": 0, "power_analyzer": 0, "pour_fed": 1}
    rows.sort(key=lambda row: (rank.get(row["rail"], len(findings)),
                               -(row.get("highest_drop_pct") or 0.0),
                               -((row.get("hottest_track") or {}).get("_rise") or 0.0),
                               row["current"] is None or row["current"]["amps"] is None,
                               status_rank.get(row["status"], 2),
                               -(row.get("highest_resistance_mohm") or 0.0),
                               natural_key(row["rail"])))
    for row in rows:
        for which in ("hottest_track", "hottest_neck"):
            if which in row:
                row[which].pop("_rise", None)

    # --- summary ---
    severity = Counter(finding["severity"] for finding in findings)
    by_check: dict[str, dict] = defaultdict(lambda: defaultdict(int))
    for finding in findings:
        by_check[finding["check"]][finding["severity"]] += 1
    paths_total = sum(row.get("paths_total", 0) for row in rows)
    summary = {
        "rails": {"in_power_tree": len(nodes), "selected": len(selected),
                  "analysed": len(rows), "not_on_pcb": len(not_on_pcb),
                  "by_status": dict(sorted(counts.items()))},
        "currents": {"by_source": dict(sorted(current_sources.items())),
                     "unknown": sum(1 for row in rows if row["current"] is None or
                                    row["current"]["amps"] is None)},
        "paths": {"total": paths_total,
                  "loads_without_track_path": sum(row.get("loads_without_track_path_total", 0)
                                                  for row in rows)},
        "findings": {check: dict(levels) for check, levels in by_check.items()},
        "severity_counts": {level: severity[level] for level in ("warning", "info")},
    }
    if not_on_pcb:
        summary["rails"]["not_on_pcb_names"] = not_on_pcb[:RAILS_LISTED]
    assumptions = {
        "resistivity_ohm_m": float(f"{rho:.4g}"), "temperature_c": temperature_c,
        "tempco_per_c": TEMPCO, "plating_um": plating_um,
        "copper": {board.layer_name(layer): {
            "um": round(board.thickness[layer] * UM_PER_MIL, 1),
            "source": board.thickness_source[layer]} for layer in board.signal},
        "barrel_lengths": "from the stackup" if board.z_known else
        "assumed: a 1.6 mm board (the stack has no heights)",
        "stub_length_mil": round(stub_mil, 2), "pour_share": POUR_SHARE,
        "pad_copper": "0 ohm: track copper inside the pads and vias it ends on is left out",
        "ipc2221": {"formula": "I = k x dT^0.44 x A^0.725 (A in mil^2)",
                    "k_external": K_EXTERNAL, "k_internal": K_INTERNAL,
                    "chart_max_rise_c": CHART_MAX_RISE_C},
        "thresholds": {"rise_info_c": rise_info_c, "rise_warning_c": rise_warning_c,
                       "drop_info_pct": drop_info_pct, "drop_warning_pct": drop_warning_pct},
    }
    notes += currents.unapplied()
    if not current:
        notes.append("No current= given: currents come from a Power Analyzer setup and part "
                     "parameters only. Ask the user (or the datasheets) for the rail or load "
                     "currents and pass current=.")
    if any(source == "assumed 1 oz" for source in board.thickness_source.values()):
        notes.append("The stack gives no copper thickness for some layers: 1 oz (35 um) is "
                     "assumed there (copper_oz= sets it).")
    return {"variant": tree.variant, "summary": summary, "assumptions": assumptions,
            "rails": rows, "findings": findings, "power_analyzer": analyzer or None,
            "notes": list(dict.fromkeys(notes)), "wanted": wanted}


def prepare_power_paths_cached(netlist: dict, plan=None, project_params: dict | None = None,
                               *, index, **kwargs) -> dict:
    """prepare_power_paths cached on the index per netlist, variant, parameters and
    arguments (the last CACHE_SIZE), so paging reuses it: treat the result as read-only."""
    key = repr((plan, project_params, sorted(kwargs.items())))
    cache = getattr(index, "_power_path_cache", None)
    if cache is None:
        cache = index._power_path_cache = {}
    held = cache.get(key)
    if held is not None and held[0] is netlist:
        return held[1]
    prepared = prepare_power_paths(netlist, plan, project_params, index=index, **kwargs)
    if "error" not in prepared:
        if len(cache) >= CACHE_SIZE:
            cache.pop(next(iter(cache)))
        cache[key] = (netlist, prepared)
    return prepared


def render_power_paths(prepared: dict, offset: int = 0, limit: int = PAGE_LIMIT) -> dict:
    """One page: the summary first, then findings (warnings first) and rails paged
    together on one offset."""
    offset = max(0, int(offset or 0))
    page = slice(offset, offset + limit)
    findings, rails = prepared["findings"], prepared["rails"]
    result = {
        "variant": prepared["variant"],
        "summary": prepared["summary"],
        "assumptions": prepared["assumptions"],
        "checks_run": [check for check in CHECKS if check in prepared["wanted"]],
        "estimate": ESTIMATE,
        "sources": SOURCES,
        "findings": findings[page],
        "findings_total": len(findings),
        "rails": rails[page],
        "rails_total": len(rails),
        "offset": offset,
    }
    if prepared.get("power_analyzer"):
        # In full on the first page; later pages only point to it.
        result["power_analyzer"] = (prepared["power_analyzer"] if offset == 0 else
                                    "listed on the first page (offset 0)")
    result["has_more"] = offset + limit < max(len(findings), len(rails))
    if result["has_more"]:
        result["next_offset"] = offset + limit
    result["notes"] = list(prepared["notes"])
    return result


def run_power_paths_check(netlist: dict, plan=None, project_params: dict | None = None, *,
                          index, offset: int = 0, limit: int = PAGE_LIMIT, **kwargs) -> dict:
    """The check in one call: prepare_power_paths and one page of render_power_paths."""
    prepared = prepare_power_paths(netlist, plan, project_params, index=index, **kwargs)
    if "error" in prepared:
        return prepared
    return render_power_paths(prepared, offset, limit)
