"""Connector and interface protection audit (heuristic): ESD, series parts, shields, grounds.

Every connector fitted in the variant gets a per-pin table: the pin's class (signal,
ground, power, shield, unconnected), and for a signal pin the walk along its net through
series passives (resistors and 0 ohm links, ferrites and inductors, common-mode chokes,
series capacitors) to the first active part: the ESD/TVS devices met on the way and on
which side of a series element, the series elements, capacitors to ground, pulls, and the
first IC (or transistor, transformer, other connector) with that pin's electrical type.
Each connector is judged external (user-facing: USB, RJ45, D-Sub, terminal block, jack,
coax, SFP port ...), internal (board-to-board, FPC, debug or test header, card edge,
configuration jumper ...) or unknown, from its text, its pin count and what its pins
reach.

Findings follow Altium's schematic review checklist (ESD on every externally accessible
connection, current-limiting resistors toward MCU pins), the TVS layout article (the
diode at the connector on a short path; the shield tied straight to chassis or ground,
not through an RC or snubber network), component placement for EMC (interleaved ground
pins, a direct return) and the EMC certification article (0 ohm series parts on
low-speed off-board lines, swappable for 10-33 ohm). With a PcbDoc the TVS-to-connector
distance is compared with the TVS-to-IC distance, straight line in plan view, pad edge to
pad edge.

Everything here is a heuristic: parts are recognised by designator prefix and the words
in their description, comment, value, footprint and part number; a TVS known only by an
unlisted part number is missed; a transceiver's own ESD rating is quoted, not judged;
whether a connector is user-facing is a guess the user can override; and choosing the
protection part (standoff and clamping voltage, capacitance, uni- or bidirectional) is a
datasheet question this tool does not judge.
"""

import re
from collections import defaultdict
from dataclasses import dataclass, field

try:
    from services.bom import build_parts, natural_key
    from services.decoupling import (
        FERRITE_TEXT, IC_PREFIXES, MIL_PER_MM, THT_LAYER, capacitance, component_pads,
        designator_prefix, farads_text, first_function, is_capacitor, net_kind, net_tokens,
        pcb_components, pin_role,
    )
    from services.pull_resistors import (
        CONNECTOR_PREFIXES, DIFF_HALF, NOT_CONNECTOR_TEXT, TRANSISTOR_PREFIXES, build_design,
        control_kind, is_resistor, ohms_text, resistance, resistor_sections, resolve_names,
    )
except ImportError:
    from server.services.bom import build_parts, natural_key
    from server.services.decoupling import (
        FERRITE_TEXT, IC_PREFIXES, MIL_PER_MM, THT_LAYER, capacitance, component_pads,
        designator_prefix, farads_text, first_function, is_capacitor, net_kind, net_tokens,
        pcb_components, pin_role,
    )
    from server.services.pull_resistors import (
        CONNECTOR_PREFIXES, DIFF_HALF, NOT_CONNECTOR_TEXT, TRANSISTOR_PREFIXES, build_design,
        control_kind, is_resistor, ohms_text, resistance, resistor_sections, resolve_names,
    )

CHECKS = (
    "no_esd", "shield_floating", "ground_indirect", "tvs_far_from_connector",
    "esd_behind_series", "shield_rc", "sparse_ground", "no_series_element",
)
PCB_CHECKS = frozenset({"tvs_far_from_connector"})
_CHECK_RANK = {check: rank for rank, check in enumerate(CHECKS)}
_SEVERITY_RANK = {"warning": 0, "info": 1}

FINDING_LIMIT = 100
CONNECTOR_LIMIT = 60
HOP_LIMIT = 3  # series elements crossed at most between the connector and the first part
SERIES_CAP_MAX_F = 10e-6  # a capacitor between two signals up to this is a series (AC) part
SPARSE_MIN_SIGNALS = 8  # sparse_ground looks at connectors with this many signal pins
SPARSE_RATIO = 1 / 8  # fewer ground pins per signal pin than this is sparse
DEFAULT_MAX_DISTANCE_MIL = 10 * MIL_PER_MM  # TVS pad to connector pad
TVS_NEAR_MIL = 5 * MIL_PER_MM  # a TVS nearer the IC is only worth a warning beyond this
TVS_AT_CONNECTOR_MIL = 2 * MIL_PER_MM  # this near the connector pad a TVS is at the connector
PIN_ROWS_LISTED = 8  # pins quoted in one finding

# ---------- recognising parts ----------

# Prefixes beyond pull_resistors' connector prefixes (J, P, CN, X, CON, CONN) that real
# boards give connectors.
INTERFACE_CONNECTOR_PREFIXES = CONNECTOR_PREFIXES | frozenset({
    "USB", "RJ", "XS", "XP", "JK", "JACK", "HDR", "SV", "CNN", "PL", "SK",
})
_CONNECTOR_WORDS = re.compile(
    r"connector|receptacle|\bjack\b|\bplug\b|header|socket|\bRJ[- ]?\d|modular|terminal|"
    r"\bUSB\b|magjack|edge ?finger|golden finger", re.I)
_NOT_A_CONNECTOR = re.compile(
    r"battery|coin cell|cell holder|fuse ?holder|jumper|shunt|test ?point|\d\s*[kKmM]?Hz\b|"
    r"heat ?sink|stand-?off|spacer", re.I)
_ESD_PREFIXES = frozenset({"TVS", "ESD", "SPD"})
_NOT_ESD_PREFIXES = frozenset({"C", "L", "FB", "J", "P", "CN", "CON", "CONN", "X", "Y", "TP"})
# Part-number families of ESD/TVS parts (Littelfuse's SP3xxx and SP7xxx share their
# prefix with transceivers and regulators: those count only beside protection words).
_ESD_FAMILY = re.compile(
    r"(?<![A-Z0-9])(?:PESD|TPD\d|USBLC|PRTR\d|SMAJ|SMBJ|SMCJ|SMDJ|SMLJ|SMF\d|ESDA|ESDALC|"
    r"ESDAXLC|IP42\d\d|RCLAMP|UCLAMP|SP0\d\d|SP1\d{3}|SM6T|SM15T|SM8S|"
    r"P6KE|P4KE|P6SMB|1\.5KE|1\.5SMC|CDSOT|CDDFN|NUP\d|PUSB|SRV05|SR05|LXES|CPDT|SLVU|"
    r"SD05|SMDA|GBLC|PGB\d|PTVS|ESD\d|ESDS\d|SZESD|SZNUP|BZA\d|D5V0|DESD|ECMF|EMIF|"
    r"ULC\d|MMBZ|VESD|GSOT|VCUT|TESD|SESD|AQ\d{4}|DT1\d{3}|LC03|TISP)",
    re.I)
_AMBIGUOUS_FAMILY = re.compile(r"(?<![A-Z0-9])SP[37]\d{3}", re.I)
# Words that name the part itself as a protection device.
_ESD_DEVICE = re.compile(
    r"\bTVS|transient[ -]voltage|varistor|\bMOV\b|gas discharge|\bGDT\b|"
    r"\bESD[ -]?(?:protection |suppression )?(?:diodes?|arrays?|devices?|suppressors?|"
    r"clamps?|protectors?)\b|\bESD[ -]?(?:protection|suppression) (?:for|of|on)\b|"
    r"surge (?:protection|suppression|suppressor|protector)s? (?:diode|device|array|thyristor)|"
    r"over-?voltage protect\w* diode",
    re.I)
# Words that only hint at protection: enough for a diode, or an IC whose pins drive nothing.
_ESD_HINT = re.compile(r"\bESD\b|surge (?:protect|suppress)", re.I)
_PROTECTION_WORDS = re.compile(r"protect|suppress|\bdiode|\barray|clamp", re.I)
# A part's own ESD rating ('ESD sensitive', '+/-15 kV ESD protection'), not a device.
_ESD_RATING = re.compile(
    r"\bESD[ -]?(?:sensitive|protected|rated|rating|tolerant|tolerance|robust\w*|immun\w*|"
    r"safe|susceptib\w*|strength|withstand)\b|"
    r"\d+(?:\.\d+)?\s*kV\s+(?:HBM\s+|IEC\s+|contact\s+|air\s+)?ESD\b"
    r"(?![ -]?(?:protection |suppression )?(?:diodes?|arrays?|devices?|suppressors?|clamps?|"
    r"protectors?)\b)(?:[ -]?(?:protection|protected))?",  # not '15 kV ESD protection array'
    re.I)
_PROCESSOR_WORDS = (r"micro-?controller|\bMCU\b|\bMPU\b|processor|\bCPU\b|\bSoC\b|\bFPGA\b|"
                    r"\bCPLD\b|\bDSP\b")
# Words naming what else a part is: a processor, a transceiver or PHY, a regulator, a switch
# or a transistor that advertises ESD robustness.
_OTHER_FUNCTION = re.compile(
    _PROCESSOR_WORDS + r"|"
    r"transceiver|\bPHY\b|\bhub\b|bridge|controller|\bUART\b|codec|regulator|converter|"
    r"\bLDO\b|e-?fuse|load switch|power switch|hot[- ]?swap|charger|supervisor|\bdriver\b|"
    r"receiver|amplifier|op-?amp|comparator|sensor|\bADC\b|\bDAC\b|memory|\bflash\b|EEPROM|"
    r"\bS?D?RAM\b|isolator|opto-?coupler|multiplexer|\bmux\b|\bbuffer\b|level[- ]?(?:shift|"
    r"translat)|\blogic\b|MOSFET|\bFET\b|transistor|\bBJT\b|\bIGBT\b|oscillator|crystal|relay",
    re.I)
_DRIVEN_TYPES = frozenset({"Input", "Output", "OpenCollector", "OpenEmitter", "HiZ"})
# A transceiver's own claim of protected pins ('+/-15 kV ESD protection', IEC 61000-4-2).
_ESD_CLAIM = re.compile(
    r"\bESD[ -]?protect\w*|\d+(?:\.\d+)?\s*kV\s+(?:HBM\s+|IEC\s+|contact\s+|air\s+)?ESD\b|"
    r"IEC ?61000-4-2", re.I)
# A processor is not a bus transceiver or PHY, whatever buses its text lists.
PROCESSOR_TEXT = re.compile(_PROCESSOR_WORDS + r"|cortex|\bRISC-?V\b", re.I)
_ZENER_TEXT = re.compile(r"zener", re.I)
_DIODE_PREFIXES = frozenset({"D", "LED", "CR", "Z", "ZD", "DZ", "VD", "DS"})
_INDUCTIVE_PREFIXES = frozenset({"L", "FB", "FL", "LF", "CMC", "CM", "BEAD", "FE"})
_CHOKE_TEXT = re.compile(r"common[- ]?mode|\bCMC\b|\bchoke", re.I)
_TRANSFORMER_TEXT = re.compile(r"transformer|magnetics|magjack", re.I)
_NO_MAGNETICS = re.compile(
    r"\b(?:without|w/o|no|non)[- ]?(?:integrated |internal |built[- ]in )?"
    r"(?:magnetics|transformers?)", re.I)
_PASSIVE_OTHER = frozenset({
    "TP", "FID", "FD", "MH", "H", "HOLE", "MK", "LOGO", "JP", "SJ", "Y", "F", "FUSE", "PTC",
    "RV", "RT", "TH", "NTC", "BAT", "BT", "B", "MP", "W",
})
_SWITCH_PREFIXES = frozenset({"SW", "S", "BTN", "KEY", "PB"})
_RELAY_PREFIXES = frozenset({"K", "RL", "RLY", "REL"})
_FUSE_PREFIXES = frozenset({"F", "FUSE", "PTC", "FU"})
# Parts a walk stops at: the first part that is not passive.
_STOPS = frozenset({"ic", "transistor", "transformer", "connector", "switch", "relay", "other"})
_STOP_RANK = {"ic": 0, "transistor": 1, "other": 2, "transformer": 3, "relay": 4,
              "switch": 5, "connector": 6}  # which part a walk reports when several meet
_VULNERABLE = frozenset({"ic", "transistor", "other"})

# ---------- pins and nets ----------

# Shield or shell pins by name; mounting pins (MH, MP, MOUNT) are classed with them.
_SHIELD_WORD = re.compile(r"(?:SH|SHLD|SHIELD|SHELL|CASE|CHASSIS|EARTH|FGND|CGND|CHGND)\d*|PE")
_MOUNT_WORD = re.compile(r"(?:MH|MP|MNT|MOUNT|MOUNTING|MTG|MECH|FRAME|HOUSING)\d*")
_SHIELD_DESIGNATOR = re.compile(r"(?:S|SH|SHLD|SHIELD|SHELL|CASE)\d*", re.I)
_MOUNT_DESIGNATOR = re.compile(r"(?:MH|MP|MTG|MNT|EH|M)\d*", re.I)
_SHIELD_NET = re.compile(r"SHIELD\w*|SHLD\w*|SHELL\w*|CHASSIS\w*|EARTH\w*|FGND|CGND|CHGND|PE")
_LED_NAME = re.compile(r"LED\w*|GRN\w*|GREEN\w*|YEL\w*|YELLOW\w*|AMBER\w*|ORANGE\w*")
_LED_PIN = re.compile(r"N?LED\w*")  # a PHY's LED1, LED_LINK pin a line reaches
_HIGH_SPEED = frozenset({
    "USB", "USB2", "USB3", "DP", "DM", "HDMI", "TMDS", "LVDS", "MIPI", "CSI", "DSI", "PCIE",
    "PCI", "SATA", "SGMII", "RGMII", "MDI", "TRD", "SSTX", "SSRX", "ETH", "ENET",
    "REFCLK", "SERDES", "GTX", "QSFP", "SFP", "FPD", "GMSL",
})
_DEBUG_TOKENS = frozenset({
    "TCK", "TMS", "TDI", "TDO", "TRST", "NTRST", "SWDIO", "SWCLK", "SWDCLK", "SWO", "JTAG",
    "SWD", "DBG", "DEBUG", "UPDI", "PDI", "SBWTCK", "SBWTDIO", "ICSP", "PGC", "PGD", "PGEC",
    "PGED", "MCLR", "SPY", "ISP",
})
# Field-bus and Ethernet line names, searched in the whole net or pin name.
_FIELD_BUS_NAME = re.compile(
    r"(?<![A-Z0-9])(?:CAN[_-]?(?:H|L|HI|LO|HIGH|LOW)|RS-?485\w*|RS-?422\w*|RS-?232\w*|LIN|"
    r"[A-Z]?MDI\d*(?:[_-]?[PN+-])?|TRD\d*(?:[_-]?[PN+-])?)(?![A-Z0-9])")  # not MDIO
_POSITION_COUNT = re.compile(
    r"(?:(\d{1,3})\s*[xX×]\s*)?(\d{1,3})\s*[- ]?(?:positions?|pos\b|contacts?|pins?\b|ways?\b|"
    r"circuits?\b)", re.I)

# ---------- exposure ----------

# A pluggable module's host connector (SFP, QSFP): the module is what users touch, so ESD
# advice there is info. Known by its text or by its nets (SFPA_TX_P, QSFP1_RX0_N).
_MODULE_PORT = r"\b(?:Q|O)?SFP(?:28|56|\+|-DD|\b)|\bXFP\b"
_EXTERNAL_TEXT = (
    ("USB", r"\bUSB|type[- ]?c\b|micro[- ]?b\b|mini[- ]?b\b"),
    ("RJ45/Ethernet", r"\bRJ[- ]?(?:45|11|12|25)\b|8P8C|modular jack|ethernet|magjack|"
                      r"\bLAN\b|10/100"),
    ("D-Sub", r"d[- ]?sub|\bD[BEACD]-?(?:9|15|25|37|50)\b|\bHD-?15\b"),
    ("circular", r"circular|\bM(?:5|8|12|16|23)\b|bayonet|\bLEMO\b|push[- ]pull"),
    ("terminal block", r"terminal block|screw terminal|spring terminal|pluggable terminal|"
                       r"euro ?block|clamp terminal|wire terminal|terminal strip"),
    ("power jack", r"\bDC (?:power )?jack|power jack|barrel|\bDC[- ]?in\b|IEC ?60320|mains|"
                   r"power supply connector|power (?:entry|inlet)"),
    ("audio jack", r"audio|phone jack|headphone|\b3\.5 ?mm (?:jack|stereo|audio)|\bTRRS?\b|"
                   r"stereo jack|mono jack|\bXLR\b|\bRCA\b|phono"),
    ("RF coax", r"\bSMA\b|\bBNC\b|\bTNC\b|\bSMB\b|\bMCX\b|\bMMCX\b|\bN[- ]type\b|coax|"
                r"\bF[- ]type\b|\bQMA\b"),
    ("video", r"\bHDMI\b|display ?port|\bDVI\b|\bVGA\b"),
    ("SFP/QSFP module port", _MODULE_PORT),
    ("other user-facing", r"\bbanana|binding post|fib(?:er|re)|optical|"
                          r"\bIP6[5-8]\b|waterproof|panel[- ]mount|bulkhead|PS/2|firewire|"
                          r"IEEE ?1394"),
)
_MODULE_PORT_TEXT = re.compile(_MODULE_PORT, re.I)
_MODULE_PORT_TOKEN = re.compile(r"(?:Q|O)?SFP\w*|XFP\w*")
# A debug or programming header's text (get_firmware_pin_map reads it too).
DEBUG_HEADER_WORDS = (r"\bJTAG\b|\bSWD\b|\bdebug|\bprogram(?:ming)?\b|\bISP\b|\bICSP\b|"
                      r"tag[- ]?connect|\bTC20[35]0|\bSWO\b|\bUPDI\b")
_INTERNAL_TEXT = (
    ("board-to-board", r"board[- ]to[- ]board|\bB2B\b|\bBTB\b|mezzanine|stacking|"
                       r"floating board"),
    ("FPC/FFC", r"\bFPC\b|\bFFC\b|\bZIF\b|flex(?:ible)? (?:flat|printed)|flat flex"),
    ("debug or programming header", DEBUG_HEADER_WORDS),
    ("test header", r"\btest (?:header|connector|port|pad)"),
    ("card edge or module socket", r"\bSO-?DIMM\b|\bDIMM\b|\bM\.2\b|mini ?PCIe?|\bPCIe?\b|"
                                   r"card edge|edge connector|golden finger|gold finger|"
                                   r"\bNGFF\b|\bMXM\b|\bCOM ?Express|\bSMARC\b"),
)
_EXTERNAL_RE = [(label, re.compile(pattern, re.I)) for label, pattern in _EXTERNAL_TEXT]
_INTERNAL_RE = [(label, re.compile(pattern, re.I)) for label, pattern in _INTERNAL_TEXT]
# A card slot faces the user only when the enclosure exposes it (many hold a storage card).
_CARD_TEXT = re.compile(r"\bSD[- ]?card|micro ?SD|\bSIM\b|smart ?card|memory card|"
                        r"card (?:socket|holder|connector)", re.I)
_HEADER_TEXT = re.compile(r"header|\bHDR|\bIDC\b|wire[- ]to[- ]board|pin strip|box header", re.I)
_RF = re.compile(r"\bSMA\b|\bBNC\b|\bTNC\b|\bSMB\b|\bMCX\b|\bMMCX\b|\bN[- ]type\b|coax|"
                 r"\bF[- ]type\b|\bQMA\b|U\.?FL|\bIPEX\b|\bMHF\b|\bW\.FL\b", re.I)
_POWER_CONNECTOR = re.compile(r"\bDC (?:power )?jack|power jack|barrel|power (?:supply )?"
                              r"connector|power entry|\bDC[- ]?in\b|IEC ?60320|mains", re.I)
# A bus transceiver behind a connector makes it external on its own; a PHY, USB part or
# generic transceiver only with high-speed, field-bus or magnetics evidence on the pins.
_BUS_TRANSCEIVER_TEXT = re.compile(
    r"\bCAN\b|CAN[- ]FD|RS-?485|RS-?422|RS-?232|\bLIN\b|line (?:driver|receiver)|profibus|"
    r"\bIO-?Link", re.I)
_PHY_TEXT = re.compile(r"ethernet|\bPHY\b|10/100|USB|transceiver", re.I)
_PARAM_KEYS = ("class", "type", "family", "categor", "package", "kind", "series")
HIGH_PIN_COUNT = 60  # with no other evidence, more pins than this is a board connector
# A small header whose every signal pin reaches a regulator's feedback, a select, strap,
# boot or mode input or an address pin, beside a ground pin or a pull: a jumper block.
_CONFIG_PIN = re.compile(
    r"V?FB\d*|FBK\d*|ADJ\d*|[A-Z]*SEL\d*|ADDR?\d*|VSET\d*|ISET\d*|RSET\d*|ILIM\d*|TRIM\d*")
JUMPER_MAX_PINS = 16
# The centre taps of integrated magnetics (TRCT1, TCT, RXCT): AC-grounded, not a line.
_CENTRE_TAP = re.compile(r"(?:TR|TX|RX|TD|RD|T|R)?CT\d*|CTAP\d*|CENT(?:ER|RE)\w*")


# ---------- the design ----------

@dataclass
class _Edge:
    """A series element between two nets."""
    part: str
    kind: str  # resistor, 0_ohm, ferrite, inductor, common_mode_choke, capacitor
    value: str | None
    near: str
    far: str
    fitted: bool

    def describe(self) -> dict:
        out: dict = {"designator": self.part, "type": self.kind}
        if self.value:
            out["value"] = self.value
        if not self.fitted:
            out["not_fitted"] = True
        return out


@dataclass
class _Context:
    design: object  # pull_resistors' design: nets, grounds, rails, pulls, caps, pins
    kind: dict[str, str]  # part -> ic, transistor, connector, esd, resistor, ...
    text: dict[str, str]  # part -> description, comment, value, footprint, MPN ...
    fitted: dict[str, bool]
    edges: dict[str, list[_Edge]]
    esd: dict[str, list[str]]  # net -> ESD/TVS parts with a pin on it
    clamps: dict[str, list[str]]  # net -> plain or zener diodes from it to another net
    pin_designators: dict[str, list[str]]  # part -> every pin, connected or not
    comment: dict[str, str]


def part_text(meta: dict, part) -> str:
    params = meta.get("params") or {}
    extra = [value for key, value in params.items()
             if isinstance(value, str) and any(word in key.lower() for word in _PARAM_KEYS)]
    return " ".join(filter(None, (
        meta.get("description"), meta.get("lib_reference"), meta.get("value"),
        part.comment if part else None, part.footprint if part else meta.get("footprint"),
        part.mpn if part else meta.get("mpn"), *extra,
    )))


def is_interface_connector(prefix: str, text: str) -> bool:
    """A connector by its designator prefix, unless its text names a crystal, oscillator or
    transformer (and no connector word), a battery or fuse holder, a jumper or a test point."""
    if prefix not in INTERFACE_CONNECTOR_PREFIXES:
        return False
    if NOT_CONNECTOR_TEXT.search(text) and not _CONNECTOR_WORDS.search(text):
        return False
    return not _NOT_A_CONNECTOR.search(text)


def is_esd(prefix: str, text: str, pin_types=()) -> bool:
    """An ESD/TVS diode or array, a varistor or a gas discharge tube: a TVS or ESD prefix,
    a protection family's part number (PESD, TPD, USBLC, SMAJ, RCLAMP ...), or words that
    name the part as one (TVS, transient voltage suppressor, ESD protection array). A part
    whose text first names something else (an MCU, transceiver, PHY, regulator, eFuse,
    MOSFET) is that, and its own rating ('ESD sensitive', '+/-15 kV ESD protection') is not
    a device. A bare 'ESD' is enough for a diode, or an IC with no Input or Output pins,
    never for a transistor. Resistors, capacitors, inductors and connectors never are; a
    plain diode is not."""
    if prefix in _ESD_PREFIXES:
        return True
    if prefix in _NOT_ESD_PREFIXES or prefix.startswith("R") and prefix not in ("RV",):
        return False
    if _ESD_FAMILY.search(text):
        return True
    text = _ESD_RATING.sub(" ", text)
    device = _ESD_DEVICE.search(text)
    other = _OTHER_FUNCTION.search(text)
    if other:
        return bool(device) and device.start() < other.start()
    if device:
        return True
    hint = _ESD_HINT.search(text) or (
        _AMBIGUOUS_FAMILY.search(text) and _PROTECTION_WORDS.search(text))
    if not hint or prefix in TRANSISTOR_PREFIXES:
        return False
    return prefix in _DIODE_PREFIXES or not any(kind in _DRIVEN_TYPES for kind in pin_types)


def has_magnetics(text: str) -> bool:
    """A transformer or integrated magnetics by its text, not 'without magnetics'."""
    return bool(_TRANSFORMER_TEXT.search(_NO_MAGNETICS.sub(" ", text)))


def categorize(prefix: str, text: str, pins: int, pin_types=()) -> str:
    if is_resistor(prefix, text):
        return "resistor"
    if is_capacitor(prefix, text):
        return "capacitor"
    if is_interface_connector(prefix, text):
        return "connector"
    if is_esd(prefix, text, pin_types):
        return "esd"
    if prefix in IC_PREFIXES:
        return "ic"
    inductive = prefix in _INDUCTIVE_PREFIXES or (
        FERRITE_TEXT.search(text) and prefix not in TRANSISTOR_PREFIXES)
    if inductive or (prefix in ("T", "TR") and _CHOKE_TEXT.search(text)):
        return "choke" if (_CHOKE_TEXT.search(text) and pins >= 4) or prefix == "CMC" \
            else "inductive"
    if prefix in _DIODE_PREFIXES:
        return "zener" if _ZENER_TEXT.search(text) else "diode"
    if has_magnetics(text):
        return "transformer"
    if prefix in TRANSISTOR_PREFIXES:
        return "transistor"
    if prefix in _FUSE_PREFIXES:
        return "fuse"
    if prefix in _PASSIVE_OTHER or prefix in CONNECTOR_PREFIXES:
        return "passive"
    if prefix in _SWITCH_PREFIXES:
        return "switch"
    if prefix in _RELAY_PREFIXES:
        return "relay"
    return "other"


def build_context(netlist: dict, plan, project_params: dict | None) -> _Context:
    design = build_design(netlist, plan, project_params)
    components = netlist.get("components") or {}
    parts = {part.designator: part for part in build_parts(netlist, plan, project_params)[0]}
    compiled = netlist.get("parts") or {}
    kinds, texts, comments, designators = {}, {}, {}, {}
    for ref, pins in design.part_pins.items():
        if ref not in design.fitted:
            continue
        meta = components.get(ref) or {}
        part = parts.get(ref)
        texts[ref] = part_text(meta, part)
        designators[ref] = [pin for pin in meta.get("pin_designators") or pins if pin]
        kinds[ref] = categorize(designator_prefix(ref), texts[ref],
                                 max(len(designators[ref]), len(pins)),
                                 [design.pin_type(ref, pin) for pin in pins])
        comments[ref] = (part.comment if part else None) or (compiled.get(ref) or {}).get(
            "comment") or meta.get("description") or ""
    ctx = _Context(design, kinds, texts, design.fitted, defaultdict(list), defaultdict(list),
                   defaultdict(list), designators, comments)
    for ref, kind in kinds.items():
        connected = design.part_pins[ref]
        nets = list(dict.fromkeys(connected.values()))
        if kind == "esd":
            if len(nets) >= 2:
                for net in nets:
                    ctx.esd[net].append(ref)
            continue
        if kind in ("diode", "zener"):
            # A diode from a line to a rail or ground (BAV99 pairs, zeners) clamps it.
            if any(design.fixed(net) for net in nets):
                for net in nets:
                    if not design.fixed(net):
                        ctx.clamps[net].append(ref)
            continue
        _add_edges(ctx, ref, kind, components.get(ref) or {}, parts.get(ref), connected)
    return ctx


def _add_edges(ctx: _Context, ref: str, kind: str, meta: dict, part, connected: dict) -> None:
    design = ctx.design
    fitted = ctx.fitted.get(ref, True)

    def add(near: str, far: str, edge_kind: str, value: str | None) -> None:
        if near == far or design.fixed(near) or design.fixed(far):
            return
        ctx.edges[near].append(_Edge(ref, edge_kind, value, near, far, fitted))
        ctx.edges[far].append(_Edge(ref, edge_kind, value, far, near, fitted))

    if kind == "resistor":
        ohms = resistance(part.value if part else meta.get("value"),
                          part.comment if part else None,
                          (meta.get("params") or {}).get("resistance"),
                          part.description if part else meta.get("description"))
        value = ohms_text(ohms)
        edge_kind = "0_ohm" if ohms == 0 else "resistor"
        for _, net_a, _, net_b, _ in resistor_sections(meta, connected):
            if net_a is not None:
                add(net_a, net_b, edge_kind, value)
        return
    nets = list(dict.fromkeys(connected.values()))
    if kind == "capacitor" and len(connected) == 2 and len(nets) == 2:
        farads = capacitance(part.value if part else meta.get("value"),
                             part.comment if part else None,
                             part.description if part else meta.get("description"))
        if farads is None or farads <= SERIES_CAP_MAX_F:
            add(nets[0], nets[1], "capacitor", farads_text(farads))
        return
    if kind == "inductive" and len(connected) == 2 and len(nets) == 2:
        text = ctx.text.get(ref, "")
        ferrite = FERRITE_TEXT.search(text) or designator_prefix(ref) in ("FB", "BEAD", "FE")
        add(nets[0], nets[1], "ferrite" if ferrite else "inductor", None)
        return
    if kind == "choke":
        # Each winding joins two nets; which pins pair is read in choke_far.
        for net in nets:
            for other in nets:
                if other != net and not design.fixed(net) and not design.fixed(other):
                    ctx.edges[net].append(_Edge(ref, "common_mode_choke", None, net, other,
                                                fitted))


_PLUS_TOKENS = frozenset({"P", "DP", "POS", "PLUS", "CANH"})
_MINUS_TOKENS = frozenset({"N", "DM", "NEG", "MINUS", "CANL"})
_CAN_TOKENS = {"H": "+", "HI": "+", "HIGH": "+", "L": "-", "LO": "-", "LOW": "-"}


def _polarity(name: str) -> str | None:
    """'+' or '-' for a differential half: a trailing + or -, or a P/N, DP/DM, H/L token
    (USB_DP_CONN, LANE0_N, TRD1+, CAN_H)."""
    text = name.upper().replace("\\", "").strip()
    if text.endswith("+"):
        return "+"
    if text.endswith("-"):
        return "-"
    tokens = net_tokens(text)
    plus = [token for token in tokens if token in _PLUS_TOKENS]
    minus = [token for token in tokens if token in _MINUS_TOKENS]
    if "CAN" in tokens:  # CAN_H, CAN_LO
        plus += [token for token in tokens if _CAN_TOKENS.get(token) == "+"]
        minus += [token for token in tokens if _CAN_TOKENS.get(token) == "-"]
    if plus and not minus:
        return "+"
    if minus and not plus:
        return "-"
    return None


def choke_far(ctx: _Context, edges: list[_Edge], connector: str, start: str) -> list[_Edge]:
    """The common-mode choke edges a walk crosses: of a choke's other nets, those not
    on the starting connector (the partner line's connector side) and, when the nets carry
    a polarity (P/N, +/-, DP/DM), the one with the starting net's polarity."""
    design = ctx.design
    out = []
    by_part: dict[str, list[_Edge]] = defaultdict(list)
    for edge in edges:
        by_part[edge.part].append(edge)
    for group in by_part.values():
        far = [edge for edge in group
               if not any(ref == connector for ref, _ in design.nets.get(edge.far, ()))]
        polarity = _polarity(start)
        if polarity and len(far) > 1:
            same = [edge for edge in far if _polarity(edge.far) == polarity]
            far = same or far
        out.extend(sorted(far, key=lambda edge: natural_key(edge.far))[:1])
    return out


# ---------- walking a pin's net ----------

@dataclass
class _Walk:
    reached: list[tuple[str, str, str, int]] = field(default_factory=list)  # part, pin, net, hops
    series: list[_Edge] = field(default_factory=list)  # on the path to the first part
    unfitted: list[_Edge] = field(default_factory=list)  # Not Fitted series parts met
    esd: list[tuple[str, int, str]] = field(default_factory=list)  # part, hops, net
    esd_unfitted: list[str] = field(default_factory=list)
    clamps: list[str] = field(default_factory=list)
    caps: list[tuple[str, float | None]] = field(default_factory=list)
    pulls: list = field(default_factory=list)
    hop_limited: bool = False

    def first(self) -> tuple[str, str, str, int] | None:
        return self.reached[0] if self.reached else None


def _walk(ctx: _Context, connector: str, start: str) -> _Walk:
    """Breadth-first from the connector's net through fitted series elements, up to
    HOP_LIMIT of them, to the first level that holds a part that is not passive."""
    design = ctx.design
    walk = _Walk()
    via: dict[str, _Edge | None] = {start: None}
    level = [start]
    for hops in range(HOP_LIMIT + 1):
        for net in level:
            for ref in ctx.esd.get(net, ()):
                if ctx.fitted.get(ref, True):
                    if all(ref != part for part, _, _ in walk.esd):
                        walk.esd.append((ref, hops, net))
                elif ref not in walk.esd_unfitted:
                    walk.esd_unfitted.append(ref)
            walk.clamps += [ref for ref in ctx.clamps.get(net, ())
                            if ctx.fitted.get(ref, True) and ref not in walk.clamps]
            walk.caps += list(design.caps.get(net, ()))
            walk.pulls += list(design.pulls.get(net, ()))
            for ref, pin in design.nets.get(net, ()):
                if ref != connector and ctx.kind.get(ref) in _STOPS and ctx.fitted.get(ref, True):
                    walk.reached.append((ref, pin, net, hops))
        if walk.reached:
            walk.reached.sort(key=lambda item: (_STOP_RANK[ctx.kind[item[0]]],
                                                natural_key(item[0]), natural_key(item[1])))
            net = walk.reached[0][2]
            while via.get(net) is not None:
                walk.series.insert(0, via[net])
                net = via[net].near
            return walk
        following = []
        for net in level:
            edges = ctx.edges.get(net, ())
            plain = [edge for edge in edges if edge.kind != "common_mode_choke"]
            chokes = choke_far(ctx, [edge for edge in edges if edge.kind == "common_mode_choke"
                                     and edge.fitted], connector, net)
            for edge in [*plain, *chokes]:
                if not edge.fitted:
                    if all(edge.part != seen.part for seen in walk.unfitted):
                        walk.unfitted.append(edge)
                    continue
                if edge.far in via:
                    continue
                if hops == HOP_LIMIT:
                    walk.hop_limited = True
                    break
                via[edge.far] = edge
                following.append(edge.far)
        level = following
        if not level:
            break
    return walk


# ---------- connectors and their pins ----------

@dataclass
class _Pin:
    pin: str
    name: str
    net: str | None
    cls: str  # signal, ground, power, shield, unconnected
    # led, centre_tap, field_bus, high_speed, differential
    tags: list[str] = field(default_factory=list)
    walk: _Walk | None = None
    why: str | None = None  # how a shield or power pin was recognised
    power: dict = field(default_factory=dict)
    pcb: dict = field(default_factory=dict)
    ground_via: list[str] = field(default_factory=list)  # a ground pin's parts to ground


@dataclass
class _Connector:
    ref: str
    pins: list[_Pin]
    exposure: str = "unknown"
    basis: list[str] = field(default_factory=list)
    rf: bool = False
    isolated: bool = False  # integrated magnetics: the board side is behind a transformer
    module_port: bool = False  # an SFP or QSFP cage's host connector
    audited: bool = True  # findings are given (not for unnamed internal connectors)
    shield: dict | None = None

    def signal_pins(self) -> list[_Pin]:
        return [pin for pin in self.pins if pin.cls == "signal"]

    def line_pins(self) -> list[_Pin]:
        """Signal pins that are lines into the board: not LED or centre-tap pins."""
        return [pin for pin in self.signal_pins()
                if not {"led", "centre_tap"} & set(pin.tags) and pin.walk]


def exposed_lines(ctx: _Context, connector: _Connector) -> list[_Pin]:
    """The connector's lines into the board whose walk reaches a part ESD can damage (an
    IC, a transistor or another active part; not a transformer, switch or connector): the
    pins no_esd asks protection for."""
    return [pin for pin in connector.line_pins() if pin.walk.first()
            and ctx.kind.get(pin.walk.first()[0]) in _VULNERABLE]


def _position_count(text: str) -> int | None:
    best = None
    for match in _POSITION_COUNT.finditer(text):
        count = int(match.group(2)) * (int(match.group(1)) if match.group(1) else 1)
        if count > 0:
            best = count if best is None else max(best, count)
    return best


def _shield_named(name: str) -> bool:
    return any(_SHIELD_WORD.fullmatch(token) for token in net_tokens(name or ""))


def _mount_named(name: str) -> bool:
    return any(_MOUNT_WORD.fullmatch(token) for token in net_tokens(name or ""))


def _shield_pin(pin: str, name: str, numeric: int) -> tuple[bool, bool]:
    """(shield-like, a real shield or shell rather than a mounting pin) by pin name, or by
    a lettered designator (S1, SH2, MP1) among numbered ones."""
    lettered = numeric >= 2 and not pin.isdigit()
    shield = (_shield_named(name) and not net_kind(name)["power"]) or (
        lettered and bool(_SHIELD_DESIGNATOR.fullmatch(pin)))
    mount = _mount_named(name) or (lettered and bool(_MOUNT_DESIGNATOR.fullmatch(pin)))
    return shield or mount, shield


def shield_net(net: str) -> bool:
    """A net named like a shield, chassis or protective earth (SHIELD, USB_SHLD,
    CHASSIS_GND, EARTH, PE)."""
    return any(_SHIELD_NET.fullmatch(token) for token in net_tokens(net or ""))


def _field_bus(name: str | None) -> bool:
    return bool(_FIELD_BUS_NAME.search((name or "").upper().replace("\\", "")))


def _supply_part_pin(ctx: _Context, ref: str, pin: str) -> bool:
    name = ctx.design.pin_name(ref, pin)
    role = pin_role(name)
    return role == "supply" or (ctx.design.pin_type(ref, pin) == "Power"
                                and role not in ("ground", "other"))


def _is_power(ctx: _Context, ref: str, pin: str, net: str) -> bool:
    design = ctx.design
    evidence = design.rails.get(net)
    if evidence and evidence != ["part_pin"]:
        return True
    if pin_role(design.pin_name(ref, pin)) == "supply":
        return True
    if evidence:
        # A supply pin of another part (a module's VOUT), not the connector's own pins
        # typed Power by its library.
        return any(other != ref and ctx.kind.get(other) != "connector"
                   and _supply_part_pin(ctx, other, other_pin)
                   for other, other_pin in design.nets.get(net, ()))
    return False


def _power_feed(ctx: _Context, net: str) -> tuple[str | None, list[str]]:
    """(rail, parts) when the net feeds a rail through a series diode, fuse, ferrite,
    inductor or a MOSFET's source-drain channel (reverse-polarity or inrush parts), up to
    two parts deep, with no IC signal pin on it."""
    design = ctx.design
    if any(ctx.kind.get(ref) == "ic" and design.pin_type(ref, pin) in (
            "Input", "Output", "IO", "OpenCollector", "OpenEmitter", "HiZ")
           for ref, pin in design.nets.get(net, ())):
        return None, []
    # Diodes to ground beside the diode to a rail make a clamp pair, not a supply feed.
    clamped = any(ctx.kind.get(ref) in ("diode", "zener") and any(
        other in design.grounds for other in design.part_pins[ref].values())
        for ref, _ in design.nets.get(net, ()))
    seen, queue = {net}, [(net, [])]
    while queue:
        current, parts = queue.pop(0)
        for ref, pin in design.nets.get(current, ()):
            kind = ctx.kind.get(ref)
            if ref in parts or not ctx.fitted.get(ref, True):
                continue
            others = []
            pins = design.part_pins[ref]
            if kind == "diode" and len(pins) == 2 and not clamped:
                others = [other for other in pins.values() if other != current]
            elif kind in ("fuse", "inductive") and len(pins) == 2:
                others = [other for other in pins.values() if other != current]
            elif kind == "transistor":
                name = first_function(design.pin_name(ref, pin))
                if name[:1] in ("S", "D"):
                    others = [other for other_pin, other in design.part_pins[ref].items()
                              if other != current and first_function(
                                  design.pin_name(ref, other_pin))[:1] in ("S", "D")]
            for other in others:
                if other in design.grounds or other in seen:
                    continue
                if other in design.rails:
                    return other, [*parts, ref]
                if len(parts) < 1:
                    seen.add(other)
                    queue.append((other, [*parts, ref]))
    return None, []


def _rc_node(ctx: _Context, net: str, connector: str) -> bool:
    """A net holding only resistors, capacitors, ferrites or protection parts besides the
    connector, at least one of them to a ground or chassis net: a shield's RC network."""
    design = ctx.design
    members = [ref for ref, _ in design.nets.get(net, ()) if ref != connector]
    if not members or any(ctx.kind.get(ref) not in _SHIELD_PASSIVES for ref in members):
        return False
    return any(far in design.grounds or shield_net(far)
               for ref in members for far in design.part_pins[ref].values() if far != net)


def _ground_via(ctx: _Context, net: str, connector: str) -> list[str]:
    """For a ground-named pin on a net that is not a ground: the resistors, capacitors,
    ferrites or protection parts that join it to a board ground, when nothing else is on
    it (a net with other parts' pins is a local ground or a signal, and is left alone)."""
    design = ctx.design
    members = {ref for ref, _ in design.nets.get(net, ()) if ref != connector}
    if not members or any(ctx.kind.get(ref) not in _SHIELD_PASSIVES for ref in members):
        return []
    return sorted((ref for ref in members if ctx.fitted.get(ref, True) and any(
        far in design.grounds for far in design.part_pins[ref].values() if far != net)),
        key=natural_key)


_SHIELD_PASSIVES = frozenset({"resistor", "capacitor", "inductive", "esd", "zener", "diode"})
_DC_PASSIVES = frozenset({"resistor", "inductive"})  # parts that pass a DC return


def _classify_pins(ctx: _Context, ref: str, power_connector: bool) -> list[_Pin]:
    design = ctx.design
    connected = design.part_pins.get(ref) or {}
    every = list(dict.fromkeys([*ctx.pin_designators.get(ref, []), *connected]))
    numeric = sum(1 for pin in every if pin.isdigit())
    count = _position_count(ctx.text.get(ref, ""))
    # A few numbered pins past the position count are shell or mounting legs (an SMA's
    # four ground legs, a 9-pin USB 3.0 receptacle's pins 10-11); many more mean the
    # count was read wrong (a '10-pin' dual-row header with 20).
    extra = numeric - count if count else 0
    pins = []
    for pin in sorted(every, key=natural_key):
        net = connected.get(pin)
        name = design.pin_name(ref, pin)
        shield_like, _ = _shield_pin(pin, name, numeric)
        beyond = 0 < extra <= max(4, count // 4) and (
            (pin.isdigit() and int(pin) > count) or not pin.isdigit())
        if net is None:
            cls = "shield" if shield_like else "unconnected"
            pins.append(_Pin(pin, name, None, cls, why="pin name" if shield_like else None))
            continue
        alone = all(other == ref for other, _ in design.nets.get(net, ()))
        grounded = net in design.grounds
        why, via = None, []
        if shield_like:
            cls, why = "shield", "pin name"
        elif shield_net(net):
            cls, why = "shield", "net name"
        elif beyond and (grounded or alone or _rc_node(ctx, net, ref)):
            cls, why = "shield", f"past the {count} positions in its description"
        elif alone:
            cls = "unconnected"
        elif grounded or net_kind(name)["ground"] or pin_role(name) == "ground":
            cls = "ground"
            via = [] if grounded else _ground_via(ctx, net, ref)
            if via:
                why = f"pin name; reaches a board ground only through {', '.join(via[:4])}"
        elif _is_power(ctx, ref, pin, net):
            cls = "power"
        elif power_connector:
            cls, why = "power", "a power connector"
        elif _power_feed(ctx, net)[0]:
            cls, why = "power", "feeds a rail"
        else:
            cls = "signal"
        entry = _Pin(pin, name, net, cls, why=why, ground_via=via)
        if cls == "signal":
            tokens = set(net_tokens(net)) | set(net_tokens(name))
            if any(_LED_NAME.fullmatch(token) for token in tokens):
                entry.tags.append("led")
            if any(_CENTRE_TAP.fullmatch(token) for token in net_tokens(name)):
                entry.tags.append("centre_tap")
            if _field_bus(net) or _field_bus(name):
                entry.tags.append("field_bus")
            differential = DIFF_HALF.search(net.upper()) or (
                name and DIFF_HALF.search(name.upper()))
            if tokens & _HIGH_SPEED or differential:
                entry.tags.append("high_speed")
            if differential or "field_bus" in entry.tags:
                entry.tags.append("differential")
        pins.append(entry)
    return pins


def bus_transceiver(ctx: _Context, ref: str) -> bool:
    """A CAN, RS-485, RS-232, LIN or line driver part, not a processor listing its buses."""
    text = ctx.text.get(ref, "")
    return bool(_BUS_TRANSCEIVER_TEXT.search(text)) and not PROCESSOR_TEXT.search(text)


def _config_targets(ctx: _Context, connector: _Connector) -> list[str]:
    """The pins a small header's signal pins all reach when it is a configuration jumper:
    every one goes to a regulator's feedback, a select, strap, boot or mode input or an
    address pin, and it has a ground pin or each line a pull. Empty otherwise."""
    signals = connector.signal_pins()
    if not signals or len(ctx.pin_designators.get(connector.ref, [])) > JUMPER_MAX_PINS:
        return []
    if not any(pin.cls == "ground" for pin in connector.pins) and not all(
            pin.walk and pin.walk.pulls for pin in signals):
        return []
    targets = []
    for pin in signals:
        first = pin.walk.first() if pin.walk else None
        if not first or ctx.kind.get(first[0]) not in _VULNERABLE:
            return []
        name = ctx.design.pin_name(first[0], first[1])
        function = first_function(name or "")
        if not (_CONFIG_PIN.fullmatch(function) or control_kind(name, pin=True) in (
                "boot", "strap", "mode", "program")):
            return []
        targets.append(f"{first[0]}.{function}")
    return list(dict.fromkeys(targets))


def _exposure(ctx: _Context, connector: _Connector, override: str | None) -> None:
    text = ctx.text.get(connector.ref, "")
    signals = connector.signal_pins()
    connector.rf = bool(_RF.search(text))
    connector.isolated = has_magnetics(text)
    module = [pin for pin in signals if any(
        _MODULE_PORT_TOKEN.fullmatch(token)
        for token in [*net_tokens(pin.net or ""), *net_tokens(pin.name)])]
    connector.module_port = bool(_MODULE_PORT_TEXT.search(text)) or (
        len(module) >= 2 and len(module) * 2 >= len(signals))
    if override:
        connector.exposure, connector.basis = override, ["given by the caller"]
        return
    external = [label for label, pattern in _EXTERNAL_RE if pattern.search(text)]
    internal = [label for label, pattern in _INTERNAL_RE if pattern.search(text)]
    if external and not internal:
        connector.exposure, connector.basis = "external", [f"text: {label}" for label in external]
        return
    if internal and not external:
        connector.exposure, connector.basis = "internal", [f"text: {label}" for label in internal]
        return
    if external and internal:
        connector.basis = [*(f"text: {label}" for label in external),
                           *(f"text: {label}" for label in internal)]
        connector.basis.append("both user-facing and board-level words")
        return
    if connector.module_port:
        connector.exposure, connector.basis = "external", [
            f"an SFP/QSFP module port by its nets ({', '.join(pin.net for pin in module[:3])})"]
        return
    names = [set(net_tokens(pin.net or "")) | set(net_tokens(pin.name)) for pin in signals]
    debug = sum(1 for tokens in names if tokens & _DEBUG_TOKENS)
    if debug >= 2 and debug * 2 >= len(signals):
        connector.exposure, connector.basis = "internal", [
            f"{debug} of {len(signals)} signal pins carry debug or programming nets"]
        return
    bus = [pin for pin in signals if "field_bus" in pin.tags]
    reached = sorted({pin.walk.first()[0] for pin in signals if pin.walk and pin.walk.first()
                      and ctx.kind.get(pin.walk.first()[0]) in ("ic", "other", "transformer")},
                     key=natural_key)
    buses = [part for part in reached if bus_transceiver(ctx, part)]
    phys = [part for part in reached if ctx.kind.get(part) == "transformer" or (
        _PHY_TEXT.search(ctx.text.get(part, ""))
        and not PROCESSOR_TEXT.search(ctx.text.get(part, "")))]
    if buses or (phys and (bus or any("high_speed" in pin.tags for pin in signals))):
        connector.exposure, connector.basis = "external", [
            f"its pins reach {', '.join((buses or phys)[:3])} (a bus transceiver, PHY, "
            "magnetics or USB part)"]
        return
    if bus and len(bus) * 2 >= len(signals):
        connector.exposure, connector.basis = "external", [
            f"field-bus or Ethernet pin names ({', '.join(pin.name or pin.net for pin in bus[:3])})"]
        return
    if targets := _config_targets(ctx, connector):
        connector.exposure, connector.basis = "internal", [
            "a configuration jumper: its signal pins only reach feedback, select or strap "
            f"pins ({', '.join(targets[:3])})"]
        return
    total = len(ctx.pin_designators.get(connector.ref, []))
    if total > HIGH_PIN_COUNT:
        connector.exposure, connector.basis = "internal", [
            f"{total} pins: a board-level connector"]
        return
    if _CARD_TEXT.search(text):
        connector.basis = ["a card slot: user-facing only if the enclosure exposes it"]
    elif _HEADER_TEXT.search(text):
        connector.basis = ["a pin header: it may go to a cable, a panel or another board"]
    else:
        connector.basis = ["nothing in its text or pins says where it goes"]


def _shield_state(ctx: _Context, connector: _Connector) -> dict | None:
    """direct (on a ground, or a chassis net with more on it than the connector and RC
    parts), rc (to a ground or chassis only through resistors, capacitors or ferrites),
    other (it reaches other parts, such as another connector, but no ground) or floating
    (nothing else on it)."""
    design = ctx.design
    shields = [pin for pin in connector.pins if pin.cls == "shield"]
    if not shields:
        return None
    numeric = sum(1 for pin in connector.pins if pin.pin.isdigit())
    named = any(_shield_pin(pin.pin, pin.name, numeric)[1] or shield_net(pin.net or "")
                for pin in shields)
    nets = sorted({pin.net for pin in shields if pin.net}, key=natural_key)
    states, parts = [], []
    for net in nets:
        if net in design.grounds:
            states.append("direct")
            continue
        members = [(ref, pin) for ref, pin in design.nets.get(net, ())
                   if ref != connector.ref]
        if not members:
            states.append("floating")
            continue
        passive, other = [], []
        for ref, _ in members:
            kind = ctx.kind.get(ref)
            if kind in ("resistor", "capacitor", "inductive", "esd", "zener", "diode"):
                ends = [far for far in design.part_pins[ref].values() if far != net]
                if any(far in design.grounds or shield_net(far) for far in ends):
                    passive.append(ref)
                    continue
            other.append(ref)
        if other and shield_net(net):
            states.append("direct")  # a chassis net: mounting holes, other shells
        elif passive:
            states.append("rc")
            parts += passive
        elif other:
            states.append("direct" if shield_net(net) else "other")
        else:
            states.append("floating")
    unconnected = [pin.pin for pin in shields if not pin.net]
    if not nets:
        state = "floating"
    else:
        state = next((s for s in ("floating", "rc", "other", "direct") if s in states), "direct")
    out: dict = {"state": state, "pins": [pin.pin for pin in shields], "nets": nets,
                 "named_shield": bool(named)}
    if parts:
        out["through"] = sorted(set(parts), key=natural_key)
    if unconnected:
        out["unconnected_pins"] = unconnected
    return out


# ---------- findings ----------

def _finding(check: str, severity: str, connector: str, issue: str, designators=(),
             **fields) -> dict:
    out: dict = {"check": check, "severity": severity, "connector": connector}
    if designators:
        out["designators"] = sorted(set(designators), key=natural_key)
    out.update({key: value for key, value in fields.items() if value not in (None, [], {})})
    out["issue"] = issue
    return out


def _pin_label(pin: _Pin) -> str:
    label = f"{pin.pin}"
    if pin.name and pin.name != pin.pin:
        label += f" ({pin.name})"
    first = pin.walk.first() if pin.walk else None
    target = f" to {first[0]}.{first[1]}" if first else ""
    return f"{label} {pin.net}{target}"


def _labels(pins: list[_Pin]) -> str:
    shown = ", ".join(_pin_label(pin) for pin in pins[:PIN_ROWS_LISTED])
    return shown + (f" and {len(pins) - PIN_ROWS_LISTED} more" if len(pins) > PIN_ROWS_LISTED
                    else "")


def _exposure_text(connector: _Connector) -> str:
    return f"{connector.exposure}: {'; '.join(connector.basis)}"


def _connector_findings(ctx: _Context, connector: _Connector, variant: str) -> list[dict]:
    if not connector.audited:
        return []
    findings = []
    ref = connector.ref
    exposure = connector.exposure
    lines = connector.line_pins()
    reaching = exposed_lines(ctx, connector)
    unprotected = [pin for pin in reaching if not pin.walk.esd]
    if unprotected:
        targets = sorted({pin.walk.first()[0] for pin in unprotected}, key=natural_key)
        # Lines into parts that claim protected pins (an RS-232 or RS-485 transceiver's
        # +/-15 kV): the datasheet decides, so info.
        claimed = [part for part in targets if _ESD_CLAIM.search(ctx.text.get(part, ""))]
        advisory = connector.isolated or connector.rf or connector.module_port or (
            len(claimed) == len(targets))
        severity = "warning" if exposure == "external" and not advisory else "info"
        absent = sorted({ref_ for pin in unprotected for ref_ in pin.walk.esd_unfitted},
                        key=natural_key)
        clamps = sorted({ref_ for pin in unprotected for ref_ in pin.walk.clamps},
                        key=natural_key)
        text = (f"{ref} ({_exposure_text(connector)}): {len(unprotected)} of {len(reaching)} "
                f"signal pins that reach a part have no ESD/TVS device on the way: "
                f"{_labels(unprotected)}. Altium's schematic review checklist asks for ESD "
                "protection on every externally accessible connection.")
        if absent:
            text += (f" {', '.join(absent)} would protect "
                     f"{'it' if len(absent) == 1 else 'them'} but "
                     f"{'is' if len(absent) == 1 else 'are'} Not Fitted in variant {variant}.")
        if clamps:
            one = len(clamps) == 1
            text += (f" {', '.join(clamps)} clamp{'s' if one else ''} to a rail or ground but "
                     f"{'is' if one else 'are'} not recognised as an ESD/TVS part: check "
                     f"{'its' if one else 'their'} rating.")
        if claimed:
            one = len(claimed) == 1
            text += (f" {', '.join(claimed)} claim{'s' if one else ''} ESD-protected pins in "
                     f"{'its' if one else 'their'} text: check that the datasheet rates "
                     "them (IEC 61000-4-2) for this port.")
        if connector.isolated:
            text += " Its integrated magnetics isolate the board side, so this is advice."
        elif connector.rf:
            text += " RF lines often take no TVS (its capacitance detunes them) or a "\
                    "low-capacitance one."
        elif connector.module_port:
            text += " A pluggable module port (SFP, QSFP): the module is what users touch "\
                    "and its high-speed lines are AC-coupled, so a TVS here is rare; this is "\
                    "advice."
        elif exposure == "unknown":
            text += " Whether the connector is user-facing is not known: pass external= if "\
                    "it is."
        elif exposure == "internal":
            text += " It looks like a board-level connector, checked because it was named."
        findings.append(_finding(
            "no_esd", severity, ref, text,
            [ref, *(pin.walk.first()[0] for pin in unprotected)],
            exposure=exposure, pins=[pin.pin for pin in unprotected],
            not_fitted_esd=absent or None, clamps=clamps or None,
            protection_claimed_by=claimed or None,
        ))
    behind = [pin for pin in lines if pin.walk.esd
              and all(hops > 0 for _, hops, _ in pin.walk.esd)]
    if behind:
        elements = sorted({edge.part for pin in behind for edge in pin.walk.series},
                          key=natural_key)
        devices = sorted({part for pin in behind for part, _, _ in pin.walk.esd},
                         key=natural_key)
        findings.append(_finding(
            "esd_behind_series", "info", ref,
            f"{ref} ({exposure}): the ESD/TVS device ({', '.join(devices[:4])}) on "
            f"{len(behind)} pin{'s' if len(behind) != 1 else ''} sits on the IC side of a "
            f"series element ({', '.join(elements[:4]) or 'see the pin table'}): "
            f"{_labels(behind)}. Often intentional (the resistor limits the current into "
            "the clamp, a choke passes the common-mode surge), but the TVS layout guidance "
            "puts the diode at the connector, before anything else.",
            [ref, *devices, *elements], exposure=exposure, pins=[pin.pin for pin in behind],
        ))
    shield = connector.shield
    if shield:
        if shield["state"] == "floating" and shield["named_shield"]:
            findings.append(_finding(
                "shield_floating", "warning" if exposure == "external" else "info", ref,
                f"{ref} ({exposure}): its shield or shell pins ({', '.join(shield['pins'][:6])}) "
                "connect to nothing: an unconnected metal shell picks up and radiates noise "
                "and gives ESD no path; the TVS layout guidance ties the shield straight to "
                "chassis or ground.", [ref], exposure=exposure, shield=shield,
            ))
        elif shield["state"] == "rc":
            findings.append(_finding(
                "shield_rc", "info", ref,
                f"{ref} ({exposure}): its shield reaches ground or chassis only through "
                f"{', '.join(shield.get('through', [])[:6])} (a resistor and/or capacitor "
                f"network on {', '.join(shield['nets'][:3])}). Altium's TVS layout article "
                "advises tying the shield directly to chassis or ground rather than through "
                "RC or snubber networks; many designs choose the RC on purpose (to break a "
                "ground loop or keep chassis off signal ground), so this is a question for "
                "the designer, not a fault.",
                [ref, *shield.get("through", [])], exposure=exposure, shield=shield,
            ))
    indirect = [pin for pin in connector.pins if pin.ground_via]
    if indirect:
        through = sorted({part for pin in indirect for part in pin.ground_via}, key=natural_key)
        dc = any(ctx.kind.get(part) in _DC_PASSIVES for part in through)
        one = len(indirect) == 1
        findings.append(_finding(
            "ground_indirect", "warning" if exposure == "external" and not dc else "info", ref,
            f"{ref} ({exposure}): ground pin{'' if one else 's'} "
            f"{', '.join(f'{pin.pin} ({pin.name or pin.pin})' for pin in indirect[:6])} on "
            f"{', '.join(sorted({pin.net for pin in indirect}, key=natural_key)[:3])} "
            f"reach{'es' if one else ''} a board ground only through "
            f"{', '.join(f'{part} ({ctx.kind.get(part)})' for part in through[:4])}"
            + (": the return current of the cable's lines crosses that part." if dc else
               ": with no DC path, the supply and signal return of the cable has to find "
               "another way (the shield, another connector).")
            + " A cable's ground pin normally joins the board ground directly; check that "
            "this is intended (some designs isolate a cable ground on purpose).",
            [ref, *through], exposure=exposure, pins=[pin.pin for pin in indirect],
            through=through,
        ))
    grounds = [pin for pin in connector.pins if pin.cls == "ground" or (
        pin.cls == "shield" and pin.net in ctx.design.grounds and not _shield_named(pin.name))]
    # Differential pairs (USB, Ethernet, CAN, RS-485) carry their own return.
    single = [pin for pin in lines if "differential" not in pin.tags]
    if len(single) >= SPARSE_MIN_SIGNALS and len(grounds) < len(single) * SPARSE_RATIO \
            and not connector.isolated and not connector.rf:
        run = _longest_signal_run(connector)
        findings.append(_finding(
            "sparse_ground", "info", ref,
            f"{ref} ({exposure}): {len(grounds)} ground pin{'s' if len(grounds) != 1 else ''} "
            f"for {len(single)} single-ended signal pins (the longest run of signal pins "
            "with no ground "
            f"between them, in pin order, is {run}). Placement guidance for EMC interleaves "
            "ground pins with the signals so each has a short return path; with few grounds "
            "the return current loops wide and the connector radiates.",
            [ref], exposure=exposure, ground_pins=len(grounds), signal_pins=len(single),
            longest_signal_run=run,
        ))
    if exposure == "external" and not (connector.rf or connector.isolated
                                       or connector.module_port):
        bare = [pin for pin in reaching
                if "high_speed" not in pin.tags and "field_bus" not in pin.tags
                and not pin.walk.series and not pin.walk.unfitted]
        if bare:
            findings.append(_finding(
                "no_series_element", "info", ref,
                f"{ref} ({_exposure_text(connector)}): {len(bare)} low-speed off-board "
                f"line{'s go' if len(bare) != 1 else ' goes'} straight to a part with no series "
                f"resistor, 0 ohm or ferrite footprint: {_labels(bare)}. EMC guidance puts a "
                "0 ohm footprint on such lines so it can become 10-33 ohm (or a ferrite) if "
                "emissions or immunity tests fail, and the schematic checklist asks for a "
                "current-limiting resistor toward MCU pins.",
                [ref, *(pin.walk.first()[0] for pin in bare)], exposure=exposure,
                pins=[pin.pin for pin in bare],
            ))
    return findings


def _longest_signal_run(connector: _Connector) -> int:
    best = run = 0
    for pin in connector.pins:  # sorted in natural pin order
        if pin.cls == "signal":
            run += 1
            best = max(best, run)
        elif pin.cls in ("ground", "shield"):
            run = 0
    return best


# ---------- PCB ----------

def _length(mil: float, units: str, exact: bool = False) -> str:
    """A distance in units for the text: as the fields round it (0.01 mm, 0.1 mil), or
    with all its digits for a limit the caller gave (0.05 mm)."""
    value = mil / MIL_PER_MM if units == "mm" else mil
    return f"{value:g} {units}" if exact else f"{round(value, 2 if units == 'mm' else 1):g} {units}"


def _pcb_findings(index, ctx: _Context, connectors: list[_Connector], max_mil: float,
                  stats: dict, units: str = "mm") -> list[dict]:
    by_name = pcb_components(index)
    wanted: set[str] = set()
    for connector in connectors:
        for pin in connector.signal_pins():
            if pin.walk and pin.walk.esd:
                wanted.add(connector.ref)
                wanted.update(part for part, _, _ in pin.walk.esd)
                if pin.walk.first():
                    wanted.add(pin.walk.first()[0])
    if not wanted:
        return []
    pads = component_pads(index, by_name, wanted)

    def pads_of(ref: str, net: str | None = None, name: str | None = None) -> list:
        return [pad for row in by_name.get(ref, []) for pad in pads[row["id"]]
                if (net is None or pad["net_name"] == net)
                and (name is None or pad["pad_name"] == name)]

    def nearest(first: list, second: list) -> float | None:
        if not first or not second:
            return None
        return min(index._primitive_distance(a, b) for a in first for b in second)

    def opposite(first: list, second: list) -> bool:
        """SMD pads on different copper layers: the path adds a via and the board."""
        one, other = {pad["layer"] for pad in first}, {pad["layer"] for pad in second}
        return THT_LAYER not in one | other and not one & other

    findings = []
    missing: set[str] = set()
    for connector in connectors:
        groups: dict[str, list[tuple[_Pin, float, float | None, bool]]] = defaultdict(list)
        for pin in connector.signal_pins():
            if not pin.walk or not pin.walk.esd:
                continue
            connector_pads = pads_of(connector.ref, name=pin.pin)
            if not connector_pads:
                missing.add(connector.ref)
                continue
            first = pin.walk.first()
            ic_pads = pads_of(first[0], name=first[1]) if first else []
            for part, _, net in pin.walk.esd:
                esd_pads = pads_of(part, net=net)
                if not by_name.get(part):
                    missing.add(part)
                    continue
                if not esd_pads:
                    stats["esd_net_differs"] += 1
                    continue
                to_connector = nearest(esd_pads, connector_pads)
                to_ic = nearest(esd_pads, ic_pads)
                flipped = opposite(esd_pads, connector_pads)
                stats["pins_measured"] += 1
                pin.pcb.setdefault("esd", []).append({
                    "designator": part, "to_connector_mil": round(to_connector, 1),
                    **({"to_ic_mil": round(to_ic, 1)} if to_ic is not None else {}),
                    **({"opposite_side": True} if flipped else {}),
                })
                groups[part].append((pin, to_connector, to_ic, flipped))
        stats["devices_measured"] += len(groups)
        if not connector.audited:
            continue
        for part, rows in sorted(groups.items(), key=lambda item: natural_key(item[0])):
            worst = max(rows, key=lambda row: row[1])
            # Within TVS_AT_CONNECTOR_MIL of the connector pad a TVS is at the connector,
            # however near the IC sits.
            closer_to_ic = [row for row in rows if row[2] is not None and row[2] < row[1]
                            and row[1] > TVS_AT_CONNECTOR_MIL]
            far = [row for row in rows if row[1] > max_mil]
            stats["closer_to_ic"] += bool(closer_to_ic)
            stats["beyond_max"] += bool(far)
            if not closer_to_ic and not far:
                continue
            pin, to_connector, to_ic, flipped = max(closer_to_ic or far, key=lambda row: row[1])
            first = pin.walk.first()
            serious = bool(closer_to_ic) and to_connector > TVS_NEAR_MIL
            severity = "warning" if serious and connector.exposure == "external" \
                and not connector.isolated and not connector.module_port else "info"
            where = (f" and {_length(to_ic, units)} from {first[0]}.{first[1]}"
                     if to_ic is not None and first else "")
            text = (f"{part}, the ESD/TVS device on {connector.ref} pin {pin.pin} ({pin.net}), "
                    f"is {_length(to_connector, units)} from the connector pad{where} "
                    "(straight line in plan view, pad edge to pad edge"
                    + ("; it is on the other side of the board from the connector pad, so the "
                       "path adds a via and the board thickness)" if flipped else ")"))
            text += ": nearer the IC than the connector, so a surge travels along the board "\
                    "before it is clamped." if closer_to_ic else \
                    f": beyond {_length(max_mil, units, exact=True)}."
            text += " The TVS layout guidance puts the diode at the connector with a short, "\
                    "wide path to ground."
            findings.append(_finding(
                "tvs_far_from_connector", severity, connector.ref, text,
                [connector.ref, part, *([first[0]] if first else [])],
                exposure=connector.exposure, device=part, pins=[row[0].pin for row in rows],
                to_connector_mil=round(to_connector, 1),
                to_ic_mil=round(to_ic, 1) if to_ic is not None else None,
                worst_to_connector_mil=round(worst[1], 1),
                opposite_side=flipped or None,
            ))
    if missing:
        stats["not_on_pcb"] = sorted(missing, key=natural_key)[:30]
    return findings


# ---------- rows ----------

def _pin_row(ctx: _Context, pin: _Pin) -> dict:
    row: dict = {"pin": pin.pin}
    if pin.name and pin.name != pin.pin:
        row["name"] = pin.name
    row["net"] = pin.net
    row["class"] = pin.cls
    if pin.why:
        row["class_basis"] = pin.why
    if pin.tags:
        row["tags"] = pin.tags
    walk = pin.walk
    if pin.power:
        row.update(pin.power)
    if walk is None:
        return row
    row["esd"] = [{"designator": part, "side": "connector" if hops == 0 else "behind_series",
                   **({"net": net} if hops else {})} for part, hops, net in walk.esd[:3]]
    if walk.esd_unfitted:
        row["esd_not_fitted"] = walk.esd_unfitted[:3]
    if walk.clamps:
        row["clamp_diodes"] = walk.clamps[:3]
    if walk.series:
        row["series"] = [edge.describe() for edge in walk.series]
    if walk.unfitted:
        row["series_not_fitted"] = [edge.describe() for edge in walk.unfitted[:3]]
    if walk.caps:
        row["caps_to_ground"] = [{"designator": ref, "value": farads_text(farads)}
                                 for ref, farads in walk.caps[:3]]
    if walk.pulls:
        row["pulls"] = [pull.describe() for pull in walk.pulls[:2]]
    first = walk.first()
    if first:
        part, part_pin, _, hops = first
        reach = {"designator": part, "pin": part_pin, "kind": ctx.kind.get(part)}
        name = ctx.design.pin_name(part, part_pin)
        if name and name != part_pin:
            reach["name"] = name
        if electrical := ctx.design.pin_type(part, part_pin):
            reach["type"] = electrical
        reach["series_elements"] = hops
        row["reaches"] = reach
        more = sorted({item[0] for item in walk.reached[1:] if item[0] != part}, key=natural_key)
        if more:
            row["also_reaches"] = more[:4]
    elif walk.hop_limited:
        row["reaches"] = f"nothing within {HOP_LIMIT} series elements"
    else:
        row["reaches"] = None
    if pin.pcb:
        row["pcb"] = pin.pcb
    return {key: value for key, value in row.items() if value not in ([], {})}


def _connector_row(ctx: _Context, connector: _Connector) -> dict:
    counts: dict[str, int] = defaultdict(int)
    for pin in connector.pins:
        counts[pin.cls] += 1
    reaching = exposed_lines(ctx, connector)
    row: dict = {
        "designator": connector.ref,
        "part": (ctx.comment.get(connector.ref) or "")[:80] or None,
        "exposure": connector.exposure,
        "exposure_basis": connector.basis,
        "pins": dict(counts),
        "signal_pins_reaching_a_part": len(reaching),
        "esd_protected": sum(1 for pin in reaching if pin.walk.esd),
        "esd_unprotected": sum(1 for pin in reaching if not pin.walk.esd),
        "with_series_element": sum(1 for pin in reaching if pin.walk.series),
    }
    if connector.rf:
        row["rf"] = True
    if connector.isolated:
        row["integrated_magnetics"] = True
    if connector.module_port:
        row["module_port"] = True
    if connector.shield:
        row["shield"] = connector.shield["state"]
    return {key: value for key, value in row.items() if value is not None}


# ---------- entry point ----------

def audit_connector(ctx: _Context, ref: str, override: str | None = None) -> _Connector:
    """One connector as run_interface_check reads it: each pin's class, a signal pin's
    walk to the first part, a power pin's ESD and the rail it feeds through series
    parts, the exposure (external, internal or unknown; override sets it) and the
    shield. get_power_tree reads the power pins and the exposure."""
    power_connector = bool(_POWER_CONNECTOR.search(ctx.text.get(ref, "")))
    pins = _classify_pins(ctx, ref, power_connector)
    connector = _Connector(ref, pins)
    walks: dict[str, _Walk] = {}
    for pin in pins:
        if pin.cls == "signal":
            if pin.net not in walks:
                walks[pin.net] = _walk(ctx, ref, pin.net)
            pin.walk = walks[pin.net]
            first = pin.walk.first()
            # A line into a CAN, RS-485 or RS-232 transceiver is a field bus whatever
            # its name (RS-485 A and B); one into a PHY's LED pin drives a LED.
            if first and bus_transceiver(ctx, first[0]):
                pin.tags += [tag for tag in ("field_bus", "differential")
                             if tag not in pin.tags]
            if first and "led" not in pin.tags and _LED_PIN.fullmatch(
                    first_function(ctx.design.pin_name(first[0], first[1]) or "")):
                pin.tags.append("led")
        elif pin.cls == "power":
            rail, through = _power_feed(ctx, pin.net)
            protection = sorted({part for part in ctx.esd.get(pin.net, ())
                                 if ctx.fitted.get(part, True)}, key=natural_key)
            if protection:
                pin.power["esd"] = protection[:3]
            if rail and rail != pin.net:
                pin.power["feeds"] = {"rail": rail, "through": through}
    _exposure(ctx, connector, override)
    connector.shield = _shield_state(ctx, connector)
    return connector


def run_interface_check(
    netlist: dict,
    plan=None,
    project_params: dict | None = None,
    index=None,
    pcb_unavailable: str | None = None,
    checks: list[str] | None = None,
    connectors: list[str] | None = None,
    external: list[str] | None = None,
    internal: list[str] | None = None,
    max_distance_mil: float = DEFAULT_MAX_DISTANCE_MIL,
    offset: int = 0,
    limit: int = FINDING_LIMIT,
    connector_limit: int = CONNECTOR_LIMIT,
    pin_tables: bool | tuple[str, ...] = True,
    table_rows: int | None = None,
    units: str = "mm",
) -> dict:
    """The audit: overview counts, per-connector summaries, findings (paged, warnings
    first) and the per-pin tables of the listed connectors (of every named one when
    connectors are named): all of them when pin_tables is True, those of the exposures it
    names when a tuple ('external',), none when False. With table_rows a table of more
    pins keeps only its signal pins, at most that many, and pin_tables_truncated says so.
    Distances in the result are in mil; units ('mm' or 'mil') is for the findings' text.
    Heuristic: see the module notes."""
    requested = [check.strip().lower() for check in checks or [] if check.strip()]
    if invalid := [check for check in requested if check not in CHECKS]:
        return {"error": "invalid_checks",
                "message": f"Unknown checks {invalid}; use {list(CHECKS)}."}
    ctx = build_context(netlist, plan, project_params)
    variant = getattr(plan, "name", None) or "[No Variations]"
    notes: list[str] = []
    found = [ref for ref, kind in ctx.kind.items() if kind == "connector"]
    wanted, unknown = resolve_names(connectors, ctx.kind)
    if connectors and not wanted:
        return {"error": "connector_not_found",
                "message": f"No part {unknown} in the compiled netlist; use "
                           "search_components to find the designator."}
    overrides: dict[str, str] = {}
    for exposure, names in (("external", external), ("internal", internal)):
        matched, missing = resolve_names(names, ctx.kind)
        for ref in matched or ():
            overrides[ref] = exposure
        unknown += missing
    if unknown:
        notes.append(f"Not found: {', '.join(unknown)}.")
    audited = found
    if wanted is not None:
        for ref in sorted(wanted - set(found), key=natural_key):
            notes.append(f"{ref} is not recognised as a connector (by its designator prefix "
                         "and text); audited as asked.")
        audited = sorted(wanted, key=natural_key)
    else:
        audited = sorted(found, key=natural_key)
    not_fitted = [ref for ref in audited if not ctx.fitted.get(ref, True)]
    if not_fitted:
        notes.append(f"{len(not_fitted)} connector{'s are' if len(not_fitted) != 1 else ' is'} "
                     f"Not Fitted in variant {variant} and not checked"
                     + (f": {', '.join(not_fitted[:10])}." if wanted is not None else "."))
    audited = [ref for ref in audited if ctx.fitted.get(ref, True)]
    built: list[_Connector] = []
    for ref in audited:
        connector = audit_connector(ctx, ref, overrides.get(ref))
        connector.audited = connector.exposure != "internal" or wanted is not None
        built.append(connector)
    findings: list[dict] = []
    for connector in built:
        findings += _connector_findings(ctx, connector, variant)
    stats: dict = defaultdict(int)
    not_checked: dict[str, str] = {}
    if index is not None:
        findings += _pcb_findings(index, ctx, built, max_distance_mil, stats, units)
    else:
        not_checked.update({check: pcb_unavailable or "no PcbDoc" for check in PCB_CHECKS})
    wanted_checks = set(requested or CHECKS)
    findings = [finding for finding in findings if finding["check"] in wanted_checks]
    findings.sort(key=lambda finding: (
        _SEVERITY_RANK[finding["severity"]], _CHECK_RANK[finding["check"]],
        natural_key(finding["connector"]),
    ))
    summary: dict[str, int] = defaultdict(int)
    for finding in findings:
        summary[finding["check"]] += 1
    rank = {"external": 0, "unknown": 1, "internal": 2}
    built.sort(key=lambda connector: (rank[connector.exposure], natural_key(connector.ref)))
    rows = [_connector_row(ctx, connector) for connector in built]
    offset = max(0, int(offset or 0))
    result: dict = {
        "variant": variant,
        "heuristic": (
            "Connectors, ESD/TVS parts and series elements are recognised by designator "
            "prefix and the words in their description, comment, value, footprint and part "
            "number; whether a connector is user-facing is a guess (pass external= or "
            "internal= to correct it). Choosing the protection part (standoff and clamping "
            "voltage, capacitance, uni- or bidirectional) is a datasheet question this tool "
            "does not judge."
        ),
        "checks_run": [check for check in CHECKS
                       if check in wanted_checks and check not in not_checked],
        "overview": _overview(ctx, built),
        "summary": dict(summary),
        "severity_counts": {level: sum(1 for f in findings if f["severity"] == level)
                            for level in ("warning", "info")},
        "connectors_total": len(rows),
        "connectors": rows[:connector_limit],
    }
    if index is not None:
        result["pcb"] = {
            "esd_pins_measured": stats["pins_measured"],
            "esd_devices_measured": stats["devices_measured"],
            "max_distance_mil": round(max_distance_mil, 3),
            "measured": "straight line in plan view, pad edge to pad edge, from each ESD/TVS "
                        "pad on the line to the connector pin's pad and to the first IC pin's "
                        "pad; opposite_side marks a TVS on the other side of the board from "
                        "the connector pad",
        }
        if stats.get("not_on_pcb"):
            result["pcb"]["not_on_pcb"] = stats["not_on_pcb"]
        if stats["esd_net_differs"]:
            result["pcb"]["esd_pads_on_other_nets"] = stats["esd_net_differs"]
    if pin_tables:
        listed = {row["designator"] for row in rows[:connector_limit]}
        tables, truncated = {}, {}
        for connector in built:
            if wanted is None and (connector.ref not in listed or (
                    pin_tables is not True and connector.exposure not in pin_tables)):
                continue
            pins = connector.pins
            if table_rows is not None and len(pins) > table_rows:
                signals = connector.signal_pins()
                pins = signals[:table_rows]
                truncated[connector.ref] = (
                    f"{len(pins)} of {len(connector.pins)} pins: signal pins only"
                    + (f", the first {len(pins)} of {len(signals)}"
                       if len(signals) > len(pins) else ""))
            tables[connector.ref] = [_pin_row(ctx, pin) for pin in pins]
        result["pin_tables"] = tables
        if truncated:
            result["pin_tables_truncated"] = truncated
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
        f"A signal pin is followed through up to {HOP_LIMIT} series elements (resistors, "
        "0 ohm links, ferrites, inductors, common-mode chokes, capacitors up to 10 uF) to "
        "the first part that is not passive; ESD on its own net is on the connector side, "
        "after a series element behind it. LED and magnetics centre-tap pins are listed but "
        "not counted as lines. Internal connectors get findings only when named in "
        "connector=. Pins left unconnected are listed without a net."
    )
    result["notes"] = notes
    return result


def _overview(ctx: _Context, connectors: list[_Connector]) -> dict:
    by_exposure: dict[str, dict[str, int]] = {}
    for connector in connectors:
        entry = by_exposure.setdefault(connector.exposure, defaultdict(int))
        entry["connectors"] += 1
        for pin in connector.pins:
            if pin.cls == "power":
                entry["power_pins"] += 1
                entry["power_pins_with_esd"] += "esd" in pin.power
                entry["power_pins_through_series_part"] += "feeds" in pin.power
        entry["signal_pins"] += len(connector.signal_pins())
        for pin in exposed_lines(ctx, connector):
            entry["reaching_a_part"] += 1
            entry["with_esd"] += bool(pin.walk.esd)
            entry["with_series_element"] += bool(pin.walk.series)
    return {
        "connectors": len(connectors),
        "by_exposure": {key: dict(value) for key, value in by_exposure.items()},
        "esd_devices": sum(1 for kind in ctx.kind.values() if kind == "esd"),
    }
