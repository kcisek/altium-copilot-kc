"""Schematic-to-layout constraints brief (get_constraints_brief) and rule files from an
agreed constraint plan (generate_rule_file), offline and read-only: nothing here edits a
design file.

The brief combines what the other checks already read, each item with its evidence and
whether the PCB or the schematic directives already cover it (a PCB rule covers an item
only when its values and mode ask at least as much: a Width rule at least the IPC width on
every layer, a Clearance at least the IPC-2221B gap, a MatchedLengths rule in the same
mode (within each pair, or across the group, through xSignals too) and no looser than the
typical tolerance; a rule that takes the members but asks less is named under "weaker"):
- proposed net classes: differential pairs grouped by interface (pair naming, the
  interfaces of the parts on their legs, the processor pin roles of get_firmware_pin_map's
  classify) with typical impedance and in-pair matching from constraints_review's
  INTERFACE_TARGETS, quoted as typical and never as the requirement; power rails grouped
  by current (check_power_paths' estimate, current= wins) with the IPC-2221 width for it
  (power_paths' formula; IPC-2152's charts are not encoded); high-voltage nets with the
  clearance check_hv_clearance's IPC-2221B table asks; RF nets only when an RF part's RF
  pin, an RF net name or an antenna says so (a coax connector alone does not);
- length matching: pairs, DDR byte lanes (DQ, DM and their DQS, one lane per side of
  series resistors) and the address/command group with its own channel's clocks, RGMII
  groups with their clocks and schematic buses of memory or high-speed lines, each with the
  source of its tolerance or none;
- placement: decoupling capacitors per IC and rail (check_decoupling's pairing), at a pin
  only when the schematic draws them at it, else one per supply pin, measured to that pin
  or the IC's nearest pad on the rail; TVS/ESD parts at
  their connectors (check_interfaces' walk), crystal keep-tight groups (crystal, load
  capacitors, series and feedback resistors, the oscillator pins), switching-converter
  keep-tight groups (converter, input capacitors, switch-node inductor and diode,
  bootstrap capacitor, output capacitors, feedback divider; get_power_tree), measured on
  the PcbDoc when it loads;
- functional zones per sheet (digital, power, analog, I/O, filtering, RF) with the basis
  of each label, and the designers' notes that state placement intent, quoted;
- a constraint plan (PLAN_SCHEMA, JSON) the user can edit and pass back.

generate_rule_file turns a plan into a .RUL file, CSV class lists and a Markdown step
list (classic projects: Parameter Set and Differential Pair directives, Design > Classes
and Import Rules; Constraint Manager projects: the Constraint Manager's entries).

The .RUL format. Altium's PCB Rules and Constraints Editor exports and imports *.rul
('Export Rules to File', 'PCB Rule Files (*.rul)' in Advpcb.dll, pcbdlgs.dll and the
Constraint Manager modules). No exported rule file was found to compare with: the only
*.rul files in Altium's install are the router's route.rul (RuleKind=Clearance|All=TRUE|
Scope0=..., a router configuration, not a rule set), and the other *.rul files read so
far are not PcbDoc rule records (other keys and a header line). Advpcb.dll's import path
names 'Invalid RUL file structure!', 'Invalid RUL file!', RuleKind, Name, RULEKIND and
SCOPE1EXPRESSION together: the record keys of the PcbDoc's
Rules6 stream, where each rule is one pipe-delimited KEY=VALUE text record
(|SELECTION=FALSE|LAYER=TOP|...|RULEKIND=Width|...|NAME=...|PRIORITY=1|...). So each rule is
written as that record, one per line (CRLF, ASCII), with the keys, key order and value
forms Altium's own records use: lengths in mil to 4 decimals (its 1/10000 mil unit),
OBJECTCLEARANCES on Clearance records (empty: the generic gap), MAXUNCOUPLEDLENGTH on every
DiffPairsRouting record, IMPEDANCEPROFILEVALUE (6 decimals) beside a profile's ID. Each line
is read back with the PcbDoc's own decoder (parse_text_properties, decode_rules,
pcb_rules.rule_values), every key and value compared, before the file is given. Import
Rules also asks which rule kinds to import and whether to clear the existing rules
(Advpcb.dll: 'Choose Design Rule Type', 'Clear existing rules prior to import?'): the steps
say to pick the file's kinds and answer No. Importing it into Altium is untested, and so is
whether it keeps the priorities written: try Import Rules on a copy of the board first.
"""

import csv
import hashlib
import io
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

try:
    from parsers.pcb_doc import decode_rules, parse_dim_mil, parse_text_properties
    from services.bom import natural_key
    from services.constraints_review import INTERFACE_TARGETS
    from services.decoupling import (
        DEFAULT_MAX_DISTANCE_MIL, component_pads, decoupling_design, pcb_components,
    )
    from services.derating import net_voltages, read_parts
    from services.diff_pair_checks import (
        interface_hint, netlist_pair_candidates, polarity_tokens,
    )
    from services.directive_sync import pair_suffixes
    from services.firmware_pins import classify, is_crystal, part_kind
    from services.hv_clearance import hv_name_reason, ipc2221_mm
    from services.impedance import impedance_profiles
    from services.interfaces import (
        PROCESSOR_TEXT, TVS_AT_CONNECTOR_MIL, audit_connector, bus_transceiver,
    )
    from services.pcb_rules import ScopeSyntaxError, evaluate_scope, parse_scope, rule_values
    from services.power_paths import OZ_MIL, ipc2221_width, rail_currents
    from services.power_tree import build_tree, inductors_on, tree_data
except ImportError:
    from server.parsers.pcb_doc import decode_rules, parse_dim_mil, parse_text_properties
    from server.services.bom import natural_key
    from server.services.constraints_review import INTERFACE_TARGETS
    from server.services.decoupling import (
        DEFAULT_MAX_DISTANCE_MIL, component_pads, decoupling_design, pcb_components,
    )
    from server.services.derating import net_voltages, read_parts
    from server.services.diff_pair_checks import (
        interface_hint, netlist_pair_candidates, polarity_tokens,
    )
    from server.services.directive_sync import pair_suffixes
    from server.services.firmware_pins import classify, is_crystal, part_kind
    from server.services.hv_clearance import hv_name_reason, ipc2221_mm
    from server.services.impedance import impedance_profiles
    from server.services.interfaces import (
        PROCESSOR_TEXT, TVS_AT_CONNECTOR_MIL, audit_connector, bus_transceiver,
    )
    from server.services.pcb_rules import (
        ScopeSyntaxError, evaluate_scope, parse_scope, rule_values,
    )
    from server.services.power_paths import OZ_MIL, ipc2221_width, rail_currents
    from server.services.power_tree import build_tree, inductors_on, tree_data

SECTIONS = ("net_classes", "length_matching", "placement", "zones", "designer_notes", "plan")
PLACEMENT_KINDS = ("decoupling", "tvs", "crystal", "smps")
FORMATS = ("rul", "csv", "steps")
PLAN_SCHEMA = "altium-copilot/constraint-plan/1"
RULE_KINDS = ("Width", "Clearance", "DiffPairsRouting", "MatchedLengths", "Length")
CLASS_KINDS = ("net", "diff_pair")
MIL_PER_MM = 1 / 0.0254
DEFAULT_COPPER_OZ = 1.0
DEFAULT_TEMP_RISE_C = 10.0
HV_MIN_V = 30.0  # above it IPC-2221B's B2 column asks more than its 0-30 V rows (0.1 mm)
CURRENT_BANDS_A = (0.5, 1.0, 2.0, 3.0, 5.0, 10.0, 20.0, 30.0, 50.0, 100.0)
WIDE_TRACK_MIL = 100.0  # an inner track this wide is better a pour: said, not enforced
OPEN_MAX_WIDTH_MIL = 250.0  # a Width rule's upper bound left open: not a requirement
PS_TO_MIL = 5.0  # INTERFACE_TARGETS' DDR4 row: TI's rule of thumb, about 5 mil per ps
LISTED = 30  # members listed per item before the page shrinks them
NOTE_CHARS = 400  # a designer note is quoted up to this
INLINE_CHARS = 40_000  # generate_rule_file's inline text per format without output_dir
CACHE_SIZE = 4
ROUND_TRIP_TOLERANCE_MIL = 0.00005  # half of Altium's 1/10000 mil
TYPICAL_SLACK = 1.05  # a board tolerance this much looser than a typical figure covers it
SMPS_NEAR_MIL = 10 * MIL_PER_MM  # a keep-tight member farther than this is flagged
CRYSTAL_NEAR_MIL = 5 * MIL_PER_MM

UNTESTED_IMPORT = (
    "Importing this .RUL into Altium is untested: the format is the PcbDoc's own rule "
    "record (one per line), read back here with the same decoder, but no file exported by "
    "Altium was available to compare with. Try the import on a copy of the board first, "
    "then check each rule's scope, values and priority."
)
TYPICAL_NOTE = (
    "Impedance and matching figures are typical values from constraints_review's "
    "INTERFACE_TARGETS (vendor layout guides, cited per row), not this design's "
    "requirement: the parts' own datasheets and layout guides decide."
)
WIDTH_SOURCE = (
    "IPC-2221 section 6.2 conductor current capacity, the curve fit I = k x dT^0.44 x "
    "A^0.725 (k 0.048 external, 0.024 internal; check_power_paths' formula). IPC-2152's "
    "measured charts (board thickness, planes) are not encoded and are often less "
    "conservative, mainly inside the board."
)
HV_SOURCE = (
    "IPC-2221B Table 6-1 (check_hv_clearance's table): B2 external uncoated up to 3050 m, "
    "B1 internal; a functional spacing minimum, not safety isolation (IEC 62368-1 / "
    "60664-1 decide that)."
)
DECOUPLING_GUIDE = (
    "Small capacitors (under 1 uF) next to the supply pins, one per pin (at the pin a "
    "capacitor is drawn at, when the schematic draws it at one), on the same side or with "
    "a via at the pad, larger ones near the part; vendor guides ask for 'as close as "
    "possible' (check_decoupling flags more than 5 mm)."
)
TVS_GUIDE = (
    "At the connector, before any series part, within about 2 mm of the pin it protects, "
    "with a short, wide path to ground (check_interfaces' TVS guidance)."
)
CRYSTAL_GUIDE = (
    "Crystal and load capacitors next to the oscillator pins, short and symmetric traces, "
    "no other signal routed between or under them, the load capacitors' ground short to "
    "the part's oscillator ground (MCU vendors' oscillator design guides, e.g. ST AN2867)."
)
SMPS_GUIDE = (
    "Hot loop (input capacitor, converter switch, catch diode or low-side switch) as small "
    "as possible; the inductor next to the switch pin with little switch-node copper; "
    "output capacitors at the inductor; the feedback divider next to the FB pin, its trace "
    "away from the switch node and the inductor (TI AN-1149 / SNVA021, 'Layout guidelines "
    "for switching power supplies')."
)
ZONE_LABELS = ("digital", "power", "analog", "io", "filtering", "rf")


# ---------- typical pair targets ----------

def _row(name: str | None) -> dict | None:
    return next((row for row in INTERFACE_TARGETS if row["interface"] == name), None)


# key -> (class name, INTERFACE_TARGETS row, typical differential ohm, typical in-pair
# tolerance in mil, the impedance as the row gives it, the skew as the row gives it). Every
# number is the row's own (ps converted at PS_TO_MIL); None where the row gives no single
# number.
TYPICAL_PAIRS: dict[str, tuple[str, str | None, float | None, float | None, str, str]] = {
    "usb2": ("DIFF_USB2", "USB 2.0 (high speed)", 90.0, 50.0,
             "90 ohm differential (81-99)",
             "in-pair 50 mil per TI; an Altium article asks tighter (sources disagree)"),
    "usb3": ("DIFF_USB3", "USB 3.x SuperSpeed", 90.0, 5.0,
             "device-specific: 90 ohm +/-7% (AM57xx) or 95 ohm +/-5% (AM65xx)",
             "in-pair 5 mil"),
    "pcie": ("DIFF_PCIE", "PCIe", None, 5.0,
             "100 ohm for Gen1-2, 85 or 100 for Gen3, 85 for Gen4-6 (by generation)",
             "in-pair 5 mil; lane-to-lane loose"),
    "sata": ("DIFF_SATA", "SATA", 100.0, 5.0, "100 ohm differential (85-115)", "in-pair 5 mil"),
    "hdmi": ("DIFF_HDMI", "HDMI (TMDS)", 100.0, 5.0, "100 ohm differential (90-110)",
             "in-pair 5 mil"),
    "displayport": ("DIFF_DP", "DisplayPort", 100.0, 5.0, "100 ohm differential",
                    "in-pair about 5 mil; lane-to-lane within about 2 UI"),
    "ethernet": ("DIFF_ETH", "Ethernet MDI (100BASE-TX / 1000BASE-T)", 100.0, 20.0,
                 "100 ohm +/-10% differential",
                 "in-pair 20 mil at 1G, 50 mil at 100M (ADIN1300 example)"),
    "serdes": ("DIFF_SERDES", "SGMII / multi-gigabit SerDes", 100.0, None,
               "about 100 ohm differential (SGMII 95 ohm +/-5% on AM65xx)",
               "from the FPGA or PHY PCB guide"),
    "ddr4_dqs": ("DIFF_DDR4_DQS", "DDR4", 80.0, 0.4 * PS_TO_MIL,
                 "80 ohm differential, 40 ohm single-ended, +/-10%",
                 "DQS P/N 0.4 ps (about 2 mil at 5 mil/ps)"),
    "ddr4_ck": ("DIFF_DDR4_CK", "DDR4", 80.0, None,
                "80 ohm differential, 40 ohm single-ended, +/-10%",
                "CK to address/control per the controller's limits"),
    "ddr3": ("DIFF_DDR3", "DDR3", None, None, "controller-specific", "controller-specific"),
    "ddr5": ("DIFF_DDR5", "DDR5 / LPDDR4 / LPDDR5", None, None, "controller-specific",
             "controller-specific"),
    "ddr": ("DIFF_DDR", None, None, None, "DDR generation not recognised",
            "the memory controller's guide decides"),
    "lvds": ("DIFF_LVDS", "LVDS", 100.0, 5.0, "100 ohm differential, closely coupled",
             "in-pair under 5 mil (vendor example)"),
    "mipi": ("DIFF_MIPI", "MIPI D-PHY (CSI / DSI)", 100.0, 2.0 * PS_TO_MIL,
             "85-115 ohm differential (AM62x), 95 ohm (AMD), 100 ohm (Intel)",
             "P/N +/-2 ps (AMD; about 10 mil at 5 mil/ps)"),
    "can": ("DIFF_CAN", "CAN", 120.0, None, "120 ohm characteristic impedance",
            "no skew figure; 120 ohm termination at both ends"),
    "rs485": ("DIFF_RS485", "RS-485 / RS-422", 120.0, None, "120 ohm characteristic impedance",
              "stub round-trip delay under 1/10 of the driver rise time"),
    "clock": ("DIFF_CLK", "General (any high-speed pair)", None, None,
              "no generic number: the clock's datasheet and I/O standard",
              "same via count on both legs"),
    "fpga_io": ("DIFF_FPGA_IO", "General (any high-speed pair)", None, None,
                "no generic number: the I/O standard the FPGA bank uses",
                "same via count on both legs"),
    "other": ("DIFF_OTHER", None, None, None, "interface not recognised: no typical target",
              "no typical figure"),
}
INTERFACE_LABELS = {
    "usb2": "USB 2.0", "usb3": "USB 3.x SuperSpeed", "pcie": "PCIe", "sata": "SATA",
    "hdmi": "HDMI / TMDS", "displayport": "DisplayPort", "ethernet": "Ethernet MDI",
    "serdes": "SGMII / SerDes", "ddr4_dqs": "DDR4 strobe", "ddr4_ck": "DDR4 clock",
    "ddr3": "DDR3", "ddr5": "DDR5 / LPDDR", "ddr": "DDR", "lvds": "LVDS",
    "mipi": "MIPI D-PHY", "can": "CAN", "rs485": "RS-485 / RS-422", "clock": "clock",
    "fpga_io": "FPGA differential I/O", "other": "not recognised",
}
DDR4_DQ_TO_DQS_MIL = 2.0 * PS_TO_MIL  # DDR4 row: DQS to DQ 2 ps

# Pair names -> interface, first match wins (USB 2 before PCIe: a mini PCIe slot's USB
# lines are USB).
_PAIR_PATTERNS = (
    ("ddr", re.compile(r"LPDDR|DDR|DQS|SODIMM|DIMM")),
    ("usb", re.compile(r"USB|SSTX|SSRX|SS_TX|SS_RX|SUPERSPEED|(?<![A-Z])SS[TR]X")),
    ("pcie", re.compile(r"PCI_?E|PCIEXPRESS|(?<![A-Z])PE[TR][PN]?\d|(?<![A-Z])HS[OI][PN]")),
    ("sata", re.compile(r"SATA")),
    ("hdmi", re.compile(r"HDMI|TMDS|(?<![A-Z])DVI")),
    ("displayport", re.compile(
        r"DISPLAY_?PORT|(?<![A-Z])E?DP\d*_(?:ML|LANE|AUX|TX\d)|(?<![A-Z])ML_?LANE")),
    ("mipi", re.compile(r"MIPI|(?<![A-Z])(?:CSI|DSI)\d*(?:_|$)|D_?PHY")),
    ("lvds", re.compile(r"LVDS")),
    ("serdes", re.compile(r"SGMII|SERDES|XAUI|SFP|1000BASE_?X|(?<![A-Z])GT[XHPYM]|MGT|XCVR|GXB")),
    ("ethernet", re.compile(
        r"MDI|(?<![A-Z])TRD\d|BI_?D[A-D]|1000BASE_?T|ETHERNET|RJ_?45|(?<![A-Z])TRX[PN]?\d")),
    ("can", re.compile(r"(?<![A-Z])F?D?CAN(?:_?[HL]|\d|_|$)")),
    ("rs485", re.compile(r"RS_?485|RS_?422|(?<![0-9])485")),
    ("clock", re.compile(r"CLK|CLOCK|(?<![A-Z])CK(?:_|\d|$)")),
    ("fpga_io", re.compile(r"(?<![A-Z])IO_L\d+[PN]")),
)
# USB: SuperSpeed by its lane words or a USB 3 name; a D+/D- (D_P, DP/DM) pair is USB 2.0
# even on a USB 3 port.
_USB3 = re.compile(r"SSTX|SSRX|SS_TX|SS_RX|SUPERSPEED|(?<![A-Z])SS[TR]X|USB_?3")
_USB2_LINE = re.compile(r"(?:^|_)(?:D|DATA)$|(?:^|_)D[PMN+\-]$|^D[PMN]$")
_HINTS = {"PCI Express": "pcie", "USB 3 SuperSpeed": "usb3", "SATA": "sata",
          "SGMII / 1000BASE-X / SerDes": "serdes", "Ethernet MDI": "ethernet",
          "HDMI / TMDS": "hdmi", "CAN": "can", "RS-485 / RS-422": "rs485",
          "USB 2.0": "usb2", "clock": "clock"}
_MEMORY_PART = re.compile(r"DDR|SDRAM|DIMM", re.I)
_DDR_GEN = (("ddr5", re.compile(r"DDR5|LPDDR")), ("ddr4", re.compile(r"DDR4")),
            ("ddr3", re.compile(r"DDR3")))

# DDR nets: DQ<n>, DM / DBI / DQM<k>, DQS<k>, and the address/command/control lines. A
# short suffix after the number (DQ0R) names the segment on the other side of series
# resistors: lanes are built per segment, never mixing the two.
_DQ = re.compile(r"^(.*?)DQ(\d{1,2})([A-Z]{0,2})$")
_DM = re.compile(r"^(.*?)(?:DM|DQM|DBI_?N?|DMI)(\d{1,2})(?:_?N)?([A-Z]{0,2})$")
_DQS = re.compile(r"^(.*?)DQS\D{0,3}?(\d{1,2})([A-Z]{0,2})$")
_ADDR = re.compile(r"^(.*?)(?:A\d{1,2}|BA\d|BG\d|RAS_?N?|CAS_?N?|WE_?N?|CS\d?_?N?|S\d_?N?|"
                   r"CKE\d?|ODT\d?|ACT_?N?|PAR(?:ITY)?|ALERT_?N?|C\d)$")
# A DDR clock pair's name: the address/command prefix, then CK or CLK (and a number).
_DDR_CK = re.compile(r"^(.*?)(?:CK|CLK)_?\d{0,2}(?:_?[TC])?$")
_RGMII_TOKENS = re.compile(r"RGMII|RMII|(?<![A-Z])(?:TXD|RXD)\d|TX_?CTL|RX_?CTL|TX_?EN|RX_?DV")
# The (R)GMII clocks a TX or RX group is matched to.
_MII_CLOCK = (("tx", re.compile(r"^(.*?)(?:G?TX_?CLK|TXC|TX_?CK)$")),
              ("rx", re.compile(r"^(.*?)(?:RX_?CLK|RXC|RX_?CK)$")))

_RF_PART = re.compile(r"\bRF\b|antenna|balun|\bLNA\b|power amplifier|RF switch|diplexer|"
                      r"duplexer|\bSAW\b|\bBAW\b|\bGNSS\b|\bGPS\b|transceiver.{0,40}[MG]Hz|"
                      r"[MG]Hz.{0,40}transceiver|Wi-?Fi|WLAN|802\.11|Bluetooth|\bBLE\b|"
                      r"\bLoRa\b|Zigbee", re.I)
_OSC_PIN = re.compile(r"CLK|OSC|XT|XI|XO|^X\d$|^X_?(?:IN|OUT)$|^XC\d$")
_ANTENNA = re.compile(r"antenna|\bANT\b|\bRF\b", re.I)
_RF_PIN = re.compile(r"^(?:RF\w*|ANT\w*|RFIO\w*|RFIN\w*|RFOUT\w*|LNA_?IN|PA_?OUT|TX_?RF|RX_?RF)$",
                     re.I)

_PLACEMENT_WORDS = re.compile(
    r"\bplace|placement|close to|close as|next to|\bnear\b|as near|keep\b|keep[- ]?out|"
    r"away from|\bshort(?:est)?\b|\broute|routing|layout|\bPCB\b|underneath|\bunder\b|"
    r"beneath|guard|isolat|separat|same side|top side|bottom side|\bvias?\b|\bplanes?\b|"
    r"length[- ]match|matched length|impedance|differential|\bohms?\b|clearance|creepage|"
    r"copper|\btrace|\btrack|stitch|thermal relief|heat ?sink|\bzone\b|split", re.I)
_NOTE_KINDS = frozenset({"note", "text_frame", "label", "text"})

# Zones: words in part text.
_ANALOG_TEXT = re.compile(r"op-?amp|operational amplifier|instrumentation amplifier|"
                          r"\bADC\b|\bDAC\b|analog[- ]to|digital[- ]to[- ]analog|comparator|"
                          r"voltage reference|\bshunt reference|current sense amplifier|"
                          r"amplifier|sensor|thermistor|\bcodec\b|audio", re.I)
_DIGITAL_TEXT = re.compile(r"\bFPGA\b|\bCPLD\b|micro-?controller|\bMCU\b|\bMPU\b|processor|"
                           r"\bSoC\b|\bDDR\d?\b|SDRAM|\bSRAM\b|DRAM|flash|EEPROM|\bFRAM\b|"
                           r"\blogic\b|\bbuffer\b|\bgate\b|flip-?flop|\blatch\b|multiplexer|"
                           r"\bmux\b|(?<![A-Z0-9])74[A-Z]{0,5}\d{2,4}|clock (?:buffer|"
                           r"generator|synthesi)|\bPLL\b", re.I)
_MEMORY_TEXT = re.compile(r"\bDDR\d?\b|SDRAM|\bSRAM\b|DRAM|flash|EEPROM|\bFRAM\b",
                          re.I)
_IO_TEXT = re.compile(r"transceiver|\bPHY\b|ethernet|\bUSB\b|\bCAN\b|RS-?485|RS-?232|"
                      r"level (?:shift|translat)|isolator|magnetics|\bHDMI\b|redriver|"
                      r"retimer", re.I)
_FILTER_TEXT = re.compile(r"ferrite|\bbead\b|common[- ]?mode|\bchoke\b|EMI filter|"
                          r"\bfilter\b|feed-?through", re.I)
_SHEET_WORDS = (
    ("power", re.compile(r"POWER|PWR|PSU|SUPPLY|REG|DCDC|DC_DC|LDO|BUCK|BOOST|\dV\d|"
                         r"\d+V_TO|_TO_\d|BATT|CHARG", re.I)),
    ("rf", re.compile(r"(?<![A-Z])RF|ANTENNA|RADIO|WIFI|WI-FI|BLUETOOTH|BLE|GNSS|GPS|LORA",
                      re.I)),
    ("analog", re.compile(r"ANALOG|AUDIO|SENS|ADC|DAC|AMP|MEASURE", re.I)),
    ("io", re.compile(r"CONN|USB|ETH|RJ45|HDMI|DISPLAY|PCIE|SFP|JTAG|IO\b|I_O|INTERFACE|"
                      r"CAN|RS485|RS232|UART|PORT", re.I)),
    ("filtering", re.compile(r"FILTER|EMI|EMC", re.I)),
    ("digital", re.compile(r"MCU|CPU|FPGA|DDR|MEM|PROCESSOR|SOC|LOGIC|CLOCK|PLL|HPS|CORE|"
                           r"CONTROLLER", re.I)),
)


# ---------- small helpers ----------

def _r(value: float | None, digits: int = 3) -> float | None:
    return None if value is None else round(value, digits)


def _names(items, limit: int = 6) -> str:
    items = list(items)
    shown = ", ".join(str(item) for item in items[:limit])
    return shown + (f" and {len(items) - limit} more" if len(items) > limit else "")


def _ceil(value: float, step: float) -> float:
    return math.ceil(value / step - 1e-9) * step


def _mil_text(mil: float, units: str) -> str:
    value = mil / MIL_PER_MM if units == "mm" else mil
    digits = 4 if units == "mm" else 3
    text = f"{value:.{digits}f}".rstrip("0").rstrip(".")
    return f"{text or '0'} {units}"


def _upper(name: str | None) -> str:
    return (name or "").replace("\\", "").upper()


def _base_name(positive: str, negative: str) -> str:
    """A pair's name from its legs: the positive net without its polarity token (USB_D_P /
    USB_D_N -> USB_D)."""
    keys = {key for key, polarity, _ in polarity_tokens(negative) if polarity == "-"}
    for key, polarity, _ in polarity_tokens(positive):
        if polarity != "+" or key not in keys:
            continue
        head, tail = key.split("{}", 1)
        name = positive[:len(head)] + (positive[len(positive) - len(tail):] if tail else "")
        name = re.sub(r"[_\-.]{2,}", "_", name).strip("_-.")
        return name or positive
    return positive


class _Pcb:
    """Pad lookups and pad-to-pad distances on the PcbDoc (schematic designators)."""

    def __init__(self, index):
        self.index = index
        self.by_name = pcb_components(index)
        self._pads: dict[str, list] = {}

    def preload(self, refs) -> None:
        wanted = [ref for ref in dict.fromkeys(refs) if ref not in self._pads]
        if not wanted:
            return
        pads = component_pads(self.index, self.by_name, wanted)
        for ref in wanted:
            self._pads[ref] = [pad for row in self.by_name.get(ref, [])
                               for pad in pads.get(row["id"], [])]

    def placed(self, ref: str) -> bool:
        return bool(self.by_name.get(ref))

    def side(self, ref: str) -> str | None:
        rows = self.by_name.get(ref) or []
        return rows[0]["side"] if rows else None

    def pads(self, ref: str, net: str | None = None, pins=None) -> list:
        if ref not in self._pads:
            self.preload([ref])
        out = self._pads[ref]
        if net is not None:
            out = [pad for pad in out if pad["net_name"] == net]
        if pins:
            named = [pad for pad in out if pad["pad_name"] in set(pins)]
            out = named or out
        return out

    def distance(self, first: list, second: list) -> float | None:
        if not first or not second:
            return None
        return min(self.index._primitive_distance(a, b) for a in first for b in second)


# ---------- assumptions ----------

@dataclass
class Assumptions:
    """What the brief assumes where the files say nothing; every one is echoed back."""

    copper_oz: float | dict | None = None  # power_paths.parse_copper_oz's value
    temp_rise_c: float = DEFAULT_TEMP_RISE_C
    current: dict[str, float] | None = None  # power_paths.parse_currents' value
    hv: dict[str, tuple] | None = None  # hv_clearance.parse_hv's value
    units: str = "mm"


def _thickness(index, copper_oz, notes: list[str] | None = None) -> dict:
    """Copper thickness (mil) of the outer and inner signal layers: copper_oz (one value,
    'outer' / 'inner', or per layer name as check_power_paths takes it; the thinnest given
    layer of each kind), else the stack's thinnest of each kind, else 1 oz; with the
    source."""
    sources = {"copper_oz": "copper_oz",
               "stack": "the PcbDoc's layer stack (thinnest such layer)",
               "assumed": "assumed 1 oz (35 um)"}
    # Per kind: (thickness in mil, source) of each signal layer.
    per_layer: dict[str, list[tuple[float, str]]] = {"outer": [], "inner": []}
    known = {"outer", "inner"}
    by_name = copper_oz if isinstance(copper_oz, dict) else {}
    if index is not None:
        try:
            rows = {row["v6_id"]: row["copper_thick_mil"] for row in index.db.execute(
                "SELECT v6_id, copper_thick_mil FROM layers WHERE kind = 'copper'")}
            signal = index.signal_layers()
            order = list(signal)
            for layer, name in signal.items():
                kind = "outer" if layer in (order[0], order[-1]) else "inner"
                known.add(name.casefold())
                if isinstance(copper_oz, (int, float)):
                    per_layer[kind].append((float(copper_oz) * OZ_MIL, "copper_oz"))
                elif (given := by_name.get(name.casefold(), by_name.get(kind))) is not None:
                    per_layer[kind].append((given * OZ_MIL, "copper_oz"))
                elif rows.get(layer):
                    per_layer[kind].append((float(rows[layer]), "stack"))
        except Exception:  # the stack is an extra source, never a failure
            pass
    if by_name and notes is not None:
        idle = sorted(name for name in by_name if name not in known)
        if idle:
            notes.append(f"copper_oz names no signal layer {idle}"
                         + (" (no PcbDoc was read, so only 'outer' and 'inner' apply)"
                            if index is None else "") + ": ignored.")
    out = {}
    for kind in ("outer", "inner"):
        if not per_layer[kind]:
            given = float(copper_oz) if isinstance(copper_oz, (int, float)) else \
                by_name.get(kind)
            if given is not None:
                per_layer[kind].append((given * OZ_MIL, "copper_oz"))
        if per_layer[kind]:
            value, source = min(per_layer[kind])
            out[kind] = {"thickness_mil": round(value, 3), "oz": round(value / OZ_MIL, 2),
                         "source": sources[source]}
        else:
            out[kind] = {"thickness_mil": round(DEFAULT_COPPER_OZ * OZ_MIL, 3),
                         "oz": DEFAULT_COPPER_OZ, "source": sources["assumed"]}
    return out


# ---------- the design as the brief reads it ----------

class _Design:
    """The readings the sections share: the power tree (with check_interfaces' part
    context), check_decoupling's design, sheets and schematic positions."""

    def __init__(self, netlist: dict, plan, project_params, compiled, index):
        self.netlist = netlist
        self.plan = plan
        self.params = project_params or {}
        self.tree = build_tree(netlist, plan, self.params)
        self.data = tree_data(self.tree)
        self.ctx = self.tree.ctx
        self.dec = decoupling_design(netlist, plan, self.params)
        self.components = netlist.get("components") or {}
        self.nets: dict[str, list[tuple[str, str]]] = self.ctx.design.nets
        self.compiled = compiled
        self.index = index
        self.pcb = _Pcb(index) if index is not None else None
        self.pin_pos: dict[tuple[str, str], tuple] = {}
        self.part_pos: dict[str, list[tuple]] = defaultdict(list)
        erc = getattr(compiled, "erc", None)
        if erc is not None:
            for net in erc.nets:
                for obj in net.objects:
                    if obj.kind == "pin" and len(obj.key) == 2:
                        self.pin_pos.setdefault((str(obj.key[0]), str(obj.key[1])),
                                                (obj.sheet, obj.copy, obj.x_mil, obj.y_mil))
            for part in erc.parts:
                if part.physical:
                    self.part_pos[part.physical].append(
                        (part.sheet, part.copy, part.x_mil, part.y_mil))

        self._connectors: dict = {}
        self._volts: dict | None = None

    def connector(self, ref: str):
        """check_interfaces' audit of a connector (memoised)."""
        if ref not in self._connectors:
            self._connectors[ref] = audit_connector(self.ctx, ref)
        return self._connectors[ref]

    def ddr_generation(self) -> str | None:
        """ddr5, ddr4 or ddr3 from the nets' names or the parts' text (memory parts, SODIMM
        sockets), the newest named; None when none is named."""
        if not hasattr(self, "_ddr"):
            names = " ".join(_upper(net) for net in self.nets)
            texts = " ".join(self.text(ref) for ref in self.ctx.kind).upper()
            self._ddr = next((gen for gen, pattern in _DDR_GEN
                              if pattern.search(names) or pattern.search(texts)), None)
        return self._ddr

    def volts(self) -> dict:
        """Net voltages as check_derating reads them (memoised): {net: Volt}."""
        if self._volts is None:
            parts, _, _ = read_parts(self.tree, self.netlist, self.plan)
            self._volts, _ = net_voltages(self.tree, self.data, None, parts)
        return self._volts

    def sheet(self, ref: str) -> str | None:
        return (self.components.get(ref) or {}).get("sheet") or None

    def sheets_of(self, refs) -> list[str]:
        return sorted({sheet for ref in refs if (sheet := self.sheet(ref))}, key=natural_key)

    def text(self, ref: str) -> str:
        return self.ctx.text.get(ref, "")

    def kind(self, ref: str) -> str | None:
        return self.ctx.kind.get(ref)

    def fitted(self, ref: str) -> bool:
        return self.ctx.fitted.get(ref, True)

    def pin_name(self, ref: str, pin: str) -> str:
        return self.ctx.design.pin_name(ref, pin) or ""

    def pins_on(self, ref: str, net: str) -> list[str]:
        return [pin for pin, other in (self.ctx.design.part_pins.get(ref) or {}).items()
                if other == net]

    def net_parts(self, net: str) -> list[str]:
        return list(dict.fromkeys(ref for ref, _ in self.nets.get(net, ())))

    def grounds(self) -> set[str]:
        return self.tree.grounds

    def points(self, ref: str, pins=None) -> list[tuple]:
        """Schematic positions of a part's pins (or of the part when none is known)."""
        found = [pos for pin in (pins or []) if (pos := self.pin_pos.get((ref, pin)))]
        return found or list(self.part_pos.get(ref, ()))

    @staticmethod
    def nearest(first: list[tuple], second: list[tuple]) -> float | None:
        best = None
        for a in first:
            for b in second:
                if a[0] == b[0] and a[1] == b[1]:
                    distance = math.hypot(a[2] - b[2], a[3] - b[3])
                    if best is None or distance < best:
                        best = distance
        return best


# ---------- differential pairs ----------

@dataclass
class _Pair:
    name: str
    positive: str
    negative: str
    sources: list[str] = field(default_factory=list)
    interface: str = "other"
    basis: list[str] = field(default_factory=list)
    sheets: list[str] = field(default_factory=list)

    def legs(self) -> tuple[str, str]:
        return self.positive, self.negative

    def member(self) -> dict:
        return {"pair": self.name, "positive": self.positive, "negative": self.negative}


def _directive_nets(compiled) -> dict:
    """Schematic directives by kind: {"class": {net: {class names}}, "pair": {nets with
    DifferentialPair=True}, "rules": count of Rule parameters}."""
    found = {"class": defaultdict(set), "pair": set(), "rules": 0, "pair_class": defaultdict(set)}
    erc = getattr(compiled, "erc", None)
    for item in getattr(erc, "parameter_sets", None) or []:
        params = {name.strip().lower(): value for name, value in item.parameters}
        pair = str(params.get("differentialpair", "")).strip().lower() in (
            "true", "t", "1", "-1", "yes")
        name = (params.get("classname") or "").strip()
        for net in item.nets:
            if pair:
                found["pair"].add(net)
                if name:
                    found["pair_class"][net].add(name)
            elif name:
                found["class"][net].add(name)
        found["rules"] += sum(1 for key in params if key.startswith("rule"))
    return found


def _classify_pair(design: _Design, pair: _Pair) -> None:
    names = [pair.name, pair.positive, pair.negative]
    blob = " ".join(_upper(name) for name in names)
    key = next((key for key, pattern in _PAIR_PATTERNS if pattern.search(blob)), None)
    if key and key != "clock":
        pair.basis.append(f"pair naming ({pair.positive} / {pair.negative})")
    pin_names, texts, roles, magnetics = [], [], [], []
    for net in pair.legs():
        for ref, pin in design.nets.get(net, ()):
            if design.kind(ref) == "transformer":
                magnetics.append(ref)
            if design.kind(ref) not in ("ic", "other", "connector", "transistor"):
                continue
            name = design.pin_name(ref, pin)
            if name:
                pin_names.append(name)
            texts.append(design.text(ref))
            found = classify(name, pin=True) if name else None
            if found and found[0] in ("usb", "memory_bus", "ethernet", "high_speed"):
                roles.append((ref, name, found[0]))
    if (key is None or key == "clock") and roles:
        for ref, name, function in roles:
            if key == "clock" and function != "memory_bus":
                continue  # a clock stays a clock, unless it is a memory's
            role_key = {"usb": "usb", "memory_bus": "ddr", "ethernet": "ethernet"}.get(
                function) or next((k for k, pattern in _PAIR_PATTERNS
                                   if k != "clock" and pattern.search(_upper(name))), None)
            if role_key:
                key = role_key
                pair.basis.append(f"processor pin role: {ref} {name} ({function})")
                break
    if key is None and magnetics:
        key = "ethernet"
        pair.basis.append(f"its legs reach magnetics ({_names(sorted(set(magnetics)), 2)})")
    if key == "clock" and _MEMORY_PART.search(" ".join(texts)):
        key = "ddr"
        pair.basis.append("its legs reach a memory part")
    if key is None:
        hint = interface_hint(names, pin_names, " ".join(texts)[:2000])
        key = _HINTS.get(hint.get("interface") or "")
        if key:
            pair.basis.append(f"pin names or part text ({hint['interface']})")
    if key == "usb":
        pins = " ".join(_upper(name) for name in pin_names)
        key = "usb2" if _USB2_LINE.search(_upper(pair.name)) or not (
            _USB3.search(blob) or _USB3.search(pins)) else "usb3"
    if key == "clock":
        pair.basis.append(f"clock naming ({pair.positive} / {pair.negative})")
    if key == "ddr":
        all_text = blob + " " + " ".join(texts).upper()
        generation = next((gen for gen, pattern in _DDR_GEN if pattern.search(all_text)), None)
        where = "its net names or the memory part's text"
        if generation is None and (generation := design.ddr_generation()):
            where = "the design's net names or memory parts"
        if generation == "ddr4":
            key = "ddr4_dqs" if "DQS" in blob else "ddr4_ck"
        elif generation:
            key = generation
        if generation:
            pair.basis.append(f"{generation.upper()} in {where}")
    pair.interface = key or "other"
    if pair.interface == "other" and not pair.basis:
        pair.basis.append(f"pair naming only ({pair.positive} / {pair.negative})")


def _collect_pairs(design: _Design, options, directives: dict) -> list[_Pair]:
    by_legs: dict[frozenset, _Pair] = {}
    folded = {net.lower(): net for net in design.nets}
    if design.index is not None:
        for pcb_pair in design.index.pcb.diff_pairs:
            legs = frozenset((pcb_pair.positive_net.lower(), pcb_pair.negative_net.lower()))
            # The legs under their netlist names (the PCB may spell them in another case).
            positive = folded.get(pcb_pair.positive_net.lower(), pcb_pair.positive_net)
            negative = folded.get(pcb_pair.negative_net.lower(), pcb_pair.negative_net)
            pair = by_legs.setdefault(legs, _Pair(pcb_pair.name, positive, negative))
            pair.sources.append("pcb_pair")
    suffixes = pair_suffixes(options)
    pair_nets = sorted(directives["pair"], key=natural_key)
    for net in pair_nets:
        for positive, negative in suffixes:
            if net.upper().endswith(positive.upper()) and len(net) > len(positive):
                base = net[:-len(positive)]
                partner = folded.get((base + negative).lower())
                if partner and partner in directives["pair"]:
                    legs = frozenset((net.lower(), partner.lower()))
                    pair = by_legs.setdefault(legs, _Pair(base, net, partner))
                    if "directive" not in pair.sources:
                        pair.sources.append("directive")
    declared = design.index.pcb.diff_pairs if design.index is not None else []
    for candidate in netlist_pair_candidates(design.netlist, declared):
        if candidate["status"] not in ("undeclared", "declared", "declared_inverted"):
            continue
        if (candidate.get("details") or {}).get("load_or_actuator"):
            continue
        legs = frozenset((candidate["positive"].lower(), candidate["negative"].lower()))
        if legs not in by_legs:
            by_legs[legs] = _Pair(_base_name(candidate["positive"], candidate["negative"]),
                                  candidate["positive"], candidate["negative"])
        if "naming" not in by_legs[legs].sources:
            by_legs[legs].sources.append("naming")
    pairs = sorted(by_legs.values(), key=lambda pair: natural_key(pair.name))
    for pair in pairs:
        _classify_pair(design, pair)
        refs = [ref for net in pair.legs() for ref in design.net_parts(net)]
        pair.sheets = design.sheets_of(refs)
    return pairs


# ---------- PCB coverage ----------

class _Coverage:
    """What the PcbDoc already holds for a group of nets or pairs: classes, pairs and the
    rules (not scoped All) whose scope takes them."""

    def __init__(self, index):
        self.index = index
        self.rules: dict[str, list] = defaultdict(list)
        if index is not None:
            for rule in index.pcb.rules:
                if rule.enabled:
                    self.rules[rule.kind].append(rule)
            for rules in self.rules.values():
                rules.sort(key=lambda rule: rule.priority)

    def classes(self, kind: str, members: list[str]) -> dict:
        """PCB classes of a kind ('net' or 'diff_pair') holding these members: exact (same
        set), containing (a superset), and one with the same name is checked by caller."""
        if self.index is None:
            return {}
        wanted = {member.lower() for member in members}
        exact, containing = [], []
        for pcb_class in self.index.pcb.classes:
            if pcb_class.kind != kind or pcb_class.superclass:
                continue
            held = {member.lower() for member in pcb_class.members}
            if held == wanted:
                exact.append(pcb_class.name)
            elif wanted and wanted <= held:
                containing.append(pcb_class.name)
        out = {}
        if exact:
            out["same_members"] = exact[:5]
        if containing:
            out["within"] = containing[:5]
        return out

    def class_named(self, kind: str, name: str):
        if self.index is None:
            return None
        return next((c for c in self.index.pcb.classes if c.kind == kind
                     and not c.superclass and c.name.lower() == name.lower()), None)

    def values(self, rule) -> dict:
        try:
            return rule_values(rule, self.index.signal_layers())
        except Exception:  # an unreadable rule has no values to judge
            return {}

    def rules_for(self, kind: str, targets: list[tuple[str, str]], accept=None) -> dict | None:
        """Rules of a kind not scoped All whose scope takes each target (name, 'net' or
        'diff_pair'): {"rules": names, "covered": n, "of": m}, or None with no PcbDoc. A
        scope through xSignals (InxSignalClass) takes a net its xSignals run through
        ("via_xsignal" counts those). accept(rule) gives None when the rule's values and
        mode satisfy the proposal, else why not: such a rule does not cover the target and
        is listed under "weaker" with the reason."""
        if self.index is None:
            return None
        rules = self.rules.get(kind, [])
        ctx = self.index.rule_context
        names: Counter = Counter()
        weaker: dict[str, str] = {}
        covered = via_xsignal = 0
        verdicts: dict[str, str | None] = {}
        for name, target_kind in targets:
            target = self.index.rule_target(name, kind=target_kind)
            if target is None:
                continue
            hit, through_xsignal = [], False
            for rule in rules:
                for scope in (rule.scope1, rule.scope2) if kind == "Clearance" else (
                        rule.scope1,):
                    if scope.strip().lower() == "all":
                        continue
                    try:
                        matches = evaluate_scope(scope, target, ctx)["matches"]
                    except Exception:  # a scope the evaluator cannot read covers nothing
                        continue
                    xsignal = matches == "conditional" and "xsignal" in scope.lower()
                    if matches != "yes" and not xsignal:
                        continue
                    if rule.name not in verdicts:
                        verdicts[rule.name] = accept(rule, xsignal) if accept else None
                    if verdicts[rule.name] is None:
                        hit.append(rule.name)
                        through_xsignal = through_xsignal or xsignal
                    else:
                        weaker[rule.name] = verdicts[rule.name]
                    break
            if hit:
                covered += 1
                via_xsignal += through_xsignal
                names.update(dict.fromkeys(hit, 1))
        out = {"rules": [name for name, _ in names.most_common(5)], "covered": covered,
               "of": len(targets)}
        if via_xsignal:
            out["via_xsignal"] = via_xsignal
        if weaker:
            out["weaker"] = [{"rule": name, "why": why} for name, why in list(weaker.items())[:5]]
        return out

    def found(self, kind: str, targets: list[tuple[str, str]], accept=None) -> dict | None:
        """rules_for, None when no rule takes any target (covering or weaker)."""
        rules = self.rules_for(kind, targets, accept)
        return rules if rules and (rules["covered"] or rules.get("weaker")) else None

    def outer_names(self) -> set[str]:
        try:
            names = list(self.index.signal_layers().values())
        except Exception:  # no readable stack: every layer counts as outer
            return set()
        return {names[0], names[-1]} if names else set()

    def width_accept(self, outer_mil: float, inner_mil: float, amps: float):
        """A Width rule covers a power class when its minimum is at least the IPC width on
        every layer (outer and inner)."""
        outer_names = self.outer_names()
        try:
            inner_layers = len(self.index.signal_layers()) > 2
        except Exception:  # no readable stack: judged as outer layers only
            inner_layers = False

        def accept(rule, _xsignal) -> str | None:
            values = self.values(rule)
            layers = values.get("layers_mil") or {}
            if not layers and values.get("min_width_mil") is not None:
                # One width for every layer: it must also do inside.
                layers = {None: {"min_width": values["min_width_mil"],
                                 "max_width": values.get("max_width_mil")}}
            short = []
            for layer, fields in layers.items():
                if layer is None:
                    need = inner_mil if inner_layers else outer_mil
                else:
                    need = outer_mil if not outer_names or layer in outer_names else inner_mil
                least, most = fields.get("min_width"), fields.get("max_width")
                if least is None or least < need - 0.05 or (most is not None and most < need):
                    short.append((layer, least, most, need))
            if not short:
                return None
            layer, least, most, need = short[0]
            where = f" on {layer}" if layer else ""
            return (f"allows {least if least is not None else '?'}-"
                    f"{most if most is not None else '?'} mil{where}; IPC-2221 asks at least "
                    f"{need:g} mil for {amps:g} A"
                    + (f" ({len(short)} layers short)" if len(short) > 1 else ""))
        return accept

    def clearance_accept(self, need_mil: float, peak: float):
        def accept(rule, _xsignal) -> str | None:
            gap = self.values(rule).get("clearance_mil")
            if gap is not None and gap >= need_mil - 0.01:
                return None
            return (f"clearance {gap if gap is not None else '?'} mil; IPC-2221B asks "
                    f"{need_mil:g} mil at {peak:g} V")
        return accept

    def matched_accept(self, within_pair: bool, tolerance_mil: float | None):
        """In-pair proposals need a rule matching within each pair; group proposals one
        matching nets, pairs or xSignals against each other; and a tolerance no looser."""
        def accept(rule, xsignal) -> str | None:
            values = self.values(rule)
            group = [values.get("group_diff_pairs"), values.get("group_xsignals"),
                     values.get("group_other_nets")]
            if within_pair and not values.get("within_pair"):
                return "matches lengths across its group, not within each pair"
            if not within_pair and not any(group):
                return "matches within each pair only, not the group"
            if xsignal and not within_pair and not values.get("group_xsignals"):
                return "takes these nets through xSignals but does not match xSignals"
            have = values.get("tolerance_mil")
            # The figure is typical (a rule of thumb): 5 % looser still counts as covered.
            if tolerance_mil is not None and (
                    have is None or have > tolerance_mil * TYPICAL_SLACK + 0.01):
                return (f"tolerance {have if have is not None else '?'} mil, looser than the "
                        f"typical {tolerance_mil:g} mil")
            return None
        return accept

    def pair_routing_accept(self, profile: dict | None):
        """A DiffPairsRouting rule covers a pair class when it is driven by an impedance
        profile with the typical target, or (not driven) its preferred width and gap are
        those the board's matching profile solves; with no such profile its values are not
        judged."""
        solved = None
        if profile is not None:
            rows = [row for row in profile["layers"] if row.get("width_mil")
                    and row.get("gap_mil")]
            solved = {row["layer"].lower(): row for row in rows}
        ids = {}
        try:
            ids = {p.id.upper(): p for p in self.index.pcb.board.impedance_profiles or []}
        except Exception:  # no profiles read
            pass

        def accept(rule, _xsignal) -> str | None:
            props = rule.properties
            if profile is None:
                return None
            if props.get("IMPEDANCEPROFILEDRIVEN", "").strip().upper() == "TRUE":
                used = ids.get(props.get("IMPEDANCEPROFILEID", "").strip().upper())
                if used is not None and used.target_ohm is not None and \
                        abs(used.target_ohm - profile["target_ohm"]) <= 0.5:
                    return None
                return (f"driven by another impedance profile"
                        + (f" ({used.target_ohm:g} ohm)" if used is not None
                           and used.target_ohm is not None else ""))
            layers = {name.lower(): fields for name, fields in
                      (self.values(rule).get("layers_mil") or {}).items()}
            for layer, row in (solved or {}).items():
                fields = layers.get(layer)
                if not fields:
                    continue
                width, gap = fields.get("preferred_width"), fields.get("preferred_gap")
                if width is None or gap is None or abs(width - row["width_mil"]) > 0.1 or \
                        abs(gap - row["gap_mil"]) > 0.1:
                    return (f"width {width} / gap {gap} mil on {row['layer']}; the board's "
                            f"profile "
                            f"{profile['name']} solves {row['width_mil']:g} / "
                            f"{row['gap_mil']:g} mil")
            return None
        return accept

    def default_width(self) -> dict | None:
        """The lowest-priority Width rule scoped All: its minimum width (mil)."""
        if self.index is None:
            return None
        rules = [rule for rule in self.rules.get("Width", [])
                 if rule.scope1.strip().lower() == "all"]
        if not rules:
            return None
        rule = rules[-1]
        values = rule_values(rule, self.index.signal_layers())
        layers = values.get("layers_mil") or {}
        mins = [fields["min_width"] for fields in layers.values() if "min_width" in fields]
        least = min(mins) if mins else values.get("min_width_mil")
        if least is None:
            return None
        return {"rule": rule.name, "min_width_mil": least}


# ---------- net classes ----------

def _pair_classes(design: _Design, pairs: list[_Pair], coverage: _Coverage,
                  directives: dict, profiles: list[dict]) -> list[dict]:
    groups: dict[str, list[_Pair]] = defaultdict(list)
    for pair in pairs:
        groups[pair.interface].append(pair)
    out = []
    for key, members in groups.items():
        class_name, row_name, ohm, in_pair, impedance, skew = TYPICAL_PAIRS[key]
        row = _row(row_name)
        entry: dict = {
            "name": class_name, "kind": "diff_pair", "interface": INTERFACE_LABELS[key],
            "pairs": [pair.member() for pair in members],
            "members_total": len(members),
            "evidence": list(dict.fromkeys(basis for pair in members for basis in pair.basis))[:8],
            "sources": dict(Counter(source for pair in members for source in pair.sources)),
            "sheets": sorted({sheet for pair in members for sheet in pair.sheets},
                             key=natural_key),
        }
        entry["typical"] = {"impedance": impedance, "skew": skew, "differential_ohm": ohm,
                            "in_pair_tolerance_mil": in_pair, "typical_only": True}
        if row is not None:
            entry["typical"].update({"row": row["interface"], "source": row["source"]})
        matched = []
        if ohm is not None:
            matched = [profile for profile in profiles if profile["type"] == "differential"
                       and profile.get("target_ohm") and abs(profile["target_ohm"] - ohm) <= 0.5]
        exists: dict = {}
        if design.index is not None:
            declared = sum(1 for pair in members if "pcb_pair" in pair.sources)
            exists["pcb_pairs"] = f"{declared} of {len(members)} declared on the PCB"
            pcb_names = [pair.name for pair in members if "pcb_pair" in pair.sources]
            if pcb_names:
                exists.update(coverage.classes("diff_pair", pcb_names))
                accepts = {"DiffPairsRouting": coverage.pair_routing_accept(
                               matched[0] if matched else None),
                           "MatchedLengths": coverage.matched_accept(True, in_pair)}
                for kind, accept in accepts.items():
                    rules = coverage.found(kind, [(name, "diff_pair") for name in pcb_names],
                                           accept)
                    if rules:
                        if kind == "DiffPairsRouting" and not matched:
                            rules["values_not_checked"] = (
                                "no impedance profile on the board solves the typical "
                                "impedance, so the rule's width and gap are not judged")
                        exists.setdefault("pcb_rules", {})[kind] = rules
        directive = sum(1 for pair in members if "directive" in pair.sources)
        if directive:
            exists["directive_pairs"] = f"{directive} of {len(members)} have Differential " \
                                        "Pair directives"
        names = {name for pair in members for net in pair.legs()
                 for name in directives["pair_class"].get(net, ())}
        if names:
            exists["directive_pair_classes"] = sorted(names)[:5]
        entry["exists"] = exists
        if matched:
            entry["impedance_profile"] = matched[0]["name"]
        out.append(entry)
    out.sort(key=lambda entry: (entry["name"] in ("DIFF_OTHER", "DIFF_CLK", "DIFF_FPGA_IO"),
                                natural_key(entry["name"])))
    return out


def _band(amps: float) -> float:
    return next((band for band in CURRENT_BANDS_A if amps <= band + 1e-9),
                float(math.ceil(amps)))


def _band_name(band: float) -> str:
    if band < 1:
        return f"PWR_0A{int(round(band * 10))}"
    return f"PWR_{band:g}A"


def _power_classes(design: _Design, assumptions: Assumptions, thickness: dict,
                   coverage: _Coverage, needs: list[dict], notes: list[str]) -> list[dict]:
    rows = {row["rail"]: row for row in design.data.get("rails") or []}
    made = {out.net for conv in design.tree.converters.values() if conv.fitted
            for out in conv.outputs}
    fed = {net for conv in design.tree.converters.values() if conv.fitted
           for net in conv.inputs}
    sources = {source.net for source in design.tree.sources}
    rails = []
    for rail, row in rows.items():
        if rail in design.grounds() or row.get("reference_inputs_only"):
            continue
        if row.get("load_count") or rail in made or rail in fed or rail in sources:
            rails.append(rail)
    rails.sort(key=natural_key)
    if not rails:
        return []
    found = rail_currents(design.tree, design.netlist, design.plan, data=design.data,
                          current=assumptions.current, nets=rails)
    notes += found["notes"]
    currents = found["currents"]
    rise = assumptions.temp_rise_c
    outer, inner = thickness["outer"]["thickness_mil"], thickness["inner"]["thickness_mil"]
    banded: dict[float, list[str]] = defaultdict(list)
    unknown: list[str] = []
    detail: dict[str, dict] = {}
    for rail in rails:
        current = currents.get(rail)
        if current is None or current.amps is None or current.amps <= 0:
            unknown.append(rail)
            continue
        banded[_band(current.amps)].append(rail)
        detail[rail] = {"rail": rail, "amps": _r(current.amps), "source": current.source,
                        **({"lower_bound": True} if current.partial else {})}
    default = coverage.default_width()
    out = []
    for band in sorted(banded):
        members = banded[band]
        width_outer = _ceil(ipc2221_width(band, rise, outer, True), 0.1)
        width_inner = _ceil(ipc2221_width(band, rise, inner, False), 0.1)
        entry = {
            "name": _band_name(band), "kind": "net", "role": "power",
            "members": members, "members_total": len(members),
            "currents": [detail[rail] for rail in members][:LISTED],
            "evidence": [f"power-tree rails carrying up to {band:g} A "
                         "(check_power_paths' current estimate)"],
            "width": {"current_a": band, "temp_rise_c": rise,
                      "outer_min_width_mil": _r(width_outer, 1),
                      "inner_min_width_mil": _r(width_inner, 1),
                      "outer_copper": thickness["outer"], "inner_copper": thickness["inner"],
                      "source": WIDTH_SOURCE},
            "sheets": design.sheets_of(ref for rail in members for ref in design.net_parts(rail)),
        }
        if width_inner > WIDE_TRACK_MIL:
            entry["width"]["inner_note"] = (
                f"An inner-layer track would need {width_inner:.0f} mil for {band:g} A: a "
                "pour or plane (or outer-layer routing) is the practical way there.")
        if any(detail[rail].get("lower_bound") for rail in members):
            entry["width"]["note"] = ("Some currents are sums of the known loads only (a "
                                      "lower bound): give current= to size them.")
        exists = coverage.classes("net", members)
        rules = coverage.found("Width", [(rail, "net") for rail in members],
                               coverage.width_accept(width_outer, width_inner, band)
                               if design.index is not None else None)
        if rules:
            exists["pcb_rules"] = {"Width": rules}
        if default and default["min_width_mil"] >= max(width_outer, width_inner) - 1e-6:
            exists["default_width_enough"] = (
                f"the board's Width rule {default['rule']} (scope All) already asks at least "
                f"{default['min_width_mil']:g} mil")
        entry["exists"] = exists
        out.append(entry)
    if unknown:
        out.append({
            "name": "PWR", "kind": "net", "role": "power", "members": unknown,
            "members_total": len(unknown),
            "evidence": ["power-tree rails (converter inputs and outputs, sources, rails with "
                         "loads)"],
            "width": None,
            "current_unknown": True,
            "sheets": design.sheets_of(ref for rail in unknown for ref in design.net_parts(rail)),
            "exists": coverage.classes("net", unknown),
        })
        # No width to judge: the board's Width rules that take these rails are listed.
        rules = coverage.found("Width", [(rail, "net") for rail in unknown])
        if rules:
            rules["values_not_checked"] = "no current is known, so no width is asked"
            out[-1]["exists"]["pcb_rules"] = {"Width": rules}
        needs.append({"item": "PWR", "needs": "rail currents",
                      "why": f"{len(unknown)} power rails have no known current (no "
                             "current= value, Power Analyzer setup or current parameter), so "
                             "no width is proposed for them",
                      "how": "get_constraints_brief(current='RAIL=amps,U5=0.3') as "
                             "check_power_paths takes it"})
    return out


def _hv_band(volts: float) -> tuple[float, str]:
    for limit in (50.0, 100.0, 150.0, 170.0, 250.0, 300.0, 500.0):
        if volts <= limit:
            return limit, f"HV_{limit:g}V"
    upper = math.ceil(volts / 100.0) * 100.0
    return upper, f"HV_{upper:g}V"


def _hv_classes(design: _Design, assumptions: Assumptions, coverage: _Coverage,
                needs: list[dict]) -> list[dict]:
    volts = design.volts()
    peaks: dict[str, tuple[float, str]] = {}
    for net, volt in volts.items():
        if net in design.grounds():
            continue
        peaks[net] = (abs(volt.volts), f"{volt.source}: {volt.basis}" if volt.basis
                      else volt.source)
    folded = {net.casefold(): net for net in design.nets}
    for name, level in (assumptions.hv or {}).items():
        net = folded.get(name.strip().casefold())
        if net is None:
            continue
        dc, ac_peak = level[0], level[1] if len(level) > 1 else 0.0
        peaks[net] = (abs(dc) + abs(ac_peak), "hv= (given)")
    for conv in design.tree.converters.values():
        if not conv.fitted or not conv.switching:
            continue
        known = [abs(volts[net].volts) for net in [*conv.inputs,
                                                   *(out.net for out in conv.outputs)]
                 if net in volts]
        if not known:
            continue
        for net in conv.switch_nodes:
            if net not in peaks or peaks[net][0] < max(known):
                peaks[net] = (max(known), f"switch node of {conv.ref} ({conv.kind}): swings "
                              f"to {max(known):g} V")
    groups: dict[str, list[str]] = defaultdict(list)
    limits: dict[str, float] = {}
    bases: dict[str, list[str]] = defaultdict(list)
    for net, (peak, basis) in peaks.items():
        if peak <= HV_MIN_V:
            continue
        limit, name = _hv_band(peak)
        groups[name].append(net)
        limits[name] = max(limits.get(name, 0.0), peak)
        bases[name].append(f"{net} {peak:g} V ({basis})")
    out = []
    for name in sorted(groups, key=lambda key: limits[key]):
        members = sorted(groups[name], key=natural_key)
        peak = limits[name]
        outer, row = ipc2221_mm("B2", peak)
        inner, _ = ipc2221_mm("B1", peak)
        entry = {
            "name": name, "kind": "net", "role": "high_voltage", "members": members,
            "members_total": len(members), "evidence": bases[name][:LISTED],
            "clearance": {"peak_v": _r(peak, 1), "band": row,
                          "outer_mil": _r(outer * MIL_PER_MM, 2),
                          "inner_mil": _r(inner * MIL_PER_MM, 2),
                          "source": HV_SOURCE},
            "sheets": design.sheets_of(ref for net in members for ref in design.net_parts(net)),
        }
        exists = coverage.classes("net", members)
        rules = coverage.found("Clearance", [(net, "net") for net in members],
                               coverage.clearance_accept(outer * MIL_PER_MM, peak))
        if rules:
            exists["pcb_rules"] = {"Clearance": rules}
        entry["exists"] = exists
        out.append(entry)
    unknown = sorted((net for net in design.nets if net not in peaks
                      and net not in design.grounds() and hv_name_reason(net)),
                     key=natural_key)
    if unknown:
        needs.append({"item": "HV", "needs": "working voltages",
                      "why": f"{len(unknown)} nets look like high voltage by their name "
                             f"({_names(unknown, 4)}) but their voltage is unknown",
                      "how": "get_constraints_brief(hv='NET=volts,L=230AC')"})
    return out


def _rf_class(design: _Design, coverage: _Coverage, profiles: list[dict],
              pairs: list[_Pair]) -> list[dict]:
    """Nets of an RF path: RF pins of RF parts, RF-named nets, and the signal nets of
    coax / antenna connectors when an antenna, RF part or RF name corroborates them (a
    coax connector alone may carry a clock or a test signal); followed through series
    capacitors, inductors and resistors (matching networks) up to three parts."""
    seeds: dict[str, str] = {}
    coax: dict[str, tuple[str, bool]] = {}
    pair_nets = {net.lower() for pair in pairs for net in pair.legs()}
    for ref, kind in design.ctx.kind.items():
        if not design.fitted(ref):
            continue
        if kind == "connector":
            connector = design.connector(ref)
            if connector.rf:
                for pin in connector.signal_pins():
                    if pin.net and pin.net not in design.grounds() \
                            and pin.net.lower() not in pair_nets:
                        coax.setdefault(pin.net, (ref, bool(_ANTENNA.search(design.text(ref)))))
            continue
        if kind in ("ic", "other", "esd") and _RF_PART.search(design.text(ref)):
            for pin, net in (design.ctx.design.part_pins.get(ref) or {}).items():
                name = design.pin_name(ref, pin)
                if net and net not in design.grounds() and _RF_PIN.match(name or ""):
                    seeds.setdefault(net, f"RF pin {ref} {name}")
    for net in design.nets:
        found = classify(net)
        if found and found[0] == "rf" and net not in design.grounds() \
                and len(design.nets.get(net, ())) <= 6:
            seeds.setdefault(net, f"RF net name {net}")
    corroborated = bool(seeds)
    for net, (ref, antenna) in coax.items():
        if antenna or corroborated:
            seeds.setdefault(net, f"{'antenna' if antenna else 'coax'} connector {ref}")
    if not seeds:
        return []
    members = dict(seeds)
    level = list(seeds)
    for _ in range(3):
        following = []
        for net in level:
            for ref in design.net_parts(net):
                if design.kind(ref) not in ("capacitor", "inductive", "resistor") \
                        or not design.fitted(ref):
                    continue
                pins = design.ctx.design.part_pins.get(ref) or {}
                others = [other for other in dict.fromkeys(pins.values()) if other != net]
                if len(pins) != 2 or len(others) != 1:
                    continue
                other = others[0]
                if other in members or other in design.grounds() or \
                        other in design.dec.rails or len(design.nets.get(other, ())) > 4:
                    continue
                members[other] = f"through {ref} from {net}"
                following.append(other)
        level = following
    nets = sorted(members, key=natural_key)
    entry = {
        "name": "RF_50", "kind": "net", "role": "rf", "members": nets,
        "members_total": len(nets),
        "evidence": list(dict.fromkeys(seeds.values()))[:LISTED],
        "typical": {"what": "50 ohm single-ended: the usual RF system impedance (coax and "
                            "RF part ports); the RF parts' datasheets decide",
                    "single_ended_ohm": 50.0, "typical_only": True},
        "sheets": design.sheets_of(ref for net in nets for ref in design.net_parts(net)),
        "exists": coverage.classes("net", nets),
    }
    matched = [profile for profile in profiles if profile["type"] == "single-ended"
               and profile.get("target_ohm") and abs(profile["target_ohm"] - 50.0) <= 0.5]
    if matched:
        entry["impedance_profile"] = matched[0]["name"]
    return [entry]


# ---------- length matching ----------

def _strobe_key(pair: _Pair) -> tuple[str, int, str] | None:
    """(prefix, byte, segment suffix) of a DQS pair from its name or its legs' base name."""
    for name in (pair.name, _base_name(pair.positive, pair.negative)):
        match = _DQS.match(_upper(name))
        if match:
            return match.group(1), int(match.group(2)), match.group(3)
    return None


def _ddr_groups(design: _Design, pairs: list[_Pair], coverage: _Coverage) -> list[dict]:
    # (prefix, byte, segment suffix): a lane on each side of series resistors is its own.
    lanes: dict[tuple[str, int, str], dict] = defaultdict(
        lambda: {"dq": [], "dm": [], "dqs": []})
    prefixes = set()
    for net in design.nets:
        upper = _upper(net)
        if match := _DQ.match(upper):
            lanes[(match.group(1), int(match.group(2)) // 8, match.group(3))]["dq"].append(net)
            prefixes.add(match.group(1))
        elif match := _DM.match(upper):
            lanes[(match.group(1), int(match.group(2)), match.group(3))]["dm"].append(net)
    strobe_pairs = {}
    for pair in pairs:
        if (key := _strobe_key(pair)) is not None:
            strobe_pairs[key] = pair
    segments: dict[tuple[str, int], list[str]] = defaultdict(list)
    for (prefix, lane, suffix), members in lanes.items():
        if len(members["dq"]) >= 4:
            segments[(prefix, lane)].append(suffix)
    generation = design.ddr_generation()
    out = []
    for (prefix, lane, suffix), members in sorted(lanes.items(), key=lambda item: (
            natural_key(item[0][0]), item[0][1], item[0][2])):
        if len(members["dq"]) < 4:
            continue
        strobe = strobe_pairs.get((prefix, lane, suffix))
        nets = sorted(members["dq"], key=natural_key) + sorted(members["dm"], key=natural_key)
        if strobe:
            nets += [strobe.positive, strobe.negative]
        name = (f"{re.sub(r'[^A-Z0-9]+', '_', prefix).strip('_') or 'DDR'}_BYTE{lane}"
                + (f"_{suffix}" if suffix else ""))
        evidence = [f"{len(members['dq'])} DQ nets of byte {lane}"
                    + (f", DQS pair {strobe.name}" if strobe else ", no DQS pair found")
                    + (f", {len(members['dm'])} DM/DBI" if members["dm"] else "")]
        others = [other for other in segments[(prefix, lane)] if other != suffix]
        if others:
            evidence.append(
                "the byte also has nets named with suffix "
                + ", ".join(f"'{other}'" if other else "none" for other in others)
                + " (the other side of series parts): each segment is matched on its own; "
                  "xSignals (Design > xSignals) match each line end to end")
        entry = {
            "name": name, "kind": "byte_lane", "members": nets, "members_total": len(nets),
            "evidence": evidence,
            "sheets": design.sheets_of(ref for net in nets for ref in design.net_parts(net)),
        }
        row = _row("DDR4")
        if generation == "ddr4" and strobe:
            entry["tolerance"] = {
                "tolerance_mil": DDR4_DQ_TO_DQS_MIL, "typical_only": True,
                "what": "DQ/DM to their DQS within a byte lane: 2 ps (about 10 mil at 5 "
                        "mil/ps); never across bytes",
                "row": row["interface"], "source": row["source"]}
        else:
            entry["tolerance"] = None
            entry["why_no_tolerance"] = (
                "the DDR generation is not DDR4 or no DQS pair was found: the memory "
                "controller's layout guide gives the byte-lane budget")
        tolerance = (entry["tolerance"] or {}).get("tolerance_mil")
        entry["exists"] = _group_exists(coverage, nets, tolerance)
        out.append(entry)
    for prefix in sorted(prefixes, key=natural_key):
        nets = [net for net in design.nets if (match := _ADDR.match(_upper(net)))
                and match.group(1) == prefix]
        # The channel's own clocks: the pair's name less its CK / CLK token is the prefix.
        clocks = [pair for pair in pairs if pair.interface in ("ddr4_ck", "ddr3", "ddr5", "ddr")
                  and "DQS" not in _upper(pair.name)
                  and any((match := _DDR_CK.match(_upper(name))) and match.group(1) == prefix
                          for name in (pair.name, _base_name(pair.positive, pair.negative)))]
        if len(nets) < 8:
            continue
        nets = sorted(nets, key=natural_key)
        legs = [net for pair in clocks for net in pair.legs()]
        name = f"{re.sub(r'[^A-Z0-9]+', '_', prefix).strip('_') or 'DDR'}_ADDR_CMD"
        out.append({
            "name": name, "kind": "address_command", "members": nets + legs,
            "members_total": len(nets) + len(legs),
            "evidence": [f"{len(nets)} address, command and control nets"
                         + (f" with clock pairs {_names([pair.name for pair in clocks], 3)}"
                            if clocks else ", no clock pair found by name")],
            "tolerance": None,
            "why_no_tolerance": "CK to address/command/control per the memory controller's "
                                "limits (INTERFACE_TARGETS' DDR rows give no generic number)",
            "sheets": design.sheets_of(ref for net in nets for ref in design.net_parts(net)),
            "exists": _group_exists(coverage, nets + legs, None),
        })
    return out


def _group_exists(coverage: _Coverage, nets: list[str], tolerance_mil: float | None) -> dict:
    """PCB classes holding a length-matching group's nets and the MatchedLengths rules
    (group mode, through xSignals too) that take them, judged against the tolerance."""
    exists = coverage.classes("net", nets)
    rules = coverage.found("MatchedLengths", [(net, "net") for net in nets],
                           coverage.matched_accept(False, tolerance_mil))
    if rules:
        exists["pcb_rules"] = {"MatchedLengths": rules}
    return exists


def _bus_groups(design: _Design, taken: set[str], coverage: _Coverage) -> list[dict]:
    out = []
    groups: dict[tuple[str, str], list[str]] = defaultdict(list)
    for net in design.nets:
        if net in taken or not _RGMII_TOKENS.search(_upper(net)):
            continue
        found = classify(net)
        if found and found[0] == "ethernet" and found[1] in ("tx", "rx"):
            prefix = re.split(r"(?:TXD|RXD|TX|RX)", _upper(net), maxsplit=1)[0]
            groups[(prefix, found[1])].append(net)
    clocks: dict[tuple[str, str], list[str]] = defaultdict(list)
    for net in design.nets:
        for role, pattern in _MII_CLOCK:
            if (match := pattern.match(_upper(net))) and net not in taken:
                clocks[(match.group(1), role)].append(net)
                break
    for (prefix, role), nets in sorted(groups.items()):
        if len(nets) < 3:
            continue
        nets = sorted(nets, key=natural_key)
        clock = sorted(clocks.get((prefix, role), []), key=natural_key)
        evidence = [f"Ethernet MAC/PHY {role.upper()} lines by name ({_names(nets, 4)})"]
        evidence.append(f"clock {_names(clock, 2)} (matched with its data: clock to data)"
                        if clock else "no clock net found by name: add the group's "
                                      f"{role.upper()} clock (RGMII matches data to clock)")
        members = nets + clock
        out.append({
            "name": f"{re.sub(r'[^A-Z0-9]+', '_', prefix).strip('_') or 'MII'}_{role.upper()}",
            "kind": "parallel_bus", "members": members, "members_total": len(members),
            "evidence": evidence,
            "tolerance": None,
            "why_no_tolerance": "RGMII / RMII: clock-to-data budget from the MAC and PHY "
                                "datasheets (internal-delay mode); no generic value",
            "sheets": design.sheets_of(ref for net in members for ref in design.net_parts(net)),
            "exists": _group_exists(coverage, members, None),
        })
        taken.update(members)
    erc = getattr(design.compiled, "erc", None)
    seen = set()
    for bus in getattr(erc, "buses", None) or []:
        nets = [net for net in dict.fromkeys(bus.nets) if net in design.nets and net not in taken]
        if len(nets) < 4 or bus.name.lower() in seen:
            continue
        families = Counter((found[0] if (found := classify(net)) else None) for net in nets)
        family = next((name for name, _ in families.most_common()
                       if name in ("memory_bus", "qspi", "sdio", "high_speed", "display",
                                   "camera")), None)
        if family is None:
            continue
        seen.add(bus.name.lower())
        out.append({
            "name": re.sub(r"[^A-Za-z0-9]+", "_", bus.name).strip("_").upper() or "BUS",
            "kind": "parallel_bus", "members": sorted(nets, key=natural_key),
            "members_total": len(nets),
            "evidence": [f"schematic bus {bus.name} on {bus.sheet} ({family} lines)"],
            "tolerance": None,
            "why_no_tolerance": "a parallel bus is matched to its timing budget: the parts' "
                                "datasheets give it",
            "sheets": [bus.sheet],
            "exists": _group_exists(coverage, nets, None),
        })
    return out


def _pair_matching(classes: list[dict]) -> list[dict]:
    out = []
    for entry in classes:
        if entry["kind"] != "diff_pair":
            continue
        tolerance = (entry.get("typical") or {}).get("in_pair_tolerance_mil")
        row = entry.get("typical") or {}
        item = {"name": entry["name"], "kind": "in_pair", "pairs": entry["members_total"],
                "evidence": [f"{entry['members_total']} {entry['interface']} pairs"],
                "sheets": entry["sheets"]}
        if tolerance is not None:
            item["tolerance"] = {"tolerance_mil": tolerance, "typical_only": True,
                                 "what": row.get("skew"), "row": row.get("row"),
                                 "source": row.get("source")}
        else:
            item["tolerance"] = None
            item["why_no_tolerance"] = row.get("skew") or "no typical in-pair figure"
        covered = (entry.get("exists") or {}).get("pcb_rules", {}).get("MatchedLengths")
        if covered:
            item["exists"] = {"pcb_rules": {"MatchedLengths": covered}}
        out.append(item)
    return out


# ---------- placement ----------

def placement_guidance(pcb: bool) -> dict:
    """What each placement kind asks, with the screening distance (in mil) its pcb block's
    within_limit uses; said once per answer instead of on every item."""
    guidance = {
        "decoupling": {"requirement": DECOUPLING_GUIDE,
                       "limit_mil": round(DEFAULT_MAX_DISTANCE_MIL, 3),
                       "limit_applies_to": "capacitors under 1 uF (bulk ones are listed, not "
                                           "judged)"},
        "rail_capacitors": {"requirement": "Shared by the rail's ICs (bulk, or drawn away "
                                           "from any of them): spread along the rail, bulk "
                                           "near its source or where it enters the ICs' "
                                           "area."},
        "tvs": {"requirement": TVS_GUIDE, "limit_mil": round(TVS_AT_CONNECTOR_MIL, 3)},
        "crystal": {"requirement": CRYSTAL_GUIDE, "limit_mil": round(CRYSTAL_NEAR_MIL, 3),
                    "limit_basis": "a screening distance: the oscillator guide decides"},
        "smps": {"requirement": SMPS_GUIDE, "limit_mil": round(SMPS_NEAR_MIL, 3),
                 "limit_basis": "from the converter's pads, a screening distance: the "
                                "datasheet's layout example decides"},
    }
    if pcb:
        guidance["measured_as"] = ("straight line in plan view, pad edge to pad edge (a "
                                   "capacitor's rail pad to the IC pin it is drawn at, else "
                                   "to the IC's nearest pad on the rail)")
    return guidance


# A capacitor is placed at one pin only when drawn this close to it on the schematic and
# clearly nearer to it than to the IC's other pins on the rail (most schematics draw the
# capacitors in a row away from the pins: those go to the IC, one per supply pin).
PIN_DRAWN_MIL = 600.0
PIN_DRAWN_RATIO = 0.5
IC_DRAWN_RATIO = 0.7


def _assign_capacitor(design: _Design, cap_points: list, targets: list) -> tuple | None:
    """(ic, pin or None, basis) for a capacitor from its schematic position, or None."""
    found = sorted((distance, ic, pin) for ic, pin, points in targets
                   if (distance := design.nearest(cap_points, points)) is not None)
    if not found:
        return None
    nearest, ic, pin = found[0]
    other_pins = [d for d, other, other_pin in found[1:] if other == ic and other_pin != pin]
    other_ics = [d for d, other, _ in found if other != ic]
    if other_ics and nearest > IC_DRAWN_RATIO * other_ics[0]:
        return None  # drawn between two ICs: not clearly either's
    if nearest <= PIN_DRAWN_MIL and (not other_pins or nearest <= PIN_DRAWN_RATIO
                                     * other_pins[0]):
        return ic, pin, f"drawn at {ic} pin {pin} ({nearest:.0f} mil away on the schematic)"
    return ic, None, f"drawn nearest to {ic} (not at one of its pins on the rail)"


def _decoupling(design: _Design, max_mil: float) -> list[dict]:
    dec = design.dec
    by_rail: dict[str, list] = defaultdict(list)
    for pair in dec.pairs:
        by_rail[pair.rail].append(pair)
    out = []
    wanted: list[str] = []
    for rail, rail_pairs in by_rail.items():
        caps = [cap for cap in dec.caps.get(rail, []) if cap.fitted]
        wanted += [pair.ic for pair in rail_pairs] + [cap.designator for cap in caps]
    if design.pcb is not None:
        design.pcb.preload(wanted)
    for rail in sorted(by_rail, key=natural_key):
        rail_pairs = by_rail[rail]
        caps = [cap for cap in dec.caps.get(rail, []) if cap.fitted]
        targets = []  # (ic, pin or None, schematic points)
        for pair in rail_pairs:
            for pin in pair.pins:
                points = design.points(pair.ic, [pin])
                if points:
                    targets.append((pair.ic, pin, points))
        assigned: dict[str, list[dict]] = defaultdict(list)
        shared: list = []
        for cap in caps:
            cap_points = design.points(cap.designator, design.pins_on(cap.designator, rail))
            best = _assign_capacitor(design, cap_points, targets)
            row = cap.describe()
            row["small"] = cap.small()
            if best is not None:
                if best[1] is not None:
                    row["next_to_pin"] = best[1]
                row["basis"] = best[2]
                assigned[best[0]].append(row)
                continue
            sheet = design.sheet(cap.designator)
            local = sorted({pair.ic for pair in rail_pairs if design.sheet(pair.ic) == sheet})
            if len(local) == 1:
                row["basis"] = f"the only IC on {rail} on its sheet"
                assigned[local[0]].append(row)
            elif len({pair.ic for pair in rail_pairs}) == 1:
                row["basis"] = f"the only IC on {rail}"
                assigned[rail_pairs[0].ic].append(row)
            else:
                shared.append(row)
        for pair in sorted(rail_pairs, key=lambda item: natural_key(item.ic)):
            rows = assigned.get(pair.ic, [])
            entry = {"kind": "decoupling", "ic": pair.ic, "rail": rail, "pins": pair.pins,
                     "capacitors": sorted(rows, key=lambda row: (not row["small"],
                                                                 natural_key(row["designator"]))),
                     "sheets": design.sheets_of([pair.ic] + [row["designator"] for row in rows]),
                     }
            if not rows:
                entry["note"] = (f"no capacitor on {rail} is drawn at {pair.ic}"
                                 + (f"; {len(shared)} rail capacitors are shared" if shared
                                    else ""))
            else:
                small_count = sum(1 for row in rows if row["small"])
                entry["per_pin"] = (f"{small_count} small capacitor"
                                    f"{'s' if small_count != 1 else ''} for {len(pair.pins)} "
                                    f"supply pin{'s' if len(pair.pins) != 1 else ''}: one at "
                                    "each pin first, as far as they go")
            if design.pcb is not None and rows:
                measured = []
                for row in rows:
                    distance = design.pcb.distance(
                        design.pcb.pads(row["designator"], rail),
                        design.pcb.pads(pair.ic, rail, [row["next_to_pin"]]
                                        if row.get("next_to_pin") else pair.pins))
                    if distance is not None:
                        measured.append({"designator": row["designator"],
                                         "distance_mil": round(distance, 1)})
                if measured:
                    small_refs = {row["designator"] for row in rows if row["small"]}
                    small = [item for item in measured if item["designator"] in small_refs]
                    farthest = max(small or measured, key=lambda item: item["distance_mil"])
                    entry["pcb"] = {"measured": measured[:LISTED],
                                    "farthest_mil": farthest["distance_mil"],
                                    "farthest": farthest["designator"],
                                    # Bulk capacitors are not held to the distance.
                                    "within_limit": (farthest["distance_mil"] <= max_mil
                                                     if small else None)}
            out.append(entry)
        if shared:
            out.append({"kind": "rail_capacitors", "rail": rail,
                        "ics": sorted({pair.ic for pair in rail_pairs}, key=natural_key),
                        "capacitors": shared,
                        "sheets": design.sheets_of(row["designator"] for row in shared)})
    return out


def _tvs(design: _Design) -> list[dict]:
    out = []
    connectors = [ref for ref, kind in design.ctx.kind.items() if kind == "connector"
                  and design.fitted(ref)]
    for ref in sorted(connectors, key=natural_key):
        connector = design.connector(ref)
        devices: dict[str, dict] = {}
        for pin in connector.signal_pins():
            if not pin.walk:
                continue
            for part, hops, net in pin.walk.esd:
                item = devices.setdefault(part, {"device": part, "pins": [], "nets": [],
                                                 "behind_series": False})
                item["pins"].append(pin.pin)
                if pin.net not in item["nets"]:
                    item["nets"].append(pin.net)
                item["behind_series"] = item["behind_series"] or hops > 0
                item.setdefault("_net_of", {})[pin.pin] = net
        if not devices:
            continue
        if design.pcb is not None:
            design.pcb.preload([ref, *devices])
        for part in sorted(devices, key=natural_key):
            item = devices[part]
            net_of = item.pop("_net_of")
            entry = {"kind": "tvs", "connector": ref, "exposure": connector.exposure,
                     **item, "pins": item["pins"][:LISTED], "nets": item["nets"][:LISTED],
                     "sheets": design.sheets_of([ref, part])}
            if item["behind_series"]:
                entry["note"] = ("check_interfaces finds it behind a series part: it clamps "
                                 "after the series element")
            if design.pcb is not None:
                worst = None
                for pin in item["pins"]:
                    distance = design.pcb.distance(
                        design.pcb.pads(part, net_of[pin]),
                        [pad for pad in design.pcb.pads(ref) if pad["pad_name"] == pin])
                    if distance is not None and (worst is None or distance > worst[0]):
                        worst = (distance, pin)
                if worst is not None:
                    entry["pcb"] = {"farthest_pin": worst[1],
                                    "distance_mil": round(worst[0], 1),
                                    "within_limit": worst[0] <= TVS_AT_CONNECTOR_MIL}
            out.append(entry)
    return out


def _crystals(design: _Design) -> list[dict]:
    out = []
    dec = design.dec
    for ref in sorted(design.ctx.kind, key=natural_key):
        if not design.fitted(ref) or not is_crystal(ref, design.text(ref)):
            continue
        pins = design.ctx.design.part_pins.get(ref) or {}
        nets = [net for net in dict.fromkeys(pins.values())
                if net not in design.grounds() and net not in dec.rails]
        if len(nets) != 2 or any(net in dec.rails for net in pins.values()):
            continue  # an active oscillator (a supply pin) or unreadable
        members = {"crystal": ref, "load_capacitors": [], "series_resistors": [],
                   "feedback_resistors": [], "oscillator_pins": []}
        reach = set(nets)
        for net in nets:
            for cap in dec.caps.get(net, []):
                if cap.fitted and cap.designator not in members["load_capacitors"]:
                    members["load_capacitors"].append(cap.designator)
            for other_ref in design.net_parts(net):
                if other_ref == ref or design.kind(other_ref) != "resistor":
                    continue
                other_pins = design.ctx.design.part_pins.get(other_ref) or {}
                others = [n for n in dict.fromkeys(other_pins.values()) if n != net]
                if len(others) != 1:
                    continue
                if others[0] in nets:
                    if other_ref not in members["feedback_resistors"]:
                        members["feedback_resistors"].append(other_ref)
                elif others[0] not in design.grounds() and others[0] not in dec.rails \
                        and len(design.nets.get(others[0], ())) <= 3 and any(
                            design.kind(far) in ("ic", "other")
                            for far, _ in design.nets.get(others[0], ())):
                    # A series resistor into the oscillator pin (a short net with an IC).
                    members["series_resistors"].append(other_ref)
                    reach.add(others[0])
        for net in sorted(reach, key=natural_key):
            for other_ref, pin in design.nets.get(net, ()):
                if other_ref != ref and design.kind(other_ref) in ("ic", "other"):
                    name = design.pin_name(other_ref, pin)
                    found = classify(name, pin=True) if name else None
                    members["oscillator_pins"].append(
                        {"part": other_ref, "pin": pin, "name": name,
                         "named_as_oscillator_pin": bool(found and found[0] == "crystal")
                         or bool(_OSC_PIN.search(_upper(name)))})
        refs = [ref, *members["load_capacitors"], *members["series_resistors"],
                *members["feedback_resistors"],
                *{item["part"] for item in members["oscillator_pins"]}]
        entry = {"kind": "crystal", **members, "nets": nets, "sheets": design.sheets_of(refs)}
        if not members["oscillator_pins"]:
            entry["note"] = "no IC pin found on the crystal's nets"
        elif not any(item["named_as_oscillator_pin"] for item in members["oscillator_pins"]):
            entry["note"] = ("the IC pins on the crystal's nets are not named as oscillator "
                             "pins: check the schematic")
        if design.pcb is not None:
            design.pcb.preload(refs)
            crystal_pads = design.pcb.pads(ref)
            measured = []
            for item in members["oscillator_pins"]:
                distance = design.pcb.distance(crystal_pads,
                                               design.pcb.pads(item["part"], pins=[item["pin"]]))
                if distance is not None:
                    measured.append({"to": f"{item['part']}.{item['pin']}",
                                     "distance_mil": round(distance, 1)})
            for cap in members["load_capacitors"]:
                distance = design.pcb.distance(crystal_pads, design.pcb.pads(cap))
                if distance is not None:
                    measured.append({"to": cap, "distance_mil": round(distance, 1)})
            if measured:
                worst = max(measured, key=lambda item: item["distance_mil"])
                entry["pcb"] = {"measured": measured, "farthest": worst["to"],
                                "farthest_mil": worst["distance_mil"],
                                "within_limit": worst["distance_mil"] <= CRYSTAL_NEAR_MIL}
        out.append(entry)
    return out


def _nearest_caps(design: _Design, conv_ref: str, pins: list[str], net: str,
                  limit: int = 3) -> list[str]:
    caps = [cap for cap in design.dec.caps.get(net, []) if cap.fitted]
    if not caps:
        return []
    points = design.points(conv_ref, pins)
    ranked = []
    for cap in caps:
        distance = design.nearest(design.points(cap.designator,
                                                design.pins_on(cap.designator, net)), points)
        if distance is not None:
            ranked.append((distance, cap.designator))
    if ranked:
        ranked.sort()
        return [ref for _, ref in ranked[:limit]]
    sheet = design.sheet(conv_ref)
    local = [cap.designator for cap in caps if design.sheet(cap.designator) == sheet]
    return sorted(local, key=natural_key)[:limit] if local else (
        [cap.designator for cap in caps] if len(caps) <= limit else [])


def _smps(design: _Design) -> list[dict]:
    out = []
    rows = {row["designator"]: row for row in design.data.get("converters") or []}
    for ref in sorted(design.tree.converters, key=natural_key):
        conv = design.tree.converters[ref]
        if not conv.fitted or not conv.switching:
            continue
        row = rows.get(ref, {})
        group: dict = {"converter": ref, "type": conv.kind}
        in_pins = [pin for pin, _, _ in conv.pins.get("in", ())]
        group["input_capacitors"] = sorted({cap for net in conv.inputs for cap in
                                            _nearest_caps(design, ref, in_pins, net)},
                                           key=natural_key)
        inductors, diodes, boot = [], [], []
        switch_nodes = sorted((set(conv.switch_nodes) | set(conv.nets("sw")))
                              - design.grounds(), key=natural_key)
        for net in switch_nodes:
            inductors += [part for part, _ in inductors_on(design.tree, net)]
            for other_ref in design.net_parts(net):
                if other_ref != ref and design.kind(other_ref) in ("diode", "zener") \
                        and design.fitted(other_ref):
                    diodes.append(other_ref)
            boot += [cap.designator for cap in design.dec.between.get(net, []) if cap.fitted]
        group["switch_nodes"] = switch_nodes
        group["inductors"] = sorted(set(inductors), key=natural_key)
        if diodes:
            group["diodes"] = sorted(set(diodes), key=natural_key)
        if boot:
            group["bootstrap_capacitors"] = sorted(set(boot), key=natural_key)
        outputs = []
        for out_row in conv.outputs:
            pins = out_row.pins or []
            outputs += _nearest_caps(design, ref, pins, out_row.net)
            for inductor in group["inductors"]:
                outputs += _nearest_caps(design, inductor,
                                         design.pins_on(inductor, out_row.net), out_row.net)
        group["output_capacitors"] = sorted(set(outputs), key=natural_key)
        divider = []
        for feedback in row.get("feedback") or []:
            divider += list(feedback.get("top") or []) + list(feedback.get("bottom") or [])
        if divider:
            group["feedback_divider"] = sorted(set(divider), key=natural_key)
        refs = [ref, *group["input_capacitors"], *group["inductors"],
                *group.get("diodes", []), *group.get("bootstrap_capacitors", []),
                *group["output_capacitors"], *group.get("feedback_divider", [])]
        entry = {"kind": "smps", **group, "sheets": design.sheets_of(refs),
                 "evidence": [f"get_power_tree: {conv.kind} {ref}"
                              + (f" ({_names(conv.basis, 2)})" if conv.basis else "")]}
        missing = [name for name, key in (("input capacitors", "input_capacitors"),
                                          ("output capacitors", "output_capacitors"))
                   if not group.get(key)]
        if not switch_nodes:
            missing.append("a switch-node pin (an integrated inductor, as in power modules, "
                           "or a pin name not recognised)")
        elif not group["inductors"]:
            missing.append("the switch-node inductor")
        if missing:
            entry["note"] = f"not found: {'; '.join(missing)}"
        if design.pcb is not None:
            design.pcb.preload(refs)
            own = design.pcb.pads(ref)
            measured = []
            for member in refs[1:]:
                distance = design.pcb.distance(own, design.pcb.pads(member))
                if distance is not None:
                    measured.append({"to": member, "distance_mil": round(distance, 1)})
            if measured:
                worst = max(measured, key=lambda item: item["distance_mil"])
                entry["pcb"] = {"measured": measured[:LISTED], "farthest": worst["to"],
                                "farthest_mil": worst["distance_mil"],
                                "within_limit": worst["distance_mil"] <= SMPS_NEAR_MIL}
        out.append(entry)
    return out


# ---------- zones and notes ----------

def _zones(design: _Design, rf_nets: set[str]) -> list[dict]:
    """rf_nets: the RF class's members (a coax connector is RF only on an RF path)."""
    by_sheet: dict[str, list[str]] = defaultdict(list)
    for ref in design.ctx.kind:
        if design.fitted(ref) and (sheet := design.sheet(ref)):
            by_sheet[sheet].append(ref)
    converters = {ref for ref, conv in design.tree.converters.items() if conv.fitted}
    switch_inductors = {part for conv in design.tree.converters.values()
                        for net in conv.switch_nodes for part, _ in inductors_on(design.tree, net)}
    rails = {row["rail"] for row in design.data.get("rails") or []} - design.grounds()
    out = []
    for sheet in sorted(by_sheet, key=natural_key):
        refs = sorted(by_sheet[sheet], key=natural_key)
        score: Counter = Counter()
        basis: dict[str, list[str]] = defaultdict(list)
        name_hits = [label for label, pattern in _SHEET_WORDS if pattern.search(sheet)]

        def add(label: str, ref: str, weight: float = 1.0) -> None:
            score[label] += weight
            basis[label].append(ref)

        for ref in refs:
            kind, text = design.kind(ref), design.text(ref)
            if ref in converters:
                add("power", ref, 3)
            elif ref in switch_inductors:
                add("power", ref, 1)
            elif kind == "transistor" and len({net for net in (
                    design.ctx.design.part_pins.get(ref) or {}).values() if net in rails}) >= 2:
                add("power", ref, 2)  # a load switch between two rails
            elif kind == "connector":
                connector = design.connector(ref)
                on_rf_path = connector.rf and any(
                    pin.net in rf_nets for pin in connector.signal_pins())
                add("rf" if on_rf_path else "io", ref, 2)
            elif kind == "esd":
                add("io", ref, 1)
            elif kind in ("choke",) or (kind == "inductive" and ref not in switch_inductors):
                add("filtering", ref, 1)
            elif kind == "transformer":
                add("io", ref, 1)
            elif kind in ("ic", "other"):
                processor = PROCESSOR_TEXT.search(text) or part_kind(text) in ("fpga", "mcu")
                if not processor and (_RF_PART.search(text) or part_kind(text) == "radio"):
                    add("rf", ref, 3)
                elif processor or _MEMORY_TEXT.search(text):
                    add("digital", ref, 3)
                elif bus_transceiver(design.ctx, ref) or _IO_TEXT.search(text):
                    add("io", ref, 2)
                elif _DIGITAL_TEXT.search(text) or part_kind(text) in ("io_expander", "module"):
                    add("digital", ref, 3)
                elif _ANALOG_TEXT.search(text):
                    # On a sheet named for power, a reference or comparator serves the
                    # supplies: it weighs less than the sheet's power parts and name.
                    add("analog", ref, 1.5 if "power" in name_hits else 3)
                elif _FILTER_TEXT.search(text):
                    add("filtering", ref, 1)
        for label in name_hits:
            score[label] += 2
        if not score:
            continue
        labels = []
        top = max(score.values())
        # Highest score first; on a tie the label the sheet's name gives.
        ranked = sorted(score.items(), key=lambda item: (-item[1], item[0] not in name_hits))
        for label, value in ranked:
            if value < max(2.0, top * 0.25):
                continue
            parts = basis.get(label, [])
            why = []
            if parts:
                why.append(f"{len(parts)} parts ({_names(parts, 4)})")
            if label in name_hits:
                why.append("the sheet's name")
            labels.append({"label": label, "basis": "; ".join(why)})
        if not labels:
            continue
        out.append({"sheet": sheet, "zone": labels[0]["label"], "labels": labels,
                    "parts": len(refs), "sheets": [sheet]})
    return out


def _designer_notes(notes: list[dict] | None) -> list[dict]:
    out = []
    for row in notes or []:
        if row.get("document_kind") != "sheet" or row.get("kind") not in _NOTE_KINDS:
            continue
        text = " ".join(str(row.get("text") or "").split())
        if not text or not _PLACEMENT_WORDS.search(text):
            continue
        quoted = text if len(text) <= NOTE_CHARS else text[:NOTE_CHARS - 1] + "…"
        entry = {"sheet": row.get("document"), "kind": row.get("kind"), "quote": quoted,
                 "sheets": [row.get("document")]}
        if row.get("author"):
            entry["author"] = row["author"]
        if row.get("near_refdes"):
            entry["near"] = list(row["near_refdes"])[:8]
        out.append(entry)
    return out


# ---------- the plan ----------

def _rule_name(kind: str, target: str) -> str:
    return f"{kind}_{target}"


def _place_rule(rule: dict, covered_by: dict | None, rules: list[dict], covered: list[dict],
                already: str | None = None) -> None:
    """Leave the rule out when the board's rules cover every target with values at least
    as strict (or already says why); else keep it, naming the board's rules that take its
    targets but ask less."""
    if already or (covered_by and covered_by["of"] and
                   covered_by["covered"] == covered_by["of"]):
        why = already or (f"covered by {_names(covered_by['rules'], 3)}"
                          + (" (through xSignals)" if covered_by.get("via_xsignal") else ""))
        covered.append({"rule": rule["name"], "why": why})
        return
    notes = []
    if covered_by and covered_by["covered"]:
        notes.append(f"{_names(covered_by['rules'], 3)} already cover "
                     f"{covered_by['covered']} of {covered_by['of']} of its targets")
    for item in (covered_by or {}).get("weaker", []):
        notes.append(f"{item['rule']} also takes its targets but {item['why']}: only the "
                     f"highest-priority {rule['kind']} rule applies to an object, so decide "
                     "which wins in Priorities (or change it)")
    if notes:
        rule["board_rules"] = notes
    rules.append(rule)


def _proposed_plan(classes: list[dict], matching: list[dict], units: str,
                   profiles_by_name: dict[str, dict],
                   outer_layers: frozenset = frozenset()) -> tuple[dict, list[dict]]:
    """The plan (classes and rules with values, units as given) and the rules left out
    because the PCB already covers them."""
    scale = 1.0 / MIL_PER_MM if units == "mm" else 1.0

    def length(mil: float) -> float:
        return round(mil * scale, 4)

    plan_classes, rules, covered = [], [], []
    for entry in classes:
        exists_same = (entry.get("exists") or {}).get("same_members")
        cls = {"name": entry["name"], "kind": entry["kind"] if entry["kind"] == "diff_pair"
               else "net", "basis": "; ".join(entry.get("evidence", [])[:3])}
        if entry["kind"] == "diff_pair":
            cls["members"] = entry["pairs"]
        else:
            cls["members"] = entry["members"]
        if exists_same:
            cls["exists_on_pcb"] = exists_same[0]
        plan_classes.append(cls)
        pcb_rules = (entry.get("exists") or {}).get("pcb_rules", {})
        if entry.get("role") == "power" and entry.get("width"):
            width = entry["width"]
            rule = {"name": _rule_name("Width", entry["name"]), "kind": "Width",
                    "scope": {"net_class": entry["name"]},
                    "values": {"min_width": length(width["outer_min_width_mil"]),
                               "preferred_width": length(width["outer_min_width_mil"]),
                               "max_width": length(max(OPEN_MAX_WIDTH_MIL,
                                                       width["inner_min_width_mil"]))},
                    "inner": {"min_width": length(width["inner_min_width_mil"]),
                              "preferred_width": length(width["inner_min_width_mil"]),
                              "max_width": length(max(OPEN_MAX_WIDTH_MIL,
                                                      width["inner_min_width_mil"]))},
                    "basis": f"IPC-2221 width for {width['current_a']:g} A at "
                             f"{width['temp_rise_c']:g} C rise, {width['outer_copper']['oz']:g} "
                             f"oz outer / {width['inner_copper']['oz']:g} oz inner; max width "
                             "left open (not a requirement)",
                    "typical": False}
            _place_rule(rule, pcb_rules.get("Width"), rules, covered,
                        (entry.get("exists") or {}).get("default_width_enough"))
        if entry.get("role") == "high_voltage":
            clearance = entry["clearance"]
            rule = {"name": _rule_name("Clearance", entry["name"]), "kind": "Clearance",
                    "scope": {"net_class": entry["name"]}, "scope2": "All",
                    "values": {"clearance": length(clearance["outer_mil"])},
                    "basis": f"IPC-2221B B2 at {clearance['peak_v']:g} V peak "
                             f"({clearance['band']}); internal layers (B1) need "
                             f"{length(clearance['inner_mil']):g} {units}",
                    "typical": False}
            _place_rule(rule, pcb_rules.get("Clearance"), rules, covered)
        profile_name = entry.get("impedance_profile")
        if entry["kind"] == "diff_pair" and profile_name in profiles_by_name:
            profile = profiles_by_name[profile_name]
            solved = [row for row in profile["layers"] if row.get("width_mil")
                      and row.get("gap_mil")]
            outer = next((row for row in solved if row["layer"].lower() in outer_layers),
                         solved[0] if solved else None)
            inner = next((row for row in solved if row["layer"].lower() not in outer_layers),
                         None)

            def pair_values(row: dict) -> dict:
                return {"min_width": length(row.get("min_width_mil", row["width_mil"])),
                        "preferred_width": length(row["width_mil"]),
                        "max_width": length(row.get("max_width_mil", row["width_mil"])),
                        "min_gap": length(row.get("min_gap_mil", row["gap_mil"])),
                        "preferred_gap": length(row["gap_mil"]),
                        "max_gap": length(row.get("max_gap_mil", row["gap_mil"]))}

            if outer is not None:
                rule = {"name": _rule_name("DiffPairsRouting", entry["name"]),
                        "kind": "DiffPairsRouting",
                        "scope": {"diff_pair_class": entry["name"]},
                        "values": pair_values(outer),
                        "impedance_profile": profile_name,
                        "basis": f"the board's impedance profile {profile_name} "
                                 f"({profile['target_ohm']:g} ohm), solved on "
                                 f"{outer['layer']}"
                                 + (f" (outer) and {inner['layer']} (inner; other inner "
                                    "layers may solve otherwise: the Layer Stack Manager "
                                    "decides)" if inner and inner is not outer else ""),
                        "typical": True}
                if inner is not None and inner is not outer:
                    rule["inner"] = pair_values(inner)
                _place_rule(rule, pcb_rules.get("DiffPairsRouting"), rules, covered)
    for item in matching:
        tolerance = item.get("tolerance")
        if not tolerance:
            continue
        if item["kind"] == "in_pair":
            rule = {"name": _rule_name("MatchedLengths", item["name"]),
                    "kind": "MatchedLengths", "scope": {"diff_pair_class": item["name"]},
                    "values": {"tolerance": length(tolerance["tolerance_mil"]),
                               "within_pair": True},
                    "basis": f"typical in-pair skew: {tolerance['what']} "
                             f"({tolerance['source']})",
                    "typical": True}
        else:
            plan_classes.append({"name": item["name"], "kind": "net", "members": item["members"],
                                 "basis": "; ".join(item.get("evidence", [])[:2]),
                                 **({"exists_on_pcb": item["exists"]["same_members"][0]}
                                    if (item.get("exists") or {}).get("same_members") else {})})
            rule = {"name": _rule_name("MatchedLengths", item["name"]),
                    "kind": "MatchedLengths", "scope": {"net_class": item["name"]},
                    "values": {"tolerance": length(tolerance["tolerance_mil"]),
                               "within_pair": False},
                    "basis": f"typical: {tolerance['what']} ({tolerance['source']})",
                    "typical": True}
        _place_rule(rule, (item.get("exists") or {}).get("pcb_rules", {}).get(
            "MatchedLengths"), rules, covered)
    used = {rule["scope"].get("net_class") or rule["scope"].get("diff_pair_class")
            for rule in rules}
    plan = {"schema": PLAN_SCHEMA, "units": units,
            # Unrecognised pairs are listed in the brief but not proposed as a class.
            "classes": [cls for cls in plan_classes if cls["name"] in used
                        or not cls["name"].startswith("DIFF_OTHER")],
            "rules": rules,
            "notes": ["Edit names, members and values, then pass the plan to "
                      "generate_rule_file(plan=...). Values are in 'units'. 'typical': true "
                      "marks a value taken from INTERFACE_TARGETS or an impedance profile, "
                      "not a requirement of this design. 'board_rules' names the board's "
                      "rules that already take a rule's targets but ask less: settle the "
                      "order in Priorities (or change those rules).",
                      "Rule kinds: Width (min_width, preferred_width, max_width; 'inner' for "
                      "internal layers), Clearance (clearance; scope2), DiffPairsRouting "
                      "(min/preferred/max width and gap, max_uncoupled_length), "
                      "MatchedLengths (tolerance, within_pair), Length (min_length, "
                      "max_length). Scopes: {'net_class': X}, {'diff_pair_class': X}, "
                      "{'net': X}, {'diff_pair': X}, 'All' or {'query': '<Altium query>'}."]}
    return plan, covered


# ---------- the brief ----------

def build_brief(netlist: dict, plan=None, project_params: dict | None = None, *,
                index=None, compiled=None, options=None, project_type: dict | None = None,
                notes: list[dict] | None = None, directive_summary: dict | None = None,
                assumptions: Assumptions | None = None) -> dict:
    """The whole brief: every section, unpaged (render_brief pages it). index is the
    PcbIndex or None; compiled the offline compile (directives, schematic positions) or
    None; options the .PrjPcb options (pair suffixes); notes design_notes.note_rows'
    rows; directive_summary check_directive_sync's summary when the PcbDoc loads."""
    assumptions = assumptions or Assumptions()
    tool_notes: list[str] = []
    needs: list[dict] = []
    design = _Design(netlist, plan, project_params, compiled, index)
    coverage = _Coverage(index)
    directives = _directive_nets(compiled)
    thickness = _thickness(index, assumptions.copper_oz, tool_notes)
    profiles = []
    if index is not None:
        try:
            profiles = impedance_profiles(index)
        except Exception as error:  # profiles are an extra source
            tool_notes.append(f"impedance profiles not read: {type(error).__name__}")
    pairs = _collect_pairs(design, options, directives)
    classes = _pair_classes(design, pairs, coverage, directives, profiles)
    for entry in classes:
        typical = entry.get("typical") or {}
        if typical.get("differential_ohm") and not entry.get("impedance_profile") \
                and not ((entry.get("exists") or {}).get("pcb_rules", {}).get(
                    "DiffPairsRouting") or {}).get("covered"):
            needs.append({"item": entry["name"], "needs": "width and gap for the impedance",
                          "why": f"typical {typical['differential_ohm']:g} ohm differential "
                                 f"({typical.get('row')}); no impedance profile on the board "
                                 "solves it, so no DiffPairsRouting values are proposed",
                          "how": "add a differential impedance profile in the Layer Stack "
                                 "Manager (or give min/preferred/max width and gap in the "
                                 "plan's DiffPairsRouting rule)"})
    classes += _power_classes(design, assumptions, thickness, coverage, needs, tool_notes)
    classes += _hv_classes(design, assumptions, coverage, needs)
    classes += _rf_class(design, coverage, profiles, pairs)
    for entry in classes:
        if entry["kind"] != "diff_pair":
            names = {name for net in entry["members"]
                     for name in directives["class"].get(net, ())}
            if names:
                entry.setdefault("exists", {})["directive_classes"] = sorted(names)[:5]
        # A proposed name another PCB class of different members already uses.
        clash = coverage.class_named(entry["kind"] if entry["kind"] == "diff_pair" else "net",
                                     entry["name"])
        if clash is not None and not (entry.get("exists") or {}).get("same_members"):
            number = 2
            while coverage.class_named("net", f"{entry['name']}_{number}") is not None:
                number += 1
            tool_notes.append(f"The PCB already has a class {clash.name} with other members: "
                              f"proposed as {entry['name']}_{number}.")
            entry["name"] = f"{entry['name']}_{number}"
    ddr = _ddr_groups(design, pairs, coverage)
    for entry in [*classes, *ddr]:
        same = (entry.get("exists") or {}).get("same_members")
        if same:
            # The PCB already holds this class: the plan uses its name.
            entry["proposed_name"] = entry["name"]
            entry["name"] = same[0]
    matching = _pair_matching(classes)
    taken = {net for item in ddr for net in item["members"]}
    matching += ddr + _bus_groups(design, taken, coverage)
    placement = {
        "decoupling": _decoupling(design, DEFAULT_MAX_DISTANCE_MIL),
        "tvs": _tvs(design),
        "crystal": _crystals(design),
        "smps": _smps(design),
    }
    guidance = placement_guidance(index is not None)
    zones = _zones(design, {net for entry in classes if entry.get("role") == "rf"
                            for net in entry["members"]})
    designer = _designer_notes(notes)
    profiles_by_name = {profile["name"]: profile for profile in profiles}
    outer_layers = frozenset()
    if index is not None:
        try:
            names = list(index.signal_layers().values())
            outer_layers = frozenset(name.lower() for name in (names[:1] + names[-1:]))
        except Exception:  # no readable stack: the first solved layer stands for outer
            pass
    proposed, covered = _proposed_plan(classes, matching, assumptions.units, profiles_by_name,
                                       outer_layers)
    kind = (project_type or {}).get("type", "classic")
    existing: dict = {"project_type": kind}
    if project_type and project_type.get("evidence"):
        existing["project_type_evidence"] = project_type["evidence"][:3]
    existing["schematic_directives"] = {
        "net_class_nets": len(directives["class"]),
        "differential_pair_nets": len(directives["pair"]),
        "rule_parameters": directives["rules"],
    }
    if directive_summary:
        existing["directive_sync"] = {key: value for key, value in directive_summary.items()
                                      if key in ("net_class", "diff_pair", "rule")}
    if index is not None:
        pcb = index.pcb
        existing["pcb"] = {
            "net_classes": sum(1 for c in pcb.classes if c.kind == "net" and not c.superclass),
            "pair_classes": sum(1 for c in pcb.classes if c.kind == "diff_pair"
                                and not c.superclass),
            "differential_pairs": len(pcb.diff_pairs),
            "rules": dict(Counter(rule.kind for rule in pcb.rules if rule.kind in RULE_KINDS)),
            "impedance_profiles": [f"{p['name']} ({p['type']} {p['target_ohm']} ohm)"
                                   for p in profiles][:10],
        }
    if covered:
        existing["rules_already_covered"] = covered
    if kind == "constraint_manager":
        tool_notes.append("Constraint Manager project: enter the plan in the Constraint "
                          "Manager (generate_rule_file's steps say how); schematic directives "
                          "are ignored there unless imported.")
    tool_notes.append(TYPICAL_NOTE)
    if compiled is None:
        tool_notes.append("The sheets were not compiled offline: schematic directives and "
                          "pin positions are not read, so decoupling capacitors are placed "
                          "by sheet only.")
    summary = {
        "classes": dict(Counter(_class_role(entry) for entry in classes)),
        "pairs": len(pairs),
        "length_matching": dict(Counter(item["kind"] for item in matching)),
        "placement": {key: len(value) for key, value in placement.items()},
        "zones": len(zones),
        "designer_notes": len(designer),
        "plan": {"classes": len(proposed["classes"]), "rules": len(proposed["rules"])},
        "needs_input": len(needs),
    }
    return {
        "existing": existing,
        "assumptions": {"units": assumptions.units, "temp_rise_c": assumptions.temp_rise_c,
                        "copper": thickness,
                        "current_given": sorted(assumptions.current or {}),
                        "hv_given": sorted(assumptions.hv or {})},
        "summary": summary,
        "needs_input": needs,
        "net_classes": classes,
        "length_matching": matching,
        "placement": placement,
        "placement_guidance": guidance,
        "zones": zones,
        "designer_notes": designer,
        "plan": proposed,
        "notes": tool_notes,
    }


def _class_role(entry: dict) -> str:
    if entry["kind"] == "diff_pair":
        return "differential_pair"
    return entry.get("role") or "net"


def build_brief_cached(netlist: dict, plan=None, project_params=None, *, index=None,
                       **kwargs) -> dict:
    """build_brief cached on the netlist object for these arguments (the last CACHE_SIZE),
    so paging reuses it: treat the result as read-only."""
    notes = kwargs.get("notes") or []
    # The variant plan is rebuilt per call: keyed by what it says, not by its identity.
    variant = None if plan is None else (
        getattr(plan, "name", None), tuple(sorted(getattr(plan, "not_fitted", ()) or ())),
        repr(sorted((getattr(plan, "params", {}) or {}).items())),
        repr(sorted((getattr(plan, "alternates", {}) or {}).items())))
    key = (id(netlist), variant, id(kwargs.get("compiled")), repr(kwargs.get("assumptions")),
           repr(kwargs.get("project_type")), repr(kwargs.get("directive_summary")),
           hash(tuple((row.get("document"), row.get("kind"), row.get("text"))
                      for row in notes)))
    cache = (index.__dict__ if index is not None else _MODULE_CACHE).setdefault(
        "_constraints_brief_cache", {})
    held = cache.get(key)
    if held is not None and held[0] is netlist:
        return held[1]
    model = build_brief(netlist, plan, project_params, index=index, **kwargs)
    if len(cache) >= CACHE_SIZE:
        cache.pop(next(iter(cache)))
    cache[key] = (netlist, model)
    return model


_MODULE_CACHE: dict = {}


def _filter_sheet(items: list[dict], sheet: str | None) -> list[dict]:
    if not sheet:
        return items
    wanted = sheet.strip().lower()
    if wanted.endswith(".schdoc"):
        wanted = wanted[:-len(".schdoc")]
    return [item for item in items if any((name or "").lower() == wanted
                                          for name in item.get("sheets") or [])]


def _shrink(item: dict, member_limit: int) -> dict:
    out = dict(item)
    for key in ("members", "pairs", "capacitors", "measured", "currents", "evidence",
                "load_capacitors", "output_capacitors", "input_capacitors", "nets", "pins"):
        value = out.get(key)
        if isinstance(value, list) and len(value) > member_limit:
            out[key] = value[:member_limit]
            out[f"{key}_shown"] = member_limit
            if key in ("members", "pairs"):
                out.setdefault("members_total", len(value))
    pcb = out.get("pcb")
    if isinstance(pcb, dict) and isinstance(pcb.get("measured"), list) and \
            len(pcb["measured"]) > member_limit:
        out["pcb"] = {**pcb, "measured": pcb["measured"][:member_limit]}
    return out


PLAN_POINTER = ("The plan is left out of this page to fit: get_constraints_brief("
                "sections=['plan']) gives it whole; generate_rule_file() without plan= uses "
                "it as proposed.")


def _plan_view(plan: dict, offset: int, plan_limit) -> tuple[dict, bool]:
    """The plan for one page and whether more plan pages follow. plan_limit None: whole
    on the first page (offset 0), a pointer on later pages; 'pointer': counts only; a
    number: that many classes from offset, each rule with the class it is scoped to (rules
    scoped to no plan class on the first page), so a page never holds a rule without its
    class."""
    head = {"schema": plan["schema"], "units": plan["units"]}
    counts = {"classes_total": len(plan["classes"]), "rules_total": len(plan["rules"])}
    if plan_limit == "pointer":
        return {**head, **counts, "omitted": PLAN_POINTER}, False
    if plan_limit is None:
        if offset:
            return {**head, **counts, "omitted": "the plan is given whole on the first page "
                                                 "(offset 0)."}, False
        return {**head, "classes": plan["classes"], "rules": plan["rules"],
                "notes": plan["notes"]}, False
    classes = plan["classes"][offset:offset + plan_limit]
    names = {cls["name"] for cls in classes}
    all_names = {cls["name"] for cls in plan["classes"]}

    def scoped(rule: dict) -> str | None:
        scope = rule["scope"] if isinstance(rule.get("scope"), dict) else {}
        return scope.get("net_class") or scope.get("diff_pair_class")

    rules = [rule for rule in plan["rules"] if scoped(rule) in names
             or (offset == 0 and scoped(rule) not in all_names)]
    more = offset + plan_limit < len(plan["classes"])
    view = {**head, "classes": classes, "rules": rules, "notes": plan["notes"], **counts,
            "paged": f"classes {offset + 1}-{offset + len(classes)} of "
                     f"{len(plan['classes'])} with their rules: join the pages before "
                     "passing the plan to generate_rule_file"}
    return view, more


def render_brief(model: dict, sections=None, sheet: str | None = None, offset: int = 0,
                 limit: int = 100, member_limit: int = LISTED, plan_limit=None) -> dict:
    """One page: the chosen sections' lists from offset (each list pages alone with the
    same offset and limit), narrowed to a sheet; member lists cut to member_limit. The
    plan's classes and members are never cut: it comes whole (plan_limit None), as a
    pointer ('pointer') or paged by class with each rule beside its class (a number)."""
    chosen = list(sections or SECTIONS)
    offset = max(0, int(offset or 0))
    limit = max(1, int(limit))
    result: dict = {key: model[key] for key in ("existing", "assumptions", "summary")}
    result["needs_input"] = model["needs_input"]
    more = False
    totals: dict = {}

    def page(items: list[dict], key: str, shrink: bool = True) -> list[dict]:
        nonlocal more
        items = _filter_sheet(items, sheet) if key != "plan" else items
        totals[key] = len(items)
        if offset + limit < len(items):
            more = True
        chunk = items[offset:offset + limit]
        return [_shrink(item, member_limit) for item in chunk] if shrink else chunk

    for section in chosen:
        if section == "placement":
            result["placement"] = {kind: page(model["placement"][kind], kind)
                                   for kind in PLACEMENT_KINDS}
            result["placement_guidance"] = model["placement_guidance"]
        elif section == "plan":
            result["plan"], plan_more = _plan_view(model["plan"], offset, plan_limit)
            more = more or plan_more
            totals["plan_classes"] = len(model["plan"]["classes"])
            totals["plan_rules"] = len(model["plan"]["rules"])
        else:
            result[section] = page(model[section], section)
    result["totals"] = totals
    result["offset"] = offset
    result["has_more"] = more
    if more:
        result["next_offset"] = offset + limit
    if sheet:
        result["filtered"] = {"sheet": sheet}
    result["notes"] = model["notes"]
    return result


def parse_sections(sections) -> tuple[list[str] | None, dict | None]:
    if not sections:
        return None, None
    chosen = [str(section).strip().lower() for section in sections if str(section).strip()]
    unknown = [section for section in chosen if section not in SECTIONS]
    if unknown:
        return None, {"error": "invalid_sections",
                      "message": f"Unknown sections {unknown}; use {list(SECTIONS)}."}
    return list(dict.fromkeys(chosen)), None


# ---------- plan validation ----------

_VALUE_KEYS = {
    "Width": {"min_width": True, "preferred_width": False, "max_width": True},
    "Clearance": {"clearance": True},
    "DiffPairsRouting": {"min_width": True, "preferred_width": False, "max_width": True,
                         "min_gap": True, "preferred_gap": False, "max_gap": True,
                         "max_uncoupled_length": False},
    "MatchedLengths": {"tolerance": True, "within_pair": False},
    "Length": {"min_length": True, "max_length": True},
}
_SCOPE_KEYS = ("net_class", "diff_pair_class", "net", "diff_pair", "query")
_BAD_NAME = re.compile(r"[|'\r\n\x00]")
# Altium stores lengths in 1/10000 mil (int32): values are written to 4 decimals of a mil
# as it writes them, and a length must fit its coordinates.
MIL_DECIMALS = 4
ALTIUM_RESOLUTION_MIL = 10.0 ** -MIL_DECIMALS
MAX_FEATURE_MIL = 10_000.0  # a width, gap, clearance or tolerance: 254 mm
MAX_LENGTH_MIL = 200_000.0  # a routed length (Length rule): about 5 m, under int32 units


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _length_problem(key: str, mil: float, units: str, scale: float) -> str | None:
    """Why a length value cannot be written, or None."""
    if mil < 0:
        return f"{key} must be a number of {units}, 0 or more"
    limit = MAX_LENGTH_MIL if key in ("min_length", "max_length", "max_uncoupled_length") \
        else MAX_FEATURE_MIL
    if mil > limit:
        return f"{key} is above {limit / scale:g} {units}, beyond any board"
    if 0 < mil < ALTIUM_RESOLUTION_MIL / 2:
        return (f"{key} rounds to 0 at Altium's resolution ({ALTIUM_RESOLUTION_MIL:g} mil): "
                "give 0 or a larger value")
    return None


def _scope_text(scope, problems: list[str], where: str) -> str | None:
    if scope is None or (isinstance(scope, str) and scope.strip().lower() == "all"):
        return "All"
    if isinstance(scope, str):
        scope = {"query": scope}
    if not isinstance(scope, dict) or len(scope) != 1:
        problems.append(f"{where}: a scope is 'All' or one of {list(_SCOPE_KEYS)} "
                        "with a name")
        return None
    (key, value), = scope.items()
    if key not in _SCOPE_KEYS or not isinstance(value, str) or not value.strip():
        problems.append(f"{where}: a scope is 'All' or one of {list(_SCOPE_KEYS)} "
                        "with a name")
        return None
    value = value.strip()
    if not value.isascii():
        problems.append(f"{where}: the scope '{value}' holds a non-ASCII character (the "
                        ".RUL file is ASCII)")
        return None
    if key == "query":
        if "|" in value or "\n" in value or "\r" in value:
            problems.append(f"{where}: the query holds '|' or a line break")
            return None
        try:
            parse_scope(value)
        except ScopeSyntaxError as error:
            problems.append(f"{where}: the query does not parse ({error})")
            return None
        return value
    if _BAD_NAME.search(value):
        problems.append(f"{where}: '{value}' holds a quote, '|' or a line break")
        return None
    function = {"net_class": "InNetClass", "diff_pair_class": "InDifferentialPairClass",
                "net": "InNet", "diff_pair": "InDifferentialPair"}[key]
    return f"{function}('{value}')"


def validate_plan(plan, default_units: str = "mm") -> tuple[dict | None, list[str]]:
    """The plan normalised to mil ({"classes": [...], "rules": [...]} with each rule's
    scope texts and values in mil), or None and the problems found."""
    problems: list[str] = []
    if not isinstance(plan, dict):
        return None, ["the plan is a JSON object with 'units', 'classes' and 'rules'"]
    units = plan.get("units", default_units)
    if units not in ("mm", "mil"):
        problems.append(f"units must be 'mm' or 'mil', not {units!r}")
        units = "mil"
    scale = MIL_PER_MM if units == "mm" else 1.0
    classes, names = [], set()
    raw_classes = plan.get("classes") or []
    if not isinstance(raw_classes, list):
        problems.append("classes must be a list")
        raw_classes = []
    for number, cls in enumerate(raw_classes, 1):
        where = f"class {number}"
        if not isinstance(cls, dict):
            problems.append(f"{where}: an object with name, kind and members")
            continue
        name = str(cls.get("name") or "").strip()
        where = f"class {name or number}"
        kind = cls.get("kind", "net")
        if not name or _BAD_NAME.search(name) or not name.isascii():
            problems.append(f"{where}: a class needs an ASCII name without quotes, '|' or "
                            "line breaks")
            continue
        if name.lower() in names:
            problems.append(f"{where}: the name is used twice")
            continue
        if kind not in CLASS_KINDS:
            problems.append(f"{where}: kind must be one of {list(CLASS_KINDS)}")
            continue
        members = cls.get("members")
        if not isinstance(members, list) or not members:
            problems.append(f"{where}: a class needs at least one member")
            continue
        clean = []
        for member in members:
            if kind == "diff_pair":
                if isinstance(member, str):
                    member = {"pair": member}
                if not isinstance(member, dict) or not str(member.get("pair") or "").strip():
                    problems.append(f"{where}: a pair member is {{'pair', 'positive', "
                                    "'negative'}} or a pair name")
                    break
                pair = {key: str(member[key]).strip() for key in ("pair", "positive", "negative")
                        if member.get(key)}
                if any(_BAD_NAME.search(value) or not value.isascii()
                       for value in pair.values()):
                    problems.append(f"{where}: member {pair['pair']} holds a quote, '|', a "
                                    "line break or a non-ASCII character")
                    break
                clean.append(pair)
            else:
                if not isinstance(member, str) or not member.strip() or \
                        _BAD_NAME.search(member) or not member.isascii():
                    problems.append(f"{where}: net members are ASCII net names without "
                                    "quotes, '|' or line breaks")
                    break
                clean.append(member.strip())
        else:
            names.add(name.lower())
            classes.append({"name": name, "kind": kind, "members": clean,
                            "basis": str(cls.get("basis") or ""),
                            "exists_on_pcb": cls.get("exists_on_pcb")})
    rules, rule_names = [], set()
    raw_rules = plan.get("rules") or []
    if not isinstance(raw_rules, list):
        problems.append("rules must be a list")
        raw_rules = []
    for number, rule in enumerate(raw_rules, 1):
        if not isinstance(rule, dict):
            problems.append(f"rule {number}: an object with name, kind, scope and values")
            continue
        name = str(rule.get("name") or "").strip()
        where = f"rule {name or number}"
        kind = rule.get("kind")
        if kind not in RULE_KINDS:
            problems.append(f"{where}: unknown rule kind {kind!r}; use {list(RULE_KINDS)}")
            continue
        if not name or _BAD_NAME.search(name) or not name.isascii():
            problems.append(f"{where}: a rule needs an ASCII name without quotes, '|' or "
                            "line breaks")
            continue
        if name.lower() in rule_names:
            problems.append(f"{where}: the name is used twice")
            continue
        scope1 = _scope_text(rule.get("scope"), problems, where)
        if kind != "Clearance" and rule.get("scope2") not in (None, "All", "all"):
            problems.append(f"{where}: scope2 is for Clearance rules only (a {kind} rule has "
                            "one scope)")
            continue
        scope2 = _scope_text(rule.get("scope2"), problems, where) if kind == "Clearance" \
            else "All"
        if scope1 is None or scope2 is None:
            continue
        values = rule.get("values")
        if not isinstance(values, dict):
            problems.append(f"{where}: values is an object of {list(_VALUE_KEYS[kind])}")
            continue
        allowed = _VALUE_KEYS[kind]
        unknown = [key for key in values if key not in allowed]
        missing = [key for key, required in allowed.items() if required and key not in values]
        if unknown or missing:
            problems.append(f"{where}: {kind} takes {list(allowed)}"
                            + (f"; unknown {unknown}" if unknown else "")
                            + (f"; missing {missing}" if missing else ""))
            continue
        out: dict = {}
        bad = False
        for key, value in values.items():
            if key == "within_pair":
                if not isinstance(value, bool):
                    problems.append(f"{where}: within_pair is true or false")
                    bad = True
                out[key] = bool(value)
                continue
            number_value = _number(value)
            if number_value is None:
                problems.append(f"{where}: {key} must be a number of {units}, 0 or more")
                bad = True
                continue
            if why := _length_problem(key, number_value * scale, units, scale):
                problems.append(f"{where}: {why}")
                bad = True
                continue
            out[key] = number_value * scale
        inner = rule.get("inner")
        inner_out = None
        if inner is not None:
            if kind not in ("Width", "DiffPairsRouting") or not isinstance(inner, dict) or \
                    any(key not in allowed or key == "max_uncoupled_length" for key in inner):
                problems.append(f"{where}: inner takes the width (and gap) keys of a Width or "
                                "DiffPairsRouting rule")
                bad = True
            else:
                inner_out = {}
                for key, value in inner.items():
                    number_value = _number(value)
                    if number_value is None:
                        problems.append(f"{where}: inner {key} must be a number of {units}")
                        bad = True
                        continue
                    if why := _length_problem(key, number_value * scale, units, scale):
                        problems.append(f"{where}: inner {why}")
                        bad = True
                        continue
                    inner_out[key] = number_value * scale
        if bad:
            continue
        for low, mid, high in (("min_width", "preferred_width", "max_width"),
                               ("min_gap", "preferred_gap", "max_gap")):
            for given in (out, inner_out or {}):
                if low in given and high in given:
                    given.setdefault(mid, given[low])
            # The inner layers take the rule's values where the inner block gives none.
            layers = [("", out)] + ([("inner ", {**out, **inner_out})] if inner_out else [])
            for label, layer_values in layers:
                if low in layer_values and high in layer_values and not (
                        layer_values[low] <= layer_values.get(mid, layer_values[low])
                        <= layer_values[high]):
                    problems.append(f"{where}: {label}{low} <= {mid} <= {high} must hold")
                    bad = True
        if kind == "Length" and out["min_length"] > out["max_length"]:
            problems.append(f"{where}: min_length is above max_length")
            bad = True
        if kind == "MatchedLengths" and "within_pair" not in out:
            out["within_pair"] = scope1.startswith(("InDifferentialPairClass(",
                                                    "InDifferentialPair("))
        priority = rule.get("priority")
        if priority is not None and (isinstance(priority, bool) or not isinstance(priority, int)
                                     or priority < 1):
            problems.append(f"{where}: priority is a whole number from 1")
            bad = True
        profile = rule.get("impedance_profile")
        if profile is not None and (kind not in ("Width", "DiffPairsRouting")
                                    or not isinstance(profile, str)):
            problems.append(f"{where}: impedance_profile names a board profile on a Width or "
                            "DiffPairsRouting rule")
            bad = True
        if bad:
            continue
        rule_names.add(name.lower())
        rules.append({"name": name, "kind": kind, "scope1": scope1, "scope2": scope2,
                      "scope": rule.get("scope"), "values": out, "inner": inner_out,
                      "priority": priority, "impedance_profile": profile,
                      "basis": str(rule.get("basis") or ""),
                      "typical": bool(rule.get("typical"))})
    if not classes and not rules and not problems:
        problems.append("the plan has no classes and no rules")
    defined = {cls["name"].lower(): cls["kind"] for cls in classes}
    for rule in rules:
        scope = rule["scope"] if isinstance(rule["scope"], dict) else {}
        for key, kind in (("net_class", "net"), ("diff_pair_class", "diff_pair")):
            name = scope.get(key)
            if name and defined.get(name.lower(), kind) != kind:
                problems.append(f"rule {rule['name']}: {name} is a "
                                f"{defined[name.lower()]} class in the plan, not a {kind} class")
    given: dict[tuple[str, int], list[str]] = defaultdict(list)
    for rule in rules:
        if rule["priority"] is not None:
            given[(rule["kind"], rule["priority"])].append(rule["name"])
    for (kind, priority), names in given.items():
        if len(names) > 1:
            problems.append(f"rules {', '.join(names)}: two {kind} rules cannot share priority "
                            f"{priority}")
    if problems:
        return None, problems
    return {"units": units, "classes": classes, "rules": rules}, []


# ---------- records ----------

_SLOTS = ["TOPLAYER", *(f"MIDLAYER{n}" for n in range(1, 31)), "BOTTOMLAYER"]
_SLOT_V6 = {"TOPLAYER": 1, "BOTTOMLAYER": 32, **{f"MIDLAYER{n}": n + 1 for n in range(1, 31)}}
_LAYER_KEY = re.compile(r"^(TOPLAYER|BOTTOMLAYER|MIDLAYER\d+)(?:_(\{[^}]+\}))?_"
                        r"(MINWIDTH|MAXWIDTH|PREFWIDTH|MINGAP|MAXGAP|PREFGAP)$")
_NETSCOPE = {"Width": "AnyNet", "Clearance": "DifferentNets", "DiffPairsRouting": "AnyNet",
             "MatchedLengths": "DifferentNets", "Length": "AnyNet"}
# Rules6 kind ids of the kinds written (the PcbDoc's u16 before each record).
KIND_IDS = {"Clearance": 0, "Width": 2, "Length": 3, "MatchedLengths": 4,
            "DiffPairsRouting": 51}
# Altium writes MAXUNCOUPLEDLENGTH on every DiffPairsRouting record; its default rule
# holds 500 mil (8 of the 10 example records).
DEFAULT_MAX_UNCOUPLED_MIL = 500.0
# Altium's default rule names: a file rule of the same name may replace the board's.
ALTIUM_DEFAULT_NAMES = frozenset({"clearance", "width", "diffpairsrouting", "matchedlengths",
                                  "length"})


def _mil_value(mil: float) -> str:
    """A length as Altium writes it: mil to 4 decimals (its 1/10000 mil unit), trailing
    zeros dropped (9.8425mil, 10mil)."""
    text = f"{round(mil, MIL_DECIMALS):.{MIL_DECIMALS}f}".rstrip("0").rstrip(".")
    return f"{text if text not in ('', '-0') else '0'}mil"


def _unique_id(name: str, taken: set[str]) -> str:
    seed = hashlib.sha1(name.encode("utf-8")).digest()
    counter = 0
    while True:
        digest = hashlib.sha1(seed + counter.to_bytes(4, "little")).digest()
        uid = "".join(chr(ord("A") + byte % 26) for byte in digest[:8])
        if uid not in taken:
            taken.add(uid)
            return uid
        counter += 1


def _layer_template(index, kind: str) -> tuple[list[tuple[str, str | None]], dict[str, str]]:
    """The per-layer slots (slot, substack guid or None) to write for a kind, mirroring the
    board's own rule of that kind (its keys and SUBSTACKn) and adding the board's signal
    layers that rule leaves out (with its substack's guid); else every V5 slot plainly."""
    if index is not None:
        for rule in sorted((r for r in index.pcb.rules if r.kind == kind),
                           key=lambda r: r.priority):
            slots, substacks = [], {}
            for key, value in rule.properties.items():
                if re.fullmatch(r"SUBSTACK\d+", key):
                    substacks[key] = value
                match = _LAYER_KEY.match(key)
                if match and (match.group(1), match.group(2)) not in slots:
                    slots.append((match.group(1), match.group(2)))
            if not slots:
                continue
            guids = Counter(guid for _, guid in slots if guid)
            guid = guids.most_common(1)[0][0] if guids else None
            covered = {slot for slot, _ in slots}
            try:
                signal = list(index.signal_layers())
            except Exception:  # no readable stack: the rule's own slots only
                signal = []
            for v6 in signal:
                slot = next((name for name, number in _SLOT_V6.items() if number == v6), None)
                if slot and slot not in covered:
                    slots.append((slot, guid))
            return slots, substacks
    return [(slot, None) for slot in _SLOTS], {}


def _per_layer(slot: str, values: dict, inner: dict | None, prefix: str) -> dict:
    external = _SLOT_V6[slot] in (1, 32)
    chosen = values if external or not inner else {**values, **inner}
    return {f"{prefix}{name}": chosen[key] for key, name in (
        ("min_width", "MINWIDTH"), ("max_width", "MAXWIDTH"), ("preferred_width", "PREFWIDTH"),
        ("min_gap", "MINGAP"), ("max_gap", "MAXGAP"), ("preferred_gap", "PREFGAP"))
        if key in chosen}


def rule_record(rule: dict, priority: int, uid: str, template, profiles: dict) -> dict:
    """One rule's Rules6 properties, in the order Altium stores them (values in mil, to
    Altium's 4 decimals). profiles: board profile name (lower) -> (id, target ohm). The
    key order per kind follows Altium's own records: Clearance GAP, GENERICCLEARANCE,
    IGNOREPADTOPADCLEARANCEINFOOTPRINT, OBJECTCLEARANCES (empty: the generic gap for every
    object pair); Width / DiffPairsRouting their limits, SUBSTACKn, the per-layer keys,
    then IMPEDANCEPROFILEDRIVEN / ID / VALUE and MAXUNCOUPLEDLENGTH."""
    values, inner = rule["values"], rule.get("inner")
    comment = re.sub(r"[|\r\n]+", " ", ("altium-copilot constraint plan"
                                        + (f": {rule['basis']}" if rule.get("basis") else "")))
    record = {
        "SELECTION": "FALSE", "LAYER": "TOP", "LOCKED": "FALSE", "POLYGONOUTLINE": "FALSE",
        "USERROUTED": "TRUE", "KEEPOUT": "FALSE", "UNIONINDEX": "0",
        "RULEKIND": rule["kind"], "NETSCOPE": _NETSCOPE[rule["kind"]],
        "LAYERKIND": "SameLayer", "SCOPE1EXPRESSION": rule["scope1"],
        "SCOPE2EXPRESSION": rule["scope2"], "NAME": rule["name"], "ENABLED": "TRUE",
        "PRIORITY": str(priority), "COMMENT": comment[:200].encode("ascii", "replace")
        .decode("ascii"), "UNIQUEID": uid, "DEFINEDBYLOGICALDOCUMENT": "FALSE",
    }
    kind = rule["kind"]
    slots, substacks = template
    if kind == "Clearance":
        record["GAP"] = _mil_value(values["clearance"])
        record["GENERICCLEARANCE"] = _mil_value(values["clearance"])
        record["IGNOREPADTOPADCLEARANCEINFOOTPRINT"] = "FALSE"
        record["OBJECTCLEARANCES"] = ""
    elif kind == "Width":
        record["MAXLIMIT"] = _mil_value(values["max_width"])
        record["MINLIMIT"] = _mil_value(values["min_width"])
        record["PREFEREDWIDTH"] = _mil_value(values["preferred_width"])
    elif kind == "DiffPairsRouting":
        record["MAXLIMIT"] = _mil_value(values["max_gap"])
        record["MINLIMIT"] = _mil_value(values["min_gap"])
        record["MOSTFREQGAP"] = _mil_value(values["preferred_gap"])
    elif kind == "MatchedLengths":
        within = values.get("within_pair", False)
        record["TOLERANCE"] = _mil_value(values["tolerance"])
        record["CHECKNETSINDIFFPAIR"] = "TRUE" if within else "FALSE"
        for key in ("CHECKDIFFPAIRVSDIFFPAIR", "CHECKXSIGNALS", "CHECKOTHERS"):
            record[key] = "FALSE" if within else "TRUE"
    elif kind == "Length":
        record["MAXLIMIT"] = _mil_value(values["max_length"])
        record["MINLIMIT"] = _mil_value(values["min_length"])
    if kind in ("Width", "DiffPairsRouting"):
        for key, value in substacks.items():
            record[key] = value
        for slot, guid in slots:
            prefix = f"{slot}_{guid}_" if guid else f"{slot}_"
            for key, value in _per_layer(slot, values, inner, prefix).items():
                record[key] = _mil_value(value)
        profile = profiles.get((rule.get("impedance_profile") or "").lower())
        if profile is not None and profile[1] is not None:
            record["IMPEDANCEPROFILEDRIVEN"] = "TRUE"
            record["IMPEDANCEPROFILEID"] = profile[0]
            record["IMPEDANCEPROFILEVALUE"] = f"{profile[1]:.6f}"
    if kind == "DiffPairsRouting":
        record["MAXUNCOUPLEDLENGTH"] = _mil_value(values.get("max_uncoupled_length",
                                                             DEFAULT_MAX_UNCOUPLED_MIL))
    return record


def record_line(record: dict) -> str:
    return "|" + "|".join(f"{key}={value}" for key, value in record.items())


def _signal_layers(index) -> dict[int, str]:
    if index is not None:
        try:
            layers = index.signal_layers()
            if layers:
                return layers
        except Exception:  # a board without a readable stack: every slot is checked
            pass
    return {1: "Top Layer", **{n + 1: f"Mid-Layer {n}" for n in range(1, 31)},
            32: "Bottom Layer"}


def _expected_layers(rule: dict, layers: dict[int, str]) -> dict[str, dict]:
    if rule["kind"] not in ("Width", "DiffPairsRouting"):
        return {}
    values, inner = rule["values"], rule.get("inner") or {}
    out = {}
    for v6, name in layers.items():
        chosen = values if v6 in (1, 32) or not inner else {**values, **inner}
        out[name] = {key: chosen[key] for key in ("min_width", "preferred_width", "max_width",
                                                  "min_gap", "preferred_gap", "max_gap")
                     if key in chosen}
    return out


def _close(want, got) -> bool:
    """The value read back is the wanted one at Altium's resolution (written to 4
    decimals of a mil, so at most ROUND_TRIP_TOLERANCE_MIL from the plan's value)."""
    if isinstance(want, bool) or isinstance(got, bool):
        return want == got
    if isinstance(want, (int, float)) and isinstance(got, (int, float)):
        return abs(round(want, MIL_DECIMALS) - got) <= 1e-9 and \
            abs(want - got) <= ROUND_TRIP_TOLERANCE_MIL + 1e-9
    return want == got


def round_trip(lines: list[str], rules: list[dict], priorities: list[int], index=None,
               substacks: list[dict] | None = None,
               written: list[dict] | None = None) -> dict:
    """Parse the written lines back with the PcbDoc's decoder and compare each with the
    rule it was written from: every key and value as written (written: the records'
    property dicts), then kind, name, scopes, priority and values (per layer for Width
    and DiffPairsRouting) as the rule reader decodes them."""
    records = []
    unreadable = []
    for number, line in enumerate(lines):
        if not line.isascii() or "\r" in line or "\n" in line:
            unreadable.append(number)
        props = parse_text_properties(line.encode("ascii", "replace"))
        records.append((KIND_IDS.get(props.get("RULEKIND", ""), 0), props))
    decoded = decode_rules(records)
    layers = _signal_layers(index)
    mismatches = []
    identical = 0
    for number, (rule, priority, back) in enumerate(zip(rules, priorities, decoded)):
        problems = []
        if number in unreadable:
            problems.append("the line is not one ASCII line")
        if written is not None and number < len(written):
            props = records[number][1]
            want = {key.upper(): value for key, value in written[number].items()}
            if list(props) != list(want):
                problems.append("keys read back differ from those written")
            for key, value in want.items():
                if props.get(key) != value:
                    problems.append(f"{key}: wrote {value!r}, read {props.get(key)!r}")
                    break
        for label, want, got in (("kind", rule["kind"], back.kind),
                                 ("name", rule["name"], back.name),
                                 ("scope", rule["scope1"], back.scope1),
                                 ("scope2", rule["scope2"], back.scope2),
                                 ("priority", priority, back.priority)):
            if want != got:
                problems.append(f"{label}: wrote {want!r}, read {got!r}")
        values = rule_values(back, layers, substacks)
        expected = rule["values"]
        flat = {"Clearance": {"clearance": "clearance_mil"},
                "MatchedLengths": {"tolerance": "tolerance_mil", "within_pair": "within_pair"},
                "Length": {"min_length": "min_length_mil", "max_length": "max_length_mil"},
                "DiffPairsRouting": {"max_uncoupled_length": "max_uncoupled_length_mil"},
                }.get(rule["kind"], {})
        for key, read_key in flat.items():
            if key in expected and not _close(expected[key], values.get(read_key)):
                problems.append(f"{key}: wrote {expected[key]!r}, read "
                                f"{values.get(read_key)!r}")
        if rule["kind"] in ("Width", "DiffPairsRouting"):
            read_layers = dict(values.get("layers_mil") or {})
            for fields in (values.get("substack_layers_mil") or {}).values():
                read_layers.update(fields)
            if not read_layers and rule["kind"] == "Width":
                read_layers = {name: {"min_width": values.get("min_width_mil"),
                                      "preferred_width": values.get("preferred_width_mil"),
                                      "max_width": values.get("max_width_mil")}
                               for name in layers.values()}
            for name, fields in _expected_layers(rule, layers).items():
                got = read_layers.get(name) or {}
                for key, want in fields.items():
                    if not _close(want, got.get(key)):
                        problems.append(f"{name} {key}: wrote {want!r}, read {got.get(key)!r}")
                        break
        if problems:
            mismatches.append({"rule": rule["name"], "problems": problems[:5]})
        else:
            identical += 1
    if len(decoded) != len(rules):
        mismatches.append({"rule": None, "problems": [
            f"wrote {len(rules)} records, read {len(decoded)}"]})
    return {"records": len(lines), "read_back": len(decoded), "identical": identical,
            "mismatches": mismatches}


# ---------- the files ----------

def _csv_text(classes: list[dict]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(["class", "kind", "member", "positive_net", "negative_net",
                     "exists_on_pcb"])
    for cls in classes:
        exists = cls.get("exists_on_pcb") or ""
        for member in cls["members"]:
            if cls["kind"] == "diff_pair":
                writer.writerow([cls["name"], "diff_pair", member["pair"],
                                 member.get("positive", ""), member.get("negative", ""), exists])
            else:
                writer.writerow([cls["name"], "net", member, "", "", exists])
    return buffer.getvalue()


def _values_text(rule: dict, units: str) -> str:
    def text(mil: float) -> str:
        return _mil_text(mil, units)

    values = rule["values"]
    kind = rule["kind"]
    if kind == "Clearance":
        return f"clearance {text(values['clearance'])}"
    if kind == "Width":
        out = (f"min {text(values['min_width'])}, preferred {text(values['preferred_width'])}, "
               f"max {text(values['max_width'])}")
        if rule.get("inner"):
            inner = rule["inner"]
            out += (f"; inner layers min {text(inner.get('min_width', values['min_width']))}, "
                    f"preferred {text(inner.get('preferred_width', values['preferred_width']))}")
        return out
    if kind == "DiffPairsRouting":
        out = (f"width {text(values['min_width'])} / {text(values['preferred_width'])} / "
               f"{text(values['max_width'])}, gap {text(values['min_gap'])} / "
               f"{text(values['preferred_gap'])} / {text(values['max_gap'])} (min / preferred "
               "/ max)")
        if "max_uncoupled_length" in values:
            out += f", max uncoupled length {text(values['max_uncoupled_length'])}"
        return out
    if kind == "MatchedLengths":
        return (f"tolerance {text(values['tolerance'])}, "
                + ("within each differential pair" if values.get("within_pair")
                   else "across the group"))
    return f"length {text(values['min_length'])} to {text(values['max_length'])}"


_EDITOR_PLACE = {"Width": "Routing > Width", "Clearance": "Electrical > Clearance",
                 "DiffPairsRouting": "Routing > Differential Pairs Routing",
                 "MatchedLengths": "High Speed > Matched Lengths",
                 "Length": "High Speed > Length"}
_CM_PLACE = {"Width": "Physical constraints: track width",
             "Clearance": "Clearance constraints",
             "DiffPairsRouting": "Physical constraints: differential pair width and gap",
             "MatchedLengths": "Electrical constraints: matched length",
             "Length": "Electrical constraints: length"}


def steps_markdown(plan: dict, rules: list[dict], priorities: list[int], *, project_type: str,
                   rul_name: str | None, csv_name: str | None, units: str,
                   pcb_pairs: set | None, renamed: list[dict], project: str | None,
                   existing_by_kind: dict[str, list[str]], context: dict | None = None,
                   undefined: list[str] | None = None, board_read: bool = True) -> str:
    cm = project_type == "constraint_manager"
    lines = [f"# Constraint steps{f' for {project}' if project else ''}", "",
             "Generated by altium-copilot (generate_rule_file) from a constraint plan. Nothing "
             "in the design was changed: follow these steps in Altium.", ""]
    if cm:
        lines += ["**This project uses the Constraint Manager** ([Design] "
                  "ConstraintManagerFlow): enter classes, pairs and rules in Design > "
                  "Constraint Manager. Schematic directives and rules made in the classic "
                  "Rules editor are not its source; they are ignored unless imported into "
                  "it.", ""]
    else:
        lines += ["This is a classic project: classes and pairs come from schematic "
                  "directives (kept by Update PCB) or are made on the PCB; rules from the "
                  "PCB Rules and Constraints Editor.", ""]
    lines += [f"Values are in {units}.", ""]
    net_classes = [cls for cls in plan["classes"] if cls["kind"] == "net"]
    pair_classes = [cls for cls in plan["classes"] if cls["kind"] == "diff_pair"]
    if net_classes:
        lines += ["## 1. Net classes", ""]
        for cls in net_classes:
            members = ", ".join(cls["members"])
            count = len(cls["members"])
            lines.append(f"### {cls['name']} ({count} net{'s' if count != 1 else ''})")
            if cls.get("basis"):
                lines.append(f"Basis: {cls['basis']}")
            lines.append(f"Members: {members}")
            if cls.get("exists_on_pcb"):
                lines.append(f"- Already on the PCB with these members ({cls['exists_on_pcb']}): "
                             "nothing to create.")
            elif cm:
                lines.append(f"- Constraint Manager: select the member nets and create the "
                             f"net class {cls['name']}.")
            else:
                lines.append(f"- Schematic (kept by Update PCB): a Parameter Set directive "
                             f"(Place > Directives > Parameter Set) on a wire of each member "
                             f"net, or a Blanket over them, with the parameter ClassName = "
                             f"{cls['name']}; then Design > Update PCB Document.")
                lines.append(f"- Or on the PCB: Design > Classes > Net Classes > Add Class "
                             f"{cls['name']}, then move the member nets in.")
            lines.append("")
    if pair_classes:
        lines += ["## 2. Differential pairs and pair classes", ""]
        for cls in pair_classes:
            count = len(cls["members"])
            lines.append(f"### {cls['name']} ({count} pair{'s' if count != 1 else ''})")
            if cls.get("basis"):
                lines.append(f"Basis: {cls['basis']}")
            undeclared = []
            for member in cls["members"]:
                legs = (f" ({member.get('positive')} / {member.get('negative')})"
                        if member.get("positive") else "")
                declared = pcb_pairs is not None and member["pair"].lower() in pcb_pairs
                lines.append(f"- {member['pair']}{legs}"
                             + (" — declared on the PCB" if declared else ""))
                if not declared:
                    undeclared.append(member["pair"])
            if undeclared:
                if cm:
                    lines.append(f"- Constraint Manager: create the differential pairs "
                                 f"{_names(undeclared, 8)} from their nets.")
                else:
                    lines.append(f"- Pairs not declared ({_names(undeclared, 8)}): a "
                                 "Differential Pair directive on both nets of each (their "
                                 "names end with the project's _P / _N suffixes), or on the "
                                 "PCB: the PCB panel in Differential Pairs Editor mode > Add.")
            if cls.get("exists_on_pcb"):
                lines.append(f"- The pair class already exists on the PCB as "
                             f"{cls['exists_on_pcb']}.")
            elif cm:
                lines.append(f"- Constraint Manager: create the differential pair class "
                             f"{cls['name']} with these pairs.")
            else:
                lines.append(f"- Class: ClassName = {cls['name']} on the Differential Pair "
                             f"directives, or on the PCB: Design > Classes > Differential Pair "
                             f"Classes > Add Class {cls['name']}.")
            lines.append("")
    if rules:
        lines += ["## 3. Rules", ""]
        if renamed:
            lines.append("Renamed to avoid the board's own rule names: " + "; ".join(
                f"{item['from']} -> {item['to']}" for item in renamed) + ".")
            lines.append("")
        lines.append("Create the classes and pairs above first: a rule scoped to a class "
                     "that does not exist yet matches nothing.")
        lines.append("")
        for note in undefined or []:
            lines.append(f"- Check: {note}")
        if not board_read:
            lines.append("- No PcbDoc was read: the rule names were not checked against the "
                         "board's; a board rule of the same name may be replaced on import.")
        kinds = ", ".join(dict.fromkeys(rule["kind"] for rule in rules))
        if rul_name:
            if cm:
                lines.append(f"- Constraint Manager: its Import Rules (from a .rul file) "
                             f"may take {rul_name}. {UNTESTED_IMPORT}")
            else:
                lines.append(f"- Import: Design > Rules (PCB Rules and Constraints Editor), "
                             f"right-click > Import Rules. In Choose Design Rule Type select "
                             f"only {kinds} (the kinds in the file), pick {rul_name}, and "
                             "answer **No** to 'Clear existing rules prior to import?': Yes "
                             "deletes the board's rules of every kind selected. "
                             f"{UNTESTED_IMPORT}")
        lines.append("- Or enter each rule by hand:")
        lines.append("")
        lines.append("| Rule | Where | Scope | Values | Priority | Basis |")
        lines.append("|---|---|---|---|---|---|")
        for rule, priority in zip(rules, priorities):
            where = _CM_PLACE[rule["kind"]] if cm else _EDITOR_PLACE[rule["kind"]]
            scope = rule["scope1"] + (f" / {rule['scope2']}" if rule["kind"] == "Clearance"
                                      else "")
            basis = (rule.get("basis") or "").replace("|", "/")
            if rule.get("typical"):
                basis = "typical, not a requirement: " + basis
            lines.append(f"| {rule['name']} | {where} | {scope} | "
                         f"{_values_text(rule, units)} | {priority} | {basis} |")
        lines.append("")
        if any(rule["kind"] == "Clearance" for rule in rules):
            lines.append("Clearance rules carry the generic gap for every object pair (an "
                         "empty object matrix, the form most of Altium's own Clearance "
                         "records take): check the rule's matrix after the import.")
            lines.append("")
        context = context or {"kinds": {}, "rules": {}}
        if context["kinds"]:
            where = "the Constraint Manager" if cm else "the Rules editor"
            lines.append("Priorities (a lower number wins). The file numbers the new rules "
                         "from 1 per kind; whether Import Rules keeps or renumbers them is "
                         f"untested, so check the rules' priorities in {where} after the "
                         "import and set the order:")
            for kind, info in context["kinds"].items():
                scoped_all = ", ".join(f"{item['rule']} ({item['priority']})"
                                       for item in info["scoped_all"])
                lines.append(f"- {kind}: the board already has {info['board_rules']} rule"
                             f"{'s' if info['board_rules'] != 1 else ''} (priorities "
                             f"{_names(info['board_priorities'], 8)})"
                             + (f"; its rules scoped All ({scoped_all}) must rank below the "
                                "new ones" if scoped_all else "") + ".")
            for rule, priority in zip(rules, priorities):
                info = context["rules"].get(rule["name"])
                if not info:
                    continue
                parts = []
                if info.get("overlapping_board_rules"):
                    parts.append("the board's " + ", ".join(
                        f"{item['rule']} ({item['priority']})"
                        for item in info["overlapping_board_rules"])
                        + " also take its nets: decide which wins")
                if info.get("same_number_on_board"):
                    parts.append(f"priority {priority} is also the board's "
                                 f"{_names(info['same_number_on_board'], 3)}")
                lines.append(f"- {rule['name']}: " + "; ".join(parts) + ".")
            lines.append("")
        if not cm:
            busy = {kind: names for kind, names in existing_by_kind.items()
                    if any(rule["kind"] == kind for rule in rules)
                    and kind not in context["kinds"]}
            for kind, names in busy.items():
                lines.append(f"- {kind}: the board's rules scoped All ({_names(names, 4)}) "
                             "must stay below the new ones: open Priorities and check the "
                             "order (lower number wins).")
            lines.append("- Or on the schematic: a Rule parameter on the class's Parameter "
                         "Set directive (Add as Rule), scoped to its nets.")
            lines.append("")
    if csv_name:
        lines += [f"The class members are also in {csv_name} (class, kind, member).", ""]
    lines += ["Finally run Design Rule Check and check_rule_compliance / check_diff_pairs.", ""]
    return "\r\n".join(lines)


def build_rule_files(plan: dict, *, index=None, project_type: str = "classic",
                     stem: str = "board", project: str | None = None,
                     default_units: str = "mm", formats=FORMATS) -> dict:
    """Validate a plan and produce the .RUL text (checked by parsing it back), the CSV and
    the step list. {"error": "invalid_plan", "problems": [...]} for a bad plan."""
    normal, problems = validate_plan(plan, default_units)
    if normal is None:
        return {"error": "invalid_plan", "message": "The plan has problems: fix them and "
                "call again.", "problems": problems[:40]}
    units = normal["units"]
    taken_names: set[str] = set()
    existing_by_kind: dict[str, list[str]] = defaultdict(list)
    uids: set[str] = set()
    pcb_pairs = None
    pcb_classes: dict[tuple[str, str], set] = {}
    profiles: dict[str, tuple[str, float | None]] = {}
    if index is not None:
        for rule in index.pcb.rules:
            taken_names.add(rule.name.lower())
            if rule.unique_id:
                uids.add(rule.unique_id.upper())
            if rule.scope1.strip().lower() == "all" and rule.kind in RULE_KINDS:
                existing_by_kind[rule.kind].append(rule.name)
        pcb_pairs = {pair.name.lower() for pair in index.pcb.diff_pairs}
        for pcb_class in index.pcb.classes:
            if not pcb_class.superclass:
                pcb_classes[(pcb_class.kind, pcb_class.name.lower())] = {
                    member.lower() for member in pcb_class.members}
        for profile in getattr(index.pcb.board, "impedance_profiles", []) or []:
            profiles[profile.name.lower()] = (profile.id, profile.target_ohm)
    renamed, notes = [], []
    rules = []
    # A new name avoids the board's rules and the plan's other rules.
    plan_names = Counter(rule["name"].lower() for rule in normal["rules"])
    for rule in normal["rules"]:
        name = rule["name"]
        if name.lower() in taken_names:
            number = 2
            while f"{name}_{number}".lower() in taken_names or \
                    plan_names[f"{name}_{number}".lower()]:
                number += 1
            renamed.append({"from": name, "to": f"{name}_{number}"})
            name = f"{name}_{number}"
        taken_names.add(name.lower())
        if rule.get("impedance_profile"):
            found = profiles.get(rule["impedance_profile"].lower())
            if found is None:
                notes.append(f"{name}: impedance profile {rule['impedance_profile']} is not on "
                             "the board: the rule is written with its values only.")
            elif found[1] is None:
                notes.append(f"{name}: impedance profile {rule['impedance_profile']} has no "
                             "target impedance: the rule is written with its values only.")
        rules.append({**rule, "name": name, "values": dict(rule["values"])})
    if index is None:
        notes.append("No PcbDoc was read: rule and class names were not checked against the "
                     "board's, and Import Rules may replace a board rule of the same name.")
        defaults = sorted(rule["name"] for rule in rules
                          if rule["name"].lower() in ALTIUM_DEFAULT_NAMES)
        if defaults:
            notes.append(f"{_names(defaults)}: Altium's default rule names; rename them so the "
                         "import cannot replace the board's own rule.")
    for rule in rules:
        if rule["kind"] == "DiffPairsRouting" and "max_uncoupled_length" not in rule["values"]:
            value, source = _uncoupled_default(index)
            rule["values"]["max_uncoupled_length"] = value
            notes.append(f"{rule['name']}: max uncoupled length {_mil_text(value, units)} "
                         f"({source}); give max_uncoupled_length in the plan to set it.")
    for cls in normal["classes"]:
        held = pcb_classes.get((cls["kind"], cls["name"].lower()))
        if held is None:
            continue
        members = {(member["pair"] if isinstance(member, dict) else member).lower()
                   for member in cls["members"]}
        if held != members and not cls.get("exists_on_pcb"):
            notes.append(f"The PCB already has a {cls['kind']} class {cls['name']} with other "
                         "members: creating it again changes that class; rename it in the "
                         "plan if that is not intended.")
    undefined = _undefined_classes(rules, normal["classes"], pcb_classes, index is not None)
    notes += undefined
    priorities = _priorities(rules)
    lines, written = [], []
    substacks = list(getattr(index.pcb.board, "substacks", []) or []) if index is not None \
        else None
    for rule, priority in zip(rules, priorities):
        template = _layer_template(index, rule["kind"])
        record = rule_record(rule, priority, _unique_id(rule["name"], uids), template, profiles)
        written.append(record)
        lines.append(record_line(record))
    check = round_trip(lines, rules, priorities, index, substacks, written)
    result: dict = {"units": units, "classes": len(normal["classes"]), "rules": len(rules),
                    "round_trip": check}
    if renamed:
        result["renamed_rules"] = renamed
    context = _priority_context(index, rules, priorities, normal["classes"])
    texts: dict[str, str] = {}
    rul_name = f"{stem}-constraints.rul" if "rul" in formats and rules else None
    csv_name = f"{stem}-net-classes.csv" if "csv" in formats and normal["classes"] else None
    if "rul" in formats:
        if not rules:
            notes.append("The plan has no rules: no .RUL file.")
        elif check["mismatches"]:
            result["rul_not_written"] = ("The records did not read back as written: no .RUL "
                                         "file is given (see round_trip.mismatches).")
            rul_name = None
        else:
            texts["rul"] = "\r\n".join(lines) + "\r\n"
    if "csv" in formats and normal["classes"]:
        texts["csv"] = _csv_text(normal["classes"])
    if "steps" in formats:
        texts["steps"] = steps_markdown(
            normal, rules, priorities, project_type=project_type, rul_name=rul_name,
            csv_name=csv_name, units=units, pcb_pairs=pcb_pairs, renamed=renamed,
            project=project, existing_by_kind=existing_by_kind, context=context,
            undefined=undefined, board_read=index is not None)
    result["texts"] = texts
    result["file_names"] = {"rul": f"{stem}-constraints.rul", "csv": f"{stem}-net-classes.csv",
                            "steps": f"{stem}-constraints-steps.md"}
    result["priorities"] = [{"rule": rule["name"], "kind": rule["kind"], "priority": priority,
                             **context["rules"].get(rule["name"], {})}
                            for rule, priority in zip(rules, priorities)]
    if context["kinds"]:
        result["board_priorities"] = context["kinds"]
    if existing_by_kind:
        result["board_rules_scoped_all"] = {kind: names[:5]
                                            for kind, names in existing_by_kind.items()}
    if "rul" in texts:
        result["import_untested"] = UNTESTED_IMPORT
        result["import_steps"] = IMPORT_STEPS.format(kinds=", ".join(
            dict.fromkeys(rule["kind"] for rule in rules)))
    result["notes"] = notes
    return result


IMPORT_STEPS = (
    "Design > Rules, right-click > Import Rules; in Choose Design Rule Type select only "
    "{kinds} (the kinds in the file); answer No to 'Clear existing rules prior to import?' "
    "(Yes deletes the board's rules of every kind selected).")


def _uncoupled_default(index) -> tuple[float, str]:
    """A DiffPairsRouting rule's max uncoupled length when the plan gives none: the board's
    own DiffPairsRouting rule scoped All (else its most used value), else Altium's
    default rule's 500 mil."""
    if index is not None:
        values = []  # (scoped All, priority, mil, name)
        for rule in index.pcb.rules:
            if rule.kind != "DiffPairsRouting":
                continue
            mil = parse_dim_mil(rule.properties.get("MAXUNCOUPLEDLENGTH"))
            if mil is not None and mil > 0:
                values.append((rule.scope1.strip().lower() == "all", rule.priority, mil,
                               rule.name))
        scoped_all = sorted((item for item in values if item[0]), key=lambda item: -item[1])
        if scoped_all:
            return scoped_all[0][2], f"the board's rule {scoped_all[0][3]} (scope All)"
        if values:
            common = Counter(item[2] for item in values).most_common(1)[0][0]
            return common, "the value most of the board's DiffPairsRouting rules use"
    return DEFAULT_MAX_UNCOUPLED_MIL, "Altium's default rule value"


def _undefined_classes(rules: list[dict], classes: list[dict], pcb_classes: dict,
                       board_read: bool) -> list[str]:
    """Notes for rules scoped to a class neither the plan nor the PCB holds."""
    planned = {(cls["kind"], cls["name"].lower()) for cls in classes}
    out = []
    for rule in rules:
        scope = rule["scope"] if isinstance(rule["scope"], dict) else {}
        for key, kind in (("net_class", "net"), ("diff_pair_class", "diff_pair")):
            name = scope.get(key)
            if not name or (kind, name.strip().lower()) in planned or \
                    (kind, name.strip().lower()) in pcb_classes:
                continue
            out.append(f"{rule['name']} is scoped to the {kind.replace('_', ' ')} class "
                       f"{name.strip()}, which is not in the plan"
                       + (" nor on the PCB" if board_read else
                          " (no PcbDoc was read to check the board's)")
                       + ": the rule matches nothing until that class exists.")
    return out


def _priorities(rules: list[dict]) -> list[int]:
    """Each rule's priority: the plan's, else the lowest number its kind has free in the
    file (1, 2, ...), so a kind's priorities never repeat."""
    used: dict[str, set[int]] = defaultdict(set)
    for rule in rules:
        if rule["priority"] is not None:
            used[rule["kind"]].add(rule["priority"])
    out = []
    for rule in rules:
        if rule["priority"] is not None:
            out.append(rule["priority"])
            continue
        number = 1
        while number in used[rule["kind"]]:
            number += 1
        used[rule["kind"]].add(number)
        out.append(number)
    return out


def _rule_targets(index, rule: dict, classes: list[dict]) -> list[tuple[str, str]]:
    """The nets or pairs a plan rule's scope names (from the plan's class, else the PCB's);
    none for All and queries."""
    scope = rule["scope"] if isinstance(rule["scope"], dict) else {}
    if scope.get("net"):
        return [(scope["net"].strip(), "net")]
    if scope.get("diff_pair"):
        return [(scope["diff_pair"].strip(), "diff_pair")]
    for key, kind in (("net_class", "net"), ("diff_pair_class", "diff_pair")):
        name = (scope.get(key) or "").strip().lower()
        if not name:
            continue
        cls = next((cls for cls in classes if cls["kind"] == kind
                    and cls["name"].lower() == name), None)
        if cls is not None:
            return [((member["pair"] if isinstance(member, dict) else member), kind)
                    for member in cls["members"]]
        held = next((c for c in index.pcb.classes if c.kind == kind and not c.superclass
                     and c.name.lower() == name), None)
        if held is not None:
            return [(member, kind) for member in held.members]
    return []


def _priority_context(index, rules: list[dict], priorities: list[int],
                      classes: list[dict]) -> dict:
    """Where the new rules' priorities sit against the board's rules of their kind: per
    kind the board's priorities and its rules scoped All; per rule the board's rules
    (not scoped All) whose scope takes its targets, and a board rule with the same
    priority number."""
    out: dict = {"kinds": {}, "rules": {}}
    if index is None:
        return out
    ctx = index.rule_context
    board: dict[str, list] = defaultdict(list)
    for rule in index.pcb.rules:
        if rule.kind in RULE_KINDS:
            board[rule.kind].append(rule)
    for kind in dict.fromkeys(rule["kind"] for rule in rules):
        held = sorted(board.get(kind, []), key=lambda rule: rule.priority)
        if not held:
            continue
        out["kinds"][kind] = {
            "board_rules": len(held),
            "board_priorities": sorted({rule.priority for rule in held})[:40],
            "scoped_all": [{"rule": rule.name, "priority": rule.priority} for rule in held
                           if rule.scope1.strip().lower() == "all"][:5]}
    for rule, priority in zip(rules, priorities):
        held = board.get(rule["kind"], [])
        if not held:
            continue
        info: dict = {}
        same = [other.name for other in held if other.priority == priority]
        if same:
            info["same_number_on_board"] = same[:5]
        overlapping = []
        targets = []
        for name, kind in _rule_targets(index, rule, classes)[:200]:
            target = index.rule_target(name, kind=kind)
            if target is not None:
                targets.append(target)
        for other in held:
            if other.scope1.strip().lower() == "all" or not other.enabled:
                continue
            scopes = (other.scope1, other.scope2) if rule["kind"] == "Clearance" else (
                other.scope1,)
            if any(evaluate_scope(scope, target, ctx)["matches"] in ("yes", "conditional")
                   for target in targets for scope in scopes
                   if scope.strip().lower() != "all"):
                overlapping.append({"rule": other.name, "priority": other.priority})
        if overlapping:
            info["overlapping_board_rules"] = overlapping[:8]
        if info:
            out["rules"][rule["name"]] = info
    return out


def write_rule_files(result: dict, output_dir: str) -> dict:
    """Write the texts into output_dir (an absolute folder, '~' expanded; created when
    missing), never over a file: an existing name gets the next free number (-2, -3 ...).
    {format: absolute path}. Raises OSError, or ValueError for a relative folder."""
    folder = Path(output_dir).expanduser()
    if not folder.is_absolute():
        raise ValueError(f"output_dir '{output_dir}' is not an absolute path")
    folder.mkdir(parents=True, exist_ok=True)
    written = {}
    for kind, text in result["texts"].items():
        name = Path(result["file_names"][kind])
        path = folder / name.name
        number = 2
        while path.exists():
            path = folder / f"{name.stem}-{number}{name.suffix}"
            number += 1
        encoding = "ascii" if kind == "rul" else "utf-8"
        with open(path, "x", encoding=encoding, newline="") as handle:
            handle.write(text)
        written[kind] = str(path.resolve())
    return written


def inline_texts(result: dict, limit: int = INLINE_CHARS) -> tuple[dict, dict]:
    """The texts to return inline, each cut at the last line break within limit
    characters (a .RUL or CSV record is never cut in half); and which were cut, with the
    lines left out."""
    shown, cut = {}, {}
    for kind, text in result["texts"].items():
        if len(text) <= limit:
            shown[kind] = text
            continue
        end = text.rfind("\r\n", 0, limit - 1)
        kept = text[:end + 2] if end >= 0 else ""
        if kept:
            shown[kind] = kept
        total = text.count("\r\n") + (0 if text.endswith("\r\n") else 1)
        cut[kind] = {"chars": len(text), "shown": len(kept),
                     "lines": total, "lines_shown": kept.count("\r\n")}
    return shown, cut
