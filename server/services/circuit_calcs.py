"""Circuit calculators (heuristic): resistor dividers, LED and optocoupler currents, ADC
input filters and other RC low-pass filters. Each result carries its inputs (each value
with where it came from), its formula, its result and a confidence, so a review can
recalculate it; Vf, Vref, thresholds and sample capacitors are datasheet values, so the
results are calculations to check, not verdicts.

Dividers: a node (the midpoint) with a resistor leg to ground and one to a rail, a net of
known voltage or a driven signal (a push-pull output, a processor GPIO that is not an
input), or legs to two rails of known voltage; a leg is one resistor, resistors in
parallel to the same net, or a series chain through nodes that hold only the chain and
capacitors, a test point, jumper or connector pin (noted: something connected there
changes the ratio), high-impedance IC inputs (a resistor stack: each tap is a midpoint of
its own, its bottom leg the rest of the stack) or an open-drain output (hysteresis: the
ratio with it off, and with it sinking when it sits on the bottom leg). A midpoint that a
push-pull output drives, that carries a supply pin or a processor's bus pin, or that is
a rail's source is a pull-up/pull-down pair or a rail, not a divider; a series resistor
into a pull-up (no ground leg) is not one either, nor is a series resistor of up to
1 kohm from a driving pin with a pull-down at a ratio of 0.9 or more (a termination;
kept, and labelled, when it drives a transistor's gate or base). A resistor that a
jumper, header or switch joins to one of the divider's ends is a selectable leg (its
ratio, and Vout/Vref for a feedback divider, with it alone), not a consumer. Its result:
ratio = Rbot / (Rtop + Rbot), the unloaded midpoint voltage when the top voltage is known
(derating's net voltages, with source and confidence; a GPIO's high level from its I/O
rail), the source impedance Rtop || Rbot and the divider current, and the min/max ratio
when every resistor's tolerance is read (a Tolerance parameter, '1%' in its text, or the
tolerance letter of a Yageo, Samsung, Stackpole, Vishay, Panasonic or Bourns part
number). What it feeds, on the midpoint or one series resistor beyond: a converter's
FB/ADJ (get_power_tree's feedback row when it reads the same divider, else its formula
here: Vout/Vref, and the implied Vref from the top's voltage, from rails= too, or the
output a given vref sets; a three-terminal adjustable regulator's Vref sits across the
top resistor), a converter's EN/UVLO (V_trip = V_th / ratio for a given vth, else at
1.2 V and 0.8 V as examples only), a supervisor's or comparator's input (V_trip for a
given vth), a reference IC (TL431: the top a given vref sets, or the implied Vref), an
ADC input, an op-amp input (with the non-inverting gain when the top is its own output),
an IC's reference input, a transistor's base or gate, another IC or processor pin, or a
connector.

LEDs (check_derating's: an LED or DS part, or a diode whose text says LED; two pins by A/K
names, else by the circuit: ground is the cathode's side, a rail beats a sinking pin; a
multi-colour part by its common pin or channel numbers; one with supply or data pins has
its own driver and is only counted; LED+/LED- or LEDA/LEDK pin pairs on another part, a
display's backlight, at low or medium confidence and with no indicator rating), the
input diodes of optocouplers (by their words or part-number families; A/K pins by name,
else the standard 4-, 6- and 8-pin pinouts when the names it has agree, at low
confidence): the anode side is walked to its supply (a rail directly or through series
resistors or resistors in parallel, a GPIO with its I/O rail, a high-side transistor to
its emitter's or source's rail) and the cathode side to its sink (ground, a GPIO or IC
pin sinking, a low-side transistor; an LED driver sets its own current), past a diode
across the diode itself, and I = (V_supply - V_sink - Vf) / R - Vf / R_across (a
resistor across the diode takes its share; when it holds the diode below Vf, the diode
stays off) with Vf from vf= (per part or colour), a stated forward-voltage parameter, a
Vf in the text (VF=, or a voltage beside a current: '30 mA, 2 V'), the colour in the
part's parameters, text or wavelength (typical: red 2.0 V, orange, amber and yellow
2.1 V, green 2.2 V, true green (InGaN) 3.0 V, blue and white 3.0 V, IR 1.3 V; an
optocoupler's input 1.2 V), else the current at Vf 2.0 V and 3.0 V; a colour or Vf one
LED states is shared by the LEDs of the same part number that state none. Each
resistor's power at that current against its rating (check_derating's: stated, else
typical for its size). Optocouplers also report a resistor or capacitor across the input
diode (noise immunity), the output's pull-up (or emitter resistor) with its rail, the
output current at saturation (V / R) and the CTR it needs (I_out / I_F).

ADC inputs: processor pins whose function (get_firmware_pin_map's reading) is analog, or
that list an ADC function and carry a divider or a filter, and the AIN/CH/IN pins of ADC
parts: the series R (or the divider's source impedance, plus a series R between), the
shunt C to ground and any capacitor to another input, and fc = 1 / (2 pi R C). RC filters:
other nodes between an IC (or a connector or switch) and an IC with one series resistor
and a capacitor to ground, with fc.

Findings: led_overcurrent (I above the stated rating or a typical 20 mA indicator rating:
a warning when Vf is known (vf=, stated or by colour), both voltages are read and I
exceeds 30 mA or a rating a parameter states, else info: a current in the text is often
the test current), diode_shunted_off (a resistor across an LED or optocoupler input holds
it below Vf: warning, info when a typical Vf decides it by less than 15%),
divider_output_above_input_rating (the unloaded midpoint above the reached pin's rail +
0.3 V, the usual absolute maximum: warning; info when that rail is a guess; an EN pin
against its converter's input), divider_output_below_ground (the midpoint below -0.3 V
on a pin powered from a positive rail: the same severities), resistor_overpower (I^2 x R
at the computed LED or input current above the resistor's rating: warning; above the
derated limit: info; with an unknown Vf, a warning when even Vf 3.0 V overloads it, info
when only 2.0 V does), adc_no_filter (no shunt capacitor at an ADC input: info) and
opto_no_input_shunt (nothing across an optocoupler's input diode: info).

Guidance: Altium's schematic review checklist (recalculate every voltage divider, compute
LED and optoisolator current-limit resistors, a filter on every ADC input, R/C across
opto diodes for noise immunity). Everything here is read from names and text: values
are nominal, loads on a midpoint are ignored, and the datasheet decides every threshold.
"""

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

try:
    from services.bom import natural_key
    from services.decoupling import designator_prefix, farads_text, first_function, pin_role
    from services.derating import (
        Part, merged_params, net_voltages, policy_or_defaults, read_parts, resistor_limits,
    )
    from services.firmware_pins import (
        SIGNAL_BUS_FUNCTIONS, PinMap, alternatives, auto_net, classify, pin_rows,
        rank_candidates,
    )
    from services.logic_levels import AMPLIFIER_TEXT, OPTO_TEXT, build_chips, pin_rail
    from services.power_tree import (
        COMMON_VREF_TOLERANCE, LINK_OHMS, build_tree, diode_side, fb_formula,
        fb_gain, floating_reference, parallel, tree_data, vout_or_vref,
    )
    from services.pull_resistors import (
        COMPARATOR_TEXT, ohms_text, open_drain_name, push_pull_drivers, resolve_names,
    )
except ImportError:
    from server.services.bom import natural_key
    from server.services.decoupling import designator_prefix, farads_text, first_function, pin_role
    from server.services.derating import (
        Part, merged_params, net_voltages, policy_or_defaults, read_parts, resistor_limits,
    )
    from server.services.firmware_pins import (
        SIGNAL_BUS_FUNCTIONS, PinMap, alternatives, auto_net, classify, pin_rows,
        rank_candidates,
    )
    from server.services.logic_levels import AMPLIFIER_TEXT, OPTO_TEXT, build_chips, pin_rail
    from server.services.power_tree import (
        COMMON_VREF_TOLERANCE, LINK_OHMS, build_tree, diode_side, fb_formula,
        fb_gain, floating_reference, parallel, tree_data, vout_or_vref,
    )
    from server.services.pull_resistors import (
        COMPARATOR_TEXT, ohms_text, open_drain_name, push_pull_drivers, resolve_names,
    )

KINDS = ("dividers", "leds", "optos", "adc_inputs", "rc_filters")
_ROW_KIND = {"dividers": "divider", "leds": "led", "optos": "opto", "adc_inputs": "adc_input",
             "rc_filters": "rc_filter"}
_KIND_RANK = {kind: rank for rank, kind in enumerate(_ROW_KIND.values())}
CHECKS = ("led_overcurrent", "diode_shunted_off", "divider_output_above_input_rating",
          "divider_output_below_ground", "resistor_overpower", "adc_no_filter",
          "opto_no_input_shunt")
_CHECK_RANK = {check: rank for rank, check in enumerate(CHECKS)}
# The kind of calculation each check belongs to (resistor_overpower and diode_shunted_off
# carry their row's kind: an LED's or an optocoupler's).
_CHECK_KIND = {"led_overcurrent": "leds", "diode_shunted_off": None,
               "divider_output_above_input_rating": "dividers",
               "divider_output_below_ground": "dividers", "resistor_overpower": None,
               "adc_no_filter": "adc_inputs", "opto_no_input_shunt": "optos"}
_SEVERITY_RANK = {"warning": 0, "info": 1}
PAGE_LIMIT = 100
CHAIN_LIMIT = 6  # resistors followed in one series leg
NEEDED_LISTED = 12  # nets of unknown voltage listed, most calculations first
# A driven top this small at a ratio this high is a series resistor and a pull-down (a
# termination, a gate or base drive), not a level divider.
SERIES_PULL_OHMS = 1000.0
SERIES_PULL_RATIO = 0.9

LED_RATING_A = 0.020  # a typical indicator LED's rating when none is stated
LED_WARN_A = 0.030  # above this (with a known Vf) an overcurrent is a warning
ABS_MAX_MARGIN = 0.3  # the usual absolute maximum of an input: its rail + 0.3 V
EXAMPLE_THRESHOLDS = (1.2, 0.8)  # EN thresholds shown as examples when none is given
REFERENCE_VREFS = (1.24, 1.25, 2.495, 2.5)  # shunt references' REF voltages (TLV431, TL431)
OPTO_VF = 1.2  # an optocoupler's input LED (IR), typical
UNKNOWN_VF = (2.0, 3.0)  # the current is given at these when the colour is not known
MAX_RC_FARADS = 100e-6  # a larger capacitor on a signal node is bulk, not a filter
TYPICAL_VF = {"red": 2.0, "orange": 2.1, "amber": 2.1, "yellow": 2.1, "green": 2.2,
              "true_green": 3.0, "blue": 3.0, "white": 3.0, "ir": 1.3}

HEURISTIC = (
    "Calculations from names and part text: resistor values and tolerances, net voltages "
    "(rails=, net names, the power tree), LED colours and pin roles are read, not measured; "
    "midpoints are unloaded, Vf, Vref, EN thresholds, GPIO current limits and ADC sample "
    "capacitors are datasheet values. Recalculate with the datasheet's figures before "
    "calling a result wrong."
)
GUIDANCE = (
    "Altium's schematic review checklist: recalculate every voltage divider, compute LED "
    "and optoisolator current-limit resistors, put a filter on every ADC input, and an R "
    "or C across opto input diodes for noise immunity."
)

# ---------- reading values ----------

_TOL_LETTER = {"B": 0.001, "C": 0.0025, "D": 0.005, "F": 0.01, "G": 0.02, "J": 0.05,
               "K": 0.10}
_TOL_PARAMS = ("tolerance", "tol", "resistance tolerance", "tolerance percent",
               "tolerance pct", "resistor tolerance")
_TOL_TEXT = re.compile(r"(?<![\w.,])(?:±|\+/-|\+-)?\s?(\d{1,2}(?:[.,]\d{1,3})?)\s?%")
_STANDARD_TOL = frozenset({0.01, 0.02, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0})
# Tolerance letters of published chip-resistor part numbers.
_TOL_MPN = (
    ("Yageo/Samsung RC", re.compile(r"^(?:RC|AC|RT)\d{4,5}([BCDFGJK])")),
    ("Stackpole RMC", re.compile(r"^RMC[FP]\d{4}([BCDFGJK])")),
    ("Vishay CRCW", re.compile(r"^CRCW\d{4}[0-9RKM]{4}([BCDFGJK])")),
    ("Panasonic ERJ", re.compile(r"^ERJ-?(?:\d[A-Z]|[A-Z]{2}|\d\d[A-Z]?|P[A-Z]\d)[A-Z]{0,2}?"
                                 r"([BDFGJ])(?:\d{3,4}|\d+R\d*)[A-Z]$")),
    ("Bourns CR", re.compile(r"^CR\d{4}-([BCDFGJK])X-")),
)


def tolerance_value(text: str | None, bare: bool = False) -> float | None:
    """A tolerance as a fraction: '1%', '±0.5 %', '+/-5%'; with bare=True (a Tolerance
    parameter) also '1' (percent) or a letter code ('F' is 1%)."""
    text = (text or "").strip()
    if bare:
        if text.upper() in _TOL_LETTER:
            return _TOL_LETTER[text.upper()]
        plain = re.fullmatch(r"(?:±|\+/-)?\s?(\d{1,2}(?:[.,]\d{1,3})?)\s?%?", text)
        if plain:
            value = float(plain[1].replace(",", "."))
            return value / 100 if 0 < value <= 20 else None
    for match in _TOL_TEXT.finditer(text):
        value = float(match[1].replace(",", "."))
        if value in _STANDARD_TOL:
            return value / 100
    return None


def resistor_tolerance(part: Part) -> tuple[float, str] | None:
    """(fraction, source) of a resistor's tolerance: a Tolerance parameter, then its value,
    comment or description text, then its part number's tolerance letter."""
    for name in _TOL_PARAMS:
        if name in part.params:
            raw_name, raw = part.params[name]
            value = tolerance_value(raw, bare=True)
            if value is not None:
                return value, f"parameter ({raw_name})"
    for label, text in part.texts:
        value = tolerance_value(text)
        if value is not None:
            return value, f"text ({label})"
    for mpn in part.mpns:
        compact = re.sub(r"\s+", "", mpn.upper())
        for family, pattern in _TOL_MPN:
            if match := pattern.match(compact):
                return _TOL_LETTER[match[1]], f"mpn ({family})"
    return None


_W = r"(?<![A-Za-z])"  # a word edge an underscore or digit also makes (LED_GREEN_K1)
_E = r"(?![A-Za-z])"
_COLOUR_TEXT = (
    ("ir", re.compile(rf"{_W}(?:IR|IRED|NIR){_E}|infra-?red", re.I)),
    ("true_green", re.compile(r"true[- _]?green|pure[- _]?green|\bInGaN\b[^,;]*green|"
                              r"green[^,;]*\bInGaN\b", re.I)),
    ("white", re.compile(rf"{_W}(?:white|WHT){_E}", re.I)),
    ("blue", re.compile(rf"{_W}(?:blue|BLU){_E}", re.I)),
    ("amber", re.compile(rf"{_W}amber{_E}", re.I)),
    ("orange", re.compile(rf"{_W}orange{_E}", re.I)),
    ("yellow", re.compile(rf"{_W}(?:yellow|YEL|YLW){_E}", re.I)),
    ("green", re.compile(rf"{_W}(?:green|GRN){_E}", re.I)),
    ("red", re.compile(rf"{_W}red{_E}", re.I)),
)
# A colour named in a pin name of a multi-colour LED (R, G, B, RED, GRN ...).
_PIN_COLOUR = {"R": "red", "RED": "red", "G": "green", "GRN": "green", "GREEN": "green",
               "B": "blue", "BLU": "blue", "BLUE": "blue", "Y": "yellow", "YEL": "yellow",
               "W": "white", "WHT": "white", "O": "orange"}
_WAVELENGTH = re.compile(r"(?<![\d.])(\d{3,4})\s?nm\b", re.I)
_MILLIAMPS = re.compile(r"(?<![\w.])(\d+(?:[.,]\d+)?)\s?mA\b")
_CURRENT = re.compile(r"(?<![\w.])(\d+(?:[.,]\d+)?)\s?(m|u|µ)?A\b")
VF_TEXT = re.compile(r"\bV_?F\b\s*(?:typ\.?)?\s*[=:]?\s*(\d(?:[.,]\d{1,2})?)\s?V\b", re.I)
# A current and a voltage side by side ('30 mA, 2 V', '2.1V @ 20mA'): the test current and Vf.
_VF_PAIR = re.compile(
    r"(?<![\w.])\d+(?:[.,]\d+)?\s?mA\s*[,;/]\s*(\d(?:[.,]\d{1,2})?)\s?V\b"
    r"|(?<![\w.])(\d(?:[.,]\d{1,2})?)\s?V\s*(?:[,;/@]|at)\s*\d+(?:[.,]\d+)?\s?mA\b", re.I)
_NOT_COLOUR_PARAM = re.compile(r"lens|body|housing|package|case|frame|reflector", re.I)


def colours_in(text: str | None) -> list[str]:
    """Every LED colour a text names (true green and IR before the plain words)."""
    found = []
    rest = text or ""
    for colour, pattern in _COLOUR_TEXT:
        if pattern.search(rest):
            found.append(colour)
            rest = pattern.sub(" ", rest)
    if "true_green" in found and "green" in found:
        found.remove("green")
    return found


def wavelength_colour(nm: float) -> str | None:
    """A colour for a dominant or peak wavelength (InGaN true green below 545 nm)."""
    if nm >= 760:
        return "ir"
    if nm >= 610:
        return "red"
    if nm >= 590:
        return "orange"
    if nm >= 575:
        return "yellow"
    if nm >= 545:
        return "green"
    if nm >= 490:
        return "true_green"
    if nm >= 400:
        return "blue"
    return None


@dataclass
class _Led:
    """What an LED's (or optocoupler's) parameters and text say."""
    colour: str | None = None
    colour_source: str | None = None
    colours: list[str] = field(default_factory=list)  # several named: a multi-colour part
    vf: float | None = None
    vf_source: str | None = None
    rating: float | None = None  # amps
    rating_source: str | None = None


def _number(text: str) -> float | None:
    match = re.search(r"(\d+(?:[.,]\d+)?)", text or "")
    return float(match[1].replace(",", ".")) if match else None


def _amps(text: str, bare_ma: bool) -> float | None:
    match = _CURRENT.search(text or "")
    if match:
        scale = {"m": 1e-3, "u": 1e-6, "µ": 1e-6}.get(match[2] or "", 1.0)
        return float(match[1].replace(",", ".")) * scale
    if bare_ma and (value := _number(text)) is not None:
        return value * 1e-3 if value > 1 else value
    return None


def read_led(params: dict[str, tuple[str, str]], text: str) -> _Led:
    """Colour, stated Vf and stated current rating from an LED's parameters (normalised
    name -> (raw name, value)), then its text."""
    out = _Led()
    for name, (raw_name, raw) in params.items():
        if "colo" in name and not _NOT_COLOUR_PARAM.search(name):
            found = colours_in(raw)
            if len(found) == 1 and out.colour is None:
                out.colour, out.colour_source = found[0], f"parameter ({raw_name})"
            elif len(found) > 1:
                out.colours = found
        elif "wavelength" in name and out.colour is None:
            nm = _number(raw)
            if nm and (colour := wavelength_colour(nm)):
                out.colour, out.colour_source = colour, f"parameter ({raw_name})"
        elif ("forward voltage" in name or name in ("vf", "v f") or name.startswith("vf ")) \
                and out.vf is None:
            volts = _number(raw)
            if volts is not None and 0.8 <= volts <= 5.0:
                out.vf, out.vf_source = volts, f"parameter ({raw_name})"
        elif ("forward current" in name or name in ("if", "i f", "current rating",
                                                     "max current", "if max")) \
                and out.rating is None:
            amps = _amps(raw, bare_ma=True)
            if amps is not None and 1e-3 <= amps <= 2.0:
                out.rating, out.rating_source = amps, f"parameter ({raw_name})"
    if out.colour is None and not out.colours:
        found = colours_in(text)
        if len(found) == 1:
            out.colour, out.colour_source = found[0], "text"
        elif len(found) > 1:
            out.colours = found
        elif match := _WAVELENGTH.search(text or ""):
            if colour := wavelength_colour(float(match[1])):
                out.colour, out.colour_source = colour, "text (wavelength)"
    if out.vf is None and (match := VF_TEXT.search(text or "")):
        volts = float(match[1].replace(",", "."))
        if 0.8 <= volts <= 5.0:
            out.vf, out.vf_source = volts, "text"
    if out.vf is None and (match := _VF_PAIR.search(text or "")):
        volts = float((match[1] or match[2]).replace(",", "."))
        if 1.2 <= volts <= 4.0:
            out.vf, out.vf_source = volts, "text (beside a current: read as Vf)"
    if out.rating is None and (match := _MILLIAMPS.search(text or "")):
        amps = float(match[1].replace(",", ".")) * 1e-3
        if 1e-3 <= amps <= 2.0:
            out.rating, out.rating_source = amps, "text"
    return out


# ---------- arguments ----------

_VF_ITEM = re.compile(r"\s*([^=:,]+?)\s*[=:]\s*(\d+(?:\.\d+)?)\s*[vV]?\s*")
_ALL_VOLTS = re.compile(r"\s*(\d+(?:\.\d+)?)\s*[vV]?\s*")
_COLOUR_KEYS = {"red": "red", "orange": "orange", "amber": "amber", "yellow": "yellow",
                "green": "green", "true_green": "true_green", "truegreen": "true_green",
                "blue": "blue", "white": "white", "ir": "ir", "infrared": "ir"}


def parse_vf(text: str | None) -> tuple[dict[str, float] | None, dict | None]:
    """'LED1=2.9,red=1.9' -> {'LED1': 2.9, 'red': 1.9}: per designator or per colour
    (red, orange, amber, yellow, green, true_green, blue, white, ir)."""
    error = {"error": "invalid_vf",
             "message": "vf takes forward voltages per LED or optocoupler designator or per "
                        "colour, e.g. 'LED1=2.9,red=1.9' (colours: red, orange, amber, "
                        "yellow, green, true_green, blue, white, ir), in volts from 0.5 to 6."}
    if text is None or not text.strip():
        return None, None
    out: dict[str, float] = {}
    for item in text.split(","):
        if not item.strip():
            continue
        match = _VF_ITEM.fullmatch(item)
        if not match or not 0.5 <= float(match[2]) <= 6:
            return None, error
        key = match[1].strip()
        out[_COLOUR_KEYS.get(key.casefold().replace(" ", "_"), key)] = float(match[2])
    return (out, None) if out else (None, error)


def parse_vth(text: str | None) -> tuple[dict[str, float] | float | None, dict | None]:
    """'U3=1.2,U7=0.8' -> per part; '1.2' -> one threshold for every EN, supervisor and
    comparator input a divider feeds."""
    error = {"error": "invalid_vth",
             "message": "vth takes the input threshold in volts of the part a divider feeds "
                        "(an EN/UVLO pin, a supervisor or comparator input), per designator, "
                        "e.g. 'U3=1.2,U7=0.8', or one number for all, e.g. '1.2'."}
    if text is None or not text.strip():
        return None, None
    if match := _ALL_VOLTS.fullmatch(text):
        value = float(match[1])
        return (value, None) if 0 < value < 100 else (None, error)
    out: dict[str, float] = {}
    for item in text.split(","):
        if not item.strip():
            continue
        match = _VF_ITEM.fullmatch(item)
        if not match or not 0 < float(match[2]) < 100:
            return None, error
        out[match[1].strip()] = float(match[2])
    return (out, None) if out else (None, error)


def _for_part(values, ref: str, everyone: bool = True) -> float | None:
    """The value given for a part: by its designator, or one value for every part (not
    when everyone is False: vref's one value is the converters' Vref, not a reference's)."""
    if isinstance(values, (int, float)):
        return float(values) if everyone else None
    if isinstance(values, dict):
        folded = {key.casefold(): value for key, value in values.items()}
        return folded.get(ref.casefold())
    return None


# ---------- formatting ----------

def _r(value: float | None, digits: int = 4) -> float | None:
    if value is None:
        return None
    if value == 0:
        return 0.0
    return round(value, max(0, digits - 1 - int(math.floor(math.log10(abs(value))))))


def _hz_text(hz: float) -> str:
    for unit, scale in (("MHz", 1e6), ("kHz", 1e3)):
        if hz >= scale:
            return f"{hz / scale:.3g} {unit}"
    return f"{hz:.3g} Hz"


def _ma(amps: float) -> float:
    return _r(amps * 1000, 4)


def _vtext(volts: float) -> str:
    return f"{_r(volts, 4):g}"


def _clean(row: dict) -> dict:
    return {key: value for key, value in row.items() if value not in (None, [], {})}


# ---------- the board ----------

@dataclass
class _Res:
    ref: str
    a: str
    b: str
    ohms: float | None
    part: Part

    def far(self, net: str) -> str:
        return self.b if net == self.a else self.a


@dataclass
class _Tap:
    """A node a series leg passes that holds more than the chain and capacitors: IC inputs
    (a tap of a resistor stack, 'input'), an open-drain output ('switch': hysteresis or a
    selectable threshold) or a test point, jumper or connector pin ('passive')."""
    net: str
    index: int  # resistors between the midpoint and this node
    what: str
    pins: list[str]


@dataclass
class _Leg:
    end: str
    resistors: list[_Res]
    taps: list[_Tap] = field(default_factory=list)

    @property
    def ohms(self) -> float | None:
        values = [res.ohms for res in self.resistors]
        return None if None in values else sum(values)

    @property
    def refs(self) -> list[str]:
        return [res.ref for res in self.resistors]


@dataclass
class _Level:
    """A voltage a calculation starts from: a net's (Volt) or a driving pin's high level."""
    volts: float | None
    source: str
    confidence: str | None
    net: str | None = None
    detail: str | None = None
    rail: str | None = None  # a driving pin's I/O rail

    @property
    def driven(self) -> bool:
        return self.source.startswith("I/O rail") or self.source == "pin rail not read"

    def describe(self) -> dict:
        if self.source == "ground":
            return {"net": self.net, "volts": 0.0, "source": "ground"}
        return _clean({"net": self.net, "volts": _r(self.volts) if self.volts is not None
                       else None, "source": self.source, "confidence": self.confidence,
                       "rail": self.rail,
                       "basis": None if self.source == "name" else self.detail})


_ADC_TOKEN = re.compile(r"ADC\d*[A-Z]?|ADC\d+IN\d+|AIN\d*|AN\d+|ADCIN\w*|VAUX[PN]?\d*")
_DAC_TOKEN = re.compile(r"DAC\d*\w*|AOUT\d*|OPAMP\d*|COMP\d*|VREF\w*|DACOUT\w*")
_ADC_PART = re.compile(
    r"\bADC\b|analog[- ]to[- ]digital|\bA/D\b|delta[- ]sigma|sigma[- ]delta|"
    r"(?<![A-Z0-9])(?:ADS1\d{2,3}|ADS7\d{3}|ADS8\d{3}|ADS9\d{3}|MCP3\d{3}|AD7\d{3}|"
    r"AD4\d{3}|LTC2[34]\d{2}|MAX11\d{3}|ADC\d{3}[A-Z]|ADC1[0-2]\d{2}|ADC08\d{2})", re.I)
_ADC_PART_PIN = re.compile(r"[+-]?A?IN\d*[+-]?|AIN\d*[PN]?|AINCOM|AIN_?COM|IN[PN]\d*|IN\d+[PN]|"
                           r"CH\d+[+-]?|ADC\d*|AN\d+|V?IN\d*[+-]|COM")
LED_DRIVER_TEXT = re.compile(r"LED driver|constant[- ]current|current sink", re.I)
_OPTO_FAMILY = re.compile(
    r"(?<![A-Z0-9])(?:PC8\d{2}|PC3\d{2}|PC4\d{2}|K8\d{2}P|TLP\d{2,4}|4N2[5-8]|4N3[5-7]|"
    r"H11[A-L]\d|LTV-?\d{3,4}|EL\d{3,4}|CNY1[7-9]|SFH6\d{2,3}|HCPL-?\d{4}|ACPL-?\w{3,5}|"
    r"FOD\d{3,4}|VO\d{3,4}|PS2\d{3}|PS8\d{3}|MOC\d{4}|TCMT\d{4}|TCET\d{4}|6N13[5-9]|"
    r"ILD\d{3}|IL\d{3})", re.I)
_TRIAC_TEXT = re.compile(r"triac|zero[- ]cross|(?<![A-Z0-9])MOC30\d\d", re.I)
_LOGIC_OPTO_TEXT = re.compile(r"logic (?:gate )?output|high[- ]speed|6N13[5-9]|HCPL-?2\d{3}|"
                              r"\bdigital\b", re.I)
_CONTROL_PIN = re.compile(r"G\d?|GATE\d?|B\d?|BASE\d?")
_HIGH_PIN = re.compile(r"D\d?|DRAIN\d?|C\d?|COL\w*|COLLECTOR\d?")
_LOW_PIN = re.compile(r"S\d?|SOURCE\d?|E\d?|EMIT\w*|EMITTER\d?")
_ANODE_PIN = re.compile(r"A\d*|AN\d*|ANODE\d*|\+|LED\+|IF\+|A_?[RGBWY]|[RGBWY]_?A|COM_?A|CA")
_CATHODE_PIN = re.compile(r"K\d*|C\d*|KA\d*|CATH\w*|-|LED-|IF-|K_?[RGBWY]|[RGBWY]_?K|COM_?K|"
                          r"CC")
_OPTO_OUT_PIN = re.compile(r"C\d*|COL\w*|COLLECTOR\d*|VO\d*|VOUT\d*|OUT\d*|O\d*")
_OPTO_EMITTER_PIN = re.compile(r"E\d*|EMIT\w*|EMITTER\d*")
_CHANNEL = re.compile(r"(\d+)$")
# Standard pinouts of unnamed phototransistor optocouplers: pin -> (role, channel).
_OPTO_PINOUTS = {
    4: {"1": ("anode", "1"), "2": ("cathode", "1"), "3": ("emitter", "1"),
        "4": ("collector", "1")},
    6: {"1": ("anode", "1"), "2": ("cathode", "1"), "4": ("emitter", "1"),
        "5": ("collector", "1")},
    8: {"1": ("anode", "1"), "2": ("cathode", "1"), "3": ("anode", "2"),
        "4": ("cathode", "2"), "5": ("emitter", "2"), "6": ("collector", "2"),
        "7": ("emitter", "1"), "8": ("collector", "1")},
}


def _transistor_role(name: str | None) -> str | None:
    first = first_function(name or "")
    if _CONTROL_PIN.fullmatch(first):
        return "control"
    if _HIGH_PIN.fullmatch(first):
        return "high"
    if _LOW_PIN.fullmatch(first):
        return "low"
    return None


def _channel(name: str | None) -> str:
    match = _CHANNEL.search(first_function(name or ""))
    return match[1] if match else ""


class Board:
    """The design read once: the power tree, the fitted resistors and capacitors (read by
    check_derating), net voltages, IC supplies (check_logic_levels) and processors
    (get_firmware_pin_map)."""

    def __init__(self, netlist: dict, plan, project_params: dict | None,
                 rails: dict[str, float] | None, vref, policy: dict):
        self.netlist, self.plan = netlist, plan
        self.tree = tree = build_tree(netlist, plan, project_params)
        self.design = tree.design
        self.grounds = tree.grounds
        self.data = tree_data(tree, vref)
        self.parts, self.leds, self.stats = read_parts(tree, netlist, plan)
        self.voltages, self.unknown_rails = net_voltages(tree, self.data, rails)
        self.policy = policy
        self.res_on: dict[str, list[_Res]] = defaultdict(list)
        self.caps_on: dict[str, list[tuple[Part, str]]] = defaultdict(list)
        for part in self.parts:
            for net_a, net_b in part.sections:
                if net_a == net_b:
                    continue
                if part.kind == "resistor":
                    res = _Res(part.ref, net_a, net_b, part.ohms, part)
                    self.res_on[net_a].append(res)
                    self.res_on[net_b].append(res)
                else:
                    self.caps_on[net_a].append((part, net_b))
                    self.caps_on[net_b].append((part, net_a))
        self.chips = build_chips(tree, self.voltages)
        self.fmap = PinMap(netlist, plan, project_params, ctx=tree.ctx)
        self.processors = {candidate.designator: candidate
                           for candidate in rank_candidates(self.fmap)
                           if candidate.fitted and candidate.kind != "io_expander"}
        self._rows: dict[tuple[str, str], object] = {}
        self.source_nets = {source.net for source in tree.sources}
        self.conv_pins: dict[tuple[str, str], str] = {}
        for conv in tree.converters.values():
            for kind, items in conv.pins.items():
                for pin, _, _ in items:
                    self.conv_pins.setdefault((conv.ref, pin), kind)
            for net in [*conv.inputs, *(out.net for out in conv.outputs)]:
                self.source_nets.add(net)
        self.feedback = {(conv["designator"], row.get("net")): row
                         for conv in self.data.get("converters", ())
                         for row in conv.get("feedback", ()) if row.get("net")}
        self.needed: dict[str, set[str]] = defaultdict(set)  # unknown net -> calculations

    # ----- small reads -----

    def fixed(self, net: str) -> bool:
        return (net in self.grounds or net in self.voltages or net in self.tree.nodes()
                or net in self.design.rails)

    def name(self, ref: str, pin: str) -> str:
        return self.design.pin_name(ref, pin) or ""

    def label(self, ref: str, pin: str) -> str:
        first = first_function(self.name(ref, pin))
        return f"{ref}.{pin}" + (f" ({first})" if first and first != pin else "")

    def members(self, net: str, skip=()) -> list[tuple[str, str]]:
        return [(ref, pin) for ref, pin in self.tree.members(net)
                if ref not in skip and self.tree.fitted(ref)]

    def caps_to_ground(self, net: str) -> list[tuple[Part, float | None]]:
        out = []
        for part, other in self.caps_on.get(net, ()):
            if other in self.grounds:
                farads = part.fields.get("farads")
                out.append((part, farads.value if farads else None))
        return out

    def level(self, net: str) -> _Level | None:
        volt = self.voltages.get(net)
        if volt is None:
            return None
        return _Level(volt.volts, volt.source, volt.confidence, net, volt.basis)

    def pin_level(self, ref: str, pin: str) -> _Level:
        """A driving pin's high level: its I/O rail's voltage (check_logic_levels)."""
        chip = self.chips.get(ref)
        if chip is None:
            return _Level(None, "pin rail not read", None, None,
                          f"{self.label(ref, pin)}: no supply pin read")
        rail = pin_rail(chip, pin, self.voltages)
        if rail.net is None:
            return _Level(None, "pin rail not read", None, None, rail.detail)
        volt = rail.volt or self.voltages.get(rail.net)
        confidence = rail.confidence
        if volt is not None and volt.confidence == "low" or confidence == "low":
            confidence = "low"
        elif volt is not None and "medium" in (volt.confidence, rail.confidence):
            confidence = "medium"
        return _Level(volt.volts if volt else None,
                      f"I/O rail of {self.label(ref, pin)}", confidence if volt else None,
                      None, rail.detail, rail.net)

    def processor_row(self, ref: str, pin: str):
        key = (ref, pin)
        if key not in self._rows:
            rows = pin_rows(self.fmap, self.processors[ref], {pin})
            self._rows[key] = rows[0] if rows else None
        return self._rows[key]

    def driven(self, net: str) -> list[tuple[str, str]]:
        """Pins that drive a net: push-pull outputs (pull_resistors' reading) and processor
        GPIOs that are not inputs (typed Input, or read as an ADC input)."""
        out = []
        drivers = {text.split(" ")[0] for text in push_pull_drivers(self.design, net)}
        for ref, pin in self.members(net):
            if f"{ref}.{pin}" in drivers:
                out.append((ref, pin))
            elif ref in self.processors and pin_role(self.name(ref, pin)) not in (
                    "supply", "ground", "ref", "cap") and \
                    self.design.pin_type(ref, pin) != "Input" and \
                    not self.adc_pin(ref, pin, net, context=True):
                out.append((ref, pin))
        return out

    # ----- what a pin is -----

    def adc_pin(self, ref: str, pin: str, net: str | None, context: bool) -> str | None:
        """Why a pin is an ADC input, or None: a processor pin read as analog (not a DAC
        or comparator), or one that lists an ADC function with a divider or filter on it
        (context), or an ADC part's analog input."""
        name = self.name(ref, pin)
        tokens = [token for alt in alternatives(name) for token in re.split(r"[_\s.]+", alt)
                  if token]
        adc_alt = any(_ADC_TOKEN.fullmatch(token) for token in tokens)
        if ref in self.processors:
            row = self.processor_row(ref, pin)
            if row is None:
                return None
            if row.function == "analog":
                net_tokens = [token for token in re.split(r"[_\s.\-]+", (net or "").upper())
                              if token]
                if adc_alt or any(_ADC_TOKEN.fullmatch(token) for token in net_tokens):
                    return "processor analog pin (ADC)"
                if any(_DAC_TOKEN.fullmatch(token) for token in [*tokens, *net_tokens]):
                    return None
                return "processor analog pin (by " + "; ".join(row.basis[:1]) + ")"
            if adc_alt and context and row.function in ("gpio", "unknown"):
                return "ADC-capable processor pin with a divider or filter on it"
            return None
        text = self.tree.text(ref)
        if _ADC_PART.search(text) and not COMPARATOR_TEXT.search(text):
            first = first_function(name)
            if pin_role(name) not in ("supply", "ground", "ref", "cap") and \
                    _ADC_PART_PIN.fullmatch(first):
                return "ADC part input"
        return None

    def pin_use(self, ref: str, pin: str, net: str | None, context: bool = True
                ) -> tuple[str, str] | None:
        """(use, detail) of a part pin a divider or filter reaches, or None for passives."""
        tree = self.tree
        kind = tree.kind(ref)
        name = self.name(ref, pin)
        first = first_function(name)
        if kind == "connector":
            return "connector", "leaves the board"
        if kind == "transistor":
            role = _transistor_role(name)
            if role == "control":
                return "transistor", "base" if first[:1] == "B" else "gate"
            if role is None:
                return "transistor", "pin role not named"
            return "transistor_channel", role
        if kind not in ("ic", "other"):
            return None
        role = pin_role(name)
        if role in ("supply", "ground"):
            return "supply_pin", role
        conv_kind = self.conv_pins.get((ref, pin))
        if ref in tree.converters:
            if conv_kind in ("fb", "en"):
                return conv_kind, tree.converters[ref].kind
            return "converter_pin", conv_kind or "pin"
        if ref in tree.supervisors and not _open_drain(self, ref, pin) and                 self.design.pin_type(ref, pin) != "Output" and role != "out":
            return "supervisor", "monitored input"
        if ref in tree.references:
            return "reference", "reference IC"
        if role == "ref":
            return "reference_input", first or pin
        if (why := self.adc_pin(ref, pin, net, context)) is not None:
            return "adc", why
        if ref in self.processors:
            row = self.processor_row(ref, pin)
            function = row.function if row is not None else "unknown"
            return "processor_pin", function
        text = tree.text(ref)
        electrical = self.design.pin_type(ref, pin)
        output = electrical in ("Output", "OpenCollector", "OpenEmitter") or role == "out"
        if COMPARATOR_TEXT.search(text) and not output:
            return "comparator", first or pin
        if AMPLIFIER_TEXT.search(text) and not output:
            return "opamp", first or pin
        if output:
            return "ic_output", first or pin
        return "ic_input", first or pin


# ---------- dividers ----------

_FEED_RANK = {use: rank for rank, use in enumerate((
    "fb", "en", "supervisor", "reference", "adc", "comparator", "opamp", "reference_input",
    "converter_pin", "transistor", "processor_pin", "ic_input", "connector"))}
_RANGE_USES = frozenset({"en", "supervisor", "adc", "comparator", "opamp", "reference_input",
                         "processor_pin", "ic_input"})


# Pins a series leg passes without loading the node: high-impedance inputs.
_TAP_USES = frozenset({"fb", "en", "supervisor", "reference", "reference_input", "adc",
                       "comparator", "opamp", "ic_input", "processor_pin"})


def _chain_node(board: Board, net: str, came: _Res) -> tuple[_Res, str, list[str]] | None:
    """(the next resistor, what the node holds, its pins) when a net links two resistors
    in series: 'plain' (only capacitors besides), 'passive' (a test point, jumper or
    connector pin), 'input' (high-impedance IC or processor inputs: a tap of a resistor
    stack) or 'switch' (an open-drain output on the stack). None when anything else (a
    driving pin, a transistor, a diode, a supply pin) loads it."""
    if board.fixed(net):
        return None
    resistors = board.res_on.get(net, ())
    if len(resistors) != 2:
        return None
    nxt = next((res for res in resistors if res is not came), None)
    if nxt is None:
        return None
    what, pins = "plain", []
    for ref, pin in board.members(net, {res.ref for res in resistors}):
        kind = board.tree.kind(ref)
        if kind in ("capacitor", "esd"):
            continue
        if kind in ("passive", "connector"):
            what = "passive" if what == "plain" else what
        elif kind not in ("ic", "other"):
            return None
        elif _open_drain(board, ref, pin):
            what = "switch"
        else:
            found = board.pin_use(ref, pin, net, context=False)
            if found is None or found[0] not in _TAP_USES:
                return None
            what = "input" if what in ("plain", "passive") else what
        pins.append(board.label(ref, pin))
    if what != "plain" and board.driven(net):
        return None
    return nxt, what, pins


def _is_end(board: Board, net: str) -> bool:
    return net in board.grounds or board.fixed(net) or bool(board.driven(net))


def _legs(board: Board, mid: str) -> list[_Leg]:
    """Each resistor's leg from the midpoint: on through series nodes (and the taps of a
    resistor stack) to the net it ends on. A leg that passes a tap but ends nowhere fixed
    stops at its first tap."""
    legs = []
    for res in board.res_on.get(mid, ()):
        chain, current, seen, taps = [res], res.far(mid), {mid}, []
        while current not in seen and len(chain) < CHAIN_LIMIT:
            found = _chain_node(board, current, chain[-1])
            if found is None:
                break
            nxt, what, pins = found
            seen.add(current)
            if what != "plain":
                taps.append(_Tap(current, len(chain), what, pins))
            chain.append(nxt)
            current = nxt.far(current)
        if taps and not _is_end(board, current):
            first = taps[0]
            chain, current, taps = chain[:first.index], first.net, []
        legs.append(_Leg(current, chain, taps))
    return legs


_SELECTOR_PREFIXES = frozenset({"JP", "SJ", "W", "JMP"})


def _selector(board: Board, leg: _Leg, ends: set[str], leg_ends: set[str]
              ) -> tuple[str, str, str] | None:
    """(part, its pin, the divider end it joins) when a jumper, header or switch on a leg's
    far end joins it to one of the divider's ends (or ground) and to nothing but this
    divider's legs: a selectable leg, not a consumer."""
    for ref, pin in board.members(leg.end, set(leg.refs)):
        if not (board.tree.kind(ref) in ("connector", "switch", "relay")
                or designator_prefix(ref) in _SELECTOR_PREFIXES):
            continue
        nets = set((board.design.part_pins.get(ref) or {}).values())
        joined = sorted((net for net in nets if net in ends or net in board.grounds),
                        key=natural_key)
        if joined and all(net in ends or net in board.grounds or net in leg_ends
                          for net in nets):
            return ref, board.label(ref, pin), joined[0]
    return None


def _candidate_mid(board: Board, net: str) -> str | None:
    """Why a net cannot be a divider's midpoint, or None when it can."""
    if net in board.grounds:
        return "ground"
    if net in board.source_nets:
        return "rail"
    volt = board.voltages.get(net)
    if volt is not None and volt.source != "name":
        return "rail"
    resistors = board.res_on.get(net, ())
    if len(resistors) < 2:
        return "one resistor"
    others = board.members(net, {res.ref for res in resistors})
    if any(res.ohms is not None and res.ohms <= LINK_OHMS and board.fixed(res.far(net))
           for res in resistors):
        return "rail"
    if len(resistors) == 2 and all(board.tree.kind(ref) == "capacitor" for ref, _ in others):
        return "chain"  # a node inside a series chain, read from the chain's ends
    for ref, pin in others:
        if board.tree.kind(ref) in ("ic", "other") and pin_role(board.name(ref, pin)) == \
                "supply":
            return "supply_pin"
    return None


def _output_legs(board: Board, others: list[_Leg]) -> list[_Leg]:
    """Legs that carry the midpoint on to a consumer: one series resistor to a net with no
    other resistor (a filter or isolation resistor before an input)."""
    return [leg for leg in others if len(leg.resistors) == 1 and not board.fixed(leg.end)
            and len(board.res_on.get(leg.end, ())) == 1]


def _feeds(board: Board, mid: str, legs: list[_Leg], outputs: list[_Leg]) -> list[dict]:
    used = {ref for leg in legs for ref in leg.refs}
    out = []
    reach = [(mid, []), *((leg.end, leg.refs) for leg in outputs)]
    for net, through in reach:
        for ref, pin in board.members(net, used):
            found = board.pin_use(ref, pin, net)
            if found is None or found[0] in ("supply_pin", "transistor_channel"):
                continue
            use, detail = found
            name = first_function(board.name(ref, pin)) or None
            entry = {"use": use, "part": ref, "pin": pin, "name": name,
                     "detail": None if detail in (name, pin) else detail,
                     "net": net if through else None, "through": through or None}
            out.append(_clean(entry))
    out.sort(key=lambda item: (_FEED_RANK.get(item["use"], 99), natural_key(item["part"])))
    return out


def _end_level(board: Board, leg: _Leg) -> _Level | None:
    if leg.end in board.grounds:
        return _Level(0.0, "ground", "high", leg.end)
    found = board.level(leg.end)
    if found is not None:
        return found
    if board.fixed(leg.end):
        return _Level(None, "unknown", None, leg.end)  # a rail: its own voltage (rails=)
    for ref, pin in board.driven(leg.end):
        level = board.pin_level(ref, pin)
        level.net = leg.end
        return level
    return _Level(None, "unknown", None, leg.end)


def _leg_input(board: Board, legs: list[_Leg]) -> dict:
    ohms = parallel([leg.ohms for leg in legs])
    tolerances = [resistor_tolerance(res.part) for leg in legs for res in leg.resistors]
    entry = {"value": ohms_text(ohms), "ohms": _r(ohms, 6),
             "parts": [ref for leg in legs for ref in leg.refs],
             "to": legs[0].end,
             "arrangement": ("series chain" if any(len(leg.resistors) > 1 for leg in legs)
                             else None),
             "parallel": len(legs) if len(legs) > 1 else None}
    if tolerances and None not in tolerances:
        values = {value for value, _ in tolerances}
        entry["tolerance"] = (f"{_r(tolerances[0][0] * 100, 3):g}%" if len(values) == 1 else
                              [f"{_r(value * 100, 3):g}%" for value, _ in tolerances])
        entry["tolerance_source"] = tolerances[0][1]
    return _clean(entry)


def _bounds(legs: list[_Leg], sign: int) -> float | None:
    """The legs' resistance with every tolerance at +1 or -1 (None when one is unknown)."""
    totals = []
    for leg in legs:
        total = 0.0
        for res in leg.resistors:
            found = resistor_tolerance(res.part)
            if found is None or res.ohms is None:
                return None
            total += res.ohms * (1 + sign * found[0])
        totals.append(total)
    return parallel(totals)


def _near(implied: float, choices) -> float | None:
    near = [common for common in choices
            if abs(implied - common) <= common * COMMON_VREF_TOLERANCE]
    return min(near, key=lambda common: abs(implied - common)) if near else None


_TAP_HOLDS = {"input": "IC inputs: another tap of the stack",
              "switch": "an open-drain output that switches the stack (hysteresis or a "
                        "selectable threshold)",
              "passive": "a test point, jumper or connector pin"}


def _tap_entries(top: list[_Leg], bottom: list[_Leg], rtop: float | None,
                 notes: list[str]) -> list[dict]:
    """The taps the legs pass, with the ratio while an open-drain tap on the bottom leg
    sinks (the rest of the bottom leg shorted), and their notes."""
    out = []
    for side, side_legs in (("top", top), ("bottom", bottom)):
        for leg in side_legs:
            for tap in leg.taps:
                entry: dict = {"net": tap.net, "leg": side, "after": leg.refs[:tap.index],
                               "holds": _TAP_HOLDS[tap.what], "pins": tap.pins[:4]}
                above = [res.ohms for res in leg.resistors[:tap.index]]
                if tap.what == "switch" and side == "bottom" and len(bottom) == 1 and \
                        rtop and None not in above:
                    entry["ratio_when_sinking"] = _r(sum(above) / (rtop + sum(above)), 5)
                out.append(entry)
                pins = ", ".join(tap.pins[:3])
                if tap.what == "input":
                    notes.append(f"a resistor stack: {tap.net} ({pins}) on the {side} leg is "
                                 "another tap")
                elif tap.what == "switch":
                    notes.append(f"an open-drain output ({pins}) at {tap.net} on the {side} "
                                 "leg switches the stack: the ratio is with it off" + (
                                     "; ratio_when_sinking with it on"
                                     if "ratio_when_sinking" in entry else ""))
                else:
                    notes.append(f"the {side} leg passes {tap.net} ({pins}): something "
                                 "connected there changes the ratio")
    return out


def _divider(board: Board, mid: str, vth, vref) -> tuple[dict | None, str]:
    """The divider row at a midpoint, or (None, why not)."""
    why = _candidate_mid(board, mid)
    if why:
        return None, why
    legs = [leg for leg in _legs(board, mid) if leg.end != mid]
    ends: dict[str, list[_Leg]] = defaultdict(list)
    others: list[_Leg] = []
    for leg in legs:
        if leg.end in board.grounds or board.fixed(leg.end) or board.driven(leg.end):
            ends[leg.end].append(leg)
        else:
            others.append(leg)
    if len(ends) < 2:
        return None, "one end"
    drivers = push_pull_drivers(board.design, mid)
    if drivers:
        return None, "driven"
    for ref, pin in board.members(mid):
        if ref in board.processors:
            row = board.processor_row(ref, pin)
            if row is not None and row.function in SIGNAL_BUS_FUNCTIONS:
                return None, "bus"
    levels = {net: _end_level(board, group[0]) for net, group in ends.items()}
    grounds = [net for net in ends if net in board.grounds]
    if grounds:
        bottom_net = grounds[0]
    else:
        # No ground leg: the bottom is the lowest rail of known voltage, below the others
        # (a driven signal is never the bottom: a series resistor and a pull-up are not one).
        rails = [net for net in ends if levels[net].volts is not None
                 and levels[net].source in ("user", "name", "power_tree")]
        if len(rails) < 2 or len(rails) != len(ends):
            return None, "no ground"
        bottom_net = min(rails, key=lambda net: (levels[net].volts, natural_key(net)))
        if not any(levels[net].volts > levels[bottom_net].volts + 0.05
                   for net in ends if net != bottom_net):
            return None, "no ground"
    bottom = [leg for net in ends if net == bottom_net or net in grounds
              for leg in ends[net]]
    top_nets = [net for net in ends if net != bottom_net and net not in grounds]
    if not top_nets:
        return None, "one end"
    # A jumper, header or switch joining a leg to one of the ends: a selectable leg.
    leg_ends = {leg.end for leg in others}
    selectable = []
    for leg in others:
        found = _selector(board, leg, set(ends), leg_ends)
        if found:
            selectable.append((leg, *found))
    chosen = {id(leg) for leg, _, _, _ in selectable}
    others = [leg for leg in others if id(leg) not in chosen]
    outputs = _output_legs(board, others)
    feeds = _feeds(board, mid, legs, outputs)
    fb_conv = next((feed["part"] for feed in feeds if feed["use"] == "fb"), None)

    def top_rank(net: str):
        level = levels[net]
        in_output = fb_conv is not None and any(
            net == out.net for out in board.tree.converters[fb_conv].outputs)
        return (not in_output, net not in board.voltages and not board.fixed(net),
                -(level.volts if level.volts is not None else -1e9), natural_key(net))

    top_net = min(top_nets, key=top_rank)
    top = ends[top_net]
    extra = [leg for net in top_nets if net != top_net for leg in ends[net]] + [
        leg for leg in others if leg not in outputs]
    v_top, v_bot = levels[top_net], levels[bottom_net]
    rtop = parallel([leg.ohms for leg in top])
    rbot = parallel([leg.ohms for leg in bottom])
    row: dict = {"kind": "divider", "midpoint": mid,
                 "_check": {"low": None, "high": None, "top": v_top}}
    inputs = {"R_top": _leg_input(board, top), "R_bottom": _leg_input(board, bottom),
              "V_top": v_top.describe(), "V_bottom": v_bot.describe()}
    row["feeds"] = feeds
    row["inputs"] = inputs
    row["formula"] = ("ratio = R_bottom / (R_top + R_bottom); V_mid = V_bottom + (V_top - "
                      "V_bottom) x ratio (unloaded); Z = R_top || R_bottom")
    notes: list[str] = []
    confidence = "high"
    result: dict = {}
    every = top + bottom + extra + outputs + [leg for leg, _, _, _ in selectable]
    selectors = {ref for _, ref, _, _ in selectable}
    if rtop is None or rbot is None or rtop <= 0 or rbot <= 0:
        notes.append("a resistor value could not be read" if rtop is None or rbot is None
                     else "a 0 ohm leg: no division")
        row["result"] = result
        row["confidence"] = "low"
        row["notes"] = notes
        _refs_nets(row, every, mid, feeds, selectors)
        return row, ""
    ratio = rbot / (rtop + rbot)
    if v_top.driven and len(top) == 1 and len(top[0].resistors) == 1 and \
            rtop <= SERIES_PULL_OHMS and ratio >= SERIES_PULL_RATIO:
        # A small series resistor from a driving pin and a pull-down: a termination, or a
        # transistor's gate or base drive, not a level divider.
        if not any(feed["use"] == "transistor" for feed in feeds):
            return None, "series"
        row["pattern"] = "a series resistor and a pull-down: gate or base drive"
    result["ratio"] = _r(ratio, 5)
    top_lo, top_hi = _bounds(top, -1), _bounds(top, +1)
    bot_lo, bot_hi = _bounds(bottom, -1), _bounds(bottom, +1)
    ranged = None not in (top_lo, top_hi, bot_lo, bot_hi)
    if ranged:
        low = bot_lo / (top_hi + bot_lo)
        high = bot_hi / (top_lo + bot_hi)
        result["ratio_range"] = [_r(low, 5), _r(high, 5)]
    result["source_impedance"] = ohms_text(rtop * rbot / (rtop + rbot))
    v_mid = None
    if v_top.volts is not None and v_bot.volts is not None:
        span = v_top.volts - v_bot.volts
        v_mid = v_bot.volts + span * ratio
        result["v_mid"] = _r(v_mid)
        bounds = (v_mid, v_mid)
        if ranged:
            bounds = (v_bot.volts + span * low, v_bot.volts + span * high)
            result["v_mid_range"] = [_r(bounds[0]), _r(bounds[1])]
        row["_check"]["low"], row["_check"]["high"] = min(bounds), max(bounds)
        result["current_ua"] = _r(span / (rtop + rbot) * 1e6, 4)
        if v_top.confidence == "low":
            confidence = "low"
            notes.append("the top voltage is a low-confidence reading")
        elif v_top.confidence == "medium":
            confidence = "medium"
    elif v_top.driven:
        confidence = "medium"
        if v_top.rail:
            board.needed[v_top.rail].add(mid)
    else:
        confidence = "medium"
        unknown = top_net if v_top.volts is None else bottom_net
        board.needed[unknown].add(mid)
        notes.append(f"{unknown}'s voltage is not known: pass rails='{unknown}=volts' for "
                     "V_mid")
    if v_top.driven:
        notes.append(f"the top is a driven signal ({v_top.source}): V_mid holds while it "
                     "drives high" + (f"; pass rails='{v_top.rail}=volts' for its level"
                                      if v_top.volts is None and v_top.rail else ""))
        confidence = "medium" if confidence == "high" else confidence
    named = board.voltages.get(mid)
    if named is not None and v_mid is not None:
        result["midpoint_named_volts"] = _r(named.volts)
        if abs(named.volts - v_mid) > max(0.05, abs(named.volts) * 0.05):
            notes.append(f"the midpoint's name says {_vtext(named.volts)} V, the divider "
                         f"gives {_vtext(v_mid)} V")
    if any(len(leg.resistors) > 1 for leg in top + bottom):
        confidence = "medium" if confidence == "high" else confidence
    taps = _tap_entries(top, bottom, rtop, notes)
    if taps:
        row["taps"] = taps
        if any(tap.what in ("passive", "switch") for leg in top + bottom for tap in leg.taps):
            confidence = "medium" if confidence == "high" else confidence
    if selectable:
        listed = []
        for leg, _, label, to in selectable:
            entry: dict = {"parts": leg.refs, "value": ohms_text(leg.ohms), "through": label,
                           "to": to}
            alt = None
            if leg.ohms:
                if to == top_net:
                    alt = (parallel([rtop, leg.ohms]), rbot)
                elif to == bottom_net or to in board.grounds:
                    alt = (rtop, parallel([rbot, leg.ohms]))
            if alt and alt[0] and alt[1]:
                entry["ratio"] = _r(alt[1] / (alt[0] + alt[1]), 5)
            else:
                alt = None
            listed.append((entry, alt))
        row["selectable_legs"] = [entry for entry, _ in listed]
        row["_selectable"] = listed
        notes.append("a jumper or switch joins more resistors to the divider: the ratio is "
                     "with none fitted; selectable_legs gives the ratio with each alone")
    if extra:
        row["also_on_midpoint"] = [_clean({"parts": leg.refs, "to": leg.end,
                                           "value": ohms_text(leg.ohms)}) for leg in extra[:4]]
        notes.append("other resistors also sit on the midpoint (a feed-forward, offset or "
                     "selectable leg): the formula covers the two main legs only")
        confidence = "medium" if confidence == "high" else confidence
    if not feeds:
        notes.append("nothing found on the midpoint besides the divider")
        confidence = "low" if confidence != "high" else "medium"
    if row.get("pattern"):
        notes.append("a small series resistor from a driving pin and a pull-down: V_mid is "
                     "the drive level the transistor sees, not a level divider")
    row["result"] = result
    _consumers(board, row, feeds, rtop, rbot, v_top, top_net, mid, vth, vref, notes)
    row["confidence"] = confidence
    if notes:
        row["notes"] = notes
    _refs_nets(row, every, mid, feeds, selectors)
    row["_taps"] = [tap for leg in top + bottom for tap in leg.taps]
    row["_ends_past_taps"] = {leg.end for leg in top + bottom
                              if any(tap.what == "passive" for tap in leg.taps)}
    return row, ""


def _refs_nets(row: dict, legs: list[_Leg], mid: str, feeds: list[dict],
               more: set[str] = frozenset()) -> None:
    row["_refs"] = {ref for leg in legs for ref in leg.refs} | {
        feed["part"] for feed in feeds} | set(more)
    row["_nets"] = {mid} | {leg.end for leg in legs} | {
        res.a for leg in legs for res in leg.resistors} | {
        res.b for leg in legs for res in leg.resistors}


_FB_KEYS = ("formula", "vout_over_vref", "named_rail", "vout_from_name", "implied_vref",
            "near_common_vref", "vref_given", "vout_computed", "vout_error")


def _fb_entry(board: Board, row: dict, ref: str, feed: dict, rtop: float, rbot: float,
              v_top: _Level, top_net: str, mid: str, vref) -> dict:
    """A converter's feedback: get_power_tree's row when it reads the same divider, else
    the same formula here; the Vref the top's voltage implies (or the output a given Vref
    sets) when that row has neither (a top voltage from rails= or a joined rail)."""
    conv = board.tree.converters[ref]
    floating = floating_reference(board.tree, conv)
    entry: dict = {"converter": ref, "type": conv.kind}
    found = board.feedback.get((ref, mid)) or board.feedback.get((ref, feed.get("net")))
    mine = set(row["inputs"]["R_top"]["parts"]) | set(row["inputs"]["R_bottom"]["parts"])
    theirs = set((found or {}).get("top", ())) | set((found or {}).get("bottom", ()))
    gain = fb_gain(rtop, rbot, floating)
    if found and found.get("state") == "divider" and theirs == mine:
        entry.update({key: found[key] for key in _FB_KEYS if key in found})
        if "selectable_leg" in found and not row.get("selectable_legs"):
            entry["selectable_leg"] = found["selectable_leg"]
        entry["from"] = "get_power_tree's feedback row"
    else:
        entry["formula"] = fb_formula(floating)
        entry["vout_over_vref"] = round(gain, 4)
        if found and found.get("state") == "divider":
            entry["note"] = ("get_power_tree reads another divider on this pin ("
                             + ", ".join(sorted(theirs, key=natural_key)) + ")")
    if "implied_vref" not in entry and "vout_computed" not in entry:
        given = _for_part(vref, ref)
        top_volts = v_top.volts if v_top.volts is not None and v_top.volts > 0 else None
        vout_or_vref(entry, gain, top_volts, given)
        if "implied_vref" in entry or "vout_error" in entry:
            entry["top_volts"] = _r(top_volts)
            entry["top_source"] = f"{top_net}: {v_top.source}"
    for leg_entry, alt in row.get("_selectable", ()):
        if alt is None:
            continue
        alt_gain = fb_gain(alt[0], alt[1], floating)
        leg_entry["vout_over_vref"] = round(alt_gain, 4)
        if v_top.volts is not None and v_top.volts > 0:
            vout_or_vref(leg_entry, alt_gain, v_top.volts, None)
    return _clean(entry)


def _consumers(board: Board, row: dict, feeds: list[dict], rtop: float, rbot: float,
               v_top: _Level, top_net: str, mid: str, vth, vref, notes: list[str]) -> None:
    """What each recognised consumer makes of the divider: an FB's Vref, an EN's trip
    voltage, a reference's set point, an op-amp's gain."""
    ratio = rbot / (rtop + rbot)
    sinking = next((tap["ratio_when_sinking"] for tap in row.get("taps", ())
                    if "ratio_when_sinking" in tap), None)
    done = set()
    for feed in feeds:
        use, ref = feed["use"], feed["part"]
        if (use, ref) in done:
            continue
        done.add((use, ref))
        if use == "fb":
            row["fb"] = _fb_entry(board, row, ref, feed, rtop, rbot, v_top, top_net, mid, vref)
        elif use in ("en", "supervisor", "comparator"):
            threshold = _for_part(vth, ref)
            entry = {"part": ref, "trip_formula": "V_top at trip = V_th / ratio"}
            if threshold is not None:
                entry["v_th"] = threshold
                entry["v_th_source"] = "vth argument"
                entry["v_top_at_trip"] = _r(threshold / ratio)
                if sinking:
                    entry["v_top_at_trip_when_sinking"] = _r(threshold / sinking)
            elif use == "en":
                entry["v_top_at_trip_examples"] = {
                    f"{example:g} V": _r(example / ratio) for example in EXAMPLE_THRESHOLDS}
                entry["note"] = ("the EN/UVLO threshold and its hysteresis are datasheet "
                                 "values: these thresholds are examples only (pass vth=)")
            else:
                entry["note"] = "the input threshold is a datasheet value: pass vth="
            row.setdefault("thresholds", []).append(_clean(entry))
        elif use == "reference":
            entry = {"part": ref, "formula": "V_top = V_ref / ratio"}
            given = _for_part(vref, ref, everyone=False)
            if given is not None:
                entry["vref_given"] = given
                entry["v_top_computed"] = _r(given / ratio)
            elif v_top.volts is not None:
                implied = v_top.volts * ratio
                entry["implied_vref"] = _r(implied)
                entry["near_common_vref"] = _near(implied, REFERENCE_VREFS)
            row["reference"] = _clean(entry)
        elif use == "opamp":
            own = any(r == ref for r, _ in board.driven(top_net)) or any(
                r == ref for r, p in board.members(top_net)
                if board.design.pin_type(r, p) == "Output"
                or pin_role(board.name(r, p)) == "out")
            if own:
                row["opamp"] = {"part": ref, "noninverting_gain": _r(1 / ratio),
                                "formula": "gain = 1 + R_top / R_bottom (its own output "
                                           "feeds the top)"}
        elif use == "transistor" and feed.get("detail") == "base":
            notes.append(f"{ref}'s base clamps near 0.6-0.7 V once it conducts: V_mid is "
                         "the unloaded value")


def _range_finding(board: Board, row: dict) -> list[dict]:
    """divider_output_above_input_rating (the midpoint's high bound above a reached pin's
    rail + 0.3 V) and divider_output_below_ground (its low bound below -0.3 V on a pin
    powered from a positive rail)."""
    check = row["_check"]
    v_top, high, low = check["top"], check["high"], check["low"]
    if high is None or v_top.confidence not in ("high", "medium"):
        return []
    out = []
    seen = set()
    for feed in row["feeds"]:
        if feed["use"] not in _RANGE_USES or feed["part"] in seen:
            continue
        ref, pin = feed["part"], feed["pin"]
        if feed["use"] == "en":
            conv = board.tree.converters.get(ref)
            net = conv.inputs[0] if conv and conv.inputs else None
            volt = board.voltages.get(net) if net else None
            if volt is None:
                continue
            rail = _Level(volt.volts, f"{ref}'s input (EN pins are commonly rated to VIN)",
                          "medium" if volt.confidence != "low" else "low", net)
        else:
            rail = board.pin_level(ref, pin)
        if rail.volts is None or rail.volts <= 0:
            continue
        limit = rail.volts + ABS_MAX_MARGIN
        if high > limit + 1e-9:
            check_name, v_mid, bound = "divider_output_above_input_rating", high, limit
            what = (f"above the usual absolute maximum of the rail + {ABS_MAX_MARGIN:g} V "
                    f"({_vtext(limit)} V)")
            reach = "up to"
        elif low < -ABS_MAX_MARGIN - 1e-9:
            check_name, v_mid, bound = "divider_output_below_ground", low, -ABS_MAX_MARGIN
            what = (f"below the usual absolute minimum of ground - {ABS_MAX_MARGIN:g} V "
                    f"({_vtext(-ABS_MAX_MARGIN)} V)")
            reach = "down to"
        else:
            continue
        seen.add(feed["part"])
        severity = "warning" if rail.confidence in ("high", "medium") else "info"
        designators = [*row["inputs"]["R_top"]["parts"], *row["inputs"]["R_bottom"]["parts"],
                       ref]
        out.append(_finding(
            check_name, severity,
            f"The divider at {row['midpoint']} ({'/'.join(designators[:-1])}) puts {reach} "
            f"{_vtext(v_mid)} V (unloaded, from {row['inputs']['V_top'].get('net')} at "
            f"{_vtext(v_top.volts)} V) on {board.label(ref, pin)}, whose rail "
            f"{rail.rail or rail.net} is {_vtext(rail.volts)} V: {what}"
            + ("; that rail is a guess" if severity == "info" else "")
            + ". Check the pin's absolute maximum ratings in its datasheet and the divider "
              "values.",
            designators, net=row["midpoint"], v_mid=_r(v_mid), limit_v=_r(bound),
            pin=board.label(ref, pin), pin_rail=rail.describe()))
    return out


# ---------- LEDs and optocouplers ----------

@dataclass
class _Side:
    kind: str  # rail, ground, gpio, ic_pin, transistor, connector, driver, unknown
    net: str | None
    groups: list[list[_Res]]  # the series steps: one resistor, or resistors in parallel
    level: _Level | None = None
    part: str | None = None
    pin: str | None = None
    detail: str | None = None

    @property
    def resistors(self) -> list[_Res]:
        return [res for group in self.groups for res in group]

    def describe(self, board: Board) -> dict:
        """The side's voltage with what it ends on (via)."""
        out = self.level.describe() if self.level else {"net": self.net}
        if self.kind not in ("rail", "ground"):
            via = board.label(self.part, self.pin) if self.part and self.pin else self.part
            out["via"] = " ".join(filter(None, (self.kind, via, self.detail)))
        if self.level is None:
            out["source"] = "not followed"
        return _clean(out)


def _open_drain(board: Board, ref: str, pin: str) -> bool:
    """A pin that can only sink: typed open-collector, or named as a status flag."""
    return board.design.pin_type(ref, pin) in ("OpenCollector", "OpenEmitter") or \
        open_drain_name(board.name(ref, pin), pin=True)


def _transistor_far(board: Board, ref: str, pin: str) -> tuple[str | None, str | None]:
    """The net on a transistor's other channel pin (emitter or source), same channel."""
    names = board.tree.pin_names(ref)
    channel = _channel(names.get(pin))
    found = [(other, net) for other, net in (board.design.part_pins.get(ref) or {}).items()
             if _transistor_role(names.get(other)) == "low"]
    same = [item for item in found if _channel(names.get(item[0])) == channel] or found
    if len({net for _, net in same}) == 1:
        return same[0][1], same[0][0]
    return None, None


def _walk(board: Board, net: str, own: str, high: bool, skip: set[str]) -> _Side:
    """From an LED's anode (high) or cathode toward its supply or sink: through series
    resistors (or resistors in parallel to one net) to a rail, ground, a driving pin or a
    transistor."""
    resistors: list[list[_Res]] = []
    current = net
    for _ in range(4):
        if current in board.grounds:
            return _Side("ground", current, resistors, _Level(0.0, "ground", "high", current))
        level = board.level(current)
        if level is not None or board.fixed(current):
            return _Side("rail", current, resistors, level or _Level(
                None, "unknown", None, current))
        walked = {res.ref for group in resistors for res in group}
        members = [(ref, pin) for ref, pin in board.members(current, {own, *skip, *walked})
                   if board.tree.kind(ref) not in ("capacitor", "esd", "resistor")]
        series = [res for res in board.res_on.get(current, ())
                  if res.ref not in walked and res.ref not in skip]
        actives = [(ref, pin) for ref, pin in members if board.tree.kind(ref) in ("ic", "other")]
        transistors = [(ref, pin) for ref, pin in members
                       if board.tree.kind(ref) == "transistor"
                       and _transistor_role(board.name(ref, pin)) == "high"]
        diodes = [ref for ref, _ in members if board.tree.kind(ref) in ("diode", "zener")]
        connectors = [(ref, pin) for ref, pin in members
                      if board.tree.kind(ref) == "connector"]
        if diodes and not actives and not transistors:
            return _Side("unknown", current, resistors,
                         detail=f"another diode ({diodes[0]}) in the path: a string or a "
                                "steering diode")
        if len(actives) > 1:
            outputs = [(ref, pin) for ref, pin in actives
                       if board.design.pin_type(ref, pin) in ("Output", "OpenCollector",
                                                              "OpenEmitter")
                       or "LED" in board.name(ref, pin).upper()]
            if len(outputs) == 1:
                actives = outputs  # an LED pin that is also a strap input (CONFIG[6])
        if actives and len(actives) == 1:
            ref, pin = actives[0]
            text = board.tree.text(ref)
            if LED_DRIVER_TEXT.search(text) or ref in board.tree.converters:
                return _Side("driver", current, resistors, None, ref, pin,
                             "an LED driver or converter: it sets the current")
            kind = "gpio" if ref in board.processors else "ic_pin"
            if high and _open_drain(board, ref, pin):
                return _Side("unknown", current, resistors, None, ref, pin,
                             "an open-drain pin, which cannot source the current")
            if high:
                level = board.pin_level(ref, pin)
                return _Side(kind, current, resistors, level, ref, pin, "sources")
            return _Side(kind, current, resistors, _Level(
                0.0, "pin sinking (VOL taken as 0 V)", "medium", current), ref, pin, "sinks")
        if transistors and not actives and len(transistors) == 1:
            ref, pin = transistors[0]
            far, _ = _transistor_far(board, ref, pin)
            if far is None:
                return _Side("transistor", current, resistors, None, ref, pin,
                             "its emitter or source pin was not read")
            if far in board.grounds:
                if high:
                    return _Side("unknown", current, resistors, None, ref, pin,
                                 "a low-side transistor on the anode side")
                return _Side("transistor", current, resistors,
                             _Level(0.0, "low-side switch (saturation taken as 0 V)",
                                    "medium", far), ref, pin, "low-side switch")
            level = board.level(far)
            if high and (level is not None or board.fixed(far)):
                return _Side("transistor", current, resistors, level or _Level(
                    None, "unknown", None, far), ref, pin,
                    f"high-side switch from {far} (saturation taken as 0 V)")
            return _Side("transistor", current, resistors, None, ref, pin,
                         f"its emitter or source is on {far}")
        fars = {res.far(current) for res in series}
        if not actives and not transistors and len(fars) == 1:
            resistors.append(series)  # one resistor, or several in parallel to one net
            current = fars.pop()
            continue
        if connectors and not actives and not series:
            return _Side("connector", current, resistors, None, connectors[0][0],
                         connectors[0][1], "driven from off the board")
        unnamed = [ref for ref, pin in members if board.tree.kind(ref) == "transistor"
                   and _transistor_role(board.name(ref, pin)) is None]
        what = ("several resistors to different nets" if len(fars) > 1 else
                "several drivers" if len(actives) > 1 else
                f"a transistor whose pins are not named ({unnamed[0]})" if unnamed else
                "nothing recognised")
        return _Side("unknown", current, resistors, detail=what)
    return _Side("unknown", current, resistors, detail="a long resistor chain")


@dataclass
class _Diode:
    anode: str
    cathode: str
    colour: str | None = None  # from a pin name (a multi-colour part)
    channel: str = ""
    guessed: bool = False  # polarity read from the nets, not the pin names


def _pin_colour(name: str) -> str | None:
    """The colour a multi-colour LED's pin name gives: R, GRN, K_B, BLUE_A, RK."""
    letters = re.sub(r"^(?:ANODE|CATHODE|CATH|AN|A|K)_?|_?(?:ANODE|CATHODE|A|K)$", "", name)
    return _PIN_COLOUR.get(letters or name)


def _orient(board: Board, ref: str, net_a: str, net_b: str) -> str | None:
    """The anode net of a two-pin LED whose pins are not named A/K, from what each side
    reaches: ground is the cathode's; a rail beats an IC pin or a transistor (a GPIO
    sinking), which beats ground; between rails the higher one. None when it stays open."""
    def rank(net: str) -> float | None:
        side = _walk(board, net, ref, True, set())
        if side.kind == "ground":
            return 0.0
        if side.kind == "transistor" and side.level is not None and side.level.volts == 0:
            return 1.0
        if side.kind in ("gpio", "ic_pin") and side.part and _open_drain(board, side.part,
                                                                          side.pin):
            return 0.5  # it can only sink: the cathode's side
        if side.kind in ("gpio", "ic_pin", "transistor", "driver"):
            return 2.0
        if side.kind == "rail":
            volts = side.level.volts if side.level else None
            return 3.0 + (volts if volts is not None else 0.0)
        return None

    rank_a, rank_b = rank(net_a), rank(net_b)
    if rank_a is not None and rank_b is not None and rank_a != rank_b:
        return net_a if rank_a > rank_b else net_b
    if rank_a == 0.0 and rank_b is None:
        return net_b
    if rank_b == 0.0 and rank_a is None:
        return net_a
    return None


# Pins of an LED with its own driver (an addressable LED): supply and data pins.
_INTEGRATED_PIN = re.compile(r"VDD\w*|VCC\w*|VSS\w*|GND\w*|VS\d*|V\+|V-|DIN|DOUT|DI|DO|SDI|"
                             r"SDO|CKI|CKO")


def _led_elements(board: Board, ref: str) -> list[_Diode] | None:
    """Each diode of an LED part, or None when its pins cannot be paired: a two-pin LED by
    its A/K pin names (else by the nets: the side on ground is the cathode), a common-anode
    or common-cathode part by its one common pin, else by channel numbers (A1/K1)."""
    names = board.tree.pin_names(ref)
    pins = board.design.part_pins.get(ref) or {}
    nets = list(dict.fromkeys(pins.values()))
    if len(pins) == 2 and len(nets) == 2:
        (pin_a, net_a), (pin_b, net_b) = pins.items()
        side_a = diode_side(names.get(pin_a)) or diode_side(pin_a)
        side_b = diode_side(names.get(pin_b)) or diode_side(pin_b)
        if side_a == "anode" or side_b == "cathode":
            return [_Diode(net_a, net_b)]
        if side_a == "cathode" or side_b == "anode":
            return [_Diode(net_b, net_a)]
        anode = _orient(board, ref, net_a, net_b)
        if anode is None:
            return None
        return [_Diode(anode, net_b if anode == net_a else net_a, guessed=True)]
    anodes, cathodes, coloured = [], [], []
    for pin, net in pins.items():
        first = first_function(names.get(pin) or "")
        if _ANODE_PIN.fullmatch(first) and not _pin_colour(first):
            anodes.append((net, first))
        elif _CATHODE_PIN.fullmatch(first) and not _pin_colour(first):
            cathodes.append((net, first))
        elif _pin_colour(first):
            coloured.append((net, first))
    anode_nets = {net for net, _ in anodes}
    cathode_nets = {net for net, _ in cathodes}
    if len(anode_nets) == 1 and (coloured or len(cathode_nets) > 1) and (
            not cathode_nets or len(cathode_nets) > 1):
        anode = next(iter(anode_nets))  # common anode: the other pins are cathodes
        return [_Diode(anode, net, _pin_colour(name), name) for net, name in
                [*coloured, *cathodes] if net != anode] or None
    if len(cathode_nets) == 1 and (coloured or len(anode_nets) > 1) and (
            not anode_nets or len(anode_nets) > 1):
        cathode = next(iter(cathode_nets))
        return [_Diode(net, cathode, _pin_colour(name), name) for net, name in
                [*coloured, *anodes] if net != cathode] or None
    by_channel: dict[str, dict[str, str]] = defaultdict(dict)
    for side, items in (("a", anodes), ("k", cathodes)):
        for net, name in items:
            by_channel[_channel(name)][side] = net
    pairs = [_Diode(item["a"], item["k"], None, channel) for channel, item in
             sorted(by_channel.items(), key=lambda pair: natural_key(pair[0]))
             if "a" in item and "k" in item and channel and item["a"] != item["k"]]
    return pairs or None


# A display's or a jack's LED pins: LED+/LED-, LEDA/LEDK, LED1_A/LED1_K, BL+/BL-.
_PART_LED_ANODE = re.compile(r"(?:LED|BL)(\d*)(?:\+|_?A|_?ANODE)", re.I)
_PART_LED_CATHODE = re.compile(r"(?:LED|BL)(\d*)(?:-|_?K|_?CATHODE)", re.I)


def _part_led_elements(board: Board, ref: str) -> list[_Diode]:
    """The LEDs inside another part (a display's backlight, a jack's indicator): its
    anode and cathode pins paired by their channel number."""
    names = board.tree.pin_names(ref)
    anodes: dict[str, set[str]] = defaultdict(set)
    cathodes: dict[str, set[str]] = defaultdict(set)
    labels: dict[str, list[str]] = defaultdict(list)
    for pin, net in (board.design.part_pins.get(ref) or {}).items():
        first = first_function(names.get(pin) or "")
        for pattern, found in ((_PART_LED_ANODE, anodes), (_PART_LED_CATHODE, cathodes)):
            if match := pattern.fullmatch(first):
                found[match[1]].add(net)
                labels[match[1]].append(first)
                break
    return [_Diode(next(iter(anodes[channel])), next(iter(cathodes[channel])), None,
                   "/".join(labels[channel]))
            for channel in sorted(anodes, key=natural_key)
            if len(anodes[channel]) == 1 and len(cathodes.get(channel, ())) == 1
            and anodes[channel] != cathodes[channel]]


def _part_number(board: Board, ref: str) -> str | None:
    bom = board.tree.parts.get(ref)
    meta = (board.netlist.get("components") or {}).get(ref) or {}
    text = (bom.mpn if bom else None) or meta.get("mpn")
    return re.sub(r"\s+", "", text).upper() if text and text.strip() else None


def _led_infos(board: Board) -> dict[str, _Led]:
    """Each LED's reading (read_led), with the colour or Vf one part states shared by the
    fitted parts of the same part number that state none."""
    components = board.netlist.get("components") or {}
    infos = {ref: read_led(merged_params(components.get(ref) or {}, board.plan, ref),
                           board.tree.text(ref)) for ref in board.leds}
    groups: dict[str, list[str]] = defaultdict(list)
    for ref in board.leds:
        if number := _part_number(board, ref):
            groups[number].append(ref)
    for refs in groups.values():
        for attr, source in (("colour", "colour_source"), ("vf", "vf_source")):
            donors = [ref for ref in refs if getattr(infos[ref], attr) is not None]
            if len({getattr(infos[ref], attr) for ref in donors}) != 1:
                continue
            donor = donors[0]
            for ref in refs:
                info = infos[ref]
                if getattr(info, attr) is None and not info.colours:
                    setattr(info, attr, getattr(infos[donor], attr))
                    setattr(info, source, f"same part number as {donor} "
                                          f"({getattr(infos[donor], source)})")
    return infos


def _led_state(result: dict) -> str:
    return ("led_current" if "current_ma" in result else
            "led_current_vs_vf" if "current_ma_at_vf" in result else
            "led_supply_unknown" if "current_formula" in result else "led_not_computed")


def _vf_choice(info: _Led, ref: str, colour: str | None, user: dict | None,
               opto: bool) -> tuple[float | None, str, bool]:
    """(Vf, source, typical) for one diode: vf= by designator, then by colour, then the
    stated Vf, then the colour's (or an opto's) typical value."""
    folded = {key.casefold(): value for key, value in (user or {}).items()}
    if ref.casefold() in folded:
        return folded[ref.casefold()], "vf argument (part)", False
    if colour and colour in folded:
        return folded[colour], f"vf argument ({colour})", False
    if info.vf is not None:
        return info.vf, f"stated: {info.vf_source}", False
    if opto:
        return OPTO_VF, "typical optocoupler input LED (IR)", True
    if colour:
        return TYPICAL_VF[colour], f"typical for {colour.replace('_', ' ')}", True
    return None, "unknown (no colour read): pass vf=", True


def _diode_row(board: Board, ref: str, diode: _Diode, info: _Led, colour: str | None,
               colour_source: str | None, user_vf: dict | None, opto: bool,
               backlight: bool = False) -> tuple[dict, list[dict]]:
    """A diode's current: (row, findings). A resistor across the diode takes Vf / R_across
    from the series current; a backlight (LED pins on another part) gets no indicator
    rating."""
    anode, cathode, label = diode.anode, diode.cathode, diode.channel
    across = [res for res in board.res_on.get(anode, ()) if res.far(anode) == cathode]
    # A diode across it (anti-parallel reverse protection, or a second LED of a bi-colour
    # pair) is not in its path.
    reverse = [other for other, _ in board.members(anode, {ref})
               if board.tree.kind(other) in ("diode", "zener") and cathode in set(
                   (board.design.part_pins.get(other) or {}).values())]
    skip = {res.ref for res in across} | set(reverse)
    high = _walk(board, anode, ref, True, skip)
    low = _walk(board, cathode, ref, False, skip)
    groups = high.groups + low.groups
    resistors = [res for group in groups for res in group]
    steps = [parallel([res.ohms for res in group]) for group in groups]
    r_total = None if not groups or None in steps else sum(steps)
    r_across = parallel([res.ohms for res in across]) if across else None
    vf, vf_source, typical = _vf_choice(info, ref, colour, user_vf, opto)
    kind = "opto" if opto else "led"
    row: dict = {"kind": kind, ("opto" if opto else "led"): ref}
    if label:
        row["channel"] = label
    if colour and not opto:
        row["colour"] = colour
        row["colour_source"] = colour_source
    row["anode"], row["cathode"] = anode, cathode
    v_high, v_low = high.level, low.level
    inputs: dict = {
        "V_supply": high.describe(board),
        "V_sink": low.describe(board),
        "Vf": _clean({"volts": vf, "source": vf_source, "typical": typical or None}),
        "R": _clean({"value": ohms_text(r_total), "ohms": _r(r_total, 6),
                     "parts": [res.ref for res in resistors],
                     "arrangement": "parallel resistors" if any(
                         len(group) > 1 for group in groups) else None}),
    }
    if across:
        inputs["R_across"] = _clean({"value": ohms_text(r_across), "ohms": _r(r_across, 6),
                                     "parts": [res.ref for res in across]})
    row["inputs"] = inputs
    name = "I_F" if opto else "I"
    row["formula"] = f"{name} = (V_supply - V_sink - Vf) / R" + (
        " - Vf / R_across" if across else "")
    result: dict = {}
    notes: list[str] = []
    confidence = "high"
    findings: list[dict] = []
    current = None
    shunted_off = None  # (V across the diode, series current) when R_across holds it off
    side = high if high.kind == "unknown" or v_high is None else low
    if "driver" in (high.kind, low.kind):
        notes.append("driven by an LED driver or a converter's pin: it sets the current "
                     "(its datasheet and its set or sense resistor), not a series resistor")
        confidence = "medium"
    elif v_high is None or v_low is None or high.kind == "unknown" or low.kind == "unknown":
        notes.append(f"the {'supply' if side is high else 'sink'} side was not followed "
                     f"({side.detail or 'not recognised'})")
        confidence = "low"
    elif not groups:
        notes.append("no series resistor found between the supply and the sink")
        confidence = "low"
    elif v_high.volts is None or v_low.volts is None:
        level = v_high if v_high.volts is None else v_low
        missing = level.rail or level.net
        if missing:
            board.needed[missing].add(ref)
            notes.append(f"{missing}'s voltage is not known: pass rails='{missing}=volts'")
        else:
            notes.append("the driving pin's I/O rail was not read (no supply pin by name): "
                         "its high level is the datasheet's VOH")
        result["current_formula"] = (f"{name} = ({f'V({missing})' if missing else 'VOH'} - "
                                     f"{_vtext(vf) + ' V' if vf is not None else 'Vf'}) / "
                                     f"{ohms_text(r_total)} ohm") if r_total else None
        confidence = "low"
    elif r_total is not None and r_total > 0:
        span = v_high.volts - v_low.volts
        if across and r_across is None:
            notes.append("the resistor across it could not be read: its current is left out")

        def diode_amps(volts: float) -> tuple[float, float]:
            """(series current, diode current) at a forward voltage."""
            series = (span - volts) / r_total
            return series, series - (volts / r_across if r_across else 0.0)

        if vf is not None:
            series, current = diode_amps(vf)
            if current <= 0 and r_across and span > 0:
                # Below Vf the diode is off: the series resistor and R_across divide.
                v_diode = span * r_across / (r_total + r_across)
                shunted_off = (v_diode, span / (r_total + r_across))
                current = 0.0
                result["current_ma"] = 0.0
                result["v_across_diode"] = _r(v_diode)
                result["series_current_ma"] = _ma(shunted_off[1])
                notes.append(f"{'/'.join(res.ref for res in across)} across it keeps it at "
                             f"{_vtext(v_diode)} V, below its Vf: it does not turn on")
            else:
                result["current_ma"] = _ma(current)
                if r_across:
                    result["series_current_ma"] = _ma(series)
                    result["across_current_ma"] = _ma(series - current)
                if current <= 0:
                    notes.append("not forward biased at the nominal voltages" + (
                        ": its anode is on ground, so it conducts only if its cathode side "
                        "goes below ground; check the symbol's A/K pins"
                        if anode in board.grounds else ""))
        else:
            result["current_ma_at_vf"] = {f"{value:g} V": _ma(max(0.0, diode_amps(value)[1]))
                                          for value in UNKNOWN_VF}
            confidence = "medium"
        for level in (v_high, v_low):
            if level.confidence == "low":
                confidence = "low"
            elif level.confidence == "medium" and confidence == "high":
                confidence = "medium"
        if (typical or "beside a current" in vf_source) and vf is not None and                 confidence == "high":
            confidence = "medium"
    if any(item.kind == "gpio" for item in (high, low)):
        notes.append("driven by a processor GPIO: its source/sink current limit is a "
                     "datasheet value")
    if diode.guessed:
        notes.append("polarity read from the circuit (the pins are not named A/K)")
        confidence = "low"
    if across:
        row["across_diode"] = [res.ref for res in across]
    if reverse:
        row["diode_across"] = reverse
    row["result"] = result
    row["confidence"] = confidence
    row["_current"] = current
    row["_refs"] = {ref, *(res.ref for res in resistors + across), *(
        side.part for side in (high, low) if side.part)}
    row["_nets"] = {anode, cathode, *(res.a for res in resistors), *(
        res.b for res in resistors)}
    sure_levels = all(level is not None and level.confidence in ("high", "medium")
                      for level in (v_high, v_low))
    row_kind = "optos" if opto else "leds"
    # Each resistor's power at this current (with an unknown Vf: at 2.0 V and at 3.0 V).
    if r_total and v_high is not None and v_low is not None and \
            v_high.volts is not None and v_low.volts is not None and (
                "current_ma" in result or "current_ma_at_vf" in result):
        span = v_high.volts - v_low.volts

        def powers(series: float, v_diode: float) -> dict[str, float]:
            """designator -> watts: a series step's resistors at the voltage across the
            step, a resistor across the diode at the diode's voltage."""
            out = {}
            for group, step in zip(groups, steps):
                for res in group:
                    if res.ohms:
                        out[res.ref] = (series * step) ** 2 / res.ohms
            for res in across:
                if res.ohms:
                    out[res.ref] = v_diode ** 2 / res.ohms
            return out

        def at(volts: float) -> dict:
            series = (span - volts) / r_total
            return powers(series, volts) if series > 0 else {}

        if shunted_off:
            exact, low_bound = powers(shunted_off[1], shunted_off[0]), None
        elif current is not None:
            exact, low_bound = at(vf), None
        else:
            exact, low_bound = at(UNKNOWN_VF[0]), at(UNKNOWN_VF[-1])
        if exact:
            key = ("resistor_power_mw" if current is not None else
                   f"resistor_power_mw_at_vf_{UNKNOWN_VF[0]:g}")
            result[key] = {name: _r(watts * 1000, 4) for name, watts in exact.items()}
            by_ref = {res.ref: res for res in resistors + across}
            for name, watts in exact.items():
                findings += _power_finding(
                    board, by_ref[name], watts, current is not None,
                    None if low_bound is None else low_bound.get(name, 0.0), ref, kind,
                    row_kind)
    if shunted_off:
        findings.append(_finding(
            "diode_shunted_off",
            # A typical Vf decides only with a margin (the diode 15% below it).
            "warning" if sure_levels and (not typical or shunted_off[0] < 0.85 * vf)
            else "info",
            f"{ref}{' ' + label if label else ''} does not turn on: "
            f"{'/'.join(res.ref for res in across)} ({ohms_text(r_across)} ohm) across it "
            f"would draw {_vtext(vf / r_across * 1000)} mA at its Vf of {_vtext(vf)} V "
            f"({vf_source}), more than the {_vtext((span - vf) / r_total * 1000)} mA "
            f"{'/'.join(res.ref for res in resistors)} supplies, so it stays at about "
            f"{_vtext(shunted_off[0])} V. Check the resistor values (a larger resistor across "
            "it, or a smaller series resistor) and its Vf in the datasheet.",
            [ref, *(res.ref for res in resistors + across)],
            v_across_diode=_r(shunted_off[0]), _kind=row_kind))
    if not opto and not backlight:
        rating, rating_source = info.rating, info.rating_source
        if rating is None:
            rating, rating_source = LED_RATING_A, "typical indicator LED (none stated)"
        result["rating_ma"] = _ma(rating)
        result["rating_source"] = rating_source
        findings += _led_current_finding(row, ref, current, result, rating, rating_source,
                                         vf_source, resistors, sure_levels)
    if notes:
        row["notes"] = notes
    return row, findings


def _power_finding(board: Board, res: _Res, watts: float, exact: bool,
                   low_watts: float | None, diode: str, kind: str, row_kind: str
                   ) -> list[dict]:
    """resistor_overpower at the computed current; with an unknown Vf, watts is the power at
    Vf 2.0 V and low_watts at 3.0 V."""
    power, _ = resistor_limits(res.part, board.policy)
    if power is None or not res.ohms:
        return []
    derating = board.policy["resistor_power_derating"]
    if exact and watts > power.value + 1e-12:
        severity, what = "warning", "above its rating"
    elif exact and watts > power.value * derating + 1e-12:
        severity, what = "info", f"above {derating * 100:.0f}% of its rating (the derated limit)"
    elif not exact and low_watts is not None and low_watts > power.value + 1e-12:
        severity, what = "warning", (
            f"above its rating at any Vf from {UNKNOWN_VF[0]:g} to {UNKNOWN_VF[-1]:g} V "
            f"(even at {UNKNOWN_VF[-1]:g} V: {low_watts * 1000:.3g} mW)")
    elif not exact and watts > power.value + 1e-12:
        severity, what = "info", (f"above its rating if the {kind}'s Vf is "
                                  f"{UNKNOWN_VF[0]:g} V (a red LED)")
    else:
        return []
    amps = math.sqrt(watts / res.ohms)
    return [_finding(
        "resistor_overpower", severity,
        f"{res.ref} ({ohms_text(res.ohms)} ohm) in series with {kind} {diode} dissipates "
        f"{watts * 1000:.3g} mW at {amps * 1000:.3g} mA: {what}, "
        f"{power.value * 1000:.3g} mW ({power.describe()}). Check the current and the "
        "resistor's power rating and temperature derating in its datasheet.",
        [res.ref, diode], power_mw=_r(watts * 1000, 4), current_ma=_ma(amps),
        rating_mw=_r(power.value * 1000, 3), rating_source=power.describe(),
        _kind=row_kind)]


def _led_current_finding(row: dict, ref: str, current: float | None, result: dict,
                         rating: float, rating_source: str, vf_source: str,
                         resistors: list[_Res], sure_levels: bool) -> list[dict]:
    """led_overcurrent: a warning when Vf is known, both voltages are read with medium or
    high confidence and I exceeds a rating a parameter states, or 30 mA; else info (a
    rating read from text is often the test current)."""
    refs = [ref, *(res.ref for res in resistors)]
    if current is not None:
        if current <= rating + 1e-9:
            return []
        stated = rating_source.startswith("parameter")
        sure = (not vf_source.startswith("unknown") and sure_levels
                and (current > LED_WARN_A + 1e-9 or stated))
        return [_finding(
            "led_overcurrent", "warning" if sure else "info",
            f"{ref} carries about {current * 1000:.3g} mA (Vf {row['inputs']['Vf']['volts']:g}"
            f" V, {vf_source}), above {rating * 1000:.3g} mA ({rating_source}). Check the "
            "LED's maximum forward current and Vf at that current in its datasheet; a "
            "larger series resistor lowers it.",
            refs, current_ma=_ma(current), rating_ma=_ma(rating))]
    at_vf = result.get("current_ma_at_vf")
    if at_vf:
        lowest = min(at_vf.values())
        if lowest / 1000 > rating + 1e-9:
            return [_finding(
                "led_overcurrent", "info",
                f"{ref}'s colour is not known, but even at Vf {UNKNOWN_VF[-1]:g} V it carries "
                f"{lowest:g} mA, above {rating * 1000:.3g} mA ({rating_source}). Pass vf= "
                "with its forward voltage and check its maximum current.",
                refs, current_ma_at_vf=at_vf, rating_ma=_ma(rating))]
    return []


def _is_opto(board: Board, ref: str) -> bool:
    text = board.tree.text(ref)
    return bool(OPTO_TEXT.search(text) or _OPTO_FAMILY.search(text))


def _opto_pins(board: Board, ref: str) -> tuple[dict[str, dict[str, str]], bool]:
    """channel -> {anode, cathode, collector, emitter: net}, and whether a standard pinout
    was assumed (unnamed pins)."""
    names = board.tree.pin_names(ref)
    pins = board.design.part_pins.get(ref) or {}
    out: dict[str, dict[str, str]] = defaultdict(dict)
    named: dict[str, str] = {}  # pin -> the role its name gives
    for pin, net in pins.items():
        first = first_function(names.get(pin) or "")
        channel = _channel(first) or "1"
        if re.fullmatch(r"A\d*|AN\d*|ANODE\d*|LED\+\d*|IF\+", first):
            role = "anode"
        elif re.fullmatch(r"K\d*|KA\d*|CATH\w*|LED-\d*|IF-", first):
            role = "cathode"
        elif _OPTO_EMITTER_PIN.fullmatch(first):
            role = "emitter"
        elif _OPTO_OUT_PIN.fullmatch(first):
            role = "collector"
        else:
            continue
        named[pin] = role
        out[channel].setdefault(role, net)
    if any("anode" in item and "cathode" in item for item in out.values()):
        return dict(out), False
    # The standard pinout for the pin count, when the names it has agree with it (a 'C'
    # where the pinout has the cathode is the cathode; unnamed pins say nothing).
    meta = board.tree.components.get(ref) or {}
    count = len([pin for pin in meta.get("pin_designators") or pins if pin])
    pinout = _OPTO_PINOUTS.get(count)
    if pinout is None:
        return {}, False
    for pin, role in named.items():
        standard = pinout.get(pin, (None,))[0]
        if role != standard and not (role == "collector" and standard == "cathode" and
                                     first_function(names.get(pin) or "") == "C"):
            return {}, False
    out = defaultdict(dict)
    for pin, (role, channel) in pinout.items():
        if pin in pins:
            out[channel][role] = pins[pin]
    return dict(out), True


def _opto_output(board: Board, ref: str, item: dict[str, str]) -> dict:
    """The output transistor's load: a pull-up on the collector (or a resistor on the
    emitter) with its rail, and the current at saturation."""
    collector, emitter = item.get("collector"), item.get("emitter")
    out: dict = {}
    if collector and not board.fixed(collector):
        ups = [res for res in board.res_on.get(collector, ())
               if board.fixed(res.far(collector)) and res.far(collector) not in board.grounds]
        if ups:
            res = ups[0]
            rail = res.far(collector)
            out = {"configuration": "common emitter (pull-up on the collector)",
                   "load": res.ref, "value": ohms_text(res.ohms), "rail": rail,
                   "emitter": emitter}
            level = board.level(rail)
            if level is not None and res.ohms:
                out["rail_volts"] = _r(level.volts)
                out["i_out_sat_ma"] = _ma(level.volts / res.ohms)
                out["_amps"] = level.volts / res.ohms
            elif level is None:
                board.needed[rail].add(ref)
    if not out and emitter and not board.fixed(emitter):
        downs = [res for res in board.res_on.get(emitter, ())
                 if res.far(emitter) in board.grounds]
        if downs and collector and board.fixed(collector):
            res = downs[0]
            out = {"configuration": "emitter follower (resistor on the emitter)",
                   "load": res.ref, "value": ohms_text(res.ohms), "rail": collector}
            level = board.level(collector)
            if level is not None and res.ohms:
                out["rail_volts"] = _r(level.volts)
                out["i_out_sat_ma"] = _ma(level.volts / res.ohms)
                out["_amps"] = level.volts / res.ohms
    if not out:
        out = {"configuration": "no pull-up or emitter resistor found (an IC input's own "
                                "pull-up, or a load elsewhere)",
               "collector": collector, "emitter": emitter}
    return _clean(out)


# ---------- ADC inputs and RC filters ----------

def _cap_input(caps: list[tuple[Part, float | None]]) -> dict:
    known = [farads for _, farads in caps if farads is not None]
    total = sum(known) if known and len(known) == len(caps) else None
    return _clean({"value": farads_text(total), "farads": total,
                   "parts": [part.ref for part, _ in caps]})


def _adc_row(board: Board, ref: str, pin: str, net: str, basis: str,
             dividers: dict[str, dict], adc_nets: set[str]) -> tuple[dict, list[dict]]:
    name = board.name(ref, pin)
    row: dict = {"kind": "adc_input", "part": ref, "pin": pin,
                 "name": name or None, "net": net, "basis": basis}
    caps = [(part, farads) for part, farads in board.caps_to_ground(net)
            if farads is None or farads <= MAX_RC_FARADS]
    differential = [(part.ref, other) for part, other in board.caps_on.get(net, ())
                    if other not in board.grounds and other in adc_nets]
    divider = dividers.get(net)
    series = [res for res in board.res_on.get(net, ())
              if not board.fixed(res.far(net)) and res.far(net) not in board.grounds]
    if divider is not None:
        legs = set(divider["inputs"]["R_top"]["parts"]) | set(
            divider["inputs"]["R_bottom"]["parts"])
        series = [res for res in series if res.ref not in legs]
    r_value, r_desc, r_words = None, None, ""
    behind = None
    if divider is not None and divider["result"].get("source_impedance"):
        r_value = _thevenin(divider)
        r_desc = {"value": divider["result"]["source_impedance"], "ohms": _r(r_value, 6),
                  "parts": [*divider["inputs"]["R_top"]["parts"],
                            *divider["inputs"]["R_bottom"]["parts"]],
                  "source": "the divider's source impedance R_top || R_bottom"}
        r_words = (f"only the divider {'/'.join(r_desc['parts'])}, source impedance "
                   f"{r_desc['value']}")
        row["divider"] = {"midpoint": net, "ratio": divider["result"].get("ratio"),
                          "v_mid": divider["result"].get("v_mid")}
    elif len(series) == 1:
        res = series[0]
        far = res.far(net)
        r_value = res.ohms
        r_desc = {"value": ohms_text(res.ohms), "ohms": _r(res.ohms, 6), "parts": [res.ref],
                  "source": f"series resistor from {far}"}
        r_words = f"only {res.ref} in series"
        upstream = dividers.get(far)
        if upstream is not None and res.ohms is not None and \
                upstream["result"].get("source_impedance"):
            thevenin = _thevenin(upstream)
            r_value = res.ohms + thevenin
            r_desc = {"value": ohms_text(r_value), "ohms": _r(r_value, 6),
                      "parts": [res.ref, *upstream["inputs"]["R_top"]["parts"],
                                *upstream["inputs"]["R_bottom"]["parts"]],
                      "source": f"{res.ref} plus the divider at {far} "
                                f"({upstream['result']['source_impedance']})"}
            r_words = f"only {res.ref} in series, behind the divider at {far}"
            row["divider"] = {"midpoint": far, "ratio": upstream["result"].get("ratio"),
                              "v_mid": upstream["result"].get("v_mid"), "through": res.ref}
        behind_caps = [part.ref for part, _ in board.caps_to_ground(far)]
        behind = _clean({"net": far, "capacitors": behind_caps,
                         "parts": sorted({other for other, _ in board.members(far, {res.ref})
                                          if board.tree.kind(other) not in
                                          ("capacitor", "resistor")}, key=natural_key)[:4]})
    elif len(series) > 1:
        row["series_resistors"] = [res.ref for res in series]
    inputs: dict = {}
    if r_desc:
        inputs["R"] = _clean(r_desc)
    if caps:
        inputs["C"] = _cap_input(caps)
    if differential:
        inputs["C_differential"] = [{"part": part, "to": other} for part, other in differential]
    row["inputs"] = inputs
    if behind:
        row["source_side"] = behind
    c_total = inputs.get("C", {}).get("farads")
    if caps and r_value:
        state = "rc"
    elif caps:
        state = "c_only"
    elif r_value:
        state = "r_only"
    else:
        state = "none"
    row["filter"] = state
    result: dict = {}
    if state == "rc" and c_total and r_value:
        hz = 1 / (2 * math.pi * r_value * c_total)
        row["formula"] = "fc = 1 / (2 pi R C)"
        result = {"cutoff_hz": _r(hz), "cutoff": _hz_text(hz),
                  "tau_ms": _r(r_value * c_total * 1000)}
    elif state == "c_only":
        result = {"note": "a shunt capacitor with no series resistor: its cutoff is set by "
                          "the source's impedance"}
    row["result"] = result
    row["confidence"] = "medium" if basis.startswith("ADC-capable") else "high"
    row["_refs"] = {ref, *(part.ref for part, _ in caps), *(r_desc or {}).get("parts", [])}
    row["_nets"] = {net} | ({behind["net"]} if behind else set())
    findings = []
    if state in ("none", "r_only"):
        findings.append(_finding(
            "adc_no_filter", "info",
            f"ADC input {board.label(ref, pin)} on {net} has no capacitor to ground"
            + (f" ({r_words})" if r_desc else "")
            + ": the checklist asks for a filter on every ADC input (a series R and a shunt "
              "C sized for the signal's bandwidth and the ADC's sampling).",
            [ref], net=net, pin=board.label(ref, pin)))
    return row, findings


def _thevenin(divider: dict) -> float:
    top = divider["inputs"]["R_top"].get("ohms")
    bottom = divider["inputs"]["R_bottom"].get("ohms")
    return top * bottom / (top + bottom) if top and bottom else 0.0


def _rc_row(board: Board, net: str, taken: set[str]) -> dict | None:
    """A node with one series resistor from a driven net and a capacitor to ground,
    feeding an IC or transistor: an RC low-pass filter."""
    if net in taken or board.fixed(net):
        return None
    caps = [(part, farads) for part, farads in board.caps_to_ground(net)]
    if not caps or any(farads is None or farads > MAX_RC_FARADS for _, farads in caps):
        return None
    resistors = board.res_on.get(net, ())
    series = [res for res in resistors
              if res.ohms is not None and res.ohms > LINK_OHMS and not board.fixed(res.far(net))]
    pulls = [res for res in resistors if board.fixed(res.far(net))]
    if len(series) != 1 or len(resistors) != len(series) + len(pulls):
        return None
    res = series[0]
    source = res.far(net)
    skip = {res.ref, *(pull.ref for pull in pulls), *(part.ref for part, _ in caps)}
    receivers = []
    for ref, pin in board.members(net, skip):
        found = board.pin_use(ref, pin, net, context=False)
        if found and found[0] not in ("supply_pin", "connector", "transistor_channel"):
            receivers.append(board.label(ref, pin))
        elif found is None and board.tree.kind(ref) not in ("esd", "passive"):
            return None  # a diode, inductor or crystal: not a plain RC
    if not receivers:
        return None
    drivers = []
    for ref, pin in board.members(source, {res.ref}):
        kind = board.tree.kind(ref)
        name = board.name(ref, pin)
        if kind in ("ic", "other") and (board.design.pin_type(ref, pin) == "Input" or pin_role(
                name) in ("supply", "ground", "ref", "cap")):
            continue
        if kind == "transistor" and _transistor_role(name) != "high":
            continue
        if kind in ("ic", "other", "connector", "switch", "transistor"):
            drivers.append(board.label(ref, pin))
    if not drivers:
        return None
    r_value = res.ohms
    if pulls and all(pull.ohms for pull in pulls):
        r_value = parallel([res.ohms, *(pull.ohms for pull in pulls)])
    c_total = sum(farads for _, farads in caps)
    hz = 1 / (2 * math.pi * r_value * c_total)
    row = {"kind": "rc_filter", "net": net, "from": source,
           "drivers": drivers[:3], "receivers": receivers[:3],
           "inputs": _clean({"R": _clean({"value": ohms_text(r_value), "ohms": _r(r_value, 6),
                                          "parts": [res.ref, *(pull.ref for pull in pulls)],
                                          "source": "series resistor" + (
                                              " in parallel with the pull(s) on the node "
                                              "(Thevenin)" if pulls else "")}),
                             "C": _cap_input(caps)}),
           "formula": "fc = 1 / (2 pi R C)",
           "result": {"cutoff_hz": _r(hz), "cutoff": _hz_text(hz),
                      "tau_us": _r(r_value * c_total * 1e6)},
           "confidence": "high"}
    row["_refs"] = {res.ref, *(pull.ref for pull in pulls), *(part.ref for part, _ in caps)}
    row["_nets"] = {net, source}
    return row


# ---------- the check ----------

def _finding(check: str, severity: str, issue: str, designators, **fields) -> dict:
    out: dict = {"check": check, "severity": severity,
                 "designators": sorted(set(designators), key=natural_key)}
    out.update({key: value for key, value in fields.items() if value not in (None, [], {})})
    out["issue"] = issue
    return out


def _row_key(row: dict) -> tuple:
    first = (row.get("midpoint") or row.get("led") or row.get("opto") or row.get("part")
             or row.get("net") or "")
    return (_KIND_RANK[row["kind"]], natural_key(first), str(row.get("channel") or ""),
            natural_key(str(row.get("pin") or "")))


def led_calculations(board: Board, user_vf: dict | None, stats: Counter
                     ) -> tuple[list[dict], list[dict], set[str]]:
    """Every LED's calculation (rows, findings, and the anode and cathode nets): LED parts,
    and LED pins on other parts (a display's backlight, a jack's indicator). A row's
    _current is the diode current in amps when computed. stats counts the LED states."""
    tree = board.tree
    rows: list[dict] = []
    findings: list[dict] = []
    nodes: set[str] = set()
    user_vf = user_vf or {}
    infos = _led_infos(board)
    for ref in board.leds:
        info = infos[ref]
        names = tree.pin_names(ref)
        if any(_INTEGRATED_PIN.fullmatch(first_function(names.get(pin) or ""))
               for pin in tree.design.part_pins.get(ref) or {}):
            stats["led_integrated"] += 1  # supply or data pins: a driver inside, no R
            continue
        elements = _led_elements(board, ref)
        if not elements:
            stats["led_unread"] += 1
            rows.append({"kind": "led", "led": ref, "confidence": "low",
                         "notes": ["its anode and cathode pins could not be told apart"],
                         "inputs": {}, "result": {}, "_refs": {ref},
                         "_nets": set((tree.design.part_pins.get(ref) or {}).values())})
            continue
        for diode in elements:
            colour = diode.colour or info.colour
            source = "pin name" if diode.colour else info.colour_source
            row, found = _diode_row(board, ref, diode, info, colour, source, user_vf, False)
            nodes |= {diode.anode, diode.cathode}
            rows.append(row)
            findings += found
            stats[_led_state(row["result"])] += 1
    # LED pins on another part (a display's backlight, a jack's indicator).
    for ref in sorted(tree.design.part_pins, key=natural_key):
        if ref in board.leds or not tree.fitted(ref) or tree.kind(ref) not in (
                "ic", "other", "connector", "passive") or ref in tree.converters or \
                ref in board.processors or LED_DRIVER_TEXT.search(tree.text(ref)) or \
                _is_opto(board, ref):
            continue
        for diode in _part_led_elements(board, ref):
            row, found = _diode_row(board, ref, diode, _Led(), None, None, user_vf, False,
                                    backlight=True)
            row.setdefault("notes", []).append(
                f"LED pins on {ref} (a backlight or an indicator inside it): it may hold "
                f"several LEDs, so its Vf and current come from its datasheet (pass "
                f"vf='{ref}=volts')")
            row["confidence"] = "low" if row["confidence"] == "low" else "medium"
            nodes |= {diode.anode, diode.cathode}
            rows.append(row)
            findings += found
            stats["led_in_other_part"] += 1
            stats[_led_state(row["result"])] += 1
    return rows, findings, nodes


def prepare_circuit_calculations(
    netlist: dict,
    plan=None,
    project_params: dict | None = None,
    kinds: list[str] | None = None,
    parts: list[str] | None = None,
    nets: list[str] | None = None,
    rails: dict[str, float] | None = None,
    vref: dict[str, float] | float | None = None,
    vf: dict[str, float] | None = None,
    vth: dict[str, float] | float | None = None,
    root_dir=None,
    policy_setting: str | None = None,
) -> dict:
    """Read the board once and compute every calculation; render_circuit_calculations
    pages the result. An {"error": ...} dict for an unknown kind, part or net.
    policy_setting: the Derating policy setting's file (resistor power limits), under the
    project's derating_policy.json."""
    requested = [kind.strip().lower() for kind in kinds or [] if kind.strip()]
    if invalid := [kind for kind in requested if kind not in KINDS]:
        return {"error": "invalid_kinds",
                "message": f"Unknown kinds {invalid}; use {list(KINDS)}."}
    wanted_kinds = set(requested or KINDS)
    notes: list[str] = []
    policy_info, policy_notes = policy_or_defaults(root_dir, policy_setting,
                                                   "the default resistor power limits")
    notes.extend(policy_notes)
    board = Board(netlist, plan, project_params, rails, vref, policy_info["values"])
    tree = board.tree
    wanted_parts, unknown_parts = resolve_names(parts, tree.design.part_pins)
    if parts and not wanted_parts:
        return {"error": "part_not_found",
                "message": f"No part {unknown_parts} in the compiled netlist; use "
                           "search_components to find the designator."}
    wanted_nets, unknown_nets = resolve_names(nets, tree.design.nets)
    if nets and not wanted_nets:
        return {"error": "net_not_found",
                "message": f"No net {unknown_nets} in the compiled netlist; use query_net to "
                           "find the real name."}
    if unknown_parts or unknown_nets:
        notes.append(f"Not found: {', '.join([*unknown_parts, *unknown_nets])}.")
    if board.unknown_rails:
        notes.append(f"rails names no net {board.unknown_rails}: ignored (use query_net to "
                     "find the real name).")
    stats: Counter = Counter()
    rows: list[dict] = []
    findings: list[dict] = []

    # Dividers (computed always: ADC inputs read them).
    dividers: dict[str, dict] = {}
    for net in sorted(tree.design.nets, key=natural_key):
        if len(board.res_on.get(net, ())) < 2:
            continue
        row, why = _divider(board, net, vth, vref)
        if row is None:
            if why in ("driven", "bus", "series"):
                stats[f"divider_skipped_{why}"] += 1
            continue
        dividers[net] = row
    # A node a series leg passes holding only a test point, jumper or connector pin is
    # inside another row's chain, and so is the end of such a leg when nothing but a
    # connector reads it (a header across a chain of resistors): that row covers it.
    passed = {tap.net for row in dividers.values() for tap in row.get("_taps", ())
              if tap.what == "passive"}
    ends = {net for row in dividers.values() for net in row.get("_ends_past_taps", ())}
    dividers = {net: row for net, row in dividers.items() if net not in passed and not (
        net in ends and all(feed["use"] == "connector" for feed in row["feeds"]))}
    for row in dividers.values():
        findings += _range_finding(board, row)
        stats[f"divider_feeds:{row['feeds'][0]['use'] if row['feeds'] else 'none'}"] += 1
        stats["divider_v_mid_known"] += "v_mid" in row["result"]
        stats["divider_ranged"] += "ratio_range" in row["result"]
    rows += list(dividers.values())

    # LEDs.
    user_vf = vf or {}
    led_rows, led_findings, nodes = led_calculations(board, user_vf, stats)
    rows += led_rows
    findings += led_findings

    # Optocouplers.
    for ref in sorted(tree.design.part_pins, key=natural_key):
        if not tree.fitted(ref) or ref in board.leds or tree.kind(ref) not in (
                "ic", "other", "transistor", "diode") or not _is_opto(board, ref):
            continue
        stats["optos"] += 1
        meta = (board.netlist.get("components") or {}).get(ref) or {}
        info = read_led(merged_params(meta, board.plan, ref), tree.text(ref))
        channels, assumed = _opto_pins(board, ref)
        usable = {channel: item for channel, item in channels.items()
                  if "anode" in item and "cathode" in item}
        if not usable:
            stats["opto_unread"] += 1
            rows.append({"kind": "opto", "opto": ref, "confidence": "low",
                         "notes": ["its input diode's pins were not read (no A/K names and "
                                   "no standard pinout)"], "inputs": {}, "result": {},
                         "_refs": {ref},
                         "_nets": set((tree.design.part_pins.get(ref) or {}).values())})
            continue
        for channel, item in sorted(usable.items(), key=lambda pair: natural_key(pair[0])):
            anode, cathode = item["anode"], item["cathode"]
            diode = _Diode(anode, cathode, channel=channel if len(usable) > 1 else "")
            row, found = _diode_row(board, ref, diode, info, None, None, user_vf, True)
            nodes |= {anode, cathode}
            if assumed:
                row.setdefault("notes", []).append(
                    "pins unnamed: the standard pinout for its pin count was assumed")
                row["confidence"] = "low"
            shunt_r = [res.ref for res in board.res_on.get(anode, ())
                       if res.far(anode) == cathode]
            shunt_c = [part.ref for part, other in board.caps_on.get(anode, ())
                       if other == cathode]
            row["input_shunt"] = _clean({"resistors": shunt_r, "capacitors": shunt_c}) or None
            if not shunt_r and not shunt_c:
                stats["opto_no_shunt"] += 1
                findings.append(_finding(
                    "opto_no_input_shunt", "info",
                    f"Nothing sits across {ref}'s input diode ({anode} to {cathode}): a "
                    "resistor (it also keeps leakage from turning it on) or a capacitor "
                    "across it improves noise immunity, as the checklist asks.",
                    [ref], nets=[anode, cathode]))
            if _TRIAC_TEXT.search(tree.text(ref)):
                row["output"] = {"configuration": "triac output: not calculated"}
            else:
                row["output"] = _opto_output(board, ref, item)
                if_amps = row["_current"]
                out_amps = row["output"].pop("_amps", None)
                if if_amps and out_amps and if_amps > 0:
                    row["result"]["ctr_needed_percent"] = _r(out_amps / if_amps * 100, 3)
                    row["result"]["ctr_note"] = ("the output saturates when CTR x I_F >= "
                                                 "I_out_sat: compare with the datasheet's "
                                                 "minimum CTR at this I_F, over life")
                if _LOGIC_OPTO_TEXT.search(tree.text(ref)):
                    row["output"]["note"] = "a logic-output optocoupler: its output stage " \
                                            "has its own ratings"
            if "current_ma" in row["result"]:
                row["result"]["if_ma"] = row["result"].pop("current_ma")
            row.pop("across_diode", None)
            row["_refs"] |= set(shunt_r) | set(shunt_c)
            rows.append(row)
            findings += found

    # ADC inputs.
    adc_pins: list[tuple[str, str, str, str]] = []
    for ref in sorted(tree.design.part_pins, key=natural_key):
        if not tree.fitted(ref) or tree.kind(ref) not in ("ic", "other"):
            continue
        is_processor = ref in board.processors
        if not is_processor and not _ADC_PART.search(tree.text(ref)):
            continue
        for pin, net in sorted((tree.design.part_pins.get(ref) or {}).items(),
                               key=lambda item: natural_key(item[0])):
            if net is None or net in board.grounds or board.fixed(net) and net not in dividers:
                continue
            if len(tree.members(net)) <= 1:
                continue
            context = net in dividers or bool(board.caps_to_ground(net))
            if is_processor:
                name = board.name(ref, pin)
                tokens = [token for alt in alternatives(name)
                          for token in re.split(r"[_\s.]+", alt) if token]
                analog_net = not auto_net(net) and (classify(net) or ("",))[0] == "analog"
                if not (analog_net or context or any(_ADC_TOKEN.fullmatch(token)
                                                     for token in tokens)):
                    continue
            why = board.adc_pin(ref, pin, net, context)
            if why:
                adc_pins.append((ref, pin, net, why))
    adc_nets = {net for _, _, net, _ in adc_pins}
    for ref, pin, net, why in adc_pins:
        row, found = _adc_row(board, ref, pin, net, why, dividers, adc_nets)
        stats[f"adc_filter:{row['filter']}"] += 1
        rows.append(row)
        findings += found

    # RC filters.
    taken = set(dividers) | nodes | adc_nets
    for net in sorted(tree.design.nets, key=natural_key):
        row = _rc_row(board, net, taken)
        if row is not None:
            rows.append(row)
            stats["rc_filters"] += 1

    rows = [row for row in rows if _ROW_KIND_TO_KIND[row["kind"]] in wanted_kinds]
    findings = [finding for finding in findings
                if (finding.get("_kind") or _CHECK_KIND[finding["check"]]) in wanted_kinds]
    if wanted_parts is not None or wanted_nets is not None:
        keep_parts, keep_nets = set(wanted_parts or ()), set(wanted_nets or ())
        rows = [row for row in rows if row["_refs"] & keep_parts or row["_nets"] & keep_nets]
        findings = [finding for finding in findings
                    if set(finding["designators"]) & keep_parts
                    or {finding.get("net"), *(finding.get("nets") or ())} & keep_nets]
    rows.sort(key=_row_key)
    findings.sort(key=lambda finding: (_SEVERITY_RANK[finding["severity"]],
                                       _CHECK_RANK[finding["check"]],
                                       natural_key(finding["designators"][0])))
    needed = sorted(board.needed.items(), key=lambda item: (-len(item[1]), natural_key(item[0])))
    return {"variant": tree.variant, "rows": rows, "findings": findings, "stats": stats,
            "kinds": [kind for kind in KINDS if kind in wanted_kinds], "notes": notes,
            "needed": [{"net": net, "calculations": len(users)} for net, users in needed]}


_ROW_KIND_TO_KIND = {row_kind: kind for kind, row_kind in _ROW_KIND.items()}


def _public(row: dict) -> dict:
    return {key: value for key, value in row.items() if not key.startswith("_")}


def _summary(prepared: dict) -> dict:
    stats = prepared["stats"]
    rows = prepared["rows"]
    count = Counter(row["kind"] for row in rows)

    def by(prefix: str) -> dict:
        return {key[len(prefix):]: value for key, value in sorted(stats.items())
                if key.startswith(prefix)}

    out = {
        "dividers": {"total": count["divider"], "by_feed": by("divider_feeds:"),
                     "v_mid_known": stats["divider_v_mid_known"],
                     "with_tolerance_range": stats["divider_ranged"],
                     "pull_pairs_skipped": stats["divider_skipped_driven"]
                     + stats["divider_skipped_bus"],
                     "series_pull_down_skipped": stats["divider_skipped_series"]},
        "leds": {"total": count["led"], "current_computed": stats["led_current"],
                 "current_vs_vf": stats["led_current_vs_vf"],
                 "supply_voltage_unknown": stats["led_supply_unknown"],
                 "not_computed": stats["led_not_computed"], "unread": stats["led_unread"],
                 "with_own_driver_skipped": stats["led_integrated"],
                 "in_other_parts": stats["led_in_other_part"]},
        "optos": {"parts": stats["optos"], "channels": count["opto"],
                  "without_input_shunt": stats["opto_no_shunt"],
                  "unread": stats["opto_unread"]},
        "adc_inputs": {"total": count["adc_input"], "by_filter": by("adc_filter:")},
        "rc_filters": {"total": count["rc_filter"]},
    }
    return {kind: value for kind, value in out.items() if kind in prepared["kinds"]}


def render_circuit_calculations(prepared: dict, offset: int = 0,
                                limit: int = PAGE_LIMIT) -> dict:
    """One page: the summary first, then the calculations and the findings paged together
    on one offset."""
    rows, findings = prepared["rows"], prepared["findings"]
    by_check: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for finding in findings:
        by_check[finding["check"]][finding["severity"]] += 1
    offset = max(0, int(offset or 0))
    page = slice(offset, offset + limit)
    summary = _summary(prepared)
    summary["findings"] = {check: dict(levels) for check, levels in by_check.items()}
    result: dict = {
        "variant": prepared["variant"],
        "summary": summary,
        "severity_counts": {level: sum(1 for finding in findings
                                       if finding["severity"] == level)
                            for level in ("warning", "info")},
        "kinds": prepared["kinds"],
        "heuristic": HEURISTIC,
        "guidance": GUIDANCE,
        "findings": [_public(finding) for finding in findings[page]],
        "findings_total": len(findings),
        "calculations": [_public(row) for row in rows[page]],
        "calculations_total": len(rows),
    }
    if prepared["needed"]:
        result["voltages_needed_total"] = len(prepared["needed"])
        result["voltages_needed"] = prepared["needed"][:NEEDED_LISTED]
    result["offset"] = offset
    result["has_more"] = offset + limit < max(len(rows), len(findings))
    if result["has_more"]:
        result["next_offset"] = offset + limit
    notes = list(prepared["notes"])
    notes.append("Results are nominal and unloaded: a midpoint's load, a GPIO's output "
                 "resistance and a transistor's saturation voltage are left out. "
                 "voltages_needed lists nets whose voltage the calculations lack: ask the "
                 "user and pass rails='NET=volts'; pass vf= for LED forward voltages, vth= "
                 "for EN or comparator thresholds and vref= for references.")
    if any(row["kind"] == "adc_input" for row in rows[page]):
        notes.append("ADC inputs: the sample capacitor (charge sharing against the shunt C) "
                     "and the maximum source impedance are datasheet values; C is given for "
                     "that comparison.")
    result["notes"] = notes
    return result


def run_circuit_calculations(
    netlist: dict,
    plan=None,
    project_params: dict | None = None,
    kinds: list[str] | None = None,
    parts: list[str] | None = None,
    nets: list[str] | None = None,
    rails: dict[str, float] | None = None,
    vref: dict[str, float] | float | None = None,
    vf: dict[str, float] | None = None,
    vth: dict[str, float] | float | None = None,
    root_dir=None,
    offset: int = 0,
    limit: int = PAGE_LIMIT,
) -> dict:
    """The calculations in one call: prepare_circuit_calculations and one page of
    render_circuit_calculations. Heuristic: see the module notes."""
    prepared = prepare_circuit_calculations(netlist, plan, project_params, kinds, parts,
                                            nets, rails, vref, vf, vth, root_dir)
    if "error" in prepared:
        return prepared
    return render_circuit_calculations(prepared, offset, limit)
