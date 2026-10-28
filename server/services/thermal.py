"""Thermal estimate and thermal-via audit (heuristic), offline: which parts dissipate, how
much where it can be computed, their junction temperature for a given thetaJA, and on the
PcbDoc the vias in each exposed or thermal pad and the hot parts that sit close together.

Hot parts: the power tree's converters (linear regulators and LDOs, switching converters
and modules, chargers, load switches, eFuses, hot-swap and ideal-diode controllers), the
MOSFETs a converter's gate pins drive and the diodes on its switch node, transistors and
diodes in series with a rail (reverse-polarity and load-switch FETs, OR-ing diodes),
current-sense shunts, resistors whose computed power (check_derating's V^2/R, or the LED
current get_circuit_calculations computes) reaches 0.25 W or half their rating, LEDs
dissipating 0.2 W or more, processors, FPGAs and Ethernet PHYs (get_firmware_pin_map's and
check_logic_levels' reading), LED, motor and other power drivers and power amplifiers by
their words, and with a PcbDoc any part with an exposed or thermal pad. Each has its basis.

Dissipation, where computable: a linear regulator P = (Vin - Vout) x I_out (+ Vin x Iq
when iq gives it), a switcher P = Vout x I_out x (1/eta - 1) with a given eta, a shunt
P = I^2 x R, a diode P = Vf x I with its stated Vf (a parameter or its text) or a typical
one, a resistor V^2/R (check_derating's net voltages; not a shunt's), an LED Vf x I
(get_circuit_calculations); output currents come only from load
(per designator or per output rail, in amps), never guessed, and a part's power can be
given outright (power). Otherwise the inputs it needs are listed. With thetaJA (theta) the
junction temperature Tj = Ta + thetaJA x P at ta (default 25 C) and at a worst-case ta_max,
against tj_max (default 125 C): above it a warning, above 80% of it info.

On the PcbDoc an exposed or thermal pad is a component's SMD pad (or pads of one name),
or a fill or region its footprint draws on its pads (a PowerPAK's drain paddle), much
larger than its other pads (2.5 times their median area or more, 0.5 mm^2 or more), in
the middle of the footprint (an exposed pad) or at one edge (a DPAK, D2PAK or SOT-223
tab) of an IC, transistor or diode; connectors, switches, crystals, inductors and other
mechanical parts are left out. More than four such pads make a pad array (an LGA module,
a power stage): its large ground pads are audited as one. The vias whose centre lies
inside the pad's copper (rotation and side as placed) and whose span reaches its layer
are counted with their drill, net and the copper they reach: a plane or pour of the pad's
net on another layer of their span (a shelved or unpoured pour holds no copper and is
listed apart). Neighbours are measured edge to edge between the parts' pad bounding boxes
in plan view.

Guidance: Altium's IC thermal management article (thermal vias close to the heat source,
check heating from neighbouring parts; thetaJA x P is the rise, 140 C/W x 1 W = 140 C)
and 'Beyond the PCB review' (a thermal estimate as a final verification step). Everything
here is an estimate: thetaJA depends on the board (copper area, layers, vias, airflow), and
the datasheet and a thermal simulation or measurement decide.
"""

import math
import re
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field

try:
    from services.bom import natural_key
    from services.circuit_calcs import LED_DRIVER_TEXT, VF_TEXT, Board, led_calculations
    from services.connectivity import connectivity
    from services.decoupling import component_pads, designator_prefix, pcb_components, pin_role
    from services.derating import (
        across_voltage, is_shunt, merged_params, net_voltages, policy_or_defaults,
        resistor_limits,
    )
    from services.logic_levels import POWER_DRIVER_TEXT
    from services.pcb_geometry import point_in_polygon, point_seg_distance
    from services.pcb_health import area as polygon_area
    from services.power_tree import SWITCH_KINDS, gate_fets
    from services.pull_resistors import CONNECTOR_PREFIXES, ohms_text, resolve_names
except ImportError:
    from server.services.bom import natural_key
    from server.services.circuit_calcs import LED_DRIVER_TEXT, VF_TEXT, Board, led_calculations
    from server.services.connectivity import connectivity
    from server.services.decoupling import (
        component_pads, designator_prefix, pcb_components, pin_role,
    )
    from server.services.derating import (
        across_voltage, is_shunt, merged_params, net_voltages, policy_or_defaults,
        resistor_limits,
    )
    from server.services.logic_levels import POWER_DRIVER_TEXT
    from server.services.pcb_geometry import point_in_polygon, point_seg_distance
    from server.services.pcb_health import area as polygon_area
    from server.services.power_tree import SWITCH_KINDS, gate_fets
    from server.services.pull_resistors import CONNECTOR_PREFIXES, ohms_text, resolve_names

CHECKS = (
    "tj_over_max", "thermal_pad_no_vias", "thermal_vias_wrong_net", "tj_near_max",
    "thermal_pad_few_vias", "thermal_pad_unconnected", "hot_neighbours", "sensitive_near_hot",
)
PCB_CHECKS = frozenset({"thermal_pad_no_vias", "thermal_vias_wrong_net", "thermal_pad_few_vias",
                        "thermal_pad_unconnected", "hot_neighbours", "sensitive_near_hot"})
_CHECK_RANK = {check: rank for rank, check in enumerate(CHECKS)}
_SEVERITY_RANK = {"warning": 0, "info": 1}
PAGE_LIMIT = 100
NEEDS_LISTED = 12  # designators listed per missing input

MIL_PER_MM = 1 / 0.0254
MM2_PER_MIL2 = 0.0254 ** 2
DEFAULT_MAX_DISTANCE_MIL = 5 * MIL_PER_MM
BESIDE_MIL = 1 * MIL_PER_MM  # a same-net via this close outside the pad is 'beside' it
DEFAULT_TA = 25.0
DEFAULT_TJ_MAX = 125.0
NEAR_TJ_MAX = 0.8  # Tj above this share of tj_max is info
HOT_RESISTOR_W = 0.25  # a resistor dissipating this much is a hot part
HOT_RESISTOR_SHARE = 0.5  # or this share of its power rating (at 50 mW or more)
HOT_RESISTOR_MIN_W = 0.05
HOT_LED_W = 0.2
EP_MIN_RATIO = 2.5  # an exposed pad's area against the median of the other pads
EP_MIN_MM2 = 0.5
EP_PIECE_SHARE = 0.25  # a same-named pad this share of the largest is a piece of it
MAX_PAD_GROUPS = 4  # more large pads than this: a pad array (an LGA module, a power stage)
SCAN_PAD_GROUPS = 16  # large pads looked at per part (equal BGA balls stop here)
VIA_AREA_MM2 = 1.5  # guideline: about one via per this much pad area
VIA_FLOOR_MM2 = 4.0  # and at least VIA_FLOOR vias on pads larger than this
VIA_FLOOR = 4
TYPICAL_VF = {"schottky": 0.45, "diode": 0.8}
DIODE_VF_RANGE = (0.15, 1.6)  # a stated rectifier Vf; a voltage under this in its text is one
THT_LAYER = 74

ESTIMATE = (
    "Everything here is an estimate: thetaJA is the datasheet's figure on its test board "
    "(JEDEC), and the real value depends on this board's copper area, layers, thermal vias "
    "and airflow; dissipation comes from nominal voltages and the loads given. The "
    "datasheet's thermal data and a thermal simulation or measurement decide."
)
GUIDANCE = (
    "Altium's IC thermal management article: thermal vias close to the heat source, under "
    "the exposed pad, into a plane or pour; check heating from neighbouring parts; the rise "
    "is thetaJA x P (140 C/W x 1 W = 140 C). 'Beyond the PCB review': a thermal estimate is a "
    "final verification step."
)
VIA_GUIDELINE = (
    f"a common guideline is about one via per {VIA_AREA_MM2:g} mm^2 of exposed pad and at "
    f"least {VIA_FLOOR} on pads over {VIA_FLOOR_MM2:g} mm^2; the datasheet's land pattern "
    "decides"
)

# Fixed basis labels, in the order they are listed.
BASES = (
    "linear_regulator", "switching_converter", "charger", "power_switch", "switch_fet",
    "converter_diode", "power_path_transistor", "power_path_diode", "shunt_resistor",
    "power_resistor", "power_led", "processor", "fpga", "phy", "led_driver", "power_driver",
    "power_amplifier", "exposed_pad", "given_power", "named",
)
_BASIS_RANK = {basis: rank for rank, basis in enumerate(BASES)}
# Bases that do not say the part dissipates by themselves.
WEAK_BASES = frozenset({"exposed_pad", "named"})
_LINEAR_KINDS = frozenset({"ldo", "linear"})
_TRANSISTOR_EDGES = frozenset({"p_fet", "n_fet", "pnp", "npn", "transistor"})
_DIODE_EDGES = frozenset({"diode", "schottky"})
_GATE_ONLY = re.compile(r"(?:MOSFET|gate|FET|IGBT) drivers?\b", re.I)
_AMPLIFIER_TEXT = re.compile(r"(?:audio|power|speaker|headphone|class[- ]?D|RF) amplifier|"
                             r"\bclass[- ]?D\b|\bPA\b", re.I)
_SCHOTTKY = re.compile(r"schottky", re.I)

# Temperature-sensitive parts (sensitive_near_hot), fixed labels.
_TEMP_SENSOR_TEXT = re.compile(
    r"temp(?:erature)?[- ]?sens|thermistor|\bNTC\b|thermocouple|\bRTD\b|thermometer|"
    r"humidity|hygrometer|\bPT100\b|\bPT1000\b", re.I)
_CRYSTAL_TEXT = re.compile(r"crystal|\bXTAL\b|oscillat|resonator|\bTCXO\b|\bOCXO\b|\bVCXO\b",
                           re.I)
_BATTERY_TEXT = re.compile(r"battery|coin cell|\bLi-?ion\b|\bLiPo\b|\bCR\d{4}\b", re.I)
_CRYSTAL_PREFIXES = frozenset({"Y", "XTAL", "XT", "OSC"})
_ELECTROLYTIC = frozenset({"aluminium_electrolytic", "aluminium_polymer", "supercapacitor"})
# Sensitive parts that belong beside the part they serve: not judged against it.
_PLACED_BY_DESIGN = frozenset({"crystal_oscillator", "voltage_reference", "temperature_sensor"})

# Parts whose large pads are mechanical (shield or hold-down tabs), never thermal pads.
_MECHANICAL_PREFIXES = frozenset({
    *CONNECTOR_PREFIXES, "CN", "JP", "SW", "S", "BTN", "KEY", "PB", "K", "RL", "RLY", "REL",
    "Y", "XTAL", "XT", "OSC", "BT", "BAT", "B", "F", "FU", "FUSE", "PTC", "L", "FB", "FL",
    "LF", "CMC", "BEAD", "T", "TR", "TP", "MH", "H", "HOLE", "FID", "FD", "MP", "SH",
    "SHIELD", "LOGO", "MK", "LS", "SPK", "BZ", "BUZ", "MIC", "ANT", "AE", "E", "SIM", "SD",
    "C", "R", "RN", "LED", "DS",
})
# Netlist kinds whose large pads are not thermal pads.
_MECHANICAL_KINDS = frozenset({"connector", "switch", "relay", "inductive", "choke",
                               "transformer", "fuse", "capacitor", "resistor", "esd"})
_EP_NAME = re.compile(r"EP\d*|EPAD\d*|E-PAD|PAD\d*|TAB\d*|THERMAL\w*|SLUG|EXP\w*|PGND\w*|"
                      r"\w*GND\w*|VSS\w*|0V|DAP|PWRPAD|POWERPAD", re.I)


# ---------- arguments ----------

_ITEM = re.compile(r"\s*([^=:,]+?)\s*[=:]\s*([+-]?\d+(?:\.\d+)?)\s*([^\d\s,][^,]*?)?\s*")
_ONE = re.compile(r"\s*([+-]?\d+(?:\.\d+)?)\s*([^\d\s,][^,]*?)?\s*")
_UNITS = {
    "load": {"": 1.0, "a": 1.0, "ma": 1e-3},
    "current": {"": 1.0, "a": 1.0, "ma": 1e-3, "ua": 1e-6, "µa": 1e-6, "μa": 1e-6},
    "power": {"": 1.0, "w": 1.0, "mw": 1e-3},
    "iq": {"": 1.0, "a": 1.0, "ma": 1e-3, "ua": 1e-6, "µa": 1e-6, "μa": 1e-6},
    "theta": {"": 1.0, "c/w": 1.0, "°c/w": 1.0, "k/w": 1.0, "cw": 1.0},
    "tj_max": {"": 1.0, "c": 1.0, "°c": 1.0},
    "eta": {"": 1.0, "%": 0.01},
}
_LIMITS = {"load": (0.0, 1000.0), "current": (0.0, 1000.0), "power": (0.0, 1000.0),
           "iq": (0.0, 10.0), "theta": (0.0, 1000.0), "tj_max": (40.0, 250.0),
           "eta": (0.05, 1.0)}
_EXAMPLES = {
    "load": "output or part currents in amps per designator or output rail, e.g. "
            "'U5=0.3,3V3=0.5' (or '300mA')",
    "current": "currents in amps per rail or per load designator (a converter's designator "
               "gives its output current; 'U5@3V3' picks the rail of a part on several), e.g. "
               "'VIN=3,3V3=0.5,U5=0.3' (or '300mA')",
    "power": "dissipation in watts per designator, e.g. 'U1=1.2,Q3=0.4' (or '400mW')",
    "iq": "quiescent (ground) currents per regulator designator, e.g. 'U5=0.005' or '5mA'",
    "theta": "thetaJA in C/W per designator, e.g. 'U5=45,Q3=62'",
    "tj_max": "the maximum junction temperature in C per designator, e.g. 'U5=150', or one "
              "for all, e.g. '125'",
    "eta": "switching converters' efficiency per designator, e.g. 'U7=0.88' or 'U7=88%', or "
           "one for all, e.g. '0.85'",
}
_ONE_FOR_ALL = frozenset({"tj_max", "eta"})


def parse_values(text: str | None, arg: str) -> tuple[dict[str, float] | float | None,
                                                       dict | None]:
    """'U5=0.3,3V3=500mA' -> {'U5': 0.3, '3V3': 0.5} in base units (A, W, C/W, C, a 0..1
    efficiency); tj_max and eta also take one value for all. (None, None) when empty, and
    an {"error": "invalid_<arg>"} dict when unreadable or out of range."""
    error = {"error": f"invalid_{arg}", "message": f"{arg} takes {_EXAMPLES[arg]}."}
    if text is None or not str(text).strip():
        return None, None
    units = _UNITS[arg]
    low, high = _LIMITS[arg]

    def value(number: str, unit: str | None) -> float | None:
        key = (unit or "").strip().casefold().replace(" ", "")
        if key not in units:
            return None
        amount = float(number) * units[key]
        if arg == "eta" and not key and 1.0 < amount <= 100.0:
            amount /= 100.0  # 'U7=88': a percentage
        amount = float(f"{amount:.12g}")  # '700mA' is 0.7, not 0.7000000000000001
        if not (low < amount <= high) and not (arg in ("load", "current") and amount == 0.0):
            return None
        return amount

    if arg in _ONE_FOR_ALL and (match := _ONE.fullmatch(str(text))):
        amount = value(match[1], match[2])
        return (amount, None) if amount is not None else (None, error)
    out: dict[str, float] = {}
    for item in str(text).split(","):
        if not item.strip():
            continue
        match = _ITEM.fullmatch(item)
        if not match:
            return None, error
        amount = value(match[2], match[3])
        if amount is None:
            return None, error
        out[match[1].strip()] = amount
    return (out, None) if out else (None, error)


@dataclass
class Inputs:
    """The user's numbers; every lookup is by designator (or rail, for loads), case blind."""
    load: dict[str, float] = field(default_factory=dict)
    power: dict[str, float] = field(default_factory=dict)
    iq: dict[str, float] = field(default_factory=dict)
    theta: dict[str, float] = field(default_factory=dict)
    tj_max: dict[str, float] | float | None = None
    eta: dict[str, float] | float | None = None
    ta: float = DEFAULT_TA
    ta_max: float | None = None

    def get(self, arg: str, key: str) -> float | None:
        values = getattr(self, arg)
        if isinstance(values, (int, float)):
            return float(values)
        folded = {name.casefold(): (name, amount) for name, amount in (values or {}).items()}
        found = folded.get(key.casefold())
        return None if found is None else found[1]

    def unknown(self, parts, nets) -> dict[str, list[str]]:
        """Names given that match no part (and, for load, no net)."""
        known = {name.casefold() for name in parts}
        rails = {name.casefold() for name in nets}
        out = {}
        for arg in ("load", "power", "iq", "theta", "tj_max", "eta"):
            values = getattr(self, arg)
            if isinstance(values, dict):
                names = [name for name in values if name.casefold() not in known and not (
                    arg == "load" and name.casefold() in rails)]
                if names:
                    out[arg] = names
        return out


# ---------- formatting ----------

def _r(value: float | None, digits: int = 4) -> float | None:
    if value is None:
        return None
    return float(f"{value:.{digits}g}")


def _w(watts: float) -> str:
    return f"{watts * 1000:.3g} mW" if abs(watts) < 1 else f"{watts:.3g} W"


def _c(celsius: float) -> str:
    return f"{celsius:.0f} C" if abs(celsius) >= 10 else f"{celsius:.1f} C"


def _names(items, limit: int = 6) -> str:
    items = list(dict.fromkeys(items))
    shown = ", ".join(items[:limit])
    return shown + (f" and {len(items) - limit} more" if len(items) > limit else "")


def _distance_text(mil: float, units: str) -> str:
    return f"{mil:.0f} mil" if units == "mil" else f"{mil / MIL_PER_MM:.1f} mm"


def _finding(check: str, severity: str, issue: str, designators, **fields) -> dict:
    out: dict = {"check": check, "severity": severity,
                 "designators": sorted(set(designators), key=natural_key)}
    out.update({key: value for key, value in fields.items() if value not in (None, [], {})})
    out["issue"] = issue
    return out


# ---------- the design ----------

@dataclass
class HotPart:
    ref: str
    bases: dict[str, str] = field(default_factory=dict)  # basis -> detail
    kind: str | None = None  # the converter's type, the chip's class, the part's kind
    # Parts placed together by design (a converter with its FETs, diodes and shunts, the
    # FETs of one power path): pairs sharing a group are not neighbour findings.
    groups: set[str] = field(default_factory=set)
    power: dict = field(default_factory=dict)
    watts: float | None = None
    tj: dict | None = None
    pads: list = field(default_factory=list)  # _ThermalPad
    neighbours: list[dict] = field(default_factory=list)
    sensitive: list[dict] = field(default_factory=list)

    def add(self, basis: str, detail: str) -> None:
        self.bases.setdefault(basis, detail)

    @property
    def known_hot(self) -> bool:
        return any(basis not in WEAK_BASES for basis in self.bases)

    def labels(self) -> list[str]:
        return sorted(self.bases, key=_BASIS_RANK.get)


class _Design:
    """The schematic read once: circuit_calcs' Board (power tree, parts, voltages, chips,
    processors) and the net voltages with divider midpoints (check_derating's)."""

    def __init__(self, netlist: dict, plan, project_params, rails, vref, root_dir,
                 policy_setting: str | None = None):
        # The resistor ratings: check_derating's policy layers (the Derating policy
        # setting's file, then the project's derating_policy.json).
        policy, notes = policy_or_defaults(root_dir, policy_setting,
                                           "the default resistor ratings")
        self.notes: list[str] = notes
        self.policy = policy["values"]
        self.board = Board(netlist, plan, project_params, rails, vref, self.policy)
        self.tree = self.board.tree
        self.voltages, _ = net_voltages(self.tree, self.board.data, rails, self.board.parts)
        self.switch_nodes = {net for conv in self.tree.converters.values()
                             for net in conv.switch_nodes}
        # The power tree's supply nets: converter inputs and outputs and the rails series
        # parts join (a shared supply says nothing about where a part is placed).
        self.supply_nets = {net for conv in self.tree.converters.values()
                            for net in [*conv.inputs, *(out.net for out in conv.outputs)]
                            } | {net for edge in self.tree.edges for net in (edge.src, edge.dst)}
        self.supply_nets.discard(None)
        self.parts = {part.ref: part for part in self.board.parts}

    def volt(self, net: str | None) -> dict | None:
        found = self.voltages.get(net) if net else None
        if found is None:
            return None
        return {"net": net, "volts": found.volts, "source": found.source,
                "confidence": found.confidence}


def _recognise(design: _Design) -> dict[str, HotPart]:
    tree = design.tree
    hot: dict[str, HotPart] = {}

    def part(ref: str) -> HotPart:
        if ref not in hot:
            hot[ref] = HotPart(ref)
        return hot[ref]

    for ref, conv in tree.converters.items():
        if conv.kind in _LINEAR_KINDS:
            basis = "linear_regulator"
        elif conv.kind == "charger":
            basis = "charger"
        elif conv.kind in SWITCH_KINDS:
            basis = "power_switch"
        elif conv.kind == "pmic" and not conv.switching:
            basis = "linear_regulator"
        else:
            basis = "switching_converter"
        entry = part(ref)
        entry.add(basis, f"power tree: {conv.kind.replace('_', ' ')}"
                  + (" module" if conv.module else "")
                  + (" controller" if conv.controller else ""))
        entry.kind = conv.kind
        entry.groups.add(ref)
        fets = gate_fets(tree, conv) if conv.pins.get("gate") else {}
        for fet in sorted(fets, key=natural_key):
            if fet in tree.converters:
                continue
            member = part(fet)
            if conv.switching:
                member.add("switch_fet", f"its gate is driven by {ref}")
            else:
                member.add("power_path_transistor", f"the pass FET whose gate {ref} drives")
            member.kind = member.kind or "transistor"
            member.groups.add(ref)
        for net in sorted(conv.switch_nodes, key=natural_key):
            for diode, _, _ in tree.diodes.get(net, ()):
                if tree.kind(diode) == "esd":
                    continue
                member = part(diode)
                member.add("converter_diode", f"on {ref}'s switch node {net}")
                member.kind = member.kind or "diode"
                member.groups.add(ref)
    for edge in tree.edges:
        if edge.converter or edge.part in tree.converters:
            continue
        if edge.kind in _TRANSISTOR_EDGES:
            entry = part(edge.part)
            entry.add("power_path_transistor",
                      f"in series between {edge.src} and {edge.dst} ({edge.kind.replace('_', '-')})")
            entry.kind = entry.kind or "transistor"
        elif edge.kind in _DIODE_EDGES:
            entry = part(edge.part)
            entry.add("power_path_diode", f"in series between {edge.src} and {edge.dst}")
            entry.kind = entry.kind or edge.kind
    processors = {ref: candidate for ref, candidate in design.board.processors.items()
                  if candidate.kind in ("mcu", "fpga", "radio", "module")}
    for ref, candidate in processors.items():
        entry = part(ref)
        basis = "fpga" if candidate.kind == "fpga" else "processor"
        entry.add(basis, f"get_firmware_pin_map reads it as {candidate.kind}")
        entry.kind = entry.kind or candidate.kind
    for ref, chip in design.board.chips.items():
        if ref in tree.converters:
            continue
        text = tree.text(ref)
        if chip.cls in ("processor", "fpga", "phy") and ref not in processors:
            entry = part(ref)
            entry.add(chip.cls, f"check_logic_levels reads it as {chip.cls}")
            entry.kind = entry.kind or chip.cls
        elif LED_DRIVER_TEXT.search(text) and chip.cls in ("driver", "peripheral", "power"):
            entry = part(ref)
            entry.add("led_driver", "LED driver words in its text")
            entry.kind = entry.kind or "led_driver"
        elif chip.cls == "driver" and POWER_DRIVER_TEXT.search(text) and not (
                _GATE_ONLY.search(text) and not re.search(r"bridge|motor|stage|DrMOS", text,
                                                          re.I)):
            entry = part(ref)
            entry.add("power_driver", "motor, bridge or power-stage words in its text")
            entry.kind = entry.kind or "driver"
        elif chip.cls in ("amplifier", "peripheral") and _AMPLIFIER_TEXT.search(text):
            entry = part(ref)
            entry.add("power_amplifier", "power or audio amplifier words in its text")
            entry.kind = entry.kind or "amplifier"
    return hot


def nets_of(tree, ref: str) -> set[str]:
    """A part's nets, grounds left out."""
    return set((tree.design.part_pins.get(ref) or {}).values()) - tree.grounds


def _flow(tree, ref: str) -> tuple[set[str], set[str]]:
    """(upstream, downstream) nets of a part's power-tree edges: directed ones as they
    are, the others from the nearer source (get_power_tree's depth); a net whose side is
    not known is in neither."""
    up: set[str] = set()
    down: set[str] = set()
    for edge in tree.edges:
        if edge.part != ref or edge.converter:
            continue
        src, dst = edge.src, edge.dst
        if not edge.directed:
            first, second = tree.depth.get(src), tree.depth.get(dst)
            if first is None or second is None or first == second:
                continue
            if first > second:
                src, dst = dst, src
        up.add(src)
        down.add(dst)
    return up, down


def _stages(design: _Design, hot: dict[str, HotPart]) -> None:
    """Power-path transistors and diodes and current-sense shunts join the group of each
    converter, power switch or LED or power driver whose pins share a net with them, and
    of every such part they share a net with (back-to-back or parallel FETs, an OR-ing
    pair, a shunt in series with a FET). A converter's input (a driver's supply rail)
    joins only a part that feeds it: a part drawing from the same supply sits on a shared
    bus, not in its stage; with the direction unknown, only an input no other converter
    takes."""
    tree = design.tree
    on_net: dict[str, set[str]] = defaultdict(set)
    on_input: dict[str, set[str]] = defaultdict(set)
    takers = Counter(net for conv in tree.converters.values() for net in set(conv.inputs))
    stages = {"linear_regulator", "switching_converter", "charger", "power_switch",
              "led_driver", "power_driver"}
    for ref, entry in hot.items():
        if entry.bases.keys() & stages:
            entry.groups.add(ref)
            conv = tree.converters.get(ref)
            supplies = set(conv.inputs) if conv else nets_of(tree, ref) & tree.rails
            for net in nets_of(tree, ref):
                (on_input if net in supplies else on_net)[net].add(ref)
    path = sorted((ref for ref, entry in hot.items() if entry.bases.keys() & {
        "power_path_transistor", "power_path_diode", "shunt_resistor"}), key=natural_key)
    parent = {ref: ref for ref in path}

    def root(ref: str) -> str:
        while parent[ref] != ref:
            parent[ref] = parent[parent[ref]]
            ref = parent[ref]
        return ref

    by_net: dict[str, list[str]] = defaultdict(list)
    for ref in path:
        for net in nets_of(tree, ref):
            by_net[net].append(ref)
    for members in by_net.values():
        for other in members[1:]:
            parent[root(other)] = root(members[0])
    for ref in path:
        hot[ref].groups.add(f"path:{root(ref)}")
        up, down = _flow(tree, ref)
        for net in nets_of(tree, ref):
            hot[ref].groups |= on_net.get(net, set())
            if net in down or (net not in up and takers[net] <= 1):
                hot[ref].groups |= on_input.get(net, set())


def _resistor_power(design: _Design, led_powers: dict[str, tuple[float, str]]
                    ) -> dict[str, dict]:
    """Each fitted resistor's computed power: V^2/R over its sections (check_derating's
    voltages), else at an LED's computed current; with its rating. Not a shunt's: the
    millivolts across it are below what nominal net voltages resolve (I^2 x R from load=,
    as check_derating asks)."""
    out: dict[str, dict] = {}
    for ref, part in design.parts.items():
        if part.kind != "resistor":
            continue
        rating, _ = resistor_limits(part, design.policy)
        row: dict = {"ohms": part.ohms, "value": part.value,
                     "rating_w": rating.value if rating else None,
                     "rating_source": rating.describe() if rating else None,
                     "shunt": is_shunt(part, design.tree.text(ref))}
        total, bases = 0.0, []
        for net_a, net_b in () if row["shunt"] else part.sections:
            across = across_voltage(net_a, net_b, design.voltages, design.switch_nodes)
            if across.volts is None or not part.ohms or part.ohms <= 0:
                total = None
                break
            total += across.volts ** 2 / part.ohms
            bases.append(across.basis)
        if total is not None and total > 0:
            row.update(watts=total, formula="P = V^2 / R", basis="; ".join(filter(None, bases)))
        elif ref in led_powers:
            watts, led = led_powers[ref]
            row.update(watts=watts, formula="P = I^2 x R at the LED current",
                       basis=f"get_circuit_calculations' current through {led}")
        out[ref] = row
    return out


# ---------- dissipation ----------

def _load_for(inputs: Inputs, ref: str, rail: str | None) -> tuple[float | None, str | None]:
    amps = inputs.get("load", ref)
    if amps is not None:
        return amps, f"load argument ({ref})"
    if rail:
        amps = inputs.get("load", rail)
        if amps is not None:
            return amps, f"load argument (rail {rail})"
    return None, None


def _converter_power(design: _Design, inputs: Inputs, entry: HotPart) -> dict:
    tree = design.tree
    conv = tree.converters[entry.ref]
    vin = design.volt(conv.inputs[0]) if conv.inputs else None
    linear_kind = conv.kind in _LINEAR_KINDS or (conv.kind == "charger" and not conv.switching)
    if conv.kind in SWITCH_KINDS:
        return {"status": "needs_inputs", "formula": "P = I^2 x R_on",
                "needs": [f"power: the switch's dissipation (I^2 x R_on, R_on from the "
                          f"datasheet), pass power='{entry.ref}=watts'"]}
    outputs = conv.outputs or []
    if not outputs:
        return {"status": "needs_inputs",
                "needs": [f"power: its output rail was not read, pass power="
                          f"'{entry.ref}=watts'"]}
    eta = inputs.get("eta", entry.ref)
    iq = inputs.get("iq", entry.ref)
    rows, needs, total, counted = [], [], 0.0, 0
    for out in outputs:
        linear = linear_kind or out.kind == "ldo"
        vout = design.volt(out.net)
        if len(outputs) == 1:
            amps, source = _load_for(inputs, entry.ref, out.net)
        else:
            amps = inputs.get("load", out.net)
            source = f"load argument (rail {out.net})" if amps is not None else None
        row: dict = {"output": out.net, "type": "linear" if linear else "switching"}
        missing = []
        if amps is None:
            missing.append(f"load: I_out of {out.net}, pass load='{entry.ref}=amps' or "
                           f"load='{out.net}=amps'" if len(outputs) == 1 else
                           f"load: I_out of {out.net}, pass load='{out.net}=amps'")
        if vout is None:
            missing.append(f"rails: Vout ({out.net}), pass rails='{out.net}=volts'")
        if linear and vin is None:
            name = conv.inputs[0] if conv.inputs else "NET"
            missing.append(f"rails: Vin ({name}), pass rails='{name}=volts'")
        if not linear and eta is None:
            missing.append(f"eta: the datasheet's efficiency at this load, pass "
                           f"eta='{entry.ref}=0.85'")
        row["inputs"] = {key: value for key, value in (
            ("Vin", vin if linear else None), ("Vout", vout),
            ("I_out", {"amps": amps, "source": source} if amps is not None else None),
            ("eta", {"value": eta, "source": "eta argument"} if not linear and eta is not None
             else None)) if value is not None}
        if missing:
            row["needs"] = missing
            needs += missing
            rows.append(row)
            continue
        if linear:
            dropout = vin["volts"] - vout["volts"]
            watts = max(0.0, dropout) * amps
            row["formula"] = "P = (Vin - Vout) x I_out"
            if dropout < 0:
                row["note"] = "Vout above Vin at the nominal voltages: check the rails"
        else:
            watts = abs(vout["volts"]) * amps * (1.0 / eta - 1.0)
            row["formula"] = "P = Vout x I_out x (1/eta - 1)"
        row["watts"] = _r(watts)
        total += watts
        counted += 1
        rows.append(row)
    result: dict = {}
    if counted and iq is not None and vin is not None:
        total += vin["volts"] * iq
        result["iq"] = {"amps": _r(iq), "source": "iq argument", "watts": _r(vin["volts"] * iq)}
    elif counted and iq is not None:
        name = conv.inputs[0] if conv.inputs else "NET"
        needs.append(f"rails: Vin ({name}) for the Vin x Iq term, pass rails='{name}=volts'")
    if len(rows) == 1:
        single = rows[0]
        result.update({key: single[key] for key in ("formula", "inputs", "note")
                       if key in single})
        if "iq" in result and "formula" in result:
            result["formula"] += " + Vin x Iq"
    else:
        result["outputs"] = rows
        result["formula"] = "sum over the outputs with a load given"
    if counted:
        result["status"] = "computed"
        result["watts"] = _r(total)
        if counted < len(rows):
            result["partial"] = f"{counted} of {len(rows)} outputs counted"
        if needs:
            result["needs"] = needs
        if not linear_kind:
            result["note"] = ("the converter's total loss: with external FETs, diodes or an "
                              "inductor, part of it is dissipated in them")
    else:
        result["status"] = "needs_inputs"
        result["needs"] = needs
    return result


_VOLTS = re.compile(r"(?<![\w.,])(\d*[.,]?\d+)\s?V(?![\w/])")


def _diode_vf(design: _Design, ref: str) -> tuple[float, str] | None:
    """(Vf, source) a rectifier states: a forward-voltage parameter, 'Vf 0.55V' in its
    text, else the one voltage in its text below DIODE_VF_RANGE's top (no rectifier is
    rated that low in reverse)."""
    low, high = DIODE_VF_RANGE
    meta = (design.board.netlist.get("components") or {}).get(ref) or {}
    for name, (raw_name, raw) in merged_params(meta, design.board.plan, ref).items():
        if "forward voltage" in name or name in ("vf", "v f") or name.startswith("vf "):
            match = re.search(r"\d*[.,]?\d+", raw)
            volts = float(match[0].replace(",", ".")) if match else None
            if volts is not None and low <= volts <= high:
                return volts, f"parameter ({raw_name})"
    text = design.tree.text(ref)
    if (match := VF_TEXT.search(text)) and low <= float(match[1].replace(",", ".")) <= high:
        return float(match[1].replace(",", ".")), "text (Vf)"
    found = {float(match[1].replace(",", ".")) for match in _VOLTS.finditer(text)}
    found = {volts for volts in found if low <= volts <= high}
    if len(found) == 1:
        return found.pop(), "text (a voltage this low on a rectifier: read as its Vf)"
    return None


def _estimate(design: _Design, inputs: Inputs, entry: HotPart, resistors: dict[str, dict],
              leds: dict[str, dict]) -> dict:
    ref = entry.ref
    given = inputs.get("power", ref)
    if given is not None:
        return {"status": "given", "watts": given, "source": "power argument"}
    tree = design.tree
    if ref in tree.converters:
        return _converter_power(design, inputs, entry)
    if "shunt_resistor" in entry.bases or "power_resistor" in entry.bases:
        info = resistors.get(ref) or {}
        if info.get("watts") is not None:
            return {"status": "computed", "watts": _r(info["watts"]),
                    "formula": info["formula"], "basis": info.get("basis"),
                    "rating_w": info.get("rating_w"), "rating_source": info.get("rating_source")}
        amps = inputs.get("load", ref)
        ohms = info.get("ohms")
        if amps is not None and ohms:
            return {"status": "computed", "watts": _r(amps * amps * ohms),
                    "formula": "P = I^2 x R",
                    "inputs": {"I": {"amps": amps, "source": f"load argument ({ref})"},
                               "R": {"ohms": ohms, "value": ohms_text(ohms)}},
                    "rating_w": info.get("rating_w"), "rating_source": info.get("rating_source")}
        need = f"load: the current through it, pass load='{ref}=amps'"
        if not ohms:
            need += " (its resistance was not read: or pass power=)"
        return {"status": "needs_inputs", "formula": "P = I^2 x R", "needs": [need]}
    if "power_led" in entry.bases and ref in leds:
        return leds[ref]
    if entry.bases.keys() & {"converter_diode", "power_path_diode"}:
        amps = inputs.get("load", ref)
        family = "schottky" if _SCHOTTKY.search(tree.text(ref)) else "diode"
        if amps is not None:
            stated = _diode_vf(design, ref)
            if stated:
                vf, source = stated
                volt = {"volts": vf, "source": f"stated: {source}"}
                note = ("its stated Vf (at the datasheet's test current) at its average "
                        "current; the datasheet's Vf at this current and temperature decides")
            else:
                vf = TYPICAL_VF[family]
                volt = {"volts": vf, "source": f"typical {family}", "typical": True}
                note = ("a typical Vf at its average current; the datasheet's Vf at this "
                        "current and temperature decides")
            return {"status": "computed", "watts": _r(vf * amps), "formula": "P = Vf x I",
                    "inputs": {"Vf": volt,
                               "I": {"amps": amps, "source": f"load argument ({ref})"}},
                    "note": note}
        return {"status": "needs_inputs", "formula": "P = Vf x I_avg",
                "needs": [f"load: its average current, pass load='{ref}=amps' (or "
                          f"power='{ref}=watts')"]}
    if entry.bases.keys() & {"switch_fet", "power_path_transistor"}:
        return {"status": "needs_inputs", "formula": "P = I^2 x R_ds(on) (+ switching loss)",
                "needs": [f"power: its conduction (I^2 x R_ds(on)) and switching loss from "
                          f"the datasheet, pass power='{ref}=watts'"]}
    return {"status": "needs_inputs",
            "needs": [f"power: its dissipation from the datasheet (typical supply current "
                      f"x voltage, or the power figure), pass power='{ref}=watts'"]}


def _tj(inputs: Inputs, entry: HotPart) -> dict | None:
    if entry.watts is None:
        return None
    theta = inputs.get("theta", entry.ref)
    limit = inputs.get("tj_max", entry.ref)
    limit_source = "tj_max argument" if limit is not None else "default"
    limit = limit if limit is not None else DEFAULT_TJ_MAX
    if theta is None:
        return {"status": "needs_theta",
                "needs": [f"theta: the datasheet's thetaJA (junction to ambient, for the "
                          f"package on a board like this one), pass theta='{entry.ref}=C/W'"]}
    rise = theta * entry.watts
    out: dict = {"theta_ja_c_per_w": theta, "watts": _r(entry.watts), "rise_c": _r(rise, 3),
                 "ta_c": inputs.ta, "tj_c": _r(inputs.ta + rise, 4),
                 "formula": "Tj = Ta + thetaJA x P"}
    worst = inputs.ta + rise
    if inputs.ta_max is not None:
        worst = inputs.ta_max + rise
        out["ta_max_c"] = inputs.ta_max
        out["tj_worst_c"] = _r(worst, 4)
    out["tj_max_c"] = limit
    out["tj_max_source"] = limit_source
    out["margin_c"] = _r(limit - worst, 4)
    out["status"] = ("over_max" if worst > limit else
                     "near_max" if worst > NEAR_TJ_MAX * limit else "ok")
    return out


# ---------- the PcbDoc: exposed and thermal pads ----------

def pad_area_mil2(pad) -> float:
    """A pad's copper area (its main copper): a rectangle, a rounded rectangle, a round or
    oblong (capsule) pad, an octagon (corners cut by a quarter of the shorter side)."""
    width = pad["width"] or 0.0
    height = pad["height"] or width
    shape = pad["shape"]
    minor, major = min(width, height), max(width, height)
    if shape == 1:
        radius = minor / 2.0
        return math.pi * radius * radius + (major - minor) * minor
    if shape == 9:
        try:
            corner = pad["corner_radius"] or 0.0
        except (IndexError, KeyError):
            corner = 0.0
        corner = max(0.0, min(corner, minor / 2.0))
        return width * height - (4.0 - math.pi) * corner * corner
    if shape == 3:
        cut = minor / 4.0
        return width * height - 2.0 * cut * cut
    return width * height


def _is_area(piece) -> bool:
    """A footprint fill's or region's piece (a dict with its outline), not a pad row."""
    return isinstance(piece, dict) and "outline" in piece


def _ring_distance(x: float, y: float, ring) -> float:
    return min(point_seg_distance(x, y, x1, y1, x2, y2)
               for (x1, y1), (x2, y2) in zip(ring, [*ring[1:], ring[0]]))


def footprint_areas(index) -> dict[int, list[dict]]:
    """The fills and regions a footprint draws on a copper layer, by PCB component id, as
    thermal-pad pieces: net, layer, outline less holes, area, centroid and bounds."""
    copper = set(index._copper_layers_in_stack_order())
    nets = index.pcb.nets
    out: dict[int, list[dict]] = defaultdict(list)
    for number, area in enumerate(getattr(index.pcb, "areas", None) or []):
        if area.component is None or area.layer not in copper or len(area.outline) < 3:
            continue
        outline = list(area.outline)
        holes = [list(hole) for hole in area.holes if len(hole) >= 3]
        size = polygon_area(outline) - sum(polygon_area(hole) for hole in holes)
        if size <= 0:
            continue
        # The outline's centroid, from the shoelace terms.
        cross = [(x1 * y2 - x2 * y1, x1 + x2, y1 + y2)
                 for (x1, y1), (x2, y2) in zip(outline, [*outline[1:], outline[0]])]
        signed = sum(item[0] for item in cross) / 2.0
        xs = [point[0] for point in outline]
        ys = [point[1] for point in outline]
        if abs(signed) > 1e-9:
            cx = sum(item[0] * item[1] for item in cross) / (6.0 * signed)
            cy = sum(item[0] * item[2] for item in cross) / (6.0 * signed)
        else:
            cx, cy = (min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0
        out[area.component].append({
            "id": f"{area.kind}:{number}", "kind": area.kind, "layer": area.layer,
            "net_id": area.net,
            "net_name": nets[area.net] if area.net is not None and 0 <= area.net < len(nets)
            else None,
            "pad_name": None, "hole": None, "x1": cx, "y1": cy, "minx": min(xs),
            "miny": min(ys), "maxx": max(xs), "maxy": max(ys), "outline": outline,
            "holes": holes, "area": size})
    return out


@dataclass
class ThermalPad:
    component: int
    pieces: list  # pad rows, or a footprint fill's or region's piece (_is_area)
    position: str  # centre or tab
    ratio: float
    area_mil2: float
    name_hint: bool
    over: list[str] = field(default_factory=list)  # the part's pads on a fill or region
    array: dict | None = None  # a pad array: its large pads, and those audited as this one
    vias: list = field(default_factory=list)
    holes: list = field(default_factory=list)  # the footprint's own plated holes in it
    beside: int = 0
    wrong: list = field(default_factory=list)
    reach: list[str] = field(default_factory=list)
    # Layers where its vias land only in a pour of its net that holds no copper in the
    # file (shelved, or never poured).
    reach_empty: list[str] = field(default_factory=list)
    reach_checked: bool = False
    own_pour: bool = False

    @property
    def net(self) -> str | None:
        return self.pieces[0]["net_name"]

    @property
    def net_id(self) -> int | None:
        return self.pieces[0]["net_id"]

    @property
    def layer(self) -> int:
        return self.pieces[0]["layer"]

    @property
    def names(self) -> list[str]:
        names = {piece["pad_name"] or "" for piece in self.pieces if not _is_area(piece)}
        return sorted(names | set(self.over), key=natural_key)

    @property
    def drawn_as(self) -> str | None:
        """'fill' or 'region' when the footprint draws it so, else None (pads)."""
        return "/".join(sorted({piece["kind"] for piece in self.pieces if _is_area(piece)})
                        ) or None

    @property
    def area_mm2(self) -> float:
        return self.area_mil2 * MM2_PER_MIL2

    @property
    def count(self) -> int:
        return len(self.vias) + len(self.holes)

    def needed(self) -> int:
        area = self.area_mm2
        need = math.ceil(area / VIA_AREA_MM2 - 1e-9)
        return max(need, VIA_FLOOR if area > VIA_FLOOR_MM2 else 0)

    def describe(self) -> str:
        """Its pads' names, or the footprint fill or region and the pads on it."""
        names = _names(self.names, 3)
        if self.drawn_as:
            return f"a footprint {self.drawn_as}" + (f" under pads {names}" if names else "")
        return names


def _piece_distance(index, x: float, y: float, piece) -> float:
    """From a point to a piece's copper, 0 inside: a pad's (the index's geometry), or a
    fill's or region's outline less its holes."""
    if not _is_area(piece):
        return index._point_primitive_distance(x, y, piece)
    if not point_in_polygon(x, y, piece["outline"]):
        return _ring_distance(x, y, piece["outline"])
    for hole in piece["holes"]:
        if point_in_polygon(x, y, hole):
            return _ring_distance(x, y, hole)
    return 0.0


def _inside(index, x: float, y: float, pieces) -> bool:
    return any(piece["minx"] - 1e-6 <= x <= piece["maxx"] + 1e-6
               and piece["miny"] - 1e-6 <= y <= piece["maxy"] + 1e-6
               and _piece_distance(index, x, y, piece) <= 1e-6 for piece in pieces)


def _make_pad(component: int, group: list, others: list, ratio: float, total: float,
              over=()) -> ThermalPad:
    """position: 'centre' when the group's centre lies inside the box of the other pads'
    centres, else 'tab'."""
    xs = [pad["x1"] for pad in others]
    ys = [pad["y1"] for pad in others]
    cx = sum(piece["x1"] for piece in group) / len(group)
    cy = sum(piece["y1"] for piece in group) / len(group)
    centre = min(xs) < cx < max(xs) and min(ys) < cy < max(ys)
    names = [piece["pad_name"] or "" for piece in group if not _is_area(piece)]
    numbers = [int(pad["pad_name"]) for pad in others if (pad["pad_name"] or "").isdigit()]
    hint = any(_EP_NAME.fullmatch(name) for name in names) or bool(
        numbers and names and names[0].isdigit() and int(names[0]) > max(numbers))
    return ThermalPad(component, group, "centre" if centre else "tab", ratio, total, hint,
                      over=sorted(set(over), key=natural_key))


def find_thermal_pads(index, component: int, pads: list, excluded: bool = False,
                      areas: list | None = None, grounds=frozenset()) -> list[ThermalPad]:
    """A component's exposed or thermal pads: its largest SMD pad (with the same-named pads
    of at least a quarter of its area: a split exposed pad), or a fill or region its
    footprint draws on its pads' layer and on one of its nets or none (a PowerPAK's drain
    paddle: areas, from footprint_areas), when it is EP_MIN_RATIO times the median area of
    the other pads or more and EP_MIN_MM2 or larger; then the next such pads of other
    names (dual pads). More than MAX_PAD_GROUPS of them make a pad array (an LGA module, a
    power stage): its large pads on a ground net (grounds), else on the largest one's net,
    are audited as one pad whose array says how many large pads there are. position is
    'centre' when the pad's centre lies inside the box of the other pads' centres, else
    'tab'. excluded: a mechanical part's (a connector's hold-down tabs), never thermal."""
    if excluded or len(pads) < 3:
        return []
    smd = [pad for pad in pads if not (pad["hole"] or 0) > 0 and pad["layer"] != THT_LAYER]
    if not smd:
        return []
    sizes = {pad["id"]: pad_area_mil2(pad) for pad in pads}
    layer = Counter(pad["layer"] for pad in smd).most_common(1)[0][0]
    nets = {pad["net_id"] for pad in pads}
    drawn = [piece for piece in areas or () if piece["layer"] == layer
             and (piece["net_id"] is None or piece["net_id"] in nets)]
    sizes.update((piece["id"], piece["area"]) for piece in drawn)
    largest = max(sizes[item["id"]] for item in [*smd, *drawn])
    groups: list[list] = [[piece] for piece in drawn if sizes[piece["id"]] >= 0.5 * largest]
    taken: set = set()
    for pad in sorted(smd, key=lambda item: -sizes[item["id"]]):
        if sizes[pad["id"]] < 0.5 * largest or len(groups) >= SCAN_PAD_GROUPS:
            break
        if pad["id"] in taken or any(_inside(index, pad["x1"], pad["y1"], group)
                                     for group in groups if _is_area(group[0])):
            continue  # a pad on a drawn paddle is part of it
        name = pad["pad_name"]
        group = [pad] + [other for other in smd if other["id"] != pad["id"]
                         and other["id"] not in taken and name and other["pad_name"] == name
                         and sizes[other["id"]] >= EP_PIECE_SHARE * sizes[pad["id"]]]
        groups.append(group)
        taken |= {item["id"] for item in group}
    groups.sort(key=lambda group: -sizes[group[0]["id"]])
    found: list[ThermalPad] = []
    for group in groups:
        # The other pads: every pad not of the group nor on it (equal pads stay others:
        # eight equal pads and a marker are no pad array).
        ids = {item["id"] for item in group}
        others, over = [], []
        for pad in pads:
            if pad["id"] in ids:
                continue
            (over if _inside(index, pad["x1"], pad["y1"], group) else others).append(pad)
        if not others:
            continue
        median = statistics.median(sizes[pad["id"]] for pad in others)
        total = sum(sizes[item["id"]] for item in group)
        ratio = sizes[group[0]["id"]] / median if median > 0 else math.inf
        if ratio < EP_MIN_RATIO or total * MM2_PER_MIL2 < EP_MIN_MM2:
            continue
        found.append(_make_pad(component, group, others, ratio, total,
                               [pad["pad_name"] or "" for pad in over if _is_area(group[0])]))
    if len(found) <= MAX_PAD_GROUPS:
        return found
    keep = [pad for pad in found if pad.net in grounds] or [
        pad for pad in found if pad.net_id == found[0].net_id]
    pieces = [piece for pad in keep for piece in pad.pieces]
    ids = {piece["id"] for piece in pieces}
    others = [pad for pad in pads if pad["id"] not in ids
              and not _inside(index, pad["x1"], pad["y1"], pieces)]
    others = others or [pad for pad in pads if pad["id"] not in ids]
    merged = _make_pad(component, pieces, others,
                       max(pad.ratio for pad in keep), sum(pad.area_mil2 for pad in keep),
                       [name for pad in keep for name in pad.over])
    merged.array = {"large_pads": len(found), "audited": len(keep),
                    "audited_net": "ground" if keep[0].net in grounds else
                    "the largest pad's net", "not_audited": len(found) - len(keep)}
    return [merged]


def _hatched_pours(index) -> set[int]:
    """The pours whose copper is hatch tracks (an outline stands for them), cached."""
    cached = index.__dict__.get("_thermal_hatched")
    if cached is None:
        cached = {row[0] for row in index.db.execute(
            "SELECT DISTINCT poured_by FROM prims WHERE poured_by IS NOT NULL")}
        index.__dict__["_thermal_hatched"] = cached
    return cached


def _audit_pad(index, pad: ThermalPad, own_pads: list) -> None:
    """The vias whose centre lies in the pad (and whose span reaches its layer), the
    footprint's plated holes in it, same-net vias just beside it, and the copper of its
    net they reach on other layers: check_connectivity's area copper (poured regions,
    fills, planes) and hatched pours' outlines; a pour of its net that holds no copper
    (shelved, or never poured) is listed apart, not as reached."""
    minx = min(piece["minx"] for piece in pad.pieces) - BESIDE_MIL
    maxx = max(piece["maxx"] for piece in pad.pieces) + BESIDE_MIL
    miny = min(piece["miny"] for piece in pad.pieces) - BESIDE_MIL
    maxy = max(piece["maxy"] for piece in pad.pieces) + BESIDE_MIL
    rows = index.db.execute(
        "SELECT p.*, n.name AS net_name FROM prims p LEFT JOIN nets n ON n.id = p.net_id "
        "WHERE p.kind = 'via' AND p.x1 BETWEEN ? AND ? AND p.y1 BETWEEN ? AND ?",
        (minx, maxx, miny, maxy)).fetchall()
    stack = index._copper_layers_in_stack_order()
    for via in rows:
        if via["layer"] in stack and via["layer_to"] in stack and \
                pad.layer not in index._primitive_layers(via):
            continue  # a blind or buried via that does not reach the pad's layer
        distance = min(_piece_distance(index, via["x1"], via["y1"], piece)
                       for piece in pad.pieces)
        if distance <= 1e-6:
            pad.vias.append(via)
            if via["net_id"] != pad.net_id:
                pad.wrong.append(via)
        elif distance <= BESIDE_MIL and pad.net_id is not None and \
                via["net_id"] == pad.net_id:
            pad.beside += 1
    piece_ids = {piece["id"] for piece in pad.pieces}
    pad.holes = [own for own in own_pads if own["id"] not in piece_ids
                 and (own["hole"] or 0) > 0 and _inside(index, own["x1"], own["y1"], pad.pieces)]
    if pad.net_id is None:
        return
    board = connectivity(index)
    areas = board.areas_by_net.get(pad.net_id, [])
    hatched = _hatched_pours(index)
    outlines = [(pour.layer, pour.vertices) for pour in index.pcb.pours
                if pour.net == pad.net_id and not pour.shelved and pour.pour_id in hatched
                and len(pour.vertices) >= 3]
    empty = [(pour.layer, pour.vertices, why) for why, pours in (
        ("shelved", board.shelved.get(pad.net_id, ())),
        ("not poured", board.unpoured.get(pad.net_id, ()))) for pour in pours
        if len(pour.vertices) >= 3]

    def covered(layer: int, item) -> bool:
        """Does the copper of a via, hole or pad on a layer meet the net's area copper
        there (as check_connectivity joins it: a relief's spokes count), or lie in a
        hatched pour's outline?"""
        x, y = item["x1"], item["y1"]
        if _is_area(item):
            hit = any(area.layer == layer and area.contains(x, y) for area in areas)
        else:
            geometry = index._primitive_geometry(item, layer)
            box = (item["minx"], item["miny"], item["maxx"], item["maxy"])
            hit = any(area.layer == layer and area.touches(geometry, (x, y), box)
                      for area in areas)
        return hit or any(where == layer and point_in_polygon(x, y, vertices)
                          for where, vertices in outlines)

    pad.own_pour = any(covered(pad.layer, piece) for piece in pad.pieces)
    pad.reach_checked = True
    reached: set[int] = set()
    landed: dict[int, str] = {}
    for item in [*pad.vias, *pad.holes]:
        if item["net_id"] != pad.net_id:
            continue
        for layer in set(index._primitive_layers(item)) - {pad.layer} - reached:
            if covered(layer, item):
                reached.add(layer)
                continue
            for where, vertices, why in empty:
                if where == layer and point_in_polygon(item["x1"], item["y1"], vertices):
                    landed.setdefault(layer, why)
    pad.reach = [index.layer_name(layer) for layer in sorted(reached)]
    pad.reach_empty = [f"{index.layer_name(layer)} ({why})"
                       for layer, why in sorted(landed.items()) if layer not in reached]


def _pad_row(index, pad: ThermalPad) -> dict:
    minx = min(piece["minx"] for piece in pad.pieces)
    maxx = max(piece["maxx"] for piece in pad.pieces)
    miny = min(piece["miny"] for piece in pad.pieces)
    maxy = max(piece["maxy"] for piece in pad.pieces)
    row: dict = {"pads": pad.names, "position": pad.position, "net": pad.net,
                 "layer": index.layer_name(pad.layer), "area_mm2": _r(pad.area_mm2, 3),
                 "size_mil": [_r(maxx - minx, 4), _r(maxy - miny, 4)],
                 "ratio_to_other_pads": _r(pad.ratio, 3), "vias": pad.count}
    if pad.drawn_as:
        row["drawn_as"] = f"footprint {pad.drawn_as}"
    if pad.array:
        row["pad_array"] = pad.array
    if len(pad.pieces) > 1:
        row["pieces"] = len(pad.pieces)
    if pad.holes:
        row["footprint_holes"] = len(pad.holes)
    drills = Counter(_r(item["hole"], 4) for item in [*pad.vias, *pad.holes] if item["hole"])
    if drills:
        row["drills"] = [{"drill_mil": drill, "count": count}
                         for drill, count in sorted(drills.items())]
    unplated = 0
    for hole in pad.holes:
        primitive = index.pcb.primitives[hole["id"] - 1] if hole["id"] - 1 < len(
            index.pcb.primitives) else None
        if primitive is not None and getattr(primitive, "plated", None) is False:
            unplated += 1
    if unplated:
        row["unplated_holes"] = unplated
    if pad.wrong:
        row["wrong_net_vias"] = len(pad.wrong)
        row["wrong_nets"] = sorted({via["net_name"] or "(no net)" for via in pad.wrong},
                                   key=natural_key)
    if pad.beside:
        row["same_net_vias_beside"] = pad.beside
    if pad.reach_checked:
        row["reaches_layers"] = len(pad.reach)
        if pad.reach:
            row["reaches"] = pad.reach[:4]
        if pad.reach_empty:
            row["reaches_pours_without_copper"] = pad.reach_empty[:4]
        row["in_pour_on_own_layer"] = pad.own_pour
    if pad.count:
        row["guideline_vias"] = pad.needed()
    return row


# ---------- the PcbDoc: placement ----------

def _box(rows: list[dict]) -> tuple[float, float, float, float]:
    return (min(row["minx"] for row in rows), min(row["miny"] for row in rows),
            max(row["maxx"] for row in rows), max(row["maxy"] for row in rows))


def _gap(first, second) -> float:
    dx = max(0.0, first[0] - second[2], second[0] - first[2])
    dy = max(0.0, first[1] - second[3], second[1] - first[3])
    return math.hypot(dx, dy)


def _sensitive(design: _Design) -> dict[str, tuple[str, str]]:
    """Fitted temperature-sensitive parts: ref -> (label, detail)."""
    tree = design.tree
    out: dict[str, tuple[str, str]] = {}
    for ref, part in design.parts.items():
        if part.kind != "capacitor":
            continue
        tech = part.fields.get("technology")
        if tech is not None and tech.value in _ELECTROLYTIC:
            label = "supercapacitor" if tech.value == "supercapacitor" else \
                "electrolytic_capacitor"
            out[ref] = (label, f"an {tech.value.replace('_', ' ')} capacitor: its life "
                               "halves for about every 10 C hotter" if label != "supercapacitor"
                        else "a supercapacitor: its life shortens and leakage rises with heat")
    for ref in tree.references:
        if tree.fitted(ref):
            out.setdefault(ref, ("voltage_reference", "a voltage reference: its output drifts "
                                                      "with temperature"))
    for ref in sorted(tree.design.part_pins, key=natural_key):
        if ref in out or not tree.fitted(ref) or ref in tree.converters:
            continue
        text = tree.text(ref)
        prefix = designator_prefix(ref)
        pins = len(tree.design.part_pins.get(ref) or {})
        kind = tree.kind(ref)
        if _TEMP_SENSOR_TEXT.search(text) or prefix in ("NTC", "RT", "TH", "THM"):
            out[ref] = ("temperature_sensor", "a temperature or humidity sensor or "
                                              "thermistor: it reads the hot part, not the "
                                              "ambient")
        elif pins <= 6 and (prefix in _CRYSTAL_PREFIXES or (
                kind in ("passive", "other", "ic", "transistor")
                and _CRYSTAL_TEXT.search(text))):
            out[ref] = ("crystal_oscillator", "a crystal or oscillator: its frequency "
                                              "drifts with temperature")
        elif _BATTERY_TEXT.search(text) and kind in ("passive", "other", "connector"):
            out[ref] = ("battery", "a battery or cell holder: heat shortens its life")
    return out


# ---------- the check ----------

def prepare_thermal(
    netlist: dict,
    plan=None,
    project_params: dict | None = None,
    index=None,
    pcb_unavailable: str | None = None,
    parts: list[str] | None = None,
    checks: list[str] | None = None,
    rails: dict[str, float] | None = None,
    vref=None,
    inputs: Inputs | None = None,
    max_distance_mil: float = DEFAULT_MAX_DISTANCE_MIL,
    units: str = "mm",
    root_dir=None,
    policy_setting: str | None = None,
) -> dict:
    """Read the design (and the PcbDoc) once; render_thermal pages the result. An
    {"error": ...} dict for an unknown check or part. policy_setting: the Derating policy
    setting's file (resistor ratings), under the project's derating_policy.json."""
    requested = [check.strip().lower() for check in checks or [] if check.strip()]
    if invalid := [check for check in requested if check not in CHECKS]:
        return {"error": "invalid_checks",
                "message": f"Unknown checks {invalid}; use {list(CHECKS)}."}
    inputs = inputs or Inputs()
    design = _Design(netlist, plan, project_params, rails, vref, root_dir, policy_setting)
    tree = design.tree
    notes = list(design.notes)
    wanted_parts, unknown_parts = resolve_names(parts, tree.design.part_pins)
    if parts and not wanted_parts:
        return {"error": "part_not_found",
                "message": f"No part {unknown_parts} in the compiled netlist; use "
                           "search_components to find the designator."}
    if unknown_parts:
        notes.append(f"Not found: {', '.join(unknown_parts)}.")
    if design.board.unknown_rails:
        notes.append(f"rails names no net {design.board.unknown_rails}: ignored (use "
                     "query_net to find the real name).")
    hot = _recognise(design)

    # LEDs and resistors with a computed power.
    led_rows, _, _ = led_calculations(design.board, None, Counter())
    led_power: dict[str, dict] = {}
    led_resistors: dict[str, tuple[float, str]] = {}
    for row in led_rows:
        current = row.get("_current")
        vf = ((row.get("inputs") or {}).get("Vf") or {}).get("volts")
        ref = row.get("led")
        if not ref or current is None or current <= 0 or vf is None:
            continue
        watts = vf * current
        previous = led_power.get(ref, {}).get("watts", 0.0)
        led_power[ref] = {"status": "computed", "watts": _r(previous + watts),
                          "formula": "P = Vf x I", "inputs": {
                              "Vf": row["inputs"]["Vf"],
                              "I": {"amps": _r(current), "source": "get_circuit_calculations"}},
                          "confidence": row.get("confidence")}
        for name, milliwatts in (row.get("result") or {}).get("resistor_power_mw", {}).items():
            led_resistors[name] = (milliwatts / 1000.0, ref)
    resistors = _resistor_power(design, led_resistors)
    for ref, info in resistors.items():
        watts = info.get("watts")
        if info["shunt"]:
            entry = hot.setdefault(ref, HotPart(ref))
            entry.add("shunt_resistor", f"a current-sense shunt ({info['value'] or '?'} ohm)")
            entry.kind = "resistor"
        elif watts is not None and (watts >= HOT_RESISTOR_W or (
                info["rating_w"] and watts >= HOT_RESISTOR_MIN_W
                and watts >= HOT_RESISTOR_SHARE * info["rating_w"])):
            entry = hot.setdefault(ref, HotPart(ref))
            entry.add("power_resistor", f"{_w(watts)} computed ({info['formula']})")
            entry.kind = "resistor"
    for ref, estimate in led_power.items():
        if estimate["watts"] >= HOT_LED_W:
            entry = hot.setdefault(ref, HotPart(ref))
            entry.add("power_led", f"{_w(estimate['watts'])} computed (Vf x I)")
            entry.kind = "led"
    for name in (inputs.power or {}):
        match = resolve_names([name], tree.design.part_pins)[0]
        for ref in match or ():
            hot.setdefault(ref, HotPart(ref)).add("given_power", "named in power")
    for ref in wanted_parts or ():
        if tree.fitted(ref) and ref not in hot:
            hot[ref] = HotPart(ref, {"named": "named in part"})
    not_fitted = sorted(ref for ref in hot if not tree.fitted(ref))
    for ref in not_fitted:
        del hot[ref]
    _stages(design, hot)

    # The PcbDoc: thermal pads, then placement.
    sensitive = _sensitive(design)
    stats: Counter = Counter()
    pads_found: list[ThermalPad] = []
    boxes: dict[str, tuple] = {}
    sides: dict[str, str] = {}
    if index is not None:
        by_name = pcb_components(index)
        # Placed parts fitted in the variant, also those the compiled netlist lacks.
        dropped = getattr(plan, "not_fitted", set()) or set()
        names = [name for name in by_name if name not in dropped and tree.fitted(name)]
        stats["placed_not_in_netlist"] = sum(1 for name in names
                                             if name not in tree.design.part_pins)
        pads_by = component_pads(index, by_name, names)
        drawn = footprint_areas(index)
        for name in sorted(names, key=natural_key):
            rows = by_name[name]
            own = [pad for row in rows for pad in pads_by.get(row["id"], [])]
            if own:
                boxes[name] = _box([dict(pad) for pad in own])
                sides[name] = rows[0]["side"] or "top"
            kind = tree.kind(name)
            prefix = designator_prefix(name)
            # T and TR also name transistors (TRANSISTOR_PREFIXES): their kind decides.
            excluded = ((prefix in _MECHANICAL_PREFIXES and kind != "transistor")
                        or kind in _MECHANICAL_KINDS
                        or name in sensitive and sensitive[name][0] in (
                            "crystal_oscillator", "battery", "electrolytic_capacitor",
                            "supercapacitor"))
            for row in rows:
                component_rows = pads_by.get(row["id"], [])
                found = find_thermal_pads(index, row["id"], component_rows, excluded,
                                          drawn.get(row["id"]), tree.grounds)
                if found and kind is not None and name not in tree.converters and \
                        kind not in ("ic", "other", "transistor", "diode", "zener"):
                    stats["pads_skipped_kind"] += len(found)
                    continue
                known = name in hot and hot[name].known_hot
                for pad in found:
                    if pad.net_id is None and not known:
                        stats["pads_without_net_skipped"] += 1
                        continue  # an anchoring tab of a holder or module
                    _audit_pad(index, pad, component_rows)
                    pads_found.append(pad)
                    entry = hot.setdefault(name, HotPart(name))
                    entry.pads.append(pad)
                    entry.add("exposed_pad", f"{_pad_kind(pad, article=True)} "
                                             f"({pad.describe()}, {pad.area_mm2:.1f} mm^2)")
        missing = sorted((ref for ref in hot if ref not in boxes), key=natural_key)
        if missing:
            stats["hot_not_on_pcb"] = len(missing)
            notes.append(f"Not on the PcbDoc (or no pads): {_names(missing, 8)}.")
        model = connectivity(index).copper_model() if pads_found else {}
        empty = [f"{count} {what}" for count, what in (
            (model.get("shelved_pours"), "shelved"),
            (model.get("pours_without_copper"), "not poured")) if count]
        if empty:
            notes.append(
                f"Pours holding no copper in the file: {' and '.join(empty)}. A thermal pad's "
                "vias landing in one are listed under reaches_pours_without_copper, not as "
                "reaching copper (and not as unconnected): restore or repour and save, then "
                "run this again.")

    # Estimates.
    for entry in hot.values():
        if len(entry.bases) > 1:
            entry.bases.pop("named", None)
        entry.power = _estimate(design, inputs, entry, resistors, led_power)
        entry.watts = entry.power.get("watts") if entry.power.get("status") in (
            "computed", "given") else None
        entry.tj = _tj(inputs, entry)

    findings: list[dict] = []
    for entry in hot.values():
        tj = entry.tj or {}
        if tj.get("status") in ("over_max", "near_max"):
            worst = tj.get("tj_worst_c", tj["tj_c"])
            at = inputs.ta_max if inputs.ta_max is not None else inputs.ta
            over = tj["status"] == "over_max"
            findings.append(_finding(
                "tj_over_max" if over else "tj_near_max", "warning" if over else "info",
                f"{entry.ref}: Tj is about {_c(worst)} at Ta {_c(at)} ({tj['theta_ja_c_per_w']:g} "
                f"C/W x {_w(entry.watts)} = {_c(tj['rise_c'])} rise), "
                + (f"above its {_c(tj['tj_max_c'])} limit" if over else
                   f"above {NEAR_TJ_MAX:.0%} of its {_c(tj['tj_max_c'])} limit")
                + ". thetaJA is the datasheet's JEDEC-board figure: more copper, thermal vias "
                  "or airflow lower it, and a thermal simulation or measurement decides.",
                [entry.ref], tj_c=_r(worst, 4), tj_max_c=tj["tj_max_c"], watts=entry.watts))
    for entry in hot.values():
        for pad in entry.pads:
            findings += _pad_findings(entry, pad)
    pairs = _neighbour_findings(design, hot, sensitive, boxes, sides, max_distance_mil, units,
                                stats)
    findings += pairs

    keep_parts = set(wanted_parts) if wanted_parts is not None else None
    if keep_parts is not None:
        findings = [finding for finding in findings if set(finding["designators"]) & keep_parts]
    wanted = set(requested or CHECKS)
    not_checked: dict[str, str] = {}
    if index is None:
        for check in CHECKS:
            if check in PCB_CHECKS:
                not_checked[check] = pcb_unavailable or "no PcbDoc"
    findings = [finding for finding in findings if finding["check"] in wanted]
    findings.sort(key=lambda finding: (_SEVERITY_RANK[finding["severity"]],
                                       _CHECK_RANK[finding["check"]],
                                       natural_key(finding["designators"][0])))
    rows = [entry for entry in hot.values()
            if keep_parts is None or entry.ref in keep_parts]
    rows.sort(key=lambda entry: (entry.watts is None, -(entry.watts or 0.0),
                                 not entry.known_hot, natural_key(entry.ref)))
    unknown = inputs.unknown(tree.design.part_pins, tree.design.nets)
    if unknown:
        notes.append("Names no part (or net, for load): " + "; ".join(
            f"{arg} {_names(names, 6)}" for arg, names in unknown.items())
            + ". Check the designators and rail names (query_net, search_components).")
    labels = {entry.ref: tree.label(entry.ref) for entry in rows if tree.label(entry.ref)}
    stage = {entry.ref: sorted((other.ref for other in hot.values()
                                if other is not entry and other.groups & entry.groups),
                               key=natural_key) for entry in rows}
    return {"variant": tree.variant, "rows": rows, "findings": findings, "stats": stats,
            "labels": labels, "stage": stage,
            "notes": notes, "inputs": inputs, "wanted": wanted, "not_checked": not_checked,
            "pcb": index is not None, "pads": pads_found, "index": index,
            "max_distance_mil": max_distance_mil, "units": units,
            "sensitive_count": len(sensitive), "not_fitted_skipped": len(not_fitted)}


def _pad_kind(pad: ThermalPad, article: bool = False) -> str:
    what = ("pad array" if pad.array else "exposed pad" if pad.position == "centre"
            else "thermal tab")
    return f"{'an' if what[0] == 'e' else 'a'} {what}" if article else what


def _pad_findings(entry: HotPart, pad: ThermalPad) -> list[dict]:
    out: list[dict] = []
    bare = f"{entry.ref}'s {_pad_kind(pad)} ({pad.describe()}, {pad.area_mm2:.1f} mm^2"
    label = f"{bare}, {pad.net or 'no net'})"
    hot = entry.known_hot
    why = ", ".join(basis.replace("_", " ") for basis in entry.labels()
                    if basis not in WEAK_BASES) or "not known to dissipate"
    fields = {"pads": pad.names, "net": pad.net, "area_mm2": _r(pad.area_mm2, 3),
              "position": pad.position,
              "drawn_as": f"footprint {pad.drawn_as}" if pad.drawn_as else None}
    if pad.count == 0:
        spread = pad.beside >= VIA_FLOOR or (pad.position == "tab" and pad.own_pour)
        severity = "warning" if hot and not spread else "info"
        text = f"{label} has no via inside it."
        if pad.beside:
            text += f" {pad.beside} via(s) of its net sit within 1 mm of its edge."
        if pad.position == "tab" and pad.own_pour:
            text += " It sits in a pour of its net on its own layer, which spreads heat."
        text += (f" {entry.ref} is {'a ' + why if hot else why}: a thermal pad is usually "
                 "stitched with an array of vias to a plane or pour on an inner or the "
                 "opposite layer, close to the heat source. Check the datasheet's land "
                 "pattern" + (" (some parts need only the pad soldered)." if not hot else "."))
        out.append(_finding("thermal_pad_no_vias", severity, text, [entry.ref],
                            vias_beside=pad.beside or None, **fields))
    if pad.wrong:
        nets = sorted({via["net_name"] or "(no net)" for via in pad.wrong}, key=natural_key)
        out.append(_finding(
            "thermal_vias_wrong_net", "warning",
            f"{len(pad.wrong)} via(s) inside {label} are on {_names(nets, 4)}: a via in the "
            "pad joins its copper to that net (a short), or it carries no connection. Move "
            "it or change its net.", [entry.ref], wrong_nets=nets, count=len(pad.wrong),
            **fields))
    needed = pad.needed()
    if 0 < pad.count < needed:
        out.append(_finding(
            "thermal_pad_few_vias", "info",
            f"{pad.count} via(s) in {label}: {VIA_GUIDELINE} ({needed} here).",
            [entry.ref], vias=pad.count, guideline_vias=needed, **fields))
    if pad.net_id is None:
        out.append(_finding(
            "thermal_pad_unconnected", "info",
            f"{bare}) has no net on the PcbDoc: its copper and any via in it join nothing, "
            "so the heat stays in the pad and the part's own pins. Assign it the net the "
            "datasheet names (often ground, or the tab's pin).", [entry.ref], **fields))
    elif pad.count and pad.reach_checked and not pad.reach and not pad.reach_empty and \
            len(pad.wrong) < pad.count:
        out.append(_finding(
            "thermal_pad_unconnected", "info",
            f"The vias in {label} reach no plane or pour of {pad.net} on another layer: "
            "they carry heat only into their barrels and any track. A plane or pour on an "
            "inner or the opposite layer spreads it (pours are read as last poured).",
            [entry.ref], **fields))
    return out


def _neighbour_findings(design: _Design, hot: dict[str, HotPart],
                        sensitive: dict[str, tuple[str, str]], boxes: dict[str, tuple],
                        sides: dict[str, str], max_mil: float, units: str, stats: Counter
                        ) -> list[dict]:
    """hot_neighbours between hot parts (a part only named in part= is no heat source),
    and sensitive_near_hot. A crystal, reference or sensor wired to a hot part by a net
    other than a supply (a power-tree net, or one it takes on a supply pin) is beside it
    by design."""
    tree = design.tree
    out: list[dict] = []
    placed = sorted((ref for ref in hot if ref in boxes and set(hot[ref].bases) != {"named"}),
                    key=natural_key)
    for number, first in enumerate(placed):
        a = hot[first]
        for second in placed[number + 1:]:
            b = hot[second]
            if a.groups & b.groups:
                continue
            if not (a.known_hot or b.known_hot):
                continue
            gap = _gap(boxes[first], boxes[second])
            if gap > max_mil:
                continue
            same = sides[first] == sides[second]
            a.neighbours.append({"designator": second, "distance_mil": _r(gap, 4),
                                 "same_side": same})
            b.neighbours.append({"designator": first, "distance_mil": _r(gap, 4),
                                 "same_side": same})
            stats["neighbour_pairs"] += 1
            out.append(_finding(
                "hot_neighbours", "info",
                f"{first} ({_basis_text(a)}) and {second} ({_basis_text(b)}) are "
                f"{_distance_text(gap, units)} apart edge to edge"
                + (" on the same side" if same else " on opposite sides") + ": each heats "
                "the other, and a datasheet's thetaJA assumes a part alone on its copper. "
                "Check their combined dissipation in that area.",
                [first, second], distance_mil=_r(gap, 4), same_side=same))
    for ref, (label, detail) in sorted(sensitive.items(), key=lambda item: natural_key(item[0])):
        if ref not in boxes or (ref in hot and hot[ref].known_hot):
            continue
        near = []
        # Its wiring: nets other than ground, the power tree's and the ones it is powered
        # from (a supply pin's: an oscillator's VDD on the processor's rail is no link).
        names = tree.pin_names(ref)
        own = {net for pin, net in (tree.design.part_pins.get(ref) or {}).items()
               if pin_role(names.get(pin)) != "supply"} - tree.grounds - design.supply_nets
        for other in placed:
            entry = hot[other]
            if not entry.known_hot or other == ref:
                continue
            if label in _PLACED_BY_DESIGN and own & nets_of(tree, other):
                continue  # a crystal at the IC it clocks, a reference at its user
            gap = _gap(boxes[ref], boxes[other])
            if gap <= max_mil:
                near.append((gap, other))
        if not near:
            continue
        near.sort()
        for gap, other in near:
            hot[other].sensitive.append({"designator": ref, "sensitivity": label,
                                         "distance_mil": _r(gap, 4),
                                         "same_side": sides[ref] == sides[other]})
        stats["sensitive_pairs"] += len(near)
        gap, other = near[0]
        out.append(_finding(
            "sensitive_near_hot", "info",
            f"{ref} ({detail}) is {_distance_text(gap, units)} from {other} "
            f"({_basis_text(hot[other])})"
            + (f" and within {_distance_text(max_mil, units)} of "
               f"{_names([name for _, name in near[1:]], 4)}" if len(near) > 1 else "")
            + (". If it is meant to read the ambient or the board, move it away from the "
               "heat; if it monitors that part, this is where it belongs."
               if label == "temperature_sensor" else
               ". Keep it away from the heat or check its temperature in a thermal "
               "simulation."), [ref, *(name for _, name in near)], sensitivity=label,
            distance_mil=_r(gap, 4), hot_parts=[name for _, name in near]))
    return out


def _basis_text(entry: HotPart) -> str:
    labels = [basis for basis in entry.labels() if basis not in WEAK_BASES] or entry.labels()
    return "/".join(label.replace("_", " ") for label in labels[:2])


# ---------- rendering ----------

def _row(prepared: dict, entry: HotPart) -> dict:
    tree_label = prepared["labels"].get(entry.ref)
    row: dict = {"designator": entry.ref, "basis": {key: entry.bases[key]
                                                    for key in entry.labels()}}
    if entry.kind:
        row["type"] = entry.kind
    if tree_label:
        row["part"] = tree_label
    stage = [other for other in prepared["stage"].get(entry.ref, ()) if other != entry.ref]
    if stage:
        row["power_stage"] = stage[:8]
    row["power"] = entry.power
    if entry.tj:
        row["tj"] = entry.tj
    if prepared["pcb"]:
        pcb: dict = {}
        index = prepared["index"]
        if entry.pads:
            pcb["thermal_pads"] = [_pad_row(index, pad) for pad in entry.pads]
        if entry.neighbours:
            pcb["hot_neighbours"] = sorted(entry.neighbours,
                                           key=lambda item: item["distance_mil"])[:8]
        if entry.sensitive:
            pcb["sensitive_neighbours"] = sorted(entry.sensitive,
                                                 key=lambda item: item["distance_mil"])[:8]
        if pcb:
            row["pcb"] = pcb
    return row


def _summary(prepared: dict) -> dict:
    rows: list[HotPart] = prepared["rows"]
    by_basis: Counter = Counter()
    for entry in rows:
        for basis in entry.bases:
            by_basis[basis] += 1
    status = Counter(entry.power.get("status", "needs_inputs") for entry in rows)
    tj = Counter((entry.tj or {}).get("status", "no_power") for entry in rows)
    out: dict = {
        "hot_parts": len(rows),
        "known_to_dissipate": sum(1 for entry in rows if entry.known_hot),
        "by_basis": {basis: by_basis[basis] for basis in BASES if by_basis[basis]},
        "dissipation": {"computed": status["computed"], "given": status["given"],
                        "needs_inputs": status["needs_inputs"]},
        "tj": {"computed": tj["ok"] + tj["near_max"] + tj["over_max"],
               "needs_theta": tj["needs_theta"], "over_tj_max": tj["over_max"],
               "near_tj_max": tj["near_max"]},
    }
    if prepared["pcb"]:
        pads = [pad for entry in rows for pad in entry.pads]
        counts = Counter("0" if pad.count == 0 else "1-3" if pad.count < 4 else
                         "4-8" if pad.count <= 8 else "9+" for pad in pads)
        out["thermal_pads"] = {
            "pads": len(pads),
            "parts": sum(1 for entry in rows if entry.pads),
            "exposed_pads": sum(1 for pad in pads if pad.position == "centre"),
            "tabs": sum(1 for pad in pads if pad.position == "tab"),
            "via_counts": {key: counts[key] for key in ("0", "1-3", "4-8", "9+")},
            "vias_total": sum(pad.count for pad in pads),
            "wrong_net_vias": sum(len(pad.wrong) for pad in pads),
            "reaching_plane_or_pour": sum(1 for pad in pads if pad.reach),
        }
        extra = {"drawn_as_fill_or_region": sum(1 for pad in pads if pad.drawn_as),
                 "pad_arrays": sum(1 for pad in pads if pad.array),
                 "landing_in_pours_without_copper": sum(1 for pad in pads
                                                        if pad.reach_empty and not pad.reach)}
        out["thermal_pads"].update({key: value for key, value in extra.items() if value})
        stats = prepared["stats"]
        out["neighbour_pairs"] = stats["neighbour_pairs"]
        out["sensitive_pairs"] = stats["sensitive_pairs"]
        out["sensitive_parts"] = prepared["sensitive_count"]
    return out


def _needs(rows: list[HotPart]) -> dict[str, list[str]]:
    """Designators per missing input, so the user can be asked in one go."""
    out: dict[str, list[str]] = defaultdict(list)
    for entry in rows:
        texts = list(entry.power.get("needs") or ()) + list((entry.tj or {}).get("needs") or ())
        for text in texts:
            arg = text.split(":")[0]
            if entry.ref not in out[arg]:
                out[arg].append(entry.ref)
    return {arg: {"parts": len(names), "designators": names[:NEEDS_LISTED]}
            for arg, names in out.items()}


def render_thermal(prepared: dict, offset: int = 0, limit: int = PAGE_LIMIT) -> dict:
    """One page: the summary first, then the hot parts (hottest first) and the findings
    (warnings first) paged together on one offset."""
    rows, findings = prepared["rows"], prepared["findings"]
    by_check: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for finding in findings:
        by_check[finding["check"]][finding["severity"]] += 1
    offset = max(0, int(offset or 0))
    page = slice(offset, offset + limit)
    summary = _summary(prepared)
    summary["findings"] = {check: dict(levels) for check, levels in by_check.items()}
    inputs: Inputs = prepared["inputs"]
    result: dict = {
        "variant": prepared["variant"],
        "summary": summary,
        "severity_counts": {level: sum(1 for finding in findings
                                       if finding["severity"] == level)
                            for level in ("warning", "info")},
        "checks_run": [check for check in CHECKS if check in prepared["wanted"]
                       and check not in prepared["not_checked"]],
        "assumptions": {"ta_c": inputs.ta, "ta_max_c": inputs.ta_max,
                        "tj_max_default_c": DEFAULT_TJ_MAX,
                        "max_distance_mil": _r(prepared["max_distance_mil"], 6)},
        "estimate": ESTIMATE,
        "guidance": GUIDANCE,
    }
    needs = _needs(rows)
    if needs:
        result["inputs_needed"] = needs
    result["findings"] = findings[page]
    result["findings_total"] = len(findings)
    result["parts"] = [_row(prepared, entry) for entry in rows[page]]
    result["parts_total"] = len(rows)
    result["offset"] = offset
    result["has_more"] = offset + limit < max(len(rows), len(findings))
    if result["has_more"]:
        result["next_offset"] = offset + limit
    if prepared["not_checked"]:
        result["not_checked"] = prepared["not_checked"]
    notes = list(prepared["notes"])
    notes.append(
        "Hot parts are recognised from the power tree, part words and pads; a part that "
        "dissipates but is named otherwise is missed (give its power=; part= lists it, but "
        "as no heat source for hot_neighbours). Loads are never guessed: inputs_needed "
        "lists the parts waiting for load= (amps per designator or output rail), eta=, "
        "power= or theta=.")
    if prepared["pcb"]:
        notes.append(
            "Thermal pads are pads (or footprint fills and regions) much larger than the "
            "part's others; vias count when their centre is inside the pad's copper and they "
            "reach its layer. Pours and planes are read as last poured (repour and save "
            "before trusting reaches).")
    result["notes"] = notes
    return result


def run_thermal_check(
    netlist: dict,
    plan=None,
    project_params: dict | None = None,
    index=None,
    pcb_unavailable: str | None = None,
    parts: list[str] | None = None,
    checks: list[str] | None = None,
    rails: dict[str, float] | None = None,
    vref=None,
    inputs: Inputs | None = None,
    max_distance_mil: float = DEFAULT_MAX_DISTANCE_MIL,
    units: str = "mm",
    root_dir=None,
    offset: int = 0,
    limit: int = PAGE_LIMIT,
) -> dict:
    """The thermal check in one call: prepare_thermal and one page of render_thermal.
    Heuristic: see the module notes."""
    prepared = prepare_thermal(netlist, plan, project_params, index, pcb_unavailable, parts,
                               checks, rails, vref, inputs, max_distance_mil, units, root_dir)
    if "error" in prepared:
        return prepared
    return render_thermal(prepared, offset, limit)
