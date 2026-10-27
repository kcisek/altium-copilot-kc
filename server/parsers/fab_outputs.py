"""Readers for the fabrication and assembly files Altium generates, for cross-checking
them against the design (verify_outputs). Plain data out, lengths in mil.

Formats, as Altium writes them (checked on a corpus of real project outputs):

- Pick and place (.txt and .csv): a preamble ("Altium Designer Pick and Place Locations",
  the output file's own path, Date, Time, Revision, Variant, "Units used"), then a header
  row and one row per part. Columns are configurable, so they are found by name: Designator,
  Comment, Layer, Footprint, Center-X/Y, Ref-X/Y, Pad-X/Y (older versions: Mid X, Ref X,
  Pad X, TB), Rotation, Description. Units come from the header ("Center-X(mm)"), the
  preamble or the values ("1234.5mil"). Text output pads each column to the header's
  start, values left-aligned and quoted when they hold spaces; CSV output quotes every
  field. UTF-8 files carry a byte-order mark at the start and again before the header;
  older files are Windows-1252, and a file can mix both line by line.
- BOM: the header row (Designator with Quantity) is found by name below any title block
  of Altium's templates; designators are listed with ', '. parse_bom_csv reads CSV,
  parse_bom_xlsx a workbook (.xlsx, which Altium's BOM templates write; xlsx_sheets is a
  stdlib reader of its sheets), and parse_bom_rows the rows of any table. .xls (BIFF)
  workbooks are not read.
- IPC-D-356A (.ipc, the Test Point Report): 80-column records. 317 through-hole and 327
  surface features (net, designator-pin or VIA, hole, plated flag, access layer, X/Y,
  feature size, rotation, solder mask), 367 tooling holes, 379 adjacency with 0xx
  continuations, P parameter records (UNITS, and NNAME aliases for long net names).
- IPC-2581 (.cvg/.xml): LogicalNet pins, PhyNet points, components (refDes, layer,
  rotation, mirror, location), the stackup (layers, thickness, material, Dk, Df), the
  drill holes and their spans, and the BOM's populate flags; units from CadHeader.
- Excellon NC drill (.txt): M48 header with ;FILE_FORMAT=i:d, INCH|METRIC[,LZ|TZ],
  ;TYPE=PLATED / NON_PLATED sections and tool definitions; the body selects tools and
  gives modal coordinates without a decimal point (zero suppression from the header),
  G85 slots, and routed slots (G00 start, M15, G01, M16).
- The NC drill report (.DRR: per layer pair, the drill files and per tool its size, hole
  type, count and PTH/NPTH) and the layer pairs file (.LDP: which copper layers each
  drill file spans, named by their Gerber extensions: gtl, g1.., gp1.., gbl).
"""

import csv
import io
import re
import zipfile
from collections import Counter
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from xml.etree.ElementTree import ParseError, XMLPullParser, fromstring

try:
    from parsers.out_job import decode_text
    from parsers.pcb_doc import MM_PER_MIL
    from services.bom import MFR_NAME, MPN_NAME, PART_NUMBER, norm_name
except ImportError:
    from server.parsers.out_job import decode_text
    from server.parsers.pcb_doc import MM_PER_MIL
    from server.services.bom import MFR_NAME, MPN_NAME, PART_NUMBER, norm_name

MIL_PER_MM = 1 / MM_PER_MIL
UNIT_TO_MIL = {
    "mil": 1.0, "mils": 1.0, "thou": 1.0, "mm": MIL_PER_MM, "um": MIL_PER_MM / 1000,
    "micron": MIL_PER_MM / 1000, "in": 1000.0, "inch": 1000.0, "inches": 1000.0,
    "millimeter": MIL_PER_MM, "metric": MIL_PER_MM, "imperial": 1.0,
}
_NUMBER = re.compile(
    r"^\s*([+-]?(?:\d+(?:[.,]\d*)?|[.,]\d+))\s*(mm|mils?|thou|um|in(?:ch(?:es)?)?)?\s*$",
    re.IGNORECASE)


def unit_scale(unit: str | None) -> float | None:
    """mil per unit for 'mm', 'mil', 'in', 'inch', 'um' (any case), else None."""
    return UNIT_TO_MIL.get(unit.strip().lower()) if unit else None


def parse_number(text: str | None, decimal_comma: bool = False
                 ) -> tuple[float, str | None] | None:
    """(value, unit suffix lower-case or None) of '12.5', '-3', '1234.5mil', '0.3 mm'; a
    decimal comma only when decimal_comma (a ';' or tab delimited file)."""
    if text is None:
        return None
    match = _NUMBER.match(text.strip().strip('"'))
    if not match:
        return None
    number = match.group(1)
    if "," in number:
        if not decimal_comma:
            return None
        number = number.replace(",", ".")
    try:
        value = float(number)
    except ValueError:
        return None
    unit = match.group(2).lower() if match.group(2) else None
    return value, ("mil" if unit == "mils" else unit)


def decode_lines(data: bytes) -> tuple[list[str], str]:
    """(lines, encoding) of a text output. Each line is decoded on its own (UTF-8, else
    Windows-1252: a file can mix both) and byte-order marks are dropped wherever they
    sit (Altium writes one before the pick-and-place header row too). encoding is
    'ascii', 'utf-8', 'cp1252', 'mixed' or 'utf-16'."""
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        text = data.decode("utf-16", errors="replace")
        return [line.replace("\ufeff", "") for line in text.splitlines()], "utf-16"
    used = set()
    lines = []
    for raw in data.split(b"\n"):
        raw = raw.rstrip(b"\r")
        text, encoding = decode_text(raw)
        if not raw.isascii():
            used.add(encoding)
        lines.append(text.replace("\ufeff", ""))
    if lines and lines[-1] == "":
        lines.pop()
    encoding = "ascii" if not used else (used.pop() if len(used) == 1 else "mixed")
    return lines, encoding


def _clean_cell(text: str) -> str:
    text = text.replace("\ufeff", "").strip()
    if len(text) >= 2 and text[0] == text[-1] == '"':
        text = text[1:-1].replace('""', '"')
    return text.strip()


def _split_quoted(line: str) -> list[str]:
    """Whitespace-separated fields, a double-quoted field kept whole."""
    return [_clean_cell(token) for token in re.findall(r'"[^"]*"|\S+', line)]


# ---------------------------------------------------------------- pick and place

_PNP_ROLES = {
    "designator": "designator", "refdes": "designator", "ref des": "designator",
    "reference": "designator", "reference designator": "designator",
    "comment": "comment", "value": "value", "description": "description",
    "footprint": "footprint", "pattern": "footprint", "package": "footprint",
    "layer": "layer", "side": "layer", "tb": "layer", "rotation": "rotation",
    "rot": "rotation", "angle": "rotation", "height": "height",
}
_PNP_POINT = re.compile(r"^(center|centre|mid|ref|pad)\s*[-_ ]?\s*([xy])$")
_POINT_NAME = {"center": "center", "centre": "center", "mid": "center", "ref": "ref",
               "pad": "pad"}
_HEADER_UNIT = re.compile(r"\(\s*(mm|mils?|in|inch|inches|um)\s*\)", re.IGNORECASE)
# Header names with a space (older pick-and-place headers) stay one token.
_PNP_TOKEN = re.compile(
    r'(?i)(?:center|centre|mid|ref|pad)[ \-_]?[xy](?:\s*\((?:mm|mils?|in|inch)\))?'
    r'|"[^"]*"|\S+')
_TOP = {"top", "toplayer", "top layer", "t", "top side", "topside", "front", "f.cu"}
_BOTTOM = {"bottom", "bottomlayer", "bottom layer", "b", "bot", "bottom side",
           "bottomside", "back", "b.cu"}


def side_of(layer: str | None) -> str | None:
    """'top' / 'bottom' for the layer words outputs use (TopLayer, Top, T, ...)."""
    if not layer:
        return None
    word = re.sub(r"\s+", " ", layer.strip().strip('"')).lower()
    return "top" if word in _TOP else "bottom" if word in _BOTTOM else None


def _header_role(cell: str) -> tuple[str | None, str | None]:
    """(role, unit) of a pick-and-place header cell: role is a _PNP_ROLES value or
    'center_x', 'ref_y', ...; unit from a '(mm)' suffix."""
    text = _clean_cell(cell)
    unit_match = _HEADER_UNIT.search(text)
    unit = unit_match.group(1).lower() if unit_match else None
    name = re.sub(r"\s+", " ", _HEADER_UNIT.sub("", text).replace("_", " ")).strip().lower()
    point = _PNP_POINT.match(name)
    if point:
        return f"{_POINT_NAME[point.group(1)]}_{point.group(2)}", ("mil" if unit == "mils"
                                                                  else unit)
    return _PNP_ROLES.get(name), None


@dataclass
class PlacementRow:
    designator: str
    x_mil: float | None          # the file's main point: Center, else Ref, else Pad
    y_mil: float | None
    side: str | None             # 'top' / 'bottom' from the layer column
    rotation: float | None
    layer: str | None            # as written (TopLayer, BottomLayer, T, B)
    footprint: str | None = None
    comment: str | None = None
    description: str | None = None
    value: str | None = None
    points: dict[str, tuple[float, float]] = field(default_factory=dict)  # center/ref/pad
    extra: dict[str, str] = field(default_factory=dict)  # other columns, header as written
    line: int = 0                # 1-based line of the row


@dataclass
class PickPlace:
    rows: list[PlacementRow] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)  # header as written
    roles: dict[str, int] = field(default_factory=dict)  # role -> column index
    point: str | None = None     # which point x/y_mil hold: 'center', 'ref' or 'pad'
    units: str | None = None     # coordinate unit as stated: 'mm', 'mil', 'in'
    units_from: str | None = None  # 'header', 'preamble', 'values' or 'assumed'
    delimiter: str | None = None  # ',', ';', '\t' or 'fixed' (text output)
    header_line: int | None = None  # 1-based
    preamble: dict[str, str] = field(default_factory=dict)  # 'Key: value' lines above
    source: str | None = None    # the preamble's path line: the output file's own path
                                 # as Altium wrote it (every file sampled), not the PcbDoc
    generator: str | None = None  # 'Altium Designer Pick and Place Locations'
    variant: str | None = None   # None for 'No variations' or no Variant line
    encoding: str = "ascii"
    unparsed_lines: list[int] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _pnp_header(lines: list[str]) -> tuple[int, list[str], str] | None:
    """(index, cells, delimiter) of the first line naming a designator and an X column."""
    for index, line in enumerate(lines[:300]):
        if not re.search(r"(?i)designator|refdes|ref ?des|reference", line):
            continue
        candidates = []
        for delimiter in (",", ";", "\t"):
            if delimiter in line:
                candidates.append((next(csv.reader([line], delimiter=delimiter)), delimiter))
        candidates.append(([t.group(0) for t in _PNP_TOKEN.finditer(line)], "fixed"))
        for cells, delimiter in candidates:
            roles = {_header_role(c)[0] for c in cells}
            if "designator" in roles and roles & {"center_x", "ref_x", "pad_x"}:
                return index, cells, delimiter
    return None


def _fixed_cells(line: str, starts: list[int]) -> list[str] | None:
    """Cells of a text-output row sliced at the header's column starts, or None when a
    value overflows its column (the row is then split on whitespace instead)."""
    for start in starts[1:]:
        if start < len(line) and line[start - 1] != " ":
            return None
    return [_clean_cell(line[a:b]) for a, b in zip(starts, starts[1:] + [None])]


def parse_pick_place(data: bytes, name: str = "") -> PickPlace:
    """A pick-and-place file (text or CSV) from its bytes; name is its file name, used only
    to prefer the CSV reading of a '.csv'."""
    lines, encoding = decode_lines(data)
    result = PickPlace(encoding=encoding)
    found = _pnp_header(lines)
    if found is None:
        result.warnings.append("no header row with a designator and an X column")
        return result
    header_index, columns, delimiter = found
    if name.lower().endswith(".csv") and delimiter == "fixed":
        result.warnings.append("a .csv file read as fixed-width text")
    result.header_line = header_index + 1
    result.columns = [_clean_cell(c) for c in columns]
    result.delimiter = delimiter
    _pnp_preamble(result, lines[:header_index])
    units: dict[int, str | None] = {}
    for column, cell in enumerate(columns):
        role, unit = _header_role(cell)
        if role and role not in result.roles:
            result.roles[role] = column
        if role and role[-2:] in ("_x", "_y"):
            units[column] = unit
    preamble_unit = None
    for key, value in result.preamble.items():
        if key.lower() in ("units used", "units", "unit"):
            preamble_unit = value.strip().lower()
            if preamble_unit in ("metric", "millimeters", "millimetres"):
                preamble_unit = "mm"
            elif preamble_unit in ("imperial", "mils"):
                preamble_unit = "mil"
    stated = next((u for u in units.values() if u), None)
    for point in ("center", "ref", "pad"):
        if f"{point}_x" in result.roles and f"{point}_y" in result.roles:
            result.point = point
            break
    rows = _pnp_rows(lines, header_index, columns, delimiter, result)
    decimal_comma = delimiter in (";", "\t")
    if stated:
        result.units, result.units_from = stated, "header"
    elif unit_scale(preamble_unit):
        result.units, result.units_from = preamble_unit, "preamble"
    else:
        suffixes = Counter(
            number[1] for _line, cells in rows for column in units
            if column < len(cells)
            for number in [parse_number(cells[column], decimal_comma)] if number and number[1])
        if suffixes:
            result.units, result.units_from = suffixes.most_common(1)[0][0], "values"
        else:
            result.units, result.units_from = "mil", "assumed"
            result.warnings.append("coordinate units not stated: mil assumed")
    for line_no, cells in rows:
        row = _placement(cells, result, units, decimal_comma)
        if row is None:
            result.unparsed_lines.append(line_no)
            continue
        row.line = line_no
        result.rows.append(row)
    if result.unparsed_lines:
        result.unparsed_lines.sort()
        result.warnings.append(f"{len(result.unparsed_lines)} rows not split into the "
                               "header's columns")
    return result


def _pnp_preamble(result: PickPlace, lines: list[str]) -> None:
    for line in lines:
        text = line.strip()
        if not text or set(text) <= set("=-_*"):
            continue
        if re.search(r"(?i)pick\s*(and|&)?\s*place", text) and ":" not in text:
            result.generator = text
            continue
        if re.match(r"^(?:[A-Za-z]:[\\/]|\\\\)", text):
            result.source = text
            continue
        key, sep, value = text.partition(":")
        if sep and key.strip():
            result.preamble[key.strip()] = value.strip()
    variant = next((v for k, v in result.preamble.items() if k.lower() == "variant"), None)
    if variant and variant.lower() not in ("no variations", "[no variations]", "none"):
        result.variant = variant


def _pnp_rows(lines, header_index, columns, delimiter, result) -> list[tuple[int, list[str]]]:
    """(1-based line, cells) of every data row; lines that split into no row are listed
    in result.unparsed_lines. A value with a line break in it continues on lines that are
    no row of their own; an odd quote alone is no such sign (Altium leaves inch marks in
    descriptions unescaped)."""
    rows: list[tuple[int, list[str]]] = []
    body = lines[header_index + 1:]
    starts = [t.start() for t in _PNP_TOKEN.finditer(lines[header_index])]
    joiner = " " if delimiter == "fixed" else "\n"

    def row_cells(text: str) -> list[str] | None:
        cells = (_text_cells(text, starts, len(columns), result) if delimiter == "fixed"
                 else _csv_cells(text, delimiter, len(columns)))
        return cells if cells is not None and _numeric_ok(cells, result) else None

    index = 0
    while index < len(body):
        if not body[index].strip():
            index += 1
            continue
        line_no = header_index + 2 + index
        cells, used = None, 1
        if body[index].count('"') % 2:
            end = index + 1
            while end < len(body) and end - index < 6 and body[end].strip() and \
                    row_cells(body[end]) is None:
                end += 1
            if end > index + 1:
                cells = row_cells(joiner.join(body[index:end]))
                used = end - index if cells is not None else 1
        if cells is None:
            cells = row_cells(body[index])
        if cells is None:
            result.unparsed_lines.append(line_no)
        else:
            rows.append((line_no, cells))
        index += used
    return rows


def _text_cells(text: str, starts: list[int], count: int, result) -> list[str] | None:
    cells = _fixed_cells(text, starts)
    if cells is not None and len(cells) == count and _numeric_ok(cells, result):
        return cells
    cells = _split_quoted(text)
    return cells if len(cells) == count else None


def _csv_cells(text: str, delimiter: str, count: int) -> list[str] | None:
    """Cells of a CSV row. Altium quotes every field without escaping the quotes inside
    one (an inch mark in a description), so a fully quoted row is split on the quoted
    separators first; any other row goes to the csv module."""
    stripped = text.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] == '"':
        cells = stripped[1:-1].split(f'"{delimiter}"')
        if len(cells) == count:
            return [c.replace('""', '"').strip() for c in cells]
    rows = list(csv.reader(text.split("\n"), delimiter=delimiter))
    if len(rows) != 1:
        return None
    cells = [_clean_cell(c) for c in rows[0]]
    while len(cells) > count and not cells[-1]:
        cells.pop()
    return cells if len(cells) == count else None


def _numeric_ok(cells: list[str], result: PickPlace) -> bool:
    """A designator, and numbers where the coordinates and the rotation go."""
    for role, column in result.roles.items():
        if role[-2:] in ("_x", "_y") or role == "rotation":
            if column >= len(cells) or parse_number(cells[column], True) is None:
                return False
    designator = result.roles["designator"]
    return designator < len(cells) and bool(cells[designator])


def _placement(cells, result: PickPlace, units, decimal_comma) -> PlacementRow | None:
    """A row from its cells, or None when a coordinate or the rotation is no number. A
    value's own unit suffix wins over its column's and the file's."""
    roles = result.roles

    def cell(role):
        column = roles.get(role)
        return cells[column] if column is not None and column < len(cells) else None

    designator = cell("designator")
    if not designator:
        return None
    points: dict[str, tuple[float, float]] = {}
    for point in ("center", "ref", "pad"):
        if f"{point}_x" not in roles or f"{point}_y" not in roles:
            continue
        xy = []
        for axis in ("x", "y"):
            column = roles[f"{point}_{axis}"]
            number = parse_number(cell(f"{point}_{axis}"), decimal_comma)
            if number is None:
                return None
            scale = unit_scale(number[1]) or unit_scale(units.get(column)) or unit_scale(
                result.units)
            xy.append(number[0] * scale)
        points[point] = (xy[0], xy[1])
    rotation_text = cell("rotation")
    rotation = parse_number(rotation_text.rstrip("\u00b0") if rotation_text else None,
                            decimal_comma)
    if rotation_text and rotation is None:
        return None
    layer = cell("layer")
    used = set(roles.values())
    extra = {result.columns[i]: c for i, c in enumerate(cells)
             if i not in used and i < len(result.columns) and result.columns[i]}
    main = points.get(result.point) if result.point else None
    return PlacementRow(
        designator=designator, x_mil=main[0] if main else None,
        y_mil=main[1] if main else None, side=side_of(layer),
        rotation=rotation[0] if rotation else None, layer=layer or None,
        footprint=cell("footprint") or None, comment=cell("comment") or None,
        description=cell("description") or None, value=cell("value") or None,
        points=points, extra=extra)


# ---------------------------------------------------------------- BOM

_BOM_ROLES = {
    "designator": "designator", "designators": "designator", "refdes": "designator",
    "ref des": "designator", "reference": "designator", "references": "designator",
    "reference designator": "designator", "reference designators": "designator",
    "ref designator": "designator", "ref": "designator",
    "quantity": "quantity", "qty": "quantity", "qty.": "quantity", "count": "quantity",
    "comment": "comment", "value": "value", "description": "description",
    "desc": "description", "footprint": "footprint", "package": "footprint",
    "pcb footprint": "footprint", "case": "footprint", "case/package": "footprint",
    "libref": "libref", "lib ref": "libref", "library ref": "libref",
    "library reference": "libref", "design item id": "item_id", "name": "item_id",
    "altium vault p/n": "item_id", "mpn": "mpn", "line #": "line", "line": "line",
    "item": "line", "item #": "line", "#": "line", "no.": "line", "pos": "line",
}
_BOM_DETAIL_ROLES = {"comment", "description", "footprint", "value", "mpn", "manufacturer",
                     "libref", "item_id"}
_RANGE = re.compile(r"^([A-Za-z_][A-Za-z_]*?)(\d+)[-\u2013]\1(\d+)$")
_RANGE_LIMIT = 1000
_DESIGNATOR_WORD = re.compile(r"^[A-Za-z_]+\d")


def split_designators(text: str) -> tuple[list[str], int]:
    """(designators, ranges expanded) of a BOM designator cell: separated by ',', ';' or
    whitespace; 'R1-R4' (the prefix repeated) expands to R1..R4. Between commas, words
    that are not all designators (letters then a digit) stay one designator: Altium
    allows a space in one ('U1 A')."""
    text = re.sub(r"\s*([-\u2013])\s*(?=[A-Za-z_]+\d)", r"\1", text.strip())
    out: list[str] = []
    ranges = 0
    tokens: list[str] = []
    for piece in re.split(r"[,;]+", text):
        words = piece.split()
        if len(words) > 1 and not all(_DESIGNATOR_WORD.match(word) for word in words):
            tokens.append(" ".join(words))
        else:
            tokens.extend(words)
    for token in tokens:
        if not token:
            continue
        match = _RANGE.match(token)
        if match and 0 < int(match.group(3)) - int(match.group(2)) <= _RANGE_LIMIT:
            prefix, first, last = match.group(1), int(match.group(2)), int(match.group(3))
            out.extend(f"{prefix}{n}" for n in range(first, last + 1))
            ranges += 1
        else:
            out.append(token)
    return out, ranges


def bom_role(header: str) -> tuple[str | None, int]:
    """(role, index) of a BOM header cell. MPN and manufacturer columns use the parameter
    spellings get_bom reads ('Manufacturer Part Number 1', 'Mfr. P/N', ...); index is
    their number (0 when none) so the lowest wins."""
    name = norm_name(_clean_cell(header))
    name = re.sub(r"^manufacture\b", "manufacturer", name)  # Altium's template spelling
    if name in _BOM_ROLES:
        return _BOM_ROLES[name], 0
    match = MPN_NAME.match(name)
    if match:
        return "mpn", int(match.group(1) or 0)
    match = MFR_NAME.match(name)
    if match:
        return "manufacturer", int(match.group(1) or 0)
    if PART_NUMBER.match(name):
        return "part_number", 0
    if name.startswith(("designator", "quantity")):
        return name.split()[0], 0
    return None, 0


@dataclass
class BomLine:
    designators: list[str]
    quantity: float | None
    designator_text: str = ""
    comment: str | None = None
    description: str | None = None
    value: str | None = None
    footprint: str | None = None
    mpn: str | None = None
    manufacturer: str | None = None
    part_number: str | None = None   # a generic 'Part Number' column (not always the MPN)
    libref: str | None = None
    item_id: str | None = None       # Design Item ID / Name
    fields: dict[str, str] = field(default_factory=dict)  # every column, header as written
    row: int = 0                     # 1-based row in the table


@dataclass
class Bom:
    lines: list[BomLine] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    roles: dict[str, int] = field(default_factory=dict)
    header_row: int | None = None    # 1-based
    title: dict[str, str] = field(default_factory=dict)  # 'Key:' cells above the header
    delimiter: str | None = None
    encoding: str | None = None
    ranges_expanded: int = 0
    skipped_rows: int = 0            # non-empty rows with neither designator nor quantity
    warnings: list[str] = field(default_factory=list)

    @property
    def designators(self) -> list[str]:
        return [d for line in self.lines for d in line.designators]


def _bom_header(rows: list[list[str]]) -> int | None:
    fallback = None
    for index, row in enumerate(rows[:80]):
        roles = {bom_role(c)[0] for c in row if c and c.strip()}
        if "designator" in roles and "quantity" in roles:
            return index
        if fallback is None and "designator" in roles and len(roles & _BOM_DETAIL_ROLES) >= 2:
            fallback = index
    return fallback


def parse_bom_rows(rows: list[list[str]]) -> Bom:
    """A BOM from the rows of any table (CSV, or a workbook read elsewhere): the header
    row is found by name, the title block above it read as 'Key:' / value cells."""
    result = Bom()
    rows = [[("" if c is None else str(c)) for c in row] for row in rows]
    header = _bom_header(rows)
    if header is None:
        result.warnings.append("no header row with a designator column")
        return result
    result.header_row = header + 1
    result.columns = [_clean_cell(c) for c in rows[header]]
    best: dict[str, tuple[int, int]] = {}
    for column, cell in enumerate(rows[header]):
        role, rank = bom_role(cell)
        if role and (role not in best or rank < best[role][0]):
            best[role] = (rank, column)
    result.roles = {role: column for role, (_rank, column) in best.items()}
    result.title = _bom_title(rows[:header])
    roles = result.roles
    keys: list[str] = []  # field names: a repeated header (a second supplier) numbered
    for i, column in enumerate(result.columns):
        key = column or f"column {i + 1}"
        while key in keys:
            key = f"{column or 'column'} ({sum(k.startswith(column) for k in keys) + 1})"
        keys.append(key)
    for index, row in enumerate(rows[header + 1:], start=header + 2):
        cells = [_clean_cell(c) for c in row]
        if not any(cells):
            continue

        def cell(role, cells=cells):
            column = roles.get(role)
            return cells[column] if column is not None and column < len(cells) else ""

        designator_text, quantity_text = cell("designator"), cell("quantity")
        if not designator_text and not quantity_text:
            result.skipped_rows += 1
            continue
        designators, ranges = split_designators(designator_text)
        result.ranges_expanded += ranges
        quantity = parse_number(quantity_text, True) if quantity_text else None
        if quantity_text and quantity is None:
            result.warnings.append(f"row {index}: quantity is not a number")
        line = BomLine(
            designators=designators, quantity=quantity[0] if quantity else None,
            designator_text=designator_text, row=index,
            fields={keys[i]: c for i, c in enumerate(cells) if i < len(keys)})
        for role in ("comment", "description", "value", "footprint", "mpn", "manufacturer",
                     "part_number", "libref", "item_id"):
            setattr(line, role, cell(role) or None)
        result.lines.append(line)
    return result


def _bom_title(rows: list[list[str]]) -> dict[str, str]:
    title: dict[str, str] = {}
    for row in rows:
        cells = [_clean_cell(c) for c in row]
        for i, text in enumerate(cells):
            if not text:
                continue
            key, sep, value = text.partition(":")
            if not sep or not key.strip() or len(key) > 40:
                continue
            if not value.strip():
                value = next((c for c in cells[i + 1:] if c), "")
            title.setdefault(key.strip(), value.strip())
    return title


def parse_bom_csv(data: bytes) -> Bom:
    """A BOM saved as CSV (or tab/semicolon separated text); of the delimiters that find
    a header row, the one giving the most recognised columns wins."""
    lines, encoding = decode_lines(data)
    best: Bom | None = None
    for delimiter in (",", ";", "\t"):
        if not any(delimiter in line for line in lines[:80]):
            continue
        bom = parse_bom_rows(list(csv.reader(lines, delimiter=delimiter)))
        if bom.header_row is not None and (best is None or len(bom.roles) > len(best.roles)):
            bom.delimiter = delimiter
            best = bom
    if best is None:
        best = Bom(warnings=["no header row with a designator column"])
    best.encoding = encoding
    return best


_XLSX_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_XLSX_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_XLSX_PACKAGE_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"
XLSX_PART_LIMIT = 64 << 20  # bytes of one unpacked workbook part


def xlsx_sheets(data: bytes, driver_like: bool = False) -> list[tuple[str, list[list[str]]]]:
    """(name, rows) of each worksheet of an .xlsx workbook (what Altium's BOM templates
    write), in workbook order; a row is its cells as text ('' for empty ones), rows keep
    their numbers (a missing row is []). Numbers are written as Excel stores them (an
    integral value without '.0'). driver_like: numbers and dates as the Excel OLE DB
    provider returns them through .NET instead (a date-formatted cell as an ISO date and
    time, 2024-01-02T00:00:00; other numbers in round-trip form, 1E-07). Raises
    ValueError for a file that is no workbook."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as error:
        raise ValueError(f"not an .xlsx workbook: {error}") from error
    try:
        return _xlsx_read(archive, driver_like)
    except (ParseError, KeyError, zipfile.BadZipFile) as error:
        raise ValueError(f"unreadable workbook: {type(error).__name__}: {error}") from error
    finally:
        archive.close()


# Excel's built-in date and time number formats.
_XLSX_DATE_FORMATS = set(range(14, 23)) | set(range(27, 37)) | set(range(45, 48)) \
    | set(range(50, 59))


def _xlsx_date_styles(styles) -> set[int]:
    """The cell style indices (cellXfs) whose number format is a date or a time."""
    if styles is None:
        return set()
    custom = {}
    for item in styles.iter(f"{_XLSX_MAIN}numFmt"):
        code = re.sub(r'"[^"]*"|\[[^\]]*\]|\\.', "", item.get("formatCode") or "")
        custom[int(item.get("numFmtId") or -1)] = bool(re.search(r"[dmyhs]", code, re.I))
    found = set()
    xfs = next(styles.iter(f"{_XLSX_MAIN}cellXfs"), None)
    for index, xf in enumerate(xfs if xfs is not None else []):
        number = int(xf.get("numFmtId") or 0)
        if number in _XLSX_DATE_FORMATS or custom.get(number):
            found.add(index)
    return found


def _driver_number(value: float) -> str:
    """A number as .NET's round-trip format writes it ('R': 15 digits, else 17)."""
    text = f"{value:.15g}"
    if float(text) != value:
        text = f"{value:.17g}"
    return text.replace("e", "E")


def _driver_date(value: float, date1904: bool) -> str:
    """An Excel serial date as an ISO date and time (OLE Automation days from
    1899-12-30, rounded to the millisecond, shown to the second)."""
    days = value + (1462 if date1904 else 0)
    moment = datetime(1899, 12, 30) + timedelta(milliseconds=round(days * 86400000))
    return moment.strftime("%Y-%m-%dT%H:%M:%S")


def _xlsx_read(archive: zipfile.ZipFile, driver_like: bool = False
               ) -> list[tuple[str, list[list[str]]]]:
    def part(name: str):
        try:
            info = archive.getinfo(name)
        except KeyError:
            return None
        if info.file_size > XLSX_PART_LIMIT:
            raise ValueError(f"{name}: larger than {XLSX_PART_LIMIT >> 20} MB")
        return fromstring(archive.read(info))

    shared = []
    strings = part("xl/sharedStrings.xml")
    if strings is not None:
        for item in strings.iter(f"{_XLSX_MAIN}si"):
            shared.append(_xlsx_text(item))
    targets = {}
    rels = part("xl/_rels/workbook.xml.rels")
    if rels is not None:
        for rel in rels.iter(f"{_XLSX_PACKAGE_REL}Relationship"):
            target = rel.get("Target", "").lstrip("/")
            targets[rel.get("Id")] = target if target.startswith("xl/") else f"xl/{target}"
    book = part("xl/workbook.xml")
    if book is None:
        raise ValueError("not an .xlsx workbook: no xl/workbook.xml")
    driver = None
    if driver_like:
        properties = next(book.iter(f"{_XLSX_MAIN}workbookPr"), None)
        date1904 = properties is not None and (properties.get("date1904") or "").lower() in (
            "1", "true")
        driver = (_xlsx_date_styles(part("xl/styles.xml")), date1904)
    sheets = []
    for sheet in book.iter(f"{_XLSX_MAIN}sheet"):
        target = targets.get(sheet.get(f"{_XLSX_REL}id"))
        root = part(target) if target else None
        if root is not None:
            sheets.append((sheet.get("name") or "", _xlsx_rows(root, shared, driver)))
    return sheets


def _xlsx_text(item) -> str:
    """The text of a shared or inline string: its own <t> or its runs' (<r><t>), not the
    phonetic guides (<rPh>)."""
    parts = []
    for child in item:
        tag = _local(child.tag)
        if tag == "t":
            parts.append(child.text or "")
        elif tag == "r":
            parts.extend(t.text or "" for t in child if _local(t.tag) == "t")
    return "".join(parts)


def _xlsx_rows(root, shared: list[str], driver=None) -> list[list[str]]:
    """driver: (date style indices, 1904 date system) for driver-like numbers."""
    rows: list[list[str]] = []
    for row in root.iter(f"{_XLSX_MAIN}row"):
        number = int(row.get("r") or len(rows) + 1)
        while len(rows) < number - 1:
            rows.append([])
        cells: dict[int, str] = {}
        for position, cell in enumerate(row.iter(f"{_XLSX_MAIN}c")):
            reference = re.match(r"([A-Z]+)", cell.get("r") or "")
            column = position
            if reference:
                column = 0
                for letter in reference.group(1):
                    column = column * 26 + ord(letter) - 64
                column -= 1
            kind = cell.get("t")
            value = cell.find(f"{_XLSX_MAIN}v")
            text = value.text if value is not None and value.text is not None else ""
            if kind == "s" and text.isdigit():
                text = shared[int(text)] if int(text) < len(shared) else ""
            elif kind == "inlineStr":
                inline = cell.find(f"{_XLSX_MAIN}is")
                text = _xlsx_text(inline) if inline is not None else ""
            elif kind in (None, "n") and text:
                number_value = _float(text)
                if number_value is not None and driver is not None:
                    dates, date1904 = driver
                    style = cell.get("s")
                    text = (_driver_date(number_value, date1904)
                            if style and style.isdigit() and int(style) in dates
                            and 0 <= number_value < 2958466 else _driver_number(number_value))
                elif number_value is not None:
                    text = (str(int(number_value)) if number_value.is_integer()
                            and abs(number_value) < 1e15 else f"{number_value:.15g}")
            cells[column] = text
        rows.append([cells.get(i, "") for i in range(max(cells) + 1)] if cells else [])
    return rows


def parse_bom_xlsx(data: bytes) -> Bom:
    """A BOM workbook (.xlsx): the first sheet with a BOM header row (Altium's templates
    put the BOM on the first), read with parse_bom_rows."""
    sheets = xlsx_sheets(data)
    first = None
    for _name, rows in sheets:
        bom = parse_bom_rows(rows)
        if bom.header_row is not None:
            return bom
        first = first or bom
    return first or Bom(warnings=["the workbook has no worksheet"])


# ---------------------------------------------------------------- IPC-D-356A

# P UNITS: CUST 0 (and a bare CUST, as Altium writes it) inch, CUST 1 millimetre; the
# coordinates and sizes are integers of 0.0001 inch or 0.001 mm.
_IPC356_UNITS = {"CUST": "inch", "CUST 0": "inch", "CUST 2": "inch", "ENG": "inch",
                 "ENGLISH": "inch", "INCH": "inch", "CUST 1": "mm", "SI": "mm",
                 "METRIC": "mm", "MM": "mm"}
_IPC356_STEP_MIL = {"inch": 0.1, "mm": 0.001 * MIL_PER_MM}
_IPC356_TAIL = re.compile(
    r"^(?P<mid>M)?\s*(?:D\s*(?P<hole>\d+))?\s*(?P<plated>[PU])?\s*(?:A\s*(?P<access>\d+))?\s*"
    r"X\s*(?P<xs>[+-]?)\s*(?P<x>\d+)\s*Y\s*(?P<ys>[+-]?)\s*(?P<y>\d+)"
    r"(?:\s*X\s*(?P<w>\d+))?(?:\s*Y\s*(?P<h>\d+))?(?:\s*R\s*(?P<rot>\d+))?"
    r"(?:\s*S\s*(?P<mask>\d))?")


@dataclass
class Ipc356Record:
    code: str                  # '317' through-hole, '327' surface, '367' tooling hole
    net: str                   # long name when an NNAME alias is used; 'N/C' as written
    refdes: str                # 'VIA' for a via
    pin: str
    x_mil: float
    y_mil: float
    hole_mil: float | None = None
    plated: bool | None = None
    access: int | None = None  # 0 both sides, 1 top, n copper layer n (bottom = count)
    width_mil: float | None = None
    height_mil: float | None = None
    rotation: float | None = None
    soldermask: int | None = None  # 0 none, 1 primary side, 2 secondary, 3 both
    midpoint: bool = False
    raw_net: str = ""          # the net field as written (an NNAME alias, if any)
    line: int = 0

    @property
    def is_via(self) -> bool:
        return self.refdes.upper() == "VIA"

    @property
    def unconnected(self) -> bool:
        return self.net.upper() in ("N/C", "")

    def side(self, copper_layers: int | None = None) -> str | None:
        """'both', 'top', 'bottom' or 'inner' from the access code (bottom needs the
        board's copper layer count; without it a code above 1 reads by the mask code)."""
        if self.access == 0:
            return "both"
        if self.access == 1:
            return "top"
        if self.access is None:
            return None
        if copper_layers:
            return "bottom" if self.access >= copper_layers else "inner"
        return "bottom" if self.soldermask == 2 else None


@dataclass
class Ipc356:
    units: str | None = None             # 'inch' or 'mm'
    units_raw: str | None = None         # the P UNITS value as written
    parameters: dict[str, str] = field(default_factory=dict)  # P JOB, TITLE, VER, ...
    aliases: dict[str, str] = field(default_factory=dict)     # NNAME alias -> net name
    records: list[Ipc356Record] = field(default_factory=list)
    adjacency: dict[str, list[str]] = field(default_factory=dict)  # 379 (+ 079)
    comments: list[str] = field(default_factory=list)
    record_counts: Counter = field(default_factory=Counter)  # by operation code
    ended: bool = False                  # a 999 end record was seen
    warnings: list[str] = field(default_factory=list)

    def nets(self) -> dict[str, list[tuple[str, str]]]:
        """Net -> (refdes, pin) of its component features (vias and N/C left out)."""
        out: dict[str, list[tuple[str, str]]] = {}
        for record in self.records:
            if record.is_via or record.unconnected or record.code == "367":
                continue
            out.setdefault(record.net, []).append((record.refdes, record.pin))
        return out


def parse_ipc356(data: bytes) -> Ipc356:
    lines, _encoding = decode_lines(data)
    result = Ipc356()
    for line in lines:  # the parameters (units, aliases) first: they may follow records
        if line.startswith("P "):
            _ipc356_parameter(result, line)
    if result.units is None:
        result.units = "inch"
        result.warnings.append("no P UNITS record: inch (CUST 0) assumed")
    step = _IPC356_STEP_MIL[result.units]
    last_adjacent: str | None = None
    for number, line in enumerate(lines, start=1):
        code = line[:3]
        if not line.strip() or line.startswith("P "):
            continue
        if line.startswith("C"):
            result.comments.append(line[1:].strip())
            continue
        result.record_counts[code] += 1
        if code == "999":
            result.ended = True
        elif code in ("317", "327", "367"):
            record = _ipc356_record(line, step, result)
            if record is None:
                result.warnings.append(f"line {number}: {code} record not read")
                continue
            record.line = number
            result.records.append(record)
        elif code == "379":
            names = line[3:].split()
            if names:
                last_adjacent = result.aliases.get(names[0], names[0])
                result.adjacency.setdefault(last_adjacent, []).extend(
                    result.aliases.get(n, n) for n in names[1:])
        elif code == "079" and last_adjacent is not None:
            result.adjacency[last_adjacent].extend(
                result.aliases.get(n, n) for n in line[3:].split())
    continued = result.record_counts["017"] + result.record_counts["027"]
    if continued:
        result.warnings.append(f"{continued} continuation records (017/027) not read")
    return result


def _ipc356_parameter(result: Ipc356, line: str) -> None:
    body = line[1:].strip()
    key, _, value = body.partition(" ")
    key, value = key.strip().upper(), value.strip()
    if key == "UNITS":
        result.units_raw = value
        normal = re.sub(r"\s+", " ", value.upper())
        result.units = _IPC356_UNITS.get(normal)
        if result.units is None:
            result.warnings.append(f"P UNITS {value!r} not known: inch assumed")
            result.units = "inch"
    elif re.fullmatch(r"NNAME\d+", key):
        result.aliases[key] = value
    elif key:
        result.parameters.setdefault(key, value)


def _ipc356_record(line: str, step: float, result: Ipc356) -> Ipc356Record | None:
    line = line.ljust(80)
    raw_net = line[3:17].strip()
    refdes = line[20:26].strip()
    pin = line[27:31].strip() if line[26] == "-" else line[26:31].strip(" -")
    match = _IPC356_TAIL.match(line[31:].strip())
    if not match:
        return None

    def length(name):
        return int(match.group(name)) * step if match.group(name) else None

    x = int(match.group("x")) * step * (-1 if match.group("xs") == "-" else 1)
    y = int(match.group("y")) * step * (-1 if match.group("ys") == "-" else 1)
    return Ipc356Record(
        code=line[:3], net=result.aliases.get(raw_net, raw_net), refdes=refdes, pin=pin,
        x_mil=x, y_mil=y, hole_mil=length("hole"),
        plated={"P": True, "U": False}.get(match.group("plated") or ""),
        access=int(match.group("access")) if match.group("access") else None,
        width_mil=length("w"), height_mil=length("h"),
        rotation=float(match.group("rot")) if match.group("rot") else None,
        soldermask=int(match.group("mask")) if match.group("mask") else None,
        midpoint=bool(match.group("mid")), raw_net=raw_net)


# ---------------------------------------------------------------- IPC-2581

_IPC2581_UNITS = {"MILLIMETER": MIL_PER_MM, "MM": MIL_PER_MM, "INCH": 1000.0,
                  "MICRON": MIL_PER_MM / 1000, "MILS": 1.0, "MIL": 1.0}


@dataclass
class Ipc2581Component:
    refdes: str
    package: str | None = None
    part: str | None = None
    layer: str | None = None
    side: str | None = None          # 'top' / 'bottom' from the layer (or mirror)
    rotation: float | None = None
    mirror: bool | None = None
    x_mil: float | None = None
    y_mil: float | None = None
    mount_type: str | None = None


@dataclass
class Ipc2581Layer:
    name: str
    function: str | None = None      # layerFunction: SIGNAL, PLANE, DIELCORE, DRILL, ...
    side: str | None = None          # TOP, BOTTOM, INTERNAL, ALL
    polarity: str | None = None


@dataclass
class Ipc2581StackupLayer:
    name: str                        # layerOrGroupRef
    thickness_mil: float | None = None
    sequence: int | None = None
    function: str | None = None      # from the Layer of that name
    material: str | None = None
    dielectric_constant: float | None = None
    loss_tangent: float | None = None
    specs: list[str] = field(default_factory=list)


@dataclass
class Ipc2581Hole:
    kind: str                        # the element: 'Hole' or 'LayerHole'
    name: str | None = None
    diameter_mil: float | None = None
    plating: str | None = None       # platingStatus: PLATED, NONPLATED, VIA, ...
    x_mil: float | None = None
    y_mil: float | None = None
    net: str | None = None           # of the enclosing Set
    layer: str | None = None         # the enclosing LayerFeature's (or its own) layerRef
    span: tuple[str, str] | None = None  # Span fromLayer, toLayer


@dataclass
class Ipc2581:
    revision: str | None = None
    units: str | None = None
    nets: dict[str, list[tuple[str, str]]] = field(default_factory=dict)  # LogicalNet pins
    phy_nets: dict[str, int] = field(default_factory=dict)  # PhyNet -> point count
    net_names: set[str] = field(default_factory=set)  # every net="..." attribute
    components: list[Ipc2581Component] = field(default_factory=list)
    layers: list[Ipc2581Layer] = field(default_factory=list)
    stackup: list[Ipc2581StackupLayer] = field(default_factory=list)
    overall_thickness_mil: float | None = None
    holes: list[Ipc2581Hole] = field(default_factory=list)
    span_count: int = 0
    populate: dict[str, bool] = field(default_factory=dict)  # Bom RefDes populate flags
    warnings: list[str] = field(default_factory=list)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _float(text: str | None) -> float | None:
    try:
        return float(text) if text not in (None, "") else None
    except ValueError:
        return None


def _bool(text: str | None) -> bool | None:
    return {"true": True, "false": False, "1": True, "0": False}.get((text or "").lower())


class _Ipc2581Reader:
    """Streams the XML (files run to hundreds of MB), keeping the open elements' tags and
    attributes for context (a Hole's Set net and LayerFeature layer)."""

    _CLEAR = {"Component", "LogicalNet", "PhyNet", "Package", "LayerFeature", "BomItem",
              "Spec", "StackupLayer", "Profile"}

    def __init__(self):
        self.result = Ipc2581()
        self.scale: float | None = None
        self.stack: list[tuple[str, dict]] = []
        self.specs: dict[str, dict] = {}
        self.raw_stackup: list[tuple[str, str | None, str | None, list[str]]] = []
        self.raw_components: list[tuple[Ipc2581Component, str | None, str | None]] = []
        self.raw_holes: list[tuple[Ipc2581Hole, dict]] = []
        self.overall: str | None = None

    def start(self, tag: str, attrib: dict) -> None:
        self.stack.append((tag, attrib))
        result = self.result
        if tag == "IPC-2581":
            result.revision = attrib.get("revision")
        elif tag == "CadHeader" and self.scale is None:
            result.units = attrib.get("units")
            self.scale = _IPC2581_UNITS.get((result.units or "").upper())
        elif tag == "DictionaryStandard" and result.units is None and attrib.get("units"):
            result.units = attrib["units"]
        elif tag == "Layer" and attrib.get("name") and self._inside("CadData"):
            result.layers.append(Ipc2581Layer(
                name=attrib["name"], function=attrib.get("layerFunction"),
                side=attrib.get("side"), polarity=attrib.get("polarity")))
        elif tag == "Stackup" and self.overall is None:
            self.overall = attrib.get("overallThickness")
        elif tag == "Span":
            result.span_count += 1
        net = attrib.get("net")
        if net:
            result.net_names.add(net)

    def _inside(self, tag: str) -> bool:
        return any(t == tag for t, _ in self.stack[:-1])

    def _ancestor(self, tag: str) -> dict | None:
        for t, attrib in reversed(self.stack[:-1]):
            if t == tag:
                return attrib
        return None

    def end(self, element) -> None:
        tag = _local(element.tag)
        attrib = element.attrib
        result = self.result
        children = [(_local(child.tag), child) for child in element]
        if tag == "LogicalNet" and attrib.get("name"):
            pins = result.nets.setdefault(attrib["name"], [])
            pins.extend((c.get("componentRef", ""), c.get("pin", ""))
                        for t, c in children if t == "PinRef")
        elif tag == "PhyNet" and attrib.get("name"):
            result.phy_nets[attrib["name"]] = result.phy_nets.get(attrib["name"], 0) + sum(
                1 for t, _ in children if t == "PhyNetPoint")
        elif tag == "Component" and attrib.get("refDes"):
            component = Ipc2581Component(
                refdes=attrib["refDes"], package=attrib.get("packageRef"),
                part=attrib.get("part"), layer=attrib.get("layerRef"),
                mount_type=attrib.get("mountType"))
            xform = next((c for t, c in children if t == "Xform"), None)
            location = next((c for t, c in children if t == "Location"), None)
            if xform is not None:
                component.rotation = _float(xform.get("rotation")) or 0.0
                component.mirror = _bool(xform.get("mirror"))
            self.raw_components.append((
                component, location.get("x") if location is not None else None,
                location.get("y") if location is not None else None))
        elif tag in ("Hole", "LayerHole"):
            span = next((c for t, c in children if t == "Span"), None)
            location = next((c for t, c in children if t == "Location"), None)
            point = {k: attrib.get(k) for k in ("x", "y", "diameter")}
            if location is not None:
                point["x"] = point["x"] or location.get("x")
                point["y"] = point["y"] or location.get("y")
            feature = self._ancestor("LayerFeature")
            hole_set = self._ancestor("Set")
            hole = Ipc2581Hole(
                kind=tag, name=attrib.get("name"), plating=attrib.get("platingStatus"),
                net=(hole_set or {}).get("net") or attrib.get("net"),
                layer=attrib.get("layerRef") or (feature or {}).get("layerRef"),
                span=(span.get("fromLayer", ""), span.get("toLayer", ""))
                if span is not None else None)
            self.raw_holes.append((hole, point))
        elif tag == "StackupLayer" and attrib.get("layerOrGroupRef"):
            refs = [c.get("id", "") for t, c in children if t == "SpecRef"]
            self.raw_stackup.append((attrib["layerOrGroupRef"], attrib.get("thickness"),
                                     attrib.get("sequence"), refs))
        elif tag == "Spec" and attrib.get("name"):
            self.specs[attrib["name"]] = _spec_values(element)
        elif tag == "RefDes" and attrib.get("name") and self._inside("BomItem"):
            populate = _bool(attrib.get("populate"))
            if populate is not None:
                result.populate[attrib["name"]] = populate
        self.stack.pop()
        if tag in self._CLEAR:
            element.clear()

    def finish(self) -> Ipc2581:
        result = self.result
        if self.scale is None:
            self.scale = _IPC2581_UNITS.get((result.units or "").upper())
        if self.scale is None:
            result.warnings.append(f"units {result.units!r} not known: millimetre assumed")
            self.scale = MIL_PER_MM

        def mil(text):
            value = _float(text)
            return value * self.scale if value is not None else None

        sides = {layer.name: (layer.side or "").upper() for layer in result.layers}
        functions = {layer.name: layer.function for layer in result.layers}
        for component, x, y in self.raw_components:
            component.x_mil, component.y_mil = mil(x), mil(y)
            side = sides.get(component.layer or "", "")
            if side in ("TOP", "BOTTOM"):
                component.side = side.lower()
            elif component.layer:
                component.side = side_of(component.layer) or (
                    "bottom" if "BOT" in component.layer.upper() else
                    "top" if "TOP" in component.layer.upper() else None)
            if component.side is None and component.mirror is not None:
                component.side = "bottom" if component.mirror else "top"
            result.components.append(component)
        for hole, point in self.raw_holes:
            hole.x_mil, hole.y_mil = mil(point["x"]), mil(point["y"])
            hole.diameter_mil = mil(point["diameter"])
            result.holes.append(hole)
        for name, thickness, sequence, refs in self.raw_stackup:
            layer = Ipc2581StackupLayer(
                name=name, thickness_mil=mil(thickness),
                sequence=int(sequence) if sequence and sequence.isdigit() else None,
                function=functions.get(name), specs=refs)
            for ref in refs:
                spec = self.specs.get(ref, {})
                layer.material = layer.material or spec.get("material")
                layer.dielectric_constant = (layer.dielectric_constant
                                             or spec.get("dielectric_constant"))
                layer.loss_tangent = layer.loss_tangent or spec.get("loss_tangent")
            result.stackup.append(layer)
        result.overall_thickness_mil = mil(self.overall)
        return result


def _spec_values(spec) -> dict:
    values: dict = {}
    for group in spec:
        kind = (group.get("type") or "").upper()
        props = [p for p in group if _local(p.tag) == "Property"]
        text = next((p.get("text") for p in props if p.get("text")), None)
        number = next((_float(p.get("value")) for p in props
                       if _float(p.get("value")) is not None), None)
        if kind == "MATERIAL" and text:
            values.setdefault("material", text)
        elif kind in ("DIELECTRIC_CONSTANT", "DK") and number is not None:
            values.setdefault("dielectric_constant", number)
        elif kind in ("LOSS_TANGENT", "DF", "DISSIPATION_FACTOR") and number is not None:
            values.setdefault("loss_tangent", number)
    return values


def parse_ipc2581(source) -> Ipc2581:
    """An IPC-2581 file from a path, its bytes or a binary file object, streamed."""
    if isinstance(source, (bytes, bytearray)):
        source = io.BytesIO(source)
    handle = source if hasattr(source, "read") else open(source, "rb")
    reader = _Ipc2581Reader()
    parser = XMLPullParser(events=("start", "end"))
    try:
        while chunk := handle.read(1 << 20):
            parser.feed(chunk)
            _drain(parser, reader)
        parser.close()
        _drain(parser, reader)
    except ParseError as error:
        reader.result.warnings.append(f"XML error: {error}")
    finally:
        if handle is not source:
            handle.close()
    return reader.finish()


def _drain(parser: XMLPullParser, reader: _Ipc2581Reader) -> None:
    for event, element in parser.read_events():
        if event == "start":
            reader.start(_local(element.tag), dict(element.attrib))
        else:
            reader.end(element)


# ---------------------------------------------------------------- Excellon NC drill

@dataclass
class DrillTool:
    number: int
    diameter_mil: float | None = None
    plated: bool | None = None     # from the ;TYPE= section the tool is defined in
    diameter_step_mil: float | None = None  # the written size's last digit, in mil
    hits: int = 0                  # drilled holes
    slots: int = 0                 # G85 and routed slots


@dataclass
class DrillHit:
    tool: int
    x_mil: float
    y_mil: float
    kind: str = "hole"             # 'hole', 'slot' (G85) or 'route' (M15..M16)
    x2_mil: float | None = None    # slot / route end
    y2_mil: float | None = None


@dataclass
class ExcellonDrill:
    units: str | None = None       # 'inch' or 'mm'
    zeros: str | None = None       # as written: 'TZ' (leading zeros left out, trailing
                                   # kept) or 'LZ' (leading kept, trailing left out)
    integer_digits: int | None = None
    decimals: int | None = None
    format_from: str | None = None  # 'FILE_FORMAT', 'header' or 'default'
    file_type: str | None = None   # the first ;TYPE= (PLATED, NON_PLATED, MIXED)
    tools: dict[int, DrillTool] = field(default_factory=dict)
    hits: list[DrillHit] = field(default_factory=list)
    comments: list[str] = field(default_factory=list)  # header ';' lines
    ended: bool = False            # M30 seen
    warnings: list[str] = field(default_factory=list)

    def holes_by_tool(self) -> Counter:
        return Counter(hit.tool for hit in self.hits)


_TOOL_DEF = re.compile(r"^T(\d+)((?:[FSBHZN][+-]?[\d.]+)*)C([\d.]+)")
_UNITS_LINE = re.compile(r"^(INCH|METRIC)\s*(?:,\s*(LZ|TZ))?\s*(?:,\s*(0*)\.(0*))?$")
_COORD = re.compile(r"([XY])([+-]?[\d.]+)")
_DEFAULT_FORMAT = {"inch": (2, 4), "mm": (3, 3)}


def parse_excellon(data: bytes, name: str = "") -> ExcellonDrill:
    lines, _encoding = decode_lines(data)
    result = ExcellonDrill()
    plated: bool | None = _plated_from_name(name)
    in_header = False
    current_tool: int | None = None
    x = y = 0.0
    route: list[tuple[float, float]] | None = None
    route_down = False
    for number, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line:
            continue
        upper = line.upper()
        if upper == "M48":
            in_header = True
            continue
        if line.startswith(";"):
            comment = line[1:].strip()
            if in_header:
                result.comments.append(comment)
            match = re.match(r"(?i)FILE_FORMAT\s*=\s*(\d+)\s*:\s*(\d+)", comment)
            if match:
                result.integer_digits, result.decimals = int(match.group(1)), int(match.group(2))
                result.format_from = "FILE_FORMAT"
            match = re.match(r"(?i)TYPE\s*=\s*(\w+)", comment)
            if match:
                kind = match.group(1).upper()
                result.file_type = result.file_type or kind
                plated = {"PLATED": True, "NON_PLATED": False, "NONPLATED": False}.get(kind)
            continue
        if upper in ("%", "M95"):
            in_header = False
            continue
        match = _UNITS_LINE.match(upper)
        if match:
            result.units = "inch" if match.group(1) == "INCH" else "mm"
            result.zeros = match.group(2) or result.zeros
            if match.group(4) is not None and result.format_from != "FILE_FORMAT":
                result.integer_digits = len(match.group(3))
                result.decimals = len(match.group(4))
                result.format_from = "header"
            continue
        if upper in ("M71", "M72"):
            result.units = "mm" if upper == "M71" else "inch"
            continue
        match = _TOOL_DEF.match(upper)
        if match:
            tool = result.tools.setdefault(int(match.group(1)), DrillTool(int(match.group(1))))
            tool.diameter_mil = float(match.group(3)) * _unit_mil(result)
            tool.diameter_step_mil = 10 ** -len(match.group(3).partition(".")[2]) * _unit_mil(
                result)
            tool.plated = plated
            continue
        if in_header:
            continue
        if re.fullmatch(r"T\d+", upper):
            current_tool = int(upper[1:])
            if current_tool and current_tool not in result.tools:
                result.tools[current_tool] = DrillTool(current_tool, plated=plated)
                result.warnings.append(f"line {number}: T{current_tool} used, not defined")
            continue
        if upper in ("M30", "M00"):
            result.ended = True
            continue
        if upper == "M15":
            route_down, route = True, [(x, y)]
            continue
        if upper in ("M16", "M17"):
            if route_down and route and len(route) > 1 and current_tool:
                result.hits.append(DrillHit(current_tool, *route[0], kind="route",
                                            x2_mil=route[-1][0], y2_mil=route[-1][1]))
                result.tools[current_tool].slots += 1
            route_down, route = False, None
            continue
        if upper.startswith("G91"):
            result.warnings.append(f"line {number}: incremental coordinates (G91) not read")
            continue
        if "G85" in upper:
            start, _, end = upper.partition("G85")
            x1, y1 = _move(start, x, y, result)
            x2, y2 = _move(end, x1, y1, result)
            x, y = x2, y2
            if current_tool:
                result.hits.append(DrillHit(current_tool, x1, y1, "slot", x2, y2))
                result.tools[current_tool].slots += 1
            continue
        motion = re.match(r"^G0?([0-3])(?!\d)", upper)
        if motion or upper[:1] in ("X", "Y"):
            x, y = _move(upper, x, y, result)
            if motion and motion.group(1) == "0":
                continue  # a router move to a route's start
            if route_down and route is not None:
                route.append((x, y))
                continue
            if motion is None and current_tool:
                result.hits.append(DrillHit(current_tool, x, y))
                result.tools[current_tool].hits += 1
            continue
        if upper.startswith("R") and re.match(r"^R\d+", upper):
            result.warnings.append(f"line {number}: repeat code not read")
    if result.units is None:
        result.units = "inch"
        result.warnings.append("no INCH/METRIC line: inch assumed")
    return result


def _plated_from_name(name: str) -> bool | None:
    stem = name.lower().rsplit(".", 1)[0]
    if stem.endswith(("-nonplated", "_nonplated", "-npth", "_npth", "-non-plated")):
        return False
    if stem.endswith(("-plated", "_plated", "-pth", "_pth")):
        return True
    return None


def _unit_mil(result: ExcellonDrill) -> float:
    return 1000.0 if (result.units or "inch") == "inch" else MIL_PER_MM


def _move(text: str, x: float, y: float, result: ExcellonDrill) -> tuple[float, float]:
    for axis, value in _COORD.findall(text):
        mil = _coordinate(value, result)
        if axis == "X":
            x = mil
        else:
            y = mil
    return x, y


def _coordinate(text: str, result: ExcellonDrill) -> float:
    scale = _unit_mil(result)
    if "." in text:
        return float(text) * scale
    if result.integer_digits is None:
        result.integer_digits, result.decimals = _DEFAULT_FORMAT[result.units or "inch"]
        result.format_from = "default"
        result.warnings.append("no number format stated: "
                               f"{result.integer_digits}:{result.decimals} assumed")
    sign = -1.0 if text.startswith("-") else 1.0
    digits = text.lstrip("+-")
    if result.zeros == "LZ":  # trailing zeros left out: pad to the full width
        digits = digits.ljust(result.integer_digits + result.decimals, "0")
    return sign * int(digits) / 10 ** result.decimals * scale


# ---------------------------------------------------------------- NC drill report (.DRR)

@dataclass
class DrillReportTool:
    tool: str                      # 'T1'
    size_mil: float
    hole_type: str                 # 'Round', 'Slot', ...
    count: int
    plated: bool | None            # PTH / NPTH
    tolerance: str | None = None   # as written ('+/-1mil (0.0254mm)')
    travel_mil: float | None = None
    size_step_mil: float | None = None  # the printed size's last digit, in mil: compare
                                        # with a drill file within half of it


@dataclass
class DrillReportPair:
    layer_pair: str | None = None  # as written ('Top Layer to Bottom Layer')
    from_layer: str | None = None
    to_layer: str | None = None
    files: dict[str, str] = field(default_factory=dict)  # 'ASCII RoundHoles' -> file name
    tools: list[DrillReportTool] = field(default_factory=list)
    total: int | None = None


@dataclass
class DrillReport:
    board: str | None = None       # 'NCDrill File Report For:' document
    stamp: str | None = None       # the date and time after it, as written
    pairs: list[DrillReportPair] = field(default_factory=list)
    hole_lists: list[dict] = field(default_factory=list)  # {label, count, points (raw)}
    processing_time: str | None = None
    warnings: list[str] = field(default_factory=list)


_DRR_TOOL = re.compile(
    r"^T(?P<tool>\d+)\s+(?P<size>\d+(?:\.\d+)?)\s*(?P<unit>mm|mil|inch|in)\b"
    r"\s*(?:\(\s*(?P<alt>\d+(?:\.\d+)?)\s*(?P<aunit>mm|mil|inch|in)\s*\))?"
    r"\s*(?P<tol>(?:\+/-|\+|-)\s*\d+(?:\.\d+)?\s*(?:mm|mil|inch)\s*(?:\([^)]*\))?)?"
    r"\s*(?P<type>[A-Za-z][\w ]*?)\s+(?P<count>\d+)(?:\s+(?P<plated>N?PTH))?"
    r"(?:\s+(?P<travel>\d+(?:\.\d+)?)\s*(?P<tunit>mm|mil|inch|in)\b)?", re.IGNORECASE)


def parse_drill_report(data: bytes) -> DrillReport:
    lines, _encoding = decode_lines(data)
    result = DrillReport()
    pair: DrillReportPair | None = None
    hole_list: dict | None = None
    for number, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line or set(line) <= set("-="):
            continue
        match = re.match(r"(?i)^NCDrill File Report For\s*:\s*(.*?)(?:\s{2,}(.*))?$", line)
        if match:
            result.board, result.stamp = match.group(1).strip(), (match.group(2) or None)
            continue
        match = re.match(r"(?i)^Layer Pair\s*:\s*(.*)$", line)
        if match:
            pair = DrillReportPair(layer_pair=match.group(1).strip())
            ends = re.split(r"\s+to\s+", pair.layer_pair, maxsplit=1)
            if len(ends) == 2:
                pair.from_layer, pair.to_layer = ends[0].strip(), ends[1].strip()
            result.pairs.append(pair)
            hole_list = None
            continue
        match = re.match(r"(?i)^((?:ASCII|EIA)(?:\s+\w+)?)\s+File\s*:\s*(.*)$", line)
        if match:
            pair = pair or _new_pair(result)
            pair.files[match.group(1)] = match.group(2).strip()
            continue
        match = _DRR_TOOL.match(line)
        if match:
            pair = pair or _new_pair(result)
            travel = match.group("travel")
            size_mil, step = _finer((match.group("size"), match.group("unit")),
                                    (match.group("alt"), match.group("aunit")))
            pair.tools.append(DrillReportTool(
                tool=f"T{match.group('tool')}",
                size_mil=size_mil, size_step_mil=step,
                hole_type=match.group("type").strip(), count=int(match.group("count")),
                plated={"PTH": True, "NPTH": False}.get((match.group("plated") or "").upper()),
                tolerance=(match.group("tol") or "").strip() or None,
                travel_mil=float(travel) * unit_scale(match.group("tunit")) if travel else None))
            continue
        match = re.match(r"(?i)^Totals?\s+(\d+)\s*$", line)
        if match:
            pair = pair or _new_pair(result)
            pair.total = int(match.group(1))
            continue
        match = re.match(r"(?i)^Total Processing Time\s*(?:\([^)]*\))?\s*:\s*(.*)$", line)
        if match:
            result.processing_time = match.group(1).strip()
            continue
        match = re.match(r"(?i)^(.*\bholes?\b.*?)\s*:\s*(\d+)$", line)
        if match:
            hole_list = {"label": match.group(1).strip(), "count": int(match.group(2)),
                         "points": []}
            result.hole_lists.append(hole_list)
            continue
        match = re.match(r"^(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)$", line)
        if match and hole_list is not None:
            hole_list["points"].append((float(match.group(1)), float(match.group(2))))
            continue
        if re.match(r"(?i)^Tool\s+Hole Size", line):
            continue
        result.warnings.append(f"line {number}: not read")
    for pair in result.pairs:
        if pair.total is not None and pair.total != sum(t.count for t in pair.tools):
            result.warnings.append(f"{pair.layer_pair or 'layer pair'}: tool counts do not "
                                   "add up to the total")
    return result


def _finer(*readings: tuple[str | None, str | None]) -> tuple[float, float | None]:
    """(size, step) in mil of whichever reading is written more finely: the report prints
    one unit rounded (whole mils) and the other with decimals."""
    best: tuple[float, float] | None = None
    for text, unit in readings:
        scale = unit_scale(unit)
        if not text or not scale:
            continue
        step = 10 ** -len(text.partition(".")[2]) * scale
        if best is None or step < best[0]:
            best = (step, float(text) * scale)
    return (best[1], best[0]) if best else (0.0, None)


def _new_pair(result: DrillReport) -> DrillReportPair:
    pair = DrillReportPair()
    result.pairs.append(pair)
    return pair


# ---------------------------------------------------------------- layer pairs (.LDP)

@dataclass
class DrillLayerPair:
    name: str | None               # LayersSetName
    drill_file: str | None         # DrillFile
    layers: list[str] = field(default_factory=list)  # DrillLayers as written: Gerber
    # extensions (gtl top, gN mid layer N, gpN plane N, gbl bottom) in layer-ID order, which
    # is not the stack order when mid layers are numbered out of it
    values: dict[str, str] = field(default_factory=dict)  # every key as written


@dataclass
class LayerPairs:
    board: str | None = None       # the PCB path of the first line
    pairs: list[DrillLayerPair] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def parse_layer_pairs(data: bytes) -> LayerPairs:
    lines, _encoding = decode_lines(data)
    result = LayerPairs()
    for number, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line:
            continue
        match = re.match(r"(?i)^Layer Pairs Export File for PCB\s*:\s*(.*)$", line)
        if match:
            result.board = match.group(1).strip()
            continue
        if "=" not in line:
            result.warnings.append(f"line {number}: not read")
            continue
        values = {}
        for part in line.split("|"):
            key, _, value = part.partition("=")
            values[key.strip()] = value.strip()
        layers = values.get("DrillLayers", "")
        result.pairs.append(DrillLayerPair(
            name=values.get("LayersSetName"), drill_file=values.get("DrillFile"),
            layers=[name.strip() for name in layers.split(",") if name.strip()],
            values=values))
    return result


# ---------------------------------------------------------------- detection

GERBER_EXTENSIONS = {
    "gtl", "gbl", "gto", "gbo", "gts", "gbs", "gtp", "gbp", "gko", "gpt", "gpb", "gm",
    "gbr", "gd1", "gg1",
}
_GERBER_NUMBERED = re.compile(r"^(?:g\d{1,2}|gp\d{1,2}|gm\d{1,3}|gd\d{1,2}|gg\d{1,2})$")


def detect_output_kind(name: str, head: bytes) -> str | None:
    """The kind of an output file from its name and first bytes (16 KB is plenty):
    'pick_place', 'bom', 'bom_workbook' (.xlsx: parse_bom_xlsx; .xls is not read), 'ipc356',
    'ipc2581', 'excellon', 'drill_report', 'layer_pairs', 'gerber', or None."""
    low_name = name.lower().replace("\\", "/").rsplit("/", 1)[-1]
    ext = low_name.rsplit(".", 1)[-1] if "." in low_name else ""
    if ext in ("xlsx", "xls", "xlsm"):
        return "bom_workbook"
    text = head.replace(b"\x00", b"") if head[:2] in (b"\xff\xfe", b"\xfe\xff") else head
    low = text.lower()
    if b"<ipc-2581" in low:
        return "ipc2581"
    if ext == "drr" or b"ncdrill file report" in low:
        return "drill_report"
    if ext == "ldp" or b"layer pairs export file" in low:
        return "layer_pairs"
    if re.search(rb"(?m)^\s*m48\s*$", low[:4096]):
        return "excellon"
    if re.search(rb"(?m)^p\s+(?:job|units|code|ver)\b", low[:4096]) or (
            ext == "ipc" and re.search(rb"(?m)^(?:317|327)", low)):
        return "ipc356"
    has_designator = re.search(rb"designator|refdes|ref ?des", low) is not None
    if has_designator and re.search(rb"(?:center|centre|mid|ref|pad)[ \-_]?x", low):
        return "pick_place"
    if has_designator and re.search(rb"quantity|qty", low):
        return "bom"
    if ext in GERBER_EXTENSIONS or _GERBER_NUMBERED.match(ext) or (
            b"%fs" in low[:4096] and b"%mo" in low[:4096]):
        return "gerber"
    return None
