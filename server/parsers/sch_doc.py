import logging
import re
import struct
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field

import olefile

_MPN_RE = re.compile(r"^Manufacturer Part Number( (\d+))?$", re.IGNORECASE)


@dataclass
class ComponentMeta:
    refdes: str = ""
    description: str | None = None
    mpn: str | None = None
    value: str | None = None
    unique_id: str = ""
    pins: dict[str, str] = field(default_factory=dict)  # pin_number -> pin_name
    # Every part's UniqueID: parts placed separately keep their own, and the PCB
    # carries only one of them (SOURCEUNIQUEID).
    unique_ids: list[str] = field(default_factory=list)
    # The current PCB footprint (RECORD=45 PCBLIB model with IsCurrent) and every
    # footprint model the part offers, in file order.
    footprint: str | None = None
    footprints: list[str] = field(default_factory=list)
    # ComponentKind: 0 Standard, 1 Mechanical, 2 Graphical, 3 Net Tie (In BOM),
    # 4 Net Tie, 5 Standard (No BOM), 6 Jumper.
    kind: int = 0
    # Every pin designator, unnamed pins included (pins drops those).
    pin_designators: list[str] = field(default_factory=list)
    # Every parameter as written (lower-case name -> text; the first part's wins), for
    # the BOM. mpn above falls back to Comment; params does not.
    params: dict[str, str] = field(default_factory=dict)
    lib_reference: str = ""
    design_item_id: str = ""
    # Inside a compile mask (RECORD=211) on its sheet: Altium leaves it out of the
    # compiled design. A multi-part component is masked when all its parts are.
    compile_masked: bool = False
    # UniqueIDs of compile-masked copies with this designator that this live part
    # replaced: they are not this part's (a variation on one names no compiled part).
    masked_unique_ids: list[str] = field(default_factory=list)
    # Every pin's electrical type (pin designator -> PIN_TYPES name), unnamed pins
    # included; each pin of a multi-part component keeps its own record's type.
    pin_types: dict[str, str] = field(default_factory=dict)
    # Where the part was placed from (RECORD=1): SourceLibraryName (a library file name
    # such as X.SchLib or X.IntLib, or a Workspace name), LibraryPath ("*" or empty
    # unless a path was kept), a Workspace item (VaultGUID or ItemGUID set) and the
    # DbLib table of a database part (DatabaseTableName).
    library: str = ""
    library_path: str = ""
    workspace_part: bool = False
    database_table: str = ""
    # A database part's library (DatabaseLibraryName, when the sheet stores it) and
    # whether its record is looked up in DatabaseTableName only (UseDBTableName=T).
    database_library: str = ""
    use_db_table_name: bool = False
    # Parameters on the sheet whose value is empty (lower-case names; params leaves them
    # out): the database check counts them as present.
    blank_params: list[str] = field(default_factory=list)
    # The current footprint model's library as its PCB model link names it
    # (ModelDataFile0: a library file name or path; empty for "Any"), and whether the
    # link takes it from the component's own library (UseComponentLibrary=T).
    footprint_library: str = ""
    footprint_from_component_library: bool = False


# A pin record's Electrical property, as Altium's pin dialog lists it; a pin without the
# property is an Input. Checked on the AD25 examples: supply pins are Power, resistor
# and capacitor pins Passive, transceiver TX pins Output, alert, interrupt and power-good
# pins of supervisors and regulators OpenCollector, FPGA and MCU I/O pins IO.
PIN_TYPES = {
    "0": "Input", "1": "IO", "2": "Output", "3": "OpenCollector", "4": "Passive",
    "5": "HiZ", "6": "OpenEmitter", "7": "Power",
}


def pin_type(props: dict[str, str]) -> str:
    """A RECORD=2 pin's electrical type name; an unknown value reads as written."""
    value = props.get("electrical", "").strip() or "0"
    return PIN_TYPES.get(value, value)


@dataclass
class DesignatorClash:
    """Two or more different components drawn with one designator on a sheet."""
    refdes: str
    # Each component's LibReference in file order; the last one is the one kept.
    lib_references: list[str] = field(default_factory=list)


def parse_sch_doc(
    path: str, clashes: list[DesignatorClash] | None = None
) -> dict[str, ComponentMeta]:
    """Parse an Altium .SchDoc OLE file and return component metadata keyed by refdes.

    Designators that name two or more different components are appended to clashes.
    """
    with olefile.OleFileIO(path) as f:
        return components_from_stream(
            f.openstream("FileHeader").read(), source=path, clashes=clashes
        )


def components_from_stream(
    data: bytes, source: str = "", clashes: list[DesignatorClash] | None = None
) -> dict[str, ComponentMeta]:
    """parse_sch_doc for the bytes of a FileHeader stream.

    The OwnerIndex field in child records (pins, designators, parameters) refers to
    the 0-based sequential record number of the parent RECORD=1 minus 1, i.e. the
    record immediately preceding the component record in the file stream.

    source names the sheet in logged warnings. Designators that name two or more
    different components are appended to clashes, see _merge_parts.
    """
    return _components_from_records(read_records(data), source, clashes)


def _components_from_records(
    records: list[dict[str, str]], source: str = "",
    clashes: list[DesignatorClash] | None = None,
) -> dict[str, ComponentMeta]:
    """components_from_stream for the records read_records gives (stripped values)."""
    # Parsing state
    # Key: owner_key = record_num - 1 for the RECORD=1 entry
    components_by_owner: dict[int, ComponentMeta] = {}
    # Map owner_key of duplicate parts (same UniqueID) to the canonical owner_key
    canonical_owner: dict[int, int] = {}   # any owner_key -> canonical owner_key
    uid_seen: dict[str, int] = {}          # UniqueID -> canonical owner_key
    best_mpn_priority: dict[int, int] = {} # canonical owner_key -> lowest suffix seen (0 = best)
    parts: dict[int, _Part] = {}           # canonical owner_key -> which part it is
    model_lists: dict[int, int] = {}       # RECORD=44 owner_key -> its component's owner_key
    origins: dict[int, list[tuple[float, float]]] = {}  # canonical owner_key -> part origins
    masks: list[list[float]] = []          # compile mask rectangles

    for record_num, props in enumerate(records):
        record_type = props.get("record")
        # owner_key for a RECORD=1 at record_num is (record_num - 1),
        # matching the OwnerIndex used by child records.
        if record_type == "1":
            _handle_component(
                props, record_num, components_by_owner, canonical_owner, uid_seen, parts
            )
            origins.setdefault(canonical_owner[record_num - 1], []).append(
                (coord_mil(props, "location.x"), coord_mil(props, "location.y"))
            )
        elif record_type == "211" and props.get("collapsed", "").upper() != "T":
            masks.append(_rect(props))  # a collapsed compile mask is disabled
        elif record_type == "34":
            _handle_designator(props, components_by_owner, canonical_owner)
        elif record_type == "2":
            _handle_pin(props, components_by_owner, canonical_owner)
        elif record_type == "41":
            _handle_parameter(
                props, components_by_owner, canonical_owner, best_mpn_priority
            )
        elif record_type == "44":
            # The component's implementation list; its models (RECORD=45) own to it.
            key = _owner_key(props, canonical_owner)
            if key is not None:
                model_lists[record_num - 1] = key
        elif record_type == "45":
            _handle_model(props, components_by_owner, model_lists)
        # RECORD=17 (power ports) intentionally skipped

    for key, meta in components_by_owner.items():
        meta.compile_masked = bool(masks) and all(
            any(x0 <= x <= x1 and y0 <= y <= y1 for x0, y0, x1, y1 in masks)
            for x, y in origins.get(key) or [(float("nan"), float("nan"))]
        )
    return _merge_parts(components_by_owner, parts, best_mpn_priority, source, clashes)


@dataclass
class _Part:
    lib_reference: str  # LibReference, the library symbol it was placed from
    part_id: str        # CurrentPartId: 1 = part A, 2 = part B, ...
    part_count: str     # PartCount: number of parts + 1


def _part_number(part_id: str) -> int:
    return int(part_id) if part_id.isdigit() else 0


class _Component:
    """A component being assembled from its parts, see _merge_parts."""

    def __init__(self, meta: ComponentMeta, part: _Part, mpn_priority: int):
        self.meta = meta
        self.first = part
        self.part_ids = {part.part_id}
        self.top_part = _part_number(part.part_id)  # the part whose UniqueID meta carries
        self.mpn_priority = mpn_priority

    def can_take(self, part: _Part) -> bool:
        return part.part_id not in self.part_ids and part.part_count == self.first.part_count

    def add(self, meta: ComponentMeta, part: _Part, mpn_priority: int) -> None:
        self.part_ids.add(part.part_id)
        for pin, name in meta.pins.items():
            self.meta.pins.setdefault(pin, name)
        for pin, kind in meta.pin_types.items():
            self.meta.pin_types.setdefault(pin, kind)
        _merge_part_lists(self.meta, meta)
        self.meta.description = self.meta.description or meta.description
        self.meta.value = self.meta.value or meta.value
        if meta.mpn and (not self.meta.mpn or mpn_priority < self.mpn_priority):
            self.meta.mpn, self.mpn_priority = meta.mpn, mpn_priority
        if _part_number(part.part_id) > self.top_part:
            self.meta.unique_id, self.top_part = meta.unique_id, _part_number(part.part_id)


def _merge_part_lists(target: ComponentMeta, other: ComponentMeta) -> None:
    """Unite the identity and model lists of two parts of one component."""
    for name in ("unique_ids", "footprints", "pin_designators"):
        held = getattr(target, name)
        held.extend(item for item in getattr(other, name) if item not in held)
    if not target.footprint and other.footprint:
        target.footprint = other.footprint
        target.footprint_library = other.footprint_library
        target.footprint_from_component_library = other.footprint_from_component_library
    target.kind = target.kind or other.kind
    for name, text in other.params.items():
        target.params.setdefault(name, text)
    for name in ("lib_reference", "design_item_id", "library", "library_path",
                 "database_table", "database_library"):
        setattr(target, name, getattr(target, name) or getattr(other, name))
    target.workspace_part = target.workspace_part or other.workspace_part
    target.use_db_table_name = target.use_db_table_name or other.use_db_table_name
    for name in other.blank_params:
        if name not in target.blank_params:
            target.blank_params.append(name)


def _merge_parts(
    components_by_owner: dict[int, ComponentMeta],
    parts: dict[int, _Part],
    best_mpn_priority: dict[int, int],
    source: str,
    clashes: list[DesignatorClash] | None = None,
) -> dict[str, ComponentMeta]:
    """One ComponentMeta per designator, the parts of a multi-part component merged.

    Each part of a multi-part component is its own record. In older sheets the parts
    share a UniqueID and are merged while reading; in newer ones (most of Altium's AD25
    examples) each part has its own, and only the designator ties them together.
    Parts with the same designator, different part ids and the same part count merge:
    pins are united, the first non-empty description and value and the best MPN are
    kept, and unique_id is the highest part's, the one Altium gives the component on
    the PCB (SOURCEUNIQUEID). A difference in LibReference is logged but merged, since
    parts of one component can be placed from different libraries. A part that cannot
    belong to the component holding its designator (a part id seen before, or another
    part count) is another component with the same designator: it replaces the earlier
    one, as before, and the clash is logged and appended to clashes, one entry per
    designator. A compile-masked copy is not compiled, so it never replaces a live
    component and is never a clash; a live one replaces a masked one. Unannotated parts
    (U?) share a designator and are neither merged nor logged.
    """
    components: dict[str, _Component] = {}
    clashed: dict[str, DesignatorClash] = {}
    for key, meta in components_by_owner.items():
        refdes, part = meta.refdes, parts[key]
        if not refdes:
            continue
        held = components.get(refdes)
        if held and "?" not in refdes:
            if held.meta.compile_masked != meta.compile_masked:
                live, masked = (held.meta, meta) if meta.compile_masked else (meta, held.meta)
                live.masked_unique_ids += [*masked.unique_ids, *masked.masked_unique_ids]
                if not meta.compile_masked:
                    components[refdes] = _Component(meta, part, best_mpn_priority.get(key, 999))
                continue
            if held.can_take(part):
                if part.lib_reference != held.first.lib_reference:
                    logging.warning(
                        "%s: the parts of %s come from different library references "
                        "(%s and %s); merged as one component", source or "SchDoc", refdes,
                        held.first.lib_reference, part.lib_reference,
                    )
                held.add(meta, part, best_mpn_priority.get(key, 999))
                continue
            if meta.compile_masked:  # two masked copies: neither is compiled
                components[refdes] = _Component(meta, part, best_mpn_priority.get(key, 999))
                continue
            logging.warning(
                "%s: %s is the designator of two different components (%s and %s); "
                "using the later one", source or "SchDoc", refdes,
                held.first.lib_reference, part.lib_reference,
            )
            if refdes not in clashed:
                clashed[refdes] = DesignatorClash(refdes, [held.first.lib_reference])
                if clashes is not None:
                    clashes.append(clashed[refdes])
            clashed[refdes].lib_references.append(part.lib_reference)
        components[refdes] = _Component(meta, part, best_mpn_priority.get(key, 999))
    return {refdes: held.meta for refdes, held in components.items()}


_C1_RE = re.compile("[\x80-\x9f]")


def _ansi(value: str) -> str:
    """A value Altium wrote in the ANSI code page (no %UTF8% copy), read as Windows-1252
    instead of the latin-1 the record was decoded with: bytes 0x80-0x9F are then the
    typographic characters they stand for (dashes, quotes), not C1 control codes."""
    if not _C1_RE.search(value):
        return value
    out = []
    for char in value:
        if "\x80" <= char <= "\x9f":
            # 0x81, 0x8D, 0x8F, 0x90 and 0x9D have no cp1252 character: kept as read.
            char = bytes([ord(char)]).decode("cp1252", errors="ignore") or char
        out.append(char)
    return "".join(out)


def _parse_props(text: str, strip: bool = True) -> dict[str, str]:
    """`|KEY=VALUE|...` as a dict with lower-case keys.

    Altium writes the same key as OwnerIndex in some sheets and OWNERINDEX in others
    (most of its AD25 examples), so keys are matched case-insensitively. strip False
    keeps each value as written (leading and trailing whitespace included) and reads
    values without a %UTF8% copy as Windows-1252, see _ansi.
    """
    props: dict[str, str] = {}
    for part in text.split("|"):
        if "=" in part:
            key, _, val = part.partition("=")
            key = key.strip().lower()
            if strip:
                val = val.strip()
            if key.startswith("%utf8%"):
                # The record was decoded as latin-1, so %UTF8% values are
                # latin-1 bytes that are actually UTF-8. Re-encode→decode to
                # get the correct Unicode (e.g. "ÂµF" → "µF").
                try:
                    val = val.encode("latin-1").decode("utf-8")
                except (UnicodeEncodeError, UnicodeDecodeError):
                    pass
                props[key[6:]] = val
            elif key not in props:
                props[key] = val if strip else _ansi(val)
    return props


def read_records(
    data: bytes, strip: bool = True,
    binary: Callable[[bytes], dict[str, str] | None] | None = None,
) -> list[dict[str, str]]:
    """Split a schematic stream into property dicts with lower-case keys.

    Each record is a 4-byte header (24-bit length, then a type byte, 0 = text) and a
    `|KEY=VALUE|...` body. Binary records become empty dicts so that list positions,
    which OwnerIndex refers to, stay aligned; binary, when given, reads a binary
    record's body into properties instead (None keeps the empty dict). Position 0 is
    the stream header, and a child's OwnerIndex is its owner's position minus one.
    strip: see _parse_props.
    """
    records: list[dict[str, str]] = []
    offset = 0
    while offset + 4 <= len(data):
        header = struct.unpack_from("<I", data, offset)[0]
        length = header & 0xFFFFFF
        body = data[offset + 4 : offset + 4 + length]
        offset += 4 + length
        if header >> 24:
            records.append((binary(body) if binary else None) or {})
            continue
        text = body.decode("latin-1").rstrip("\x00")
        records.append(_parse_props(text, strip))
    return records


def _owner_key(props: dict[str, str], canonical_owner: dict[int, int]) -> int | None:
    """The canonical owner_key a child record belongs to, or None if it has no owner."""
    try:
        owner = int(props.get("ownerindex", ""))
    except ValueError:
        return None
    return canonical_owner.get(owner, owner)


def _handle_component(
    props: dict[str, str],
    record_num: int,
    components_by_owner: dict[int, ComponentMeta],
    canonical_owner: dict[int, int],
    uid_seen: dict[str, int],
    parts: dict[int, _Part],
) -> None:
    # The owner_key used by child records equals record_num - 1
    owner_key = record_num - 1
    uid = props.get("uniqueid", "")
    desc = props.get("componentdescription") or None

    if uid and uid in uid_seen:
        # Subsequent part of a multi-part component — map to canonical owner_key
        canonical_owner[owner_key] = uid_seen[uid]
    else:
        comp = ComponentMeta(
            description=desc,
            unique_id=uid,
            unique_ids=[uid] if uid else [],
            kind=_component_kind(props),
            lib_reference=props.get("libreference", ""),
            design_item_id=props.get("designitemid", ""),
            library=props.get("sourcelibraryname", ""),
            library_path=props.get("librarypath", ""),
            workspace_part=bool(props.get("vaultguid") or props.get("itemguid")),
            database_table=props.get("databasetablename", ""),
            database_library=props.get("databaselibraryname", ""),
            use_db_table_name=props.get("usedbtablename", "").strip().upper()[:1] == "T",
        )
        components_by_owner[owner_key] = comp
        canonical_owner[owner_key] = owner_key
        parts[owner_key] = _Part(
            lib_reference=props.get("libreference", ""),
            part_id=props.get("currentpartid", ""),
            part_count=props.get("partcount", ""),
        )
        if uid:
            uid_seen[uid] = owner_key


def _handle_designator(
    props: dict[str, str],
    components_by_owner: dict[int, ComponentMeta],
    canonical_owner: dict[int, int],
) -> None:
    if props.get("name", "").lower() != "designator":
        return
    key = _owner_key(props, canonical_owner)
    text = props.get("text", "")
    if key is None or not text:
        return
    if key in components_by_owner:
        components_by_owner[key].refdes = text


def _handle_pin(
    props: dict[str, str],
    components_by_owner: dict[int, ComponentMeta],
    canonical_owner: dict[int, int],
) -> None:
    key = _owner_key(props, canonical_owner)
    name = props.get("name", "")
    designator = props.get("designator", "")
    if key is None or not designator or key not in components_by_owner:
        return
    component = components_by_owner[key]
    if designator not in component.pin_designators:
        component.pin_designators.append(designator)
    # A pin drawn in several display modes keeps the normal mode's type.
    if designator not in component.pin_types or props.get("ownerpartdisplaymode", "0") == "0":
        component.pin_types[designator] = pin_type(props)
    if not name or name == "?":
        return
    component.pins[designator] = name


def _component_kind(props: dict[str, str]) -> int:
    """ComponentKind as Altium reads it back: ComponentKindVersion3=6 is a Jumper,
    ComponentKindVersion2 of 5 or more (Standard (No BOM)) wins over ComponentKind."""
    def number(key: str) -> int:
        value = props.get(key, "").strip()
        return int(value) if value.isdigit() else 0

    if number("componentkindversion3") == 6:
        return 6
    if number("componentkindversion2") >= 5:
        return number("componentkindversion2")
    return number("componentkind")


def _handle_model(
    props: dict[str, str],
    components_by_owner: dict[int, ComponentMeta],
    model_lists: dict[int, int],
) -> None:
    """A RECORD=45 model: PCBLIB models are the part's footprints."""
    if props.get("modeltype", "").upper() != "PCBLIB":
        return
    try:
        key = model_lists.get(int(props.get("ownerindex", "")))
    except ValueError:
        return
    name = props.get("modelname", "")
    if key is None or key not in components_by_owner or not name:
        return
    component = components_by_owner[key]
    if name not in component.footprints:
        component.footprints.append(name)
    if props.get("iscurrent", "").upper() == "T" and not component.footprint:
        component.footprint = name
        component.footprint_library = props.get("modeldatafile0", "")
        component.footprint_from_component_library = (
            props.get("usecomponentlibrary", "").upper() == "T")


def _handle_parameter(
    props: dict[str, str],
    components_by_owner: dict[int, ComponentMeta],
    canonical_owner: dict[int, int],
    best_mpn_priority: dict[int, int],
) -> None:
    key = _owner_key(props, canonical_owner)
    # Parameter names are case-insensitive in Altium ("Value" and "VALUE" both occur).
    name = props.get("name", "").lower()
    text = props.get("text", "")
    if key is not None and not text and name and key in components_by_owner:
        # On the sheet with no value (params keeps only valued ones, for the BOM).
        blank = components_by_owner[key].blank_params
        if name not in blank:
            blank.append(name)
    if key is None or not text or text.startswith('"'):
        return
    if key not in components_by_owner:
        return
    comp = components_by_owner[key]
    if name:
        comp.params.setdefault(name, text)

    if name == "value":
        comp.value = text
        return

    m = _MPN_RE.match(name)
    if m:
        # Priority: no suffix (0) beats suffix 1 beats suffix 2 etc.
        priority = int(m.group(2)) if m.group(2) else 0
        if priority < best_mpn_priority.get(key, 999):
            comp.mpn = text
            best_mpn_priority[key] = priority
        return

    # Fall back to Comment as MPN when no Manufacturer Part Number exists.
    # Comment is used as the primary part number in many Altium libraries.
    if name == "comment" and best_mpn_priority.get(key, 999) == 999:
        comp.mpn = text


# ---------- design annotations: notes, text frames, labels, images, directives ----------

# Schematic coordinates are in units of 10 mil; a *_Frac field adds 1/100000 of a unit.
_UNIT_MIL = 10.0
NEAR_COMPONENT_MIL = 800.0  # text this close to a component's pins is reported as near it
CAPTION_MIL = 250.0         # text this close to a picture is reported as its caption
_MAX_NEAR = 4

# Sheet parameters Altium fills in by itself, not information a designer entered.
_AUTO_SHEET_PARAMS = {
    "currenttime", "currentdate", "time", "date", "documentfullpathandname",
    "documentname", "modifieddate", "application_buildnumber", "spicemodelcache",
    "configurationparameters", "configuratorname", "isuserconfigurable", "pagesetup",
}
# A label holding only a special string (e.g. "=Title") repeats a title-block parameter.
_SPECIAL_STRING_RE = re.compile(r"^=[\w.]+$")
_TEXT_KINDS = {"209": "note", "28": "text_frame", "4": "label"}
# Text frames and notes escape their text: ~1 is a line break, ~2 a '|', ~~ a '~'.
_TEXT_ESCAPES = {"1": "\n", "2": "|", "~": "~"}
_TEXT_ESCAPE_RE = re.compile(r"~([12~])")
# Altium draws schematic font size N at about 0.646 N points (checked against its own
# 1:1 PDF export); an average character is about half an em wide.
_FONT_SIZE_MIL = 0.646 * 1000 / 72
_CHAR_WIDTH_EM = 0.5


@dataclass
class SheetAnnotation:
    kind: str                       # "note" | "text_frame" | "label" | "image"
    text: str = ""
    x_mil: float = 0.0              # lower-left corner; Altium's Y axis points up
    y_mil: float = 0.0
    width_mil: float = 0.0
    height_mil: float = 0.0
    author: str | None = None       # notes
    collapsed: bool = False         # notes folded to an icon on the sheet
    file_name: str | None = None    # images: embedded picture name or linked file path
    embedded: bool = False          # images: picture data is stored in the .SchDoc
    image_index: int | None = None  # images: index for get_design_image, in file order
    near: list[str] = field(default_factory=list)      # refdes of components close by
    captions: list[str] = field(default_factory=list)  # images: text placed next to them


@dataclass
class SheetDirective:
    name: str
    x_mil: float
    y_mil: float
    parameters: dict[str, str] = field(default_factory=dict)


@dataclass
class SheetAnnotations:
    items: list[SheetAnnotation] = field(default_factory=list)
    directives: list[SheetDirective] = field(default_factory=list)
    title_block: dict[str, str] = field(default_factory=dict)
    template: str | None = None
    # Every sheet-level parameter as stored, for resolving special strings ("=Revision").
    sheet_parameters: dict[str, str] = field(default_factory=dict)


def coord_mil(props: dict[str, str], key: str) -> float:
    try:
        whole = int(props.get(key, "0"))
        frac = int(props.get(key + "_frac", "0"))
    except ValueError:
        return 0.0
    return (whole + frac / 100_000) * _UNIT_MIL


def _rect(props: dict[str, str]) -> list[float]:
    """[x0, y0, x1, y1] in mil from Location and Corner."""
    x0, y0 = coord_mil(props, "location.x"), coord_mil(props, "location.y")
    x1, y1 = coord_mil(props, "corner.x"), coord_mil(props, "corner.y")
    return [min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)]


def _label_rect(props: dict[str, str], text: str, font_sizes: dict[str, int]) -> list[float]:
    """Approximate extent of a free text label; Altium stores only its anchor point.

    The box is sized from the font size and placed by Justification (0..8 = bottom/
    center/top x left/center/right) in the text's own frame, then turned about the
    anchor by Orientation x 90 degrees counter-clockwise.
    """
    x, y = coord_mil(props, "location.x"), coord_mil(props, "location.y")
    em = font_sizes.get(props.get("fontid", "1"), 10) * _FONT_SIZE_MIL
    lines = text.split("\n")
    width, height = max(len(line) for line in lines) * em * _CHAR_WIDTH_EM, em * len(lines)
    try:
        justification = int(props.get("justification", "0"))
        turns = int(props.get("orientation", "0")) % 4
    except ValueError:
        justification, turns = 0, 0
    x0, y0 = -width * (justification % 3) / 2, -height * (justification // 3) / 2
    corners = [(x0, y0), (x0 + width, y0), (x0, y0 + height), (x0 + width, y0 + height)]
    for _ in range(turns):
        corners = [(-cy, cx) for cx, cy in corners]
    xs = [x + cx for cx, _ in corners]
    ys = [y + cy for _, cy in corners]
    return [min(xs), min(ys), max(xs), max(ys)]


def _gap(a: list[float], b: list[float]) -> float:
    """Edge-to-edge distance between two rectangles (0 when they overlap)."""
    dx = max(b[0] - a[2], a[0] - b[2], 0.0)
    dy = max(b[1] - a[3], a[1] - b[3], 0.0)
    return (dx * dx + dy * dy) ** 0.5


def _owner_position(props: dict[str, str]) -> int | None:
    """Position of the owning record, or None for objects placed on the sheet itself."""
    try:
        owner = int(props.get("ownerindex", ""))
    except ValueError:
        return None
    return owner + 1 if owner > 0 else None


def parse_sch_doc_annotations(path: str) -> SheetAnnotations:
    """Read the information a designer added to a .SchDoc besides the circuit itself.

    Returns sheet-level notes (RECORD=209), text frames (28), free text labels (4),
    pictures (30), directives / parameter sets (43 with their RECORD=41 children), the
    title-block (sheet) parameters (RECORD=41 without an owner) and the sheet template
    (39). Text and pictures that belong to a component symbol or to the template are
    skipped. Each text lists the components whose pins are close by, and each picture
    the text placed next to it.
    """
    with olefile.OleFileIO(path) as f:
        return annotations_from_stream(f.openstream("FileHeader").read())


def annotations_from_stream(data: bytes) -> SheetAnnotations:
    """parse_sch_doc_annotations for the bytes of a FileHeader stream."""
    records = read_records(data)
    result = SheetAnnotations()
    rects: dict[int, list[float]] = {}           # id(annotation) -> rectangle
    part_boxes: dict[int, list[float]] = {}      # component part position -> pin bounding box
    part_modes: dict[int, tuple[str, str]] = {}  # position -> (CurrentPartId, DisplayMode)
    designators: dict[int, str] = {}
    directives: dict[int, SheetDirective] = {}
    font_sizes: dict[str, int] = {}
    image_count = 0

    for pos, props in enumerate(records):
        record = props.get("record")
        owner = _owner_position(props)
        if record == "31":  # sheet settings, including the font table
            for key, val in props.items():
                if key.startswith("size") and key[4:].isdigit() and val.isdigit():
                    font_sizes[key[4:]] = int(val)
            continue
        if record == "1":
            x, y = coord_mil(props, "location.x"), coord_mil(props, "location.y")
            part_boxes[pos] = [x, y, x, y]
            part_modes[pos] = (props.get("currentpartid", "1"), props.get("displaymode", "0"))
            continue
        if owner is not None:
            if record == "2" and owner in part_boxes:
                # A placed part carries the pins of every part; only its own are drawn.
                part, mode = part_modes[owner]
                if (props.get("ownerpartid", part) in (part, "-1", "0")
                        and props.get("ownerpartdisplaymode", "0") == mode):
                    x, y = coord_mil(props, "location.x"), coord_mil(props, "location.y")
                    box = part_boxes[owner]
                    box[:] = [min(box[0], x), min(box[1], y), max(box[2], x), max(box[3], y)]
            elif record == "34" and props.get("name") == "Designator" and owner in part_boxes:
                designators[owner] = props.get("text", "")
            elif record == "41" and owner in directives and props.get("name"):
                directives[owner].parameters[props["name"]] = props.get("text", "")
            continue  # symbol graphics, component parameters, template content

        if record in _TEXT_KINDS:
            text = props.get("text", "")
            if record != "4":  # labels are single-line and stored without escapes
                text = _TEXT_ESCAPE_RE.sub(lambda m: _TEXT_ESCAPES[m.group(1)], text)
            text = text.strip()
            if (not text or props.get("ishidden") == "T"
                    or (record == "4" and _SPECIAL_STRING_RE.match(text))):
                continue
            rect = _label_rect(props, text, font_sizes) if record == "4" else _rect(props)
            item = SheetAnnotation(kind=_TEXT_KINDS[record], text=text)
            if record == "209":
                item.author = props.get("author") or None
                item.collapsed = props.get("collapsed") == "T"
        elif record == "30":
            rect = _rect(props)
            item = SheetAnnotation(
                kind="image",
                file_name=props.get("filename") or None,
                embedded=props.get("embedimage") == "T",
                image_index=image_count,
            )
            image_count += 1
        elif record == "43":
            directives[pos] = SheetDirective(
                name=props.get("name", ""),
                x_mil=coord_mil(props, "location.x"),
                y_mil=coord_mil(props, "location.y"),
            )
            result.directives.append(directives[pos])
            continue
        elif record == "41":
            name, value = props.get("name", ""), props.get("text", "")
            if name:
                result.sheet_parameters[name] = value
            if (name and value not in ("", "*") and name.lower() not in _AUTO_SHEET_PARAMS
                    and not name.lower().startswith("versioncontrol_")
                    and not value.startswith("Record=")):  # printer setup, not a title
                result.title_block[name] = value
            continue
        elif record == "39":
            result.template = props.get("filename") or None
            continue
        else:
            continue
        item.x_mil, item.y_mil = rect[0], rect[1]
        item.width_mil, item.height_mil = rect[2] - rect[0], rect[3] - rect[1]
        rects[id(item)] = rect
        result.items.append(item)

    components = [(designators[pos], box) for pos, box in part_boxes.items() if designators.get(pos)]
    # A special string ("=A + '-' + B") is drawn as its value, whose extent is unknown
    # here, so it is not related to nearby parts or pictures.
    texts = [(i.text, rects[id(i)]) for i in result.items
             if i.kind != "image" and not i.text.startswith("=")]
    for item in result.items:
        if item.kind != "image" and not item.text.startswith("="):
            item.near = _closest(rects[id(item)], components, NEAR_COMPONENT_MIL, _MAX_NEAR)
        elif item.kind == "image":
            item.captions = _closest(rects[id(item)], texts, CAPTION_MIL, 3)
    # Reading order: top of the sheet first, then left to right.
    result.items.sort(key=lambda i: (-round(i.y_mil + i.height_mil), i.x_mil))
    return result


def _closest(rect: list[float], candidates: list[tuple[str, list[float]]],
             limit_mil: float, count: int) -> list[str]:
    names: list[str] = []
    for distance, name in sorted((_gap(rect, box), name) for name, box in candidates):
        if distance > limit_mil or len(names) == count:
            break
        if name not in names:
            names.append(name)
    return names


def read_sch_doc_image(path: str, file_name: str) -> bytes:
    """Return the picture Altium embedded in a .SchDoc under `file_name`.

    Embedded pictures live in the "Storage" stream: a `|HEADER=Icon storage|` record,
    then one record per picture holding 0xD0, a length-prefixed name, a uint32 size and
    zlib-compressed data. Altium stores the decoded bitmap (a BMP) whatever the original
    file type was. Raises KeyError when no picture has that name.
    """
    with olefile.OleFileIO(path) as f:
        if not f.exists("Storage"):
            raise KeyError(file_name)
        return image_from_storage(f.openstream("Storage").read(), file_name)


def image_from_storage(data: bytes, file_name: str) -> bytes:
    """read_sch_doc_image for the bytes of a Storage stream."""
    offset = 0
    first = True
    while offset + 4 <= len(data):
        length = struct.unpack_from("<I", data, offset)[0] & 0xFFFFFF
        body = data[offset + 4 : offset + 4 + length]
        offset += 4 + length
        if first:  # |HEADER=Icon storage|Weight=N
            first = False
            continue
        if len(body) < 6 or body[0] != 0xD0:
            continue
        name_len = body[1]
        raw_name = body[2 : 2 + name_len]
        size = struct.unpack_from("<I", body, 2 + name_len)[0]
        names = {raw_name.decode("latin-1")}
        try:
            names.add(raw_name.decode("utf-8"))
        except UnicodeDecodeError:
            pass
        if file_name in names:
            return zlib.decompress(body[6 + name_len : 6 + name_len + size])
    raise KeyError(file_name)


# ---------- hierarchy: sheet symbols, sheet entries, ports and their wiring ----------

IO_TYPES = {"0": "Unspecified", "1": "Output", "2": "Input", "3": "Bidirectional"}
_SIDES = {"0": "left", "1": "right", "2": "top", "3": "bottom"}
# A sheet symbol designator Repeat(Name, First, Last) places the child once per channel;
# a sheet entry Repeat(Name) splits its bus or harness across the channels.
REPEAT_RE = re.compile(r"^\s*repeat\s*\(\s*([^,()]+?)\s*,\s*(-?\d+)\s*,\s*(-?\d+)\s*\)\s*$",
                       re.IGNORECASE)
ENTRY_REPEAT_RE = re.compile(r"^\s*repeat\s*\(\s*([^,()]+?)\s*\)\s*$", re.IGNORECASE)
_EPS_MIL = 0.01  # points closer than this are one point


@dataclass
class WireGroup:
    """What one piece of wiring joins on a sheet: wires, buses and signal harnesses
    joined end to line, and the pins, labels, power ports, ports and entries on them."""
    wires: int = 0
    pins: list[tuple[str, str]] = field(default_factory=list)  # (sheet designator, pin)
    labels: list[str] = field(default_factory=list)            # net labels (RECORD=25)
    power_ports: list[str] = field(default_factory=list)       # power ports (RECORD=17)
    ports: list[str] = field(default_factory=list)
    entries: list[tuple[str, str]] = field(default_factory=list)  # (symbol, entry name)

    @property
    def wired(self) -> bool:
        """Something besides one port or entry is attached."""
        return bool(self.wires or self.pins or self.labels or self.power_ports
                    or len(self.ports) + len(self.entries) > 1)


@dataclass
class SheetPort:
    name: str
    io_type: str = "Unspecified"
    harness_type: str = ""
    group: int = -1  # index into SheetHierarchy.groups


@dataclass
class SheetEntry:
    name: str
    io_type: str = "Unspecified"
    side: str = "left"
    harness_type: str = ""
    group: int = -1

    @property
    def port_name(self) -> str:
        """The name of the child sheet's port it connects to (Repeat(X) -> X)."""
        m = ENTRY_REPEAT_RE.match(self.name)
        return m.group(1) if m else self.name

    @property
    def repeated(self) -> bool:
        return bool(ENTRY_REPEAT_RE.match(self.name))


@dataclass
class SheetSymbol:
    designator: str = ""
    file_name: str = ""
    unique_id: str = ""
    symbol_type: str = ""     # Normal, or Design Item for a sheet placed from a server
    design_item_id: str = ""
    entries: list[SheetEntry] = field(default_factory=list)
    x_mil: float = 0.0        # top-left corner; Altium's Y axis points up
    y_mil: float = 0.0
    width_mil: float = 0.0
    height_mil: float = 0.0

    @property
    def channel(self) -> tuple[str, int, int] | None:
        """(name, first, last) of a Repeat(name, first, last) designator, else None."""
        m = REPEAT_RE.match(self.designator)
        return (m.group(1), int(m.group(2)), int(m.group(3))) if m else None


@dataclass
class NoErc:
    """A No ERC directive (RECORD=22) placed on the sheet.

    IsActive and SuppressAll read as KiCad's Altium importer reads them: T is true and an
    absent key true too (records saved before Altium had the flags carry neither).
    SuppressAll true is the generic No ERC (every violation suppressed); false is a
    specific one, which suppresses only ErrorKindSetToSuppress (violation types) and
    ConnectionPairsToSuppress (connection-matrix pairs), kept as written."""
    x_mil: float = 0.0
    y_mil: float = 0.0
    active: bool = True
    suppress_all: bool = True
    symbol: str = ""            # Symbol: Thin Cross, Thick Cross, ...
    error_kinds: str = ""       # ErrorKindSetToSuppress, raw
    connection_pairs: str = ""  # ConnectionPairsToSuppress, raw
    flags_written: bool = True  # IsActive, SuppressAll or Symbol present (not a legacy record)
    wiring: WireGroup = field(default_factory=WireGroup)  # what it sits on


@dataclass
class SheetHierarchy:
    symbols: list[SheetSymbol] = field(default_factory=list)
    ports: list[SheetPort] = field(default_factory=list)
    groups: list[WireGroup] = field(default_factory=list)  # those of ports and entries
    power_ports: list[str] = field(default_factory=list)   # every power port name, once
    net_labels: list[str] = field(default_factory=list)    # every net label, once
    no_ercs: list[NoErc] = field(default_factory=list)     # sheet-level No ERC directives


def parse_sch_hierarchy(path: str) -> SheetHierarchy:
    """Read a sheet's sheet symbols (RECORD=15 with their designator 32, file name 33 and
    sheet entries 16), its ports (18), and the wiring each port and entry is on: the
    component pins, net labels (25) and power ports (17) joined to it by wires (27),
    buses (26), signal harnesses (218, in the Additional stream) and junctions (29)."""
    with olefile.OleFileIO(path) as f:
        additional = f.openstream("Additional").read() if f.exists("Additional") else b""
        return hierarchy_from_stream(f.openstream("FileHeader").read(), additional)


def hierarchy_from_stream(data: bytes, additional: bytes = b"") -> SheetHierarchy:
    """parse_sch_hierarchy for the bytes of a FileHeader stream and an Additional one,
    where Altium keeps the harness objects."""
    result = SheetHierarchy()
    symbols: dict[int, SheetSymbol] = {}
    parts: dict[int, tuple[str, str]] = {}  # component position -> (part id, display mode)
    designators: dict[int, str] = {}
    pins: list[tuple[int, str, tuple[float, float]]] = []  # (owner, pin, electrical end)
    wiring = _Wiring()

    for pos, props in enumerate(read_records(data)):
        record = props.get("record")
        owner = _owner_position(props)
        if record == "1":
            parts[pos] = (props.get("currentpartid", "1"), props.get("displaymode", "0"))
        elif record == "2" and owner in parts:
            part, mode = parts[owner]
            conglomerate = _int(props.get("pinconglomerate", "0"))
            if (props.get("ownerpartid", part) in (part, "-1", "0")
                    and props.get("ownerpartdisplaymode", "0") == mode
                    and not conglomerate & 0x04 and props.get("designator")):  # 4: hidden
                pins.append((owner, props["designator"], pin_end(props, conglomerate)))
        elif record == "34" and owner in parts and props.get("name", "").lower() == "designator":
            designators[owner] = props.get("text", "")
        elif record == "15" and owner is None:
            symbols[pos] = SheetSymbol(
                unique_id=props.get("uniqueid", ""),
                symbol_type=props.get("symboltype", ""),
                design_item_id=props.get("designitemid", ""),
                x_mil=coord_mil(props, "location.x"),
                y_mil=coord_mil(props, "location.y"),
                width_mil=coord_mil(props, "xsize"),
                height_mil=coord_mil(props, "ysize"),
            )
            result.symbols.append(symbols[pos])
        elif record in ("32", "33") and owner in symbols:
            name = "designator" if record == "32" else "file_name"
            setattr(symbols[owner], name, props.get("text", "").strip())
        elif record == "16" and owner in symbols:
            symbol = symbols[owner]
            entry = SheetEntry(
                name=props.get("name", "").strip(),
                io_type=IO_TYPES.get(props.get("iotype", "0"), "Unspecified"),
                side=_SIDES.get(props.get("side", "0"), "left"),
                harness_type=props.get("harnesstype", ""),
            )
            symbol.entries.append(entry)
            wiring.add_object("entry", (entry, symbol), [_entry_point(symbol, entry, props)])
        elif owner is not None:
            continue  # symbol graphics, template content
        elif record == "18":
            port = SheetPort(
                name=props.get("name", "").strip(),
                io_type=IO_TYPES.get(props.get("iotype", "0"), "Unspecified"),
                harness_type=props.get("harnesstype", ""),
            )
            result.ports.append(port)
            wiring.add_object("port", port, _port_ends(props))
        elif record in ("26", "27", "218"):
            wiring.add_line(_vertices(props))
        elif record == "29":
            wiring.add_object("junction", None, [_location(props)])
        elif record == "22":
            no_erc = NoErc(
                x_mil=coord_mil(props, "location.x"),
                y_mil=coord_mil(props, "location.y"),
                active=_flag(props, "isactive"),
                suppress_all=_flag(props, "suppressall"),
                symbol=props.get("symbol", ""),
                error_kinds=props.get("errorkindsettosuppress", ""),
                connection_pairs=props.get("connectionpairstosuppress", ""),
                flags_written=any(k in props for k in ("isactive", "suppressall", "symbol")),
            )
            result.no_ercs.append(no_erc)
            wiring.add_no_erc(no_erc, _location(props))
        elif record == "17" and _off_sheet(props):
            # An off-sheet connector: a RECORD=17 too, but not a power port.
            wiring.add_object("off_sheet", props.get("text", ""), [_location(props)])
        elif record in ("17", "25") and props.get("text"):
            kind = "power_port" if record == "17" else "label"
            wiring.add_object(kind, props["text"], [_location(props)])
            names = result.power_ports if record == "17" else result.net_labels
            if props["text"] not in names:
                names.append(props["text"])

    for props in read_records(additional) if additional else ():
        if props.get("record") == "218":
            wiring.add_line(_vertices(props))
    for owner, pin, point in pins:
        refdes = designators.get(owner, "")
        if refdes and "?" not in refdes:
            wiring.add_object("pin", (refdes, pin), [point])
    result.groups = wiring.resolve()
    return result


def _int(text: str) -> int:
    try:
        return int(text)
    except ValueError:
        return 0


def _off_sheet(props: dict[str, str]) -> bool:
    """A RECORD=17 that is an off-sheet connector (IsCrossSheetConnector=T), not a power
    port."""
    return props.get("iscrosssheetconnector", "").strip().upper() in ("T", "TRUE")


def _flag(props: dict[str, str], key: str) -> bool:
    """A T/F flag, true when absent (KiCad's ReadBool(..., true) for No ERC records)."""
    value = props.get(key)
    return True if value is None else value.strip().upper() in ("T", "TRUE")


def _location(props: dict[str, str]) -> tuple[float, float]:
    return coord_mil(props, "location.x"), coord_mil(props, "location.y")


def _vertices(props: dict[str, str]) -> list[tuple[float, float]]:
    count = _int(props.get("locationcount", "0"))
    return [(coord_mil(props, f"x{i}"), coord_mil(props, f"y{i}")) for i in range(1, count + 1)]


def pin_end(props: dict[str, str], conglomerate: int) -> tuple[float, float]:
    """A pin's electrical end: Location is where it meets the body, and the pin points
    PinLength away in the direction PinConglomerate bits 0-1 give (0 right, 1 up, 2 left,
    3 down)."""
    x, y = _location(props)
    length = coord_mil(props, "pinlength")
    dx, dy = ((1, 0), (0, 1), (-1, 0), (0, -1))[conglomerate & 0x03]
    return x + dx * length, y + dy * length


def _entry_point(symbol: SheetSymbol, entry: SheetEntry, props: dict[str, str]) -> tuple[float, float]:
    """Where a sheet entry sits on its symbol's edge. DistanceFromTop counts from the top
    edge (left and right sides) or the left edge (top and bottom) in units of 100 mil,
    plus DistanceFromTop_Frac1 in 1/100000 of a 10 mil unit."""
    distance = (_int(props.get("distancefromtop", "0")) * 10
                + _int(props.get("distancefromtop_frac1", "0")) / 100_000) * _UNIT_MIL
    x, y = symbol.x_mil, symbol.y_mil
    if entry.side == "right":
        return x + symbol.width_mil, y - distance
    if entry.side == "top":
        return x + distance, y
    if entry.side == "bottom":
        return x + distance, y - symbol.height_mil
    return x, y - distance


def _port_ends(props: dict[str, str]) -> list[tuple[float, float]]:
    """A port's two connection points: Width to the right of Location for the horizontal
    styles (0-3), above it for the vertical ones (4-7)."""
    x, y = _location(props)
    width = coord_mil(props, "width")
    if _int(props.get("style", "0")) >= 4:
        return [(x, y), (x, y + width)]
    return [(x, y), (x + width, y)]


def _key(value: float) -> int:
    return round(value / _EPS_MIL)


def _on_segment(x: float, y: float, x0: float, y0: float, x1: float, y1: float) -> bool:
    dx, dy = x1 - x0, y1 - y0
    length2 = dx * dx + dy * dy
    if length2 == 0:
        return abs(x - x0) < _EPS_MIL and abs(y - y0) < _EPS_MIL
    t = ((x - x0) * dx + (y - y0) * dy) / length2
    if t < -1e-9 or t > 1 + 1e-9:
        return False
    return (x0 + t * dx - x) ** 2 + (y0 + t * dy - y) ** 2 < _EPS_MIL ** 2


class LineIndex:
    """Polylines (wires, buses, harnesses) indexed for point queries: which lines pass
    through a point, or within a distance of it. Line numbers count from 0 in the order
    added; a line of fewer than two vertices is not added."""

    _CELL_MIL = 10.0  # row and column width of the index near() reads

    def __init__(self):
        self.lines: list[list[tuple[float, float]]] = []
        self._horizontal: dict[int, list[tuple[float, float, int]]] = {}
        self._vertical: dict[int, list[tuple[float, float, int]]] = {}
        self._other: list[tuple[float, float, float, float, int]] = []
        self._rows: dict[int, list[tuple[float, float, float, int]]] | None = None
        self._columns: dict[int, list[tuple[float, float, float, int]]] | None = None

    def add(self, vertices: list[tuple[float, float]]) -> int | None:
        if len(vertices) < 2:
            return None
        node = len(self.lines)
        self.lines.append(vertices)
        self._rows = self._columns = None
        for (x0, y0), (x1, y1) in zip(vertices, vertices[1:]):
            if abs(y0 - y1) < _EPS_MIL:
                self._horizontal.setdefault(_key(y0), []).append((min(x0, x1), max(x0, x1), node))
            elif abs(x0 - x1) < _EPS_MIL:
                self._vertical.setdefault(_key(x0), []).append((min(y0, y1), max(y0, y1), node))
            else:
                self._other.append((x0, y0, x1, y1, node))
        return node

    def at(self, x: float, y: float) -> set[int]:
        found = set()
        for key in (_key(y) - 1, _key(y), _key(y) + 1):
            for x0, x1, node in self._horizontal.get(key, ()):
                if x0 - _EPS_MIL <= x <= x1 + _EPS_MIL and abs(y - key * _EPS_MIL) < 2 * _EPS_MIL:
                    found.add(node)
        for key in (_key(x) - 1, _key(x), _key(x) + 1):
            for y0, y1, node in self._vertical.get(key, ()):
                if y0 - _EPS_MIL <= y <= y1 + _EPS_MIL and abs(x - key * _EPS_MIL) < 2 * _EPS_MIL:
                    found.add(node)
        for x0, y0, x1, y1, node in self._other:
            if _on_segment(x, y, x0, y0, x1, y1):
                found.add(node)
        return found

    def near(self, x: float, y: float, distance: float) -> set[int]:
        """Lines with a segment no farther than distance (mil) from the point."""
        if self._rows is None:
            self._rows, self._columns = {}, {}
            for key, segments in self._horizontal.items():
                for x0, x1, node in segments:
                    self._rows.setdefault(int(key * _EPS_MIL // self._CELL_MIL), []).append(
                        (key * _EPS_MIL, x0, x1, node))
            for key, segments in self._vertical.items():
                for y0, y1, node in segments:
                    self._columns.setdefault(int(key * _EPS_MIL // self._CELL_MIL), []).append(
                        (key * _EPS_MIL, y0, y1, node))
        found = set()
        span = range(int((y - distance) // self._CELL_MIL), int((y + distance) // self._CELL_MIL) + 1)
        for row in span:
            for level, x0, x1, node in self._rows.get(row, ()):
                dx = max(x0 - x, 0.0, x - x1)
                if dx * dx + (y - level) ** 2 <= distance * distance:
                    found.add(node)
        span = range(int((x - distance) // self._CELL_MIL), int((x + distance) // self._CELL_MIL) + 1)
        for column in span:
            for level, y0, y1, node in self._columns.get(column, ()):
                dy = max(y0 - y, 0.0, y - y1)
                if dy * dy + (x - level) ** 2 <= distance * distance:
                    found.add(node)
        for x0, y0, x1, y1, node in self._other:
            if _distance_to_segment(x, y, x0, y0, x1, y1) <= distance:
                found.add(node)
        return found


def _distance_to_segment(x: float, y: float, x0: float, y0: float, x1: float, y1: float) -> float:
    dx, dy = x1 - x0, y1 - y0
    length2 = dx * dx + dy * dy
    t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((x - x0) * dx + (y - y0) * dy) / length2))
    return ((x0 + t * dx - x) ** 2 + (y0 + t * dy - y) ** 2) ** 0.5


class _Wiring:
    """A sheet's connectivity. Lines (wires, buses, harnesses) join where an end of one
    lies on another (Altium's automatic junctions); objects (pins, ports, entries,
    labels, power ports, junctions) join the lines their points lie on and objects at
    the same point. Lines crossing without a junction stay apart."""

    def __init__(self):
        self._index = LineIndex()
        self._lines = self._index.lines
        self._objects: list[tuple[str, object, list[tuple[float, float]]]] = []
        self._parent: list[int] = []
        self._no_ercs: list[tuple[NoErc, tuple[float, float]]] = []

    def add_line(self, vertices: list[tuple[float, float]]) -> None:
        self._index.add(vertices)

    def add_object(self, kind: str, item, points: list[tuple[float, float]]) -> None:
        self._objects.append((kind, item, points))

    def add_no_erc(self, item: "NoErc", point: tuple[float, float]) -> None:
        """A No ERC marker: it joins nothing (two wires crossing under it stay apart) but
        takes the wiring of the object at its point, else of a line through it."""
        self._no_ercs.append((item, point))

    def _lines_at(self, x: float, y: float) -> set[int]:
        return self._index.at(x, y)

    def _find(self, i: int) -> int:
        while self._parent[i] != i:
            self._parent[i] = self._parent[self._parent[i]]
            i = self._parent[i]
        return i

    def _union(self, a: int, b: int) -> None:
        a, b = self._find(a), self._find(b)
        if a != b:
            self._parent[b] = a

    def resolve(self) -> list[WireGroup]:
        """Join everything, set each port's and entry's group and return those groups."""
        lines = len(self._lines)
        self._parent = list(range(lines + len(self._objects)))
        for node, vertices in enumerate(self._lines):
            for x, y in (vertices[0], vertices[-1]):
                for other in self._lines_at(x, y):
                    self._union(node, other)
        at: dict[tuple, int] = {}
        for i, (kind, item, points) in enumerate(self._objects):
            node = lines + i
            for x, y in points:
                for line in self._lines_at(x, y):
                    self._union(node, line)
                key = (_key(x), _key(y))
                if key in at:
                    self._union(node, at[key])
                else:
                    at[key] = node
            # On one sheet net labels, power ports and ports of one name are one net.
            if kind in ("label", "power_port", "port"):
                name = (kind, (item.name if kind == "port" else item).strip().lower())
                if name in at:
                    self._union(node, at[name])
                else:
                    at[name] = node

        line_counts: dict[int, int] = {}
        for node in range(lines):
            root = self._find(node)
            line_counts[root] = line_counts.get(root, 0) + 1
        members: dict[int, list[tuple[str, object]]] = {}
        for i, (kind, item, _) in enumerate(self._objects):
            members.setdefault(self._find(lines + i), []).append((kind, item))

        groups: list[WireGroup] = []
        index: dict[int, int] = {}
        for i, (kind, item, _) in enumerate(self._objects):
            if kind not in ("port", "entry"):
                continue
            root = self._find(lines + i)
            if root not in index:
                index[root] = len(groups)
                groups.append(_group(line_counts.get(root, 0), members[root]))
            (item[0] if kind == "entry" else item).group = index[root]
        for item, (x, y) in self._no_ercs:
            node = at.get((_key(x), _key(y)))
            if node is None:
                node = min(self._lines_at(x, y), default=None)
            if node is not None:
                root = self._find(node)
                item.wiring = _group(line_counts.get(root, 0), members.get(root, []))
        return groups


def _group(wires: int, members: list[tuple[str, object]]) -> WireGroup:
    group = WireGroup(wires=wires)
    for kind, item in members:
        if kind == "pin":
            target, value = group.pins, item
        elif kind == "label":
            target, value = group.labels, item
        elif kind == "power_port":
            target, value = group.power_ports, item
        elif kind == "port":
            target, value = group.ports, item.name
        elif kind == "entry":
            target, value = group.entries, (item[1].designator, item[0].name)
        else:
            continue
        if kind in ("port", "entry") or value not in target:
            target.append(value)
    return group


# ---------- connectivity: every object the netlist compiler joins ----------

Point = tuple[float, float]


@dataclass
class SchPart:
    """One placed part (RECORD=1); a multi-part component has one per part."""
    designator: str
    unique_id: str
    part_id: str              # CurrentPartId: 1 = part A, ...
    part_count: str           # PartCount: number of parts + 1
    origin: Point


@dataclass
class SchPin:
    """A pin of a placed part, drawn for that part and display mode."""
    part: int                 # index into SheetConnectivity.parts
    designator: str
    point: Point              # the electrical end
    hidden: bool = False
    name: str = ""
    hidden_net: str = ""      # HiddenNetName: the legacy net of a hidden pin


@dataclass
class HarnessConnector:
    """A harness connector (RECORD=215 in the Additional stream) and its entries (216):
    the harness side takes the signal harness, each entry a wire of its own."""
    point: Point              # where the signal harness attaches
    entries: list[tuple[str, Point]] = field(default_factory=list)
    harness_type: str = ""
    # Location (top-left), XSize, YSize and HarnessConnectorSide, placing the entries.
    box: tuple[float, float, float, float, str] = (0.0, 0.0, 0.0, 0.0, "0")


@dataclass
class ParameterSet:
    """A Parameter Set directive (RECORD=43) and its parameters (RECORD=41 children) as
    (name, value) in file order, unnamed ones included: a differential pair directive is
    one with DifferentialPair=True. It applies to the wire, bus, signal harness or blanket
    at its location. unique_id is its UniqueID, parameter_ids each parameter's ("" when
    the record has none), in the order of parameters."""
    name: str
    x_mil: float
    y_mil: float
    parameters: list[tuple[str, str]] = field(default_factory=list)
    unique_id: str = ""
    parameter_ids: list[str] = field(default_factory=list)


@dataclass
class SheetConnectivity:
    """What a sheet draws that the netlist compiler joins, read in one pass."""
    parts: list[SchPart] = field(default_factory=list)
    pins: list[SchPin] = field(default_factory=list)
    wires: list[list[Point]] = field(default_factory=list)       # RECORD=27
    buses: list[list[Point]] = field(default_factory=list)       # RECORD=26
    harnesses: list[list[Point]] = field(default_factory=list)   # RECORD=218
    junctions: list[Point] = field(default_factory=list)         # RECORD=29
    labels: list[tuple[str, Point]] = field(default_factory=list)       # RECORD=25
    power_ports: list[tuple[str, Point]] = field(default_factory=list)  # RECORD=17
    off_sheet: list[tuple[str, Point]] = field(default_factory=list)    # 17, cross-sheet
    ports: list[tuple[SheetPort, list[Point]]] = field(default_factory=list)  # 18, both ends
    symbols: list[tuple[SheetSymbol, list[Point]]] = field(default_factory=list)  # entry points
    connectors: list[HarnessConnector] = field(default_factory=list)
    masks: list[list[float]] = field(default_factory=list)       # enabled compile masks
    # parse_sch_doc's view of the same records, for the netlist's component metadata.
    components: dict[str, ComponentMeta] = field(default_factory=dict)
    clashes: list[DesignatorClash] = field(default_factory=list)
    # Connect nothing; the offline ERC reads them (RECORD=22 and 43 with their 41s).
    no_ercs: list[NoErc] = field(default_factory=list)
    parameter_sets: list[ParameterSet] = field(default_factory=list)
    # Blankets (RECORD=225): the outline of each, as vertices. A directive on a blanket's
    # edge applies to the nets under it.
    blankets: list[list[Point]] = field(default_factory=list)
    # The sheet header (RECORD=31) and title block, for the offline ERC: the sheet size
    # as parse_sch_integrity reads it, the snap grid (SnapGridSize, None when the header
    # has none; SnapGridOn=T when snap is on, absent when off: Altium leaves a false flag
    # out) and the SheetNumber sheet parameter (RECORD=41 without an owner).
    sheet_style: str = "A4"
    width_mil: float = 0.0
    height_mil: float = 0.0
    portrait: bool = False
    snap_grid_mil: float | None = None
    snap_grid_on: bool = False
    sheet_number: str = ""


def parse_sch_connectivity(path: str) -> SheetConnectivity:
    """Read a .SchDoc for the netlist compiler: the parts and their pins (hidden ones
    flagged), wires, buses, signal harnesses with their connectors and entries (in the
    Additional stream), junctions, net labels, power ports, off-sheet connectors, ports,
    sheet symbols with their entries, the enabled compile masks, No ERC markers and
    parameter sets, plus the component metadata parse_sch_doc gives."""
    with olefile.OleFileIO(path) as f:
        additional = f.openstream("Additional").read() if f.exists("Additional") else b""
        return connectivity_from_stream(f.openstream("FileHeader").read(), additional,
                                        source=path)


def connectivity_from_stream(data: bytes, additional: bytes = b"",
                             source: str = "") -> SheetConnectivity:
    """parse_sch_connectivity for the bytes of a FileHeader and an Additional stream."""
    records = read_records(data)
    result = SheetConnectivity()
    (result.sheet_style, result.width_mil, result.height_mil,
     result.portrait) = _sheet_size({})
    parts: dict[int, int] = {}                  # record position -> index into parts
    modes: dict[int, tuple[str, str]] = {}      # record position -> (part id, display mode)
    symbols: dict[int, tuple[SheetSymbol, list[Point]]] = {}
    parameter_sets: dict[int, ParameterSet] = {}
    for pos, props in enumerate(records):
        record = props.get("record")
        owner = _owner_position(props)
        if record == "41" and owner in parameter_sets:
            parameter_sets[owner].parameters.append((props.get("name", ""),
                                                     props.get("text", "")))
            parameter_sets[owner].parameter_ids.append(props.get("uniqueid", ""))
        elif record == "1":
            parts[pos] = len(result.parts)
            modes[pos] = (props.get("currentpartid", "1"), props.get("displaymode", "0"))
            result.parts.append(SchPart("", props.get("uniqueid", ""), modes[pos][0],
                                        props.get("partcount", ""), _location(props)))
        elif record == "2" and owner in parts:
            part, mode = modes[owner]
            conglomerate = _int(props.get("pinconglomerate", "0"))
            if (props.get("ownerpartid", part) in (part, "-1", "0")
                    and props.get("ownerpartdisplaymode", "0") == mode
                    and props.get("designator")):
                result.pins.append(SchPin(
                    parts[owner], props["designator"], pin_end(props, conglomerate),
                    hidden=bool(conglomerate & 0x04), name=props.get("name", ""),
                    hidden_net=props.get("hiddennetname", "")))
        elif record == "34" and owner in parts:
            if props.get("name", "").lower() == "designator":
                result.parts[parts[owner]].designator = props.get("text", "")
        elif record == "15" and owner is None:
            symbol = SheetSymbol(
                unique_id=props.get("uniqueid", ""),
                symbol_type=props.get("symboltype", ""),
                design_item_id=props.get("designitemid", ""),
                x_mil=coord_mil(props, "location.x"),
                y_mil=coord_mil(props, "location.y"),
                width_mil=coord_mil(props, "xsize"),
                height_mil=coord_mil(props, "ysize"),
            )
            symbols[pos] = (symbol, [])
            result.symbols.append(symbols[pos])
        elif record in ("32", "33") and owner in symbols:
            name = "designator" if record == "32" else "file_name"
            setattr(symbols[owner][0], name, props.get("text", "").strip())
        elif record == "16" and owner in symbols:
            symbol, points = symbols[owner]
            entry = SheetEntry(
                name=props.get("name", "").strip(),
                io_type=IO_TYPES.get(props.get("iotype", "0"), "Unspecified"),
                side=_SIDES.get(props.get("side", "0"), "left"),
                harness_type=props.get("harnesstype", ""),
            )
            symbol.entries.append(entry)
            points.append(_entry_point(symbol, entry, props))
        elif owner is not None:
            continue  # symbol graphics, template content
        elif record == "18":
            port = SheetPort(
                name=props.get("name", "").strip(),
                io_type=IO_TYPES.get(props.get("iotype", "0"), "Unspecified"),
                harness_type=props.get("harnesstype", ""),
            )
            result.ports.append((port, _port_ends(props)))
        elif record == "27":
            result.wires.append(_vertices(props))
        elif record == "26":
            result.buses.append(_vertices(props))
        elif record == "218":
            result.harnesses.append(_vertices(props))
        elif record == "29":
            result.junctions.append(_location(props))
        elif record == "17" and props.get("text"):
            target = result.off_sheet if _off_sheet(props) else result.power_ports
            target.append((props["text"], _location(props)))
        elif record == "25" and props.get("text"):
            result.labels.append((props["text"], _location(props)))
        elif record == "211" and props.get("collapsed", "").upper() != "T":
            result.masks.append(_rect(props))  # a collapsed compile mask is disabled
        elif record == "22":
            result.no_ercs.append(NoErc(
                x_mil=coord_mil(props, "location.x"), y_mil=coord_mil(props, "location.y"),
                active=_flag(props, "isactive"), suppress_all=_flag(props, "suppressall"),
                symbol=props.get("symbol", ""),
                error_kinds=props.get("errorkindsettosuppress", ""),
                connection_pairs=props.get("connectionpairstosuppress", ""),
                flags_written=any(k in props for k in ("isactive", "suppressall", "symbol"))))
        elif record == "43":
            parameter_sets[pos] = ParameterSet(props.get("name", ""),
                                               *_location(props),
                                               unique_id=props.get("uniqueid", ""))
            result.parameter_sets.append(parameter_sets[pos])
        elif record == "225":
            outline = _vertices(props)
            if len(outline) < 3 and "corner.x" in props:
                (x0, y0), (x1, y1) = _location(props), (coord_mil(props, "corner.x"),
                                                        coord_mil(props, "corner.y"))
                outline = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
            if len(outline) >= 3:
                result.blankets.append(outline)
        elif record == "31":
            (result.sheet_style, result.width_mil, result.height_mil,
             result.portrait) = _sheet_size(props)
            if "snapgridsize" in props:
                result.snap_grid_mil = coord_mil(props, "snapgridsize")
            # Not _flag: a sheet header leaves out a false flag, so absent is off.
            result.snap_grid_on = props.get("snapgridon", "").strip().upper() in ("T", "TRUE")
        elif record == "41" and props.get("name", "").lower() == "sheetnumber":
            result.sheet_number = props.get("text", "")
    connector: HarnessConnector | None = None
    for props in read_records(additional) if additional else ():
        record = props.get("record")
        if record == "218":
            result.harnesses.append(_vertices(props))
        elif record == "215":
            connector = _harness_connector(props)
            result.connectors.append(connector)
        elif record == "216" and connector is not None:
            connector.entries.append((props.get("name", "").strip(),
                                      _harness_entry_point(connector, props)))
        elif record == "217" and connector is not None:
            connector.harness_type = props.get("text", "").strip()
    result.components = _components_from_records(records, source, result.clashes)
    return result


def _harness_connector(props: dict[str, str]) -> HarnessConnector:
    """A harness connector box from its top-left Location, XSize and YSize. The signal
    harness attaches on HarnessConnectorSide (0 left, 1 right, 2 top, 3 bottom) at
    PrimaryConnectionPosition along it; the entries sit on the opposite side."""
    x, y = _location(props)
    width, height = coord_mil(props, "xsize"), coord_mil(props, "ysize")
    side = props.get("harnessconnectorside", "0")
    along = coord_mil(props, "primaryconnectionposition")
    point = {"1": (x + width, y - along), "2": (x + along, y),
             "3": (x + along, y - height)}.get(side, (x, y - along))
    return HarnessConnector(point, box=(x, y, width, height, side))


def _harness_entry_point(connector: HarnessConnector, props: dict[str, str]) -> Point:
    """A harness entry on the side of its connector opposite the harness, DistanceFromTop
    (100 mil units, plus DistanceFromTop_Frac1) from the top or left corner."""
    x, y, width, height, side = connector.box
    distance = (_int(props.get("distancefromtop", "0")) * 10
                + _int(props.get("distancefromtop_frac1", "0")) / 100_000) * _UNIT_MIL
    return {"1": (x, y - distance), "2": (x + distance, y - height),
            "3": (x + distance, y)}.get(side, (x + width, y - distance))


# ---------- integrity: the sheet size, parts and names as written ----------

# SheetStyle N (Design > Sheet Properties > Standard): name, width and height in 10 mil
# units, landscape. The table and order are KiCad's Altium importer's (ASchSheetGetSize,
# after python-altium's format notes); an absent or unknown style is A4, as there.
SHEET_STYLES = (
    ("A4", 1150, 760), ("A3", 1550, 1110), ("A2", 2230, 1570), ("A1", 3150, 2230),
    ("A0", 4460, 3150), ("A", 950, 750), ("B", 1500, 950), ("C", 2000, 1500),
    ("D", 3200, 2000), ("E", 4200, 3200), ("Letter", 1100, 850), ("Legal", 1400, 850),
    ("Tabloid", 1700, 1100), ("OrCAD A", 990, 790), ("OrCAD B", 1540, 990),
    ("OrCAD C", 2060, 1560), ("OrCAD D", 3260, 2060), ("OrCAD E", 4280, 3280),
)


@dataclass
class SheetName:
    """A name on a sheet as written: not stripped, the %UTF8% copy when Altium wrote one."""
    kind: str                 # net_label, power_port, port, sheet_entry, designator, parameter
    text: str                 # the name (a parameter's name)
    x_mil: float = 0.0
    y_mil: float = 0.0
    owner: str = ""           # the part's designator (designators, parameters), the symbol's
    value: str | None = None  # parameters: the value as written


@dataclass
class SheetPart:
    """One placed part (RECORD=1); a multi-part component has one per part."""
    designator: str           # as written
    unique_id: str
    lib_reference: str
    x_mil: float
    y_mil: float
    points: list[tuple[float, float]] = field(default_factory=list)  # origin, drawn pin ends
    masked: bool = False      # its origin is inside an enabled compile mask


@dataclass
class SheetIntegrity:
    sheet_style: str          # a SHEET_STYLES name, or "Custom"
    width_mil: float
    height_mil: float
    portrait: bool = False
    parts: list[SheetPart] = field(default_factory=list)
    names: list[SheetName] = field(default_factory=list)
    bus_wires: int = 0        # RECORD=26
    # parse_sch_doc's components: compile_masked and masked_unique_ids as the loads see them
    components: dict[str, ComponentMeta] = field(default_factory=dict)


def parse_sch_integrity(path: str) -> SheetIntegrity:
    """Read what check_project_integrity needs from a .SchDoc: the sheet size (RECORD=31
    SheetStyle, UseCustomSheet with CustomX/CustomY, WorkspaceOrientation), every part
    with its origin, drawn pin ends and compile-mask cover (RECORD=211), and the names as
    written: net labels (25), power ports (17), ports (18), sheet entries (16), designators
    (34) and component parameters (41)."""
    with olefile.OleFileIO(path) as f:
        return integrity_from_stream(f.openstream("FileHeader").read(), source=path)


def _sheet_size(props: dict[str, str]) -> tuple[str, float, float, bool]:
    portrait = props.get("workspaceorientation", "").strip() == "1"
    if props.get("usecustomsheet", "").strip().upper() in ("T", "TRUE"):
        # A custom size is the width and height as given. Whether Altium turns it on a
        # portrait sheet is undocumented (KiCad's importer does); the integrity check
        # accepts both.
        return "Custom", coord_mil(props, "customx"), coord_mil(props, "customy"), portrait
    style = _int(props.get("sheetstyle", "0").strip())
    name, width, height = SHEET_STYLES[style if 0 <= style < len(SHEET_STYLES) else 0]
    width, height = width * _UNIT_MIL, height * _UNIT_MIL
    return name, (height if portrait else width), (width if portrait else height), portrait


def integrity_from_stream(data: bytes, source: str = "") -> SheetIntegrity:
    """parse_sch_integrity for the bytes of a FileHeader stream."""
    result = SheetIntegrity(*_sheet_size({}))
    parts: dict[int, SheetPart] = {}
    modes: dict[int, tuple[str, str]] = {}
    designators: dict[int, tuple[str, float, float]] = {}
    parameters: list[tuple[int, str, str, float, float]] = []
    symbols: dict[int, tuple[str, float, float]] = {}  # designator, top-left corner
    entries: list[tuple[int, str, float, float]] = []
    masks: list[list[float]] = []
    records = read_records(data, strip=False)
    for pos, props in enumerate(records):
        record = props.get("record", "").strip()
        owner = _owner_position(props)
        x, y = _location(props)
        if record == "31":
            result.sheet_style, result.width_mil, result.height_mil, result.portrait = (
                _sheet_size(props))
        elif record == "1":
            parts[pos] = SheetPart("", props.get("uniqueid", "").strip(),
                                   props.get("libreference", "").strip(), x, y, [(x, y)])
            modes[pos] = (props.get("currentpartid", "1").strip(),
                          props.get("displaymode", "0").strip())
        elif record == "2" and owner in parts:
            part, mode = modes[owner]
            conglomerate = _int(props.get("pinconglomerate", "0").strip())
            if (props.get("ownerpartid", part).strip() in (part, "-1", "0")
                    and props.get("ownerpartdisplaymode", "0").strip() == mode
                    and not conglomerate & 0x04):
                parts[owner].points.append(pin_end(props, conglomerate))
        elif record == "34" and owner in parts:
            if props.get("name", "").strip().lower() == "designator":
                designators[owner] = (props.get("text", ""), x, y)
        elif record == "41" and owner in parts:
            if props.get("name", "").strip():
                parameters.append((owner, props["name"], props.get("text", ""), x, y))
        elif record == "211" and owner is None:
            if props.get("collapsed", "").strip().upper() != "T":
                masks.append(_rect(props))
        elif record == "15" and owner is None:
            symbols[pos] = ("", x, y)  # an entry is placed by its symbol's top-left corner
        elif record == "32" and owner in symbols:
            symbols[owner] = (props.get("text", "").strip(), *symbols[owner][1:])
        elif record == "16" and owner in symbols:
            entries.append((owner, props.get("name", ""), *symbols[owner][1:]))
        elif owner is not None:
            continue  # symbol graphics, template content
        elif record in ("17", "25", "18"):
            kind = {"17": "power_port", "25": "net_label", "18": "port"}[record]
            text = props.get("name" if record == "18" else "text", "")
            if text.strip():
                result.names.append(SheetName(kind, text, x, y))
        elif record == "26":
            result.bus_wires += 1
    for pos, part in parts.items():
        text, x, y = designators.get(pos, ("", part.x_mil, part.y_mil))
        part.designator = text
        part.masked = any(x0 <= part.x_mil <= x1 and y0 <= part.y_mil <= y1
                          for x0, y0, x1, y1 in masks)
        result.parts.append(part)
        if text:
            result.names.append(SheetName("designator", text, x, y, owner=text.strip()))
    for owner, name, value, x, y in parameters:
        designator = parts[owner].designator.strip()
        result.names.append(SheetName("parameter", name, x, y, owner=designator, value=value))
    for owner, name, x, y in entries:
        if name.strip():
            result.names.append(SheetName("sheet_entry", name, x, y, owner=symbols[owner][0]))
    # The same records stripped, as parse_sch_doc reads them (one decode of the stream).
    result.components = _components_from_records(
        [{key: value.strip() for key, value in props.items()} for props in records], source)
    return result
