"""Offline decoupling coverage: which capacitors decouple each IC's power pins, and gaps.

Grounds, rails, ICs and capacitors come from the compiled netlist and the sheets' pin
names. The rules were calibrated on real boards: a net is a rail by its name or by an IC
supply pin on it (a power port alone proves little), never when it carries EN, PG or
SENSE; an IC is a U or IC part fitted in the variant, paired with a rail through pins
whose first function is a supply, reference or capacitor pin. Capacitors are parts from
a rail to ground (two-pin, or arrays and feed-throughs with a ground pin), fitted in
the variant. A fitted 0 ohm link joins two rails; a ferrite, inductor or resistor to a
rail with capacitors is a filter.

With a PcbDoc, the distance from each IC power pad to the nearest fitted capacitor on
its rail is measured straight, pad edge to pad edge (warning above 5 mm by default: 85%
of IC rails measured on real boards are within it). Regions and planes are not read,
and pours only to see whether an opposite-side capacitor's pad sits in one, so vias are
reported as found, never as copper that is missing.
"""

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field

try:
    from services.bom import build_parts, natural_key
    from services.bom_checks import normalize_value
    from services.pcb_geometry import point_in_polygon
except ImportError:
    from server.services.bom import build_parts, natural_key
    from server.services.bom_checks import normalize_value
    from server.services.pcb_geometry import point_in_polygon

CHECKS = (
    "no_capacitor", "not_fitted_only", "filtered_rail", "far_capacitor",
    "capacitor_elsewhere", "pads_far", "opposite_side_no_via", "no_small_capacitor",
    "no_bulk_capacitor", "unknown_value",
)
PCB_CHECKS = frozenset({"far_capacitor", "pads_far", "opposite_side_no_via"})
# Bulk is per rail or where it enters the board, often behind a ferrite or on a module:
# on real boards 'no bulk' held for about half the rails, so it runs only when asked.
OPT_IN_CHECKS = frozenset({"no_bulk_capacitor"})
_SEVERITY = {
    "no_capacitor": "warning", "not_fitted_only": "warning", "filtered_rail": "warning",
    "far_capacitor": "warning",
}
_CHECK_RANK = {check: rank for rank, check in enumerate(CHECKS)}
_SEVERITY_RANK = {"warning": 0, "info": 1}

MIL_PER_MM = 1 / 0.0254
DEFAULT_MAX_DISTANCE_MIL = 5 * MIL_PER_MM
VIA_NEAR_MIL = 2 * MIL_PER_MM  # a via this close to an opposite-side capacitor's pad
SMALL_FARADS = 1e-6  # under this a capacitor is a small (high-frequency) one
LINK_OHMS = 0.1  # a resistor up to this joins two nets (0 ohm links, shunts)
FILTER_OHMS = 100.0  # a resistor up to this filters a supply; above, it biases or divides
FINDING_LIMIT = 100
RAIL_LIMIT = 60
DETAIL_CAPACITORS = 5  # nearest capacitors listed per pair when ICs are named
THT_LAYER = 74  # a through-hole pad's layer (multi-layer)

# ---------- names ----------

_SPLIT = re.compile(r"[_\-\s/]+")
_POWER_TOKEN = re.compile(
    r"^(\d+V\d*[A-Z]*|\d+[.,P]\d+V\w*|\d+P\d+|V\d+(V\d+|P\d+)?|[PNM]\d+V\d*|\w*VCC\w*|\w*VDD\w*|"
    r"VEE\w*|VBATT?\w*|VBUS\w*|VIN\w*|VSYS\w*|VMAIN\w*|VPP|VIO\w*|PVIN\w*|VSUP\w*|VPWR|PWR|"
    r"VDC|VAUX\w*|VCORE\w*|VLED\w*|VMOT\w*|VUSB|VLOGIC|VDRIVE|VCHG|VREF\w*|VOUT\w*|VS|VM|VL|"
    r"VPOS|VNEG|VBOOST|VCHARGE|VTT|VREG\w*)$"
)
_GROUND_TOKEN = re.compile(r"^(\w*GND\w*|VSS\w*|0V|GROUND)$")
_SIGNAL_TOKENS = frozenset({
    "EN", "ENA", "ENABLE", "PG", "PGOOD", "PWRGD", "PGD", "GOOD", "OK", "SENSE", "SNS", "SEN",
    "DET", "DETECT", "FB", "ON", "OFF", "FAULT", "FLT", "ALERT", "SW", "LX", "CTRL", "CTL",
    "ADC", "MON", "MEAS", "ISNS", "ISENSE", "SEL", "RST", "RESET", "INT", "IRQ", "STAT",
    "STATUS", "TX", "RX", "SDA", "SCL", "CLK", "DATA", "SIG", "GATE", "DRV", "PWM", "BST",
    "BOOT", "ILIM", "SS", "TRK", "COMP", "SYNC", "MODE", "REQ", "ACK", "CS", "DIV", "LVL",
    "CMD", "SET", "TRIG", "SDI", "SDO", "MISO", "MOSI", "SCK", "SCLK", "DIS", "SHDN",
    "NFAULT", "CC", "CC1", "CC2", "DP", "DM", "D+", "D-", "ID", "SBU1", "SBU2",
})
_SIGNAL_INSIDE = re.compile(r"SENSE|SNS|PGOOD|PWRGD|ENABLE|ALERT|FAULT")
# Tokens that make a net an enable, status or measurement net even when a supply pin is
# on it (VBAT_SENSE, 3V3_EN); other signal tokens (SW, ADC, CLK, DRV) still name rails
# that feed a supply pin (+3V3_SW, VDDA_ADC).
_STATUS_TOKENS = frozenset({
    "EN", "ENA", "ENABLE", "PG", "PGOOD", "PWRGD", "PGD", "GOOD", "OK", "SENSE", "SNS",
    "SEN", "FB", "ALERT", "FAULT", "FLT", "NFAULT", "DET", "DETECT", "MON", "MEAS", "ISNS",
    "ISENSE", "STAT", "STATUS", "SHDN", "DIS",
})

_PIN_GROUND = re.compile(
    r"^(\w*GND\w*|VSS\w*|EP|EPAD|E-PAD|PAD|THERMAL\w*|TAB|SLUG|0V|GROUND|EXPOSED\w*|\d*GND)$"
)
_PIN_SUPPLY = re.compile(
    r"^(\w*VDD\w*|\w*VCC\w*|VIN\w*|PVIN\w*|AVIN\w*|VBAT\w*|VBUS\w*|VS[+-]?\d*|V[+-]|VSUP\w*|"
    r"VIO\w*|VL|VLOGIC|VDRIVE|VPP|VEE\w*|VNEG|VPOS|VM|VMOT\w*|VPWR|PWR|VSYS|V\d+V\d*|"
    r"\d+V\d*|\d+P\d+V?\w*|V\d+P\d+|VPLUS|VSUPPLY|SUPPLY|POWER|\+?V(CC|DD)|DVDD\w*|AVDD\w*)$"
)
_PIN_REF = re.compile(
    r"^(\w*VREF[A-Z0-9_]*[+-]?|REF|REF[+-]|REFIN\d*|REFOUT\d*|REF_?[PN]|REFP|REFN|VCM|VMID|"
    r"VBIAS)$"
)
_PIN_CAP = re.compile(
    r"^(VCAP\w*|CAP\d*|CAP[+-]|VREG\w*|DECOUP\w*|BYP|BYPASS|LDO_?OUT\w*|LDO_?CAP\w*|"
    r"VCORE\w*|VDDCORE\w*|VDD_?OUT\w*)$"
)
_PIN_OUT = re.compile(r"^(VOUT\w*|OUT\w*|SW\d*|LX\d*)$")
_PIN_IN = re.compile(r"^(IN\d*|INPUT)$")
# IN+, VIN-, -IN, VINP, AIN_N: differential or op-amp inputs, signals.
_PIN_INPUT_PAIR = re.compile(r"^((V|A)?IN\d*[+-]|[+-]A?IN\d*|(V|A)?IN_?[PN]\d*)$")
_PIN_BOOTSTRAP = re.compile(r"^(BST|BOOT|BS|CB|CBOOT|SW|LX|PH|VSW)\w*$")
_PAIR_ROLES = frozenset({"supply", "ref", "cap"})

IC_PREFIXES = frozenset({"U", "IC"})
_FILTER_PREFIXES = frozenset({"L", "FB", "FL", "R"})
_NOT_CAP_PREFIXES = frozenset({
    "D", "U", "IC", "Q", "J", "P", "TVS", "ESD", "Z", "LED", "CN", "CON", "CONN", "CR", "F",
    "L", "FB", "R", "T", "X", "Y", "TP", "PS", "SW", "K",
})
_CAP_TEXT = re.compile(
    r"\bcap\b|\bcap[ .,_/-]|\bcaps\b|capacitor|\bmlcc\b|electrolytic|tantalum|kondensator|"
    r"feed-?through",
    re.IGNORECASE,
)
FERRITE_TEXT = re.compile(r"ferrite|\bbead", re.IGNORECASE)
_FARAD_TEXT = re.compile(r"\d\s*[pnuµμmPNU]?F\b|\d[pnuµμ]\d", re.IGNORECASE)
_CAP_VALUE = re.compile(
    r"(?<![\w.])(\d+(?:[.,]\d+)?)\s*([pnuµμPNU])\s*[fF]?(?![a-zA-Z\d])"
    r"|(?<![\w.])(\d{1,3})([pnuµμPNU])(\d{1,2})(?![\w.])"  # 4u7, not 1N4148
)
_FARADS = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6}


def net_tokens(net: str) -> list[str]:
    return [token for token in _SPLIT.split(net.upper().replace("\\", "")) if token]


def net_kind(net: str) -> dict:
    """Whether a net name says ground, power, or carries a signal token."""
    tokens = net_tokens(net)
    stripped = [token.strip("+-") for token in tokens]
    signal = any(token in _SIGNAL_TOKENS or token.rstrip("+-") in _SIGNAL_TOKENS
                 for token in tokens) or any(_SIGNAL_INSIDE.search(token) for token in tokens)
    status = any(token.rstrip("+-") in _STATUS_TOKENS for token in tokens) or any(
        _SIGNAL_INSIDE.search(token) for token in tokens)
    ground = any(_GROUND_TOKEN.match(token) for token in stripped)
    power = any(_POWER_TOKEN.match(token) for token in stripped) or bool(
        re.match(r"^[+-]\d", net.strip())
    )
    if len(tokens) > 1 and tokens[-1] in ("P", "N"):
        power = False  # a differential pair's half (A10_HLVDDI_P)
    return {"ground": ground, "power": power and not ground, "signal": signal,
            "status": status}


def first_function(name: str) -> str:
    """'I/O, VREF 1' -> 'I'; 'VDD(3V3)' -> 'VDD'; '3.3 V' -> '3P3V'; '+VS' -> 'VS'."""
    text = re.sub(r"\([^)]*\)", "", name.upper().replace("\\", ""))
    first = next((part for part in re.split(r"[/,]", text.replace(" ", "")) if part), "")
    first = re.sub(r"(?<=\d)[.,](?=\d)", "P", first.strip("_"))
    return first


def pin_role(name: str | None) -> str:
    """ground, supply, ref, cap, out, in, other, or none (no name, or a number), from the
    pin name's first function ('I/O, VREF 1' is an I/O pin, not a reference)."""
    first = first_function(name or "")
    if not first or first.isdigit():
        return "none"
    if _SIGNAL_INSIDE.search(first) or _PIN_INPUT_PAIR.match(first):
        return "other"  # VCCSENSE, VDD_PGOOD, VIN- of an ADC or a current monitor
    if first[0] in "+-" and len(first) > 1:
        first = first[1:]  # +VS, -VIN
        if first == "V":
            return "supply"  # +V, -V of an op amp
    for role, pattern in (("ground", _PIN_GROUND), ("ref", _PIN_REF), ("cap", _PIN_CAP),
                          ("supply", _PIN_SUPPLY), ("out", _PIN_OUT), ("in", _PIN_IN)):
        if pattern.match(first):
            return role
    if first[-1] in "+-" and _PIN_SUPPLY.match(first[:-1]):
        return "supply"  # VCC-, VDD+, VEE-
    return "other"


def designator_prefix(designator: str) -> str:
    match = re.match(r"[A-Za-z_]+", designator or "")
    return match.group(0).upper().strip("_") if match else ""


def capacitance(*texts: str | None) -> float | None:
    """Farads from the first text holding a capacitance ('100nF/16V', '4u7', '0.1UF')."""
    for text in texts:
        for match in _CAP_VALUE.finditer(text or ""):
            if match.group(1):
                number, prefix = match.group(1).replace(",", "."), match.group(2)
            else:
                number, prefix = f"{match.group(3)}.{match.group(5)}", match.group(4)
            try:
                return float(number) * _FARADS[prefix.lower()]
            except ValueError:
                continue
    return None


def is_capacitor(prefix: str, text: str) -> bool:
    """A C part, or another C-prefixed part whose text says capacitor or a value in
    farads (CA, CN arrays); never a C-prefixed connector (CN, CON) or diode (CR)."""
    return prefix == "C" or (
        prefix.startswith("C") and prefix not in _NOT_CAP_PREFIXES
        and bool(_CAP_TEXT.search(text) or _FARAD_TEXT.search(text))
    )


def farads_text(value: float | None) -> str | None:
    if value is None:
        return None
    for unit, scale in (("uF", 1e-6), ("nF", 1e-9), ("pF", 1e-12)):
        if value >= scale:
            return f"{value / scale:.3g}{unit}"
    return f"{value:.3g}F"


# ---------- the design ----------

@dataclass
class _Cap:
    designator: str
    rail: str  # the net it decouples (the non-ground one)
    other: str  # ground, or the other net of a capacitor between two nets
    to_ground: bool
    farads: float | None
    fitted: bool

    def small(self) -> bool:
        return self.farads is not None and self.farads < SMALL_FARADS

    def describe(self) -> dict:
        out = {"designator": self.designator, "value": farads_text(self.farads)}
        if not self.fitted:
            out["not_fitted"] = True
        return out


@dataclass
class _Pair:
    ic: str
    rail: str
    pins: list[str] = field(default_factory=list)  # IC pin designators on the rail


@dataclass
class _Design:
    grounds: set[str]
    rails: dict[str, list[str]]  # rail -> evidence ('name', 'ic_pin')
    ics: set[str]  # fitted ICs
    not_fitted_ics: set[str]
    capacitors: set[str]  # every capacitor part
    pairs: list[_Pair]
    caps: dict[str, list[_Cap]]  # rail -> capacitors to ground
    between: dict[str, list[_Cap]]  # net -> capacitors to another non-ground net
    filters: dict[str, list[tuple[str, str]]]  # net -> fitted (filter, the other net)
    links: dict[str, list[tuple[str, str, float]]]  # net -> fitted (link, other net, ohms)
    ic_nets: dict[str, set[str]]
    bootstrap_nets: dict[str, set[str]]  # IC -> nets of its BST/SW/LX/out pins
    pin_to_net: dict[str, dict[str, str]]
    fitted: dict[str, bool]


@dataclass
class NetClasses:
    """The compiled design's nets, read the way the decoupling check reads them."""
    refs: list[str]  # compiled parts with a connected pin
    nets: dict[str, list[tuple[str, str]]]  # net -> (part, pin)
    kinds: dict[str, dict]  # net -> net_kind
    roles: dict[str, list[str]]  # net -> pin_role of each IC pin on it
    ics: set[str]  # U and IC parts, fitted or not
    grounds: set[str]
    # Nets named like power or reaching an IC supply or capacitor pin, with that
    # evidence ('name', 'ic_pin'); never a ground or a net with a status token (EN, PG).
    rails: dict[str, list[str]]


def classify_nets(netlist: dict) -> NetClasses:
    """Grounds and rails of the compiled netlist: a net is a ground by its name, or by a
    strict majority of IC ground pins with no output pin on it; a rail by its name or an
    IC supply or capacitor pin, unless it carries EN, PG, SENSE and the like."""
    components = netlist.get("components") or {}
    compiled = netlist.get("parts") or {}
    pin_to_net = netlist.get("pin_to_net") or {}
    names = set(compiled) if compiled else {
        name for name, meta in components.items() if not meta.get("compile_masked")
    }
    refs = [name for name in pin_to_net if name in names]
    nets: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for ref in refs:
        for pin, net in pin_to_net[ref].items():
            nets[net].append((ref, pin))

    def pin_name(ref: str, pin: str) -> str:
        return (((components.get(ref) or {}).get("pins") or {}).get(pin) or {}).get("name") or ""

    all_ics = {ref for ref in refs if designator_prefix(ref) in IC_PREFIXES}
    kinds = {net: net_kind(net) for net in nets}
    roles = {net: [pin_role(pin_name(ref, pin)) for ref, pin in members if ref in all_ics]
             for net, members in nets.items()}
    grounds = set()
    for net in nets:
        named = [role for role in roles[net] if role not in ("none", "other")]
        # A strict majority of ground pins, and no output on it (a charge pump's VOUT
        # meeting a VSS pin is a negative rail).
        by_pins = named.count("ground") * 2 > len(named) and "out" not in named
        if (kinds[net]["ground"] and not kinds[net]["signal"]) or (
            by_pins and not kinds[net]["power"]
        ):
            grounds.add(net)
    rails: dict[str, list[str]] = {}
    for net in nets:
        if net in grounds or kinds[net]["status"]:
            continue
        evidence = ["name"] if kinds[net]["power"] and not kinds[net]["signal"] else []
        if set(roles[net]) & {"supply", "cap"}:
            evidence.append("ic_pin")
        if evidence:
            rails[net] = evidence
    return NetClasses(refs, dict(nets), kinds, roles, all_ics, grounds, rails)


def decoupling_design(netlist: dict, plan, project_params: dict | None) -> _Design:
    """The check's reading of the design: grounds, rails with their evidence, fitted ICs,
    the (IC, rail) pairs with the IC's pins on the rail, and per rail the capacitors to
    ground, filters and links (get_constraints_brief places capacitors from it)."""
    components = netlist.get("components") or {}
    compiled = netlist.get("parts") or {}
    pin_to_net = netlist.get("pin_to_net") or {}
    classes = classify_nets(netlist)
    refs, nets, kinds, grounds = classes.refs, classes.nets, classes.kinds, classes.grounds
    all_ics = classes.ics

    def pin_name(ref: str, pin: str) -> str:
        return (((components.get(ref) or {}).get("pins") or {}).get(pin) or {}).get("name") or ""

    parts = {part.designator: part for part in build_parts(netlist, plan, project_params)[0]}
    not_fitted = getattr(plan, "not_fitted", set()) or set()
    fitted = {ref: parts[ref].fitted if ref in parts else ref not in not_fitted for ref in refs}

    caps: dict[str, list[_Cap]] = defaultdict(list)
    between: dict[str, list[_Cap]] = defaultdict(list)
    filters: dict[str, list[tuple[str, str]]] = defaultdict(list)
    links: dict[str, list[tuple[str, str, float]]] = defaultdict(list)
    capacitors: set[str] = set()
    for ref in refs:
        connected = pin_to_net[ref]
        if len(connected) < 2:
            continue
        meta = components.get(ref) or {}
        part = parts.get(ref)
        varied = (getattr(plan, "params", {}) or {}).get(ref) or {}
        params = {**(meta.get("params") or {}), **{key: value for key, (_, value) in varied.items()}}
        text = " ".join(filter(None, (
            meta.get("description"), meta.get("lib_reference"),
            (compiled.get(ref) or {}).get("comment"), meta.get("value"),
            params.get("capacitance"),
        )))
        prefix = designator_prefix(ref)
        part_nets = list(dict.fromkeys(connected.values()))
        is_filter = prefix in _FILTER_PREFIXES or (
            FERRITE_TEXT.search(text) and prefix not in IC_PREFIXES
        )
        if is_filter:
            if len(connected) == 2 and len(part_nets) == 2 and fitted[ref]:
                net_a, net_b = part_nets
                if prefix != "R":
                    filters[net_a].append((ref, net_b))
                    filters[net_b].append((ref, net_a))
                    continue
                # A resistor: a link up to LINK_OHMS, a supply filter up to FILTER_OHMS;
                # pull-ups, dividers and unreadable values join nothing.
                ohms = normalize_value(part.value or part.comment) if part else None
                if isinstance(ohms, float) and ohms <= LINK_OHMS:
                    links[net_a].append((ref, net_b, ohms))
                    links[net_b].append((ref, net_a, ohms))
                elif isinstance(ohms, float) and ohms <= FILTER_OHMS:
                    filters[net_a].append((ref, net_b))
                    filters[net_b].append((ref, net_a))
            continue
        if not is_capacitor(prefix, text) or len(part_nets) < 2:
            continue
        capacitors.add(ref)
        farads = capacitance(
            part.value if part else params.get("value"), params.get("capacitance"),
            part.comment if part else (compiled.get(ref) or {}).get("comment"),
            part.description if part else meta.get("description"),
        )
        returns = [net for net in part_nets if net in grounds]
        live = [net for net in part_nets if net not in grounds]
        if returns:
            # Two-pin capacitors, and arrays or feed-throughs with a ground pin.
            for net in live:
                caps[net].append(_Cap(ref, net, returns[0], True, farads, fitted[ref]))
        elif len(live) == 2:
            net_a, net_b = live
            between[net_a].append(_Cap(ref, net_a, net_b, False, farads, fitted[ref]))
            between[net_b].append(_Cap(ref, net_b, net_a, False, farads, fitted[ref]))

    rails: dict[str, list[str]] = {}
    for net in nets:
        if net in grounds or kinds[net]["status"]:
            continue
        evidence = list(classes.rails.get(net, []))
        if "ic_pin" not in evidence and "ref" in classes.roles[net] and caps.get(net):
            # A reference pin alone (a TL431 divider node) makes a rail only with a
            # capacitor to ground.
            evidence.append("ref_pin")
        if evidence:
            rails[net] = evidence

    ics = {ic for ic in all_ics if fitted[ic]}
    pairs: dict[tuple[str, str], _Pair] = {}
    ic_nets: dict[str, set[str]] = defaultdict(set)
    bootstrap: dict[str, set[str]] = defaultdict(set)
    for ic in sorted(all_ics, key=natural_key):
        pins = pin_to_net[ic]
        roles = {pin: pin_role(pin_name(ic, pin)) for pin in pins}
        unnamed = all(role == "none" for role in roles.values())
        for pin, net in sorted(pins.items(), key=lambda item: natural_key(item[0])):
            ic_nets[ic].add(net)
            role = roles[pin]
            if role == "out" or _PIN_BOOTSTRAP.match(first_function(pin_name(ic, pin))):
                bootstrap[ic].add(net)
            if ic not in ics or net not in rails:
                continue
            named_rail = "name" in rails[net]
            if role in _PAIR_ROLES or (role in ("in", "ground") and named_rail) or (
                unnamed and named_rail
            ):
                # 'ground' here is a VSS pin on a negative rail named like power (-15V).
                pairs.setdefault((ic, net), _Pair(ic, net)).pins.append(pin)
    return _Design(
        grounds, rails, ics, all_ics - ics, capacitors, list(pairs.values()), caps, between,
        filters, links, ic_nets, bootstrap, pin_to_net, fitted,
    )


def _ohms_text(ohms: float) -> str:
    return "0 ohm" if ohms == 0 else f"{ohms * 1000:g} mOhm"


def _linked_caps(design: _Design, rail: str) -> list[tuple[str, str, list[_Cap]]]:
    """Fitted capacitors on nets joined to the rail by a link: ('R1 (0 ohm)', net, caps)."""
    out = []
    for part, other, ohms in design.links.get(rail, []):
        if other in design.grounds:
            continue
        found = [cap for cap in design.caps.get(other, []) if cap.fitted]
        if found:
            out.append((f"{part} ({_ohms_text(ohms)})", other, found))
    return out


def _feeders(design: _Design, rail: str) -> list[tuple[str, str]]:
    """Fitted filters (ferrite, inductor, resistor) joining the rail to a rail that has
    fitted capacitors. Which side feeds which is not known."""
    return [
        (part, other) for part, other in design.filters.get(rail, [])
        if other not in design.grounds and other in design.rails
        and design.rails[other] != ["ref_pin"]  # a reference divider node feeds nothing
        and any(cap.fitted for cap in design.caps.get(other, []))
    ]


# ---------- findings ----------

def _finding(check: str, issue: str, designators=(), **fields) -> dict:
    out = {"check": check, "severity": _SEVERITY.get(check, "info")}
    if designators:
        out["designators"] = sorted(set(designators), key=natural_key)
    out.update({key: value for key, value in fields.items() if value is not None})
    out["issue"] = issue
    return out


def _names(items) -> str:
    items = sorted(set(items), key=natural_key)
    return ", ".join(items[:8]) + (f" and {len(items) - 8} more" if len(items) > 8 else "")


def _schematic_findings(design: _Design, pairs: list[_Pair], variant: str) -> list[dict]:
    findings: list[dict] = []
    by_rail: dict[str, list[_Pair]] = defaultdict(list)
    for pair in pairs:
        by_rail[pair.rail].append(pair)
    unknown: set[str] = set()
    for rail, rail_pairs in sorted(by_rail.items(), key=lambda item: natural_key(item[0])):
        ics = [pair.ic for pair in rail_pairs]
        pins = {pair.ic: pair.pins for pair in rail_pairs}
        shown = _names(ics)
        caps = design.caps.get(rail, [])
        own = [cap for cap in caps if cap.fitted]
        linked = _linked_caps(design, rail)
        fitted = own + [cap for _, _, found in linked for cap in found]
        unknown.update(cap.designator for cap in own if cap.farads is None)
        if not own and linked:
            joined = "; ".join(f"{part} to {other}, with "
                               f"{_names(c.designator for c in found)}"
                               for part, other, found in linked[:4])
            findings.append(_finding(
                "capacitor_elsewhere",
                f"{rail} ({shown}) has no capacitor of its own; it is joined through "
                f"{joined}, which decouple it.",
                [*ics, *(part.split(" ")[0] for part, _, _ in linked)], rail=rail, ics=ics,
            ))
        if fitted:
            if all(cap.farads is not None and not cap.small() for cap in fitted):
                findings.append(_finding(
                    "no_small_capacitor",
                    f"{rail} ({shown}) has only capacitors of 1 µF or more "
                    f"({_names(c.designator for c in fitted)}); MCU and logic guides usually "
                    "add a 100 nF ceramic per power pin, while many regulators ask only for "
                    "1-10 µF: check the datasheet.",
                    [*ics, *(cap.designator for cap in fitted)], rail=rail, ics=ics,
                ))
            elif not _feeders(design, rail) and all(
                cap.farads is not None and cap.small() for cap in fitted
            ):
                findings.append(_finding(
                    "no_bulk_capacitor",
                    f"{rail} ({shown}) has only capacitors under 1 µF; guides put a bulk "
                    "capacitor (1-10 µF or more) per rail or where it enters the board, "
                    "which may be elsewhere on this rail's source.",
                    [*ics, *(cap.designator for cap in fitted)], rail=rail, ics=ics,
                ))
            continue
        absent = [cap for cap in caps if not cap.fitted]
        if absent:
            findings.append(_finding(
                "not_fitted_only",
                f"{rail} ({shown}) is decoupled only by "
                f"{_names(c.designator for c in absent)}, which "
                f"{'is' if len(absent) == 1 else 'are'} Not Fitted in variant {variant}.",
                [*ics, *(cap.designator for cap in absent)], rail=rail, ics=ics,
                pins=pins,
            ))
            continue
        elsewhere = [
            cap for cap in design.between.get(rail, [])
            if cap.fitted and (cap.other in design.rails
                               or any(cap.other in design.bootstrap_nets[ic] for ic in ics))
        ]
        feeders = _feeders(design, rail)
        if elsewhere:
            targets = ", ".join(
                f"{cap.designator} to {cap.other}" for cap in
                sorted(elsewhere, key=lambda cap: natural_key(cap.designator))[:6]
            )
            findings.append(_finding(
                "capacitor_elsewhere",
                f"{rail} ({shown}) has no capacitor to ground, only to other nets: "
                f"{targets} (a bootstrap capacitor, or decoupling to another rail). Check "
                "that this is what the datasheet asks for.",
                [*ics, *(cap.designator for cap in elsewhere)], rail=rail, ics=ics,
            ))
        elif feeders:
            joined = "; ".join(f"{part} to {other}" for part, other in feeders[:4])
            findings.append(_finding(
                "filtered_rail",
                f"{rail} ({shown}) has no capacitor of its own; it is joined through "
                f"{joined}, whose capacitors sit on the far side of the ferrite, inductor "
                "or resistor, so these pins get no local decoupling.",
                [*ics, *(part for part, _ in feeders)], rail=rail, ics=ics, pins=pins,
            ))
        else:
            findings.append(_finding(
                "no_capacitor",
                f"{rail} ({shown}) has no capacitor to ground in variant {variant}: "
                "every IC power pin usually wants one, close to the pin.",
                ics, rail=rail, ics=ics, pins=pins,
            ))
    if unknown:
        findings.append(_finding(
            "unknown_value",
            f"The value of {len(unknown)} decoupling capacitors could not be read "
            "(Value, Capacitance, Comment, description), so they are not counted as small "
            "or bulk.",
            unknown,
        ))
    return findings


# ---------- PCB ----------

def pcb_components(index) -> dict[str, list[dict]]:
    """The PCB's component rows by schematic designator (several when the board holds
    more than one copy of a part)."""
    by_name: dict[str, list[dict]] = defaultdict(list)
    for row in index.db.execute("SELECT * FROM components"):
        if row["sch_refdes"]:
            by_name[row["sch_refdes"]].append(dict(row))
    return by_name


def component_pads(index, by_name: dict[str, list[dict]], wanted) -> dict[int, list]:
    """Pad rows (with net_name) of the named parts, by PCB component id."""
    ids = [row["id"] for name in wanted for row in by_name.get(name, [])]
    pads: dict[int, list] = defaultdict(list)
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        rows = index.db.execute(
            "SELECT p.*, n.name AS net_name FROM prims p LEFT JOIN nets n ON n.id = p.net_id "
            f"WHERE p.kind = 'pad' AND p.component IN ({','.join('?' * len(chunk))})", chunk,
        )
        for row in rows:
            pads[row["component"]].append(row)
    return pads


def _pcb_findings(index, design: _Design, pairs: list[_Pair], max_mil: float,
                  stats: dict, detail: bool) -> tuple[list[dict], list[dict]]:
    """Findings, and one measured row per pair."""
    db = index.db
    by_name = pcb_components(index)
    candidates_of: dict[str, list[_Cap]] = {}
    wanted = {pair.ic for pair in pairs}
    for pair in pairs:
        if pair.rail not in candidates_of:
            candidates_of[pair.rail] = [
                *design.caps.get(pair.rail, []),
                *(cap for _, _, found in _linked_caps(design, pair.rail) for cap in found),
            ]
        wanted.update(cap.designator for cap in candidates_of[pair.rail])
    pads = component_pads(index, by_name, wanted)
    links: dict[str, tuple[list, list]] = {}

    def links_of(net: str) -> tuple[list, list]:
        """Same-net vias and through-hole pads, and the net's pour outlines
        (not shelved ones: they have no copper)."""
        if net not in links:
            net_id = index.net_id(net)
            vias = list(db.execute(
                "SELECT * FROM prims WHERE net_id = ? AND (kind = 'via' OR "
                "(kind = 'pad' AND layer = ?))", (net_id, THT_LAYER),
            )) if net_id is not None else []
            pours = [dict(row) for row in db.execute(
                "SELECT layer, vertices FROM pours WHERE net_id = ? AND NOT shelved",
                (net_id,),
            )] if net_id is not None else []
            links[net] = (vias, pours)
        return links[net]

    def describe(item) -> dict:
        distance, cap, _, row = item
        return {**cap.describe(), "distance_mil": round(distance, 1), "side": row["side"]}

    findings: list[dict] = []
    rows_out: list[dict] = []
    missing: set[str] = set()
    distances: list[float] = []
    for pair in pairs:
        ic_rows = by_name.get(pair.ic) or []
        if not ic_rows:
            missing.add(pair.ic)
            continue
        on_rail = [(pad, row) for row in ic_rows for pad in pads[row["id"]]
                   if pad["net_name"] == pair.rail]
        power = [item for item in on_rail if item[0]["pad_name"] in pair.pins] or on_rail
        if not power:
            stats["net_differs"] += 1
            continue
        candidates = []  # (cap, pad, component row)
        for cap in candidates_of[pair.rail]:
            rows = by_name.get(cap.designator) or []
            if not rows and cap.fitted:
                missing.add(cap.designator)
            for row in rows:
                candidates.extend((cap, pad, row) for pad in pads[row["id"]]
                                  if pad["net_name"] == cap.rail)
        fitted = [item for item in candidates if item[0].fitted]
        if not fitted:
            continue

        def ranked(pad, items) -> list[tuple]:
            return sorted(((index._primitive_distance(pad, cap_pad), cap, cap_pad, row)
                           for cap, cap_pad, row in items), key=lambda item: item[0])

        def nearest(pad, items) -> tuple:
            return ranked(pad, items)[0]

        per_pad = [nearest(pad, fitted) for pad, _ in power]
        best = min(per_pad, key=lambda item: item[0])
        distance, cap, cap_pad, cap_row = best
        ic_side = ic_rows[0]["side"]
        stats["pairs_measured"] += 1
        distances.append(distance)
        measured = {"ic": pair.ic, "rail": pair.rail, "pins": pair.pins, "nearest": describe(best)}
        small = [item for item in fitted if item[0].small()]
        if small and best[1].farads is not None and not best[1].small():
            measured["nearest_under_1uF"] = describe(
                min((nearest(pad, small) for pad, _ in power), key=lambda item: item[0])
            )
        if detail:
            seen: dict[str, tuple] = {}
            for pad, _ in power:
                for item in ranked(pad, fitted):
                    if item[1].designator not in seen or item[0] < seen[item[1].designator][0]:
                        seen[item[1].designator] = item
            measured["capacitors"] = [
                describe(item) for item in sorted(seen.values(), key=lambda item: item[0])
            ][:DETAIL_CAPACITORS]
        rows_out.append(measured)
        absent = [item for item in candidates if not item[0].fitted]
        closer = None
        if absent:
            gap, absent_cap, _, _ = min(
                (nearest(pad, absent) for pad, _ in power), key=lambda item: item[0],
            )
            if gap < distance:
                closer = {"designator": absent_cap.designator, "distance_mil": round(gap, 1)}
        if distance > max_mil:
            text = (
                f"{pair.ic} on {pair.rail}: the nearest capacitor is {cap.designator} "
                f"({farads_text(cap.farads) or 'value unknown'}), "
                f"{distance / MIL_PER_MM:.1f} mm from its nearest power pad (straight line, "
                "pad edge to pad edge). Vendor guides ask for 'as close as possible', often "
                "a few mm; on boards with closely spaced planes the via connection matters "
                "more than the distance."
            )
            if closer:
                text += f" {closer['designator']} would be nearer but is Not Fitted."
            findings.append(_finding(
                "far_capacitor", text, [pair.ic, cap.designator], ic=pair.ic,
                rail=pair.rail, nearest=measured["nearest"], ic_side=ic_side,
                not_fitted_nearer=closer,
            ))
        elif len(power) > 1:
            far = [item for item in per_pad if item[0] > max_mil]
            if far:
                worst = max(far, key=lambda item: item[0])
                findings.append(_finding(
                    "pads_far",
                    f"{pair.ic} on {pair.rail}: {len(far)} of {len(power)} power pads have "
                    f"no capacitor within {max_mil / MIL_PER_MM:.1f} mm (the farthest "
                    f"{worst[0] / MIL_PER_MM:.1f} mm, to {worst[1].designator}). Large parts "
                    "often share capacitors between pads; check the datasheet's per-pin "
                    "advice.",
                    [pair.ic], ic=pair.ic, rail=pair.rail, pads_far=len(far),
                    pads=len(power), farthest_mil=round(worst[0], 1),
                ))
        if cap_row["side"] != ic_side and cap_pad["layer"] != THT_LAYER:
            vias, pours = links_of(cap.rail)
            via_near = any(
                via["id"] != cap_pad["id"]
                and index._primitive_distance(cap_pad, via) <= VIA_NEAR_MIL
                for via in vias
            )
            poured = any(
                pour["layer"] == cap_pad["layer"]
                and point_in_polygon(cap_pad["x1"], cap_pad["y1"], json.loads(pour["vertices"]))
                for pour in pours
            )
            if not via_near and not poured:
                findings.append(_finding(
                    "opposite_side_no_via",
                    f"{cap.designator}, the nearest capacitor to {pair.ic} on {pair.rail}, "
                    f"is on the other side ({cap_row['side']}) and no {cap.rail} via was "
                    "found within 2 mm of its pad, nor a pour around it (regions and planes "
                    "are not read, so the connection may still be there).",
                    [pair.ic, cap.designator], ic=pair.ic, rail=pair.rail,
                    nearest=measured["nearest"],
                ))
    if distances:
        stats["within_mm"] = {
            f"{mm}": sum(1 for value in distances if value <= mm * MIL_PER_MM)
            for mm in (1, 3, 5, 10)
        }
    if missing:
        stats["not_on_pcb"] = sorted(missing, key=natural_key)[:30]
        stats["not_on_pcb_count"] = len(missing)
    return findings, rows_out


# ---------- entry point ----------

def _match(requested: list[str] | None, available) -> tuple[set[str] | None, list[str]]:
    if not requested:
        return None, []
    folded = {name.casefold(): name for name in available}
    found = {folded[name.strip().casefold()] for name in requested
             if name.strip().casefold() in folded}
    unknown = [name for name in requested if name.strip().casefold() not in folded]
    return found, unknown


def run_decoupling_check(
    netlist: dict,
    plan=None,
    project_params: dict | None = None,
    index=None,
    pcb_unavailable: str | None = None,
    checks: list[str] | None = None,
    ics: list[str] | None = None,
    rails: list[str] | None = None,
    max_distance_mil: float = DEFAULT_MAX_DISTANCE_MIL,
    offset: int = 0,
    limit: int = FINDING_LIMIT,
    rail_limit: int = RAIL_LIMIT,
) -> dict:
    requested = [check.strip().lower() for check in checks or [] if check.strip()]
    invalid = [check for check in requested if check not in CHECKS]
    if invalid:
        return {"error": "invalid_checks",
                "message": f"Unknown checks {invalid}; use {list(CHECKS)}."}
    design = decoupling_design(netlist, plan, project_params)
    pairs = design.pairs
    notes: list[str] = []
    variant = getattr(plan, "name", None) or "[No Variations]"
    wanted_ics, unknown_ics = _match(ics, design.pin_to_net)
    if wanted_ics is not None:
        named_caps = wanted_ics & design.capacitors
        if named_caps:
            if named_caps == wanted_ics:
                return {"error": "not_an_ic",
                        "message": f"{_names(named_caps)} is a capacitor: name the IC it "
                        "decouples; its nearest capacitors are listed with their distances."}
            notes.append(f"{_names(named_caps)}: capacitors, not checked as ICs.")
            wanted_ics -= named_caps
    if ics and not wanted_ics:
        return {"error": "ic_not_found",
                "message": f"No part {unknown_ics} in the compiled netlist."}
    if wanted_ics is not None:
        for part in sorted(wanted_ics, key=natural_key):
            if part in design.not_fitted_ics or not design.fitted.get(part, True):
                continue
            if any(pair.ic == part for pair in pairs):
                continue
            # A part named explicitly and paired with nothing (a connector, a module, an
            # IC with unusual pin names) is checked on every rail named like power.
            added = {}
            for pin, net in design.pin_to_net.get(part, {}).items():
                design.ic_nets[part].add(net)
                if net in design.rails and "name" in design.rails[net]:
                    added.setdefault(net, _Pair(part, net)).pins.append(pin)
            pairs = [*pairs, *added.values()]
        pairs = [pair for pair in pairs if pair.ic in wanted_ics]
        for part in sorted(wanted_ics, key=natural_key):
            if part in design.not_fitted_ics or not design.fitted.get(part, True):
                notes.append(f"{part} is Not Fitted in variant {variant}: not checked.")
            elif not any(pair.ic == part for pair in pairs):
                notes.append(f"{part}: no pin on a power rail was recognised.")
    wanted_rails, unknown_rails = _match(rails, design.rails)
    if rails and not wanted_rails:
        return {"error": "rail_not_found",
                "message": f"No power rail {unknown_rails}; rails are named like power or "
                "reach an IC supply pin."}
    if wanted_rails is not None:
        pairs = [pair for pair in pairs if pair.rail in wanted_rails]
        for rail in sorted(wanted_rails - {pair.rail for pair in pairs}, key=natural_key):
            whose = f"of {_names(wanted_ics)}" if wanted_ics is not None else "of a fitted IC"
            notes.append(f"{rail}: no power pin {whose} on it.")
    if unknown_ics or unknown_rails:
        notes.append(f"Not found: {', '.join([*unknown_ics, *unknown_rails])}.")
    if design.not_fitted_ics and wanted_ics is None:
        notes.append(f"{len(design.not_fitted_ics)} ICs Not Fitted in variant {variant} are "
                     "not checked.")
    findings = _schematic_findings(design, pairs, variant)
    stats: dict = defaultdict(int)
    not_checked: dict[str, str] = {}
    measured: list[dict] = []
    if index is not None:
        pcb_findings, measured = _pcb_findings(index, design, pairs, max_distance_mil, stats,
                                               detail=wanted_ics is not None)
        findings += pcb_findings
    else:
        for check in CHECKS:
            if check in PCB_CHECKS:
                not_checked[check] = pcb_unavailable or "no PcbDoc"
    wanted = set(requested or set(CHECKS) - OPT_IN_CHECKS)
    findings = [finding for finding in findings if finding["check"] in wanted]
    findings.sort(key=lambda finding: (
        _SEVERITY_RANK[finding["severity"]], _CHECK_RANK[finding["check"]],
        natural_key(finding.get("rail") or ""), natural_key(finding.get("ic") or ""),
    ))
    rail_rows = _rail_rows(design, pairs)
    rail_names = {pair.rail for pair in pairs}
    overview = {
        "ics": len({pair.ic for pair in pairs}),
        "rails": len(rail_rows),
        "ic_rail_pairs": len(pairs),
        "decoupling_capacitors": len({cap.designator for rail in rail_names
                                      for cap in design.caps.get(rail, []) if cap.fitted}),
        "not_fitted_capacitors": len({cap.designator for rail in rail_names
                                      for cap in design.caps.get(rail, []) if not cap.fitted}),
    }
    severities = {level: sum(1 for f in findings if f["severity"] == level)
                  for level in ("warning", "info")}
    summary: dict[str, int] = defaultdict(int)
    for finding in findings:
        summary[finding["check"]] += 1
    offset = max(0, int(offset or 0))
    result: dict = {
        "variant": variant,
        "checks_run": [check for check in CHECKS
                       if check in wanted and check not in not_checked],
        "overview": overview,
        "summary": dict(summary),
        "severity_counts": severities,
    }
    if index is not None:
        result["pcb"] = {
            "pairs_measured": stats["pairs_measured"],
            "within_mm": stats.get("within_mm", {}),
            "max_distance_mil": round(max_distance_mil, 3),
            "measured": "straight line, pad edge to pad edge, from each IC power pad to "
                        "the nearest fitted capacitor on its rail",
        }
        for key in ("not_on_pcb", "not_on_pcb_count"):
            if key in stats:
                result["pcb"][key] = stats[key]
        if stats["net_differs"]:
            result["pcb"]["pairs_with_other_net_on_pcb"] = stats["net_differs"]
            notes.append("Some IC power pins have another net on the PCB than in the "
                         "netlist: run check_sch_pcb_sync.")
        result["pairs_total"] = len(measured)
        result["pairs"] = measured[:rail_limit]
    result["rails_total"] = len(rail_rows)
    result["rails"] = rail_rows[:rail_limit]
    result.update({
        "findings": findings[offset:offset + limit],
        "total": len(findings),
        "offset": offset,
        "has_more": offset + limit < len(findings),
    })
    if result["has_more"]:
        result["next_offset"] = offset + limit
    if not_checked:
        result["not_checked"] = not_checked
    notes.append(
        "Rails are nets named like power or reaching an IC supply, reference or capacitor "
        "pin; ICs are U and IC parts (pass ic= for others, e.g. a connector). How many "
        "capacitors a part needs is in its datasheet, not checked here."
    )
    result["notes"] = notes
    return result


def _rail_rows(design: _Design, pairs: list[_Pair]) -> list[dict]:
    by_rail: dict[str, list[_Pair]] = defaultdict(list)
    for pair in pairs:
        by_rail[pair.rail].append(pair)
    rows = []
    for rail, rail_pairs in by_rail.items():
        caps = design.caps.get(rail, [])
        fitted = [cap for cap in caps if cap.fitted]
        row: dict = {
            "rail": rail,
            "evidence": design.rails.get(rail, []),
            "ics": sorted({pair.ic for pair in rail_pairs}, key=natural_key),
            "power_pins": sum(len(pair.pins) for pair in rail_pairs),
            "capacitors": len(fitted),
            "under_1uF": sum(1 for cap in fitted if cap.small()),
            "from_1uF": sum(1 for cap in fitted if cap.farads is not None and not cap.small()),
        }
        total = sum(cap.farads for cap in fitted if cap.farads is not None)
        if total:
            row["total_uF"] = round(total * 1e6, 3)
        if unknown := sum(1 for cap in fitted if cap.farads is None):
            row["value_unknown"] = unknown
        if absent := [cap.designator for cap in caps if not cap.fitted]:
            row["not_fitted"] = sorted(absent, key=natural_key)
        if feeders := _feeders(design, rail):
            row["filtered_to"] = [f"{part} to {other}" for part, other in feeders[:4]]
        if linked := _linked_caps(design, rail):
            row["linked_to"] = [f"{part} to {other}" for part, other, _ in linked[:4]]
        rows.append(row)
    rows.sort(key=lambda row: (-row["power_pins"], natural_key(row["rail"])))
    return rows
