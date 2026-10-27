"""Bill of materials per variant, from the compiled part list and the sheets' parameters.

The parts are the compiled ones (netlist['parts'], from the Protel .NET; compile-masked
parts are not in it), with their physical designators. Altium leaves out Graphical,
Net Tie (No BOM) and Standard (No BOM) parts, and drops a variant's Not Fitted parts;
its parameter variations replace the part's values. Altium matches a variation to its
component by UniqueId, so the designator stored with it is only a fallback.

The manufacturer part number comes from the parts' parameters ('Manufacturer Part
Number 1' and the spellings libraries use for it), never from Comment: Comment is its own
column, with special strings such as '=Value' resolved.
"""

import csv
import io
import re
from dataclasses import dataclass, field

try:
    from parsers.prj_pcb import Variation
except ImportError:
    from server.parsers.prj_pcb import Variation

# ComponentKind values that stay in the BOM: Standard, Mechanical, Net Tie (In BOM),
# Jumper. Out: Graphical (2), Net Tie (4), Standard (No BOM) (5).
BOM_KINDS = {0, 1, 3, 6}
KIND_NAMES = {
    0: "standard", 1: "mechanical", 2: "graphical", 3: "net tie (in BOM)",
    4: "net tie", 5: "standard (no BOM)", 6: "jumper",
}
BASE_VARIANT = "[No Variations]"
LINE_LIMIT = 100
_HOPS = 4
_VARIES_SHOWN = 5  # distinct values listed per varying line field

_MANUFACTURER = r"(?:manufacturer|manufact|manuf|mfr|mfg)"
MPN_NAME = re.compile(
    rf"^{_MANUFACTURER}\.? ?(?:part ?(?:number|num|no\.?|#)?|pn|p/n|#) ?(\d*)$"
)
MFR_NAME = re.compile(rf"^{_MANUFACTURER}\.?(?: ?name)? ?(\d*)$")
PART_NUMBER = re.compile(r"^part ?(?:number|no\.?|#)(?: ?\d+)?$")
_NATURAL = re.compile(r"(\d+)")
# Part number texts that say there is none: on purpose ('N/A' on a test point or a
# fiducial), or not chosen yet ('TBD'), which still needs one.
_NO_MPN = {
    "-", "--", "---", "n/a", "na", "n.a.", "n.a", "none", "x", "nc", "dnp",
    "not applicable", "tbd", "tba", "?",
}
_UNDECIDED_MPN = {"tbd", "tba", "?"}
# A CSV cell starting like this is run as a formula by spreadsheets.
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def norm_name(name: str) -> str:
    return re.sub(r"\s+", " ", name.replace("_", " ")).strip().lower()


def _unset(text: str | None) -> bool:
    return text is None or text.strip() in ("", "*")


def resolve(
    text: str | None, params: dict[str, str], fallback: dict[str, str] | None = None,
) -> tuple[str | None, bool]:
    """Resolve an '=Name' special string against the part's parameters, then fallback
    (the project parameters, lower-case names), a few hops deep. Returns (text,
    resolved); unresolvable text comes back as written."""
    fallback = fallback or {}
    hops = 0
    while text and text.startswith("=") and hops < _HOPS:
        name = text[1:].strip().strip('"').lower()
        if name in params:
            text = params[name]
        elif name in fallback:
            text = fallback[name]
        else:
            return text, False
        hops += 1
    if text and text.startswith("="):
        return text, False
    return text, True


def part_numbers(params: dict[str, str]) -> tuple[str | None, str | None, str | None]:
    """(mpn, manufacturer, source): the lowest-index 'Manufacturer Part Number N' (in the
    spellings libraries use) with the manufacturer of the same index (or an unnumbered
    one); else a generic 'Part Number' (source 'part number')."""
    mpns: dict[int, str] = {}
    makers: dict[int, str] = {}
    generic = None
    for name, text in params.items():
        if _unset(text):
            continue
        normal = norm_name(name)
        match = MPN_NAME.match(normal)
        if match:
            mpns.setdefault(int(match[1] or 0), text.strip())
            continue
        match = MFR_NAME.match(normal)
        if match:
            makers.setdefault(int(match[1] or 0), text.strip())
            continue
        if PART_NUMBER.match(normal) and generic is None:
            generic = text.strip()
    if mpns:
        index = min(mpns)
        maker = makers.get(index) or makers.get(0) or (makers.get(1) if index == 0 else None)
        return mpns[index], maker, "parameter"
    if generic:
        return generic, makers.get(0) or makers.get(1), "part number"
    return None, (makers.get(0) or makers.get(1)), None


def natural_key(designator: str):
    return [int(part) if part.isdigit() else part.lower()
            for part in _NATURAL.split(designator)]


@dataclass
class BomPart:
    designator: str
    kind: int = 0
    comment: str | None = None
    value: str | None = None
    description: str | None = None
    footprint: str | None = None
    mpn: str | None = None
    manufacturer: str | None = None
    mpn_source: str | None = None
    fitted: bool = True
    logical: str | None = None
    sheet: str | None = None
    lib_reference: str = ""
    overrides: dict[str, str] = field(default_factory=dict)  # field -> varied value
    unresolved: list[str] = field(default_factory=list)  # special strings left as written
    mpn_placeholder: str | None = None  # 'N/A', 'TBD', ... written as the part number
    alternate_part: str | None = None  # the variant's alternate part (Kind=2)
    # The alternate's data came from its copy in the .PrjPcbVariants file.
    alternate_library: bool = False
    # Its parameters as the BOM sees them (the variant's variations applied, special
    # strings as written): what a BOM column of a parameter's name shows.
    parameters: dict[str, str] = field(default_factory=dict, repr=False)


def needs_mpn(part: BomPart) -> bool:
    """Whether a part should carry a manufacturer part number: not a mechanical part,
    and not marked as having none on purpose ('N/A'; 'TBD' still needs one)."""
    if part.kind == 1:
        return False
    placeholder = (part.mpn_placeholder or "").strip().lower()
    return not placeholder or placeholder in _UNDECIDED_MPN


@dataclass
class VariantPlan:
    """A variant resolved against the compiled parts."""

    name: str
    not_fitted: set[str] = field(default_factory=set)
    params: dict[str, dict[str, tuple[str, str]]] = field(default_factory=dict)
    stale: list[dict] = field(default_factory=list)  # variation entries that do not fit
    # Alternate parts (Kind=2) by designator: {"part": DesignItemID, "footprint": ...}.
    alternates: dict[str, dict] = field(default_factory=dict)


@dataclass
class _PartIndex:
    """What variation lines bind against, see plan_variant."""

    known: set[str]
    by_path: dict[str, list[str]]
    by_tail: dict[str, list[str]]
    compiled: set[str]  # empty when there is no compiled part list
    loose: set[str]  # sheet parts outside the compiled design that are not masked
    masked: set[str]  # UniqueIds of compile-masked copies a live part replaced


def _bind(variation, index: _PartIndex, plan: VariantPlan) -> list[str]:
    """The parts one variation line names; its problems go to plan.stale."""
    stored = variation.designator
    label = stored or variation.unique_id
    path = variation.unique_id
    targets = list(index.by_path.get(path) or []) if path else []
    ambiguous = False
    if not targets and variation.uid_tail:
        candidates = index.by_tail.get(variation.uid_tail) or []
        # Channel copies share the tail: the stored designator picks one if it can.
        if stored in candidates:
            targets = [stored]
        else:
            targets, ambiguous = list(candidates), len(candidates) > 1
    if not targets and variation.uid_tail in index.masked:
        plan.stale.append({
            "designator": label, "issue": "on_masked_part",
            "detail": "its UniqueId is on a compile-masked copy, which is not compiled; "
            "not applied",
        })
        return []
    if targets and stored in index.compiled and set(targets) <= index.loose:
        # The UniqueId is on a sheet part whose compiled copies were not matched to it
        # (e.g. channel names outside the usual suffixes): the compiled designator wins.
        plan.stale.append({
            "designator": stored, "issue": "unique_id_not_compiled",
            "detail": f"its UniqueId is on {', '.join(sorted(targets, key=natural_key))}, "
            "which is not in the compiled design; matched by designator",
        })
        return [stored]
    if not targets and stored in index.known and "?" not in stored:
        if variation.unique_id:
            plan.stale.append({
                "designator": stored, "issue": "unique_id_not_found",
                "detail": "matched by designator; its UniqueId is on no part",
            })
        return [stored]
    if not targets:
        plan.stale.append({
            "designator": label, "issue": "no_such_part",
            "detail": "the variation names no part (deleted or renamed)",
        })
        return []
    shown = ", ".join(sorted(targets, key=natural_key))
    if ambiguous:
        plan.stale.append({
            "designator": label, "issue": "ambiguous_unique_id",
            "detail": f"its UniqueId is on {len(targets)} channel copies ({shown}) and the "
            "stored designator names none of them; applied to all of them",
        })
    elif stored and stored not in targets and "?" not in stored:
        plan.stale.append({
            "designator": stored, "issue": "designator_changed",
            "detail": f"its UniqueId is on {shown} now; the stored designator is stale",
        })
    return targets


def alternate_of(variation) -> dict:
    """An alternate part (Kind=2): its library item ('part') and, when it differs, its
    footprint, with the variation line itself ('variation': its alternate_identity()
    and its copy in the .PrjPcbVariants file, library_part)."""
    alt = variation.alt or {}
    same = alt.get("IsSameFootprint", "").strip().lower() in ("true", "t", "1")
    library = getattr(variation, "library_part", None)
    footprint = None if same else (alt.get("Footprint") or (library.footprint if library else None))
    return {
        "part": alternate_lib_reference(variation) or None,
        "footprint": footprint or None,
        "variation": variation,
    }


def alternate_lib_reference(variation) -> str:
    """The library reference an alternate-part variation names ('' for none):
    Variation.alternate_reference."""
    return getattr(variation, "alternate_reference", "") if variation is not None else ""


def variant_names(netlist: dict) -> list[str]:
    """The parts variants bind to: the compiled ones and every sheet component, so a
    variation on a part outside the compiled design binds to it (and leaves the BOM
    alone) instead of falling back to its designator."""
    return list(dict.fromkeys([*(netlist.get("parts") or {}), *(netlist.get("components") or {})]))


def plan_variant(variant, netlist: dict, names: list[str]) -> VariantPlan:
    """Bind a variant's entries to compiled parts: by full UniqueId path, then its last
    segment, then the designator stored with it (Altium follows the UniqueId). Parameter
    variations belong to the component variation of their designator and follow its
    binding. A variant known only by its Not Fitted designators (the older [CompVar]
    format, or one built by hand) binds by designator."""
    if variant is None or variant.name == BASE_VARIANT:
        return VariantPlan(name=BASE_VARIANT)
    components = netlist.get("components") or {}
    compiled = set(netlist.get("parts") or {})
    index = _PartIndex(known=set(names), by_path={}, by_tail={}, compiled=compiled,
                       loose=set(), masked=set())
    for name in names:
        meta = components.get(name) or {}
        for path in meta.get("unique_paths") or []:
            index.by_path.setdefault(path, []).append(name)
        tails = list(meta.get("unique_ids") or [])
        if meta.get("unique_id") and meta["unique_id"] not in tails:
            tails.append(meta["unique_id"])
        for tail in tails:
            index.by_tail.setdefault(tail, []).append(name)
        index.masked.update(meta.get("masked_unique_ids") or [])
        if compiled and name not in compiled and not meta.get("compile_masked"):
            index.loose.add(name)
    plan = VariantPlan(name=variant.name)
    variations = list(getattr(variant, "variations", None) or []) or [
        Variation(designator=designator, unique_id="", kind=1)
        for designator in getattr(variant, "dnp_refdes", None) or []
    ]
    line_targets: dict[str, set[tuple[str, ...]]] = {}
    for variation in variations:
        targets = _bind(variation, index, plan)
        line_targets.setdefault(variation.designator, set()).add(tuple(targets))
        if variation.kind == 1:
            plan.not_fitted.update(targets)
        elif variation.kind == 2 and targets:
            for target in targets:
                plan.alternates[target] = alternate_of(variation)
    for designator, params in (getattr(variant, "param_variations", None) or {}).items():
        bound = line_targets.get(designator)
        if bound and len(bound) > 1:
            plan.stale.append({
                "designator": designator, "issue": "ambiguous_designator",
                "detail": "parameter variations on a designator that several variations "
                "carry for different parts; not applied",
            })
            continue
        if bound:
            targets = list(next(iter(bound)))  # empty: its variation was reported
        elif designator in index.known and "?" not in designator:
            targets = [designator]
        else:
            plan.stale.append({
                "designator": designator, "issue": "no_such_part",
                "detail": "a parameter variation names no part",
            })
            continue
        for target in targets:
            plan.params.setdefault(target, {}).update(params)
    for designator in sorted(set(plan.params) & plan.not_fitted, key=natural_key):
        plan.stale.append({
            "designator": designator, "issue": "varied_but_not_fitted",
            "detail": "parameter variations on a part the variant does not fit",
        })
    return plan


def build_parts(
    netlist: dict, plan: VariantPlan | None = None, project_params: dict | None = None,
) -> tuple[list[BomPart], dict]:
    """The variant's BOM parts, and what was left out by kind. Special strings resolve
    against the part's parameters, then project_params (the .PrjPcb's)."""
    plan = plan or VariantPlan(name=BASE_VARIANT)
    fallback = {name.lower(): value for name, value in (project_params or {}).items()}
    compiled = netlist.get("parts") or {}
    components = netlist.get("components") or {}
    names = list(compiled) if compiled else [
        name for name, meta in components.items() if not meta.get("compile_masked")
    ]
    parts: list[BomPart] = []
    excluded: dict[str, list[str]] = {}
    for name in names:
        meta = components.get(name) or {}
        kind = int(meta.get("kind") or 0)
        if kind not in BOM_KINDS:
            excluded.setdefault(KIND_NAMES.get(kind, str(kind)), []).append(name)
            continue
        alternate = plan.alternates.get(name) or {}
        library = getattr(alternate.get("variation"), "library_part", None)
        # An alternate part is another library part: its saved copy's parameters and
        # description replace the base part's (the variant's parameter variations then
        # apply on top, as Altium stores the alternate's edited values there).
        params = ({key: text for key, (_, text) in library.params.items()} if library
                  else dict(meta.get("params") or {}))
        overrides: dict[str, str] = {}
        varied = plan.params.get(name) or {}
        for key, (original, value) in varied.items():
            params[key] = value
            overrides[original] = value
        unresolved: list[str] = []

        def text(raw):
            value, ok = resolve(raw, params, fallback)
            if not ok:
                unresolved.append(raw)
            return None if _unset(value) else value.strip()

        block = compiled.get(name) or {}
        if "comment" in params:
            comment = text(params["comment"])
        else:  # the compiled Comment is the base part's
            comment = None if library else (block.get("comment") or None)
        mpn, manufacturer, source = part_numbers(params)
        mpn, manufacturer = text(mpn), text(manufacturer)
        placeholder = None
        if mpn and mpn.lower() in _NO_MPN:
            placeholder, mpn, source = mpn, None, None
        if manufacturer and manufacturer.lower() in _NO_MPN:
            manufacturer = None
        # A variant can vary the footprint and description, which are not parameters.
        if "description" in varied:
            description = text(params["description"])
        elif library:
            description = library.description or text(params.get("description"))
        else:
            description = meta.get("description") or text(params.get("description"))
        footprint = (
            text(params["footprint"]) if "footprint" in varied
            else alternate.get("footprint") or block.get("footprint") or meta.get("footprint")
        )
        part = BomPart(
            designator=name,
            kind=kind,
            comment=comment,
            value=text(params.get("value")),
            description=description,
            footprint=footprint,
            mpn=mpn,
            manufacturer=manufacturer,
            mpn_source=source,
            fitted=name not in plan.not_fitted,
            logical=meta.get("logical_refdes"),
            sheet=meta.get("sheet"),
            lib_reference=(alternate_lib_reference(alternate.get("variation"))
                           or meta.get("lib_reference") or ""),
            overrides=overrides,
            unresolved=unresolved,
            mpn_placeholder=placeholder,
            alternate_part=alternate.get("part"),
            alternate_library=library is not None,
            parameters=params,
        )
        parts.append(part)
    parts.sort(key=lambda part: natural_key(part.designator))
    return parts, excluded


def _fold(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).lower()


def group_key(part: BomPart, group_by: str) -> tuple:
    """MPN (with manufacturer) when the part has one, else Comment and footprint. This
    reproduced the grouping of 37 of 41 BOMs exported from real projects; 'comment'
    groups by Comment and footprint only, close to Altium's default."""
    if group_by == "mpn" and part.mpn:
        return ("mpn", _fold(part.mpn), _fold(part.manufacturer))
    return ("comment", _fold(part.comment or part.value), _fold(part.footprint))


_LINE_FIELDS = ("comment", "description", "value", "footprint", "manufacturer", "mpn")


def group_lines(parts: list[BomPart], group_by: str = "mpn") -> list[dict]:
    """BOM lines: quantity, designators (natural order) and the members' fields; a
    field the members disagree on is listed in 'varies' with its values (null where a
    member has none; the first five, with varies_count when there are more).
    logical_designators and alternate_parts map a member's designator to its sheet
    designator and to the variant's alternate part."""
    groups: dict[tuple, list[BomPart]] = {}
    for part in parts:
        groups.setdefault(group_key(part, group_by), []).append(part)
    lines = []
    for members in groups.values():
        first = members[0]
        line: dict = {
            "quantity": len(members),
            "designators": [part.designator for part in members],
        }
        varies: dict[str, list] = {}
        for name in _LINE_FIELDS:
            values = [getattr(part, name) for part in members]
            present = [value for value in values if value]
            if present:
                line[name] = present[0]
            distinct: dict[str, str | None] = {}
            for value in values:
                distinct.setdefault(_fold(value), value or None)
            if len(distinct) > 1:
                varies[name] = list(distinct.values())[:_VARIES_SHOWN]
                if len(distinct) > _VARIES_SHOWN:
                    line.setdefault("varies_count", {})[name] = len(distinct)
        placeholders = list(dict.fromkeys(
            part.mpn_placeholder for part in members if part.mpn_placeholder
        ))
        if placeholders and "mpn" not in line:
            line["mpn_placeholder"] = " / ".join(placeholders)
        if first.mpn_source and first.mpn_source != "parameter":
            line["mpn_source"] = first.mpn_source
        if varies:
            line["varies"] = varies
        logical = {part.designator: part.logical for part in members
                   if part.logical and part.logical != part.designator}
        if logical:
            line["logical_designators"] = logical
        alternates = {part.designator: part.alternate_part for part in members
                      if part.alternate_part}
        if alternates:
            line["alternate_parts"] = alternates
        overridden = sorted({key for part in members for key in part.overrides})
        if overridden:
            line["varied_parameters"] = overridden
        lines.append(line)
    lines.sort(key=lambda line: natural_key(line["designators"][0]))
    return lines


def compare_variants(
    netlist: dict, variants: list, names: list[str], project_params: dict | None = None,
) -> list[dict]:
    """What each variant changes against the BOM with no variations: its BOM parts not
    fitted, values changed, and alternate parts."""
    base, _ = build_parts(netlist, project_params=project_params)
    by_name = {part.designator: part for part in base}
    out = []
    for variant in variants:
        plan = plan_variant(variant, netlist, names)
        parts, _ = build_parts(netlist, plan, project_params)
        changed = []
        for part in parts:
            before = by_name.get(part.designator)
            if not part.fitted or before is None:
                continue
            fields: dict = {
                name: {"from": getattr(before, name), "to": getattr(part, name)}
                for name in _LINE_FIELDS
                if _fold(getattr(before, name)) != _fold(getattr(part, name))
            }
            if part.alternate_part:
                fields["alternate_part"] = part.alternate_part
            if fields:
                changed.append({"designator": part.designator, **fields})
        out.append({
            "variant": variant.name,
            "not_fitted": [part.designator for part in parts if not part.fitted],
            "changed": changed,
        })
    return out


CSV_COLUMNS = [
    ("Comment", "comment"), ("Description", "description"), ("Designator", "designators"),
    ("Footprint", "footprint"), ("Quantity", "quantity"), ("Manufacturer", "manufacturer"),
    ("Manufacturer Part Number", "mpn"), ("Value", "value"),
]


def _cell(value) -> str:
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(_FORMULA_START) else text


def to_csv(lines: list[dict], extra_columns: tuple = ()) -> str:
    """The lines as CSV. A field the members disagree on lists its values with ' / '
    ('(none)' for members without one, '...' when more were left out); a cell that
    would start a spreadsheet formula ('=', '+', '-', '@') gets a leading apostrophe.
    extra_columns ((title, line key), ...) follow the usual ones; a list value is joined
    with ', '."""
    columns = [*CSV_COLUMNS, *extra_columns]
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow([title for title, _ in columns])
    for line in lines:
        varies = line.get("varies") or {}
        row = []
        for _, key in columns:
            if key == "designators" or isinstance(line.get(key), list):
                value = ", ".join(str(item) if item is not None else "" for item in line[key])
            elif key in varies:
                shown = [value or "(none)" for value in varies[key]]
                if key in (line.get("varies_count") or {}):
                    shown.append("...")
                value = " / ".join(shown)
            elif key == "mpn":
                value = line.get("mpn") or line.get("mpn_placeholder", "")
            else:
                value = line.get(key, "")
            row.append(_cell(value))
        writer.writerow(row)
    return out.getvalue()
