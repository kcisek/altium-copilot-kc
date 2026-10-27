"""Database library parts (roadmap #53): the database record behind a placed part, and a
read-only preview of Altium's Update Parameters From Database.

Finding the libraries. Database libraries (.DbLib, .SVNDbLib) and database link files
(.DbLink) are found where Altium finds them: the project's own documents, the installed
libraries (Altium's preferences, as services/libraries.py reads them), the project's
search paths, and the library a part names (SourceLibraryName or DatabaseLibraryName,
also by its LibraryPath when that is a path to the file). Each library says which of
these found it; parts naming a database library that none of them has are listed.

Linking a part to a record.
- A part placed from a DbLib or SVNDbLib names the library (SourceLibraryName or
  DatabaseLibraryName) and its table (DatabaseTableName). Its record is looked up in
  that table (in every enabled table when it names none) by the table's key field,
  whose value is the part's parameter (or system field, such as [Library Ref]) the key
  maps to, else its Design Item ID (Altium stores the key's value there when placing;
  both are tried when they differ), or by the table's WHERE clause with each
  {Parameter} taking the part's value.
- A DbLink links parts placed from ordinary libraries: every part not placed from a
  database library is looked up in the DbLink's enabled tables (only its
  DatabaseTableName's table when UseDBTableName is set; a part restricted to a table
  the DbLink lacks or has disabled is counted) by the key parameter or WHERE clause. A
  part without the key parameter is not linked (counted, not reported).
- Tables are tried in the library's order and the first that has the record is used
  (others: found_in_several_tables). No record: record_not_found. More than one in that
  table: duplicate_records (the first is used). The key parameter and the Design Item ID
  finding different records: key_conflict (the key parameter's record is used).

Comparing. Each mapped field of the record against the part, as Update Parameters From
Database would apply it with the field's update options (Default: the library's):
would_change (the part has the parameter with another value and the update option is
Always), differs_not_updated (Never), would_add (the part lacks it; Always, or If not
blank in the database with a value; with VisibleOnAdd), would_remove (blank in the
database, remove option If blank), blank_in_database (blank in the database, kept). A
parameter present on the sheet with a blank value counts as present. The key field's
own parameter is compared too when the record was found by the Design Item ID. System
fields: the symbol ([Library Ref]) against the part's library reference, the footprint
([Footprint Ref], then [Footprint Ref 2] ...) against the part's current footprint and
its other footprint models, [Library Path] / [Footprint Path] against the library file
the part records (when it records one), and [Description] against its description.
Values compare exactly after trimming spaces; a difference only in letter case is said
so.

Data sources are queried read-only through services/db_query.py; one that cannot or may
not be queried is skipped with the reason (no driver, network source not allowed, file
missing, a OneDrive or network-share path, unreadable). Nothing is written anywhere."""

import json
import logging
import os
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

try:
    from parsers.db_lib import (
        DB_KINDS, FILE_KINDS, DatabaseLibrary, TableLink, WhereError, db_kind, parse_db_lib,
        parse_where, where_placeholders,
    )
    from services import db_query
    from services.bom import BASE_VARIANT, natural_key
    from services.libraries import project_libraries
except ImportError:
    from server.parsers.db_lib import (
        DB_KINDS, FILE_KINDS, DatabaseLibrary, TableLink, WhereError, db_kind, parse_db_lib,
        parse_where, where_placeholders,
    )
    from server.services import db_query
    from server.services.bom import BASE_VARIANT, natural_key
    from server.services.libraries import project_libraries

ORIGIN_PROJECT = "project"
ORIGIN_INSTALLED = "installed"
ORIGIN_SEARCH_PATH = "search path"
ORIGIN_PART = "part's library path"

VIA_DBLIB = "database library"
VIA_DBLINK = "database link"

LOOKUP_LIMIT = 5  # rows read per lookup: more than one is a duplicate
ALTERNATE_LIMIT = 10
ALTERNATES_BY = ("Value", "[Footprint Ref]")
EXAMPLES = 10  # parts listed per missing library
CACHE_SECONDS = 120
CACHE_SIZE = 4

SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}
KINDS = {
    "library_not_found": "warning",
    "library_unreadable": "warning",
    "library_skipped": "info",
    "table_not_in_library": "warning",
    "table_disabled": "warning",
    "no_lookup": "warning",
    "where_unsupported": "warning",
    "bad_name": "warning",
    "key_missing": "warning",
    "query_failed": "warning",
    "record_not_found": "warning",
    "duplicate_records": "warning",
    "key_conflict": "warning",
    "would_change": "warning",
    "symbol_differs": "warning",
    "footprint_differs": "warning",
    "alternate_record_not_found": "warning",
    "differs_not_updated": "info",
    "would_add": "info",
    "would_remove": "info",
    "blank_in_database": "info",
    "library_path_differs": "info",
    "description_differs": "info",
    "found_in_several_tables": "info",
    "no_tables": "info",
}
# Lookup problems of a table rather than a part (reported once per table, with its parts,
# for a database link file or a table besides one that ran).
TABLE_PROBLEMS = ("where_unsupported", "no_lookup", "bad_name")
BAD_NAME = ("a table or field name holds a quote, semicolon or control character, or the "
            "clause nests too deeply: it is not queried")
SHRINK_STEPS = (100, 50, 25, 10, 5, 1)
PART_SHRINK_STEPS = ((ALTERNATE_LIMIT, 200), (5, 60), (3, 20), (1, 5), (0, 0))


# ---------------------------------------------------------------------------
# Parts
# ---------------------------------------------------------------------------

@dataclass
class Part:
    """A placed part as the database lookups see it (base design parameters)."""

    designator: str
    params: dict[str, str] = field(default_factory=dict)  # lower-case name -> value
    description: str = ""
    lib_reference: str = ""
    design_item_id: str = ""
    library: str = ""
    library_path: str = ""
    database_table: str = ""
    database_library: str = ""
    use_db_table_name: bool = False
    footprint: str = ""
    footprints: list[str] = field(default_factory=list)
    footprint_library: str = ""
    fitted: bool = True
    variant_params: dict[str, str] = field(default_factory=dict)  # lower-case -> value
    alternate: dict | None = None  # the variant's alternate part (alternate_identity)

    @property
    def database_name(self) -> str:
        """The database library the part names ('' for none)."""
        for name in (self.database_library, self.library):
            if db_kind(name) in ("DbLib", "SVNDbLib"):
                return name
        if db_kind(self.library_path) in ("DbLib", "SVNDbLib"):
            return self.library_path
        return ""

    def value(self, name: str) -> str | None:
        """A parameter's value by name (any case); the special names Design Item ID,
        Library Ref, Description, Footprint and Designator read the part's own fields."""
        key = re.sub(r"\s+", " ", name.strip().strip("[]")).lower()
        if key in self.params:
            return self.params[key]
        special = {
            "design item id": self.design_item_id, "designitemid": self.design_item_id,
            "library ref": self.lib_reference, "lib ref": self.lib_reference,
            "libreference": self.lib_reference, "description": self.description,
            "footprint": self.footprint, "footprint ref": self.footprint,
            "designator": self.designator,
        }
        value = special.get(key)
        return value if value else None

    def brief(self) -> dict:
        out: dict = {"designator": self.designator}
        for name in ("lib_reference", "design_item_id", "library", "database_table",
                     "footprint"):
            value = getattr(self, name)
            if value:
                out[name] = value
        if not self.fitted:
            out["fitted"] = False
        return out


def make_part(name: str, netlist: dict, plan=None) -> Part | None:
    """A Part for one netlist component (None when the netlist has no such component)."""
    compiled = netlist.get("parts") or {}
    meta = (netlist.get("components") or {}).get(name)
    if not meta:
        return None
    params = {key.lower(): str(value) for key, value in (meta.get("params") or {}).items()}
    for blank in meta.get("blank_params") or []:
        params.setdefault(str(blank).lower(), "")  # on the sheet with no value: present
    part = Part(
        designator=name,
        params=params,
        description=meta.get("description") or "",
        lib_reference=meta.get("lib_reference") or "",
        design_item_id=meta.get("design_item_id") or "",
        library=meta.get("library") or "",
        library_path=meta.get("library_path") or "",
        database_table=meta.get("database_table") or "",
        database_library=meta.get("database_library") or "",
        use_db_table_name=bool(meta.get("use_db_table_name")),
        footprint=meta.get("footprint") or (compiled.get(name) or {}).get("footprint") or "",
        footprints=list(meta.get("footprints") or []),
        footprint_library=meta.get("footprint_library") or "")
    if plan is not None and plan.name != BASE_VARIANT:
        part.fitted = name not in plan.not_fitted
        part.variant_params = {key.lower(): value for key, (_, value)
                               in (plan.params.get(name) or {}).items()}
        alternate = plan.alternates.get(name)
        variation = (alternate or {}).get("variation")
        if variation is not None:
            part.alternate = variation.alternate_identity()
    return part


def collect_parts(netlist: dict, plan=None) -> list[Part]:
    """The project's placed parts (compiled designators; compile-masked copies left out)
    with the variant's fitting, parameter variations and alternate parts."""
    compiled = netlist.get("parts") or {}
    components = netlist.get("components") or {}
    names = list(compiled) if compiled else [
        name for name, meta in components.items() if not meta.get("compile_masked")]
    parts = [part for part in (make_part(name, netlist, plan) for name in names) if part]
    parts.sort(key=lambda part: natural_key(part.designator))
    return parts


# ---------------------------------------------------------------------------
# Libraries
# ---------------------------------------------------------------------------

@dataclass
class FoundLibrary:
    path: str
    kind: str
    origins: list[str] = field(default_factory=list)
    library: DatabaseLibrary | None = None
    error: str = ""
    named_by: list[str] = field(default_factory=list)  # parts naming it
    plan: db_query.SourcePlan | None = None
    skip: str = ""  # a db_query.SKIP_* reason
    query_error: str = ""
    queries: int = 0
    seconds: float = 0.0
    # Parts a database link file does not look up because they are restricted to a
    # table it lacks or has disabled (UseDBTableName): designator -> (reason, table).
    excluded: dict[str, tuple[str, str]] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return Path(self.path).name

    @property
    def status(self) -> str:
        if self.error:
            return "unreadable"
        if self.skip or self.query_error:
            return "skipped"
        return "queried" if self.plan is not None else "not_queried"

    def reason(self) -> str:
        return self.skip or self.query_error


def _base(name: str) -> str:
    return re.split(r"[\\/]", name.strip())[-1].casefold()


def _file_library(name: str) -> bool:
    """A symbol library file name (SchLib, IntLib): the part was placed from it."""
    return os.path.splitext(name.strip())[1].lower() in (".schlib", ".intlib")


def find_libraries(prj_path: str, parts: list[Part], installed: list[str],
                   project_dir: str = "", allow_network: bool = False
                   ) -> tuple[list[FoundLibrary], list[dict]]:
    """(the database libraries and link files the project can use, in Altium's search
    order with where each was found; the database libraries parts name that none has,
    with the parts). A Data Link (.udl) file a library names is read only when it is
    local or allow_network is true."""
    found: dict[str, FoundLibrary] = {}
    try:
        libraries = project_libraries(prj_path, installed=installed)
        groups = ((libraries.project_libraries, ORIGIN_PROJECT),
                  (libraries.installed, ORIGIN_INSTALLED),
                  (libraries.search_path_libraries, ORIGIN_SEARCH_PATH))
    except OSError as error:
        logging.warning("project libraries not read: %s", type(error).__name__)
        groups = ()
    for group, origin in groups:
        for entry in group:
            if entry.kind not in DB_KINDS.values():
                continue
            key = os.path.normcase(os.path.normpath(entry.path))
            if key in found:
                if origin not in found[key].origins:
                    found[key].origins.append(origin)
                continue
            library = FoundLibrary(entry.path, entry.kind, [origin])
            if not entry.exists:
                library.error = "file_missing"
            found[key] = library
    missing: dict[str, list[str]] = {}
    folder = project_dir or str(Path(prj_path).parent)
    for part in parts:
        name = part.database_name
        if not name:
            continue
        wanted = _base(name)
        hits = [library for library in found.values()
                if _base(library.path) == wanted and library.kind != "DbLink"]
        if not hits:
            for candidate in (part.library_path, name):
                if db_kind(candidate) not in ("DbLib", "SVNDbLib"):
                    continue
                path = candidate if os.path.isabs(candidate) else os.path.join(folder, candidate)
                if _base(path) == wanted and os.path.isfile(path):
                    key = os.path.normcase(os.path.normpath(path))
                    found.setdefault(key, FoundLibrary(os.path.normpath(path), db_kind(path),
                                                       [ORIGIN_PART]))
                    hits = [found[key]]
                    break
        if hits:
            if part.designator not in hits[0].named_by:
                hits[0].named_by.append(part.designator)
        else:
            missing.setdefault(Path(re.split(r"[\\/]", name)[-1]).name, []).append(part.designator)
    udl_allowed = None if allow_network else (
        lambda path: db_query.path_location(path) == "local")
    for library in found.values():
        if library.error:
            continue
        try:
            library.library = parse_db_lib(library.path, dsn_lookup=db_query.dsn_lookup,
                                           udl_allowed=udl_allowed)
        except OSError as error:
            library.error = ("file_missing" if isinstance(error, FileNotFoundError)
                             else f"unreadable ({type(error).__name__})")
        except Exception as error:  # a malformed library is reported, never raised
            library.error = f"unreadable ({type(error).__name__})"
    missing_list = [{"library": name, "parts": sorted(designators, key=natural_key)[:EXAMPLES],
                     "part_count": len(designators)}
                    for name, designators in sorted(missing.items())]
    return list(found.values()), missing_list


def library_matches(library: FoundLibrary, wanted: str) -> bool:
    text = wanted.strip()
    return bool(text) and (_base(library.path) == _base(text)
                           or Path(library.path).stem.casefold() == text.casefold()
                           or os.path.normcase(library.path) == os.path.normcase(text))


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------

@dataclass
class Lookup:
    """One way to find a part's record in one table."""

    table: TableLink
    where: dict | None = None
    matched_by: dict = field(default_factory=dict)
    problem: str = ""  # key_missing, where_unsupported, no_lookup, bad_name
    detail: str = ""
    # The Design Item ID used in place of the key parameter (the part lacks it, or it
    # was tried after the parameter's own value).
    fallback: bool = False
    result: db_query.QueryResult | None = None


@dataclass
class Resolved:
    """The record a link settles on: the lookup that found it and every record it read
    (the first is compared), whether the read was cut at the lookup limit, the other
    tables that also had a record, and a conflict between the key parameter and the
    Design Item ID (each found another record)."""

    lookup: Lookup | None = None
    records: list[dict] = field(default_factory=list)
    truncated: bool = False
    other_tables: list[str] = field(default_factory=list)
    conflict: dict | None = None


@dataclass
class Link:
    part: Part
    library: FoundLibrary
    via: str
    how: str
    tables: list[TableLink] = field(default_factory=list)
    lookups: list[Lookup] = field(default_factory=list)
    problems: list[dict] = field(default_factory=list)
    alternate: bool = False  # the variant's alternate part, not the placed one

    def resolve(self) -> Resolved:
        """The first table (in the library's order) whose lookups found a record, and in
        it the key parameter's record before the Design Item ID's."""
        hits: dict[int, list[Lookup]] = defaultdict(list)
        for lookup in self.lookups:
            if lookup.result is not None and not lookup.result.error and lookup.result.rows:
                hits[id(lookup.table)].append(lookup)
        order = [table for table in self.tables if id(table) in hits]
        order += [lookup.table for lookup in self.lookups
                  if id(lookup.table) in hits and lookup.table not in order]
        if not order:
            return Resolved()
        found = hits[id(order[0])]
        chosen = found[0]
        resolved = Resolved(chosen, chosen.result.records(), chosen.result.truncated,
                            [table.name for table in order[1:]])
        if len(found) > 1:
            def key_value(lookup: Lookup) -> str | None:
                fields = lookup.matched_by.get("fields") or [{}]
                return fields[0].get("value")

            resolved.conflict = {"key_parameter_value": key_value(found[0]),
                                 "design_item_id": key_value(found[1])}
        return resolved

    def matches(self) -> list[tuple[Lookup, dict]]:
        """(lookup, record) of every record resolve() settles on."""
        resolved = self.resolve()
        return [(resolved.lookup, record) for record in resolved.records]

    def errors(self) -> list[str]:
        return sorted({lookup.result.error for lookup in self.lookups
                       if lookup.result is not None and lookup.result.error})


_WHERE_CACHE: dict[tuple, object] = {}


def _where_tree(text: str, left: str, right: str, double_quoted_strings: bool = False):
    key = (text, left, right, double_quoted_strings)
    if key not in _WHERE_CACHE:
        try:
            _WHERE_CACHE[key] = parse_where(text, left, right, double_quoted_strings)
        except (WhereError, RecursionError) as error:
            _WHERE_CACHE[key] = WhereError(str(error)) if isinstance(error, RecursionError) \
                else error
    return _WHERE_CACHE[key]


def _number_or_text(text: str):
    """A bare value as SQL would read it: a number when it is one, else the text."""
    return float(text) if re.fullmatch(r"\s*-?\d+(\.\d+)?\s*", text) else text


def bind_where(tree, values: dict[str, str]):
    """A parsed WHERE tree as a query node with the placeholders' values in."""
    kind = tree[0]
    if kind in ("and", "or"):
        return {kind: [bind_where(child, values) for child in tree[1]]}
    if kind == "not":
        return {"not": bind_where(tree[1], values)}
    if kind == "null":
        return {"null": [tree[1], bool(tree[2])]}
    _, name, op, value = tree
    text = value.render(values)
    return {"cmp": [name, op, text if value.kind == "string" else _number_or_text(text)]}


def _key_lookups(table: TableLink, part: Part, via: str, key_value: str | None = None,
                 ) -> list[Lookup]:
    keys = table.key_fields
    if not keys:
        return [Lookup(table, problem="no_lookup",
                       detail="the table has no key field and no WHERE clause")]
    if key_value is not None:
        if len(keys) != 1:
            return [Lookup(table, problem="no_lookup",
                           detail="the table's key has several fields: give a designator")]
        item = keys[0]
        return [Lookup(table, where={"cmp": [item.field, "=", key_value]}, matched_by={
            "by": "key", "fields": [{"field": item.field, "value": key_value,
                                      "source": "key value given"}]})]
    terms, used, missing, fallback = [], [], [], []
    stand_in = False
    for item in keys:
        parameter = item.design_parameter
        value, source = None, ""
        if item.system == "design_item_id":
            value, source = part.design_item_id or None, "design item id"
        elif item.system:
            # A key mapped to a system field ([Library Ref], [Footprint Ref],
            # [Description] ...): the part's own field.
            value, source = part.value(item.parameter), "system field"
        elif parameter:
            value, source = part.value(parameter), "parameter"
        if not value and via == VIA_DBLIB and part.design_item_id:
            # The Design Item ID holds the key's value when the part lacks the parameter;
            # when the key maps to no parameter it is the link itself.
            stand_in = stand_in or bool(parameter or item.system)
            value, source = part.design_item_id, "design item id"
        elif value and via == VIA_DBLIB and len(keys) == 1 and part.design_item_id and \
                item.system != "design_item_id" and \
                part.design_item_id.strip().casefold() != value.strip().casefold():
            fallback.append((item, part.design_item_id))
        if not value:
            missing.append(parameter or item.parameter or item.field)
            continue
        terms.append({"cmp": [item.field, "=", value]})
        used.append({"field": item.field, "parameter": parameter or item.parameter or None,
                     "value": value, "source": source})
    if missing:
        return [Lookup(table, problem="key_missing", detail=", ".join(missing))]
    where = terms[0] if len(terms) == 1 else {"and": terms}
    lookups = [Lookup(table, where=where, matched_by={"by": "key", "fields": used},
                      fallback=stand_in)]
    for item, value in fallback:
        lookups.append(Lookup(table, where={"cmp": [item.field, "=", value]}, fallback=True,
                              matched_by={"by": "key", "fields": [{
                                  "field": item.field, "parameter": "Design Item ID",
                                  "value": value, "source": "design item id"}]}))
    return lookups


def _where_lookups(library: DatabaseLibrary, table: TableLink, part: Part | None,
                   key_value: str | None = None) -> list[Lookup]:
    tree = _where_tree(table.where_text, library.left_quote, library.right_quote,
                       library.connection.kind in FILE_KINDS)
    if isinstance(tree, WhereError):
        return [Lookup(table, problem="where_unsupported", detail=str(tree))]
    names = where_placeholders(tree)
    values: dict[str, str] = {}
    if key_value is not None:
        if len(names) != 1:
            return [Lookup(table, problem="no_lookup", detail="the WHERE clause takes "
                           f"{len(names)} parameters: give a designator")]
        values[names[0].lower()] = key_value
    else:
        missing = []
        for name in names:
            value = part.value(name) if part is not None else None
            if value is None:
                missing.append(name)
            else:
                values[name.lower()] = value
        if missing:
            return [Lookup(table, problem="key_missing", detail=", ".join(missing))]
    node = bind_where(tree, values)
    return [Lookup(table, where=node, matched_by={
        "by": "where", "where": table.where_text,
        "values": {name: values.get(name.lower()) for name in names}})]


def _refuse_unsafe(library: DatabaseLibrary, lookups: list[Lookup]) -> list[Lookup]:
    """lookups with those whose table, schema or field names the query layer would
    refuse turned into bad_name problems, so that the library's other lookups run."""
    for lookup in lookups:
        if lookup.where is None or lookup.problem:
            continue
        schema = _schema(library, lookup.table)
        if not db_query.check_name(lookup.table.name) or (
                schema and not db_query.check_name(schema)) or \
                not db_query.check_where(lookup.where):
            lookup.problem, lookup.detail, lookup.where = "bad_name", BAD_NAME, None
    return lookups


def plan_lookups(link: Link) -> None:
    library = link.library.library
    for table in link.tables:
        if table.user_where and table.where_text.strip():
            link.lookups += _where_lookups(library, table, link.part)
        else:
            link.lookups += _key_lookups(table, link.part, link.via)
    _refuse_unsafe(library, link.lookups)


def _alternate_part(part: Part) -> Part | None:
    """The variant's alternate part as a Part to look up (its library, table and Design
    Item ID; its parameters from the .PrjPcbVariants copy and the variation)."""
    identity = part.alternate or {}
    if not identity or not (identity.get("database_table")
                            or db_kind(identity.get("library") or "") in ("DbLib", "SVNDbLib")):
        return None
    alternate = Part(designator=part.designator, library=identity.get("library") or "",
                     database_table=identity.get("database_table") or "",
                     design_item_id=identity.get("design_item_id") or "",
                     lib_reference=identity.get("lib_reference") or "",
                     use_db_table_name=bool(identity.get("database_table")),
                     fitted=part.fitted)
    alternate.params = dict(part.variant_params)
    for name in ("comment", "value"):
        if identity.get(name) and name not in alternate.params:
            alternate.params[name] = identity[name]
    return alternate


def link_parts(parts: list[Part], libraries: list[FoundLibrary]) -> list[Link]:
    """Every (part, library) pair to look up: database library parts in the library they
    name (a table name alone picks the one library holding it), every other part in each
    database link file."""
    readable = [library for library in libraries if library.library is not None]
    dblibs = [library for library in readable if library.kind != "DbLink"]
    dblinks = [library for library in readable if library.kind == "DbLink"]
    links: list[Link] = []

    def database_link(part: Part, alternate: bool = False) -> Link | None:
        name = part.database_name
        hits, how = [], ""
        if name:
            hits = [library for library in dblibs if _base(library.path) == _base(name)]
            how = "database library name" if part.database_library else "source library name"
        elif part.database_table and not _file_library(part.library):
            # Only the table is named: the one database library holding such a table.
            hits = [library for library in dblibs
                    if library.library.table(part.database_table) is not None]
            hits, how = (hits if len(hits) == 1 else []), "table name"
        if not hits:
            return None
        library = hits[0]
        link = Link(part, library, VIA_DBLIB, how, alternate=alternate)
        if part.database_table:
            table = library.library.table(part.database_table)
            if table is None:
                link.problems.append({"kind": "table_not_in_library",
                                      "detail": f"the part names table '{part.database_table}', "
                                                f"which {library.name} does not have"})
                link.tables = list(library.library.enabled_tables)
            elif not table.enabled:
                link.problems.append({"kind": "table_disabled",
                                      "detail": f"table '{table.name}' is disabled in "
                                                f"{library.name}"})
                link.tables = [table]
            else:
                link.tables = [table]
        else:
            link.tables = list(library.library.enabled_tables)
        return link

    for part in parts:
        link = database_link(part)
        if link is not None:
            links.append(link)
        elif not part.database_name:
            for library in dblinks:
                tables = library.library.enabled_tables
                if part.database_table and part.use_db_table_name:
                    # The part's own table only (UseDBTableName); without the flag every
                    # enabled table is searched in order.
                    named = library.library.table(part.database_table)
                    if named is None or not named.enabled:
                        if library.library.enabled_tables:
                            library.excluded[part.designator] = (
                                "table_not_in_link" if named is None else "table_disabled",
                                part.database_table)
                        continue
                    tables = [named]
                if tables:
                    links.append(Link(part, library, VIA_DBLINK, "database link",
                                      tables=list(tables)))
        alternate = _alternate_part(part)
        if alternate is not None:
            link = database_link(alternate, alternate=True)
            if link is not None:
                links.append(link)
    for link in links:
        plan_lookups(link)
    return links


def run_lookups(libraries: list[FoundLibrary], links: list[Link], allow_network: bool,
                extra: dict[int, list[db_query.Query]] | None = None) -> dict[int, list]:
    """Query every library's lookups (one batch per library, identical queries once) and
    store the results on them; extra: more queries per library (id) whose results are
    returned."""
    by_library: dict[int, list[Lookup]] = defaultdict(list)
    for link in links:
        for lookup in link.lookups:
            if lookup.where is not None and not lookup.problem:
                by_library[id(link.library)].append(lookup)
    extra = extra or {}
    extra_results: dict[int, list] = {}
    for library in libraries:
        if library.library is None:
            continue
        lookups = by_library.get(id(library), [])
        more = extra.get(id(library), [])
        if not lookups and not more:
            continue
        plan, reason = db_query.plan_source(library.library, allow_network)
        if plan is None:
            library.skip = reason
            continue
        library.plan = plan
        queries: list[db_query.Query] = []
        index: dict[str, int] = {}
        slots = []
        for lookup in lookups:
            query = db_query.Query(lookup.table.name, lookup.where, LOOKUP_LIMIT,
                                   _schema(library.library, lookup.table))
            key = json.dumps(query.payload(), sort_keys=True)
            if key not in index:
                index[key] = len(queries)
                queries.append(query)
            slots.append(index[key])
        start = len(queries)
        queries += more
        started = time.monotonic()
        try:
            results = db_query.run_queries(plan, queries)
        except db_query.QueryError as error:
            library.query_error = error.code
            continue
        finally:
            library.queries = len(queries)
            library.seconds = round(time.monotonic() - started, 2)
        for lookup, slot in zip(lookups, slots):
            lookup.result = results[slot]
        extra_results[id(library)] = results[start:]
    return extra_results


def _schema(library: DatabaseLibrary, table: TableLink) -> str:
    if not library.use_table_schema_name:
        return ""
    return table.schema or library.schema


# ---------------------------------------------------------------------------
# Comparing a record with the part
# ---------------------------------------------------------------------------

def _text(value) -> str:
    return "" if value is None else str(value).strip()


def _record_value(record: dict, name: str):
    if name in record:
        return record[name]
    folded = name.casefold()
    return next((value for key, value in record.items() if key.casefold() == folded), None)


def _has_column(record: dict, name: str) -> bool:
    folded = name.casefold()
    return any(key.casefold() == folded for key in record)


def compare_record(link: Link, lookup: Lookup, record: dict) -> tuple[list[dict], list[dict]]:
    """(field rows, findings) of one record against its part."""
    library = link.library.library
    part = link.part
    rows, findings = [], []
    # The key field's parameter matched the record by its own value: nothing to compare.
    # Found through the Design Item ID instead, it is compared like any other field.
    key_fields = {item.field.casefold() for item in lookup.table.key_fields} \
        if lookup.matched_by.get("by") == "key" and not lookup.fallback else set()

    def finding(kind: str, **extra) -> None:
        entry = {"kind": kind, "severity": KINDS[kind], "designator": part.designator,
                 "library": link.library.name, "table": lookup.table.name, **extra}
        if not part.fitted:
            entry["fitted"] = False
        findings.append(entry)

    footprint_rows = []
    for item in lookup.table.fields:
        if not item.mapped:
            continue
        present = _has_column(record, item.field)
        database = _text(_record_value(record, item.field)) if present else None
        modes = item.modes(library.defaults)
        row: dict = {"field": item.field}
        if not present:
            row.update(parameter=item.design_parameter or item.parameter,
                       status="field_not_in_record")
            rows.append(row)
            continue
        system = item.system
        if system and system != "comment":
            row.update(system_field=system, parameter=item.parameter, database=database)
            if system == "symbol_ref":
                row["schematic"] = part.lib_reference
                row["status"] = _same(part.lib_reference, database)
                if row["status"] != "same" and database:
                    finding("symbol_differs", schematic=part.lib_reference or None,
                            database=database, case_only=row["status"] == "case_only",
                            detail=("the record names another symbol than the one placed "
                                    "(Update From Libraries would change it)"
                                    if link.via == VIA_DBLIB else
                                    "the record names another symbol than the one placed from "
                                    "the part's own library: the library and the database "
                                    "disagree"))
                    if row["status"] == "case_only":
                        findings[-1]["severity"] = "info"
            elif system.startswith("footprint_ref"):
                footprint_rows.append((item, database))
                row["schematic"] = part.footprint
                if not database:
                    row["status"] = "blank"
                elif system == "footprint_ref":
                    row["status"] = _same(part.footprint, database)
                else:
                    # Another footprint model the record names: on the part or not.
                    row["status"] = (
                        "same" if _same(part.footprint, database) != "differs" else
                        "part_model" if database.casefold() in {
                            model.casefold() for model in part.footprints if model}
                        else "not_on_part")
            elif system in ("symbol_path", "footprint_path"):
                placed = part.library_path if system == "symbol_path" else part.footprint_library
                files = (".schlib", ".intlib") if system == "symbol_path" else (".pcblib", ".intlib")
                # Compared only when the part records a library file ('*', a database
                # library or nothing say where it was placed from, not the file).
                recorded = placed if os.path.splitext(placed.strip())[1].lower() in files else ""
                row["schematic"] = placed or None
                row["status"] = ("not_recorded" if not recorded else "same" if not database
                                 or _base(recorded) == _base(database) else "differs")
                if row["status"] == "differs":
                    finding("library_path_differs", system_field=system,
                            schematic=placed, database=database,
                            detail="the part records another library file than the record")
            elif system == "description":
                row["schematic"] = part.description
                row["status"] = _same(part.description, database)
                if row["status"] != "same" and database:
                    finding("description_differs", schematic=part.description or None,
                            database=database, case_only=row["status"] == "case_only")
            elif system == "design_item_id":
                row["schematic"] = part.design_item_id
                row["status"] = _same(part.design_item_id, database)
            else:
                row["status"] = "not_compared"
            rows.append(row)
            continue
        name = item.design_parameter or "Comment"
        current = part.params.get(name.lower())
        options = dict(modes)
        if item.visible_on_add is not None:
            options["visible_on_add"] = item.visible_on_add
        row.update(parameter=name, database=database, schematic=current, options=options)
        if item.field.casefold() in key_fields:
            row["status"] = "key"
            rows.append(row)
            continue
        variant = part.variant_params.get(name.lower())
        extra = {"variant_value": variant} if variant is not None else {}
        blank = not database
        if current is None:
            if (not blank and modes["add"] in ("always", "if_not_blank")) or (
                    blank and modes["add"] == "always"):
                row["status"] = "would_add"
                visible = {} if item.visible_on_add is None else {
                    "visible_on_add": item.visible_on_add}
                finding("would_add", parameter=name, database=database, **visible, **extra,
                        detail="the part lacks the parameter; Update Parameters From "
                               "Database would add it")
            else:
                row["status"] = "missing"
        elif blank:
            if modes["remove"] == "if_blank":
                row["status"] = "would_remove"
                finding("would_remove", parameter=name, schematic=current, **extra,
                        detail="blank in the database; Update Parameters From Database "
                               "would remove it")
            elif current.strip():
                row["status"] = "blank_in_database"
                finding("blank_in_database", parameter=name, schematic=current, **extra,
                        detail="blank in the database; the part keeps its value")
            else:
                row["status"] = "same"
        else:
            status = _same(current, database)
            row["status"] = status if status == "same" else (
                "would_change" if modes["update"] == "always" else "differs_not_updated")
            if status != "same":
                kind = row["status"]
                finding(kind, parameter=name, schematic=current, database=database,
                        case_only=status == "case_only", **extra,
                        detail=("Update Parameters From Database would change it"
                                if kind == "would_change" else
                                "the field's update option is Never: the part keeps its value"))
                if status == "case_only":
                    findings[-1]["severity"] = "info"
        rows.append(row)
    if footprint_rows:
        findings += _footprint_findings(link, lookup, footprint_rows)
    for entry in findings:
        if not entry.get("case_only"):
            entry.pop("case_only", None)
    return rows, findings


def _same(schematic: str | None, database: str | None) -> str:
    a, b = _text(schematic), _text(database)
    if a == b:
        return "same"
    return "case_only" if a.casefold() == b.casefold() else "differs"


def _footprint_number(system: str) -> int:
    """1 for [Footprint Ref], n for [Footprint Ref n]."""
    tail = system.rsplit("_", 1)[-1]
    return int(tail) if tail.isdigit() else 1


def _footprint_findings(link: Link, lookup: Lookup, footprint_rows) -> list[dict]:
    part = link.part
    ordered = sorted(footprint_rows, key=lambda row: _footprint_number(row[0].system))
    names = [database for _, database in ordered if database]
    if not names:
        return []
    models = [model for model in part.footprints if model]
    folded = {model.casefold() for model in models}
    current = part.footprint
    first = names[0]
    base = {"kind": "footprint_differs", "designator": part.designator,
            "library": link.library.name, "table": lookup.table.name,
            "schematic": current or None, "database": first}
    if len(names) > 1:
        base["database_all"] = names
    if not part.fitted:
        base["fitted"] = False
    if current and current.casefold() == first.casefold():
        return []
    if current and current.casefold() in {name.casefold() for name in names}:
        return [{**base, "severity": "info",
                 "detail": "the current footprint is another of the record's footprints"}]
    if first.casefold() in folded:
        return [{**base, "severity": "info",
                 "detail": "the record's footprint is one of the part's footprint models, "
                           "but not the current one"}]
    return [{**base, "severity": KINDS["footprint_differs"],
             "detail": "the record names a footprint the part does not have"}]


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------

def _query_error_detail(errors: list[str]) -> str:
    return "; ".join(f"{code}: {db_query.QUERY_ERRORS.get(code, 'the query failed')}"
                     for code in errors)


def evaluate_links(links: list[Link]) -> list[dict]:
    """The findings of looked-up links. Lookup problems that depend only on the library
    file and the part (key_missing, where_unsupported, no_lookup, bad_name) are reported
    whether or not the data source could be queried."""
    findings: list[dict] = []
    table_problems: dict[tuple[str, str, str], dict] = {}
    for link in links:
        part, library = link.part, link.library
        base = {"designator": part.designator, "library": library.name}
        if not part.fitted:
            base["fitted"] = False
        for problem in link.problems:
            findings.append({"kind": problem["kind"], "severity": KINDS[problem["kind"]],
                             **base, "detail": problem["detail"]})
        problems = [lookup for lookup in link.lookups if lookup.problem]
        runnable = [lookup for lookup in link.lookups if not lookup.problem]
        for lookup in problems:
            # A table's own problem: once per table for a database link file (every part
            # meets it) or for a table besides one that ran.
            if lookup.problem in TABLE_PROBLEMS and (link.via == VIA_DBLINK or runnable):
                entry = table_problems.setdefault(
                    (library.name, lookup.table.name, lookup.problem),
                    {"detail": lookup.detail, "parts": []})
                if part.designator not in entry["parts"]:
                    entry["parts"].append(part.designator)
        if not runnable:
            if link.via == VIA_DBLINK:
                continue  # the part does not carry the key: not linked
            for lookup in problems[:1]:
                findings.append({"kind": lookup.problem, "severity": KINDS[lookup.problem],
                                 **base, "table": lookup.table.name, "detail": lookup.detail})
            continue
        if library.status != "queried":
            continue
        resolved = link.resolve()
        if link.alternate:
            if not resolved.records and not link.errors():
                findings.append({"kind": "alternate_record_not_found",
                                 "severity": KINDS["alternate_record_not_found"], **base,
                                 "design_item_id": part.design_item_id or None,
                                 "detail": "the variant's alternate part has no record"})
            continue
        if not resolved.records:
            errors = link.errors()
            if errors:
                findings.append({"kind": "query_failed", "severity": KINDS["query_failed"],
                                 **base, "error": errors[0],
                                 "detail": "a lookup query failed: " + _query_error_detail(errors)})
            else:
                findings.append({"kind": "record_not_found",
                                 "severity": KINDS["record_not_found"], **base,
                                 "tables": [table.name for table in link.tables],
                                 "matched_by": runnable[0].matched_by,
                                 "detail": "no record matches the part"})
            continue
        lookup, record = resolved.lookup, resolved.records[0]
        if len(resolved.records) > 1:
            entry = {"kind": "duplicate_records", "severity": KINDS["duplicate_records"],
                     **base, "table": lookup.table.name, "records": len(resolved.records),
                     "matched_by": lookup.matched_by,
                     "detail": "several records match the part; the first is compared"}
            if resolved.truncated:
                entry["more_records"] = True
                entry["detail"] += f" (only the first {len(resolved.records)} were read)"
            findings.append(entry)
        if resolved.conflict:
            findings.append({"kind": "key_conflict", "severity": KINDS["key_conflict"], **base,
                             "table": lookup.table.name, **resolved.conflict,
                             "detail": "the key parameter and the Design Item ID name different "
                                       "records; the key parameter's record is compared"})
        if resolved.other_tables:
            findings.append({"kind": "found_in_several_tables",
                             "severity": KINDS["found_in_several_tables"], **base,
                             "table": lookup.table.name, "also_in": resolved.other_tables,
                             "detail": "other tables also hold a record for the part; the "
                                       "first table in the library's order is used"})
        _, record_findings = compare_record(link, lookup, record)
        findings += record_findings
    for (library_name, table, kind), entry in table_problems.items():
        parts = sorted(entry["parts"], key=natural_key)
        findings.append({"kind": kind, "severity": KINDS[kind], "library": library_name,
                         "table": table, "parts": parts[:EXAMPLES], "part_count": len(parts),
                         "detail": entry["detail"]})
    return findings


def sort_findings(findings: list[dict]) -> list[dict]:
    return sorted(findings, key=lambda entry: (
        SEVERITY_ORDER.get(entry["severity"], 3), entry["kind"],
        natural_key(entry.get("designator") or ""), entry.get("parameter") or ""))


def library_rows(libraries: list[FoundLibrary], links: list[Link], findings: list[dict],
                 tables: bool = True, allow_network: bool = False) -> list[dict]:
    rows = []
    for library in libraries:
        row: dict = {"name": library.name, "kind": library.kind, "path": library.path,
                     "found_by": list(library.origins)}
        if library.named_by:
            row["named_by_parts"] = len(library.named_by)
        row["status"] = library.status
        if library.error:
            row["error"] = library.error
        if library.reason():
            row["skipped_reason"] = library.reason()
            row["skipped_detail"] = db_query.SKIP_MESSAGES.get(
                library.reason(), "the query layer reported " + library.reason())
        elif library.status == "not_queried" and library.library is not None:
            # Nothing to look up here; whether its source could be read (nothing opened).
            _, reason = db_query.plan_source(library.library, allow_network)
            row["data_source"] = reason or "available"
        if library.library is not None:
            described = library.library.describe(tables=False)
            for key in ("connection", "update_defaults", "update_defaults_assumed",
                        "table_count", "enabled_tables", "warnings", "repository",
                        "field_maps_without_table"):
                if key in described:
                    row[key] = described[key]
            if tables:
                row["tables"] = [library.library.describe_table(table, fields=False)
                                 for table in library.library.tables]
        mine = [link for link in links if link.library is library and not link.alternate]
        if mine:
            looked = [link for link in mine if any(not lookup.problem for lookup in link.lookups)]
            found = [link for link in looked if link.matches()]
            row["parts_linked"] = len({link.part.designator for link in looked})
            if library.status == "queried":
                row["records_found"] = len({link.part.designator for link in found})
            if library.kind == "DbLink":
                row["parts_without_key"] = len({link.part.designator for link in mine
                                                if link not in looked})
        if library.excluded:
            # Restricted to their own table (UseDBTableName), which this file lacks or
            # has disabled: not looked up here.
            reasons = Counter(reason for reason, _ in library.excluded.values())
            for reason in ("table_not_in_link", "table_disabled"):
                if reasons.get(reason):
                    row[f"parts_{reason}"] = reasons[reason]
            row["excluded_parts"] = sorted(library.excluded, key=natural_key)[:EXAMPLES]
        if library.queries:
            row["queries"] = library.queries
            row["seconds"] = library.seconds
        kinds = Counter(entry["kind"] for entry in findings if entry.get("library") == library.name)
        if kinds:
            row["findings"] = dict(sorted(kinds.items()))
        rows.append(row)
    return rows


@dataclass
class CheckInputs:
    prj_path: str
    netlist: dict
    plan: object = None
    installed: list[str] = field(default_factory=list)
    library: str | None = None
    table: str | None = None
    allow_network: bool = False


def build_check(inputs: CheckInputs) -> dict:
    """check_db_parameters' model: summary, libraries, missing libraries, findings."""
    started = time.monotonic()
    parts = collect_parts(inputs.netlist, inputs.plan)
    libraries, missing = find_libraries(inputs.prj_path, parts, inputs.installed,
                                        allow_network=inputs.allow_network)
    if inputs.library:
        chosen = [library for library in libraries if library_matches(library, inputs.library)]
        if not chosen:
            return {"error": "library_not_found",
                    "message": f"No database library or link file '{inputs.library}' was found "
                               "for the project.",
                    "libraries": [library.name for library in libraries]}
        libraries = chosen
        missing = [entry for entry in missing
                   if _base(entry["library"]) == _base(inputs.library)]
    links = link_parts(parts, libraries)
    if inputs.table:
        wanted = inputs.table.strip().casefold()
        for link in links:
            link.tables = [table for table in link.tables if table.name.casefold() == wanted]
            link.lookups = [lookup for lookup in link.lookups
                            if lookup.table.name.casefold() == wanted]
        links = [link for link in links if link.tables]
    run_lookups(libraries, links, inputs.allow_network)
    findings = evaluate_links(links)
    for entry in missing:
        findings.append({"kind": "library_not_found", "severity": KINDS["library_not_found"],
                         "library": entry["library"], "parts": entry["parts"],
                         "part_count": entry["part_count"],
                         "detail": "parts name a database library that is not among the "
                                   "project's documents, installed libraries or search paths"})
    for library in libraries:
        if library.error:
            findings.append({"kind": "library_unreadable", "severity": KINDS["library_unreadable"],
                             "library": library.name, "error": library.error,
                             "detail": "the project lists the library but the file is not on "
                                       "disk" if library.error == "file_missing" else
                                       "the library file could not be read"})
        elif not library.library.enabled_tables:
            findings.append({"kind": "no_tables", "severity": KINDS["no_tables"],
                             "library": library.name, "tables": len(library.library.tables),
                             "detail": "the file has no enabled table: it links no part"})
        elif library.reason():
            linked = len({link.part.designator for link in links
                          if link.library is library and not link.alternate})
            findings.append({"kind": "library_skipped", "severity": KINDS["library_skipped"],
                             "library": library.name, "reason": library.reason(),
                             "parts_linked": linked,
                             "detail": db_query.SKIP_MESSAGES.get(
                                 library.reason(), "the query layer reported "
                                 + library.reason())})
    findings = sort_findings(findings)
    placed = [link for link in links if not link.alternate]
    looked = {link.part.designator for link in placed
              if any(not lookup.problem for lookup in link.lookups)}
    found = {link.part.designator for link in placed if link.matches()}
    queried = [library for library in libraries if library.status == "queried"]
    database_parts = {link.part.designator for link in placed if link.via == VIA_DBLIB}
    summary = {
        "parts": len(parts),
        "database_library_parts": len(database_parts),
        "database_libraries": sum(1 for library in libraries if library.kind != "DbLink"),
        "database_links": sum(1 for library in libraries if library.kind == "DbLink"),
        "libraries_queried": len(queried),
        "libraries_skipped": sum(1 for library in libraries if library.status == "skipped"),
        "libraries_unreadable": sum(1 for library in libraries if library.error),
        "parts_linked": len(looked),
        "records_found": len(found),
        "findings": dict(sorted(Counter(entry["kind"] for entry in findings).items())),
        "severity_counts": dict(sorted(Counter(entry["severity"] for entry in findings).items(),
                                       key=lambda item: SEVERITY_ORDER[item[0]])),
        "seconds": round(time.monotonic() - started, 2),
    }
    if missing:
        summary["missing_libraries"] = len(missing)
    model = {"summary": summary,
             "libraries": library_rows(libraries, links, findings,
                                       allow_network=inputs.allow_network),
             "findings": findings}
    if missing:
        model["missing_libraries"] = missing
    if not libraries and not missing:
        model["note"] = ("The project uses no database library: no .DbLib, .SVNDbLib or "
                         ".DbLink among its documents, the installed libraries or its search "
                         "paths, and no part names one.")
    return model


def page_check(model: dict, offset: int, limit: int) -> dict:
    findings = model["findings"]
    page = findings[offset:offset + limit]
    out = {key: value for key, value in model.items() if key != "findings"}
    out["findings_total"] = len(findings)
    out["offset"] = offset
    out["findings"] = page
    out["has_more"] = offset + limit < len(findings)
    if out["has_more"]:
        out["next_offset"] = offset + limit
    return out


# ---------------------------------------------------------------------------
# One part
# ---------------------------------------------------------------------------

def _alternate_queries(link: Link, lookup: Lookup, record: dict, by: list[str]
                       ) -> tuple[db_query.Query | None, list[dict], str]:
    """The query for other records of the table with the same values of by (parameter
    names or system fields such as [Footprint Ref]), the terms used, or why none."""
    table = lookup.table
    terms, used = [], []
    for name in by:
        text = name.strip()
        item = None
        if text.startswith("[") and text.endswith("]"):
            wanted = text.casefold()
            item = next((field_map for field_map in table.fields
                         if field_map.parameter.strip().casefold() == wanted), None)
        if item is None:
            item = table.by_parameter(text)
        if item is None:
            continue
        value = _record_value(record, item.field)
        if value is None or not str(value).strip():
            continue
        terms.append({"cmp": [item.field, "=", str(value)]})
        used.append({"field": item.field, "parameter": item.design_parameter or item.parameter,
                     "value": str(value)})
    if not terms:
        return None, [], ("none of " + ", ".join(by) + " is mapped in table "
                          f"'{table.name}' with a value in the record")
    where = terms[0] if len(terms) == 1 else {"and": terms}
    if not db_query.check_where(where):
        return None, [], "a field name cannot be quoted safely"
    return (db_query.Query(table.name, where, ALTERNATE_LIMIT + 2,
                           _schema(link.library.library, table)), used, "")


def _alternate_rows(link: Link, lookup: Lookup, record: dict, result: db_query.QueryResult,
                    limit: int) -> tuple[list[dict], bool]:
    rows = []
    keys = [item.field for item in lookup.table.key_fields]
    shown = [item for item in lookup.table.fields if item.mapped]

    def key_of(row: dict) -> tuple:
        return tuple(_text(_record_value(row, name)).casefold() for name in keys)

    own = key_of(record)
    for candidate in result.records():
        # The record itself, and its duplicates (the same key value), are no alternates.
        if candidate == record or (keys and key_of(candidate) == own):
            continue
        row: dict = {}
        for name in keys:
            row[name] = _record_value(candidate, name)
        for item in shown:
            label = item.design_parameter or item.parameter
            if item.field in keys:
                continue
            row[label] = _record_value(candidate, item.field)
        rows.append(row)
    more = result.truncated or len(rows) > limit
    return rows[:limit], more


def part_detail(link: Link, lookup: Lookup, record: dict) -> dict:
    """A record's fields against the part: every mapped field with the parameter it
    maps to, the symbol and footprint the record names, and the unmapped fields."""
    rows, findings = compare_record(link, lookup, record)
    mapped = {item.field.casefold() for item in lookup.table.fields if item.mapped}
    unmapped = {key: value for key, value in record.items() if key.casefold() not in mapped}
    symbols = [_record_value(record, item.field) for item in lookup.table.system_fields("symbol_ref")]
    footprints = [_record_value(record, item.field)
                  for item in lookup.table.system_fields("footprint_ref")]
    footprints = [value for value in footprints if value]
    detail = {
        "table": lookup.table.name,
        "matched_by": lookup.matched_by,
        "fields": rows,
        "unmapped_fields": unmapped,
        "symbol": {"database": symbols[0] if symbols else None,
                   "placed": link.part.lib_reference or None},
        "footprint": {"database": footprints, "placed": link.part.footprint or None,
                      "part_models": list(link.part.footprints)},
        "drift": sort_findings(findings),
    }
    paths = {item.system: _record_value(record, item.field)
             for item in lookup.table.fields if item.system in ("symbol_path", "footprint_path")}
    if paths.get("symbol_path"):
        detail["symbol"]["database_path"] = paths["symbol_path"]
    if paths.get("footprint_path"):
        detail["footprint"]["database_path"] = paths["footprint_path"]
    detail["symbol"]["same"] = (detail["symbol"]["database"] is None or _same(
        link.part.lib_reference, detail["symbol"]["database"]) != "differs")
    detail["footprint"]["same"] = not footprints or bool(link.part.footprint) and (
        link.part.footprint.casefold() == footprints[0].casefold())
    return detail


@dataclass
class PartInputs:
    prj_path: str
    netlist: dict
    plan: object = None
    installed: list[str] = field(default_factory=list)
    designator: str | None = None
    design_item: str | None = None
    key: str | None = None
    library: str | None = None
    table: str | None = None
    allow_network: bool = False
    alternates_by: list[str] | None = None


def find_part(parts: list[Part], designator: str) -> Part | None:
    wanted = designator.strip().casefold()
    return next((part for part in parts if part.designator.casefold() == wanted), None)


def build_part(inputs: PartInputs) -> dict:
    """get_db_part's model, or {'error': ...}."""
    parts = collect_parts(inputs.netlist, inputs.plan)
    libraries, missing = find_libraries(inputs.prj_path, parts, inputs.installed,
                                        allow_network=inputs.allow_network)
    if inputs.library:
        libraries = [library for library in libraries if library_matches(library, inputs.library)]
        if not libraries:
            return {"error": "library_not_found",
                    "message": f"No database library or link file '{inputs.library}' was found "
                               "for the project."}
    query: dict = {}
    part = None
    also = []
    if inputs.designator:
        part = find_part(parts, inputs.designator)
        if part is None:
            # A sheet part outside the compiled list (compile-masked, a sheet designator
            # of a multi-channel design): looked up as the sheet has it.
            wanted = inputs.designator.strip().casefold()
            name = next((name for name in inputs.netlist.get("components") or {}
                         if name.casefold() == wanted), None)
            part = make_part(name, inputs.netlist, inputs.plan) if name else None
        if part is None:
            return {"error": "part_not_found",
                    "message": f"No placed part '{inputs.designator}': use search_components "
                               "to find its designator."}
        query["designator"] = part.designator
    elif inputs.design_item:
        wanted = inputs.design_item.strip().casefold()
        placed = [candidate for candidate in parts
                  if candidate.design_item_id.strip().casefold() == wanted]
        query["design_item"] = inputs.design_item
        if placed:
            part = placed[0]
            also = [candidate.designator for candidate in placed[1:]]
    if part is None:
        key = inputs.key if inputs.key is not None else inputs.design_item
        query.setdefault("key", key)
        return _key_search(libraries, key, query, inputs)
    links = [link for link in link_parts([part], libraries)]
    placed_links = [link for link in links if not link.alternate]
    if inputs.table:
        wanted = inputs.table.strip().casefold()
        for link in placed_links:
            link.tables = [table for table in link.tables if table.name.casefold() == wanted]
            link.lookups = [lookup for lookup in link.lookups
                            if lookup.table.name.casefold() == wanted]
    run_lookups(libraries, placed_links, inputs.allow_network)
    out: dict = {"query": query, "part": part.brief()}
    if also:
        out["also_placed_as"] = also[:20]
    if part.params:
        out["part"]["parameters"] = len(part.params)
    name = part.database_name
    if name and not any(link.via == VIA_DBLIB for link in placed_links):
        out["not_linked"] = (f"the part names database library '{Path(name).name}', which is "
                             "not among the project's documents, installed libraries or "
                             "search paths" if any(entry["library"] == Path(
                                 re.split(r"[\\/]", name)[-1]).name for entry in missing)
                             else f"the part names database library '{Path(name).name}', "
                                  "which was not found")
    matches_out, problems, skipped = [], [], []
    truncated = False
    for link in placed_links:
        library = link.library
        for lookup in link.lookups:
            # A database link file's key_missing only means the part is not linked.
            if lookup.problem and (link.via == VIA_DBLIB or lookup.problem in TABLE_PROBLEMS):
                problems.append({"library": library.name, "table": lookup.table.name,
                                 "problem": lookup.problem, "detail": lookup.detail})
        for problem in link.problems:
            problems.append({"library": library.name, "problem": problem["kind"],
                             "detail": problem["detail"]})
        if library.status != "queried":
            if library.reason() and not any(entry["library"] == library.name
                                            for entry in skipped):
                skipped.append({"library": library.name, "reason": library.reason(),
                                "detail": db_query.SKIP_MESSAGES.get(library.reason(), "")})
            continue
        errors = link.errors()
        if errors:
            problems.append({"library": library.name, "problem": "query_failed",
                             "errors": errors, "detail": _query_error_detail(errors)})
        resolved = link.resolve()
        truncated = truncated or resolved.truncated
        for position, record in enumerate(resolved.records):
            lookup = resolved.lookup
            entry = {"library": library.name, "via": link.via, "linked_by": link.how,
                     **part_detail(link, lookup, record)}
            if resolved.conflict or len(resolved.records) > 1:
                entry["confidence"] = "ambiguous"
            elif lookup.fallback:
                entry["confidence"] = "design_item_id"
            else:
                entry["confidence"] = "exact"
            if resolved.conflict:
                entry["key_conflict"] = dict(resolved.conflict)
            if resolved.other_tables:
                entry["also_found_in_tables"] = list(resolved.other_tables)
            entry["_link"], entry["_lookup"], entry["_record"] = link, lookup, record
            entry["position"] = position
            matches_out.append(entry)
    out["libraries"] = [{"name": library.name, "kind": library.kind,
                         "found_by": list(library.origins),
                         "connection": library.library.connection.describe()
                         if library.library is not None else None,
                         "status": library.status}
                        for library in libraries
                        if any(link.library is library for link in placed_links)]
    out["records_found"] = len(matches_out)
    if len(matches_out) > 1:
        out["duplicates"] = True
    if truncated:
        out["records_truncated"] = True  # more records than the lookup reads (5)
    if skipped:
        out["skipped"] = skipped
    if problems:
        out["problems"] = problems
    if not placed_links and "not_linked" not in out:
        excluded = [(library, library.excluded[part.designator]) for library in libraries
                    if part.designator in library.excluded]
        if excluded:
            out["not_linked"] = "; ".join(
                f"the part is restricted to its table '{table}' (UseDBTableName), which "
                f"{library.name} " + ("does not have" if reason == "table_not_in_link"
                                      else "has disabled")
                for library, (reason, table) in excluded)
        elif any(library.kind == "DbLink" for library in libraries):
            out["not_linked"] = ("the part is not placed from a database library and the "
                                 "project's database link files have no enabled table")
        else:
            out["not_linked"] = ("the part is not placed from a database library and no "
                                 "database link file applies to it")
    elif placed_links and not matches_out and not skipped and not problems:
        via_link = all(link.via == VIA_DBLINK for link in placed_links)
        if via_link and not any(not lookup.problem for link in placed_links
                                for lookup in link.lookups):
            out["not_linked"] = "the part lacks the key parameter of every database link table"
        else:
            out["record_not_found"] = True
    alternates = [link for link in links if link.alternate]
    if alternates:
        run_lookups(libraries, alternates, inputs.allow_network)
        link = alternates[0]
        found = link.matches()
        out["variant_alternate"] = {"design_item_id": link.part.design_item_id or None,
                                    "table": link.tables[0].name if link.tables else None,
                                    "record_found": bool(found)}
    out["matches"] = matches_out
    return out


def _key_search(libraries: list[FoundLibrary], key: str | None, query: dict,
                inputs: PartInputs) -> dict:
    if not key or not str(key).strip():
        return {"error": "nothing_to_look_up",
                "message": "Pass designator= (a placed part), design_item= or key= (a key "
                           "field value)."}
    key = str(key).strip()
    links = []
    for library in libraries:
        if library.library is None:
            continue
        stand_in = Part(designator="")
        link = Link(stand_in, library, VIA_DBLINK if library.kind == "DbLink" else VIA_DBLIB,
                    "key value")
        for table in library.library.enabled_tables:
            if inputs.table and table.name.casefold() != inputs.table.strip().casefold():
                continue
            link.tables.append(table)
            if table.user_where and table.where_text.strip():
                link.lookups += _where_lookups(library.library, table, None, key)
            else:
                link.lookups += _key_lookups(table, stand_in, link.via, key)
        _refuse_unsafe(library.library, link.lookups)
        if link.tables:
            links.append(link)
    run_lookups(libraries, links, inputs.allow_network)
    matches, skipped, problems = [], [], []
    truncated = False
    for link in links:
        library = link.library
        for lookup in link.lookups:
            if lookup.problem:
                problems.append({"library": library.name, "table": lookup.table.name,
                                 "problem": lookup.problem, "detail": lookup.detail})
        if library.status != "queried":
            if library.reason():
                skipped.append({"library": library.name, "reason": library.reason(),
                                "detail": db_query.SKIP_MESSAGES.get(library.reason(), "")})
            continue
        for lookup in link.lookups:
            if lookup.problem:
                continue
            if lookup.result is not None and lookup.result.error:
                problems.append({"library": library.name, "table": lookup.table.name,
                                 "problem": "query_failed", "error": lookup.result.error,
                                 "detail": _query_error_detail([lookup.result.error])})
            elif lookup.result is not None:
                truncated = truncated or lookup.result.truncated
                for record in lookup.result.records():
                    entry = {"library": library.name, "via": link.via, "table": lookup.table.name,
                             "matched_by": lookup.matched_by,
                             "fields": _record_fields(lookup.table, record),
                             "unmapped_fields": {name: value for name, value in record.items()
                                                 if name.casefold() not in {
                                                     item.field.casefold()
                                                     for item in lookup.table.fields
                                                     if item.mapped}}}
                    entry["_link"], entry["_lookup"], entry["_record"] = link, lookup, record
                    matches.append(entry)
    out: dict = {"query": query, "records_found": len(matches), "matches": matches,
                 "libraries": [{"name": library.name, "kind": library.kind,
                                "found_by": list(library.origins), "status": library.status}
                               for library in libraries if library.library is not None]}
    if not libraries:
        out["note"] = ("The project uses no database library: no .DbLib, .SVNDbLib or .DbLink "
                       "among its documents, the installed libraries or its search paths.")
    if skipped:
        out["skipped"] = skipped
    if problems:
        out["problems"] = problems
    if len(matches) > 1:
        out["duplicates"] = True
    if truncated:
        out["records_truncated"] = True  # a table holds more records than are read (5)
    return out


def _record_fields(table: TableLink, record: dict) -> list[dict]:
    rows = []
    for item in table.fields:
        if not item.mapped:
            continue
        row = {"field": item.field, "parameter": item.design_parameter or item.parameter,
               "database": _record_value(record, item.field)}
        if item.system:
            row["system_field"] = item.system
        if item.key:
            row["key"] = True
        rows.append(row)
    return rows


def add_alternates(model: dict, by: list[str] | None, allow_network: bool) -> None:
    """Other records with the same value and footprint (or by) for the first match of a
    get_db_part model, in place: alternates."""
    matches = model.get("matches") or []
    if not matches:
        return
    first = matches[0]
    link, lookup, record = first["_link"], first["_lookup"], first["_record"]
    wanted = list(by or ALTERNATES_BY)
    query, used, reason = _alternate_queries(link, lookup, record, wanted)
    if query is None:
        model["alternates"] = {"by": wanted, "unavailable": reason}
        return
    library = link.library
    if library.plan is None:
        model["alternates"] = {"by": wanted, "unavailable": "the library was not queried"}
        return
    try:
        result = db_query.run_queries(library.plan, [query])[0]
    except db_query.QueryError as error:
        model["alternates"] = {"by": wanted, "unavailable": f"query failed ({error.code})"}
        return
    if result.error:
        model["alternates"] = {"by": wanted, "unavailable": f"query failed ({result.error})"}
        return
    rows, more = _alternate_rows(link, lookup, record, result, ALTERNATE_LIMIT)
    model["alternates"] = {"by": used, "table": lookup.table.name, "rows": rows, "more": more}


def page_part(model: dict, alternates: int, unmapped: int) -> dict:
    """get_db_part's result at a size: alternates rows and unmapped fields per record
    cut to the limits (the model keeps everything)."""
    out = {key: value for key, value in model.items() if key not in ("matches", "alternates")}
    matches = []
    for entry in model.get("matches") or []:
        shown = {key: value for key, value in entry.items() if not key.startswith("_")
                 and key != "position"}
        fields = shown.get("unmapped_fields") or {}
        if len(fields) > unmapped:
            shown["unmapped_fields"] = dict(list(fields.items())[:unmapped])
            shown["unmapped_fields_total"] = len(fields)
        matches.append(shown)
    out["matches"] = matches
    if "alternates" in model:
        alternate = dict(model["alternates"])
        if "rows" in alternate and len(alternate["rows"]) > alternates:
            alternate["rows"] = alternate["rows"][:alternates]
            alternate["more"] = True
        out["alternates"] = alternate
    return out


# ---------------------------------------------------------------------------
# Cache (check_db_parameters pages read one model)
# ---------------------------------------------------------------------------

_CACHE: dict[tuple, tuple[float, dict]] = {}


def cached_check(key: tuple, build) -> dict:
    now = time.monotonic()
    held = _CACHE.get(key)
    if held and now - held[0] < CACHE_SECONDS:
        return held[1]
    model = build()
    while len(_CACHE) >= CACHE_SIZE:
        del _CACHE[next(iter(_CACHE))]
    _CACHE[key] = (now, model)
    return model


def clear_cache() -> None:
    _CACHE.clear()


def library_signature(prj_path: str) -> tuple:
    """Modification times of the project file (its documents and search paths decide
    which libraries are found): part of the cache key."""
    try:
        return (os.path.getmtime(prj_path),)
    except OSError:
        return (0.0,)
