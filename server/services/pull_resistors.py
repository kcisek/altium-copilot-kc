"""Floating-net and pull-up / pull-down audit, with I2C pull-up sizing (heuristic).

Nets that need a defined default state are found by net names, the pin names of the
parts on them and the pins' electrical types from the schematic library: reset, enable,
shutdown, boot, strap and mode nets; open-drain nets (OpenCollector or OpenEmitter pins,
I2C, interrupt, alert, power-good and fault names); MOSFET gates and BJT bases driven from
an IC; comparator outputs; and, when asked, connector pins wired straight to IC inputs.
Nets joined by a series resistor under 1 kohm (a 0 ohm link, a damping resistor) are one
signal: a pull or a push-pull driver on either side holds both. A pull is a resistor (or a
resistor array section) from the signal to a rail (pull-up) or a ground (pull-down), rails
and grounds read the way check_decoupling reads them; an open-drain or I2C signal may be
pulled up to another net through 1 kohm or more (a supply switched by a GPIO). I2C SDA and
SCL signals pair into buses by name, else by the parts they share, and their pull-ups are
sized against NXP UM10204: Rmin = (VDD - VOL) / IOL, Rmax = tr / (0.8473 * Cb).

Calibrated on Altium's public examples and private boards: names alone call too many
reset and enable nets that ICs pull internally, so a missing pull is a warning only on
an OpenCollector/OpenEmitter pin, an I2C line or a MOSFET gate driven from an IC, and on
none of them when the net leaves the board through a connector (the pull may be on the
other board). Everything here is a heuristic: a net named otherwise, or a library that
types every pin Passive, is missed; pull-ups inside ICs are invisible; the bus
capacitance is an estimate unless given; and each part's datasheet decides.
"""

import re
from collections import defaultdict
from dataclasses import dataclass, field

try:
    from services.bom import build_parts, natural_key
    from services.bom_checks import normalize_value
    from services.decoupling import (
        IC_PREFIXES, capacitance, classify_nets, designator_prefix, first_function,
        net_tokens, pin_role,
    )
    from services.pcb_length import net_length
except ImportError:
    from server.services.bom import build_parts, natural_key
    from server.services.bom_checks import normalize_value
    from server.services.decoupling import (
        IC_PREFIXES, capacitance, classify_nets, designator_prefix, first_function,
        net_tokens, pin_role,
    )
    from server.services.pcb_length import net_length

CHECKS = (
    "open_drain_no_pull", "control_no_pull", "gate_no_pull", "comparator_no_pull",
    "not_fitted_pull", "i2c_pull_value", "parallel_pull_ups", "pull_up_and_down",
    "connector_no_pull",
)
# Run only when named in checks: on the examples most connector pins wired to IC inputs
# were memory and transceiver buses, which never want a pull.
OPT_IN_CHECKS = frozenset({"connector_no_pull"})
_CHECK_RANK = {check: rank for rank, check in enumerate(CHECKS)}
_SEVERITY_RANK = {"warning": 0, "info": 1}
_CLASS_CHECK = {
    "i2c": "open_drain_no_pull", "open_drain": "open_drain_no_pull",
    "control": "control_no_pull", "gate": "gate_no_pull", "base": "gate_no_pull",
    "comparator": "comparator_no_pull", "connector": "connector_no_pull",
}
# The class a signal's finding is filed under when it has several.
_CLASS_ORDER = ("i2c", "open_drain", "gate", "comparator", "control", "base", "connector")

FINDING_LIMIT = 100
NET_LIMIT = 60
BUS_LIMIT = 20
LINK_OHMS = 1000.0  # a series resistor below this joins two nets into one signal
SWITCHED_PULL_OHMS = 1000.0  # an open-drain net's resistor of this or more to another net
GROUP_LIMIT = 8  # nets joined into one signal at most

# ---------- I2C (NXP UM10204, rev. 7) ----------

I2C_MODES = {
    # mode: (rise time tr in ns, IOL in mA at VOL 0.4 V, maximum bus capacitance in pF)
    "standard": (1000.0, 3.0, 400.0),
    "fast": (300.0, 3.0, 400.0),
    "fast_plus": (120.0, 20.0, 550.0),
}
DEFAULT_I2C_MODE = "fast"
I2C_VOL = 0.4
RISE_FACTOR = 0.8473  # ln(0.7 / 0.3): tr from 30% to 70% of VDD on an RC
PIN_PF = 10.0  # UM10204's maximum capacitance of one I/O pin
TRACE_PF_PER_MM = 0.12  # about 3 pF per inch: a 50 ohm microstrip on FR-4

# ---------- names ----------

_RESISTOR_PREFIXES = frozenset({"R", "RN", "RA", "RES"})
# Other R prefixes (RP, RS, RB on real boards) are resistors when their text says so;
# these never are: varistors, thermistors, relays.
_NOT_RESISTOR_PREFIXES = frozenset({"RV", "RT", "RTH", "RLY", "RY", "REL"})
_NOT_RESISTOR_TEXT = re.compile(r"thermistor|varistor|\bntc\b|\bptc\b|potentiometer|trimmer|"
                                r"relay|rheostat", re.I)
_RESISTOR_TEXT = re.compile(r"resist|\bres\b|\bohms?\b|Ω|\d\s*[kKmM]?\s*(?:ohm|Ω)", re.I)
TRANSISTOR_PREFIXES = frozenset({"Q", "T", "TR", "FET"})
CONNECTOR_PREFIXES = frozenset({"J", "P", "CN", "X", "CON", "CONN"})
# Parts whose pin names say nothing about the net (resistor pins 1 and 2, a diode's A/K).
_PASSIVE_PREFIXES = frozenset({
    "R", "RN", "RA", "RES", "C", "L", "FB", "FL", "D", "LED", "TP", "F", "Y", "TVS", "ESD",
    "Z", "RV", "RT", "VR",
})
NOT_CONNECTOR_TEXT = re.compile(r"crystal|xtal|oscillat|resonator|transformer", re.I)
COMPARATOR_TEXT = re.compile(r"comparator", re.I)
DRIVER_TEXT = re.compile(r"gate driver|mosfet driver|fet driver|half[- ]bridge driver|"
                         r"igbt driver|low[- ]side driver|high[- ]side driver", re.I)

_NEGATION = re.compile(r"^[~!#*]+|[~!#*]+$")
_CONTROL_WORDS = {
    "reset": r"RST|RESET|PERST|SRST|TRST|SYSRST|MRST|POR|XRES|XRST",
    "enable": r"EN|ENA|ENABLE|CE|OE",
    "shutdown": r"SHDN|SHUTDOWN|PWDN|PWRDN|PDN|STBY|STANDBY|SLEEP|SLP",
    "boot": r"BOOT|BOOTSEL|BOOTMODE",
    "strap": r"STRAP|CFG|CONFIG",
    "mode": r"MODE",
    "program": r"PROG|PROGRAM",
}
_CONTROL = {kind: re.compile(rf"(?:{words})\d*") for kind, words in _CONTROL_WORDS.items()}
_PIN_ONLY_CONTROL = re.compile(r"RUN\d*")  # an LTC regulator's enable; as a net name, a LED
# A data-path enable (an Ethernet MAC's TX_EN) is driven all the time, not a control.
_DATA_PATH = frozenset({"TX", "RX", "TXD", "RXD", "GTX", "MII", "RGMII", "RMII"})
_OPEN_DRAIN = re.compile(
    r"(?:INT|INTR|IRQ|ALERT|SMBALERT|FAULT|FLT|PGOOD|PWRGD|PWROK|POK|PGD)\d*|PG"
)
_PIN_ONLY_OPEN_DRAIN = re.compile(r"(?:STAT|CHG|CHRG|FLAG|THERM|OTW|EVENT)\d*")
_I2C = re.compile(r"(?P<head>I2C\d*|IIC\d*|TWI\d*|SMB|PMB)?(?P<line>SDA|SCL)(?P<tail>\d*)")
_SMBUS = re.compile(r"(?P<head>SMB|SMBUS|PMB|PMBUS)(?P<line>DAT|DATA|CLK)(?P<tail>\d*)")
_SPI = frozenset({"SCLK", "SCK", "MOSI", "MISO", "SDO", "SDI", "DOUT", "DIN", "SPC", "SPI",
                  "CS", "SS", "NCS", "CSN", "QSPI"})
_GATE_PIN = re.compile(r"G|GATE|G\d")
_BASE_PIN = re.compile(r"B|BASE|B\d")
_SOURCE_PIN = re.compile(r"S|SOURCE|S\d|E|EMITTER|E\d")
_DRIVER_PIN = re.compile(r"HO|LO|HG|LG|GH|GL|DH|DL|UG|TG|BG|GATE\w*|GDRV\w*|DRV\w*|GD\w*")
_REGULATOR_SWITCH = re.compile(r"SW\d*|LX\d*|PH\d*|VSW\d*")
_BOOTSTRAP = re.compile(r"BOOT|BST|CBOOT|BS|CB")
DIFF_HALF = re.compile(r"(_|-)(P|N|\+|-)$|^(D[PM]|D[+-])$|[+-]$")
_VOLTAGE = (
    # 3V3, 1V8, 5V, 12V, 3V3A; not the 8V of 1.8V, 1,8V or 1P8V (the next pattern's).
    re.compile(r"(?<![\d.,])(?<!\dP)(\d{1,2})V(\d{1,3})?(?!\d)"),
    re.compile(r"(?<![\d.])(\d{1,2})[.,P](\d{1,3})V?(?!\d)"),  # 3.3V, 3P3, 1,8V, 1P8V
    re.compile(r"^(?:VDD|VCC|VIO|VCCIO|VDDIO|DVDD|AVDD)(\d)(\d)(?!\d)"),  # VDD33, VCC18
)
# The two digits after VDD or VCC read as volts and tenths only for the usual rails
# (VDD33, VCC18, VDD09): a VCC36 or VCC48 is not 3.6 V or 4.8 V.
_TENTHS_RAILS = frozenset({"08", "09", "10", "11", "12", "15", "18", "25", "28", "33"})


def _forms(token: str) -> list[str]:
    """A name token and its forms without a negation mark: NRST, RSTN, RST#, ~RST, XRES."""
    token = _NEGATION.sub("", token)
    forms = [token]
    if len(token) > 2 and token[0] in "NX":
        forms.append(token[1:])
    if len(token) > 2 and token[-1] in "NB":
        forms.append(token[:-1])
    return forms


def _match(tokens: list[str], pattern: re.Pattern) -> str | None:
    for token in tokens:
        for form in _forms(token):
            if pattern.fullmatch(form):
                return form
    return None


def control_kind(name: str | None, pin: bool = False) -> str | None:
    """reset, enable, shutdown, boot, strap, mode or program, from a net or pin name
    (NRST, RESET_N, PWR_EN, ~SHDN, BOOT0, STRAP1, PROG_B); None otherwise."""
    tokens = net_tokens(name or "")
    if set(tokens) & _DATA_PATH:
        return None
    for kind, pattern in _CONTROL.items():
        if _match(tokens, pattern):
            return kind
    if pin and _match(tokens, _PIN_ONLY_CONTROL):
        return "enable"
    return None


def open_drain_name(name: str | None, pin: bool = False) -> bool:
    """An interrupt, alert, power-good or fault name (INT, nIRQ, ALERT#, PGOOD, FAULT);
    as a pin name also a charger's or sensor's status flag (STAT, CHG, FLAG, THERM)."""
    tokens = net_tokens(name or "")
    return bool(_match(tokens, _OPEN_DRAIN) or (pin and _match(tokens, _PIN_ONLY_OPEN_DRAIN)))


def i2c_line(name: str | None) -> tuple[str, str] | None:
    """('SDA' or 'SCL', the name with the line replaced by '*') for an I2C or SMBus
    name: I2C1_SDA, SDA0, SCL, SMBCLK, PMB_DATA; None otherwise (SCLK is SPI)."""
    tokens = net_tokens(name or "")
    for index, token in enumerate(tokens):
        for form in _forms(token):
            match = _I2C.fullmatch(form) or _SMBUS.fullmatch(form)
            if match:
                line = {"DAT": "SDA", "DATA": "SDA", "CLK": "SCL"}.get(match["line"],
                                                                        match["line"])
                stem = f"{match['head'] or ''}*{match['tail']}"
                return line, "_".join([*tokens[:index], stem, *tokens[index + 1:]])
        if token in ("SMB", "SMBUS", "PMB", "PMBUS") and index + 1 < len(tokens) and \
                tokens[index + 1] in ("DAT", "DATA", "CLK"):
            line = "SCL" if tokens[index + 1] == "CLK" else "SDA"
            return line, "_".join([*tokens[:index], f"{token}*", *tokens[index + 2:]])
    return None


def rail_voltage(net: str) -> float | None:
    """Volts from a rail's name: 3V3, +3.3V, 1V8, 5V, VDD_3V3, P3V3, VCC_1.8V, 1P8V, VDD33;
    None when the name holds no voltage (VCC, VBUS, VBAT, VCC36) or a negative one."""
    text = net.upper().replace("\\", "")
    if text.strip().startswith("-"):
        return None
    for token in re.split(r"[_\s/]+", text):
        token = token.lstrip("+")
        for index, pattern in enumerate(_VOLTAGE):
            match = pattern.search(token)
            if match:
                whole, fraction = match.group(1), match.group(2) or ""
                value = float(f"{whole}.{fraction}" if fraction else whole)
                if index == 2 and whole + fraction not in _TENTHS_RAILS:
                    continue  # VDD60, VCC36: not a two-digit voltage
                if 0 < value <= 60:
                    return value
    return None


def ohms_text(ohms: float | None) -> str | None:
    if ohms is None:
        return None
    for unit, scale in (("M", 1e6), ("k", 1e3)):
        if ohms >= scale:
            return f"{ohms / scale:.4g}{unit}"
    return f"{ohms:.4g}"


_OHM_TEXT = re.compile(
    r"(?<![\w.])(\d+(?:[.,]\d+)?)\s*([kKmM]|meg)?\s*(?:ohms?|Ω|R\b)"
    r"|(?<![\w.])(\d{1,3})([kKmMR])(\d{1,3})(?![\w.])"
    r"|(?<![\w.])(\d+(?:[.,]\d+)?)([kKM])(?![\w.])",
    re.IGNORECASE,
)
_OHM_SCALE = {"": 1.0, "r": 1.0, "k": 1e3, "m": 1e6, "meg": 1e6}
# A value that is arithmetic on numbers ('(12-2.7)/1m', '2/10m'), as simulation parts carry.
_EXPRESSION = re.compile(r"[\s\d.,()+\-*/^kKmMuUnNpPrRgG]+")
_OPERATOR = re.compile(r"[\d)]\s*[-+*/^]\s*[\d(.]")


def resistance(*texts: str | None) -> float | None:
    """Ohms from the first text holding a resistance: '10k', '4K7', '100R', '10 kOhm',
    'RES 10K 1% 0402'. normalize_value reads a value field; descriptions are searched. A
    simulation expression ('(12-2.7)/1m') is no resistance."""
    texts = tuple(text for text in texts if not (text and _EXPRESSION.fullmatch(text)
                                                 and _OPERATOR.search(text)))
    for text in texts:
        value = normalize_value(text)
        if isinstance(value, float):
            return value
    for text in texts:
        for match in _OHM_TEXT.finditer(text or ""):
            if match.group(1):
                number, prefix = match.group(1), match.group(2) or ""
                if prefix == "m":
                    continue  # milliohm: a shunt
            elif match.group(3):
                number, prefix = f"{match.group(3)}.{match.group(5)}", match.group(4)
            else:
                number, prefix = match.group(6), match.group(7)
            try:
                return float(number.replace(",", ".")) * _OHM_SCALE.get(prefix.lower(), 1.0)
            except ValueError:
                continue
    return None


# ---------- the design ----------

@dataclass
class Pull:
    resistor: str
    net: str
    other: str  # the rail, the ground, a gate's source net or a switched supply
    direction: str  # up, down, or source (a gate-source resistor)
    ohms: float | None
    fitted: bool
    pins: str | None = None  # a resistor array's section, e.g. '2-7'
    switched: bool = False  # up to a net that is not a rail (a GPIO-switched supply)

    def describe(self) -> dict:
        out: dict = {"designator": self.resistor, "direction": self.direction,
                     "to": self.other, "value": ohms_text(self.ohms)}
        if self.net:
            out["on"] = self.net
        if self.pins:
            out["pins"] = self.pins
        if self.switched:
            out["to_signal"] = True
        if not self.fitted:
            out["not_fitted"] = True
        return out


@dataclass
class _Net:
    """One signal: a classified net and the nets joined to it by links."""
    name: str
    classes: dict[str, list[str]] = field(default_factory=dict)  # class -> reasons
    control_kind: str | None = None
    group: list[str] = field(default_factory=list)  # the signal's nets, name first
    extra: list[str] = field(default_factory=list)  # a gate's driving nets through a resistor
    drivers: list[str] = field(default_factory=list)  # push-pull outputs, 'U1.5 (OUT)'
    offboard: list[str] = field(default_factory=list)  # connectors on the signal
    # Every part pin on it is an Input and no series resistor feeds it from another
    # signal: nothing can drive it, not even a GPIO in reset.
    undriven: bool = False

    def add(self, kind: str, reason: str) -> None:
        reasons = self.classes.setdefault(kind, [])
        if reason not in reasons and len(reasons) < 4:
            reasons.append(reason)

    def main_class(self) -> str:
        return next(kind for kind in _CLASS_ORDER if kind in self.classes)


@dataclass
class _Design:
    nets: dict[str, list[tuple[str, str]]]
    grounds: set[str]
    rails: dict[str, list[str]]
    pulls: dict[str, list[Pull]]
    series: dict[str, list[tuple[str, str, float | None]]]  # net -> fitted (R, other, ohms)
    caps: dict[str, list[tuple[str, float | None]]]  # net -> fitted capacitors to ground
    fitted: dict[str, bool]
    pin_name: object
    pin_type: object
    text: dict[str, str]  # part -> description, comment, value and library reference
    part_pins: dict[str, dict[str, str]]  # part -> pin -> net
    resistors: set[str] = field(default_factory=set)
    stats: dict = field(default_factory=lambda: defaultdict(int))

    def fixed(self, net: str) -> bool:
        return net in self.grounds or net in self.rails


def is_resistor(prefix: str, text: str) -> bool:
    if prefix in _RESISTOR_PREFIXES:
        return True
    return (prefix.startswith("R") and prefix not in _NOT_RESISTOR_PREFIXES
            and bool(_RESISTOR_TEXT.search(text)) and not _NOT_RESISTOR_TEXT.search(text))


def resistor_sections(meta: dict, connected: dict[str, str]) -> list[tuple]:
    """(pin a, net a, pin b, net b, section label) for each resistor of a part: a two-pin
    resistor, an array of an even count of numbered pins paired 1 with N, 2 with N-1 (the
    usual isolated array) or a bussed array of an odd count with one common pin (pin a
    'common', resolved by the caller)."""
    pins = [pin for pin in meta.get("pin_designators") or connected if pin]
    if len(pins) <= 2:
        items = list(connected.items())
        if len(items) == 2:
            (pin_a, net_a), (pin_b, net_b) = items
            return [(pin_a, net_a, pin_b, net_b, None)]
        return []
    if not all(pin.isdigit() for pin in pins):
        return []
    count = len(pins)
    if sorted(int(pin) for pin in pins) != list(range(1, count + 1)):
        return []
    if count % 2 == 0:
        out = []
        for low in range(1, count // 2 + 1):
            high = count + 1 - low
            net_a, net_b = connected.get(str(low)), connected.get(str(high))
            if net_a and net_b:
                out.append((str(low), net_a, str(high), net_b, f"{low}-{high}"))
        return out
    return [("common", None, pin, net, pin) for pin, net in connected.items()]


def build_design(netlist: dict, plan, project_params: dict | None) -> _Design:
    components = netlist.get("components") or {}
    compiled = netlist.get("parts") or {}
    classes = classify_nets(netlist)
    pin_to_net = netlist.get("pin_to_net") or {}
    parts = {part.designator: part for part in build_parts(netlist, plan, project_params)[0]}
    not_fitted = getattr(plan, "not_fitted", set()) or set()
    fitted = {ref: parts[ref].fitted if ref in parts else ref not in not_fitted
              for ref in classes.refs}

    def pin_name(ref: str, pin: str) -> str:
        return (((components.get(ref) or {}).get("pins") or {}).get(pin) or {}).get("name") or ""

    def pin_type(ref: str, pin: str) -> str | None:
        return ((components.get(ref) or {}).get("pin_types") or {}).get(pin)

    text: dict[str, str] = {}
    for ref in classes.refs:
        meta = components.get(ref) or {}
        part = parts.get(ref)
        text[ref] = " ".join(filter(None, (
            meta.get("description"), meta.get("lib_reference"), meta.get("value"),
            (compiled.get(ref) or {}).get("comment"), part.comment if part else None,
        )))
    rails = dict(classes.rails)
    for net, members in classes.nets.items():
        # A supply pin of any part (a power module's VOUT, a connector's 3V3) makes a rail
        # for pulls too: real boards pull a module's power-good up to its own output.
        if net in rails or net in classes.grounds or classes.kinds[net]["status"]:
            continue
        for ref, pin in members:
            prefix = designator_prefix(ref)
            if prefix in _PASSIVE_PREFIXES or is_resistor(prefix, text.get(ref, "")):
                continue
            role = pin_role(pin_name(ref, pin))
            if role == "supply" or (pin_type(ref, pin) == "Power" and role not in (
                    "ground", "other") and not _REGULATOR_SWITCH.fullmatch(
                        first_function(pin_name(ref, pin)))):
                rails[net] = ["part_pin"]
                break
    design = _Design(classes.nets, classes.grounds, rails, defaultdict(list),
                     defaultdict(list), defaultdict(list), fitted, pin_name, pin_type, text,
                     pin_to_net)
    for ref in classes.refs:
        prefix = designator_prefix(ref)
        connected = pin_to_net[ref]
        meta = components.get(ref) or {}
        part = parts.get(ref)
        if prefix == "C" and len(set(connected.values())) == 2 and fitted[ref]:
            net_a, net_b = list(dict.fromkeys(connected.values()))
            farads = capacitance(part.value if part else meta.get("value"),
                                 part.comment if part else None,
                                 part.description if part else meta.get("description"))
            for net, other in ((net_a, net_b), (net_b, net_a)):
                if other in design.grounds:
                    design.caps[net].append((ref, farads))
            continue
        if not is_resistor(prefix, text[ref]):
            continue
        design.resistors.add(ref)
        design.stats["resistors"] += 1
        ohms = resistance(part.value if part else meta.get("value"),
                          part.comment if part else (compiled.get(ref) or {}).get("comment"),
                          (meta.get("params") or {}).get("resistance"),
                          part.description if part else meta.get("description"))
        sections = resistor_sections(meta, connected)
        if sections and sections[0][0] == "common":
            # A bussed array: the common pin is the one on a rail or a ground.
            ends = [(pin, net) for *_, pin, net, _ in sections]
            commons = [(pin, net) for pin, net in ends if design.fixed(net)]
            if len(commons) != 1:
                continue
            common_pin, common_net = commons[0]
            sections = [(common_pin, common_net, pin, net, f"{common_pin}-{pin}")
                        for pin, net in ends if pin != common_pin]
        for _, net_a, _, net_b, label in sections:
            if net_a == net_b:
                continue
            for net, other in ((net_a, net_b), (net_b, net_a)):
                if design.fixed(net):
                    continue
                if other in design.grounds:
                    design.pulls[net].append(Pull(ref, net, other, "down", ohms, fitted[ref],
                                                  label))
                elif other in design.rails:
                    design.pulls[net].append(Pull(ref, net, other, "up", ohms, fitted[ref],
                                                  label))
                elif fitted[ref]:
                    design.series[net].append((ref, other, ohms))
    return design


# ---------- classification ----------

def _is_active(design: "_Design", ref: str) -> bool:
    return ref not in design.resistors and designator_prefix(ref) not in _PASSIVE_PREFIXES


def _is_connector(design: _Design, ref: str) -> bool:
    if designator_prefix(ref) not in CONNECTOR_PREFIXES:
        return False
    return not NOT_CONNECTOR_TEXT.search(design.text.get(ref, ""))


def _bootstrap(design: _Design, net: str, ref: str) -> bool:
    """A regulator's BOOT/BST pin: its IC has a switch-node pin, or a capacitor joins the
    net to another signal net."""
    for pin in design.part_pins.get(ref) or ():
        if _REGULATOR_SWITCH.fullmatch(first_function(design.pin_name(ref, pin))):
            return True
    return any(
        designator_prefix(other) == "C" and any(
            other_net != net and not design.fixed(other_net)
            for other_net in (design.part_pins.get(other) or {}).values()
        )
        for other, _ in design.nets[net]
    )


def link_group(design: _Design, net: str) -> list[str]:
    """The net and the nets joined to it by fitted series resistors under LINK_OHMS."""
    group, queue = [net], [net]
    while queue and len(group) < GROUP_LIMIT:
        current = queue.pop(0)
        for _, other, ohms in design.series.get(current, ()):
            if ohms is not None and ohms < LINK_OHMS and other not in group \
                    and not design.fixed(other):
                group.append(other)
                queue.append(other)
    return group


def push_pull_drivers(design: _Design, net: str) -> list[str]:
    """Push-pull outputs on a net: Output pins, and IC pins named like an output (OUT,
    VOUT) or a gate drive (GATE, DRV, HO, LO), unless they are open-drain, inputs or a
    comparator's (LM393 outputs are open-collector)."""
    out = []
    for ref, pin in design.nets.get(net, ()):
        if not _is_active(design, ref):
            continue
        name = first_function(design.pin_name(ref, pin))
        electrical = design.pin_type(ref, pin)
        if electrical == "Output" or (
            designator_prefix(ref) in IC_PREFIXES
            and electrical not in ("Input", "OpenCollector", "OpenEmitter", "HiZ")
            and (pin_role(name) == "out" or _DRIVER_PIN.fullmatch(name or "-"))
            and not COMPARATOR_TEXT.search(design.text.get(ref, ""))
        ):
            out.append(f"{ref}.{pin} ({name})" if name else f"{ref}.{pin}")
    return out


def classify_net(design: _Design, net: str) -> _Net | None:
    entry = _Net(net)
    members = design.nets[net]
    spi = bool(set(net_tokens(net)) & _SPI)
    line = i2c_line(net)
    if line:
        entry.add("i2c", f"net name ({line[0]})")
    kind = control_kind(net)
    if kind == "boot" and _BOOTSTRAP.fullmatch(net_tokens(net)[-1] if net_tokens(net) else "") \
            and any(_bootstrap(design, net, ref) for ref, _ in members):
        kind = None
    if kind:
        entry.add("control", f"net name ({kind})")
        entry.control_kind = kind
    if open_drain_name(net):
        entry.add("open_drain", "net name")
    for ref, pin in members:
        if not _is_active(design, ref):
            continue
        label = f"{ref}.{pin}"
        name = first_function(design.pin_name(ref, pin))
        electrical = design.pin_type(ref, pin)
        if electrical in ("OpenCollector", "OpenEmitter"):
            entry.add("open_drain", f"{label} ({name or 'pin'}) is {electrical}")
        prefix = designator_prefix(ref)
        if prefix in TRANSISTOR_PREFIXES:
            if _GATE_PIN.fullmatch(name):
                entry.add("gate", f"{ref} gate")
            elif _BASE_PIN.fullmatch(name):
                entry.add("base", f"{ref} base")
            continue
        if not name:
            continue
        if i2c_line(name) and not spi:
            entry.add("i2c", f"{label} ({name})")
        control = control_kind(name, pin=True)
        if control == "boot" and _BOOTSTRAP.fullmatch(name) and _bootstrap(design, net, ref):
            control = None
        if control:
            entry.add("control", f"{label} ({name})")
            entry.control_kind = entry.control_kind or control
        if open_drain_name(name, pin=True):
            entry.add("open_drain", f"{label} ({name})")
        if COMPARATOR_TEXT.search(design.text.get(ref, "")) and (
            pin_role(name) == "out" or name.startswith("OUT")
        ):
            entry.add("comparator", f"{ref} output {pin} ({name})")
    return entry if entry.classes else None


def _ic_signal_pins(design: _Design, net: str) -> list[tuple[str, str]]:
    """IC pins that can drive a net: not inputs, supplies or references."""
    return [(ref, pin) for ref, pin in design.nets.get(net, ())
            if designator_prefix(ref) in IC_PREFIXES
            and design.pin_type(ref, pin) not in ("Input", "Power")
            and pin_role(design.pin_name(ref, pin)) not in ("supply", "ground", "cap", "ref")]


def merged_signals(design: _Design) -> list[_Net]:
    """Classified signals: each classified net with the nets linked to it, merged."""
    found = {}
    for net in design.nets:
        if not design.fixed(net) and (entry := classify_net(design, net)):
            found[net] = entry
    signals: list[_Net] = []
    done: set[str] = set()
    for net in sorted(found, key=lambda name: (name.upper().startswith("NET"),
                                               natural_key(name))):
        if net in done:
            continue
        group = link_group(design, net)
        done.update(group)
        signal = _Net(net, group=group)
        for member in group:
            entry = found.get(member)
            if entry is None:
                continue
            for kind, reasons in entry.classes.items():
                for reason in reasons:
                    signal.add(kind, reason if member == net else f"{member}: {reason}")
            signal.control_kind = signal.control_kind or entry.control_kind
            signal.drivers += push_pull_drivers(design, member)
        for member in group:
            if member not in found:
                signal.drivers += push_pull_drivers(design, member)
            signal.offboard += [ref for ref, _ in design.nets.get(member, ())
                                if _is_connector(design, ref) and ref not in signal.offboard]
        active = [(ref, pin) for member in group for ref, pin in design.nets.get(member, ())
                  if _is_active(design, ref)]
        # A series resistor to another signal (an RC reset fed from a GPIO) may drive it.
        fed = any(other not in group and not design.fixed(other)
                  for member in group for _, other, _ in design.series.get(member, ()))
        signal.undriven = bool(active) and not fed and all(
            design.pin_type(ref, pin) == "Input" for ref, pin in active)
        if _gate_driven(design, signal):
            signals.append(signal)
        elif signal.classes:
            signals.append(signal)
    return signals


def _gate_driven(design: _Design, signal: _Net) -> bool:
    """A gate or base class stays only when an IC pin drives it, through links or one
    series resistor of any value; a gate driver's output is a push-pull driver. Returns
    True when the signal keeps a gate or base class."""
    if not ({"gate", "base"} & set(signal.classes)):
        return False
    extra = [other for member in signal.group for _, other, _ in design.series.get(member, ())
             if other not in signal.group and not design.fixed(other)]
    drivers = [(source, ref, pin) for source in [*signal.group, *extra]
               for ref, pin in _ic_signal_pins(design, source)]
    if not drivers:
        for kind in ("gate", "base"):
            signal.classes.pop(kind, None)
        design.stats["gates_not_driven_by_ic"] += 1
        return False
    signal.extra = list(dict.fromkeys(source for source, _, _ in drivers
                                      if source not in signal.group))
    for source in signal.extra:
        signal.drivers += push_pull_drivers(design, source)
    for source, ref, pin in drivers[:2]:
        via = "" if source in signal.group else f" through {source}"
        for kind in ("gate", "base"):
            if kind in signal.classes:
                signal.add(kind, f"driven by {ref}.{pin}{via}")
    return True


def _connector_signals(design: _Design, taken: set[str]) -> list[_Net]:
    """A connector pin wired straight to IC Input or IO pins, with nothing that drives
    the net: it floats when nothing is plugged in."""
    out = []
    for net, members in design.nets.items():
        if design.fixed(net) or net in taken or DIFF_HALF.search(net.upper()):
            continue
        connectors = [ref for ref, _ in members if _is_connector(design, ref)]
        if not connectors:
            continue
        inputs, other = [], False
        for ref, pin in members:
            prefix = designator_prefix(ref)
            if ref in connectors or prefix in ("C", "D", "TVS", "ESD", "Z", "TP"):
                continue
            if prefix in IC_PREFIXES and design.pin_type(ref, pin) in ("Input", "IO"):
                inputs.append(f"{ref}.{pin}")
                continue
            if any(pull.resistor == ref for pull in design.pulls.get(net, ())):
                continue
            other = True
            break
        if inputs and not other:
            signal = _Net(net, group=[net], offboard=connectors)
            signal.add("connector", f"{', '.join(connectors[:3])} to {', '.join(inputs[:3])}")
            out.append(signal)
    return out


def _pulls_of(design: _Design, signal: _Net, open_drain_names: set[str]) -> list[Pull]:
    out = [pull for net in signal.group for pull in design.pulls.get(net, ())]
    for net in signal.extra:
        out += design.pulls.get(net, [])
    if {"gate", "base"} & set(signal.classes):
        # A gate-source (base-emitter) resistor holds the transistor off too.
        sources = set()
        for ref, pin in design.nets[signal.name]:
            name = first_function(design.pin_name(ref, pin))
            if designator_prefix(ref) in TRANSISTOR_PREFIXES and (
                _GATE_PIN.fullmatch(name) or _BASE_PIN.fullmatch(name)
            ):
                for other_pin, other_net in (design.part_pins.get(ref) or {}).items():
                    if _SOURCE_PIN.fullmatch(first_function(design.pin_name(ref, other_pin))):
                        sources.add(other_net)
        for net in signal.group:
            for ref, other, ohms in design.series.get(net, ()):
                if other in sources:
                    out.append(Pull(ref, net, other, "source", ohms, True))
    if ({"i2c", "open_drain"} & set(signal.classes)) and not any(
        pull.fitted for pull in out
    ):
        # Pulled up to a supply a GPIO switches (or a supply not named like one): a net
        # that pulls up two or more open-drain or I2C lines through 1 kohm or more.
        for net in signal.group:
            for ref, other, ohms in design.series.get(net, ()):
                if ohms is not None and ohms >= SWITCHED_PULL_OHMS and \
                        other not in signal.group and other not in open_drain_names and \
                        pull_supply(design, other, open_drain_names):
                    out.append(Pull(ref, net, other, "up", ohms, True, switched=True))
    return out


def open_drain_nets(signals: list[_Net]) -> set[str]:
    """Every net of the open-drain and I2C signals, the nets linked to them included."""
    return {net for signal in signals if {"i2c", "open_drain"} & set(signal.classes)
            for net in signal.group}


def pull_supply(design: _Design, net: str, open_drain_names: set[str]) -> bool:
    """Whether net pulls up two or more open-drain or I2C lines through 1 kohm or more (a
    supply a GPIO switches, or one not named like a supply)."""
    lines = {other for _, other, ohms in design.series.get(net, ())
             if ohms is not None and ohms >= SWITCHED_PULL_OHMS and other in open_drain_names}
    return len(lines) >= 2


# ---------- findings ----------

def _finding(check: str, severity: str, net: str, issue: str, designators=(),
             **fields) -> dict:
    out: dict = {"check": check, "severity": severity, "net": net}
    if designators:
        out["designators"] = sorted(set(designators), key=natural_key)
    out.update({key: value for key, value in fields.items() if value not in (None, [], {})})
    out["issue"] = issue
    return out


def _parallel(values: list[float]) -> float:
    if min(values) <= 0:
        return 0.0  # a 0 ohm link ties the net to the rail
    return 1.0 / sum(1.0 / value for value in values)


_WHAT = {
    "i2c": "an I2C line with no pull-up",
    "open_drain": "an open-drain net with no pull-up",
    "control": "{article} {kind} net with no pull-up or pull-down",
    "gate": "a MOSFET gate driven from an IC with no pull resistor",
    "base": "a transistor base driven from an IC with no pull resistor",
    "comparator": "a comparator output with no pull-up",
    "connector": "a connector pin wired straight to IC inputs with no pull",
}
_ADVICE = {
    "i2c": "I2C needs a pull-up on SDA and SCL somewhere on the bus.",
    "open_drain": "an open-drain output only pulls low; without a pull-up the net floats "
                  "when released.",
    "control": "it floats while its driver is in reset or unpowered, unless the IC pulls "
               "it internally (check the datasheet).",
    "gate": "while the driving pin is in reset (high impedance) the gate floats and the "
            "MOSFET may turn partly on; a pull-down (N-channel) or a pull-up to the source "
            "(P-channel) holds it off.",
    "base": "the base floats while the driving pin is in reset; a base-emitter resistor "
            "holds the transistor off.",
    "comparator": "many comparators (LM393, LM339) have open-collector outputs that need a "
                  "pull-up; push-pull ones do not.",
    "connector": "the input floats when nothing is plugged in.",
}


def _no_pull_finding(signal: _Net) -> dict | None:
    kind = signal.main_class()
    typed = any(" is Open" in reason for reason in signal.classes.get("open_drain", ()))
    if signal.drivers and kind not in ("i2c",) and not (kind == "open_drain" and typed):
        return None  # a push-pull output drives it
    severity = "info"
    if kind == "i2c" or (kind == "open_drain" and typed) or kind == "gate":
        severity = "warning"
    advice = _ADVICE[kind]
    if signal.undriven:
        advice += " Every part pin on it is an input, so nothing ever drives it."
    if signal.offboard and kind != "connector":
        advice += (f" It reaches {', '.join(signal.offboard[:3])}: the pull may be on the "
                   "other board.")
        severity = "info"
    reasons = "; ".join(f"{name}: {', '.join(why)}" for name, why in signal.classes.items())
    control = signal.control_kind or "control"
    what = _WHAT[kind].format(kind=control, article="an" if control[0] in "aeiou" else "a")
    return _finding(
        _CLASS_CHECK[kind], severity, signal.name,
        f"{signal.name} is {what} ({reasons}); {advice}",
        signal_class=kind, joined_nets=signal.group[1:] or None,
        offboard=signal.offboard[:5] or None, undriven=signal.undriven or None,
    )


def _signal_findings(design: _Design, signals: list[_Net], pulls: dict[str, list[Pull]],
                     variant: str) -> list[dict]:
    findings = []
    for signal in signals:
        net = signal.name
        live = [pull for pull in pulls[net] if pull.fitted]
        absent = [pull for pull in pulls[net] if not pull.fitted]
        if not live and absent and not signal.drivers:
            # As for a missing pull: off the board, the pull may be on the other board
            # (an I2C pull-up fitted only on the board that is the bus master).
            offboard = signal.offboard if signal.main_class() != "connector" else []
            findings.append(_finding(
                "not_fitted_pull", "info" if offboard else "warning", net,
                f"{net} ({signal.main_class()}) is pulled only by "
                f"{', '.join(pull.resistor for pull in absent[:6])}, Not Fitted in variant "
                f"{variant}, so nothing holds it in this variant"
                + (f". It reaches {', '.join(offboard[:3])}: the pull may be on the other "
                   "board." if offboard else "."),
                [pull.resistor for pull in absent], signal_class=signal.main_class(),
                pulls=[pull.describe() for pull in absent[:6]], offboard=offboard[:5] or None,
            ))
            continue
        if not live:
            if finding := _no_pull_finding(signal):
                findings.append(finding)
            continue
        ups = [pull for pull in live if pull.direction == "up"]
        downs = [pull for pull in live if pull.direction in ("down", "source")]
        if ups and downs:
            findings.append(_finding(
                "pull_up_and_down", "info", net,
                f"{net} has a pull-up ({', '.join(p.resistor for p in ups[:4])}) and a "
                f"pull-down ({', '.join(p.resistor for p in downs[:4])}): normal for a strap "
                "or a divider (an enable from a supply's undervoltage divider); check the "
                "level they set.",
                [pull.resistor for pull in ups + downs], signal_class=signal.main_class(),
                pulls=[pull.describe() for pull in ups + downs][:8],
            ))
        if len(ups) > 1:
            values = [pull.ohms for pull in ups]
            effective = _parallel(values) if None not in values else None
            listed = ", ".join(f"{p.resistor} {ohms_text(p.ohms) or '?'}" for p in ups[:6])
            findings.append(_finding(
                "parallel_pull_ups", "info", net,
                f"{net} has {len(ups)} pull-ups in parallel ({listed})"
                + (f", {ohms_text(effective)} ohm together" if effective is not None
                   else ", a value unread")
                + "; check that this is the intended strength.",
                [pull.resistor for pull in ups], signal_class=signal.main_class(),
                effective_ohm=round(effective, 1) if effective is not None else None,
            ))
    return findings


# ---------- I2C sizing ----------

def _buses(design: _Design, signals: list[_Net]) -> list[dict]:
    """I2C buses: SDA and SCL signals paired by name, then by the parts they share."""
    lines: dict[str, tuple[str, str]] = {}  # signal -> (line, stem)
    by_name = {signal.name: signal for signal in signals}
    for signal in signals:
        if "i2c" not in signal.classes:
            continue
        line = next((i2c_line(net) for net in signal.group if i2c_line(net)), None)
        if not line:
            names = [first_function(design.pin_name(ref, pin))
                     for net in signal.group for ref, pin in design.nets[net]]
            line = next((i2c_line(name) for name in names if i2c_line(name)), None)
            line = (line[0], f"#{signal.name}") if line else None
        if line:
            lines[signal.name] = line
    by_stem: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for net, (line, stem) in lines.items():
        by_stem[stem][line].append(net)
    buses, loose = [], {"SDA": [], "SCL": []}
    for stem, sides in by_stem.items():
        if not stem.startswith("#") and len(sides["SDA"]) == 1 and len(sides["SCL"]) == 1:
            buses.append({"SDA": sides["SDA"][0], "SCL": sides["SCL"][0], "paired": "name"})
        else:
            for line, nets in sides.items():
                loose[line].extend(nets)

    def parts(net: str) -> set[str]:
        return {ref for member in by_name[net].group for ref, _ in design.nets.get(member, ())
                if _is_active(design, ref)}

    for sda in sorted(loose["SDA"], key=natural_key):
        best = max(loose["SCL"], key=lambda scl: (len(parts(sda) & parts(scl)), scl),
                   default=None)
        if best and parts(sda) & parts(best):
            loose["SCL"].remove(best)
            buses.append({"SDA": sda, "SCL": best, "paired": "shared_parts"})
        else:
            buses.append({"SDA": sda, "SCL": None, "paired": "none"})
    buses.extend({"SDA": None, "SCL": scl, "paired": "none"} for scl in loose["SCL"])
    buses.sort(key=lambda bus: natural_key(bus["SDA"] or bus["SCL"]))
    return buses


def _bus_capacitance(design: _Design, signal: _Net, given_pf: float | None,
                     lengths: dict[str, float | None]) -> tuple[float, str]:
    if given_pf is not None:
        return given_pf, "given (bus_capacitance_pf)"
    pins = [(ref, pin) for net in signal.group for ref, pin in design.nets[net]
            if ref not in design.resistors and designator_prefix(ref) != "C"]
    caps = [farads for net in signal.group for _, farads in design.caps.get(net, ())]
    cb = PIN_PF * len(pins)
    basis = [f"estimate: {len(pins)} pins x {PIN_PF:g} pF"]
    known = [farads for farads in caps if farads is not None]
    if known:
        cb += sum(known) * 1e12
        basis.append(f"{sum(known) * 1e12:.3g} pF of capacitors to ground")
    if len(known) < len(caps):
        basis.append(f"{len(caps) - len(known)} capacitor value(s) unread, not counted")
    measured = [lengths.get(net) for net in signal.group]
    if any(value is not None for value in measured):
        length_mm = sum(value for value in measured if value is not None)
        cb += length_mm * TRACE_PF_PER_MM
        basis.append(f"{length_mm:.0f} mm routed x {TRACE_PF_PER_MM:g} pF/mm")
    else:
        basis.append("routing not counted")
    if any(_is_connector(design, ref) for ref, _ in pins):
        basis.append("a connector: cables and off-board devices not counted")
    return cb, ", ".join(basis)


def _size_line(design: _Design, signal: _Net, pulls: list[Pull], mode: str,
               given_pf: float | None, lengths: dict) -> dict:
    tr_ns, iol_ma, cb_max = I2C_MODES[mode]
    ups = [pull for pull in pulls if pull.fitted and pull.direction == "up"]
    row: dict = {"net": signal.name, "pull_ups": [pull.describe() for pull in ups]}
    if len(signal.group) > 1:
        row["joined_nets"] = signal.group[1:]
    if absent := [pull.resistor for pull in pulls if not pull.fitted]:
        row["not_fitted"] = absent
    cb, basis = _bus_capacitance(design, signal, given_pf, lengths)
    rmax = tr_ns * 1e-9 / (RISE_FACTOR * cb * 1e-12)
    row.update({"cb_pf": round(cb, 1), "cb_basis": basis, "rmax_ohm": round(rmax, 1)})
    if cb > cb_max:
        row["cb_note"] = f"above the {cb_max:g} pF this mode allows"
    if not ups:
        row.update({"verdict": "unknown", "reason": "no fitted pull-up"})
        return row
    values = [pull.ohms for pull in ups]
    if None in values:
        row.update({"verdict": "unknown", "reason": "a pull-up value could not be read"})
        return row
    effective = _parallel(values)
    row["effective_ohm"] = round(effective, 1)
    voltages = {pull.other: rail_voltage(pull.other) for pull in ups}
    row["rail"] = ", ".join(sorted(voltages, key=natural_key))
    known = [volts for volts in voltages.values() if volts is not None]
    if not known:
        unread = f"no voltage in the pull-up's rail name ({row['rail']})"
        if effective > rmax:
            # Rmax needs no supply voltage: the pull-up is too weak whatever the rail.
            row.update({"verdict": "too_high", "rmin_note": f"Rmin not checked: {unread}"})
        else:
            row.update({"verdict": "unknown", "reason": f"{unread}; check Rmin by hand"})
        return row
    vdd = max(known)
    row["vdd_v"] = vdd
    if len(set(known)) > 1:
        row["vdd_note"] = "pull-ups to rails of different voltages; the highest is used"
    rmin = (vdd - I2C_VOL) / (iol_ma * 1e-3)
    row["rmin_ohm"] = round(rmin, 1)
    row["verdict"] = "too_low" if effective < rmin else "too_high" if effective > rmax else "ok"
    if rmin > rmax:
        row["range_note"] = "Rmin exceeds Rmax: no pull-up meets this mode at this capacitance"
    return row


_VERDICT_RANK = {"too_low": 0, "too_high": 0, "unknown": 1, "ok": 2}


def _size_buses(design: _Design, buses: list[dict], by_name: dict[str, _Net],
                pulls: dict[str, list[Pull]], mode: str, given_pf: float | None,
                lengths: dict, explicit_mode: bool = True) -> tuple[list[dict], list[dict]]:
    rows, findings = [], []
    tr_ns, iol_ma, _ = I2C_MODES[mode]
    for bus in buses:
        lines = {line: _size_line(design, by_name[bus[line]], pulls[bus[line]], mode,
                                  given_pf, lengths)
                 for line in ("SDA", "SCL") if bus[line]}
        verdict = min((row["verdict"] for row in lines.values()), key=_VERDICT_RANK.get)
        name = " / ".join(filter(None, (bus["SDA"], bus["SCL"])))
        rows.append({"bus": name, "mode": mode, "verdict": verdict,
                     "paired_by": bus["paired"], "lines": lines})
        for line, detail in lines.items():
            designators = [pull["designator"] for pull in detail["pull_ups"]]
            if detail["verdict"] == "too_low":
                findings.append(_finding(
                    "i2c_pull_value", "warning", detail["net"],
                    f"{detail['net']} ({line}, {mode} mode): the pull-up of "
                    f"{ohms_text(detail['effective_ohm'])} ohm is under Rmin "
                    f"{detail['rmin_ohm']:.0f} ohm = ({detail['vdd_v']:g} V - {I2C_VOL} V) / "
                    f"{iol_ma:g} mA: a driver sinking {iol_ma:g} mA cannot pull it below "
                    f"{I2C_VOL} V.",
                    designators, bus=name, line=line, verdict="too_low",
                ))
            elif detail["verdict"] == "too_high":
                standard = I2C_MODES["standard"][0] * 1e-9 / (
                    RISE_FACTOR * detail["cb_pf"] * 1e-12)
                # The bus speed is not in the design: a pull-up too weak only for the
                # assumed fast mode is advice (10 kohm at 100 kHz is common and fine).
                assumed = not explicit_mode and detail["effective_ohm"] <= standard
                findings.append(_finding(
                    "i2c_pull_value", "info" if assumed else "warning", detail["net"],
                    f"{detail['net']} ({line}, {mode} mode): the pull-up of "
                    f"{ohms_text(detail['effective_ohm'])} ohm is over Rmax "
                    f"{detail['rmax_ohm']:.0f} ohm = {tr_ns:g} ns / (0.8473 x "
                    f"{detail['cb_pf']:g} pF), so the rise is too slow (Cb "
                    f"{detail['cb_basis']})"
                    + (f"; it meets standard mode (Rmax {standard:.0f} ohm): pass "
                       "i2c_mode='standard' if the bus runs at 100 kHz, else use a smaller "
                       "resistor." if assumed else
                       "; a slower mode or a smaller resistor fixes it."),
                    designators, bus=name, line=line, verdict="too_high",
                ))
            elif detail["verdict"] == "unknown" and detail["pull_ups"]:
                findings.append(_finding(
                    "i2c_pull_value", "info", detail["net"],
                    f"{detail['net']} ({line}): the pull-up cannot be sized: "
                    f"{detail['reason']}.",
                    designators, bus=name, line=line, verdict="unknown",
                ))
    return rows, findings


# ---------- entry point ----------

def resolve_names(requested: list[str] | None, available) -> tuple[set[str] | None, list[str]]:
    if not requested:
        return None, []
    folded = {name.casefold(): name for name in available}
    found = {folded[name.strip().casefold()] for name in requested
             if name.strip().casefold() in folded}
    return found, [name for name in requested if name.strip().casefold() not in folded]


def _mode(text: str | None) -> str:
    mode = (text or DEFAULT_I2C_MODE).strip().lower().replace("-", "_").replace(" ", "_")
    return {"fastplus": "fast_plus", "fast_mode_plus": "fast_plus", "fm+": "fast_plus",
            "sm": "standard", "fm": "fast"}.get(mode, mode)


def run_pull_resistor_check(
    netlist: dict,
    plan=None,
    project_params: dict | None = None,
    checks: list[str] | None = None,
    nets: list[str] | None = None,
    i2c_mode: str | None = None,
    bus_capacitance_pf: float | None = None,
    index_loader=None,
    offset: int = 0,
    limit: int = FINDING_LIMIT,
    net_limit: int = NET_LIMIT,
    bus_limit: int = BUS_LIMIT,
) -> dict:
    """The audit: overview counts, findings (paged, warnings first), I2C buses with their
    sizing and the classified signals with their pulls. Heuristic: see the module notes.

    index_loader, when given, returns (PcbIndex or None, why not); it is called only when
    an I2C bus with a pull-up is sized without bus_capacitance_pf, to add routed length."""
    requested = [check.strip().lower() for check in checks or [] if check.strip()]
    if invalid := [check for check in requested if check not in CHECKS]:
        return {"error": "invalid_checks",
                "message": f"Unknown checks {invalid}; use {list(CHECKS)}."}
    mode = _mode(i2c_mode)
    if mode not in I2C_MODES:
        return {"error": "invalid_i2c_mode",
                "message": f"i2c_mode must be one of {list(I2C_MODES)}."}
    if bus_capacitance_pf is not None and not (0 < bus_capacitance_pf < 1e5):
        return {"error": "invalid_bus_capacitance",
                "message": "bus_capacitance_pf must be a number of picofarads above 0 and "
                           "below 100000."}
    design = build_design(netlist, plan, project_params)
    wanted_nets, unknown = resolve_names(nets, design.nets)
    if nets and not wanted_nets:
        return {"error": "net_not_found",
                "message": f"No net {unknown} in the compiled netlist; use query_net to "
                           "find the real name."}
    wanted = set(requested or set(CHECKS) - OPT_IN_CHECKS)
    signals = merged_signals(design)
    if "connector_no_pull" in wanted:
        signals += _connector_signals(design, {net for s in signals for net in s.group})
    variant = getattr(plan, "name", None) or "[No Variations]"
    notes: list[str] = []
    if unknown:
        notes.append(f"Not found: {', '.join(unknown)}.")
    # Every open-drain and I2C net of the design, before the net filter: a supply switched
    # by a GPIO is recognised by the lines it pulls up together.
    open_drain_names = open_drain_nets(signals)
    if wanted_nets is not None:
        # A gate's driving net (through its series resistor) finds the gate's signal too.
        covered = {net for signal in signals for net in (*signal.group, *signal.extra)}
        for net in sorted(wanted_nets - covered, key=natural_key):
            if design.fixed(net):
                notes.append(f"{net} is a {'ground' if net in design.grounds else 'rail'}, "
                             "not a net that needs a pull.")
                continue
            notes.append(f"{net}: no reset, enable, boot, open-drain, I2C, gate or "
                         "comparator evidence was found; its pulls are listed in signals.")
            signals.append(_Net(net, group=link_group(design, net)))
        signals = [signal for signal in signals
                   if {*signal.group, *signal.extra} & wanted_nets]
    pulls ={signal.name: _pulls_of(design, signal, open_drain_names) for signal in signals}
    classified = [signal for signal in signals if signal.classes]
    findings = _signal_findings(design, classified, pulls, variant)
    by_name = {signal.name: signal for signal in classified}
    buses = _buses(design, classified)
    lengths: dict[str, float | None] = {}
    sized = [by_name[bus[line]] for bus in buses for line in ("SDA", "SCL") if bus[line]]
    if bus_capacitance_pf is None and index_loader is not None and any(
        pull.fitted for signal in sized for pull in pulls[signal.name]
    ):
        index, why = index_loader()
        if index is None:
            notes.append(f"I2C bus capacitance leaves out routing: {why}.")
        else:
            for signal in sized:
                for net in signal.group:
                    measured = net_length(index, net)
                    lengths[net] = (None if "error" in measured
                                    else (measured.get("routed_mil") or 0.0) * 0.0254)
    bus_rows, bus_findings = _size_buses(design, buses, by_name, pulls, mode,
                                         bus_capacitance_pf, lengths, i2c_mode is not None)
    findings = [finding for finding in findings + bus_findings if finding["check"] in wanted]
    findings.sort(key=lambda finding: (
        _SEVERITY_RANK[finding["severity"]], _CHECK_RANK[finding["check"]],
        natural_key(finding["net"]),
    ))
    rows = _signal_rows(signals, pulls)
    summary: dict[str, int] = defaultdict(int)
    for finding in findings:
        summary[finding["check"]] += 1
    offset = max(0, int(offset or 0))
    tr_ns, iol_ma, cb_max = I2C_MODES[mode]
    result: dict = {
        "variant": variant,
        "heuristic": (
            "Signals are classified by net and pin names and the library's pin electrical "
            "types, which misses nets named otherwise and libraries that type every pin "
            "Passive; pull-ups inside ICs are invisible here; the datasheet decides."
        ),
        "checks_run": [check for check in CHECKS if check in wanted],
        "overview": _overview(design, classified, pulls),
        "summary": dict(summary),
        "severity_counts": {level: sum(1 for f in findings if f["severity"] == level)
                            for level in ("warning", "info")},
        "i2c": {
            "mode": mode, "rise_time_ns": tr_ns, "iol_ma": iol_ma, "vol_v": I2C_VOL,
            "max_bus_capacitance_pf": cb_max,
            "formulas": "Rmin = (VDD - VOL) / IOL; Rmax = tr / (0.8473 x Cb) (NXP UM10204)",
            "buses_total": len(bus_rows),
            "buses": bus_rows[:bus_limit],
        },
        "signals_total": len(rows),
        "signals": rows[:net_limit],
        "findings": findings[offset:offset + limit],
        "total": len(findings),
        "offset": offset,
        "has_more": offset + limit < len(findings),
    }
    if result["has_more"]:
        result["next_offset"] = offset + limit
    notes.append(
        "A pull is a resistor from the signal to a rail (up) or a ground (down) fitted in "
        "the variant; nets joined by a series resistor under 1 kohm are one signal. "
        "Resistor arrays are read as the usual isolated (1 to N) or bussed (one common pin) "
        "arrays. Pins left unconnected are not in the netlist, and unused op-amp or "
        "comparator sections are not checked."
    )
    if "connector_no_pull" not in wanted:
        notes.append("connector_no_pull (connector pins wired straight to IC inputs) runs "
                     "only when named in checks.")
    result["notes"] = notes
    return result


def _signal_rows(signals: list[_Net], pulls: dict[str, list[Pull]]) -> list[dict]:
    rows = []
    for signal in signals:
        row: dict = {"net": signal.name, "classes": signal.classes}
        if len(signal.group) > 1:
            row["joined_nets"] = signal.group[1:]
        if signal.extra:
            row["driven_through"] = signal.extra
        if signal.control_kind:
            row["control"] = signal.control_kind
        row["pulls"] = [pull.describe() for pull in pulls[signal.name]]
        if signal.drivers:
            row["push_pull_drivers"] = signal.drivers[:4]
        if signal.offboard:
            row["connectors"] = signal.offboard[:4]
        rows.append(row)
    rows.sort(key=lambda row: (any(not p.get("not_fitted") for p in row["pulls"]),
                               natural_key(row["net"])))
    return rows


def _overview(design: _Design, signals: list[_Net], pulls: dict[str, list[Pull]]) -> dict:
    classified: dict[str, int] = defaultdict(int)
    pulled: dict[str, int] = defaultdict(int)
    driven: dict[str, int] = defaultdict(int)
    for signal in signals:
        kind = signal.main_class()
        classified[kind] += 1
        if any(pull.fitted for pull in pulls[signal.name]):
            pulled[kind] += 1
        elif signal.drivers:
            driven[kind] += 1
    return {
        "signals_classified": dict(classified),
        "with_fitted_pull": dict(pulled),
        "driven_push_pull_without_pull": dict(driven),
        "pull_resistors": len({pull.resistor for net_pulls in design.pulls.values()
                               for pull in net_pulls}),
        "resistors": design.stats["resistors"],
    }
