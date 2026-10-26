"""Pin tables from datasheets and vendor pinout files (roadmap #22), for comparing them
with a part's pins (services/pin_table.py, check_pin_table).

Input is a table pasted as text, a file (.csv, .tsv, .txt, or an .xlsx workbook read
with fab_outputs.xlsx_sheets) or a vendor pinout file recognised by its header row:

- AMD/Xilinx package pin files (.txt aligned with spaces, or .csv): a "Device/Package"
  line, the header "Pin  Pin Name  Memory Byte Group  Bank  VCCAUX Group  Super Logic
  Region  I/O Type  No-Connect" (the columns vary by family), one row per pin and a
  "Total Number of Pins N" footer. I/O Type holds the bank type (HR, HP, GTP), not a
  direction, so pin types come from the names (GND, VCCO_34, IO_L1P_T0_34, NC).
- Intel/Altera pin-out files (.txt, tab-separated, CSV-style quotes): a "Pin Information
  for the ... Device" preamble, then one section per package, each with its header row
  "Bank Number, VREF, Pin Name/Function, Optional Function(s), Configuration Function,
  ..., <package>, DQS for X4, ...". The package column (F34, NF40, U484) holds the
  ball; a pin missing from a package has an empty cell there. Older files put several
  package columns in one header row.
- STM32CubeMX pinout CSV: "Position","Name","Type","Signal","Label"; ST datasheet tables
  with a column per package (LQFP64, UFQFPN48, ...).

Any other table is read by its header names (pin number or ball, pin name, alternate
functions, type or direction, bank or unit), or without a header from what its columns
hold. Pasted text is split on tabs, commas, semicolons, pipes (Markdown tables), runs
of two or more spaces (text copied from a PDF) or single spaces, whichever reads the
most rows. With single spaces, a header's multi-word names are joined back ("Pin
Number", "Pin Name") and a row's extra words go to the name, the type or a last column
of free text, whichever leaves a pin number and a type word in their columns (rows no
way fits are listed as unread). A list of pins in one cell ("4, 9, 15" or "12-15")
gives one row per pin. Rows that could not be read are kept with their line and reason.
A table's name cells lose their footnote marks ((1), *, superscripts), except an
attached (n) the table uses as a bus index (D(0), D(1)).

Names are normalised for comparison (function_key): case, separators (_ - / . space),
"I/O" and "N/C", leading zeros, D(3) as D3, and the active-low notations nRESET,
RESETn, ~RESET, !RESET, /RESET, RESET#, RESET_N, RESET_B, Altium's R\\E\\S\\E\\T\\ and a
Unicode overline all read as one active-low RESET (RESET* too in a design's name; in a
table's cell a trailing * is a footnote mark). A name holding several functions
("PA9/SERX9_TXO", "I/O, VREF 7, L49P_Y", "IO (DIN, D0)", "OE/R\\E\\S\\E\\T\\") is split
into them (split_functions); alternates are kept apart from the primary name.
"""

import csv
import io
import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

try:
    from parsers.fab_outputs import decode_lines, xlsx_sheets
except ImportError:
    from server.parsers.fab_outputs import decode_lines, xlsx_sheets

MAX_TABLE_BYTES = 32 << 20
UNREAD_KEPT = 200  # unread rows kept per table (all are counted)

FORMAT_GENERIC = "generic"
FORMAT_XILINX = "amd_xilinx_package_file"
FORMAT_INTEL = "intel_pinout_file"
FORMAT_STM32 = "stm32_pinout_csv"

DELIMITERS = ("tab", "pipe", "semicolon", "comma", "spaces", "space")
TEXT_SUFFIXES = (".csv", ".tsv", ".txt", ".tab", ".prn", ".dat", ".md", "")
XLSX_SUFFIXES = (".xlsx", ".xlsm")

# Pin classes a type or a name maps to.
KINDS = ("power", "ground", "input", "output", "io", "open_drain", "analog", "passive", "nc",
         "unknown")


class PinTableError(ValueError):
    """A table that cannot be read at all; code is the tool's error code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class UnreadRow:
    line: int  # 1-based line of the text, or row of the worksheet
    text: str
    reason: str


@dataclass
class PinRow:
    number: str  # one pin number or ball, as written
    name: str  # the pin name as written (may hold several functions)
    alternates: list[str] = field(default_factory=list)  # other function cells, as written
    type: str = ""  # type or direction as written ("" when the table gives none)
    kind: str = "unknown"  # one of KINDS: from type, else from the name
    bank: str = ""
    line: int = 0
    key: str = ""  # pin_key(number)


@dataclass
class PinTable:
    """One numbering of a table: its rows for one package (package None when the
    table has a single number column)."""

    package: str | None
    header: list[str]
    columns: dict[str, object]  # role -> header cell (alternates: a list)
    rows: list[PinRow] = field(default_factory=list)
    unread: list[UnreadRow] = field(default_factory=list)
    unread_total: int = 0
    not_in_package: int = 0  # rows with an empty cell in this package's column
    declared_pins: int | None = None  # a "Total Number of Pins N" footer
    type_source: str = "none"  # "column", "column and names", "names", "none"


@dataclass
class PinSource:
    source: str
    format: str
    delimiter: str | None
    tables: list[PinTable]
    sheet: str | None = None
    device: str | None = None
    notes: list[str] = field(default_factory=list)
    headed: bool = False  # read under a header row (not inferred from the values)

    @property
    def packages(self) -> list[str]:
        return [table.package for table in self.tables if table.package]

    @property
    def rows_read(self) -> int:
        return max((len(table.rows) for table in self.tables), default=0)


# ---------------------------------------------------------------- pin numbers

_PIN = re.compile(r"[A-Za-z]{0,4}\d{1,4}[A-Za-z]{0,2}")
_PAD_WORDS = re.compile(
    r"(?i)(?:e-?pad|ep|pad|tab|thermal(?: pad)?|exposed(?: thermal)? pad|die pad|"
    r"(?:sh|shield|mh|mp|mtg|mnt)\d{0,3})")
_SUPERSCRIPTS = "\u00b9\u00b2\u00b3\u2070-\u2079"
_FOOTNOTE = re.compile(rf"\s*(?:\(\d{{1,2}}\)|\*+|[{_SUPERSCRIPTS}]+)\s*$")
# The marks that are footnotes in a design's own pin name too: a (n) after a space, two
# or more stars, superscript digits. An attached (n) is a bus index there (D(0)), and a
# single attached * the old active-low mark (RESET*).
_LOOSE_FOOTNOTE = re.compile(rf"(?:\s+\(\d{{1,2}}\)|\s+\*+|\*{{2,}}|\s*[{_SUPERSCRIPTS}]+)\s*$")
_ATTACHED_INDEX = re.compile(r"^(.*[^\W_])\((\d{1,3})\)$")
_RANGE = re.compile(r"^(\d{1,4})\s*(?:-|\u2013|\u2014|to|\.\.)\s*(\d{1,4})$", re.IGNORECASE)
RANGE_LIMIT = 64


def strip_footnote(text: str) -> str:
    """A cell without a trailing footnote mark: (1), [2], *, superscript digits."""
    previous = None
    text = text.strip()
    while previous != text:
        previous = text
        text = _FOOTNOTE.sub("", text)
    return text


def _strip_loose_footnote(text: str) -> str:
    previous = None
    text = text.strip()
    while previous != text:
        previous = text
        text = _LOOSE_FOOTNOTE.sub("", text).strip()
    return text


def bus_bases(names) -> set[str]:
    """The bare names (upper case) a table writes with several different attached (n)
    marks: D(0), D(1), D(2) are a bus written with parentheses, not three footnotes."""
    marks: dict[str, set[str]] = {}
    for name in names:
        match = _ATTACHED_INDEX.match(_strip_loose_footnote(str(name or "")))
        if match:
            marks.setdefault(match.group(1).upper(), set()).add(match.group(2))
    return {base for base, seen in marks.items() if len(seen) > 1}


def clean_name(cell: str, buses: set[str] = frozenset()) -> str:
    """A table's name cell without its footnote marks; an attached (n) is kept when the
    table uses it as a bus index (bus_bases)."""
    text = _strip_loose_footnote(cell)
    match = _ATTACHED_INDEX.match(text)
    if match and match.group(1).upper() in buses:
        return text
    return strip_footnote(text)


def pin_like(text: str) -> bool:
    """A single pin number or ball: 12, A1, AA12, P6, 1A, EP, PAD, SH1."""
    text = text.strip()
    return bool(text) and (bool(_PIN.fullmatch(text)) or bool(_PAD_WORDS.fullmatch(text)))


def pin_key(number: str) -> str:
    """The number compared: upper case without spaces, leading zeros dropped (A01 is
    A1, 007 is 7)."""
    text = re.sub(r"\s+", "", str(number)).upper()
    return re.sub(r"\d+", lambda match: str(int(match.group())), text)


def pin_sort_key(number: str):
    """Natural order of pin numbers and BGA balls: numbers first (1, 2, 10), then balls
    by row letters (A1 < A2 < A10 < B1 < Y1 < AA1 < AB1), then names (EP)."""
    text = pin_key(number)
    match = re.fullmatch(r"([A-Z]*)(\d*)(.*)", text)
    letters, digits, rest = match.groups() if match else ("", "", text)
    if not letters:
        return (0, 0, "", int(digits) if digits else -1, rest)
    return (1 if digits else 2, len(letters), letters, int(digits) if digits else -1, rest)


_DESIGNATOR = re.compile(r"[^\s,;:|()\[\]\"]{1,12}")


def split_numbers(cell: str, strict: bool = True) -> list[str] | None:
    """The pin numbers a cell lists ("12", "4, 9, 15", "A1 B2", "12-15"); None when it
    is not a list of pins. strict False (a column its header calls the pin number) also
    takes any short designator without spaces (A, K, +, G1)."""
    text = strip_footnote(cell)
    if not text:
        return None
    if pin_like(text):
        return [text]
    match = _RANGE.match(text)
    if match:
        first, last = int(match.group(1)), int(match.group(2))
        if first < last and last - first < RANGE_LIMIT:
            return [str(number) for number in range(first, last + 1)]
    parts = [part for part in re.split(r"\s*[,;/]\s*|\s+", text) if part]
    if len(parts) > 1 and all(pin_like(part) for part in parts):
        return parts
    if not strict and _DESIGNATOR.fullmatch(text):
        return [text]
    return None


# ---------------------------------------------------------------- names

_OVERLINES = "\u0305\u0304\u203e"
# Dashes and minus signs a PDF copy gives (hyphen, en and em dash, minus sign): '-'.
_DASHES = str.maketrans({c: "-" for c in "\u2010\u2011\u2012\u2013\u2014\u2212\ufe63\uff0d"})
_LOW_SUFFIX = re.compile(r"(?:(?<=[A-Za-z0-9]{2})(?:[_-][NB]|#|\*)|(?<=^[A-Za-z])(?:[_-]N|#|\*))$")
_TOKEN = re.compile(r"[^\W_]+|\+|-")
_SPLIT_SLASH = re.compile(
    r"(?<=[^\W_]{2}|[^\W_]\\)/(?=[^\W_~!#]{2}|[~!#/][^\W_]|[^\W_]\\)")
_NC_NAMES = {"NC", "DNC", "NIC", "NO_CONNECT", "NOT_CONNECTED", "N_C", "DNU",
             "DO_NOT_CONNECT", "DO_NOT_USE"}
_GROUND_NAMES = {"GND", "VSS", "GROUND"}
_GENERIC = {"IO", "GPIO", "NC", "DNC", "DNU", "NIC", "RSVD", "RESERVED", "TEST", "TP"}
_GROUND = re.compile(r"(?:[ADPS]?GND|GND\w*|[AD]?VSS\w*|GROUND\w*|0V)")
_POWER = re.compile(r"(?:[ADP]?V(?:CC|DD|EE)\w*|VBATT?\w*|VDDIO\w*|VIO\d*|"
                    r"MGT\w*(?:AVCC|AVTT|VCCAUX)\w*|V\d+V\d*|\+\d+V\d*)")


def split_functions(text: str) -> list[str]:
    """The functions a name holds: split on commas and semicolons, on '/' between names
    ("PA0/WKUP", not "I/O" or "/RESET") and with each parenthesised group as its own
    function ("I/O (DIN, D0)"). A number in parentheses attached to the name is a bus
    index (D(0) is D0); after a space it is a footnote mark and dropped, as are two or
    more stars and superscript digits (a table's cells lose their footnote marks when
    read: clean_name)."""
    text = _strip_loose_footnote(
        unicodedata.normalize("NFKC", str(text or "")).translate(_DASHES))
    if not text:
        return []
    text = re.sub(r"(?<=[^\W_])\((\d{1,3})\)", r"_\1", text)
    groups = re.findall(r"\(([^()]*)\)", text)
    text = re.sub(r"\([^()]*\)", ",", text)
    # A parenthesised number is a footnote mark inside the name.
    groups = [group for group in groups if not re.fullmatch(r"\s*\d{1,2}\s*", group)]
    parts: list[str] = []
    for chunk in (text, *groups):
        for piece in re.split(r"\s*[,;]\s*|\s+/\s+", chunk):
            parts.extend(_SPLIT_SLASH.split(piece))
    return [part.strip() for part in parts if part.strip() and _TOKEN.search(part)]


@lru_cache(maxsize=65536)
def function_key(text: str) -> tuple[str, bool]:
    """(canonical form, active low) of one function name: upper case, tokens joined by
    '_' (separators _ - / . space and brackets unified; "I/O" is IO, "N/C" NC), leading
    zeros dropped, a trailing '+' or '-' kept; nRESET, RESETn, ~RESET, !RESET, /RESET,
    RESET#, RESET*, RESET_N, RESET_B, R\\E\\S\\E\\T\\ and an overline all give ("RESET",
    True). A lower-case n after a digit is a differential leg (CLK0n, LVDS_6n), not
    active low."""
    text = str(text).strip()
    low = False
    if "\\" in text:
        low, text = True, text.replace("\\", "")
    if any(mark in text for mark in _OVERLINES):
        low, text = True, "".join(c for c in text if c not in _OVERLINES)
    text = unicodedata.normalize("NFKC", text).translate(_DASHES).strip()
    while len(text) > 1 and text[0] in "~!/#":
        low, text = True, text[1:].lstrip()
    if re.match(r"n[A-Z]", text) and (sum(c.isupper() for c in text) >= 2 or len(text) == 2):
        low, text = True, text[1:]
    if re.fullmatch(r"[A-Z0-9_]*[A-Z]{2}n", text):
        low, text = True, text[:-1]  # RESETn, CSn (CLK0n, LVDS_6n are differential)
    suffix = _LOW_SUFFIX.search(text)
    if suffix:
        low, text = True, text[:suffix.start()]
    text = text.upper().replace("I/O", "IO").replace("N/C", "NC")
    tokens = []
    stripped = text.rstrip()
    for match in _TOKEN.finditer(text):
        token = match.group()
        if token == "-":
            if match.start() == 0 or match.end() == len(stripped):
                tokens.append("-")
            continue
        tokens.append(re.sub(r"\d+", lambda d: str(int(d.group())), token))
    return "_".join(tokens), low


def normalize_name(text: str) -> str:
    """A name as compared: its functions' canonical forms, active-low ones marked '~'."""
    keys = []
    for function in split_functions(text):
        canonical, low = function_key(function)
        if canonical:
            keys.append(("~" if low else "") + canonical)
    return ", ".join(keys)


def loose_key(canonical: str) -> str:
    """A canonical name with ground and no-connect synonyms unified (VSS, GROUND: GND;
    DNC, NIC, DNU, N_C: NC), token by token."""
    if canonical in _NC_NAMES:
        return "NC"
    return "_".join("GND" if token in _GROUND_NAMES else token for token in canonical.split("_"))


def name_kind(text: str) -> str | None:
    """ground, power or nc when the name says so (GND, VSS*, VCC*, VDD*, VCCO_34,
    MGTAVCC, NC, DNU), else None."""
    functions = split_functions(text)
    if not functions:
        return None
    canonical, _ = function_key(functions[0])
    if canonical in _NC_NAMES:
        return "nc"
    if _GROUND.fullmatch(canonical):
        return "ground"
    if _POWER.fullmatch(canonical):
        return "power"
    return None


def generic_name(text: str) -> bool:
    """A name many pins share by design: IO, GPIO, NC, RSVD, supply and ground names."""
    functions = split_functions(text)
    if not functions:
        return True
    canonical = function_key(functions[0])[0]
    return len(functions) == 1 and (canonical in _GENERIC or name_kind(text) is not None)


# ---------------------------------------------------------------- types

_TYPE_WORDS = {
    "power": {"p", "pwr", "power", "s", "supply", "power supply", "pi", "po", "p/s", "power input",
              "power output", "power/ground", "power pin", "supply pin", "pwr/gnd", "vcc", "vdd",
              "p (supply)", "power in", "power out", "p/g"},
    "ground": {"g", "gnd", "ground", "ground pin"},
    "input": {"i", "in", "input", "di", "inp", "reset", "boot", "ist", "i/pu", "i/pd", "ck",
              "clock input", "digital input", "i (pu)", "i (pd)", "input only", "dedicated input",
              "ipu", "ipd"},
    "output": {"o", "out", "output", "do", "o/z", "z", "tri-state", "3-state", "hiz", "o (3s)",
               "digital output", "tri-state output", "output only", "oz"},
    "io": {"i/o", "io", "b", "bidir", "bidirectional", "inout", "i/o/z", "b/d", "monoio", "bi",
           "digital i/o", "in/out", "i/o (ft)", "gpio", "user i/o", "ioz", "dio"},
    "open_drain": {"od", "oc", "open drain", "open-drain", "open collector", "opencollector",
                   "o/od", "i/od", "i/o/od", "openemitter", "open emitter", "o (od)",
                   "i/o (od)", "od/o"},
    "analog": {"a", "an", "analog", "ai", "ao", "a/i", "aio", "a i/o", "analog input",
               "analog output", "analog i/o", "a/o", "a i", "a o", "analog in", "analog out"},
    "nc": {"nc", "n/c", "no connect", "dnc", "dnu", "not connected", "nic", "do not connect"},
    "passive": {"passive", "pas", "pass"},
}
_TYPE_LOOKUP = {word: kind for kind, words in _TYPE_WORDS.items() for word in words}
_TYPE_PREFIXES = [(re.compile(rf"(?:{words})(?![\w/])"), kind) for words, kind in (
    ("open[ -]drain|open collector", "open_drain"), ("bidirectional|i/o", "io"),
    ("power|supply", "power"), ("ground", "ground"), ("analog", "analog"), ("input", "input"),
    ("output", "output"))]


def type_kind(text: str) -> str:
    """The class of a type or direction cell (vendor and Altium spellings): power,
    ground, input, output, io, open_drain, analog, passive, nc or unknown."""
    value = re.sub(r"\s+", " ", strip_footnote(str(text or "")).lower()).strip()
    if not value or value in ("-", "na", "n/a"):
        return "unknown"
    if value in _TYPE_LOOKUP:
        return _TYPE_LOOKUP[value]
    first = re.split(r"[\s,(]+", value)[0]
    if first in _TYPE_LOOKUP and first not in ("p", "s", "g", "a", "b", "z"):
        return _TYPE_LOOKUP[first]
    for pattern, kind in _TYPE_PREFIXES:
        if pattern.match(value):
            return kind
    return "unknown"


def row_kind(type_text: str, name: str, no_connect: bool = False) -> str:
    """A row's class: no_connect first, then the type cell, else the name. A name that
    says ground wins over a power type (Altium and many tables type ground pins as
    power)."""
    if no_connect:
        return "nc"
    by_type = type_kind(type_text)
    by_name = name_kind(name)
    if by_type == "power" and by_name == "ground":
        return "ground"
    if by_type != "unknown":
        return by_type
    return by_name or "unknown"


# ---------------------------------------------------------------- header roles

_NUMBER_HEADERS = {
    "pin", "pin #", "pin#", "pin no", "pin no.", "pin nr", "pin nr.", "pin number", "pin num",
    "pin numbers", "pins", "number", "no", "no.", "#", "nr", "nr.", "num", "ball", "ball #",
    "ball no", "ball no.", "ball number", "ball id", "ball location", "pad", "pad #",
    "pad no", "pad no.", "pad number", "position", "pos", "pin position", "location",
    "pin location", "pin id", "pin designator", "designator", "terminal", "terminal no",
    "terminal no.", "terminal number", "lead", "lead no", "lead no.", "pin/ball",
    "ball/pin", "package pin", "pkg pin", "bump", "pin (ball)", "ball (pin)",
}
_NAME_HEADERS = {
    "name", "pin name", "pin name/function", "pin name / function", "signal name",
    "pad name", "pin function", "pin name (function after reset)",
    "pin name(function after reset)", "function after reset", "mnemonic", "symbol",
    "terminal name", "pin label", "net name", "ball name", "pin signal", "default function",
    "primary function", "main function", "pin name/ function", "pin name /function",
    "pin symbol", "signal",
}
_TYPE_HEADERS = {
    "type", "pin type", "i/o", "io", "i/o type", "io type", "direction", "dir", "dir.",
    "electrical type", "electrical", "signal type", "pin direction", "signal direction",
    "kind", "i/o direction", "type (direction)", "pin type (direction)",
}
_BANK_HEADERS = {"bank", "bank number", "bank no", "bank no.", "i/o bank", "io bank", "unit",
                 "part", "bank #", "bank name"}
_NC_HEADERS = {"no-connect", "no connect", "nc", "no connection"}
_ALTERNATE = re.compile(
    r"(?:alternat|optional function|configuration function|config function|additional|"
    r"other function|secondary|multiplex|remap|hps function|tx/rx channel|lvds output|"
    r"^functions?$|^label$|^alt\.?(?: |$)|special function|peripheral)")
_PACKAGE = re.compile(
    r"(?i)(?:[a-z]{1,3}\d{2,4}[a-z]?|"
    r"(?:lqfp|tqfp|qfp|pqfp|vqfp|hlqfp|qfn|ufqfpn|vfqfpn|uqfn|wqfn|vqfn|hvqfn|dfn|son|soic|"
    r"so|ssop|tssop|htssop|msop|sop|bga|fbga|tfbga|ufbga|lfbga|vfbga|wlcsp|csp|ucsp|dip|"
    r"pdip|plcc|lga|pqfn|sot|ewlcsp)[- ]?\d{1,4}[a-z]?)")


_HEADER_HINT = re.compile(r"(?i)name|function|signal|symbol|mnemonic|label")


def header_key(text: str) -> str:
    """A header cell as matched: lower case, one space, no footnote mark or colon."""
    value = re.sub(r"\s+", " ", str(text or "")).strip().lower()
    value = strip_footnote(value).rstrip(":").strip()
    return value


def _role(key: str) -> str | None:
    if key in _NUMBER_HEADERS:
        return "number"
    if key in _NAME_HEADERS:
        return "name"
    if key == "function":
        return "function"  # the name when no name column, else an alternate
    if key in _TYPE_HEADERS:
        return "type"
    if key in _BANK_HEADERS:
        return "bank"
    if key in _NC_HEADERS:
        return "nc"
    if _ALTERNATE.search(key) and not key.startswith("dqs"):
        return "alternate"
    if package_header(key):
        return None
    if re.search(r"(?<![a-z])name(?![a-z])", key) and not key.startswith(
            ("file", "package", "device", "part", "bank")):
        return "name"
    if re.search(r"(?:pin|ball|pad|terminal|lead)", key) and re.search(
            r"(?:no|number|num|nr|id)|#", key):
        return "number"
    return None


def package_header(text: str) -> str | None:
    """The package a header cell names (F34, NF40, LQFP64, U484), else None."""
    value = strip_footnote(re.sub(r"\s+", " ", str(text or "")).strip())
    value = re.sub(r"(?i)^(?:pin\s*(?:number|no\.?|#)?|ball)\s*", "", value).strip(" ()")
    return value if value and _PACKAGE.fullmatch(value) else None


@dataclass
class _Layout:
    header: list[str]
    number: int | None
    name: int
    alternates: list[int]
    type: int | None
    bank: int | None
    nc: int | None
    packages: dict[str, int]  # package -> column (confirmed with the data)
    format: str = FORMAT_GENERIC


def _layout(cells: list[str]) -> _Layout | None:
    """The layout a header row gives, or None when the row is no header (a header
    names a pin name column and a pin number or package column)."""
    if len(cells) < 2 or len(cells) > 80:
        return None
    if not any(_HEADER_HINT.search(str(cell)) for cell in cells):
        return None
    keys = [header_key(cell) for cell in cells]
    roles = [_role(key) for key in keys]
    number = next((i for i, role in enumerate(roles) if role == "number"), None)
    name = next((i for i, role in enumerate(roles) if role == "name"), None)
    functions = [i for i, role in enumerate(roles) if role == "function"]
    if name is None and functions:
        name = functions.pop(0)
    if name is None:
        return None
    packages = {}
    for index, (cell, role) in enumerate(zip(cells, roles)):
        if role is None and index != name:
            package = package_header(cell)
            if package and package not in packages:
                packages[package] = index
    if number is None and not packages:
        return None
    # A header names at least two columns, or a pin name column in the usual words and
    # the package columns: a data row whose name happens to hold "name" is no header.
    named_roles = sum(role is not None for role in roles)
    if named_roles < 2 and not (keys[name] in _NAME_HEADERS and packages):
        return None
    keyset = set(keys)
    alternates = [i for i, role in enumerate(roles) if role == "alternate" and i != name]
    alternates += functions
    # Another name-like column (a second "Signal") is an alternate.
    alternates += [i for i, role in enumerate(roles) if role == "name" and i != name]
    layout = _Layout(
        header=[str(cell).strip() for cell in cells], number=number, name=name,
        alternates=sorted(set(alternates)),
        type=next((i for i, role in enumerate(roles) if role == "type"), None),
        bank=next((i for i, role in enumerate(roles) if role == "bank"), None),
        nc=next((i for i, role in enumerate(roles) if role == "nc"), None),
        packages=packages)
    if "pin name/function" in keyset or ("bank number" in keyset and keyset & {"vref", "vrefb group"}):
        layout.format = FORMAT_INTEL
        layout.type = None  # Intel files give no direction column
    elif {"pin", "pin name"} <= keyset and keyset & {"memory byte group", "super logic region",
                                                      "vccaux group", "i/o type", "no-connect"}:
        layout.format = FORMAT_XILINX
        layout.type = None  # I/O Type is the bank type (HR, HP, GTP), not a direction
    elif {"position", "name", "type"} <= keyset:
        layout.format = FORMAT_STM32
    return layout


# ---------------------------------------------------------------- splitting text

def _split_pipe(line: str) -> list[str] | None:
    text = line.strip()
    if "|" not in text:
        return [text] if text else []
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|"):
        text = text[:-1]
    cells = [cell.strip() for cell in text.split("|")]
    if cells and all(re.fullmatch(r":?-{2,}:?", cell) for cell in cells if cell):
        return None  # a Markdown separator row
    return cells


def split_text(text: str, delimiter: str) -> list[tuple[int, list[str], str]]:
    """(line, cells, line text) of each row of text split on delimiter (one of
    DELIMITERS). Tab, comma and semicolon rows follow CSV quoting (a quoted cell may
    hold the delimiter or span lines)."""
    rows: list[tuple[int, list[str], str]] = []
    if delimiter in ("tab", "comma", "semicolon"):
        mark = {"tab": "\t", "comma": ",", "semicolon": ";"}[delimiter]
        lines = text.split("\n")
        reader = csv.reader(io.StringIO(text), delimiter=mark)
        start = 1
        try:
            for cells in reader:
                end = reader.line_num
                rows.append((start, [cell.strip() for cell in cells],
                             lines[start - 1] if start - 1 < len(lines) else ""))
                start = end + 1
        except csv.Error:
            return []
        return rows
    for number, line in enumerate(text.split("\n"), start=1):
        if delimiter == "pipe":
            cells = _split_pipe(line)
            if cells is None:
                continue
        elif delimiter == "spaces":
            cells = [cell for cell in re.split(r"\s{2,}|\t", line.strip())]
        else:
            cells = line.split()
        rows.append((number, cells, line))
    return rows


def _fixed_cells(line: str, starts: list[int]) -> list[str]:
    cells = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(line)
        cells.append(line[start:end].strip() if start < len(line) else "")
    return cells


def _header_starts(line: str, cells: list[str]) -> list[int] | None:
    starts, position = [], 0
    for cell in cells:
        found = line.find(cell, position)
        if found < 0:
            return None
        starts.append(found)
        position = found + len(cell)
    return starts


# ---------------------------------------------------------------- reading rows

_FOOTER = re.compile(r"(?i)^total number of pins\D*(\d+)")
_DEVICE = (re.compile(r"(?i)^device/package\s+(\S+)"),
           re.compile(r"(?i)pin information for the\s+(.+?)\s+device"))
_EMPTY = {"", "-", "na", "n/a", "\u2013", "\u2014", "none"}


def _cell(cells: list[str], index: int | None) -> str:
    if index is None or index >= len(cells):
        return ""
    return str(cells[index] or "").strip()


def _value(text: str) -> str:
    return "" if text.strip().lower() in _EMPTY else text.strip()


def _unread(table: PinTable, line: int, text: str, reason: str) -> None:
    table.unread_total += 1
    if len(table.unread) < UNREAD_KEPT:
        table.unread.append(UnreadRow(line, text.strip()[:120], reason))


def _row_text(cells: list[str], raw: str) -> str:
    return raw if raw.strip() else " | ".join(cell for cell in cells if cell)


class _Section:
    def __init__(self, layout: _Layout, line: int, raw: str, starts: list[int] | None):
        self.layout = layout
        self.line = line
        self.raw = raw
        self.starts = starts
        self.rows: list[tuple[int, list[str], str]] = []
        self.misfit: set[int] = set()  # lines whose cells could not be fitted (single spaces)
        self.fitted = 0  # rows whose extra words were joined into one cell


def _confirm_packages(section: _Section) -> dict[str, int]:
    """The package columns whose cells are mostly pin numbers."""
    confirmed = {}
    for package, index in section.layout.packages.items():
        values = [_cell(cells, index) for _, cells, _ in section.rows]
        values = [value for value in values if _value(value)]
        if values and sum(split_numbers(value) is not None for value in values) >= 0.6 * len(values):
            confirmed[package] = index
    return confirmed


def _type_column_works(section: _Section) -> bool:
    """A type column that names directions or classes for most of its cells (not the
    bank type or a buffer type)."""
    index = section.layout.type
    values = [_value(_cell(cells, index)) for _, cells, _ in section.rows]
    values = [value for value in values if value]
    return bool(values) and sum(type_kind(value) != "unknown" for value in values) >= 0.5 * len(values)


def _tables_of(section: _Section) -> list[PinTable]:
    layout = section.layout
    packages = _confirm_packages(section)
    columns: list[tuple[str | None, int | None]]
    if packages:
        columns = list(packages.items())
    elif layout.number is not None:
        columns = [(None, layout.number)]
    else:
        return []
    typed = layout.type is not None and _type_column_works(section)
    role_columns: dict[str, object] = {"name": layout.header[layout.name]}
    if typed:
        role_columns["type"] = layout.header[layout.type]
    if layout.bank is not None:
        role_columns["bank"] = layout.header[layout.bank]
    if layout.alternates:
        role_columns["alternates"] = [layout.header[i] for i in layout.alternates]
    if layout.nc is not None:
        role_columns["no_connect"] = layout.header[layout.nc]
    headed = not layout.header[0].startswith("column ")
    buses = bus_bases(_cell(cells, layout.name) for _, cells, _ in section.rows)
    tables = []
    for package, number_column in columns:
        table = PinTable(package=package, header=layout.header,
                         columns={"number": layout.header[number_column], **role_columns})
        named_types = 0
        for line, cells, raw in section.rows:
            if not any(str(cell).strip() for cell in cells):
                continue
            footer = _FOOTER.match(" ".join(cell for cell in cells if cell))
            if footer:
                table.declared_pins = int(footer.group(1))
                continue
            if line in section.misfit:
                _unread(table, line, _row_text(cells, raw),
                        "more words than the header has columns, and no way of joining them "
                        "leaves a pin number and a type in their columns: paste the table "
                        "with tabs, or keep its columns aligned")
                continue
            number_cell = _cell(cells, number_column)
            name = clean_name(_cell(cells, layout.name), buses)
            if package and not _value(number_cell):
                if name:
                    table.not_in_package += 1
                else:
                    _unread(table, line, _row_text(cells, raw), "no pin name or number")
                continue
            numbers = split_numbers(number_cell, strict=not headed)
            if not numbers:
                _unread(table, line, _row_text(cells, raw),
                        "no pin number" if not _value(number_cell)
                        else f"not a pin number: {number_cell[:30]}")
                continue
            if not name:
                _unread(table, line, _row_text(cells, raw), "no pin name")
                continue
            alternates = []
            for index in layout.alternates:
                value = strip_footnote(_value(_cell(cells, index)))
                if value and value.lower() not in ("yes", "no"):
                    alternates.append(value)
            type_text = _value(_cell(cells, layout.type)) if typed else ""
            no_connect = layout.nc is not None and _value(_cell(cells, layout.nc)).lower() not in (
                "", "no", "n", "false", "0")
            kind = row_kind(type_text, name, no_connect)
            if not type_text and kind != "unknown":
                named_types += 1
            for number in numbers:
                table.rows.append(PinRow(
                    number=number, name=name, alternates=alternates, type=type_text,
                    kind=kind, bank=_value(_cell(cells, layout.bank)), line=line,
                    key=pin_key(number)))
        table.type_source = ("column and names" if typed and named_types else "column" if typed
                             else "names" if named_types else "none")
        tables.append(table)
    return tables


def _headerless_layout(rows: list[tuple[int, list[str], str]]) -> _Layout | None:
    """Columns inferred from the data: the pin number column (mostly pin numbers,
    preferring unique values), the name column next to it (mostly text with a
    letter), and a type column (mostly type words)."""
    data = [cells for _, cells, _ in rows if sum(1 for cell in cells if cell.strip()) >= 2]
    if not data:
        return None
    width = max(len(cells) for cells in data)

    def column(index):
        return [_cell(cells, index) for cells in data if _cell(cells, index)]

    stats = []
    for index in range(width):
        values = column(index)
        if not values:
            stats.append((0.0, 0.0, 0.0, 0.0))
            continue
        pins = sum(split_numbers(value) is not None for value in values) / len(values)
        unique = len(set(values)) / len(values)
        texts = sum(bool(re.search(r"[A-Za-z]", value)) for value in values) / len(values)
        types = sum(type_kind(value) != "unknown" for value in values) / len(values)
        stats.append((pins, unique, texts, types))
    numbers = [i for i, (pins, *_rest) in enumerate(stats) if pins >= 0.8]
    if not numbers:
        return None
    digits = {i: sum(value.isdigit() for value in column(i)) / max(1, len(column(i)))
              for i in numbers}
    number = max(numbers, key=lambda i: (digits[i] >= 0.5, stats[i][1], -i))
    order = [number + 1, number - 1] + [i for i in range(width) if i not in (number + 1, number - 1)]
    named = [i for i in order if 0 <= i < width and i != number and stats[i][2] >= 0.8]
    # Names can read like types (GND, OUT, IN): then the column next to the numbers.
    name = next((i for i in named if stats[i][3] < 0.8),
                next((i for i in named if abs(i - number) == 1), None))
    if name is None:
        return None
    kind = next((i for i in range(width) if i not in (number, name) and stats[i][3] >= 0.6), None)
    header = [f"column {i + 1}" for i in range(width)]
    return _Layout(header=header, number=number, name=name, alternates=[], type=kind,
                   bank=None, nc=None, packages={})


_PHRASES = (_NUMBER_HEADERS | _NAME_HEADERS | _TYPE_HEADERS | _BANK_HEADERS | _NC_HEADERS | {
    "alternate function", "alternate functions", "alternate function(s)",
    "alternative function", "alternative functions", "alt function", "alt functions",
    "additional function", "additional functions", "optional function(s)",
    "optional functions", "configuration function", "other functions", "special function",
    "special functions", "pin description", "signal description", "description"})


def merge_header_phrases(cells: list[str]) -> list[str]:
    """A header row split on single spaces with its multi-word names joined back ("Pin",
    "Number", "Pin", "Name" is "Pin Number", "Pin Name"), longest name first."""
    merged, index = [], 0
    while index < len(cells):
        for size in (4, 3, 2):
            if index + size <= len(cells) and header_key(" ".join(cells[index:index + size])) in _PHRASES:
                merged.append(" ".join(cells[index:index + size]))
                index += size
                break
        else:
            merged.append(cells[index])
            index += 1
    return merged


FIT_EXTRA_LIMIT = 40  # extra words of one row that are fitted (a longer row is unread)


def _allocations(extra: int, columns: list[int]):
    """Every way of sharing extra words among the columns (column -> words)."""
    if len(columns) == 1:
        yield {columns[0]: extra}
        return
    for words in range(extra + 1):
        for rest in _allocations(extra - words, columns[1:]):
            yield {columns[0]: words, **rest}


def _fit_cells(cells: list[str], layout: _Layout) -> list[str] | None:
    """A row split on single spaces with more cells than its header: its extra words
    shared among the columns that can hold several (the name: "MNT 1", "I/O, L38P"; the
    type: "Open Drain"; a last column of free text such as a description). Of the ways
    that leave a pin number in the number column, a type word (or nothing) in the type
    column and no name ending in a comma, the one giving the name, then the type, the
    fewest words wins; a bare index after a name goes with it (VREF 7). None when no way
    fits."""
    width = len(layout.header)
    extra = len(cells) - width
    if extra <= 0:
        return cells
    if extra > FIT_EXTRA_LIMIT:
        return None
    numbers = [layout.number] if layout.number is not None else list(layout.packages.values())
    used = {layout.number, layout.name, layout.type, layout.bank, layout.nc,
            *layout.packages.values()}
    flexible = {layout.name, width - 1} if width - 1 not in used else {layout.name}
    if layout.type is not None:
        flexible.add(layout.type)
    flexible = sorted(flexible)

    def fits(candidate: list[str]) -> bool:
        for index in numbers:
            value = _value(_cell(candidate, index))
            if value and split_numbers(value) is None and not (
                    len(value) <= 3 and re.fullmatch(r"[A-Za-z0-9+\-]+", value)):
                return False
        if layout.type is not None:
            value = _value(_cell(candidate, layout.type))
            if value and type_kind(value) == "unknown":
                return False
        name = _cell(candidate, layout.name)
        if name.endswith((",", "/")):
            return False
        following = _cell(candidate, layout.name + 1).split(" ")[0]
        return not (layout.name + 1 == width - 1 and layout.name + 1 not in used
                    and re.fullmatch(r"\d{1,2}", following))

    best, best_rank = None, None
    for allocation in _allocations(extra, flexible):
        candidate, position = [], 0
        for column in range(width):
            take = 1 + allocation.get(column, 0)
            candidate.append(" ".join(cells[position:position + take]))
            position += take
        if not fits(candidate):
            continue
        rank = (allocation.get(layout.name, 0), allocation.get(layout.type, 0))
        if best_rank is None or rank < best_rank:
            best, best_rank = candidate, rank
    return best


def _sub_header(section: _Section, cells: list[str]) -> bool:
    """A second header row naming the packages under a spanning "Pin number" cell (as a
    datasheet table copied out of a PDF gives): merged into the section's header."""
    layout = section.layout
    if _cell(cells, layout.name):
        return False
    packages = {i: package_header(cell) for i, cell in enumerate(cells) if package_header(cell)}
    if len(packages) < 2:
        return False  # one package needs no second header row: a data row without a name
    merged = list(layout.header) + [""] * max(0, len(cells) - len(layout.header))
    for index, package in packages.items():
        merged[index] = package
    combined = _layout(merged)
    if combined is None or not combined.packages:
        return False
    combined.number = None if layout.number in packages else combined.number
    section.layout = combined
    return True


def parse_rows(rows: list[tuple[int, list[str], str]], source: str,
               delimiter: str | None = None) -> PinSource:
    """A pin table from split rows ((line, cells, line text)): every header row starts
    a section (Intel files have one per package); rows before the first header are a
    preamble; with no header the columns are inferred."""
    sections: list[_Section] = []
    device = None
    preamble = 0
    for line, cells, raw in rows:
        joined = " ".join(cell for cell in cells if cell)
        layout = None
        if delimiter == "space":
            # "Pin Number Pin Name Type" copied from a PDF: the header's own names.
            merged = merge_header_phrases(cells)
            if merged != cells:
                layout = _layout(merged)
                cells = merged if layout is not None else cells
        if layout is None:
            layout = _layout(cells)
        if layout is not None:
            starts = _header_starts(raw, [c for c in cells]) if delimiter == "spaces" else None
            sections.append(_Section(layout, line, raw, starts))
            continue
        if not sections:
            for pattern in _DEVICE:
                match = pattern.search(joined)
                if match and device is None:
                    device = match.group(1).strip().strip('"')
            preamble += 1
            continue
        section = sections[-1]
        if not section.rows and _sub_header(section, cells):
            continue
        if section.starts and len(cells) != len(section.layout.header) and raw.strip():
            cells = _fixed_cells(raw, section.starts)
        elif delimiter == "space" and len(cells) > len(section.layout.header):
            fitted = _fit_cells(cells, section.layout)
            if fitted is None:
                section.misfit.add(line)
            else:
                section.fitted += 1
                cells = fitted
        section.rows.append((line, cells, raw))
    notes: list[str] = []
    if not sections:
        layout = _headerless_layout(rows)
        if layout is None:
            return PinSource(source, FORMAT_GENERIC, delimiter, [], device=device)
        section = _Section(layout, 0, "", None)
        section.rows = list(rows)
        sections = [section]
        notes.append("no header row: the columns were inferred from the values (pin "
                     f"numbers in column {layout.number + 1}, names in column {layout.name + 1}"
                     + (f", types in column {layout.type + 1}" if layout.type is not None else "")
                     + ")")
    tables: list[PinTable] = []
    for section in sections:
        for table in _tables_of(section):
            # One table per package: a later section of a package already read adds
            # its rows (a long file can repeat its header on every page).
            same = next((t for t in tables if t.package == table.package
                         and t.header == table.header), None)
            if same is not None:
                same.rows += table.rows
                same.unread += table.unread[:max(0, UNREAD_KEPT - len(same.unread))]
                same.unread_total += table.unread_total
                same.not_in_package += table.not_in_package
                same.declared_pins = same.declared_pins or table.declared_pins
            else:
                tables.append(table)
    formats = [section.layout.format for section in sections]
    fmt = max(set(formats), key=formats.count) if formats else FORMAT_GENERIC
    fitted = sum(section.fitted for section in sections)
    result = PinSource(source, fmt, delimiter, tables, device=device, notes=list(notes),
                       headed=not notes)
    if fitted:
        result.notes.append(
            f"single spaces separate the cells: {fitted} rows had more words than the header "
            "has columns, and their extra words were joined into the name or the last column "
            "(check names that hold spaces)")
    if fmt == FORMAT_XILINX:
        result.notes.append("pin types come from the names: the I/O Type column is the bank "
                            "type (HR, HP, GT), not a direction")
    elif fmt == FORMAT_INTEL:
        result.notes.append("pin types come from the names (Intel pin-out files give no "
                            "direction)")
    return result


def _score(result: PinSource) -> tuple[int, int]:
    """Readable rows of the best table, then fewer unread rows."""
    best = max(result.tables, key=lambda t: len(t.rows), default=None)
    if best is None:
        return (0, 0)
    return (len(best.rows), -best.unread_total)


def parse_pin_text(text: str, source: str = "pasted text") -> PinSource:
    """A pin table pasted as text: the delimiter that reads the most rows wins (ties go
    to tab, pipe, semicolon, comma, runs of spaces, single spaces, in that order; single
    spaces only when they read more rows than any other)."""
    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\ufeff", "")
    if not text.strip():
        raise PinTableError("empty_table", "The table is empty.")
    results = []
    for delimiter, mark in zip(DELIMITERS, ("\t", "|", ";", ",", " ", " ")):
        if mark in text:
            results.append(parse_rows(split_text(text, delimiter), source, delimiter))
    # A header found by a tab, pipe, semicolon or comma beats any split on spaces.
    marked = [result for result in results if result.headed and result.rows_read
              and result.delimiter not in ("spaces", "space")]
    best: PinSource | None = None
    for result in marked or results:
        if best is None or _score(result) > _score(best):
            best = result
    if best is None or not best.rows_read:
        raise PinTableError(
            "no_pins_read",
            "No pin rows could be read: give a table with a pin number (or ball) column and "
            "a pin name column, one pin per line, separated by tabs, commas, semicolons, "
            "pipes or runs of spaces (a header row helps).")
    return best


def parse_pin_file(path: str) -> PinSource:
    """A pin table file: .xlsx/.xlsm (the worksheet that reads the most rows), else
    text (.csv, .tsv, .txt: UTF-8, UTF-16 or Windows-1252)."""
    file = Path(path)
    if not file.is_file():
        raise PinTableError("file_not_found", f"File not found: {path}")
    if file.stat().st_size > MAX_TABLE_BYTES:
        raise PinTableError("file_too_large",
                            f"{file.name} is larger than {MAX_TABLE_BYTES >> 20} MB.")
    suffix = file.suffix.lower()
    if suffix == ".xls":
        raise PinTableError("xls_not_supported",
                            f"{file.name} is an Excel 97-2003 workbook (.xls), which is not "
                            "read: save it as .xlsx or CSV (vendor pin-out files also come as "
                            ".txt).")
    if suffix == ".pdf":
        raise PinTableError("pdf_not_supported",
                            f"{file.name} is a PDF: copy the pin table out of it and pass it "
                            "as table=, or use the vendor's .txt/.csv pinout file.")
    data = file.read_bytes()
    if suffix in XLSX_SUFFIXES or data[:2] == b"PK":
        try:
            sheets = xlsx_sheets(data)
        except ValueError as error:
            raise PinTableError("unreadable_workbook", f"{file.name}: {error}") from error
        best = None
        for name, rows in sheets:
            result = parse_rows([(index, [str(cell) for cell in cells], "")
                                 for index, cells in enumerate(rows, start=1)],
                                file.name, None)
            result.sheet = name
            if best is None or _score(result) > _score(best):
                best = result
        if best is None or not best.rows_read:
            raise PinTableError("no_pins_read",
                                f"{file.name}: no worksheet holds a pin table (a pin number "
                                "or ball column and a pin name column).")
        return best
    if b"\0" in data[:4096] and data[:2] not in (b"\xff\xfe", b"\xfe\xff"):
        raise PinTableError("not_a_table", f"{file.name} is a binary file, not a pin table.")
    lines, _ = decode_lines(data)
    return parse_pin_text("\n".join(lines), file.name)
