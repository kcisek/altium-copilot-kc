"""Differential-pair consistency checks: naming, pin polarity, series parts, skew.

Pure logic over a PcbIndex and the enriched netlist; no MCP. The PCB's
DifferentialPairs6 records are the pairs checked. Schematic pin names come
from the netlist (netlist["components"][refdes]["pins"][pin]["name"]), joined
to PCB pads through the index's sch_refdes mapping.

Checks:
- naming: nets named like a pair (X_P / X_N, D+ / D-, CK_T / CK_C...) that are
  not a PCB pair, and PCB pairs whose names are inverted, unconventional,
  missing from the board or sharing a net with another pair.
- polarity: on every component touching both legs, complementary pin names
  (TXP / TXN) tell whether the positive net lands on the positive pin; shunt
  protection parts are skipped and pass-through parts (chokes, muxes) are
  judged across their two sides.
- series: two-pin parts in series with each leg (AC-coupling caps, series
  resistors) paired and compared across the legs, shunts (pull-ups, bias)
  told apart, and the coupled side's pairing.
- skew: in-pair length skew against MatchedLengths rules with "Within
  Differential Pair Length" set, with the same verdict as check_lengths.

Every heuristic was calibrated on the Altium example boards and a corpus of
user boards (no false warnings on the example boards). Findings carry a
severity: "warning" (likely a defect, verify it) or "info" (worth knowing,
often fine).
"""

import math
import re
from collections import Counter, defaultdict
from dataclasses import replace
from itertools import combinations

try:
    from services.pcb_length import MARGINAL_MIL
    from services.pcb_rules import (
        ScopeSyntaxError,
        evaluate,
        evaluate_scope,
        parse_scope,
        rule_values,
        stale_references,
    )
except ImportError:
    from server.services.pcb_length import MARGINAL_MIL
    from server.services.pcb_rules import (
        ScopeSyntaxError,
        evaluate,
        evaluate_scope,
        parse_scope,
        rule_values,
        stale_references,
    )

CHECKS = ("naming", "polarity", "series", "skew")

# Checks designed but not implemented yet: name -> reason reported under
# not_checked when they are requested (or when every check runs).
DEFERRED_CHECKS = {
    "directives": (
        "Schematic Differential Pair directives are not compared with the PCB pairs "
        "here: check_directive_sync compares them (and net class and rule directives)."
    ),
}

DEFAULT_LIMIT = 100

# A far net with more pads than this is a rail or bus, not a coupled leg; a
# pair-named net with more pads is a rail, not an undeclared pair.
RAIL_FANOUT = 12

# A one-leg resistor of at least this value is a pull-up or bias, not a
# series part, when the topology cannot tell.
BIAS_OHMS = 1000.0

# Skew over a tolerance by at most MARGINAL_MIL (the threshold check_lengths
# uses) is marked marginal: within the length model's own uncertainty, so
# inconclusive (info) rather than a violation (warning).

# Coupled-side stubs shorter than this between auto-named nets (oscillator or
# SMA test points) are not reported as undeclared pairs.
SHORT_STUB_MIL = 500.0

_SEVERITY_RANK = {"error": 0, "warning": 1, "info": 2}
_CHECK_RANK = {name: rank for rank, name in enumerate(CHECKS)}

# ---------- polarity tokens ----------

_TOKEN_RE = re.compile(r"PLUS|MINUS|POS|NEG|[PNTCHLM+\-]")
_POSITIVE_TOKENS = frozenset({"P", "+", "T", "H", "POS", "PLUS"})
_STYLES = {
    ("P", "N"): "P/N",
    ("+", "-"): "+/-",
    ("T", "C"): "T/C",
    ("H", "L"): "H/L",
    ("P", "M"): "P/M",
    ("POS", "NEG"): "POS/NEG",
    ("PLUS", "MINUS"): "PLUS/MINUS",
}
# Undeclared candidates in these styles are almost always real pairs.
_WARNING_STYLES = frozenset({"P/N", "T/C"})
_UNICODE_MINUS = chr(0x2212)


def _normalise_name(name: str | None) -> str:
    """Upper case, without overbar escapes ("R\\E\\S\\E\\T\\") or Unicode minus."""
    return (name or "").replace("\\", "").replace(_UNICODE_MINUS, "-").strip().upper()


def polarity_tokens(name: str | None) -> list[tuple[str, str, str]]:
    """(key, polarity, token) for each polarity token in a net or pin name.

    key is the upper-case name with the token replaced by "{}"; polarity is
    "+" or "-". A letter token must be followed by the end, "_", "#" or a
    digit (so TXP, RX0P, IO_L01P_0 qualify). T and C count only after "_";
    H and L only after "_" or "CAN"; M only after "D" or "_" (DP / DM).
    """
    upper = _normalise_name(name)
    tokens: list[tuple[str, str, str]] = []
    for match in _TOKEN_RE.finditer(upper):
        token = match.group()
        start, end = match.start(), match.end()
        after = upper[end : end + 1]
        if token not in ("+", "-") and after and not (after in "_#" or after.isdigit()):
            continue
        before = upper[start - 1] if start > 0 else ""
        if token in ("T", "C") and before != "_":
            continue
        if token in ("H", "L") and before != "_" and not upper[:start].endswith("CAN"):
            continue
        if token == "M" and before not in ("_", "D"):
            continue
        polarity = "+" if token in _POSITIVE_TOKENS else "-"
        tokens.append((upper[:start] + "{}" + upper[end:], polarity, token))
    return tokens


def complement(first: str | None, second: str | None) -> tuple[str, str] | None:
    """(polarity of first, style) when the names differ by one polarity pair.

    TXP / TXN -> ("+", "P/N"); DM / DP -> ("-", "P/M"); RESET / RESETN,
    CS / CSN, SDA / SCL and a name compared with itself -> None.
    """
    if not first or not second:
        return None
    by_key: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for key, polarity, token in polarity_tokens(first):
        by_key[key].append((polarity, token))
    for key, polarity, token in polarity_tokens(second):
        for first_polarity, first_token in by_key.get(key, []):
            if first_polarity == polarity:
                continue
            positive, negative = (
                (first_token, token) if first_polarity == "+" else (token, first_token)
            )
            style = _STYLES.get((positive, negative))
            if style:
                return first_polarity, style
    return None


# ---------- name classes ----------

# Altium names unnamed nets Net<designator>_<pin> (NetC544_2, NetBT1_P).
_AUTO_NET_RE = re.compile(r"^Net[A-Za-z]+\d*_\w+$")
_SUPPLY_RE = re.compile(
    r"GND|VCC|VDD|VSS|VEE|BAT|VIN|VOUT|VBUS|PWR|SUPPLY|VREF|VPP|^V[+-]$"
    r"|^[+-]?\d+(?:[.,]\d+)?V|\dV\d",
    re.IGNORECASE,
)
_SENSE_RE = re.compile(r"SENSE|SNS", re.IGNORECASE)


# Pair-named nets that feed loads or actuators: worth knowing, not a defect.
_LOAD_RE = re.compile(
    r"(?<![A-Z])(?:MOTOR|MOT|SPKR?|SPEAKER|LEDS?|HEAT(?:ER)?|TEC|COIL|SOL(?:ENOID)?"
    r"|BUZZ(?:ER)?|PIEZO|LAMP|FAN|RELAY|VALVE|DC_?LINK|DC)(?![A-Z])"
)
# Nets a protection part may shunt to besides supplies.
_SHUNT_RETURN_RE = re.compile(r"SHIELD|SHLD|CHASSIS|EARTH", re.IGNORECASE)


def is_auto_net_name(name: str | None) -> bool:
    return bool(name and _AUTO_NET_RE.match(name))


def is_supply_name(name: str | None) -> bool:
    return bool(name and _SUPPLY_RE.search(name))


def _suppressed_candidate(name: str) -> str | None:
    if is_auto_net_name(name):
        return "auto_named"
    if is_supply_name(name) or _SENSE_RE.search(name):
        return "supply_or_sense"
    return None


# ---------- component values ----------

_MULTIPLIERS = {
    "p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6, "m": 1e-3,
    "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9, "R": 1.0, "r": 1.0, "": 1.0,
}
_RKM_RE = re.compile(r"^(\d+)([pnuµμmkKMGRr])(\d+)([A-Za-zΩω]*)$")
_PLAIN_RE = re.compile(
    r"^(\d+(?:[.,]\d+)?)\s*([pnuµμmkKMGRr]?)\s*(?:[FfHh]|[Ωω]|[Oo][Hh][Mm][Ss]?)?$"
)
# "100 nF", "10 kΩ": a space between the number and its prefix or unit.
_SPACED_PREFIX_RE = re.compile(
    r"^(\d+(?:[.,]\d+)?)\s+(?=[pnuµμmkKMGRrΩω]|[PNU][FfHh])"
)
# "100NF", "10UF": an upper-case prefix before a unit.
_UPPER_PREFIX_RE = re.compile(r"^(\d+(?:[.,]\d+)?)([PNU])(?=[FfHh])")


def parse_value(raw: str | None) -> float | None:
    """A component value in base units, or None when it is not a plain value.

    "100n", "100nF", "100 nF", "100NF", "0.1uF", "100nF/16V" -> 1e-7; "4K7"
    -> 4700; "10 kΩ" -> 1e4; "49R9" -> 49.9; "0R" -> 0. Part numbers
    ("GRM155R71C104KA88D") give None.
    """
    if not raw:
        return None
    text = _SPACED_PREFIX_RE.sub(r"\1", raw.strip())
    text = _UPPER_PREFIX_RE.sub(lambda match: match.group(1) + match.group(2).lower(), text)
    first = re.split(r"[\s/;]+", text, maxsplit=1)[0]
    # "0,1uF" is a decimal comma; "100nF,16V" lists qualifiers after a comma.
    for token in dict.fromkeys((first, first.split(",", 1)[0])):
        if not token:
            continue
        match = _RKM_RE.match(token)
        if match:
            number = float(f"{match.group(1)}.{match.group(3)}")
            return number * _MULTIPLIERS[match.group(2)]
        match = _PLAIN_RE.match(token)
        if match:
            return float(match.group(1).replace(",", ".")) * _MULTIPLIERS[match.group(2)]
    return None


def _value_key(raw: str | None):
    """Sort/compare key: (0, number) for parsed values, (1, TEXT) otherwise."""
    number = parse_value(raw)
    if number is not None:
        return (0, number)
    return (1, re.sub(r"\s+", "", raw or "").upper())


def _same_values(first: list, second: list) -> bool:
    if len(first) != len(second):
        return False
    for left, right in zip(sorted(first), sorted(second)):
        if left[0] != right[0]:
            return False
        if left[0] == 0:
            if not math.isclose(left[1], right[1], rel_tol=1e-6, abs_tol=1e-15):
                return False
        elif left[1] != right[1]:
            return False
    return True


def _prefix(designator: str | None) -> str:
    match = re.match(r"^([A-Za-z]+)", designator or "")
    prefix = match.group(1).upper() if match else "?"
    return "L" if prefix in ("FB", "L") else prefix


_CONNECTOR_PREFIXES = frozenset({"J", "P", "CN", "CON", "X", "XS"})


# ---------- interface hints for swapped polarity ----------

# (pattern on pin/net/pair names and part description, interface, inversion
# tolerated: True / False / None = depends on the device, explanation).
_INTERFACE_HINTS = (
    (
        re.compile(r"CLK|CLOCK"),
        "clock", None,
        "Clock pair: an inverted clock is shifted by half a period; check "
        "whether the receiver tolerates it.",
    ),
    (
        re.compile(r"PCI ?E|PCI[_-]E|PCI EXPRESS|\bPE[TR][PN]?\d|\bHS[OI][PN]"),
        "PCI Express", True,
        "PCIe receivers detect and correct lane polarity during link training.",
    ),
    (
        re.compile(r"SSTX|SSRX|SS_TX|SS_RX|USB ?3|SUPERSPEED|\bSS[TR]X"),
        "USB 3 SuperSpeed", True,
        "USB 3 receivers must detect and correct SuperSpeed lane polarity.",
    ),
    (
        # Before Ethernet: SGMII / SerDes lanes of an Ethernet PHY are not MDI.
        re.compile(
            r"SGMII|QSGMII|SERDES|XAUI|SFP|1000BASE-?X|\bS_?(?:IN|OUT)[PN+\-]|\bSD_?[TR]X"
        ),
        "SGMII / 1000BASE-X / SerDes", None,
        "SGMII/1000BASE-X/SFP lanes are not polarity-corrected by the standard; "
        "some PHYs and FPGAs can invert a lane by register or in logic. Check "
        "both datasheets.",
    ),
    (
        re.compile(
            r"MDI|TRD\d|\bBI_?D[A-D]|1000BASE-?T|TXRX|\bTRX[PN]?\d|ETHERNET|RJ ?45|MAGNETIC"
        ),
        "Ethernet MDI", True,
        "1000BASE-T PHYs correct pair polarity automatically (100BASE-TX "
        "MLT-3 is polarity-insensitive).",
    ),
    (
        re.compile(r"SATA"),
        "SATA", None,
        "SATA does not require polarity correction; some controllers can "
        "invert a lane by configuration. Check the datasheets.",
    ),
    (
        re.compile(r"HDMI|TMDS|\bDVI"),
        "HDMI / TMDS", False,
        "TMDS polarity is not corrected; a swap corrupts the link.",
    ),
    (
        re.compile(r"\bCAN|CAN[HL]\b"),
        "CAN", False,
        "CAN_H / CAN_L swapped: the bus does not work.",
    ),
    (
        re.compile(r"RS-?485|RS-?422|\b485"),
        "RS-485 / RS-422", False,
        "RS-485 A/B polarity is not corrected by the transceiver.",
    ),
    (
        re.compile(r"LVDS|MIPI|\bIO_L\d+[PN]|SERDES|GTX|GTP|GTH|MGT"),
        "LVDS / MIPI / FPGA I/O", None,
        "Many FPGAs and serializers can invert a lane in logic; check the device.",
    ),
)
_USB_RE = re.compile(r"USB")
_USB_TXRX_RE = re.compile(r"TX|RX")
_USB2_PIN_RE = re.compile(r"(?<![A-Z0-9])(?:USB_?)?D[PMN+\-](?![A-Z0-9])|DATA[+\-]")
_USB2_HINT = {
    "interface": "USB 2.0",
    "inversion_tolerated": False,
    "hint": "USB 2.0 D+/D- polarity is not corrected; a swap breaks the link.",
}


def interface_hint(names: list[str], pin_names: list[str], description: str = "") -> dict:
    """Guess the interface of a swapped pair and whether inversion is tolerated.

    The swapped pin names decide first, then the net / pair names, then the
    part description or MPN (so the USB 2 pins of a PCIe-to-USB controller
    are not taken for PCIe lanes). Returns {"interface", "inversion_tolerated"
    (True / False / None = depends on the device), "hint"}.
    """
    pins = " ".join(_normalise_name(name) for name in pin_names if name)
    layers = (
        pins,
        " ".join(_normalise_name(name) for name in names if name),
        _normalise_name(description),
    )
    for layer, blob in enumerate(layers):
        if not blob:
            continue
        for pattern, interface, tolerated, hint in _INTERFACE_HINTS:
            if interface == "clock" and layer == 2:
                continue  # "clock" in a description says little about this pair
            if pattern.search(blob):
                return {"interface": interface, "inversion_tolerated": tolerated, "hint": hint}
        if layer == 0 and _USB2_PIN_RE.search(blob):
            return dict(_USB2_HINT)
    text = " ".join(layers)
    if _USB_RE.search(text) and _USB_TXRX_RE.search(pins):
        return {
            "interface": "USB 3 SuperSpeed",
            "inversion_tolerated": True,
            "hint": "USB part with TX/RX pins: USB 3 receivers correct lane polarity.",
        }
    if _USB_RE.search(text):
        return dict(_USB2_HINT)
    return {
        "interface": None,
        "inversion_tolerated": None,
        "hint": "Interface not recognised; check the datasheets before calling it a defect.",
    }


# ---------- board view ----------


class _Board:
    """Pad connectivity and netlist joins shared by the checks."""

    def __init__(self, index, netlist: dict | None, is_dnp):
        self.index = index
        self.is_dnp = is_dnp
        self.netlist_components: dict = (netlist or {}).get("components") or {}
        self._component_names = {
            name.lower(): name for name in self.netlist_components
        }
        self.net_names = {name.lower(): name for name in index.pcb.nets}
        self.comp_pads: dict[int, dict[str, str]] = defaultdict(dict)
        self.net_pads: dict[str, list[tuple[int, str]]] = defaultdict(list)
        component_count = len(index.pcb.components)
        for row in index.db.execute(
            "SELECT p.component, p.pad_name, n.name AS net FROM prims p "
            "JOIN nets n ON n.id = p.net_id "
            "WHERE p.kind = 'pad' AND p.component IS NOT NULL "
            "AND p.pad_name IS NOT NULL AND p.pad_name != '' "
            "ORDER BY p.id"
        ):
            component = row["component"]
            if not 0 <= component < component_count:
                continue
            pads = self.comp_pads[component]
            if row["pad_name"] in pads:
                continue
            pads[row["pad_name"]] = row["net"]
            self.net_pads[row["net"].lower()].append((component, row["pad_name"]))
        self.sch_refdes = {
            row["id"]: row["sch_refdes"]
            for row in index.db.execute("SELECT id, sch_refdes FROM components")
        }
        self._lengths: dict[str, float | None] = {}
        self._by_key: dict | None = None
        self._partners: dict[str, set[str]] | None = None

    def polarity_index(self) -> dict[str, dict[str, list[tuple[str, str]]]]:
        """Board nets by polarity key: {key: {"+": [(net, token)], "-": [...]}}."""
        if self._by_key is None:
            by_key: dict = defaultdict(lambda: {"+": [], "-": []})
            for net in self.index.pcb.nets:
                for key, polarity, token in polarity_tokens(net):
                    by_key[key][polarity].append((net, token))
            self._by_key = by_key
        return self._by_key

    def named_partners(self, net: str) -> set[str]:
        """Lower-case names of the board nets named as net's complement."""
        if self._partners is None:
            partners: dict[str, set[str]] = defaultdict(set)
            for members in self.polarity_index().values():
                for positive, positive_token in members["+"]:
                    for negative, negative_token in members["-"]:
                        if (positive_token, negative_token) not in _STYLES:
                            continue
                        if positive.lower() == negative.lower():
                            continue
                        partners[positive.lower()].add(negative.lower())
                        partners[negative.lower()].add(positive.lower())
            self._partners = partners
        return self._partners.get(net.lower(), set())

    def endpoint(self, component: int) -> bool:
        """A part a signal ends at: three or more connected pads, or a
        connector; not a test point."""
        prefix = _prefix(self.designator(component))
        if prefix == "TP":
            return False
        return len(self.comp_pads.get(component, {})) >= 3 or prefix in _CONNECTOR_PREFIXES

    def endpoints(self, net: str) -> set[int]:
        return {component for component in self.components_on(net) if self.endpoint(component)}

    def designator(self, component: int) -> str:
        return self.index.designator(component) or f"#{component}"

    def footprint(self, component: int) -> str | None:
        return self.index.pcb.components[component].footprint

    def components_on(self, net: str) -> dict[int, list[str]]:
        found: dict[int, list[str]] = defaultdict(list)
        for component, pad in self.net_pads.get(net.lower(), []):
            found[component].append(pad)
        return found

    def fanout(self, net: str) -> int:
        return len(self.net_pads.get(net.lower(), []))

    def netlist_part(self, component: int) -> tuple[str | None, dict | None]:
        """(netlist refdes, netlist entry) of a PCB component, if known."""
        for name in (self.sch_refdes.get(component), self.index.designator(component)):
            if not name:
                continue
            key = (
                name
                if name in self.netlist_components
                else self._component_names.get(name.lower())
            )
            if key:
                return key, self.netlist_components[key]
            if name == self.sch_refdes.get(component):
                # A resolved name that has no metadata: do not guess further.
                return name, None
        return None, None

    @staticmethod
    def pin_name(part: dict | None, pad: str) -> str:
        pins = (part or {}).get("pins") or {}
        entry = pins.get(pad)
        if entry is None:
            lowered = pad.lower()
            entry = next(
                (value for key, value in pins.items() if str(key).lower() == lowered),
                None,
            )
        if not isinstance(entry, dict):
            return ""
        return entry.get("name") or ""

    def dnp(self, component: int) -> bool | None:
        if self.is_dnp is None:
            return None
        name = self.sch_refdes.get(component) or self.index.designator(component)
        if not name:
            return None
        try:
            return bool(self.is_dnp(name))
        except Exception:
            return None

    def routed(self, net: str) -> float | None:
        key = net.lower()
        if key not in self._lengths:
            self._lengths[key] = self.index.routed_length(net)
        return self._lengths[key]


def _finding(check: str, issue: str, severity: str, pair: str | None,
             message: str, details: dict | None = None, **lengths) -> dict:
    finding: dict = {
        "check": check,
        "issue": issue,
        "severity": severity,
        "pair": pair,
        "message": message,
    }
    finding.update(lengths)
    if details:
        finding["details"] = details
    return finding


# ---------- naming ----------


def _pairs_of_net(all_pairs: list) -> dict[str, list[str]]:
    """Lower-case net -> the names of the PCB pairs it is a leg of."""
    pairs_of_net: dict[str, list[str]] = defaultdict(list)
    for pair in all_pairs:
        for net in (pair.positive_net, pair.negative_net):
            if pair.name not in pairs_of_net[net.lower()]:
                pairs_of_net[net.lower()].append(pair.name)
    return pairs_of_net


def pair_name_candidates(board: _Board, all_pairs: list, regex=None):
    """Each two board nets named as complementary legs (X_P / X_N, D+ / D-, CK_T / CK_C),
    once, with its outcome under "status": "declared" or "declared_inverted" (a PCB pair
    already), "conflict" (a leg belongs to another pair: "owners"), "suppressed_<reason>"
    (auto-named, supply or sense, or a rail by its pad count), or "undeclared" with the
    severity the naming check gives it and its "details". regex keeps candidates with a
    leg it matches."""
    declared = {
        frozenset((pair.positive_net.lower(), pair.negative_net.lower())): pair
        for pair in all_pairs
    }
    pairs_of_net = _pairs_of_net(all_pairs)
    by_key = board.polarity_index()
    seen: set[frozenset] = set()
    for key in sorted(by_key):
        members = by_key[key]
        for positive, positive_token in members["+"]:
            for negative, negative_token in members["-"]:
                style = _STYLES.get((positive_token, negative_token))
                legs = frozenset((positive.lower(), negative.lower()))
                if not style or len(legs) < 2 or legs in seen:
                    continue
                seen.add(legs)
                if regex and not (regex.search(positive) or regex.search(negative)):
                    continue
                candidate = {"positive": positive, "negative": negative, "style": style}
                pair = declared.get(legs)
                if pair is not None:
                    inverted = pair.positive_net.lower() != positive.lower()
                    yield {**candidate,
                           "status": "declared_inverted" if inverted else "declared"}
                    continue
                owners = pairs_of_net.get(positive.lower(), []) + pairs_of_net.get(
                    negative.lower(), []
                )
                if owners:
                    yield {**candidate, "status": "conflict",
                           "owners": list(dict.fromkeys(owners))}
                    continue
                reason = _suppressed_candidate(positive) or _suppressed_candidate(negative)
                if reason:
                    yield {**candidate, "status": f"suppressed_{reason}"}
                    continue
                pads = [board.fanout(positive), board.fanout(negative)]
                if max(pads) > RAIL_FANOUT:
                    # A rail or DC bus (VP / VN, DC_P / DC_N), whatever its name.
                    yield {**candidate, "status": "suppressed_rail"}
                    continue
                details = {"positive_net": positive, "negative_net": negative,
                           "style": style, "pads": pads}
                severity = "warning" if style in _WARNING_STYLES else "info"
                if severity == "warning" and (
                    _LOAD_RE.search(_normalise_name(positive))
                    or _LOAD_RE.search(_normalise_name(negative))
                ):
                    # Motor, speaker, LED, heater... outputs: not a routing defect.
                    severity = "info"
                    details["load_or_actuator"] = True
                yield {**candidate, "status": "undeclared", "severity": severity,
                       "details": details}


class _NetlistNames:
    """The two board reads pair_name_candidates needs, from a netlist instead of a PcbDoc:
    nets by polarity key and each net's pin count."""

    def __init__(self, netlist: dict):
        self.nets: dict[str, list] = netlist.get("nets") or {}
        self._by_key: dict | None = None
        self._pins = {name.lower(): len(pins) for name, pins in self.nets.items()}

    def polarity_index(self) -> dict[str, dict[str, list[tuple[str, str]]]]:
        if self._by_key is None:
            by_key: dict = defaultdict(lambda: {"+": [], "-": []})
            for net in sorted(self.nets):
                for key, polarity, token in polarity_tokens(net):
                    by_key[key][polarity].append((net, token))
            self._by_key = by_key
        return self._by_key

    def fanout(self, net: str) -> int:
        return self._pins.get(net.lower(), 0)


def netlist_pair_candidates(netlist: dict, declared: list | None = None) -> list[dict]:
    """pair_name_candidates on a netlist (no PcbDoc needed): every two nets named as
    complementary legs, with its status against the declared pairs given (PcbDoc
    DiffPair-like objects; none by default, so every real candidate is "undeclared"). A
    net's fanout is its pin count."""
    return list(pair_name_candidates(_NetlistNames(netlist), list(declared or [])))


def inferred_pairs(index) -> list[dict]:
    """The undeclared pairs the naming check reports (nets named as complementary legs
    that are not a PCB differential pair, without auto-named, supply, sense and rail
    nets): {"positive", "negative", "style", "severity", "details"} each."""
    board = _Board(index, None, None)
    return [
        candidate
        for candidate in pair_name_candidates(board, list(index.pcb.diff_pairs))
        if candidate["status"] == "undeclared"
    ]


def _check_naming(board: _Board, pairs: list, all_pairs: list, regex) -> tuple[dict, list]:
    findings: list[dict] = []
    summary = Counter()
    summary["pairs"] = len(pairs)
    pairs_of_net = _pairs_of_net(all_pairs)

    # PCB pairs: conventions.
    reported_shared: set[str] = set()
    for pair in pairs:
        positive, negative = pair.positive_net, pair.negative_net
        missing = [net for net in (positive, negative) if net.lower() not in board.net_names]
        if missing:
            summary["missing_legs"] += 1
            findings.append(_finding(
                "naming", "pair_leg_missing", "warning", pair.name,
                f"Pair {pair.name} names net(s) {', '.join(missing)} that do not "
                "exist on the PCB; the pair has no effect on routing or rules.",
                {"positive_net": positive, "negative_net": negative, "missing_nets": missing},
            ))
            continue
        if positive.lower() == negative.lower():
            summary["same_net"] += 1
            findings.append(_finding(
                "naming", "pair_same_net", "warning", pair.name,
                f"Pair {pair.name} uses {positive} for both legs.",
                {"net": positive},
            ))
            continue
        for net in (positive, negative):
            owners = pairs_of_net[net.lower()]
            if len(owners) > 1 and net.lower() not in reported_shared:
                reported_shared.add(net.lower())
                summary["nets_in_multiple_pairs"] += 1
                findings.append(_finding(
                    "naming", "net_in_multiple_pairs", "warning", pair.name,
                    f"Net {net} is a leg of {len(owners)} pairs: {', '.join(owners)}.",
                    {"net": net, "pairs": owners},
                ))
        verdict = complement(positive, negative)
        if verdict is None:
            summary["unconventional"] += 1
            findings.append(_finding(
                "naming", "pair_names_unconventional", "info", pair.name,
                f"Pair {pair.name}: {positive} / {negative} carry no complementary "
                "polarity suffix (e.g. _P/_N), so polarity cannot be read from the names.",
                {"positive_net": positive, "negative_net": negative},
            ))
        elif verdict[0] == "-":
            summary["inverted"] += 1
            findings.append(_finding(
                "naming", "pair_polarity_inverted", "warning", pair.name,
                f"Pair {pair.name} has the negative-named net {positive} as its "
                f"positive leg and {negative} as its negative leg.",
                {"positive_net": positive, "negative_net": negative, "style": verdict[1]},
            ))
        else:
            summary["conventional"] += 1

    # Nets named like a pair.
    for candidate in pair_name_candidates(board, all_pairs, regex):
        summary["candidates"] += 1
        status = candidate["status"]
        positive, negative, style = (
            candidate["positive"], candidate["negative"], candidate["style"])
        if status == "conflict":
            summary["conflict"] += 1
            findings.append(_finding(
                "naming", "candidate_conflicts_with_pair", "info", None,
                f"{positive} / {negative} are named as a {style} pair, but "
                f"a leg already belongs to {', '.join(candidate['owners'])}.",
                {"positive_net": positive, "negative_net": negative,
                 "style": style, "pairs": candidate["owners"]},
            ))
        elif status == "undeclared":
            severity = candidate["severity"]
            summary[f"undeclared_{severity}"] += 1
            findings.append(_finding(
                "naming", "undeclared_pair", severity, None,
                f"Nets {positive} / {negative} are named as a {style} pair but "
                "are not a PCB differential pair, so no pair rule (gap, "
                "uncoupled length, in-pair skew) applies to them.",
                candidate["details"],
            ))
        else:
            summary[status] += 1
    keys = (
        "pairs", "conventional", "inverted", "unconventional", "missing_legs",
        "same_net", "nets_in_multiple_pairs", "candidates", "declared",
        "declared_inverted", "conflict", "undeclared_warning", "undeclared_info",
        "suppressed_auto_named", "suppressed_supply_or_sense", "suppressed_rail",
    )
    return {key: summary[key] for key in keys}, findings


# ---------- polarity ----------


# Parts that only shunt a leg to a supply (ESD / TVS arrays): their pin order
# does not change link polarity.
_PROTECTION_RE = re.compile(r"ESD|TVS|PROTECT|SUPPRESS")
_PROTECTION_PREFIXES = frozenset({"D", "TVS", "ESD"})
# Parts the pair passes through (common-mode chokes, filters, muxes,
# magnetics): only a crossing between their two sides matters.
_PASS_THROUGH_RE = re.compile(
    r"COMMON[ -]?MODE|CHOKE|\bCMC\b|FILTER|MULTIPLEX|\bMUX\b|\bDEMUX\b"
    r"|ANALOG SWITCH|USB SWITCH|SIGNAL SWITCH|\bSPDT\b|\bDPDT\b|REDRIVER"
    r"|TRANSFORMER|MAGNETIC"
)
_PASS_THROUGH_PREFIXES = frozenset({"L", "FL", "CMC", "T"})
_PASS_THROUGH_PIN_LIMIT = 64


def _polarity_role(board: _Board, component: int, part: dict, legs_lower: set[str]) -> str:
    """"shunt" (a protection part between the legs and supplies), "pass_through"
    (choke, filter, mux or transformer with pins beyond this pair) or
    "endpoint" (transceivers, PHYs, controllers, connectors)."""
    prefix = _prefix(board.designator(component))
    if prefix in _CONNECTOR_PREFIXES:
        return "endpoint"
    text = _normalise_name(
        " ".join(str(part.get(key) or "") for key in ("description", "mpn", "value"))
    )
    beyond = [
        net for net in board.comp_pads.get(component, {}).values()
        if net.lower() not in legs_lower
        and not is_supply_name(net)
        and not _SHUNT_RETURN_RE.search(net)
    ]
    if not beyond and (prefix in _PROTECTION_PREFIXES or _PROTECTION_RE.search(text)):
        return "shunt"
    if beyond and (prefix in _PASS_THROUGH_PREFIXES or _PASS_THROUGH_RE.search(text)):
        return "pass_through"
    return "endpoint"


def _other_sides(board: _Board, component: int, part: dict, legs_lower: set[str],
                 declared: dict) -> list[dict]:
    """Complementary pin pairs of a pass-through part away from this pair, with
    whether the positive net of that side lands on the positive pin
    (verdict "ok" / "swapped", None when the nets' polarity is unknown)."""
    items = [
        (pad, net, board.pin_name(part, pad))
        for pad, net in sorted(board.comp_pads.get(component, {}).items())
        if net.lower() not in legs_lower and not is_supply_name(net)
    ][:_PASS_THROUGH_PIN_LIMIT]
    sides = []
    for first, second in combinations(items, 2):
        if first[1].lower() == second[1].lower():
            continue
        found = complement(first[2], second[2])
        if not found:
            continue
        plus, minus = (first, second) if found[0] == "+" else (second, first)
        other_pair = declared.get(frozenset((plus[1].lower(), minus[1].lower())))
        if other_pair is not None:
            verdict = "ok" if other_pair.positive_net.lower() == plus[1].lower() else "swapped"
        else:
            named = complement(plus[1], minus[1])
            verdict = None if named is None else ("ok" if named[0] == "+" else "swapped")
        sides.append({
            "verdict": verdict,
            "pair": other_pair.name if other_pair is not None else None,
            "positive_pin": {"pin": plus[0], "pin_name": plus[2], "net": plus[1]},
            "negative_pin": {"pin": minus[0], "pin_name": minus[2], "net": minus[1]},
        })
    return sides


def _check_polarity(board: _Board, pairs: list, all_pairs: list) -> tuple[dict, list]:
    findings: list[dict] = []
    summary = Counter()
    summary["pairs"] = len(pairs)
    declared = {
        frozenset((pair.positive_net.lower(), pair.negative_net.lower())): pair
        for pair in all_pairs
    }
    for pair in pairs:
        positive, negative = pair.positive_net, pair.negative_net
        if positive.lower() == negative.lower():
            continue
        legs_lower = {positive.lower(), negative.lower()}
        on_positive = board.components_on(positive)
        on_negative = board.components_on(negative)
        both = sorted(set(on_positive) & set(on_negative), key=board.designator)
        judged_here = False
        for component in both:
            summary["incidences"] += 1
            refdes, part = board.netlist_part(component)
            if part is None:
                summary["unjudged"] += 1
                summary["unjudged_no_netlist_part"] += 1
                continue
            positive_pins = [(pad, board.pin_name(part, pad)) for pad in sorted(on_positive[component])]
            negative_pins = [(pad, board.pin_name(part, pad)) for pad in sorted(on_negative[component])]
            verdicts = []
            for positive_pad, positive_name in positive_pins:
                for negative_pad, negative_name in negative_pins:
                    found = complement(positive_name, negative_name)
                    if found:
                        verdicts.append((
                            "ok" if found[0] == "+" else "swapped",
                            found[1],
                            (positive_pad, positive_name),
                            (negative_pad, negative_name),
                        ))
            if not verdicts:
                summary["unjudged"] += 1
                continue
            role = _polarity_role(board, component, part, legs_lower)
            if role == "shunt":
                # Pin order of a shunt protection part does not affect the link.
                summary["unjudged"] += 1
                summary["unjudged_shunt"] += 1
                continue
            kinds = {verdict[0] for verdict in verdicts}
            crossing = None
            if role == "pass_through" and len(kinds) == 1:
                this_side = next(iter(kinds))
                known = [
                    side for side in _other_sides(board, component, part, legs_lower, declared)
                    if side["verdict"] is not None
                ]
                # A crossing into a declared pair is reported from the swapped side.
                crossing = next(
                    (
                        side for side in known
                        if side["verdict"] != this_side
                        and (this_side == "swapped" or side["pair"] is None)
                    ),
                    None,
                )
                if crossing is None and this_side == "swapped":
                    if not known:
                        summary["unjudged"] += 1
                        summary["unjudged_pass_through"] += 1
                        continue
                    # Swapped on both sides of the part: polarity is preserved.
                    summary["judged"] += 1
                    summary["pass_through_consistent"] += 1
                    judged_here = True
                    continue
            summary["judged"] += 1
            judged_here = True
            if kinds == {"ok"} and crossing is None:
                summary["ok"] += 1
                continue
            first = next((verdict for verdict in verdicts if verdict[0] == "swapped"), verdicts[0])
            designator = board.designator(component)
            dnp = board.dnp(component)
            details = {
                "component": designator,
                "positive_net": {"net": positive, "pin": first[2][0], "pin_name": first[2][1]},
                "negative_net": {"net": negative, "pin": first[3][0], "pin_name": first[3][1]},
                "style": first[1],
            }
            if refdes and refdes != designator:
                details["sch_refdes"] = refdes
            if dnp is not None:
                details["dnp"] = dnp
            if len(kinds) > 1:
                summary["mixed"] += 1
                findings.append(_finding(
                    "polarity", "mixed_pin_polarity", "warning", pair.name,
                    f"{designator}: pins on {positive} / {negative} match both "
                    "the positive and the negative pin names; check the pinout.",
                    details,
                ))
                continue
            hint = interface_hint(
                [positive, negative, pair.name],
                [first[2][1], first[3][1]],
                f"{part.get('description') or ''} {part.get('mpn') or ''}",
            )
            details.update(hint)
            tolerated = hint["inversion_tolerated"] is True
            severity = "info" if tolerated or dnp else "warning"
            if crossing is not None:
                summary["crossed_through_part"] += 1
                other = {key: value for key, value in crossing.items() if key != "verdict"}
                details["other_side"] = other
                this_order = "swapped" if first[0] == "swapped" else "in order"
                other_order = "in order" if crossing["verdict"] == "ok" else "swapped"
                findings.append(_finding(
                    "polarity", "crossed_through_part", severity, pair.name,
                    f"{designator} passes {positive} / {negative} through: they meet "
                    f"pins '{first[2][1]}' / '{first[3][1]}' {this_order}, but on the "
                    f"other side {other['positive_pin']['net']} / "
                    f"{other['negative_pin']['net']} meet '{other['positive_pin']['pin_name']}' / "
                    f"'{other['negative_pin']['pin_name']}' {other_order}: polarity "
                    "is crossed through this part.",
                    details,
                ))
                continue
            summary["swapped"] += 1
            if tolerated:
                summary["swapped_inversion_tolerated"] += 1
            findings.append(_finding(
                "polarity", "swapped_at_pins", severity, pair.name,
                f"{designator}: positive net {positive} lands on pin "
                f"{first[2][0]} '{first[2][1]}' and negative net {negative} on "
                f"pin {first[3][0]} '{first[3][1]}' (P/N swapped at this part).",
                details,
            ))
        if judged_here:
            summary["pairs_with_judged_component"] += 1
    keys = (
        "pairs", "incidences", "judged", "ok", "swapped",
        "swapped_inversion_tolerated", "mixed", "crossed_through_part",
        "pass_through_consistent", "unjudged", "unjudged_no_netlist_part",
        "unjudged_shunt", "unjudged_pass_through", "pairs_with_judged_component",
    )
    return {key: summary[key] for key in keys}, findings


# ---------- series parts ----------

# Prefixes of parts that are never in series with a leg (ICs, connectors).
_NOT_SERIES_PREFIXES = frozenset({"U", "IC", "J", "P", "CN", "CON", "X", "XS", "TP", "Q"})
_KINDS = {"C": "capacitor", "R": "resistor", "L": "inductor/ferrite"}
# Two-pad hops followed beyond a series part to find where it leads.
_REACH_HOPS = 3
_PRIVATE_KEYS = frozenset({"prefix", "component", "reach"})


def _series_part(board: _Board, component: int, far: str) -> dict:
    designator = board.designator(component)
    refdes, part = board.netlist_part(component)
    raw = None
    if part:
        raw = part.get("value") or part.get("mpn") or None
    entry = {
        "refdes": designator,
        "prefix": _prefix(designator),
        "value": raw,
        "footprint": board.footprint(component),
        "far_net": far,
        "component": component,
    }
    if refdes and refdes != designator:
        entry["sch_refdes"] = refdes
    dnp = board.dnp(component)
    if dnp is not None:
        entry["dnp"] = dnp
    return entry


def _public(part: dict) -> dict:
    return {key: value for key, value in part.items() if key not in _PRIVATE_KEYS}


def _far_reach(board: _Board, part: dict, legs_lower: set[str]) -> set[int]:
    """Endpoints (ICs, connectors, transistors) a series part's far net leads
    to, directly or through up to two more two-pad parts; supplies and rails
    are not followed."""
    seen = {part["far_net"].lower()} | legs_lower
    frontier = [part["far_net"]]
    reach: set[int] = set()
    for _ in range(_REACH_HOPS):
        following = []
        for net in frontier:
            if is_supply_name(net) or board.fanout(net) > RAIL_FANOUT:
                continue
            for component in board.components_on(net):
                if component == part["component"]:
                    continue
                if board.endpoint(component):
                    reach.add(component)
                    continue
                pads = board.comp_pads.get(component, {})
                if len(pads) != 2:
                    continue
                for other in pads.values():
                    if other.lower() not in seen:
                        seen.add(other.lower())
                        following.append(other)
        frontier = following
    return reach


def _same_value(first: dict, second: dict) -> bool:
    return _same_values([_value_key(first["value"])], [_value_key(second["value"])])


def _match_series(on_positive: list, on_negative: list, declared: dict) -> tuple[list, list, list]:
    """Pair one prefix's series parts across the legs: far nets that are a
    declared pair, then far nets named as a pair, then far nets reaching a
    common part, then equal value and footprint.

    Returns (matched [(positive part, negative part)], unmatched positive,
    unmatched negative).
    """
    tests = (
        lambda p, n: frozenset((p["far_net"].lower(), n["far_net"].lower())) in declared,
        lambda p, n: complement(p["far_net"], n["far_net"]) is not None,
        lambda p, n: bool(p["reach"] & n["reach"]),
        lambda p, n: p["footprint"] == n["footprint"] and _same_value(p, n),
    )
    positive_left, negative_left = list(on_positive), list(on_negative)
    matched = []
    for test in tests:
        for part in list(positive_left):
            other = next((candidate for candidate in negative_left if test(part, candidate)), None)
            if other is not None:
                matched.append((part, other))
                positive_left.remove(part)
                negative_left.remove(other)
    return matched, positive_left, negative_left


def _is_shunt(board: _Board, part: dict, leg_reach: set[int], partner_reach: set[int],
              pair_nets: set[str]) -> bool:
    """An unmatched part that does not lead where the partner leg goes (a
    pull-up, bias or stub): its far net is not a pair leg nor named as one,
    and nothing it reaches is a part the partner leg reaches and this leg,
    without it, does not."""
    far = part["far_net"]
    if far.lower() in pair_nets or board.named_partners(far) or not partner_reach:
        return False
    return not part["reach"] & (partner_reach - leg_reach)


def _one_leg_reason(part: dict) -> str | None:
    """Why a part on one leg only is probably not a defect."""
    if part.get("dnp"):
        return "dnp"
    if part["prefix"] == "R":
        value = parse_value(part["value"])
        if value is not None and value >= BIAS_OHMS:
            return "bias_resistor"
    return None


_ONE_LEG_REASONS = {
    "dnp": "DNP in the active variant",
    "bias_resistor": f"a resistor of {BIAS_OHMS / 1000:g}k or more, probably a pull-up or bias",
}


def _check_series(board: _Board, pairs: list, all_pairs: list) -> tuple[dict, list]:
    findings: list[dict] = []
    summary = Counter()
    summary["pairs"] = len(pairs)
    declared = {
        frozenset((pair.positive_net.lower(), pair.negative_net.lower())): pair
        for pair in all_pairs
    }
    pairs_of_net: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for pair in all_pairs:
        for side, net in (("positive", pair.positive_net), ("negative", pair.negative_net)):
            if (pair.name, side) not in pairs_of_net[net.lower()]:
                pairs_of_net[net.lower()].append((pair.name, side))
    pair_nets = set(pairs_of_net)
    # Parts between two declared pairs (TX / TX_C) are seen from both pairs;
    # report each issue once and name the other pair on it.
    reported: dict[tuple, dict] = {}

    def emit(finding: dict, parts: list[dict]) -> None:
        key = (finding["issue"], frozenset(part["refdes"] for part in parts))
        if key in reported:
            first = reported[key]
            if finding["pair"] != first["pair"]:
                others = first.setdefault("details", {}).setdefault("also_pairs", [])
                if finding["pair"] not in others:
                    others.append(finding["pair"])
            return
        reported[key] = finding
        findings.append(finding)

    for pair in pairs:
        positive, negative = pair.positive_net, pair.negative_net
        legs_lower = {positive.lower(), negative.lower()}
        if len(legs_lower) < 2:
            continue
        nets = {"positive": positive, "negative": negative}
        series: dict[str, list[dict]] = {"positive": [], "negative": []}
        across: set[int] = set()
        multi_pin: set[int] = set()
        for leg, net in nets.items():
            for component in sorted(board.components_on(net), key=board.designator):
                pads = board.comp_pads.get(component, {})
                if len(pads) != 2:
                    if 3 <= len(pads) <= 4 and _prefix(board.designator(component)) not in _NOT_SERIES_PREFIXES:
                        multi_pin.add(component)
                    continue
                others = [other for other in pads.values() if other.lower() != net.lower()]
                if len(others) != 1:
                    continue
                far = others[0]
                if far.lower() in legs_lower:
                    across.add(component)
                    continue
                if is_supply_name(far) or board.fanout(far) > RAIL_FANOUT:
                    continue
                series[leg].append(_series_part(board, component, far))
        summary["across_pair_parts"] += len(across)
        summary["multi_pin_parts_not_analysed"] += len(multi_pin)
        common = {part["far_net"].lower() for part in series["positive"]} & {
            part["far_net"].lower() for part in series["negative"]
        }
        if common:
            summary["common_node_pairs"] += 1
            for leg in series:
                series[leg] = [
                    part for part in series[leg] if part["far_net"].lower() not in common
                ]
        if not series["positive"] and not series["negative"]:
            continue
        for leg_parts in series.values():
            for part in leg_parts:
                part["reach"] = _far_reach(board, part, legs_lower)
        base = {leg: board.endpoints(net) for leg, net in nets.items()}

        def reach_of(leg: str, without: dict | None = None) -> set[int]:
            found = set(base[leg])
            for part in series[leg]:
                if part is not without:
                    found |= part["reach"]
            return found

        groups = {}
        for prefix in sorted({part["prefix"] for leg in series.values() for part in leg}):
            groups[prefix] = _match_series(
                [part for part in series["positive"] if part["prefix"] == prefix],
                [part for part in series["negative"] if part["prefix"] == prefix],
                declared,
            )
        # Unmatched parts that lead nowhere the partner leg goes are shunts
        # (pull-ups, bias, stubs), not series parts.
        for _, left_positive, left_negative in groups.values():
            for leg, partner, left in (
                ("positive", "negative", left_positive),
                ("negative", "positive", left_negative),
            ):
                for part in list(left):
                    if not _is_shunt(board, part, reach_of(leg, part), reach_of(partner), pair_nets):
                        continue
                    left.remove(part)
                    summary["shunt_parts"] += 1
                    emit(_finding(
                        "series", "shunt_part_one_leg", "info", pair.name,
                        f"{part['refdes']} on the {leg} leg ({nets[leg]}) leads to "
                        f"{part['far_net']}, which does not continue the pair: a "
                        "shunt (pull-up, bias or stub), not a series part.",
                        {"leg": leg, "part": _public(part)},
                    ), [part])
        live = [prefix for prefix, group in groups.items() if any(group)]
        if not live:
            continue
        summary["pairs_with_series"] += 1
        if "C" in live:
            summary["capacitor_coupled"] += 1
        if "R" in live:
            summary["resistor_series"] += 1
        if any(prefix not in ("C", "R") for prefix in live):
            summary["other_series"] += 1
        asymmetric = False
        for prefix in live:
            matched, left_positive, left_negative = groups[prefix]
            kind = _KINDS.get(prefix, f"{prefix} part")
            far_pairs = list(matched)
            if left_positive and len(left_positive) == len(left_negative):
                # As many unmatched parts on each leg: compare them as pairs.
                left_positive.sort(key=lambda part: part["refdes"])
                left_negative.sort(key=lambda part: part["refdes"])
                leftover = list(zip(left_positive, left_negative))
                matched = matched + leftover
                if len(leftover) == 1:
                    far_pairs += leftover
                left_positive, left_negative = [], []
            on_positive = [part for part, _ in matched]
            on_negative = [other for _, other in matched]
            extras = left_positive + left_negative
            if extras:
                asymmetric = True
                all_positive = on_positive + left_positive
                all_negative = on_negative + left_negative
                listing = {
                    "positive": [_public(part) for part in all_positive],
                    "negative": [_public(part) for part in all_negative],
                }
                reasons = [_one_leg_reason(part) for part in extras]
                severity = "info" if all(reasons) else "warning"
                details: dict = {"prefix": prefix, **listing}
                suffix = ""
                if severity == "info":
                    details["likely_not_series"] = sorted(set(reasons))
                    suffix = "; " + " / ".join(
                        _ONE_LEG_REASONS[reason] for reason in sorted(set(reasons))
                    )
                if not matched and not (left_positive and left_negative):
                    leg = "positive" if left_positive else "negative"
                    issue = "ac_coupling_asymmetric" if prefix == "C" else "series_part_one_leg_only"
                    message = (
                        f"Series {kind} on the {leg} leg only "
                        f"({', '.join(part['refdes'] for part in extras)}){suffix}."
                    )
                else:
                    issue = "series_count_mismatch"
                    message = (
                        f"{len(all_positive)} series {kind}(s) on the positive leg, "
                        f"{len(all_negative)} on the negative leg (unmatched: "
                        f"{', '.join(part['refdes'] for part in extras)}){suffix}."
                    )
                emit(_finding("series", issue, severity, pair.name, message, details),
                     all_positive + all_negative)
            if not matched:
                continue
            listing = {
                "positive": [_public(part) for part in on_positive],
                "negative": [_public(part) for part in on_negative],
            }
            values_p = [part["value"] for part in on_positive]
            values_n = [part["value"] for part in on_negative]
            if all(values_p + values_n) and not _same_values(
                [_value_key(value) for value in values_p],
                [_value_key(value) for value in values_n],
            ):
                asymmetric = True
                emit(_finding(
                    "series", "series_value_mismatch", "warning", pair.name,
                    f"Series {kind} values differ between the legs: "
                    f"{', '.join(values_p)} vs {', '.join(values_n)}.",
                    {"prefix": prefix, **listing},
                ), on_positive + on_negative)
            footprints_p = [part["footprint"] for part in on_positive]
            footprints_n = [part["footprint"] for part in on_negative]
            if all(footprints_p + footprints_n) and sorted(footprints_p) != sorted(footprints_n):
                asymmetric = True
                emit(_finding(
                    "series", "series_footprint_mismatch", "warning", pair.name,
                    f"Series {kind} footprints differ between the legs: "
                    f"{', '.join(footprints_p)} vs {', '.join(footprints_n)}.",
                    {"prefix": prefix, **listing},
                ), on_positive + on_negative)
            dnp_p = [part.get("dnp") for part in on_positive]
            dnp_n = [part.get("dnp") for part in on_negative]
            if None not in dnp_p + dnp_n and sorted(dnp_p) != sorted(dnp_n):
                asymmetric = True
                emit(_finding(
                    "series", "series_dnp_mismatch", "warning", pair.name,
                    f"Series {kind}s are fitted on one leg and DNP on the other "
                    "in the active variant.",
                    {"prefix": prefix, **listing},
                ), on_positive + on_negative)
            _far_side(board, pair, kind, far_pairs, declared, pairs_of_net, summary, emit)
        if asymmetric:
            summary["asymmetric_pairs"] += 1
    keys = (
        "pairs", "pairs_with_series", "capacitor_coupled", "resistor_series",
        "other_series", "asymmetric_pairs", "shunt_parts", "far_pair_ok",
        "far_pair_crossed", "coupled_side_crossed_by_name", "coupled_side_cross_pair",
        "coupled_side_not_pair", "coupled_side_not_pair_suppressed",
        "common_node_pairs", "across_pair_parts", "multi_pin_parts_not_analysed",
    )
    return {key: summary[key] for key in keys}, findings


def _describe_far(net: str, owners: list[tuple[str, str]]) -> str:
    if not owners:
        return f"{net} (in no declared pair)"
    legs = ", ".join(f"the {side} leg of pair {name}" for name, side in owners)
    return f"{net} ({legs})"


def _far_side(board, pair, kind, matched, declared, pairs_of_net, summary, emit) -> None:
    """Where the legs continue beyond matched series parts: a declared pair
    (with which polarity), legs of different pairs, or undeclared nets."""
    for part, other in matched:
        far_positive, far_negative = part["far_net"], other["far_net"]
        refs = f"{part['refdes']} / {other['refdes']}"
        sides = {"positive_side": _public(part), "negative_side": _public(other)}
        far_pair = declared.get(frozenset((far_positive.lower(), far_negative.lower())))
        if far_pair is not None:
            if far_pair.positive_net.lower() == far_positive.lower():
                summary["far_pair_ok"] += 1
                continue
            summary["far_pair_crossed"] += 1
            emit(_finding(
                "series", "coupled_side_inverted", "warning", pair.name,
                f"Through {refs} the positive leg of {pair.name} continues on "
                f"{far_positive}, the negative leg of pair {far_pair.name}: "
                f"polarity is crossed at the series {kind}s.",
                {"far_pair": far_pair.name, **sides},
            ), [part, other])
            continue
        owners_p = pairs_of_net.get(far_positive.lower(), [])
        owners_n = pairs_of_net.get(far_negative.lower(), [])
        if (owners_p or owners_n) and {name for name, _ in owners_p} != {name for name, _ in owners_n}:
            summary["coupled_side_cross_pair"] += 1
            emit(_finding(
                "series", "coupled_side_cross_pair", "warning", pair.name,
                f"Through {refs} the positive leg of {pair.name} continues on "
                f"{_describe_far(far_positive, owners_p)} and the negative leg on "
                f"{_describe_far(far_negative, owners_n)}: the legs are coupled "
                "into different pairs.",
                {"far_nets": [far_positive, far_negative],
                 "far_pairs": {"positive": [name for name, _ in owners_p],
                               "negative": [name for name, _ in owners_n]},
                 **sides},
            ), [part, other])
            continue
        named = complement(far_positive, far_negative)
        if named is not None and named[0] == "-":
            summary["coupled_side_crossed_by_name"] += 1
            emit(_finding(
                "series", "coupled_side_crossed_by_name", "warning", pair.name,
                f"Through {refs} the positive leg of {pair.name} continues on "
                f"{far_positive} and the negative leg on {far_negative}: polarity "
                f"is crossed at the series {kind}s (the far nets are not a "
                "declared pair).",
                {"far_nets": [far_positive, far_negative], "style": named[1], **sides},
            ), [part, other])
            continue
        if named is None:
            partners_p = board.named_partners(far_positive) - {far_negative.lower()}
            partners_n = board.named_partners(far_negative) - {far_positive.lower()}
            if partners_p and partners_n:
                summary["coupled_side_cross_pair"] += 1

                def names(found: set[str]) -> str:
                    return ", ".join(sorted(board.net_names.get(net, net) for net in found))

                emit(_finding(
                    "series", "coupled_side_cross_pair", "warning", pair.name,
                    f"Through {refs} the positive leg of {pair.name} continues on "
                    f"{far_positive} (named as a pair with {names(partners_p)}) and "
                    f"the negative leg on {far_negative} (named with "
                    f"{names(partners_n)}): the legs are coupled into different pairs.",
                    {"far_nets": [far_positive, far_negative],
                     "named_partners": {"positive": names(partners_p).split(", "),
                                        "negative": names(partners_n).split(", ")},
                     **sides},
                ), [part, other])
                continue
        lengths = [board.routed(far_positive), board.routed(far_negative)]
        short = all(length is not None and length < SHORT_STUB_MIL for length in lengths)
        if is_auto_net_name(far_positive) and is_auto_net_name(far_negative) and short:
            summary["coupled_side_not_pair_suppressed"] += 1
            continue
        summary["coupled_side_not_pair"] += 1
        emit(_finding(
            "series", "coupled_side_not_a_pair", "info", pair.name,
            f"Beyond the series {kind}s ({refs}) the legs continue on "
            f"{far_positive} / {far_negative}"
            f"{' (named as a pair, polarity consistent)' if named else ''}, which "
            "are not a PCB differential pair, so pair rules stop at the parts.",
            {"far_nets": [far_positive, far_negative],
             "named_like_pair": bool(named),
             "named_polarity": "ok" if named else None},
            far_routed_mil=[length if length is not None else 0.0 for length in lengths],
        ), [part, other])


# ---------- skew ----------

_NET_TERMS = frozenset({"innet", "innetclass", "isnet", "inanynet"})


def _scope_calls(node, found: set) -> None:
    tag = node[0]
    if tag == "call":
        found.add(node[1])
    elif tag == "cmp":
        found.add(f"cmp:{node[1]}")
    elif tag == "not":
        _scope_calls(node[1], found)
    elif tag in ("and", "or", "xor"):
        _scope_calls(node[1], found)
        _scope_calls(node[2], found)


_PAIR_TERMS = frozenset({
    "indifferentialpair", "indifferentialpairclass", "inanydifferentialpair",
    "isdifferentialpair",
})


def _is_net_term(node) -> bool:
    if node[0] == "call":
        return node[1] in _NET_TERMS
    return node[0] == "cmp" and node[1] == "objectkind" and str(node[3]).lower() == "net"


def _fix_net_terms(node, value: bool):
    """The scope tree with every net term replaced by a constant."""
    tag = node[0]
    if tag in ("call", "cmp") and _is_net_term(node):
        return ("const", value, node[-1])
    if tag == "not":
        return ("not", _fix_net_terms(node[1], value), node[-1])
    if tag in ("and", "or", "xor"):
        return (tag, _fix_net_terms(node[1], value), _fix_net_terms(node[2], value), node[-1])
    return node


def _scope_kind(rule, target, ctx) -> str:
    """How a rule that matches a pair selects it: "pair" (a pair term decides),
    "net" (the pair matches only through its member nets) or "all"."""
    try:
        tree = parse_scope(rule.scope1)
    except ScopeSyntaxError:
        return "pair"
    calls: set = set()
    _scope_calls(tree, calls)
    as_nets = all(
        evaluate_scope(
            rule.scope1, replace(target, kind="net", pairs=set(), member=net), ctx
        )["matches"] == "yes"
        for net in sorted(target.nets)
    )
    if not as_nets:
        return "pair"
    if calls & _PAIR_TERMS or "cmp:objectkind" in calls:
        # InDifferentialPair('X') Or InNetClass('HS'): the pair terms select
        # X whatever the net terms say.
        if all(
            evaluate(_fix_net_terms(tree, value), target, ctx)[0] is True
            for value in (False, True)
        ):
            return "pair"
    if calls & _NET_TERMS or "cmp:objectkind" in calls:
        return "net"
    return "all"


# Per-leg facts of the length engine passed through to unrouted findings.
_LEG_KEYS = (
    "pad_count", "copper_mil", "routed_mil", "unrouted_manhattan_mil", "pad_islands", "note",
)
_MISSING_STATUSES = frozenset({"net_missing", "net_not_found"})


def _leg_lengths(board: _Board, pair, pair_lengths) -> tuple[dict, dict, float | None, str]:
    """(positive, negative, skew, source); legs are {net, length_mil, status}
    plus pad_count, copper_mil and the engine's pad_islands / note when known."""
    nets = (pair.positive_net, pair.negative_net)
    if pair_lengths is not None:
        try:
            measured = pair_lengths(pair.name)
        except Exception:
            measured = None
        if isinstance(measured, dict):
            legs = []
            for leg, net in zip(("positive", "negative"), nets):
                entry = measured.get(leg) or {}
                length = entry.get("length_mil")
                status = entry.get("status") or ("ok" if length else "unrouted")
                facts = {"net": net, "length_mil": length, "status": status}
                facts.update(
                    (key, entry[key]) for key in _LEG_KEYS if entry.get(key) is not None
                )
                legs.append(facts)
            skew = measured.get("skew_mil")
            if skew is None and all(leg["length_mil"] is not None for leg in legs):
                skew = round(abs(legs[0]["length_mil"] - legs[1]["length_mil"]), 3)
            return legs[0], legs[1], skew, "pair_lengths"
    legs = []
    for net in nets:
        length = board.routed(net)
        if length is None:
            legs.append({"net": net, "length_mil": None, "status": "net_missing"})
        else:
            legs.append({
                "net": net, "length_mil": length,
                "status": "ok" if length > 0 else "unrouted",
                "pad_count": board.fanout(net), "copper_mil": length,
            })
    skew = None
    if all(leg["length_mil"] is not None for leg in legs):
        skew = round(abs(legs[0]["length_mil"] - legs[1]["length_mil"]), 1)
    return legs[0], legs[1], skew, "routed_length"


def _complete(leg: dict) -> bool:
    return leg["status"] == "ok" and bool(leg["length_mil"])


def _nothing_to_route(leg: dict) -> bool:
    """A leg with at most one pad: there is no connection to route."""
    pads = leg.get("pad_count")
    return pads is not None and pads <= 1 and leg["status"] not in _MISSING_STATUSES


def _has_copper(leg: dict) -> bool:
    return bool(leg.get("copper_mil") or leg["length_mil"]) or leg["status"] == "incomplete"


def _copper(leg: dict) -> float | None:
    for key in ("routed_mil", "copper_mil", "length_mil"):
        if leg.get(key) is not None:
            return leg[key]
    return None


def _leg_facts(leg: dict) -> dict:
    return {key: leg[key] for key in ("net", "status", *_LEG_KEYS, "length_mil") if key in leg}


_NET_SCOPE_NOTE = (
    "The rule selects the pair through its nets (e.g. InNetClass); Altium "
    "applies a within-pair length rule to differential pair objects, so it may "
    "not check this pair. Scope it with InDifferentialPair / "
    "InDifferentialPairClass to have it enforced."
)


def _check_skew(board: _Board, pairs: list, all_pairs: list, pair_lengths,
                regex) -> tuple[dict, list, list]:
    """(summary, findings, not_checked)."""
    index = board.index
    ctx = index.rule_context
    findings: list[dict] = []
    not_checked: list[dict] = []
    summary = Counter()
    summary["pairs"] = len(pairs)

    layers = index.signal_layers()
    in_pair_rules = []
    disabled = []
    for rule in index.pcb.rules:
        if rule.kind != "MatchedLengths":
            continue
        values = rule_values(rule, layers)
        if not values.get("within_pair"):
            continue
        if not rule.enabled:
            disabled.append(rule.name)
            continue
        in_pair_rules.append((rule, values))
    in_pair_rules.sort(key=lambda item: item[0].priority)

    targets = {}
    for pair in all_pairs:
        target = index.rule_target(pair.name, kind="diff_pair")
        if target is not None and target.kind == "diff_pair":
            targets[pair.name] = target
    checked_names = {pair.name for pair in pairs}

    rule_reports = []
    matches: dict[str, list[tuple]] = defaultdict(list)
    for rule, values in in_pair_rules:
        report = {
            "rule": rule.name,
            "scope": rule.scope1,
            "priority": rule.priority,
            "tolerance_mil": values.get("tolerance_mil"),
            "pairs_matched": 0,
            "pairs_conditional": 0,
            "pairs_checked": 0,
            "over_tolerance": 0,
        }
        stale = stale_references(rule.scope1, ctx)
        matched_any = False
        matched_checked = False
        scope_kinds: set[str] = set()
        for pair in all_pairs:
            target = targets.get(pair.name)
            if target is None:
                continue
            verdict = evaluate_scope(rule.scope1, target, ctx)
            if verdict["matches"] == "no":
                continue
            matched_any = True
            if pair.name not in checked_names:
                continue
            matched_checked = True
            if verdict["matches"] == "yes":
                scope_kind = _scope_kind(rule, target, ctx)
                scope_kinds.add(scope_kind)
                report["pairs_matched"] += 1
                matches[pair.name].append((rule, values, scope_kind))
            else:
                report["pairs_conditional"] += 1
                matches[pair.name].append(
                    (rule, values, "conditional", verdict.get("conditions", []))
                )
        if scope_kinds:
            report["scope_kind"] = scope_kinds.pop() if len(scope_kinds) == 1 else "mixed"
        relevant = (
            regex is None
            or matched_checked
            or bool(regex.search(rule.name) or regex.search(rule.scope1))
        )
        if stale:
            report["stale_references"] = stale
            summary["stale_rules"] += 1
            if relevant:
                findings.append(_finding(
                    "skew", "in_pair_rule_stale", "warning", None,
                    f"Within-pair length rule {rule.name} references "
                    f"{', '.join(stale)}, which does not exist on the PCB; the pairs it "
                    "was meant for are not checked.",
                    {"rule": rule.name, "scope": rule.scope1, "stale_references": stale},
                ))
        elif not matched_any:
            summary["stale_rules"] += 1
            report["matches_no_pair"] = True
            if relevant:
                findings.append(_finding(
                    "skew", "in_pair_rule_matches_no_pair", "warning", None,
                    f"Within-pair length rule {rule.name} matches no differential "
                    "pair on the PCB.",
                    {"rule": rule.name, "scope": rule.scope1},
                ))
        if values.get("use_delay_units"):
            report["not_evaluated"] = "delay units"
            not_checked.append({
                "check": "skew",
                "rule": rule.name,
                "reason": "Rule uses delay units; only length tolerances are evaluated.",
            })
        elif values.get("tolerance_mil") is None:
            report["not_evaluated"] = "no length tolerance"
        rule_reports.append(report)
    reports_by_rule = {report["rule"]: report for report in rule_reports}

    largest_without_rule = None
    source_used = None
    for pair in pairs:
        positive, negative, skew, source = _leg_lengths(board, pair, pair_lengths)
        source_used = source_used or source
        empty = [_nothing_to_route(positive), _nothing_to_route(negative)]
        if all(empty):
            # One pad (or none) per leg: nothing to route, nothing to judge.
            summary["single_pad_pairs"] += 1
            continue
        complete = [_complete(positive), _complete(negative)]
        lengths = {
            "positive_mil": positive["length_mil"],
            "negative_mil": negative["length_mil"],
        }
        pair_matches = matches.get(pair.name, [])
        decided = [match for match in pair_matches if match[2] != "conditional"]
        rule_names = [match[0].name for match in decided]
        legs = [_leg_facts(positive), _leg_facts(negative)]
        if any(empty):
            single = positive if empty[0] else negative
            summary["single_pad_legs"] += 1
            findings.append(_finding(
                "skew", "leg_single_pad", "info", pair.name,
                f"{single['net']} of {pair.name} has {single['pad_count']} pad(s), so "
                "it has no connection to route; in-pair skew cannot be judged.",
                {"single_pad_net": single["net"], "legs": legs, "in_pair_rules": rule_names},
                **lengths,
            ))
        elif not all(complete):
            summary["unrouted"] += 1
            both = not any(complete)
            severity = "warning" if decided and not both else "info"
            unrouted = [leg for leg, ok in zip((positive, negative), complete) if not ok]
            if both and any(_has_copper(leg) for leg in (positive, negative)):
                copper = [_copper(positive), _copper(negative)]
                amounts = (
                    f" ({positive['net']} {copper[0]:g} mil, {negative['net']} "
                    f"{copper[1]:g} mil of copper)"
                    if None not in copper else ""
                )
                message = f"Neither leg of {pair.name} is completely routed{amounts}."
            elif both:
                message = f"Neither leg of {pair.name} is routed."
            else:
                leg = unrouted[0]
                copper = _copper(leg)
                amount = f" ({copper:g} mil of copper)" if _has_copper(leg) and copper else ""
                message = (
                    f"{leg['net']} of {pair.name} is not (completely) routed{amount}; "
                    "in-pair skew cannot be judged."
                )
            details = {
                "unrouted_nets": [leg["net"] for leg in unrouted],
                "statuses": [positive["status"], negative["status"]],
                "legs": legs,
                "in_pair_rules": rule_names,
            }
            note = next((leg["note"] for leg in unrouted if leg.get("note")), None)
            if note:
                details["note"] = note
            findings.append(_finding(
                "skew", "pair_unrouted" if both else "leg_unrouted", severity, pair.name,
                message, details, **lengths,
            ))
        for match in pair_matches:
            if match[2] != "conditional":
                continue
            summary["conditional"] += 1
            findings.append(_finding(
                "skew", "in_pair_rule_conditional", "info", pair.name,
                f"Within-pair length rule {match[0].name} applies to {pair.name} only "
                "under conditions that cannot be decided for the whole pair; no verdict.",
                {"rule": match[0].name, "conditions": match[3]},
            ))
        if not decided:
            summary["without_in_pair_rule"] += 1
            if all(complete) and skew is not None and (
                largest_without_rule is None or skew > largest_without_rule["skew_mil"]
            ):
                largest_without_rule = {"pair": pair.name, "skew_mil": skew}
            continue
        summary["with_in_pair_rule"] += 1
        kinds = {match[2] for match in decided}
        if kinds & {"pair", "all"}:
            summary["pair_scoped"] += 1
        else:
            summary["net_scoped_only"] += 1
        if not all(complete) or skew is None:
            summary["no_verdict_unrouted"] += 1
            continue
        over = False
        evaluated = False
        for rule, values, scope_kind in decided:
            tolerance = values.get("tolerance_mil")
            report = reports_by_rule[rule.name]
            if values.get("use_delay_units") or tolerance is None:
                continue
            evaluated = True
            report["pairs_checked"] += 1
            report["max_skew_mil"] = max(report.get("max_skew_mil", 0.0), skew)
            if skew <= tolerance + 1e-6:
                continue
            over = True
            report["over_tolerance"] += 1
            excess = round(skew - tolerance, 3)
            # The verdict check_lengths gives: a violation whatever the scope
            # (a pair matched only through its nets carries a note), and
            # inconclusive (info) when marked marginal.
            details = {"rule": rule.name, "rule_scope": scope_kind, "approximate": True}
            if scope_kind == "net":
                summary["net_scoped_violations"] += 1
                details["note"] = _NET_SCOPE_NOTE
            severity = "warning"
            if excess <= MARGINAL_MIL:
                severity = "info"
                summary["marginal"] += 1
                details["marginal"] = True
                details["marginal_note"] = (
                    f"Exceeds the tolerance by at most {MARGINAL_MIL:g} mil, within the "
                    "uncertainty of the length estimate (check_lengths marks it marginal "
                    "too): inconclusive, confirm with Altium's DRC."
                )
            findings.append(_finding(
                "skew", "in_pair_skew_exceeds_tolerance", severity, pair.name,
                f"{pair.name}: legs {positive['length_mil']} / {negative['length_mil']} "
                f"mil, skew {skew} mil exceeds {rule.name}'s tolerance {tolerance} mil.",
                details,
                **lengths,
                skew_mil=skew,
                tolerance_mil=tolerance,
                excess_mil=excess,
            ))
        if over:
            summary["over_tolerance"] += 1
        elif evaluated:
            summary["within_tolerance"] += 1

    result = {
        key: summary[key]
        for key in (
            "pairs", "with_in_pair_rule", "pair_scoped", "net_scoped_only",
            "conditional", "over_tolerance", "marginal", "net_scoped_violations",
            "within_tolerance", "unrouted", "single_pad_legs", "single_pad_pairs",
            "no_verdict_unrouted", "without_in_pair_rule", "stale_rules",
        )
    }
    result["length_source"] = source_used or (
        "pair_lengths" if pair_lengths is not None else "routed_length"
    )
    result["in_pair_rules"] = rule_reports
    if disabled:
        result["disabled_in_pair_rules"] = disabled
    if largest_without_rule:
        result["largest_skew_without_rule"] = largest_without_rule
    return result, findings, not_checked


# ---------- entry point ----------


def _approximations(run: list[str], length_source: str | None) -> list[str]:
    notes = []
    if "naming" in run:
        notes.append(
            "Pair-like names are found with a suffix tokenizer (P/N, +/-, T/C, H/L, "
            "DP/DM, POS/NEG, PLUS/MINUS); auto-named (Net<des>_<pin>), supply or "
            f"sense nets and rails (more than {RAIL_FANOUT} pads on a leg) are not "
            "reported as undeclared pairs; motor, speaker, LED, heater and similar "
            "load names are info, not warnings."
        )
    if "polarity" in run:
        notes.append(
            "Pin polarity is judged only on parts whose two leg pins have "
            "complementary names; connectors with numeric pins and PCB-only parts "
            "are counted as unjudged, so 0 swaps does not prove polarity. Shunt "
            "protection parts (ESD / TVS to a supply) are not judged; pass-through "
            "parts (chokes, filters, muxes) are judged by comparing their two sides."
        )
        notes.append(
            "interface_hint is a name-based guess; confirm inversion tolerance in "
            "the datasheets."
        )
    if "series" in run:
        notes.append(
            "Series parts are two-pad parts between a leg and a far net with at most "
            f"{RAIL_FANOUT} pads (not a supply); parts whose far nets join both legs "
            "(common-node terminations) are excluded. Parts are paired across the "
            "legs by far-net pair or names, a common part reached, or value and "
            "footprint; an unpaired part that leads nowhere the partner leg goes is "
            "a shunt (pull-up, bias, stub), and unpaired resistors of "
            f"{BIAS_OHMS / 1000:g}k or more and DNP parts are info. Values compare the "
            "Value parameter numerically (100n = 100 nF = 0.1uF), else the "
            "Comment/MPN text."
        )
    if "skew" in run:
        if length_source == "pair_lengths":
            notes.append("Leg lengths come from the pad-to-pad length engine; see its approximations.")
        else:
            notes.append(
                "Leg length = all track and arc copper of the net, including segments "
                "inside pads and any stubs or branches; via barrels and pours are excluded."
            )
        notes.append(
            "Skew over a tolerance gets the verdict check_lengths gives: a warning, "
            f"or info marked marginal (inconclusive) within {MARGINAL_MIL:g} mil of the "
            "tolerance; a rule that selects the pair only through its nets carries a "
            "note. Legs with one pad have nothing to route and are not reported as "
            "unrouted."
        )
    return notes


def check_diff_pairs(
    index,
    netlist: dict | None,
    is_dnp=None,
    checks: list[str] | None = None,
    pattern: str | None = None,
    pair_lengths=None,
    offset: int = 0,
    limit: int = DEFAULT_LIMIT,
) -> dict:
    """Run differential-pair consistency checks on a PcbIndex.

    netlist is the enriched netlist (pin names, values); is_dnp(refdes) tells
    whether a schematic part is DNP in the active variant; pair_lengths(name)
    returns {"positive": {"length_mil", "status"}, "negative": {...},
    "skew_mil"} (defaults to whole-net routed length). pattern is a
    case-insensitive regex on pair and net names. Findings are sorted
    warnings first and paginated with offset / limit.
    """
    if isinstance(checks, str):
        checks = [part for part in re.split(r"[,\s]+", checks) if part]
    requested = [str(check).strip().lower() for check in (checks or CHECKS)]
    unknown = [
        check for check in requested
        if check not in CHECKS and check not in DEFERRED_CHECKS
    ]
    if unknown:
        return {
            "error": "invalid_checks",
            "message": f"Unknown check(s): {', '.join(unknown)}.",
            "valid_checks": [*CHECKS, *DEFERRED_CHECKS],
        }
    regex = None
    if pattern:
        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error:
            return {"error": "invalid_pattern", "message": f"Invalid regex: {pattern}"}

    run = [check for check in CHECKS if check in requested]
    all_pairs = list(index.pcb.diff_pairs)
    pairs = [
        pair for pair in all_pairs
        if regex is None
        or regex.search(pair.name)
        or regex.search(pair.positive_net)
        or regex.search(pair.negative_net)
    ]
    board = _Board(index, netlist, is_dnp)
    has_pin_names = any(
        isinstance(part, dict) and part.get("pins")
        for part in board.netlist_components.values()
    )

    summary: dict = {}
    findings: list[dict] = []
    not_checked: list[dict] = []
    length_source = None
    if "naming" in run:
        summary["naming"], found = _check_naming(board, pairs, all_pairs, regex)
        findings.extend(found)
    if "polarity" in run:
        if has_pin_names:
            summary["polarity"], found = _check_polarity(board, pairs, all_pairs)
            findings.extend(found)
        else:
            not_checked.append({
                "check": "polarity",
                "reason": "No schematic pin names: the netlist is not loaded or not "
                "enriched from the schematic sheets.",
            })
    if "series" in run:
        summary["series"], found = _check_series(board, pairs, all_pairs)
        findings.extend(found)
        if not board.netlist_components:
            not_checked.append({
                "check": "series",
                "reason": "No netlist component data: series part values are not compared.",
            })
        if is_dnp is None:
            not_checked.append({
                "check": "series",
                "reason": "No variant information: DNP asymmetry is not checked.",
            })
    if "skew" in run:
        summary["skew"], found, skipped = _check_skew(
            board, pairs, all_pairs, pair_lengths, regex
        )
        findings.extend(found)
        not_checked.extend(skipped)
        length_source = summary["skew"]["length_source"]
        not_checked.append({
            "check": "skew",
            "reason": "Uncoupled length against DiffPairsRouting max uncoupled length "
            "is not estimated here: check_diff_pair_geometry measures it.",
        })
    for check, reason in DEFERRED_CHECKS.items():
        if check in requested or checks is None or not checks:
            not_checked.append({"check": check, "reason": reason})

    findings.sort(key=lambda finding: (
        _SEVERITY_RANK.get(finding["severity"], 9),
        _CHECK_RANK.get(finding["check"], 9),
        finding["pair"] or "",
        finding["issue"],
    ))
    severity_counts = Counter(finding["severity"] for finding in findings)
    offset = max(0, int(offset or 0))
    limit = max(1, int(limit or DEFAULT_LIMIT))
    page = findings[offset : offset + limit]
    meta = {
        "total": len(findings),
        "offset": offset,
        "has_more": offset + limit < len(findings),
    }
    if meta["has_more"]:
        meta["next_offset"] = offset + limit
    result: dict = {
        "pairs_total": len(all_pairs),
        "pairs_checked": len(pairs),
        "checks_run": run,
        "summary": summary,
        "severity_counts": {
            "warning": severity_counts.get("warning", 0),
            "info": severity_counts.get("info", 0),
        },
        "findings": page,
        **meta,
        "not_checked": not_checked,
        "approximations": _approximations(run, length_source),
    }
    if not all_pairs:
        result["note"] = (
            "The PCB defines no differential pairs; only undeclared pair-like net "
            "names can be reported."
        )
    return result
