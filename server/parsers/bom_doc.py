"""Reader for Altium ActiveBOM documents (.BomDoc).

A .BomDoc is text, one record per line (CRLF), each a '|'-separated list of KEY=VALUE
fields starting with RECORD=<kind>. Lines are written in the ANSI code page (Windows-1252
on the files seen; a file saved as UTF-8 carries a byte-order mark), so each line is
decoded on its own: UTF-8 when it is valid UTF-8, else Windows-1252. A key written as
%UTF8%KEY holds the UTF-8 copy of KEY's value and wins over it.

Record order: BOM (the document header: production quantity, currency, target and
actual unit price, the date it was saved), then the option records (GeneralOptions,
SortOrderOptions, GroupOrderOptions, VisibleOrderOptions, VisibleOrderOptions2,
CustomColumns, ColumnsAliases, FilterOptions, CheckGroupCompParamsOptions,
ManufacturerLinkOptions, FavoriteSuppliers, CheckOptions, LineNumberingOptions),
Catalog, and then one block per BOM line: a CatalogItem followed by its PartChoice
records (one per supplier part number: manufacturer, MPN, supplier, SKU, stock, price
breaks P<n>/Q<n>, lifecycle and when that supply data was cached), Solution records
(a manufacturer part ranked by the designer) and PartChoiceGroups. Every record after a
CatalogItem belongs to it until the next CatalogItem.

Values are not escaped: a description can hold '|' and a line break. A physical line that
does not start with '|RECORD=' continues the previous record (the line break is kept in
the value), and the joined text is then split into fields: a fragment that does not start
with an upper-case KEY= continues the previous value, so the fields written after a
multi-line description (USERCOMMENTS, NOTE, LINENUMBER, COMPONENTPARAMETERS...) are read.
A number that is not finite (NaN, INF) is read as missing.

Unknown record kinds and keys are kept as written (raw, extra), so nothing is lost when
a newer Altium writes more. Pure parsing, no project logic."""

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

MAX_BYTES = 64 << 20
OPTION_KINDS = (
    "GeneralOptions", "SortOrderOptions", "GroupOrderOptions", "VisibleOrderOptions",
    "VisibleOrderOptions2", "CustomColumns", "ColumnsAliases", "FilterOptions",
    "CheckGroupCompParamsOptions", "ManufacturerLinkOptions", "FavoriteSuppliers",
    "CheckOptions", "LineNumberingOptions", "Catalog",
)
ITEM_KINDS = ("CatalogItem", "PartChoice", "Solution", "PartChoiceGroups")
_KEY = re.compile(r"(?:%UTF8%)?[A-Z][A-Z0-9_]*=")
# Per price break: P<n> price, Q<n> quantity, and other short <letters><n> fields (L<n>
# and the like, kept as written).
_BREAK = re.compile(r"^([A-Z]{1,3})(\d+)$")
_OLE_EPOCH = datetime(1899, 12, 30)
_ALIAS_SPLIT = re.compile("[¦|]")  # Altium separates manufacturer aliases with a broken bar

_PART_CHOICE_KEYS = {
    "RECORD", "PARTCHOICETYPE", "SUPPLIERSOURCE", "MANUFACTURER", "MANUFACTURERALIASES",
    "MANUFACTURERPARTNO", "SUPPLIER", "SUPPLIERPARTNO", "PROVIDERNAME", "PROVIDERGUID",
    "DESCRIPTION", "RANK", "AVAILABLEQUANTITY", "BESTUNITPRICE", "CURRENCY",
    "PRODUCTPHOTOURL", "MANUFACTURERPACKAGING", "SUPPLIERPACKAGING", "LIFECYCLESTATUSID",
    "LIFECYCLESTATUS", "LASTUPDATEDAT", "LASTUPDATED", "PRICECOUNT", "PINNED", "PRIORITY",
}
_ITEM_KEYS = {
    "RECORD", "ITEMTYPE", "UNIQUEID", "DESIGNITEMID", "ITEMSOURCE", "STATUS", "DESCRIPTION",
    "USERCOMMENTS", "NOTE", "LINENUMBER", "SELECTEDPARAMETERSHASH", "COMPONENTPARAMETERS",
    "CUSTOMPARAMETERS",
}
_SOLUTION_KEYS = {"RECORD", "MANUFACTURER", "MANUFACTURERPARTNO", "RANK"}


class BomDocError(ValueError):
    """The file is not a readable .BomDoc."""


@dataclass
class Record:
    kind: str
    fields: dict[str, str]
    line: int  # 1-based line of the record in the file


@dataclass
class PriceBreak:
    quantity: float
    price: float
    # The break's other fields by letter (L for L<n>, ...), as written; not interpreted.
    attributes: dict[str, str] = field(default_factory=dict)


@dataclass
class PartChoice:
    """One supplier part (PartChoice record) of a CatalogItem."""
    manufacturer: str = ""
    mpn: str = ""
    supplier: str = ""
    sku: str = ""                    # SUPPLIERPARTNO
    description: str = ""
    rank: int | None = None
    choice_type: str = ""            # PARTCHOICETYPE (AUTO_PCL: from the part choice list)
    supplier_source: str = ""
    provider_name: str = ""
    provider_guid: str = ""
    # AVAILABLEQUANTITY when cached (negative: unknown; None without a supplier)
    stock: int | None = None
    best_unit_price: float | None = None
    currency: str = ""
    lifecycle_id: str = ""           # LIFECYCLESTATUSID (Production, NRND, EOL, ...)
    lifecycle: str = ""              # LIFECYCLESTATUS (its long name)
    last_updated: datetime | None = None  # LASTUPDATEDAT, an OLE date
    last_updated_text: str = ""      # LASTUPDATED ('Updated 3 days ago' when saved)
    price_breaks: list[PriceBreak] = field(default_factory=list)
    pinned: bool = False
    priority: int | None = None
    aliases: list[str] = field(default_factory=list)  # MANUFACTURERALIASES
    manufacturer_packaging: str = ""
    supplier_packaging: str = ""
    photo_url: str = ""
    extra: dict[str, str] = field(default_factory=dict)  # keys not mapped above
    line: int = 0

    @property
    def has_supplier(self) -> bool:
        """Whether the choice is a supplier's offer (supplier or SKU): one without is a
        manufacturer part only and caches no stock or price."""
        return bool(self.supplier or self.sku)


@dataclass
class Solution:
    """A Solution record: a manufacturer part the designer ranked."""
    manufacturer: str = ""
    mpn: str = ""
    rank: int | None = None
    extra: dict[str, str] = field(default_factory=dict)
    line: int = 0


@dataclass
class CatalogItem:
    """One BOM line (CatalogItem record) with the records that follow it."""
    line_number: int | None = None
    line_number_text: str = ""
    design_item_id: str = ""
    item_source: str = ""            # ITEMSOURCE: the library or server it came from
    item_type: str = ""              # ITEMTYPE (Automatic-Vault, Automatic-NonVault, ...)
    unique_id: str = ""              # UNIQUEID: '<source or vault GUID>\<item>'
    description: str = ""
    note: str = ""
    user_comments: str = ""
    status: str = ""                 # STATUS: the BOM check status (Unknown, ...)
    component_parameters: dict[str, str] = field(default_factory=dict)
    component_parameters_text: str = ""
    selected_parameters_hash: str = ""
    custom_parameters: dict[str, str] = field(default_factory=dict)
    custom_parameters_text: str = ""
    part_choices: list[PartChoice] = field(default_factory=list)
    solutions: list[Solution] = field(default_factory=list)
    groups: list[dict[str, str]] = field(default_factory=list)  # PartChoiceGroups fields
    other_records: list[Record] = field(default_factory=list)   # unknown kinds in the block
    extra: dict[str, str] = field(default_factory=dict)
    line: int = 0

    @property
    def automatic(self) -> bool:
        """Whether ActiveBOM made the line from design components (ITEMTYPE
        'Automatic-...'); other lines were added in the BomDoc (BOM items)."""
        return self.item_type.lower().startswith("automatic") or not self.item_type


@dataclass
class BomDoc:
    path: str = ""
    encoding: str = "ascii"          # ascii, utf-8, cp1252 or mixed
    header: dict[str, str] = field(default_factory=dict)          # the BOM record
    options: dict[str, dict[str, str]] = field(default_factory=dict)  # option kind -> fields
    items: list[CatalogItem] = field(default_factory=list)
    other_records: list[Record] = field(default_factory=list)   # unknown kinds outside items
    record_counts: dict[str, int] = field(default_factory=dict)
    continuation_lines: int = 0      # lines that continued a value (a line break in it)
    warnings: list[str] = field(default_factory=list)

    @property
    def production_quantity(self) -> int | None:
        return to_int(self.header.get("PRODUCTIONQUANTITY"))

    @property
    def currency(self) -> str:
        return self.header.get("CURRENCY", "")


def to_int(text: str | None) -> int | None:
    if text is None:
        return None
    text = text.strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        number = to_float(text)
        return int(number) if number is not None and number.is_integer() else None


def to_float(text: str | None) -> float | None:
    """A number as Delphi writes it ('.' decimal mark; a ',' mark read too); None when
    empty, not a number or not finite (Delphi writes NAN and INF)."""
    if text is None:
        return None
    text = text.strip()
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        try:
            number = float(text.replace(",", "."))
        except ValueError:
            return None
    return number if math.isfinite(number) else None


def to_bool(text: str | None) -> bool:
    return (text or "").strip().lower() in ("true", "t", "1", "yes")


def ole_date(text: str | None) -> datetime | None:
    """An OLE Automation date (days since 1899-12-30, as Delphi's TDateTime) as a naive
    datetime; None when empty, zero or not a number."""
    value = to_float(text)
    if value is None or value <= 0 or value > 2_958_465:  # 9999-12-31
        return None
    return _OLE_EPOCH + timedelta(days=value)


def comma_text(text: str) -> dict[str, str]:
    """Name=Value pairs of a Delphi CommaText list ('A=1,"B=two words",C=3'): items are
    separated by commas or blanks, an item with blanks or commas is double-quoted with
    '""' for a quote. Items without '=' are kept with an empty value."""
    items: list[str] = []
    i, size = 0, len(text)
    while i < size:
        char = text[i]
        if char in ", \t":
            i += 1
            continue
        if char == '"':
            i += 1
            chunk = []
            while i < size:
                if text[i] == '"':
                    if i + 1 < size and text[i + 1] == '"':
                        chunk.append('"')
                        i += 2
                        continue
                    i += 1
                    break
                chunk.append(text[i])
                i += 1
            # A quoted run may continue up to the next separator ('a"b c"' is rare).
            while i < size and text[i] not in ", \t":
                chunk.append(text[i])
                i += 1
            items.append("".join(chunk))
            continue
        start = i
        while i < size and text[i] not in ", \t":
            i += 1
        items.append(text[start:i])
    pairs: dict[str, str] = {}
    for item in items:
        name, _, value = item.partition("=")
        if name:
            pairs.setdefault(name, value)
    return pairs


def _decode(raw: bytes) -> tuple[str, str]:
    if raw.isascii():
        return raw.decode("ascii"), "ascii"
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace"), "cp1252"


def split_fields(text: str) -> dict[str, str]:
    """The KEY=VALUE fields of one record line ('|RECORD=x|A=1|...'). A fragment that
    does not start with KEY= belongs to the previous value (a '|' inside it); a
    %UTF8%KEY value replaces KEY's."""
    fields: dict[str, str] = {}
    last: str | None = None
    for fragment in text.split("|")[1:] if text.startswith("|") else text.split("|"):
        match = _KEY.match(fragment)
        if match is None:
            if last is not None:
                fields[last] += "|" + fragment
            continue
        key = fragment[: match.end() - 1]
        value = fragment[match.end():]
        if key in fields:  # a repeated key: keep the first, as Altium reads it
            last = None
            continue
        fields[key] = value
        last = key
    for key in [key for key in fields if key.startswith("%UTF8%")]:
        fields[key[6:]] = fields.pop(key)
    return fields


def _stock(text: str | None) -> int | None:
    """AVAILABLEQUANTITY: Altium writes -1 (and other negatives) when the supplier gave
    no stock figure."""
    value = to_int(text)
    return value if value is not None and value >= 0 else None


def _part_choice(fields: dict[str, str], line: int) -> PartChoice:
    breaks: dict[int, dict[str, tuple[str, str]]] = {}
    extra: dict[str, str] = {}
    for key, value in fields.items():
        match = _BREAK.match(key) if key not in _PART_CHOICE_KEYS else None
        if match:
            breaks.setdefault(int(match[2]), {})[match[1]] = (key, value)
        elif key not in _PART_CHOICE_KEYS:
            extra[key] = value
    price_breaks = []
    for _index, entry in sorted(breaks.items()):
        quantity = to_float(entry["Q"][1]) if "Q" in entry else None
        price = to_float(entry["P"][1]) if "P" in entry else None
        if quantity is None or price is None:
            # Not a price break (or an unreadable one): its fields are kept as written.
            extra.update(dict(entry.values()))
            continue
        price_breaks.append(PriceBreak(quantity, price, {
            letter: value for letter, (_key, value) in entry.items()
            if letter not in ("P", "Q")}))
    # Some choices hold several price tables back to back (Q restarts at 1): sorted by
    # quantity, price_at takes the lowest price usable at a need.
    price_breaks.sort(key=lambda entry: (entry.quantity, entry.price))
    supplier = fields.get("SUPPLIER", "").strip()
    sku = fields.get("SUPPLIERPARTNO", "").strip()
    aliases = [alias.strip() for alias in _ALIAS_SPLIT.split(fields.get("MANUFACTURERALIASES", ""))
               if alias.strip()]
    return PartChoice(
        manufacturer=fields.get("MANUFACTURER", "").strip(),
        mpn=fields.get("MANUFACTURERPARTNO", "").strip(),
        supplier=supplier,
        sku=sku,
        description=fields.get("DESCRIPTION", ""),
        rank=to_int(fields.get("RANK")),
        choice_type=fields.get("PARTCHOICETYPE", ""),
        supplier_source=fields.get("SUPPLIERSOURCE", ""),
        provider_name=fields.get("PROVIDERNAME", ""),
        provider_guid=fields.get("PROVIDERGUID", ""),
        # A choice with no supplier (a manufacturer part only) caches no stock: its
        # AVAILABLEQUANTITY=0 is a default.
        stock=_stock(fields.get("AVAILABLEQUANTITY")) if supplier or sku else None,
        best_unit_price=to_float(fields.get("BESTUNITPRICE")),
        currency=fields.get("CURRENCY", "").strip(),
        lifecycle_id=fields.get("LIFECYCLESTATUSID", "").strip(),
        lifecycle=fields.get("LIFECYCLESTATUS", "").strip(),
        last_updated=ole_date(fields.get("LASTUPDATEDAT")),
        last_updated_text=fields.get("LASTUPDATED", ""),
        price_breaks=price_breaks,
        pinned=to_bool(fields.get("PINNED")),
        priority=to_int(fields.get("PRIORITY")),
        aliases=aliases,
        manufacturer_packaging=fields.get("MANUFACTURERPACKAGING", ""),
        supplier_packaging=fields.get("SUPPLIERPACKAGING", ""),
        photo_url=fields.get("PRODUCTPHOTOURL", ""),
        extra=extra,
        line=line,
    )


def _catalog_item(fields: dict[str, str], line: int) -> CatalogItem:
    number_text = fields.get("LINENUMBER", "")
    return CatalogItem(
        line_number=to_int(number_text),
        line_number_text=number_text,
        design_item_id=fields.get("DESIGNITEMID", ""),
        item_source=fields.get("ITEMSOURCE", ""),
        item_type=fields.get("ITEMTYPE", ""),
        unique_id=fields.get("UNIQUEID", ""),
        description=fields.get("DESCRIPTION", ""),
        note=fields.get("NOTE", ""),
        user_comments=fields.get("USERCOMMENTS", ""),
        status=fields.get("STATUS", ""),
        component_parameters=comma_text(fields.get("COMPONENTPARAMETERS", "")),
        component_parameters_text=fields.get("COMPONENTPARAMETERS", ""),
        selected_parameters_hash=fields.get("SELECTEDPARAMETERSHASH", ""),
        custom_parameters=comma_text(fields.get("CUSTOMPARAMETERS", "")),
        custom_parameters_text=fields.get("CUSTOMPARAMETERS", ""),
        extra={key: value for key, value in fields.items() if key not in _ITEM_KEYS},
        line=line,
    )


def parse_bom_doc_bytes(data: bytes, path: str = "") -> BomDoc:
    """A BomDoc from its bytes. Raises BomDocError when no BOM record starts it."""
    if data[:3] == b"\xef\xbb\xbf":
        data = data[3:]
    doc = BomDoc(path=path)
    used: set[str] = set()
    # Each record's text with the lines that continue it (a line break inside a value),
    # split into fields once it is whole.
    texts: list[tuple[list[str], int]] = []
    blank = 0  # empty lines since the last text: kept only inside a continued value
    for number, raw in enumerate(data.split(b"\n"), start=1):
        raw = raw.rstrip(b"\r")
        if not raw:
            blank += 1
            continue
        text, encoding = _decode(raw)
        if encoding != "ascii":
            used.add(encoding)
        if not text.startswith("|RECORD=") and not text.startswith("RECORD="):
            if texts:
                texts[-1][0].append("\n" * (blank + 1) + text)
                doc.continuation_lines += 1
            blank = 0
            continue
        blank = 0
        texts.append(([text], number))
    records: list[Record] = []
    for chunks, number in texts:
        fields = split_fields("".join(chunks))
        records.append(Record(fields.get("RECORD", ""), fields, number))
    doc.encoding = "ascii" if not used else (used.pop() if len(used) == 1 else "mixed")
    if not records or records[0].kind != "BOM":
        raise BomDocError("not an ActiveBOM document: it does not start with a BOM record")
    counts: dict[str, int] = {}
    current: CatalogItem | None = None
    for record in records:
        counts[record.kind] = counts.get(record.kind, 0) + 1
        kind = record.kind
        if kind == "BOM" and not doc.header:
            doc.header = {k: v for k, v in record.fields.items() if k != "RECORD"}
        elif kind == "CatalogItem":
            current = _catalog_item(record.fields, record.line)
            doc.items.append(current)
        elif current is not None and kind == "PartChoice":
            current.part_choices.append(_part_choice(record.fields, record.line))
        elif current is not None and kind == "Solution":
            current.solutions.append(Solution(
                manufacturer=record.fields.get("MANUFACTURER", "").strip(),
                mpn=record.fields.get("MANUFACTURERPARTNO", "").strip(),
                rank=to_int(record.fields.get("RANK")),
                extra={k: v for k, v in record.fields.items() if k not in _SOLUTION_KEYS},
                line=record.line))
        elif current is not None and kind == "PartChoiceGroups":
            current.groups.append({k: v for k, v in record.fields.items() if k != "RECORD"})
        elif current is None and kind in OPTION_KINDS and kind not in doc.options:
            doc.options[kind] = {k: v for k, v in record.fields.items() if k != "RECORD"}
        elif current is not None:
            current.other_records.append(record)
        else:
            doc.other_records.append(record)
    doc.record_counts = counts
    if "Catalog" not in doc.options:
        doc.warnings.append("no Catalog record: the file may be cut short")
    return doc


def parse_bom_doc(path: str | Path) -> BomDoc:
    """Parse a .BomDoc file. Raises OSError when it cannot be read and BomDocError when
    it is not a BomDoc (or larger than MAX_BYTES)."""
    path = Path(path)
    if path.stat().st_size > MAX_BYTES:
        raise BomDocError(f"larger than {MAX_BYTES >> 20} MB")
    return parse_bom_doc_bytes(path.read_bytes(), str(path))


def split_list(text: str, separator: str = "!") -> list[tuple[str, str]]:
    """(name, value) pairs of an option list such as VisibleOrderOptions2's
    'Line #=68!Comment=120' or ColumnsAliases' 'Comment=Name!...'."""
    pairs = []
    for item in text.split(separator):
        if not item.strip():
            continue
        name, _, value = item.partition("=")
        pairs.append((name, value))
    return pairs


def utc_iso(moment: datetime | None) -> str | None:
    """A cache date as ISO text (the file stores local time without a zone: shown
    without one, to the minute)."""
    if moment is None:
        return None
    return moment.replace(tzinfo=None, microsecond=0).isoformat(timespec="minutes")


def age_days(moment: datetime | None, now: datetime | None = None) -> int | None:
    if moment is None:
        return None
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    return max(0, (now - moment).days)
