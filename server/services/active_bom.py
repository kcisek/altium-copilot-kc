"""ActiveBOM read offline: the project's .BomDoc joined to the compiled parts of a variant.

What a BomDoc holds (parsers/bom_doc.py): one CatalogItem per BOM line (its line number,
Design Item ID, library source, BOM-check status, notes) with the supply data ActiveBOM
cached when it last refreshed: the part choices (manufacturer, MPN, supplier, SKU,
stock, price breaks, lifecycle, the date that data was fetched) and the designer's ranked
Solution records; plus the document's production quantity, currency, favourite
suppliers and export options. It holds no designators and no variant: the parts of a
line come from the design.

The join (validated on 56 projects' BomDocs and the 52 BOMs their OutJobs exported from
ActiveBOM): a line covers the compiled BOM parts whose Design Item ID equals its
DESIGNITEMID (807 of 830 exported base-view rows hold exactly one line's fitted parts;
flat-view exports list one designator per row). When one Design Item ID has several
lines (ActiveBOM splits a design item whose parts differ in Comment, Value, Footprint or
other parameters), COMPONENTPARAMETERS, the values the line was made for, picks the line
whose values agree best with the part's (516 of 538 parts picked one line; 56 of 59
exported rows agreed). A Workspace item saved under another revision (CMP-...-1 against
-2: a 1-2 digit suffix after an item number of 3 or more digits) matches with
other_revision set, and check_bom reports it as out of sync. The variant's Not Fitted
parts and parameter variations are applied as get_bom applies them; an alternate part
joins by its own Design Item ID. A line left without parts says why (no part has its ID,
only parts outside the BOM have it, a split line whose parts went to its sibling, or the
variant's alternate replaces them).

Solutions: the part choices grouped by manufacturer and MPN, ranked by their RANK (a
Solution record's RANK overrides it; the higher rank first, then file order). On lines
with part choices the exported MPN was the first solution's in 386 of 389 rows; on lines
without, the parts' MPN parameter's (279 of 306 rows, mostly empty otherwise), so such a
line gets its parts' MPN as a component_parameters solution. Higher rank first is read
from Altium's examples, where the designer's Solution records carry rank 5 against the
automatic part choices' 3; no exported BOM had two solutions of different ranks to
confirm it. Supplier order within a solution is the file's (pinned first). Stock, prices
and lifecycle are the cache's: every value comes with its cache date (LASTUPDATEDAT,
local time as saved). A part choice with no supplier or SKU is a manufacturer part only:
it caches no stock (its AVAILABLEQUANTITY=0 is a default). Prices: the lowest price of
the breaks the need reaches (a choice can hold several price tables back to back); under
the first break the minimum order is bought (extended_price), and a solution's best price
comes from offers the need can buy unless none can. No currency conversion and no total
BOM cost.

Pure logic, no MCP."""

import os
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

try:
    from parsers.bom_doc import (
        BomDoc, CatalogItem, PartChoice, parse_bom_doc, split_list, to_bool,
        to_float, to_int,
    )
    from parsers.prj_pcb import parse_prj_documents
    from services.bom import BomPart, natural_key, needs_mpn, norm_name
    from services.bom_checks import normalize_value
except ImportError:
    from server.parsers.bom_doc import (
        BomDoc, CatalogItem, PartChoice, parse_bom_doc, split_list, to_bool,
        to_float, to_int,
    )
    from server.parsers.prj_pcb import parse_prj_documents
    from server.services.bom import BomPart, natural_key, needs_mpn, norm_name
    from server.services.bom_checks import normalize_value

SECTIONS = ("header", "lines", "solutions", "non_pcb", "join")
DEFAULT_SECTIONS = ("header", "lines", "non_pcb", "join")
# (lines per page, price breaks shown per part choice), tried in order until a page fits.
SHRINK_STEPS = ((50, 8), (25, 4), (10, 2), (4, 1), (1, 0))
STALE_CACHE_DAYS = 365
SUPPLY_COVERAGE = 0.5  # below this share of lines with part choices, ActiveBOM supply is not used
JOIN_LISTED = 50       # designators / lines listed per join group
CACHE_SIZE = 8

# A Workspace item's Design Item ID with its revision: '<item ID ending in a number of 3 or
# more digits>-<revision of 1 or 2 digits>' (CMP-1234-00056-2). An ID without such a
# suffix (PART-0001-00045) has no revision to strip.
_REVISION = re.compile(r"^(.+-\d{3,})-\d{1,2}$")
# Lifecycle texts (LIFECYCLESTATUSID or LIFECYCLESTATUS, lower case) by category.
_LIFECYCLE = {
    "production": "production", "volume production": "production", "active": "production",
    "new": "new", "new product": "new", "preview": "new", "pre-release": "new",
    "nrnd": "nrnd", "not recommended for new design": "nrnd",
    "not recommended for new designs": "nrnd",
    "eol": "eol", "end of life": "eol",
    "ltb": "last_time_buy", "last time buy": "last_time_buy",
    "obsolete": "obsolete", "discontinued": "obsolete",
}
RISKY = ("nrnd", "eol", "last_time_buy", "obsolete")
_RISK_ORDER = {"obsolete": 0, "last_time_buy": 1, "eol": 2, "nrnd": 3}

NO_VARIANT_NOTE = (
    "A BomDoc stores no variant: its lines are joined to the parts of the variant shown "
    "(Not Fitted parts left out of the quantities, parameter variations applied).")
CACHE_NOTE = (
    "Stock, prices and lifecycle are what ActiveBOM cached on the dates shown (no supplier "
    "is queried): refresh the BomDoc in Altium for current data.")
# Why a line made from design parts has none in the variant shown.
ORPHAN_REASONS = {
    "no_part_has_id": "no part of the design has its Design Item ID (parts deleted or "
                      "changed since the BomDoc was saved)",
    "only_on_parts_not_in_bom": "its Design Item ID is only on parts that are not in the "
                                "BOM (compile-masked, not compiled, or Standard (No BOM))",
    "split_line_unused": "its Design Item ID's parts all matched another line of the same "
                         "item (ActiveBOM split the item by parameter values)",
    "replaced_by_alternate": "the variant replaces its parts with an alternate part",
}


def fold(text) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip()).lower()


def _key(text) -> str:
    """An identifier compared without case, blanks or punctuation."""
    return re.sub(r"[\W_]+", "", str(text or "").lower())


def revision_base(identifier: str) -> str | None:
    """The item ID of a Design Item ID that ends in a revision suffix, else None."""
    match = _REVISION.match(identifier)
    return match[1] if match else None


def lifecycle_category(choice: PartChoice) -> str | None:
    for text in (choice.lifecycle_id, choice.lifecycle):
        category = _LIFECYCLE.get(fold(text))
        if category:
            return category
    return "unknown" if (choice.lifecycle_id or choice.lifecycle) else None


def iso(moment: datetime | None) -> str | None:
    """A cache date as saved (local time, no zone), to the minute."""
    return moment.replace(microsecond=0).isoformat(timespec="minutes") if moment else None


# ---------------------------------------------------------------- finding the BomDoc

@dataclass
class BomDocChoice:
    listed: list[str] = field(default_factory=list)    # .BomDoc paths the project lists
    missing: list[str] = field(default_factory=list)   # listed but not on disk
    unlisted: list[str] = field(default_factory=list)  # in the project folder, not listed
    chosen: str | None = None
    chosen_by: str = ""
    in_project: bool = True
    error: dict | None = None

    def describe(self) -> dict:
        out: dict = {"file": Path(self.chosen).name if self.chosen else None,
                     "chosen_by": self.chosen_by, "in_project": self.in_project}
        others = [Path(path).name for path in self.listed if path != self.chosen]
        if others:
            out["other_bom_docs"] = others
        if self.unlisted and self.in_project:
            out["not_in_project"] = [Path(path).name for path in self.unlisted]
        if self.missing:
            out["missing_on_disk"] = [Path(path).name for path in self.missing]
        return out


def _same_name(path: str, name: str) -> bool:
    file = Path(path).name.lower()
    wanted = Path(name.replace("\\", "/")).name.lower()
    return file == wanted or Path(file).stem == wanted


def locate_bom_docs(project: dict, name: str | None = None, outjob_documents=None) -> BomDocChoice:
    """The BomDoc to read: name= (a listed one, else one in the project folder, read with
    in_project False), else the only one the project lists, else the one the OutJobs'
    BOM outputs name (outjob_documents(): their data sources as written), else the first
    listed. error when there is none."""
    choice = BomDocChoice()
    prj_path = project.get("prj_pcb_path")
    if not prj_path:
        choice.error = {"error": "no_bom_doc", "message": "No project file to read."}
        return choice
    folder = Path(prj_path).parent
    try:
        documents = parse_prj_documents(prj_path)
    except (OSError, UnicodeDecodeError) as error:
        choice.error = {"error": "project_file_unreadable",
                        "message": f"{Path(prj_path).name}: {type(error).__name__}: {error}"}
        return choice
    for document in documents:
        if document.raw.lower().endswith(".bomdoc"):
            (choice.listed if os.path.isfile(document.path) else choice.missing).append(
                document.path)
    listed_keys = {os.path.normcase(path) for path in choice.listed + choice.missing}
    try:
        choice.unlisted = sorted(
            str(path) for path in folder.iterdir()
            if path.suffix.lower() == ".bomdoc" and path.is_file()
            and os.path.normcase(str(path.resolve())) not in listed_keys)
    except OSError:
        choice.unlisted = []
    if name:
        found = [path for path in choice.listed if _same_name(path, name)]
        if found:
            choice.chosen, choice.chosen_by = found[0], "name"
            return choice
        found = [path for path in choice.unlisted if _same_name(path, name)]
        if not found and name.lower().endswith(".bomdoc"):
            candidate = Path(name) if Path(name).is_absolute() else folder / name
            if candidate.is_file():
                found = [str(candidate)]
        if found:
            choice.chosen, choice.chosen_by, choice.in_project = found[0], "name", False
            return choice
        choice.error = {
            "error": "bom_doc_not_found",
            "message": f"No BomDoc named '{name}'. The project lists "
            + (", ".join(Path(p).name for p in choice.listed) or "none")
            + (f"; in its folder but not in the project: "
               f"{', '.join(Path(p).name for p in choice.unlisted)}" if choice.unlisted else "")
            + ".",
        }
        return choice
    if len(choice.listed) == 1:
        choice.chosen, choice.chosen_by = choice.listed[0], "the project's only BomDoc"
        return choice
    if len(choice.listed) > 1:
        named = []
        if outjob_documents is not None:
            try:
                named = [doc for doc in outjob_documents() if doc.lower().endswith(".bomdoc")]
            except Exception:  # the OutJobs only break a tie
                named = []
        for document in named:
            found = [path for path in choice.listed if _same_name(path, document)]
            if found:
                choice.chosen, choice.chosen_by = found[0], "the BomDoc the OutJob's BOM uses"
                return choice
        choice.chosen, choice.chosen_by = choice.listed[0], "the first BomDoc the project lists"
        return choice
    message = "The project lists no ActiveBOM document (.BomDoc)"
    if choice.missing:
        message = (f"The project lists {', '.join(Path(p).name for p in choice.missing)}, "
                   "which is not on disk")
    if choice.unlisted:
        message += (f"; {', '.join(Path(p).name for p in choice.unlisted)} is in its folder "
                    "but not in the project (pass name= to read it anyway)")
    choice.error = {"error": "no_bom_doc", "message": message + ".",
                    "hint": "In Altium, File > New > ActiveBOM Document adds one to the "
                            "project; the BOM tools (get_bom, check_bom) work without it."}
    return choice


_parsed: dict[tuple, BomDoc] = {}


def load_bom_doc(path: str) -> BomDoc:
    """parse_bom_doc, cached per file size and time. Raises OSError or BomDocError."""
    stat = os.stat(path)
    key = (os.path.normcase(os.path.abspath(path)), stat.st_size, stat.st_mtime_ns)
    doc = _parsed.get(key)
    if doc is None:
        doc = parse_bom_doc(path)
        if len(_parsed) >= CACHE_SIZE:
            _parsed.pop(next(iter(_parsed)))
        _parsed[key] = doc
    return doc


# ---------------------------------------------------------------- the model

@dataclass(eq=False)  # compared by identity
class ChoiceView:
    choice: PartChoice
    unit_price: float | None = None   # at the needed quantity (production need)
    break_quantity: float | None = None
    below_first_break: bool = False   # the need is below the smallest price break
    enough_stock: bool | None = None  # stock >= need (None: no need or no stock known)
    # unit price x the quantity bought: the need, or the first break's quantity (the
    # minimum order) when the need is below it.
    extended_price: float | None = None


@dataclass(eq=False)  # compared by identity
class SolutionView:
    manufacturer: str
    mpn: str
    rank: int | None = None
    source: str = "part_choices"      # part_choices | solution_record | component_parameters
    choices: list[ChoiceView] = field(default_factory=list)
    ranked_by_designer: bool = False  # a Solution record set its rank

    @property
    def lifecycle(self) -> tuple[str | None, str | None]:
        """(worst category, its text as written) over the solution's part choices."""
        worst, text = None, None
        for view in self.choices:
            category = lifecycle_category(view.choice)
            if category is None:
                continue
            rank = _RISK_ORDER.get(category, 9)
            if worst is None or rank < _RISK_ORDER.get(worst, 9):
                worst = category
                text = view.choice.lifecycle_id or view.choice.lifecycle
        return worst, text

    def lifecycle_evidence(self) -> tuple[list[datetime], list[str]]:
        """(cache dates of the part choices that report the worst lifecycle, the other
        lifecycle texts the solution's other part choices report)."""
        worst, _text = self.lifecycle
        dates, others = [], []
        for view in self.choices:
            category = lifecycle_category(view.choice)
            if category is None:
                continue
            if category == worst:
                if view.choice.last_updated:
                    dates.append(view.choice.last_updated)
            else:
                text = view.choice.lifecycle_id or view.choice.lifecycle
                if text not in others:
                    others.append(text)
        return dates, others

    @property
    def dates(self) -> list[datetime]:
        return [view.choice.last_updated for view in self.choices if view.choice.last_updated]

    @property
    def offers(self) -> list[ChoiceView]:
        """The part choices that are a supplier's offer (supplier or SKU)."""
        return [view for view in self.choices if view.choice.has_supplier]


@dataclass(eq=False)  # compared by identity
class Line:
    item: CatalogItem
    index: int                                  # position in the file
    members: list[BomPart] = field(default_factory=list)
    basis: str = ""                             # design_item_id | other_revision | ...
    ambiguous: list[str] = field(default_factory=list)  # designators picked by a tie
    solutions: list[SolutionView] = field(default_factory=list)
    # A line without parts: ORPHAN_REASONS key
    orphan_reason: str = ""
    # '#<position in the file>' when the line's number (or, unnumbered, its Design Item ID)
    # also names another line: what line= takes to name this one.
    ref: str = ""

    @property
    def fitted(self) -> list[BomPart]:
        return [part for part in self.members if part.fitted]

    @property
    def not_fitted(self) -> list[BomPart]:
        return [part for part in self.members if not part.fitted]

    @property
    def quantity(self) -> float | None:
        """Per board: the fitted parts; a BOM item added in the BomDoc: its stored quantity
        when one of its fields holds it."""
        if self.item.automatic:
            return len(self.fitted)
        for key in ("QUANTITY", "QTY"):
            if key in self.item.extra:
                return to_float(self.item.extra[key])
        for name, value in self.item.custom_parameters.items():
            if norm_name(name) in ("quantity", "qty"):
                return to_float(value)
        return None

    @property
    def dates(self) -> list[datetime]:
        return [moment for solution in self.solutions for moment in solution.dates]

    @property
    def label(self) -> str:
        """What names the line to get_active_bom's line=: its number, else its Design
        Item ID; its ref ('#<position in the file>') when that names another line too."""
        if self.ref:
            return self.ref
        return self.plain_label

    @property
    def plain_label(self) -> str:
        if self.item.line_number is not None:
            return str(self.item.line_number)
        return self.item.design_item_id or self.item.line_number_text or f"#{self.index + 1}"

    @property
    def title(self) -> str:
        """'Line 10', or 'Unnumbered line <Design Item ID>' for a line ActiveBOM has not
        numbered; with '(#<n> in the file)' when that names another line too."""
        if self.item.line_number is not None:
            text = f"Line {self.item.line_number}"
        elif self.item.design_item_id:
            text = f"Unnumbered line {self.item.design_item_id}"
        else:
            return f"Unnumbered line {self.index + 1} of the file"
        return f"{text} ({self.ref} in the file)" if self.ref else text


@dataclass(eq=False)  # compared by identity
class ActiveBom:
    doc: BomDoc
    path: str
    variant: str
    lines: list[Line]                  # lines made from design parts (automatic)
    non_pcb: list[Line]                # BOM items added in the BomDoc
    orphans: list[Line]                # automatic lines whose parts are not in the design
    parts_without_line: list[BomPart]
    production_quantity: int
    currency: str
    now: datetime
    choice: BomDocChoice | None = None
    # Where production_quantity comes from: the BomDoc's, the caller's (another number of
    # boards), or 1 when the BomDoc gives none.
    production_from: str = "bom_doc"

    @property
    def all_lines(self) -> list[Line]:
        return sorted(self.lines + self.non_pcb + self.orphans, key=_line_order)

    def line_of(self) -> dict[str, Line]:
        """Line by designator (upper case) of each joined part."""
        return {part.designator.upper(): line for line in self.lines for part in line.members}

    def dates(self) -> list[datetime]:
        return [moment for line in self.all_lines for moment in line.dates]

    def duplicate_numbers(self) -> dict[int, list[Line]]:
        """Line numbers the BomDoc gives to more than one line (line number -> lines)."""
        by_number: dict[int, list[Line]] = {}
        for line in self.all_lines:
            if line.item.line_number is not None:
                by_number.setdefault(line.item.line_number, []).append(line)
        return {number: lines for number, lines in by_number.items() if len(lines) > 1}


def _line_order(line: Line):
    number = line.item.line_number
    return (number is None, number if number is not None else 0, line.index)


def _part_values(part: BomPart) -> dict[str, tuple[str, str | None]]:
    """A part's values by normalised parameter name: (as the BOM shows it, the parameter's
    raw text); Comment, Description, Footprint and Value as get_bom reads them."""
    values: dict[str, tuple[str, str | None]] = {}
    for parameter, text in part.parameters.items():
        values.setdefault(norm_name(parameter), (text, text))
    for key in ("comment", "description", "footprint", "value"):
        shown = getattr(part, key)
        if shown is not None or key != "value":
            values[key] = (shown, part.parameters.get(key))
    return values


def _score(wanted: list[tuple[str, str]], values: dict) -> int:
    """How well a line's COMPONENTPARAMETERS (normalised name, folded value) fit a part's
    values: +1 per equal value, -1 per different one (a parameter the part lacks counts
    neither way)."""
    score = 0
    for key, value in wanted:
        got = values.get(key)
        if got is None or got[0] is None:
            continue
        same = fold(got[0]) == value or (got[1] is not None and fold(got[1]) == value)
        score += 1 if same else -1
    return score


def design_item_of(part: BomPart, components: dict, plan=None) -> str:
    """The Design Item ID a part is placed from: an alternate part's own, else the
    component's (its library reference when it has none)."""
    alternate = (getattr(plan, "alternates", None) or {}).get(part.designator)
    if alternate:
        variation = alternate.get("variation")
        identity = variation.alternate_identity() if variation is not None else {}
        library = getattr(variation, "library_part", None)
        return (identity.get("design_item_id") or getattr(library, "design_item_id", "")
                or alternate.get("part") or part.lib_reference or "")
    meta = components.get(part.designator) or {}
    return meta.get("design_item_id") or meta.get("lib_reference") or part.lib_reference or ""


def _solutions(item: CatalogItem, members: list[BomPart]) -> list[SolutionView]:
    groups: dict[tuple[str, str], SolutionView] = {}
    for choice in item.part_choices:
        key = (_key(choice.manufacturer), _key(choice.mpn))
        view = groups.get(key)
        if view is None:
            view = groups[key] = SolutionView(choice.manufacturer, choice.mpn, choice.rank)
        elif choice.rank is not None and (view.rank is None or choice.rank > view.rank):
            view.rank = choice.rank
        view.choices.append(ChoiceView(choice))
    for record in item.solutions:
        key = (_key(record.manufacturer), _key(record.mpn))
        view = groups.get(key)
        if view is None:
            view = groups[key] = SolutionView(record.manufacturer, record.mpn, record.rank,
                                              source="solution_record")
        if record.rank is not None:
            view.rank = record.rank
        view.ranked_by_designer = True
    ordered = sorted(groups.values(), key=lambda view: -(view.rank if view.rank is not None
                                                          else -1))
    for view in ordered:
        # Pinned part choices first (by priority), then the file's order.
        view.choices.sort(key=lambda c: (not c.choice.pinned,
                                         c.choice.priority if c.choice.priority is not None
                                         else 1 << 30))
    if not ordered:
        seen = set()
        for part in members:
            if part.mpn and (_key(part.manufacturer), _key(part.mpn)) not in seen:
                seen.add((_key(part.manufacturer), _key(part.mpn)))
                ordered.append(SolutionView(part.manufacturer or "", part.mpn,
                                            source="component_parameters"))
    return ordered


def price_at(choice: PartChoice, need: float | None) -> tuple[float | None, float | None, bool]:
    """(unit price, the price break's quantity, below the first break) for buying need
    parts: the lowest price of the breaks whose quantity is not above need (a choice can
    hold several suppliers' tables back to back); below the first break, that break's
    price (the minimum order). BESTUNITPRICE when there are no breaks."""
    breaks = choice.price_breaks
    if not breaks:
        price = choice.best_unit_price
        return (price if price else None), None, False
    if need is None or need <= 0:
        return breaks[0].price, breaks[0].quantity, False
    usable = [entry for entry in breaks if entry.quantity <= need]
    if not usable:
        return breaks[0].price, breaks[0].quantity, True
    best = min(usable, key=lambda entry: (entry.price, -entry.quantity))
    return best.price, best.quantity, False


def _evaluate(line: Line, production_quantity: int) -> None:
    quantity = line.quantity
    need = quantity * production_quantity if quantity is not None else None
    for solution in line.solutions:
        for view in solution.choices:
            view.unit_price, view.break_quantity, view.below_first_break = price_at(
                view.choice, need)
            if view.unit_price is not None and need:
                bought = view.break_quantity if view.below_first_break else need
                view.extended_price = round(view.unit_price * bought, 6)
            if need is not None and view.choice.stock is not None:
                view.enough_stock = view.choice.stock >= need


def join(doc: BomDoc, parts: list[BomPart], components: dict, plan=None
         ) -> tuple[list[Line], list[Line], list[Line], list[BomPart]]:
    """(design lines, non-PCB lines, orphan lines, parts without a line) of a BomDoc and
    the variant's BOM parts (build_parts: fitted or not)."""
    lines = [Line(item, index) for index, item in enumerate(doc.items)]
    by_id: dict[str, list[Line]] = {}
    by_base: dict[str, list[Line]] = {}
    for line in lines:
        if not line.item.automatic:
            continue
        identifier = line.item.design_item_id.strip().lower()
        by_id.setdefault(identifier, []).append(line)
        base = revision_base(identifier) if "vault" in line.item.item_type.lower() else None
        if base:
            by_base.setdefault(base, []).append(line)
    without: list[BomPart] = []
    wanted: dict[int, list[tuple[str, str]]] = {}

    def wanted_of(line: Line) -> list[tuple[str, str]]:
        if line.index not in wanted:
            wanted[line.index] = [(norm_name(name), fold(value)) for name, value
                                  in line.item.component_parameters.items()]
        return wanted[line.index]

    bom_ids: set[str] = set()
    for part in parts:
        identifier = design_item_of(part, components, plan).strip().lower()
        bom_ids.add(identifier)
        candidates = by_id.get(identifier) if identifier else None
        basis = "design_item_id"
        base = revision_base(identifier) if identifier and not candidates else None
        if base:
            candidates = by_base.get(base)
            basis = "other_revision"
            if candidates:
                bom_ids.update(line.item.design_item_id.strip().lower() for line in candidates)
        if not candidates:
            without.append(part)
            continue
        target = candidates[0]
        if len(candidates) > 1:
            values = _part_values(part)
            scored = sorted(((_score(wanted_of(line), values), -line.index, line)
                             for line in candidates),
                            key=lambda entry: (entry[0], entry[1]), reverse=True)
            target = scored[0][2]
            basis = "component_parameters"
            if scored[0][0] == scored[1][0]:
                target.ambiguous.append(part.designator)
        target.members.append(part)
        if not target.basis or target.basis == "design_item_id":
            target.basis = basis
    # Design Item IDs of every component, BOM part or not (compile-masked, Standard (No
    # BOM), ...): why a line has no parts.
    def id_of(meta) -> str:
        return str(meta.get("design_item_id") or meta.get("lib_reference") or "").strip().lower()

    design_ids = {id_of(meta) for meta in components.values()}
    replaced = {id_of(components.get(designator) or {})
                for designator in (getattr(plan, "alternates", None) or {})}
    design, non_pcb, orphans = [], [], []
    for line in lines:
        line.members.sort(key=lambda part: natural_key(part.designator))
        line.solutions = _solutions(line.item, line.members)
        if not line.item.automatic:
            line.basis = "bom_item"
            non_pcb.append(line)
        elif line.members:
            design.append(line)
        else:
            line.basis = "no_parts"
            identifier = line.item.design_item_id.strip().lower()
            line.orphan_reason = (
                "split_line_unused" if identifier in bom_ids
                else "replaced_by_alternate" if identifier in replaced
                else "only_on_parts_not_in_bom" if identifier in design_ids
                else "no_part_has_id")
            orphans.append(line)
    return design, non_pcb, orphans, without


def build_active_bom(doc: BomDoc, parts: list[BomPart], netlist: dict, plan=None,
                     variant: str = "", now: datetime | None = None,
                     choice: BomDocChoice | None = None,
                     production_quantity: int | None = None) -> ActiveBom:
    """The ActiveBom model of a parsed BomDoc for a variant's BOM parts. production_quantity
    (boards) replaces the BomDoc's for the need, stock and prices."""
    design, non_pcb, orphans, without = join(doc, parts, netlist.get("components") or {}, plan)
    if production_quantity:
        production, source = production_quantity, "argument"
    elif doc.production_quantity and doc.production_quantity > 0:
        production, source = doc.production_quantity, "bom_doc"
    else:
        production, source = 1, "default_1"
    model = ActiveBom(doc=doc, path=doc.path, variant=variant, lines=design, non_pcb=non_pcb,
                      orphans=orphans, parts_without_line=without,
                      production_quantity=production, currency=doc.currency,
                      now=now or datetime.now(), choice=choice, production_from=source)
    for line in design + non_pcb + orphans:
        _evaluate(line, production)
    # A line that its number (or, unnumbered, its Design Item ID) does not name alone, as
    # find_lines looks them up, gets its position in the file as its ref.
    lines = model.all_lines
    numbers = Counter(line.item.line_number for line in lines
                      if line.item.line_number is not None)
    ids = Counter(fold(line.item.design_item_id) for line in lines)
    for line in lines:
        if line.item.line_number is not None:
            shared = numbers[line.item.line_number] > 1
        else:
            identifier = line.item.design_item_id
            shared = (not identifier or ids[fold(identifier)] > 1
                      or to_int(identifier) in numbers)
        if shared:
            line.ref = f"#{line.index + 1}"
    return model


# ---------------------------------------------------------------- rendering

def _num(value):
    if value is None:
        return None
    return int(value) if float(value).is_integer() else round(value, 6)


def header(model: ActiveBom) -> dict:
    doc = model.doc
    general = doc.options.get("GeneralOptions", {})
    favourites = doc.options.get("FavoriteSuppliers", {})
    out: dict = {
        "production_quantity": doc.production_quantity,
        "currency": doc.currency or None,
        "target_unit_price": _num(to_float(doc.header.get("PERUNITTARGETPRICE"))),
        "actual_unit_price": _num(to_float(doc.header.get("PERUNITACTUALPRICE"))),
        "saved_in_file": " ".join(part for part in (doc.header.get("DATE"),
                                                    doc.header.get("TIME")) if part) or None,
        "favorite_suppliers": [name for name, _ in split_list(favourites.get("SUPPLIERS", ""))],
        "use_supplier_order": to_bool(favourites.get("USESUPPLIERORDER")),
        "solutions_per_item": to_int(general.get("SOLUTIONSPERITEM")),
        "suppliers_per_solution": to_int(general.get("SUPPLIERSPERSOLUTION")),
        "exclude_risky_solutions": to_bool(general.get("FILTEROUTRISKYSOLUTIONS")),
        "custom_columns": [name for name, _ in split_list(
            doc.options.get("CustomColumns", {}).get("CUSTOMCOLUMNS", ""))],
        "variant_stored": False,
        "export_options": {
            "include_not_fitted": to_bool(general.get("NOTFITTED")),
            "designator_grouping": general.get("DESIGNATORGROUPING") or None,
            "template": general.get("TEMPLATEFILENAME") or None,
            "supplier_production_quantity": to_int(general.get("SUPPLIERPRODQTY")),
            "use_cached_pricing": to_bool(general.get("SUPPLIERUSECACHEDPRICING")),
            "supplier_currency": general.get("SUPPLIERCURRENCY") or None,
        },
    }
    sources = [name for name, _ in split_list(favourites.get("PARTSOURCES", ""))]
    if sources:
        out["part_sources"] = sources
    aliases = dict(split_list(doc.options.get("ColumnsAliases", {}).get("COLUMNSALIASES", "")))
    if aliases:
        out["column_aliases"] = aliases
    columns = [name for name, _ in split_list(
        doc.options.get("VisibleOrderOptions2", {}).get("BASEVIEW", ""))]
    if columns:
        out["columns"] = columns
    numbering = doc.options.get("LineNumberingOptions", {})
    if numbering.get("STARTVALUE") or numbering.get("INCREMENT"):
        out["line_numbering"] = {"start": to_int(numbering.get("STARTVALUE")),
                                 "increment": to_int(numbering.get("INCREMENT"))}
    levels = doc.options.get("CheckOptions", {}).get("CHECKLEVELS", "")
    if levels:
        out["bom_check_levels"] = levels
    grouped = doc.options.get("CheckGroupCompParamsOptions", {}).get("CHECKGROUPCOMPPARAMS")
    if grouped:
        out["grouping_parameters"] = dict(
            (name, value) for name, value in split_list(grouped, ","))
    if doc.other_records:
        out["unread_records"] = dict(Counter(record.kind for record in doc.other_records))
    out["note"] = NO_VARIANT_NOTE
    return {key: value for key, value in out.items() if value not in (None, [], {})}


def cache_summary(model: ActiveBom, stale_days: int = STALE_CACHE_DAYS) -> dict:
    dates = model.dates()
    total = sum(len(s.choices) for line in model.all_lines for s in line.solutions)
    out: dict = {"part_choices": total, "dated": len(dates)}
    if dates:
        oldest, newest = min(dates), max(dates)
        out.update({
            "oldest": iso(oldest), "newest": iso(newest),
            "oldest_age_days": max(0, (model.now - oldest).days),
            "newest_age_days": max(0, (model.now - newest).days),
            f"older_than_{stale_days}_days": sum(
                1 for moment in dates if (model.now - moment).days > stale_days),
        })
    out["note"] = CACHE_NOTE
    return out


def _choice_row(view: ChoiceView, currency: str, breaks_shown: int) -> dict:
    choice = view.choice
    row: dict = {
        "supplier": choice.supplier or None,
        "sku": choice.sku or None,
        "stock": choice.stock,
        "enough_stock": view.enough_stock,
        "unit_price": _num(view.unit_price),
        "currency": choice.currency or currency or None,
        "lifecycle": choice.lifecycle_id or choice.lifecycle or None,
        "cache_date": iso(choice.last_updated),
    }
    if view.extended_price is not None:
        row["extended_price"] = _num(view.extended_price)
    if view.break_quantity is not None:
        row["price_break_quantity"] = _num(view.break_quantity)
    if view.below_first_break:
        # The need is under the smallest break: extended_price buys that break's quantity.
        row["below_minimum_order"] = True
    if not choice.has_supplier:
        row["no_supplier"] = True
    if choice.pinned:
        row["pinned"] = True
    if breaks_shown and choice.price_breaks:
        row["price_breaks"] = [[_num(entry.quantity), _num(entry.price)]
                               for entry in choice.price_breaks[:breaks_shown]]
        if len(choice.price_breaks) > breaks_shown:
            row["price_breaks_total"] = len(choice.price_breaks)
    if not choice.last_updated and choice.last_updated_text:
        row["cache_date_text"] = choice.last_updated_text
    return {key: value for key, value in row.items() if value is not None}


def _solution_row(solution: SolutionView, line: Line, model: ActiveBom, breaks_shown: int,
                  with_choices: bool) -> dict:
    category, text = solution.lifecycle
    row: dict = {"rank": solution.rank, "manufacturer": solution.manufacturer or None,
                 "mpn": solution.mpn or None, "source": solution.source}
    if text:
        row["lifecycle"] = text
        if category in RISKY:
            row["lifecycle_risk"] = category
    if solution.ranked_by_designer:
        row["ranked_by_designer"] = True
    if solution.choices:
        dates = solution.dates
        if dates:
            row["cache_date"] = iso(max(dates))
        # One supplier's SKUs of a part (cut tape, reel...) share its stock: counted once.
        by_supplier: dict[str, int] = {}
        for view in solution.offers:
            if view.choice.stock is not None:
                key = _key(view.choice.supplier) or _key(view.choice.sku)
                by_supplier[key] = max(by_supplier.get(key, 0), view.choice.stock)
        if by_supplier:
            row["stock_total"] = sum(by_supplier.values())
        priced = [view for view in solution.choices if view.unit_price is not None]
        # The best price a buyer of the need can pay: from an offer whose first break the
        # need reaches; only when none does, the lowest minimum-order price (said so).
        orderable = [view for view in priced if not view.below_first_break]
        if priced:
            best = min(orderable or priced, key=lambda view: view.unit_price)
            row["best_unit_price"] = _num(best.unit_price)
            row["best_price_supplier"] = best.choice.supplier or None
            row["currency"] = best.choice.currency or model.currency or None
            if not orderable:
                row["best_price_below_minimum_order"] = True
                row["minimum_order_quantity"] = _num(best.break_quantity)
        row["suppliers"] = len({_key(view.choice.supplier) or _key(view.choice.sku)
                                for view in solution.offers})
        if with_choices:
            row["part_choices"] = [_choice_row(view, model.currency, breaks_shown)
                                   for view in solution.choices]
    return {key: value for key, value in row.items() if value is not None}


def line_row(line: Line, model: ActiveBom, with_solutions: bool, breaks_shown: int) -> dict:
    item = line.item
    quantity = line.quantity
    row: dict = {
        "line": item.line_number if item.line_number is not None else None,
        "design_item_id": item.design_item_id or None,
        "source": item.item_source or None,
        "item_type": item.item_type or None,
        "description": item.description or None,
        "comment": item.user_comments or None,
        "note": item.note or None,
        "status": item.status or None,
        "designators": [part.designator for part in line.fitted],
        "quantity": _num(quantity) if quantity is not None else None,
    }
    if item.line_number is None:
        row["line"] = None
        row["line_unnumbered"] = True
    if line.ref:
        # The number (or Design Item ID) names another line too: line= takes this.
        row["ref"] = line.ref
    if quantity is not None:
        row["quantity_at_production"] = _num(quantity * model.production_quantity)
    if line.not_fitted:
        row["not_fitted"] = [part.designator for part in line.not_fitted]
    if line.basis and line.basis != "design_item_id":
        row["join"] = line.basis
    if line.orphan_reason:
        row["why_no_parts"] = ORPHAN_REASONS[line.orphan_reason]
    if line.ambiguous:
        row["ambiguous_designators"] = line.ambiguous
    if item.custom_parameters:
        row["custom_parameters"] = item.custom_parameters
    if line.solutions:
        primary = line.solutions[0]
        row["chosen"] = _solution_row(primary, line, model, 0, False)
        row["solutions"] = len(line.solutions)
        if with_solutions:
            row["ranked_solutions"] = [_solution_row(solution, line, model, breaks_shown, True)
                                       for solution in line.solutions]
    else:
        row["solutions"] = 0
    dates = line.dates
    if dates:
        row["cache_dates"] = {"oldest": iso(min(dates)), "newest": iso(max(dates))}
    if item.other_records:
        row["unread_records"] = dict(Counter(record.kind for record in item.other_records))
    return {key: value for key, value in row.items()
            if value is not None or key in ("line", "quantity")}


def join_report(model: ActiveBom) -> dict:
    lines = model.lines
    report: dict = {
        "basis": ("A line covers the compiled BOM parts whose Design Item ID is its "
                  "DESIGNITEMID; several lines of one Design Item ID are told apart by the "
                  "values in their COMPONENTPARAMETERS."),
        "parts_joined": sum(len(line.members) for line in lines),
        "lines_with_parts": len(lines),
    }
    without = model.parts_without_line
    if without:
        report["parts_without_line"] = {
            "count": len(without),
            "designators": [part.designator for part in without][:JOIN_LISTED],
            "why": "No line has their Design Item ID: the BomDoc was saved before they were "
                   "added or changed (Altium refreshes its lines when the BomDoc is opened; "
                   "save it there).",
        }
    if model.orphans:
        report["lines_without_parts"] = [
            {"line": line.item.line_number, "design_item_id": line.item.design_item_id,
             "description": line.item.description or None, "solutions": len(line.solutions),
             "why": line.orphan_reason}
            for line in model.orphans][:JOIN_LISTED]
        report["lines_without_parts_count"] = len(model.orphans)
        report["lines_without_parts_why"] = {
            reason: ORPHAN_REASONS[reason] for reason in dict.fromkeys(
                line.orphan_reason for line in model.orphans)}
    revisions = [line for line in lines if line.basis == "other_revision"]
    if revisions:
        report["other_revision"] = [
            {"line": line.item.line_number, "design_item_id": line.item.design_item_id,
             "designators": [part.designator for part in line.members][:JOIN_LISTED]}
            for line in revisions]
    ambiguous = [line for line in lines if line.ambiguous]
    if ambiguous:
        report["ambiguous"] = [
            {"line": line.item.line_number, "design_item_id": line.item.design_item_id,
             "designators": line.ambiguous[:JOIN_LISTED]} for line in ambiguous]
    differs = mpn_differences(model)
    if differs:
        report["mpn_differs"] = differs[:JOIN_LISTED]
        report["mpn_differs_count"] = len(differs)
    duplicates = model.duplicate_numbers()
    if duplicates:
        report["duplicate_line_numbers"] = [
            {"line": number, "design_item_ids": [line.item.design_item_id for line in lines],
             "refs": [line.ref for line in lines]}
            for number, lines in sorted(duplicates.items())][:JOIN_LISTED]
        report["duplicate_line_numbers_note"] = (
            "The BomDoc gives these numbers to more than one line: line= picks one by its ref "
            "or a designator, and an exported BOM may number its lines otherwise.")
    return report


def mpn_differences(model: ActiveBom) -> list[dict]:
    """Lines whose chosen (first-ranked) BomDoc solution's MPN is not the MPN parameter of
    their fitted parts."""
    out = []
    for line in model.lines:
        if not line.solutions or line.solutions[0].source == "component_parameters":
            continue
        chosen = line.solutions[0]
        wrong = [part for part in line.fitted if part.mpn and _key(part.mpn) != _key(chosen.mpn)]
        if not wrong:
            continue
        mpns = list(dict.fromkeys(part.mpn for part in wrong))
        out.append({"line": line.item.line_number, "design_item_id": line.item.design_item_id,
                    "title": line.title, "designators": [p.designator for p in wrong],
                    "bom_doc_mpn": chosen.mpn, "bom_doc_manufacturer": chosen.manufacturer,
                    "part_mpn": mpns[0] if len(mpns) == 1 else mpns,
                    "alternate": any(_key(solution.mpn) == _key(mpns[0])
                                     for solution in line.solutions[1:])})
    return out


def summary(model: ActiveBom) -> dict:
    lines = model.all_lines
    lifecycle = Counter()
    for line in lines:
        for solution in line.solutions:
            for view in solution.choices:
                text = view.choice.lifecycle_id or view.choice.lifecycle
                if text:
                    lifecycle[text] += 1
    return {
        "lines": len(lines),
        "lines_with_parts": len(model.lines),
        "non_pcb_lines": len(model.non_pcb),
        "lines_without_parts": len(model.orphans),
        "parts_without_line": len(model.parts_without_line),
        "lines_numbered": sum(1 for line in lines if line.item.line_number is not None),
        "duplicate_line_numbers": len(model.duplicate_numbers()),
        "lines_with_part_choices": sum(1 for line in lines if line.item.part_choices),
        "solutions": sum(1 for line in lines for s in line.solutions
                         if s.source != "component_parameters"),
        "part_choices": sum(len(line.item.part_choices) for line in lines),
        "lifecycle": dict(lifecycle.most_common()),
    }


def parse_sections(value) -> tuple[list[str] | None, str | None]:
    """(sections, error) of a sections= argument: a list or a comma-separated text."""
    if value is None or value == "" or value == []:
        return list(DEFAULT_SECTIONS), None
    items = value if isinstance(value, (list, tuple)) else re.split(r"[,\s]+", str(value))
    wanted = [str(item).strip().lower() for item in items if str(item).strip()]
    unknown = [item for item in wanted if item not in SECTIONS]
    if unknown:
        return None, f"Unknown sections {unknown}; use {list(SECTIONS)}."
    return list(dict.fromkeys(wanted)), None


def find_lines(model: ActiveBom, wanted: str) -> list[Line]:
    """The lines a line= names: '#<n>' the n-th line of the file (a line's ref), else by
    number, else Design Item ID, else one of its designators. Several when the BomDoc
    gives one number to several lines or splits one Design Item ID into several lines
    (their refs or a designator then name one)."""
    text = str(wanted).strip()
    lines = model.all_lines
    position = re.fullmatch(r"#\s*(\d+)", text)
    if position:
        return [line for line in lines if line.index + 1 == int(position[1])]
    number = to_int(text)
    if number is not None:
        found = [line for line in lines if line.item.line_number == number]
        if found:
            return found
    found = [line for line in lines if fold(line.item.design_item_id) == fold(text)]
    if found:
        return found
    return [line for line in lines
            if any(part.designator.upper() == text.upper() for part in line.members)]


def find_line(model: ActiveBom, wanted: str) -> Line | None:
    """The first line find_lines gives, or None."""
    found = find_lines(model, wanted)
    return found[0] if found else None


def page(model: ActiveBom, sections: list[str], offset: int, limit: int, breaks_shown: int,
         line: Line | None = None) -> dict:
    """One page of get_active_bom: the header and summary, the cache dates, a page of
    lines (all solutions with sections 'solutions' or one line), the non-PCB lines and
    the join report."""
    out: dict = {"variant": model.variant}
    if model.choice is not None:
        out["bom_doc"] = model.choice.describe()
    else:
        out["bom_doc"] = {"file": Path(model.path).name}
    out["bom_doc"]["encoding"] = model.doc.encoding
    try:
        out["bom_doc"]["saved_utc"] = datetime.fromtimestamp(
            os.stat(model.path).st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OSError, ValueError):
        pass
    if model.choice is not None and not model.choice.in_project:
        out["bom_doc"]["note"] = ("This BomDoc is not in the project: Altium does not use it "
                                  "for the project's BOM.")
    out["summary"] = summary(model)
    out["production_quantity_used"] = model.production_quantity
    out["production_quantity_from"] = model.production_from
    out["cache"] = cache_summary(model)
    if "header" in sections:
        out["header"] = header(model)
    if line is not None:
        out["line"] = line_row(line, model, True, breaks_shown)
        return out
    with_solutions = "solutions" in sections
    if "lines" in sections or with_solutions:
        rows = sorted(model.lines + model.orphans, key=_line_order)
        offset = max(0, int(offset or 0))
        chunk = rows[offset:offset + limit]
        out["lines"] = [line_row(entry, model, with_solutions, breaks_shown) for entry in chunk]
        out["lines_total"] = len(rows)
        out["offset"] = offset
        out["has_more"] = offset + limit < len(rows)
        if out["has_more"]:
            out["next_offset"] = offset + limit
    if "non_pcb" in sections:
        out["non_pcb_lines"] = [line_row(entry, model, with_solutions, breaks_shown)
                                for entry in model.non_pcb]
        if not model.non_pcb:
            out["non_pcb_note"] = ("No BOM items were added in the BomDoc (bare board, "
                                   "screws, glue...): every line comes from design parts.")
    if "join" in sections:
        out["join"] = join_report(model)
    return out


# ---------------------------------------------------------------- check_bom

def _finding(check: str, severity: str, issue: str, designators=(), **fields) -> dict:
    out: dict = {"check": check, "severity": severity}
    if designators:
        out["designators"] = sorted(designators, key=natural_key)
    out.update({key: value for key, value in fields.items() if value is not None})
    out["issue"] = issue
    return out


def _date_text(dates: list[datetime]) -> str:
    if not dates:
        return "no cache date"
    return f"cached {iso(max(dates))[:10]}"


def _numbers_and_unnumbered(lines: list[Line]) -> dict:
    """A finding's lines field (the numbered lines) and unnumbered_lines (Design Item IDs of
    the others), without nulls."""
    out: dict = {"lines": [line.item.line_number for line in lines
                           if line.item.line_number is not None]}
    unnumbered = [line.label for line in lines if line.item.line_number is None]
    if unnumbered:
        out["unnumbered_lines"] = unnumbered
    return {key: value for key, value in out.items() if value}


def bom_doc_findings(model: ActiveBom, stale_days: int = STALE_CACHE_DAYS) -> list[dict]:
    """check_bom's BomDoc checks (BOMDOC_CHECKS) and the MPN checks applied to every ranked
    MPN (mpn_without_manufacturer, mpn_inconsistent). A finding about one line carries its
    line number (null when unnumbered) and design_item_id."""
    findings: list[dict] = []
    single: list[Line] = []
    lines = sorted([line for line in model.lines if line.fitted] + model.non_pcb,
                   key=_line_order)
    supplied = [line for line in lines if line.item.part_choices or line.item.solutions]
    coverage = len(supplied) / len(lines) if lines else 1.0
    # A BomDoc with no part choice at all keeps no supply data: missing_mpn says what
    # lacks a part number then.
    judged = [line for line in lines if not line.solutions and supplied and (
        not line.item.automatic or any(needs_mpn(part) for part in line.fitted))]
    for line in lines:
        designators = [part.designator for part in line.fitted]
        title = line.title
        where = {"line": line.item.line_number,
                 "design_item_id": line.item.design_item_id or None}
        solutions = [s for s in line.solutions if s.source != "component_parameters"]
        for rank, solution in enumerate(solutions):
            category, text = solution.lifecycle
            if category not in RISKY:
                continue
            chosen = rank == 0
            # The date of the part choices that report this status, not the solution's newest.
            dates, others = solution.lifecycle_evidence()
            findings.append(_finding(
                "lifecycle_risk", "warning" if chosen else "info",
                f"{title}: {'the chosen' if chosen else 'an alternate'} solution "
                f"{solution.manufacturer} {solution.mpn} is {text} ({_date_text(dates)})"
                + (f"; other suppliers report {', '.join(others)}" if others else "") + ".",
                designators, **where, mpn=solution.mpn,
                manufacturer=solution.manufacturer or None, lifecycle=text,
                cache_date=iso(max(dates)) if dates else None,
                other_lifecycles=others or None))
        if line in judged and coverage >= SUPPLY_COVERAGE:
            findings.append(_finding(
                "no_solution", "warning",
                f"{title} has no manufacturer part: no part choice in the BomDoc and "
                "no MPN parameter on its parts.", designators, **where))
        if solutions:
            primary = solutions[0]
            quantity = line.quantity
            need = quantity * model.production_quantity if quantity else None
            # Only suppliers' offers cache stock (a manufacturer-only choice has none).
            known = [view for view in primary.offers if view.choice.stock is not None]
            if need and known and not any(view.enough_stock for view in known):
                best = max(view.choice.stock for view in known)
                dates = [view.choice.last_updated for view in known if view.choice.last_updated]
                findings.append(_finding(
                    # Without a cache date the figure cannot be placed in time.
                    "out_of_stock_at_qty", "warning" if dates else "info",
                    f"{title}: no supplier of {primary.manufacturer} {primary.mpn} had "
                    f"{_num(need)} in stock (most: {best}; {_date_text(dates)}).",
                    designators, **where, mpn=primary.mpn,
                    need=_num(need), stock=best,
                    cache_date=iso(max(dates)) if dates else None))
        manufacturers = {_key(s.manufacturer) for s in solutions if s.manufacturer}
        for solution in solutions:
            if solution.mpn and not solution.manufacturer.strip():
                findings.append(_finding(
                    "mpn_without_manufacturer", "warning",
                    f"{title}: BomDoc solution {solution.mpn} has no manufacturer.",
                    designators, **where, mpn=solution.mpn, source="bom_doc"))
        if solutions and len(manufacturers) <= 1:
            single.append(line)
    if single:
        listed = single[:JOIN_LISTED]
        findings.append(_finding(
            "single_source", "info",
            f"{len(single)} of {len(supplied)} lines with supply data have one manufacturer "
            "solution: no approved alternate in the BomDoc.",
            [part.designator for line in listed for part in line.fitted][:JOIN_LISTED],
            **_numbers_and_unnumbered(listed), count=len(single)))
    if judged and coverage < SUPPLY_COVERAGE:
        findings.append(_finding(
            "no_solution", "info",
            f"Only {len(supplied)} of {len(lines)} lines have part choices in the BomDoc, so "
            f"its supply data is not kept up: {len(judged)} lines have neither a part choice "
            "nor an MPN parameter.",
            [part.designator for line in judged for part in line.fitted][:JOIN_LISTED],
            count=len(judged)))
    dates = model.dates()
    if dates:
        old = [moment for moment in dates if (model.now - moment).days > stale_days]
        if old:
            findings.append(_finding(
                "stale_cache", "info",
                f"{len(old)} of {len(dates)} part choices were cached more than {stale_days} "
                f"days ago (oldest {iso(min(dates))[:10]}, newest {iso(max(dates))[:10]}): "
                "refresh the BomDoc in Altium before trusting stock, price or lifecycle.",
                oldest=iso(min(dates)), newest=iso(max(dates)), count=len(old)))
    findings.extend(_sync_findings(model))
    findings.extend(_ranked_mpn_consistency(model))
    return findings


def _sync_findings(model: ActiveBom) -> list[dict]:
    """bomdoc_out_of_sync: parts without a line, lines without parts (per reason), lines
    joined to another revision of their item, and chosen MPNs that differ from the parts'
    MPN parameter."""
    findings = []
    if model.parts_without_line:
        findings.append(_finding(
            "bomdoc_out_of_sync", "info",
            f"{len(model.parts_without_line)} BOM parts have no line in the BomDoc (it was "
            "saved before they were added or changed): open and save the BomDoc in Altium.",
            [part.designator for part in model.parts_without_line][:JOIN_LISTED],
            reason="parts_without_line", count=len(model.parts_without_line)))
    by_reason: dict[str, list[Line]] = {}
    for line in model.orphans:
        by_reason.setdefault(line.orphan_reason, []).append(line)
    for reason, orphans in by_reason.items():
        findings.append(_finding(
            "bomdoc_out_of_sync", "info",
            f"{len(orphans)} BomDoc lines have no part in this variant: "
            f"{ORPHAN_REASONS[reason]}. Their part choices are unused.",
            reason="lines_without_parts", why=reason, count=len(orphans),
            **_numbers_and_unnumbered(orphans[:JOIN_LISTED]),
            design_item_ids=[line.item.design_item_id for line in orphans][:JOIN_LISTED]))
    revisions = [line for line in model.lines if line.basis == "other_revision"]
    if revisions:
        findings.append(_finding(
            "bomdoc_out_of_sync", "info",
            f"{len(revisions)} BomDoc lines hold another revision of their parts' Workspace "
            "item: their part choices were cached for that revision. Open and save the "
            "BomDoc in Altium to update the lines.",
            [part.designator for line in revisions for part in line.members][:JOIN_LISTED],
            reason="other_revision", count=len(revisions),
            **_numbers_and_unnumbered(revisions[:JOIN_LISTED]),
            design_item_ids=[line.item.design_item_id for line in revisions][:JOIN_LISTED]))
    for entry in mpn_differences(model):
        findings.append(_finding(
            "bomdoc_out_of_sync", "warning",
            f"{entry['title']}: the BomDoc's chosen part {entry['bom_doc_mpn']} is not the "
            f"parts' MPN parameter {entry['part_mpn']}"
            + (" (an alternate in the BomDoc)" if entry["alternate"] else "")
            + ": a BOM exported from ActiveBOM shows the BomDoc's.",
            entry["designators"], reason="mpn_differs", line=entry["line"],
            design_item_id=entry["design_item_id"] or None,
            mpn=entry["bom_doc_mpn"], part_mpn=entry["part_mpn"]))
    return findings


def _ranked_mpn_consistency(model: ActiveBom) -> list[dict]:
    """mpn_inconsistent over every ranked MPN: one MPN offered on lines whose parts have
    different values or footprints (run_bom_checks drops one the parameter check already
    reported for the same MPN and field)."""
    by_mpn: dict[str, list[Line]] = {}
    shown: dict[str, str] = {}
    for line in sorted(model.lines, key=_line_order):
        if not line.fitted:
            continue
        for solution in line.solutions:
            if solution.source == "component_parameters" or not solution.mpn:
                continue
            key = _key(solution.mpn)
            shown.setdefault(key, solution.mpn)
            if line not in by_mpn.setdefault(key, []):
                by_mpn[key].append(line)
    findings = []
    for key, lines in by_mpn.items():
        if len(lines) < 2:
            continue
        for name, normal in (("value", normalize_value), ("footprint", _key)):
            values = {normal(getattr(part, name)) for line in lines for part in line.fitted
                      if getattr(part, name)}
            if len(values) < 2:
                continue
            texts = sorted({getattr(part, name) for line in lines for part in line.fitted
                            if getattr(part, name)})
            findings.append(_finding(
                "mpn_inconsistent", "info" if name == "footprint" else "warning",
                f"{shown[key]} is a ranked solution on lines "
                f"{', '.join(str(line.label) for line in lines)} whose parts have different "
                f"{name}s: {', '.join(texts[:4])}.",
                [part.designator for line in lines for part in line.fitted][:JOIN_LISTED],
                mpn=shown[key], field=name, source="bom_doc",
                **_numbers_and_unnumbered(lines)))
    return findings


def covered_designators(model: ActiveBom) -> set[str]:
    """Designators (upper case) whose line has a BomDoc solution: they have an MPN even
    without an MPN parameter."""
    return {part.designator.upper() for line in model.lines
            if any(s.source != "component_parameters" for s in line.solutions)
            for part in line.members}


# ---------------------------------------------------------------- other tools

def export_rows(model: ActiveBom) -> dict[str, list[dict]]:
    """package_for_xfn's bom_solutions (one row per line, solution and part choice; a
    solution without part choices once) and bom_non_pcb_lines rows."""
    solutions: list[dict] = []
    for line in model.all_lines:
        for rank, solution in enumerate(line.solutions, start=1):
            category, text = solution.lifecycle
            base = {
                "line": line.item.line_number, "design_item_id": line.item.design_item_id,
                "designators": [part.designator for part in line.fitted],
                "position": rank, "rank": solution.rank,
                "manufacturer": solution.manufacturer or None, "mpn": solution.mpn or None,
                "source": solution.source, "solution_lifecycle": text,
            }
            if not solution.choices:
                solutions.append({**base, "supplier": None, "sku": None, "stock": None,
                                  "unit_price": None, "price_break_quantity": None,
                                  "below_minimum_order": None, "extended_price": None,
                                  "currency": None, "lifecycle": text, "cache_date": None})
                continue
            for view in solution.choices:
                choice = view.choice
                solutions.append({
                    **base, "supplier": choice.supplier or None, "sku": choice.sku or None,
                    "stock": choice.stock, "unit_price": view.unit_price,
                    "price_break_quantity": view.break_quantity,
                    "below_minimum_order": int(view.below_first_break)
                    if view.unit_price is not None else None,
                    "extended_price": view.extended_price,
                    "currency": choice.currency or model.currency or None,
                    "lifecycle": choice.lifecycle_id or choice.lifecycle or None,
                    "cache_date": iso(choice.last_updated),
                })
    non_pcb = [{
        "line": line.item.line_number, "design_item_id": line.item.design_item_id,
        "description": line.item.description or None, "comment": line.item.user_comments or None,
        "item_type": line.item.item_type or None, "quantity": line.quantity,
        "mpn": line.solutions[0].mpn if line.solutions else None,
        "manufacturer": line.solutions[0].manufacturer if line.solutions else None,
        "note": line.item.note or None,
    } for line in model.non_pcb]
    return {"bom_solutions": solutions, "bom_non_pcb_lines": non_pcb}


def expected_columns(model: ActiveBom) -> dict[str, dict]:
    """What an ActiveBOM-filled BOM shows per designator (upper case): its line number,
    the chosen solution's manufacturer (and aliases) and MPN, and its suppliers and SKUs
    (compared key -> name as written)."""
    out: dict[str, dict] = {}
    duplicated = model.duplicate_numbers()
    for line in model.lines:
        primary = line.solutions[0] if line.solutions else None
        choices = [view.choice for view in primary.choices] if primary else []
        entry = {
            "line": line.item.line_number,
            # The BomDoc gives the number to several lines: an export may renumber them.
            "line_numbers_duplicated": bool(duplicated),
            "mpn": primary.mpn if primary else None,
            "manufacturer": primary.manufacturer if primary else None,
            "aliases": {_key(alias) for choice in choices for alias in choice.aliases},
            "suppliers": {_key(choice.supplier): choice.supplier for choice in choices
                          if choice.supplier},
            "skus": {_key(choice.sku): choice.sku for choice in choices if choice.sku},
            "source": primary.source if primary else None,
            "solutions": [s.mpn for s in line.solutions],
        }
        for part in line.members:
            out[part.designator.upper()] = entry
    return out


def line_annotations(model: ActiveBom) -> dict[str, dict]:
    """get_bom's active_bom columns per designator (upper case): the BomDoc line number
    and the chosen solution."""
    out = {}
    for line in model.lines:
        primary = line.solutions[0] if line.solutions else None
        chosen = None
        if primary is not None:
            category, text = primary.lifecycle
            chosen = {key: value for key, value in {
                "manufacturer": primary.manufacturer or None, "mpn": primary.mpn or None,
                "source": primary.source, "lifecycle": text,
                "cache_date": iso(max(primary.dates)) if primary.dates else None,
            }.items() if value is not None}
        for part in line.members:
            out[part.designator.upper()] = {"line": line.item.line_number, "chosen": chosen}
    return out


def open_model(project: dict, parts: list[BomPart], netlist: dict, plan=None, variant: str = "",
               name: str | None = None, outjob_documents=None, now: datetime | None = None,
               production_quantity: int | None = None
               ) -> tuple[ActiveBom | None, dict | None]:
    """(model, error) for the project's BomDoc (see locate_bom_docs) and a variant's parts
    (production_quantity: build_active_bom's)."""
    choice = locate_bom_docs(project, name, outjob_documents)
    if choice.error:
        return None, choice.error
    try:
        doc = load_bom_doc(choice.chosen)
        return build_active_bom(doc, parts, netlist, plan, variant, now, choice,
                                production_quantity), None
    except Exception as error:  # a bad BomDoc must never break the BOM tools
        return None, {"error": "bom_doc_unreadable",
                      "message": f"{Path(choice.chosen).name}: {type(error).__name__}: {error}",
                      "bom_doc": choice.describe()}
