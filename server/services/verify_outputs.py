"""verify_outputs: the files Altium generated (pick and place, BOM, IPC-D-356A and
IPC-2581 netlists, NC drill files with their .DRR report and .LDP layer pairs) cross-checked
against the saved design they came from, offline.

Where the files are: an explicit folder or .zip (zips are read in memory, one nested zip
level deep; nothing is extracted), else the folders the project's OutJobs generate into
(get_output_jobs' containers; a container folder named by an expression only Altium
resolves, such as CurrentDate, is searched from its base, where each generation has its
own folder and only the newest generation of each kind is checked: by the date and time
in the folder names, else by file time) and the project's Output Path. Files are
classified by content (parsers/fab_outputs.detect_output_kind; a workbook is a BOM once a
BOM header row is found in it), identical files are checked once, and each is matched to
the OutJob output whose folder holds it (its variant, data source and settings).

The checks (each mismatch is a warning, a missing output or a stale file is info):

- pick_place: every fitted part of the file's variant (the Variant line of its preamble;
  [No Variations] fits every part; with a variant the project lacks, a part some variant
  does not fit may be absent) present once, Not Fitted parts absent, side and rotation
  (mod 360; Altium writes the component's own rotation on both sides) equal, Center-X/Y
  within the position tolerance of the midpoint of the part's pad centres relative to the
  board origin (dfm_assembly's centroid, validated on Altium's own files; a part without
  pads at the absolute origin), Ref-X/Y and Pad-X/Y when the file has them (a bottom-side
  Y written negated is Altium's Y-flip option), footprint (the file writes a space in its
  name as '_'), and Comment and Description against the variant's BOM part (info).
  Standard (No BOM), Graphical and Net Tie parts may be listed or not (Altium's "Include
  Standard (No BOM) Items" option); designators not on the board are extra. A file whose
  parts mostly sit elsewhere is called out once (layout_differs: another revision).
- bom: the designators of the fitted lines equal the variant's BOM parts as get_bom
  builds them (lines a 'Fitted' column or a zero quantity marks Not Fitted must be the
  variant's Not Fitted parts), each line's quantity equals its designator count, and the
  Comment, Value, Footprint and Manufacturer Part Number columns the file has equal the
  part's (Description and Manufacturer as info: part choices and supplier data word them
  their own way); a column named after a parameter shows that parameter, its special
  strings resolved. A BOM generated from an ActiveBOM document (its OutJob output's data
  source is a .BomDoc) is checked against that BomDoc (services/active_bom): Manufacturer
  Part Number against the line's chosen solution (on a line without part choices the
  parts' MPN parameter, which ActiveBOM falls back to on most rows, not all: info,
  field_mpn_parameter), Manufacturer against it and its aliases (info), Supplier and
  Supplier Part Number columns against the chosen solution's part choices, and Line #
  against the BomDoc's line number (empty for an unnumbered line; info when the BomDoc
  gives a number to several lines, as an export may number them otherwise); when the
  BomDoc cannot be read the part number differences are info, unverified.
  Differences are reported, not judged. The variant: the file's
  title block, else its OutJob output (unless the file's parts are exactly another
  variant's: info), else a variant named in its path, else the variant whose parts fit
  it best.
- netlist: IPC-D-356A (317/327 records; vias and 367 tooling holes left out, N/C is
  unconnected; designators cut to 6, pins to 4 and nets to 14 characters as the format
  does) and IPC-2581 LogicalNets against the PcbDoc's pad nets, pin by pin: pins missing or
  extra, pins connected on one side only, nets split or merged (with the pins on the
  smaller side), and names that differ, each with designator, pin and XY. IPC-2581
  components against the placement (side, rotation, the populate flags against the
  variant).
- stackup: IPC-2581's stackup against the PcbDoc's layer stack (copper layer count, names
  and order, copper thickness, and the dielectrics between the outer copper layers:
  thickness within the tolerance, Dk and material where both have them), and the layer
  names the .LDP layer pairs and .DRR reports use.
- drill: per drill set (a .DRR report's layer pair with the drill files it names, an .LDP
  pair's files no report names, else a folder's other drill files taken as the through
  holes), the drill files' tools against the PcbDoc's pad, via and bare holes of the pair
  (coincident ones drilled once): sizes within half the tool size's written precision
  (plus drill_tolerance), plating from the ;TYPE sections, hole and slot counts per size,
  and the holes' positions (relative or absolute origin as the OutJob sets it, else
  whichever fits); the .DRR report against the drill files it names, or against the holes
  when they are not there. A report or layer-pairs file naming another PcbDoc is called
  out (other_board) and its files are not compared.
- staleness: a design file saved after the output file (by more than
  output_jobs.STALE_TOLERANCE_S) makes it stale: the OutJob output's data sources, else
  the PcbDoc (and for a BOM the sheets and .BomDocs), and the .PrjPcb for pick and place
  and BOMs when the project has variants (file times: a copy or checkout resets them, and
  the comparisons above are what shows an output out of date).

Not read: Gerber and ODB++ contents (counted only), .xls workbooks, the EIA drill format,
Board Stack Reports (no layout to rely on). Pure logic, no MCP; lengths in mil."""

import hashlib
import io
import math
import os
import re
import time
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

try:
    from parsers import fab_outputs as fo
    from services import dfm_assembly, dfm_geometry
    from services.bom import (
        BASE_VARIANT, BOM_KINDS, build_parts, natural_key, norm_name, plan_variant, resolve,
        variant_names,
    )
    from services.output_jobs import (
        NO_VARIATIONS, STALE_TOLERANCE_S, output_locations, utc_iso,
    )
    from services.via_checks import layer_token_v6
    from services import active_bom
except ImportError:
    from server.parsers import fab_outputs as fo
    from server.services import dfm_assembly, dfm_geometry
    from server.services.bom import (
        BASE_VARIANT, BOM_KINDS, build_parts, natural_key, norm_name, plan_variant, resolve,
        variant_names,
    )
    from server.services.output_jobs import (
        NO_VARIATIONS, STALE_TOLERANCE_S, output_locations, utc_iso,
    )
    from server.services.via_checks import layer_token_v6
    from server.services import active_bom

CHECKS = ("pick_place", "bom", "netlist", "stackup", "drill", "staleness")
MM = 1 / 0.0254
DEFAULT_POSITION_TOLERANCE_MIL = 0.01 * MM
DEFAULT_ROTATION_TOLERANCE_DEG = 0.01
DEFAULT_DRILL_TOLERANCE_MIL = 0.01
DEFAULT_THICKNESS_TOLERANCE_PERCENT = 10.0
HOLE_POSITION_MIL = 1.0           # a drill hit this close to a design hole is that hole
POSITION_FIT_SHARE = 0.5          # fewer holes on design holes: positions not compared
EXAMPLES_KEPT = 25                # examples stored per finding (pages show fewer)
PINS_KEPT = 10                    # pins listed per split, merged or renamed net
FILES_LISTED = 60
MAX_FILES = 300                   # files parsed per call
MAX_FILE_BYTES = 256 << 20
MAX_WALK = 20_000
MAX_DEPTH = 8
HEAD_BYTES = 16 << 10
SHRINK_STEPS = ((60, 5), (30, 3), (15, 1), (5, 0))
CACHE_SIZE = 4

# Output families: the kinds of file each OutJob output type generates.
FAMILY_OF_KIND = {
    "pick_place": "pick_place", "bom": "bom", "bom_workbook": "bom", "ipc356": "ipc356",
    "ipc2581": "ipc2581", "excellon": "drill", "drill_report": "drill",
    "layer_pairs": "drill", "gerber": "gerber", "odb": "odb",
}
FAMILY_OF_TYPE = {
    "Pick Place": "pick_place", "BOM_PartType": "bom", "SimpleBOM": "bom",
    "NC Drill": "drill", "Test Points": "ipc356", "Test Points For Assembly": "ipc356",
    "IPC2581": "ipc2581", "Gerber": "gerber", "Gerber X2": "gerber", "ODB": "odb",
}
FAMILY_LABELS = {
    "pick_place": "pick-and-place", "bom": "BOM", "ipc356": "IPC-D-356A netlist",
    "ipc2581": "IPC-2581", "drill": "NC drill", "gerber": "Gerber", "odb": "ODB++",
}
# Parts Altium's pick-and-place lists whatever its options (ComponentKind in the BOM);
# Standard (No BOM), Graphical and Net Tie (No BOM) parts only with "Include Standard
# (No BOM) Items".
OPTIONAL_PNP_KINDS = {2, 4, 5}
_SKIP_DIRS = {"history", "__previews", ".git", ".svn", "project logs"}
_DESIGN_SUFFIXES = {".prjpcb", ".schdoc", ".pcbdoc", ".pcbdwf", ".outjob", ".bomdoc",
                    ".harness", ".prjpcbstructure", ".annotation", ".schlib", ".pcblib",
                    ".intlib", ".dblib", ".svndblib", ".step", ".stp", ".pdf", ".png",
                    ".jpg", ".bmp", ".dxf", ".dwg", ".log", ".zip"}
_TEXT_SUFFIXES = {"", ".txt", ".csv", ".ipc", ".xml", ".cvg", ".drr", ".ldp", ".tsv",
                  ".d356", ".356", ".drl", ".xln", ".exc", ".nc", ".tx1", ".tx2", ".tx3",
                  ".tx4", ".tx5", ".tx6", ".tx7", ".tx8", ".tx9", ".pnp", ".pos", ".xy"}
_WORKBOOK_SUFFIXES = {".xlsx", ".xlsm", ".xls"}
_NOT_FITTED_WORDS = {"not fitted", "notfitted", "dnp", "dnf", "dni", "nf", "no fit",
                     "not placed", "do not populate", "do not fit", "do not place",
                     "not populated", "no"}
_GENERATION_NAME = re.compile(r"^[\d\s._\-:()]{6,}$")  # a date/time folder name
_FITTED_COLUMN = re.compile(r"(?i)\b(fitted|fit|dnp|populat\w*|place(?:d|ment)?|mount\w*)\b")
# ActiveBOM's supplier columns ('Supplier 1', 'Supplier Part Number 1', 'Supplier Order
# Qty 1' is neither).
_SUPPLIER_COLUMN = re.compile(r"^supplier ?(\d*)$")
_SKU_COLUMN = re.compile(r"^supplier (?:part ?(?:number|no\.?|#)|p/?n|sku|order code) ?(\d*)$")

FIXES = {
    "pick_place": "Generate the pick-and-place output again from the saved PcbDoc with the "
                  "variant the assembly uses, and send that file.",
    "bom": "Generate the BOM again (the OutJob's BOM output or Reports > Bill of Materials) "
           "with the variant the build uses, after the last design change.",
    "netlist": "Generate the netlist output again from the saved PcbDoc (Test Points / "
               "IPC-D-356A, or IPC-2581) after the last layout change.",
    "stackup": "Generate the output again after the last Layer Stack Manager change, and "
               "check the layer pairs (Design > Layer Stack Manager > Via Types).",
    "drill": "Generate the NC drill files again from the saved PcbDoc (Fabrication Outputs > "
             "NC Drill Files) after the last change to pads, vias or holes.",
    "staleness": "Generate the OutJob again after the last design change (Generate "
                 "Content or the Project Releaser), and archive or delete old outputs.",
    "missing": "Connect the output to its container and generate it (right-click the "
               "container > Generate Content), or pass path= to the folder or release zip "
               "that holds the files.",
}


# ---------------------------------------------------------------- the files

@dataclass(eq=False)  # one file found: compared by identity
class OutputFile:
    label: str                   # relative to the searched root ('a.zip/x/y.txt' in a zip)
    name: str                    # file name
    kind: str | None             # detect_output_kind, or 'odb'
    size: int
    mtime: float | None          # file time, or the zip member's
    group: str                   # the folder (or generation) it was found in
    path: str | None = None      # on disk; None inside a zip
    member_data: bytes | None = field(default=None, repr=False)
    outputs: list[dict] = field(default_factory=list)  # OutJob outputs whose folder holds it
    same_as: str | None = None   # an identical file checked instead
    _data: bytes | None = field(default=None, repr=False)

    def data(self) -> bytes:
        if self._data is None:
            if self.member_data is not None:
                self._data = self.member_data
            else:
                with open(self.path, "rb") as handle:
                    self._data = handle.read(MAX_FILE_BYTES + 1)
        return self._data

    @property
    def family(self) -> str | None:
        return FAMILY_OF_KIND.get(self.kind or "")


@dataclass
class Located:
    mode: str                                   # 'path' | 'project'
    root: str | None = None
    files: list[OutputFile] = field(default_factory=list)
    searched: list[str] = field(default_factory=list)
    outputs: list[dict] = field(default_factory=list)  # output_locations rows
    locations: dict = field(default_factory=dict)  # output_locations' model
    older_generations: Counter = field(default_factory=Counter)  # family -> skipped files
    generations_checked: list[dict] = field(default_factory=list)  # the newest, per family
    zips_not_opened: list[str] = field(default_factory=list)
    skipped: Counter = field(default_factory=Counter)  # reason -> files
    error: dict | None = None


def _relative(path: str, root: str) -> str:
    try:
        return os.path.relpath(path, root).replace("\\", "/")
    except ValueError:  # another drive
        return path.replace("\\", "/")


def _suffix(name: str) -> str:
    return os.path.splitext(name)[1].lower()


def _candidate(name: str) -> bool:
    suffix = _suffix(name)
    ext = suffix[1:]
    return (suffix in _TEXT_SUFFIXES or suffix in _WORKBOOK_SUFFIXES
            or ext in fo.GERBER_EXTENSIONS or bool(re.fullmatch(r"g[a-z]?\d{1,3}", ext))
            or name.lower() == "matrix")


def _classify(name: str, head: bytes, folder: str) -> str | None:
    if name.lower() == "matrix" and folder.lower().replace("\\", "/").endswith("matrix"):
        return "odb"
    if name.lower().endswith(".tgz") and "odb" in name.lower():
        return "odb"
    return fo.detect_output_kind(name, head)


def _walk(root: str, located: Located, depth: int = MAX_DEPTH):
    """(path, name) of the candidate files under root, sorted, design files and history
    folders left out; zips are listed in located.zips_not_opened by the caller."""
    count = 0
    base_depth = root.rstrip("\\/").count(os.sep)
    for folder, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d.lower() not in _SKIP_DIRS
                         and not d.startswith(".") and "onedrive" not in d.lower())
        if folder.count(os.sep) - base_depth >= depth:
            dirs[:] = []
        for name in sorted(files):
            count += 1
            if count > MAX_WALK:
                located.skipped["walk limit"] += 1
                return
            yield folder, name


def _file_entry(path: str, name: str, root: str, group: str) -> OutputFile | None:
    try:
        stat = os.stat(path)
    except OSError:
        return None
    kind = None
    if _candidate(name) or name.lower().endswith(".tgz"):
        ext = _suffix(name)[1:]
        if ext in fo.GERBER_EXTENSIONS:
            kind = "gerber"
        else:
            try:
                with open(path, "rb") as handle:
                    head = handle.read(HEAD_BYTES)
            except OSError:
                return None
            kind = _classify(name, head, os.path.dirname(path))
    if kind is None:
        return None
    return OutputFile(label=_relative(path, root), name=name, kind=kind, size=stat.st_size,
                      mtime=stat.st_mtime, group=group, path=path)


def _zip_entries(data: bytes, label: str, mtime: float | None, located: Located,
                 nested: bool = True) -> list[OutputFile]:
    """The output files in a zip (and in the zips it holds, one level), in memory."""
    entries: list[OutputFile] = []
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        located.skipped["unreadable zip"] += 1
        return entries
    with archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            member = info.filename.replace("\\", "/")
            name = member.rsplit("/", 1)[-1]
            folder = member.rsplit("/", 1)[0] if "/" in member else ""
            if name.lower().endswith(".zip"):
                if not nested:
                    located.skipped["zip inside a nested zip"] += 1
                    continue
                if info.file_size > MAX_FILE_BYTES:
                    located.skipped["too large"] += 1
                    continue
                try:
                    inner = archive.read(info)
                except (zipfile.BadZipFile, OSError, RuntimeError, NotImplementedError):
                    located.skipped["unreadable zip member"] += 1
                    continue
                entries.extend(_zip_entries(inner, f"{label}/{member}", mtime, located,
                                            nested=False))
                continue
            if not (_candidate(name) or name.lower().endswith(".tgz")):
                continue
            if info.file_size > MAX_FILE_BYTES:
                located.skipped["too large"] += 1
                continue
            if _suffix(name)[1:] in fo.GERBER_EXTENSIONS or name.lower().endswith(".tgz"):
                content = b""  # counted by its name, never read
                kind = "gerber" if not name.lower().endswith(".tgz") else _classify(
                    name, b"", folder)
            else:
                try:
                    content = archive.read(info)
                except (zipfile.BadZipFile, OSError, RuntimeError, NotImplementedError):
                    located.skipped["unreadable zip member"] += 1
                    continue
                kind = _classify(name, content[:HEAD_BYTES], folder)
            if kind is None:
                continue
            try:
                stamp = datetime(*info.date_time).timestamp()
            except (ValueError, OverflowError, OSError):
                stamp = mtime
            entries.append(OutputFile(
                label=f"{label}/{member}", name=name, kind=kind, size=info.file_size,
                mtime=stamp, group=f"{label}/{folder.split('/')[0]}",
                member_data=content))
    return entries


def locate_path(path: str, project_folder: str | None) -> Located:
    """The output files under an explicit folder, in a .zip or one file."""
    target = Path(path)
    if not target.is_absolute() and project_folder:
        target = Path(project_folder) / target
    located = Located(mode="path", root=str(target))
    if not target.exists():
        located.error = {"error": "path_not_found",
                         "message": f"No file or folder at {target}."}
        return located
    if target.is_file():
        located.searched.append(target.name)
        root = str(target.parent)
        if target.suffix.lower() == ".zip":
            if target.stat().st_size > MAX_FILE_BYTES:
                located.error = {"error": "path_too_large",
                                 "message": f"{target.name} is larger than "
                                            f"{MAX_FILE_BYTES >> 20} MB."}
                return located
            located.files = _zip_entries(target.read_bytes(), target.name,
                                         target.stat().st_mtime, located)
        else:
            entry = _file_entry(str(target), target.name, root, root)
            located.files = [entry] if entry else []
        return located
    root = str(target)
    located.searched.append(".")
    for folder, name in _walk(root, located):
        full = os.path.join(folder, name)
        if name.lower().endswith(".zip"):
            try:
                size = os.path.getsize(full)
                if size > MAX_FILE_BYTES:
                    located.skipped["too large"] += 1
                    continue
                with open(full, "rb") as handle:
                    data = handle.read()
            except OSError:
                continue
            located.files.extend(_zip_entries(data, _relative(full, root),
                                              os.path.getmtime(full), located))
            continue
        relative = os.path.relpath(folder, root)
        group = root if relative == "." else os.path.join(root, relative.split(os.sep)[0])
        entry = _file_entry(full, name, root, group)
        if entry is not None:
            located.files.append(entry)
    return located


def locate_project(project: dict) -> Located:
    """The output files the project's OutJobs generated (container folders; the newest
    generation of each kind under a date/time-named container's base) and those in its
    Output Path."""
    root = str(Path(project["prj_pcb_path"]).parent)
    located = Located(mode="project", root=root)
    model = output_locations(project)
    if "error" in model:
        located.error = model
        return located
    located.outputs = model["outputs"]
    located.locations = model
    seen: dict[str, OutputFile] = {}
    claimed: set[str] = set()  # every file walked once (an older generation's too)

    def norm(path: str) -> str:
        return os.path.normcase(os.path.abspath(path))

    # Folders as written (labels keep their case), each searched once.
    roots: dict[str, tuple[str, bool]] = {}
    for row in sorted(model["outputs"], key=lambda row: not row["exact"]):
        roots.setdefault(norm(row["folder"]), (os.path.abspath(row["folder"]), row["exact"]))
    roots.setdefault(norm(model["output_path"]), (os.path.abspath(model["output_path"]), False))
    project_folder = norm(root)
    for key_folder, (folder, is_exact) in roots.items():
        if key_folder == project_folder or not os.path.isdir(folder):
            continue
        located.searched.append(_relative(folder, root))
        groups: dict[str, list[OutputFile]] = defaultdict(list)
        for parent, name in _walk(folder, located):
            full = os.path.join(parent, name)
            key = norm(full)
            if key in claimed:
                continue
            claimed.add(key)
            if name.lower().endswith(".zip"):
                located.zips_not_opened.append(_relative(full, root))
                continue
            relative = os.path.relpath(full, folder)
            first = relative.split(os.sep)[0] if os.sep in relative else ""
            # Below a date/time container's base every first-level folder is a
            # generation; below a container folder only a timestamp folder is (its
            # Timestamp Folder option), the others are output sub-folders.
            if first and (not is_exact or _GENERATION_NAME.match(first)):
                group = os.path.join(folder, first)
            else:
                group = folder
            entry = _file_entry(full, name, root, group)
            if entry is not None:
                seen[key] = entry
                groups[group].append(entry)
        older, newest = _older_generations(groups)
        for entry in older:
            located.older_generations[entry.family] += 1
            del seen[norm(entry.path)]
        located.generations_checked.extend(
            {"kind": FAMILY_LABELS.get(family, family), "folder": _relative(group, root),
             "newest_by": how} for family, (group, how) in sorted(newest.items())
            if any(entry.family == family for entry in older))
    located.files = sorted(seen.values(), key=lambda entry: entry.label.lower())
    located.zips_not_opened = sorted(set(located.zips_not_opened))
    _attribute(located)
    return located


def _older_generations(groups: dict[str, list[OutputFile]]
                       ) -> tuple[list[OutputFile], dict[str, tuple[str, str]]]:
    """(the files of each family outside its newest generation (group), per family the
    newest (group, 'folder name' | 'file time')): each generation holds a full set, so only
    the newest is checked. Generations are ordered by the date and time their folder names
    hold (a copy or checkout resets file times), else by their newest file's time."""
    keys = _generation_keys([os.path.basename(group) for group in groups])
    candidates: dict[str, dict[str, float]] = defaultdict(dict)  # family -> group -> newest
    for group, entries in groups.items():
        for entry in entries:
            if entry.family:
                times = candidates[entry.family]
                times[group] = max(times.get(group, 0.0), entry.mtime or 0.0)
    newest: dict[str, tuple[str, str]] = {}
    for family, times in candidates.items():
        named = {group: keys.get(os.path.basename(group)) for group in times}
        if len(times) > 1 and all(named.values()):
            newest[family] = (max(times, key=lambda group: named[group]), "folder name")
        else:
            newest[family] = (max(times, key=lambda group: times[group]), "file time")
    older = [entry for group, entries in groups.items() for entry in entries
             if entry.family and newest[entry.family][0] != group]
    return older, newest


def _generation_keys(names: list[str]) -> dict[str, tuple[int, ...]]:
    """Sort keys of date/time folder names (one container's generations, in one format):
    year, month, day, then the time. The year is the first or the third number (four
    digits); with the year third, a value above 12 in any name tells which of the first two
    is the day. Names whose date cannot be read so get no key."""
    parsed: dict[str, list[str]] = {}
    for name in names:
        if not _GENERATION_NAME.match(name):
            continue
        numbers = re.findall(r"\d+", name)
        if numbers and len(numbers[0]) >= 8:  # 20261024[1030][_103000]
            digits = numbers[0]
            numbers = [digits[:4], digits[4:6], digits[6:8], *re.findall(r"\d\d", digits[8:]),
                       *numbers[1:]]
        if len(numbers) >= 3:
            parsed[name] = numbers
    keys: dict[str, tuple[int, ...]] = {}
    year_third = {name: numbers for name, numbers in parsed.items()
                  if len(numbers[0]) != 4 and len(numbers[2]) == 4}
    day_first = any(int(n[0]) > 12 for n in year_third.values())
    month_first = any(int(n[1]) > 12 for n in year_third.values())
    for name, numbers in parsed.items():
        values = [int(n) for n in numbers]
        if len(numbers[0]) == 4:
            keys[name] = tuple(values)
        elif name in year_third and day_first != month_first:
            day, month = (values[0], values[1]) if day_first else (values[1], values[0])
            keys[name] = (values[2], month, day, *values[3:])
    return keys


def _attribute(located: Located) -> None:
    """Match each file to the OutJob outputs whose folder holds it and whose type makes
    its kind: by [Output Name] in its path when several could, else all of them."""
    for entry in located.files:
        if entry.path is None or entry.family is None:
            continue
        here = os.path.normcase(os.path.abspath(entry.path))
        candidates = []
        for row in located.outputs:
            folder = os.path.normcase(os.path.abspath(row["folder"]))
            if FAMILY_OF_TYPE.get(row["type"]) == entry.family and \
                    here.startswith(folder.rstrip("\\/") + os.sep):
                candidates.append(row)
        if len(candidates) > 1:
            named = [row for row in candidates
                     if row["output"] and row["output"].lower() in entry.label.lower()]
            candidates = named or candidates
        entry.outputs = candidates


# ---------------------------------------------------------------- the design

_GERBER_LAYER = re.compile(r"^g(tl|bl|\d+|p\d+)$", re.IGNORECASE)


def gerber_layer_v6(word: str | None) -> int | None:
    """The v6 copper layer an Altium Gerber extension names (gtl top, gbl bottom, gN mid
    layer N, gpN internal plane N): the .LDP layer-pairs file lists a drill file's layers
    so (every .LDP sampled)."""
    match = _GERBER_LAYER.match((word or "").strip())
    if not match:
        return None
    code = match.group(1).lower()
    if code == "tl":
        return 1
    if code == "bl":
        return 32
    if code.startswith("p"):
        return 38 + int(code[1:])
    return 1 + int(code)


@dataclass
class PartFacts:
    component: int
    designator: str              # on the PCB (what the outputs write)
    name: str                    # the netlist name variants use (else the designator)
    side: str
    kind: int
    footprint: str | None
    comment: str | None
    rotation: float
    centre: tuple[float, float]  # Center-X/Y relative to the board origin
    padless: bool
    ref: tuple[float, float]     # Ref-X/Y
    pad1: tuple[float, float] | None  # the pad named 1


class Design:
    """The saved design the outputs are checked against: the PcbDoc's parts, pads,
    holes and stack (index may be None: the BOM check still runs) and the variants'
    BOM parts (from the netlist; without one, the PCB's parts approximate them)."""

    def __init__(self, project: dict, index, netlist: dict | None, variant_state):
        self.project = project
        self.index = index
        self.netlist = netlist if netlist and netlist.get("components") else None
        self.variant_state = variant_state
        self.parts: list[PartFacts] = []
        self.by_designator: dict[str, PartFacts] = {}
        self.duplicates: set[str] = set()
        self.origin = (0.0, 0.0)
        self._bom: dict[str, tuple] = {}
        self._plans: dict[str, object] = {}
        self._active: dict[tuple, tuple] = {}
        # The project's OutJob outputs (output_jobs.output_locations rows) and .BomDocs.
        self.outputs: list[dict] = []
        self.bomdocs: list[str] = []
        if index is None:
            return
        board = index.pcb.board
        self.origin = (board.origin_x or 0.0, board.origin_y or 0.0)
        ox, oy = self.origin
        pcb = index.pcb
        centroids = dfm_assembly.centroids(index)
        for part, centroid in zip(dfm_assembly.parts(index), centroids):
            if not part.designator:
                continue
            pad1 = None
            for row in part.pads:
                pad = pcb.primitives[row - 1]
                if (pad.pad_name or "").strip() == "1":
                    pad1 = (pad.x1 - ox, pad.y1 - oy)
                    break
            centre = centroid.centre
            facts = PartFacts(
                component=part.component, designator=part.designator,
                name=part.name or part.designator, side=part.side, kind=part.kind,
                footprint=part.footprint, comment=part.comment,
                rotation=part.rotation or 0.0,
                centre=(centre[0] - ox, centre[1] - oy) if centre else (-ox, -oy),
                padless=centre is None,
                ref=(centroid.reference[0] - ox, centroid.reference[1] - oy), pad1=pad1)
            key = facts.designator.upper()
            if key in self.by_designator:
                self.duplicates.add(key)
            self.by_designator.setdefault(key, facts)
            self.parts.append(facts)

    # ---------- variants ----------

    def variant(self, name: str | None):
        """(VariantDefinition or None for the base design, known)."""
        if name is None or name.strip().lower() in ("", NO_VARIATIONS.lower(),
                                                     "no variations", "[all variants]"):
            return None, True
        found = self.variant_state.get(name.strip()) if self.variant_state else None
        return found, found is not None

    def variant_names(self) -> list[str]:
        return [v.name for v in (self.variant_state.variants if self.variant_state else [])]

    def bom_parts(self, name: str | None) -> tuple[list, set[str], str]:
        """(BOM parts of the variant: get_bom's (fitted flag set), not fitted names,
        basis 'netlist' | 'pcb')."""
        key = (name or BASE_VARIANT).lower()
        if key not in self._bom:
            variant, _known = self.variant(name)
            if self.netlist is not None:
                plan = plan_variant(variant, self.netlist, variant_names(self.netlist))
                parts, _ = build_parts(self.netlist, plan, self.project.get("parameters") or {})
                self._bom[key] = (parts, set(plan.not_fitted), "netlist")
                self._plans[key] = plan
            else:
                dnp = set(variant.dnp_refdes) if variant is not None else set()
                parts = [_PcbBomPart(part, part.name not in dnp and part.designator not in dnp)
                         for part in self.parts if part.kind in BOM_KINDS]
                parts.sort(key=lambda part: natural_key(part.designator))
                self._bom[key] = (parts, dnp, "pcb")
        return self._bom[key]

    def active_bom(self, path: str, name: str | None):
        """(ActiveBom model, why not) of a BomDoc for the variant's BOM parts; only with
        the netlist's parts (the PCB's carry no Design Item ID)."""
        parts, _names, basis = self.bom_parts(name)
        if basis != "netlist":
            return None, "no netlist with schematic parts: the BomDoc's lines cannot be joined"
        key = (path, (name or BASE_VARIANT).lower())
        if key not in self._active:
            try:
                doc = active_bom.load_bom_doc(path)
                plan = self._plans.get((name or BASE_VARIANT).lower())
                self._active[key] = (active_bom.build_active_bom(
                    doc, parts, self.netlist, plan, getattr(plan, "name", "")), None)
            except Exception as error:  # a bad BomDoc leaves the BOM checked without it
                self._active[key] = (None, f"{Path(path).name}: {type(error).__name__}: "
                                           f"{error}")
        return self._active[key]

    def not_fitted_designators(self, name: str | None) -> set[str]:
        """PCB designators (upper case) of the parts the variant does not fit."""
        _parts, names, _basis = self.bom_parts(name)
        lowered = {n.upper() for n in names}
        return {part.designator.upper() for part in self.parts
                if part.name.upper() in lowered or part.designator.upper() in lowered}

    def not_fitted_anywhere(self) -> set[str]:
        """PCB designators (upper case) of the parts some variant does not fit."""
        found: set[str] = set()
        for name in self.variant_names():
            found |= self.not_fitted_designators(name)
        return found

    def board_names(self) -> list[str]:
        """The file names of the project's PcbDocs (the first is the one checked)."""
        return [Path(path).name for path in self.project.get("pcb_doc_paths") or []]

    # ---------- copper ----------

    def copper_layers(self) -> list[str]:
        """Copper layer names in stack order (the order hole spans count in)."""
        if self.index is None:
            return []
        model = dfm_geometry.copper_model(self.index)
        return [self.index.layer_name(layer) for layer in model.order]

    def layer_position(self, token: str | None) -> int | None:
        """The stack position (copper_layers' index) of a layer: a Gerber extension (as
        .LDP files name layers), a Board6 token (TOP, MID1, PLANE2) or a layer name; None
        for none."""
        if self.index is None or not token:
            return None
        order = dfm_geometry.copper_model(self.index).order
        layer = gerber_layer_v6(token) or layer_token_v6(self.index, token)
        if layer in order:
            return order.index(layer)
        folded = _fold(token)
        for number, name in enumerate(self.copper_layers()):
            if _fold(name) == folded:
                return number
        return None

    def layer_span(self, layers: list[str | None]) -> tuple[int, int] | None:
        """(top, bottom) stack positions a layer pair spans: the outermost of the layers it
        names (an .LDP lists them in layer-ID order, which need not be the stack order);
        None when it names none or one the stack lacks."""
        positions = [self.layer_position(layer) for layer in layers]
        if not positions or None in positions:
            return None
        return min(positions), max(positions)

    def holes(self) -> list:
        return dfm_geometry.holes(self.index) if self.index is not None else []

    def pins(self) -> dict[tuple[str, str], dict]:
        """(designator, pad name) -> {net, at (relative), designator, pad} of every
        component pad and bare hole; a pin drawn as several pads keeps its first."""
        if self.index is None:
            return {}
        pcb = self.index.pcb
        ox, oy = self.origin
        pins: dict[tuple[str, str], dict] = {}
        nets = pcb.nets

        def add(component, pad_name, net, x, y):
            if component is None or not (0 <= component < len(pcb.components)):
                return
            designator = self.index.designator(component)
            if not designator:
                return
            key = (designator.upper(), (pad_name or "").strip().upper())
            name = nets[net] if net is not None and 0 <= net < len(nets) else None
            pin = pins.get(key)
            if pin is None:
                pins[key] = {"designator": designator, "pad": (pad_name or "").strip(),
                             "net": name, "at": (x - ox, y - oy)}
            elif pin["net"] is None and name:
                pin["net"] = name
        for primitive in pcb.primitives:
            if primitive.kind == "pad" and primitive.pour_id is None:
                add(primitive.component, primitive.pad_name, primitive.net, primitive.x1,
                    primitive.y1)
        for hole in getattr(pcb, "bare_holes", None) or ():
            add(hole.component, hole.pad_name, hole.net, hole.x, hole.y)
        return pins


@dataclass
class _PcbBomPart:
    """A PCB part standing in for a BOM part without a netlist."""

    part: PartFacts
    fitted: bool

    @property
    def designator(self) -> str:
        return self.part.designator

    @property
    def comment(self):
        return self.part.comment

    @property
    def footprint(self):
        return self.part.footprint

    description = value = mpn = manufacturer = mpn_placeholder = None


# ---------------------------------------------------------------- findings

class Findings:
    def __init__(self):
        self.rows: list[dict] = []

    def add(self, check: str, code: str, severity: str, message: str, file: str | None,
            examples: list[dict] | None = None, count: int | None = None, **fields) -> None:
        examples = examples or []
        row = {"check": check, "code": code, "severity": severity,
               "count": count if count is not None else max(1, len(examples))}
        if file:
            row["file"] = file
        row["message"] = message
        row.update(fields)
        if examples:
            row["examples"] = examples[:EXAMPLES_KEPT]
            if len(examples) > EXAMPLES_KEPT:
                row["examples_total"] = len(examples)
        self.rows.append(row)


def _fold(text) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().casefold()


def _footprint(text) -> str:
    """A footprint name compared: Altium's pick-and-place writes a space in the name as
    '_' (every such row of the real files sampled)."""
    return _fold(str(text or "").replace("_", " "))


def _point(xy) -> list[float]:
    return [round(xy[0], 3), round(xy[1], 3)]


def _angle(a: float, b: float) -> float:
    """The smallest difference of two angles in degrees."""
    diff = (a - b) % 360.0
    return min(diff, 360.0 - diff)


@dataclass
class Tolerances:
    position_mil: float = DEFAULT_POSITION_TOLERANCE_MIL
    rotation_deg: float = DEFAULT_ROTATION_TOLERANCE_DEG
    drill_mil: float = DEFAULT_DRILL_TOLERANCE_MIL
    thickness_percent: float = DEFAULT_THICKNESS_TOLERANCE_PERCENT

    def echo(self) -> dict:
        return {"position_mil": round(self.position_mil, 4),
                "rotation_deg": self.rotation_deg, "drill_mil": round(self.drill_mil, 4),
                "thickness_percent": self.thickness_percent}


# ---------------------------------------------------------------- pick and place

def _same_variant(a: str | None, b: str | None) -> bool:
    def norm(name):
        name = (name or "").strip().lower()
        return "" if name in ("", NO_VARIATIONS.lower(), "no variations", "[all variants]") \
            else name
    return norm(a) == norm(b)


def board_named(text: str | None) -> str | None:
    """The PcbDoc file name in a .DRR report's or .LDP file's first line (the report writes
    the bare name, the layer-pairs file the full path), or None."""
    match = re.search(r"([^\\/]*?\.pcbdoc)(?![\w.])", text or "", re.IGNORECASE)
    return match.group(1).strip() if match else None


def _other_board(design: Design, named: str | None) -> tuple[str, str] | None:
    """(severity, message) when a file names another PcbDoc than the one checked: info for
    another of the project's PcbDocs, a warning for a board the project does not list;
    None when it names the checked one, or none."""
    boards = design.board_names()
    if not named or not boards or named.lower() == boards[0].lower():
        return None
    if named.lower() in (name.lower() for name in boards[1:]):
        return "info", (f"Drill reports or layer-pairs files for the project's other PcbDoc "
                        f"'{named}': only '{boards[0]}' is checked, so their drill files were "
                        "not compared.")
    return "warning", (f"Drill reports or layer-pairs files name the board '{named}', not the "
                       f"project's '{boards[0]}' (another board, or this one under an older "
                       "name): their drill files were not compared.")


def _variant_label(definition, known: bool, name: str | None) -> str:
    if definition is not None:
        return definition.name
    return NO_VARIATIONS if known else str(name)


def _file_variant(design: Design, stated: str | None, override: str | None
                  ) -> tuple[str | None, str, str | None]:
    """(variant to check against, where from, note). stated is the file's own."""
    if override is not None:
        note = None
        if stated is not None and stated.strip().lower() != override.strip().lower():
            note = f"the file says variant '{stated}'; checked against '{override}' as asked"
        return override, "argument", note
    if stated is not None:
        return stated, "file", None
    return None, "file", None


def check_pick_place(entry: OutputFile, parsed: fo.PickPlace, design: Design,
                     variant_arg: str | None, tol: Tolerances, findings: Findings) -> dict:
    stats: dict = {"rows": len(parsed.rows)}
    variant, source, note = _file_variant(design, parsed.variant, variant_arg)
    definition, known = design.variant(variant)
    stats["variant"] = _variant_label(definition, known, variant)
    stats["variant_from"] = source
    if note:
        stats["note"] = note
    label = entry.label
    outjob_variants = sorted({row["variant"] for row in entry.outputs if row.get("variant")})
    stated = parsed.variant or NO_VARIATIONS
    if outjob_variants and not any(_same_variant(stated, name) for name in outjob_variants):
        findings.add("pick_place", "variant_differs_from_outjob", "info",
                     f"The file says variant '{stated}', the OutJob output in its folder is "
                     f"set to '{outjob_variants[0]}': it was generated with other settings.",
                     label)
    if not known:
        findings.add("pick_place", "variant_unknown", "warning",
                     f"The file's variant '{variant}' is not one of the project's variants "
                     f"({', '.join(design.variant_names()) or 'none'}): Not Fitted parts are "
                     "not checked, and a part some variant does not fit is not required.",
                     label)
    not_fitted = design.not_fitted_designators(variant) if known else set()
    # With the variant unknown, a part some variant leaves out may rightly be absent.
    optional = design.not_fitted_anywhere() if not known else set()
    rows_by: dict[str, list] = defaultdict(list)
    for row in parsed.rows:
        rows_by[row.designator.strip().upper()].append(row)
    groups: dict[str, list[dict]] = defaultdict(list)
    point = parsed.point or "center"
    flipped = 0
    rounded = 0
    for key, rows in sorted(rows_by.items(), key=lambda item: natural_key(item[0])):
        row = rows[0]
        if len(rows) > 1:
            groups["duplicate"].append({"designator": row.designator, "rows": len(rows),
                                        "lines": [r.line for r in rows][:5]})
        part = design.by_designator.get(key)
        if part is None:
            groups["extra"].append({"designator": row.designator, "line": row.line})
            continue
        if key in not_fitted:
            groups["dnp_present"].append({"designator": row.designator, "line": row.line})
        if row.side and row.side != part.side:
            groups["side"].append({"designator": row.designator, "file": row.side,
                                   "design": part.side})
        if row.rotation is not None:
            diff = _angle(row.rotation, part.rotation)
            if diff > tol.rotation_deg + 1e-9:
                if float(row.rotation).is_integer() and diff <= 0.5 + 1e-9:
                    rounded += 1
                else:
                    groups["rotation"].append({"designator": row.designator,
                                               "file": row.rotation,
                                               "design": round(part.rotation, 3)})
        expected = {"center": part.centre, "ref": part.ref, "pad": part.pad1}
        for name, (x, y) in row.points.items():
            want = expected.get(name)
            if want is None:
                continue
            off = math.hypot(x - want[0], y - want[1])
            if off <= tol.position_mil:
                continue
            if name in ("ref", "pad") and part.side == "bottom" and \
                    math.hypot(x - want[0], y + want[1]) <= tol.position_mil:
                flipped += 1
                continue
            example = {"designator": row.designator, "point": name,
                       "file_at_mil": _point((x, y)), "design_at_mil": _point(want),
                       "off_mil": round(off, 3)}
            if name == "center" and part.padless:
                example["note"] = "no pads: Altium writes the absolute origin"
            groups["position"].append(example)
        if row.footprint and part.footprint and (
                _footprint(row.footprint) != _footprint(part.footprint)):
            groups["footprint"].append({"designator": row.designator, "file": row.footprint,
                                        "design": part.footprint})
    stats["rows_checked"] = len(rows_by)
    if flipped:
        stats["y_flipped_bottom_rows"] = flipped
    if rounded:
        stats["rotation_whole_degrees"] = rounded
    # Comment and Description against the variant's BOM part (variations applied).
    parts, _names, basis = design.bom_parts(variant if known else None)
    by_name = {part.designator.upper(): part for part in parts}
    for key, rows in rows_by.items():
        part = design.by_designator.get(key)
        bom_part = by_name.get(part.name.upper()) if part is not None else None
        if bom_part is None:
            continue
        row = rows[0]
        for field_name in ("comment", "description"):
            text = getattr(row, field_name)
            want = getattr(bom_part, field_name, None)
            if text and want is not None and _fold(text) != _fold(want):
                groups[field_name].append({"designator": row.designator, "file": text,
                                           "design": want})
    missing = []
    absent_optional = 0
    for part in design.parts:
        key = part.designator.upper()
        if key in rows_by or key in not_fitted or part.kind in OPTIONAL_PNP_KINDS:
            continue
        if key in optional:
            absent_optional += 1
            continue
        missing.append({"designator": part.designator, "side": part.side,
                        "at_mil": _point(part.centre)})
    missing.sort(key=lambda example: natural_key(example["designator"]))
    stats["expected"] = sum(1 for part in design.parts
                            if part.designator.upper() not in not_fitted | optional
                            and part.kind not in OPTIONAL_PNP_KINDS)
    if absent_optional:
        stats["absent_not_fitted_in_a_variant"] = absent_optional
    messages = {
        "missing": ("warning", "Fitted parts of the variant missing from the file."),
        "dnp_present": ("warning", "Parts the variant does not fit are listed: they would "
                                   "be placed."),
        "extra": ("warning", "Designators in the file that are not on the board."),
        "duplicate": ("warning", "Designators listed more than once."),
        "side": ("warning", "Parts on the other side than on the board."),
        "rotation": ("warning", "Rotation differs from the board's (beyond the "
                                "rotation tolerance, mod 360)."),
        "position": ("warning", f"Position differs from the board's beyond the position "
                                f"tolerance ({point} point, relative to the board origin)."),
        "footprint": ("warning", "Footprint differs from the board's."),
        "comment": ("info", f"Comment differs from the part's ({basis} data, variant "
                            "variations applied)."),
        "description": ("info", "Description differs from the part's."),
    }
    groups["missing"] = missing
    # Most rows off the board's layout: the file is from another revision (or the parts
    # moved since); say so once before the rows.
    moved = {example["designator"] for code in ("position", "rotation", "side")
             for example in groups.get(code, [])}
    on_board = sum(1 for key in rows_by if key in design.by_designator)
    if on_board >= 5 and len(moved) > on_board / 2:
        findings.add("pick_place", "layout_differs", "warning",
                     f"{len(moved)} of {on_board} parts differ in position, rotation or side: "
                     "the file is from another revision of the board, or the parts moved "
                     "since it was generated.", label, count=len(moved))
    for code, (severity, message) in messages.items():
        if groups.get(code):
            findings.add("pick_place", code, severity, message, label, groups[code])
    stats["mismatches"] = sum(len(groups[code]) for code in messages
                              if messages[code][0] == "warning" and groups.get(code))
    return stats


# ---------------------------------------------------------------- BOM

def _bom_variant(design: Design, bom: fo.Bom, entry: OutputFile, override: str | None
                 ) -> tuple[str | None, str, str | None]:
    """(variant, from, the OutJob output's variant when the file fits another exactly) of
    a BOM file: the argument, its title block, its OutJob output (unless the fitted lines
    are exactly another variant's parts while not the output's), a variant named in its
    path, else the variant whose parts fit it best."""
    if override is not None:
        return override, "argument", None
    for key, value in bom.title.items():
        if "variant" in key.lower() and value.strip():
            return value.strip(), "title", None
    listed = {d.upper() for line in bom.lines if not _not_fitted_line(line)
              for d in line.designators}
    distance: dict[str | None, int] = {}
    for name in [None, *design.variant_names()]:
        parts, _names, _basis = design.bom_parts(name)
        distance[name] = len({part.designator.upper() for part in parts if part.fitted}
                             ^ listed)
    active = design.variant_state.active.name if design.variant_state and \
        design.variant_state.variants else None
    best = min(distance, key=lambda name: (distance[name], 0 if name == active else
                                           1 if name is None else 2))
    variants = {row["variant"] for row in entry.outputs if row.get("variant")}
    if len(variants) == 1:
        stated = next(iter(variants))
        definition, known = design.variant(stated)
        mine = definition.name if definition is not None else None
        if known and distance[mine] and distance[best] == 0:
            return best, "best_match", stated
        return stated, "outjob", None
    names = sorted(design.variant_names(), key=len, reverse=True)
    label = entry.label.lower()
    for name in names:
        if len(name) >= 3 and name.lower() in label:
            return name, "file_name", None
    return best, "best_match", None


def _bom_value(part, role: str, header: str, fallback: dict[str, str]):
    """What the part shows in a BOM column: the parameter of the column's name when the
    part has one ('Manufacturer 1' shows that parameter, not 'Manufacturer'; a special
    string such as '=Value' resolved as get_bom resolves it, fallback the project's
    parameters), else the role's value as get_bom reads it."""
    if role in ("manufacturer", "mpn", "value"):
        wanted = re.sub(r"^manufacture\b", "manufacturer", norm_name(header))
        parameters = getattr(part, "parameters", None) or {}
        for name, value in parameters.items():
            if norm_name(name) == wanted:
                text, _resolved = resolve(value, parameters, fallback)
                return None if text is None or text.strip() in ("", "*") else text.strip()
    want = getattr(part, role, None)
    if role == "mpn" and not want:
        want = getattr(part, "mpn_placeholder", None)
    return want


def _from_bomdoc(design: Design, entry: OutputFile) -> bool:
    """Whether the BOM comes from an ActiveBOM document (.BomDoc): its OutJob output's data
    source, else (a file no output's folder holds) every BOM output of the project's."""
    rows = entry.outputs or [row for row in design.outputs
                             if FAMILY_OF_TYPE.get(row["type"]) == "bom"]
    return bool(rows) and all(
        (row.get("document") or "").lower().endswith(".bomdoc")
        or "activebom" in (row.get("document") or "").lower() for row in rows)


def _bom_doc_of(design: Design, entry: OutputFile) -> str | None:
    """The BomDoc a BOM came from: the one its OutJob output names, else the project's
    first (an output naming '[ActiveBOM Document]' uses the project's)."""
    if not design.bomdocs:
        return None
    for row in entry.outputs or []:
        named = (row.get("document") or "").replace("\\", "/").rsplit("/", 1)[-1].lower()
        for path in design.bomdocs:
            if Path(path).name.lower() == named:
                return path
    return design.bomdocs[0]


def _supply_columns(bom: fo.Bom) -> dict[str, str]:
    """The lowest-numbered Supplier and Supplier Part Number columns of a BOM (header as
    written)."""
    found: dict[str, tuple[int, str]] = {}
    for header in bom.columns:
        name = norm_name(header)
        for role, pattern in (("sku", _SKU_COLUMN), ("supplier", _SUPPLIER_COLUMN)):
            match = pattern.match(name)
            if match:
                rank = int(match[1] or 0)
                if role not in found or rank < found[role][0]:
                    found[role] = (rank, header)
                break
    return {role: header for role, (_rank, header) in found.items()}


def _not_fitted_line(line: fo.BomLine) -> bool:
    if line.quantity == 0 and line.designators:
        return True
    for header, value in line.fields.items():
        if _FITTED_COLUMN.search(header) and _fold(value) in _NOT_FITTED_WORDS:
            return True
    return False


def check_bom(entry: OutputFile, bom: fo.Bom, design: Design, variant_arg: str | None,
              findings: Findings) -> dict:
    label = entry.label
    variant, source, outjob_variant = _bom_variant(design, bom, entry, variant_arg)
    definition, known = design.variant(variant)
    stats: dict = {"lines": len(bom.lines),
                   "variant": _variant_label(definition, known, variant),
                   "variant_from": source}
    if outjob_variant is not None:
        findings.add("bom", "variant_differs_from_outjob", "info",
                     f"The BOM's fitted parts are exactly those of variant "
                     f"'{stats['variant']}', not of '{outjob_variant}' that the OutJob output "
                     "in its folder is set to: it was generated with other settings (checked "
                     f"against '{stats['variant']}').", label)
    if not known:
        findings.add("bom", "variant_unknown", "warning",
                     f"The BOM's variant '{variant}' is not one of the project's variants: "
                     "checked against the design with no variations.", label)
    parts, _names, basis = design.bom_parts(variant if known else None)
    stats["basis"] = basis
    fallback = {name.lower(): value
                for name, value in (design.project.get("parameters") or {}).items()}
    bomdoc = _from_bomdoc(design, entry)
    expected: dict[str, dict] | None = None
    if bomdoc:
        path = _bom_doc_of(design, entry)
        model, why = design.active_bom(path, variant if known else None) if path else (
            None, "the project lists no .BomDoc")
        if model is None:
            stats["part_choices_not_read"] = True
            if why:
                stats["bom_doc_not_read"] = why
        else:
            stats["bom_doc"] = Path(path).name
            expected = active_bom.expected_columns(model)
    supply = _supply_columns(bom) if expected is not None else {}
    compared_rows: set[tuple[int, str]] = set()
    by_designator = {part.designator.upper(): part for part in parts}
    fitted = {key for key, part in by_designator.items() if part.fitted}
    groups: dict[str, list[dict]] = defaultdict(list)
    listed: dict[str, int] = {}
    dnp_listed: set[str] = set()
    for line in bom.lines:
        keys = [d.upper() for d in line.designators]
        if _not_fitted_line(line):
            dnp_listed.update(keys)
            continue
        if line.quantity is not None and line.designators and \
                abs(line.quantity - len(line.designators)) > 1e-9:
            groups["quantity"].append({"row": line.row, "quantity": line.quantity,
                                       "designators": len(line.designators),
                                       "first": line.designators[0]})
        elif line.quantity and not line.designators:
            groups["no_designators"].append({"row": line.row, "quantity": line.quantity})
        for designator, key in zip(line.designators, keys):
            if key in listed:
                groups["duplicate"].append({"designator": designator, "rows": [listed[key],
                                                                                line.row]})
                continue
            listed[key] = line.row
            part = by_designator.get(key)
            if part is None:
                groups["extra"].append({"designator": designator, "row": line.row})
                continue
            if not part.fitted:
                groups["dnp_listed"].append({"designator": designator, "row": line.row})
                continue
            for role in ("comment", "description", "value", "footprint", "manufacturer",
                         "mpn"):
                if role not in bom.roles:
                    continue
                if basis == "pcb" and role not in ("comment", "footprint"):
                    continue
                text = getattr(line, role) or ""
                want = _bom_value(part, role, bom.columns[bom.roles[role]], fallback)
                chosen = (expected or {}).get(key)
                from_choices = bool(chosen) and chosen["source"] in (
                    "part_choices", "solution_record")
                if from_choices and role in ("mpn", "manufacturer"):
                    want = chosen[role]
                    if role == "manufacturer" and active_bom._key(text) in chosen["aliases"]:
                        continue
                code = f"field_{role}"
                if expected is not None and role == "mpn" and not from_choices:
                    # A line without BomDoc part choices: ActiveBOM shows the parts' MPN
                    # parameter on most rows, not all (unverified, info).
                    code = "field_mpn_parameter"
                if _fold(text) != _fold(want):
                    groups[code].append({"designator": designator,
                                         "row": line.row, "file": text or None,
                                         "design": want or None})
            if expected is not None and key in expected:
                _compare_supply(line, bom, expected[key], supply, designator, groups,
                                compared_rows)
    missing = sorted((key for key in fitted if key not in listed),
                     key=natural_key)
    groups["missing"] = [{"designator": by_designator[key].designator} for key in missing]
    if dnp_listed:
        not_fitted = {key for key, part in by_designator.items() if not part.fitted}
        stats["not_fitted_lines"] = len(dnp_listed)
        wrong = sorted(dnp_listed - not_fitted, key=natural_key)
        groups["not_fitted_differs"] = [
            {"designator": key, "design": "fitted" if key in fitted else "not in the design"}
            for key in wrong]
    messages = {
        "missing": ("warning", "Fitted parts of the variant missing from the BOM."),
        "dnp_listed": ("warning", "Parts the variant does not fit are listed as fitted."),
        "extra": ("warning", "Designators not in the design's BOM (not a part, or a "
                             "Graphical, Net Tie or Standard (No BOM) part)."),
        "duplicate": ("warning", "Designators listed on more than one line."),
        "quantity": ("warning", "Quantity differs from the number of designators on the line."),
        "no_designators": ("info", "Lines with a quantity and no designators (added by hand "
                                   "or from a BomDoc): not checked."),
        "not_fitted_differs": ("warning", "Lines marked Not Fitted list parts the variant "
                                          "fits."),
        "field_comment": ("warning", "Comment differs from the part's (variant variations "
                                     "applied)."),
        "field_description": ("info", "Description differs from the part's (a BOM can "
                                      "take it from its part choices)."),
        "field_value": ("warning", "Value differs from the part's."),
        "field_footprint": ("warning", "Footprint differs from the part's."),
        "field_manufacturer": ("info", "Manufacturer name differs from the part's (supplier "
                                       "data spells makers' names its own way)."),
        "field_mpn": ("warning", "Manufacturer part number differs from the part's."),
    }
    if bomdoc and expected is None:
        # ActiveBOM fills these columns from the BomDoc's part choices, which could not be
        # read: the part's parameters are no reference for them.
        messages["field_mpn"] = (
            "info", "Manufacturer part number differs from the part's parameters. The BOM "
                    "comes from an ActiveBOM document (.BomDoc), whose part choices could not "
                    "be read: unverified.")
    elif expected is not None:
        messages["field_mpn"] = (
            "warning", "Manufacturer part number differs from the BomDoc's chosen solution.")
        messages["field_mpn_parameter"] = (
            "info", "Manufacturer part number differs from the parts' MPN parameter on a line "
                    "without part choices in the BomDoc (ActiveBOM shows that parameter on "
                    "most such rows, not all): unverified.")
        messages["field_manufacturer"] = (
            "info", "Manufacturer differs from the BomDoc's chosen solution and its aliases.")
        messages["field_supplier"] = (
            "warning", "Supplier is none of the BomDoc's suppliers for the line's chosen "
                       "solution.")
        messages["field_supplier_part_number"] = (
            "warning", "Supplier part number is none of the BomDoc's part choices for the "
                       "line's chosen solution.")
        duplicated = any(entry.get("line_numbers_duplicated") for entry in expected.values())
        messages["line_number"] = (
            "info", "Line # differs from the BomDoc's line number (empty for a line the BomDoc "
                    "has not numbered). The BomDoc gives some line numbers to more than one "
                    "line, so an export may number its lines otherwise.") if duplicated else (
            "warning", "Line # differs from the BomDoc's line number (empty for a line the "
                       "BomDoc has not numbered).")
    for code, (severity, message) in messages.items():
        if groups.get(code):
            findings.add("bom", code, severity, message, label, groups[code])
    stats["designators"] = len(listed)
    stats["expected"] = len(fitted)
    stats["mismatches"] = sum(len(groups[code]) for code in messages
                              if messages[code][0] == "warning" and groups.get(code))
    return stats


def _compare_supply(line: fo.BomLine, bom: fo.Bom, chosen: dict, supply: dict[str, str],
                    designator: str, groups: dict, compared: set) -> None:
    """An ActiveBOM-filled row's Line #, Supplier and Supplier Part Number cells against
    the BomDoc (each cell once per row)."""
    if "line" in bom.roles and (line.row, "line") not in compared:
        compared.add((line.row, "line"))
        text = (line.fields.get(bom.columns[bom.roles["line"]]) or "").strip()
        want = "" if chosen["line"] is None else str(chosen["line"])
        if text != want and not (text.endswith(".0") and text[:-2] == want):
            groups["line_number"].append({"designator": designator, "row": line.row,
                                          "file": text or None, "design": chosen["line"]})
    for role, column, known, code in (
            ("supplier", supply.get("supplier"), chosen["suppliers"], "field_supplier"),
            ("sku", supply.get("sku"), chosen["skus"], "field_supplier_part_number")):
        if not column or (line.row, role) in compared or not known:
            continue
        compared.add((line.row, role))
        text = (line.fields.get(column) or "").strip()
        if text and active_bom._key(text) not in known:
            groups[code].append({"designator": designator, "row": line.row, "file": text,
                                 "design": sorted(known.values())[:5]})


# ---------------------------------------------------------------- netlists

def compare_netlist(check: str, label: str, out_pins: dict, design: Design,
                    findings: Findings, cut: tuple[int, int, int] | None = None) -> dict:
    """out_pins: (designator, pin) upper -> {net (None: unconnected), at (or None)}. cut:
    the format's (designator, pin, net) lengths, None for no limit."""
    design_pins = design.pins()

    def key_of(designator: str, pad: str) -> tuple[str, str]:
        if cut:
            return designator[:cut[0]], pad[:cut[1]]
        return designator, pad

    keyed: dict[tuple[str, str], dict] = {}
    ambiguous = set()
    for (designator, pad), pin in design_pins.items():
        key = key_of(designator, pad)
        if key in keyed and keyed[key] is not pin:
            ambiguous.add(key)
        keyed[key] = pin

    def same_name(out_net: str, design_net: str) -> bool:
        if out_net.upper() == design_net.upper():
            return True
        return bool(cut) and len(design_net) > cut[2] and \
            out_net.upper() == design_net[:cut[2]].upper()

    groups: dict[str, list[dict]] = defaultdict(list)
    design_of_out: dict[str, Counter] = defaultdict(Counter)
    out_of_design: dict[str, Counter] = defaultdict(Counter)
    # (design net, file net) -> the pins on both: where a split or merge shows.
    pins_on: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for key, pin in sorted(out_pins.items(), key=lambda item: natural_key(item[0][0])):
        if key in ambiguous:
            continue
        mine = keyed.get(key)
        if mine is None:
            groups["pins_extra"].append({"designator": key[0], "pin": key[1],
                                         "net": pin["net"], "at_mil": pin.get("at")})
            continue
        if pin["net"] is None and mine["net"] is None:
            continue
        if pin["net"] is None:
            groups["pin_unconnected"].append({"designator": mine["designator"],
                                              "pin": mine["pad"], "design_net": mine["net"],
                                              "at_mil": _point(mine["at"])})
            continue
        if mine["net"] is None:
            groups["pin_connected"].append({"designator": mine["designator"],
                                            "pin": mine["pad"], "file_net": pin["net"],
                                            "at_mil": _point(mine["at"])})
            continue
        design_of_out[pin["net"]][mine["net"]] += 1
        out_of_design[mine["net"]][pin["net"]] += 1
        pins_on[(mine["net"], pin["net"])].append(
            {"designator": mine["designator"], "pin": mine["pad"], "at_mil": _point(mine["at"])})
    for key, pin in sorted(keyed.items(), key=lambda item: natural_key(item[0][0])):
        if key in ambiguous or key in out_pins or pin["net"] is None:
            continue
        groups["pins_missing"].append({"designator": pin["designator"], "pin": pin["pad"],
                                       "design_net": pin["net"], "at_mil": _point(pin["at"])})
    for net, outs in sorted(out_of_design.items()):
        if len(outs) > 1:
            # The pins on the file's other nets (all but the one most of the net is on).
            pins = [{"file_net": other, **pin} for other, _ in outs.most_common()[1:]
                    for pin in pins_on[(net, other)]]
            groups["net_split"].append({"design_net": net, "file_nets": dict(outs.most_common(6)),
                                        **_pin_list(pins)})
    for net, mine in sorted(design_of_out.items()):
        if len(mine) > 1:
            pins = [{"design_net": other, **pin} for other, _ in mine.most_common()[1:]
                    for pin in pins_on[(other, net)]]
            groups["nets_merged"].append({"file_net": net,
                                          "design_nets": dict(mine.most_common(6)),
                                          **_pin_list(pins)})
        elif len(mine) == 1:
            design_net = next(iter(mine))
            if len(out_of_design[design_net]) == 1 and not same_name(net, design_net):
                groups["net_name"].append({"file_net": net, "design_net": design_net,
                                           **_pin_list(pins_on[(design_net, net)], 3)})
    messages = {
        "pins_missing": ("warning", "Pins on a net in the design missing from the file."),
        "pins_extra": ("warning", "Pins in the file that are not on the board."),
        "pin_unconnected": ("warning", "Pins unconnected (N/C) in the file but on a net in "
                                       "the design."),
        "pin_connected": ("warning", "Pins on a net in the file but unconnected in the "
                                     "design."),
        "net_split": ("warning", "Design nets split over several nets in the file."),
        "nets_merged": ("warning", "File nets joining several design nets."),
        "net_name": ("info", "Nets with the same pins under another name."),
    }
    wired = sum(1 for pin in keyed.values() if pin["net"])
    unmatched = len(groups.get("pins_missing", [])) + len(groups.get("pins_extra", []))
    if wired >= 5 and unmatched > max(wired, len(out_pins)) / 2:
        findings.add(check, "netlist_differs", "warning",
                     f"{unmatched} pins are on one side only ({len(out_pins)} in the file, "
                     f"{wired} on a net on the board): the file is for another board or "
                     "revision.", label, count=unmatched)
    for code, (severity, message) in messages.items():
        if groups.get(code):
            findings.add(check, code, severity, message, label, groups[code])
    stats = {"pins": len(out_pins), "design_pins": len(keyed),
             "nets": len(design_of_out),
             "mismatches": sum(len(groups[code]) for code in messages
                               if messages[code][0] == "warning" and groups.get(code))}
    if ambiguous:
        stats["ambiguous_pins"] = len(ambiguous)
    return stats


def _pin_list(pins: list[dict], kept: int = PINS_KEPT) -> dict:
    """{"pins": the first pins (designator, pin, XY), "pins_total" when more}."""
    listed = {"pins": pins[:kept]}
    if len(pins) > kept:
        listed["pins_total"] = len(pins)
    return listed


def check_ipc356(entry: OutputFile, ipc: fo.Ipc356, design: Design,
                 findings: Findings) -> dict:
    pins: dict[tuple[str, str], dict] = {}
    for record in ipc.records:
        if record.is_via or record.code == "367" or not record.refdes:
            continue
        key = (record.refdes.upper(), record.pin.upper())
        net = None if record.unconnected else record.net
        if key not in pins or (pins[key]["net"] is None and net):
            pins[key] = {"net": net, "at": _point((record.x_mil, record.y_mil))}
    stats = compare_netlist("netlist", entry.label, pins, design, findings, cut=(6, 4, 14))
    stats["records"] = len(ipc.records)
    stats["vias"] = sum(1 for record in ipc.records if record.is_via)
    return stats


def check_ipc2581(entry: OutputFile, export: fo.Ipc2581, design: Design,
                  variant_arg: str | None, tol: Tolerances, findings: Findings) -> dict:
    pins: dict[tuple[str, str], dict] = {}
    for net, members in export.nets.items():
        for refdes, pin in members:
            if refdes:
                pins[(refdes.upper(), (pin or "").upper())] = {"net": net, "at": None}
    stats = compare_netlist("netlist", entry.label, pins, design, findings)
    # Components against the placement: side and rotation, the populate flags.
    groups: dict[str, list[dict]] = defaultdict(list)
    for component in export.components:
        part = design.by_designator.get(component.refdes.upper())
        if part is None:
            groups["extra"].append({"designator": component.refdes})
            continue
        if component.side and component.side != part.side:
            groups["side"].append({"designator": component.refdes, "file": component.side,
                                   "design": part.side})
        if component.rotation is not None and \
                _angle(component.rotation, part.rotation) > tol.rotation_deg + 1e-9:
            groups["rotation"].append({"designator": component.refdes,
                                       "file": component.rotation,
                                       "design": round(part.rotation, 3)})
    if export.populate:
        # The variant: the argument, the OutJob output's, else the one whose Not Fitted
        # parts fit the file's unpopulated ones best (the file names none).
        variant = variant_arg if variant_arg is not None else next(
            (row["variant"] for row in entry.outputs if row.get("variant")), None)
        source = "argument" if variant_arg is not None else "outjob"
        if variant is None or not design.variant(variant)[1]:
            unpopulated = {refdes.upper() for refdes, populate in export.populate.items()
                           if not populate}
            variant = min([None, *design.variant_names()], key=lambda name: (
                len(design.not_fitted_designators(name) ^ unpopulated), name is not None))
            source = "best_match"
        stats["populate_variant"] = _variant_label(design.variant(variant)[0], True, variant)
        stats["populate_variant_from"] = source
        not_fitted = design.not_fitted_designators(variant)
        for refdes, populate in sorted(export.populate.items()):
            key = refdes.upper()
            if key in design.by_designator and populate == (key in not_fitted):
                groups["populate"].append({"designator": refdes, "file_populate": populate,
                                           "design_fitted": key not in not_fitted})
    for code, message in (("extra", "IPC-2581 components that are not on the board."),
                          ("side", "IPC-2581 components on the other side."),
                          ("rotation", "IPC-2581 component rotation differs from the board's."),
                          ("populate", "IPC-2581 BOM populate flags differ from the "
                                       "variant's fitted parts.")):
        if groups.get(code):
            findings.add("netlist", f"component_{code}", "warning", message, entry.label,
                         groups[code])
            stats["mismatches"] += len(groups[code])
    stats["components"] = len(export.components)
    return stats


# ---------------------------------------------------------------- stackup

_CONDUCTOR = {"CONDUCTOR", "SIGNAL", "PLANE", "MIXED", "POWER_GROUND", "CONDFILM",
              "CONDFOIL"}
_DIELECTRIC = {"DIELCORE", "DIELPREG", "DIELBASE", "DIELADHV", "DIELECTRIC",
               "COVERLAY", "COVERFILM"}


def check_ipc2581_stackup(entry: OutputFile, export: fo.Ipc2581, design: Design,
                          tol: Tolerances, findings: Findings) -> dict:
    """The file's stackup against the PcbDoc's: copper layers, and the dielectrics between
    the outer copper layers (solder mask, overlay and paste are outside them on both
    sides). Layers pair up in order when the counts agree, else by name only."""
    layers = [layer for layer in design.index.pcb.board.layers
              if layer.stack_order is not None and layer.kind in ("copper", "dielectric")] \
        if design.index is not None else []
    layers.sort(key=lambda layer: layer.stack_order)
    stack = list(export.stackup)
    if any(layer.sequence is not None for layer in stack):
        stack.sort(key=lambda layer: (layer.sequence is None, layer.sequence or 0))
    if not stack:
        findings.add("stackup", "no_stackup", "info", "The IPC-2581 file has no stackup.",
                     entry.label)
        return {"layers": 0, "mismatches": 0}

    def conductor(layer) -> bool:
        return (layer.function or "").upper() in _CONDUCTOR

    file_copper = [layer for layer in stack if conductor(layer)]
    file_diel = [layer for layer in _inside_copper(stack, conductor)
                 if (layer.function or "").upper() in _DIELECTRIC]
    copper = [layer for layer in layers if layer.kind == "copper"]
    dielectric = [layer for layer in _inside_copper(layers, lambda layer: layer.kind == "copper")
                  if layer.kind == "dielectric"]
    groups: dict[str, list[dict]] = defaultdict(list)
    if len(file_copper) != len(copper):
        groups["copper_count"].append({"file": len(file_copper), "design": len(copper)})
    for theirs, mine in _paired(file_copper, copper):
        if _fold(theirs.name) != _fold(mine.name):
            groups["copper_name"].append({"file": theirs.name, "design": mine.name})
        _thickness(groups, "copper_thickness", theirs, mine.copper_thick_mil, mine.name, tol)
    if file_diel and len(file_diel) != len(dielectric):
        groups["dielectric_count"].append({"file": len(file_diel), "design": len(dielectric)})
    for theirs, mine in _paired(file_diel, dielectric):
        _thickness(groups, "dielectric_thickness", theirs, mine.diel_height_mil, mine.name, tol)
        if theirs.dielectric_constant and mine.diel_const and \
                abs(theirs.dielectric_constant - mine.diel_const) > 0.01:
            groups["dielectric_constant"].append({"layer": mine.name,
                                                  "file": theirs.dielectric_constant,
                                                  "design": mine.diel_const})
        if theirs.material and mine.material and _fold(theirs.material) != _fold(mine.material):
            groups["material"].append({"layer": mine.name, "file": theirs.material,
                                       "design": mine.material})
    messages = {
        "copper_count": "Copper layer count differs (layers are then compared by name only).",
        "copper_name": "Copper layer names or order differ.",
        "copper_thickness": "Copper thickness differs beyond the thickness tolerance.",
        "dielectric_count": "Count of dielectric layers between the outer copper layers "
                            "differs (they are then compared by name only).",
        "dielectric_thickness": "Dielectric thickness differs beyond the thickness tolerance.",
        "dielectric_constant": "Dielectric constant differs.",
        "material": "Dielectric material differs.",
    }
    for code, message in messages.items():
        if groups.get(code):
            findings.add("stackup", code, "warning", message, entry.label, groups[code])
    return {"layers": len(stack), "copper_layers": len(file_copper),
            "mismatches": sum(len(rows) for rows in groups.values())}


def _inside_copper(layers: list, is_copper) -> list:
    """The layers from the first copper layer to the last (stack order)."""
    marks = [number for number, layer in enumerate(layers) if is_copper(layer)]
    return layers[marks[0]:marks[-1] + 1] if marks else []


def _paired(theirs: list, mine: list) -> list[tuple]:
    """File and design layers side by side: in order when the counts agree, else the
    ones whose names agree."""
    if len(theirs) == len(mine):
        return list(zip(theirs, mine))
    by_name = {_fold(layer.name): layer for layer in mine}
    return [(layer, by_name[_fold(layer.name)]) for layer in theirs
            if _fold(layer.name) in by_name]


def _thickness(groups, code, theirs, mine: float | None, name: str, tol: Tolerances):
    if theirs.thickness_mil is None or not mine:
        return
    if abs(theirs.thickness_mil - mine) > mine * tol.thickness_percent / 100.0 + 0.01:
        groups[code].append({"layer": name, "file_mil": round(theirs.thickness_mil, 4),
                             "design_mil": round(mine, 4)})


def check_layer_names(entry: OutputFile, pairs: list[tuple[str, list[str]]],
                      design: Design, findings: Findings) -> dict:
    """The layers .LDP and .DRR layer pairs name, against the board's copper layers. Their
    order is not judged: the .LDP lists a pair's layers in layer-ID order (every file
    sampled), which is not the stack order when mid layers are numbered out of it."""
    rows = []
    for what, layers in pairs:
        unknown = [layer for layer in layers if design.layer_position(layer) is None]
        if unknown:
            rows.append({"pair": what, "unknown_layers": unknown})
    if rows:
        findings.add("stackup", "layer_pair_layers", "warning",
                     "Layer pairs name layers the board's stack does not have.", entry.label,
                     rows)
    return {"pairs": len(pairs), "mismatches": len(rows)}


# ---------------------------------------------------------------- drill

@dataclass
class _Tool:
    file: str
    number: int
    size_mil: float
    step_mil: float
    plated: bool | None
    holes: int
    slots: int


def _drill_sets(files: list[OutputFile], parsed: dict[str, object], design: Design,
                findings: Findings, digest_of: dict[str, str], foreign: set[str]
                ) -> list[dict]:
    """The drill sets to compare, [{span: stack positions (top, bottom), or None for a file
    no .DRR or .LDP names; files; reports: [(entry, DrillReportPair, its files)]}]:

    - a .DRR report's layer pairs with the drill files each names, one set per report and
      layer pair (one NC Drill output's generation);
    - an .LDP file's pairs whose drill file no report names, one set per file and layer
      pair;
    - drill files neither names, one set per folder (taken as the through holes).

    A report names its drill files by name: the one in its own folder, else elsewhere in
    its generation (group) nearest in time. Files a report or layer-pairs file in foreign
    (naming another board) names are left out, as are pairs naming a layer the stack
    lacks (a finding). Identical sets (copies) are compared once, and a file no report
    names is left out when an identical file was compared in a named set."""
    by_group: dict[str, list[OutputFile]] = defaultdict(list)
    for entry in files:
        by_group[entry.group].append(entry)
    sets: list[dict] = []
    unnamed: list[dict] = []
    for members in by_group.values():
        drills = [entry for entry in members if entry.kind == "excellon"]
        by_name: dict[str, list[OutputFile]] = defaultdict(list)
        for entry in drills:
            by_name[entry.name.lower()].append(entry)

        def named(name: str, near: OutputFile) -> OutputFile | None:
            base = re.split(r"[\\/]", name.strip())[-1].lower()
            candidates = by_name.get(base) or []
            if not candidates:
                return None
            return min(candidates, key=lambda entry: (
                _folder_of(entry) != _folder_of(near),
                abs((entry.mtime or 0.0) - (near.mtime or 0.0))))

        claimed: set[int] = set()
        for entry in members:
            result = parsed.get(entry.label)
            if not isinstance(result, fo.DrillReport):
                continue
            per_span: dict[tuple | None, dict] = {}
            for pair in result.pairs:
                own = []
                for name in pair.files.values():
                    drill = named(name, entry)
                    if drill is not None and drill not in own:
                        own.append(drill)
                claimed.update(id(drill) for drill in own)
                if entry.label in foreign:
                    continue
                span = design.layer_span([pair.from_layer, pair.to_layer]) \
                    if pair.from_layer else None
                if pair.from_layer and span is None:
                    findings.add("drill", "layer_pair_unknown", "warning",
                                 f"The drill report's layer pair '{pair.layer_pair}' names a "
                                 "layer the board's stack does not have: its holes are not "
                                 "compared.", entry.label)
                    continue
                drill_set = per_span.setdefault(span, {"span": span, "files": [],
                                                       "reports": []})
                drill_set["files"] += [drill for drill in own if drill not in drill_set["files"]]
                drill_set["reports"].append((entry, pair, own))
            sets.extend(per_span.values())
        for entry in members:
            result = parsed.get(entry.label)
            if not isinstance(result, fo.LayerPairs):
                continue
            per_span = {}
            for pair in result.pairs:
                drill = named(pair.drill_file or "", entry)
                if drill is None or id(drill) in claimed:
                    continue
                claimed.add(id(drill))
                if entry.label in foreign:
                    continue
                span = design.layer_span(pair.layers)
                if span is None:
                    findings.add("drill", "layer_pair_unknown", "warning",
                                 f"The layer pair '{pair.name or pair.drill_file}' names a layer "
                                 "the board's stack does not have: its drill file is not "
                                 "compared.", entry.label)
                    continue
                per_span.setdefault(span, {"span": span, "files": [], "reports": []})[
                    "files"].append(drill)
            sets.extend(per_span.values())
        by_folder: dict[str, list[OutputFile]] = defaultdict(list)
        for drill in drills:
            if id(drill) not in claimed:
                by_folder[_folder_of(drill)].append(drill)
        unnamed += [{"span": None, "files": group, "reports": []}
                    for group in by_folder.values()]
    kept: list[dict] = []
    signatures: set[tuple] = set()
    compared: set[str] = set()
    for drill_set, by_folder_only in [(s, False) for s in sets] + [(s, True) for s in unnamed]:
        if by_folder_only:
            drill_set["files"] = [entry for entry in drill_set["files"]
                                  if digest_of.get(entry.label, entry.label) not in compared]
            if not drill_set["files"]:
                continue
        members = [*drill_set["files"], *(report[0] for report in drill_set["reports"])]
        signature = (drill_set["span"], tuple(sorted(
            digest_of.get(member.label, member.label) for member in members)))
        if signature in signatures:
            continue
        signatures.add(signature)
        compared.update(digest_of.get(entry.label, entry.label) for entry in drill_set["files"])
        kept.append(drill_set)
    return kept


def _folder_of(entry: OutputFile) -> str:
    return entry.label.rsplit("/", 1)[0] if "/" in entry.label else ""


def check_drill(sets: list[dict], parsed: dict, design: Design, tol: Tolerances,
                findings: Findings) -> dict:
    names = design.copper_layers()
    holes = design.holes()
    last = len(names) - 1
    stats = {"drill_sets": 0, "tools": 0, "holes": 0, "mismatches": 0}
    if not names:
        return stats
    spans = {hole.span or (0, last) for hole in holes}
    merged_in: dict[tuple, int] = {}
    for drill_set in sets:
        span = drill_set["span"]
        assumed = span is None
        span = span or (0, last)
        stats["drill_sets"] += 1
        mine, merged_in[span] = _distinct_holes([hole for hole in holes
                                                 if (hole.span or (0, last)) == span])
        pair_label = f"{names[span[0]]} - {names[span[1]]}"
        if drill_set["files"]:
            stats["mismatches"] += _compare_drill_files(drill_set, parsed, mine, pair_label,
                                                        assumed and len(spans) > 1, design,
                                                        tol, findings, stats)
        for report in drill_set["reports"]:
            stats["mismatches"] += _compare_report(report, parsed, mine, pair_label, tol,
                                                   findings)
    if sum(merged_in.values()):
        stats["coincident_holes_merged"] = sum(merged_in.values())
    return stats


def _distinct_holes(holes: list) -> tuple[list, int]:
    """The holes with coincident ones (same place, size, shape and plating: stacked pads)
    kept once, as the drill file drills them; and how many were merged."""
    seen = set()
    distinct = []
    for hole in holes:
        key = (round(hole.x, 2), round(hole.y, 2), round(hole.hole_mil, 2), hole.hole_shape,
               hole.plated is not False)
        if key in seen:
            continue
        seen.add(key)
        distinct.append(hole)
    return distinct, len(holes) - len(distinct)


def _tool_tolerance(tool: _Tool, tol: Tolerances) -> float:
    return tool.step_mil / 2.0 + tol.drill_mil


def _hole_class(hole) -> str:
    return "slot" if hole.hole_shape == 2 else "square" if hole.hole_shape == 1 else "round"


def _match_tools(mine: list, tools: list[_Tool], tol: Tolerances, use_plating: bool
                 ) -> tuple[dict, list]:
    """Assign each design hole to the nearest tool of its size (and plating) and kind:
    (design holes per (file, tool number), holes no tool takes); square holes left out."""
    assigned: dict[tuple, list] = defaultdict(list)
    unmatched = []
    for hole in mine:
        kind = _hole_class(hole)
        if kind == "square":
            continue
        plated = hole.plated is not False
        best = None
        for tool in tools:
            if use_plating and tool.plated is not None and tool.plated != plated:
                continue
            if kind == "slot" and not tool.slots:
                continue
            if kind == "round" and not tool.holes and tool.slots:
                continue
            diff = abs(tool.size_mil - hole.hole_mil)
            if diff <= _tool_tolerance(tool, tol) and (best is None or diff < best[0]):
                best = (diff, tool)
        if best is None:
            unmatched.append(hole)
        else:
            assigned[(best[1].file, best[1].number)].append(hole)
    return assigned, unmatched


def _compare_drill_files(drill_set: dict, parsed: dict, mine: list, pair_label: str,
                         ambiguous_pair: bool, design: Design, tol: Tolerances,
                         findings: Findings, stats: dict) -> int:
    files = drill_set["files"]
    label = files[0].label if len(files) == 1 else f"{_folder_of(files[0]) or '.'}/" + \
        ", ".join(entry.name for entry in files)
    tools: list[_Tool] = []
    hits: list[tuple[float, float, _Tool]] = []
    for entry in files:
        drill = parsed.get(entry.label)
        if not isinstance(drill, fo.ExcellonDrill):
            continue
        by_number = {}
        for tool in drill.tools.values():
            if tool.diameter_mil is None:
                continue
            row = _Tool(entry.label, tool.number, tool.diameter_mil,
                        tool.diameter_step_mil or 0.0, tool.plated, tool.hits, tool.slots)
            by_number[tool.number] = row
            tools.append(row)
        for hit in drill.hits:
            tool = by_number.get(hit.tool)
            if tool is not None and hit.kind == "hole":
                hits.append((hit.x_mil, hit.y_mil, tool))
    use_plating = any(tool.plated is not None for tool in tools)
    assigned, unmatched = _match_tools(mine, tools, tol, use_plating)
    groups: dict[str, list[dict]] = defaultdict(list)
    for tool in sorted(tools, key=lambda tool: (tool.file, tool.number)):
        matched = assigned.get((tool.file, tool.number), [])
        named = {"tool": f"T{tool.number}", **({"in": tool.file.rsplit("/", 1)[-1]}
                                                 if len(files) > 1 else {})}
        design_holes = sum(1 for hole in matched if _hole_class(hole) == "round")
        design_slots = sum(1 for hole in matched if _hole_class(hole) == "slot")
        if not matched and (tool.holes or tool.slots):
            other = [hole for hole in mine if abs(hole.hole_mil - tool.size_mil)
                     <= _tool_tolerance(tool, tol)]
            groups["tool_not_in_design"].append({
                **named, "size_mil": round(tool.size_mil, 4),
                "plated": tool.plated, "holes": tool.holes, "slots": tool.slots,
                **({"note": "the board has holes of this size with the other plating"}
                   if other else {})})
            continue
        if design_holes != tool.holes or design_slots != tool.slots:
            groups["hole_count"].append({
                **named, "size_mil": round(tool.size_mil, 4),
                "plated": tool.plated, "file_holes": tool.holes, "design_holes": design_holes,
                **({"file_slots": tool.slots, "design_slots": design_slots}
                   if tool.slots or design_slots else {})})
    missing: dict[tuple, int] = Counter()
    for hole in unmatched:
        missing[(round(hole.hole_mil, 3), hole.plated is not False, _hole_class(hole))] += 1
    for (size, plated, kind), count in sorted(missing.items()):
        groups["size_missing"].append({"size_mil": size, "plated": plated, "kind": kind,
                                       "design_holes": count})
    squares = sum(1 for hole in mine if _hole_class(hole) == "square")
    if squares:
        stats["square_holes_not_compared"] = stats.get("square_holes_not_compared", 0) + squares
    stats["tools"] += len(tools)
    stats["holes"] += len(hits)
    # Positions: hits against the design's round holes, at the origin the OutJob sets,
    # else at whichever fits.
    origin_setting = next((str(row["settings"].get("origin", "")) for entry in files
                           for row in entry.outputs if row.get("settings")), "")
    positions = _compare_positions(hits, mine, design, origin_setting)
    if positions is not None:
        stats.setdefault("positions", []).append({"pair": pair_label, **positions["stats"]})
        groups["hole_missing_at"] = positions["missing"]
        groups["hole_extra_at"] = positions["extra"]
        if positions["stats"].get("unfit"):
            findings.add("drill", "positions_do_not_fit", "warning",
                         f"Most drill hits ({pair_label}) sit on no board hole at either "
                         "origin: the file is from another revision or origin.", label,
                         count=positions["stats"]["hits"],
                         on_board_share=positions["stats"]["on_board_share"])
            groups["hole_missing_at"] = []
            groups["hole_extra_at"] = []
    messages = {
        "tool_not_in_design": "Drill tools with no board hole of that size (and plating).",
        "size_missing": "Board holes of a size (and plating) no tool drills.",
        "hole_count": "Hole counts per tool differ from the board's holes of that size.",
        "hole_missing_at": "Board holes with no drill hit at their position.",
        "hole_extra_at": "Drill hits at no board hole of the pair.",
    }
    count = 0
    for code, message in messages.items():
        if groups.get(code):
            extra = {"layer_pair": pair_label}
            if ambiguous_pair:
                extra["note"] = ("no .DRR or .LDP says which layer pair the file drills: "
                                 "compared with the through holes")
            findings.add("drill", code, "warning", message, label, groups[code], **extra)
            count += len(groups[code])
    return count


def _compare_positions(hits, mine, design: Design, origin_setting: str) -> dict | None:
    rounds = [hole for hole in mine if _hole_class(hole) == "round"]
    if not hits or not rounds:
        return None
    ox, oy = design.origin
    cell = HOLE_POSITION_MIL * 2

    def fit(dx: float, dy: float) -> tuple[list, list]:
        grid: dict[tuple[int, int], list[int]] = defaultdict(list)
        for number, hole in enumerate(rounds):
            grid[(int((hole.x - dx) // cell), int((hole.y - dy) // cell))].append(number)
        used = set()
        extra = []
        for x, y, tool in hits:
            cx, cy = int(x // cell), int(y // cell)
            found = None
            for gx in (cx - 1, cx, cx + 1):
                for gy in (cy - 1, cy, cy + 1):
                    for number in grid.get((gx, gy), ()):
                        hole = rounds[number]
                        if number not in used and math.hypot(hole.x - dx - x, hole.y - dy - y) \
                                <= HOLE_POSITION_MIL:
                            found = number
                            break
                    if found is not None:
                        break
                if found is not None:
                    break
            if found is None:
                extra.append((x, y, tool))
            else:
                used.add(found)
        missing = [hole for number, hole in enumerate(rounds) if number not in used]
        return missing, extra

    setting = origin_setting.lower()
    candidates = [("relative", (ox, oy)), ("absolute", (0.0, 0.0))]
    if setting.startswith("absolute"):
        candidates.reverse()
    if (ox, oy) == (0.0, 0.0):  # the board origin is the absolute one
        candidates = candidates[:1]
    best = None
    for name, (dx, dy) in candidates:
        missing, extra = fit(dx, dy)
        share = 1 - len(extra) / len(hits)
        if best is None or share > best[0]:
            best = (share, name, (dx, dy), missing, extra)
        if share == 1.0 or (setting and name == candidates[0][0] and share >= 0.99):
            break
    share, name, (dx, dy), missing, extra = best
    stats = {"origin": name, "hits": len(hits), "on_board_share": round(share, 4)}
    if share < POSITION_FIT_SHARE:
        stats["unfit"] = True
    return {
        "stats": stats,
        "missing": [{"at_mil": _point((hole.x - dx, hole.y - dy)),
                     "size_mil": round(hole.hole_mil, 3), "kind": hole.kind}
                    for hole in missing],
        "extra": [{"at_mil": _point((x, y)), "tool": f"T{tool.number}",
                   "size_mil": round(tool.size_mil, 3)} for x, y, tool in extra],
    }


def _compare_report(report: tuple, parsed: dict, mine: list, pair_label: str,
                    tol: Tolerances, findings: Findings) -> int:
    """The .DRR report's tool rows against the drill files it names (by tool number), or
    against the board's holes when they are not there."""
    entry, pair, own = report
    groups: dict[str, list[dict]] = defaultdict(list)
    drills = [parsed.get(member.label) for member in own]
    drills = [drill for drill in drills if isinstance(drill, fo.ExcellonDrill)]
    if drills:
        tools = {}
        for drill in drills:
            for tool in drill.tools.values():
                tools.setdefault(tool.number, tool)
        for row in pair.tools:
            number = int(row.tool[1:]) if row.tool[1:].isdigit() else None
            tool = tools.get(number)
            if tool is None:
                groups["report_tool_missing"].append({"tool": row.tool,
                                                      "size_mil": round(row.size_mil, 3)})
                continue
            step = (row.size_step_mil or 0.0) / 2 + (tool.diameter_step_mil or 0.0) / 2 + \
                tol.drill_mil
            problems = {}
            if tool.diameter_mil is not None and abs(tool.diameter_mil - row.size_mil) > step:
                problems["size_mil"] = [round(row.size_mil, 3), round(tool.diameter_mil, 4)]
            if row.count != tool.hits + tool.slots:
                problems["count"] = [row.count, tool.hits + tool.slots]
            if row.plated is not None and tool.plated is not None and row.plated != tool.plated:
                problems["plated"] = [row.plated, tool.plated]
            if problems:
                groups["report_differs"].append({"tool": row.tool, "report_vs_file": problems})
    else:
        tools = [_Tool(entry.label, int(row.tool[1:]) if row.tool[1:].isdigit() else index,
                       row.size_mil, row.size_step_mil or 0.0, row.plated,
                       row.count if row.hole_type.lower() != "slot" else 0,
                       row.count if row.hole_type.lower() == "slot" else 0)
                 for index, row in enumerate(pair.tools)]
        assigned, unmatched = _match_tools(mine, tools, tol, True)
        for tool in tools:
            matched = len(assigned.get((tool.file, tool.number), []))
            if matched != tool.holes + tool.slots:
                groups["report_count"].append({"tool": f"T{tool.number}",
                                               "size_mil": round(tool.size_mil, 3),
                                               "report": tool.holes + tool.slots,
                                               "design": matched})
        sizes = Counter((round(hole.hole_mil, 3), hole.plated is not False)
                        for hole in unmatched if _hole_class(hole) != "square")
        for (size, plated), count in sorted(sizes.items()):
            groups["report_size_missing"].append({"size_mil": size, "plated": plated,
                                                  "design_holes": count})
    messages = {
        "report_tool_missing": "Tools in the drill report that the drill file does not define.",
        "report_differs": "The drill report and its drill file disagree (report, file).",
        "report_count": "Drill report hole counts differ from the board's holes of that size.",
        "report_size_missing": "Board holes of a size the drill report has no tool for.",
    }
    count = 0
    for code, message in messages.items():
        if groups.get(code):
            findings.add("drill", code, "warning", message, entry.label, groups[code],
                         layer_pair=pair_label)
            count += len(groups[code])
    return count


# ---------------------------------------------------------------- staleness

def _sources(design: Design, entry: OutputFile) -> dict[str, float]:
    """The design files an output comes from, with their times: its OutJob outputs' data
    sources (as get_output_jobs reads them), else the PcbDoc checked (for a BOM also the
    sheets and the .BomDocs); for pick and place and a BOM also the .PrjPcb when the
    project has variants (they are defined there)."""
    project = design.project
    paths = [path for row in entry.outputs for path in row.get("sources") or []]
    if not paths:
        paths = list(project.get("pcb_doc_paths") or [])[:1]
        if entry.family == "bom":
            paths += [sheet["path"] for sheet in project.get("sheets") or []
                      if sheet.get("path")]
            paths += design.bomdocs
    if entry.family in ("pick_place", "bom") and design.variant_names():
        paths.append(project["prj_pcb_path"])
    times = {}
    for path in paths:
        try:
            times[path] = os.stat(path).st_mtime
        except OSError:
            continue
    return times


# ---------------------------------------------------------------- the run

def _parse(entry: OutputFile):
    """The parsed file, or an error text."""
    data = entry.data() if entry.kind != "ipc2581" or entry.path is None else None
    if data is not None and len(data) > MAX_FILE_BYTES:
        return f"larger than {MAX_FILE_BYTES >> 20} MB"
    kind = entry.kind
    try:
        if kind == "pick_place":
            return fo.parse_pick_place(data, entry.name)
        if kind == "bom":
            return fo.parse_bom_csv(data)
        if kind == "bom_workbook":
            if _suffix(entry.name) == ".xls":
                return "an .xls (BIFF) workbook: not read; save the BOM as .xlsx or .csv"
            return fo.parse_bom_xlsx(data)
        if kind == "ipc356":
            return fo.parse_ipc356(data)
        if kind == "ipc2581":
            return fo.parse_ipc2581(entry.path if data is None else data)
        if kind == "excellon":
            return fo.parse_excellon(data, entry.name)
        if kind == "drill_report":
            return fo.parse_drill_report(data)
        if kind == "layer_pairs":
            return fo.parse_layer_pairs(data)
    except (ValueError, OSError, UnicodeDecodeError, IndexError, KeyError) as error:
        return f"{type(error).__name__}: {error}"
    return None


def _cache_key(located: Located, args: tuple) -> tuple:
    return (args, tuple((entry.label, entry.size, entry.mtime) for entry in located.files))


def run_verify_outputs(project: dict, index, netlist: dict | None, variant_state, *,
                       path: str | None = None, variant: str | None = None,
                       checks: list[str] | None = None,
                       tolerances: Tolerances | None = None) -> dict:
    """The full verify_outputs model (findings unpaged), or {"error": ...}. Cached on the
    index per arguments and found files (paging reuses it)."""
    start = time.perf_counter()
    tol = tolerances or Tolerances()
    if checks:
        unknown = sorted(set(checks) - set(CHECKS))
        if unknown:
            return {"error": "invalid_checks",
                    "message": f"Unknown checks {unknown}; choose from {list(CHECKS)}."}
    wanted = [check for check in CHECKS if not checks or check in checks]
    root = str(Path(project["prj_pcb_path"]).parent)
    located = locate_path(path, root) if path else locate_project(project)
    if located.error:
        return located.error
    # The OutJob outputs (BOM data sources, staleness sources) in either mode.
    locations = located.locations if located.mode == "project" else output_locations(project)
    if "error" in locations:
        locations = {}
    outputs = locations.get("outputs") or []
    key = _cache_key(located, (path, variant, tuple(wanted), tuple(sorted(tol.echo().items())),
                               id(netlist), id(variant_state),
                               tuple((row["outjob"], row["output"], row["variant"],
                                      row["document"], row["folder"]) for row in outputs),
                               tuple(locations.get("bomdocs") or ())))
    cache = getattr(index, "_verify_outputs", None) if index is not None else None
    if cache is not None and key in cache:
        return cache[key]
    design = Design(project, index, netlist, variant_state)
    design.outputs = outputs
    design.bomdocs = list(locations.get("bomdocs") or [])
    if variant is not None:
        known = design.variant(variant)[1]
        if not known:
            return {"error": "variant_not_found",
                    "message": f"No variant '{variant}'. Variants: "
                               + ", ".join(design.variant_names()) + f", or '{BASE_VARIANT}'."}
    model = _run(located, design, variant, wanted, tol)
    model["elapsed_ms"] = round((time.perf_counter() - start) * 1000)
    if index is not None:
        if cache is None:
            cache = index._verify_outputs = {}
        if len(cache) >= CACHE_SIZE:
            cache.pop(next(iter(cache)))
        cache[key] = model
    return model


def _run(located: Located, design: Design, variant: str | None, wanted: list[str],
         tol: Tolerances) -> dict:
    findings = Findings()
    not_checked: list[dict] = []
    files_out: list[dict] = []
    parsed: dict[str, object] = {}
    digests: dict[str, str] = {}
    digest_of: dict[str, str] = {}
    rows_seen: dict[tuple, str] = {}
    results: dict[str, dict] = {check: {"files": 0, "mismatches": 0} for check in wanted}
    has_board = design.index is not None
    checked_files = 0
    by_label = {entry.label: entry for entry in located.files}
    for entry in located.files:
        if entry.kind in ("gerber", "odb"):
            continue
        if checked_files >= MAX_FILES:
            located.skipped["file limit"] += 1
            continue
        try:
            data = entry.data() if entry.kind != "ipc2581" or entry.path is None else None
        except OSError as error:
            files_out.append({"file": entry.label, "kind": entry.kind,
                              "status": "unreadable", "error": str(error)})
            continue
        if data is not None:
            digest = hashlib.sha1(data).hexdigest()
            digest_of[entry.label] = digest
            if digest in digests:
                entry.same_as = digests[digest]
                entry.kind = by_label[entry.same_as].kind  # a workbook found no BOM
                # A copy of a drill file stays a member of its own folder's set (identical
                # sets are compared once).
                if entry.family == "drill" and entry.same_as in parsed:
                    parsed[entry.label] = parsed[entry.same_as]
                continue
            digests[digest] = entry.label
        result = _parse(entry)
        checked_files += 1
        if entry.kind == "bom_workbook" and (
                isinstance(result, str) or result is None or result.header_row is None):
            # A workbook is a BOM once a BOM header row is found in it.
            entry.kind = "workbook"
            files_out.append({"file": entry.label, "kind": "workbook", "status": "not_read",
                              "reason": result if isinstance(result, str) else
                              "not a BOM: no sheet with a BOM header row"})
            continue
        if isinstance(result, str) or result is None:
            files_out.append({"file": entry.label, "kind": entry.kind, "status": "not_read",
                              "reason": result or "no reader"})
            continue
        if entry.kind == "pick_place":
            signature = tuple((row.designator, row.x_mil, row.y_mil, row.rotation, row.side)
                              for row in result.rows)
            if signature and signature in rows_seen:
                entry.same_as = rows_seen[signature]
                continue
            rows_seen[signature] = entry.label
        parsed[entry.label] = result
    # Drill reports and layer-pairs files that name another board than the one checked.
    foreign: set[str] = set()
    other_boards: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for label, result in parsed.items():
        if not isinstance(result, (fo.DrillReport, fo.LayerPairs)) or not has_board:
            continue
        other = _other_board(design, board_named(result.board))
        if other is not None:
            foreign.add(label)
            if not by_label[label].same_as:
                other_boards[other].append({"file": label})
    board_check = next((check for check in ("drill", "stackup") if check in wanted), None)
    for (severity, message), examples in other_boards.items():
        if board_check:
            findings.add(board_check, "other_board", severity, message,
                         examples[0]["file"] if len(examples) == 1 else None, examples)
            if severity == "warning":
                results[board_check]["mismatches"] += len(examples)

    def row_of(entry: OutputFile, check: str, stats: dict, parse_warnings: list) -> None:
        row = {"file": entry.label, "kind": entry.kind, "check": check}
        row.update({key: value for key, value in stats.items() if key != "positions"})
        if stats.get("positions"):
            row["positions"] = stats["positions"]
        if parse_warnings:
            row["parse_warnings"] = parse_warnings[:5]
        if entry.outputs:
            row["outjob_output"] = [f"{o['outjob']}: {o['output']}" for o in entry.outputs][:3]
        files_out.append(row)

    for label, result in parsed.items():
        entry = by_label[label]
        warnings = list(getattr(result, "warnings", []) or [])
        if label in foreign:
            if not entry.same_as and "drill" not in wanted and "stackup" in wanted:
                row_of(entry, "stackup", {"status": "not_checked: another board"}, warnings)
            continue
        if isinstance(result, fo.PickPlace) and "pick_place" in wanted:
            if not has_board:
                row_of(entry, "pick_place", {"status": "not_checked: no PcbDoc"}, warnings)
                continue
            stats = check_pick_place(entry, result, design, variant, tol, findings)
            _tally(results, "pick_place", stats)
            row_of(entry, "pick_place", stats, warnings)
        elif isinstance(result, fo.Bom) and "bom" in wanted:
            if result.header_row is None:
                row_of(entry, "bom", {"status": "not_read: no BOM header row"}, warnings)
                continue
            stats = check_bom(entry, result, design, variant, findings)
            _tally(results, "bom", stats)
            row_of(entry, "bom", stats, warnings)
        elif isinstance(result, fo.Ipc356) and "netlist" in wanted and has_board:
            stats = check_ipc356(entry, result, design, findings)
            _tally(results, "netlist", stats)
            row_of(entry, "netlist", stats, warnings)
        elif isinstance(result, fo.Ipc2581) and has_board and (
                "netlist" in wanted or "stackup" in wanted):
            stats: dict = {}
            if "netlist" in wanted:
                stats = check_ipc2581(entry, result, design, variant, tol, findings)
                _tally(results, "netlist", stats)
            if "stackup" in wanted:
                stack = check_ipc2581_stackup(entry, result, design, tol, findings)
                _tally(results, "stackup", stack)
                stats = {**stats, "stackup": stack}
            row_of(entry, "netlist" if "netlist" in wanted else "stackup", stats, warnings)
        elif isinstance(result, fo.LayerPairs) and "stackup" in wanted and has_board:
            pairs = [(pair.name or pair.drill_file or "?", pair.layers) for pair in result.pairs]
            stats = check_layer_names(entry, pairs, design, findings)
            _tally(results, "stackup", stats)
            row_of(entry, "stackup", stats, warnings)
        elif isinstance(result, fo.DrillReport) and "stackup" in wanted and has_board:
            pairs = [(pair.layer_pair or "?", [pair.from_layer, pair.to_layer])
                     for pair in result.pairs if pair.from_layer]
            stats = check_layer_names(entry, pairs, design, findings)
            _tally(results, "stackup", stats)
            if "drill" not in wanted:
                row_of(entry, "stackup", stats, warnings)
    if "drill" in wanted and has_board:
        drill_files = [by_label[label] for label, result in parsed.items()
                       if isinstance(result, (fo.ExcellonDrill, fo.DrillReport, fo.LayerPairs))]
        if drill_files:
            sets = _drill_sets(drill_files, parsed, design, findings, digest_of, foreign)
            stats = check_drill(sets, parsed, design, tol, findings)
            # The drill files and reports compared (a copy counted once).
            results["drill"]["files"] = len({
                digest_of.get(member.label, member.label) for drill_set in sets
                for member in [*drill_set["files"],
                               *(report[0] for report in drill_set["reports"])]})
            results["drill"]["mismatches"] += stats.pop("mismatches")
            results["drill"].update({key: value for key, value in stats.items()})
            for entry in drill_files:
                if entry.kind in ("excellon", "drill_report") and not entry.same_as:
                    result = parsed[entry.label]
                    summary = ({"tools": len(result.tools), "hits": len(result.hits),
                                "units": result.units}
                               if isinstance(result, fo.ExcellonDrill)
                               else {"layer_pairs": len(result.pairs)})
                    if entry.label in foreign:
                        summary["status"] = "not_checked: another board"
                    row_of(entry, "drill", summary, list(result.warnings))
    if "staleness" in wanted:
        stale = []
        for entry in located.files:
            if entry.same_as or entry.mtime is None or entry.family is None:
                continue
            sources = _sources(design, entry)
            newer = sorted((stamp, Path(p).name) for p, stamp in sources.items()
                           if stamp > entry.mtime + STALE_TOLERANCE_S)
            if newer:
                stale.append({"file": entry.label, "file_time_utc": utc_iso(entry.mtime),
                              "saved_since": [{"document": name, "modified_utc": utc_iso(stamp)}
                                              for stamp, name in reversed(newer[-3:])]})
        results["staleness"] = {"files": len([e for e in located.files if not e.same_as
                                              and e.family]), "stale": len(stale)}
        if stale:
            findings.add("staleness", "stale", "info",
                         "Output files older than the design documents they come from (file "
                         "times; a copy or checkout resets them).", None, stale)
    found = Counter(entry.family for entry in located.files if entry.family)
    missing_outputs = []
    if located.mode == "project":
        claimed = {(o["outjob"], o["output"]) for entry in located.files if entry.family
                   for o in entry.outputs}
        for row in located.outputs:
            family = FAMILY_OF_TYPE.get(row["type"])
            if family is None or (row["outjob"], row["output"]) in claimed:
                continue
            if family in ("gerber", "odb") and found.get(family):
                continue
            missing_outputs.append({"outjob": row["outjob"], "output": row["output"],
                                    "type": row["type"],
                                    "folder": _relative(row["folder"], located.root),
                                    **({} if row["exact"] else
                                       {"note": "a date/time container: its base folder was "
                                                "searched"})})
        if missing_outputs:
            findings.add("files", "output_not_found", "info",
                         "OutJob outputs with no generated file found in their container "
                         "folder.", None, missing_outputs, fix=FIXES["missing"])
    if not found:
        findings.add("files", "no_outputs_found", "info",
                     "No generated output files were found"
                     + (" in the OutJob container folders or the Output Path."
                        if located.mode == "project" else " at the path."), None,
                     [{"searched": located.searched[:10]}], fix=FIXES["missing"])
    for check in wanted:
        entry = results.get(check)
        if entry is None:
            continue
        if check == "staleness":
            entry["status"] = "stale" if entry.get("stale") else "current" if entry["files"] \
                else "no_files"
            continue
        if not has_board and check != "bom":
            families = {"pick_place": ("pick_place",), "netlist": ("ipc356", "ipc2581"),
                        "stackup": ("ipc2581", "drill"), "drill": ("drill",)}[check]
            if any(found.get(family) for family in families):
                entry["status"] = "not_checked"
                not_checked.append({"check": check, "reason": "the project lists no "
                                                              "readable PcbDoc"})
            else:
                entry["status"] = "no_files"
            continue
        entry["status"] = ("mismatch" if entry["mismatches"] else
                           "no_files" if not entry["files"] else "match")
    for finding in findings.rows:
        finding.setdefault("fix", FIXES.get(finding["check"]))
    order = {"warning": 0, "info": 1}
    check_rank = {check: rank for rank, check in enumerate((*CHECKS, "files"))}
    findings.rows.sort(key=lambda row: (order.get(row["severity"], 2),
                                        check_rank.get(row["check"], 9), row.get("file") or "",
                                        -row["count"]))
    severities = Counter(row["severity"] for row in findings.rows)
    files_out.sort(key=lambda row: row["file"].lower())
    duplicates = [{"file": entry.label, "same_as": entry.same_as}
                  for entry in located.files if entry.same_as]
    model: dict = {
        "source": {
            "mode": located.mode,
            **({"path": located.root} if located.mode == "path" else {}),
            "searched": located.searched,
        },
        "found": {FAMILY_LABELS.get(family, family): count
                  for family, count in sorted(found.items())},
        "checks_run": wanted,
        "tolerances": tol.echo(),
        "results": results,
        "verdict": ("no_outputs" if not found else
                    "mismatches" if severities.get("warning") else "consistent"),
        "severity_counts": {"warning": severities.get("warning", 0),
                            "info": severities.get("info", 0)},
        "files": files_out,
        "findings": findings.rows,
        "not_checked": not_checked + _not_checked(located, found),
        "notes": _notes(design),
    }
    if duplicates:
        model["identical_files_skipped"] = len(duplicates)
        model["identical_examples"] = duplicates[:10]
    if located.older_generations:
        model["older_generations_skipped"] = {FAMILY_LABELS.get(family, family): count
                                              for family, count in
                                              sorted(located.older_generations.items())}
        model["generations_checked"] = located.generations_checked[:10]
    if located.zips_not_opened:
        model["zips_not_opened"] = located.zips_not_opened[:10]
        if len(located.zips_not_opened) > 10:
            model["zips_not_opened_total"] = len(located.zips_not_opened)
    if located.skipped:
        model["skipped"] = dict(located.skipped)
    if design.duplicates:
        model["board_duplicate_designators"] = sorted(design.duplicates, key=natural_key)[:10]
    return model


def _tally(results: dict, check: str, stats: dict) -> None:
    entry = results.setdefault(check, {"files": 0, "mismatches": 0})
    entry["files"] += 1
    entry["mismatches"] += stats.get("mismatches", 0)


def _not_checked(located: Located, found: Counter) -> list[dict]:
    rows = []
    if found.get("gerber"):
        rows.append({"check": "gerber", "reason": f"{found['gerber']} Gerber files found: "
                     "their copper, mask and silkscreen are not compared with the board"})
    if found.get("odb"):
        rows.append({"check": "odb", "reason": "ODB++ found: not read"})
    xls = sum(1 for entry in located.files
              if entry.kind == "workbook" and _suffix(entry.name) == ".xls" and not entry.same_as)
    if xls:
        rows.append({"check": "bom", "reason": f"{xls} .xls (BIFF) workbooks found: not read; "
                     "save the BOM as .xlsx or .csv"})
    rows.append({"check": "board_stack_report", "reason": "Board Stack Reports are not read "
                 "(their layout comes from an editable template); IPC-2581 carries the "
                 "stackup"})
    return rows


def _notes(design: Design) -> list[str]:
    notes = [
        "Read from the saved files: save the PcbDoc and sheets in Altium first. A mismatch "
        "says the file and the saved design differ, not which one is right.",
        "Pick-and-place Center-X/Y is the midpoint of the part's pad centres (all pads) "
        "relative to the board origin, as Altium writes it; a part without pads is written "
        "at the absolute origin.",
    ]
    if design.netlist is None:
        notes.append("No netlist with schematic parts is loaded: the BOM is compared with the "
                     "PCB's parts (designators, Comment and footprint only), and Not Fitted "
                     "parts come from the designators stored in the project's variants.")
    if design.index is None:
        notes.append("No readable PcbDoc: only the BOM was checked.")
    return notes


def page_verify_outputs(model: dict, offset: int, limit: int, examples: int) -> dict:
    """One page of findings (each with at most `examples` examples); the summary, the
    results and the file list (first FILES_LISTED) come with every page."""
    offset = max(0, int(offset or 0))
    result = {key: value for key, value in model.items() if key not in ("findings", "notes")}
    files = model["files"]
    if len(files) > FILES_LISTED:
        result["files"] = files[:FILES_LISTED]
        result["files_total"] = len(files)
    page = []
    for finding in model["findings"][offset:offset + limit]:
        finding = dict(finding)
        shown = finding.get("examples") or []
        if len(shown) > examples:
            finding["examples"] = shown[:examples]
            finding["examples_shown"] = examples
        if not finding.get("examples"):
            finding.pop("examples", None)
        page.append(finding)
    result["findings"] = page
    result["findings_total"] = len(model["findings"])
    result["offset"] = offset
    result["has_more"] = offset + limit < len(model["findings"])
    if result["has_more"]:
        result["next_offset"] = offset + limit
    result["notes"] = model["notes"]
    return result
