"""Power tree extraction and power-path checks (heuristic): sources, protection,
converters, rails and loads, with each regulator's EN, PG, feedback divider and
capacitors.

Converters are the parts fitted in the variant that read as linear regulators or LDOs,
switching regulators and controllers (buck, boost, buck-boost, SEPIC, flyback,
inverting), charge pumps, PMICs, DC/DC and power modules (PSUs), load switches, eFuses,
hot-swap and ideal-diode controllers or chargers, by the words in their description,
comment and part number and by their pin names (VIN, VOUT, SW, FB, EN, PG ...), and
regulator modules on a connector's designator whose words name a regulator and whose
pins an input and an output; references and supervisors are recognised and listed, not
taken for converters (a reset, watchdog or reference word after a regulator word is a
feature of that regulator), and MOSFET or gate drivers are not converters. Each
converter's input is the net on its VIN/IN pins; its output the net on its VOUT/OUT
pins, or for a switcher the far side of the inductor on its SW/LX/PH pin (through a
diode for a boost, through the MOSFETs a controller's gate pins drive), for a pass
controller (load switch, eFuse, hot-swap, ideal diode) the far side of the MOSFET it
drives, or the top of its feedback divider. Rails are the nets check_decoupling reads
as rails, every converter's input and output, the power pins of connectors and
batteries, and unnamed nets that only carry power between two of them (a fuse to a
diode to a P-FET). Edges join rails: converters (input to output), and fitted series
parts between rails: 0 ohm links and shunts, resistors up to 100 ohm (a larger one
only into reference inputs: a VREF divider), ferrites and inductors (filters), fuses,
diodes (OR-ing, reverse protection), MOSFETs or transistors (reverse polarity or load
switches), mechanical switches and jumpers. Sources are connector power pins
(check_interfaces' classes), batteries, converter outputs, voltage references' outputs
and IC pins that regulate a net themselves (VCAP, VDD_CORE, an internal SMPS's LX
through its inductor, a converter's own bias pin); a rail is supplied when one reaches
it through the edges. Loads are the fitted parts with a supply pin on a rail (never
resistors or capacitors).

Per converter: the EN pin's state (tied to its input or another rail, pulled up or down,
an undervoltage divider with its ratio, driven by a GPIO, by another converter's PG
(sequencing) or a supervisor, or floating, and whether it is held at the level that
keeps the part off), the PG pin (pulled up to which rail, read by which EN, reset or
GPIO, or unused), input and output capacitors, the feedback divider (Rtop, Rbot, the
ratio, and the Vref the rail name implies, or the output a given Vref makes, with a leg
a jumper switches in) and soft-start, current-limit and mode pins. Per power input (a
connector power pin not fed from the board, or a battery; findings for external
connectors and power-only ones of unknown exposure): the fuse, reverse-polarity element (a forward diode, a P-FET with its drain or an
N-FET with its source toward the input, an ideal diode), TVS or clamp, bulk capacitance
and inrush parts on its way in. Per switching converter: whether an inductor or ferrite
with capacitors (LC or pi) separates its input from its source.

Guidance: Altium's schematic review checklist (sequencing, supervisors, input range,
fuses, reverse-polarity, overvoltage and undervoltage protection, input filters on
switchers), its power-supply guideline article (the power tree, ENABLE/PGOOD
sequencing) and its EMC certification article (a pi filter or similar on every SMPS
input). Everything here is a heuristic draft read from names: parts named otherwise are
missed, internal pull-ups and fixed-output options are invisible, Vref and thresholds
come from datasheets, and Claude should verify each converter against its datasheet.
"""

import re
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field

try:
    from services.bom import build_parts, natural_key
    from services.decoupling import (
        FERRITE_TEXT, IC_PREFIXES, capacitance, designator_prefix, farads_text,
        first_function, net_kind, net_tokens, pin_role,
    )
    from services.firmware_pins import negated
    from services.interfaces import PROCESSOR_TEXT, audit_connector, build_context
    from services.pull_resistors import (
        control_kind, link_group, ohms_text, rail_voltage, resistance, resistor_sections,
        resolve_names,
    )
except ImportError:
    from server.services.bom import build_parts, natural_key
    from server.services.decoupling import (
        FERRITE_TEXT, IC_PREFIXES, capacitance, designator_prefix, farads_text,
        first_function, net_kind, net_tokens, pin_role,
    )
    from server.services.firmware_pins import negated
    from server.services.interfaces import PROCESSOR_TEXT, audit_connector, build_context
    from server.services.pull_resistors import (
        control_kind, link_group, ohms_text, rail_voltage, resistance, resistor_sections,
        resolve_names,
    )

CHECKS = (
    "rail_without_source", "enable_floating", "enable_held_off", "fb_divider_mismatch",
    "no_input_fuse", "no_reverse_protection", "no_input_tvs", "smps_input_unfiltered",
    "no_output_capacitor", "no_input_capacitor", "converter_unknown_rail", "pgood_unused",
    "sequencing", "enable_tied_note",
)
# An EN tied straight to its own input is the usual way to run a regulator: the note
# (no undervoltage divider, so it starts at its internal UVLO) runs only when asked.
OPT_IN_CHECKS = frozenset({"enable_tied_note"})
_CHECK_RANK = {check: rank for rank, check in enumerate(CHECKS)}
_SEVERITY_RANK = {"warning": 0, "info": 1}

PAGE_LIMIT = 100  # rows per page of converters, findings, rails, sources and inputs
LOADS_LISTED = 8
LINK_OHMS = 0.1  # a resistor up to this joins two nets (0 ohm links, current shunts)
FILTER_OHMS = 100.0  # a series resistor up to this can carry a supply (an RC filter)
VOUT_TOLERANCE = 0.05  # a given Vref's output this far from the rail name's voltage
COMMON_VREFS = (0.4, 0.5, 0.6, 0.75, 0.765, 0.8, 0.9, 1.0, 1.2, 1.215, 1.23, 1.25)
COMMON_VREF_TOLERANCE = 0.03
TRUNK_STEPS = 5  # series steps followed from a power input before giving up
HEURISTIC = (
    "A draft read from names: converters, pins and protection parts are recognised by "
    "designator prefix, the words in their description, comment and part number and "
    "their pin names; internal pull-ups, fixed-output options, Vref and thresholds are "
    "datasheet facts this tool cannot see. Verify each converter against its datasheet."
)

# ---------- converter text ----------

_CATEGORY_TEXT = (
    ("supervisor", r"supervis|reset (?:IC|generator|controller|circuit|monitor)|voltage "
                   r"(?:monitor|detector|supervisor)|window (?:comparator|watchdog)|"
                   r"power[- ]on reset|watchdog|under-?voltage (?:detect|monitor|lockout IC)|"
                   r"over-?voltage (?:detect|monitor)|sequencer|power[- ]?good (?:monitor|"
                   r"generator)|voltage detector"),
    ("reference", r"(?:voltage|shunt|precision|bandgap|series|micropower) (?:voltage )?"
                  r"reference|reference (?:voltage|diode|IC)\b|shunt regulator|"
                  r"(?<![A-Z0-9])(?:TL431|TLV431|ATL431|AZ431|LM4040|LM4041|LM4050|LM385|"
                  r"REF\d{2,4}|ADR\d{3,4})"),
    ("charger", r"charger|charge (?:management|controller)|battery management|\bBMS\b|"
                r"li-?ion charg|power[- ]path management"),
    ("ideal_diode", r"ideal[- ]diode|\bOR-?ing (?:controller|FET|MOSFET)|\bORing\b|"
                    r"power (?:mux|multiplexer)|priority (?:switch|mux)"),
    ("hot_swap", r"hot[- ]?swap|surge stopper|inrush (?:current )?(?:limit|controller)"),
    ("efuse", r"e-?fuse|electronic fuse|smart fuse|circuit breaker IC|"
              r"over-?voltage protect\w* (?:switch|IC|controller)|\bOVP (?:switch|IC|controller)"),
    ("load_switch", r"load switch|power (?:distribution )?switch|current[- ]limit(?:ed|ing)? "
                    r"(?:power |load )?switch|high[- ]side (?:power )?switch|USB (?:power )?"
                    r"switch|slew[- ]rate controlled|smart high[- ]side"),
    ("pmic", r"\bPMIC\b|power management (?:IC|unit|integrated)|\bPMU\b|multi-?(?:output|"
             r"channel|rail) (?:regulator|converter|power|PMIC|DC)|(?:dual|triple|quad|"
             r"\d)[- ](?:output|channel) (?:buck|step-?down|regulator|converter|DC|LDO)"),
)
_TOPOLOGY_TEXT = (
    ("buck_boost", r"buck[- /]?boost|4-switch|four-switch|non-?inverting buck"),
    ("sepic", r"\bSEPIC\b"),
    ("flyback", r"flyback"),
    ("charge_pump", r"charge pump|switched[- ]capacitor|voltage doubler|"
                    r"voltage (?:converter|inverter)\b"),
    ("inverting", r"\binverting\b|negative (?:output|voltage) (?:converter|regulator|"
                  r"DC)|\bCuk\b"),
    ("boost", r"\bboost\b|step[- ]?up"),
    ("buck", r"\bbuck\b|step[- ]?down|PowerSoC"),
    ("ldo", r"\bV?LDO\b|low[- ]?drop-?out|linear (?:voltage )?regulator|"
            r"(?:positive|negative) (?:voltage )?regulator|(?:3|three)-terminal"),
    ("switching", r"switch(?:ing|-?mode) (?:regulator|converter|controller|power)|"
                  r"\bDC[- /]?DC\b|PWM (?:controller|regulator)|synchronous (?:regulator|"
                  r"converter|controller)|\bSMPS\b|\bconverter\b|power (?:supply )?module|"
                  r"\bPSU\b|point[- ]of[- ]load"),
    ("regulator", r"regulator|\bVREG\b|power supply"),
)
_CATEGORY_RE = [(kind, re.compile(pattern, re.I)) for kind, pattern in _CATEGORY_TEXT]
# Categories a regulator can carry as a feature: they win only when named first.
_FEATURE_CATEGORIES = frozenset({"supervisor", "reference", "load_switch"})
_TOPOLOGY_RE = [(kind, re.compile(pattern, re.I)) for kind, pattern in _TOPOLOGY_TEXT]
# Words naming what else a part is. An earlier power word wins ('LDO with error amplifier').
_NOT_POWER = re.compile(
    r"micro-?controller|\bMCU\b|processor|\bFPGA\b|\bCPLD\b|op-?amp|operational amplifier|"
    r"instrumentation amplifier|\bamplifier|transceiver|\bPHY\b|codec|\bADC\b|\bDAC\b|"
    r"analog[- ]to[- ]digital|digital[- ]to[- ]analog|sensor|memory|\bflash\b|EEPROM|"
    r"\bS?D?RAM\b|oscillator|crystal|multiplexer|\bmux\b|\bbuffer|\blogic\b|gate driver|"
    r"mosfet driver|motor driver|LED driver|display driver|audio|USB (?:hub|controller|"
    r"bridge)|\bUART\b|isolator|opto|comparator|level[- ]shift|translator|analog switch|"
    r"\bclock\b|\btimer\b|\bRTC\b|real[- ]time|thermistor|\bfan\b|\bIGBT\b|\bmotor\b", re.I)
# A driver or switch stage, wherever its words stand ('Synchronous buck MOSFET driver'),
# unless a converter, regulator or controller noun says the part is the whole converter.
_DRIVER_TEXT = re.compile(r"(?:MOSFET|gate|FET|IGBT|bridge) drivers?\b|\bIGBT\b|\bIPM\b|"
                          r"intelligent power module", re.I)
_CONVERTER_NOUN = re.compile(r"converter|regulator|controller|\bLDO\b|\bPMIC\b|"
                             r"power supply", re.I)
# Part-number families, read only when the words say nothing.
_MPN_FAMILIES = (
    ("reference", r"TL431|TLV431|LM4040|LM4041|LM4050|LM4120|LM385|REF\d{2,4}|ADR\d{3,4}|"
                  r"LT1389|LT1009|LT1004|LT6656|LTC6655|ISL21\d{3}|MCP15\d\d"),
    ("supervisor", r"TPS3\d{3}|TLV8\d\d|APX8\d\d|ADM8\d\d|STM6\d{3}|LTC29\d\d|TCM809|"
                   r"NCP30\d|MIC8\d\d"),
    ("load_switch", r"TPS22\d{3}|SIP3\d{3}|AP22\d\d|MIC20\d\d|AAT4\d{3}|SLG59\w+|"
                    r"FPF\d{4}|NCP45\d\d|STMPS\d+"),
    ("charge_pump", r"ICL7660|LTC1044|TPS60\d{3}|LM266[2-5]|LM828|MAX660|MAX1044|"
                    r"TC7660|TC1044"),
    ("boost", r"TPS61\d+|TLV61\d+|LM273[135]|LT1308|LT1930|MT3608|MCP16\d\d|SX1308"),
    ("buck_boost", r"TPS63\d+"),
    ("pmic", r"TPS65\d+|AXP\d{3}|DA9\d{3}|ACT88\d\d|PCA94\d\d|STPMIC\w+|MAX7\d{4}"),
    ("buck", r"TPS5\d{4}|TPS62\d+|TLV62\d+|LMR\d{5}|LM25\d{2,3}|LM26\d\d|LM2596|LM2576|"
             r"LM2675|LMZ\w+|LT86\d\d|LT36\d\d|LT80\d\d|LTC36\d\d|MP1\d{3}|MP2\d{3}|"
             r"MP8\d{3}|AOZ\d{4}|SY8\d{3}|RT8\d{3}|RT6\d{3}|ISL8\d{3}|EN6\d{3}|EN5\d{3}|"
             r"ST1S\d\d|XL\d{4}"),
    ("ldo", r"LM317|LM337|LM1117|AMS1117|LD1117|LD39\d{3}|LM78\d\d|LM79\d\d|L78\w+|"
            r"L79\w+|MC78\w+|LP29\d\d|LP38\d\d|LP59\d\d|TLV7\d{2,4}|TPS7[0-9AB]\w+|"
            r"MIC5\d{3}|MCP17\d\d|XC62\d\d|AP2\d{3}|AP7\d{3}|NCP1\d\d|RT9\d{3}|ME6\d{3}|"
            r"HT7\d{3}|SPX\d{4}|LM2937|LM108[456]|LT176\d|LT30\d\d|LT1963|ADP1\d\d|"
            r"ADP7\d{3}|LP2985|LP5907|LP5912|LDK\d{3}|LDL\d{3}|AZ1117"),
)
_MPN_RE = [(kind, re.compile(rf"(?<![A-Z0-9])(?:{pattern})", re.I))
           for kind, pattern in _MPN_FAMILIES]
_MODULE_TEXT = re.compile(r"\bmodule\b|[uµμ]Module|power block|\bbrick\b|"
                          r"isolated DC[- /]?DC", re.I)
_MODULE_PREFIXES = frozenset({"PS", "PSU", "M", "MOD", "DC", "PM", "PWR", "A"})
_CONTROLLER_TEXT = re.compile(r"controller", re.I)
_ISOLATED_TEXT = re.compile(r"\bisolated\b", re.I)
SWITCHING_KINDS = frozenset({"buck", "boost", "buck_boost", "sepic", "flyback",
                             "inverting", "switching"})
SWITCH_KINDS = frozenset({"load_switch", "efuse", "hot_swap", "ideal_diode"})

# ---------- pins ----------

_V = r"(?!\w*(?:CFG|SET|SNS|SENS|FB|ADJ|TRIM|MARG|SEL|EN|DIS|MON|OK|GOOD|UV|OV))"
_PIN_KIND_TEXT = (
    ("gnd", r"\w*GND\w*|VSS\w*|EP|EPAD|E-PAD|PAD|THERMAL\w*|TAB|GROUND"),
    ("return", r"-V?IN\d*|V?IN-|-V?OUT\d*|V?OUT-|0V|COM"),
    ("fb", r"V?FB\d*[A-Z]?|FBK\d*|FB_?\w+|N?FB|ADJ\d*|VADJ\w*|V?SET\d*|TRIM\d*|VOUT_?SET\w*|"
           r"VOSET|VPROG\d*|INV"),
    ("isense", r"ISNS\w*|ISEN\w*|CS[PN+-]\d*|SENSE[+-]|ISENSE\w*|IMON\w*|IOUT\w*|CSA|CSB"),
    ("sense", r"V?OS\d*|VOS\w+|V?SENSE\w*|SNS\w*|VOSNS\w*|OUTS\w*|VO?SEN\w*|VOUTS\w*"),
    # EN_PB (an Enpirion part's pre-bias mode select) is a mode pin, not an enable.
    ("en", r"(?!EN_?PB$)N?EN(?:_?[A-Z]*\d*)?|ENA|ENABLE\w*|UVLO\d*|N?SHDN\d*|SHUTDOWN|"
           r"RUN\d*|CE|N?ON|ON_?OFF|N?INH\w*|N?SD"),
    # PG_1V8 or PG_CORE is a power-good; PGATE, PGM and PG_DRV are not (gate drive, a
    # program pin).
    ("pg", r"N?PG\d*|N?PGOOD\w*|N?PWRGD\w*|N?PGD\d*|N?POK\d*|N?PWROK\w*|POWERGOOD|"
           r"N?PWR_?GOOD\w*|PG_(?!DRV|GATE|M\b)\w+"),
    ("reset", r"N?RESET\w*|N?RST\w*|N?POR|N?WDO\w*"),
    ("flag", r"N?FLAG\w*|N?FAULT\w*|N?FLT\w*|N?OC\d*|N?ALERT\w*|N?OVC\w*"),
    ("sw", r"SW\d*[A-Z]?|SW_?\d+|LX\d*[A-Z]?|LX_?\d+|PH\d*|PHASE\d*|VSW\d*|SWN|SWNODE|L[12]"),
    ("bst", r"BST\w*|BOOT\d*|BS|CB|CBOOT\d*|VBST\d*|BOOTSTRAP|C?BST\w*"),
    ("gate", r"HG\d*|LG\d*|GH\d*|GL\d*|HO\d*|LO\d*|HDRV\d*|LDRV\d*|TG\d*|BG\d*|DH\d*|DL\d*|"
             r"UG\d*|UGATE\d*|LGATE\d*|GATE\w*|GDRV\w*|DRVH\d*|DRVL\d*|HSD\d*|LSD\d*|"
             r"NGATE\w*|PGATE\w*|NDRV\w*|PDRV\w*|[PN]G_?DRV\w*|GD\d*|G\d?|DRV\d*"),
    ("ss", r"SS\d*|SS_?\w+|SSTR\d*|TR_?SS\d*|TRK\d*|TRACK\d*"),
    ("comp", r"COMP\w*|VC\d*|ITH\d*"),
    ("ilim", r"ILIM\w*|ISET\w*|OCSET\w*|OCP\w*|RLIM\w*|I?LIMIT\w*|CL|SETI|ICHG\w*|IPRG\w*|"
             r"PROG|RILIM|IMAX"),
    ("mode", r"MODE\w*|EN_?PB|SYNC\w*|FSW\w*|FREQ\w*|RT\d*|RT_?CLK|FSET\w*|PFM\w*|FPWM|"
             r"PWM|LLM|PS|CLKIN|FS"),
    # AVINO (an Enpirion part's internal regulator output for its AVIN) is a bias pin.
    ("in", rf"(?!AVINO$)(?:P|A|S|D|L|H|B|C|PV|AV)?VIN{_V}\w*|IN\d*|INPUT|VI|VCC_?IN|VDD_?IN|"
           r"V_IN|IN_?\d+"),
    ("out", rf"V?OUT{_V}\w*|VO\d*|OUTPUT|LDO_?OUT\w*|OUT_?\d+|VCC_?OUT|VDD_?OUT|V_OUT|"
            r"LDO\d*"),
    ("bias", r"BIAS\w*|VCC\w*|VDD\w*|INTVCC|DRVCC|EXTVCC|VBIAS|V5V|VREG\w*|VDDB|AVINO|"
             r"INTVDD|VL|V\+|VS|VDRV|PVCC|AVCC|DVDD|AVDD|VBAT\w*|VBUS\w*|BAT\w*|SYS|VSYS"),
)
_PIN_KIND_RE = [(kind, re.compile(pattern)) for kind, pattern in _PIN_KIND_TEXT]
_CHARGER_IN = re.compile(r"V?IN\d*|VBUS\w*|USB\w*|ACIN|DCIN|VAC|VDC|VCHG|CHG_?IN|ADP\w*")
_CHARGER_OUT = re.compile(r"V?BAT\w*|BATT?\w*|V?SYS\w*|V?OUT\w*")
_WEAK_IN = re.compile(r"VCC\w*|VDD\w*|V\+|VS|VBAT\w*|VBUS\w*|PVCC|VSUP\w*|VPWR")
# A pin that regulates its own net: an internal LDO's output or capacitor pin; not the
# internal regulator's input (a name ending in IN, such as VDD_REG_IN).
_INTERNAL_OUT = re.compile(
    r"(?!\w*IN\d*$)(?:V?OUT\w*|VREG\w*|LDO\w*|VDD_?OUT\w*|VCC_?OUT\w*|"
    r"V\d+P?\d*_?OUT\w*|VCAP\w*|VDD_?CORE\w*|DCDC\w*|VDD_?REG\w*|REG_?OUT\w*|VREGOUT\w*)")
_INTERNAL_SW = re.compile(r"\w*(?:LX|SW|DCDC|SMPS)\w*")
_DIODE_ANODE = re.compile(r"A\d?|ANODE\d?|AN")
_DIODE_CATHODE = re.compile(r"K\d?|C\d?|CATHODE\d?|KA|CA")
_SCHOTTKY_TEXT = re.compile(r"schottky", re.I)
_P_CHANNEL = re.compile(r"P-?CH\w*|P-?channel|\bPMOS|P-MOS|\bPFET|P-FET|\bPNP\b", re.I)
_N_CHANNEL = re.compile(r"N-?CH\w*|N-?channel|\bNMOS|N-MOS|\bNFET|N-FET|\bNPN\b", re.I)
_BJT_TEXT = re.compile(r"\bBJT\b|bipolar|\bNPN\b|\bPNP\b", re.I)
_FUSE_TEXT = re.compile(r"\bfuse|polyfuse|polyswitch|poly-?switch|resettable|\bPPTC\b|"
                        r"\bPTC\b", re.I)
_NTC_TEXT = re.compile(r"\bNTC\b|inrush", re.I)
_BATTERY_TEXT = re.compile(r"battery|coin cell|cell holder|\bLi-?ion\b|\bLiPo\b|"
                           r"\bCR\d{4}\b|\bBR\d{4}\b", re.I)
_BATTERY_PLUS = re.compile(r"\+|\+?V?BATT?\w*\+?|POS\w*|PLUS|[VB]\+|\+V\w*")
_USB_TEXT = re.compile(r"\bUSB|type[- ]?c\b", re.I)
_SUPERVISED_IN = re.compile(r"SENSE\w*|V?MON\w*|IN\d*[AB+-]?|VIN\w*|VDD\w*|VCC\w*|VS")
_CHANNEL = re.compile(r"(\d+)$")

# Part kinds (check_interfaces' categories) that are never loads or converters.
_PASSIVE_KINDS = frozenset({"resistor", "capacitor", "inductive", "choke", "diode",
                            "zener", "esd", "fuse", "passive", "connector", "switch",
                            "relay", "transformer"})
# Members an unnamed net may have and still only carry power between two rails.
_POWER_PATH_KINDS = frozenset({"capacitor", "esd", "zener", "diode", "fuse", "inductive",
                               "choke", "resistor", "connector", "switch", "relay",
                               "passive"})
_VOLTAGE_PIN = re.compile(r"[+-]?\d{1,3}(?:P\d{1,3})?V\d{0,3}|V\d+P\d+")  # +15V, 3P3V, 1V8
_SERIES_PINS = re.compile(r"[SDCE]\w*|SOURCE\w*|DRAIN\w*|COLLECTOR\w*|EMITTER\w*")


def _clean(name: str | None) -> str:
    """A pin name's first function without negation marks or a '+' (+VIN, VOUT+)."""
    text = first_function(name or "")
    return re.sub(r"^[~!#/*+]+|[~!#*+]+$", "", text)


def pin_kind(name: str | None) -> str | None:
    """in, out, sw, fb, en, pg, reset, flag, bst, gate, ss, comp, ilim, mode, sense,
    isense, bias, gnd or return for a power part's pin name; None otherwise."""
    clean = _clean(name)
    if not clean or clean.isdigit():
        return None
    for kind, pattern in _PIN_KIND_RE:
        if pattern.fullmatch(clean):
            return kind
    return None


def _channel(name: str | None) -> str | None:
    match = _CHANNEL.search(_clean(name))
    return match.group(1) if match else None


def classify_text(text: str) -> tuple[str | None, str | None]:
    """(kind, basis) from a part's words: the category words first (supervisor,
    reference, charger, ideal diode, hot-swap, eFuse, load switch, PMIC), then the
    earliest topology word (buck, boost, LDO ...). A supervisor, reference or load-switch
    word after a topology or other category word is a feature ('LDO with reset and
    watchdog', 'buck with integrated load switch'). None when words of another function
    come first (an MCU, op-amp, sensor, LED driver), or for a MOSFET or gate driver with
    no converter, regulator or controller noun anywhere."""
    if _DRIVER_TEXT.search(text) and not _CONVERTER_NOUN.search(text):
        return None, None
    other = _NOT_POWER.search(text)
    found = [(match.start(), rank, kind, match.group(0))
             for rank, (kind, pattern) in enumerate(_TOPOLOGY_RE)
             if (match := pattern.search(text))]
    categories = [(kind, match) for kind, pattern in _CATEGORY_RE
                  if (match := pattern.search(text))]
    for kind, match in categories:
        if other and other.start() < match.start():
            continue
        if kind in _FEATURE_CATEGORIES and (
                any(start < match.start() for start, *_ in found)
                or any(match_other.start() < match.start() for kind_other, match_other
                       in categories if kind_other not in _FEATURE_CATEGORIES)):
            continue
        return kind, f"text: {match.group(0)}"
    if not found:
        return None, None
    start, _, kind, word = min(found)
    if other and other.start() < start:
        return None, None
    return kind, f"text: {word}"


def classify_mpn(text: str) -> tuple[str | None, str | None]:
    for kind, pattern in _MPN_RE:
        match = pattern.search(text)
        if match:
            return kind, f"part-number family: {match.group(0)}"
    return None, None


# ---------- the design ----------

@dataclass
class _Output:
    net: str
    via: str
    pins: list[str] = field(default_factory=list)
    channel: str | None = None
    kind: str | None = None  # a PMIC channel's own type


@dataclass
class _Converter:
    ref: str
    kind: str
    basis: list[str]
    pins: dict[str, list[tuple[str, str, str | None]]]  # kind -> (pin, name, net)
    fitted: bool = True
    module: bool = False
    controller: bool = False
    isolated: bool = False
    inputs: list[str] = field(default_factory=list)
    outputs: list[_Output] = field(default_factory=list)
    switch_nodes: set[str] = field(default_factory=set)
    notes: list[str] = field(default_factory=list)

    @property
    def category(self) -> str:
        if self.kind in ("reference", "supervisor"):
            return self.kind
        return "switch" if self.kind in SWITCH_KINDS else "converter"

    @property
    def switching(self) -> bool:
        return self.kind in SWITCHING_KINDS or (
            self.kind == "pmic" and any(out.kind in SWITCHING_KINDS for out in self.outputs))

    def nets(self, kind: str) -> list[str]:
        return list(dict.fromkeys(net for _, _, net in self.pins.get(kind, ()) if net))


@dataclass
class _Edge:
    src: str
    dst: str
    part: str
    kind: str
    directed: bool
    value: str | None = None
    converter: bool = False
    source_net: str | None = None  # a FET's source (a BJT's emitter) side, when its pins say

    def other(self, net: str) -> str:
        return self.dst if net == self.src else self.src


@dataclass
class _Source:
    kind: str  # connector, battery, converter, internal, driven, reference
    part: str
    net: str
    pins: list[str] = field(default_factory=list)
    detail: str | None = None
    role: str = "input"  # input or outlet (connector pins on a rail made on the board)
    exposure: str | None = None
    exposure_basis: list[str] = field(default_factory=list)
    power_only: bool = False
    usb: bool = False
    folded_into: str | None = None  # another input connector on the same net, as likely


class _Tree:
    """Everything the builder learns; data() turns it into plain dicts."""

    def __init__(self, netlist: dict, plan, project_params: dict | None):
        self.ctx = build_context(netlist, plan, project_params)
        self.design = self.ctx.design
        self.variant = getattr(plan, "name", None) or "[No Variations]"
        self.components = netlist.get("components") or {}
        self.parts = {part.designator: part
                      for part in build_parts(netlist, plan, project_params)[0]}
        self.grounds = self.design.grounds
        self.converters: dict[str, _Converter] = {}
        self.not_fitted: dict[str, _Converter] = {}
        self.references: dict[str, _Converter] = {}
        self.supervisors: dict[str, _Converter] = {}
        self.rails: set[str] = set()
        self.intermediate: set[str] = set()
        self.edges: list[_Edge] = []
        self.sources: list[_Source] = []
        self.supplied: set[str] = set()
        self.depth: dict[str, int] = {}
        self.loads: dict[str, list[tuple[str, list[str]]]] = defaultdict(list)
        self.connectors_on: dict[str, list[str]] = defaultdict(list)
        self.test_points: dict[str, list[str]] = defaultdict(list)
        self.resistors: dict[str, list[tuple[str, str, float | None]]] = defaultdict(list)
        self.diodes: dict[str, list[tuple[str, str, str | None]]] = defaultdict(list)
        self.capacitors: dict[str, list[tuple[str, float | None]]] = defaultdict(list)
        self.rails = {net for net in self.design.rails if net not in self.grounds}
        self._nodes: set[str] = set()
        self._nodes_key: tuple = ()

    # ----- small reads -----

    def fitted(self, ref: str) -> bool:
        return self.ctx.fitted.get(ref, True)

    def kind(self, ref: str) -> str | None:
        return self.ctx.kind.get(ref)

    def text(self, ref: str) -> str:
        return self.ctx.text.get(ref, "")

    def label(self, ref: str) -> str:
        return (self.ctx.comment.get(ref) or "")[:60]

    def pin_names(self, ref: str) -> dict[str, str]:
        meta = self.components.get(ref) or {}
        names = dict(meta.get("pin_names") or {})
        for pin in self.design.part_pins.get(ref) or {}:
            names.setdefault(pin, self.design.pin_name(ref, pin))
        return names

    def members(self, net: str) -> list[tuple[str, str]]:
        return self.design.nets.get(net, [])

    def caps(self, net: str) -> list[tuple[str, float | None]]:
        return list(self.capacitors.get(net, ()))

    def nodes(self) -> set[str]:
        """Rails and power-path nets (cached until either set is replaced or grows)."""
        key = (id(self.rails), len(self.rails), id(self.intermediate), len(self.intermediate))
        if key != self._nodes_key:
            self._nodes, self._nodes_key = self.rails | self.intermediate, key
        return self._nodes


# ---------- building ----------

def _resistor_graph(tree: _Tree) -> None:
    design = tree.design
    for ref, connected in design.part_pins.items():
        if ref not in design.resistors or not tree.fitted(ref):
            continue
        meta = tree.components.get(ref) or {}
        part = tree.parts.get(ref)
        ohms = resistance(part.value if part else meta.get("value"),
                          part.comment if part else None,
                          (meta.get("params") or {}).get("resistance"),
                          part.description if part else meta.get("description"))
        for _, net_a, _, net_b, _ in resistor_sections(meta, connected):
            if net_a is None or net_b is None or net_a == net_b:
                continue
            tree.resistors[net_a].append((ref, net_b, ohms))
            tree.resistors[net_b].append((ref, net_a, ohms))


def _capacitor_graph(tree: _Tree) -> None:
    """Fitted capacitors (any prefix check_decoupling reads as one: C, CE, EC with a
    capacitor's text) from a net to ground, arrays and feed-throughs included."""
    for ref, kind in tree.ctx.kind.items():
        if kind != "capacitor" or not tree.fitted(ref):
            continue
        nets = set((tree.design.part_pins.get(ref) or {}).values())
        if not nets & tree.grounds:
            continue
        meta = tree.components.get(ref) or {}
        part = tree.parts.get(ref)
        farads = capacitance(part.value if part else meta.get("value"),
                             (meta.get("params") or {}).get("capacitance"),
                             part.comment if part else None,
                             part.description if part else meta.get("description"))
        for net in sorted(nets - tree.grounds, key=natural_key):
            tree.capacitors[net].append((ref, farads))


def _diode_graph(tree: _Tree) -> None:
    """Two-pin diodes: net -> (part, other net, 'anode' or 'cathode' for this net's side,
    None when the pins are not named)."""
    for ref, kind in tree.ctx.kind.items():
        if kind not in ("diode", "zener", "esd") or not tree.fitted(ref):
            continue
        pins = tree.design.part_pins.get(ref) or {}
        if len(pins) != 2 or len(set(pins.values())) != 2:
            continue
        (pin_a, net_a), (pin_b, net_b) = pins.items()
        side_a = diode_side(tree.design.pin_name(ref, pin_a))
        side_b = diode_side(tree.design.pin_name(ref, pin_b))
        if side_a and side_b and side_a == side_b:
            side_a = side_b = None
        tree.diodes[net_a].append((ref, net_b, side_a or _opposite(side_b)))
        tree.diodes[net_b].append((ref, net_a, side_b or _opposite(side_a)))


def diode_side(name: str | None) -> str | None:
    clean = _clean(name)
    if _DIODE_ANODE.fullmatch(clean):
        return "anode"
    if _DIODE_CATHODE.fullmatch(clean):
        return "cathode"
    return None


def _opposite(side: str | None) -> str | None:
    return {"anode": "cathode", "cathode": "anode"}.get(side or "")


def _candidate(tree: _Tree, ref: str) -> bool:
    kind = tree.kind(ref)
    prefix = designator_prefix(ref)
    if kind in ("ic", "other", "connector"):
        return True
    return kind == "passive" and prefix in _MODULE_PREFIXES


# Words that make a part with a connector's designator (J, P, X, CN) a regulator module.
_MODULE_KINDS = frozenset({"ldo", "regulator", "charge_pump", "pmic"}) | SWITCHING_KINDS


def _drop_false_pg(tree: _Tree, ref: str, pins: dict) -> None:
    """A bare 'PG' pin is not power-good when the part also has POK, PGOOD or PWRGD, or
    when a capacitor joins it to another of the part's own pins (a P-gate bias or
    bootstrap pin)."""
    bare = [item for item in pins.get("pg", ()) if _clean(item[1]) == "PG"]
    if not bare:
        return
    named = [item for item in pins.get("pg", ())
             if re.match(r"N?(?:POK|PGOOD|PWRGD|PWROK|POWERGOOD|PWR_?GOOD|PGD)", _clean(item[1]))]
    own = set((tree.design.part_pins.get(ref) or {}).values()) - tree.grounds
    drop = []
    for item in bare:
        net = item[2]
        if named:
            drop.append(item)
            continue
        for other, _ in tree.members(net) if net else ():
            if tree.kind(other) != "capacitor":
                continue
            far = set((tree.design.part_pins.get(other) or {}).values()) - {net}
            if far & own:
                drop.append(item)
                break
    pins["pg"] = [item for item in pins["pg"] if item not in drop]
    if not pins["pg"]:
        del pins["pg"]


def _classify_parts(tree: _Tree) -> None:
    for ref in sorted(tree.ctx.kind, key=natural_key):
        if not _candidate(tree, ref):
            continue
        text = tree.text(ref)
        names = tree.pin_names(ref)
        connected = tree.design.part_pins.get(ref) or {}
        pins: dict[str, list[tuple[str, str, str | None]]] = defaultdict(list)
        for pin in sorted(set(names) | set(connected), key=natural_key):
            name = names.get(pin) or ""
            kind = pin_kind(name)
            if kind:
                pins[kind].append((pin, name, connected.get(pin)))
        _drop_false_pg(tree, ref, pins)
        kind, basis = classify_text(text)
        if tree.kind(ref) == "connector":
            # A regulator module on a connector's designator: its words name a regulator
            # and its pins an input and an output.
            if kind in _MODULE_KINDS and pins.get("in") and pins.get("out"):
                basis += " (a module with a connector's designator)"
            else:
                continue
        if kind is None:
            kind, basis = classify_mpn(text)
            if kind is not None and _NOT_POWER.search(text):
                kind = None
        if kind is None and not _NOT_POWER.search(text) and not PROCESSOR_TEXT.search(text):
            has_in = bool(pins.get("in"))
            has_out = bool(pins.get("out") or pins.get("sw"))
            control = any(pins.get(k) for k in ("fb", "en", "pg", "sw", "bst", "ss", "comp"))
            if has_in and has_out and control and len(names) <= 64:
                # A DC/DC module whose words name no topology still switches.
                kind = "switching" if pins.get("sw") or _MODULE_TEXT.search(text) \
                    else "linear"
                shown = sorted({_clean(name) for k in ("in", "out", "sw", "fb", "en", "pg")
                                for _, name, _ in pins.get(k, ())})
                basis = f"pin names: {', '.join(shown[:6])}" + (
                    " (a linear regulator or a load switch)" if kind == "linear" else "")
        if kind is None:
            continue
        converter = _Converter(ref, kind, [basis], dict(pins), fitted=tree.fitted(ref))
        prefix = designator_prefix(ref)
        if _MODULE_TEXT.search(text) or tree.kind(ref) == "connector" or (
                prefix in _MODULE_PREFIXES and kind in ("switching", "regulator", "buck",
                                                        "boost")):
            converter.module = True
        if kind == "regulator":
            # A module named only 'regulator' is a DC/DC; a part with a SW pin switches.
            switching = bool(pins.get("sw")) or converter.module
            converter.kind = "switching" if switching else "linear"
            converter.basis.append("SW pin" if pins.get("sw") else "a module" if switching
                                   else "no SW pin: linear")
        if converter.kind in ("switching", "buck", "boost", "buck_boost", "sepic",
                              "flyback", "inverting") and (
                _CONTROLLER_TEXT.search(text) or pins.get("gate")):
            converter.controller = True
        converter.isolated = bool(_ISOLATED_TEXT.search(text))
        if kind == "reference":
            tree.references[ref] = converter
        elif kind == "supervisor":
            tree.supervisors[ref] = converter
        elif converter.fitted:
            tree.converters[ref] = converter
        else:
            tree.not_fitted[ref] = converter


def inductors_on(tree: _Tree, net: str, ferrite: bool = False) -> list[tuple[str, str]]:
    """Fitted two-net inductors (ferrites when ferrite) with a pin on net: (part, the other
    net)."""
    out = []
    for ref, _ in tree.members(net):
        if tree.kind(ref) != "inductive" or not tree.fitted(ref):
            continue
        is_ferrite = bool(FERRITE_TEXT.search(tree.text(ref))) or designator_prefix(ref) in (
            "FB", "BEAD", "FE")
        if is_ferrite != ferrite:
            continue
        pins = tree.design.part_pins.get(ref) or {}
        others = [other for other in dict.fromkeys(pins.values()) if other != net]
        if len(others) == 1:
            out.append((ref, others[0]))
    return out


def _transformer_outputs(tree: _Tree, net: str, skip: set[str]) -> list[tuple[str, str]]:
    """A flyback's outputs: from a transformer on the switch node, through a diode on a
    secondary winding, to a rail."""
    out = []
    for ref, _ in tree.members(net):
        pins = tree.design.part_pins.get(ref) or {}
        if len(pins) < 4 or not (tree.kind(ref) == "transformer" or re.search(
                r"transformer|flyback|coupled", tree.text(ref), re.I)):
            continue
        for other in set(pins.values()) - {net} - skip - tree.grounds:
            for diode, far, side in tree.diodes.get(other, ()):
                if far not in tree.grounds and far != net and side != "cathode":
                    out.append((f"{ref}, {diode}", far))
    return out


def gate_fets(tree: _Tree, conv: _Converter) -> dict[str, set[str]]:
    """The MOSFETs whose gate the part's gate pins drive: each one's drain and source
    nets, not ground."""
    fets: dict[str, set[str]] = {}
    for _, _, net in conv.pins.get("gate", ()):
        if not net:
            continue
        for ref, pin in tree.members(net):
            if tree.kind(ref) != "transistor" or not tree.fitted(ref):
                continue
            if first_function(tree.design.pin_name(ref, pin))[:1] != "G":
                continue
            for other_pin, other in (tree.design.part_pins.get(ref) or {}).items():
                name = first_function(tree.design.pin_name(ref, other_pin))
                if name[:1] in ("S", "D") and other not in tree.grounds:
                    fets.setdefault(ref, set()).add(other)
    return fets


def _pass_fet_outputs(tree: _Tree, conv: _Converter, fets: dict[str, set[str]]) -> list:
    """A pass controller (load switch, eFuse, hot-swap, ideal diode, charger, linear) with
    no OUT pin: the far side of the MOSFETs its gate drives (past a sense resistor on the
    input, not the common node of back-to-back FETs)."""
    inputs = _output_group(tree, conv.inputs) if conv.inputs else set()
    counts = Counter(net for nets in fets.values() for net in nets)
    out = []
    for ref, nets in sorted(fets.items(), key=lambda item: natural_key(item[0])):
        for net in sorted(nets, key=natural_key):
            if net not in inputs and counts[net] == 1:
                out.append((ref, net))
    return out


def _trace_io(tree: _Tree, conv: _Converter) -> None:
    grounds = tree.grounds
    if conv.kind == "charger":
        ins = [(pin, name, net) for kinds in conv.pins.values() for pin, name, net in kinds
               if net and net not in grounds and _CHARGER_IN.fullmatch(_clean(name))]
        outs = [(pin, name, net) for kinds in conv.pins.values() for pin, name, net in kinds
                if net and net not in grounds and _CHARGER_OUT.fullmatch(_clean(name))
                and not _CHARGER_IN.fullmatch(_clean(name))]
    else:
        ins = [item for item in conv.pins.get("in", ()) if item[2] and item[2] not in grounds]
        outs = [item for item in conv.pins.get("out", ())
                if item[2] and item[2] not in grounds]
    if not ins:
        ins = [item for item in conv.pins.get("bias", ())
               if item[2] and item[2] not in grounds and _WEAK_IN.fullmatch(_clean(item[1]))
               and item[2] not in {net for _, _, net in outs}]
        if ins:
            conv.basis.append("input from a supply pin (no VIN/IN pin)")
    counts = Counter(net for _, _, net in ins)
    conv.inputs = sorted(counts, key=lambda net: (-counts[net], natural_key(net)))
    by_net: dict[str, _Output] = {}
    for pin, name, net in outs:
        if net in conv.inputs:
            continue
        entry = by_net.setdefault(net, _Output(net, f"pin {_clean(name)}",
                                               channel=_channel(name)))
        entry.pins.append(pin)
    sw_nets = conv.nets("sw")
    conv.switch_nodes = set(sw_nets)
    fets = gate_fets(tree, conv) if conv.pins.get("gate") else {}
    if conv.kind in SWITCHING_KINDS or conv.kind == "pmic":
        conv.switch_nodes |= {net for nets in fets.values() for net in nets
                              if net not in conv.inputs}
    elif not by_net:
        # A load switch, eFuse, hot-swap, ideal-diode, charger or linear controller: the
        # MOSFET passes the supply, so its far side is the output, not a switch node.
        for fet, net in _pass_fet_outputs(tree, conv, fets):
            by_net.setdefault(net, _Output(net, f"through {fet} (the MOSFET its gate "
                                                "drives)"))
    for net in sorted(conv.switch_nodes, key=natural_key):
        if net in grounds:
            continue
        channel = next((_channel(name) for _, name, sw in conv.pins.get("sw", ())
                        if sw == net), None)
        for inductor, far in inductors_on(tree, net):
            if far in grounds or far in conv.switch_nodes:
                continue
            if far in conv.inputs:
                # A boost: the inductor comes from the input; the output is past a diode
                # (or the part's own VOUT pin, already read).
                for diode, other, side in tree.diodes.get(net, ()):
                    if other not in grounds and other not in conv.inputs and side != "cathode":
                        by_net.setdefault(other, _Output(
                            other, f"switch node through {inductor} from the input and "
                                   f"{diode}", channel=channel))
                continue
            if far not in by_net:
                by_net[far] = _Output(far, f"switch node through {inductor}", channel=channel)
        for parts, far in _transformer_outputs(tree, net, set(conv.inputs)):
            by_net.setdefault(far, _Output(far, f"switch node through {parts}",
                                           channel=channel))
    if not by_net and conv.category != "reference" and conv.category != "supervisor":
        # A DC/DC module may name its output pins for their voltage (+15V, -15V, 5V).
        for pin, net in (tree.design.part_pins.get(conv.ref) or {}).items():
            name = first_function(tree.design.pin_name(conv.ref, pin))
            if _VOLTAGE_PIN.fullmatch(name) and net not in grounds and net not in conv.inputs:
                by_net.setdefault(net, _Output(net, f"pin {name} (named for its voltage)",
                                               channel=None)).pins.append(pin)
    conv.outputs = sorted(by_net.values(), key=lambda out: natural_key(out.net))
    if conv.kind == "pmic":
        # A PMIC's switcher output is the far side of its inductor; a pin output is an LDO.
        for out in conv.outputs:
            out.kind = "buck" if out.via.startswith("switch node") else "ldo"
    if not conv.inputs and conv.kind != "pmic":
        _guess_by_voltage(tree, conv)


def _guess_by_voltage(tree: _Tree, conv: _Converter) -> None:
    """Pins not named: of the part's nets that are rails with a voltage in their name,
    the higher is the input and the lower the output (a three-terminal regulator)."""
    nets = [net for net in dict.fromkeys((tree.design.part_pins.get(conv.ref) or {}).values())
            if net not in tree.grounds and net not in {out.net for out in conv.outputs}]
    named = [(rail_voltage(net), net) for net in nets
             if net in tree.design.rails and rail_voltage(net)]
    if conv.outputs:
        out_volts = [rail_voltage(out.net) for out in conv.outputs if rail_voltage(out.net)]
        higher = [(volts, net) for volts, net in named
                  if not out_volts or volts > max(out_volts)]
        if higher:
            conv.inputs = [max(higher)[1]]
            conv.basis.append("input guessed: the higher-voltage rail on its pins")
        return
    if len(named) == 2 and conv.kind in ("ldo", "linear", "buck", "switching"):
        (low_v, low), (high_v, high) = sorted(named)
        if low_v < high_v:
            conv.inputs = [high]
            conv.outputs = [_Output(low, "guessed: the lower-voltage rail on its pins")]
            conv.basis.append("pins not named: input and output guessed from rail voltages")


def _refine_kind(conv: _Converter) -> None:
    """A 'switching' or module part's topology from its rails' voltages."""
    if conv.kind not in ("switching",) or not conv.inputs or not conv.outputs:
        return
    vin = rail_voltage(conv.inputs[0])
    vouts = [rail_voltage(out.net) for out in conv.outputs]
    if any(out.net.strip().startswith("-") for out in conv.outputs):
        conv.kind = "inverting"
    elif vin and vouts and all(v is not None and v < vin for v in vouts):
        conv.kind = "buck"
    elif vin and vouts and all(v is not None and v > vin for v in vouts):
        conv.kind = "boost"
    else:
        return
    conv.basis.append(f"topology from the rail voltages ({conv.kind})")


def _expand_rails(tree: _Tree) -> None:
    """Rails: check_decoupling's (and supply pins of any part), converter inputs and
    outputs and power-source nets; then unnamed nets that only carry power between two
    of them through series parts (a fuse, a diode, a MOSFET channel)."""
    design = tree.design
    rails = set(tree.rails)
    for conv in tree.converters.values():
        rails.update(net for net in conv.inputs if net not in tree.grounds)
        rails.update(out.net for out in conv.outputs if out.net not in tree.grounds)
    switch_nodes = {net for conv in tree.converters.values() for net in conv.switch_nodes}
    rails -= switch_nodes
    tree.rails = rails
    candidates: set[str] = set()
    for net, members in design.nets.items():
        if net in rails or net in tree.grounds or net in switch_nodes:
            continue
        kinds = net_kind(net)
        if kinds["status"] or kinds["signal"]:
            continue
        ok = True
        for ref, pin in members:
            kind = tree.kind(ref)
            if kind == "transistor":
                if not _SERIES_PINS.fullmatch(first_function(design.pin_name(ref, pin))):
                    ok = False
                    break
            elif kind not in _POWER_PATH_KINDS:
                ok = False
                break
        if ok:
            candidates.add(net)
    links: dict[str, set[str]] = defaultdict(set)
    for edge in _series_edges(tree, rails | candidates):
        if edge.kind == "resistor" and not _low_ohms(edge.value):
            continue  # a pull-up or a divider, not a power path
        links[edge.src].add(edge.dst)
        links[edge.dst].add(edge.src)
    # Prune dead ends (a switch's gate-control branch): a net that is not an entry point
    # carries power only when it has two neighbours.
    anchors = {net for net in candidates if _connector_power_net(tree, {net})}
    pruned = True
    while pruned:
        pruned = False
        for net in sorted(candidates - anchors, key=natural_key):
            if len({other for other in links.get(net, ()) if other in rails
                    or other in candidates}) < 2:
                candidates.discard(net)
                pruned = True
    seen: set[str] = set()
    for start in sorted(candidates, key=natural_key):
        if start in seen:
            continue
        component, queue, touching = {start}, [start], set()
        while queue:
            net = queue.pop()
            for other in links.get(net, ()):
                if other in rails:
                    touching.add(other)
                elif other in candidates and other not in component:
                    component.add(other)
                    queue.append(other)
        seen |= component
        if len(touching) >= 2 or (touching and _connector_power_net(tree, component)):
            tree.intermediate |= component


def _low_ohms(value: str | None) -> bool:
    ohms = resistance(value) if value else None
    return ohms is not None and ohms <= FILTER_OHMS


def _connector_power_net(tree: _Tree, nets: set[str]) -> bool:
    """A connector, battery or power jack pin on one of the nets: the net may bring
    power in from outside."""
    for net in nets:
        for ref, _ in tree.members(net):
            if ref not in tree.converters and (tree.kind(ref) == "connector"
                                               or _is_battery(tree, ref)):
                return True
    return False


def _is_battery(tree: _Tree, ref: str) -> bool:
    prefix = designator_prefix(ref)
    if tree.kind(ref) in ("ic", "resistor", "capacitor", "inductive", "diode", "esd",
                          "transistor"):
        return False
    return prefix in ("BT", "BAT", "BATT") or bool(_BATTERY_TEXT.search(tree.text(ref)))


def _series_edges(tree: _Tree, nodes: set[str]) -> list[_Edge]:
    """Fitted series parts between two nodes (not grounds); a resistor above
    FILTER_OHMS only into reference inputs."""
    edges: list[_Edge] = []
    seen: set[tuple] = set()
    design = tree.design

    def add(src: str, dst: str, ref: str, kind: str, directed: bool = False,
            value: str | None = None, source_net: str | None = None) -> None:
        if src == dst or src not in nodes or dst not in nodes:
            return
        key = (ref, *sorted((src, dst)))
        if key in seen:
            return
        seen.add(key)
        edges.append(_Edge(src, dst, ref, kind, directed, value, source_net=source_net))

    for net in sorted(nodes, key=natural_key):
        for ref, other, ohms in tree.resistors.get(net, ()):
            if ohms is not None and ohms <= LINK_OHMS:
                add(net, other, ref, "0_ohm" if ohms == 0 else "shunt",
                    value=ohms_text(ohms) if ohms else None)
            elif ohms is None or ohms <= FILTER_OHMS:
                add(net, other, ref, "resistor", value=ohms_text(ohms))
            elif _reference_only(tree, other) and not _reference_only(tree, net):
                # A divider setting a reference input (a DDR VREF, an FPGA's VREF pins).
                add(net, other, ref, "reference_feed", True, value=ohms_text(ohms))
            # Otherwise a larger resistor is a pull-up, bleeder or sense divider, not a
            # supply path (see _high_value_feeds).
        for ref, other, side in tree.diodes.get(net, ()):
            if tree.kind(ref) == "esd":
                continue
            kind = "schottky" if _SCHOTTKY_TEXT.search(tree.text(ref)) else "diode"
            if side == "anode":
                add(net, other, ref, kind, True)
            elif side == "cathode":
                add(other, net, ref, kind, True)
            else:
                add(net, other, ref, kind)
    for ref, kind in tree.ctx.kind.items():
        if not tree.fitted(ref) or kind in ("resistor", "diode", "zener", "esd", "capacitor",
                                            "connector", "ic", "other"):
            continue
        pins = design.part_pins.get(ref) or {}
        live = [net for net in dict.fromkeys(pins.values())
                if net in nodes and net not in tree.grounds]
        text = tree.text(ref)
        if kind == "inductive" and len(pins) == 2 and len(live) == 2:
            ferrite = FERRITE_TEXT.search(text) or designator_prefix(ref) in ("FB", "BEAD",
                                                                               "FE")
            add(live[0], live[1], ref, "ferrite" if ferrite else "inductor")
        elif kind == "choke" and len(live) == 2:
            add(live[0], live[1], ref, "common_mode_choke")
        elif kind == "transistor":
            _transistor_edges(tree, ref, pins, nodes, add)
        elif len(live) == 2 and kind in ("fuse", "passive", "switch", "relay"):
            if kind == "fuse" or _FUSE_TEXT.search(text):
                add(live[0], live[1], ref, "fuse")
            elif _NTC_TEXT.search(text) or re.search(r"thermistor", text, re.I):
                add(live[0], live[1], ref, "ntc")
            elif kind in ("switch", "relay"):
                add(live[0], live[1], ref, kind)
            elif designator_prefix(ref) in ("JP", "SJ", "W"):
                add(live[0], live[1], ref, "jumper")
    return edges


def _reference_only(tree: _Tree, net: str) -> bool:
    """The net's loads are all reference inputs (VREF pins), set by a divider."""
    loads = tree.loads.get(net)
    return bool(loads) and all(pin_role(tree.design.pin_name(ref, pin)) == "ref"
                               for ref, pins in loads for pin in pins)


def _transistor_edges(tree: _Tree, ref: str, pins: dict[str, str], nodes: set[str],
                      add) -> None:
    text = tree.text(ref)
    bjt = bool(_BJT_TEXT.search(text))
    if _P_CHANNEL.search(text):
        kind = "pnp" if bjt else "p_fet"
    elif _N_CHANNEL.search(text):
        kind = "npn" if bjt else "n_fet"
    else:
        kind = "transistor"
    channels: dict[str | None, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for pin, net in pins.items():
        name = first_function(tree.design.pin_name(ref, pin))
        if not name or name.isdigit():
            continue
        role = name[:1]
        if role in ("S", "D", "C", "E"):
            channels[_channel(name)][role].add(net)
    for roles in channels.values():
        for first, second in (("S", "D"), ("E", "C")):
            for a in roles.get(first, ()):
                for b in roles.get(second, ()):
                    if a in nodes and b in nodes and a not in tree.grounds and \
                            b not in tree.grounds:
                        add(a, b, ref, kind, source_net=a)


def _converter_edges(tree: _Tree) -> list[_Edge]:
    edges = []
    for conv in tree.converters.values():
        for out in conv.outputs:
            src = None
            if out.channel:
                src = next((net for _, name, net in conv.pins.get("in", ())
                            if net and _channel(name) == out.channel
                            and net not in tree.grounds), None)
            src = src or (conv.inputs[0] if conv.inputs else None)
            if src and src != out.net:
                edges.append(_Edge(src, out.net, conv.ref, out.kind or conv.kind, True,
                                   converter=True))
    return edges


def _find_sources(tree: _Tree) -> None:
    design = tree.design
    nodes = tree.nodes()
    connectors = [ref for ref, kind in tree.ctx.kind.items()
                  if kind == "connector" and tree.fitted(ref) and ref not in tree.converters]
    for ref in sorted(connectors, key=natural_key):
        if not any(net in nodes for net in (design.part_pins.get(ref) or {}).values()):
            continue
        connector = audit_connector(tree.ctx, ref)
        power = [pin for pin in connector.pins if pin.cls == "power" and pin.net]
        power_only = bool(power) and all(
            pin.cls in ("power", "ground", "shield", "unconnected") for pin in connector.pins)
        usb = bool(_USB_TEXT.search(tree.text(ref)))
        by_net: dict[str, list[str]] = defaultdict(list)
        for pin in power:
            by_net[pin.net].append(pin.pin)
        for net, pins in by_net.items():
            if net not in tree.intermediate:
                tree.rails.add(net)
            vbus = any(token.startswith("VBUS") for token in net_tokens(net))
            tree.sources.append(_Source(
                "connector", ref, net, pins, exposure=connector.exposure,
                exposure_basis=connector.basis, power_only=power_only, usb=usb or vbus,
                detail="USB VBUS" if vbus else None))
        for pin in connector.pins:
            if pin.net and pin.net in tree.nodes():
                tree.connectors_on[pin.net].append(ref)
    for ref in sorted(tree.ctx.kind, key=natural_key):
        if not tree.fitted(ref) or not _is_battery(tree, ref) or ref in tree.converters:
            continue
        pins = design.part_pins.get(ref) or {}
        by_net = defaultdict(list)
        for pin, net in sorted(pins.items(), key=lambda item: natural_key(item[0])):
            if net in tree.grounds:
                continue
            # A pack or connector with more pins also carries a thermistor or ID line:
            # only its positive pins (named so, or on a rail) are the source.
            if len(set(pins.values()) - tree.grounds) > 1 and not (
                    _BATTERY_PLUS.fullmatch(first_function(design.pin_name(ref, pin)))
                    or net in tree.rails or net_kind(net)["power"]):
                continue
            by_net[net].append(pin)
        for net, net_pins in by_net.items():
            if net not in tree.intermediate:
                tree.rails.add(net)
            tree.sources.append(_Source("battery", ref, net, net_pins,
                                        detail=tree.label(ref) or None))
    nodes = tree.nodes()
    for conv in tree.converters.values():
        for out in conv.outputs:
            tree.sources.append(_Source("converter", conv.ref, out.net, out.pins,
                                        detail=conv.kind))
        # A bias pin alone on its net with passives (VDDB, INTVCC, VDD33, an AVINO feeding
        # its own AVIN): the part's own internal regulator output.
        for pin, name, net in conv.pins.get("bias", ()):
            if net and net in nodes and net not in conv.inputs[:1] and all(
                    ref == conv.ref or tree.kind(ref) in _PASSIVE_KINDS
                    for ref, _ in tree.members(net)):
                tree.sources.append(_Source("internal", conv.ref, net, [pin],
                                            detail=f"{_clean(name)} (its own bias "
                                                   "regulator)"))
    for ref, conv in tree.references.items():
        if not conv.fitted:
            continue
        for out in conv.outputs:
            if out.net in nodes:
                tree.sources.append(_Source("reference", ref, out.net, out.pins,
                                            detail="a voltage reference's output"))
    power_parts = set(tree.converters) | set(tree.references) | set(tree.supervisors)
    for ref, pins in sorted(design.part_pins.items(), key=lambda item: natural_key(item[0])):
        kind = tree.kind(ref)
        if kind not in ("ic", "other") or not tree.fitted(ref) or ref in power_parts:
            continue
        # One source per part, net and kind: the pins grouped (VDD_CORE on two pins).
        found: dict[tuple[str, str], _Source] = {}

        def note(source_kind: str, net: str, pin: str, detail: str) -> None:
            if (source_kind, net) in found:
                found[(source_kind, net)].pins.append(pin)
            else:
                found[(source_kind, net)] = _Source(source_kind, ref, net, [pin],
                                                    detail=detail)

        for pin, net in sorted(pins.items(), key=lambda item: natural_key(item[0])):
            name = design.pin_name(ref, pin)
            clean = _clean(name)
            electrical = design.pin_type(ref, pin)
            if net in nodes and electrical != "Input" and (
                    pin_role(name) == "cap" or _INTERNAL_OUT.fullmatch(clean)):
                note("internal", net, pin, f"{clean} (an internal regulator)")
            elif net in nodes and electrical == "Output" and pin_role(name) not in (
                    "supply", "ground", "ref", "cap", "in"):
                note("driven", net, pin, f"{clean or pin} (an output pin)")
            elif _INTERNAL_SW.fullmatch(clean) and net not in tree.grounds:
                for inductor, far in inductors_on(tree, net):
                    if far in nodes:
                        note("internal", far, pin,
                             f"{clean} through {inductor} (an internal SMPS)")
        tree.sources += found.values()
    for net in nodes:
        for ref, _ in tree.members(net):
            if designator_prefix(ref) in ("TP", "T_P", "TEST") and tree.fitted(ref):
                tree.test_points[net].append(ref)


def _adjacency(tree: _Tree) -> dict[str, list[tuple[str, _Edge]]]:
    adjacency: dict[str, list[tuple[str, _Edge]]] = defaultdict(list)
    for edge in tree.edges:
        adjacency[edge.src].append((edge.dst, edge))
        if not edge.directed:
            adjacency[edge.dst].append((edge.src, edge))
    return adjacency


def _reach(adjacency, starts) -> dict[str, int]:
    depth = {net: 0 for net in starts}
    queue = deque(starts)
    while queue:
        net = queue.popleft()
        for other, _ in adjacency.get(net, ()):
            if other not in depth:
                depth[other] = depth[net] + 1
                queue.append(other)
    return depth


def _supply(tree: _Tree) -> None:
    adjacency = _adjacency(tree)
    tree.supplied = set(_reach(adjacency, list(dict.fromkeys(
        source.net for source in tree.sources))))
    _connector_roles(tree, adjacency)
    # Depth (for the edges' direction) counts from the power inputs, not from connectors
    # that pass power on.
    outside = [source.net for source in tree.sources
               if (source.kind == "connector" and source.role == "input")
               or source.kind in ("battery", "internal", "driven", "reference")]
    depth = _reach(adjacency, list(dict.fromkeys(outside)))
    # Converters whose input nothing reaches still make their output: seed those too.
    rest = [source.net for source in tree.sources
            if source.kind in ("converter", "connector") and source.net not in depth]
    if rest:
        later = _reach(adjacency, list(dict.fromkeys(rest)))
        base = max(depth.values(), default=0) + 1
        for net, value in later.items():
            depth.setdefault(net, base + value)
    tree.depth = depth


def _connector_roles(tree: _Tree, adjacency) -> None:
    """A connector power pin on a rail made on the board is an outlet; so is one reached
    from a likelier power input."""
    onboard = [source.net for source in tree.sources if source.kind != "connector"]
    from_board = set(_reach(adjacency, list(dict.fromkeys(onboard))))
    connectors = [source for source in tree.sources if source.kind == "connector"]
    for source in connectors:
        if source.net in from_board:
            source.role = "outlet"
    # A connector reached from a likelier power input (a power jack before a pin header,
    # an external connector before a board-to-board one) passes that power on.
    for source in connectors:
        if source.role == "outlet":
            continue
        stronger = [other.net for other in connectors if other.role == "input"
                    and _input_rank(other) < _input_rank(source) and other.net != source.net]
        if stronger and source.net in _reach(adjacency, list(dict.fromkeys(stronger))):
            source.role = "outlet"
    # Two input connectors on one net: the likelier is the input and a less likely one
    # passes its power on; as likely ones are folded into one input row.
    by_net: dict[str, list[_Source]] = defaultdict(list)
    for source in connectors:
        if source.role == "input":
            by_net[source.net].append(source)
    for sources in by_net.values():
        sources.sort(key=lambda source: (_input_rank(source), natural_key(source.part)))
        first = sources[0]
        for source in sources[1:]:
            if source.part == first.part:
                continue
            if _input_rank(source) > _input_rank(first):
                source.role = "outlet"
            else:
                source.folded_into = first.part


def _input_rank(source: _Source) -> int:
    """How likely a connector power pin is the board's power input (lower is likelier)."""
    exposure = {"external": 0, "unknown": 2, "internal": 4}.get(source.exposure or "", 4)
    return exposure + (0 if source.power_only else 1)


def _find_loads(tree: _Tree) -> None:
    design = tree.design
    nodes = tree.nodes()
    for ref, pins in design.part_pins.items():
        kind = tree.kind(ref)
        conv = tree.converters.get(ref)
        if not tree.fitted(ref) or (conv is None and (kind in _PASSIVE_KINDS
                                                      or kind == "transistor")):
            continue
        names = {pin: design.pin_name(ref, pin) for pin in pins}
        unnamed = all(pin_role(name) == "none" for name in names.values())
        on: dict[str, list[str]] = defaultdict(list)
        for pin, net in pins.items():
            if net not in nodes:
                continue
            name = names[pin]
            role = pin_role(name)
            electrical = design.pin_type(ref, pin)
            if conv is not None:
                if net in conv.inputs or pin_kind(name) in ("in", "bias"):
                    on[net].append(pin)
                continue
            if role in ("supply", "ref", "cap") or (
                    electrical == "Power" and role in ("none", "in")) or (
                    unnamed and kind == "ic" and net_kind(net)["power"]):
                on[net].append(pin)
        for net, load_pins in on.items():
            tree.loads[net].append((ref, load_pins))


def build_tree(netlist: dict, plan=None, project_params: dict | None = None) -> _Tree:
    tree = _Tree(netlist, plan, project_params)
    _resistor_graph(tree)
    _capacitor_graph(tree)
    _diode_graph(tree)
    _classify_parts(tree)
    for conv in [*tree.converters.values(), *tree.references.values(),
                 *tree.supervisors.values()]:
        _trace_io(tree, conv)
    for conv in tree.converters.values():
        if not conv.outputs:
            _output_from_divider(tree, conv)
        elif not conv.pins.get("fb"):
            _unnamed_feedback(tree, conv)
        _refine_kind(conv)
    _expand_rails(tree)
    _find_sources(tree)
    _find_loads(tree)
    tree.edges = _series_edges(tree, tree.nodes()) + _converter_edges(tree)
    _supply(tree)
    return tree


# ---------- per-converter facts ----------

def _output_group(tree: _Tree, nets) -> set[str]:
    """The nets joined to these by 0 ohm links, shunts and ferrites (one rail)."""
    group = set(nets)
    queue = list(nets)
    while queue:
        net = queue.pop()
        for _, other, ohms in tree.resistors.get(net, ()):
            if ohms is not None and ohms <= 1.0 and other not in group and \
                    other not in tree.grounds:
                group.add(other)
                queue.append(other)
        for _, other in inductors_on(tree, net, ferrite=True):
            if other not in group and other not in tree.grounds:
                group.add(other)
                queue.append(other)
    return group


def _ends(tree: _Tree, start: str, skip: set[str]) -> list[tuple[str, float | None, list[str]]]:
    """Resistor paths from a node: (far net, ohms, resistors), following one middle node
    that holds only a second resistor (and capacitors): two resistors in series."""
    out = []
    for ref, other, ohms in tree.resistors.get(start, ()):
        if other in skip:
            continue
        if other in tree.grounds or other in tree.nodes():
            out.append((other, ohms, [ref]))
            continue
        onward = [(r, far, o) for r, far, o in tree.resistors.get(other, ()) if r != ref]
        members = [m for m, _ in tree.members(other) if m != ref]
        if len(onward) == 1 and all(
                m == onward[0][0] or tree.kind(m) == "capacitor" for m in members):
            r2, far, o2 = onward[0]
            if far != start:
                total = ohms + o2 if ohms is not None and o2 is not None else None
                out.append((far, total, [ref, r2]))
                continue
        out.append((other, ohms, [ref]))
    return out


def parallel(values: list[float | None]) -> float | None:
    if not values or None in values:
        return None
    if min(values) <= 0:
        return 0.0
    return 1.0 / sum(1.0 / value for value in values)


def _feedback(tree: _Tree, conv: _Converter, vref: float | None) -> list[dict]:
    rows = []
    out_nets = [out.net for out in conv.outputs]
    group = _output_group(tree, out_nets) if out_nets else set()
    for pin, name, net in conv.pins.get("fb", ()):
        row: dict = {"pin": pin, "name": _clean(name) or pin}
        if net is None:
            row["state"] = "unconnected"
            rows.append(row)
            continue
        row["net"] = net
        if net in group or net in tree.nodes():
            row["state"] = "tied_to_output" if net in group else "on_a_rail"
            row["note"] = ("the feedback pin sits on the output: a fixed-output part, or "
                           "Vout = Vref")
            rows.append(row)
            continue
        tops, bottoms, others = [], [], []
        for far, ohms, parts in _ends(tree, net, {net}):
            if far in tree.grounds:
                bottoms.append((far, ohms, parts))
            elif far in group or far in tree.nodes():
                tops.append((far, ohms, parts))
            else:
                others.append((far, ohms, parts))
        if group and any(far in group for far, _, _ in tops):
            tops = [top for top in tops if top[0] in group]
        elif len({far for far, _, _ in tops}) > 1:
            tops = [top for top in tops if top[0] == tops[0][0]]
        if not tops or not bottoms:
            row["state"] = "no_divider"
            row["note"] = ("no resistor divider from an output rail to ground was found on "
                           "the feedback pin (a compensation network, a module set by other "
                           "pins, or a fixed-output part)")
            if tops:
                row["top"] = [part for _, _, parts in tops for part in parts]
            if bottoms:
                row["bottom"] = [part for _, _, parts in bottoms for part in parts]
            rows.append(row)
            continue
        rtop = parallel([ohms for _, ohms, _ in tops])
        rbot = parallel([ohms for _, ohms, _ in bottoms])
        rail = tops[0][0]
        row.update({
            "state": "divider", "rail": rail,
            "top": [part for _, _, parts in tops for part in parts],
            "bottom": [part for _, _, parts in bottoms for part in parts],
            "rtop": ohms_text(rtop), "rbot": ohms_text(rbot),
        })
        if others:
            row["also_on_fb"] = [part for _, _, parts in others for part in parts][:4]
        if rtop is None or rbot is None or rbot == 0:
            row["note"] = "a divider value could not be read"
            rows.append(row)
            continue
        floating = floating_reference(tree, conv)
        if floating and rtop == 0:
            row["note"] = "a 0 ohm top resistor: the ratio cannot be read"
            rows.append(row)
            continue
        gain = fb_gain(rtop, rbot, floating)
        row["formula"] = fb_formula(floating)
        row["vout_over_vref"] = round(gain, 4)
        named = None
        for candidate in [rail, *sorted(group, key=natural_key)]:
            named = rail_voltage(candidate)
            if named:
                row["vout_from_name"] = named
                row["named_rail"] = candidate
                break
        vout_or_vref(row, gain, named, vref)
        # A resistor a jumper or switch joins to ground or the output: a selectable leg.
        for far, ohms, parts in others:
            switched = _switched_leg(tree, far, group)
            if not switched or not ohms:
                continue
            via, to = switched
            alt_top = parallel([rtop, ohms]) if to == "output" else rtop
            alt_bot = parallel([rbot, ohms]) if to == "ground" else rbot
            if not alt_top or not alt_bot:
                continue
            alt = fb_gain(alt_top, alt_bot, floating)
            leg: dict = {"resistors": parts, "through": via, "to": to,
                         "vout_over_vref": round(alt, 4)}
            vout_or_vref(leg, alt, named, vref)
            leg.pop("vref_given", None)
            row["selectable_leg"] = leg
            break
        rows.append(row)
    return rows


def fb_gain(rtop: float, rbot: float, floating: bool) -> float:
    """Vout / Vref of a feedback divider (floating: a three-terminal adjustable regulator,
    its Vref across the top resistor)."""
    return 1 + rbot / rtop if floating else 1 + rtop / rbot


def fb_formula(floating: bool) -> str:
    return ("Vout = Vref x (1 + Rbot/Rtop): a three-terminal adjustable regulator, its Vref "
            "between OUT and ADJ (plus Iadj x Rbot)" if floating else
            "Vout = Vref x (1 + Rtop/Rbot)")


def vout_or_vref(row: dict, gain: float, named: float | None, vref: float | None) -> None:
    """With a given Vref, the output it sets (and its error against the rail name's
    voltage); without one, the Vref the rail name implies and the nearest common one."""
    if vref is not None:
        vout = vref * gain
        row["vref_given"] = vref
        row["vout_computed"] = round(vout, 4)
        if named:
            row["vout_error"] = round((vout - named) / named, 4)
    elif named:
        implied = named / gain
        row["implied_vref"] = round(implied, 4)
        near = [common for common in COMMON_VREFS
                if abs(implied - common) <= common * COMMON_VREF_TOLERANCE]
        if near:
            row["near_common_vref"] = min(near, key=lambda common: abs(implied - common))


def _switched_leg(tree: _Tree, net: str, group: set[str]) -> tuple[str, str] | None:
    """(part, 'ground' or 'output') when a two-pin jumper, header or switch joins the net
    to ground or to the output."""
    for ref, _ in tree.members(net):
        kind = tree.kind(ref)
        if not tree.fitted(ref) or not (
                kind in ("connector", "switch", "relay") or designator_prefix(ref) in (
                    "JP", "SJ", "W", "JMP")):
            continue
        pins = tree.design.part_pins.get(ref) or {}
        if len(pins) != 2:
            continue
        far = next((other for other in pins.values() if other != net), None)
        if far in tree.grounds:
            return ref, "ground"
        if far in group:
            return ref, "output"
    return None


def _unnamed_feedback(tree: _Tree, conv: _Converter) -> None:
    """A pin with no recognised name whose net holds a resistor to the output and one
    to ground is the feedback or ADJ pin (an LM317's pin 1)."""
    group = _output_group(tree, [out.net for out in conv.outputs])
    names = tree.pin_names(conv.ref)
    for pin, net in (tree.design.part_pins.get(conv.ref) or {}).items():
        if pin_kind(names.get(pin)) is not None or net in tree.grounds or \
                net in tree.nodes() or net in group:
            continue
        ends = {far for far, _, _ in _ends(tree, net, {net})}
        if ends & tree.grounds and ends & group:
            conv.pins.setdefault("fb", []).append((pin, names.get(pin) or pin, net))
            conv.basis.append(f"pin {pin} read as feedback/ADJ: a divider from the output "
                              "to ground")


def floating_reference(tree: _Tree, conv: _Converter) -> bool:
    """A three-terminal adjustable regulator (LM317, LM1117-ADJ): no pin on ground, so
    its reference sits between OUT and ADJ and Vout = Vref x (1 + Rbot/Rtop)."""
    nets = (tree.design.part_pins.get(conv.ref) or {}).values()
    return conv.kind in ("ldo", "linear") and not any(net in tree.grounds for net in nets)


def _output_from_divider(tree: _Tree, conv: _Converter) -> None:
    """A controller whose output was not traced: the rail at the top of its feedback
    divider."""
    for row in _feedback(tree, conv, None):
        if row.get("state") == "divider" and row.get("rail"):
            conv.outputs.append(_Output(row["rail"], f"top of the feedback divider "
                                                     f"({', '.join(row['top'])})"))
            conv.basis.append("output from the feedback divider")
            return


def _users(tree: _Tree, nets: list[str], own: str) -> list[tuple[str, str, str]]:
    """(role, part, label) of the other parts on these nets: a converter's EN or PG pin,
    a supervisor, an IC pin (reset, GPIO), a connector, a transistor or a diode."""
    out = []
    design = tree.design
    for net in nets:
        for ref, pin in tree.members(net):
            if ref == own or not tree.fitted(ref):
                continue
            kind = tree.kind(ref)
            name = design.pin_name(ref, pin)
            label = f"{ref}.{pin}" + (f" ({_clean(name)})" if _clean(name) and
                                      _clean(name) != pin else "")
            conv = tree.converters.get(ref)
            pkind = pin_kind(name)
            if conv is not None and pkind == "en":
                out.append(("enable", ref, label))
            elif conv is not None and pkind == "pg":
                out.append(("pg", ref, label))
            elif ref in tree.supervisors and pkind in ("reset", "out", "pg", "flag"):
                out.append(("supervisor", ref, label))
            elif kind in ("ic", "other"):
                control = control_kind(name, pin=True)
                electrical = design.pin_type(ref, pin)
                if control == "reset":
                    out.append(("reset", ref, label))
                elif (control in ("enable", "shutdown") or pkind == "en") and \
                        electrical == "Input":
                    out.append(("enable", ref, label))  # another part's enable input
                elif pin_role(name) in ("supply", "ground", "cap", "ref"):
                    continue
                else:
                    out.append(("gpio", ref, label))
            elif kind == "connector":
                out.append(("connector", ref, label))
            elif kind == "transistor":
                # A drain or collector drives the net (open drain); a gate or base is
                # another input on it.
                first = first_function(name)[:1]
                out.append(("transistor" if first in ("D", "C") else "transistor_input",
                            ref, label))
            elif kind in ("diode", "zener"):
                out.append(("diode", ref, label))
            elif kind in ("switch",):
                out.append(("switch", ref, label))
    return out


def _signal_nets(tree: _Tree, net: str) -> tuple[list[str], list[str]]:
    """The signal's nets (joined by series resistors under 1 kohm), and the nets one
    series resistor of any value away."""
    group = link_group(tree.design, net)
    extra = []
    for member in group:
        for _, other, _ in tree.design.series.get(member, ()):
            if other not in group and other not in extra:
                extra.append(other)
    return group, extra


def _enable(tree: _Tree, conv: _Converter) -> list[dict]:
    rows = []
    for pin, name, net in conv.pins.get("en", ()):
        row: dict = {"pin": pin, "name": _clean(name) or pin}
        if negated(name):
            row["active_low"] = True
        rows.append(row)
        if net is None:
            row["state"] = "unconnected"
            continue
        row["net"] = net
        if net in tree.grounds:
            row["state"] = "tied_to_ground"
            continue
        if net in tree.nodes() or net in tree.design.rails:
            row["state"] = "tied"
            row["to"] = net
            if net in conv.inputs:
                row["to_own_input"] = True
            continue
        group, extra = _signal_nets(tree, net)
        if len(group) > 1:
            row["joined_nets"] = group[1:]
        pulls = [pull for member in group for pull in tree.design.pulls.get(member, ())
                 if pull.fitted]
        if not pulls:
            # A shared enable net one series resistor away (a common EN bus with its own
            # pull-up or divider and a delay capacitor) holds this pin through it.
            pulls = [pull for member in extra for pull in tree.design.pulls.get(member, ())
                     if pull.fitted]
            if pulls:
                row["through"] = sorted({ref for member in group for ref, other, _ in
                                         tree.design.series.get(member, ())
                                         if other in extra}, key=natural_key)[:3]
        ups = [pull for pull in pulls if pull.direction == "up"]
        downs = [pull for pull in pulls if pull.direction == "down"]
        users = _users(tree, group, conv.ref)
        through = [user for user in _users(tree, extra, conv.ref)
                   if user[0] in ("pg", "supervisor", "gpio", "reset", "connector",
                                  "transistor")]
        drivers = users + through
        caps = [ref for member in group for ref, _ in tree.caps(member)]
        if ups:
            row["pull_up"] = [{"designator": p.resistor, "to": p.other,
                               "value": ohms_text(p.ohms)} for p in ups[:3]]
        if downs:
            row["pull_down"] = [{"designator": p.resistor, "value": ohms_text(p.ohms)}
                                for p in downs[:3]]
        if caps:
            row["capacitors"] = caps[:3]
        pg = [user for user in drivers if user[0] == "pg"]
        supervisors = [user for user in drivers if user[0] == "supervisor"]
        gpios = [user for user in drivers if user[0] in ("gpio", "reset")]
        others = [user for user in drivers if user[0] in ("connector", "transistor",
                                                          "switch", "diode")]
        shared = [user for user in drivers if user[0] == "enable"]
        if shared:
            row["shared_with"] = sorted({user[1] for user in shared}, key=natural_key)
        if pg:
            row["state"] = "sequenced"
            row["by"] = [user[2] for user in pg]
        elif supervisors:
            row["state"] = "supervisor"
            row["by"] = [user[2] for user in supervisors]
        elif gpios:
            row["state"] = "gpio"
            row["by"] = [user[2] for user in gpios[:3]]
        elif others:
            row["state"] = others[0][0]
            row["by"] = [user[2] for user in others[:3]]
        elif ups and downs:
            row["state"] = "divider"
            _uvlo(row, ups, downs)
        elif ups:
            row["state"] = "pulled_up"
            row["to"] = ups[0].other
            if ups[0].other in conv.inputs:
                row["to_own_input"] = True
        elif downs:
            row["state"] = "pulled_down"
        elif caps:
            row["state"] = "capacitor_only"
        else:
            row["state"] = "floating"
    for row, (_, name, _) in zip(rows, conv.pins.get("en", ())):
        # Held at the level that turns the part off, with nothing to drive it.
        level = _on_level(name, conv.module)
        if (level == "high" and row["state"] in ("pulled_down", "tied_to_ground")) or (
                level == "low" and row["state"] in ("pulled_up", "tied")):
            row["held_off"] = True
            row["on_when"] = level
    return rows


_EN_HIGH = re.compile(r"N?EN\w*|ENA|ENABLE\w*|RUN\d*|CE|N?ON|ON_?OFF|UVLO\d*")
_SHUTDOWN = re.compile(r"N?SHDN\d*|SHUTDOWN|N?SD")
_INHIBIT = re.compile(r"N?INH\w*")


def _on_level(name: str | None, module: bool = False) -> str | None:
    """'high' or 'low': the level of an enable pin that turns the part on (EN, RUN, ON:
    high; EN\\, nEN: low; SHDN\\: high; INH: low); None when the name does not say (a
    SHDN with no negation mark, or a module's remote on/off pin: modules come in positive
    and negative logic)."""
    clean = _clean(name)
    if module and re.fullmatch(r"N?ON|ON_?OFF", clean):
        return None
    low = negated(name) or bool(re.match(r"N(?:EN|ON|SHDN|SD|INH)", clean))
    if _INHIBIT.fullmatch(clean):
        return None if low else "low"
    if _SHUTDOWN.fullmatch(clean):
        return "high" if low else None
    if _EN_HIGH.fullmatch(clean):
        return "low" if low else "high"
    return None


def _uvlo(row: dict, ups, downs) -> None:
    top = parallel([pull.ohms for pull in ups])
    bottom = parallel([pull.ohms for pull in downs])
    row["from"] = ups[0].other
    if top is None or bottom is None or top + bottom == 0:
        return
    ratio = bottom / (top + bottom)
    row["ratio"] = round(ratio, 4)
    volts = rail_voltage(ups[0].other)
    if volts:
        row["en_volts_at_rail_name"] = round(volts * ratio, 3)
    row["note"] = ("an undervoltage divider: the part turns on when the rail reaches the "
                   f"EN threshold x {round(1 / ratio, 3) if ratio else '?'} (threshold "
                   "from the datasheet)")


def _power_good(tree: _Tree, conv: _Converter) -> list[dict]:
    rows = []
    for kind in ("pg",):
        for pin, name, net in conv.pins.get(kind, ()):
            row: dict = {"pin": pin, "name": _clean(name) or pin}
            rows.append(row)
            if net is None:
                row["state"] = "unconnected"
                continue
            row["net"] = net
            if net in tree.grounds:
                row["state"] = "tied_to_ground"
                continue
            if net in tree.nodes():
                row["state"] = "tied"
                row["to"] = net
                continue
            group, extra = _signal_nets(tree, net)
            pulls = [pull for member in group for pull in tree.design.pulls.get(member, ())
                     if pull.fitted and pull.direction == "up"]
            if pulls:
                row["pulled_up_to"] = sorted({pull.other for pull in pulls}, key=natural_key)
            users = _users(tree, group, conv.ref) + [
                user for user in _users(tree, extra, conv.ref)
                if user[0] in ("enable", "reset", "gpio", "supervisor", "connector")]
            used = [user for user in users if user[0] != "pg"]
            wired = [user for user in users if user[0] == "pg"]
            if wired:
                row["shared_with"] = sorted({user[2] for user in wired}, key=natural_key)
            if used:
                row["state"] = "used"
                row["used_by"] = [{"role": role, "pin": label} for role, _, label in used[:6]]
            else:
                row["state"] = "unused"
    return rows


def _caps_row(tree: _Tree, nets: list[str]) -> dict:
    caps = [(ref, farads) for net in nets for ref, farads in tree.caps(net)]
    known = [farads for _, farads in caps if farads is not None]
    row: dict = {"count": len(caps)}
    if known:
        row["total_uF"] = round(sum(known) * 1e6, 3)
    values = Counter(farads_text(farads) or "?" for _, farads in caps)
    if values:
        row["values"] = dict(values.most_common(6))
    return row


def _facts(conv: _Converter) -> dict:
    out = {}
    for kind in ("ss", "ilim", "mode", "comp", "sense"):
        items = conv.pins.get(kind, ())
        if items:
            out[kind] = [{"pin": pin, "name": _clean(name) or pin, "net": net}
                         for pin, name, net in items[:4]]
    return out


def _input_filter(tree: _Tree, conv: _Converter) -> dict | None:
    if not conv.switching or not conv.inputs:
        return None
    start = conv.inputs[0]
    near = {start}
    for edge in tree.edges:
        if edge.converter:
            continue
        if start in (edge.src, edge.dst) and edge.kind in ("0_ohm", "shunt", "fuse"):
            near.add(edge.other(start))
    for edge in tree.edges:
        if edge.converter or edge.kind not in ("ferrite", "inductor", "common_mode_choke"):
            continue
        for net in near:
            if net not in (edge.src, edge.dst):
                continue
            far = edge.other(net)
            if far in near:
                continue
            if (edge.directed and edge.dst != net) or (
                    far in tree.depth and net in tree.depth and
                    tree.depth[far] > tree.depth[net]):
                continue  # a filter on a branch the input feeds, not in front of it
            here, there = bool(tree.caps(net)), bool(tree.caps(far))
            sharing = sorted({ref for ref, _ in tree.loads.get(net, ()) if ref != conv.ref},
                             key=natural_key)
            row = {"filtered": True, "part": edge.part, "type": edge.kind, "from": far,
                   "form": "pi" if here and there else "LC" if here else
                   "L (no capacitor on the converter side)"}
            if sharing:
                row["shared_with"] = sharing[:6]
            return row
    return {"filtered": False}


# ---------- power inputs ----------

def _trunk(tree: _Tree, source: _Source) -> dict:
    """Follow a power input inward while it is one chain (one onward net): the series
    parts on the way and the protection on each net, up to the first branch, converter
    input or net with loads."""
    adjacency: dict[str, list[_Edge]] = defaultdict(list)
    for edge in tree.edges:
        adjacency[edge.src].append(edge)
        adjacency[edge.dst].append(edge)
    conv_inputs = {net for conv in tree.converters.values() if conv.category == "converter"
                   for net in conv.inputs}
    path = [source.net]
    series: list[tuple[_Edge, str]] = []
    net = source.net
    for _ in range(TRUNK_STEPS):
        if net in conv_inputs or (tree.loads.get(net) and net != source.net):
            break
        onward: dict[str, list[_Edge]] = defaultdict(list)
        for edge in adjacency.get(net, ()):
            far = edge.other(net)
            if far in path:
                continue
            if edge.directed and edge.src != net:
                if edge.converter:
                    continue
            onward[far].append(edge)
        if len(onward) != 1:
            break
        far, edges = next(iter(onward.items()))
        series += [(edge, net) for edge in edges]
        path.append(far)
        net = far
    row: dict = {"path": path}
    fuses, reverse, inrush, filters, switches = [], [], [], [], []
    for edge, upstream in series:
        label = f"{edge.part} ({edge.kind.replace('_', ' ')})"
        if edge.converter:
            conv = tree.converters.get(edge.part)
            if conv and conv.kind in ("efuse", "hot_swap"):
                fuses.append(label)
                inrush.append(label)
            elif conv and conv.kind == "ideal_diode":
                reverse.append(label)
            else:
                switches.append(label)
        elif edge.kind == "fuse":
            fuses.append(label)
        elif edge.kind in ("diode", "schottky"):
            if not edge.directed or edge.src == upstream:
                reverse.append(label if edge.directed else f"{label}, polarity unread")
            else:
                switches.append(f"{label}, cathode toward the input: not a series "
                                "rectifier")
        elif edge.kind in ("p_fet", "n_fet") and edge.source_net:
            # The body diode runs drain to source in a P-FET, source to drain in an
            # N-FET: forward from the input it blocks a reversed supply when the FET is
            # off; backward it conducts one, so the FET is a switch.
            source_up = edge.source_net == upstream
            if (edge.kind == "n_fet") == source_up:
                reverse.append(f"{label}: {'source' if source_up else 'drain'} toward the "
                               "input, body diode forward: reverse-polarity protection "
                               "(check its gate drive)")
            else:
                switches.append(f"{label}: {'source' if source_up else 'drain'} toward the "
                                "input, so its body diode conducts a reversed supply: a "
                                "switch, not reverse-polarity protection")
        elif edge.kind in ("pnp", "npn"):
            switches.append(f"{label}: a bipolar switch or pass transistor")
        elif edge.kind in ("p_fet", "n_fet", "transistor"):
            reverse.append(f"{label}: reverse-polarity protection or a switch (its pins "
                           "or type unread), check its orientation and gate")
        elif edge.kind == "ntc":
            inrush.append(label)
        elif edge.kind in ("ferrite", "inductor", "common_mode_choke"):
            filters.append(label)
        else:
            switches.append(label)
    tvs, clamps, caps = [], [], []
    for net in path:
        tvs += [ref for ref in tree.ctx.esd.get(net, ()) if tree.fitted(ref) and ref not in tvs]
        for ref, other, _ in tree.diodes.get(net, ()):
            if other in tree.grounds and ref not in clamps and tree.kind(ref) != "esd":
                clamps.append(ref)
        caps += tree.caps(net)
    if fuses and not reverse and (tvs or clamps):
        # A diode or unidirectional TVS to ground behind the fuse conducts when the supply
        # is reversed and blows (or trips) the fuse: a crowbar.
        reverse.append(f"{_names(clamps + tvs, 3)} to ground behind {fuses[0]}: a "
                       "crowbar if unidirectional (check the part)")
    on_path = {edge.part for edge, _ in series}
    for conv in tree.converters.values():
        if conv.kind in ("efuse", "hot_swap", "ideal_diode") and \
                set(conv.inputs) & set(path) and conv.ref not in on_path:
            (fuses if conv.kind != "ideal_diode" else reverse).append(
                f"{conv.ref} ({conv.kind.replace('_', ' ')})")
            if conv.kind == "hot_swap":
                inrush.append(f"{conv.ref} (hot swap)")
    for key, value in (("fuse", fuses), ("reverse_polarity", reverse), ("tvs", tvs),
                       ("clamp_diodes_to_ground", clamps), ("inrush", inrush),
                       ("filter", filters), ("other_series", switches)):
        if value:
            row[key] = value
    known = [farads for _, farads in caps if farads is not None]
    row["bulk"] = {"count": len(caps)}
    if known:
        row["bulk"]["total_uF"] = round(sum(known) * 1e6, 3)
        row["bulk"]["largest_uF"] = round(max(known) * 1e6, 3)
    end = path[-1]
    reaches = sorted({conv.ref for conv in tree.converters.values() if end in conv.inputs},
                     key=natural_key)
    if reaches:
        row["feeds_converters"] = reaches
    if tree.loads.get(end):
        row["feeds_loads"] = len(tree.loads[end])
    return row


def _inputs(tree: _Tree) -> list[dict]:
    rows = []
    seen: set[tuple[str, str]] = set()
    folded: dict[tuple[str, str], list[str]] = defaultdict(list)
    for source in tree.sources:
        if source.folded_into and source.role == "input":
            folded[(source.folded_into, source.net)].append(source.part)
    for source in tree.sources:
        if source.kind not in ("connector", "battery") or source.role != "input" or \
                source.folded_into:
            continue
        key = (source.part, source.net)
        if key in seen:
            continue
        seen.add(key)
        row: dict = {"source": source.part, "kind": source.kind, "pins": source.pins,
                     "net": source.net}
        if folded.get(key):
            row["also_input_on_net"] = sorted(set(folded[key]), key=natural_key)
        if source.kind == "connector":
            row["exposure"] = source.exposure
            row["exposure_basis"] = source.exposure_basis
            if source.power_only:
                row["power_only"] = True
            if source.usb:
                row["usb"] = True
        if source.detail:
            row["detail"] = source.detail
        row.update(_trunk(tree, source))
        rows.append(row)
    return rows


# ---------- sequencing ----------

def _sequencing(tree: _Tree, enables: dict[str, list[dict]]) -> list[dict]:
    links = []
    outputs = {out.net: conv.ref for conv in tree.converters.values() for out in conv.outputs}
    for ref, rows in enables.items():
        conv = tree.converters[ref]
        for row in rows:
            state = row.get("state")
            if state in ("sequenced", "supervisor"):
                for label in row.get("by", ()):
                    source = label.split(".")[0]
                    links.append((source, ref, "PG -> EN" if state == "sequenced"
                                  else "supervisor -> EN"))
            elif state in ("tied", "pulled_up", "divider") and not row.get("held_off"):
                rail = row.get("to") or row.get("from")
                source = outputs.get(rail)
                if rail and source and source != ref and rail not in conv.inputs:
                    links.append((source, ref, f"{rail} -> EN"))
    graph: dict[str, set[str]] = defaultdict(set)
    for source, target, _ in links:
        graph[source].add(target)
        graph[target].add(source)
    chains, seen = [], set()
    for start in sorted(graph, key=natural_key):
        if start in seen:
            continue
        component, queue = {start}, [start]
        while queue:
            node = queue.pop()
            for other in graph[node]:
                if other not in component:
                    component.add(other)
                    queue.append(other)
        seen |= component
        if len(component) < 2:
            continue
        chain_links = [{"from": source, "to": target, "how": how}
                       for source, target, how in links
                       if source in component and target in component]
        targets = {link["to"] for link in chain_links}
        order, frontier = [], sorted(component - targets, key=natural_key)
        placed = set()
        while frontier:
            order += frontier
            placed |= set(frontier)
            frontier = sorted({link["to"] for link in chain_links
                               if link["from"] in placed and link["to"] not in placed},
                              key=natural_key)
        order += sorted(component - placed, key=natural_key)
        chains.append({"parts": order, "links": chain_links})
    return chains


# ---------- plain data ----------

def build_power_tree(netlist: dict, plan=None, project_params: dict | None = None,
                     vref: dict[str, float] | float | None = None) -> dict:
    """The power tree as plain data: rails, converters (with EN, PG, feedback, capacitors,
    input filter), edges, sources, power inputs with their protection, references,
    supervisors and sequencing chains. vref is a Vref in volts per converter designator,
    or one for all. Heuristic: see the module notes."""
    return tree_data(build_tree(netlist, plan, project_params), vref)


def _vref_for(vref, ref: str) -> float | None:
    if isinstance(vref, (int, float)):
        return float(vref)
    if isinstance(vref, dict):
        folded = {key.casefold(): value for key, value in vref.items()}
        value = folded.get(ref.casefold())
        return float(value) if value is not None else None
    return None


def tree_data(tree: _Tree, vref=None) -> dict:
    """A built tree as plain data (what build_power_tree returns), for callers that also
    read the tree itself (check_derating reads its switch nodes and edges)."""
    converters = []
    enables: dict[str, list[dict]] = {}
    for ref in sorted(tree.converters, key=natural_key):
        conv = tree.converters[ref]
        row: dict = {"designator": ref, "type": conv.kind, "category": conv.category,
                     "part": tree.label(ref) or None, "basis": conv.basis,
                     "switching": conv.switching}
        for flag in ("module", "controller", "isolated"):
            if getattr(conv, flag):
                row[flag] = True
        row["inputs"] = [{"rail": net, "voltage": rail_voltage(net)} for net in conv.inputs]
        row["outputs"] = []
        for out in conv.outputs:
            entry = {"rail": out.net, "voltage": rail_voltage(out.net), "via": out.via}
            if out.channel:
                entry["channel"] = out.channel
            if out.kind:
                entry["type"] = out.kind
            group = _output_group(tree, [out.net])
            entry["capacitors"] = sum(len(tree.caps(net)) for net in group)
            joined = sorted((net for net in group - {out.net}
                             if net_kind(net)["power"] or rail_voltage(net)), key=natural_key)
            if joined:
                entry["same_rail_as"] = joined[:4]
            row["outputs"].append(entry)
        enables[ref] = _enable(tree, conv)
        row["enable"] = enables[ref]
        row["power_good"] = _power_good(tree, conv)
        row["feedback"] = _feedback(tree, conv, _vref_for(vref, ref))
        row["input_capacitors"] = _caps_row(tree, conv.inputs)
        row["output_capacitors"] = _caps_row(tree, [out.net for out in conv.outputs])
        if (filt := _input_filter(tree, conv)) is not None:
            row["input_filter"] = filt
        if facts := _facts(conv):
            row["pins_of_note"] = facts
        if conv.notes:
            row["notes"] = conv.notes
        converters.append({key: value for key, value in row.items()
                           if value not in (None, [], {})})
    rails = _rail_rows(tree)
    edges = []
    for edge in tree.edges:
        src, dst = edge.src, edge.dst
        directed = edge.directed
        if not directed and src in tree.depth and dst in tree.depth and \
                tree.depth[src] != tree.depth[dst]:
            if tree.depth[src] > tree.depth[dst]:
                src, dst = dst, src
            directed = True
        entry = {"from": src, "to": dst, "part": edge.part, "type": edge.kind,
                 "directed": directed}
        if edge.value:
            entry["value"] = edge.value
        if edge.converter:
            entry["converter"] = True
        edges.append(entry)
    sources = []
    for source in tree.sources:
        entry = {"type": source.kind, "part": source.part, "net": source.net,
                 "pins": source.pins}
        if source.kind == "connector":
            entry["role"] = source.role
            entry["exposure"] = source.exposure
            if source.folded_into:
                entry["input_row"] = source.folded_into
        if source.detail:
            entry["detail"] = source.detail
        sources.append(entry)
    return {
        "variant": tree.variant,
        "converters": converters,
        "rails": rails,
        "edges": edges,
        "sources": sources,
        "inputs": _inputs(tree),
        "references": [_other_row(tree, conv) for _, conv in
                       sorted(tree.references.items(), key=lambda item: natural_key(item[0]))],
        "supervisors": [_supervisor_row(tree, conv) for _, conv in
                        sorted(tree.supervisors.items(), key=lambda item: natural_key(item[0]))],
        "sequencing": _sequencing(tree, enables),
        "not_fitted_converters": [
            {"designator": ref, "type": conv.kind,
             "nets": sorted({net for items in conv.pins.values() for _, _, net in items
                             if net and net not in tree.grounds}, key=natural_key)[:6]}
            for ref, conv in sorted(tree.not_fitted.items(),
                                    key=lambda item: natural_key(item[0]))],
    }


def _other_row(tree: _Tree, conv: _Converter) -> dict:
    row = {"designator": conv.ref, "part": tree.label(conv.ref) or None, "basis": conv.basis,
           "fitted": conv.fitted}
    outs = [out.net for out in conv.outputs] or [
        net for net in (tree.design.part_pins.get(conv.ref) or {}).values()
        if net not in tree.grounds]
    row["nets"] = sorted(set(outs), key=natural_key)[:4]
    return {key: value for key, value in row.items() if value not in (None, [])}


def _supervisor_row(tree: _Tree, conv: _Converter) -> dict:
    design = tree.design
    pins = design.part_pins.get(conv.ref) or {}
    monitored, outputs = [], []
    for pin, net in pins.items():
        name = design.pin_name(conv.ref, pin)
        kind = pin_kind(name)
        if net in tree.grounds:
            continue
        if kind in ("reset", "pg", "out", "flag") or design.pin_type(conv.ref, pin) in (
                "Output", "OpenCollector"):
            users = _users(tree, link_group(design, net), conv.ref) if net not in \
                tree.nodes() else []
            outputs.append({"pin": pin, "name": _clean(name) or pin, "net": net,
                            "used_by": [label for _, _, label in users[:4]]})
        elif _SUPERVISED_IN.fullmatch(_clean(name)) or net in tree.nodes():
            monitored.append(net)
    row = {"designator": conv.ref, "part": tree.label(conv.ref) or None, "basis": conv.basis,
           "fitted": conv.fitted,
           "monitors": sorted(set(monitored), key=natural_key)[:6], "outputs": outputs[:4]}
    return {key: value for key, value in row.items() if value not in (None, [])}


def _upstream_unrecognised(tree: _Tree, net: str) -> list[dict]:
    """Series power parts (a resistor up to 100 ohm, an inductor or ferrite, a fuse, a
    diode, a transistor's channel) from the net to a net that is not read as a rail: the
    rail is probably fed from there (a bus switched by MOSFETs, a sense node)."""
    out = []
    nodes = tree.nodes()
    for ref, other, ohms in tree.resistors.get(net, ()):
        if ohms is not None and ohms <= FILTER_OHMS and other not in nodes and \
                other not in tree.grounds:
            out.append({"part": ref, "type": "resistor", "from": other})
    for ferrite in (False, True):
        for ref, other in inductors_on(tree, net, ferrite):
            if other not in nodes and other not in tree.grounds:
                out.append({"part": ref, "type": "ferrite" if ferrite else "inductor",
                            "from": other})
    for ref, other, side in tree.diodes.get(net, ()):
        if tree.kind(ref) != "esd" and side != "anode" and other not in nodes and \
                other not in tree.grounds:
            out.append({"part": ref, "type": "diode", "from": other})
    for ref, pin in tree.members(net):
        kind = tree.kind(ref)
        if not tree.fitted(ref) or kind not in ("fuse", "transistor"):
            continue
        if kind == "transistor" and not _SERIES_PINS.fullmatch(
                first_function(tree.design.pin_name(ref, pin))):
            continue
        for other_pin, other in (tree.design.part_pins.get(ref) or {}).items():
            if other == net or other in nodes or other in tree.grounds:
                continue
            if kind == "transistor" and not _SERIES_PINS.fullmatch(
                    first_function(tree.design.pin_name(ref, other_pin))):
                continue
            out.append({"part": ref, "type": kind, "from": other})
    return out


def _rail_rows(tree: _Tree) -> list[dict]:
    sources_on: dict[str, list[_Source]] = defaultdict(list)
    for source in tree.sources:
        sources_on[source.net].append(source)
    rows = []
    for net in tree.nodes():
        row: dict = {"rail": net, "voltage": rail_voltage(net)}
        if net in tree.intermediate:
            row["origin"] = "power path (unnamed net between rails)"
        row["supplied"] = net in tree.supplied
        if net in tree.depth:
            row["depth"] = tree.depth[net]
        sources = []
        for source in sources_on.get(net, ()):
            entry = {"type": source.kind, "part": source.part}
            if source.kind == "connector":
                entry["role"] = source.role
            if source.detail and source.kind != "converter":
                entry["detail"] = source.detail
            if entry not in sources:
                sources.append(entry)
        if sources:
            row["sources"] = sources[:6]
        fed_by, feeds = [], []
        for edge in tree.edges:
            if net not in (edge.src, edge.dst):
                continue
            other = edge.other(net)
            entry = {"part": edge.part, "type": edge.kind}
            incoming = (edge.directed and edge.dst == net) or (
                not edge.directed and tree.depth.get(other, 1 << 30) <
                tree.depth.get(net, 1 << 30))
            outgoing = (edge.directed and edge.src == net) or (
                not edge.directed and tree.depth.get(other, 1 << 30) >
                tree.depth.get(net, 1 << 30))
            if incoming:
                fed_by.append({**entry, "from": other})
            elif outgoing:
                feeds.append({**entry, "to": other})
            else:
                feeds.append({**entry, "with": other})
        if fed_by:
            row["fed_by"] = fed_by[:6]
        if feeds:
            row["feeds"] = feeds[:10]
            if len(feeds) > 10:
                row["feeds_total"] = len(feeds)
        loads = sorted(tree.loads.get(net, ()), key=lambda item: natural_key(item[0]))
        row["load_count"] = len(loads)
        row["load_pins"] = sum(len(pins) for _, pins in loads)
        if loads:
            row["loads"] = [ref for ref, _ in loads[:LOADS_LISTED]]
            roles = {pin_role(tree.design.pin_name(ref, pin)) for ref, pins in loads
                     for pin in pins}
            if roles <= {"ref"}:
                row["reference_inputs_only"] = True
        if tree.connectors_on.get(net):
            row["connectors"] = sorted(set(tree.connectors_on[net]), key=natural_key)[:6]
        if tree.test_points.get(net):
            row["test_points"] = sorted(set(tree.test_points[net]), key=natural_key)[:4]
        caps = tree.caps(net)
        if caps:
            known = [farads for _, farads in caps if farads is not None]
            row["capacitors"] = len(caps)
            if known:
                row["capacitance_uF"] = round(sum(known) * 1e6, 3)
        feeders = [conv.ref for conv in tree.not_fitted.values()
                   if net in {item[2] for kind in ("out", "sw") for item in conv.pins.get(
                       kind, ())}]
        if feeders:
            row["not_fitted_feeders"] = sorted(feeders, key=natural_key)
        if net not in tree.supplied:
            if upstream := _upstream_unrecognised(tree, net):
                row["fed_through_from_unrecognised_net"] = upstream[:4]
            if high := _high_value_feeds(tree, net):
                row["only_through_resistor"] = high[:4]
        rows.append(row)

    def rank(row: dict) -> int:
        # Unsupplied rails with loads first (the findings name them), then supplied rails
        # from the sources outward, a part's own bias or regulator pins, the rest.
        if not row["supplied"]:
            return 0 if row["load_count"] else 3
        own = all(source["type"] in ("internal", "driven", "reference")
                  for source in row.get("sources", ())) and row.get("sources")
        return 2 if own and not row.get("fed_by") and not row.get("feeds") else 1

    rows.sort(key=lambda row: (rank(row), row.get("depth", 1 << 30), -row["load_count"],
                               natural_key(row["rail"])))
    return rows


def _high_value_feeds(tree: _Tree, net: str) -> list[dict]:
    """Resistors above FILTER_OHMS from a supplied rail: a pull-up, bleeder or sense
    divider, which can feed only a microamp load (a backup or reference pin)."""
    return [{"part": ref, "value": ohms_text(ohms), "from": other}
            for ref, other, ohms in sorted(tree.resistors.get(net, ()),
                                           key=lambda item: natural_key(item[0]))
            if ohms is not None and ohms > FILTER_OHMS and other in tree.supplied]


# ---------- findings ----------

def _finding(check: str, severity: str, issue: str, designators=(), **fields) -> dict:
    out: dict = {"check": check, "severity": severity}
    if designators:
        out["designators"] = sorted(set(designators), key=natural_key)
    out.update({key: value for key, value in fields.items() if value not in (None, [], {})})
    out["issue"] = issue
    return out


def _names(items, limit: int = 6) -> str:
    items = list(dict.fromkeys(items))
    shown = ", ".join(items[:limit])
    return shown + (f" and {len(items) - limit} more" if len(items) > limit else "")


def power_findings(data: dict) -> list[dict]:
    """Findings from build_power_tree's data (every check; the caller filters)."""
    findings: list[dict] = []
    findings += _rail_findings(data)
    for conv in data["converters"]:
        findings += _converter_findings(conv, data)
    findings += _input_findings(data)
    for chain in data["sequencing"]:
        steps = "; ".join(f"{link['from']} -> {link['to']} ({link['how']})"
                          for link in chain["links"][:8])
        findings.append(_finding(
            "sequencing", "info",
            f"Power-up sequence {' -> '.join(chain['parts'][:8])}: {steps}. Check the "
            "order and the delays against what the loads ask for (core before I/O, or "
            "the FPGA's or processor's power-up sequence).",
            chain["parts"], parts=chain["parts"],
        ))
    return findings


def _rail_findings(data: dict) -> list[dict]:
    findings = []
    takes = {item["rail"]: conv["designator"] for conv in data["converters"]
             for item in conv.get("inputs", ())}
    for row in data["rails"]:
        if row["supplied"] or not row.get("load_count"):
            continue
        loads = row.get("loads", [])
        shown = _names(loads)
        rail = row["rail"]
        if row.get("reference_inputs_only") and not net_kind(rail)["power"]:
            continue  # a VREF input set by a divider or tied off, not a supply rail
        if row.get("fed_through_from_unrecognised_net"):
            continue  # fed from a net not read as a rail: the row says through what
        if high := row.get("only_through_resistor"):
            first = high[0]
            findings.append(_finding(
                "rail_without_source", "warning" if len(loads) > 1 else "info",
                f"{rail} feeds {row['load_count']} part{'s' if len(loads) != 1 else ''} "
                f"({shown}) but reaches a supplied rail only through {first['part']} "
                f"({first['value']}) from {first['from']}: a resistor that size feeds a "
                "microamp load (a backup or reference pin), not a supply. Check the loads' "
                "current, or the net (a missing link or regulator).",
                [*loads, *(item["part"] for item in high)], rail=rail, loads=loads,
            ))
        elif row.get("not_fitted_feeders"):
            findings.append(_finding(
                "rail_without_source", "warning",
                f"{rail} feeds {row['load_count']} parts ({shown}) but its only source "
                f"{'is' if len(row['not_fitted_feeders']) == 1 else 'are'} "
                f"{_names(row['not_fitted_feeders'])}, Not Fitted in variant "
                f"{data['variant']}: nothing powers it in this variant.",
                [*loads, *row["not_fitted_feeders"]], rail=rail, loads=loads,
            ))
        elif row.get("connectors") or row.get("test_points"):
            where = _names(row.get("connectors", []) + row.get("test_points", []))
            findings.append(_finding(
                "rail_without_source", "info",
                f"{rail} feeds {row['load_count']} parts ({shown}) and nothing on the board "
                f"was found to supply it; it reaches {where}, so it may be supplied from "
                "outside the board (another board, a bench supply).",
                loads, rail=rail, loads=loads,
            ))
        elif len(loads) == 1 and takes.get(rail) == loads[0]:
            findings.append(_finding(
                "rail_without_source", "info",
                f"{rail} is the input of {loads[0]} and nothing on the board was found to "
                "supply it (no connector power pin, converter output, battery or series "
                "part from a supplied rail): check where its input comes from (an off-board "
                "feed, a part not recognised).",
                loads, rail=rail, loads=loads,
            ))
        elif len(loads) == 1:
            findings.append(_finding(
                "rail_without_source", "info",
                f"{rail} links only {loads[0]}'s pins and passives, and no source was found "
                "for it: most likely a pin the part regulates itself (an internal "
                "regulator's output or capacitor pin, see the datasheet), else a supply pin "
                "left off its rail (a misspelt net name).",
                loads, rail=rail, loads=loads,
            ))
        else:
            findings.append(_finding(
                "rail_without_source", "warning",
                f"{rail} feeds {row['load_count']} parts ({shown}) and nothing was found to "
                "supply it: no converter output, connector power pin, battery, or series "
                "part from a supplied rail. Check the net name (a typo splits a rail) or "
                "a regulator not recognised (its name or pins).",
                loads, rail=rail, loads=loads,
            ))
    return findings


def _converter_findings(conv: dict, data: dict) -> list[dict]:
    findings = []
    ref = conv["designator"]
    kind = conv["type"].replace("_", " ")
    if conv["category"] == "converter" or conv["category"] == "switch":
        if not conv.get("outputs"):
            findings.append(_finding(
                "converter_unknown_rail", "info",
                f"{ref} ({kind}): its output was not traced (no VOUT/OUT pin, no inductor "
                "on a SW pin, no feedback divider to a rail), so it is not in the tree.",
                [ref], part=ref,
            ))
        if not conv.get("inputs"):
            findings.append(_finding(
                "converter_unknown_rail", "info",
                f"{ref} ({kind}): its input was not found (no VIN/IN pin on a net).",
                [ref], part=ref,
            ))
        rails = {row["rail"]: row for row in data["rails"]}
        for out in conv.get("outputs", ()):
            row = rails.get(out["rail"], {})
            if not (row.get("load_count") or row.get("connectors") or row.get("feeds")
                    or out.get("same_rail_as")):
                findings.append(_finding(
                    "converter_unknown_rail", "info",
                    f"{ref} ({kind}) outputs on {out['rail']}, which reaches no load, "
                    "connector or other rail that was recognised: check what it powers "
                    "(a load whose supply pins are not named, an off-board feed).",
                    [ref], part=ref, rail=out["rail"],
                ))
    enables = conv.get("enable", ())
    for row in enables:
        state = row.get("state")
        if row.get("held_off"):
            downs = [pull["designator"] for pull in row.get("pull_down", ())]
            through = f" (through {_names(row['through'])})" if row.get("through") else ""
            how = {"tied_to_ground": "is tied to ground",
                   "tied": f"is tied to {row.get('to')}",
                   "pulled_up": f"is pulled up to {row.get('to')}{through}",
                   "pulled_down": "is pulled down" + (f" by {_names(downs)}" if downs else "")
                   + through,
                   }[state]
            outputs = [out["rail"] for out in conv.get("outputs", ())]
            # A multi-output part with an enable per channel may leave one channel off.
            several = len(enables) > 1 and len(outputs) > 1
            findings.append(_finding(
                "enable_held_off", "info" if several else "warning",
                f"{ref}'s enable pin {row['pin']} ({row['name']}) {how} and nothing drives "
                f"it, while it turns the part on when {row['on_when']}: the converter stays "
                "off" + (f" and {_names(outputs)} never comes up" if outputs and not several
                         else "")
                + ". Intended only if this part or channel is meant to stay off; check the "
                "pin's polarity in the datasheet.",
                [ref, *downs], part=ref, pin=row["pin"],
            ))
        elif state in ("floating", "unconnected"):
            shared = row.get("shared_with", [])
            findings.append(_finding(
                "enable_floating", "warning",
                f"{ref}'s enable pin {row['pin']} ({row['name']}) "
                + ("is not connected" if state == "unconnected" else
                   f"is on {row.get('net')} with nothing that drives or pulls it")
                + (f" (the net also reaches the enable of {_names(shared)})" if shared else "")
                + ": the converter's state is undefined unless the datasheet gives the pin "
                "an internal pull-up or pull-down.",
                [ref, *shared], part=ref, pin=row["pin"],
            ))
        elif state == "capacitor_only":
            findings.append(_finding(
                "enable_floating", "info",
                f"{ref}'s enable pin {row['pin']} ({row['name']}) has only "
                f"{_names(row.get('capacitors', []))} to ground: a start-up delay with the "
                "pin's internal pull-up current, or floating; check the datasheet.",
                [ref, *row.get("capacitors", [])], part=ref, pin=row["pin"],
            ))
        elif state in ("tied", "pulled_up") and row.get("to_own_input"):
            findings.append(_finding(
                "enable_tied_note", "info",
                f"{ref}'s enable pin {row['pin']} ({row['name']}) is "
                f"{'tied' if state == 'tied' else 'pulled up'} to its own input "
                f"{row.get('to')}: it starts at its internal UVLO; an EN divider sets a "
                "higher start voltage if the input may sag or rise slowly.",
                [ref], part=ref, pin=row["pin"],
            ))
    for row in conv.get("power_good", ()):
        if row.get("state") in ("unused", "unconnected", "tied_to_ground"):
            how = {"unconnected": "is not connected",
                   "tied_to_ground": "is tied to ground"}.get(
                row["state"], "has nothing reading it" + (
                    f" (pulled up to {_names(row['pulled_up_to'])})"
                    if row.get("pulled_up_to") else ""))
            findings.append(_finding(
                "pgood_unused", "info",
                f"{ref}'s power-good pin {row['pin']} ({row['name']}) {how}: it could "
                "sequence the next regulator, hold a processor in reset or tell firmware.",
                [ref], part=ref, pin=row["pin"],
            ))
    for row in conv.get("feedback", ()):
        if row.get("state") != "divider" or "vout_over_vref" not in row:
            continue
        parts = [ref, *row.get("top", []), *row.get("bottom", [])]
        ratio = (f"Rtop {_names(row['top'])} {row['rtop']}, Rbot {_names(row['bottom'])} "
                 f"{row['rbot']}: Vout = Vref x {row['vout_over_vref']:g}")
        leg = row.get("selectable_leg")
        joined = (f"; with {leg['through']} closed, {_names(leg['resistors'])} joins the "
                  f"{'bottom' if leg['to'] == 'ground' else 'top'} leg: Vout = Vref x "
                  f"{leg['vout_over_vref']:g}") if leg else ""
        if leg:
            parts += [*leg["resistors"], leg["through"]]
        if "vref_given" in row:
            error = row.get("vout_error")
            alt_error = leg.get("vout_error") if leg else None
            if error is not None and abs(error) > VOUT_TOLERANCE and (
                    alt_error is None or abs(alt_error) > VOUT_TOLERANCE):
                findings.append(_finding(
                    "fb_divider_mismatch", "warning",
                    f"{ref}: with Vref {row['vref_given']:g} V the divider ({ratio}) sets "
                    f"{row['vout_computed']:.3g} V, but {row['named_rail']} is named "
                    f"{row['vout_from_name']:g} V ({error * 100:+.1f}%)"
                    + (f"{joined}, {leg['vout_computed']:.3g} V" if leg else "") + ".",
                    parts, part=ref, rail=row.get("named_rail"),
                    vout_computed=row["vout_computed"], vout_from_name=row["vout_from_name"],
                ))
            continue
        if "implied_vref" in row:
            near = row.get("near_common_vref")
            alt = ""
            if leg and "implied_vref" in leg:
                alt_near = leg.get("near_common_vref")
                alt = (f"{joined}, implying Vref {leg['implied_vref']:g} V"
                       + (f" (the common {alt_near:g} V)" if alt_near else ""))
            findings.append(_finding(
                "fb_divider_mismatch", "info",
                f"{ref}: the divider ({ratio}) and the rail name {row['named_rail']} "
                f"({row['vout_from_name']:g} V) imply Vref {row['implied_vref']:g} V"
                + (f", close to the common {near:g} V" if near else
                   ", not a common reference value")
                + alt
                + ": check it against the datasheet, or pass vref= to compute the output.",
                parts, part=ref, rail=row.get("named_rail"),
                implied_vref=row["implied_vref"], near_common_vref=near,
            ))
    filt = conv.get("input_filter")
    if filt is not None and not filt.get("filtered"):
        rail = conv["inputs"][0]["rail"] if conv.get("inputs") else None
        findings.append(_finding(
            "smps_input_unfiltered", "info",
            f"{ref} ({kind}{', module' if conv.get('module') else ''}) takes its input "
            f"from {rail} with no inductor or ferrite (LC or pi filter) between it and its "
            "source: Altium's EMC guidance puts a pi filter or similar on every SMPS input "
            "to keep its switching current off the supply.",
            [ref], part=ref, rail=rail,
        ))
    if conv["category"] in ("converter", "switch"):
        for out in conv.get("outputs", ()):
            if out.get("capacitors") or conv["category"] != "converter":
                continue
            findings.append(_finding(
                "no_output_capacitor", "info",
                f"{ref} ({kind}): no capacitor to ground on its output {out['rail']}: most "
                "regulators need one (value and ESR from the datasheet) to be stable.",
                [ref], part=ref, rail=out["rail"],
            ))
        if conv.get("inputs") and not conv["input_capacitors"]["count"] and \
                not conv.get("_decoupled"):
            findings.append(_finding(
                "no_input_capacitor", "info",
                f"{ref} ({kind}): no capacitor to ground on its input "
                f"{conv['inputs'][0]['rail']}.",
                [ref], part=ref, rail=conv["inputs"][0]["rail"],
            ))
    return findings


_INPUT_TEXT = {
    "no_input_fuse": ("no fuse, PTC or eFuse on the way in", "the schematic checklist asks "
                      "for one on power inputs so a fault downstream cannot burn the board "
                      "or the cable"),
    "no_reverse_protection": ("no series diode, P-FET, ideal-diode controller or crowbar "
                              "on the way in", "a reversed supply reaches every part on it"),
    "no_input_tvs": ("no TVS, varistor or clamp diode on the input path",
                     "surges and hot-plug spikes reach the regulators"),
}


def _input_findings(data: dict) -> list[dict]:
    """One finding per connector and check, naming its power nets that lack the part."""
    missing: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in data["inputs"]:
        if row["kind"] != "connector" or row.get("exposure") == "internal":
            continue
        if row.get("exposure") != "external" and not row.get("power_only"):
            continue
        # USB: the host limits VBUS current and the plug cannot be reversed.
        if not row.get("fuse") and not row.get("usb"):
            missing[(row["source"], "no_input_fuse")].append(row)
        if not row.get("reverse_polarity") and not row.get("usb"):
            missing[(row["source"], "no_reverse_protection")].append(row)
        if not row.get("tvs") and not row.get("clamp_diodes_to_ground"):
            missing[(row["source"], "no_input_tvs")].append(row)
    findings = []
    for (source, check), rows in missing.items():
        what, why = _INPUT_TEXT[check]
        exposure = rows[0].get("exposure")
        nets = [row["net"] for row in rows]
        paths = "; ".join(" -> ".join(row["path"][:5]) for row in rows[:3])
        caveat = "" if exposure == "external" else (
            " Its exposure is unknown: if it is an output or another board's supply, "
            "ignore this.")
        findings.append(_finding(
            check, "info",
            f"{source} ({exposure}{', power pins only' if rows[0].get('power_only') else ''}"
            f") power input {_names(nets, 4)}: {what} (path {paths}); {why}." + caveat,
            [source], source=source, nets=nets,
        ))
    return findings


# ---------- mermaid ----------

def _node_id(prefix: str, name: str, ids: dict) -> str:
    key = (prefix, name)
    if key not in ids:
        base = re.sub(r"[^A-Za-z0-9_]", "_", name) or "n"
        ids[key] = f"{prefix}_{base}_{len(ids)}"
    return ids[key]


def _quote(text: str) -> str:
    return text.replace('"', "'").replace("\n", " ")


def mermaid(data: dict, loads: bool = True, max_lines: int | None = None) -> str:
    """A Mermaid flowchart: sources -> protection -> converters -> rails -> load counts."""
    ids: dict = {}
    lines = ["flowchart LR",
             "  %% Draft read from names: verify each converter against its datasheet"]
    rails = {row["rail"]: row for row in data["rails"]}

    def rail_node(net: str) -> str:
        row = rails.get(net, {})
        node = _node_id("R", net, ids)
        if ("R", net, "decl") not in ids:
            ids[("R", net, "decl")] = True
            volts = row.get("voltage")
            label = net + (f" {volts:g} V" if volts else "")
            if not row.get("supplied", True):
                label += " (no source found)"
            lines.append(f'  {node}(["{_quote(label)}"])')
        return node

    for source in data["sources"]:
        if source["type"] not in ("connector", "battery"):
            continue
        node = _node_id("S", f"{source['part']}_{source['net']}", ids)
        what = source["type"] if source["type"] == "battery" else (
            f"{source.get('exposure', '')} {source.get('role', '')}".strip())
        label = f"{source['part']} pin {', '.join(source['pins'][:2])} ({what})"
        lines.append(f'  {node}[/"{_quote(label)}"/]')
        if source.get("role") == "outlet":
            lines.append(f"  {rail_node(source['net'])} --> {node}")
        else:
            lines.append(f"  {node} --> {rail_node(source['net'])}")
    for conv in data["converters"]:
        node = _node_id("C", conv["designator"], ids)
        kind = conv["type"].replace("_", " ")
        lines.append(f'  {node}["{_quote(conv["designator"])} {kind}"]')
        for item in conv.get("inputs", ())[:1]:
            lines.append(f"  {rail_node(item['rail'])} --> {node}")
        for out in conv.get("outputs", ()):
            lines.append(f"  {node} --> {rail_node(out['rail'])}")
    for edge in data["edges"]:
        if edge.get("converter"):
            continue
        label = f"{edge['part']} {edge['type'].replace('_', ' ')}"
        arrow = "-->" if edge["directed"] else "---"
        lines.append(f'  {rail_node(edge["from"])} {arrow}|"{_quote(label)}"| '
                     f"{rail_node(edge['to'])}")
    if loads:
        converters = {conv["designator"] for conv in data["converters"]}
        for row in data["rails"]:
            if not row.get("load_count") or ("R", row["rail"], "decl") not in ids:
                continue
            # Converters are drawn already; the node counts the other loads.
            count = row["load_count"] - sum(1 for ref in row.get("loads", [])
                                            if ref in converters)
            if count <= 0:
                continue
            shown = [ref for ref in row.get("loads", []) if ref not in converters]
            label = f"{count} load{'s' if count != 1 else ''}" + (
                f": {', '.join(shown[:4])}" if shown else "")
            if count > len(shown[:4]):
                label += " ..."
            node = _node_id("L", row["rail"], ids)
            lines.append(f'  {node}["{_quote(label)}"]')
            lines.append(f"  {rail_node(row['rail'])} -.-> {node}")
    if max_lines is not None and len(lines) > max_lines:
        dropped = len(lines) - max_lines
        lines = lines[:max_lines] + [f"  %% {dropped} more lines left out: name rail= or "
                                     "part= for a smaller tree"]
    return "\n".join(lines)


# ---------- entry point ----------

def _filter(data: dict, rails: set[str] | None, parts: set[str] | None) -> dict:
    """The tree around the named rails and parts: their converters, rails one edge away,
    and the findings and inputs that name them."""
    if rails is None and parts is None:
        return data
    keep_rails = set(rails or ())
    keep_parts = set(parts or ())
    for conv in data["converters"]:
        nets = {item["rail"] for item in conv.get("inputs", ())} | {
            item["rail"] for item in conv.get("outputs", ())}
        if conv["designator"] in keep_parts or nets & (rails or set()):
            keep_parts.add(conv["designator"])
            keep_rails |= nets
    for edge in data["edges"]:
        if edge["part"] in keep_parts or {edge["from"], edge["to"]} & (rails or set()):
            keep_rails |= {edge["from"], edge["to"]}
    for row in data["rails"]:
        if set(row.get("loads", [])) & (parts or set()):
            keep_rails.add(row["rail"])
    out = dict(data)
    out["converters"] = [conv for conv in data["converters"]
                         if conv["designator"] in keep_parts]
    out["rails"] = [row for row in data["rails"] if row["rail"] in keep_rails]
    out["edges"] = [edge for edge in data["edges"]
                    if edge["from"] in keep_rails and edge["to"] in keep_rails]
    out["sources"] = [source for source in data["sources"] if source["net"] in keep_rails]
    out["inputs"] = [row for row in data["inputs"] if set(row["path"]) & keep_rails
                     or row["source"] in keep_parts]
    out["sequencing"] = [chain for chain in data["sequencing"]
                         if set(chain["parts"]) & keep_parts]
    out["_keep_parts"] = keep_parts
    out["_keep_rails"] = keep_rails
    return out


def prepare_power_tree(
    netlist: dict,
    plan=None,
    project_params: dict | None = None,
    checks: list[str] | None = None,
    rails: list[str] | None = None,
    parts: list[str] | None = None,
    vref: dict[str, float] | float | None = None,
) -> dict:
    """Build the tree once, with its findings filtered and sorted (warnings first), the
    view narrowed to rails and parts, and the notes; render_power_tree pages or draws it.
    An {"error": ...} dict for an unknown check, rail or part."""
    requested = [check.strip().lower() for check in checks or [] if check.strip()]
    if invalid := [check for check in requested if check not in CHECKS]:
        return {"error": "invalid_checks",
                "message": f"Unknown checks {invalid}; use {list(CHECKS)}."}
    tree = build_tree(netlist, plan, project_params)
    notes: list[str] = []
    wanted_rails, unknown_rails = resolve_names(rails, tree.nodes() | set(tree.design.nets))
    if rails and not wanted_rails:
        return {"error": "rail_not_found",
                "message": f"No net {unknown_rails} in the compiled netlist; use query_net "
                           "to find the real name."}
    every = set(tree.design.part_pins)
    wanted_parts, unknown_parts = resolve_names(parts, every)
    if parts and not wanted_parts:
        return {"error": "part_not_found",
                "message": f"No part {unknown_parts} in the compiled netlist; use "
                           "search_components to find the designator."}
    if unknown_rails or unknown_parts:
        notes.append(f"Not found: {', '.join([*unknown_rails, *unknown_parts])}.")
    for ref in sorted(wanted_parts or (), key=natural_key):
        if ref in tree.not_fitted:
            notes.append(f"{ref} is Not Fitted in variant {tree.variant}: not in the tree.")
        elif ref not in tree.converters and ref not in tree.references and \
                ref not in tree.supervisors:
            notes.append(f"{ref} is not recognised as a converter, reference or "
                         "supervisor; the rails it is on are shown.")
    data = tree_data(tree, vref)
    if isinstance(vref, dict):
        dividers = {conv["designator"].casefold() for conv in data["converters"]
                    if any(row.get("state") == "divider" for row in conv.get("feedback", ()))}
        known = {ref.casefold() for ref in tree.converters}
        if stray := [ref for ref in vref if ref.casefold() not in known]:
            notes.append(f"vref given for {_names(stray)}, not a converter here: ignored.")
        if unread := [ref for ref in vref if ref.casefold() in known - dividers]:
            notes.append(f"vref given for {_names(unread)}, which has no feedback divider "
                         "read (no FB pin, or no resistors from the output to ground on "
                         "it): ignored.")
    for conv in data["converters"]:
        conv["_decoupled"] = _decoupling_covers(tree, tree.converters[conv["designator"]])
    findings = power_findings(data)
    for conv in data["converters"]:
        conv.pop("_decoupled", None)
    view = _filter(data, wanted_rails, wanted_parts)
    if wanted_rails is not None or wanted_parts is not None:
        keep_parts, keep_rails = view.pop("_keep_parts"), view.pop("_keep_rails")
        findings = [finding for finding in findings
                    if _kept(finding, keep_parts, keep_rails)]
    wanted = set(requested or set(CHECKS) - OPT_IN_CHECKS)
    findings = [finding for finding in findings if finding["check"] in wanted]
    findings.sort(key=lambda finding: (
        _SEVERITY_RANK[finding["severity"]], _CHECK_RANK[finding["check"]],
        natural_key((finding.get("designators") or [""])[0]),
    ))
    return {"variant": data["variant"], "view": view, "findings": findings,
            "wanted": wanted, "notes": notes}


def _kept(finding: dict, keep_parts: set[str], keep_rails: set[str]) -> bool:
    """A finding about a kept part; or, for a rail or power-input finding (not one about a
    converter), about a kept rail."""
    if set(finding.get("designators", ())) & keep_parts:
        return True
    if "part" in finding:
        return False  # a converter's own finding: shown with that converter only
    return finding.get("rail") in keep_rails or bool(set(finding.get("nets", ())) & keep_rails)


def render_power_tree(
    prepared: dict,
    format: str = "json",
    offset: int = 0,
    limit: int = PAGE_LIMIT,
    mermaid_loads: bool = True,
    mermaid_lines: int | None = None,
) -> dict:
    """One page of a prepared tree (converters, findings, rails, sources and power inputs
    page together on one offset), or the tree as a Mermaid flowchart."""
    if format not in ("json", "mermaid"):
        return {"error": "invalid_format", "message": "format must be 'json' or 'mermaid'."}
    view, findings, wanted = prepared["view"], prepared["findings"], prepared["wanted"]
    notes = list(prepared["notes"])
    summary: dict[str, int] = defaultdict(int)
    for finding in findings:
        summary[finding["check"]] += 1
    overview = _overview(view)
    result: dict = {"variant": prepared["variant"], "heuristic": HEURISTIC}
    if format == "mermaid":
        result.update({
            "overview": overview,
            "format": "mermaid",
            "mermaid": mermaid(view, loads=mermaid_loads, max_lines=mermaid_lines),
            "legend": ("[/source/] connector or battery; [converter]; ([rail]); labelled "
                       "links are series parts (fuse, diode, FET, ferrite, 0 ohm); -.-> "
                       "load counts. Directions come from the sources outward; '---' "
                       "means the direction was not found."),
            "summary": dict(summary),
            "severity_counts": _severities(findings),
        })
        notes.append("format='json' gives the converters' EN, PG, feedback and capacitor "
                     "details and the findings.")
        result["notes"] = notes
        return result
    offset = max(0, int(offset or 0))
    page = slice(offset, offset + limit)
    lists = {
        "converters": view["converters"],
        "findings": findings,
        "rails": view["rails"],
        "sources": [source for source in view["sources"] if source["type"] != "converter"],
        "inputs": view["inputs"],
    }
    result.update({
        "checks_run": [check for check in CHECKS if check in wanted],
        "overview": overview,
        "summary": dict(summary),
        "severity_counts": _severities(findings),
        "sources_total": len(lists["sources"]),
        "sources": lists["sources"][page],
        "inputs_total": len(lists["inputs"]),
        "inputs": lists["inputs"][page],
        "converters_total": len(lists["converters"]),
        "converters": lists["converters"][page],
        "rails_total": len(lists["rails"]),
        "rails": lists["rails"][page],
        "references": view["references"],
        "supervisors": view["supervisors"],
        "sequencing": view["sequencing"],
    })
    if view["not_fitted_converters"]:
        result["not_fitted_converters"] = view["not_fitted_converters"][:20]
        if len(view["not_fitted_converters"]) > 20:
            result["not_fitted_converters_total"] = len(view["not_fitted_converters"])
    result.update({
        "findings": findings[page],
        "total": len(findings),
        "offset": offset,
        "has_more": offset + limit < max(len(rows) for rows in lists.values()),
    })
    if result["has_more"]:
        result["next_offset"] = offset + limit
    notes.append(
        "Converters, rails and protection parts come from names: an EN or PG with an "
        "internal pull-up, a fixed-output version, Vref and thresholds are datasheet facts. "
        "Rails are listed unsupplied ones with loads first, then supplied ones in order "
        "from the sources (depth counts the edges from a source), then a part's own bias "
        "or regulator pins. Converters, findings, rails, sources and inputs page together "
        "(offset)."
    )
    if "enable_tied_note" not in wanted:
        notes.append("enable_tied_note (EN tied to its own input, no UVLO divider) runs "
                     "only when named in checks.")
    result["notes"] = notes
    return result


def run_power_tree_check(
    netlist: dict,
    plan=None,
    project_params: dict | None = None,
    checks: list[str] | None = None,
    rails: list[str] | None = None,
    parts: list[str] | None = None,
    vref: dict[str, float] | float | None = None,
    format: str = "json",
    offset: int = 0,
    limit: int = PAGE_LIMIT,
    mermaid_loads: bool = True,
    mermaid_lines: int | None = None,
) -> dict:
    """The power tree and its findings: overview, converters, findings (warnings first),
    rails, sources and power inputs (paged together), references, supervisors and
    sequencing, or the tree as a Mermaid flowchart (format 'mermaid'). Heuristic: see the
    module notes."""
    if format not in ("json", "mermaid"):
        return {"error": "invalid_format", "message": "format must be 'json' or 'mermaid'."}
    prepared = prepare_power_tree(netlist, plan, project_params, checks, rails, parts, vref)
    if "error" in prepared:
        return prepared
    return render_power_tree(prepared, format, offset, limit, mermaid_loads, mermaid_lines)


def _decoupling_covers(tree: _Tree, conv: _Converter) -> bool:
    """check_decoupling already checks the input when the part is a U or IC part with an
    input pin it reads as a supply (or an IN pin on a rail named like power)."""
    if designator_prefix(conv.ref) not in IC_PREFIXES:
        return False
    for _, name, net in conv.pins.get("in", ()):
        role = pin_role(name)
        if net and (role in ("supply", "cap", "ref") or (
                role == "in" and net_kind(net)["power"])):
            return True
    return False


def _severities(findings: list[dict]) -> dict:
    return {level: sum(1 for finding in findings if finding["severity"] == level)
            for level in ("warning", "info")}


def _overview(data: dict) -> dict:
    types = Counter(conv["type"] for conv in data["converters"])
    rails = data["rails"]
    sources = Counter(source["type"] if source["type"] != "connector" else
                      f"connector_{source.get('role', 'input')}" for source in data["sources"])
    return {
        "converters": len(data["converters"]),
        "converters_by_type": dict(types),
        "rails": len(rails),
        "rails_supplied": sum(1 for row in rails if row["supplied"]),
        "rails_with_loads_unsupplied": sum(1 for row in rails
                                           if not row["supplied"] and row.get("load_count")),
        "sources": dict(sources),
        "power_inputs": len(data["inputs"]),
        "references": len(data["references"]),
        "supervisors": len(data["supervisors"]),
        "sequencing_chains": len(data["sequencing"]),
    }
