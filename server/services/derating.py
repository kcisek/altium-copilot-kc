"""Component derating check (heuristic): each capacitor's voltage rating against the
voltage across it, small ceramics' DC-bias loss, and resistors' working voltage and power.

Net voltages carry their source and confidence: rails= given by the user (wins over
everything), grounds (0 V), the voltage in a net's name (3V3, +5V, VDD_1V8, 1P8V, -12V,
N5V and VNEG_5V negative, HV_400V; VDD33-style names only for the usual rails and at
medium confidence, VDD12 and VCC15 (1.2 V or 12 V?) left unknown; never an EN, PG or SENSE net, an AC net, a switch node, nor a net on a
converter's bootstrap, soft-start, compensation, feedback, current-limit, gate or mode pin
such as BST_5V), then the power tree: a converter output that a given Vref sets through
its feedback divider, and rails joined to a known one through 0 ohm links, shunts,
ferrites, fuses, inductors or common-mode chokes (the same voltage); a buck's switch node
peaks at its input and a boost's at its output (low confidence); last the unloaded
midpoint of a two-resistor divider between two known voltages with nothing on it but
capacitors, connectors and IC inputs (medium confidence). Ripple nets are the input and
output rails of switching converters and their switch nodes, each with every converter
role named. The voltage across a capacitor is the difference between its two nets'
voltages when both are known; a bootstrap capacitor (a switch node to a net of unknown
voltage) sees the converter's bias, so it stays unknown.

For each part fitted in the variant, the rating, dielectric, technology, capacitance,
case size and a resistor's power rating come from its parameters first (Voltage, Rated
Voltage, Voltage Rating, Dielectric, Temperature Coefficient, Power, Power Rating,
Case/Package, Size and the like), then its value, comment and description text ('10uF
25V X5R 0603', a text rating only when it is a standard rating, so a '3V3' rail named in
a description is not read, and of several only those beside the capacitance, dielectric,
case or power), then its part number decoded by the published part-numbering schemes of
the major families (Murata GRM/GCM/GRT, TDK C and CGA/CGB/CGJ, Samsung CL, KEMET C...C,
Yageo CC, AVX/Kyocera, Taiyo Yuden, Walsin; KEMET T49x/T5xx, AVX TAJ/TPS/TCJ and Panasonic
POSCAP tantalums; Yageo RC, Vishay CRCW, Panasonic ERJ and Stackpole RMCF resistors for
their size), then the footprint name for the case size (imperial codes; metric codes
marked metric, metric-only or in IPC-7351 names, mapped to imperial). Each field records
its source; when sources disagree, the value most kinds of source agree on is used (on a
tie a parameter, then the part number, then free text) and the others are kept as a
conflict. A capacitor of no stated technology on a chip footprint (or with a ceramic
dielectric) is taken for a ceramic. A resistor array's elements pair pins 1 and N, 2 and
N-1; an array whose power sources disagree takes the lowest, per element.

Checks (thresholds from DEFAULT_POLICY, which the user's Derating policy setting (a policy
file anywhere on the PC, ALTIUM_COPILOT_DERATING_POLICY), derating_policy.json in the
project folder and the policy argument override, in that order): over_rated (more than the rating across a part:
warning), ceramic_voltage (above 75% of rated, or 50% on a ripple net: warning; C0G the
same), tantalum_voltage (above 80% for MnO2 and polymer tantalums: warning),
electrolytic_voltage (above 80%: warning), capacitor_voltage (a capacitor whose
technology was not read: warning above the most lenient limit, info above the strictest),
dc_bias (a Class II ceramic in 0603 or smaller with a high capacitance for its case at
30% of its rating or more: info, with an approximate loss band), resistor_voltage (above
the typical working voltage of its size: warning; info when only one end's voltage is
known), resistor_power (dV^2/R against the stated or typical rating: warning above it,
info above the derated limit; a 0 ohm link between two voltages is a warning),
needs_current (LED series resistors, shunts, series supply resistors and resistors from
a rail to a net of unknown voltage whose power cannot be bounded under the derated limit:
info, saying what to get from the datasheet) and unknown_rating (capacitors with a known
voltage across them whose rating was not read: one info row per board with the list). A
lower rating from a source as well supported as the one used is judged too: its warnings
stay warnings, saying the sources disagree.

Guidance: Altium's ceramic-capacitor derating article (at least 25% derating as standard
and 50% under ripple; DC-bias loss is worse in small cases and Class II dielectrics) and
its schematic review checklist (at least 20% voltage headroom on tantalums, resistor
power and temperature rise, the voltage rating of 0402 and smaller parts). Everything
here is a heuristic from names and text: voltages are nominal (no tolerance, transients
or ripple peaks), ratings can be read wrong, package limits are typical, and the
datasheet decides each finding.
"""

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

try:
    from services.bom import natural_key
    from services.bom_checks import IPC_SIZE, METRIC_SIZE
    from services.decoupling import capacitance, designator_prefix, farads_text, net_kind
    from services.fab_profiles import (
        ProfileError, read_project_json, setting_folder, setting_path_problem,
    )
    from services.power_tree import (
        FILTER_OHMS, LINK_OHMS, SWITCHING_KINDS, build_tree, tree_data,
    )
    from services.pull_resistors import (
        ohms_text, rail_voltage, resistance, resistor_sections, resolve_names,
    )
except ImportError:
    from server.services.bom import natural_key
    from server.services.bom_checks import IPC_SIZE, METRIC_SIZE
    from server.services.decoupling import capacitance, designator_prefix, farads_text, net_kind
    from server.services.fab_profiles import (
        ProfileError, read_project_json, setting_folder, setting_path_problem,
    )
    from server.services.power_tree import (
        FILTER_OHMS, LINK_OHMS, SWITCHING_KINDS, build_tree, tree_data,
    )
    from server.services.pull_resistors import (
        ohms_text, rail_voltage, resistance, resistor_sections, resolve_names,
    )

CHECKS = (
    "over_rated", "ceramic_voltage", "tantalum_voltage", "electrolytic_voltage",
    "capacitor_voltage", "dc_bias", "resistor_voltage", "resistor_power", "needs_current",
    "unknown_rating",
)
_CHECK_RANK = {check: rank for rank, check in enumerate(CHECKS)}
_SEVERITY_RANK = {"warning": 0, "info": 1}
PAGE_LIMIT = 100
UNKNOWN_LISTED = 100  # designators listed in the unknown_rating row
RAILS_LISTED = 12  # rails without a known voltage listed, most capacitors first
POLICY_FILE = "derating_policy.json"
SETTING_VARIABLE = "ALTIUM_COPILOT_DERATING_POLICY"

HEURISTIC = (
    "A heuristic from names and part text: net voltages are nominal (from rails=, net "
    "names and the power tree; no tolerance, transients or ripple peaks), ratings, "
    "dielectrics and sizes are read from parameters, text, part numbers and footprints and "
    "can be wrong or missing, and resistor limits are typical for the size. Confirm each "
    "finding in the part's datasheet (its rating, DC-bias curve, power and working "
    "voltage)."
)
GUIDANCE = (
    "Altium's ceramic capacitor derating article: at least 25% voltage derating as "
    "standard, at least 50% under ripple, DC-bias loss worse in small cases and Class II "
    "dielectrics. Altium's schematic review checklist: at least 20% voltage headroom on "
    "tantalum capacitors, resistor power and temperature rise, the voltage rating of 0402 "
    "and smaller parts."
)

# ---------- policy ----------

CASES = ("01005", "0201", "0402", "0603", "0805", "1206", "1210", "1806", "1808", "1812",
         "1825", "2010", "2220", "2225", "2512", "2920", "1411", "2312", "2917")
_CHIP_CASES = frozenset(CASES[:15])  # the ceramic chip sizes (not the tantalum codes)
_CASE_ORDER = {case: rank for rank, case in enumerate(CASES[:16])}

DEFAULT_POLICY: dict = {
    # Altium's ceramic derating article: at least 25% derating, at least 50% under ripple.
    "ceramic_max_ratio": 0.75,
    "ceramic_ripple_max_ratio": 0.5,
    # Altium's schematic review checklist: at least 20% headroom on tantalums. MnO2
    # tantalums are often run at 50% in practice; KEMET's polymer guidance allows 10%
    # derating above 10 V rated, so 0.8 is the conservative choice for polymer too.
    "tantalum_max_ratio": 0.8,
    "tantalum_polymer_max_ratio": 0.8,
    "electrolytic_max_ratio": 0.8,
    # DC bias: Class II ceramics this small, this large, at this share of their rating.
    "dc_bias_min_ratio": 0.3,
    "dc_bias_max_case": "0603",
    "dc_bias_min_uf": {"01005": 0.01, "0201": 0.1, "0402": 1.0, "0603": 4.7},
    # Typical thick-film chip resistor limits per size (check the datasheet).
    "resistor_max_voltage": {"01005": 15.0, "0201": 25.0, "0402": 50.0, "0603": 75.0,
                             "0805": 150.0, "1206": 200.0, "1210": 200.0, "2010": 200.0,
                             "2512": 200.0},
    "resistor_power_w": {"01005": 0.03125, "0201": 0.05, "0402": 0.0625, "0603": 0.1,
                         "0805": 0.125, "1206": 0.25, "1210": 0.5, "2010": 0.75,
                         "2512": 1.0},
    "resistor_power_derating": 0.5,
    # The highest level assumed for a net a logic output drives (an LED's GPIO).
    "signal_max_v": 5.0,
}
_RATIO_KEYS = frozenset({
    "ceramic_max_ratio", "ceramic_ripple_max_ratio", "tantalum_max_ratio",
    "tantalum_polymer_max_ratio", "electrolytic_max_ratio", "dc_bias_min_ratio",
    "resistor_power_derating",
})
_TABLE_KEYS = frozenset({"dc_bias_min_uf", "resistor_max_voltage", "resistor_power_w"})
_POLICY_FILE_KEYS = ("name", "source", "comment", "policy")
POLICY_HINT = (
    "Show the user what to fix (the key, the value and the reason) and call again once it "
    "is fixed; the defaults are in examples/derating_policy.json."
)


def _policy_error(message: str, file: str | None = None, **extra) -> dict:
    out = {"error": "invalid_policy", "message": message}
    if file:
        out["file"] = file
    out.update(extra)
    out["hint"] = POLICY_HINT
    return out


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _policy_value(key: str, value, current):
    """The validated value, or raise ValueError with the reason (said after the key)."""
    if key in _RATIO_KEYS:
        number = _number(value)
        if number is None or not 0 < number <= 1:
            raise ValueError("must be a ratio between 0 (excluded) and 1, e.g. 0.75")
        return number
    if key == "dc_bias_max_case":
        if not isinstance(value, str) or value.strip() not in _CASE_ORDER:
            raise ValueError(f"must be an imperial case code, one of {list(_CASE_ORDER)[:9]}")
        return value.strip()
    if key == "signal_max_v":
        number = _number(value)
        if number is None or not 0 < number <= 1000:
            raise ValueError("must be volts between 0 (excluded) and 1000")
        return number
    if key in _TABLE_KEYS:
        if not isinstance(value, dict):
            raise ValueError("must be an object of imperial case codes to positive numbers, "
                             'e.g. {"0402": 50}')
        merged = dict(current)
        for case, raw in value.items():
            if case.startswith("_"):
                continue
            number = _number(raw)
            if case not in CASES:
                raise ValueError(f"has '{case[:20]}', which is not an imperial case code "
                                 "(use 0201, 0402, 0603, ...)")
            if number is None or number <= 0:
                raise ValueError(f"must map {case} to a positive number")
            merged[case] = number
        return merged
    raise KeyError(key)


def _apply_policy(policy: dict, raw, origin: str, file: str | None = None
                  ) -> tuple[list[str], dict | None]:
    """Apply raw overrides to policy in place: (keys set, None) or (keys, error)."""
    if raw is None:
        return [], None
    if not isinstance(raw, dict):
        return [], _policy_error(f"{origin}: the policy must be a JSON object of policy "
                                 "keys.", file, valid_keys=sorted(DEFAULT_POLICY))
    applied = []
    for key, value in raw.items():
        if not isinstance(key, str) or key.startswith("_"):
            continue
        if key not in DEFAULT_POLICY:
            return applied, _policy_error(
                f"{origin}: unknown policy key '{str(key)[:60]}'.", file,
                key=str(key)[:60], valid_keys=sorted(DEFAULT_POLICY))
        try:
            policy[key] = _policy_value(key, value, policy[key])
        except ValueError as error:
            return applied, _policy_error(f"{origin}: {key} {error}.", file, key=key)
        applied.append(key)
    return applied, None


def _with_extra(error: dict, extra: dict) -> dict:
    """error with extra keys before its hint."""
    if not extra:
        return error
    details = {key: value for key, value in error.items() if key != "hint"}
    return {**details, **extra, "hint": error["hint"]}


def _policy_file_layer(policy: dict, path: Path, root, label: str, file: str,
                       extra: dict | None = None) -> tuple[dict | None, list[str], dict | None]:
    """Apply a policy file (read like a fab profile: a JSON object with name, source,
    comment and policy) onto policy in place: (data, keys set, None) or (None, [], error).
    label starts the error messages; extra keys go into an error (before its hint)."""
    extra = extra or {}
    try:
        data = read_project_json(path, root)
    except ProfileError as error:
        details = {key: value for key, value in error.error.items()
                   if key in ("line", "column")}
        return None, [], _policy_error(error.error["message"], file, **details, **extra)
    for key in data:
        if not key.startswith("_") and key not in _POLICY_FILE_KEYS:
            return None, [], _policy_error(
                f"{label}: unknown key '{key[:60]}'.", file,
                valid_keys=list(_POLICY_FILE_KEYS), **extra)
    keys, error = _apply_policy(policy, data.get("policy"), label, file)
    if error:
        return None, [], _with_extra(error, extra)
    return data, keys, None


def _file_names(info: dict, data: dict) -> None:
    for key in ("name", "source"):
        if isinstance(data.get(key), str) and data[key].strip():
            info[key] = " ".join(data[key].split())[:120]


def setting_layer(policy: dict, setting: str) -> tuple[dict | None, list[str], dict | None]:
    """Apply the Derating policy setting's file (an absolute path) onto policy:
    (data, keys set, None) or (None, [], an invalid_policy error naming the setting)."""
    text = str(setting).strip()
    path = Path(text)
    extra = {"setting": SETTING_VARIABLE}
    reason = setting_path_problem(path)
    if reason:
        return None, [], _policy_error(
            f"The Derating policy setting ({SETTING_VARIABLE}) names '{text[:200]}', which "
            f"{reason}. Ask the user to fix the setting (Claude Desktop: the extension's "
            "settings).", path.name or None, **extra)
    label = f"{path.name} (the Derating policy setting)"
    return _policy_file_layer(policy, path, setting_folder(path), label, path.name, extra)


def load_policy(root_dir, override=None, setting: str | None = None
                ) -> tuple[dict | None, dict | None]:
    """The effective policy, as (info, None) or (None, error): the defaults, then the
    Derating policy setting's file (setting: its absolute path), then
    derating_policy.json in the project folder (read like a fab profile: a JSON object
    with name, source, comment and policy), then the override argument; each sets single
    keys over the one before. info holds the values, which files were read and which keys
    each layer set; chosen_by joins the layers that applied (default when none)."""
    policy = {key: (dict(value) if isinstance(value, dict) else value)
              for key, value in DEFAULT_POLICY.items()}
    info: dict = {"chosen_by": "default"}
    layers: list[str] = []
    if setting:
        data, keys, error = setting_layer(policy, setting)
        if error:
            return None, error
        info.update({"setting_file": Path(str(setting).strip()).name, "set_by_setting": keys})
        _file_names(info, data)
        layers.append("setting")
    if root_dir is not None:
        path = Path(root_dir) / POLICY_FILE
        try:
            found = path.is_file()
        except OSError:
            found = False
        if found:
            data, keys, error = _policy_file_layer(policy, path, root_dir, POLICY_FILE,
                                                   POLICY_FILE)
            if error:
                return None, error
            info.update({"file": POLICY_FILE, "set_by_file": keys})
            _file_names(info, data)
            layers.append("project_file")
    keys, error = _apply_policy(policy, override, "policy argument")
    if error:
        return None, error
    if keys:
        info["set_by_argument"] = keys
        layers.append("argument")
    info["chosen_by"] = "+".join(layers) or "default"
    info["values"] = policy
    return info, None


def policy_or_defaults(root_dir, setting: str | None, limits: str) -> tuple[dict, list[str]]:
    """load_policy for the tools that read only the resistor limits and never answer a
    policy error (get_circuit_calculations, check_thermal): (info, notes). A Derating
    policy setting whose file cannot be read is left out, with a note naming it: the
    project's derating_policy.json still applies over the defaults. A project file that
    cannot be read leaves the defaults. limits names what the defaults give, for the
    notes ("the default resistor power limits")."""
    notes: list[str] = []
    info, error = load_policy(root_dir, setting=setting)
    if error and setting and error.get("setting"):
        message = " ".join(str(error.get("message", "")).split())
        info, error = load_policy(root_dir)
        if error is None:
            rest = (f"{POLICY_FILE} in the project folder applies over the defaults"
                    if info.get("file") else f"{limits} apply")
            notes.append(f"{message} The setting is left out here: {rest}.")
        else:
            notes.append(f"{message} The setting is left out here.")
    if error:
        info, _ = load_policy(None)
        notes.append(f"{error.get('file') or POLICY_FILE} could not be read "
                     f"({error['message']}): {limits} apply.")
    return info, notes


# ---------- reading values ----------

_SPLIT_PARAM = re.compile(r"[^a-z0-9]+")


def _param_name(name: str) -> str:
    return _SPLIT_PARAM.sub(" ", name.lower().replace("\\", "")).strip()


_VOLTAGE_PARAMS = (
    "voltage rating", "rated voltage", "voltage", "voltage rated", "voltage rating dc",
    "rated voltage dc", "voltage dc", "dc voltage", "rated dc voltage", "dc rated voltage",
    "voltage rating vdc", "rated voltage vdc", "voltage v", "rated voltage v",
    "voltage rating v", "working voltage", "rated working voltage", "max working voltage",
    "maximum working voltage", "voltage max", "max voltage", "vdc", "vrated", "v rated",
    "rating voltage",
)
_DIELECTRIC_PARAMS = (
    "dielectric", "temperature coefficient", "temp coefficient", "tempco",
    "temperature characteristic", "temperature characteristics", "dielectric material",
    "dielectric type", "dielectric characteristic", "type capacitors", "capacitor type",
    "characteristic", "tc", "type", "material", "technology",
)
_TECHNOLOGY_PARAMS = (
    "technology", "capacitor type", "type capacitors", "type", "dielectric",
    "dielectric material", "material", "construction", "family", "series", "category",
    "subcategory", "sub category", "product type", "component type",
)
_POWER_PARAMS = (
    "power", "power rating", "rated power", "power w", "power watts", "power rating w",
    "power rating watts", "wattage", "power max", "max power", "rated dissipation",
    "power dissipation",
)
_CASE_PARAMS = (
    "case package", "package case", "case code imperial", "case code", "case", "package",
    "size", "case size", "size code", "case eia", "eia size", "imperial size", "case code eia",
    "package size", "footprint size", "case code metric", "case metric", "metric size",
    "size metric", "package reference", "packagereference", "package description",
    "packagedescription",
)
_CAPACITANCE_PARAMS = ("capacitance", "capacitance value", "cap", "value capacitance")

# Ratings a capacitor or resistor is made with; a voltage in text is read as a rating only
# when it is one of these (a '3V3' or '12V' rail named in a description is not).
_STANDARD_RATINGS = frozenset({
    2.0, 2.5, 2.7, 3.0, 4.0, 5.5, 6.3, 7.5, 8.0, 10.0, 16.0, 20.0, 25.0, 35.0, 40.0, 42.0,
    50.0, 63.0, 71.0, 75.0, 80.0, 100.0, 125.0, 150.0, 160.0, 200.0, 250.0, 275.0, 300.0,
    305.0, 315.0, 350.0, 400.0, 450.0, 500.0, 550.0, 630.0, 700.0, 750.0, 800.0, 1000.0,
    1200.0, 1500.0, 1600.0, 2000.0, 2500.0, 3000.0, 4000.0, 5000.0, 6000.0,
})
_NEXT_RATINGS = (2.5, 4.0, 6.3, 10.0, 16.0, 25.0, 35.0, 50.0, 63.0, 100.0, 200.0, 250.0,
                 500.0, 630.0, 1000.0, 2000.0, 3000.0)

_VOLT_TEXT = re.compile(
    r"(?<![A-Za-z0-9.,])(\d{1,4}(?:[.,]\d{1,2})?)\s?(k)?\s?V(?:DC|olts?\b)?"
    r"(?P<ac>\s?(?:AC|RMS|~)(?!\s?/\s?DC))?(?![A-Za-z0-9])"
    r"|(?<![A-Za-z0-9.,])(\d{1,2})V(\d{1,2})(?![A-Za-z0-9.])",
    re.IGNORECASE,
)
_BARE_NUMBER = re.compile(r"^\s*(\d{1,4}(?:[.,]\d{1,3})?)\s*$")
_WATT_TEXT = re.compile(
    r"(?<![A-Za-z0-9.,/])(\d{1,4}(?:[.,]\d{1,4})?)\s?(m|k)?W(?![A-Za-z0-9])"
    r"|(?<![A-Za-z0-9.,/])(\d{1,2})\s?/\s?(\d{1,4})\s?W(?![A-Za-z0-9])",
)
_DIELECTRIC_TEXT = re.compile(
    r"(?<![A-Za-z0-9])(C0G|COG|NP0|NPO|U2J|X5R|X5S|X6S|X6T|X7R|X7S|X7T|X7U|X8R|X8L|X8G|"
    r"Y5V|Y5U|Z5U)(?![A-Za-z0-9])", re.IGNORECASE)
_CLASS_ONE = frozenset({"C0G", "U2J", "X8G", "CH"})
_CLASS_THREE = frozenset({"Y5V", "Y5U", "Z5U"})

_TECH_TEXT = (
    ("supercapacitor", re.compile(r"super-?cap|ultra-?cap|double[- ]layer|\bEDLC\b", re.I)),
    ("tantalum_polymer", re.compile(r"polymer\W*(?:\w+\W+){0,2}tantal|tantal\w*\W*(?:\w+\W+)"
                                    r"{0,2}polymer|\bPOSCAP\b|\bKO-?CAP\b", re.I)),
    ("tantalum", re.compile(r"tantal|\btant\b|\btant\.", re.I)),
    ("aluminium_polymer", re.compile(r"polymer|\bOS-?CON\b|\bSP-?CAP\b", re.I)),
    ("aluminium_electrolytic", re.compile(
        r"electrolytic|\belk?o\b|\belco\b|alumin(?:i)?um|\balu\b|\be-?cap\b", re.I)),
    ("film", re.compile(r"\bfilm\b|polyester|polypropylene|polyphenylene|metalli[sz]ed|"
                        r"\bMKT\b|\bMKP\b|\bMKS\b|\bPET\b|\bPPS\b", re.I)),
    ("ceramic", re.compile(r"ceramic|\bMLCC\b|multi-?layer|\bkeramik|\bcer\b", re.I)),
)
_NOT_POLAR = re.compile(r"non-?polar|bi-?polar|\bunpolar|\bNP\b|non-?polari[sz]ed", re.I)
_POLAR = re.compile(r"polari[sz]ed|\bpolar\b|\bCAP\s?POL|\bCPOL\b|\bPOL\s?CAP", re.I)
_CHIP_CAP = re.compile(r"chip\s+cap", re.I)
# IPC-7351 names: CAPAE aluminium electrolytic, CAPPR polarized radial, CAPMP molded
# polarized (a tantalum).
_FOOTPRINT_TECH = (
    ("aluminium_electrolytic", re.compile(r"CAPAE|CAPPR|(?<![A-Za-z])(?:ELEC|ELKO|E-?CAP)",
                                          re.I)),
    ("tantalum", re.compile(r"CAPMP|(?<![A-Za-z])TANT", re.I)),
)

_CASE_TOKEN = re.compile(r"(?<!\d)(\d{4,5})(?!\d)")
_METRIC_MARK = re.compile(r"\s*\)?\s*(?:metric|mm\b|m(?![A-Za-z]))", re.IGNORECASE)
_BODY_MM = re.compile(r"\bbody\s+(\d+(?:\.\d+)?)\s*x\s*(\d+(?:\.\d+)?)\s*mm", re.IGNORECASE)
# Metric codes the shared table (bom_checks.METRIC_SIZE) does not hold: wide ceramics and
# the EIA tantalum case codes.
_METRIC_EXTRA = {"4516": "1806", "4520": "1808", "4564": "1825", "5750": "2220",
                 "5763": "2225", "5025": "2010", "3528": "1411", "6032": "2312",
                 "7343": "2917"}
_METRIC = {**METRIC_SIZE, **_METRIC_EXTRA}


def _metric_case(code: str) -> str | None:
    """Imperial size for a metric code: exact, else within 0.1 mm in each dimension
    (IPC-7351 body sizes such as 2013 for a 2012)."""
    if code in _METRIC:
        return _METRIC[code]
    if len(code) != 4 or not code.isdigit():
        return None
    length, width = int(code[:2]), int(code[2:])
    near = [imperial for metric, imperial in _METRIC.items()
            if abs(int(metric[:2]) - length) <= 1 and abs(int(metric[2:]) - width) <= 1]
    return near[0] if len(set(near)) == 1 else None


def case_in_text(text: str | None, metric: bool = False) -> str | None:
    """The imperial case size in a text: an IPC-7351 name (RESC1005X40: metric), a code
    marked metric ('1005 Metric', '1608M') or metric-only (1608, 3216), or an imperial
    code (0402, 0603 unmarked are imperial). metric=True reads unmarked codes as metric (a
    'Case Code (Metric)' parameter)."""
    if not text:
        return None
    ipc = IPC_SIZE.search(text)
    if ipc:
        return _metric_case(ipc[1])
    for match in _CASE_TOKEN.finditer(text):
        code = match[1]
        marked = metric or bool(_METRIC_MARK.match(text, match.end()))
        if marked:
            found = _metric_case(code)
            if found:
                return found
            continue
        if code in CASES:
            return code
        if code in _METRIC and code not in CASES:
            return _METRIC[code]
    body = _BODY_MM.search(text)
    if body:
        code = f"{round(float(body[1]) * 10):02d}{round(float(body[2]) * 10):02d}"
        return _metric_case(code)
    return None


def _volt_matches(text: str | None, standard: bool):
    """(volts, AC, where) of each voltage in a text."""
    for match in _VOLT_TEXT.finditer(text or ""):
        if match.group(1):
            value = float(match.group(1).replace(",", "."))
            if match.group(2):
                value *= 1000
            ac = bool(match.group("ac"))
        else:
            value = float(f"{match.group(4)}.{match.group(5)}")
            ac = False
        if value <= 0 or (standard and value not in _STANDARD_RATINGS):
            continue
        yield value, ac, match.start()


def volts_in_text(text: str | None, standard: bool = True) -> tuple[float | None, bool]:
    """(volts, AC) of the first voltage in a text: '25V', '6.3 VDC', '6V3', '1kV',
    '275VAC'. standard=True keeps only standard ratings (not a '3V3' rail)."""
    for value, ac, _ in _volt_matches(text, standard):
        return value, ac
    return None, False


def text_ratings(text: str | None) -> tuple[list[float], float | None]:
    """The DC ratings in a text (standard ratings only) and its first AC rating. Several
    DC ratings narrow to those in the comma-separated part of the text that also gives
    a capacitance, dielectric, case or power ('Decoupling for 5.5V supply, CAP 100nF 50V
    X7R': 50 V); when several are left, all are returned (a conflict)."""
    found = list(_volt_matches(text, True))
    ac = next((value for value, is_ac, _ in found if is_ac), None)
    dc = [(value, start) for value, is_ac, start in found if not is_ac]
    if len({value for value, _ in dc}) > 1:
        spans, at = [], 0
        for piece in re.split(r"([,;|])", text or ""):
            spans.append((at, at + len(piece), piece))
            at += len(piece)

        def specified(start: int) -> bool:
            piece = next(piece for begin, end, piece in spans if begin <= start < end)
            return bool(capacitance(piece) or dielectric_in_text(piece) or
                        case_in_text(piece) or watts_in_text(piece))

        dc = [item for item in dc if specified(item[1])] or dc
    return list(dict.fromkeys(value for value, _ in dc)), ac


def _param_volts(text: str) -> tuple[float | None, bool]:
    """A voltage parameter: '6.3 V', '25', '50VDC', '1 kV', '6V3'."""
    bare = _BARE_NUMBER.match(text or "")
    if bare:
        value = float(bare[1].replace(",", "."))
        return (value, False) if value > 0 else (None, False)
    return volts_in_text(text, standard=False)


def watts_in_text(text: str | None, bare: bool = False) -> float | None:
    """Watts from '100 mW', '0.05W', '1/16W'; with bare=True a plain number up to 5 is
    watts (a 'Power' parameter of 0.1)."""
    if bare:
        plain = _BARE_NUMBER.match(text or "")
        if plain:
            value = float(plain[1].replace(",", "."))
            return value if 0 < value <= 5 else None
    for match in _WATT_TEXT.finditer(text or ""):
        if match.group(1):
            value = float(match.group(1).replace(",", "."))
            value *= {"m": 1e-3, "k": 1e3}.get(match.group(2) or "", 1.0)
        else:
            denominator = int(match.group(4))
            if not denominator:
                continue
            value = int(match.group(3)) / denominator
        if 0 < value <= 1000:
            return value
    return None


def dielectric_in_text(text: str | None) -> str | None:
    match = _DIELECTRIC_TEXT.search(text or "")
    if not match:
        return None
    code = match[1].upper()
    return {"COG": "C0G", "NP0": "C0G", "NPO": "C0G"}.get(code, code)


def technology_in_text(text: str | None) -> str | None:
    """ceramic, tantalum, tantalum_polymer, aluminium_electrolytic, aluminium_polymer,
    film, supercapacitor, polarized (a polar capacitor of no stated kind) or None."""
    text = text or ""
    for kind, pattern in _TECH_TEXT:
        if pattern.search(text):
            return kind
    if _POLAR.search(text) and not _NOT_POLAR.search(text):
        return "polarized"
    if _CHIP_CAP.search(text):
        return "ceramic"
    return None


def dielectric_class(code: str | None) -> int | None:
    if not code:
        return None
    if code in _CLASS_ONE:
        return 1
    return 3 if code in _CLASS_THREE else 2


# ---------- part numbers (manufacturers' published part-numbering schemes) ----------

_PF = r"(\d{3}|\dR\d|R\d{2})"


def _pf_code(code: str) -> float | None:
    """Farads from an EIA capacitance code: 104 = 100 nF, 4R7 = 4.7 pF, R50 = 0.5 pF
    (a third digit 8 or 9 scales by 0.01 or 0.1)."""
    if "R" in code:
        return float(code.replace("R", ".")) * 1e-12
    if not code.isdigit():
        return None
    exponent = int(code[2])
    scale = {8: 0.01, 9: 0.1}.get(exponent, 10 ** exponent)
    return int(code[:2]) * scale * 1e-12


# EIA/JIS two-character rated-voltage codes (Murata, TDK).
_JIS_VOLTS = {
    "0E": 2.5, "0G": 4.0, "0J": 6.3, "1A": 10.0, "1C": 16.0, "1E": 25.0, "1V": 35.0,
    "YA": 35.0, "1H": 50.0, "1J": 63.0, "1K": 80.0, "2A": 100.0, "2D": 200.0, "2E": 250.0,
    "2V": 350.0, "2W": 450.0, "2H": 500.0, "2J": 630.0, "3A": 1000.0, "3D": 2000.0,
    "3F": 3150.0,
}
_MURATA = re.compile(r"^(G(?:RM|CM|RT|CJ|JM|QM|CD|CE|CG|XT|MA|MD|RJ))(\d{2})([0-9A-Z])"
                     rf"([0-9A-Z]{{2}})([0-9][A-Z]|YA){_PF}([A-Z])")
_MURATA_SIZE = {"02": "01005", "03": "0201", "15": "0402", "18": "0603", "21": "0805",
                "31": "1206", "32": "1210", "43": "1812", "55": "2220"}
_MURATA_TC = {"5C": "C0G", "7U": "U2J", "R6": "X5R", "R7": "X7R", "C6": "X5S", "C7": "X7S",
              "C8": "X6S", "D7": "X7T", "D8": "X6T", "E7": "X7U", "F5": "Y5V", "L8": "X8L"}
_EIA_TC = r"(C0G|NP0|X5R|X5S|X6S|X7R|X7S|X7T|X8R|X8L|Y5V|JB|CH)"
_TDK = re.compile(rf"^C(0402|0603|1005|1608|2012|3216|3225|4532|5750){_EIA_TC}([0-9][A-Z])"
                  rf"{_PF}([A-Z])")
_TDK_AUTO = re.compile(rf"^C(GA|GB|GJ|KG|NC|EU)(\d)([A-Z])(\d){_EIA_TC}([0-9][A-Z]){_PF}"
                       r"([A-Z])")
_TDK_SIZE = {"1": "0201", "2": "0402", "3": "0603", "4": "0805", "5": "1206", "6": "1210",
             "8": "1812", "9": "2220"}
_SAMSUNG = re.compile(rf"^CL(03|05|10|21|31|32|43|55)([A-Z]){_PF}([A-Z])([A-Z])")
_SAMSUNG_SIZE = {"03": "0201", "05": "0402", "10": "0603", "21": "0805", "31": "1206",
                 "32": "1210", "43": "1812", "55": "2220"}
_SAMSUNG_TC = {"C": "C0G", "A": "X5R", "B": "X7R", "F": "Y5V", "X": "X6S", "Y": "X7S",
               "Z": "X7T", "G": "X8G"}
_SAMSUNG_VOLTS = {"S": 2.5, "R": 4.0, "Q": 6.3, "P": 10.0, "O": 16.0, "A": 25.0, "L": 35.0,
                  "B": 50.0, "T": 75.0, "C": 100.0, "D": 200.0, "E": 250.0, "G": 500.0,
                  "H": 630.0, "I": 1000.0, "X": 1250.0, "V": 1500.0, "J": 2000.0,
                  "K": 3000.0}
_KEMET = re.compile(rf"^C(0201|0402|0603|0805|1206|1210|1808|1812|1825|2220|2225)C{_PF}"
                    r"([A-Z])([0-9A])([A-Z])")
_KEMET_VOLTS = {"7": 4.0, "9": 6.3, "8": 10.0, "4": 16.0, "3": 25.0, "6": 35.0, "5": 50.0,
                "1": 100.0, "2": 200.0, "A": 250.0}
_KEMET_TC = {"G": "C0G", "R": "X7R", "P": "X5R", "U": "Z5U", "V": "Y5V"}
_YAGEO = re.compile(r"^[CA]C(0201|0402|0603|0805|1206|1210|1812|2220)([A-Z])([A-Z])"
                    rf"(NPO|NP0|X5R|X7R|X7S|X6S|X8R|Y5V|Z5U)([0-9A-Z])([A-Z]{{2}}){_PF}")
_YAGEO_VOLTS = {"5": 6.3, "6": 10.0, "7": 16.0, "8": 25.0, "9": 50.0, "0": 100.0}
_AVX = re.compile(rf"^(0201|0402|0603|0805|1206|1210|1812|2220)([0-9A-Z])([A-Z]){_PF}"
                  r"([A-Z])([A-Z0-9])([A-Z0-9])")
_AVX_VOLTS = {"4": 4.0, "6": 6.3, "Z": 10.0, "Y": 16.0, "3": 25.0, "D": 35.0, "5": 50.0,
              "1": 100.0, "2": 200.0, "7": 500.0}
_AVX_TC = {"A": "C0G", "C": "X7R", "D": "X5R", "G": "Y5V"}
_TAIYO = re.compile(r"^([PAJLETGUH])MK(063|105|107|212|316|325|432)([A-Z]?)(BJ|B7|C7|CG)"
                    rf"{_PF}([A-Z])")
_TAIYO_VOLTS = {"P": 2.5, "A": 4.0, "J": 6.3, "L": 10.0, "E": 16.0, "T": 25.0, "G": 35.0,
                "U": 50.0, "H": 100.0}
_TAIYO_SIZE = {"063": "0201", "105": "0402", "107": "0603", "212": "0805", "316": "1206",
               "325": "1210", "432": "1812"}
_TAIYO_TC = {"BJ": "X5R", "B7": "X7R", "C7": "X7S", "CG": "C0G"}
_WALSIN = re.compile(rf"^(0201|0402|0603|0805|1206|1210|1812)([NBXF]){_PF}([A-Z])"
                     r"(\dR\d|\d{3})C")
_WALSIN_TC = {"N": "C0G", "B": "X7R", "X": "X5R", "F": "Y5V"}
_KEMET_TANTALUM = re.compile(rf"^T(49\d|5[0-4]\d)([A-Z]){_PF}([A-Z])(\d{{3}}|\dR\d)")
_AVX_TANTALUM = re.compile(r"^(TAJ|TPS|TPM|TPC|TAC|TBJ|TBM|TRJ|THJ|TCJ|TCM|TCN|TCQ|TCR|TCO)"
                           rf"([A-Z]){_PF}([A-Z])(\d{{3}}|\dR\d)")
# Panasonic POSCAP: rated voltage (6 is 6.3 V, 2R5 2.5 V), T and the series (TPE, TPF,
# TQC ...), the capacitance in uF, M.
_POSCAP = re.compile(r"^(\dR\d|\d{1,2})(T[PQ][A-Z]{1,2})(\d{2,4})M")
# Resistors: the size, and whether the series is rated above the size's typical power.
_YAGEO_R = re.compile(r"^(?:RC|AC|RT)(01005|0201|0402|0603|0805|1206|1210|1218|2010|2512)"
                      r"[A-Z]")
_VISHAY_R = re.compile(r"^CRCW(0201|0402|0603|0805|1206|1210|1218|2010|2512)")
_ERJ = re.compile(r"^ERJ-?(XG|1G|1T|2G|2R|2B|3G|3E|3R|3B|6G|6E|6R|6B|8G|8E|8R|8B|14|12Z|12|"
                  r"PA2|PA3|PA6|PA8|P03|P06|P08|U02|U03|U06|U08)")
_ERJ_SIZE = {"XG": "01005", "1G": "0201", "1T": "2512", "14": "1210", "12Z": "2010",
             "12": "1812"}
_STACKPOLE = re.compile(r"^RMC([FP])(0201|0402|0603|0805|1206|1210|2010|2512)")


def _walsin_volts(code: str) -> float:
    if "R" in code:
        return float(code.replace("R", "."))
    return int(code[:2]) * 10 ** int(code[2])


def _tantalum_volts(code: str) -> float:
    if "R" in code:
        return float(code.replace("R", "."))
    value = int(code)
    return 6.3 if value == 6 else float(value)


def _ceramic(family, case, dielectric, volts, farads) -> dict:
    out = {"family": family, "technology": "ceramic", "case": case,
           "dielectric": dielectric, "rated_v": volts, "farads": farads}
    return {key: value for key, value in out.items() if value is not None}


def decode_capacitor_mpn(text: str | None) -> dict | None:
    """Rating, dielectric, case, capacitance and technology from a capacitor part number
    of a family whose part-numbering scheme is published; None when none matches."""
    mpn = re.sub(r"\s+", "", (text or "").upper())
    if len(mpn) < 8:
        return None
    if match := _MURATA.match(mpn):
        return _ceramic(f"Murata {match[1]}", _MURATA_SIZE.get(match[2]),
                        _MURATA_TC.get(match[4]), _JIS_VOLTS.get(match[5]), _pf_code(match[6]))
    if match := _TDK.match(mpn):
        return _ceramic("TDK C", _metric_case(match[1]), _eia(match[2]),
                        _JIS_VOLTS.get(match[3]), _pf_code(match[4]))
    if match := _TDK_AUTO.match(mpn):
        return _ceramic(f"TDK C{match[1]}", _TDK_SIZE.get(match[2]), _eia(match[5]),
                        _JIS_VOLTS.get(match[6]), _pf_code(match[7]))
    if (match := _SAMSUNG.match(mpn)) and match[5] in _SAMSUNG_VOLTS:
        return _ceramic("Samsung CL", _SAMSUNG_SIZE[match[1]], _SAMSUNG_TC.get(match[2]),
                        _SAMSUNG_VOLTS[match[5]], _pf_code(match[3]))
    if (match := _KEMET.match(mpn)) and match[4] in _KEMET_VOLTS:
        return _ceramic("KEMET C", match[1], _KEMET_TC.get(match[5]), _KEMET_VOLTS[match[4]],
                        _pf_code(match[2]))
    if match := _YAGEO.match(mpn):
        return _ceramic("Yageo CC", match[1], _eia(match[4]), _YAGEO_VOLTS.get(match[5]),
                        _pf_code(match[7]))
    if (match := _AVX.match(mpn)) and match[2] in _AVX_VOLTS and match[3] in _AVX_TC:
        return _ceramic("AVX/Kyocera", match[1], _AVX_TC[match[3]], _AVX_VOLTS[match[2]],
                        _pf_code(match[4]))
    if match := _TAIYO.match(mpn):
        return _ceramic("Taiyo Yuden", _TAIYO_SIZE[match[2]], _TAIYO_TC[match[4]],
                        _TAIYO_VOLTS[match[1]], _pf_code(match[5]))
    if match := _WALSIN.match(mpn):
        return _ceramic("Walsin", match[1], _WALSIN_TC[match[2]], _walsin_volts(match[5]),
                        _pf_code(match[3]))
    if match := _KEMET_TANTALUM.match(mpn):
        polymer = not match[1].startswith(("49", "51"))
        return {"family": f"KEMET T{match[1]}",
                "technology": "tantalum_polymer" if polymer else "tantalum",
                "rated_v": _tantalum_volts(match[5]), "farads": _pf_code(match[3])}
    if match := _AVX_TANTALUM.match(mpn):
        polymer = match[1].startswith("TC")
        return {"family": f"AVX {match[1]}",
                "technology": "tantalum_polymer" if polymer else "tantalum",
                "rated_v": _tantalum_volts(match[5]), "farads": _pf_code(match[3])}
    if match := _POSCAP.match(mpn):
        # The capacitance is in uF here (330 = 330 uF), not the EIA pF code.
        volts = 6.3 if match[1] == "6" else float(match[1].replace("R", "."))
        return {"family": f"Panasonic POSCAP {match[2]}", "technology": "tantalum_polymer",
                "rated_v": volts, "farads": int(match[3]) * 1e-6}
    return None


def _eia(code: str) -> str:
    return {"NP0": "C0G", "NPO": "C0G"}.get(code, code)


def decode_resistor_mpn(text: str | None) -> dict | None:
    """Size (and whether the series is a high-power one, whose rating is above the
    size's typical) from a chip-resistor part number: Yageo RC/AC/RT, Vishay CRCW,
    Panasonic ERJ, Stackpole RMCF/RMCP."""
    mpn = re.sub(r"\s+", "", (text or "").upper())
    if len(mpn) < 6:
        return None
    if match := _YAGEO_R.match(mpn):
        return {"family": "Yageo " + mpn[:2], "case": match[1]}
    if match := _VISHAY_R.match(mpn):
        # The HP suffix is Vishay's high-power CRCW-HP series.
        return {"family": "Vishay CRCW", "case": match[1],
                **({"high_power": True} if mpn.endswith("HP") else {})}
    if match := _ERJ.match(mpn):
        code = match[1]
        case = _ERJ_SIZE.get(code) or {"2": "0402", "3": "0603", "6": "0805", "8": "1206"}[
            code[-1] if code[0] in "PU" else code[0]]
        high = code.startswith("P") or code.endswith("B")
        return {"family": "Panasonic ERJ", "case": case, **({"high_power": True} if high
                                                            else {})}
    if match := _STACKPOLE.match(mpn):
        return {"family": f"Stackpole RMC{match[1]}", "case": match[2],
                **({"high_power": True} if match[1] == "P" else {})}
    return None


# ---------- the parts ----------

@dataclass
class Field:
    value: object
    source: str  # parameter, text, mpn, footprint, inferred, package_typical
    detail: str | None = None  # the parameter name, the text field or the MPN family

    def describe(self) -> str:
        return f"{self.source} ({self.detail})" if self.detail else self.source


@dataclass
class Part:
    ref: str
    kind: str  # capacitor, resistor
    sections: list[tuple[str, str]]
    fields: dict[str, Field] = field(default_factory=dict)
    conflicts: dict[str, dict] = field(default_factory=dict)
    candidates: dict[str, list[Field]] = field(default_factory=dict)
    rated_ac: float | None = None
    ohms: float | None = None
    value: str | None = None
    # What the fields were read from, for other readers (circuit_calcs' tolerance):
    # normalised parameter name -> (raw name, value), (label, text) and part numbers.
    params: dict[str, tuple[str, str]] = field(default_factory=dict)
    texts: list[tuple[str, str]] = field(default_factory=list)
    mpns: list[str] = field(default_factory=list)


def merged_params(meta: dict, plan, ref: str) -> dict[str, tuple[str, str]]:
    """normalised name -> (raw name, value), the variant's parameter variations applied."""
    params = dict(meta.get("params") or {})
    for key, (_, value) in ((getattr(plan, "params", {}) or {}).get(ref) or {}).items():
        params[key] = value
    out: dict[str, tuple[str, str]] = {}
    for name, value in params.items():
        if isinstance(value, str) and value.strip() and value.strip() not in ("*", "-"):
            out.setdefault(_param_name(name), (name, value.strip()))
    return out


# A technology read from text that a later source may narrow: 'Tantalum Capacitor' with a
# polymer series number, a 'Polar Capacitor' with a tantalum footprint.
_REFINES = {
    "tantalum": frozenset({"tantalum_polymer"}),
    "aluminium_electrolytic": frozenset({"aluminium_polymer"}),
    "polarized": frozenset({"tantalum", "tantalum_polymer", "aluminium_electrolytic",
                            "aluminium_polymer"}),
}


# Which kind of source settles a tie: a parameter, then a part number decoded by its maker's
# published scheme, then free text, then the footprint name.
_SOURCE_RANK = {"parameter": 0, "mpn": 1, "text": 2, "footprint": 3}


def _add(part: Part, name: str, value, source: str, detail: str | None = None) -> None:
    if value is not None:
        part.candidates.setdefault(name, []).append(Field(value, source, detail))


def _same(a, b) -> bool:
    if isinstance(a, float) and isinstance(b, float):
        return abs(a - b) <= 1e-9 * max(1.0, abs(a), abs(b))
    return a == b


def _resolve(part: Part) -> None:
    """Each field from its candidates (in source order: parameters, text, part number,
    footprint). The first wins unless sources disagree: then the value the most kinds of
    source agree on wins (a parameter that the text and the part number contradict
    loses); on a tie the better kind of source (a parameter, then a part number decoded
    by its maker's scheme, then free text, then the footprint), and every candidate is
    kept in conflicts. A technology a later source narrows ('Tantalum', then a polymer
    series) takes the narrower one."""
    for name, items in part.candidates.items():
        if name == "technology":
            chosen, disagree = items[0], False
            for item in items[1:]:
                if item.value in _REFINES.get(chosen.value, ()):
                    chosen = Field(item.value, item.source,
                                   f"{item.detail + ', ' if item.detail else ''}narrowing "
                                   f"{chosen.describe()}")
                elif chosen.value not in _REFINES.get(item.value, ()) and \
                        item.value != chosen.value:
                    disagree = True
            part.fields[name] = chosen
            if disagree:
                part.conflicts[name] = {item.describe(): item.value for item in items}
            continue
        groups: list[list[Field]] = []
        for item in items:
            group = next((group for group in groups if _same(group[0].value, item.value)), None)
            if group is None:
                groups.append([item])
            else:
                group.append(item)
        if len(groups) == 1:
            part.fields[name] = items[0]
            continue
        best = max(range(len(groups)),
                   key=lambda index: (len({item.source for item in groups[index]}),
                                      -min(_SOURCE_RANK[item.source] for item in groups[index]),
                                      -index))
        part.fields[name] = groups[best][0]
        part.conflicts[name] = {item.describe(): item.value for item in items}


def _read_part(part: Part, params: dict, texts: list[tuple[str, str]], mpns: list[str],
               footprint: str | None) -> None:
    """A part's fields from parameters, then texts, then part numbers, then the
    footprint (see _resolve for how disagreeing sources settle)."""
    cap = part.kind == "capacitor"
    # Parameters.
    for name in _VOLTAGE_PARAMS:
        if name in params:
            volts, ac = _param_volts(params[name][1])
            if volts is not None and ac:
                part.rated_ac = part.rated_ac or volts
            elif volts is not None:
                _add(part, "rated_v", volts, "parameter", params[name][0])
    for raw_name, raw in params.values():
        if re.search(r"\bac\b", _param_name(raw_name)) and "volt" in raw_name.lower():
            volts, _ = _param_volts(raw)
            part.rated_ac = part.rated_ac or volts
    if cap:
        for name in _DIELECTRIC_PARAMS:
            if name in params:
                _add(part, "dielectric", dielectric_in_text(params[name][1]), "parameter",
                     params[name][0])
        for name in _TECHNOLOGY_PARAMS:
            if name in params and (tech := technology_in_text(params[name][1])):
                _add(part, "technology", tech, "parameter", params[name][0])
                break
        for name in _CAPACITANCE_PARAMS:
            if name in params:
                _add(part, "farads", capacitance(params[name][1]), "parameter",
                     params[name][0])
    else:
        for name in _POWER_PARAMS:
            if name in params:
                _add(part, "power_w", watts_in_text(params[name][1], bare=True), "parameter",
                     params[name][0])
    for name in _CASE_PARAMS:
        if name in params:
            _add(part, "case", case_in_text(params[name][1], metric="metric" in name),
                 "parameter", params[name][0])
    # Value, comment and description text.
    for label, text in texts:
        ratings, ac = text_ratings(text)
        if ac is not None:
            part.rated_ac = part.rated_ac or ac
        for volts in ratings:
            _add(part, "rated_v", volts, "text", label)
        _add(part, "case", case_in_text(text), "text", label)
        if cap:
            _add(part, "dielectric", dielectric_in_text(text), "text", label)
            _add(part, "farads", capacitance(text), "text", label)
            _add(part, "technology", technology_in_text(text), "text", label)
        else:
            _add(part, "power_w", watts_in_text(text), "text", label)
    # Part numbers.
    for text in mpns:
        decoded = decode_capacitor_mpn(text) if cap else decode_resistor_mpn(text)
        if not decoded:
            continue
        family = decoded["family"]
        for name in ("rated_v", "dielectric", "farads", "case", "technology"):
            _add(part, name, decoded.get(name), "mpn", family)
        if decoded.get("high_power"):
            part.fields["high_power"] = Field(True, "mpn", family)
        break
    # The footprint name.
    _add(part, "case", case_in_text(footprint), "footprint", None)
    if cap:
        for kind, pattern in _FOOTPRINT_TECH:
            if pattern.search(footprint or ""):
                _add(part, "technology", kind, "footprint")
                break
    _resolve(part)
    if cap and "technology" not in part.fields:
        if part.fields.get("dielectric") is not None:
            part.fields["technology"] = Field("ceramic", "inferred", "a ceramic dielectric")
        elif part.fields.get("case") and part.fields["case"].value in _CHIP_CASES:
            part.fields["technology"] = Field("ceramic", "inferred",
                                              "a chip case of no stated kind")


def read_parts(tree, netlist: dict, plan) -> tuple[list[Part], list[str], Counter]:
    """Fitted capacitors and resistors with their fields, and fitted LEDs (with counts of
    the parts left out)."""
    components = netlist.get("components") or {}
    caps_and_resistors: list[Part] = []
    leds: list[str] = []
    stats: Counter = Counter()
    for ref in sorted(tree.design.part_pins, key=natural_key):
        kind = tree.kind(ref)
        if kind not in ("capacitor", "resistor", "diode"):
            continue
        if not tree.fitted(ref):
            stats[f"not_fitted_{kind}"] += 1
            continue
        connected = tree.design.part_pins.get(ref) or {}
        meta = components.get(ref) or {}
        if kind == "diode":
            if is_led(tree, ref):
                leds.append(ref)
            continue
        bom = tree.parts.get(ref)
        nets = list(dict.fromkeys(connected.values()))
        if len(nets) < 2:
            continue
        sections = _sections(tree, ref, kind, meta, connected, nets)
        if not sections:
            stats[f"{kind}_sections_unread"] += 1
            continue
        part = Part(ref, kind, sections)
        params = merged_params(meta, plan, ref)
        texts = [(label, text) for label, text in (
            ("value", bom.value if bom else meta.get("value")),
            ("comment", bom.comment if bom else None),
            ("description", bom.description if bom else meta.get("description")),
        ) if text]
        mpns = [text for text in dict.fromkeys((
            bom.mpn if bom else meta.get("mpn"), bom.comment if bom else None,
            bom.value if bom else None)) if text]
        description = (bom.description if bom else meta.get("description")) or ""
        mpns += [token for token in re.split(r"[\s,;()]+", description)
                 if len(token) >= 8 and any(ch.isdigit() for ch in token)]
        footprint = bom.footprint if bom else meta.get("footprint")
        _read_part(part, params, texts, mpns, footprint)
        part.params, part.texts, part.mpns = params, texts, mpns
        if kind == "resistor":
            part.ohms = resistance(bom.value if bom else meta.get("value"),
                                   bom.comment if bom else None,
                                   params.get("resistance", (None, None))[1],
                                   bom.description if bom else meta.get("description"))
            part.value = ohms_text(part.ohms)
        else:
            farads = part.fields.get("farads")
            part.value = farads_text(farads.value) if farads else None
        caps_and_resistors.append(part)
    return caps_and_resistors, leds, stats


_LED_TEXT = re.compile(r"\bLEDs?\b|light[- ]?emitting", re.I)


def is_led(tree, ref: str) -> bool:
    return designator_prefix(ref) in ("LED", "DS") or bool(_LED_TEXT.search(tree.text(ref)))


_ISOLATED_TEXT = re.compile(r"\bisolated\b", re.I)
_BUSSED_TEXT = re.compile(r"\bbuss?ed\b|\bcommon\b", re.I)


def _sections(tree, ref: str, kind: str, meta: dict, connected: dict, nets: list[str]
              ) -> list[tuple[str, str]]:
    """The (net, net) pairs a part's elements sit between: a two-net part; a resistor
    array of numbered pins as an isolated one (pins 1 and N, 2 and N-1 paired), also with
    one element on ground, unless its text says bussed or its one ground pin is an end
    pin (a common pin); else a part with a ground pin (a feed-through, an array to a common
    ground: each other net to ground). A bussed array not on ground is not read."""
    if len(nets) == 2:
        return [(nets[0], nets[1])]
    grounds = [net for net in nets if net in tree.grounds]
    sections = resistor_sections(meta, connected)
    bussed = bool(sections) and sections[0][0] == "common"
    paired = [] if bussed else [(net_a, net_b) for _, net_a, _, net_b, _ in sections
                                if net_a != net_b]
    if grounds and paired and kind == "resistor":
        text = tree.text(ref)
        ground_pins = [pin for pin, net in connected.items() if net in tree.grounds]
        ends = {"1", str(len(meta.get("pin_designators") or connected))}
        if _ISOLATED_TEXT.search(text) or (
                not _BUSSED_TEXT.search(text) and len(ground_pins) == 1
                and ground_pins[0] not in ends):
            return paired
    if grounds:
        return [(net, grounds[0]) for net in nets if net not in tree.grounds]
    return paired


# ---------- net voltages ----------

@dataclass
class Volt:
    volts: float
    source: str  # user, ground, name, power_tree, divider
    confidence: str  # high, medium, low
    basis: str

    def describe(self, net: str) -> str:
        return f"{net} {_v(self.volts)} V ({self.source})"


# Power-tree edges a net's voltage carries across unchanged (net_voltages joins through them).
SAME_VOLTAGE_EDGES = frozenset({"0_ohm", "shunt", "ferrite", "inductor", "fuse",
                                "common_mode_choke"})
_NEGATIVE_TOKEN = re.compile(r"^[NM]\d+(?:V\d*|P\d+V?)$")
_NEGATIVE_WORDS = frozenset({"NEG", "VNEG", "VEE", "MINUS", "VMINUS", "NEGATIVE"})
# VDD33, VCC18: volts and tenths by convention (pull_resistors.rail_voltage reads only the
# usual ones), so the reading is less sure than a 3V3. A VDD12 or VCC15 may as well be the
# 12 V or 15 V of an analog supply as 1.2 V or 1.5 V: left unknown, for the user to give.
_TENTHS_TOKEN = re.compile(r"(?:VDD|VCC|VIO|VCCIO|VDDIO|DVDD|AVDD)(\d\d)")
_WHOLE_OR_TENTHS = frozenset({"12", "15"})
# A name token that is a voltage as a whole: 3V3, 12V, +5V, P3V3, VDD3V3, 0V95OUT, 3.3V,
# 1.8V, 3P3, P1P8V, VDD33. Not a frequency (32.768K) or a pin-bank name (A10VCCAPLL).
_VOLTAGE_TOKEN = re.compile(
    r"[+-]?[A-Z]{0,8}?\d{1,2}V\d{0,3}[A-Z]{0,4}"
    r"|[+-]?[A-Z]{0,8}?\d{1,2}[.,]\d{1,3}V[A-Z]{0,4}"
    r"|[+-]?[A-Z]{0,8}?\d{1,2}P\d{1,3}(?:V[A-Z]{0,4})?"
    rf"|{_TENTHS_TOKEN.pattern}"
)
# Above rail_voltage's 60 V: 72V, HV_400V, VBUS_100V.
_HIGH_VOLTAGE_TOKEN = re.compile(r"\+?(\d{2,4})V(?:DC)?")
# An AC net (24VAC, 230V_AC, MAINS_L): its name gives no DC voltage.
_AC_TOKEN = re.compile(r"AC|VAC|RMS|MAINS|\d+V?(?:AC|RMS)")
# The nets of a converter's own pins named after the rail they serve (BST_5V, SS_3V3,
# COMP_1V8, GATE_12V, VFB_1V8) or a divided-down copy of it (12V_DIV): not that voltage.
_CONTROL_TOKENS = frozenset({
    "BST", "VBST", "BOOT", "CBOOT", "SS", "TRK", "TRACK", "COMP", "ITH", "ILIM", "ISET",
    "OCSET", "GATE", "HG", "LG", "GH", "GL", "VFB", "DIV",
})
# Converter pins whose nets are not rails even when named after one (power_tree's pin kinds).
_CONTROL_PIN_KINDS = ("bst", "ss", "comp", "ilim", "gate", "fb", "mode")


def _name_tokens(net: str) -> list[str]:
    text = net.strip().replace("\\", "").upper().lstrip("-")
    return [token for token in re.split(r"[_\s/]+", text) if token]


def _control_name(net: str) -> bool:
    """A converter's control net by its name (BST_5V, SS_3V3, 12V_DIV)."""
    return any(token.strip("+-") in _CONTROL_TOKENS for token in _name_tokens(net))


def name_voltage(net: str) -> tuple[float, bool] | None:
    """(volts, by convention) of the voltage in a net's name (rail_voltage's reading of a
    token that is a voltage as a whole, or 61 to 9999 V such as HV_400V), with its sign:
    '-12V', 'N5V', 'M3V3' and 'VNEG_5V' are negative. by convention is True for VDD33-style
    names. None for names that only hold digits beside a V (A10VCCAPLL), a frequency
    (32.768K), an AC net (24VAC), a converter's control net (BST_5V, SS_3V3) or a VDD12 or
    VCC15 (1.2 V or 12 V?)."""
    text = net.strip().replace("\\", "").upper()
    tokens = _name_tokens(net)
    if any(_AC_TOKEN.fullmatch(token) for token in tokens) or _control_name(net):
        return None
    negative = text.startswith("-") or any(token in _NEGATIVE_WORDS for token in tokens)
    for token in tokens:
        bare = token.lstrip("+")
        tenths = _TENTHS_TOKEN.fullmatch(bare)
        if tenths and tenths[1] in _WHOLE_OR_TENTHS:
            continue
        volts = rail_voltage(bare) if _VOLTAGE_TOKEN.fullmatch(token) else None
        if volts is None and (high := _HIGH_VOLTAGE_TOKEN.fullmatch(token)):
            volts = float(high[1]) if 60 < int(high[1]) <= 9999 else None
        if volts is None:
            continue
        if negative or _NEGATIVE_TOKEN.match(bare):
            volts = -volts
        return volts, bool(tenths)
    return None


def signed_rail_voltage(net: str) -> float | None:
    """The signed voltage in a net's name (see name_voltage), or None."""
    found = name_voltage(net)
    return found[0] if found else None


def parse_rail_map(text: str | None) -> tuple[dict[str, float] | None, dict | None]:
    """'VIN=24,VBUS=5' -> {'VIN': 24.0, 'VBUS': 5.0}; names listed before a value share
    it ('VIN,VIN_F=24'); '=' or ':', an optional V and a sign. (None, None) when empty."""
    error = {"error": "invalid_rails",
             "message": "rails takes net voltages, e.g. 'VIN=24,VBUS=5' or 'VIN,VIN_F=24' "
                        "(names before a value share it), in volts between -1000 and 1000."}
    if text is None or not text.strip():
        return None, None
    out: dict[str, float] = {}
    pending: list[str] = []
    for item in re.split(r"[,;]", text):
        item = item.strip()
        if not item:
            continue
        match = re.fullmatch(r"(.+?)\s*[=:]\s*([+-]?\d+(?:\.\d+)?)\s*[vV]?", item)
        if not match:
            if re.search(r"[=:]", item):
                return None, error
            pending.append(item)
            continue
        volts = float(match[2])
        if not -1000 <= volts <= 1000:
            return None, error
        for name in [*pending, match[1].strip()]:
            out[name] = volts
        pending = []
    if pending or not out:
        return None, error
    return out, None


def _control_nets(tree) -> set[str]:
    """Nets on a converter's bootstrap, soft-start, compensation, feedback, current-limit,
    gate or mode pin that no source feeds and no supply pin loads: a name such as BST_5V
    gives the rail they serve, not their voltage."""
    powered = set(tree.loads) | tree.supplied | {
        net for conv in tree.converters.values()
        for net in [*conv.inputs, *(output.net for output in conv.outputs)]}
    return {net for conv in tree.converters.values() for kind in _CONTROL_PIN_KINDS
            for net in conv.nets(kind)} - powered


def net_voltages(tree, data: dict, user: dict[str, float] | None, parts: list | None = None
                 ) -> tuple[dict[str, Volt], list[str]]:
    """Each net's voltage with its source: user (rails=, which win), ground, name, then the
    power tree (data is tree_data's, for the outputs a given Vref sets) and, when the
    fitted parts read_parts gives are passed, the unloaded midpoints of resistor
    dividers. Also the names in user that match no net."""
    nets = set(tree.design.nets)
    out: dict[str, Volt] = {}
    switch_nodes = {net: conv for conv in tree.converters.values()
                    for net in conv.switch_nodes}
    folded = {net.casefold(): net for net in nets}
    unknown = []
    for name, volts in (user or {}).items():
        net = folded.get(name.strip().casefold())
        if net is None:
            unknown.append(name)
            continue
        out[net] = Volt(volts, "user", "high", "given in rails")
    for net in sorted(tree.grounds, key=natural_key):
        out.setdefault(net, Volt(0.0, "ground", "high", "a ground net"))
    control = _control_nets(tree)
    for net in sorted(nets, key=natural_key):
        if net in out or net in switch_nodes or net in control:
            continue
        kind = net_kind(net)
        if kind["status"]:
            continue
        found = name_voltage(net)
        if found is not None:
            volts, by_convention = found
            confidence = ("high" if kind["power"] and not kind["signal"] and not by_convention
                          else "medium")
            out[net] = Volt(volts, "name", confidence,
                            "the net name" + (" (volts and tenths by convention, as VDD33 "
                                              "is 3.3 V)" if by_convention else ""))
    for conv in data.get("converters", ()):
        for row in conv.get("feedback", ()):
            rail = row.get("rail")
            if "vout_computed" in row and rail and rail not in out:
                out[rail] = Volt(row["vout_computed"], "power_tree", "medium",
                                 f"{conv['designator']}'s feedback divider with Vref "
                                 f"{row['vref_given']:g} V")
    edges = sorted(((edge.src, edge.dst, edge.part, edge.kind) for edge in tree.edges
                    if edge.kind in SAME_VOLTAGE_EDGES),
                   key=lambda item: (natural_key(item[2]), item[0], item[1]))
    changed = True
    while changed:
        changed = False
        for src, dst, part, kind in edges:
            for near, far in ((src, dst), (dst, src)):
                if near in out and far not in out and far not in switch_nodes and \
                        far not in tree.grounds:
                    out[far] = Volt(out[near].volts, "power_tree", "medium",
                                    f"joined to {near} through {part} ({kind.replace('_', ' ')})")
                    changed = True
    for net, conv in sorted(switch_nodes.items(), key=lambda item: natural_key(item[0])):
        if net in out:
            continue
        peak = None
        if conv.kind == "buck" and conv.inputs and conv.inputs[0] in out:
            peak = conv.inputs[0]
        elif conv.kind == "boost" and conv.outputs and conv.outputs[0].net in out:
            peak = conv.outputs[0].net
        if peak:
            out[net] = Volt(out[peak].volts, "power_tree", "low",
                            f"switch node of {conv.ref} ({conv.kind}): peaks at {peak}")
    if parts is not None:
        out.update(_divider_midpoints(tree, parts, out))
    return out, unknown


# What may sit on a divider's midpoint besides its two resistors: nothing that sinks or
# sources current (an IC's input pin, a capacitor, a connector or test point).
_MIDPOINT_KINDS = frozenset({"capacitor", "connector", "passive", "esd"})


def _divider_midpoints(tree, checked: list, voltages: dict[str, Volt]) -> dict[str, Volt]:
    """Nets of unknown voltage that are the midpoint of two fitted resistors from two
    different known voltages, with nothing else on them but capacitors, connectors and
    IC input pins: their unloaded divider voltage (medium confidence)."""
    legs: dict[str, list[tuple[Part, str]]] = defaultdict(list)
    for part in checked:
        if part.kind != "resistor" or len(part.sections) != 1 or not part.ohms or \
                part.ohms <= 1e-6:
            continue
        net_a, net_b = part.sections[0]
        for near, far in ((net_a, net_b), (net_b, net_a)):
            known = voltages.get(far)
            if near not in voltages and near not in tree.grounds and known is not None \
                    and known.confidence != "low":
                legs[near].append((part, far))
    out: dict[str, Volt] = {}
    for net, found in sorted(legs.items(), key=lambda item: natural_key(item[0])):
        if len(found) != 2:
            continue
        (top, top_net), (bottom, bottom_net) = found
        v_top, v_bottom = voltages[top_net].volts, voltages[bottom_net].volts
        if abs(v_top - v_bottom) < 0.05:
            continue
        others = [(ref, pin) for ref, pin in tree.members(net)
                  if ref not in (top.ref, bottom.ref) and tree.fitted(ref)]
        if not all(tree.kind(ref) in _MIDPOINT_KINDS or (
                tree.kind(ref) == "ic" and ((tree.components.get(ref) or {}).get(
                    "pin_types") or {}).get(pin) == "Input") for ref, pin in others):
            continue
        volts = (v_top * bottom.ohms + v_bottom * top.ohms) / (top.ohms + bottom.ohms)
        out[net] = Volt(round(volts, 4), "divider", "medium",
                        f"the midpoint of {top.ref} from {top_net} and {bottom.ref} from "
                        f"{bottom_net}, unloaded")
    return out


def _ripple_nets(tree) -> dict[str, str]:
    """net -> why it carries ripple: every switching converter it is an output, an input or
    the switch node of ('output of U2 (buck); input of U5 (buck), U6 (boost)')."""
    roles: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for ref in sorted(tree.converters, key=natural_key):
        conv = tree.converters[ref]
        if not conv.switching:
            continue
        for output in conv.outputs:
            if (output.kind or conv.kind) in SWITCHING_KINDS:
                roles[output.net]["output"].append(f"{ref} ({output.kind or conv.kind})")
        for net in conv.inputs:
            roles[net]["input"].append(f"{ref} ({conv.kind})")
        for net in conv.switch_nodes:
            roles[net]["switch node"].append(ref)
    out: dict[str, str] = {}
    for net, by_role in roles.items():
        if net in tree.grounds:
            continue
        parts = []
        for role in ("output", "input", "switch node"):
            refs = list(dict.fromkeys(by_role.get(role, ())))
            if refs:
                more = f" and {len(refs) - 3} more" if len(refs) > 3 else ""
                parts.append(f"{role} of {', '.join(refs[:3])}{more}")
        out[net] = "; ".join(parts)
    return out


# ---------- checks ----------

def _v(volts: float) -> str:
    return f"{round(volts, 3):g}"


def _pct(ratio: float) -> str:
    return f"{ratio * 100:.0f}%"


def _w(watts: float) -> str:
    return f"{watts * 1000:.3g} mW" if watts < 1 else f"{watts:.3g} W"


def _next_rating(volts: float) -> float | None:
    return next((rating for rating in _NEXT_RATINGS if rating >= volts - 1e-9), None)


@dataclass
class _Across:
    volts: float | None
    nets: tuple[str, str]
    basis: str | None = None
    confidence: str | None = None
    sources: tuple[str, ...] = ()
    one_known: Volt | None = None  # one end known, the other not
    bootstrap: bool = False


_CONFIDENCE_RANK = {"high": 0, "medium": 1, "low": 2}


def across_voltage(net_a: str, net_b: str, voltages: dict[str, Volt], switch_nodes: set[str]
                   ) -> _Across:
    """The voltage across two nets (both known), or which end is known."""
    va, vb = voltages.get(net_a), voltages.get(net_b)
    if va is not None and vb is not None:
        confidence = max((va.confidence, vb.confidence), key=_CONFIDENCE_RANK.get)
        return _Across(abs(va.volts - vb.volts), (net_a, net_b),
                       f"{va.describe(net_a)} to {vb.describe(net_b)}", confidence,
                       (va.source, vb.source))
    known = va if va is not None else vb
    # A switch node to a net of unknown voltage: a bootstrap capacitor (BST to SW).
    bootstrap = (net_a in switch_nodes and vb is None) or (net_b in switch_nodes and va is None)
    return _Across(None, (net_a, net_b), one_known=known, bootstrap=bootstrap)


def _finding(check: str, severity: str, issue: str, designators, **fields) -> dict:
    out: dict = {"check": check, "severity": severity,
                 "designators": sorted(set(designators), key=natural_key)}
    out.update({key: value for key, value in fields.items() if value not in (None, [], {})})
    out["issue"] = issue
    return out


def _value(part: Part, name: str):
    found = part.fields.get(name)
    return found.value if found else None


def _source(part: Part, name: str) -> str | None:
    found = part.fields.get(name)
    return found.describe() if found else None


def _cap_findings(part: Part, across: _Across, ripple: str | None, policy: dict) -> list[dict]:
    """The capacitor's findings with its rating; when no warning comes of it and a source
    as well supported gave a lower rating (a tie: which is right is not known), the
    warnings that lower rating gives, still warnings, saying the sources disagree."""
    rated = part.fields.get("rated_v")
    if across.volts is None or rated is None or across.volts <= 0:
        return []
    out = _judge_cap(part, across, ripple, policy, rated.value, rated.describe())
    conflicts = part.conflicts.get("rated_v") or {}
    if any(finding["severity"] == "warning" for finding in out) or not conflicts:
        return out

    def support(volts: float) -> int:
        return len({item.source for item in part.candidates.get("rated_v", ())
                    if _same(item.value, volts)})

    for source, value in sorted(conflicts.items(), key=lambda item: item[1]):
        if value >= rated.value - 1e-9:
            break
        if support(value) < support(rated.value):
            continue  # outvoted: more kinds of source agree on the higher rating
        lower = [finding for finding in _judge_cap(part, across, ripple, policy, value, source)
                 if finding["severity"] == "warning"]
        for finding in lower:
            finding["issue"] = (
                f"The rating sources disagree: {_v(rated.value)} V ({rated.describe()}) "
                f"against {_v(value)} V ({source}), as well supported. With the lower "
                "rating: " + finding["issue"] + " Confirm the part's rating.")
        if lower:
            return out + lower
    return out


def _judge_cap(part: Part, across: _Across, ripple: str | None, policy: dict, rated: float,
               rating_source: str) -> list[dict]:
    applied = across.volts
    tech = _value(part, "technology")
    dielectric = _value(part, "dielectric")
    case = _value(part, "case")
    ratio = applied / rated
    common = {
        "nets": list(across.nets), "applied_v": round(applied, 3), "rated_v": rated,
        "ratio": round(ratio, 3), "technology": tech, "dielectric": dielectric, "case": case,
        "value": part.value, "voltage_basis": across.basis,
        "voltage_confidence": across.confidence, "rating_source": rating_source,
        "ripple": ripple,
    }
    if part.conflicts.get("rated_v"):
        common["rating_conflict"] = part.conflicts["rated_v"]
    if applied > rated + 1e-9:
        return [_finding("over_rated", "warning",
                         f"{_v(applied)} V across a part rated {_v(rated)} V: above its "
                         "rating. Check the rating in the datasheet and the rail's maximum.",
                         [part.ref], **common)]
    out = []
    if tech == "ceramic":
        limit = policy["ceramic_max_ratio"]
        why = f"{_pct(1 - limit)} derating"
        if ripple and policy["ceramic_ripple_max_ratio"] < limit:
            limit = policy["ceramic_ripple_max_ratio"]
            why = f"{_pct(1 - limit)} derating on a ripple net ({ripple})"
        if ratio > limit + 1e-9:
            need = _next_rating(applied / limit)
            out.append(_finding(
                "ceramic_voltage", "warning",
                f"{_v(applied)} V on a {_v(rated)} V ceramic: {_pct(ratio)} of its rating, "
                f"above {_pct(limit)} ({why})"
                + (f"; a {_v(need)} V part would meet it" if need else "")
                + ". Its DC-bias loss also grows with the voltage: see the datasheet.",
                [part.ref], limit=limit, **common))
    elif tech in ("tantalum", "tantalum_polymer"):
        polymer = tech == "tantalum_polymer"
        limit = policy["tantalum_polymer_max_ratio" if polymer else "tantalum_max_ratio"]
        if ratio > limit + 1e-9:
            note = ("polymer tantalum: the checklist asks 20% headroom; some makers allow "
                    "10% above 10 V rated" if polymer else
                    "MnO2 tantalum: the checklist asks 20% headroom, and 50% derating is "
                    "common practice on low-impedance rails (surge failures)")
            out.append(_finding(
                "tantalum_voltage", "warning",
                f"{_v(applied)} V on a {_v(rated)} V tantalum: {_pct(ratio)} of its rating, "
                f"above {_pct(limit)} ({note}). Check the maker's derating guidance.",
                [part.ref], limit=limit, **common))
    elif tech in ("aluminium_electrolytic", "aluminium_polymer", "polarized"):
        limit = policy["electrolytic_max_ratio"]
        if ratio > limit + 1e-9:
            kind = ("polarized capacitor of no stated kind (tantalum or electrolytic)"
                    if tech == "polarized" else tech.replace("_", " ") + " capacitor")
            out.append(_finding(
                "electrolytic_voltage", "warning",
                f"{_v(applied)} V on a {_v(rated)} V {kind}: {_pct(ratio)} of its rating, "
                f"above {_pct(limit)}. Check the maker's derating and ripple-current "
                "ratings.", [part.ref], limit=limit, **common))
    elif tech is None:
        out += _judge_unknown_technology(part, applied, rated, ratio, ripple, common, policy)
    if tech == "ceramic":
        out += _dc_bias(part, applied, rated, ratio, common, policy)
    return out


def _judge_unknown_technology(part: Part, applied: float, rated: float, ratio: float,
                              ripple: str | None, common: dict, policy: dict) -> list[dict]:
    """A capacitor whose technology was not read: a warning above the most lenient limit
    (above it whatever it is), info above the strictest that could apply."""
    limits = {"ceramic": policy["ceramic_max_ratio"],
              "tantalum": min(policy["tantalum_max_ratio"],
                              policy["tantalum_polymer_max_ratio"]),
              "electrolytic": policy["electrolytic_max_ratio"]}
    if ripple:
        limits["ceramic"] = min(limits["ceramic"], policy["ceramic_ripple_max_ratio"])
    lenient, strict = max(limits.values()), min(limits.values())
    if ratio > lenient + 1e-9:
        severity, what = "warning", (f"above {_pct(lenient)}, the most lenient limit of any "
                                     "technology here")
    elif ratio > strict + 1e-9:
        over = " or ".join(f"a {kind} ({_pct(limit)})" for kind, limit in limits.items()
                           if ratio > limit + 1e-9)
        severity, what = "info", f"above the limit for {over} if it is one"
    else:
        return []
    return [_finding(
        "capacitor_voltage", severity,
        f"{_v(applied)} V on a {_v(rated)} V capacitor whose technology was not read "
        f"(ceramic, tantalum or electrolytic): {_pct(ratio)} of its rating, {what}. Check "
        "its technology and the maker's derating.", [part.ref], **common)]


def _dc_bias(part: Part, applied: float, rated: float, ratio: float, common: dict,
             policy: dict) -> list[dict]:
    dielectric = _value(part, "dielectric")
    klass = dielectric_class(dielectric)
    case = _value(part, "case")
    farads = _value(part, "farads")
    max_case = _CASE_ORDER.get(policy["dc_bias_max_case"], -1)
    if klass == 1 or case not in _CASE_ORDER or _CASE_ORDER[case] > max_case or \
            farads is None or ratio < policy["dc_bias_min_ratio"] - 1e-9:
        return []
    threshold = policy["dc_bias_min_uf"].get(case)
    if threshold is None or farads * 1e6 < threshold - 1e-12:
        return []
    if klass == 3:
        band = "about 60-90%"
    elif ratio >= 0.75:
        band = "about 50-80%"
    elif ratio >= 0.5:
        band = "about 40-70%"
    else:
        band = "about 20-50%"
    assumed = "" if klass else " (dielectric not read: taken for Class II, as a value this " \
                               "large in this case is)"
    return [_finding(
        "dc_bias", "info",
        f"{part.value} {dielectric or 'ceramic'} in {case} at {_pct(ratio)} of its "
        f"{_v(rated)} V rating{assumed}: small high-value Class II parts typically lose "
        f"{band} of their capacitance at this bias (an approximate band, not a number). "
        "The datasheet's DC-bias curve or the maker's simulation tool gives the effective "
        "value: check it is enough.",
        [part.ref], loss_band=band, **common)]


def resistor_limits(part: Part, policy: dict) -> tuple[Field | None, Field | None]:
    """(power rating, maximum working voltage) as stated, else typical for the size. An
    array whose sources disagree takes the lowest stated power: a per-element rating, not
    the package's."""
    case = _value(part, "case")
    multi = len(part.sections) > 1
    power = part.fields.get("power_w")
    if multi and power is not None and part.conflicts.get("power_w"):
        lowest = min(part.candidates["power_w"], key=lambda item: item.value)
        if lowest.value < power.value:
            power = Field(lowest.value, lowest.source, ", ".join(filter(None, (
                lowest.detail, "the lowest stated, taken per element"))))
    if power is None and case and not multi and not _value(part, "high_power"):
        typical = policy["resistor_power_w"].get(case)
        if typical:
            power = Field(typical, "package_typical", case)
    voltage = part.fields.get("rated_v")
    if voltage is None and case and not multi:
        typical = policy["resistor_max_voltage"].get(case)
        if typical:
            voltage = Field(typical, "package_typical", case)
    return power, voltage


def is_shunt(part: Part, text: str) -> bool:
    return (part.ohms is not None and 0 < part.ohms <= LINK_OHMS) or bool(
        re.search(r"\bshunt\b|current[- ]sens", text, re.I))


def _resistor_findings(part: Part, tree, voltages: dict[str, Volt], switch_nodes: set[str],
                       led_nets: dict[str, list[str]], series_nets: set[frozenset],
                       policy: dict) -> tuple[list[dict], dict]:
    power, max_v = resistor_limits(part, policy)
    derating = policy["resistor_power_derating"]
    ohms = part.ohms
    case = _value(part, "case")
    out: list[dict] = []
    row: dict = {}
    base = {"value": part.value, "case": case,
            "power_rating_w": power.value if power else None,
            "power_rating_source": power.describe() if power else None,
            "max_voltage": max_v.value if max_v else None,
            "max_voltage_source": max_v.describe() if max_v else None}
    text = tree.text(part.ref)
    for net_a, net_b in part.sections:
        across = across_voltage(net_a, net_b, voltages, switch_nodes)
        nets = [net_a, net_b]
        leds = sorted({led for net in nets for led in led_nets.get(net, ())}, key=natural_key)
        if across.volts is not None:
            row.update(across_v=round(across.volts, 3), voltage_basis=across.basis)
        if is_shunt(part, text):
            out.append(_finding(
                "needs_current", "info",
                f"Current-sense shunt ({part.value or 'value not read'} ohm): its power is "
                "I^2 x R. Get the maximum current through it (the load's or the converter's "
                "current limit) and compare with its power rating"
                + (f" ({_w(power.value)}, {power.describe()})" if power else "") + ".",
                [part.ref], kind="shunt", nets=nets, **base))
            continue
        if leds:
            out += _led_resistor(part, nets, leds, across, voltages, tree, power, base, policy)
            continue
        if across.volts is not None:
            dv = across.volts
            if ohms is not None and ohms <= 1e-6:
                if dv > 0.05:
                    out.append(_finding(
                        "resistor_power", "warning",
                        f"A 0 ohm link joins nets at {_v(dv)} V apart ({across.basis}): a "
                        "short, or one of the voltages is wrong (check the net names and "
                        "rails=).", [part.ref], nets=nets, across_v=round(dv, 3), **base))
                continue
            if dv <= 1e-9:
                if ohms is not None and ohms <= FILTER_OHMS and frozenset(nets) in series_nets:
                    out.append(_series_supply(part, nets, power, base))
                continue
            if max_v is not None and dv > max_v.value + 1e-9:
                out.append(_finding(
                    "resistor_voltage", "warning",
                    f"{_v(dv)} V across a {case or 'chip'} resistor: above its "
                    f"{'stated' if max_v.source != 'package_typical' else 'typical'} "
                    f"{_v(max_v.value)} V working voltage ({max_v.describe()}). Check the "
                    "datasheet's limiting element voltage; a larger size or a series pair "
                    "spreads it.", [part.ref], nets=nets, across_v=round(dv, 3),
                    voltage_basis=across.basis, **base))
            if ohms is None or ohms <= 0:
                continue
            watts = dv * dv / ohms
            row["power_w"] = round(watts, 6)
            if power is None:
                continue
            if watts > power.value + 1e-12:
                severity, what = "warning", "above its rating"
            elif watts > power.value * derating + 1e-12:
                severity, what = "info", f"above {_pct(derating)} of its rating (the derated limit)"
            else:
                continue
            out.append(_finding(
                "resistor_power", severity,
                f"{_w(watts)} in a {case or ''} {part.value} resistor ({_v(dv)} V across): "
                f"{what}, {_w(power.value)} ({power.describe()}). Check the datasheet's "
                "power derating with temperature and the part's temperature rise.",
                [part.ref], nets=nets, across_v=round(dv, 3), power_w=round(watts, 6),
                voltage_basis=across.basis, **base))
            continue
        known = across.one_known
        if known is None or abs(known.volts) <= 1e-9 or known.confidence == "low":
            continue  # nothing to bound with, or a switch node's peak (duty cycle unknown)
        rail = next(net for net in nets if voltages.get(net) is known)
        other = net_b if rail == net_a else net_a
        if ohms is not None and 0 < ohms <= FILTER_OHMS and frozenset(nets) in series_nets:
            out.append(_series_supply(part, nets, power, base, rail))
            continue
        bound_v = abs(known.volts)
        if max_v is not None and bound_v > max_v.value + 1e-9:
            out.append(_finding(
                "resistor_voltage", "info",
                f"{rail} is at {_v(known.volts)} V and {other}'s voltage is not known: up to "
                f"{_v(bound_v)} V could sit across this {case or 'chip'} resistor, above its "
                f"{_v(max_v.value)} V working voltage ({max_v.describe()}) if {other} sits "
                "near 0 V. Check the voltage across it.", [part.ref], nets=nets,
                bound_v=round(bound_v, 3), **base))
        if ohms and power is not None:
            bound_w = bound_v * bound_v / ohms
            if bound_w > power.value * derating + 1e-12:
                out.append(_finding(
                    "needs_current", "info",
                    f"{rail} is at {_v(known.volts)} V and {other}'s voltage is not known: "
                    f"up to {_w(bound_w)} if {other} sits at 0 V, above the derated "
                    f"{_w(power.value * derating)} of this {part.value} resistor. Get the "
                    f"current through it (or {other}'s voltage, e.g. a clamp or a load's "
                    "input) and compare P = I^2 x R with its rating.",
                    [part.ref], kind="one_end_known", nets=nets, rail=rail,
                    bound_w=round(bound_w, 6), **base))
    return out, row


def _series_supply(part: Part, nets: list[str], power: Field | None, base: dict,
                   rail: str | None = None) -> dict:
    return _finding(
        "needs_current", "info",
        f"{part.value or 'A'} ohm resistor in series with a supply ({' to '.join(nets)}): its "
        "power is I^2 x R with the supply current of the parts it feeds. Get their maximum "
        "current from their datasheets"
        + (f" and compare with its {_w(power.value)} rating ({power.describe()})" if power
           else "") + ".", [part.ref], kind="series_supply", nets=nets, rail=rail, **base)


def _led_resistor(part: Part, nets: list[str], leds: list[str], across: _Across,
                  voltages: dict[str, Volt], tree, power: Field | None, base: dict,
                  policy: dict) -> list[dict]:
    """An LED series resistor: bounded by the drive voltage (Vf taken as 0); listed when
    the bound exceeds the derated rating or cannot be made."""
    drive = None
    for net in nets:
        volt = voltages.get(net)
        if volt is not None and abs(volt.volts) > 1e-9:
            drive = (net, abs(volt.volts), volt.describe(net))
    if drive is None:
        for led in leds:
            for net in (tree.design.part_pins.get(led) or {}).values():
                volt = voltages.get(net)
                if net not in nets and volt is not None and abs(volt.volts) > 1e-9:
                    drive = (net, abs(volt.volts), f"{volt.describe(net)} through {led}")
    if drive is None:
        level = _v(policy["signal_max_v"])
        rail = next((net for net in nets if net in tree.nodes()), None)
        through = ""
        if rail is None:  # the LED's other end on a rail (its anode on VBAT)
            rail, through = next(((net, f" through {led}") for led in leds
                                  for net in (tree.design.part_pins.get(led) or {}).values()
                                  if net not in nets and net in tree.nodes()), (None, ""))
        drive = (rail, policy["signal_max_v"],
                 f"{rail}{through}, whose voltage is not known (assumed at most {level} V; "
                 "pass rails=)" if rail else f"a logic output, assumed at most {level} V")
    derating = policy["resistor_power_derating"]
    ohms = part.ohms
    if ohms and power is not None:
        bound = drive[1] ** 2 / ohms
        if bound <= power.value * derating + 1e-12:
            return []
    else:
        bound = None
    return [_finding(
        "needs_current", "info",
        f"LED series resistor for {', '.join(leds)}, driven from {drive[2]}: its current is "
        "(V - Vf) / R. Get the LED's forward voltage at its operating current and its "
        "maximum current from its datasheet, then check P = I^2 x R against the "
        "resistor's rating"
        + (f" ({_w(power.value)}, {power.describe()}; up to {_w(bound)} with Vf = 0)"
           if power and bound is not None else "") + ".",
        [part.ref, *leds], kind="led_resistor", nets=nets, rail=drive[0],
        bound_w=round(bound, 6) if bound is not None else None, **base)]


# ---------- the check ----------

def _group(findings: list[dict]) -> list[dict]:
    """Findings that differ only in their designators become one row; count is how many
    were merged (the parts, or the LED circuits)."""
    grouped: dict[tuple, dict] = {}
    merged: Counter = Counter()
    for finding in findings:
        key = tuple(sorted((name, repr(value)) for name, value in finding.items()
                           if name != "designators"))
        merged[key] += 1
        if key in grouped:
            grouped[key]["designators"] = sorted(
                set(grouped[key]["designators"]) | set(finding["designators"]),
                key=natural_key)
        else:
            grouped[key] = dict(finding)
    for key, finding in grouped.items():
        if merged[key] > 1:
            finding.setdefault("count", merged[key])
    return list(grouped.values())


def _part_row(part: Part, across: list[_Across], ripple: dict[str, str]) -> dict:
    row: dict = {"designator": part.ref, "type": part.kind,
                 "nets": [list(section) for section in part.sections]
                 if len(part.sections) > 1 else list(part.sections[0]),
                 "value": part.value, "across_v": None}
    known = [item for item in across if item.volts is not None]
    if known:
        worst = max(known, key=lambda item: item.volts)
        row["across_v"] = round(worst.volts, 3)
        row["voltage_basis"] = worst.basis
        row["voltage_confidence"] = worst.confidence
    elif any(item.bootstrap for item in across):
        row["across_v"] = None
        row["voltage_basis"] = ("a bootstrap capacitor (switch node to the BST pin): it sees "
                                "the converter's bias, from its datasheet")
    for name, found in part.fields.items():
        if name != "farads":  # the value, already given as text
            row[name] = found.value
        row[f"{'value' if name == 'farads' else name}_source"] = found.describe()
    rated = part.fields.get("rated_v")
    if rated and known and part.kind == "capacitor":
        row["ratio"] = round(max(item.volts for item in known) / rated.value, 3)
    if part.rated_ac:
        row["rated_ac_v"] = part.rated_ac
    if part.conflicts:
        row["conflicts"] = part.conflicts
    if on := [net for section in part.sections for net in section if net in ripple]:
        row["ripple"] = ripple[on[0]]
    return {key: value for key, value in row.items() if value not in (None, [], {})
            or key == "across_v"}


def prepare_derating(
    netlist: dict,
    plan=None,
    project_params: dict | None = None,
    checks: list[str] | None = None,
    parts: list[str] | None = None,
    nets: list[str] | None = None,
    rails: dict[str, float] | None = None,
    vref: dict[str, float] | float | None = None,
    policy: dict | None = None,
) -> dict:
    """Read every fitted capacitor, resistor and LED once, with the net voltages, and
    check them; render_derating pages the result. policy is load_policy's info (the
    defaults when None). An {"error": ...} dict for an unknown check, part or net."""
    requested = [check.strip().lower() for check in checks or [] if check.strip()]
    if invalid := [check for check in requested if check not in CHECKS]:
        return {"error": "invalid_checks",
                "message": f"Unknown checks {invalid}; use {list(CHECKS)}."}
    policy_info = policy or load_policy(None)[0]
    values = policy_info["values"]
    tree = build_tree(netlist, plan, project_params)
    data = tree_data(tree, vref)
    notes: list[str] = []
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
    checked, leds, stats = read_parts(tree, netlist, plan)
    voltages, unknown_rails = net_voltages(tree, data, rails, checked)
    if unknown_rails:
        notes.append(f"rails names no net {unknown_rails}: ignored (use query_net to find "
                     "the real name).")
    ripple = _ripple_nets(tree)
    switch_nodes = {net for conv in tree.converters.values() for net in conv.switch_nodes}
    series_nets = {frozenset((edge.src, edge.dst)) for edge in tree.edges
                   if edge.kind in ("resistor", "shunt")}
    if values["ceramic_ripple_max_ratio"] > values["ceramic_max_ratio"]:
        notes.append(f"ceramic_ripple_max_ratio ({values['ceramic_ripple_max_ratio']:g}) is "
                     f"looser than ceramic_max_ratio ({values['ceramic_max_ratio']:g}): "
                     "ripple nets use the standard limit too.")
    # An LED's own node: not a rail, a ground or a net of known voltage, and few pins (the
    # LED and its resistor), so a 0 ohm link on a rail the LED sits on is not its resistor.
    led_nets: dict[str, list[str]] = defaultdict(list)
    for led in leds:
        for net in set((tree.design.part_pins.get(led) or {}).values()):
            if net not in tree.grounds and net not in voltages and net not in tree.nodes() \
                    and len(tree.members(net)) <= 4:
                led_nets[net].append(led)
    findings: list[dict] = []
    rows: dict[str, dict] = {}
    unknown_rating: list[str] = []
    cap_stats: Counter = Counter()
    res_stats: Counter = Counter()
    for part in checked:
        across = [across_voltage(a, b, voltages, switch_nodes) for a, b in part.sections]
        if part.kind == "capacitor":
            _count_cap(part, across, cap_stats)
            known = [item for item in across if item.volts is not None]
            worst = max(known, key=lambda item: item.volts) if known else None
            if worst is not None and worst.volts > 0 and "rated_v" not in part.fields:
                unknown_rating.append(part.ref)
            if worst is not None:
                on_ripple = next((ripple[net] for net in worst.nets if net in ripple), None)
                findings += _cap_findings(part, worst, on_ripple, values)
            rows[part.ref] = _part_row(part, across, ripple)
        else:
            _count_resistor(part, across, res_stats, values)
            found, row = _resistor_findings(part, tree, voltages, switch_nodes, led_nets,
                                            series_nets, values)
            findings += found
            rows[part.ref] = {**_part_row(part, across, ripple), **row}
    keep = None
    if wanted_parts is not None or wanted_nets is not None:
        keep = set(wanted_parts or ())
        for part in checked:
            if wanted_nets and {net for section in part.sections for net in section} & \
                    wanted_nets:
                keep.add(part.ref)
        for led in leds:
            if wanted_nets and set((tree.design.part_pins.get(led) or {}).values()) & \
                    wanted_nets:
                keep.add(led)
        for ref in sorted(wanted_parts or (), key=natural_key):
            if ref not in rows and ref not in leds:
                notes.append(f"{ref} is not a fitted capacitor, resistor or LED in variant "
                             f"{tree.variant}: not checked.")
        findings = [finding for finding in findings if set(finding["designators"]) & keep]
        unknown_rating = [ref for ref in unknown_rating if ref in keep]
    if unknown_rating:
        findings.append(_finding(
            "unknown_rating", "info",
            f"{len(unknown_rating)} capacitor(s) with a known voltage across them have no "
            "voltage rating that could be read (parameters, value, comment, description or a "
            "decoded part number): add a Voltage parameter to their library parts, or check "
            "each datasheet.", unknown_rating[:UNKNOWN_LISTED], count=len(unknown_rating)))
    wanted = set(requested or CHECKS)
    findings = _group([finding for finding in findings if finding["check"] in wanted])
    findings.sort(key=lambda finding: (_SEVERITY_RANK[finding["severity"]],
                                       _CHECK_RANK[finding["check"]],
                                       natural_key(finding["designators"][0])))
    summary = _summary(checked, leds, voltages, ripple, cap_stats, res_stats, stats, tree)
    # Rails whose voltage is not known, by how many capacitors they hold: rails= fills them.
    missing: Counter = Counter()
    nodes = tree.nodes()
    control = _control_nets(tree) | switch_nodes
    for part in checked:
        if part.kind == "capacitor":
            for net in {net for section in part.sections for net in section}:
                if net not in voltages and net not in control and not _control_name(net) \
                        and (net in nodes or net_kind(net)["power"]):
                    missing[net] += 1
    part_rows = None
    if keep is not None:
        part_rows = [rows[ref] for ref in sorted(keep, key=natural_key) if ref in rows]
        part_rows += [{"designator": led, "type": "led",
                       "nets": sorted(set((tree.design.part_pins.get(led) or {}).values()),
                                      key=natural_key)}
                      for led in sorted(keep & set(leds), key=natural_key)]
    return {"variant": tree.variant, "summary": summary, "findings": findings,
            "wanted": wanted, "notes": notes, "policy": policy_info, "parts": part_rows,
            "voltages": voltages, "ripple": ripple, "wanted_nets": wanted_nets,
            "rails_without_voltage": [
                {"net": net, "capacitors": count} for net, count in
                sorted(missing.items(), key=lambda item: (-item[1], natural_key(item[0])))]}


def _count_cap(part: Part, across: list[_Across], stats: Counter) -> None:
    stats["capacitors"] += 1
    known = any(item.volts is not None for item in across)
    stats["applied_voltage_known" if known else "applied_voltage_unknown"] += 1
    if not known and any(item.bootstrap for item in across):
        stats["bootstrap"] += 1
    for name in ("rated_v", "dielectric", "technology", "case"):
        found = part.fields.get(name)
        stats[f"{name}:{found.source if found else 'unknown'}"] += 1
    stats[f"tech:{_value(part, 'technology') or 'unknown'}"] += 1
    if part.rated_ac and "rated_v" not in part.fields:
        stats["ac_rating_only"] += 1
    for name in part.conflicts:
        stats[f"conflict:{name}"] += 1


def _count_resistor(part: Part, across: list[_Across], stats: Counter, policy: dict) -> None:
    stats["resistors"] += 1
    known = any(item.volts is not None for item in across)
    stats["across_known" if known else "across_unknown"] += 1
    power, max_v = resistor_limits(part, policy)
    for name, found in (("case", part.fields.get("case")), ("power_w", power),
                        ("rated_v", max_v)):
        stats[f"{name}:{found.source if found else 'unknown'}"] += 1
    if part.ohms is None:
        stats["value_unknown"] += 1


def _summary(checked: list[Part], leds: list[str], voltages: dict[str, Volt],
             ripple: dict[str, str], cap: Counter, res: Counter, stats: Counter, tree) -> dict:
    sources = Counter(volt.source for volt in voltages.values())

    def by(counter: Counter, prefix: str) -> dict:
        return {key[len(prefix):]: value for key, value in sorted(counter.items())
                if key.startswith(prefix)}

    return {
        "parts_checked": {"capacitors": cap["capacitors"], "resistors": res["resistors"],
                          "leds": len(leds)},
        "not_fitted_skipped": stats["not_fitted_capacitor"] + stats["not_fitted_resistor"]
        + stats["not_fitted_diode"],
        "net_voltages": {"known_by_source": {source: sources[source] for source in
                                             ("user", "name", "power_tree", "divider",
                                              "ground")
                                             if sources[source]},
                         "unknown": len(tree.design.nets) - len(voltages),
                         "ripple_nets": len(ripple)},
        "capacitors": {
            "applied_voltage_known": cap["applied_voltage_known"],
            "applied_voltage_unknown": cap["applied_voltage_unknown"],
            **({"bootstrap": cap["bootstrap"]} if cap["bootstrap"] else {}),
            "rating_source": by(cap, "rated_v:"),
            "technology": by(cap, "tech:"),
            "technology_source": by(cap, "technology:"),
            "dielectric_source": by(cap, "dielectric:"),
            "case_source": by(cap, "case:"),
            **({"ac_rating_only": cap["ac_rating_only"]} if cap["ac_rating_only"] else {}),
            **({"conflicts": by(cap, "conflict:")} if by(cap, "conflict:") else {}),
        },
        "resistors": {
            "voltage_across_known": res["across_known"],
            "voltage_across_unknown": res["across_unknown"],
            "case_source": by(res, "case:"),
            "power_rating_source": by(res, "power_w:"),
            "working_voltage_source": by(res, "rated_v:"),
            **({"value_unknown": res["value_unknown"]} if res["value_unknown"] else {}),
        },
    }


def render_derating(prepared: dict, offset: int = 0, limit: int = PAGE_LIMIT) -> dict:
    """One page of a prepared check: summary first, then the findings (and, when parts
    or nets were named, their parts) paged together on one offset."""
    findings = prepared["findings"]
    by_check: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for finding in findings:
        by_check[finding["check"]][finding["severity"]] += finding.get("count", 1)
    severities = {level: sum(1 for finding in findings if finding["severity"] == level)
                  for level in ("warning", "info")}
    offset = max(0, int(offset or 0))
    page = slice(offset, offset + limit)
    policy = prepared["policy"]
    summary = dict(prepared["summary"])
    summary["findings"] = {check: dict(levels) for check, levels in by_check.items()}
    missing = prepared["rails_without_voltage"]
    result: dict = {
        "variant": prepared["variant"],
        "summary": summary,
        "severity_counts": severities,
        "rails_without_voltage_total": len(missing),
        "rails_without_voltage": missing[:RAILS_LISTED],
        "checks_run": [check for check in CHECKS if check in prepared["wanted"]],
        "heuristic": HEURISTIC,
        "guidance": GUIDANCE,
        "policy": policy,
        "findings": findings[page],
        "total": len(findings),
    }
    lengths = [len(findings)]
    if prepared["parts"] is not None:
        result["parts_total"] = len(prepared["parts"])
        result["parts"] = prepared["parts"][page]
        lengths.append(len(prepared["parts"]))
        if prepared.get("wanted_nets"):
            result["net_voltages"] = {
                net: {"volts": volt.volts, "source": volt.source,
                      "confidence": volt.confidence, "basis": volt.basis,
                      **({"ripple": prepared["ripple"][net]} if net in prepared["ripple"]
                         else {})}
                if (volt := prepared["voltages"].get(net)) else None
                for net in sorted(prepared["wanted_nets"], key=natural_key)}
    result["offset"] = offset
    result["has_more"] = offset + limit < max(lengths)
    if result["has_more"]:
        result["next_offset"] = offset + limit
    notes = list(prepared["notes"])
    notes.append(
        "Voltages are nominal: a rail's tolerance, transients and ripple peaks add to them. "
        "rails_without_voltage lists rails whose voltage neither the name nor the power tree "
        "gives, by their capacitors: ask the user and pass rails='NAME=volts' (an input, a "
        "battery), or vref= for a converter whose Vref sets an unnamed output.")
    if prepared["parts"] is None:
        notes.append("Name parts (part=) or nets (net=) for each part's parsed rating, "
                     "sources, voltages and ratio.")
    result["notes"] = notes
    return result


def run_derating_check(
    netlist: dict,
    plan=None,
    project_params: dict | None = None,
    checks: list[str] | None = None,
    parts: list[str] | None = None,
    nets: list[str] | None = None,
    rails: dict[str, float] | None = None,
    vref: dict[str, float] | float | None = None,
    policy: dict | None = None,
    root_dir=None,
    offset: int = 0,
    limit: int = PAGE_LIMIT,
) -> dict:
    """The derating check in one call: load_policy(root_dir, policy), prepare_derating
    and one page of render_derating. Heuristic: see the module notes."""
    info, error = load_policy(root_dir, policy)
    if error:
        return error
    prepared = prepare_derating(netlist, plan, project_params, checks, parts, nets, rails,
                                vref, info)
    if "error" in prepared:
        return prepared
    return render_derating(prepared, offset, limit)
