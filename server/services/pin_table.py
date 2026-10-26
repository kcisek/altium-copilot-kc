"""Compare a datasheet or vendor pin table with a part's pins (roadmap #22,
check_pin_table): the placed part's pins (netlist: every pin's name and electrical
type, multi-part units merged, and the nets of the connected ones) and/or its library
symbol's (every unit's pins, hidden ones marked).

Pins are paired by number (pin_key: case, spaces and leading zeros ignored; a prefix
every number of one side carries, such as Xilinx's P on PQ208 pins, is dropped or added
when only that pairs the numbers). Each pair's names are classified (classify):

- exact: the same text; equivalent: the same after normalisation (parsers/pin_table:
  case, separators, active-low notations, I/O, D(3) / D[3] / D3), or the design names
  the table's name with only some of its functions;
- likely_same: the design names an alternate function, adds functions, marks active-low
  differently, uses another ground or no-connect name, or its tokens are the table's
  reordered (TX_UART / UART_TX), plus an index, a voltage or a qualifier (VSS_1 / VSS,
  VCCO / VCCO_0, VDD / VDD_3V3, VREF 7 / VREF Bank 7, L9P / L9P_Y) or abbreviated by
  three letters or more (PROG_B / PROGRAM_B, RST / RESET; not supply names);
- different: anything else, and a resembling name that is another pin's own name on
  the other side (CLK_P / CLK_N, CE / CEO, a pin and its complement).

Names close enough to be other pins of the part are different, not likely_same: a
differential or channel suffix (CLK_P / CLK_N, OUT_A / OUT_B), a word added (CLK /
CLK_EN), an abbreviation by one letter or of fewer than three (CE / CEO, IN / INT,
DQ0 / DQS0, C1 / CS1, VDD / VDDA). Functions are paired one to one.

On top of that: a block of three or more pins whose names are offset by a constant
number of positions (shifted), two pins whose names are exchanged (swapped), table pins
missing in the design, design pins missing in the table (shield, mounting and
no-connect pins exempt; listed once when the table holds fewer than half the design's
pins), pin types against the table's (power vs signal, input vs output; mild
differences such as IO for an input are info), duplicated numbers and names (over the
pins both sides have), hidden symbol pins, and on the placed part power pins left
unconnected and no-connect pins on a net. A finding both sides have is reported once.
"""

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from functools import lru_cache

try:
    from parsers.pin_table import (
        PinRow, PinSource, PinTable, function_key, generic_name, loose_key, name_kind,
        normalize_name, pin_key, pin_sort_key, row_kind, split_functions, type_kind,
    )
except ImportError:
    from server.parsers.pin_table import (
        PinRow, PinSource, PinTable, function_key, generic_name, loose_key, name_kind,
        normalize_name, pin_key, pin_sort_key, row_kind, split_functions, type_kind,
    )

EXACT, EQUIVALENT, LIKELY, DIFFERENT, UNNAMED = (
    "exact", "equivalent", "likely_same", "different", "unnamed")
SIMILAR = (EXACT, EQUIVALENT, LIKELY)
SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}
KIND_ORDER = (
    "swapped", "shifted", "different_name", "missing_in_design", "extra_in_design",
    "duplicate_number", "duplicate_name", "type_mismatch", "hidden_signal_pin",
    "power_pin_unconnected", "likely_same_name", "unnamed_design_pins", "type_differs",
    "hidden_power_pins", "nc_not_in_design", "nc_pin_connected", "extra_exempt",
    "not_in_partial_table", "exposed_pad_numbered_differently")
GROUP_PINS = 40  # pins listed in a grouped (info) finding
EXAMPLES = 5
SHIFT_OFFSETS = (1, -1, 2, -2, 3, -3)
SHIFT_MIN_RUN = 3
BRUTE_SWAP_LIMIT = 300
MIN_MATCH_SHARE = 0.5
PARTIAL_TABLE_SHARE = 0.5  # a table of fewer of the design's pins is a partial one

SIDE_PLACED, SIDE_LIBRARY = "placed", "library"
SAME_NAME = "the same name after normalisation"
LOW_DIFFERS = "the active-low marking differs"


# ---------------------------------------------------------------- name classes

@dataclass(frozen=True)
class _Name:
    upper: str
    functions: tuple[str, ...]  # canonical, '~' marking active low
    keys: frozenset
    loose: tuple[str, ...]  # canonical with synonyms, low ignored
    plain: frozenset  # canonical, low ignored
    compact: str
    squeezed: str  # the text in upper case without spaces, '_', '-' or '.'


_NAMES: dict[str, _Name] = {}


def _name(text: str) -> _Name:
    held = _NAMES.get(text)
    if held is not None:
        return held
    functions, loose = [], []
    for function in split_functions(text):
        canonical, low = function_key(function)
        if not canonical:
            continue
        functions.append(("~" if low else "") + canonical)
        loose.append(loose_key(canonical))
    raw = str(text).strip().upper()
    if not functions and raw:
        functions, loose = [raw], [raw]  # a name of symbols only (+, ~, a sign)
    plain = frozenset(f.lstrip("~") for f in functions)
    # Without separators, the active-low mark kept: D[3] is D3, RESET is not ~RESET.
    compact = "".join(f.replace("_", "") for f in functions)
    # The text itself without separators: a table in other case and separators
    # ("nceo" for nCEO, "reset-n" for RESET_N) reads the same. A sign is no separator:
    # IN- is not IN.
    squeezed = re.sub(r"[\s_.]|(?<=\w)-(?=\w)", "", raw)
    result = _Name(raw, tuple(functions), frozenset(functions), tuple(loose), plain, compact,
                   squeezed)
    if len(_NAMES) > 200_000:
        _NAMES.clear()
    _NAMES[text] = result
    return result


def _split_alpha(token: str) -> tuple[str, str]:
    match = re.match(r"([A-Z]*)(.*)", token)
    return match.group(1), match.group(2)


def _subsequence(short: str, long: str) -> bool:
    position = 0
    for char in short:
        position = long.find(char, position) + 1
        if not position:
            return False
    return True


def _abbreviation(a: str, b: str) -> bool:
    """One token abbreviates the other: the same index part, and the shorter letters (at
    least three) start the longer ones or are their subsequence with the same first and
    last letter, the longer having at least two letters more (PROG / PROGRAM, RST / RESET,
    CLK / CLOCK, PWR / POWER). Shorter or closer pairs are other pins as often as not
    (CE / CEO, IN / INT, RS / RST, DQ0 / DQS0, C1 / CS1, VDD / VDDA, STAT / START,
    INT / INIT, RSET / RESETN)."""
    if a == b:
        return True
    (pa, ra), (pb, rb) = _split_alpha(a), _split_alpha(b)
    if ra != rb or not pa or not pb or pa == pb:
        return False
    short, long = sorted((pa, pb), key=len)
    if len(short) < 3 or len(long) < len(short) + 2:
        return False
    return long.startswith(short) or (short[0] == long[0] and short[-1] == long[-1]
                                       and _subsequence(short, long))


def _tokens(canonical: str) -> list[str]:
    """A canonical name's tokens, a leading index split off (1OUT: 1, OUT), without the
    qualifiers datasheets add or leave out: a _Y or _YY suffix (Spartan-II pin names) and
    'Bank' before a number (VREF Bank 7)."""
    tokens = []
    for token in canonical.split("_"):
        match = None if _minor(token) else re.fullmatch(r"(\d+)([A-Z].*)", token)
        tokens += list(match.groups()) if match else [token]
    kept = [token for index, token in enumerate(tokens)
            if not (index and token in ("Y", "YY"))
            and not (token == "BANK" and index + 1 < len(tokens) and tokens[index + 1].isdigit())]
    return kept or tokens


def _minor(token: str) -> bool:
    """A token one name may add to the other: an index (VSS / VSS_1, OUT / 1OUT) or a
    voltage (VDD / VDD_3V3)."""
    return token.isdigit() or bool(re.fullmatch(r"\d+V\d*", token))


def _in_order(short: list[str], long: list[str]) -> bool:
    remaining = iter(long)
    return all(token in remaining for token in short)


@lru_cache(maxsize=65536)
def _supply(canonical: str) -> bool:
    return name_kind(canonical) in ("power", "ground")


@lru_cache(maxsize=262144)
def _token_relation(a: str, b: str) -> str | None:
    """How two canonical functions relate token by token, or None: the same tokens in
    another order (TX_UART / UART_TX), one name the other plus indexes or a voltage
    (VSS / VSS_1, VREF_7 / VREF_BANK_7), or each token an abbreviation of the other's
    (PROG_B / PROGRAM_B; never for supply names: VDD / VDDIO are other rails). Tokens are
    compared as sequences, so 3.3 V and 3.4 V do not relate."""
    if a == b:
        return "same"
    ta, tb = _tokens(a), _tokens(b)
    if ta == tb:
        return "one name contains the other"  # they differ only in a qualifier
    if Counter(ta) == Counter(tb):
        return "reordered"
    short, long = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    if len(short) < len(long) and _in_order(short, long):
        extra = Counter(long) - Counter(short)
        if all(_minor(token) for token in extra.elements()) and any(
                not token.isdigit() for token in short):
            return "one name contains the other"
    if (len(ta) == len(tb) and not (_supply(a) or _supply(b))
            and all(_abbreviation(x, y) for x, y in zip(ta, tb))):
        return "abbreviated"
    return None


_RELATIONS = ("same", "reordered", "one name contains the other",
              "abbreviated")


def _paired(left, right) -> str | None:
    """Each function of left paired with a different function of right that it relates
    to (one to one, so C1 and CS1 cannot both lean on the table's CS1): the weakest
    relation used, or None when some function has no partner left."""
    left, right = list(left), list(right)
    if not left or len(left) > len(right):
        return None
    options = []
    for function in left:
        found = [(index, relation) for index, other in enumerate(right)
                 if (relation := _token_relation(function, other))]
        if not found:
            return None
        options.append(sorted(found, key=lambda item: _RELATIONS.index(item[1])))
    owner: dict[int, int] = {}

    def assign(index: int, seen: set) -> bool:
        for other, _ in options[index]:
            if other in seen:
                continue
            seen.add(other)
            if other not in owner or assign(owner[other], seen):
                owner[other] = index
                return True
        return False

    if not all(assign(index, set()) for index in range(len(left))):
        return None
    used = [dict(options[index])[other] for other, index in owner.items()]
    return max(used, key=_RELATIONS.index)


def _agrees(extra: set, missing: set) -> bool:
    """Functions the design adds beyond the table do not contradict the table's
    alternates it leaves out: one of the sets is empty, or each added function relates
    token by token to a different left-out one (VREF 7 / VREF Bank 7). IO, LVDS3G_6p
    against IO with the alternate LVDS3D_1n does not agree."""
    if not extra or not missing:
        return True
    return _paired([f.lstrip("~") for f in extra], [f.lstrip("~") for f in missing]) is not None


def classify(design: str, table: str, alternates=()) -> tuple[str, str]:
    """(class, reason) of a design pin name against a table row's name and alternate
    functions: exact, equivalent, likely_same, different, or unnamed (no design name)."""
    d = _name(design or "")
    if not d.functions:
        return UNNAMED, "the design pin has no name"
    t = _name(table or "")
    if d.upper == t.upper:
        return EXACT, ""
    alts = [_name(a) for a in alternates if a]
    full = set(t.keys).union(*(a.keys for a in alts)) if alts else set(t.keys)
    if (d.keys == t.keys or d.compact == t.compact or d.squeezed == t.squeezed
            or (alts and d.keys == full)):
        return EQUIVALENT, SAME_NAME
    if d.keys <= full and d.keys & t.keys:
        return EQUIVALENT, "the design leaves out some of the table's alternate functions"
    if d.keys <= full:
        return LIKELY, "the design names an alternate function"
    if t.keys and t.keys <= d.keys and _agrees(d.keys - full, full - t.keys - d.keys):
        return LIKELY, "the design adds functions to the table's name"
    full_loose = set(t.loose).union(*(a.loose for a in alts)) if alts else set(t.loose)
    loose = set(d.loose)
    if loose <= full_loose or (t.loose and set(t.loose) <= loose and _agrees(
            loose - full_loose, full_loose - set(t.loose) - loose)):
        plain_full = set(t.plain).union(*(a.plain for a in alts)) if alts else set(t.plain)
        if d.plain <= plain_full or (t.plain and t.plain <= d.plain):
            return LIKELY, LOW_DIFFERS
        return LIKELY, "another ground or no-connect name"
    pool = list(t.loose) + [f for a in alts for f in a.loose]
    reason = _paired(d.loose, pool) or _paired(pool, d.loose)
    if reason:
        return LIKELY, "the same tokens" if reason == "same" else reason
    return DIFFERENT, ""


def similar(design: str, table: str, alternates=()) -> bool:
    return classify(design, table, alternates)[0] in SIMILAR


# ---------------------------------------------------------------- the design's pins

ALTIUM_KINDS = {
    "input": "input", "io": "io", "output": "output", "opencollector": "open_drain",
    "openemitter": "open_drain", "passive": "passive", "hiz": "output", "power": "power",
}


def design_kind(electrical: str, name: str = "") -> str:
    kind = ALTIUM_KINDS.get(str(electrical or "").strip().lower(), "unknown")
    if kind == "power" and name_kind(name) == "ground":
        return "ground"
    return kind


@dataclass
class DesignPin:
    number: str
    name: str
    electrical: str = ""  # Altium's electrical type as written
    net: str | None = None
    connected: bool | None = None  # None: unknown (a library symbol)
    hidden: bool = False
    parts: list[int] = field(default_factory=list)


@dataclass
class DesignPins:
    side: str  # placed or library
    pins: dict[str, DesignPin]  # by pin_key
    duplicates: list[dict] = field(default_factory=list)  # one number, different names
    notes: list[str] = field(default_factory=list)


def placed_pins(component: dict) -> DesignPins:
    """A netlist component's pins: every designator (pin_designators, pin_types,
    pin_names and the connected pins), named from pin_names or the connected pin, with
    the net of each connected pin."""
    connected = component.get("pins") or {}
    names = component.get("pin_names") or {}
    types = component.get("pin_types") or {}
    numbers = list(dict.fromkeys([*map(str, component.get("pin_designators") or []),
                                  *map(str, types), *map(str, names)]))
    own = set(numbers)
    notes = []
    foreign = [number for number in map(str, connected) if number not in own]
    if own and foreign:
        notes.append(f"{len(foreign)} connected pins under this designator are not this part's "
                     "pins (another part compiled with the same designator): left out")
    elif not own:
        numbers = list(map(str, connected))
    wired = any((meta or {}).get("net") for meta in connected.values())
    if numbers and not wired:
        notes.append("The netlist puts none of this part's pins on a net (unwired, or a "
                     "designator clash): its connections were not checked")
    pins: dict[str, DesignPin] = {}
    for number in numbers:
        meta = connected.get(number) or {}
        name = names.get(number) or meta.get("name") or ""
        net = meta.get("net") or None
        pins.setdefault(pin_key(number), DesignPin(
            number=number, name=name, electrical=types.get(number, ""), net=net,
            connected=bool(net) if wired else None))
    return DesignPins(SIDE_PLACED, pins, notes=notes)


def symbol_pins(symbol) -> DesignPins:
    """A library symbol's pins (sch_lib.SchLibComponent): every part's pins in the
    normal display mode (a pin drawn only in another mode counts too); a designator
    every part repeats with one name is one pin, with different names a duplicate."""
    by_number: dict[str, list] = defaultdict(list)
    normal = {pin.designator for pin in symbol.pins if pin.display_mode == 0}
    for pin in symbol.pins:
        if not pin.designator:
            continue
        if pin.display_mode != 0 and pin.designator in normal:
            continue
        by_number[pin.designator].append(pin)
    pins: dict[str, DesignPin] = {}
    duplicates = []
    for number, drawn in by_number.items():
        first = drawn[0]
        names = list(dict.fromkeys(pin.name for pin in drawn))
        distinct = {normalize_name(name) for name in names}
        if len(distinct) > 1:
            duplicates.append({"pin": number, "names": names,
                               "parts": sorted({pin.part for pin in drawn})})
        key = pin_key(number)
        if key in pins:
            duplicates.append({"pin": number, "names": [pins[key].name, first.name],
                               "parts": sorted({pin.part for pin in drawn})})
            continue
        pins[key] = DesignPin(number=number, name=first.name, electrical=first.electrical,
                              hidden=all(pin.hidden for pin in drawn),
                              parts=sorted({pin.part for pin in drawn}))
    return DesignPins(SIDE_LIBRARY, pins, duplicates)


def table_from_pins(design: DesignPins) -> PinTable:
    """A pin table holding a design's own pins (number, name, electrical type): what a
    perfect datasheet table for that symbol would say."""
    table = PinTable(package=None, header=["Pin", "Name", "Type"],
                     columns={"number": "Pin", "name": "Name", "type": "Type"},
                     type_source="column")
    for pin in sorted(design.pins.values(), key=lambda p: pin_sort_key(p.number)):
        name = pin.name or pin.number  # an unnamed pin: its number, as compare_side reads it
        table.rows.append(PinRow(number=pin.number, name=name, type=pin.electrical,
                                 kind=row_kind(pin.electrical, name), key=pin_key(pin.number)))
    return table


# ---------------------------------------------------------------- types

_POWERISH = ("power", "ground")
_SIGNAL = ("input", "output", "io", "open_drain")


def type_relation(table: str, design: str) -> str | None:
    """None (compatible), 'info' (a mild difference) or 'warning' for a table pin class
    against the design pin's (from its Altium type)."""
    if table == "unknown" or design == "unknown":
        return None
    if table in _POWERISH:
        return None if design in _POWERISH else "info" if design == "passive" else "warning"
    if design in _POWERISH:
        if table == "nc":
            return "warning"
        return "info" if table in ("analog", "passive") else "warning"
    if table in ("nc", "passive", "analog") or table == design:
        return None
    if design == "passive":
        return "info"
    if table == "input":
        return "warning" if design in ("output", "open_drain") else "info"
    if table == "output":
        return None if design == "open_drain" else "warning" if design == "input" else "info"
    if table == "open_drain":
        return "info" if design in ("output", "io", "input") else None
    if table == "io":
        return "info"
    return None


# ---------------------------------------------------------------- comparing

_EXEMPT_NUMBER = re.compile(r"(?i)(?:sh|shld|shield|mh|mp|mtg|mnt|mount|case)\d{0,3}")
# Shield, mounting and no-connect names: a short form only with a number (SH1, MH_2, NC3;
# SH_CP or SH/LD are signals), a whole word also with a qualifier (SHIELD_GND, MOUNT_PAD).
_EXEMPT_NAME = re.compile(
    r"(?:(?:SH|SHLD|MH|MTG|NC|DNC|NIC|N_C)(?:_?\d+)?|"
    r"(?:SHIELD|SHELL|MOUNT|MOUNTING|MECH|MECHANICAL|CASE|CHASSIS|PEG|HOLE|BOSS|TAB_MOUNT)"
    r"(?:_?\d+|_[A-Z0-9_]+)?)")
_PAD_NUMBER = re.compile(r"(?i)(?:e-?pad|ep|pad|tab|thermal(?: pad)?|exposed(?: thermal)? pad|"
                         r"die pad)")
_PAD_NAME = re.compile(r"(?:EP|EPAD|E_PAD|PAD|THERMAL_PAD|EXPOSED_PAD|DAP|TAB|PGND_EP|GND_EP|"
                       r"EP_GND|GND_PAD)")


def _exempt_extra(pin: DesignPin) -> bool:
    """A shield, mounting or no-connect pin by its number (SH1, MH2) or by every function
    of its name."""
    if _EXEMPT_NUMBER.fullmatch(pin.number.strip()):
        return True
    functions = _name(pin.name).functions
    return bool(functions) and all(_EXEMPT_NAME.fullmatch(f.lstrip("~")) for f in functions)


def _pad_like(number: str, name: str) -> bool:
    functions = _name(name).functions
    return bool(_PAD_NUMBER.fullmatch(number.strip())) or (
        bool(functions) and bool(_PAD_NAME.fullmatch(functions[0].lstrip("~"))))


def _table_pin(row: PinRow) -> dict:
    shown = {"name": row.name}
    if row.alternates:
        shown["alternates"] = row.alternates
    if row.type:
        shown["type"] = row.type
    if row.kind != "unknown":
        shown["class"] = row.kind
    return shown


def _design_pin(pin: DesignPin) -> dict:
    shown = {"name": pin.name, "type": pin.electrical}
    if pin.net is not None:
        shown["net"] = pin.net
    if pin.hidden:
        shown["hidden"] = True
    return shown


@dataclass
class _Context:
    rows: dict[str, PinRow]
    design: DesignPins
    findings: list[dict] = field(default_factory=list)
    status: dict[str, tuple[str, str]] = field(default_factory=dict)
    counts: Counter = field(default_factory=Counter)
    equivalent_examples: list[dict] = field(default_factory=list)
    partial: bool = False


def _finding(kind: str, severity: str, message: str, **detail) -> dict:
    return {"kind": kind, "severity": severity, "message": message, **detail}


def _without_prefix(keys: list[str]) -> tuple[str, dict[str, str]] | None:
    """(prefix, key -> number without it) when nine in ten keys carry one letter prefix
    (P1..P208)."""
    prefixes = Counter(re.match(r"[A-Z]*", key).group() for key in keys)
    prefix, count = prefixes.most_common(1)[0] if prefixes else ("", 0)
    if not prefix or count < 0.9 * len(keys):
        return None
    return prefix, {key: key[len(prefix):] if key.startswith(prefix) and key[len(prefix):].isdigit()
                    else key for key in keys}


def number_mapping(table_keys: list[str], design_keys: list[str]) -> tuple[dict[str, str], str | None]:
    """Table keys mapped to the design's numbering: as they are, or without a prefix
    every table number shares (P1..P208 against 1..208), or with the prefix every design
    number shares (1..8 against P1..P8), when that pairs more pins."""
    direct = {key: key for key in table_keys}
    design = set(design_keys)
    needed = max(len(design & set(table_keys)), 0.5 * len(table_keys))
    stripped = _without_prefix(table_keys)
    if stripped and len(design & set(stripped[1].values())) > needed:
        return stripped[1], stripped[0]
    added = _without_prefix(list(design_keys))
    if added:
        back = {number: key for key, number in added[1].items() if number != key}
        mapped = {key: back.get(key, key) for key in table_keys}
        if len(design & set(mapped.values())) > needed:
            return mapped, added[0]
    return direct, None


def compare_side(table: PinTable, design: DesignPins) -> dict:
    """The table against one side's pins: (findings, counts, status, notes)."""
    notes: list[str] = list(design.notes)
    mapping, prefix = number_mapping([row.key for row in table.rows], list(design.pins))
    if prefix and any(number.startswith(prefix) and number != key
                      for key, number in mapping.items()):
        notes.append(f"the design's pin numbers carry the prefix '{prefix}' (as in "
                     f"{prefix}1, {prefix}2) and the table's do not: compared with it")
    elif prefix:
        notes.append(f"the table's pin numbers carry the prefix '{prefix}' (as in "
                     f"{prefix}1, {prefix}2): compared without it")
    rows: dict[str, PinRow] = {}
    findings: list[dict] = []
    for row in table.rows:
        key = mapping.get(row.key, row.key)
        held = rows.get(key)
        if held is None:
            rows[key] = row
            continue
        if classify(row.name, held.name, held.alternates)[0] not in (EXACT, EQUIVALENT):
            findings.append(_finding(
                "duplicate_number", "warning",
                f"The table lists pin {row.number} twice with different names "
                f"({held.name!r}, {row.name!r}); the first is compared.",
                pin=row.number, side="table", names=[held.name, row.name],
                lines=[held.line, row.line]))
    context = _Context(rows, design, findings)
    common = [key for key in rows if key in design.pins]
    for key in common:
        row, pin = rows[key], design.pins[key]
        if not pin.name.strip() or pin_key(pin.name) == key:
            # An unnamed pin (or one named by its number, as connectors often are).
            context.status[key] = ((EXACT, "") if pin_key(row.name) in (key, row.key)
                                   else (UNNAMED, "the design pin has no name"))
            continue
        context.status[key] = classify(pin.name, row.name, row.alternates)
    _claimed(context, common)
    # A run of names offset by one pin first: two of its pins can look exchanged.
    _shifts(context)
    _swaps(context, common)
    _names(context, common)
    _missing_and_extra(context)
    _types_and_connections(context, common)
    _duplicates(context)
    if context.partial:
        notes.append(f"The table lists {len(rows)} of the part's {len(design.pins)} pins: a "
                     "partial table. The pins it leaves out are listed once "
                     "(not_in_partial_table, info) and were not checked; give the whole "
                     "table to check them.")
    counts = context.counts
    for cls, _ in context.status.values():
        counts[cls] += 1
    counts["pins_in_table"] = len(rows)
    counts["pins_in_design"] = len(design.pins)
    counts["compared"] = len(common)
    return {"findings": context.findings, "counts": dict(counts), "notes": notes,
            "status": context.status, "rows": rows,
            "equivalent_examples": context.equivalent_examples}


def _claimed(context: _Context, common: list[str]) -> None:
    """A name that only resembles its pin's but is another pin's own name is a different
    name: the table naming pin 3 CLK_N where the design has CLK_P, while the design's pin
    4 is CLK_N (likewise CE / CEO, OUT_A / OUT_B, a pin and its complement, or a name
    copied from the next row). The other pin must be the only one holding that name on
    its side; names many pins share (IO, GPIO, supply and ground names, NC) are left
    alone."""
    rows, pins = context.rows, context.design.pins
    design_by: dict[str, list[str]] = defaultdict(list)
    table_by: dict[str, list[str]] = defaultdict(list)
    for key, pin in pins.items():
        if pin.name.strip():
            design_by[_name(pin.name).compact].append(key)
    for key, row in rows.items():
        table_by[_name(row.name).compact].append(key)
    for key in common:
        cls, reason = context.status[key]
        if cls not in (EQUIVALENT, LIKELY) or reason == SAME_NAME:
            continue
        row, pin = rows[key], pins[key]
        theirs = _name(row.name).compact
        if reason == LOW_DIFFERS and len(table_by.get(theirs, [])) > 1:
            continue  # Q and Q for Q and Q-bar: the table lost its overlines (a PDF copy)
        holders = design_by.get(theirs, [])
        if len(holders) == 1 and holders[0] != key and not generic_name(row.name):
            context.status[key] = (
                DIFFERENT, f"the design gives the table's name to pin {pins[holders[0]].number}")
            continue
        holders = table_by.get(_name(pin.name).compact, [])
        if len(holders) == 1 and holders[0] != key and not generic_name(pin.name):
            context.status[key] = (
                DIFFERENT, f"the table gives the design's name to pin {rows[holders[0]].number}")


def _different(context: _Context) -> list[str]:
    return sorted((key for key, (cls, _) in context.status.items() if cls == DIFFERENT),
                  key=pin_sort_key)


def _swaps(context: _Context, common: list[str]) -> None:
    rows, pins = context.rows, context.design.pins
    different = _different(context)
    if len(different) < 2:
        return
    if len(different) <= BRUTE_SWAP_LIMIT:
        candidates = {key: different for key in different}
    else:
        index: dict[str, set[str]] = defaultdict(set)
        for key in different:
            row = rows[key]
            for function in {*_name(row.name).loose, *(f for a in row.alternates
                                                        for f in _name(a).loose)}:
                index[function].add(key)
        candidates = {}
        for key in different:
            found: set[str] = set()
            for function in _name(pins[key].name).loose:
                held = index.get(function, set())
                if len(held) <= 3:
                    found |= held
            candidates[key] = sorted(found, key=pin_sort_key)
    used: set[str] = set()

    def strict(design: str, row: PinRow) -> bool:
        cls, reason = classify(design, row.name, row.alternates)
        return cls == EXACT or reason == SAME_NAME

    def loose(design: str, row: PinRow) -> bool:
        return similar(design, row.name, row.alternates)

    # Names exchanged exactly first, so a loosely similar name does not take another
    # pin's partner.
    for test in (strict, loose):
        for key in different:
            if key not in used:
                _swap_one(context, key, candidates[key], used, test)


def _swap_one(context: _Context, key: str, candidates: list[str], used: set[str],
              test) -> None:
    """The first candidate whose names are exchanged with key's (test both ways)."""
    rows, pins = context.rows, context.design.pins
    for other in candidates:
        if other == key or other in used:
            continue
        a, b = rows[key], rows[other]
        if not (test(pins[key].name, b) and test(pins[other].name, a)):
            continue
        used.update((key, other))
        context.status[key] = context.status[other] = ("swapped", "")
        first, second = sorted((key, other), key=pin_sort_key)
        ra, rb = rows[first], rows[second]
        context.findings.append(_finding(
            "swapped", "error",
            f"Pins {ra.number} and {rb.number} are swapped: the design names pin "
            f"{ra.number} {pins[first].name!r} and pin {rb.number} "
            f"{pins[second].name!r}; the table has them the other way round.",
            pins=[pins[first].number, pins[second].number],
            table={ra.number: _table_pin(ra), rb.number: _table_pin(rb)},
            design={pins[first].number: _design_pin(pins[first]),
                    pins[second].number: _design_pin(pins[second])}))
        context.counts["swapped_pairs"] += 1
        return


def _shifts(context: _Context) -> None:
    rows, pins = context.rows, context.design.pins
    if len(_different(context)) < SHIFT_MIN_RUN:
        return
    sequence = sorted(rows, key=pin_sort_key)
    runs = []
    for offset in SHIFT_OFFSETS:
        run: list[str] = []
        for position, key in enumerate(sequence + [None]):
            ok = False
            if key is not None and context.status.get(key, ("",))[0] == DIFFERENT:
                target = position + offset
                if 0 <= target < len(sequence):
                    other = rows[sequence[target]]
                    ok = similar(pins[key].name, other.name, other.alternates)
            if ok:
                run.append(key)
                continue
            if len(run) >= SHIFT_MIN_RUN:
                runs.append((offset, list(run)))
            run = []
    runs.sort(key=lambda item: (-len(item[1]), abs(item[0]), item[0] < 0))
    position = {key: index for index, key in enumerate(sequence)}
    for offset, run in runs:
        run = [key for key in run if context.status[key][0] == DIFFERENT]
        if len(run) < SHIFT_MIN_RUN:
            continue
        for key in run:
            context.status[key] = ("shifted", "")
        first, last = rows[run[0]], rows[run[-1]]
        examples = []
        for key in run[:EXAMPLES]:
            other = rows[sequence[position[key] + offset]]
            examples.append({"pin": pins[key].number, "design": pins[key].name,
                             "table_at_pin": rows[key].name,
                             "table_pin_with_that_name": other.number})
        direction = "later" if offset > 0 else "earlier"
        context.findings.append(_finding(
            "shifted", "error",
            f"Pins {first.number} to {last.number} ({len(run)} pins) carry the names the "
            f"table gives {abs(offset)} pin{'s' if abs(offset) > 1 else ''} {direction}: a "
            "pin inserted or left out of the numbering.",
            pins=[pins[key].number for key in run], first=first.number, last=last.number,
            offset=offset, count=len(run), examples=examples))
        context.counts["shifted_pins"] += len(run)


def _group(context: _Context, kind: str, severity: str, message: str, entries: list[dict],
           **detail) -> None:
    if not entries:
        return
    entries = sorted(entries, key=lambda e: pin_sort_key(e["pin"]))
    finding = _finding(kind, severity, message, count=len(entries),
                       pins=entries[:GROUP_PINS], **detail)
    if len(entries) > GROUP_PINS:
        finding["pins_omitted"] = len(entries) - GROUP_PINS
    context.findings.append(finding)


def _names(context: _Context, common: list[str]) -> None:
    rows, pins = context.rows, context.design.pins
    likely: dict[str, list[dict]] = defaultdict(list)
    unnamed = []
    for key in sorted(common, key=pin_sort_key):
        cls, reason = context.status[key]
        row, pin = rows[key], pins[key]
        if cls == DIFFERENT:
            detail = {"why": reason} if reason else {}
            context.findings.append(_finding(
                "different_name", "error",
                f"Pin {pin.number} is {pin.name!r} in the design but {row.name!r} in the "
                "table" + (f" ({reason})." if reason else "."), pin=pin.number,
                table=_table_pin(row), design=_design_pin(pin), **detail))
        elif cls == LIKELY:
            likely[reason].append({"pin": pin.number, "design": pin.name, "table": row.name})
        elif cls == UNNAMED:
            unnamed.append({"pin": pin.number, "table": row.name})
        elif cls == EQUIVALENT and len(context.equivalent_examples) < EXAMPLES:
            context.equivalent_examples.append(
                {"pin": pin.number, "design": pin.name, "table": row.name, "why": reason})
    for reason, entries in sorted(likely.items()):
        _group(context, "likely_same_name", "info",
               f"Names that differ but likely mean the same pin: {reason}.", entries,
               reason=reason)
    _group(context, "unnamed_design_pins", "info",
           "Design pins without a name: only their numbers could be compared.", unnamed)


def _missing_and_extra(context: _Context) -> None:
    rows, pins = context.rows, context.design.pins
    missing = [key for key in rows if key not in pins]
    extra = [key for key in pins if key not in rows]
    # An exposed pad the table and the design number differently (EP against 33).
    pads_t = [key for key in missing if _pad_like(rows[key].number, rows[key].name)]
    numeric = [int(key) for key in rows if key.isdigit()]
    top = max(numeric) if numeric else None
    pads_d = [key for key in extra if _pad_like(pins[key].number, pins[key].name) or (
        top is not None and key.isdigit() and int(key) == top + 1
        and design_kind(pins[key].electrical, pins[key].name) in _POWERISH + ("passive",))]
    if len(pads_t) == 1 and len(pads_d) == 1:
        row, pin = rows[pads_t[0]], pins[pads_d[0]]
        context.findings.append(_finding(
            "exposed_pad_numbered_differently", "info",
            f"The table's {row.number} ({row.name}) is taken as the design's pin "
            f"{pin.number} ({pin.name}): an exposed pad numbered differently. Check the "
            "footprint's pad number matches the symbol's.",
            pins=[{"pin": pin.number, "design": pin.name, "table_pin": row.number,
                   "table": row.name}]))
        missing.remove(pads_t[0])
        extra.remove(pads_d[0])
        context.counts["exposed_pad_paired"] += 1
    nc_missing = []
    for key in sorted(missing, key=pin_sort_key):
        row = rows[key]
        if row.kind == "nc":
            nc_missing.append({"pin": row.number, "table": row.name})
            continue
        context.findings.append(_finding(
            "missing_in_design", "error",
            f"Pin {row.number} ({row.name}) is in the table but not in the design.",
            pin=row.number, table=_table_pin(row)))
        context.counts["missing_in_design"] += 1
    _group(context, "nc_not_in_design", "info",
           "No-connect pins of the table that the symbol does not draw (common practice; "
           "the footprint should still have their pads).", nc_missing)
    context.counts["nc_not_in_design"] += len(nc_missing)
    exempt, extras = [], []
    for key in sorted(extra, key=pin_sort_key):
        pin = pins[key]
        if _exempt_extra(pin):
            exempt.append({"pin": pin.number, "design": pin.name})
        else:
            extras.append(pin)
    if len(rows) < PARTIAL_TABLE_SHARE * len(pins) and len(extras) > 1:
        # A table of some of the pins (one bank, the configuration pins): the rest are
        # listed once, not one warning each.
        context.partial = True
        _group(context, "not_in_partial_table", "info",
               f"The table lists {len(rows)} of the design's {len(pins)} pins (a partial "
               "table): these design pins were not compared.",
               [{"pin": pin.number, "design": pin.name} for pin in extras])
        context.counts["not_in_partial_table"] += len(extras)
        extras = []
    for pin in extras:
        context.findings.append(_finding(
            "extra_in_design", "warning",
            f"Pin {pin.number} ({pin.name or 'no name'}) is in the design but not in the "
            "table.", pin=pin.number, design=_design_pin(pin)))
        context.counts["extra_in_design"] += 1
    _group(context, "extra_exempt", "info",
           "Design pins not in the table that are shield, mounting or no-connect pins by "
           "their number or name.", exempt)
    context.counts["extra_exempt"] += len(exempt)


def _types_and_connections(context: _Context, common: list[str]) -> None:
    rows, pins = context.rows, context.design.pins
    soft: dict[tuple, list[dict]] = defaultdict(list)
    hidden_power, nc_connected = [], []
    for key in sorted(common, key=pin_sort_key):
        cls = context.status[key][0]
        if cls not in (*SIMILAR, UNNAMED):
            continue
        row, pin = rows[key], pins[key]
        theirs = row.kind
        ours = design_kind(pin.electrical, pin.name)
        relation = type_relation(theirs, ours)
        if relation == "warning":
            context.findings.append(_finding(
                "type_mismatch", "warning",
                f"Pin {pin.number} ({pin.name}) is typed {pin.electrical or 'unknown'} in the "
                f"design; the table makes it {theirs} ({row.type or 'from its name'}).",
                pin=pin.number, table=_table_pin(row), design=_design_pin(pin)))
            context.counts["type_mismatch"] += 1
        elif relation == "info":
            soft[(theirs, pin.electrical)].append({"pin": pin.number, "design": pin.name,
                                                   "table": row.name})
        if pin.hidden:
            if theirs in _POWERISH:
                hidden_power.append({"pin": pin.number, "design": pin.name})
            elif theirs in _SIGNAL or theirs == "analog":
                context.findings.append(_finding(
                    "hidden_signal_pin", "warning",
                    f"Pin {pin.number} ({pin.name}) is hidden in the symbol but the table makes "
                    f"it a {theirs} pin: a hidden pin connects only to the net named after it.",
                    pin=pin.number, table=_table_pin(row), design=_design_pin(pin)))
        if pin.connected is False and theirs in _POWERISH:
            context.findings.append(_finding(
                "power_pin_unconnected", "warning",
                f"Pin {pin.number} ({pin.name}) is a {theirs} pin in the table but is on no "
                "net in the design.", pin=pin.number, table=_table_pin(row),
                design=_design_pin(pin)))
            context.counts["power_pin_unconnected"] += 1
        if pin.connected and theirs == "nc":
            nc_connected.append({"pin": pin.number, "design": pin.name, "net": pin.net})
    for (theirs, electrical), entries in sorted(soft.items()):
        _group(context, "type_differs", "info",
               f"The table makes these {theirs} pins; the design types them "
               f"{electrical or 'unknown'} (compatible, but less precise for ERC).",
               entries, table_class=theirs, design_type=electrical)
    _group(context, "hidden_power_pins", "info",
           "Power pins hidden in the symbol: each connects to the net named after it. Check "
           "that net is the right rail.", hidden_power)
    _group(context, "nc_pin_connected", "info",
           "No-connect pins of the table that are on a net in the design: check the datasheet "
           "allows it.", nc_connected)


def _duplicates(context: _Context) -> None:
    rows, design = context.rows, context.design
    for duplicate in design.duplicates:
        context.findings.append(_finding(
            "duplicate_number", "warning",
            f"The symbol draws pin {duplicate['pin']} more than once with different names: "
            + ", ".join(repr(name) for name in duplicate["names"]) + ".",
            pin=duplicate["pin"], side=design.side, names=duplicate["names"],
            parts=duplicate["parts"]))

    def by_name(items):
        grouped = defaultdict(list)
        for key, number, name in items:
            if name and not generic_name(name):
                grouped[normalize_name(name)].append((key, number, name))
        return {key: value for key, value in grouped.items() if len(value) > 1}

    # Over the pins both sides have: a row left out of the table (reported as such) does
    # not make the design's other pins of that name duplicates.
    table_names = by_name((key, row.number, row.name) for key, row in rows.items()
                          if key in design.pins)
    design_names = by_name((key, pin.number, pin.name) for key, pin in design.pins.items()
                           if key in rows)
    for side, names, other in (("table", table_names, design_names),
                               (design.side, design_names, table_names)):
        for key, items in sorted(names.items()):
            if len(other.get(key, [])) == len(items):
                continue  # the other side repeats the name as often: by design
            if all(context.status.get(key, ("",))[0] in SIMILAR for key, _, _ in items):
                continue  # each pin matches its counterpart: a naming style (units)
            numbers = sorted((number for _, number, _ in items), key=pin_sort_key)
            context.findings.append(_finding(
                "duplicate_name", "warning",
                f"The {'table' if side == 'table' else 'design'} gives the name "
                f"{items[0][2]!r} to {len(items)} pins: {', '.join(numbers)}.",
                side=side, name=items[0][2], pins=numbers))
            context.counts["duplicate_names"] += 1


# ---------------------------------------------------------------- the whole check

_SIDE_DETAILS = ("net", "hidden")  # what one side's design pin shows and the other's not


def _side_free(value):
    """A finding's design detail without what differs between the sides for one pin:
    the placed pin's net, the symbol's hidden mark, the spelling of the name (a placed
    part and its symbol can write it in another case or with other separators)."""
    if isinstance(value, dict):
        return {k: normalize_name(v) if k == "name" else _side_free(v)
                for k, v in value.items() if k not in _SIDE_DETAILS}
    if isinstance(value, list):
        return [_side_free(v) for v in value]
    return value


def _signature(finding: dict) -> str:
    """What makes two sides' findings one: all but the sides, the message (it quotes the
    design's spelling) and the per-side details of the design pins."""
    shown = {k: _side_free(v) if k == "design" else v for k, v in finding.items()
             if k not in ("in", "message")}
    return json.dumps(shown, sort_keys=True, default=str)


def _merge_design(held, new):
    """The design detail of a finding both sides have: each side's net and hidden mark
    kept (the placed pin's net, the symbol's hidden mark)."""
    if isinstance(held, dict) and isinstance(new, dict):
        merged = dict(held)
        for key, value in new.items():
            merged[key] = _merge_design(held[key], value) if key in held else value
        return merged
    return held


def _sort_key(finding: dict):
    order = {kind: index for index, kind in enumerate(KIND_ORDER)}
    pin = finding.get("pin") or finding.get("first") or (
        finding["pins"][0] if finding.get("pins") and isinstance(finding["pins"][0], str)
        else (finding["pins"][0]["pin"] if finding.get("pins") else ""))
    return (SEVERITY_ORDER[finding["severity"]], order.get(finding["kind"], 99),
            pin_sort_key(str(pin)))


def choose_table(source: PinSource, package: str | None, design: DesignPins | None):
    """(table, selection info) for package= (matched case-insensitively, ignoring spaces
    and dashes, or contained in or containing a package name), else the table that
    pairs the most pins with the design by number and name. Raises ValueError with the
    packages when package= names none of them."""
    tables = source.tables
    if not tables:
        raise ValueError("the table has no pins")
    if len(tables) == 1:
        info = {"package": tables[0].package}
        if package and tables[0].package is None:
            info["note"] = "package= was not used: the table has one pin number column"
        return tables[0], info

    def norm(text):
        return re.sub(r"[\s\-_]", "", str(text or "")).lower()

    if package:
        wanted = norm(package)
        exact = [t for t in tables if t.package and norm(t.package) == wanted]
        loose = [t for t in tables if t.package and (norm(t.package) in wanted
                                                      or wanted in norm(t.package))]
        found = exact or (loose if len(loose) == 1 else [])
        if not found:
            raise ValueError(", ".join(source.packages))
        return found[0], {"package": found[0].package, "how": "package="}
    scores = {}
    for table in tables:
        if design is None:
            scores[table.package] = len(table.rows)
            continue
        mapping, _ = number_mapping([row.key for row in table.rows], list(design.pins))
        score = 0
        for row in table.rows:
            pin = design.pins.get(mapping.get(row.key, row.key))
            if pin is not None and classify(pin.name, row.name, row.alternates)[0] in (
                    EXACT, EQUIVALENT):
                score += 1
        scores[table.package] = score
    best = max(tables, key=lambda t: scores[t.package])
    return best, {"package": best.package,
                  "how": "the package whose pins match the design best (pass package= to "
                         "choose another)",
                  "pins_matched_by_package": scores}


def check_pins(source: PinSource, sides: list[DesignPins], package: str | None = None) -> dict:
    """check_pin_table's model: the chosen table against each side's pins, findings
    merged across sides (in: the sides that have each), counts per side and, with both
    sides, how the placed part differs from its library symbol."""
    table, selection = choose_table(source, package, sides[0] if sides else None)
    merged: dict[str, dict] = {}
    per_side: dict[str, dict] = {}
    notes: list[str] = list(source.notes)
    for design in sides:
        result = compare_side(table, design)
        per_side[design.side] = {**result["counts"]}
        if result["equivalent_examples"]:
            per_side[design.side]["equivalent_examples"] = result["equivalent_examples"]
        for note in result["notes"]:
            if note not in notes:
                notes.append(note)
        for finding in result["findings"]:
            signature = _signature(finding)
            if signature in merged:
                held = merged[signature]
                held["in"].append(design.side)
                if "design" in finding:
                    held["design"] = _merge_design(held["design"], finding["design"])
            else:
                merged[signature] = {**finding, "in": [design.side]}
    findings = sorted(merged.values(), key=_sort_key)
    severity = Counter(f["severity"] for f in findings)
    by_kind = Counter(f["kind"] for f in findings)
    poor = bool(per_side)
    for side, counts in per_side.items():
        # Swapped and shifted pins carry names of this part: they count as matched.
        matched = sum(counts.get(cls, 0) for cls in (EXACT, EQUIVALENT, LIKELY, "swapped",
                                                     "shifted"))
        share = matched / counts["pins_in_table"] if counts.get("pins_in_table") else 0.0
        counts["match_share"] = round(share, 3)
        if counts.get("pins_in_table") and share < MIN_MATCH_SHARE:
            notes.insert(0,
                f"Only {matched} of the table's {counts['pins_in_table']} pins match the {side} "
                "part's by number and name: the table may be for another part or package "
                "(see packages), or number the pins another way; the findings then mostly say "
                "so, not that the part is wrong.")
        else:
            poor = False
    verdict = ("poor_match" if poor else "differences" if severity["error"] or severity["warning"]
               else "match")
    model = {
        "table_used": {"package": selection.get("package"), "rows": len(table.rows),
                       **{k: v for k, v in selection.items() if k != "package"}},
        "summary": {"verdict": verdict, "by_side": per_side, "severity_counts": dict(severity),
                    "finding_kinds": dict(sorted(by_kind.items()))},
        "findings": findings,
        "notes": notes,
        "_table": table,
    }
    if len(sides) == 2:
        model["placed_vs_library"] = placed_vs_library(*sides)
    return model


def placed_vs_library(placed: DesignPins, library: DesignPins) -> dict:
    """How the placed part's pins differ from its library symbol's: a symbol edited in
    the library after placement (Update From Libraries brings it over) or edited on the
    sheet."""
    only_placed = sorted((p.number for k, p in placed.pins.items() if k not in library.pins),
                         key=pin_sort_key)
    only_library = sorted((p.number for k, p in library.pins.items() if k not in placed.pins),
                          key=pin_sort_key)
    renamed, retyped = [], []
    for key, pin in placed.pins.items():
        other = library.pins.get(key)
        if other is None:
            continue
        if normalize_name(pin.name) != normalize_name(other.name):
            renamed.append({"pin": pin.number, "placed": pin.name, "library": other.name})
        if pin.electrical and other.electrical and pin.electrical != other.electrical:
            retyped.append({"pin": pin.number, "placed": pin.electrical,
                            "library": other.electrical})
    result = {"same": not (only_placed or only_library or renamed or retyped)}
    if not result["same"]:
        result.update({
            "pins_only_placed": only_placed[:GROUP_PINS], "pins_only_library": only_library[:GROUP_PINS],
            "renamed": sorted(renamed, key=lambda e: pin_sort_key(e["pin"]))[:GROUP_PINS],
            "retyped": sorted(retyped, key=lambda e: pin_sort_key(e["pin"]))[:GROUP_PINS],
            "counts": {"only_placed": len(only_placed), "only_library": len(only_library),
                       "renamed": len(renamed), "retyped": len(retyped)},
            "note": "The placed part differs from its library symbol: the symbol was edited "
                    "after placement (Tools > Update From Libraries brings it over) or on the "
                    "sheet. Findings say which side (in) has them."})
    return result


def table_description(source: PinSource, table: PinTable) -> dict:
    """The table as read: source, format, delimiter, columns, packages, rows and the
    rows that could not be read (first 20)."""
    described = {"source": source.source, "format": source.format}
    if source.delimiter:
        described["delimiter"] = source.delimiter
    if source.sheet:
        described["sheet"] = source.sheet
    if source.device:
        described["device"] = source.device
    described["columns"] = table.columns
    if source.packages:
        described["packages"] = source.packages
        described["package"] = table.package
        described["rows_not_in_package"] = table.not_in_package
    described["pins"] = len(table.rows)
    described["types_from"] = table.type_source
    if table.declared_pins is not None:
        described["declared_pins"] = table.declared_pins
        if table.declared_pins != len(table.rows):
            described["declared_pins_differ"] = True
    described["unread_rows_total"] = table.unread_total
    if table.unread:
        described["unread_rows"] = [{"line": row.line, "text": row.text, "reason": row.reason}
                                    for row in table.unread[:20]]
    return described


def page(model: dict, offset: int, limit: int) -> dict:
    """The model with its findings paged (offset from next_offset)."""
    offset = max(0, int(offset or 0))
    findings = model["findings"]
    result = {key: value for key, value in model.items() if not key.startswith("_")}
    result["findings"] = findings[offset:offset + limit]
    result["findings_total"] = len(findings)
    result["offset"] = offset
    result["has_more"] = offset + limit < len(findings)
    if result["has_more"]:
        result["next_offset"] = offset + limit
    return result


__all__ = ["classify", "similar", "placed_pins", "symbol_pins", "table_from_pins",
           "check_pins", "compare_side", "choose_table", "page", "table_description",
           "placed_vs_library", "type_relation", "design_kind", "number_mapping",
           "DesignPin", "DesignPins", "type_kind"]
