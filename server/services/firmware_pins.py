"""Firmware pin map and firmware-facing pinout review (heuristic).

For a processor (an MCU or SoC, an FPGA, or a radio module with GPIO-named pins) the pin
table a firmware engineer needs: each pin's number and full name (alternate functions
often follow it, PA9/USART1_TX), the port and bit parsed from the name for the common
schemes (PA9, PTA1, RB3, P0.12, P1_05, PIO0_7, P100, GPIO1_IO05; GPIO12, IO12, PIO[2] as a
GPIO number), its electrical type and net, the parts it reaches on that net and through
up to two series parts (resistors and 0 ohm links, ferrites, chokes, series capacitors;
check_interfaces' series edges), its pull-up or pull-down (check_pull_resistors' reading)
and a likely function with its basis: a dedicated pin name first (NRST, BOOT0, OSC_IN),
then the net name, then the pins and parts it reaches (a UART bridge's RXD, a LED, a
switch, a crystal; a peer pin that lists several functions gives none, an Ethernet PHY's
TXD is Ethernet, and a peer pin's name outside the functions the pin's own name lists
yields to them), then the pin's own alternate functions when they agree (an I2C one when
the line is pulled up). Power and ground pins are summarised per net. Output as JSON
rows, CSV, a C header or a Zephyr-style devicetree fragment.

The review follows Altium's 'Beyond the PCB Design Review' (swapped TX/RX, debug-pin
access, conflicting pin assignments, bootstrap resistor values and default states) and
its schematic review checklist (a programming header or pads; a way to read and write
flash or EEPROM on prototypes): UART TX meeting TX or RX meeting RX (also a CAN
controller's lines to its transceiver); whether SWD, JTAG or
a vendor programming set reaches a connector or a test point (or only another processor's
GPIOs); boot, strap, mode and reset pins with their resistors and the level they set (an
STM32's BOOT0 or an ESP32's GPIO0 held at its bootloader level); the access path to
external flash, EEPROM, FRAM or an FPGA's configuration memory; unused GPIO pins; two MCU
pins on one net or a net named for a function the pin does not list; and pins whose
parts change between variants.

Everything here is a heuristic, and every output says so: most IC pins in real libraries
are typed Passive, so names carry most of it; a net named otherwise, an MCU library that
names pins only by number, or a vendor's own pin naming is missed or read wrong; the
datasheet and the firmware decide.
"""

import csv
import io
import re
from collections import defaultdict
from dataclasses import dataclass, field

try:
    from services.bom import natural_key
    from services.decoupling import IC_PREFIXES, designator_prefix, pin_role
    from services.interfaces import (
        DEBUG_HEADER_WORDS, PROCESSOR_TEXT, build_context, bus_transceiver, choke_far,
    )
    from services.pull_resistors import (
        LINK_OHMS, control_kind, i2c_line, link_group, ohms_text, open_drain_name,
        push_pull_drivers, rail_voltage, resistance, resolve_names,
    )
    from services.testpoints import TP_DESIGNATOR, TP_FOOTPRINT
except ImportError:
    from server.services.bom import natural_key
    from server.services.decoupling import IC_PREFIXES, designator_prefix, pin_role
    from server.services.interfaces import (
        DEBUG_HEADER_WORDS, PROCESSOR_TEXT, build_context, bus_transceiver, choke_far,
    )
    from server.services.pull_resistors import (
        LINK_OHMS, control_kind, i2c_line, link_group, ohms_text, open_drain_name,
        push_pull_drivers, rail_voltage, resistance, resolve_names,
    )
    from server.services.testpoints import TP_DESIGNATOR, TP_FOOTPRINT

CHECKS = (
    "uart_crossing", "debug_access", "boot_straps", "external_memory", "unused_pins",
    "conflicting_assignment", "variant_changes",
)
FORMATS = ("table", "csv", "c_header", "devicetree")
_CHECK_RANK = {check: rank for rank, check in enumerate(CHECKS)}
_SEVERITY_RANK = {"warning": 0, "info": 1}

ROW_LIMIT = 100  # pin rows and findings per page
CANDIDATE_LIMIT = 10
PEER_LIMIT = 4  # parts listed per pin row
HOP_LIMIT = 2  # series parts crossed at most from a pin to the parts it reaches
MIN_GPIO_PINS = 4  # GPIO-named pins that make a part a candidate
EXPORT_MCU_LIMIT = 4
HEURISTIC = (
    "A heuristic from pin and net names, the pins' electrical types and the parts each pin "
    "reaches: most library pins are typed Passive, so names carry most of it. Functions, "
    "ports and default levels are a starting point for the firmware team; the schematic, "
    "the datasheet and the firmware decide."
)

# ---------- names ----------

# Alternatives in a pin name: PA9/USART1_TX, 'PD0-OSC_IN', 'PA13 (JTMS-SWDIO)', 'I/O, VREF'.
_ALT_SPLIT = re.compile(r"\s*[/,|;()]\s*|\s+")
_DASH_SPLIT = re.compile(r"(?<=[A-Z0-9_\]])-(?=[A-Z])")
_TOKEN_SPLIT = re.compile(r"[_\s.]+")
_NEGATION_MARKS = re.compile(r"^[~!#*]+|[~!#*]+$")
# Altium's own names for nets nobody named: NetR9_2, NetU1_23, NetC19_1.
_AUTO_NET = re.compile(r"Net[A-Za-z_]*\d*[A-Za-z]*_\w+", re.I)

# (scheme, pattern, port known): the GPIO naming schemes, read on one alternative.
_SCHEMES = (
    ("kinetis", re.compile(r"PT([A-K])(\d{1,2})")),  # PTA1
    ("lettered", re.compile(r"P([A-K])(\d{1,2})")),  # PA9, PB12, PA00, PD2
    ("pic", re.compile(r"R([A-K])(\d{1,2})")),  # RB3
    ("numbered", re.compile(r"P(?:IO)?(\d{1,2})[._](\d{1,2})")),  # P0.12, P1_05, PIO0_7
    ("renesas", re.compile(r"P(\d)(\d{2})")),  # P100, P407
    ("imx", re.compile(r"GPIO(\d)_IO(\d{1,2})")),  # GPIO1_IO05
    ("indexed", re.compile(r"(?:PIO|GPIO|IO)\[(\d{1,3})\]")),  # PIO[2]
    ("gpio_number", re.compile(r"GPIO_?(\d{1,3})")),  # GPIO12, GPIO_5
    ("io_number", re.compile(r"IO_?(\d{1,2})")),  # IO12 (ESP32 modules)
    ("dio_number", re.compile(r"DIO_?(\d{1,2})")),  # DIO_5 (TI CC13xx/CC26xx)
    ("mio", re.compile(r"MIO_?(\d{1,2})")),  # MIO12 (Zynq)
    ("pad", re.compile(r"GPIO_[A-Z]+\d*(?:_B\d)?_\d{2}")),  # GPIO_AD_B0_12 (i.MX RT pads)
    ("fpga_io", re.compile(r"IO_L\d+[PN]?(?:_\w+)?|IO_\d+_\w*|IO_[A-Z]\w*_\d+")),  # IO_L1P_T0_14
)
_PORTED = frozenset({"kinetis", "lettered", "pic", "numbered", "renesas", "imx"})
_NUMBERED_ONLY = frozenset({"indexed", "gpio_number", "io_number", "dio_number", "mio"})
# Schemes a part's own pins prove only in numbers: a PHY's RD0 or a flash's IO0..IO3 are
# no GPIO bank.
_SCHEME_MIN = {"pic": 8, "io_number": 8, "dio_number": 4, "renesas": 4}

# Control signals that a _B or _L suffix marks active low (RESET_B, CS_L, WE_B).
_LOW_CONTROL = re.compile(r"(?:CS|SS|CE|OE|WE|WR|RD|RE|INT|IRQ|INIT|DONE|READY|RDY|BUSY|ALERT|"
                          r"FAULT|WAKE|SEL|STROBE|ACK|REQ|LOAD|LD|CLR)\d*")
_NC_NAME = re.compile(r"N\.?C\.?\d*|DNC|DNU|NU|RFU|RES|RSVD|RESERVED\d*|NOCONNECT")


def alternatives(name: str | None) -> list[str]:
    """The names a pin name lists: 'PA9/USART1_TX' -> ['PA9', 'USART1_TX'];
    'PD0-OSC_IN' -> ['PD0', 'OSC_IN']; 'PA13 (JTMS-SWDIO)' -> ['PA13', 'JTMS', 'SWDIO']."""
    text = (name or "").upper().replace("\\", "")
    out = []
    for part in _ALT_SPLIT.split(text):
        for piece in _DASH_SPLIT.split(part):
            piece = piece.strip("_ ")
            if piece and piece not in out:
                out.append(piece)
    return out


def negated(name: str | None) -> bool:
    """An active-low name: nRESET, RESET_N, RST#, ~CS, /CS, Altium's overline R\\S\\T\\."""
    raw = (name or "").strip()
    if not raw:
        return False
    if "\\" in raw or raw[0] in "~!#/" or raw.endswith("#") or raw.endswith("*"):
        return True
    upper = raw.upper()
    tokens = [token for token in _TOKEN_SPLIT.split(upper.replace("-", "_")) if token]
    if not tokens:
        return False
    if tokens[-1] == "N" and len(tokens) > 1:
        return True
    if tokens[-1] in ("B", "L") and len(tokens) > 1 and (
            _LOW_CONTROL.fullmatch(tokens[-2]) or control_kind("_".join(tokens[:-1]), pin=True)):
        return True  # RESET_B, CS_L: not LED_B (blue) or MOTOR_L (left)
    return bool(re.fullmatch(r"N[A-Z]{2,}\d*", tokens[0])
                and control_kind(tokens[0][1:], pin=True)) or bool(re.match(r"n[A-Z]", raw))


@dataclass
class Port:
    scheme: str
    port: str | None  # 'A', '0', None for a GPIO number
    bit: int | None
    text: str  # the alternative it was read from


def parse_port(name: str | None, schemes: set[str] | None = None) -> Port | None:
    """The GPIO port and bit a pin name gives (first alternative that matches a scheme,
    only the schemes named when given): PA9 -> A 9, PTA1 -> A 1, RB3 -> B 3, P0.12 and
    P0_12 -> 0 12, PIO0_7 -> 0 7, P407 -> 4 7, GPIO1_IO05 -> 1 5; GPIO12, IO12, PIO[2],
    DIO_5 and MIO12 give a GPIO number without a port; FPGA and pad names give neither."""
    for text in alternatives(name):
        for scheme, pattern in _SCHEMES:
            if schemes is not None and scheme not in schemes:
                continue
            match = pattern.fullmatch(text)
            if not match:
                continue
            if scheme in _PORTED:
                return Port(scheme, match.group(1), int(match.group(2)), text)
            if scheme in _NUMBERED_ONLY:
                return Port(scheme, None, int(match.group(1)), text)
            return Port(scheme, None, None, text)
    return None


# ---------- functions from names ----------

@dataclass(frozen=True)
class _Family:
    name: str
    strong: re.Pattern  # a token that names the family alone
    roles: tuple  # (pattern, role) on a token, first match wins
    standalone: re.Pattern | None = None  # a role token that names it with no bus token (MOSI)
    weak: re.Pattern | None = None  # a token that names it together with a role


def _family(name, strong, roles=(), standalone=None, weak=None) -> _Family:
    return _Family(name, re.compile(strong), tuple((re.compile(p), r) for p, r in roles),
                   re.compile(standalone) if standalone else None,
                   re.compile(weak) if weak else None)


# A MAC's and a PHY's own Ethernet pin names (RMII and MII both name lines from the MAC's
# side, so a PHY's TXD0 takes the MCU's TXD0).
_ETHERNET_ROLES = ((r"MDIO", "mdio"), (r"MDC", "mdc"),
                   (r"TXD\d*|TXEN|TXER|TXCLK|GTXCLK|TXCTL", "tx"),
                   (r"RXD\d*|RXER|RXDV|CRSDV|CRS|COL|RXCLK|RXCTL", "rx"), (r"REFCLK\w*", "ref_clk"))

_FAMILIES = (
    _family("swd", r"SWD|SWDIO\d?|SWDAT|SWCLK|SWDCLK|SWCLKTCK|SWO|TRACESWO",
            ((r"SWDIO\d?|SWDAT|SWD_?IO|IO|DIO|DAT|DATA", "swdio"),
             (r"SWCLK|SWDCLK|SWCLKTCK|SWCK|CLK|CK", "swclk"), (r"SWO|TRACESWO|SWV", "swo"))),
    _family("jtag", r"JTAG|J?TCK|J?TMS|J?TDI|J?TDO|N?J?N?TRST|TAP",
            ((r"J?TCK", "tck"), (r"J?TMS", "tms"), (r"J?TDI", "tdi"), (r"J?TDO", "tdo"),
             (r"N?J?N?TRST", "trst"))),
    _family("programming", r"UPDI|PDI_?DATA|PDI_?CLK|SBWTCK|SBWTDIO|SBW|C2CK|C2D|PGEC\d?|"
                           r"PGED\d?|ICSPCLK|ICSPDAT|ICSP|MCLR|ISP",
            ((r"UPDI", "updi"), (r"PDI_?DATA", "pdi_data"), (r"PDI_?CLK", "pdi_clk"),
             (r"SBWTCK", "sbwtck"), (r"SBWTDIO", "sbwtdio"), (r"C2CK", "c2ck"), (r"C2D", "c2d"),
             (r"PGEC\d?|ICSPCLK", "pgec"), (r"PGED\d?|ICSPDAT", "pged"), (r"MCLR", "mclr"))),
    _family("usb", r"USBDP|USBDM|USBP|USBN",
            ((r"DP|D\+|DPLUS|P|USBDP|USBP", "dp"), (r"DM|DN|D-|DMINUS|N|M|USBDM|USBN", "dm"),
             (r"ID", "id"), (r"VBUS\w*", "vbus"), (r"SOF", "sof")),
            standalone=r"D\+|D-|DP|DM|VBUS", weak=r"USB\d*|OTG\w*|USBD|USBH|USBFS|USBHS"),
    _family("can", r"F?D?CAN\d*(?:TXD?|RXD?)?\d*|TWAI\d*|(?:TX|RX)CAN\d*",
            ((r"(?:F?D?CAN\d*)?TXD?\d*|TXCAN\d*", "tx"), (r"(?:F?D?CAN\d*)?RXD?\d*|RXCAN\d*", "rx"),
             (r"STB|S|SILENT|EN", "control"))),
    _family("ethernet", r"ETH\d*|ENET\d*|EMAC\d*|GMAC\d*|RMII\d*|MII|RGMII\d*|MDIO|MDC|CRS_?DV|"
                        r"CRSDV|ETHERNET",
            _ETHERNET_ROLES + ((r"REF|CLK|CLKIN", "ref_clk"), (r"EN", "tx"), (r"DV|ER", "rx")),
            standalone=r"MDIO|MDC"),
    _family("high_speed", r"PCIE\w*|PCI|SATA\w*|Q?SFP\w*|GXB\w*|GT[XHPYM]\w*|MGT\w*|XCVR\w*|"
                          r"SERDES\w*|SGMII\w*|XAUI\w*|LVDS\w*|TMDS\w*|MIPI\w*|LANE\d*|SSTX\w*|"
                          r"SSRX\w*|REFCLK\w*|PERST\w*"),
    _family("qspi", r"QSPI\w*|OSPI\w*|QUADSPI\w*|OCTOSPI\w*|SQI\w*|XSPI\w*|BK\d",
            ((r"CLK|SCK|SCLK|C", "clk"), (r"N?CS\d?|NCS|SS|CE|NSS", "cs"),
             (r"IO\d|D\d|DQ\d|SIO\d", "io"))),
    _family("sdio", r"SDIO\d*|SDMMC\d*|SDHC\d*|EMMC|SDCARD|MMC\d*",
            ((r"CMD", "cmd"), (r"CLK|CK", "clk"), (r"D\d|DAT\d|DATA\d", "data"),
             (r"CD|DET", "card_detect")), weak=r"SD\d?"),
    _family("memory_bus", r"DDR\d?\w*|LPDDR\d*|SDRAM|DRAM|FMC|FSMC|EMC|EMIF|EBI|DQS\w*|DQ\d+|"
                          r"DQM\d*|CKE\d*|ODT\d*|ZQ\w*|SDCKE\d*|SDNE\d*|SDCLK|SDNWE|SDNRAS|"
                          r"SDNCAS|NBL\d|NRAS|NCAS|RAS|CAS|BA\d|BG\d|HYPER\w*"),
    _family("i2s", r"I2S\d*|SAI\d*|IIS|TDM\d*|PCM\d*|PDM\d*|BCLK|LRCLK|LRCK",
            ((r"MCLK|MCK", "mclk"), (r"BCLK|BCK|SCK|CK|SCLK", "bclk"),
             (r"LRCLK|LRCK|WS|FS|LRC", "ws"), (r"SD\w*|DIN|DOUT|SDI|SDO|DATA\w*|DI|DO", "data"))),
    _family("spi", r"SPI\d*|SSP\d*|LPSPI\d*|SPIM\d*|SPIS\d*|HSPI|VSPI|FSPI|USCI\w*",
            ((r"SCK|SCLK|SPCK|CLK|CK", "sck"), (r"MOSI\d*|SIMO|COPI|PICO|SDO|DOUT|SO", "mosi"),
             (r"MISO\d*|SOMI|CIPO|POCI|SDI|DIN|SI", "miso"),
             (r"N?CS\d*|NSS\d*|SS\d*|CSN\d*|CE\d*|PCS\d*|SSEL\d*|STE", "cs"),
             (r"OUT|TX", "mosi"), (r"IN|RX", "miso")),
            standalone=r"MOSI\d*|MISO\d*|SIMO|SOMI|COPI|CIPO|PICO|POCI|SCK|SCLK|SPCK|SDO|SDI|"
                       r"N?CS\d*|NSS\d*|CSN\d*"),
    _family("uart", r"UART\d*|USART\d*|LPUART\d*|LEUART\d*|SCI\d*|SERIAL\d*|COM\d+|RS-?232|"
                    r"RS-?485|RS-?422|IRDA|LIN\d*|CONSOLE|U\d?TXD?|U\d?RXD?|UART\d*(?:TXD?|RXD?)",
            ((r"TXD?\d?|U\d?TXD?|UTXD\d?|TXO|UART\d*TXD?", "tx"),
             (r"RXD?\d?|U\d?RXD?|URXD\d?|RXI|UART\d*RXD?", "rx"),
             (r"N?RTS\d*|RTSN|U\d?RTS", "rts"), (r"N?CTS\d*|CTSN|U\d?CTS", "cts"),
             (r"DE|TXEN|DIR", "de")),
            standalone=r"TXD?\d?|RXD?\d?|U\d?TXD?|U\d?RXD?|UTXD\d?|URXD\d?|N?RTS\d*|N?CTS\d*|"
                       r"U\d?RTS|U\d?CTS"),
    # XTAL1 and OSC1 are the oscillator's input (AVR, 8051, LPC, PIC), XTAL2 and OSC2 its output.
    _family("crystal", r"OSC\w*|XTAL\w*|XIN\d*|XOUT\d*|XI|XO|HSE\w*|LSE\w*|XT\d|XTI|XTO|EXTAL\d*|"
                       r"XTAL32\w*|X32\w*|32K\w*|32KHZ|CLKIN|XC\d",
            ((r"IN|I|XIN\d*|XI|EXTAL\d*|XTI|CLKIN|XTAL1|OSC1", "in"),
             (r"OUT|O|XOUT\d*|XO|XTO|XTAL2|OSC2", "out"))),
    _family("display", r"LCD\w*|TFT\w*|LTDC\w*|DISPLAY|DISP|OLED\w*|EPD\w*|DSI\w*|HDMI\w*"),
    _family("camera", r"DCMI\w*|CAM\w*|CAMERA|CSI\d*|PCLK|HSYNC|VSYNC"),
)
_ANALOG = re.compile(
    r"ADC\d*|AIN\d*|ANA\w*|AN\d+|DAC\d*|AOUT\d*|OPAMP\d*|COMP\d*|VSENSE|SENSE|SNS|MEAS\w*|VMON|"
    r"IMON|ISENSE|ISNS|THERM\w*|NTC\d*|TEMP\w*|VBATSNS|ADC\d+IN\d+|INP\d+|INN\d+")
_TIMER = re.compile(
    r"PWM\w*|TIM\d+\w*|TMR\d*\w*|CCP\d*|EPWM\w*|FTM\d+\w*|TCC\d*\w*|TC\d+|MCPWM\w*|LEDC\w*|"
    r"SERVO\w*|BUZZ\w*|BEEP\w*|MOTOR\w*|ENC\w*|QEI\w*|QENC\w*|HALL\w*|CAPTURE|ICAP\w*|CH\d+N?")
_LED = re.compile(r"N?LED\w*|\w+LED\d*|LEDS?")
_LED_WORD = re.compile(r"N?LEDS?\d*|\w+LED\d*")  # not LEDC (ESP32's LED PWM controller)
_FPGA_MODE = re.compile(r"MSEL\d*")
# Families a net is often named after while it carries an I2C bus (a DIMM's SPD, HDMI DDC).
_CONTEXT_FAMILIES = frozenset({"memory_bus", "display", "camera", "high_speed"})
_BUTTON = re.compile(r"BTN\w*|BUTTON\w*|KEY\d*|PUSH\w*|SW\d+|SWITCH\w*|USERSW")
_WAKE = re.compile(r"WAKE\w*|WKUP\d*|WUP\d*")
_RF = re.compile(r"RF\d*|RFIO\w*|ANT\w*|RFIN|RFOUT")
_DEBUG_WORDS = frozenset({"DBG", "DEBUG", "CONSOLE", "LOG", "TERM", "TRACE"})
# Serial lines whose TX and RX must cross: a UART's, and a CAN controller's to its transceiver.
_SERIAL = frozenset({"uart", "debug_uart", "can"})
_DIFF_END = re.compile(r"[PN]|\w*\d[PN]")
_LANE_WORDS = frozenset({"TX", "RX", "TXD", "RXD", "TXP", "TXN", "RXP", "RXN", "HSTX", "HSRX"})
# A bare IO alternative marks a general-purpose pin of an FPGA or SoC ('IO, CLK_3D_1p').
_GENERIC_IO = frozenset({"IO", "I", "O", "GPIO", "IO_PIN", "USERIO"})
# Functions a GPIO keeps in firmware (devicetree gpios), the rest are peripheral pins.
GPIO_FUNCTIONS = frozenset({
    "gpio", "led", "button", "reset", "enable", "boot_strap", "interrupt", "wakeup", "unknown",
})
SIGNAL_BUS_FUNCTIONS = frozenset({
    "uart", "debug_uart", "spi", "qspi", "i2c", "can", "usb", "ethernet", "sdio", "i2s", "swd",
    "jtag", "memory_bus",
})


# Signal names written in two tokens (CRS_DV, TX_EN): read as one, so TX_EN is no UART TX.
_COMPOUNDS = frozenset({("CRS", "DV"), ("RX", "DV"), ("RX", "ER"), ("TX", "ER"), ("TX", "EN"),
                        ("RX", "CLK"), ("TX", "CLK"), ("GTX", "CLK"), ("PDI", "DATA"),
                        ("PDI", "CLK")})
_BUS_INDEX = re.compile(r"\[(\d+)\]")


def _tokens(name: str) -> list[str]:
    text = (name or "").upper().replace("\\", "")
    out = []
    for part in alternatives(text):
        tokens = [token for token in (_NEGATION_MARKS.sub("", token)
                                      for token in _TOKEN_SPLIT.split(part)) if token]
        index = 0
        while index < len(tokens):
            if tuple(tokens[index:index + 2]) in _COMPOUNDS:
                out.append(tokens[index] + tokens[index + 1])
                index += 2
                continue
            out.append(tokens[index])
            index += 1
    return out


def _forms(token: str) -> list[str]:
    forms = [token]
    if len(token) > 2 and token[0] == "N" and token[1:2].isalpha():
        forms.append(token[1:])
    if len(token) > 2 and token[-1] == "N" and token[-2:-1].isalpha():
        forms.append(token[:-1])
    return forms


def _family_of(family: _Family, tokens: list[str],
               standalone: bool = False) -> tuple[str, str | None] | None:
    """(family, role) when a strong token (or a weak one with a role) names the family; with
    standalone, also when only a role that names the family alone is there (MOSI, TX)."""
    strong = weak = False
    role = None
    for token in tokens:
        for form in _forms(token):
            if family.strong.fullmatch(form):
                strong = True
            if family.weak is not None and family.weak.fullmatch(form):
                weak = True
    for pattern, label in family.roles:
        if any(pattern.fullmatch(form) for token in tokens for form in _forms(token)):
            role = label
            break
    lone = standalone and family.standalone is not None and any(
        family.standalone.fullmatch(form) for token in tokens for form in _forms(token))
    if strong or (weak and role) or (lone and role):
        if family.name == "qspi" and role == "io":
            role = next((form.lower() for token in tokens for form in _forms(token)
                         if re.fullmatch(r"IO\d|D\d|DQ\d|SIO\d", form)), "io")
        return family.name, role
    return None


def classify(name: str | None, pin: bool = False) -> tuple[str, str | None] | None:
    """(function, role) from one net or pin name, or None: uart (tx, rx, rts, cts),
    debug_uart, spi (sck, mosi, miso, cs), qspi, i2c (SDA, SCL), swd, jtag, programming,
    usb, can, ethernet, sdio, i2s, crystal, reset, boot_strap, enable, interrupt, analog,
    pwm_timer, led, button, wakeup, rf; as a pin name also nc. Names only: a heuristic."""
    if not name:
        return None
    name = _BUS_INDEX.sub(r"\1", name)  # CONFIG[5] reads as CONFIG5
    alts = alternatives(name)
    if pin and alts and all(_NC_NAME.fullmatch(alt) for alt in alts):
        return "nc", None
    tokens = _tokens(name)
    if not tokens:
        return None
    # A token that names a bus (USART2, SPI1, CAN) wins over a bare role token (TX, CS).
    found = next((hit for family in _FAMILIES if (hit := _family_of(family, tokens))), None)
    if found and found[0] in _CONTEXT_FAMILIES and not found[1] and (line := i2c_line(name)):
        return "i2c", line[0].lower()  # DDR4_SPD_SDA, HDMI_SCL: the I2C bus beside it
    if found is None and _DIFF_END.fullmatch(tokens[-1]) and len(tokens) > 1 and \
            {re.sub(r"\d+$", "", token) for token in tokens[:-1]} & _LANE_WORDS:
        return "high_speed", None  # TX0_P, RX_CH2N: a differential lane, not a UART
    if found is None:
        line = i2c_line(name)
        if line:
            return "i2c", line[0].lower()
        hits = {hit[0]: hit for family in _FAMILIES
                if (hit := _family_of(family, tokens, standalone=True))}
        # A lone role token of two buses (RTS1_DP: a UART's RTS or USB's D+) names neither,
        # and one beside a LED word names the LED (LED_RX).
        if len(hits) == 1 and not any(_LED_WORD.fullmatch(token) for token in tokens):
            found = next(iter(hits.values()))
    if found:
        if found[0] == "uart" and set(tokens) & _DEBUG_WORDS:
            return "debug_uart", found[1]
        return found
    if any(_FPGA_MODE.fullmatch(token) for token in tokens):
        return "boot_strap", "mode"  # MSEL0: an FPGA's configuration mode select
    kind = control_kind(name, pin=pin)
    if kind == "reset":
        return "reset", None
    if kind in ("boot", "strap", "mode", "program"):
        return "boot_strap", kind
    if kind in ("enable", "shutdown"):
        return "enable", kind
    if any(_ANALOG.fullmatch(token) for token in tokens):
        return "analog", None
    if any(_TIMER.fullmatch(token) for token in tokens):
        return "pwm_timer", None
    if open_drain_name(name, pin=pin):
        return "interrupt", None
    for pattern, function in ((_LED, "led"), (_BUTTON, "button"), (_WAKE, "wakeup"),
                              (_RF, "rf")):
        if any(pattern.fullmatch(token) for token in tokens):
            return function, None
    return None


def auto_net(net: str | None) -> bool:
    """A net Altium named itself (NetR9_2): its name says nothing about the function."""
    return bool(net) and bool(_AUTO_NET.fullmatch(net))


_FAMILY_BY_NAME = {family.name: family for family in _FAMILIES}
_PHY_ROLES = tuple((re.compile(pattern), role) for pattern, role in _ETHERNET_ROLES)


def port_like(alt: str) -> bool:
    return alt in _GENERIC_IO or parse_port(alt) is not None


def name_functions(name: str | None) -> list[tuple[str, str | None]]:
    """(function, role) of each alternative a pin name lists, GPIO port names left out:
    'PB6/USART1_TX/I2C1_SCL' -> [uart tx, i2c scl]."""
    return [found for alt in alternatives(name) if not port_like(alt)
            and (found := classify(alt, pin=True)) and found[0] != "nc"]


def _alt_function(alt: str, ethernet: bool = False) -> tuple[str, str | None] | None:
    """One alternative's function; on an Ethernet PHY its TXD0, RXD1 and CRS_DV are
    Ethernet lines, not a UART's."""
    role = _phy_role(alt) if ethernet else None
    return ("ethernet", role) if role else classify(alt, pin=True)


def _phy_role(name: str) -> str | None:
    tokens = [form for token in _tokens(name) for form in _forms(token)]
    return next((role for pattern, role in _PHY_ROLES
                 if any(pattern.fullmatch(token) for token in tokens)), None)


def role_in(function: str, name: str | None) -> str | None:
    """The role a name gives within one function's family, read on its role words alone:
    'RTS1_DP' in uart -> rts, 'TXD0' in ethernet -> tx."""
    if function == "i2c":
        line = i2c_line(name)
        return line[0].lower() if line else None
    family = _FAMILY_BY_NAME.get("uart" if function == "debug_uart" else function)
    if family is None:
        return None
    forms = [form for token in _tokens(name or "") for form in _forms(token)]
    return next((label for pattern, label in family.roles
                 if any(pattern.fullmatch(form) for form in forms)), None)


# ---------- parts ----------

_MCU_FAMILY = re.compile(
    r"(?<![A-Z0-9])(?:STM32|STM8|GD32|AT32|APM32|CH32|HK32|MM32|ATSAM|SAM[A-Z]?\d{2}|ATMEGA|"
    r"ATTINY|ATXMEGA|AVR\d|AT90|PIC\d{2}|DSPIC|PIC32|MSP430|MSP432|MSPM0|NRF5\d|NRF91|NRF54|"
    r"ESP32|ESP8266|ESP-?WROOM|ESP-?WROVER|RP2040|RP2350|LPC\d{2,4}|MK\d{2}|MKL\d{2}|MKE\d{2}|"
    r"S32K|MIMXRT|IMXRT|EFM32|EFR32|EFM8|C8051|CC13\d{2}|CC26\d{2}|CC32\d{2}|CC25\d{2}|TM4C|"
    r"TMS320|TMS570|F28\d{3}|XMC\d|R7F|RA[246][A-Z]\d|RX\d{3}|RL78|CY8C|PSOC|MAX32\d{3}|ADUC|"
    r"BL60\d|W60\d|AM335|ZYNQ|XC7Z)", re.I)
_FPGA_TEXT = re.compile(
    r"\bFPGA\b|\bCPLD\b|spartan|artix|kintex|virtex|zynq|cyclone|arria|stratix|\bMAX ?(?:II|V|10)\b|"
    r"iCE40|\bECP5\b|MachXO|PolarFire|IGLOO|SmartFusion|\bXC\d[A-Z]|\b10AS\d|\bEP4C", re.I)
_MODULE_TEXT = re.compile(r"\bmodule\b", re.I)
_RADIO_TEXT = re.compile(r"\bBLE\b|bluetooth|Wi-?Fi|\bWLAN\b|LoRa|zigbee|\bGNSS\b|\bGPS\b|"
                         r"\bLTE\b|NB-?IoT|cellular|\bmodem\b|\b802\.15\.4\b", re.I)
_EXPANDER_TEXT = re.compile(r"(?:I/?O|GPIO|port) expander|\bexpander\b", re.I)
_MEMORY_WORDS = re.compile(r"\bflash\b|EEPROM|E2PROM|\bFRAM\b|F-RAM|\bMRAM\b|serial NOR|\bNOR\b|"
                           r"\bNAND\b|\bQSPI\b|quad[- ]?SPI|serial memory|\bPROM\b|"
                           r"configuration (?:device|memory|flash|PROM)", re.I)
_MEMORY_FAMILY = re.compile(
    r"(?<![A-Z0-9])(?:W25[QXNM]|MX25|MX66|IS25|AT25|AT45|AT24|24(?:AA|LC|FC|C|CW|CS)\d|M24|M95|"
    r"CAT24|BR24|FM25|FM24|CY15|MB85|MT25|MT29|S25FL|S25FS|S26|N25Q|GD25|GD5F|SST25|SST26|SST39|"
    r"AT26|EN25|XM25|P25Q|ZD25|BY25|IS24|S34|W29N|W35N|EPCQ|EPCS\d|XCF\d)", re.I)
# An FPGA's configuration memory (Intel EPCQ/EPCS, Xilinx Platform Flash XCF).
_CONFIG_MEMORY = re.compile(r"configuration|\bPROM\b|(?<![A-Z0-9])(?:EPCQ|EPCS\d|XCF\d)", re.I)
_RAM_WORDS = re.compile(r"S?D?RAM\b|\bDDR\d?\b|PSRAM|SDRAM|\bSRAM\b|\bDRAM\b", re.I)
BRIDGE_TEXT = re.compile(
    r"USB[- ]?(?:to[- ])?(?:UART|serial)|UART[- ]bridge|serial (?:converter|bridge)|"
    r"(?<![A-Z0-9])(?:FT23\d|FT2232|FT4232|FT230|FT231|CP210\d|CH340|CH341|CH9102|CH343|"
    r"PL2303|MCP2200|MCP2221|CY7C652)", re.I)
_IRDA_TEXT = re.compile(r"\bIrDA\b|infrared transceiver", re.I)
CRYSTAL_TEXT = re.compile(r"crystal|\bxtal|resonator|oscillat|\d\s*[kM]Hz\b", re.I)
_ETHERNET_TEXT = re.compile(r"ethernet|\bPHY\b|10/100|\d+BASE-?T|\bR?G?MII\b|\bRMII\b", re.I)
_USB_TEXT = re.compile(r"\bUSB", re.I)
_CAN_TEXT = re.compile(r"\bCAN\b|CAN[- ]?FD", re.I)
_LED_TEXT = re.compile(r"\bLED\b|light[- ]emitting", re.I)
_DEBUG_HEADER = re.compile(DEBUG_HEADER_WORDS + r"|pogo", re.I)
_CANDIDATE_PREFIXES = IC_PREFIXES | frozenset({"M", "MOD", "MD", "MDL", "A", "CPU", "MCU", "SOC",
                                               "FPGA"})
_KIND_BONUS = {"mcu": 40, "radio": 20, "module": 20, "fpga": 10, "other": 0, "io_expander": -20}
# Strapping pins of common families (named in the pin only as GPIOs).
_STRAP_FAMILIES = (
    (re.compile(r"ESP32-?S[23]", re.I), ("GPIO0", "GPIO3", "GPIO45", "GPIO46")),
    (re.compile(r"ESP32-?C[236]", re.I), ("GPIO2", "GPIO8", "GPIO9")),
    (re.compile(r"ESP32", re.I), ("GPIO0", "GPIO2", "GPIO5", "GPIO12", "GPIO15")),
    (re.compile(r"ESP8266", re.I), ("GPIO0", "GPIO2", "GPIO15")),
)
_STM32_TEXT = re.compile(r"(?<![A-Z0-9])(?:STM32|GD32|AT32|APM32)", re.I)
_ESP32_TEXT = re.compile(r"(?<![A-Z0-9])ESP32", re.I)
# Espressif serial bootloader pins per family: (part text, boot pin names held low for the
# download mode, UART0 TX names, UART0 RX names).
_ESP_BOOT = (
    (re.compile(r"(?<![A-Z0-9])ESP32-?[CH]\d", re.I), ("GPIO9", "IO9"), ("U0TXD", "TXD0"),
     ("U0RXD", "RXD0")),
    (re.compile(r"(?<![A-Z0-9])ESP32-?S[23]", re.I), ("GPIO0", "IO0"),
     ("U0TXD", "TXD0", "GPIO43", "IO43"), ("U0RXD", "RXD0", "GPIO44", "IO44")),
    (re.compile(r"(?<![A-Z0-9])(?:ESP32|ESP8266)", re.I), ("GPIO0", "IO0"),
     ("U0TXD", "TXD0", "GPIO1", "IO1"), ("U0RXD", "RXD0", "GPIO3", "IO3")),
)


def _esp_boot(text: str) -> tuple[tuple[str, ...], ...] | None:
    return next(((boot, tx, rx) for pattern, boot, tx, rx in _ESP_BOOT if pattern.search(text)),
                None)
_RP2040_TEXT = re.compile(r"(?<![A-Z0-9])RP2(?:040|350)", re.I)


@dataclass
class Candidate:
    designator: str
    kind: str  # mcu, fpga, radio, module, io_expander, other
    score: float
    gpio_pins: int
    io_pins: int
    pins: int
    scheme: str | None
    basis: list[str]
    fitted: bool = True
    forced: bool = False  # named in mcu= though not recognised as a processor

    def describe(self, comment: str | None) -> dict:
        out: dict = {"designator": self.designator, "kind": self.kind,
                     "score": round(self.score, 1), "gpio_named_pins": self.gpio_pins,
                     "io_typed_pins": self.io_pins, "pins": self.pins}
        if comment:
            out["part"] = comment[:80]
        if self.scheme:
            out["port_scheme"] = self.scheme
        out["basis"] = self.basis
        if not self.fitted:
            out["not_fitted"] = True
        return out


@dataclass
class _Peer:
    ref: str
    pin: str
    name: str
    kind: str
    net: str
    through: tuple[str, ...]
    fitted: bool  # the part and every series part on the way are fitted in the variant
    direct: bool = True  # every series part on the way is a link (under 1 kohm, a ferrite)

    def describe(self) -> dict:
        out: dict = {"designator": self.ref, "pin": self.pin}
        if self.name and self.name != self.pin:
            out["name"] = self.name
        out["kind"] = self.kind
        if self.through:
            out["through"] = list(self.through)
        return out

    def label(self) -> str:
        text = f"{self.ref}.{self.pin}"
        if self.name and self.name != self.pin:
            text += f" ({self.name})"
        if self.through:
            text += f" via {'+'.join(self.through)}"
        return text


@dataclass
class _Reach:
    peers: list[_Peer] = field(default_factory=list)
    nets: list[str] = field(default_factory=list)  # nets reached through fitted parts
    series: list[str] = field(default_factory=list)  # every series part met
    same_part: list[tuple[str, str]] = field(default_factory=list)  # (pin, net) of the MCU
    caps: list[str] = field(default_factory=list)

    def fitted_peers(self) -> list[_Peer]:
        return [peer for peer in self.peers if peer.fitted]


@dataclass
class PinRow:
    mcu: str
    pin: str
    name: str
    alternatives: list[str]
    port: Port | None
    type: str | None
    net: str | None
    function: str = "unknown"
    role: str | None = None
    basis: list[str] = field(default_factory=list)
    reach: _Reach | None = None
    pulls: list = field(default_factory=list)  # pull_resistors.Pull
    tie: str | None = None  # 'ground' or 'rail' when the net is one
    gpio_like: bool = False
    candidates: list[str] = field(default_factory=list)  # alternate functions, ambiguous
    unused: bool = False
    drives: str | None = None  # another part's debug pin this GPIO drives (bit-banged)

    @property
    def power(self) -> bool:
        return self.function in ("power", "ground")


class PinMap:
    """The design read once: check_interfaces' context and the part pin names."""

    def __init__(self, netlist: dict, plan, project_params: dict | None, ctx=None):
        # ctx: check_interfaces' context when the caller has built it already.
        self.ctx = ctx if ctx is not None else build_context(netlist, plan, project_params)
        self.design = self.ctx.design
        self.components = netlist.get("components") or {}
        self.variant = getattr(plan, "name", None) or "[No Variations]"

    def pin_name(self, ref: str, pin: str) -> str:
        meta = self.components.get(ref) or {}
        name = ((meta.get("pins") or {}).get(pin) or {}).get("name")
        return name or (meta.get("pin_names") or {}).get(pin) or ""

    def pin_type(self, ref: str, pin: str) -> str | None:
        return ((self.components.get(ref) or {}).get("pin_types") or {}).get(pin)

    def all_pins(self, ref: str) -> list[str]:
        meta = self.components.get(ref) or {}
        pins = [*(meta.get("pin_designators") or []), *(self.design.part_pins.get(ref) or {}),
                *(meta.get("pin_names") or {})]
        return sorted(dict.fromkeys(pin for pin in pins if pin), key=natural_key)

    def text(self, ref: str) -> str:
        return self.ctx.text.get(ref, "")

    def fitted(self, ref: str) -> bool:
        return self.ctx.fitted.get(ref, True)

    def is_tp(self, ref: str) -> bool:
        return bool(TP_DESIGNATOR.match(ref)) or bool(TP_FOOTPRINT.search(self.text(ref)))

    def is_access(self, ref: str) -> bool:
        """A part a probe or a cable reaches: a connector, a test point, a debug header."""
        kind = self.ctx.kind.get(ref)
        return (kind == "connector" or self.is_tp(ref)
                or bool(kind in ("passive", "other") and _DEBUG_HEADER.search(self.text(ref))))


# ---------- candidates ----------

def _scheme_counts(names: list[str]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for name in names:
        port = parse_port(name)
        if port:
            counts[port.scheme] += 1
    return counts


def _accepted_schemes(counts: dict[str, int]) -> set[str]:
    return {scheme for scheme, count in counts.items() if count >= _SCHEME_MIN.get(scheme, 1)}


def part_kind(text: str) -> str:
    if _FPGA_TEXT.search(text):
        return "fpga"
    if PROCESSOR_TEXT.search(text) or _MCU_FAMILY.search(text):
        return "mcu"
    if _EXPANDER_TEXT.search(text):
        return "io_expander"
    if _MODULE_TEXT.search(text):
        return "module"
    if _RADIO_TEXT.search(text):
        return "radio"
    return "other"


def ethernet_part(text: str) -> bool:
    """An Ethernet PHY or transceiver by its text (not a processor with a MAC, not USB)."""
    return bool(_ETHERNET_TEXT.search(text)) and not (
        PROCESSOR_TEXT.search(text) or _MCU_FAMILY.search(text) or _USB_TEXT.search(text))


def _memory(text: str) -> bool:
    """Non-volatile memory by its text: flash, EEPROM, FRAM, NOR or a memory family's part
    number; not a processor with on-chip flash, nor RAM."""
    if PROCESSOR_TEXT.search(text) or _MCU_FAMILY.search(text):
        return False
    if _MEMORY_FAMILY.search(text):
        return True
    return bool(_MEMORY_WORDS.search(text)) and not (
        _RAM_WORDS.search(text) and not re.search(r"flash|EEPROM|FRAM", text, re.I))


def rank_candidates(fmap: PinMap) -> list[Candidate]:
    """Parts that look like a processor: U/IC (and module) parts fitted in the variant, by
    GPIO-named pins, IO-typed pins and the words in their text."""
    out = []
    for ref in fmap.design.part_pins:
        if designator_prefix(ref) not in _CANDIDATE_PREFIXES:
            continue
        text = fmap.text(ref)
        kind = part_kind(text)
        if kind not in ("mcu", "fpga") and _memory(text):
            continue
        pins = fmap.all_pins(ref)
        names = [fmap.pin_name(ref, pin) for pin in pins]
        counts = _scheme_counts(names)
        accepted = _accepted_schemes(counts)
        gpio = sum(count for scheme, count in counts.items() if scheme in accepted)
        io_pins = sum(1 for pin in pins if fmap.pin_type(ref, pin) == "IO")
        if not (gpio >= MIN_GPIO_PINS or (kind in ("mcu", "fpga") and io_pins >= MIN_GPIO_PINS)):
            continue
        if designator_prefix(ref) == "A" and kind == "other" and gpio < 2 * MIN_GPIO_PINS:
            continue
        basis = []
        if gpio:
            scheme = max(accepted, key=lambda name: counts[name]) if accepted else None
            basis.append(f"{gpio} GPIO-named pins" + (f" ({scheme})" if scheme else ""))
        else:
            scheme = None
        if io_pins:
            basis.append(f"{io_pins} pins typed IO")
        word = {"mcu": "processor words or an MCU family's part number in its text",
                "fpga": "FPGA or CPLD words in its text: an FPGA's pins are set by its "
                        "design, not fixed peripherals",
                "radio": "radio words in its text (a radio SoC or module)",
                "module": "'module' in its text",
                "io_expander": "an I/O expander, not a processor"}.get(kind)
        if word:
            basis.append(word)
        score = min(gpio, 100) * 2 + min(io_pins, 64) * 0.5 + _KIND_BONUS[kind]
        out.append(Candidate(ref, kind, score, gpio, io_pins, len(pins), scheme, basis,
                             fmap.fitted(ref)))
    out.sort(key=lambda c: (not c.fitted, -c.score, natural_key(c.designator)))
    return out


# ---------- walking a pin's net ----------

_SKIP_KINDS = frozenset({"resistor", "capacitor", "inductive", "choke", "esd"})


def _link(edge) -> bool:
    """A series part a signal passes as it is: a resistor under 1 kohm (a 0 ohm link, a
    damping resistor), a ferrite, an inductor or a common-mode choke. A larger resistor
    (a base resistor, a pull to a switched supply) or a capacitor is crossed only as the
    last step of a walk."""
    if edge.kind == "capacitor":
        return False
    if edge.kind == "resistor":
        ohms = resistance(edge.value)
        return ohms is not None and ohms < LINK_OHMS
    return True


def _reach(fmap: PinMap, ref: str, pin: str, net: str, limit: int = HOP_LIMIT) -> _Reach:
    """The parts on a pin's net and on the nets behind up to limit series parts (fitted or
    not; a peer behind a Not Fitted part is marked unfitted). A resistor of 1 kohm or more
    or a capacitor ends the walk. Pulls, capacitors to ground, ESD parts and the series
    parts themselves are not peers."""
    ctx, design = fmap.ctx, fmap.design
    out = _Reach()
    seen: dict[str, tuple[tuple[str, ...], bool, bool]] = {net: ((), True, True)}
    level = [net]
    for hops in range(limit + 1):
        following = []
        for current in level:
            through, path_fitted, direct = seen[current]
            if path_fitted:
                out.nets.append(current)
            edges = ctx.edges.get(current, ())
            skip = {edge.part for edge in edges}
            skip |= {pull.resistor for pull in design.pulls.get(current, ())}
            caps = {cap for cap, _ in design.caps.get(current, ())}
            for other, other_pin in design.nets.get(current, ()):
                if other == ref:
                    if other_pin != pin and path_fitted and direct:
                        out.same_part.append((other_pin, current))
                    continue
                if other in skip:
                    continue
                if other in caps:
                    if other not in out.caps and fmap.fitted(other):
                        out.caps.append(other)
                    continue
                kind = ctx.kind.get(other, "other")
                if kind in _SKIP_KINDS:
                    continue
                out.peers.append(_Peer(other, other_pin, fmap.pin_name(other, other_pin), kind,
                                       current, through, path_fitted and fmap.fitted(other),
                                       direct))
            if hops == limit or not direct:
                continue
            plain = [edge for edge in edges if edge.kind != "common_mode_choke"]
            chokes = choke_far(ctx, [edge for edge in edges if edge.kind == "common_mode_choke"],
                               ref, current)
            for edge in [*plain, *chokes]:
                if edge.part not in out.series:
                    out.series.append(edge.part)
                if edge.far in seen or design.fixed(edge.far):
                    continue
                seen[edge.far] = ((*through, edge.part), path_fitted and edge.fitted,
                                  _link(edge))
                following.append(edge.far)
        level = following
        if not level:
            break
    return out


# ---------- one MCU's pins ----------

def is_crystal(ref: str, text: str) -> bool:
    """A crystal, resonator or oscillator by its designator prefix (Y, XTAL, XT, OSC), or an
    X part whose text names one."""
    prefix = designator_prefix(ref)
    return prefix in ("Y", "XTAL", "XT", "OSC") or (
        prefix == "X" and bool(CRYSTAL_TEXT.search(text)))


def _crystal(fmap: PinMap, ref: str) -> bool:
    return is_crystal(ref, fmap.text(ref))


def _peer_function(fmap: PinMap, peer: _Peer) -> tuple[str, str | None, str, bool] | None:
    """(function, the peer pin's role, basis, read from the peer's pin name) a peer gives
    the MCU pin. A peer pin that lists several functions (PB6/USART1_TX/I2C1_SCL on another
    MCU) gives none; on an Ethernet PHY TXD and RXD are Ethernet lines."""
    prefix = designator_prefix(peer.ref)
    text = fmap.text(peer.ref)
    if peer.kind in ("diode", "zener") and (prefix == "LED" or _LED_TEXT.search(text)):
        return "led", None, f"reaches LED {peer.label()}", False
    if peer.kind == "switch" or prefix in ("SW", "BTN", "S", "KEY", "PB") and \
            peer.kind not in ("ic", "connector"):
        return "button", None, f"reaches switch {peer.label()}", False
    if _crystal(fmap, peer.ref):
        return "crystal", None, f"reaches crystal {peer.label()}", False
    if peer.kind == "transistor":
        return "gpio", "drives_transistor", f"drives {peer.label()}", False
    if prefix in ("BZ", "BUZ", "LS"):
        return "pwm_timer", "buzzer", f"reaches {peer.label()}", False
    if peer.kind in ("ic", "other", "connector") and peer.name and peer.direct:
        if peer.kind != "connector" and ethernet_part(text):
            # A PHY's strap-shared pin (RXD0/MODE0, nINT/REFCLKO) is its Ethernet line.
            role = next((role for alt in alternatives(peer.name) if (role := _phy_role(alt))),
                        None)
            if role:
                return "ethernet", role, f"reaches {peer.label()} (an Ethernet PHY)", True
        found = _single(name_functions(peer.name))
        if found and found[0] in ("uart", "debug_uart") and _CAN_TEXT.search(text) and \
                bus_transceiver(fmap.ctx, peer.ref):
            found = ("can", found[1])  # a CAN transceiver's TXD and RXD: the CAN controller's
        if found:
            return found[0], found[1], f"reaches {peer.label()}", True
    return None


SLAVE_IN = frozenset({"SI", "SDI", "DI", "DIN", "MOSI", "SIMO", "COPI", "PICO"})
SLAVE_OUT = frozenset({"SO", "SDO", "DO", "DOUT", "MISO", "SOMI", "CIPO", "POCI"})


def _complement(function: str, role: str | None, peer_name: str = "") -> str | None:
    """The MCU pin's role across from a peer pin's role: the peer's RXD is the MCU's TX; a
    SPI device's SI or SDI (its input) is the MCU's MOSI."""
    if function in ("uart", "debug_uart"):
        return {"tx": "rx", "rx": "tx", "rts": "cts", "cts": "rts"}.get(role or "", role)
    if function == "spi":
        tokens = set(_tokens(peer_name))
        if tokens & SLAVE_IN:
            return "mosi"
        if tokens & SLAVE_OUT:
            return "miso"
    return role


def _single(found: list[tuple[str, str | None]]) -> tuple[str, str | None] | None:
    families = {function for function, _ in found}
    if len(families) == 1:
        roles = {role for _, role in found if role}
        return found[0][0], (roles.pop() if len(roles) == 1 else None)
    return None


def _own_role(others: list[tuple[str, str | None]], function: str) -> str | None:
    """The role the pin's own alternate names give for one function, when they agree."""
    roles = {role for found, role in others if role and (
        found == function or {found, function} == {"uart", "debug_uart"})}
    return roles.pop() if len(roles) == 1 else None


def _build_row(fmap: PinMap, ref: str, pin: str, schemes: set[str],
               fpga: bool = False, ethernet: bool = False) -> PinRow:
    """One pin's row. On an FPGA the alternate names (LVDS2A_17p, DATA31) say what a pin
    can do, not what the design does with it, so they give no function there. On an
    Ethernet PHY (ethernet) TXD and RXD are Ethernet lines."""
    design = fmap.design
    name = fmap.pin_name(ref, pin)
    net = (design.part_pins.get(ref) or {}).get(pin)
    alts = alternatives(name)
    port = parse_port(name, schemes) if schemes else None
    row = PinRow(ref, pin, name, alts, port, fmap.pin_type(ref, pin), net,
                 gpio_like=port is not None)
    per_alt = [(alt, _alt_function(alt, ethernet)) for alt in alts]
    own = [found for _, found in per_alt if found]
    role = pin_role(name)
    base = [alt for alt in alts if alt in _GENERIC_IO or (schemes and parse_port(alt, schemes))]
    # A dedicated pin: no GPIO base in its name and one function (NRST, BOOT0, OSC_IN).
    dedicated = _single(own) if own and not base else None
    supply = role in ("ground", "supply", "ref", "cap") or (
        row.type == "Power" and (not dedicated or dedicated[0] in ("crystal", "analog")))
    if not row.gpio_like and supply:
        row.function = "ground" if role == "ground" or (net in design.grounds) else "power"
        row.basis.append("pin name" if role in ("ground", "supply", "ref", "cap")
                         else "pin typed Power")
        if net is None:
            row.basis.append("not connected")
        return row
    if net is not None and net in design.grounds:
        row.tie = "ground"
    elif net is not None and net in design.rails:
        row.tie = "rail"
    if net is None:
        if dedicated and dedicated[0] == "nc" or (alts and all(_NC_NAME.fullmatch(a) for a in alts)):
            row.function, row.basis = "nc", ["pin name"]
        else:
            row.function = dedicated[0] if dedicated else "unconnected"
            row.role = dedicated[1] if dedicated else None
            row.basis = ["pin name", "not connected"] if dedicated else ["not connected"]
            row.unused = row.gpio_like
        return row
    if row.tie:
        row.reach = _Reach(nets=[net])  # a rail's or ground's other parts are no peers
        if not dedicated or dedicated[0] == "nc":
            row.function, row.role = "tied", row.tie
            row.basis.append(f"{'a GPIO' if row.gpio_like else 'the pin'} tied to {net}")
            return row
    else:
        row.reach = _reach(fmap, ref, pin, net)
        row.pulls = [pull for member in link_group(design, net)
                     for pull in design.pulls.get(member, ())]
        row.unused = row.gpio_like and len(design.nets.get(net, ())) <= 1
    if dedicated and dedicated[0] != "nc":
        row.function, row.role = dedicated
        row.basis.append(f"pin name {name}")
        return row
    others = [] if fpga else [found for alt, found in per_alt
                              if found and alt not in base and found[0] != "nc"]
    families = {function for function, _ in others}
    listed = ", ".join(alt for alt in alts if alt not in base)[:80]
    peers = sorted(row.reach.fitted_peers(), key=lambda p: (len(p.through), _PEER_RANK.get(
        p.kind, 9), natural_key(p.ref)))
    if not auto_net(net):
        found = classify(net)
        if found and found[0] == "crystal" and not any(_crystal(fmap, peer.ref) for peer in peers):
            found = None  # XOUT on an accelerometer's output: no crystal is there
        if found:
            row.function, row.role = found
            row.basis.append(f"net name {net}")
            # The pin's own name says which end it is (PIO[0]/UART_TX on a net named RX,
            # a module's RTS1 on a net named for the MCU's CTS).
            mine = {mine for alt in alts if alt not in base and (
                (hit := classify(alt, pin=True)) is None or hit[0] == found[0])
                and (mine := role_in(found[0], alt))}
            if len(mine) == 1 and mine != {found[1]}:
                row.role = mine.pop()
                row.basis.append("role from the pin name")
            ics = [peer for peer in peers if peer.kind in ("ic", "other") and peer.direct]
            own_debug = bool(_STM32_TEXT.search(fmap.text(ref))) and bool(
                {"PA13", "PA14", "PB3"} & set(alts))  # the STM32's SWD after reset
            if found[0] in ("swd", "jtag", "programming") and row.gpio_like and ics and \
                    found[0] not in families and not own_debug and \
                    not any(fmap.is_access(peer.ref) for peer in peers):
                # A GPIO on a net named for another part's debug pin (PC13_MODULE_SWDIO):
                # firmware drives that part's debug port, the pin is no debug pin of its own.
                row.drives = ics[0].label()
                row.basis.append(f"a GPIO wired to {row.drives}")
    if row.function == "unknown":
        for peer in peers:
            given = _peer_function(fmap, peer)
            if not given:
                continue
            function, peer_role, basis, named = given
            role = _complement(function, peer_role, peer.name)
            if function in _SERIAL and (
                    peer.kind == "connector" or bus_transceiver(fmap.ctx, peer.ref)):
                # A connector's TX may be named from either side; a transceiver's TXD
                # takes the MCU's TX.
                role = None if peer.kind == "connector" else peer_role
            if named and families and function not in families:
                # The pin's own alternate names list what it can do, and the peer's pin name
                # read alone is none of them (a PHY's TXD0 beside ETH_RMII_TXD0): read the
                # peer's pin within the pin's own functions, or trust the pin's own name.
                refit = {family: hit for family in families if (hit := role_in(family, peer.name))}
                if len(refit) == 1 or len(families) == 1:
                    if not refit:  # the peer's reading stays listed (a bit-banged bus)
                        row.candidates = sorted({*families, function})
                    function = next(iter(refit or families))
                    role = _own_role(others, function) or refit.get(function)
                    basis = f"pin alternate function {listed}; {basis}"
                else:
                    row.candidates = sorted(families)
            elif function in families:
                role = _own_role(others, function) or role
            row.function, row.role = function, role
            row.basis.append(basis)
            break
    if row.function == "unknown" and others:
        single = _single(others)
        pulled_up = any(pull.fitted and pull.direction == "up" and not pull.switched
                        for pull in row.pulls)
        if single:
            row.function, row.role = single
            row.basis.append(f"pin alternate function {listed}")
        elif "i2c" in families and pulled_up:
            # An I2C line needs its pull-up; the other alternates do not.
            row.function, row.role = "i2c", _own_role(others, "i2c")
            row.basis.append(f"pin alternate function {listed}, and a pull-up on the line")
        else:
            row.candidates = sorted(families)
    if row.function == "unknown" and row.gpio_like:
        row.function = "gpio"
        row.basis.append("GPIO pin, nothing names its use"
                         + (f" (alternates: {', '.join(row.candidates)})"
                            if row.candidates else ""))
    return row


_PEER_RANK = {"ic": 0, "other": 1, "transistor": 2, "diode": 3, "zener": 3, "switch": 4,
              "passive": 5, "connector": 6}


def pin_rows(fmap: PinMap, candidate: Candidate, pins=None) -> list[PinRow]:
    """The candidate's pin rows (only the pins in pins, when given)."""
    ref = candidate.designator
    names = [fmap.pin_name(ref, pin) for pin in fmap.all_pins(ref)]
    schemes = _accepted_schemes(_scheme_counts(names))
    fpga = candidate.kind == "fpga"
    ethernet = ethernet_part(fmap.text(ref))
    return [_build_row(fmap, ref, pin, schemes, fpga, ethernet) for pin in fmap.all_pins(ref)
            if pins is None or pin in pins]


# ---------- pulls and levels ----------

def _ohms(value: float | None) -> str:
    text = ohms_text(value)
    if text is None:
        return "?"
    return text if text[-1].isalpha() else f"{text}R"


def _pull_text(pulls) -> str | None:
    parts = []
    for pull in pulls:
        text = f"{pull.direction} {_ohms(pull.ohms)} to {pull.other} ({pull.resistor}"
        text += ", Not Fitted)" if not pull.fitted else ")"
        parts.append(text)
    return "; ".join(parts) or None


def _parallel(values: list[float]) -> float:
    if min(values) <= 0:
        return 0.0
    return 1.0 / sum(1.0 / value for value in values)


def _gpio_drivers(fmap: PinMap, row: PinRow, hosts: set[str]) -> list[_Peer]:
    """Another processor's GPIO pins that drive a pin's net (directly or through series
    resistors; a series capacitor passes no level): a host MCU driving a module's BOOT0."""
    out = []
    for peer in row.reach.fitted_peers() if row.reach else []:
        if peer.ref == row.mcu or peer.kind not in ("ic", "other") or any(
                fmap.ctx.kind.get(part) == "capacitor" for part in peer.through):
            continue
        text = fmap.text(peer.ref)
        if not (peer.ref in hosts or PROCESSOR_TEXT.search(text) or _MCU_FAMILY.search(text)):
            continue
        if fmap.pin_type(peer.ref, peer.pin) in ("IO", "Output") or parse_port(peer.name):
            out.append(peer)
    return out


def default_level(fmap: PinMap, row: PinRow, hosts: set[str] = frozenset()) -> dict:
    """The level a strap or reset pin sits at with nothing driving it: tied, pulled up or
    down, a divider (with the computed voltage when the values and the rail's voltage are
    known), set by a switch or jumper, driven (by a push-pull output, or by another
    processor's GPIO, hosts naming the processors), shared with another pin of the same
    part (a PHY's CONFIG pin on its LED pin's net), or none (the part's internal
    default)."""
    design = fmap.design
    out: dict = {}
    if row.tie:
        out.update(level="low" if row.tie == "ground" else "high",
                   basis=f"tied straight to {row.net}")
        return out
    live = [pull for pull in row.pulls if pull.fitted]
    absent = [pull for pull in row.pulls if not pull.fitted]
    ups = [pull for pull in live if pull.direction == "up"]
    downs = [pull for pull in live if pull.direction == "down"]
    drivers = [driver for member in link_group(design, row.net)
               for driver in push_pull_drivers(design, member) if not driver.startswith(
                   f"{row.mcu}.")]
    driving = {driver.split(" ")[0] for driver in drivers}
    gpios = [peer.label() for peer in _gpio_drivers(fmap, row, hosts)
             if f"{peer.ref}.{peer.pin}" not in driving]
    if gpios:
        out["gpio_driven_by"] = gpios[:3]
    setters = [peer.label() for peer in (row.reach.fitted_peers() if row.reach else [])
               if peer.kind in ("switch",) or designator_prefix(peer.ref) in ("JP", "SJ", "J", "P")
               and peer.kind != "ic"]
    if absent:
        out["not_fitted"] = [pull.describe() for pull in absent]
    if drivers:
        out["driven_by"] = drivers[:3]
    if setters:
        out["set_by"] = setters[:3]
    if ups and downs:
        up_values = [pull.ohms for pull in ups]
        down_values = [pull.ohms for pull in downs]
        if None in up_values or None in down_values:
            out.update(level="divider", basis=f"pull-up and pull-down, a value unread "
                       f"({_pull_text(live)})")
            return out
        rup, rdown = _parallel(up_values), _parallel(down_values)
        if rup == 0 or rdown == 0:
            out.update(level="high" if rup == 0 else "low",
                       basis=f"a 0 ohm link wins over the other pull ({_pull_text(live)})")
            return out
        ratio = rdown / (rup + rdown)
        out["ratio"] = round(ratio, 3)
        volts = rail_voltage(ups[0].other)
        if volts is not None:
            out["volts"] = round(volts * ratio, 3)
        out["level"] = "high" if ratio >= 0.7 else "low" if ratio <= 0.3 else "mid_rail"
        out["basis"] = (f"divider {ohms_text(rup)} up / {ohms_text(rdown)} down: "
                        f"{ratio:.0%} of {ups[0].other}" + (f" = {volts * ratio:.2f} V"
                                                            if volts is not None else ""))
        return out
    if ups or downs:
        pulls = ups or downs
        values = [pull.ohms for pull in pulls]
        strength = _parallel(values) if None not in values else None
        tie = strength is not None and strength < LINK_OHMS and strength <= 100
        out["level"] = "high" if ups else "low"
        out["basis"] = ("tied through: " if tie else "pull: ") + (_pull_text(pulls) or "")
        return out
    if drivers:
        out.update(level="driven", basis=f"driven by {', '.join(drivers[:2])}, no pull")
        return out
    if gpios:
        out.update(level="driven", basis=f"driven by {', '.join(gpios[:2])}, another "
                   "processor's GPIO, no pull: it floats while that processor is in reset or "
                   "until its firmware sets the pin")
        return out
    if setters:
        out.update(level="set_externally", basis=f"set by {', '.join(setters[:2])}, no pull")
        return out
    shared = [pin for pin, _ in (row.reach.same_part if row.reach else [])]
    if shared:
        # A PHY's CONFIG pin on its own LED pin's net: the LED circuit sets the strap.
        out.update(level="shared", shared_with=shared[:4],
                   basis=f"no pull or driver; it shares its net with the part's own pin "
                         f"{', '.join(shared[:3])}, whose circuit sets the level (see the "
                         "datasheet's strapping table)")
        return out
    out.update(level="internal_default", basis="no pull, tie or driver: the part's internal "
               "default applies (see its datasheet)")
    return out


# ---------- findings ----------

def _finding(check: str, severity: str, mcu: str, issue: str, designators=(), **fields) -> dict:
    out: dict = {"check": check, "severity": severity, "mcu": mcu}
    out.update({key: value for key, value in fields.items() if value not in (None, [], {})})
    if designators:
        out["designators"] = sorted(set(designators), key=natural_key)
    out["issue"] = issue
    return out


def _pin_label(row: PinRow) -> str:
    text = f"{row.mcu} pin {row.pin}"
    if row.name and row.name != row.pin:
        text += f" ({row.name})"
    return text


# UART direction of a pin by its name: what a device's own TX/RX means.
OUT_NAME = re.compile(r"TXD?\d?|U\d?TXD?|UTXD\d?|TXO|SOUT|DOUT\d?|DO|OUT[A-Z]?|R\d?OUT|ROUT|RO")
IN_NAME = re.compile(r"RXD?\d?|U\d?RXD?|URXD\d?|RXI|SIN|DIN\d?|DI|IN[A-Z]?|T\d?IN|TIN")
# A transceiver's logic side: TXD/DI/TxIN take the MCU's TX, RXD/RO/RxOUT drive its RX.
XCVR_IN = re.compile(r"TXD?\d?|DI|D|T\d?IN|TIN|TX_?IN")
XCVR_OUT = re.compile(r"RXD?\d?|RO|R|R\d?OUT|ROUT|RX_?OUT")


def _peer_class(fmap: PinMap, ref: str, candidates: set[str]) -> str:
    kind = fmap.ctx.kind.get(ref)
    text = fmap.text(ref)
    if kind == "connector":
        return "connector"
    if bus_transceiver(fmap.ctx, ref) or _IRDA_TEXT.search(text):
        return "transceiver"
    if BRIDGE_TEXT.search(text):
        return "bridge"
    if ref in candidates or PROCESSOR_TEXT.search(text) or _MCU_FAMILY.search(text) or \
            _RADIO_TEXT.search(text) or _MODULE_TEXT.search(text):
        return "device"
    return "other"


def _uart_direction(fmap: PinMap, peer: _Peer, cls: str) -> tuple[str | None, bool, str]:
    """('out' or 'in', certain, basis) for a peer pin on a UART line."""
    electrical = fmap.pin_type(peer.ref, peer.pin)
    if electrical in ("Output", "OpenCollector", "OpenEmitter"):
        return "out", True, f"{peer.ref}.{peer.pin} is typed {electrical}"
    if electrical == "Input":
        return "in", True, f"{peer.ref}.{peer.pin} is typed Input"
    # Only the pin's UART names count; one that also lists another function (another MCU's
    # PB6/USART1_TX/I2C1_SCL) is not certain to be a UART end.
    mixed = bool({function for function, _ in name_functions(peer.name)}
                 - _SERIAL)
    alts = [alt for alt in alternatives(peer.name) if not port_like(alt) and (
        (found := classify(alt, pin=True)) is None or found[0] in _SERIAL)]
    tokens = [form for alt in alts for token in _tokens(alt) for form in _forms(token)]
    if cls == "transceiver":
        if any(XCVR_IN.fullmatch(token) for token in tokens):
            return "in", True, (f"{peer.label()} is a bus transceiver's driver input: it takes "
                                "the MCU's TX")
        if any(XCVR_OUT.fullmatch(token) for token in tokens):
            return "out", True, (f"{peer.label()} is a bus transceiver's receiver output: it "
                                 "drives the MCU's RX")
        return None, False, ""
    direction = ("out" if any(OUT_NAME.fullmatch(token) for token in tokens) else
                 "in" if any(IN_NAME.fullmatch(token) for token in tokens) else None)
    if direction is None:
        return None, False, ""
    if cls == "connector":
        return direction, False, (f"{peer.label()} is a connector pin named "
                                  f"{'TX' if direction == 'out' else 'RX'}: whether that is "
                                  "this board's or the cable's side depends on the naming "
                                  "convention")
    what = {"bridge": "a USB-UART bridge", "device": "another processor or module",
            "other": "a part"}[cls]
    return direction, cls != "other" and not mixed, f"{peer.label()} on {what}: its own " + (
        "TX is an output" if direction == "out" else "RX is an input") + (
        " (the pin also lists other functions)" if mixed else "")


def _mcu_uart_direction(fmap: PinMap, row: PinRow, schemes_base: set[str]) -> tuple[str | None, bool, str]:
    electrical = row.type
    if electrical == "Output":
        return "out", True, "the MCU pin is typed Output"
    if electrical == "Input":
        return "in", True, "the MCU pin is typed Input"
    roles = {found[1] for alt in row.alternatives if alt not in schemes_base
             and (found := classify(alt, pin=True)) and found[0] in _SERIAL}
    roles &= {"tx", "rx"}
    if len(roles) == 1:
        role = roles.pop()
        return ("out" if role == "tx" else "in"), True, f"the MCU pin's name lists {role.upper()}"
    if row.net and not auto_net(row.net):
        found = classify(row.net)
        if found and found[0] in _SERIAL and found[1] in ("tx", "rx"):
            return ("out" if found[1] == "tx" else "in"), False, (
                f"net name {row.net}, read from the MCU's side")
    return None, False, ""


def _uart_findings(fmap: PinMap, rows: list[PinRow], candidates: set[str]) -> list[dict]:
    findings = []
    for row in rows:
        if row.function not in _SERIAL or row.reach is None:
            continue
        base = {alt for alt in row.alternatives if row.port and alt == row.port.text}
        mcu_dir, mcu_sure, mcu_basis = _mcu_uart_direction(fmap, row, base)
        if mcu_dir is None:
            continue
        net_found = classify(row.net) if row.net and not auto_net(row.net) else None
        if mcu_sure and row.type not in ("Output", "Input") and net_found and \
                net_found[0] in _SERIAL and net_found[1] in ("tx", "rx") and \
                (net_found[1] == "tx") != (mcu_dir == "out"):
            findings.append(_finding(
                "uart_crossing", "info", row.mcu,
                f"{_pin_label(row)} is a {'TX' if mcu_dir == 'out' else 'RX'} by its name but "
                f"sits on {row.net}, named {net_found[1].upper()}: the net is probably named from "
                "the other end (the firmware sets this pin as the MCU's "
                f"{'TX' if mcu_dir == 'out' else 'RX'}); if not, TX and RX are swapped.",
                [row.mcu], pin=row.pin, net=row.net))
        for peer in row.reach.fitted_peers():
            if not peer.direct or peer.kind not in ("ic", "other", "connector"):
                continue
            cls = _peer_class(fmap, peer.ref, candidates)
            peer_dir, peer_sure, peer_basis = _uart_direction(fmap, peer, cls)
            if peer_dir is None or peer_dir != mcu_dir:
                continue
            both = "TX meets TX: two outputs drive the line" if mcu_dir == "out" else \
                "RX meets RX: nothing drives the line"
            sure = mcu_sure and peer_sure
            severity = "warning" if sure else "info"
            reason = f"{mcu_basis}; {peer_basis}"
            advice = (" Swap TX and RX at one end (or fix the net or library name if the "
                      "pins are right)." if sure else
                      " Check which side each name is from before swapping anything.")
            findings.append(_finding(
                "uart_crossing", severity, row.mcu,
                f"{_pin_label(row)} on {row.net} reaches {peer.label()}: {both} ({reason})."
                + advice, [row.mcu, peer.ref, *peer.through], pin=row.pin, net=row.net,
                peer=f"{peer.ref}.{peer.pin}", peer_class=cls,
                certain=sure or None))
    return findings


# Debug and programming interfaces: (name, required roles, optional roles).
_DEBUG_SETS = (
    ("swd", ("swdio", "swclk"), ("swo", "reset")),
    ("jtag", ("tck", "tms", "tdi", "tdo"), ("trst", "reset")),
    ("updi", ("updi",), ()),
    ("pdi", ("pdi_data", "pdi_clk"), ()),
    ("spy_bi_wire", ("sbwtck", "sbwtdio"), ()),
    ("c2", ("c2ck", "c2d"), ()),
    ("icsp", ("pgec", "pged", "mclr"), ()),
    ("debug_spi", ("debug_clk", "debug_mosi", "debug_miso"), ("debug_cs",)),
    ("uart_bootloader", ("boot", "uart_tx", "uart_rx"), ("reset",)),
)


_DEBUG_SPI = (
    (frozenset({"CLK", "SCK", "SCLK"}), "debug_clk"),
    (frozenset({"MOSI", "SI", "DI", "DIN", "SDI"}), "debug_mosi"),
    (frozenset({"MISO", "SO", "DO", "DOUT", "SDO"}), "debug_miso"),
    (frozenset({"CS", "NCS", "CSN", "SS"}), "debug_cs"),
)


def _debug_roles(fmap: PinMap, row: PinRow, mcu_text: str) -> list[tuple[str, str]]:
    """(role, basis) of the debug or programming roles a pin carries."""
    out: list[tuple[str, str]] = []
    names = [(alt, "pin name") for alt in row.alternatives]
    ics = [peer for peer in (row.reach.fitted_peers() if row.reach else [])
           if peer.kind in ("ic", "other") and peer.direct]
    peer_families = {function for peer in ics for function, _ in name_functions(peer.name)}
    if row.net and not auto_net(row.net):
        net_found = classify(row.net)
        # A GPIO wired to another part's SWDIO or BOOT0 debugs or straps that part: a debug
        # net is the other part's when its pin names the function, or when no connector is
        # on it and this pin is the GPIO side. A pin that is no GPIO, wired only to other
        # processors' GPIOs, is the target (a module's SWDIO driven by its host MCU).
        access = any(fmap.is_access(peer.ref) for peer in (
            row.reach.fitted_peers() if row.reach else []))
        target = not row.gpio_like and bool(ics) and all(parse_port(peer.name) for peer in ics)
        foreign = net_found and (net_found[0] in peer_families or (
            net_found[0] in ("swd", "jtag", "programming") and ics and not access
            and not target))
        if not foreign:
            names.append((row.net, "net name"))
    for text, basis in names:
        tokens = set(_tokens(text))
        if tokens & {"DEBUG", "DBG"}:
            # A vendor's SPI debug port (DEBUG_CLK, DEBUG_MOSI, DEBUG_MISO, DEBUG_CS#).
            role = next((label for words, label in _DEBUG_SPI if tokens & words), None)
            if role:
                out.append((role, f"{basis} {text}"))
                continue
        found = classify(text, pin=basis == "pin name")
        if not found:
            continue
        function, role = found
        own = basis == "pin name"  # a net named RESET may be another part's reset
        if function in ("swd", "jtag", "programming") and role:
            out.append((role, f"{basis} {text}"))
        elif function == "reset" and own:
            out.append(("reset", f"{basis} {text}"))
        elif function == "boot_strap" and role == "boot" and own:
            out.append(("boot", f"{basis} {text}"))
    alt_set = set(row.alternatives)
    if _STM32_TEXT.search(mcu_text):
        for port, role in (("PA13", "swdio"), ("PA14", "swclk"), ("PB3", "swo")):
            if port in alt_set:
                out.append((role, f"vendor default: {port} is {role.upper()} after reset on "
                                  "STM32-family parts"))
    if esp := _esp_boot(mcu_text):
        for names_, role in ((esp[0], "boot"), (esp[1], "uart_tx"), (esp[2], "uart_rx"),
                             (("EN", "CHIP_PU", "CHIP_EN"), "reset")):
            hit = next((name for name in names_ if name in alt_set), None)
            if hit:
                out.append((role, f"vendor default: {hit} on this ESP32 or ESP8266 family "
                                  "(serial bootloader)"))
    if _STM32_TEXT.search(mcu_text):
        for port, role in (("PA9", "uart_tx"), ("PA10", "uart_rx")):
            if port in alt_set:
                out.append((role, f"vendor default: {port} is USART1, the UART the STM32 "
                                  "system bootloader uses"))
    seen, unique = set(), []
    for role, basis in out:
        if role not in seen:
            seen.add(role)
            unique.append((role, basis))
    return unique


# Which pin wins a debug role when several carry it: the pin's own name (a dedicated TCK)
# before a vendor default, before a net name (a soft-core JTAG on user I/O).
_BASIS_RANK = (("pin name", 0), ("vendor default", 1), ("net name", 2))


def _basis_rank(basis: str) -> int:
    return next((rank for prefix, rank in _BASIS_RANK if basis.startswith(prefix)), 3)


def _access(fmap: PinMap, row: PinRow, testpoint_nets: set[str] | None,
            role: str | None = None) -> list[str]:
    """Where a probe or cable reaches a pin: connectors, test points and testpoint pads; for
    a boot pin also the switch or jumper that sets it."""
    if row.reach is None:
        return []
    found = [peer.label() for peer in row.reach.fitted_peers() if fmap.is_access(peer.ref)]
    if testpoint_nets:
        found += [f"testpoint pad on {net}" for net in row.reach.nets if net in testpoint_nets]
    if role == "boot":
        found += [f"set by {peer.label()}" for peer in row.reach.fitted_peers()
                  if peer.kind == "switch" or designator_prefix(peer.ref) in ("JP", "SJ")]
    return list(dict.fromkeys(found))


def _chain(fmap: PinMap, row: PinRow) -> list[str]:
    """Other parts' JTAG pins a JTAG pin reaches (a JTAG chain: TDO to the next TDI)."""
    return [peer.label() for peer in (row.reach.fitted_peers() if row.reach else [])
            if peer.kind in ("ic", "other") and peer.direct
            and any(function == "jtag" for function, _ in name_functions(peer.name))]


def _debug_section(fmap: PinMap, mcu: Candidate, rows: list[PinRow],
                   testpoint_nets: set[str] | None,
                   hosts: set[str] = frozenset()) -> tuple[list[dict], list[dict]]:
    text = fmap.text(mcu.designator)
    by_role: dict[str, list[tuple[PinRow, str]]] = defaultdict(list)
    for row in rows:
        if row.power:
            continue
        for role, basis in _debug_roles(fmap, row, text):
            by_role[role].append((row, basis))
    for listed in by_role.values():
        listed.sort(key=lambda item: _basis_rank(item[1]))  # stable: pin order within a rank
    interfaces = []
    for name, required, optional in _DEBUG_SETS:
        found = sum(role in by_role for role in required)
        # A partial SWD or JTAG set is listed (a missing pin is worth a look); other sets
        # only when complete.
        if found < len(required) and not (name in ("swd", "jtag") and found):
            continue
        pins = []
        for role in (*required, *optional):
            for row, basis in by_role.get(role, [])[:1]:
                access = _access(fmap, row, testpoint_nets, role)
                entry = {"role": role, "pin": row.pin, "name": row.name, "net": row.net,
                         "basis": basis, "access": access}
                if row.net is None:
                    entry["not_connected"] = True
                if not access:
                    host = [peer.label() for peer in _gpio_drivers(fmap, row, hosts)]
                    if host:
                        entry["via_processor"] = host[:2]
                    if name == "jtag" and (chain := _chain(fmap, row)):
                        entry["jtag_chain"] = chain[:2]
                pins.append(entry)
        missing = [role for role in required if role not in by_role]
        needed = [pin for pin in pins if pin["role"] in required]
        reached = [pin for pin in needed if pin["access"]]
        interface = {
            "interface": name, "pins": pins,
            "missing_roles": missing,
            "complete": not missing and len(reached) == len(needed),
            "reached": len(reached), "required": len(required),
        }
        if not missing and needed and all(pin["access"] or pin.get("via_processor")
                                          for pin in needed) and len(reached) < len(needed):
            interface["in_system"] = True  # reached through another processor's GPIOs
        interfaces.append(interface)
    usb = [row for row in rows if row.function == "usb" and row.role in ("dp", "dm")]
    usb_access = sorted({label for row in usb for label in _access(fmap, row, testpoint_nets)})
    findings = []
    complete = [entry for entry in interfaces if entry["complete"]]
    usable = [entry for entry in interfaces if entry["reached"]]
    in_system = [entry for entry in interfaces if entry.get("in_system")]
    # A part named in mcu= that is no processor (a PHY's boundary scan) gets no warning.
    severity = "info" if mcu.forced and mcu.kind not in ("mcu", "fpga", "radio", "module") \
        else "warning"
    if not complete:
        listed = "; ".join(
            f"{entry['interface']} ({', '.join(pin['role'] + ' pin ' + pin['pin'] for pin in entry['pins'][:4])})"
            for entry in interfaces[:3])
        # Every data and clock pin of every set recognised is unconnected (a boot or reset
        # pin wired up does not make a programming path).
        unconnected = all(pin.get("not_connected") for entry in interfaces
                          for pin in entry["pins"] if pin["role"] not in ("boot", "reset", "mclr")
                          and pin["role"] in next(roles for name, roles, _ in _DEBUG_SETS
                                                  if name == entry["interface"]))
        if interfaces and not usable and in_system:
            entry = in_system[0]
            hosts_named = sorted({label.split(".")[0] for pin in entry["pins"]
                                  for label in pin.get("via_processor", [])}, key=natural_key)
            findings.append(_finding(
                "debug_access", "info", mcu.designator,
                f"{mcu.designator}: its {entry['interface']} pins reach no connector or test "
                f"point, only GPIOs of {', '.join(hosts_named)}: it is programmed in-system by "
                "that processor's firmware alone. A header or pads on the same lines help "
                "bring-up and recovery.", [mcu.designator, *hosts_named],
                interface=entry["interface"]))
        elif interfaces and not usable:
            usb_text = (f"; only USB reaches a connector ({', '.join(usb_access[:3])}), which "
                        "programs the part only through a USB bootloader (DFU) in its ROM or "
                        "one programmed before") if usb_access else ""
            if usb_access and not unconnected:
                findings.append(_finding(
                    "debug_access", "info", mcu.designator,
                    f"{mcu.designator}: its debug or programming pins ({listed}) reach no "
                    f"connector or test point{usb_text}. The schematic review checklist asks "
                    "for a programming header or pads.", [mcu.designator],
                    interfaces=[entry["interface"] for entry in interfaces]))
            else:
                findings.append(_finding(
                    "debug_access", severity, mcu.designator,
                    f"{mcu.designator}: no debug or programming path found: its debug or "
                    f"programming pins ({listed}) "
                    + ("are not connected" if unconnected else "reach no connector, test point"
                       + (" or testpoint pad" if testpoint_nets is not None else "")
                       + " directly or through series parts")
                    + usb_text + ". The schematic review checklist asks for a programming "
                    "header or pads on every programmable part; check the pins against the "
                    "datasheet (they may be named otherwise).",
                    [mcu.designator], interfaces=[entry["interface"] for entry in interfaces]))
        elif not interfaces:
            findings.append(_finding(
                "debug_access", "info", mcu.designator,
                f"{mcu.designator}: no SWD, JTAG or vendor programming pins were recognised "
                "by pin or net name"
                + (f"; USB reaches {', '.join(usb_access[:3])}" if usb_access else "")
                + ". Check the datasheet for its debug pins and that they reach a header or "
                "test pads.", [mcu.designator]))
    for entry in usable:
        if entry["complete"]:
            continue
        required = next(roles for name, roles, _ in _DEBUG_SETS if name == entry["interface"])
        lacking = [f"{pin['role']} (pin {pin['pin']}"
                   + (f", in a JTAG chain through {', '.join(pin['jtag_chain'])}"
                      if pin.get("jtag_chain") else "") + ")"
                   for pin in entry["pins"] if pin["role"] in required and not pin["access"]]
        lacking += [f"{role} (not found)" for role in entry["missing_roles"]]
        findings.append(_finding(
            "debug_access", "info", mcu.designator,
            f"{mcu.designator}: {entry['interface']} reaches a connector or test point on "
            f"{entry['reached']} of {entry['required']} required pins; not on "
            f"{', '.join(lacking[:6])}.", [mcu.designator], interface=entry["interface"]))
    return interfaces, findings


# STM32F1, F2 and F4 (and GD32F10x, F40x) read BOOT1 on PB2 when BOOT0 is high.
_BOOT1_TEXT = re.compile(r"(?<![A-Z0-9])(?:STM32F[124]|GD32F[14]0)", re.I)


def _strap_rows(fmap: PinMap, mcu: Candidate,
                rows: list[PinRow]) -> list[tuple[PinRow, str, str, str | None]]:
    """(row, kind, basis, vendor role): boot, strap, mode and reset pins of the MCU; vendor
    role 'boot1' for an STM32F1/F2/F4's PB2."""
    text = fmap.text(mcu.designator)
    straps = next((pins for pattern, pins in _STRAP_FAMILIES if pattern.search(text)), ())
    out = []
    for row in rows:
        if row.power or row.function in ("nc",):
            continue
        kind = basis = vendor = None
        for alt in row.alternatives:
            found = control_kind(alt, pin=True)
            named = classify(alt, pin=True)
            if named and named[0] in ("jtag", "swd"):
                continue  # TRST, NTRST: the debug port's reset
            if found in ("boot", "strap", "mode", "program", "reset"):
                kind, basis = found, f"pin name {alt}"
                break
            if named and named[0] == "boot_strap" and named[1]:
                kind, basis = named[1], f"pin name {alt}"  # MSEL0
                break
            if mcu.kind == "fpga" and alt in ("M0", "M1", "M2"):
                kind, basis = "mode", f"pin name {alt} (an FPGA's configuration mode pin)"
                break
        if kind is None and straps:
            hit = next((alt for alt in row.alternatives if alt in straps or
                        re.sub(r"^IO", "GPIO", alt) in straps), None)
            if hit:
                kind, basis = "strap", f"a strapping pin of this family ({hit})"
            elif {"EN", "CHIP_PU", "CHIP_EN"} & set(row.alternatives):
                kind, basis = "reset", "the chip enable of this family (EN resets it)"
        if kind is None and "PB2" in row.alternatives and _BOOT1_TEXT.search(text):
            kind, basis, vendor = "boot", "BOOT1 on this family (PB2)", "boot1"
        if kind is None and row.net and not auto_net(row.net) and row.gpio_like:
            found = control_kind(row.net)
            peer_boot = row.reach and any(control_kind(peer.name, pin=True) in (
                "boot", "strap", "mode") for peer in row.reach.fitted_peers())
            if found in ("boot", "strap", "mode") and not peer_boot:
                kind, basis = found, f"net name {row.net}"
        if kind:
            out.append((row, kind, basis, vendor))
    return out


def _bootloader_level(text: str, row: PinRow) -> tuple[str, str] | None:
    """(level, what happens) when a known family's boot pin, held at that level at every
    reset, starts its ROM bootloader instead of the program."""
    if _STM32_TEXT.search(text) and "BOOT0" in row.alternatives:
        return "high", ("an STM32-family part with BOOT0 high starts its system-memory "
                        "bootloader, not the program in flash (unless its option bytes ignore "
                        "the pin, as nSWBOOT0 does on newer series)")
    esp = _esp_boot(text)
    if esp and set(esp[0]) & set(row.alternatives):
        return "low", ("an ESP32 or ESP8266 with its boot pin low at reset enters the serial "
                       "download mode instead of running its flash")
    return None


def _strap_section(fmap: PinMap, mcu: Candidate, rows: list[PinRow],
                   hosts: set[str] = frozenset()) -> tuple[list[dict], list[dict]]:
    text = fmap.text(mcu.designator)
    entries, findings, listed = [], [], []
    for row, kind, basis, vendor in _strap_rows(fmap, mcu, rows):
        entry: dict = {"pin": row.pin, "name": row.name, "net": row.net, "kind": kind,
                       "basis": basis}
        if row.net is None:
            entry.update(level="not_connected", level_basis="the pin is not connected: the "
                         "part's internal default applies (see its datasheet)")
        else:
            level = default_level(fmap, row, hosts)
            entry["level"] = level.pop("level")
            entry["level_basis"] = level.pop("basis")
            entry.update(level)
            entry["pulls"] = [pull.describe() for pull in row.pulls][:4]
        entries.append(entry)
        listed.append((row, kind, vendor, entry))
    # BOOT1 matters only when BOOT0 can be high.
    boot0 = next((entry["level"] for row, _, _, entry in listed
                  if "BOOT0" in row.alternatives), None)
    for row, kind, vendor, entry in listed:
        level = entry["level"]
        what = "reset" if kind == "reset" else f"{kind} pin"
        designators = [mcu.designator, *(pull.resistor for pull in row.pulls)]
        changeable = entry.get("set_by") or entry.get("driven_by") or entry.get("gpio_driven_by")
        bootloader = _bootloader_level(text, row)
        if level == "mid_rail":
            findings.append(_finding(
                "boot_straps", "warning", mcu.designator,
                f"{_pin_label(row)} ({kind}) on {row.net} sits at {entry.get('ratio', 0):.0%} of "
                f"the rail ({entry['level_basis']}): neither a clear high nor a clear low, so "
                "the boot mode it selects is undefined. Check the divider values.",
                designators, pin=row.pin, net=row.net, level=level))
        elif bootloader and level == bootloader[0] and not changeable:
            findings.append(_finding(
                "boot_straps", "warning", mcu.designator,
                f"{_pin_label(row)} on {row.net} is held {level} ({entry['level_basis']}) and "
                f"no switch, jumper or driver changes it: {bootloader[1]}, at every reset. "
                "Check the level against the datasheet's boot table.",
                designators, pin=row.pin, net=row.net, level=level))
        elif level in ("internal_default", "not_connected") and not (
                vendor == "boot1" and boot0 in ("low", None)):
            findings.append(_finding(
                "boot_straps", "info", mcu.designator,
                f"{_pin_label(row)} ({what}{', BOOT1' if vendor == 'boot1' else ''}) has no "
                "pull, tie or driver on the board"
                + (f" ({len(entry.get('not_fitted', []))} pull(s) Not Fitted in variant "
                   f"{fmap.variant})" if entry.get("not_fitted") else "")
                + (": with BOOT0 high it selects system memory or SRAM, so a floating BOOT1 "
                   "leaves that boot undefined (tie it low through a resistor)."
                   if vendor == "boot1" else
                   ": the part's internal default applies (many MCUs pull reset up and some "
                   "boot pins down internally; see the datasheet for its level)."),
                designators, pin=row.pin, net=row.net, level=level))
        elif level == "driven" and entry.get("gpio_driven_by") and not (
                entry.get("driven_by") or entry.get("set_by")):
            findings.append(_finding(
                "boot_straps", "info", mcu.designator,
                f"{_pin_label(row)} ({what}) is driven only by "
                f"{', '.join(entry['gpio_driven_by'][:2])}, another processor's GPIO, with no "
                "pull: it floats while that processor is in reset or until its firmware sets "
                "the pin, and the part's internal default applies then. A pull resistor keeps "
                "it defined.",
                [*designators, *(label.split(".")[0] for label in entry["gpio_driven_by"])],
                pin=row.pin, net=row.net, level=level))
    return entries, findings


# ---------- external memory ----------

_MEM_CS = re.compile(r"N?CS\d?|CSN|S|NS|N?CE\d?|CEN|SS|NSS|E")
_MEM_HOLD = re.compile(r"N?HOLD|HOLDN|N?RESET|RESETN|N?RST|RSTN|HOLD_?RESET")
_MEM_WP = re.compile(r"N?WP|WPN|W|VPP|ACC")
_MEM_ADDR = re.compile(r"A[0-2]|E[0-2]")
_MEM_CLOCK = re.compile(r"SCK|SCLK|CLK|C|SCL|DCLK")
_MEM_IO = re.compile(r"IO[0-7]|DQ[0-7]|SIO[0-7]|D[0-7]|DATA[0-7]")
_MEM_DATA = re.compile(r"SI|SO|DI|DO|D|Q|MOSI|MISO|SDI|SDO")


def _mem_role(name: str) -> str | None:
    tokens = [form for token in _tokens(name) for form in _forms(token)]
    alts = alternatives(name)
    family = classify(name, pin=True)
    if family and family[0] == "jtag" and family[1]:
        return f"jtag_{family[1]}"
    if i2c_line(name):
        return i2c_line(name)[0].lower()
    if any(_MEM_HOLD.fullmatch(token) for token in tokens):
        if any(re.fullmatch(r"IO3|DQ3|SIO3", alt) for alt in alts + tokens):
            return "hold_io3"
        return "hold" if any("HOLD" in token for token in tokens) else "reset"
    if any(_MEM_WP.fullmatch(token) for token in tokens):
        if any(re.fullmatch(r"IO2|DQ2|SIO2", alt) for alt in alts + tokens):
            return "wp_io2"
        return "wp"
    for token in tokens:
        if _MEM_IO.fullmatch(token):
            return token.lower()
    if any(_MEM_CLOCK.fullmatch(token) for token in tokens):
        return "clock"
    if any(_MEM_CS.fullmatch(token) for token in tokens):
        return "cs"
    if any(_MEM_DATA.fullmatch(token) for token in tokens):
        return "data"
    if any(_MEM_ADDR.fullmatch(token) for token in tokens):
        return "address"
    return None


def _pin_state(fmap: PinMap, ref: str, pin: str, net: str | None, mcu_refs: set[str]) -> str:
    design = fmap.design
    if net is None:
        return "not connected"
    if net in design.grounds:
        return f"tied low ({net})"
    if net in design.rails:
        return f"tied high ({net})"
    members = [(other, other_pin) for other, other_pin in design.nets.get(net, ())
               if other != ref]
    pulls = [pull for member in link_group(design, net) for pull in design.pulls.get(member, ())
             if pull.fitted]
    mcu = [f"{other}.{other_pin}" for member in link_group(design, net)
           for other, other_pin in design.nets.get(member, ()) if other in mcu_refs]
    if mcu:
        return "driven by " + ", ".join(mcu[:2]) + (f" ({_pull_text(pulls)})" if pulls else "")
    if pulls:
        return _pull_text(pulls)
    if not members and not fmap.ctx.edges.get(net):
        return "floating (alone on its net)"
    return f"on {net}"


def _memory_section(fmap: PinMap, mcu_rows: dict[str, list[PinRow]], candidates: set[str],
                    testpoint_nets: set[str] | None) -> tuple[list[dict], list[dict]]:
    design = fmap.design
    mcu_refs = set(mcu_rows)
    # MCU pins by every net their walk reaches (with the series parts on the way).
    by_net: dict[str, list[tuple[PinRow, _Peer | None]]] = defaultdict(list)
    for rows in mcu_rows.values():
        for row in rows:
            if row.reach is None or row.power or row.tie:
                continue
            for peer in row.reach.fitted_peers():
                if peer.direct:
                    by_net[f"{peer.ref}\0{peer.pin}"].append((row, peer))
    entries, findings = [], []
    for ref in sorted(design.part_pins, key=natural_key):
        if ref in mcu_refs or ref in candidates or not fmap.fitted(ref):
            continue
        if designator_prefix(ref) not in _CANDIDATE_PREFIXES or not _memory(fmap.text(ref)):
            continue
        pins = fmap.all_pins(ref)
        roles = {pin: _mem_role(fmap.pin_name(ref, pin)) for pin in pins}
        links = []
        for pin in pins:
            if roles[pin] is None:
                continue
            for row, peer in by_net.get(f"{ref}\0{pin}", ()):
                links.append({"memory_pin": pin, "memory_name": fmap.pin_name(ref, pin),
                              "role": roles[pin], "mcu": row.mcu, "mcu_pin": row.pin,
                              "mcu_name": row.name, "net": row.net,
                              **({"through": list(peer.through)} if peer.through else {})})
        if not links:
            continue
        linked_roles = {link["role"] for link in links}
        configuration = bool(_CONFIG_MEMORY.search(fmap.text(ref)))
        if {"sda", "scl"} & linked_roles:
            bus = "i2c"
        elif {"io2", "io3", "dq2", "dq3", "sio2", "sio3", "data2", "data3", "wp_io2",
              "hold_io3"} & linked_roles and {"clock", "cs"} & linked_roles:
            bus = "qspi"
        elif "clock" in linked_roles:
            # An FPGA's serial configuration PROM (D0, CLK, CE, OE) streams its bits: not SPI.
            bus = "serial_configuration" if configuration else "spi"
        elif sum(1 for role in roles.values() if role == "address") >= 6 or len(links) >= 12:
            bus = "parallel"
        else:
            bus = "unknown"
        state = {}
        for pin, role in roles.items():
            if role in ("wp", "hold", "reset", "cs", "wp_io2", "hold_io3", "address") and not any(
                    link["memory_pin"] == pin for link in links):
                net = (design.part_pins.get(ref) or {}).get(pin)
                state[f"{fmap.pin_name(ref, pin) or pin} (pin {pin})"] = _pin_state(
                    fmap, ref, pin, net, mcu_refs)
        cs = [link for link in links if link["role"] == "cs"]
        # External programming access: the memory's bus nets reaching a connector or pads.
        access = []
        for pin, net in (design.part_pins.get(ref) or {}).items():
            if roles.get(pin) in (None, "address") or net in design.grounds or net in design.rails:
                continue
            reach = _reach(fmap, ref, pin, net)
            access += [peer.label() for peer in reach.fitted_peers() if fmap.is_access(peer.ref)]
            if testpoint_nets:
                access += [f"testpoint pad on {member}" for member in reach.nets
                           if member in testpoint_nets]
        access = list(dict.fromkeys(access))
        entry = {"designator": ref, "part": (fmap.ctx.comment.get(ref) or "")[:80] or None,
                 "bus": bus, "use": "FPGA configuration" if configuration else None,
                 "links": links[:16], "chip_select": [
                     f"{link['mcu']}.{link['mcu_pin']}" for link in cs] or None,
                 "control_pins": state or None, "external_access": access[:8]}
        entries.append({key: value for key, value in entry.items() if value is not None})
        mcus = sorted({link["mcu"] for link in links}, key=natural_key)
        what = ("FPGA configuration memory" if bus == "serial_configuration"
                else f"{bus} {'configuration ' if configuration else ''}memory")
        floating = [label for label, value in state.items()
                    if value.startswith(("not connected", "floating"))]
        holds = [label for label in floating if re.search(r"HOLD|RESET|RST", label.upper())]
        if holds and bus in ("spi", "qspi"):
            findings.append(_finding(
                "external_memory", "warning", mcus[0],
                f"{ref} ({bus.upper()} memory on {', '.join(mcus)}): {', '.join(holds)} "
                f"{'is' if len(holds) == 1 else 'are'} not connected or alone on a net: a "
                "floating HOLD or RESET input can pause or reset the memory at random. Tie it "
                "high or pull it up unless the datasheet gives it an internal pull-up.",
                [ref, *mcus], memory=ref, bus=bus))
        if access:
            text = (f"{ref} ({what} on {', '.join(mcus)}): its bus reaches "
                    f"{', '.join(access[:4])}, so it can be read and written on a prototype "
                    "from outside (hold the MCU in reset or its pins high-impedance while you "
                    "do).")
        else:
            text = (f"{ref} ({what} on {', '.join(mcus)}): its bus reaches no connector "
                    "or test point, so it is read and written only through "
                    + ("the FPGA it configures (JTAG indirect programming)" if configuration
                       else "the MCU's firmware")
                    + " or a clip on the package. The schematic review checklist asks for a "
                    "way to read and write flash or EEPROM on prototypes.")
        if state:
            text += " Pins not on the MCU: " + "; ".join(
                f"{label}: {value}" for label, value in list(state.items())[:5]) + "."
        findings.append(_finding("external_memory", "info", mcus[0], text, [ref, *mcus],
                                 memory=ref, bus=bus))
    return entries, findings


# ---------- other checks ----------

_DEBUG_PORT_ROLES = frozenset({"swdio", "swclk", "swo", "tck", "tms", "tdi", "tdo", "trst"})


def _unused_findings(fmap: PinMap, mcu: Candidate, rows: list[PinRow]) -> list[dict]:
    """Unused GPIO pins; the debug port's pins (SWD after reset on an STM32) are left out of
    the advice, since setting them to analog or output disables debugging, and strap pins
    are named apart."""
    unused = [row for row in rows if row.unused and row.function != "nc"]
    if not unused:
        return []
    text = fmap.text(mcu.designator)
    debug = {row.pin for row in unused
             if any(role in _DEBUG_PORT_ROLES for role, _ in _debug_roles(fmap, row, text))}
    straps = {row.pin for row, *_ in _strap_rows(fmap, mcu, unused)}
    spare = [row for row in unused if row.pin not in debug]

    def names(chosen: list[PinRow], limit: int = 40) -> str:
        return ", ".join(f"{row.pin} ({row.name})" if row.name else row.pin
                         for row in chosen[:limit]) + (
            f" and {len(chosen) - limit} more" if len(chosen) > limit else "")

    issue = f"{mcu.designator}: "
    if spare:
        issue += (f"{len(spare)} GPIO pin{'s are' if len(spare) != 1 else ' is'} not connected "
                 f"or alone on a net: {names(spare)}. Firmware should set unused pins to a "
                 "defined state (analog or output low, or an internal pull) to save power and "
                 "avoid floating inputs; spare pins may also be worth bringing to a test pad.")
        strap = [row for row in spare if row.pin in straps]
        if strap:
            issue += (f" {names(strap, 6)} {'are' if len(strap) > 1 else 'is'} also a boot "
                     "or strap pin, read at reset (see boot_straps).")
    if debug:
        port = [row for row in unused if row.pin in debug]
        issue += (" " if spare else "") + (
            f"{names(port, 8)} {'are' if len(port) > 1 else 'is'} the debug port after reset "
            "and unconnected: leave them in their debug function (setting them to analog or "
            "output disables debugging) or bring them to pads.")
    return [_finding("unused_pins", "info", mcu.designator, issue, [mcu.designator],
                     pins=[row.pin for row in spare],
                     debug_pins=[row.pin for row in unused if row.pin in debug] or None)]


def _conflict_findings(fmap: PinMap, mcu: Candidate, rows: list[PinRow]) -> list[dict]:
    findings = []
    by_net: dict[str, list[PinRow]] = defaultdict(list)
    for row in rows:
        if row.net and not row.power and not row.tie:
            by_net[row.net].append(row)
    for net, members in sorted(by_net.items(), key=lambda item: natural_key(item[0])):
        if len(members) < 2:
            continue
        outputs = [row for row in members if row.type == "Output"]
        severity = "warning" if len(outputs) >= 2 else "info"
        # Pins tied together to a rail or ground through a 0 ohm link, or a strap pin
        # sharing a LED or data line (a PHY's CONFIG pins), are wired so on purpose.
        if severity == "info" and (any(pull.fitted and pull.ohms is not None and pull.ohms <= 100
                                       for pull in members[0].pulls) or any(
                found[0] == "boot_strap" for row in members for found in name_functions(row.name))):
            continue
        findings.append(_finding(
            "conflicting_assignment", severity, mcu.designator,
            f"{mcu.designator} pins {', '.join(f'{row.pin} ({row.name})' for row in members[:6])} "
            f"share {net}"
            + (": two pins typed Output drive one net." if severity == "warning" else
               ": intentional for some uses (a timer input beside an ADC, a pin pair for "
               "current), else a short; only one may drive it in firmware."),
            [mcu.designator], pins=[row.pin for row in members], net=net))
    for row in rows:
        if row.power or not row.net or auto_net(row.net) or not row.port or mcu.kind == "fpga":
            continue
        net_fn = classify(row.net)
        listed = [found for alt in row.alternatives if alt != row.port.text
                  and (found := classify(alt, pin=True)) and found[0] != "nc"]
        if not net_fn or net_fn[0] not in SIGNAL_BUS_FUNCTIONS or len(listed) < 2:
            continue
        functions = {function for function, _ in listed}
        same = net_fn[0] in functions or (net_fn[0] == "debug_uart" and "uart" in functions) or (
            net_fn[0] == "uart" and "debug_uart" in functions)
        if not same:
            findings.append(_finding(
                "conflicting_assignment", "info", mcu.designator,
                f"{_pin_label(row)}: the net {row.net} names {net_fn[0]}, but the pin's name "
                f"lists only {', '.join(sorted(functions))}: the function may not be "
                "available on this pin (if the library lists every alternate function), or "
                "the net is named for the other end. Check the pin-mux table.",
                [mcu.designator], pin=row.pin, net=row.net))
    return findings


def _variant_findings(fmap: PinMap, mcu: Candidate, rows: list[PinRow], plans: list) -> list[dict]:
    findings = []
    names = [plan.name for plan in plans]
    for row in rows:
        if row.reach is None or row.power:
            continue
        parts = list(dict.fromkeys([*row.reach.series, *(peer.ref for peer in row.reach.peers),
                                    *(pull.resistor for pull in row.pulls)]))
        changes, values = {}, {}
        for part in parts:
            fitted_in = [plan.name for plan in plans if part not in plan.not_fitted]
            if 0 < len(fitted_in) < len(plans):
                changes[part] = {"fitted_in": fitted_in,
                                 "not_fitted_in": [name for name in names if name not in fitted_in]}
            varied = {plan.name: next((value for key, (_, value) in (plan.params.get(part) or {})
                                       .items() if key in ("value", "comment")), None)
                      for plan in plans}
            if any(varied.values()) and len(set(varied.values())) > 1:
                values[part] = varied
        if not changes and not values:
            continue
        text = "; ".join(f"{part} fitted in {', '.join(info['fitted_in'])}, Not Fitted in "
                         f"{', '.join(info['not_fitted_in'])}" for part, info in list(changes.items())[:4])
        if values:
            text += ("; " if text else "") + "; ".join(
                f"{part} value " + ", ".join(f"{name}: {value or 'as drawn'}"
                                             for name, value in varied.items())
                for part, varied in list(values.items())[:3])
        findings.append(_finding(
            "variant_changes", "info", mcu.designator,
            f"{_pin_label(row)} on {row.net}: its connections change between variants ({text}). "
            "Firmware that runs on several variants has to handle each.",
            [mcu.designator, *changes, *values], pin=row.pin, net=row.net,
            parts=changes or None, values=values or None))
    return findings


# ---------- output ----------

def _row_dict(row: PinRow) -> dict:
    out: dict = {"pin": row.pin}
    if row.name and row.name != row.pin:
        out["name"] = row.name
    if row.port is not None:
        if row.port.port is not None:
            out["port"] = row.port.port
        if row.port.bit is not None:
            out["bit"] = row.port.bit
    if row.type:
        out["type"] = row.type
    out["net"] = row.net
    out["function"] = row.function
    if row.role:
        out["role"] = row.role
    if row.basis:
        out["basis"] = row.basis
    if row.candidates:
        out["alternates"] = row.candidates
    if row.pulls:
        out["pulls"] = [{key: value for key, value in pull.describe().items()
                         if not (key == "on" and value == row.net)} for pull in row.pulls[:4]]
    if row.reach is not None:
        fitted = row.reach.fitted_peers()
        if fitted:
            out["peers"] = [peer.describe() for peer in fitted[:PEER_LIMIT]]
            if len(fitted) > PEER_LIMIT:
                out["peers_total"] = len(fitted)
        absent = [peer for peer in row.reach.peers if not peer.fitted]
        if absent:
            out["not_fitted_paths"] = [peer.label() for peer in absent[:3]]
        if row.reach.same_part:
            out["same_part_pins"] = [pin for pin, _ in row.reach.same_part][:4]
    if row.unused:
        out["unused"] = True
    return out


def _power_summary(rows: list[PinRow]) -> dict:
    out: dict = {}
    for row in rows:
        if not row.power:
            continue
        group = out.setdefault(row.function, {})
        group.setdefault(row.net or "(not connected)", []).append(row.pin)
    return out


def _peer_text(row: PinRow) -> str:
    if row.reach is None:
        return ""
    return "; ".join(peer.label() for peer in row.reach.fitted_peers()[:6])


CSV_COLUMNS = ("mcu", "pin", "name", "port", "bit", "type", "net", "function", "role", "pull",
               "peers", "basis")


def to_csv(rows: list[PinRow]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for row in rows:
        writer.writerow([
            row.mcu, row.pin, row.name, row.port.port if row.port and row.port.port else "",
            row.port.bit if row.port and row.port.bit is not None else "", row.type or "",
            row.net or "", row.function, row.role or "", _pull_text(row.pulls) or "",
            _peer_text(row), "; ".join(row.basis),
        ])
    return buffer.getvalue()


_IDENT = re.compile(r"[^A-Z0-9_]+")


def identifier(text: str) -> str:
    """A C identifier from a net or pin name: USB_D+ -> USB_DP, nRESET -> NRESET, 3V3_EN ->
    P_3V3_EN, RST# -> RST_N."""
    raw = (text or "").strip()
    upper = raw.upper().replace("\\", "")
    if raw[:1] in ("~", "!", "/") or "\\" in raw:
        upper = "N" + upper.lstrip("~!/")  # ~CS, an overlined RST
    upper = re.sub(r"#|\*$", "_N", upper)
    upper = upper.replace("+", "P")
    upper = re.sub(r"-$", "N", upper)
    upper = _IDENT.sub("_", upper)
    upper = re.sub(r"_+", "_", upper).strip("_") or "PIN"
    return f"P_{upper}" if upper[0].isdigit() else upper


def _row_ident(row: PinRow) -> str:
    if row.net and not auto_net(row.net):
        return identifier(row.net)
    base = row.port.text if row.port else (row.alternatives[0] if row.alternatives else row.pin)
    return identifier(f"{base}")


# Vendors whose GPIO naming the C header's PORT macro and the devicetree's controller
# labels follow; any other part's port is written plainly (C) or as a comment (devicetree).
_VENDORS = (
    ("stm32", _STM32_TEXT),
    ("nrf", re.compile(r"(?<![A-Z0-9])NRF(?:5\d|91|54)", re.I)),
    ("kinetis", re.compile(r"(?<![A-Z0-9])(?:MK[LEVW]?\d{2}|S32K|Kinetis)", re.I)),
    ("lpc", re.compile(r"(?<![A-Z0-9])LPC\d{2,4}", re.I)),
    ("imx", re.compile(r"(?<![A-Z0-9])(?:MIMXRT|IMXRT|i\.?MX)", re.I)),
    ("esp32", _ESP32_TEXT),
    ("rp2040", _RP2040_TEXT),
)


def _vendor(text: str) -> str | None:
    return next((name for name, pattern in _VENDORS if pattern.search(text or "")), None)


def _port_macro(row: PinRow, mcu_text: str = "") -> str | None:
    """The PORT macro: GPIOA on STM32-family parts, PTA for Kinetis (PTA1), PORTB for PIC
    (RB3), the port number for numbered schemes, else the port letter as a character."""
    port = row.port
    if port is None or port.port is None:
        return None
    if port.scheme == "lettered":
        return f"GPIO{port.port}" if _vendor(mcu_text) == "stm32" else f"'{port.port}'"
    if port.scheme == "kinetis":
        return f"PT{port.port}"
    if port.scheme == "pic":
        return f"PORT{port.port}"
    return port.port


def _idents(rows: list[PinRow], prefix: bool) -> dict[int, str]:
    """A C identifier per row (by id), made unique over the rows given: pass every row of
    the map, not one page, so concatenated pages define each name once."""
    used: dict[str, int] = defaultdict(int)
    out = {}
    for row in rows:
        ident = _row_ident(row)
        if prefix:
            ident = f"{identifier(row.mcu)}_{ident}"
        used[ident] += 1
        out[id(row)] = ident if used[ident] == 1 else f"{ident}_{used[ident]}"
    return out


def _text_rows(rows: list[PinRow]) -> list[PinRow]:
    """The rows the C header and devicetree list: connected signal pins."""
    return [row for row in rows if not row.power and row.net and row.function not in (
        "nc", "unconnected", "tied")]


def to_c_header(rows: list[PinRow], variant: str, page_note: str = "",
                texts: dict[str, str] | None = None,
                idents: dict[int, str] | None = None) -> str:
    """texts: each MCU's part text (its vendor picks the PORT macro); idents: identifiers
    made over every row of the map (_idents), so pages do not redefine a name."""
    signal = _text_rows(rows)
    mcus = sorted({row.mcu for row in rows}, key=natural_key)
    texts = texts or {}
    lines = [
        "/*",
        f" * Firmware pin map for {', '.join(mcus)} (variant {variant}){page_note}.",
        " * Generated by altium-copilot get_firmware_pin_map from the schematic netlist: a",
        " * heuristic starting point. Check every line against the schematic, the datasheet",
        " * and the pin-mux table before use.",
        " * PORT is GPIOx on STM32-family parts, PTx (Kinetis), PORTx (PIC), the port number",
        " * for numbered ports, else the port letter as a character; PIN is the bit, or the",
        " * GPIO number when the name gives no port. Pins whose naming scheme is not",
        " * recognised are comments only.",
        " */",
    ]
    idents = idents or _idents(signal, prefix=len(mcus) > 1)
    for row in signal:
        ident = idents[id(row)]
        note = f"pin {row.pin}" + (f" {row.name}" if row.name and row.name != row.pin else "")
        note += f", {row.function}" + (f" {row.role}" if row.role else "")
        pull = _pull_text([pull for pull in row.pulls if pull.fitted])
        note += f", pull {pull}" if pull else ", no pull"
        if row.net:
            note += f", net {row.net}"
        note = note.replace("*/", "* /")
        macro = _port_macro(row, texts.get(row.mcu, ""))
        if row.port is not None and row.port.bit is not None:
            if macro is not None:
                lines.append(f"#define {ident}_PORT {macro}")
                lines.append(f"#define {ident}_PIN {row.port.bit}  /* {note} */")
            else:
                lines.append(f"#define {ident}_PIN {row.port.bit}  /* GPIO number, no port; "
                             f"{note} */")
        else:
            lines.append(f"/* {ident}: port scheme not recognised, not emitted; {note} */")
    return "\n".join(lines) + "\n"


def _dt_controller(row: PinRow, mcu_text: str) -> tuple[str, int] | None:
    """Zephyr's GPIO controller label and pin: &gpioa on STM32, &gpio0 on nRF and LPC,
    &gpio1 on i.MX RT, Kinetis' PTA1 on &gpioa, ESP32 and RP2040 GPIO numbers; None for
    any other vendor (SAM's &porta, AVR, MSP430, PIC, Renesas are not guessed)."""
    port = row.port
    if port is None or port.bit is None:
        return None
    vendor = _vendor(mcu_text)
    if port.scheme == "kinetis" or (port.scheme == "lettered" and vendor == "stm32"):
        return f"gpio{port.port.lower()}", port.bit
    if (port.scheme == "numbered" and vendor in ("nrf", "lpc")) or port.scheme == "imx":
        return f"gpio{port.port}", port.bit
    if port.scheme in ("gpio_number", "io_number"):
        if vendor == "esp32":
            return ("gpio0", port.bit) if port.bit < 32 else ("gpio1", port.bit - 32)
        if vendor == "rp2040":
            return "gpio0", port.bit
    return None


def to_devicetree(rows: list[PinRow], variant: str, texts: dict[str, str],
                  page_note: str = "", idents: dict[int, str] | None = None,
                  prefix: bool | None = None) -> str:
    """texts: each MCU's part text (its vendor picks the controller label); idents and
    prefix (MCU designators lead the names): made over every row of the map."""
    signal = _text_rows(rows)
    mcus = sorted({row.mcu for row in rows}, key=natural_key)
    prefix = len(mcus) > 1 if prefix is None else prefix
    lines = [
        "/*",
        f" * Zephyr-style devicetree fragment for {', '.join(mcus)} (variant {variant})"
        f"{page_note}.",
        " * Generated by altium-copilot get_firmware_pin_map from the schematic netlist: a",
        " * heuristic STARTING POINT, not a board file. Controller labels follow Zephyr's usual",
        " * names for STM32 (&gpioa), Kinetis (&gpioa), nRF and LPC (&gpio0), i.MX RT (&gpio1),",
        " * ESP32 and RP2040; other parts' pins are comments. The active level is guessed from",
        " * the net name (an n prefix, _N suffix, # or ~, or _B or _L after a control name such",
        " * as RESET_B, means active low); peripheral pins belong in pinctrl and are listed as",
        " * comments. Check every line.",
        " */",
        "/ {",
        "\tzephyr,user {",
    ]
    idents = idents or _idents(signal, prefix=prefix)
    comments = []
    for row in signal:
        note = f"pin {row.pin}" + (f" {row.name}" if row.name and row.name != row.pin else "")
        note += f", net {row.net}, {row.function}" + (f" {row.role}" if row.role else "")
        note = note.replace("*/", "* /")
        if row.function not in GPIO_FUNCTIONS and not row.drives:
            comments.append(f"/* {row.function}: {note} (configure in pinctrl) */")
            continue
        controller = _dt_controller(row, texts.get(row.mcu, ""))
        prop = idents[id(row)].lower().replace("_", "-") + "-gpios"
        if not prop[0].isalpha():
            prop = "pin-" + prop
        if len(prop) > 31:  # a devicetree property name has at most 31 characters
            prop = (f"{identifier(row.mcu)}_" if prefix else "") + identifier(
                row.port.text if row.port else f"PIN_{row.pin}")
            prop = prop.lower().replace("_", "-") + "-gpios"
        if controller is None:
            why = ("no controller label known for this part" if row.port and row.port.bit
                   is not None else "port scheme not recognised")
            comments.append(f"/* {prop}: {why}, not emitted; {note} */")
            continue
        level = "GPIO_ACTIVE_LOW" if negated(row.net) or negated(row.name if not row.port
                                                                  else "") else "GPIO_ACTIVE_HIGH"
        lines.append(f"\t\t{prop} = <&{controller[0]} {controller[1]} {level}>;\t/* {note} */")
    lines += ["\t};", "};"]
    if comments:
        lines.append("/* Not emitted as gpios: */")
        lines += comments
    return "\n".join(lines) + "\n"


# ---------- entry points ----------

def _overview(rows_by_mcu: dict[str, list[PinRow]]) -> list[dict]:
    out = []
    for mcu, rows in rows_by_mcu.items():
        functions: dict[str, int] = defaultdict(int)
        for row in rows:
            if not row.power:
                functions[row.function] += 1
        gpio = [row for row in rows if row.gpio_like]
        ported = [row for row in gpio if row.port and row.port.bit is not None]
        out.append({
            "designator": mcu, "pins": len(rows),
            "signal_pins": sum(1 for row in rows if not row.power),
            "power_pins": sum(1 for row in rows if row.function == "power"),
            "ground_pins": sum(1 for row in rows if row.function == "ground"),
            "not_connected": sum(1 for row in rows if row.net is None),
            "gpio_named_pins": len(gpio),
            "port_parsed": len(ported),
            "functions": dict(sorted(functions.items(), key=lambda item: -item[1])),
        })
    return out


def build_map(netlist: dict, plan=None, project_params: dict | None = None,
              mcu: list[str] | None = None, all_processors: bool = False):
    """(PinMap, candidates, mapped Candidates, error or None, notes)."""
    fmap = PinMap(netlist, plan, project_params)
    candidates = rank_candidates(fmap)
    notes: list[str] = []
    if mcu:
        wanted, unknown = resolve_names(mcu, fmap.design.part_pins)
        if not wanted:
            return fmap, candidates, [], {
                "error": "mcu_not_found",
                "message": f"No connected part {unknown} in the compiled netlist; use "
                           "search_components to find the designator."}, notes
        if unknown:
            notes.append(f"Not found: {', '.join(unknown)}.")
        by_ref = {candidate.designator: candidate for candidate in candidates}
        mapped = []
        for ref in sorted(wanted, key=natural_key):
            if ref not in by_ref:
                notes.append(f"{ref} is not recognised as a processor (few GPIO-named pins, no "
                             "processor words); mapped as asked.")
                names = [fmap.pin_name(ref, pin) for pin in fmap.all_pins(ref)]
                by_ref[ref] = Candidate(ref, part_kind(fmap.text(ref)), 0.0, 0,
                                        sum(1 for pin in fmap.all_pins(ref)
                                            if fmap.pin_type(ref, pin) == "IO"),
                                        len(names), None, ["named in mcu="], fmap.fitted(ref),
                                        forced=True)
            if not fmap.fitted(ref):
                notes.append(f"{ref} is Not Fitted in variant {fmap.variant}; mapped as asked.")
            mapped.append(by_ref[ref])
        return fmap, candidates, mapped, None, notes
    fitted = [candidate for candidate in candidates if candidate.fitted]
    if not fitted:
        return fmap, candidates, [], None, notes
    if all_processors:
        chosen = [candidate for candidate in fitted if candidate.kind in ("mcu", "radio", "module")]
        mapped = (chosen or fitted[:1])[:EXPORT_MCU_LIMIT]
    else:
        mapped = fitted[:1]
    return fmap, candidates, mapped, None, notes


def _netlist_notes(fmap: PinMap, netlist: dict, plan, involved: set[str]) -> list[str]:
    """Notes on what the compiled netlist hides: a part that reads as a processor but has
    no connected pin, and duplicate designators (compile_warnings) among the parts involved,
    whose pins and nets the compile merged."""
    notes = []
    warning = next((entry for entry in netlist.get("compile_warnings") or []
                    if entry.get("kind") == "duplicate_designators"), None)
    named = set((warning or {}).get("examples") or [])
    compiled = netlist.get("parts") or {}
    not_fitted = set(getattr(plan, "not_fitted", None) or ())
    silent = []
    for ref, meta in (netlist.get("components") or {}).items():
        if ref in fmap.design.part_pins or ref in not_fitted or \
                designator_prefix(ref) not in _CANDIDATE_PREFIXES:
            continue
        text = " ".join(str(value) for value in (
            meta.get("description"), meta.get("value"), (compiled.get(ref) or {}).get("comment"))
            if value)
        if PROCESSOR_TEXT.search(text) or _MCU_FAMILY.search(text) or _FPGA_TEXT.search(text):
            silent.append(ref)
    if silent:
        silent.sort(key=natural_key)
        one = len(silent) == 1
        notes.append(
            f"{', '.join(silent[:5])} read{'s' if one else ''} as a processor but "
            f"{'has' if one else 'have'} no connected pin in the compiled netlist, so "
            f"{'it is' if one else 'they are'} not mapped"
            + ("; the compile reports duplicate designators, which merge one part's pins "
               "and nets into another part of the same designator" if warning else "") + ".")
    hit = sorted(named & involved, key=natural_key)
    if hit:
        notes.append(
            f"{', '.join(hit[:6])} {'is' if len(hit) == 1 else 'are'} among the compile's "
            "duplicate designators (different parts compiled to one designator): their pins "
            "and nets are merged, so rows, peers and memory pins that involve them may be "
            "wrong. Annotate them apart in Altium.")
    elif warning and (warning.get("count") or 0) > len(named) and involved:
        notes.append(f"The compile reports {warning.get('count')} duplicate designators, only "
                     f"{len(named)} named: parts here may be merged with others.")
    return notes


@dataclass
class FirmwareMap:
    """A built pin map and review, paged by page() (the tool shrinks its pages to fit)."""
    head: dict
    fmt: str
    fmap: PinMap | None = None
    mapped: list[Candidate] = field(default_factory=list)
    rows_by_mcu: dict[str, list[PinRow]] = field(default_factory=dict)
    sections: dict = field(default_factory=dict)
    findings: list[dict] = field(default_factory=list)
    checks_run: list[str] = field(default_factory=list)
    not_checked: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def page(self, offset: int = 0, limit: int = ROW_LIMIT) -> dict:
        offset = max(0, int(offset or 0))
        result = dict(self.head)
        if not self.mapped:
            result.update({"mapped": [], "rows_total": 0, "findings": [], "total": 0,
                           "offset": offset, "has_more": False, "notes": self.notes})
            return result
        findings = self.findings
        summary: dict[str, int] = defaultdict(int)
        for finding in findings:
            summary[finding["check"]] += 1
        result["mapped"] = [candidate.designator for candidate in self.mapped]
        result["checks_run"] = self.checks_run
        result["summary"] = dict(summary)
        result["severity_counts"] = {level: sum(1 for f in findings if f["severity"] == level)
                                     for level in ("warning", "info")}
        result["overview"] = _overview(self.rows_by_mcu)
        all_rows = [row for rows in self.rows_by_mcu.values() for row in rows]
        page_rows = all_rows if self.fmt == "csv" else [row for row in all_rows if not row.power]
        if self.fmt in ("c_header", "devicetree"):
            page_rows = _text_rows(page_rows)
        page = page_rows[offset:offset + limit]
        note = (f", pins {offset + 1}-{offset + len(page)} of {len(page_rows)}"
                if len(page_rows) > limit or offset else "")
        result["rows_total"] = len(page_rows)
        variant = self.head["variant"]
        if self.fmt == "table":
            result["power_pins"] = {mcu: _power_summary(rows)
                                    for mcu, rows in self.rows_by_mcu.items()}
            several = len(self.mapped) > 1
            result["rows"] = [{"mcu": row.mcu, **_row_dict(row)} if several else _row_dict(row)
                              for row in page]
        elif self.fmt == "csv":
            result["csv"] = to_csv(page)
        else:
            # Identifiers over every row, so pages joined together define each name once.
            texts = {mcu: self.fmap.text(mcu) for mcu in self.rows_by_mcu}
            idents = _idents(page_rows, prefix=len(self.mapped) > 1)
            if self.fmt == "c_header":
                result["c_header"] = to_c_header(page, variant, note, texts, idents)
            else:
                result["devicetree"] = to_devicetree(page, variant, texts, note, idents,
                                                     prefix=len(self.mapped) > 1)
        result.update(self.sections)
        result["findings"] = findings[offset:offset + limit]
        result["total"] = len(findings)
        result["offset"] = offset
        result["has_more"] = offset + limit < max(len(findings), len(page_rows))
        if result["has_more"]:
            result["next_offset"] = offset + limit
        if self.not_checked:
            result["not_checked"] = self.not_checked
        result["notes"] = [*self.notes, (
            "Rows and findings page together (offset). A pin's peers are the parts on its net "
            f"and behind up to {HOP_LIMIT} series parts (resistors under 1 kohm, 0 ohm links, "
            "ferrites, chokes; a larger resistor or a series capacitor only as the last step); "
            "pulls, capacitors to ground and ESD parts are not peers. Functions come from a "
            "dedicated pin name, the net name, the parts reached and the pin's alternate "
            "functions, in that order, except that a pin's own single alternate function wins "
            "over a peer pin's name it does not list (basis says which).")]
        return result


def _format(text: str | None) -> str:
    fmt = (text or "table").strip().lower().replace("-", "_")
    return {"json": "table", "dts": "devicetree", "header": "c_header", "h": "c_header",
            "c": "c_header"}.get(fmt, fmt)


def build_firmware_map(
    netlist: dict,
    plan=None,
    project_params: dict | None = None,
    mcu: list[str] | None = None,
    checks: list[str] | None = None,
    format: str = "table",
    variant_plans: list | None = None,
    testpoint_loader=None,
    candidate_limit: int = CANDIDATE_LIMIT,
) -> FirmwareMap | dict:
    """The pin map and its review (an error dict for bad arguments): candidates, the
    mapped processors' pin rows, boot straps, debug access, external memory and findings,
    warnings first. Page it with FirmwareMap.page.

    variant_plans (VariantPlan per project variant) feed variant_changes, which needs two
    or more. testpoint_loader, when given, returns (net names with a testpoint pad, why
    not): it is called only when a debug or memory path reaches no connector or test
    point in the netlist."""
    fmt = _format(format)
    if fmt not in FORMATS:
        return {"error": "invalid_format", "message": f"format must be one of {list(FORMATS)}."}
    requested = [check.strip().lower() for check in checks or [] if check.strip()]
    if invalid := [check for check in requested if check not in CHECKS]:
        return {"error": "invalid_checks",
                "message": f"Unknown checks {invalid}; use {list(CHECKS)}."}
    wanted = set(requested or CHECKS)
    fmap, candidates, mapped, error, notes = build_map(netlist, plan, project_params, mcu)
    if error:
        return error
    comments = {candidate.designator: fmap.ctx.comment.get(candidate.designator)
                for candidate in candidates}
    head = {"variant": fmap.variant, "heuristic": HEURISTIC,
            "candidates_total": len(candidates),
            "candidates": [candidate.describe(comments.get(candidate.designator))
                           for candidate in candidates[:candidate_limit]]}
    if not mapped:
        notes.append("No processor found: no U or IC part fitted in the variant has four or "
                     "more GPIO-named pins (PA0, P0.12, GPIO12 ...) or processor or FPGA words "
                     "with IO-typed pins. Name the part in mcu= to map it anyway.")
        notes += _netlist_notes(fmap, netlist, plan, set())
        return FirmwareMap(head, fmt, fmap, notes=notes)
    if any(candidate.kind == "fpga" for candidate in mapped):
        notes.append("An FPGA is mapped: its pin functions come from its HDL design, so "
                     "functions here come from net names and the parts each pin reaches, not from its "
                     "alternate names.")
    rows_by_mcu = {candidate.designator: pin_rows(fmap, candidate) for candidate in mapped}
    all_rows = [row for rows in rows_by_mcu.values() for row in rows]
    candidate_refs = {candidate.designator for candidate in candidates}
    findings: list[dict] = []
    not_checked: dict[str, str] = {}
    loaded: dict = {}

    def testpoints() -> set[str] | None:
        """Testpoint pad nets from the PcbDoc, loaded once and only when needed."""
        if "nets" not in loaded:
            nets, why = testpoint_loader() if testpoint_loader is not None else (None, None)
            loaded.update(nets=nets, why=why)
        return loaded["nets"]

    sections: dict = {}
    if "debug_access" in wanted:
        debug = {}
        for candidate in mapped:
            rows = rows_by_mcu[candidate.designator]
            if candidate.kind == "io_expander":
                notes.append(f"{candidate.designator} looks like an I/O expander: debug access "
                             "not checked.")
                continue
            interfaces, found = _debug_section(fmap, candidate, rows, None, candidate_refs)
            # Testpoint pads only help a recognised interface that reaches nothing.
            if found and interfaces and testpoints():
                interfaces, found = _debug_section(fmap, candidate, rows, testpoints(),
                                                   candidate_refs)
            debug[candidate.designator] = interfaces
            findings += found
        sections["debug_access"] = debug
    if "boot_straps" in wanted:
        straps = {}
        for candidate in mapped:
            entries, found = _strap_section(fmap, candidate, rows_by_mcu[candidate.designator],
                                            candidate_refs)
            straps[candidate.designator] = entries
            findings += found
        sections["boot_straps"] = straps
    if "external_memory" in wanted:
        memories, found = _memory_section(fmap, rows_by_mcu, candidate_refs, None)
        if any(not entry["external_access"] for entry in memories) and testpoints():
            memories, found = _memory_section(fmap, rows_by_mcu, candidate_refs, testpoints())
        sections["external_memory"] = memories
        findings += found
    if "uart_crossing" in wanted:
        findings += _uart_findings(fmap, all_rows, candidate_refs)
    for candidate in mapped:
        rows = rows_by_mcu[candidate.designator]
        if "unused_pins" in wanted:
            findings += _unused_findings(fmap, candidate, rows)
        if "conflicting_assignment" in wanted:
            findings += _conflict_findings(fmap, candidate, rows)
    if "variant_changes" in wanted:
        plans = list(variant_plans or [])
        if len(plans) >= 2:
            for candidate in mapped:
                findings += _variant_findings(fmap, candidate, rows_by_mcu[candidate.designator],
                                              plans)
        else:
            not_checked["variant_changes"] = "the project defines fewer than two variants"
    if loaded.get("nets") is None and loaded.get("why"):
        notes.append(f"Testpoint pads not read: {loaded['why']}.")
    involved = {*rows_by_mcu, *(peer.ref for row in all_rows if row.reach
                                for peer in row.reach.fitted_peers()),
                *(entry["designator"] for entry in sections.get("external_memory", []))}
    notes += _netlist_notes(fmap, netlist, plan, involved)
    findings.sort(key=lambda finding: (_SEVERITY_RANK[finding["severity"]],
                                       _CHECK_RANK[finding["check"]], natural_key(finding["mcu"]),
                                       natural_key(str(finding.get("pin", "")))))
    checks_run = [check for check in CHECKS if check in wanted and check not in not_checked]
    return FirmwareMap(head, fmt, fmap, mapped, rows_by_mcu, sections, findings, checks_run,
                       not_checked, notes)


def run_firmware_pin_map(netlist: dict, plan=None, project_params: dict | None = None,
                         offset: int = 0, limit: int = ROW_LIMIT, **options) -> dict:
    """build_firmware_map and one page of it (rows and findings from offset, limit each)."""
    built = build_firmware_map(netlist, plan, project_params, **options)
    if isinstance(built, dict):
        return built
    return built.page(offset, limit)


def export_rows(netlist: dict, plan=None, project_params: dict | None = None) -> list[dict]:
    """Rows for package_for_xfn's firmware_pins table: every pin of each processor the map
    picks (every fitted MCU, radio or module candidate, at most four; else the top one)."""
    fmap, _, mapped, error, _ = build_map(netlist, plan, project_params, all_processors=True)
    if error:
        return []
    out = []
    for candidate in mapped:
        for row in pin_rows(fmap, candidate):
            peers = [] if row.reach is None else [
                peer.describe() for peer in row.reach.fitted_peers()[:8]]
            out.append({
                "mcu": row.mcu, "pin": row.pin, "name": row.name,
                "port": row.port.port if row.port else None,
                "bit": row.port.bit if row.port else None,
                "net": row.net, "function": row.function, "role": row.role,
                "pull": _pull_text(row.pulls), "peers": peers,
            })
    return out
