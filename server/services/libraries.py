"""The libraries behind a project's parts (roadmap #52): which library files the project
can use, and which symbol and footprint each placed part resolves to on disk.

Altium finds a part's library items among the libraries available to the project,
searched in the order of the Available File-based Libraries dialog's tabs: the project's
own libraries ([DocumentN] entries of the .PrjPcb, in order), the installed libraries,
then the project's search paths ([SearchPathN] Path, a folder and file pattern,
IncludeSubFolders); then the library file the part was placed from when its path is
known. Names match exactly first, then differing only in case.

A symbol placed from a named library file (SourceLibraryName: X.SchLib or X.IntLib) is
looked up in that library only, by its Design Item ID (the library component's name)
and then its library reference; when the library is missing or lacks it, a symbol of
that name in another available library is reported as a hint, not as the part's. A
symbol naming no library file is searched for in every available library. A footprint
is looked up in the library its link or placement names (a footprint link's
ModelDataFile0, the PCB's SOURCEFOOTPRINTLIBRARY, the part's IntLib) first, then in
every available library, as a footprint link set to Any library is.

Parts that live outside library files cannot be resolved offline: Workspace (A365) parts
keep their symbol and footprint in the Workspace (a footprint whose cached PcbLib is
still on disk, as the PCB records it, is read from the cache), and database-library
parts (DbLib, SVNDbLib) take their links from a database (roadmap #53). They are
reported with that reason, as are parts whose library file is missing and names no
library has.

On top of that (check_libraries, get_library_part): each placed footprint against its
library footprint (compare_footprint, Altium's Footprint Comparison Report), the pins on
nets against the pads, Altium's SchLib and PcbLib Component Rule Checks
(symbol_rule_findings, footprint_rule_findings, pin_pad_findings, copper_report), net
ties, 3D bodies against the fallback Height and where-used across projects. Lengths are
in mil; pad positions in the footprint's own frame.
"""

import fnmatch
import math
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import olefile

try:
    from parsers.int_lib import parse_int_lib
    from parsers.pcb_doc import (
        MULTI_LAYER, PAD_SHAPE_OCTAGONAL, PAD_SHAPE_RECT, PAD_SHAPE_ROUND, PAD_SHAPE_ROUNDRECT,
        decode_components, decode_fill_records, decode_region_records, read_text_records,
    )
    from parsers.pcb_lib import Footprint, parse_pcb_lib
    from parsers.prj_pcb import parse_prj_documents, split_sections
    from parsers.sch_doc import components_from_stream
    from parsers.sch_lib import SchLibComponent, parse_sch_lib
    from services.bom import natural_key
    from services.pcb_geometry import oriented_rect_vertices
    from services.pcb_index import PcbIndex
except ImportError:
    from server.parsers.int_lib import parse_int_lib
    from server.parsers.pcb_doc import (
        MULTI_LAYER, PAD_SHAPE_OCTAGONAL, PAD_SHAPE_RECT, PAD_SHAPE_ROUND, PAD_SHAPE_ROUNDRECT,
        decode_components, decode_fill_records, decode_region_records, read_text_records,
    )
    from server.parsers.pcb_lib import Footprint, parse_pcb_lib
    from server.parsers.prj_pcb import parse_prj_documents, split_sections
    from server.parsers.sch_doc import components_from_stream
    from server.parsers.sch_lib import SchLibComponent, parse_sch_lib
    from server.services.bom import natural_key
    from server.services.pcb_geometry import oriented_rect_vertices
    from server.services.pcb_index import PcbIndex

LIBRARY_KINDS = {
    ".schlib": "SchLib", ".pcblib": "PcbLib", ".intlib": "IntLib", ".dblib": "DbLib",
    ".svndblib": "SVNDbLib", ".dblink": "DbLink", ".libpkg": "LibPkg",
}
SYMBOL_KINDS = ("SchLib", "IntLib")
FOOTPRINT_KINDS = ("PcbLib", "IntLib")
DATABASE_KINDS = ("DbLib", "SVNDbLib")

ORIGIN_PROJECT = "project"
ORIGIN_SEARCH_PATH = "search path"
ORIGIN_INSTALLED = "installed"
ORIGIN_SOURCE = "source"  # the library file the part was placed from, by its path
ORIGIN_WORKSPACE_CACHE = "workspace cache"

REASON_WORKSPACE = "workspace part"
REASON_DATABASE = "database library part"
REASON_FILE_MISSING = "library file missing"
REASON_NOT_FOUND = "name not found"
REASON_NO_NAME = "no name to look up"

# Altium's own library folders: AD<version>\Library under Public Documents (the
# installer's IntLibs) and the libraries extensions bring under ProgramData.
INSTALLED_LIBRARY_GLOBS = (
    r"C:\Users\Public\Documents\Altium\AD*\Library",
    r"C:\ProgramData\Altium\Altium Designer *\Extensions\*\Library",
)
_INSTALLED_LIST = "IntLib_Installed.ini"


def library_kind(name: str) -> str:
    """SchLib, PcbLib, IntLib, DbLib, SVNDbLib, DbLink or LibPkg by extension; ''."""
    return LIBRARY_KINDS.get(os.path.splitext(name.strip())[1].lower(), "")


def workspace_name(name: str) -> bool:
    """A library name that is not a library file but a Workspace (A365) one, such as
    the content vault Altium's own parts come from."""
    return not library_kind(name) and bool(
        re.search(r"vault|workspace|\.designlib$", name.strip(), re.IGNORECASE))


def _base(name: str) -> str:
    return re.split(r"[\\/]", name.strip())[-1].casefold()


@dataclass
class LibraryFile:
    path: str
    kind: str
    origin: str
    exists: bool


@dataclass
class SearchPath:
    raw: str  # Path as written
    folder: str  # resolved against the project folder
    pattern: str  # the file pattern (*.* when none)
    include_subfolders: bool
    exists: bool


@dataclass
class ProjectLibraries:
    project: str
    project_libraries: list[LibraryFile] = field(default_factory=list)
    search_paths: list[SearchPath] = field(default_factory=list)
    search_path_libraries: list[LibraryFile] = field(default_factory=list)
    installed: list[LibraryFile] = field(default_factory=list)

    def available(self) -> list[LibraryFile]:
        """Every library file on disk the project can use, in Altium's search order: the
        Available File-based Libraries dialog's tabs in order (Project, Installed, Search
        Path), each in its own order."""
        return [library for library in (*self.project_libraries, *self.installed,
                                         *self.search_path_libraries) if library.exists]


def _search_paths(prj_path: str) -> list[SearchPath]:
    folder = Path(prj_path).parent
    text = Path(prj_path).read_text(encoding="utf-8-sig", errors="replace")
    paths = []
    for header, lines in split_sections(text):
        if not re.fullmatch(r"SearchPath\d+", header, re.IGNORECASE):
            continue
        fields = {key.strip().lower(): value.strip() for key, _, value in
                  (line.partition("=") for line in lines)}
        raw = fields.get("path", "")
        if not raw:
            continue
        normalized = raw.replace("\\", "/")
        head, _, tail = normalized.rpartition("/")
        if "*" in tail or "?" in tail:
            target, pattern = Path(head or "."), tail
        else:
            target, pattern = Path(normalized), "*.*"
        resolved = str(target if target.is_absolute() else (folder / target).resolve())
        paths.append(SearchPath(raw, resolved, pattern,
                                fields.get("includesubfolders", "").lower() in ("1", "true"),
                                os.path.isdir(resolved)))
    return paths


def _search_path_files(search_path: SearchPath) -> list[str]:
    """The library files a search path covers, in sorted-walk order."""
    if not search_path.exists:
        return []
    found = []
    for dirpath, dirnames, filenames in os.walk(search_path.folder):
        dirnames.sort()
        for name in sorted(filenames):
            if library_kind(name) and fnmatch.fnmatch(name.lower(), search_path.pattern.lower()):
                found.append(os.path.join(dirpath, name))
        if not search_path.include_subfolders:
            break
    return found


def installed_libraries(globs: tuple[str, ...] = INSTALLED_LIBRARY_GLOBS,
                        profile: str | None = None) -> list[str]:
    """The installed libraries found on this PC: every library file under Altium's own
    library folders (globs), then the existing files of the IntLib_Installed.ini lists
    under the Altium profile folder (default %APPDATA%\\Altium). One file per name."""
    import glob

    found: list[str] = []
    for pattern in globs:
        for folder in sorted(glob.glob(pattern)):
            for dirpath, dirnames, filenames in os.walk(folder):
                dirnames.sort()
                found += [os.path.join(dirpath, name) for name in sorted(filenames)
                          if library_kind(name)]
    profile = profile if profile is not None else os.path.join(os.environ.get("APPDATA", ""), "Altium")
    if profile and os.path.isdir(profile):
        for ini in sorted(glob.glob(os.path.join(profile, "**", _INSTALLED_LIST), recursive=True)):
            try:
                text = Path(ini).read_text(encoding="utf-8-sig", errors="replace")
            except OSError:
                continue
            for match in re.finditer(r"^Library\d+=(.+)$", text, re.MULTILINE):
                path = match.group(1).strip()
                if library_kind(path) and os.path.isfile(path):
                    found.append(path)
    unique: dict[str, str] = {}
    for path in found:
        unique.setdefault(_base(path), path)
    return list(unique.values())


def project_libraries(prj_path: str, installed: list[str] | None = None) -> ProjectLibraries:
    """The libraries a project can use. installed: the installed library files (default
    installed_libraries())."""
    libraries = ProjectLibraries(project=prj_path)
    for document in parse_prj_documents(prj_path):
        kind = library_kind(document.raw)
        if kind:
            libraries.project_libraries.append(
                LibraryFile(document.path, kind, ORIGIN_PROJECT, os.path.isfile(document.path)))
    libraries.search_paths = _search_paths(prj_path)
    seen = {os.path.normcase(library.path) for library in libraries.project_libraries}
    for path in installed_libraries() if installed is None else installed:
        if os.path.normcase(path) not in seen:
            seen.add(os.path.normcase(path))
            libraries.installed.append(
                LibraryFile(path, library_kind(path), ORIGIN_INSTALLED, os.path.isfile(path)))
    for search_path in libraries.search_paths:
        for path in _search_path_files(search_path):
            if os.path.normcase(path) not in seen:
                seen.add(os.path.normcase(path))
                libraries.search_path_libraries.append(
                    LibraryFile(path, library_kind(path), ORIGIN_SEARCH_PATH, True))
    return libraries


@dataclass
class LibraryIndex:
    """The names a library file holds: components (with aliases) and footprints, each
    as written; error when the file could not be read."""

    path: str
    kind: str
    components: list[str] = field(default_factory=list)
    footprints: list[str] = field(default_factory=list)
    error: str = ""

    def match(self, name: str, footprint: bool) -> tuple[str, bool] | None:
        """(the name as the library has it, whether only its case differs) or None."""
        names = self.footprints if footprint else self.components
        if name in names:
            return name, False
        folded = name.casefold()
        held = next((held for held in names if held.casefold() == folded), None)
        return (held, True) if held is not None else None


_INDEX_CACHE: dict[tuple[str, int, int], LibraryIndex] = {}


def library_index(path: str, kind: str | None = None) -> LibraryIndex:
    """The names a SchLib, PcbLib or IntLib holds (read from its headers; cached by
    path, size and modification time)."""
    kind = kind or library_kind(path)
    try:
        stat = os.stat(path)
    except OSError as error:
        return LibraryIndex(path, kind, error=f"{type(error).__name__}: {error}")
    key = (os.path.normcase(path), stat.st_size, stat.st_mtime_ns)
    if key in _INDEX_CACHE:
        return _INDEX_CACHE[key]
    index = LibraryIndex(path, kind)
    try:
        if kind == "SchLib":
            library = parse_sch_lib(path, names=set())
            index.components = library.header_names + [
                alias for held in library.aliases.values() for alias in held]
        elif kind == "PcbLib":
            index.footprints = parse_pcb_lib(path, names=set()).names
        elif kind == "IntLib":
            library = parse_int_lib(path, components=set(), footprints=set())
            for sch_lib in library.sch_libs:
                index.components += sch_lib.header_names
            for pcb_lib in library.pcb_libs:
                index.footprints += pcb_lib.names
            if library.warnings and not (library.sch_libs or library.pcb_libs):
                index.error = "; ".join(library.warnings)
    except Exception as error:  # an unreadable library is reported, not raised
        index.error = f"{type(error).__name__}: {error}"
    _INDEX_CACHE[key] = index
    return index


@dataclass
class LibraryHit:
    """Where a symbol or footprint was found. path is the library file (an IntLib for
    its embedded libraries); named: the library the part names, not one found by
    searching every available library."""

    path: str
    kind: str
    origin: str
    name: str  # as the library writes it
    case_differs: bool = False
    named: bool = False


@dataclass
class PartResolution:
    designator: str
    source: str  # "schematic" (netlist component) or "pcb" (PcbDoc component)
    lib_reference: str
    footprint: str
    design_item_id: str = ""
    library: str = ""  # the library the part names (SourceLibraryName / component lib)
    footprint_library: str = ""  # the footprint's library as recorded, if any
    symbol: LibraryHit | None = None
    symbol_reason: str = ""  # why symbol is None (a REASON_*), "" when found
    # A symbol of that name in another available library, when the library the part
    # names is missing or lacks it (Altium would not use it; a hint for the user).
    symbol_elsewhere: LibraryHit | None = None
    footprint_hit: LibraryHit | None = None
    footprint_reason: str = ""
    # Library files the lookups could not read (path -> error).
    errors: dict[str, str] = field(default_factory=dict)


class LibraryResolver:
    """Resolve parts against the libraries a project can use (project_libraries)."""

    def __init__(self, libraries: ProjectLibraries):
        self.libraries = libraries
        self.available = libraries.available()

    def _by_name(self, library_name: str, kinds: tuple[str, ...]) -> list[LibraryFile]:
        """The available libraries of kinds the part names (a file name or a path),
        then the named file itself by its path when it exists but is not available."""
        if not library_name or library_kind(library_name) not in kinds:
            return []
        wanted = _base(library_name)
        found = [library for library in self.available
                 if library.kind in kinds and _base(library.path) == wanted]
        if not found and os.path.isabs(library_name) and os.path.isfile(library_name):
            found.append(LibraryFile(library_name, library_kind(library_name),
                                     ORIGIN_SOURCE, True))
        return found

    def _lookup(self, names: list[str], named: list[LibraryFile], kinds: tuple[str, ...],
                footprint: bool, errors: dict[str, str]) -> LibraryHit | None:
        """The first library, named ones first and then every available one of kinds in
        order, that holds one of names (tried in order in each library)."""
        candidates = [(library, True) for library in named]
        candidates += [(library, False) for library in self.available
                       if library.kind in kinds and library not in named]
        for library, is_named in candidates:
            index = library_index(library.path, library.kind)
            if index.error:
                errors[library.path] = index.error
                continue
            for name in names:
                match = index.match(name, footprint)
                if match:
                    return LibraryHit(library.path, library.kind, library.origin, match[0],
                                      match[1], is_named)
        return None

    def find_symbol(self, names: list[str], library: str = "", workspace: bool = False,
                    database: bool = False, errors: dict[str, str] | None = None,
                    ) -> tuple[LibraryHit | None, str]:
        """(hit, reason) for a symbol placed from library (a file name, a Workspace
        name or empty). names: the keys a library may hold it under, in order: the
        Design Item ID (a library component's name) and the library reference."""
        errors = {} if errors is None else errors
        names = [name for name in dict.fromkeys(names) if name]
        kind = library_kind(library)
        if database or kind in DATABASE_KINDS:
            return None, REASON_DATABASE
        if kind not in SYMBOL_KINDS and (workspace or workspace_name(library)):
            return None, REASON_WORKSPACE
        if not names:
            return None, REASON_NO_NAME
        if kind in SYMBOL_KINDS:
            # Placed from a named library: Altium looks for it there only.
            named = self._by_name(library, SYMBOL_KINDS)
            hit = self._lookup(names, named, (), False, errors)
            if hit:
                return hit, ""
            return None, REASON_NOT_FOUND if named else REASON_FILE_MISSING
        hit = self._lookup(names, [], SYMBOL_KINDS, False, errors)
        return (hit, "") if hit else (None, REASON_NOT_FOUND)

    def elsewhere(self, names: list[str], footprint: bool) -> LibraryHit | None:
        """The first available library holding one of names, wherever the part says it
        came from: a hint for a part whose own library is missing or lacks it."""
        kinds = FOOTPRINT_KINDS if footprint else SYMBOL_KINDS
        return self._lookup([name for name in names if name], [], kinds, footprint, {})

    def find_footprint(self, name: str, library: str = "", component_library: str = "",
                       workspace: bool = False, errors: dict[str, str] | None = None,
                       search_all: bool = True) -> tuple[LibraryHit | None, str]:
        """(hit, reason) for a footprint whose link or placement names library (a
        library file name or path; for a Workspace part, the path of its cached
        PcbLib), placed with a part from component_library. search_all False: in the
        named libraries only."""
        errors = {} if errors is None else errors
        if not name:
            return None, REASON_NO_NAME
        kind = library_kind(library)
        workspace = (workspace or (kind not in FOOTPRINT_KINDS and workspace_name(library))
                     or (library_kind(component_library) not in SYMBOL_KINDS
                         and workspace_name(component_library)))
        if workspace:
            # A Workspace footprint is only in the Workspace and its local file cache.
            if kind in FOOTPRINT_KINDS and os.path.isabs(library) and os.path.isfile(library):
                cached = LibraryFile(library, kind, ORIGIN_WORKSPACE_CACHE, True)
                hit = self._lookup([name], [cached], (), True, errors)
                return (hit, "") if hit else (None, REASON_NOT_FOUND)
            return None, REASON_WORKSPACE
        named = self._by_name(library, FOOTPRINT_KINDS)
        if library_kind(component_library) == "IntLib":
            named += [library for library in self._by_name(component_library, ("IntLib",))
                      if library not in named]
        # Footprints are searched for in every available library after the named ones,
        # as a footprint link set to Any library does.
        hit = self._lookup([name], named, FOOTPRINT_KINDS if search_all else (), True, errors)
        if hit:
            return hit, ""
        return None, REASON_FILE_MISSING if kind and not named else REASON_NOT_FOUND

    def resolve_netlist(self, netlist: dict) -> list[PartResolution]:
        """A PartResolution per netlist component (sch_doc's library fields)."""
        results = []
        for designator, component in sorted((netlist.get("components") or {}).items()):
            result = PartResolution(
                designator=designator, source="schematic",
                lib_reference=component.get("lib_reference") or "",
                design_item_id=component.get("design_item_id") or "",
                footprint=component.get("footprint") or "",
                library=component.get("library") or "",
                footprint_library=component.get("footprint_library") or "")
            workspace = bool(component.get("workspace_part"))
            result.symbol, result.symbol_reason = self.find_symbol(
                [result.design_item_id, result.lib_reference], result.library, workspace,
                bool(component.get("database_table")), result.errors)
            if result.symbol_reason in (REASON_FILE_MISSING, REASON_NOT_FOUND):
                result.symbol_elsewhere = self.elsewhere(
                    [result.design_item_id, result.lib_reference], footprint=False)
            footprint_library = result.footprint_library
            if not footprint_library and library_kind(result.library) == "IntLib":
                footprint_library = result.library  # the footprint is in the part's IntLib
            result.footprint_hit, result.footprint_reason = self.find_footprint(
                result.footprint, footprint_library, result.library, workspace,
                result.errors)
            results.append(result)
        return results

    def resolve_pcb(self, components) -> list[PartResolution]:
        """A PartResolution per PcbDoc component (pcb_doc.PcbComponent); its
        SOURCELIBREFERENCE is the library component's name."""
        results = []
        for component in components:
            result = PartResolution(
                designator=component.designator or component.refdes or "",
                source="pcb", lib_reference=component.lib_reference,
                footprint=component.footprint or "", library=component.component_library,
                footprint_library=component.footprint_library)
            result.symbol, result.symbol_reason = self.find_symbol(
                [result.lib_reference], result.library, component.workspace_part,
                errors=result.errors)
            if result.symbol_reason in (REASON_FILE_MISSING, REASON_NOT_FOUND):
                result.symbol_elsewhere = self.elsewhere([result.lib_reference], footprint=False)
            result.footprint_hit, result.footprint_reason = self.find_footprint(
                result.footprint, result.footprint_library, result.library,
                component.workspace_part, result.errors)
            results.append(result)
        return results


def load_symbol(hit: LibraryHit) -> SchLibComponent | None:
    """The full library component a hit names."""
    wanted = {hit.name.casefold()}
    if hit.kind == "IntLib":
        found = parse_int_lib(hit.path, components=wanted, footprints=set()).find_component(hit.name)
        return found[1] if found else None
    return parse_sch_lib(hit.path, names=wanted).find(hit.name)


def load_footprint(hit: LibraryHit) -> Footprint | None:
    """The full library footprint a hit names."""
    wanted = {hit.name.casefold()}
    if hit.kind == "IntLib":
        found = parse_int_lib(hit.path, components=set(), footprints=wanted).find_footprint(hit.name)
        return found[1] if found else None
    return parse_pcb_lib(hit.path, names=wanted).find(hit.name)


def resolution_summary(results: list[PartResolution]) -> dict:
    """Counts: parts, symbols and footprints found (by origin) and not (by reason)."""
    summary: dict = {"parts": len(results), "symbols": {}, "footprints": {}}
    for result in results:
        for key, hit, reason in (("symbols", result.symbol, result.symbol_reason),
                                 ("footprints", result.footprint_hit, result.footprint_reason)):
            label = f"found ({hit.origin})" if hit else reason
            summary[key][label] = summary[key].get(label, 0) + 1
    return summary


# ---------------------------------------------------------------------------
# Checks on the library items behind the parts (roadmap #52, stage B)
# ---------------------------------------------------------------------------

CHECK_RESOLUTION = "resolution"
CHECK_DRIFT = "footprint_drift"
CHECK_PINS = "pads_cover_pins"
CHECK_SYMBOL_RULES = "symbol_rules"
CHECK_FOOTPRINT_RULES = "footprint_rules"
CHECK_NET_TIES = "net_ties"
CHECK_HEIGHT = "height_3d"
CHECK_WHERE_USED = "where_used"
PROJECT_CHECKS = (CHECK_RESOLUTION, CHECK_DRIFT, CHECK_PINS, CHECK_SYMBOL_RULES,
                  CHECK_FOOTPRINT_RULES, CHECK_NET_TIES, CHECK_HEIGHT)
LIBRARY_CHECKS = (CHECK_SYMBOL_RULES, CHECK_FOOTPRINT_RULES, CHECK_NET_TIES, CHECK_HEIGHT)
ALL_CHECKS = (*PROJECT_CHECKS, CHECK_WHERE_USED)
SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}

# ComponentKind (pcb_doc.component_kind): 3 and 4 are net ties.
KIND_NAMES = {0: "Standard", 1: "Mechanical", 2: "Graphical", 3: "Net Tie (In BOM)",
              4: "Net Tie", 5: "Standard (No BOM)", 6: "Jumper"}
NET_TIE_KINDS = (3, 4)
PAD_SHAPES = {PAD_SHAPE_ROUND: "round", PAD_SHAPE_RECT: "rectangular",
              PAD_SHAPE_OCTAGONAL: "octagonal", PAD_SHAPE_ROUNDRECT: "rounded rectangle"}
HOLE_SHAPES = {0: "round", 1: "square", 2: "slot"}

# Library and board store coordinates in 1e-4 mil, the placement's X and Y as text: a
# placed pad lands within a few hundredths of a mil of where its library pad puts it (up
# to 0.016 mil seen on Altium's examples), so anything within POSITION_TOLERANCE_MIL
# (1.3 um) is the same.
POSITION_TOLERANCE_MIL = 0.05
ANGLE_TOLERANCE_DEG = 0.01
TOUCH_MIL = 0.001  # copper this close touches (shorted and unconnected copper)
BODY_HEIGHT_TOLERANCE_MIL = 0.1
EXAMPLES = 8  # pads, pins and designators listed per finding

NOT_AVAILABLE_OFFLINE = [
    "Workspace (Altium 365) parts: their symbols, footprints and 3D models live in the "
    "Workspace, which is not read offline. A Workspace part's footprint is read only "
    "when the PcbDoc records a path to its cached PcbLib that is still on disk.",
    "Database libraries (DbLib, SVNDbLib; roadmap #53): a database part's symbol and "
    "footprint links come from the database, which is not read. The SchLibs and PcbLibs "
    "they point at are searched like any other library.",
    "LibPkg library packages are not compiled: their source libraries count only when "
    "the project lists them or a search path holds them.",
]

_FLIPPED_LAYERS = {1: 32, 32: 1, 33: 34, 34: 33, 35: 36, 36: 35, 37: 38, 38: 37}
_LAYER_NAMES = {1: "Top Layer", 32: "Bottom Layer", 33: "Top Overlay", 34: "Bottom Overlay",
                35: "Top Paste", 36: "Bottom Paste", 37: "Top Solder", 38: "Bottom Solder",
                55: "Drill Guide", 56: "Keep-Out Layer", 73: "Drill Drawing",
                MULTI_LAYER: "Multi-Layer"}


def layer_label(layer: int | None) -> str:
    """Altium's default name of a v6 layer (a board may rename its layers)."""
    if layer is None:
        return "?"
    if layer in _LAYER_NAMES:
        return _LAYER_NAMES[layer]
    if 2 <= layer <= 31:
        return f"Mid-Layer {layer - 1}"
    if 39 <= layer <= 54:
        return f"Internal Plane {layer - 38}"
    if 57 <= layer <= 72:
        return f"Mechanical {layer - 56}"
    return f"layer {layer}"


def flip_layer(layer: int) -> int:
    """The layer a primitive moves to when its footprint is flipped to the other side:
    top and bottom copper, overlay, paste and solder mask swap; the rest stay (the
    board's mechanical layer pairs are not read)."""
    return _FLIPPED_LAYERS.get(layer, layer)


def footprint_frame(x: float, y: float, component) -> tuple[float, float]:
    """A board point in a placed footprint's own frame (as its library stores it).

    Altium places a footprint by mirroring it about its own X axis (y to -y) when it
    goes on the bottom side, then turning it by the component's rotation about the
    component's reference point (X, Y). Checked on every bottom-side part of Altium's
    examples and of a private set of boards whose footprints were not edited."""
    angle = math.radians(component.rotation or 0.0)
    dx, dy = x - (component.x or 0.0), y - (component.y or 0.0)
    cosine, sine = math.cos(angle), math.sin(angle)
    local_x, local_y = dx * cosine + dy * sine, -dx * sine + dy * cosine
    return (local_x, -local_y) if component.side == "bottom" else (local_x, local_y)


def board_frame(x: float, y: float, component) -> tuple[float, float]:
    """A footprint point placed on the board: footprint_frame's inverse."""
    if component.side == "bottom":
        y = -y
    angle = math.radians(component.rotation or 0.0)
    cosine, sine = math.cos(angle), math.sin(angle)
    return ((component.x or 0.0) + x * cosine - y * sine,
            (component.y or 0.0) + x * sine + y * cosine)


def footprint_rotation(rotation: float, component) -> float:
    """A placed pad's rotation in its footprint's frame: the board stores the component's
    rotation plus the pad's on the top side, and minus it on the bottom."""
    turn = component.rotation or 0.0
    return ((turn - rotation) if component.side == "bottom" else (rotation - turn)) % 360.0


@dataclass
class FootprintPad:
    """A pad (or a bare hole) in its footprint's frame (mil), for comparing a placed
    footprint with its library source. layer is the footprint's own (a bottom-side
    placement's layers flipped back)."""

    designator: str
    x: float
    y: float
    width: float
    height: float
    rotation: float
    shape: int | None
    layer: int
    hole: float = 0.0
    hole_shape: int | None = None
    slot_length: float | None = None
    plated: bool | None = None
    corner_radius: float | None = None
    copper: bool = True
    net: int | None = None  # the board pad's net index


def copper_pad_names(pads) -> set[str]:
    """The designators of pads with copper (pcb_doc Primitives), '' left out. Only these
    can carry a pin's connection: a bare hole (a pad without copper) has no copper to
    route to."""
    return {pad.pad_name for pad in pads if pad.pad_name}


def bare_hole_names(holes) -> set[str]:
    """The designators of bare holes (pcb_doc BareHoles), '' left out."""
    return {hole.pad_name for hole in holes if hole.pad_name}


def library_pads(footprint: Footprint) -> list[FootprintPad]:
    """A library footprint's pads, then its bare holes."""
    pads = [FootprintPad(pad.pad_name or "", pad.x1, pad.y1, pad.width, pad.height or pad.width,
                         pad.rotation % 360.0, pad.shape, pad.layer, pad.hole or 0.0,
                         pad.hole_shape, pad.slot_length, pad.plated, pad.corner_radius)
            for pad in footprint.pads]
    pads += [FootprintPad(hole.pad_name, hole.x, hole.y, 0.0, 0.0, hole.rotation % 360.0, None,
                          hole.layer, hole.hole, hole.hole_shape, hole.slot_length, hole.plated,
                          copper=False)
             for hole in footprint.holes]
    return pads


def placed_pads(component, pads, holes=()) -> list[FootprintPad]:
    """A placed component's pads (pcb_doc Primitives) and bare holes in its footprint's
    frame (footprint_frame, footprint_rotation, layers flipped back on the bottom)."""
    bottom = component.side == "bottom"
    found = []
    for pad in pads:
        x, y = footprint_frame(pad.x1, pad.y1, component)
        found.append(FootprintPad(
            pad.pad_name or "", x, y, pad.width, pad.height or pad.width,
            footprint_rotation(pad.rotation, component), pad.shape,
            flip_layer(pad.layer) if bottom else pad.layer, pad.hole or 0.0, pad.hole_shape,
            pad.slot_length, pad.plated, pad.corner_radius, net=pad.net))
    for hole in holes:
        x, y = footprint_frame(hole.x, hole.y, component)
        found.append(FootprintPad(
            hole.pad_name, x, y, 0.0, 0.0, footprint_rotation(hole.rotation, component), None,
            flip_layer(hole.layer) if bottom else hole.layer, hole.hole, hole.hole_shape,
            hole.slot_length, hole.plated, copper=False, net=hole.net))
    return found


def _form(width: float, height: float, rotation: float, shape: int | None,
          tolerance: float) -> tuple[float, float, float]:
    """(width, height, angle) of a pad's outline with the angle in [0, 90): every pad
    shape looks the same turned by 180 degrees, and turned by 90 with its sides
    swapped; a circle by any angle."""
    angle = rotation % 180.0
    while angle >= 90.0 - ANGLE_TOLERANCE_DEG:
        width, height, angle = height, width, angle - 90.0
    angle = max(angle, 0.0)
    if abs(width - height) <= tolerance and shape in (PAD_SHAPE_ROUND, None):
        angle = 0.0
    return width, height, angle


def _place(x: float, y: float, turn: int, mirror: bool) -> tuple[float, float]:
    """A point mirrored (x to -x) and then turned by turn x 90 degrees."""
    if mirror:
        x = -x
    for _ in range(turn % 4):
        x, y = -y, x
    return x, y


def _pair_pads(library: list[FootprintPad], board: list[FootprintPad]):
    """(pairs, library pads left, board pads left): pads paired by designator, the
    nearest first where a designator names several."""
    by_name: dict[str, list[FootprintPad]] = defaultdict(list)
    for pad in board:
        by_name[pad.designator].append(pad)
    wanted: dict[str, list[FootprintPad]] = defaultdict(list)
    for pad in library:
        wanted[pad.designator].append(pad)
    pairs, library_left = [], []
    used: set[int] = set()
    for name, group in wanted.items():
        candidates = by_name.get(name, [])
        options = sorted((math.hypot(a.x - b.x, a.y - b.y), i, j)
                         for i, a in enumerate(group) for j, b in enumerate(candidates))
        taken: set[int] = set()
        for _, i, j in options:
            if i in taken or id(candidates[j]) in used:
                continue
            taken.add(i)
            used.add(id(candidates[j]))
            pairs.append((group[i], candidates[j]))
        library_left += [pad for i, pad in enumerate(group) if i not in taken]
    board_left = [pad for pad in board if id(pad) not in used]
    return pairs, library_left, board_left


def _fit(pairs, tolerance: float) -> tuple[int, bool, float, float] | None:
    """(turn, mirror, dx, dy) placing every paired library pad on its board pad (the
    library point mirrored, turned by turn x 90 degrees, then moved by dx, dy); the
    untransformed fit first. None when no such placement fits them all."""
    if not pairs:
        return None
    for mirror in (False, True):
        for turn in range(4):
            moved = []
            for library, board in pairs:
                x, y = _place(library.x, library.y, turn, mirror)
                moved.append((board.x - x, board.y - y))
            dx = sum(item[0] for item in moved) / len(moved)
            dy = sum(item[1] for item in moved) / len(moved)
            if all(math.hypot(x - dx, y - dy) <= tolerance for x, y in moved):
                return turn, mirror, dx, dy
    return None


def _xy(x: float, y: float) -> list[float]:
    return [round(x, 4) + 0.0, round(y, 4) + 0.0]  # + 0.0: no -0.0


def _pad_text(pad: FootprintPad, tolerance: float) -> dict:
    """A pad's outline, hole and layer as a finding shows them."""
    width, height, angle = _form(pad.width, pad.height, pad.rotation, pad.shape, tolerance)
    shown: dict = {"layer": layer_label(pad.layer)}
    if pad.copper:
        shown.update({"shape": PAD_SHAPES.get(pad.shape, str(pad.shape)),
                      "size_mil": [round(width, 4), round(height, 4)], "angle": round(angle, 3)})
        if pad.shape == PAD_SHAPE_ROUNDRECT and pad.corner_radius is not None:
            shown["corner_radius_mil"] = round(pad.corner_radius, 4)
    if pad.hole:
        shown["hole_mil"] = round(pad.hole, 4)
        if pad.hole_shape in (1, 2):
            shown["hole_shape"] = HOLE_SHAPES[pad.hole_shape]
            shown["slot_length_mil"] = round(pad.slot_length or 0.0, 4)
        if pad.plated is not None:
            shown["plated"] = pad.plated
    return shown


def _pad_attributes(library: FootprintPad, board: FootprintPad, turn: int, mirror: bool,
                    tolerance: float) -> list[str]:
    """Which of size, angle, shape, corner radius, hole, hole shape, plating and layer
    differ between a library pad (placed by turn and mirror) and its board pad."""
    rotation = (180.0 - library.rotation if mirror else library.rotation) + 90.0 * turn
    differs = []
    if library.copper and board.copper:
        a = _form(library.width, library.height, rotation, library.shape, tolerance)
        b = _form(board.width, board.height, board.rotation, board.shape, tolerance)
        if abs(a[0] - b[0]) > tolerance or abs(a[1] - b[1]) > tolerance:
            turned = abs(a[0] - b[1]) <= tolerance and abs(a[1] - b[0]) <= tolerance
            differs.append("angle" if turned else "size")
        elif min(abs(a[2] - b[2]), 90.0 - abs(a[2] - b[2])) > ANGLE_TOLERANCE_DEG:
            differs.append("angle")
        if library.shape != board.shape:
            differs.append("shape")
        elif library.shape == PAD_SHAPE_ROUNDRECT and abs(
                (library.corner_radius or 0.0) - (board.corner_radius or 0.0)) > tolerance:
            differs.append("corner_radius")
    if abs(library.hole - board.hole) > tolerance:
        differs.append("hole")
    elif library.hole and ((library.hole_shape or 0) != (board.hole_shape or 0) or (
            library.hole_shape in (1, 2)
            and abs((library.slot_length or 0.0) - (board.slot_length or 0.0)) > tolerance)):
        differs.append("hole_shape")
    if library.hole and library.plated is not None and board.plated is not None \
            and library.plated != board.plated:
        differs.append("plating")
    if library.layer != board.layer:
        differs.append("layer")
    return differs


_ATTRIBUTE_MESSAGES = {
    "size": "pad size differs",
    "angle": "pad angle differs",
    "shape": "pad shape differs",
    "corner_radius": "corner radius differs",
    "hole": "hole size differs",
    "hole_shape": "hole shape or slot length differs",
    "plating": "hole plating differs",
    "layer": "pad layer differs",
}


def primitive_counts(tracks, arcs, fills, regions, bottom: bool = False) -> Counter:
    """Primitives other than pads per (type, layer) in the footprint's own layers (a
    bottom-side placement's flipped back); mechanical layers together (their pairs are
    not read). arcs are pcb_doc's chord rows, one arc per record."""
    counts: Counter = Counter()

    def layer(raw: int) -> str:
        own = flip_layer(raw) if bottom else raw
        return "Mechanical" if 57 <= own <= 72 else layer_label(own)

    for track in tracks:
        counts[("track", layer(track.layer))] += 1
    for key in {(arc.layer, arc.source_id, arc.arc_x, arc.arc_y, arc.arc_radius) for arc in arcs}:
        counts[("arc", layer(key[0]))] += 1
    for fill in fills:
        counts[("fill", layer(fill.layer))] += 1
    for region in regions:
        counts[("region", layer(region.layer))] += 1
    return counts


def footprint_counts(footprint: Footprint) -> Counter:
    return primitive_counts(footprint.tracks, footprint.arcs, footprint.fills,
                            footprint.regions)


def _body_text(bodies) -> dict:
    """3D bodies in brief: how many, their models (the first EXAMPLES, and how many
    there are when more) and the tallest OverallHeight."""
    heights = [body.overall_height_mil for body in bodies if body.overall_height_mil is not None]
    models = sorted({body.model_name for body in bodies if body.model_name})
    shown = {"bodies": len(bodies), "models": models[:EXAMPLES],
             "height_mil": round(max(heights), 4) if heights else None}
    if len(models) > EXAMPLES:
        shown["model_count"] = len(models)
    return shown


def compare_footprint(component, footprint: Footprint, pads, holes=(), bodies=(),
                      counts: Counter | None = None,
                      tolerance: float = POSITION_TOLERANCE_MIL) -> list[dict]:
    """The differences between a placed footprint and its library source, as Altium's
    Footprint Comparison Report lists them: pad count, pads missing or extra by
    designator, the whole footprint moved (origin) or turned, each pad moved, its size,
    angle, shape, hole and layer, the Height, the 3D bodies and the other primitives'
    counts per layer. component: a pcb_doc.PcbComponent; pads and holes: its Pads6
    primitives and bare holes; bodies: its ComponentBodies6 bodies; counts: its other
    primitives (primitive_counts), None to skip them. Positions are in the footprint's
    frame (mil). [] when they match."""
    library = library_pads(footprint)
    board = placed_pads(component, pads, holes)
    differences: list[dict] = []
    lib_copper = sum(pad.copper for pad in library)
    board_copper = sum(pad.copper for pad in board)
    if lib_copper != board_copper or len(library) - lib_copper != len(board) - board_copper:
        differences.append({
            "kind": "pad_count", "severity": "warning",
            "message": (f"the library footprint has {lib_copper} pads and "
                        f"{len(library) - lib_copper} bare holes, the board's "
                        f"{board_copper} and {len(board) - board_copper}"),
            "in_library": {"pads": lib_copper, "bare_holes": len(library) - lib_copper},
            "on_board": {"pads": board_copper, "bare_holes": len(board) - board_copper}})
    pairs, library_left, board_left = [], [], []
    for copper in (True, False):
        found = _pair_pads([pad for pad in library if pad.copper == copper],
                           [pad for pad in board if pad.copper == copper])
        pairs += found[0]
        library_left += found[1]
        board_left += found[2]
    for kind, left, where in (("pads_missing_on_board", library_left, "have no pad on the board"),
                              ("pads_extra_on_board", board_left, "are not in the library footprint")):
        if left:
            names = sorted({pad.designator or "(no designator)" for pad in left}, key=natural_key)
            differences.append({"kind": kind, "severity": "warning",
                                "message": f"{len(left)} pad(s) {where}",
                                "pads": names[:EXAMPLES], "count": len(left)})
    fit = _fit(pairs, tolerance)
    turn, mirror, dx, dy = fit if fit else (0, False, 0.0, 0.0)
    if fit and (turn or mirror):
        differences.append({
            "kind": "footprint_turned", "severity": "warning",
            "message": (f"the board's pads are the library's {'mirrored and ' if mirror else ''}"
                        f"turned by {90 * turn} degrees"),
            "turn_degrees": 90 * turn, "mirrored": mirror})
    if fit and math.hypot(dx, dy) > tolerance:
        differences.append({
            "kind": "origin_offset", "severity": "warning",
            "message": ("every pad sits at the same offset from where the library footprint "
                        "has it, relative to the component's reference point: the reference "
                        "point moved in the library, or the pads were moved on the board"),
            "offset_mil": _xy(dx, dy)})
    moved = []
    attributes: dict[str, list[dict]] = defaultdict(list)
    for lib_pad, board_pad in pairs:
        x, y = _place(lib_pad.x, lib_pad.y, turn, mirror)
        x, y = x + dx, y + dy
        if fit is None and math.hypot(board_pad.x - x, board_pad.y - y) > tolerance:
            moved.append({"pad": lib_pad.designator, "library_at_mil": _xy(x, y),
                          "board_at_mil": _xy(board_pad.x, board_pad.y),
                          "delta_mil": _xy(board_pad.x - x, board_pad.y - y)})
        for attribute in _pad_attributes(lib_pad, board_pad, turn, mirror, tolerance):
            attributes[attribute].append({"pad": lib_pad.designator,
                                          "library": _pad_text(lib_pad, tolerance),
                                          "board": _pad_text(board_pad, tolerance)})
    if moved:
        moved.sort(key=lambda item: -math.hypot(*item["delta_mil"]))
        differences.append({
            "kind": "pads_moved", "severity": "warning",
            "message": (f"{len(moved)} pad(s) are not where the library footprint has them "
                        "(max_delta: the farthest)"),
            "count": len(moved), "max_delta_mil": round(math.hypot(*moved[0]["delta_mil"]), 4),
            "pads": moved[:EXAMPLES]})
    for attribute, rows in attributes.items():
        rows.sort(key=lambda row: natural_key(row["pad"]))
        differences.append({
            "kind": f"pad_{attribute}", "severity": "warning",
            "message": f"{_ATTRIBUTE_MESSAGES[attribute]} on {len(rows)} pad(s)",
            "count": len(rows), "pads": rows[:EXAMPLES]})
    if component.height_mil is not None and footprint.height_mil is not None and abs(
            component.height_mil - footprint.height_mil) > tolerance:
        differences.append({
            "kind": "height", "severity": "info",
            "message": "the footprint's Height differs from the library's",
            "in_library_mil": round(footprint.height_mil, 4),
            "on_board_mil": round(component.height_mil, 4)})
    library_bodies, board_bodies = _body_text(footprint.bodies), _body_text(bodies)
    lib_height, board_height = library_bodies["height_mil"], board_bodies["height_mil"]
    if (library_bodies["bodies"] != board_bodies["bodies"]
            or {body.model_name for body in footprint.bodies} != {body.model_name for body in bodies}
            or (lib_height is None) != (board_height is None)
            or (lib_height is not None and abs(lib_height - board_height)
                > BODY_HEIGHT_TOLERANCE_MIL)):
        differences.append({
            "kind": "bodies", "severity": "info",
            "message": "the 3D bodies differ from the library's (count, model or height)",
            "in_library": library_bodies, "on_board": board_bodies})
    if counts is not None:
        library_counts = footprint_counts(footprint)
        changed = sorted(key for key in set(library_counts) | set(counts)
                         if library_counts[key] != counts[key])
        if changed:
            differences.append({
                "kind": "primitives", "severity": "info",
                "message": "the footprint's other primitives (tracks, arcs, fills, regions) "
                           "differ in number from the library's",
                "counts": [{"type": kind, "layer": layer,
                            "in_library": library_counts[(kind, layer)],
                            "on_board": counts[(kind, layer)]}
                           for kind, layer in changed][:EXAMPLES]})
    return differences


# ---------- copper inside a footprint: shorted and unconnected copper ----------

# PcbIndex builds a primitive's copper outline without a board.
_SHAPES = object.__new__(PcbIndex)


@dataclass
class FootprintCopper:
    """One copper primitive of a footprint, as PcbIndex outlines it (geometry, box)."""

    kind: str  # pad, track, arc, fill, region
    layer: int
    pad: str | None  # a pad's designator ('' without one); None for other copper
    geometry: tuple
    box: tuple[float, float, float, float]

    def shares_layer(self, other: "FootprintCopper") -> bool:
        return MULTI_LAYER in (self.layer, other.layer) or self.layer == other.layer


def _signal_copper(layer: int) -> bool:
    """Signal copper: the plane layers' primitives are voids, not copper."""
    return 1 <= layer <= 32 or layer == MULTI_LAYER


def footprint_copper(footprint: Footprint) -> list[FootprintCopper]:
    """A library footprint's copper (copper_items)."""
    return copper_items(footprint.pads, (*footprint.tracks, *footprint.arcs), footprint.fills,
                        footprint.regions)


def copper_items(pads, lines, fills, regions) -> list[FootprintCopper]:
    """The copper of a footprint's primitives (a library footprint's, or a placed one's
    from the board): pads, tracks, arc chords, fills and copper regions on the signal
    layers (keepouts and cutouts left out)."""
    items: list[FootprintCopper] = []

    def add(kind: str, layer: int, pad: str | None, geometry: tuple) -> None:
        if _signal_copper(layer):
            items.append(FootprintCopper(kind, layer, pad, geometry,
                                         PcbIndex._geometry_bounds(geometry)))

    for pad in pads:
        add("pad", pad.layer, pad.pad_name or "", _SHAPES._copper_geometry(pad, None))
    for line in lines:
        add(line.kind, line.layer, None, _SHAPES._copper_geometry(line, None))
    for fill in fills:
        if fill.keepout or fill.x1 == fill.x2 or fill.y1 == fill.y2:
            continue
        outline = oriented_rect_vertices((fill.x1 + fill.x2) / 2.0, (fill.y1 + fill.y2) / 2.0,
                                         abs(fill.x2 - fill.x1), abs(fill.y2 - fill.y1),
                                         fill.rotation)
        add("fill", fill.layer, None, (None, 0.0, outline, None))
    for region in regions:
        if region.keepout or region.kind != 0 or len(region.vertices) < 3:
            continue
        add("region", region.layer, None,
            (None, 0.0, [(vertex.x, vertex.y) for vertex in region.vertices], None))
    return items


def copper_groups(items: list[FootprintCopper]) -> list[list[FootprintCopper]]:
    """The copper split into connected groups: primitives on a common layer (a
    Multi-Layer pad is on every one) whose copper touches (within TOUCH_MIL)."""
    parent = list(range(len(items)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    active: list[int] = []
    for index in sorted(range(len(items)), key=lambda i: items[i].box[0]):
        item = items[index]
        active = [other for other in active if items[other].box[2] >= item.box[0] - TOUCH_MIL]
        for other_index in active:
            other = items[other_index]
            if (other.box[1] > item.box[3] + TOUCH_MIL or other.box[3] < item.box[1] - TOUCH_MIL
                    or not item.shares_layer(other)):
                continue
            first, second = find(index), find(other_index)
            if first != second and PcbIndex._geometry_distance(
                    item.geometry, other.geometry) <= TOUCH_MIL:
                parent[first] = second
        active.append(index)
    groups: dict[int, list[FootprintCopper]] = defaultdict(list)
    for index, item in enumerate(items):
        groups[find(index)].append(item)
    return list(groups.values())


@dataclass
class CopperReport:
    """shorts: the pad designators each copper group joins when it joins two or more;
    unconnected: copper groups that touch no pad, as {primitives, kinds, layers}."""

    shorts: list[list[str]] = field(default_factory=list)
    unconnected: list[dict] = field(default_factory=list)


def copper_report(footprint: Footprint | None = None,
                  items: list[FootprintCopper] | None = None) -> CopperReport:
    """The shorted and unconnected copper of a library footprint, or of copper items
    (copper_items: a placed footprint's)."""
    report = CopperReport()
    for group in copper_groups(footprint_copper(footprint) if items is None else items):
        pads = [item for item in group if item.kind == "pad"]
        names = sorted({item.pad for item in pads if item.pad}, key=natural_key)
        if len(names) >= 2:
            report.shorts.append(names)
        if not pads:
            report.unconnected.append({
                "primitives": len(group),
                "kinds": dict(sorted(Counter(item.kind for item in group).items())),
                "layers": sorted({layer_label(item.layer) for item in group})})
    report.shorts.sort(key=lambda names: natural_key(names[0]))
    return report


# ---------- Altium's component rule checks ----------

def _ranges(numbers: list[int]) -> str:
    """'3-5, 9' for [3, 4, 5, 9]."""
    parts, start = [], None
    for index, number in enumerate(numbers):
        if start is None:
            start = number
        if index + 1 == len(numbers) or numbers[index + 1] != number + 1:
            parts.append(str(start) if start == number else f"{start}-{number}")
            start = None
    return ", ".join(parts)


_SEQUENCE = re.compile(r"([A-Za-z]*)(\d+)")
# Default designator prefixes of integrated circuits, whose pins should carry types.
_IC_PREFIXES = ("U", "IC", "AR", "N")


def sequence_gaps(designators) -> dict[str, list[int]]:
    """Missing numbers per designator prefix ('' for plain numbers, 'A' for A1, A2 ...):
    plain numbers from 1 to the highest, prefixed ones between their lowest and highest
    (a BGA row may start anywhere)."""
    numbers: dict[str, set[int]] = defaultdict(set)
    for designator in designators:
        match = _SEQUENCE.fullmatch(designator.strip())
        if match:
            numbers[match.group(1).upper()].add(int(match.group(2)))
    gaps = {}
    for prefix, held in numbers.items():
        if len(held) < 2 and prefix:
            continue
        low = min(1, min(held)) if not prefix else min(held)
        missing = sorted(set(range(low, max(held) + 1)) - held)
        if missing:
            gaps[prefix] = missing
    return gaps


def _rule(kind: str, severity: str, message: str, **details) -> dict:
    return {"kind": kind, "severity": severity, "message": message, **details}


def symbol_rule_findings(symbol: SchLibComponent) -> list[dict]:
    """Altium's SchLib Component Rule Check on one symbol: duplicate pins (in one part a
    warning; the same designator in different parts of a multi-part symbol info), pins
    without a designator (error; info on a Graphical symbol) or a name (info), gaps in
    the pin numbering, no description, no default designator, no footprint link (a
    warning; info for a symbol with simulation models only or no pins; Graphical parts
    need none), and an IC (default designator U, IC, AR or N) whose pins are all Passive
    (no type: ERC has nothing to check). Pins of alternate display modes are left out."""
    findings: list[dict] = []
    pins = [pin for pin in symbol.pins if pin.display_mode == 0]
    nameless = [pin for pin in pins if not pin.designator.strip()]
    if nameless:
        graphical = symbol.kind == 2  # never placed on the PCB: no pad to wire to
        findings.append(_rule(
            "missing_pin_designators", "info" if graphical else "error",
            f"{len(nameless)} pin(s) have no designator"
            + (" (a Graphical symbol: it has no footprint, so this only matters to "
               "the schematic's connectivity)" if graphical
               else ": they cannot be wired to a pad"),
            count=len(nameless), pin_names=sorted({pin.name for pin in nameless})[:EXAMPLES]))
    by_designator: dict[str, list] = defaultdict(list)
    for pin in pins:
        if pin.designator.strip():
            by_designator[pin.designator.strip()].append(pin)
    same_part, same_name, across_parts = [], [], []
    for designator, group in by_designator.items():
        if len(group) < 2:
            continue
        parts = Counter(pin.part for pin in group)
        if any(count > 1 for count in parts.values()) or (0 in parts and len(parts) > 1):
            (same_name if len({pin.name for pin in group}) == 1 else same_part).append(designator)
        else:
            across_parts.append(designator)
    if same_part:
        findings.append(_rule(
            "duplicate_pin_designators", "warning",
            f"{len(same_part)} pin designator(s) are used by pins of different names in "
            "one part: they are one pin on the footprint, so their nets merge",
            count=len(same_part), pins=sorted(same_part, key=natural_key)[:EXAMPLES]))
    if same_name:
        findings.append(_rule(
            "duplicate_pin_designators_same_name", "info",
            f"{len(same_name)} pin(s) are drawn more than once in a part under the same "
            "designator and name (one pin on the footprint)",
            count=len(same_name), pins=sorted(same_name, key=natural_key)[:EXAMPLES]))
    if across_parts:
        findings.append(_rule(
            "duplicate_pin_designators_across_parts", "info",
            f"{len(across_parts)} pin designator(s) appear in more than one part of the "
            "symbol (one pin on the footprint for all of them)",
            count=len(across_parts), pins=sorted(across_parts, key=natural_key)[:EXAMPLES]))
    unnamed = [pin for pin in pins if pin.designator.strip() and not pin.name.strip()]
    if unnamed:
        findings.append(_rule(
            "missing_pin_names", "info", f"{len(unnamed)} pin(s) have no name",
            count=len(unnamed),
            pins=sorted({pin.designator for pin in unnamed}, key=natural_key)[:EXAMPLES]))
    gaps = sequence_gaps(by_designator)
    if gaps:
        missing = sum(len(numbers) for numbers in gaps.values())
        findings.append(_rule(
            "pin_sequence_gaps", "info",
            f"{missing} pin number(s) missing from the sequence",
            count=missing, missing={prefix or "(numbers)": _ranges(numbers)
                                    for prefix, numbers in sorted(gaps.items())}))
    if not symbol.description.strip():
        findings.append(_rule("missing_description", "info", "no description"))
    designator = symbol.designator.strip()
    if not designator or designator.startswith("?"):
        findings.append(_rule(
            "missing_default_designator", "warning",
            "no default designator prefix (U?, R?): annotation cannot name the part",
            default_designator=designator))
    if symbol.kind != 2 and not symbol.footprints:
        models = sorted({model.model_type for model in symbol.models})
        if pins and models == ["SIM"]:
            findings.append(_rule(
                "missing_footprint", "info",
                "no footprint model, only simulation models: a simulation-only part (a "
                "source, a model or a directive) that gets nothing on the PCB",
                models=models))
        elif pins:
            findings.append(_rule("missing_footprint", "warning",
                                  "no footprint model: the part cannot be placed on the PCB",
                                  models=models))
        else:
            findings.append(_rule(
                "missing_footprint", "info",
                f"no pins and no footprint, and its type is {KIND_NAMES.get(symbol.kind)}: a "
                "drawing-only symbol is Graphical (left out of the BOM and the PCB)",
                models=models))
    prefix = re.match(r"[A-Za-z]*", designator).group(0).upper()
    if (prefix in _IC_PREFIXES and len(pins) >= 3
            and all(pin.electrical == "Passive" for pin in pins)):
        findings.append(_rule(
            "untyped_pins", "info",
            f"every one of the {len(pins)} pins is Passive (no electrical type): ERC "
            "cannot check what drives what", count=len(pins)))
    return findings


def _pad_box(pads) -> tuple[float, float, float, float] | None:
    if not pads:
        return None
    return (min(pad.x1 - pad.width / 2.0 for pad in pads),
            min(pad.y1 - (pad.height or pad.width) / 2.0 for pad in pads),
            max(pad.x1 + pad.width / 2.0 for pad in pads),
            max(pad.y1 + (pad.height or pad.width) / 2.0 for pad in pads))


def footprint_rule_findings(footprint: Footprint, net_tie: bool = False,
                            kinds_declared: bool = False,
                            copper: CopperReport | None = None) -> list[dict]:
    """Altium's PcbLib Component Rule Check on one footprint: pads without a designator,
    a designator on several pads (info: they share a net), duplicate primitives,
    mirrored (its pads drawn on the bottom side), the reference point outside its pads,
    shorted copper (pads of different designators joined by copper; expected in a net
    tie: net_tie) and unconnected copper (touching no pad), no description, and no
    courtyard when its library declares courtyard layers (kinds_declared)."""
    findings: list[dict] = []
    pads = footprint.pads
    unnamed = [pad for pad in pads if not (pad.pad_name or "").strip()]
    if unnamed:
        findings.append(_rule(
            "missing_pad_designators", "warning",
            f"{len(unnamed)} pad(s) have no designator: no pin can connect them",
            count=len(unnamed),
            at_mil=[_xy(pad.x1, pad.y1) for pad in unnamed[:EXAMPLES]]))
    names = Counter(pad.pad_name for pad in pads if (pad.pad_name or "").strip())
    repeated = sorted((name for name, count in names.items() if count > 1), key=natural_key)
    if repeated:
        findings.append(_rule(
            "duplicate_pad_designators", "info",
            f"{len(repeated)} pad designator(s) are on more than one pad (one pin, one net "
            "for all of them)", count=len(repeated),
            pads=[{"pad": name, "pads": names[name]} for name in repeated[:EXAMPLES]]))
    keys: Counter = Counter()
    for pad in pads:
        keys[("pad", pad.layer, round(pad.x1, 3), round(pad.y1, 3), round(pad.width, 3),
              round(pad.height or 0.0, 3), pad.shape, round(pad.rotation % 180.0, 2),
              round(pad.hole or 0.0, 3), pad.pad_name)] += 1
    for track in footprint.tracks:
        ends = sorted(((round(track.x1, 3), round(track.y1, 3)), (round(track.x2, 3), round(track.y2, 3))))
        keys[("track", track.layer, track.mechanical, *ends, round(track.width, 3))] += 1
    duplicated = sum(count - 1 for count in keys.values() if count > 1)
    if duplicated:
        findings.append(_rule(
            "duplicate_primitives", "warning",
            f"{duplicated} primitive(s) lie exactly on top of an identical one",
            count=duplicated,
            types=dict(sorted(Counter(key[0] for key, count in keys.items() if count > 1).items()))))
    surface = [pad for pad in pads if pad.layer != MULTI_LAYER]
    if surface and all(pad.layer == 32 for pad in surface):
        findings.append(_rule(
            "mirrored", "warning",
            "its surface pads are on the Bottom Layer: the footprint is drawn mirrored (a "
            "library footprint is drawn on the top side and flipped when placed)"))
    box = _pad_box(pads)
    if box and not (box[0] - TOUCH_MIL <= 0.0 <= box[2] + TOUCH_MIL
                    and box[1] - TOUCH_MIL <= 0.0 <= box[3] + TOUCH_MIL):
        findings.append(_rule(
            "reference_outside_pads", "info",
            "the reference point (origin) lies outside the pads: pick-and-place and "
            "rotation turn the part about a point off its body", pads_box_mil=[
                round(value, 4) for value in box]))
    copper = copper or copper_report(footprint)
    if copper.shorts and not net_tie:
        findings.append(_rule(
            "shorted_copper", "warning",
            "copper joins pads of different designators (they touch or overlap, or a "
            "track, arc, fill or region joins them): unless the part is a Net Tie, DRC "
            "reports a short wherever those pads are on different nets",
            joined=copper.shorts[:EXAMPLES]))
    if copper.unconnected:
        findings.append(_rule(
            "unconnected_copper", "warning",
            f"{len(copper.unconnected)} piece(s) of copper touch no pad: they get no net",
            count=len(copper.unconnected), pieces=copper.unconnected[:EXAMPLES]))
    if not footprint.description.strip():
        findings.append(_rule("missing_description", "info", "no description"))
    if kinds_declared and not footprint.outline("courtyard"):
        findings.append(_rule(
            "no_courtyard", "info",
            "nothing on its library's courtyard layers: placement checks have no courtyard"))
    return findings


def pin_pad_findings(symbol: SchLibComponent, footprint: Footprint,
                     unpadded: str = "warning") -> list[dict]:
    """A symbol against its footprint by designator: pins without a pad (severity
    unpadded: a connected one fails Update PCB as an Unknown Pin; pads_cover_pins checks
    the connected ones of placed parts) and copper pads without a pin (info; a warning
    for a pad much larger than the others, an exposed or thermal pad left without a
    net)."""
    pins = {pin.designator.strip() for pin in symbol.pins
            if pin.display_mode == 0 and pin.designator.strip()}
    pads = {name.strip() for name in copper_pad_names(footprint.pads)} - {""}
    holes = {name.strip() for name in bare_hole_names(footprint.holes)} - {""}
    findings = []
    without_pad = sorted(pins - pads, key=natural_key)
    if without_pad:
        on_holes = [pin for pin in without_pad if pin in holes]
        finding = _rule(
            "pins_without_pads", unpadded,
            f"{len(without_pad)} of the symbol's {len(pins)} pin(s) have no copper pad in "
            "the footprint" + (f" ({len(on_holes)} of them only a bare hole, a pad "
                               "without copper)" if on_holes else ""),
            count=len(without_pad), pins=without_pad[:EXAMPLES])
        if on_holes:
            finding["on_bare_holes"] = on_holes[:EXAMPLES]
        findings.append(finding)
    copper = [pad for pad in footprint.pads if (pad.pad_name or "").strip()]
    without_pin = [pad for pad in copper if pad.pad_name.strip() not in pins]
    if without_pin:
        areas = sorted(pad.width * (pad.height or pad.width) for pad in copper)
        median = areas[len(areas) // 2]
        large = sorted({pad.pad_name for pad in without_pin
                        if len(copper) >= 4 and pad.width * (pad.height or pad.width) >= 4 * median},
                       key=natural_key)
        names = sorted({pad.pad_name for pad in without_pin}, key=natural_key)
        findings.append(_rule(
            "pads_without_pins", "warning" if large else "info",
            f"{len(names)} pad designator(s) have no pin in the symbol ({len(pads)} pad "
            f"designators, {len(pins)} pins): those pads get no net"
            + (" (the large ones look like an exposed or thermal pad)" if large else ""),
            count=len(names), pads=names[:EXAMPLES], large_pads=large[:EXAMPLES]))
    return findings


# ---------- reading the items once per library file ----------

class ItemLoader:
    """Library items read once per library file: every symbol and footprint asked for
    of one file is read in one pass (an IntLib is inflated once)."""

    def __init__(self):
        self._symbols: dict[tuple[str, str], SchLibComponent | None] = {}
        self._footprints: dict[tuple[str, str], Footprint | None] = {}
        self.kinds_declared: dict[str, bool] = {}  # library path -> mechanical layer kinds
        self.errors: dict[str, str] = {}

    @staticmethod
    def _key(path: str, name: str) -> tuple[str, str]:
        return os.path.normcase(path), name.casefold()

    def load(self, symbols=(), footprints=()) -> None:
        """Read the symbols and footprints the hits (LibraryHit) name, not read yet."""
        wanted: dict[str, tuple[str, set[str], set[str]]] = {}
        for hits, slot, held in ((symbols, 1, self._symbols), (footprints, 2, self._footprints)):
            for hit in hits:
                if hit is None or self._key(hit.path, hit.name) in held:
                    continue
                entry = wanted.setdefault(hit.path, (hit.kind, set(), set()))
                entry[slot].add(hit.name.casefold())
        for path, (kind, symbol_names, footprint_names) in wanted.items():
            components, found, declared = [], [], False
            try:
                if kind == "IntLib":
                    library = parse_int_lib(path, components=symbol_names,
                                            footprints=footprint_names)
                    components = [c for sch in library.sch_libs for c in sch.components]
                    found = [fp for pcb in library.pcb_libs for fp in pcb.footprints]
                    declared = any(pcb.mech_kinds for pcb in library.pcb_libs)
                elif kind == "SchLib":
                    components = parse_sch_lib(path, names=symbol_names).components
                elif kind == "PcbLib":
                    pcb_lib = parse_pcb_lib(path, names=footprint_names)
                    found, declared = pcb_lib.footprints, bool(pcb_lib.mech_kinds)
            except Exception as error:  # an unreadable library is reported, not raised
                self.errors[path] = f"{type(error).__name__}: {error}"
            for name in symbol_names:
                self._symbols[self._key(path, name)] = next(
                    (c for c in components if c.lib_reference.casefold() == name
                     or any(alias.casefold() == name for alias in c.aliases)), None)
            for name in footprint_names:
                self._footprints[self._key(path, name)] = next(
                    (fp for fp in found if fp.name.casefold() == name), None)
            if footprint_names:
                self.kinds_declared[os.path.normcase(path)] = declared

    def symbol(self, hit: LibraryHit | None) -> SchLibComponent | None:
        if hit is None:
            return None
        self.load(symbols=[hit])
        return self._symbols.get(self._key(hit.path, hit.name))

    def footprint(self, hit: LibraryHit | None) -> Footprint | None:
        if hit is None:
            return None
        self.load(footprints=[hit])
        return self._footprints.get(self._key(hit.path, hit.name))

    def declared(self, hit: LibraryHit) -> bool:
        return self.kinds_declared.get(os.path.normcase(hit.path), False)


def footprint_body_heights(components, libraries: "ProjectLibraries") -> dict[int, dict]:
    """The library footprint's tallest 3D body for PcbDoc components (pcb_doc.PcbComponent)
    whose footprint the project's libraries hold: {component index: {height_mil,
    standoff_mil, library (file name), footprint, bodies}}. Components whose footprint is
    not found or has no body with a height are left out (the libraries are read once
    each, as check_libraries reads them)."""
    components = list(components)
    if not components:
        return {}
    resolver = LibraryResolver(libraries)
    loader = ItemLoader()
    results = resolver.resolve_pcb(components)
    loader.load(footprints=[result.footprint_hit for result in results
                            if result.footprint_hit is not None])
    found: dict[int, dict] = {}
    for component, result in zip(components, results):
        footprint = loader.footprint(result.footprint_hit)
        if footprint is None:
            continue
        bodies = [body for body in footprint.bodies if body.overall_height_mil]
        if not bodies:
            continue
        tallest = max(bodies, key=lambda body: body.overall_height_mil)
        found[component.index] = {
            "height_mil": tallest.overall_height_mil,
            "standoff_mil": tallest.standoff_height_mil,
            "library": _library_name(result.footprint_hit.path),
            "footprint": footprint.name,
            "bodies": len(bodies),
        }
    return found


def _library_name(path: str) -> str:
    return os.path.basename(path)


def _hit_text(hit: LibraryHit | None, reason: str = "") -> dict:
    """Where a symbol or footprint was found, or why not."""
    if hit is None:
        return {"found": False, "reason": reason}
    shown = {"found": True, "name": hit.name, "library": _library_name(hit.path),
             "library_path": hit.path, "origin": hit.origin}
    if hit.case_differs:
        shown["case_differs"] = True
    if not hit.named:
        shown["in_named_library"] = False
    return shown


def used_pins(component: dict) -> set[str]:
    """The pin designators of a netlist component that are on a net."""
    return {str(pin) for pin, meta in (component.get("pins") or {}).items()
            if (meta or {}).get("net")}


def body_category(bodies, height_mil: float | None) -> str:
    """3d_model (a body with a 3D model), extruded_body (bodies, none with a model),
    height_only (no body: the footprint's Height, the fallback 3D height, is set) or
    no_height (no body and Height 0 or unset)."""
    if any(body.model_name for body in bodies):
        return "3d_model"
    if bodies:
        return "extruded_body"
    return "height_only" if height_mil else "no_height"


# Finding kinds that many items share: grouped into one finding per library.
_BULK = {
    "missing_description": "items without a description",
    "missing_pin_names": "symbols with unnamed pins",
    "pin_sequence_gaps": "symbols with gaps in their pin numbering",
    "untyped_pins": "IC symbols whose pins are all Passive (no electrical type for ERC)",
    "duplicate_pin_designators_across_parts": "multi-part symbols repeating a pin designator "
                                              "across parts",
    "duplicate_pin_designators_same_name": "symbols drawing a pin twice (same designator "
                                           "and name)",
    "missing_footprint": "symbols with no footprint model that need none: drawings that "
                         "are not Graphical (no pins) or simulation-only parts",
    "no_courtyard": "footprints with nothing on their library's courtyard layers",
    "reference_outside_pads": "footprints whose reference point lies outside their pads",
    "duplicate_pad_designators": "footprints with a designator on several pads",
    "pads_without_pins": "symbol and footprint pairs with pads that have no pin",
}
# Warning kinds a whole library can repeat for most of its items (a library of
# simulation sources or touch-sensor symbols): grouped the same way.
_BULK_WARNINGS = {
    "missing_footprint": "symbols with pins but no footprint model: they cannot be placed "
                         "on the PCB",
}


def _group_bulk(findings: list[dict]) -> list[dict]:
    """Findings of a _BULK kind at info severity (or a _BULK_WARNINGS kind at warning)
    merged per (check, kind, severity, library): one finding listing the items and the
    first designators of the parts using them."""
    kept, groups = [], {}
    for finding in findings:
        severity = finding["severity"]
        bulk = (_BULK if severity == "info" else
                _BULK_WARNINGS if severity == "warning" else {})
        if finding["kind"] not in bulk:
            kept.append(finding)
            continue
        key = (finding["check"], finding["kind"], severity, finding.get("library"))
        group = groups.get(key)
        if group is None:
            group = groups[key] = {
                "check": finding["check"], "kind": finding["kind"], "severity": severity,
                "message": bulk[finding["kind"]], "library": finding.get("library"),
                "items": [], "count": 0, "parts": 0, "designators": []}
            kept.append(group)
        group["count"] += 1
        group["parts"] += finding.get("parts", 0)
        group["designators"] = _designators(
            group["designators"] + finding.get("designators", []))[:EXAMPLES]
        if len(group["items"]) < EXAMPLES:
            item = finding.get("symbol") or finding.get("footprint")
            if finding.get("symbol") and finding.get("footprint"):
                item = f"{finding['symbol']} / {finding['footprint']}"
            group["items"].append(item)
    for group in groups.values():
        if not group["designators"]:
            del group["designators"]  # a whole library's items: no parts
    return kept


def _sort_findings(findings: list[dict]) -> list[dict]:
    order = {check: index for index, check in enumerate(ALL_CHECKS)}

    def key(finding: dict):
        first = (finding.get("designators") or finding.get("items") or [""])[0] or ""
        return (SEVERITY_ORDER[finding["severity"]], order.get(finding["check"], 99),
                finding["kind"], natural_key(str(first)))

    return sorted(findings, key=key)


# ---------- the parts a check looks at ----------

@dataclass
class LibraryCheckInputs:
    """What check_libraries reads: the project's libraries, its netlist and PcbDoc
    (either may be None), sch_refdes (PcbDoc component index -> netlist designator; None:
    by designator), and the arguments. projects: the .PrjPcb files where_used scans."""

    libraries: ProjectLibraries
    netlist: dict | None = None
    pcb: object | None = None
    sch_refdes: dict[int, str] | None = None
    checks: list[str] | None = None
    parts: list[str] | None = None
    library: str | None = None
    footprint: str | None = None
    where_used: str | None = None
    projects: list[str] = field(default_factory=list)
    tolerance_mil: float = POSITION_TOLERANCE_MIL


class _Parts:
    """The project's parts resolved, filtered by part=, with their board data."""

    def __init__(self, inputs: LibraryCheckInputs, resolver: LibraryResolver):
        self.inputs = inputs
        self.netlist = inputs.netlist
        self.pcb = inputs.pcb
        self.components = (self.netlist or {}).get("components") or {}
        self.schematic = resolver.resolve_netlist(self.netlist) if self.netlist else []
        board_components = list(self.pcb.components) if self.pcb is not None else []
        self.board = list(zip(board_components, resolver.resolve_pcb(board_components)))
        sch_refdes = inputs.sch_refdes
        if sch_refdes is None:
            names = {name.casefold(): name for name in self.components}
            sch_refdes = {component.index: names[designator.casefold()]
                          for component in board_components
                          if (designator := component.designator or component.refdes or "")
                          and designator.casefold() in names}
        self.sch_refdes = sch_refdes
        self.board_of: dict[str, tuple] = {}
        for component, result in self.board:
            name = sch_refdes.get(component.index)
            if name:
                self.board_of.setdefault(name, (component, result))
        self.matched: set[str] = set()
        if inputs.parts:
            wanted = {name.strip().casefold() for name in inputs.parts if name.strip()}

            def hit(*names) -> bool:
                found = {name.casefold() for name in names if name} & wanted
                self.matched |= found
                return bool(found)

            self.schematic = [r for r in self.schematic if hit(
                r.designator, r.lib_reference, r.design_item_id, r.footprint)]
            self.board = [(c, r) for c, r in self.board if hit(
                r.designator, c.refdes, sch_refdes.get(c.index), r.lib_reference, r.footprint)]
        self._by_component: dict[str, dict[int, list]] = {}

    def by_component(self, what: str) -> dict[int, list]:
        """The board's pads, holes, bodies, tracks, arcs, fills or regions per component
        index, read once."""
        if what in self._by_component:
            return self._by_component[what]
        grouped: dict[int, list] = defaultdict(list)
        pcb = self.pcb
        if what in ("pad", "track", "arc"):
            items = [p for p in pcb.primitives if p.kind == what and p.pour_id is None]
        elif what == "hole":
            items = pcb.bare_holes
        elif what == "body":
            items = pcb.bodies
        elif what == "fill":
            items = (decode_fill_records(pcb.raw["Fills6/Data"])[0]
                     if "Fills6/Data" in pcb.raw else [])
        else:
            items = [region for region in (
                decode_region_records(pcb.raw["Regions6/Data"], shape_based=False)[0]
                if "Regions6/Data" in pcb.raw else []) if region.polygon is None]
        for item in items:
            if item.component is not None:
                grouped[item.component].append(item)
        self._by_component[what] = grouped
        return grouped

    def board_name(self, component) -> str:
        return (component.designator or component.refdes
                or self.sch_refdes.get(component.index) or f"#{component.index}")

    def net_name(self, net: int | None) -> str | None:
        nets = self.pcb.nets if self.pcb is not None else []
        return nets[net] if net is not None and 0 <= net < len(nets) else None

    def symbol_results(self):
        """(designator, hit, kind of the placed part) per part: the schematic's parts,
        else the board's."""
        if self.netlist:
            return [(r.designator, r.symbol, (self.components.get(r.designator) or {}).get("kind", 0))
                    for r in self.schematic]
        return [(r.designator, r.symbol, c.kind) for c, r in self.board]

    def footprint_results(self):
        """(designator, footprint hit, its part's kinds) per part: the board's parts and
        the schematic's not on the board."""
        rows = []
        seen = set()
        for component, result in self.board:
            name = self.board_name(component)
            sch = self.sch_refdes.get(component.index)
            kinds = {component.kind}
            if sch:
                seen.add(sch)
                kinds.add((self.components.get(sch) or {}).get("kind", 0))
            rows.append((name, result.footprint_hit, kinds))
        for result in self.schematic:
            if result.designator not in seen:
                rows.append((result.designator, result.footprint_hit,
                             {(self.components.get(result.designator) or {}).get("kind", 0)}))
        return rows


def _designators(names) -> list[str]:
    return sorted(set(names), key=natural_key)


def _part_finding(check: str, finding: dict, designators, **where) -> dict:
    names = _designators(designators)
    return {"check": check, **where, **finding, "designators": names[:EXAMPLES],
            "parts": len(names)}


# ---------- resolution ----------

def _check_resolution(parts: _Parts) -> tuple[dict, list[dict]]:
    findings: list[dict] = []
    summary = {}
    if parts.netlist:
        summary["schematic"] = resolution_summary(parts.schematic)
    if parts.pcb is not None:
        summary["pcb"] = resolution_summary([result for _, result in parts.board])
    symbol_rows = parts.schematic if parts.netlist else [r for _, r in parts.board]
    footprint_rows = [r for _, r in parts.board] if parts.pcb is not None else parts.schematic
    groups: dict[tuple, list] = defaultdict(list)
    # Workspace and database parts are one finding per side, whatever library each
    # names (a Workspace part's footprint names its own cached PcbLib): library -> parts.
    offline_libraries: dict[tuple, Counter] = defaultdict(Counter)

    def missing(what: str, reason: str, library: str, result: PartResolution) -> None:
        if reason in (REASON_WORKSPACE, REASON_DATABASE):
            offline_libraries[(what, reason, "")][_library_name(library) or "(none)"] += 1
            library = ""
        groups[(what, reason, library)].append(result)

    for result in symbol_rows:
        if result.symbol is None:
            missing("symbol", result.symbol_reason, result.library, result)
        elif result.symbol.case_differs:
            groups[("symbol", "case", "")].append(result)
    for result in footprint_rows:
        if result.footprint_hit is None:
            missing("footprint", result.footprint_reason,
                    result.footprint_library or result.library, result)
        else:
            hit = result.footprint_hit
            if not hit.named and library_kind(result.footprint_library) in FOOTPRINT_KINDS:
                groups[("footprint", "elsewhere", result.footprint_library,
                        _library_name(hit.path))].append(result)
            elif hit.case_differs:
                groups[("footprint", "case", "")].append(result)
    for key, results in groups.items():
        what, reason, library = key[0], key[1], key[2]
        names = Counter((r.design_item_id or r.lib_reference) if what == "symbol" else r.footprint
                        for r in results)
        where = {"library": _library_name(library)} if library else {}
        if reason == REASON_FILE_MISSING:
            hint = Counter(_library_name(r.symbol_elsewhere.path) for r in results
                           if what == "symbol" and r.symbol_elsewhere)
            finding = {"kind": f"{what}_library_missing", "severity": "warning",
                       "message": (f"placed from {_library_name(library)}, which the project no "
                                   "longer has (not in its libraries, search paths or the "
                                   "installed ones): the part cannot be updated from its "
                                   "library")}
            if hint:
                finding["same_name_in"] = dict(hint.most_common(3))
                finding["message"] += ("; a symbol of the same name is in another library: "
                                       "point the parts at it (Update from Libraries / "
                                       "Edit Library Reference)")
        elif reason == REASON_NOT_FOUND:
            finding = {"kind": f"{what}_not_found", "severity": "warning",
                       "message": (f"no {what} of that name in {_library_name(library)}"
                                   if library else f"no {what} of that name in any library "
                                   "the project can use")}
        elif reason in (REASON_WORKSPACE, REASON_DATABASE):
            held = offline_libraries[key]
            finding = {"kind": f"{what}_{'workspace' if reason == REASON_WORKSPACE else 'database'}",
                       "severity": "info",
                       "message": (f"Workspace (Altium 365) parts: their {what}s are not "
                                   "readable offline" if reason == REASON_WORKSPACE else
                                   "database-library parts: their links come from the database, "
                                   "which is not read offline (roadmap #53)"),
                       "libraries": dict(held.most_common(EXAMPLES)),
                       "library_count": len(held)}
        elif reason == REASON_NO_NAME:
            finding = {"kind": f"{what}_no_name", "severity": "info",
                       "message": f"the part names no {what} to look up"}
        elif reason == "elsewhere":
            finding = {"kind": "footprint_from_another_library", "severity": "info",
                       "message": (f"placed from {_library_name(library)}, which does not hold "
                                   f"it (or is missing); found in {key[3]}, as a footprint link "
                                   "set to Any library would be"),
                       "found_in": key[3]}
        else:
            finding = {"kind": f"{what}_name_case_differs", "severity": "info",
                       "message": f"the {what} is found under a name that differs only in case"}
        finding["names"] = [name for name, _ in names.most_common(EXAMPLES)]
        findings.append(_part_finding(CHECK_RESOLUTION, finding,
                                      [r.designator for r in results], **where))
    return summary, findings


# ---------- footprint drift ----------

def _shape_and_numbers(value) -> tuple[object, list[float]]:
    """A finding's structure with its numbers taken out (each as '#') and the numbers in
    order: two differences of one structure whose numbers each lie within the tolerance
    are the same difference."""
    numbers: list[float] = []

    def strip(item):
        if item is None or isinstance(item, (bool, str)):
            return item
        if isinstance(item, (int, float)):
            numbers.append(float(item))
            return "#"
        if isinstance(item, dict):
            return tuple((key, strip(item[key])) for key in sorted(item))
        if isinstance(item, (list, tuple)):
            return tuple(strip(element) for element in item)
        return repr(item)

    return strip(value), numbers


def _check_drift(parts: _Parts, loader: ItemLoader, tolerance: float) -> tuple[dict, list[dict]]:
    if parts.pcb is None:
        return {"skipped": "no PcbDoc"}, []
    loader.load(footprints=[result.footprint_hit for _, result in parts.board])
    status: Counter = Counter()
    groups: dict[tuple, list[dict]] = {}
    pads, holes, bodies = (parts.by_component(what) for what in ("pad", "hole", "body"))
    tracks, arcs = parts.by_component("track"), parts.by_component("arc")
    fills, regions = parts.by_component("fill"), parts.by_component("region")
    for component, result in parts.board:
        hit = result.footprint_hit
        if hit is None:
            status[f"not compared: {result.footprint_reason}"] += 1
            continue
        footprint = loader.footprint(hit)
        if footprint is None or component.x is None:
            status["not compared: library unreadable" if footprint is None
                   else "not compared: no placement"] += 1
            continue
        index = component.index
        counts = primitive_counts(tracks.get(index, []), arcs.get(index, []),
                                  fills.get(index, []), regions.get(index, []),
                                  component.side == "bottom")
        differences = compare_footprint(component, footprint, pads.get(index, []),
                                        holes.get(index, []), bodies.get(index, []),
                                        counts, tolerance)
        if not differences:
            status["match"] += 1
            continue
        copper = any(d["severity"] == "warning" for d in differences)
        status["differs (pads)" if copper else "differs (height, bodies or other primitives)"] += 1
        for difference in differences:
            # Parts with the same difference (its numbers each within the tolerance: a
            # placement lands a few hundredths of a mil off) form one group.
            shape, numbers = _shape_and_numbers(
                {key: value for key, value in difference.items() if key != "message"})
            bucket = groups.setdefault(
                (os.path.normcase(hit.path), hit.name.casefold(), hit.named, shape), [])
            group = next((group for group in bucket if all(
                abs(a - b) <= tolerance for a, b in zip(group["numbers"], numbers))), None)
            if group is None:
                group = {"numbers": numbers, "names": [], "finding": {
                    "check": CHECK_DRIFT, "footprint": hit.name,
                    "library": _library_name(hit.path), **difference}}
                if not hit.named:
                    group["finding"]["compared_with"] = (
                        "a footprint of that name in another library: the one the part was "
                        f"placed from ({_library_name(result.footprint_library) or 'unknown'}) "
                        "does not hold it or is missing")
                bucket.append(group)
            group["names"].append(parts.board_name(component))
    findings = []
    for bucket in groups.values():
        for group in bucket:
            names = group["names"]
            findings.append({**group["finding"], "designators": _designators(names)[:EXAMPLES],
                             "parts": len(set(names))})
    summary = {"compared": sum(v for k, v in status.items() if not k.startswith("not")),
               "status": dict(sorted(status.items()))}
    return summary, findings


# ---------- pads cover the pins the nets use ----------

# Said of used pins whose only pad of their designator is a bare hole.
_ON_HOLES = (" ({} of them only on a bare hole, a pad without copper: there is no copper "
             "to connect)")


def _check_pins(parts: _Parts, loader: ItemLoader, resolver: LibraryResolver,
                swap: str | None) -> tuple[dict, list[dict], list[dict]]:
    if not parts.netlist:
        return {"skipped": "no netlist"}, [], []
    loader.load(footprints=[result.footprint_hit for result in parts.schematic])
    pads_of = parts.by_component("pad") if parts.pcb is not None else {}
    holes_of = parts.by_component("hole") if parts.pcb is not None else {}
    summary: Counter = Counter()
    findings: list[dict] = []
    swaps: list[dict] = []
    library_missing: dict[tuple, list[str]] = defaultdict(list)
    for result in parts.schematic:
        component = parts.components.get(result.designator) or {}
        used = used_pins(component)
        pins = {str(pin) for pin in (component.get("pin_designators") or component.get("pins") or [])}
        summary["parts"] += 1
        if component.get("pin_designators") and used - pins:
            # The netlist's pins for this designator are not its symbol's (another part
            # compiled under that name: a designator clash or board annotation).
            summary["skipped: netlist pins not on the part's symbol"] += 1
            continue
        footprint = loader.footprint(result.footprint_hit)
        if footprint is not None:
            summary["library footprint read"] += 1
            missing = sorted(used - copper_pad_names(footprint.pads), key=natural_key)
            if missing:
                hit = result.footprint_hit
                on_holes = tuple(pin for pin in missing
                                 if pin in bare_hole_names(footprint.holes))
                library_missing[(hit.name, _library_name(hit.path), tuple(missing),
                                 on_holes)].append(result.designator)
            else:
                summary["library footprint has every used pin"] += 1
        board = parts.board_of.get(result.designator)
        if board is not None:
            board_component = board[0]
            board_pads = pads_of.get(board_component.index, [])
            summary["placed on the board"] += 1
            missing = sorted(used - copper_pad_names(board_pads), key=natural_key)
            if missing:
                holes = bare_hole_names(holes_of.get(board_component.index, []))
                on_holes = [pin for pin in missing if pin in holes]
                message = (f"{len(missing)} pin(s) on nets have no copper pad in the placed "
                           "footprint: their connections are missing on the board")
                if len(on_holes) < len(missing):
                    message += " (Update PCB reports a pin without a pad as Unknown Pin)"
                finding = {
                    "kind": "used_pins_without_board_pad", "severity": "error",
                    "message": message + (_ON_HOLES.format(len(on_holes)) if on_holes else ""),
                    "pins": missing[:EXAMPLES], "footprint": board_component.footprint}
                if on_holes:
                    finding["on_bare_holes"] = on_holes[:EXAMPLES]
                if result.footprint and (board_component.footprint or "") != result.footprint:
                    finding["schematic_footprint"] = result.footprint
                    finding["message"] += ("; the board's footprint is not the schematic's "
                                           "(Update PCB brings it over)")
                findings.append(_part_finding(CHECK_PINS, finding, [result.designator]))
            else:
                summary["board footprint has every used pin"] += 1
            stray = sorted({pad.pad_name for pad in board_pads if pad.pad_name
                            and pad.net is not None and pad.pad_name not in pins and pins},
                           key=natural_key)
            if stray:
                findings.append(_part_finding(CHECK_PINS, {
                    "kind": "board_pads_on_nets_without_pins", "severity": "info",
                    "message": (f"{len(stray)} pad(s) of the placed footprint carry a net but "
                                "the symbol has no pin of that designator (a net assigned on "
                                "the board)"),
                    "pads": [{"pad": name, "net": parts.net_name(next(
                        pad.net for pad in board_pads if pad.pad_name == name))}
                        for name in stray[:EXAMPLES]]}, [result.designator]))
        if swap:
            # The part's own footprint library first (a Workspace part's too), then any.
            hit, reason = resolver.find_footprint(
                swap, result.footprint_library if library_kind(result.footprint_library)
                in FOOTPRINT_KINDS else "",
                result.library if library_kind(result.library) == "IntLib" else "")
            proposed = loader.footprint(hit)
            row = {"designator": result.designator, "footprint": swap,
                   "current_footprint": result.footprint, "source": _hit_text(hit, reason)}
            if proposed is None:
                row["keeps_all_used_pins"] = None
                row["message"] = "the proposed footprint was not found in the libraries " \
                                 "the project can use" if hit is None else "it could not be read"
            else:
                pads = copper_pad_names(proposed.pads)
                lost = sorted(used - pads, key=natural_key)
                on_holes = [pin for pin in lost if pin in bare_hole_names(proposed.holes)]
                row.update({
                    "keeps_all_used_pins": not lost, "used_pins": len(used),
                    "used_pins_without_pad": lost[:EXAMPLES * 4],
                    "pads_without_pins": sorted(pads - pins, key=natural_key)[:EXAMPLES * 4]
                    if pins else [],
                    "pad_count": len(proposed.pads), "hole_count": len(proposed.holes)})
                if on_holes:
                    row["used_pins_on_bare_holes"] = on_holes[:EXAMPLES * 4]
                if lost:
                    finding = {
                        "kind": "swap_loses_pins", "severity": "error",
                        "message": (f"the proposed footprint {swap} has no copper pad for "
                                    f"{len(lost)} pin(s) on nets: those connections would be "
                                    "lost" + (_ON_HOLES.format(len(on_holes))
                                              if on_holes else "")),
                        "pins": lost[:EXAMPLES], "footprint": swap}
                    if on_holes:
                        finding["on_bare_holes"] = on_holes[:EXAMPLES]
                    findings.append(_part_finding(CHECK_PINS, finding, [result.designator]))
            swaps.append(row)
    for (name, library, missing, on_holes), designators in library_missing.items():
        finding = {
            "kind": "used_pins_without_library_pad", "severity": "error",
            "message": (f"the library footprint has no copper pad for {len(missing)} pin(s) on "
                        "nets: updating the board from the library would drop their "
                        "connections" + (_ON_HOLES.format(len(on_holes)) if on_holes else "")),
            "pins": list(missing[:EXAMPLES]), "footprint": name, "library": library}
        if on_holes:
            finding["on_bare_holes"] = list(on_holes[:EXAMPLES])
        findings.append(_part_finding(CHECK_PINS, finding, designators))
    return dict(summary), findings, swaps


# ---------- component rule checks on the parts' library items ----------

def _check_symbol_rules(parts: _Parts, loader: ItemLoader) -> tuple[dict, list[dict]]:
    rows = parts.symbol_results()
    loader.load(symbols=[hit for _, hit, _ in rows])
    users: dict[tuple, list[str]] = defaultdict(list)
    hits: dict[tuple, LibraryHit] = {}
    for designator, hit, _ in rows:
        if hit is not None:
            key = (os.path.normcase(hit.path), hit.name.casefold())
            users[key].append(designator)
            hits[key] = hit
    findings: list[dict] = []
    for key, designators in users.items():
        hit = hits[key]
        symbol = loader.symbol(hit)
        if symbol is None:
            continue
        for finding in symbol_rule_findings(symbol):
            findings.append(_part_finding(CHECK_SYMBOL_RULES, finding, designators,
                                          symbol=hit.name, library=_library_name(hit.path)))
    # The symbol against the footprint each part uses with it.
    pairs: dict[tuple, list[str]] = defaultdict(list)
    pair_hits = {}
    footprint_of = {r.designator: r.footprint_hit for r in parts.schematic}
    for component, result in parts.board:
        sch = parts.sch_refdes.get(component.index)
        if sch:
            footprint_of[sch] = result.footprint_hit
    if not parts.netlist:
        footprint_of = {parts.board_name(c): r.footprint_hit for c, r in parts.board}
    for designator, hit, _ in rows:
        footprint_hit = footprint_of.get(designator)
        if hit is None or footprint_hit is None:
            continue
        key = (os.path.normcase(hit.path), hit.name.casefold(),
               os.path.normcase(footprint_hit.path), footprint_hit.name.casefold())
        pairs[key].append(designator)
        pair_hits[key] = (hit, footprint_hit)
    loader.load(footprints=[fp for _, fp in pair_hits.values()])
    for key, designators in pairs.items():
        hit, footprint_hit = pair_hits[key]
        symbol, footprint = loader.symbol(hit), loader.footprint(footprint_hit)
        if symbol is None or footprint is None:
            continue
        for finding in pin_pad_findings(symbol, footprint, unpadded="info"):
            findings.append(_part_finding(CHECK_SYMBOL_RULES, finding, designators,
                                          symbol=hit.name, footprint=footprint_hit.name,
                                          library=_library_name(hit.path)))
    return {"symbols": len(users), "symbol_footprint_pairs": len(pairs)}, findings


def _short_on_board(parts: _Parts, component, shorts: list[list[str]]) -> list[str] | None:
    """The board nets of a placed part's pads that one group of shorts (pad designators
    joined by copper) puts together, when they are two or more; else None."""
    if parts.pcb is None:
        return None
    nets: dict[str, set] = defaultdict(set)
    for pad in parts.by_component("pad").get(component.index, []):
        if pad.net is not None:
            nets[pad.pad_name].add(pad.net)
    for names in shorts:
        joined = set().union(*(nets.get(name, set()) for name in names))
        if len(joined) > 1:
            return sorted(filter(None, (parts.net_name(net) for net in joined)))
    return None


def placed_copper_report(parts: _Parts, component) -> CopperReport:
    """The shorted and unconnected copper of a placed footprint: the board's pads,
    tracks, arcs, fills and regions of the component."""
    index = component.index
    lines = [*parts.by_component("track").get(index, []), *parts.by_component("arc").get(index, [])]
    return copper_report(items=copper_items(
        parts.by_component("pad").get(index, []), lines,
        parts.by_component("fill").get(index, []), parts.by_component("region").get(index, [])))


def _check_footprint_rules(parts: _Parts, loader: ItemLoader) -> tuple[dict, list[dict]]:
    rows = parts.footprint_results()
    loader.load(footprints=[hit for _, hit, _ in rows])
    users: dict[tuple, list] = defaultdict(list)
    hits: dict[tuple, LibraryHit] = {}
    for designator, hit, kinds in rows:
        if hit is not None:
            key = (os.path.normcase(hit.path), hit.name.casefold())
            users[key].append((designator, kinds))
            hits[key] = hit
    board_by_name = {parts.board_name(c): c for c, _ in parts.board}
    findings: list[dict] = []
    for key, used_by in users.items():
        hit = hits[key]
        footprint = loader.footprint(hit)
        if footprint is None:
            continue
        net_tie = all(kinds & set(NET_TIE_KINDS) for _, kinds in used_by)
        report = copper_report(footprint)
        designators = [designator for designator, _ in used_by]
        for finding in footprint_rule_findings(footprint, net_tie, loader.declared(hit), report):
            if finding["kind"] == "shorted_copper":
                # The library item's rule; what it does on this board is net_ties' finding.
                apart = [name for name in designators if name in board_by_name
                         and _short_on_board(parts, board_by_name[name], report.shorts)]
                if apart:
                    finding = {**finding, "on_different_nets": _designators(apart)[:EXAMPLES],
                               "message": finding["message"] + "; this board has them on "
                               "different nets on some parts (net_ties reports them)"}
            findings.append(_part_finding(CHECK_FOOTPRINT_RULES, finding, designators,
                                          footprint=hit.name, library=_library_name(hit.path)))
    return {"footprints": len(users)}, findings


# ---------- net ties ----------

def _check_net_ties(parts: _Parts, loader: ItemLoader) -> tuple[dict, list[dict]]:
    """Net-tie parts: their footprint's copper joins its pads, the schematic, the PCB
    and the library agree that they are net ties, and on the board they tie different
    nets; and parts whose library footprint joins pads without being a net tie, where
    the board has those pads on different nets: an error when the placed copy joins them
    too (DRC reports a short), a warning when only the library's does (an update from
    the library would short them). One finding per footprint."""
    rows = []  # (designator, symbol hit, footprint hit, kinds by source, board component)
    seen = set()
    schematic = {result.designator: result for result in parts.schematic}
    for component, result in parts.board:
        sch = parts.sch_refdes.get(component.index)
        sch_result = schematic.get(sch) if sch else None
        kinds = {"pcb": component.kind}
        if sch:
            seen.add(sch)
            kinds["schematic"] = (parts.components.get(sch) or {}).get("kind", 0)
        rows.append((parts.board_name(component), (sch_result or result).symbol,
                     result.footprint_hit, kinds, component))
    for result in parts.schematic:
        if result.designator not in seen:
            rows.append((result.designator, result.symbol, result.footprint_hit,
                         {"schematic": (parts.components.get(result.designator) or {}).get("kind", 0)},
                         None))
    loader.load(symbols=[row[1] for row in rows], footprints=[row[2] for row in rows])
    summary: Counter = Counter()
    findings: list[dict] = []
    reports: dict[tuple, CopperReport] = {}
    # Parts that are not net ties whose library footprint joins pads the board has on
    # different nets: (kind, footprint key) -> (joined pads, {designator: nets}).
    shorted: dict[tuple, tuple[list, dict]] = {}
    for designator, symbol_hit, footprint_hit, kinds, component in rows:
        symbol = loader.symbol(symbol_hit)
        if symbol is not None:
            kinds = {**kinds, "library": symbol.kind}
        footprint = loader.footprint(footprint_hit)
        report = None
        if footprint is not None:
            key = (os.path.normcase(footprint_hit.path), footprint_hit.name.casefold())
            if key not in reports:
                reports[key] = copper_report(footprint)
            report = reports[key]
        tie = any(kind in NET_TIE_KINDS for kind in kinds.values())
        if not tie:
            if report and report.shorts and component is not None:
                nets = _short_on_board(parts, component, report.shorts)
                if nets:
                    # DRC reports a short only where the placed copy joins them too.
                    placed = placed_copper_report(parts, component)
                    on_board = _short_on_board(parts, component, placed.shorts)
                    kind = "short_not_net_tie" if on_board else "library_short_not_net_tie"
                    joined, by_part = shorted.setdefault(
                        (kind, footprint_hit.name, _library_name(footprint_hit.path)),
                        (placed.shorts if on_board else report.shorts, {}))
                    by_part[designator] = on_board or nets
            continue
        summary["net tie parts"] += 1
        where = {"footprint": footprint_hit.name} if footprint_hit else {}
        named = {source: KIND_NAMES.get(kind, str(kind)) for source, kind in kinds.items()}
        if len({kind in NET_TIE_KINDS for kind in kinds.values()}) > 1:
            findings.append(_part_finding(CHECK_NET_TIES, {
                "kind": "net_tie_type_differs", "severity": "warning",
                "message": ("the schematic, the PCB and the library do not agree that the "
                            "part is a net tie (Component Type): Update PCB or Update from "
                            "Libraries brings them back in line, and DRC only allows the "
                            "short on a Net Tie"), "types": named, **where}, [designator]))
        if report is None:
            summary["net ties whose footprint was not read"] += 1
            continue
        if not report.shorts:
            findings.append(_part_finding(CHECK_NET_TIES, {
                "kind": "net_tie_without_copper", "severity": "warning",
                "message": ("its footprint has no copper joining two pads: the net tie ties "
                            "nothing (add a track or region between the pads, inside the "
                            "footprint)"), "types": named, **where}, [designator]))
            continue
        tied = set().union(*report.shorts)
        loose = sorted({pad.pad_name for pad in footprint.pads
                        if pad.pad_name and pad.pad_name not in tied}, key=natural_key)
        if loose:
            findings.append(_part_finding(CHECK_NET_TIES, {
                "kind": "net_tie_pads_not_tied", "severity": "info",
                "message": "some of its pads are not joined to another pad by the tie's copper",
                "pads": loose[:EXAMPLES], "joined": report.shorts[:EXAMPLES], **where},
                [designator]))
        if component is not None and parts.pcb is not None:
            nets = {parts.net_name(pad.net) for pad in parts.by_component("pad").get(
                component.index, []) if pad.pad_name in tied and pad.net is not None}
            if len(nets) < 2:
                findings.append(_part_finding(CHECK_NET_TIES, {
                    "kind": "net_tie_one_net", "severity": "info",
                    "message": ("on this board its tied pads are on " +
                                ("one net" if nets else "no net") +
                                ": it ties nothing apart"), "nets": sorted(n for n in nets if n),
                    **where}, [designator]))
                continue
        summary["net ties joining their pads"] += 1
    for (kind, name, library), (joined, by_part) in shorted.items():
        summary["shorted parts that are not net ties" if kind == "short_not_net_tie" else
                "parts that are not net ties shorted only in the library footprint"] += len(by_part)
        names = _designators(by_part)
        findings.append(_part_finding(CHECK_NET_TIES, {
            "kind": kind, "severity": "error" if kind == "short_not_net_tie" else "warning",
            "message": ("the placed footprint's copper joins pads that are on different nets on "
                        "this board, and the part is not a Net Tie: DRC reports a short (make it "
                        "a Net Tie in the library and the schematic, or separate the pads)"
                        if kind == "short_not_net_tie" else
                        "the library footprint's copper joins pads that are on different nets "
                        "on this board (the placed copy does not join them): Update From PCB "
                        "Libraries would short them, and the part is not a Net Tie"),
            "joined": joined[:EXAMPLES], "footprint": name, "library": library,
            "nets": {designator: by_part[designator][:EXAMPLES] for designator in names[:EXAMPLES]}},
            names))
    return dict(summary), findings


# ---------- 3D bodies and Height ----------

def _check_height(parts: _Parts, loader: ItemLoader) -> tuple[dict, list[dict]]:
    """Per placed part: a 3D body with a model, an extruded body, only the footprint's
    Height (the fallback 3D height) or neither; from the library footprint without a
    PcbDoc. Graphical parts and net ties are left out."""
    categories: Counter = Counter()
    groups: dict[tuple, list[str]] = defaultdict(list)
    if parts.pcb is not None:
        bodies = parts.by_component("body")
        for component, _ in parts.board:
            if component.kind in (2, *NET_TIE_KINDS):
                continue
            category = body_category(bodies.get(component.index, []), component.height_mil)
            categories[category] += 1
            if category in ("height_only", "no_height"):
                groups[(category, component.footprint or "")].append(parts.board_name(component))
        source = "pcb"
    else:
        loader.load(footprints=[r.footprint_hit for r in parts.schematic])
        for result in parts.schematic:
            kind = (parts.components.get(result.designator) or {}).get("kind", 0)
            footprint = loader.footprint(result.footprint_hit)
            if kind in (2, *NET_TIE_KINDS) or footprint is None:
                continue
            category = body_category(footprint.bodies, footprint.height_mil)
            categories[category] += 1
            if category in ("height_only", "no_height"):
                groups[(category, result.footprint)].append(result.designator)
        source = "library footprints"
    findings = []
    for (category, footprint), designators in groups.items():
        findings.append(_part_finding(CHECK_HEIGHT, {
            "kind": category, "severity": "info",
            "message": ("no 3D body: only the footprint's Height (the fallback 3D height) "
                        "says how tall it is" if category == "height_only" else
                        "no 3D body and no Height: 3D clearance checks and assembly height "
                        "have nothing to go on")}, designators, footprint=footprint))
    return {"source": source, "parts": dict(sorted(categories.items()))}, findings


# ---------- where used ----------

_PART_CACHE: dict[tuple[str, int, int], list[tuple]] = {}


def document_parts(path: str) -> list[tuple]:
    """(source, designator, library reference, design item id, footprint, the symbol's
    library, the footprint's library) per part of a SchDoc (its compiled-free records)
    or a PcbDoc (its Components6 records), cached by path, size and modification time.
    Raises on an unreadable file."""
    stat = os.stat(path)
    key = (os.path.normcase(path), stat.st_size, stat.st_mtime_ns)
    if key in _PART_CACHE:
        return _PART_CACHE[key]
    kind = os.path.splitext(path)[1].lower()
    rows = []
    with olefile.OleFileIO(path) as ole:
        if kind == ".schdoc":
            for designator, meta in components_from_stream(ole.openstream("FileHeader").read()).items():
                rows.append(("schematic", designator, meta.lib_reference, meta.design_item_id,
                             meta.footprint or "", meta.library, meta.footprint_library))
        elif ole.exists("Components6/Data"):
            for component in decode_components(read_text_records(
                    ole.openstream("Components6/Data").read())):
                rows.append(("pcb", component.refdes or "", component.lib_reference, "",
                             component.footprint or "", component.component_library,
                             component.footprint_library))
    _PART_CACHE[key] = rows
    return rows


def project_files(folders: list[str], limit: int = 500) -> list[str]:
    """Every .PrjPcb under the folders (sorted walk), at most limit."""
    found: list[str] = []
    for folder in folders:
        for dirpath, dirnames, filenames in os.walk(folder):
            dirnames.sort()
            found += [os.path.join(dirpath, name) for name in sorted(filenames)
                      if name.lower().endswith(".prjpcb")]
            if len(found) >= limit:
                return found[:limit]
    return found


def registry_projects(registry: dict) -> list[str]:
    """The .PrjPcb of each registered project that is still on disk."""
    found = []
    for entry in registry.get("projects") or []:
        folder, name = entry.get("dir") or "", entry.get("name") or ""
        path = os.path.join(folder, name)
        if name.lower().endswith(".prjpcb") and os.path.isfile(path):
            found.append(path)
        elif os.path.isdir(folder):
            found += project_files([folder], limit=5)[:1]
    return list(dict.fromkeys(found))


def where_used(name: str, projects: list[str], library: str | None = None) -> dict:
    """Which projects place a symbol (library reference or design item id) or a
    footprint called name (case ignored), read offline from their SchDocs and PcbDocs:
    per project the schematic and PCB parts using it and their designators. library
    narrows to parts placed from that library file (a symbol's library for symbol
    matches, the footprint's for footprint matches; an IntLib holds both)."""
    wanted = name.strip().casefold()
    library_base = _base(library) if library else None
    uses, unreadable = [], 0
    for prj in projects:
        try:
            documents = parse_prj_documents(prj)
        except Exception:
            unreadable += 1
            continue
        counts: Counter = Counter()
        designators: list[str] = []
        for document in documents:
            if os.path.splitext(document.path)[1].lower() not in (".schdoc", ".pcbdoc") \
                    or not os.path.isfile(document.path):
                continue
            try:
                rows = document_parts(document.path)
            except Exception:
                counts["documents unreadable"] += 1
                continue
            for (source, designator, lib_reference, item_id, footprint, symbol_library,
                 footprint_library) in rows:
                symbol = wanted in (lib_reference.casefold(), item_id.casefold()) and (
                    not library_base or _base(symbol_library) == library_base)
                footprint_match = wanted == footprint.casefold() and (
                    not library_base or library_base in (
                        _base(footprint_library), _base(symbol_library)
                        if library_kind(symbol_library) == "IntLib" else None))
                if symbol:
                    counts[f"{source} symbol"] += 1
                if footprint_match:
                    counts[f"{source} footprint"] += 1
                if (symbol or footprint_match) and designator not in designators:
                    designators.append(designator)
        if any(key != "documents unreadable" for key in counts):
            uses.append({"project": os.path.basename(prj), "folder": os.path.dirname(prj),
                         "schematic_parts_with_symbol": counts["schematic symbol"],
                         "schematic_parts_with_footprint": counts["schematic footprint"],
                         "pcb_parts_with_symbol": counts["pcb symbol"],
                         "pcb_parts_with_footprint": counts["pcb footprint"],
                         "designators": _designators(designators)[:EXAMPLES * 2],
                         "parts": len(designators)})
    uses.sort(key=lambda row: (-row["parts"], row["project"].casefold()))
    return {"name": name, **({"placed_from_library": library} if library else {}),
            "projects_scanned": len(projects), "projects_using": len(uses),
            "projects_unreadable": unreadable, "uses": uses}


# ---------- a whole library ----------

def find_library_file(name: str, libraries: ProjectLibraries) -> LibraryFile | None:
    """A library the project can use, by file name or path, else a SchLib, PcbLib or
    IntLib at that path (relative to the project folder)."""
    wanted = _base(name)
    for library in (*libraries.project_libraries, *libraries.installed,
                    *libraries.search_path_libraries):
        if library.exists and (_base(library.path) == wanted
                               or os.path.normcase(library.path) == os.path.normcase(name)):
            return library
    path = name if os.path.isabs(name) else os.path.join(os.path.dirname(libraries.project), name)
    kind = library_kind(path)
    if kind in ("SchLib", "PcbLib", "IntLib") and os.path.isfile(path):
        return LibraryFile(path, kind, ORIGIN_SOURCE, True)
    return None


def _read_library(library: LibraryFile) -> tuple[list[SchLibComponent], list[tuple[Footprint, bool]], list[str]]:
    """(symbols, (footprint, its library declares mechanical layer kinds), warnings)."""
    if library.kind == "SchLib":
        read = parse_sch_lib(library.path)
        return read.components, [], read.warnings
    if library.kind == "PcbLib":
        read = parse_pcb_lib(library.path)
        return [], [(fp, bool(read.mech_kinds)) for fp in read.footprints], read.warnings
    read = parse_int_lib(library.path)
    return ([c for sch in read.sch_libs for c in sch.components],
            [(fp, bool(pcb.mech_kinds)) for pcb in read.pcb_libs for fp in pcb.footprints],
            [warning for lib in (*read.sch_libs, *read.pcb_libs) for warning in lib.warnings]
            + read.warnings)


def _check_whole_library(library: LibraryFile, checks: list[str], resolver: LibraryResolver,
                         loader: ItemLoader) -> tuple[dict, list[dict]]:
    """The component rule checks, net ties and 3D bodies on every item of one library;
    a symbol's footprint is looked up in the same library first, then as its link
    names it."""
    symbols, footprints, warnings = _read_library(library)
    own = {fp.name.casefold(): fp for fp, _ in footprints}
    name = _library_name(library.path)
    findings: list[dict] = []
    summary: dict = {"symbols": len(symbols), "footprints": len(footprints)}
    if warnings:
        summary["read_warnings"] = warnings[:EXAMPLES]

    # The symbols' footprints in other libraries, read together (one pass per library).
    links: dict[tuple[str, str], LibraryHit | None] = {}
    for symbol in symbols:
        link = symbol.current_footprint
        if (link is not None and link.name.casefold() not in own
                and (link.name, link.library) not in links):
            links[(link.name, link.library)] = resolver.find_footprint(link.name, link.library)[0]
    loader.load(footprints=[hit for hit in links.values() if hit is not None])

    def footprint_of(symbol: SchLibComponent) -> Footprint | None:
        link = symbol.current_footprint
        if link is None:
            return None
        if link.name.casefold() in own:
            return own[link.name.casefold()]
        return loader.footprint(links.get((link.name, link.library)))

    tie_footprints = {link.name.casefold() for symbol in symbols if symbol.kind in NET_TIE_KINDS
                      for link in symbol.footprints}
    if CHECK_SYMBOL_RULES in checks:
        for symbol in symbols:
            for finding in symbol_rule_findings(symbol):
                findings.append({"check": CHECK_SYMBOL_RULES, "symbol": symbol.lib_reference,
                                 "library": name, **finding})
            footprint = footprint_of(symbol)
            if footprint is not None:
                for finding in pin_pad_findings(symbol, footprint):
                    findings.append({"check": CHECK_SYMBOL_RULES, "symbol": symbol.lib_reference,
                                     "footprint": footprint.name, "library": name, **finding})
    if CHECK_FOOTPRINT_RULES in checks:
        for footprint, declared in footprints:
            for finding in footprint_rule_findings(footprint, footprint.name.casefold() in tie_footprints,
                                                   declared):
                findings.append({"check": CHECK_FOOTPRINT_RULES, "footprint": footprint.name,
                                 "library": name, **finding})
    if CHECK_NET_TIES in checks:
        ties = [symbol for symbol in symbols if symbol.kind in NET_TIE_KINDS]
        summary["net_tie_symbols"] = len(ties)
        for symbol in ties:
            footprint = footprint_of(symbol)
            if footprint is None:
                continue
            report = copper_report(footprint)
            where = {"symbol": symbol.lib_reference, "footprint": footprint.name, "library": name}
            if not report.shorts:
                findings.append({"check": CHECK_NET_TIES, "kind": "net_tie_without_copper",
                                 "severity": "warning", **where,
                                 "message": ("its footprint has no copper joining two pads: the "
                                             "net tie ties nothing")})
    if CHECK_HEIGHT in checks:
        categories: Counter = Counter()
        for footprint, _ in footprints:
            category = body_category(footprint.bodies, footprint.height_mil)
            categories[category] += 1
            if category == "no_height":
                findings.append({"check": CHECK_HEIGHT, "kind": "no_height", "severity": "info",
                                 "footprint": footprint.name, "library": name,
                                 "message": "no 3D body and no Height"})
        summary["footprints_by_3d"] = dict(sorted(categories.items()))
    return summary, findings


# ---------- check_libraries ----------

LIBRARY_CHECK_NOTES = [
    "Library items are read from the files on disk the way Altium resolves a part: the "
    "project's libraries, the installed libraries, then its search paths (the order of the "
    "Available File-based Libraries tabs); a symbol in the "
    "library the part names, a footprint in its named library first, then in any.",
    "footprint_drift compares each placed footprint with its library footprint as "
    "Altium's Footprint Comparison Report does. A difference says they differ, not which "
    "is right: Tools > Update From PCB Libraries brings the board's to the library's, "
    "Design > Make PCB Library (or editing the library) goes the other way. Pad "
    "positions are in the footprint's own frame (as its library draws it; a bottom-side "
    "part flipped back).",
    "The component rule checks are Altium's SchLib and PcbLib Component Rule Checks run "
    "offline on the items the project uses (library= for a whole library).",
]


def library_overview(libraries: ProjectLibraries) -> dict:
    return {
        "project_libraries": [{"name": _library_name(library.path), "kind": library.kind,
                               "on_disk": library.exists}
                              for library in libraries.project_libraries],
        "search_paths": [{"path": search.raw, "on_disk": search.exists}
                         for search in libraries.search_paths],
        "search_path_libraries": len(libraries.search_path_libraries),
        "installed_libraries": sum(library.exists for library in libraries.installed),
        "available": len(libraries.available()),
    }


def _offline_counts(parts: _Parts) -> dict:
    counts: Counter = Counter()
    for result in [*parts.schematic, *(r for _, r in parts.board)]:
        for reason in (result.symbol_reason, result.footprint_reason):
            if reason in (REASON_WORKSPACE, REASON_DATABASE):
                counts[f"{result.source} {reason}"] += 1
                break
    return dict(sorted(counts.items()))


def run_library_checks(inputs: LibraryCheckInputs) -> dict:
    """check_libraries' model: the checks' summaries and findings (unpaged)."""
    unknown = [check for check in inputs.checks or [] if check not in ALL_CHECKS]
    if unknown:
        return {"error": "invalid_checks",
                "message": f"Unknown checks {unknown}. Known: {', '.join(ALL_CHECKS)}."}
    if inputs.footprint and not inputs.parts:
        return {"error": "part_required",
                "message": "footprint= checks a footprint swap: name the part(s) to swap "
                           "with part=."}
    if inputs.footprint and inputs.library:
        return {"error": "invalid_arguments",
                "message": "footprint= (a swap check on the project's parts) and library= "
                           "(a whole library's checks) do not go together."}
    if inputs.checks:
        checks = list(dict.fromkeys(inputs.checks))
    elif inputs.where_used:
        checks = [CHECK_WHERE_USED]
    elif inputs.library:
        checks = list(LIBRARY_CHECKS)
    else:
        checks = list(PROJECT_CHECKS)
    if CHECK_WHERE_USED in checks and not (inputs.where_used or "").strip():
        return {"error": "where_used_name_required",
                "message": "where_used needs the symbol or footprint name: where_used='<name>'."}
    if inputs.footprint and CHECK_PINS not in checks:
        checks.append(CHECK_PINS)
    resolver = LibraryResolver(inputs.libraries)
    loader = ItemLoader()
    model: dict = {"checks_run": checks, "libraries": library_overview(inputs.libraries)}
    summary: dict = {}
    findings: list[dict] = []
    if inputs.library and checks != [CHECK_WHERE_USED]:
        library = find_library_file(inputs.library, inputs.libraries)
        if library is None:
            return {"error": "library_not_found",
                    "message": (f"No library '{inputs.library}' among the libraries the project "
                                "can use, and no SchLib, PcbLib or IntLib at that path."),
                    "available": sorted(_library_name(lib.path)
                                        for lib in inputs.libraries.available())[:40]}
        if library.kind not in ("SchLib", "PcbLib", "IntLib"):
            return {"error": "library_not_readable",
                    "message": f"{library.kind} files are not read offline (only SchLib, PcbLib "
                               "and IntLib)."}
        model["library"] = {"name": _library_name(library.path), "path": library.path,
                            "kind": library.kind, "origin": library.origin}
        try:
            library_summary, found = _check_whole_library(
                library, [check for check in checks if check in LIBRARY_CHECKS], resolver, loader)
        except Exception as error:
            return {"error": "library_unreadable", "message": f"{type(error).__name__}: {error}"}
        summary["library"] = library_summary
        findings += found
        skipped = [check for check in checks if check not in (*LIBRARY_CHECKS, CHECK_WHERE_USED)]
        if skipped:
            model["not_run"] = {"checks": skipped,
                                "reason": "they look at the project's parts, not a library"}
    elif any(check != CHECK_WHERE_USED for check in checks):
        parts = _Parts(inputs, resolver)
        if inputs.parts and not (parts.schematic or parts.board):
            return {"error": "part_not_found",
                    "message": (f"No part matches {', '.join(inputs.parts)} (designators, "
                                "library references and footprint names are matched).")}
        missing = sorted({name.strip() for name in inputs.parts or []
                          if name.strip().casefold() not in parts.matched}, key=natural_key)
        if missing:
            model["parts_not_found"] = missing
        model["parts"] = {"schematic": len(parts.schematic), "pcb": len(parts.board)}
        for check in checks:
            if check == CHECK_RESOLUTION:
                summary[check], found = _check_resolution(parts)
            elif check == CHECK_DRIFT:
                summary[check], found = _check_drift(parts, loader, inputs.tolerance_mil)
            elif check == CHECK_PINS:
                summary[check], found, swaps = _check_pins(parts, loader, resolver,
                                                           inputs.footprint)
                if inputs.footprint:
                    model["swap"] = swaps
            elif check == CHECK_SYMBOL_RULES:
                summary[check], found = _check_symbol_rules(parts, loader)
            elif check == CHECK_FOOTPRINT_RULES:
                summary[check], found = _check_footprint_rules(parts, loader)
            elif check == CHECK_NET_TIES:
                summary[check], found = _check_net_ties(parts, loader)
            elif check == CHECK_HEIGHT:
                summary[check], found = _check_height(parts, loader)
            else:
                continue
            findings += found
        offline = _offline_counts(parts)
        if offline:
            model["not_readable_offline_parts"] = offline
    if CHECK_WHERE_USED in checks:
        model["where_used"] = where_used(inputs.where_used, inputs.projects, inputs.library)
    if loader.errors:
        model["unreadable_libraries"] = {_library_name(path): error
                                         for path, error in sorted(loader.errors.items())}
    findings = _sort_findings(_group_bulk(findings))
    model["summary"] = summary
    model["severity_counts"] = dict(sorted(Counter(f["severity"] for f in findings).items(),
                                           key=lambda item: SEVERITY_ORDER[item[0]]))
    model["findings"] = findings
    model["not_available_offline"] = NOT_AVAILABLE_OFFLINE
    model["notes"] = LIBRARY_CHECK_NOTES
    return model


def page_library_checks(model: dict, offset: int, limit: int) -> dict:
    """One page of findings; everything else comes with every page."""
    offset = max(0, int(offset or 0))
    result = {key: value for key, value in model.items() if key not in ("findings", "notes")}
    findings = model["findings"]
    result["findings"] = findings[offset:offset + limit]
    result["findings_total"] = len(findings)
    result["offset"] = offset
    result["has_more"] = offset + limit < len(findings)
    if result["has_more"]:
        result["next_offset"] = offset + limit
    result["notes"] = model["notes"]
    return result


# ---------- get_library_part ----------

def symbol_detail(symbol: SchLibComponent) -> dict:
    """A library symbol as get_library_part shows it: its pins with their electrical
    types (part 0: on every part; display_mode only for alternate graphics)."""
    pins = []
    for pin in sorted(symbol.pins, key=lambda p: (p.display_mode, natural_key(p.designator), p.part)):
        row = {"designator": pin.designator, "name": pin.name, "electrical": pin.electrical,
               "part": pin.part}
        if pin.hidden:
            row["hidden"] = True
        if pin.display_mode:
            row["display_mode"] = pin.display_mode
        pins.append(row)
    others = Counter(model.model_type for model in symbol.models if model.model_type != "PCBLIB")
    detail = {
        "lib_reference": symbol.lib_reference, "design_item_id": symbol.design_item_id,
        "description": symbol.description, "default_designator": symbol.designator,
        "comment": symbol.comment, "type": KIND_NAMES.get(symbol.kind, str(symbol.kind)),
        "parts": symbol.part_count,
        "footprint_links": [{"name": model.name, "current": model.current,
                             "library": model.library or ("the component's own library"
                                                          if model.from_component_library
                                                          else "any")}
                            for model in symbol.footprints],
        "pin_count": len([pin for pin in symbol.pins if pin.display_mode == 0]),
        "pins": pins,
    }
    if symbol.aliases:
        detail["aliases"] = symbol.aliases
    if others:
        detail["other_models"] = dict(sorted(others.items()))
    if symbol.parameters:
        detail["parameters"] = dict(list(symbol.parameters.items())[:30])
    return detail


def footprint_detail(footprint: Footprint, kinds_declared: bool) -> dict:
    """A library footprint as get_library_part shows it (mil, its own frame)."""
    pads = []
    for pad in sorted(library_pads(footprint), key=lambda p: natural_key(p.designator)):
        row = {"designator": pad.designator, "x_mil": round(pad.x, 4), "y_mil": round(pad.y, 4),
               "layer": layer_label(pad.layer)}
        if pad.copper:
            row.update({"shape": PAD_SHAPES.get(pad.shape, str(pad.shape)),
                        "width_mil": round(pad.width, 4), "height_mil": round(pad.height, 4),
                        "rotation": round(pad.rotation, 3)})
            if pad.shape == PAD_SHAPE_ROUNDRECT and pad.corner_radius is not None:
                row["corner_radius_mil"] = round(pad.corner_radius, 4)
        else:
            row["bare_hole"] = True
        if pad.hole:
            row["hole_mil"] = round(pad.hole, 4)
            if pad.hole_shape in (1, 2):
                row["hole_shape"] = HOLE_SHAPES[pad.hole_shape]
                row["slot_length_mil"] = round(pad.slot_length or 0.0, 4)
            if pad.plated is not None:
                row["plated"] = pad.plated
        pads.append(row)
    courtyard = footprint.outline("courtyard")
    detail = {
        "name": footprint.name, "description": footprint.description,
        "height_mil": footprint.height_mil, "pad_count": len(footprint.pads),
        "hole_count": len(footprint.holes),
        "pads": pads,
        "outlines": [{"kind": outline.kind, "side": outline.side,
                      "layer": f"Mechanical {outline.mechanical}",
                      "box_mil": [round(value, 4) for value in outline.box]}
                     for outline in footprint.outlines],
        "courtyard": ("found" if courtyard else "none on its library's courtyard layers"
                      if kinds_declared else "its library declares no courtyard layers"),
        "bodies_summary": _body_text(footprint.bodies),
        "bodies": [{"model": body.model_name, "embedded": body.model_embedded,
                    "layer": body.v7_layer or layer_label(body.layer),
                    "overall_height_mil": body.overall_height_mil,
                    "standoff_height_mil": body.standoff_height_mil}
                   for body in footprint.bodies],
        "3d": body_category(footprint.bodies, footprint.height_mil),
        "primitives": footprint.records,
    }
    return detail


def library_part(libraries: ProjectLibraries, netlist: dict | None = None, pcb=None,
                 sch_refdes: dict[int, str] | None = None, designator: str | None = None,
                 lib_reference: str | None = None, footprint: str | None = None,
                 library: str | None = None,
                 tolerance: float = POSITION_TOLERANCE_MIL) -> dict:
    """get_library_part's model: a placed part's (designator) or a library item's
    (lib_reference, footprint; library narrows the search to that file) symbol and
    footprint with where they were found, and for a placed part its rule checks, its
    pins on nets against the pads and its footprint against the library's."""
    resolver = LibraryResolver(libraries)
    loader = ItemLoader()
    model: dict = {}
    symbol_hit = footprint_hit = None
    symbol_reason = footprint_reason = ""
    board = None
    schematic = None
    if designator:
        parts = _Parts(LibraryCheckInputs(libraries, netlist, pcb, sch_refdes), resolver)
        wanted = designator.strip().casefold()
        schematic = next((r for r in parts.schematic if r.designator.casefold() == wanted), None)
        board = next(((c, r) for c, r in parts.board
                      if wanted in {parts.board_name(c).casefold(),
                                    (parts.sch_refdes.get(c.index) or "").casefold()}), None)
        if schematic is None and board is None:
            return {"error": "component_not_found",
                    "message": f"No part '{designator}' in the netlist or on the PCB."}
        if schematic is None and board is not None:
            sch_name = parts.sch_refdes.get(board[0].index)
            schematic = next((r for r in parts.schematic if r.designator == sch_name), None)
        model["designator"] = designator
        if schematic is not None:
            component = parts.components.get(schematic.designator) or {}
            model["schematic"] = {
                "designator": schematic.designator, "lib_reference": schematic.lib_reference,
                "design_item_id": schematic.design_item_id, "library": schematic.library,
                "footprint": schematic.footprint, "footprint_library": schematic.footprint_library,
                "type": KIND_NAMES.get(component.get("kind", 0), "Standard")}
            symbol_hit, symbol_reason = schematic.symbol, schematic.symbol_reason
            footprint_hit, footprint_reason = schematic.footprint_hit, schematic.footprint_reason
            if schematic.symbol_elsewhere:
                model["symbol_elsewhere"] = _hit_text(schematic.symbol_elsewhere)
        if board is not None:
            component, result = board
            model["placement"] = {
                "designator": parts.board_name(component), "footprint": component.footprint,
                "x_mil": component.x, "y_mil": component.y, "rotation": component.rotation,
                "side": component.side, "height_mil": component.height_mil,
                "type": KIND_NAMES.get(component.kind, str(component.kind)),
                "footprint_library": component.footprint_library,
                "component_library": component.component_library,
                "lib_reference": component.lib_reference}
            footprint_hit, footprint_reason = result.footprint_hit, result.footprint_reason
            if schematic is None:
                symbol_hit, symbol_reason = result.symbol, result.symbol_reason
    else:
        kind = library_kind(library or "")
        if library and (kind not in (*SYMBOL_KINDS, *FOOTPRINT_KINDS)
                        or (not footprint and kind not in SYMBOL_KINDS)
                        or (not lib_reference and kind not in FOOTPRINT_KINDS)):
            return {"error": "library_kind_mismatch",
                    "message": (f"library= '{library}' cannot hold what was asked for: a "
                                "symbol (lib_reference=) is in a SchLib or IntLib, a footprint "
                                "(footprint=) in a PcbLib or IntLib.")}
        if lib_reference:
            if library and kind in SYMBOL_KINDS:
                symbol_hit, symbol_reason = resolver.find_symbol([lib_reference], library)
            else:
                symbol_hit, symbol_reason = resolver.find_symbol([lib_reference])
                if library:
                    model.setdefault("notes", []).append(
                        f"library= is a {kind}, which holds no symbols: the symbol was looked "
                        "up in every library the project can use")
            if symbol_hit is None and symbol_reason in (REASON_FILE_MISSING, REASON_NOT_FOUND):
                elsewhere = resolver.elsewhere([lib_reference], footprint=False)
                if elsewhere:
                    model["symbol_elsewhere"] = _hit_text(elsewhere)
        if footprint:
            if library and kind in FOOTPRINT_KINDS:
                # In that library only; a footprint of that name elsewhere is a hint.
                footprint_hit, footprint_reason = resolver.find_footprint(
                    footprint, library, search_all=False)
                if footprint_hit is None:
                    elsewhere = resolver.elsewhere([footprint], footprint=True)
                    if elsewhere:
                        model["footprint_elsewhere"] = _hit_text(elsewhere)
            else:
                footprint_hit, footprint_reason = resolver.find_footprint(footprint)
                if library:
                    model.setdefault("notes", []).append(
                        f"library= is a {kind}, which holds no footprints: the footprint was "
                        "looked up in every library the project can use")
        elif symbol_hit is not None:
            symbol = loader.symbol(symbol_hit)
            link = symbol.current_footprint if symbol else None
            if link is not None:
                own = symbol_hit.path if symbol_hit.kind == "IntLib" else ""
                footprint_hit, footprint_reason = resolver.find_footprint(
                    link.name, link.library or own, own)
    symbol = loader.symbol(symbol_hit)
    found_footprint = loader.footprint(footprint_hit)
    if lib_reference or designator:
        model["symbol"] = {**_hit_text(symbol_hit, symbol_reason),
                           **(symbol_detail(symbol) if symbol else {})}
    if footprint or designator or found_footprint is not None or footprint_reason:
        model["footprint"] = {**_hit_text(footprint_hit, footprint_reason),
                              **(footprint_detail(found_footprint, loader.declared(footprint_hit))
                                 if found_footprint else {})}
    checks: dict = {}
    if symbol is not None:
        checks["symbol_rules"] = symbol_rule_findings(symbol)
    if found_footprint is not None:
        tie = symbol is not None and symbol.kind in NET_TIE_KINDS
        if board is not None:
            tie = tie or board[0].kind in NET_TIE_KINDS
        checks["footprint_rules"] = footprint_rule_findings(
            found_footprint, tie, loader.declared(footprint_hit))
    if symbol is not None and found_footprint is not None:
        checks["pins_vs_pads"] = pin_pad_findings(symbol, found_footprint)
    if designator and schematic is not None and found_footprint is not None:
        component = (netlist or {}).get("components", {}).get(schematic.designator) or {}
        used = used_pins(component)
        pins = {str(pin) for pin in component.get("pin_designators") or []}
        checks["used_pins"] = len(used)
        if pins and used - pins:
            checks["used_pins_without_library_pad"] = (
                "not checked: the netlist's pins for this designator are not its symbol's "
                "(another part compiled under the same designator)")
        else:
            missing = sorted(used - copper_pad_names(found_footprint.pads), key=natural_key)
            checks["used_pins_without_library_pad"] = missing
            on_holes = [pin for pin in missing if pin in bare_hole_names(found_footprint.holes)]
            if on_holes:
                checks["used_pins_on_library_bare_holes"] = on_holes
    if board is not None and found_footprint is not None and board[0].x is not None:
        component = board[0]
        index = component.index
        grouped = {what: parts.by_component(what) for what in
                   ("pad", "hole", "body", "track", "arc", "fill", "region")}
        counts = primitive_counts(grouped["track"].get(index, []), grouped["arc"].get(index, []),
                                  grouped["fill"].get(index, []), grouped["region"].get(index, []),
                                  component.side == "bottom")
        differences = compare_footprint(component, found_footprint, grouped["pad"].get(index, []),
                                        grouped["hole"].get(index, []),
                                        grouped["body"].get(index, []), counts, tolerance)
        checks["footprint_drift"] = {"status": "differs" if differences else "match",
                                     "differences": differences}
        checks["3d"] = body_category(grouped["body"].get(index, []), component.height_mil)
    if checks:
        model["checks"] = checks
    if (symbol_hit is None and symbol_reason in (REASON_WORKSPACE, REASON_DATABASE)) or (
            footprint_hit is None and footprint_reason in (REASON_WORKSPACE, REASON_DATABASE)):
        model["not_available_offline"] = NOT_AVAILABLE_OFFLINE
    if loader.errors:
        model["unreadable_libraries"] = {_library_name(path): error
                                         for path, error in sorted(loader.errors.items())}
    return model


def page_library_part(model: dict, offset: int, limit: int) -> dict:
    """The part with its symbol's pins and its footprint's pads and 3D bodies paged
    together (bodies_summary gives every body in brief)."""
    offset = max(0, int(offset or 0))
    result = dict(model)
    more = False
    for key, rows in (("symbol", "pins"), ("footprint", "pads"), ("footprint", "bodies")):
        if key in result and rows in result[key]:
            items = model[key][rows]
            result[key] = {**result[key], rows: items[offset:offset + limit],
                           f"{rows}_total": len(items)}
            more = more or offset + limit < len(items)
    result["offset"] = offset
    result["has_more"] = more
    if more:
        result["next_offset"] = offset + limit
    return result
