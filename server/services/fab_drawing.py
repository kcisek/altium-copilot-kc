"""Draftsman fabrication and assembly drawings (.PCBDwf) checked offline: each drawing of
the project classified from its content (fabrication, assembly or other), its notes,
free text, tables, document parameters and layer notes scanned for the items a
fabricator or assembler needs, and what the drawing states or caches cross-checked
against the design.

Checklist (fabrication): IPC class (IPC-6012/6013, IPC-A-600), laminate material, Tg,
flammability (UL 94), surface finish, solder mask and silkscreen colours, board
thickness and its tolerance, copper weights, impedance (a table, or a note with a
tolerance, where the stack has impedance profiles), drill table, registration and
tolerances, electrical (netlist) test and, on a stack with flex regions, the rigid-flex
items. Assembly: IPC-A-610 / J-STD-001 class, a BOM table, views of both sides when
parts are on both. Each item is found (with the note line quoted briefly and where it
is), missing (with a suggested note line using the design's own numbers where known) or
not applicable.

Cross-checks: thickness and layer count stated against the layer stack, materials named
against the stack's dielectrics, copper weights stated against the stack, impedance
targets in the notes and the impedance table's lines against the Layer Stack Manager's
profiles, the drill table against the board's holes, the layer stack legend against the
stack, the BOM table's variant against the OutJob and the active variant and its parts
against the BOM (only with a netlist), the board the drawing was made from and whether
the board was saved after the drawing's last update, and notes that contradict each
other (two finishes, thicknesses, IPC classes or colours).

Draftsman tables are not live: the drill table and the layer stack legend come from the
PcbDoc and the Layer Stack Manager, the BOM table from the compiled project (or the
board) through ActiveBOM, all as they were at the last Import Changes. Notes are typed
text.

Severity: error is a stated number that contradicts the design (board thickness, layer
count); warning is a missing must-have item (surface finish, thickness, material, IPC
class), a table cached from an older design, a drawing made from another board or saved
before the board, or notes that contradict each other or the design (laminate, copper
weight, impedance targets the Layer Stack Manager has other profiles for); info is worth
a look (e.g. impedance targets with no profiles in the stack to confirm them)."""
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath

try:
    from parsers.pcb_dwf import DraftsmanDoc
    from services.bom import natural_key
    from services.clone_status import GitError, _git, _has_git_dir
except ImportError:
    from server.parsers.pcb_dwf import DraftsmanDoc
    from server.services.bom import natural_key
    from server.services.clone_status import GitError, _git, _has_git_dir

CHECKS = (
    "unreadable", "thickness", "layer_count", "required_items", "contradiction",
    "materials", "copper_weight", "impedance", "drill_table", "layer_stack_legend",
    "bom_variant", "bom_parts", "source_pcb", "stale", "other_items",
)
_CHECK_RANK = {check: rank for rank, check in enumerate(CHECKS)}
_SEVERITY_RANK = {"error": 0, "warning": 1, "info": 2}
EXAMPLE_LIMIT = 25
QUOTE_CHARS = 110
NO_VARIATIONS = "[No Variations]"
MM_PER_MIL = 0.0254
OZ_MM = 1.4 * MM_PER_MIL  # 1 oz copper, as check_dfm counts it
# A board saved this much after the drawing's last update makes the drawing stale;
# closer times are one save writing both.
STALE_TOLERANCE_S = 120
DEFAULT_THICKNESS_TOLERANCE = 0.10  # IPC-6012's usual +/-10% when a note states none
_SIZE_TOLERANCE_MM = 0.0026  # drill sizes and trace widths: 0.1 mil, the stored rounding

_KB = "https://www.altium.com/documentation/knowledge-base/altium-designer/"
KB = {
    "bom_variant": _KB + "how-to-control-the-draftsman-output-variant-for-project-release",
}

FIXES = {
    "unreadable": "Open the drawing in Draftsman and save it again (File > Save).",
    "import_changes": (
        "Open the drawing in Draftsman and run Tools > Import Changes From <the PcbDoc>: the "
        "drill table, layer stack legend, impedance and BOM tables refresh from the board, "
        "the Layer Stack Manager and the BOM. Save, then generate the drawing again."),
    "source_pcb": (
        "Open the drawing, select nothing and set Properties > Source to this project's "
        "PcbDoc, run Tools > Import Changes and save."),
    "add_notes": (
        "Add the suggested lines to the drawing's fabrication notes (select the Note, add "
        "an entry in Properties) with the values agreed with the fabricator, then save."),
    "add_assembly_notes": (
        "Add the suggested lines or tables to the assembly drawing (a Note entry, Place > "
        "Bill of Materials, an assembly view per side), then save."),
    "contradiction": (
        "Keep one value: edit or delete the conflicting note lines, template text or "
        "document parameters so the drawing states it once."),
    "stack_numbers": (
        "Make the note agree with the Layer Stack Manager, or correct the stack if the "
        "note is right: the fabricator builds to the note, while the impedance solutions "
        "and the drill spans come from the stack."),
    "materials": (
        "Set the dielectric materials in the Layer Stack Manager to the laminate the notes "
        "name (its Dk sets the impedance widths), or correct the note."),
    "impedance_notes": (
        "Make the impedance note match the Layer Stack Manager's profiles (targets and "
        "tolerances), or place an impedance table, which reads them from the stack."),
    "bom_variant": (
        "Select the BOM table and set its Variation to 'Document' so it follows the "
        "drawing, and set the drawing's variant (nothing selected, Properties > Source > "
        "Variants) to the one the OutJob releases (the KB's fix)."),
}

ASSEMBLY_WORDS = re.compile(
    r"IPC[-\s]?A[-\s]?610|J[-\s]?STD[-\s]?00[12]|solder\s+paste|reflow|conformal|"
    r"assembl|populat|\bDNP\b|do\s+not\s+(?:fit|place|mount)|torque|\bESD\b|"
    r"pick\s*(?:and|&)\s*place|wave\s+solder|no[-\s]clean|flux|polarity|component\s+side",
    re.I)


# ---------- inputs ----------

@dataclass
class BoardFacts:
    """What the checks need from the project's (first) PcbDoc."""
    pcb_doc: str                      # file name
    path: str
    saved: float | None = None        # file modification time (UTC seconds)
    legacy: bool = False              # no v9 layer stack: thicknesses unknown
    copper_layers: int = 0
    total_mm: float | None = None     # all stack layers, solder mask included
    laminate_mm: float | None = None  # outer copper to outer copper
    copper_mm: list[tuple[str, float]] = field(default_factory=list)  # (layer, thickness)
    materials: list[str] = field(default_factory=list)  # dielectrics between outer coppers
    substacks: int = 0
    flex: bool = False
    impedance: list[dict] = field(default_factory=list)  # per profile layer
    drills: list[dict] | None = None  # [{type, size_mm, plated, slot_length_mm, count}]
    parts: dict[str, int] = field(default_factory=dict)  # placed parts per side
    embedded_boards: int = 0          # a panel: its boards' copper and holes are not read

    def summary(self) -> dict:
        row: dict = {"pcb_doc": self.pcb_doc, "copper_layers": self.copper_layers}
        if self.total_mm:
            row["stack_total_mm"] = round(self.total_mm, 3)
        if self.laminate_mm:
            row["copper_to_copper_mm"] = round(self.laminate_mm, 3)
        weights = sorted({_oz(t) for _, t in self.copper_mm})
        if weights:
            row["copper_oz"] = weights
        if self.materials:
            row["dielectric_materials"] = sorted(set(self.materials))
        if self.substacks > 1:
            row["substacks"] = self.substacks
        row["flex"] = self.flex
        targets = sorted({(r["target_ohm"], r["differential"]) for r in self.impedance
                          if r["target_ohm"]})
        if targets:
            row["impedance_targets_ohm"] = [
                f"{t:g} {'differential' if d else 'single-ended'}" for t, d in targets]
        if self.legacy:
            row["stack"] = "legacy layer stack: no thicknesses or materials"
        if self.embedded_boards:
            row["embedded_boards"] = self.embedded_boards
        return row


def board_facts(index, drills: list[dict] | None, path: str) -> BoardFacts:
    """BoardFacts of a PcbIndex; drills is check_dfm's drill table (review_dfm's
    "drills": kind, hole_mil, slot_length_mil, plated, count) or None."""
    board = index.pcb.board
    facts = BoardFacts(pcb_doc=PureWindowsPath(path).name, path=path,
                       legacy=board.stackup_source != "v9")
    try:
        facts.saved = Path(path).stat().st_mtime
    except OSError:
        facts.saved = None
    stack = sorted((layer for layer in board.layers
                    if layer.stack_order is not None and layer.kind != "non-stack"),
                   key=lambda layer: layer.stack_order)
    coppers = [n for n, layer in enumerate(stack) if layer.kind == "copper"]
    facts.copper_layers = len(coppers) or sum(1 for layer in board.layers
                                              if layer.kind == "copper")
    if not facts.legacy and coppers:
        heights = [layer.copper_thick_mil if layer.kind == "copper" else layer.diel_height_mil
                   for layer in stack]
        if all(h is not None for h in heights):
            facts.total_mm = sum(heights) * MM_PER_MIL
            inner = heights[coppers[0]: coppers[-1] + 1]
            facts.laminate_mm = sum(inner) * MM_PER_MIL
        facts.copper_mm = [(layer.name, layer.copper_thick_mil * MM_PER_MIL)
                           for layer in stack if layer.kind == "copper" and layer.copper_thick_mil]
        facts.materials = [layer.material for layer in stack[coppers[0]: coppers[-1] + 1]
                           if layer.kind == "dielectric" and layer.material]
    facts.substacks = len(board.substacks)
    facts.flex = any(substack.get("is_flex") for substack in board.substacks)
    facts.embedded_boards = getattr(index.pcb, "embedded_boards", 0) or 0
    names = {layer.v7_id: layer.name for layer in board.layers if layer.v7_id is not None}
    substacks = {s["id"].upper(): s["name"] for s in board.substacks}
    for profile in board.impedance_profiles:
        for entry in profile.layers:
            if not entry.enabled:
                continue
            facts.impedance.append({
                "profile": profile.name, "differential": profile.differential,
                "target_ohm": profile.target_ohm, "tolerance_pct": profile.tolerance_pct,
                "layer": names.get(entry.layer_v7, f"layer {entry.layer_v7}"),
                "width_mm": _round(entry.width_mil * MM_PER_MIL) if entry.width_mil else None,
                "gap_mm": (_round(entry.gap_mil * MM_PER_MIL)
                           if profile.differential and entry.gap_mil else None),
                "calculated_ohm": entry.calculated_ohm,
                "substack": substacks.get((entry.substack_id or "").upper()),
            })
    if drills is not None:
        facts.drills = [{
            "type": "Via" if row["kind"] == "via" else "Pad",
            "size_mm": round(row["hole_mil"] * MM_PER_MIL, 4),
            "plated": bool(row["plated"]),
            "slot_length_mm": (round(row["slot_length_mil"] * MM_PER_MIL, 4)
                               if row.get("slot_length_mil") else None),
            "count": row["count"],
        } for row in drills]
    try:
        for row in index.db.execute("SELECT side, COUNT(*) c FROM components GROUP BY side"):
            if row["side"]:
                facts.parts[str(row["side"]).lower()] = row["c"]
    except Exception:  # an index without the components table: sides unknown
        facts.parts = {}
    return facts


def file_history(path: str) -> dict | None:
    """When a file last changed: {"time", "basis", "commit"}. In a Git clone a file with
    no change since its last commit has the commit's time (a checkout writes every file,
    so its modification time says nothing), else the file's modification time. None
    when the file is not on disk."""
    file = Path(path)
    try:
        mtime = file.stat().st_mtime
    except OSError:
        return None
    plain = {"time": mtime, "basis": "file time", "commit": None}
    git = shutil.which("git")
    if not git or not _has_git_dir(file.parent):
        return plain
    try:
        status = _git(git, file.parent, "status", "--porcelain", "--", file.name, check=False)
        if status is None:
            return plain
        if status.strip():
            return {**plain, "basis": "file time (changed since its last commit)"}
        log = _git(git, file.parent, "log", "-1", "--format=%H %ct", "--", file.name,
                   check=False)
    except GitError:
        return plain
    if not log or len(log.split()) != 2:
        return plain
    sha, seconds = log.split()
    return {"time": float(seconds), "basis": "last commit", "commit": sha}


# ---------- the model ----------

def build_fab_drawing(project: dict, read_drawing, read_board, outputs: list[dict],
                      active_variant: str | None, bom=None,
                      document: str | None = None, history=file_history) -> dict:
    """The fab-drawing model of the loaded project, or {"error": ...}.

    read_drawing(path) returns a DraftsmanDoc (parse_pcb_dwf, cached). read_board(path)
    returns (BoardFacts, None) for a PcbDoc of the project, or (None, why not); each
    drawing is checked against the project's PcbDoc it was made from (its source_pcb, by
    path, then by file name), else the first. outputs: output_jobs.draftsman_outputs.
    bom(variant) -> {"variant", "parts": {designator: comment}, "not_fitted": {designator:
    comment}} for the fitted and not-fitted parts of a variant ({"error": ...} for an
    unknown one), or None without a netlist. document (a
    drawing's file name or stem) narrows the drawings checked. history(path) says when a
    file last changed (file_history; None: unknown), for the stale check."""
    paths = list(project.get("pcb_dwf_paths") or [])
    if document:
        wanted = document.strip().lower()
        chosen = [p for p in paths if PureWindowsPath(p).name.lower() == wanted
                  or PureWindowsPath(p).stem.lower() == wanted]
        if not chosen:
            return {"error": "document_not_found",
                    "message": f"'{document}' is not a Draftsman document of this project.",
                    "drawings": [PureWindowsPath(p).name for p in paths]}
        paths = chosen
    not_checked: list[dict] = []
    pcbs = list(project.get("pcb_doc_paths") or [])
    boards: dict[str, tuple[BoardFacts | None, str | None]] = {}

    def board_for(doc: DraftsmanDoc) -> tuple[BoardFacts | None, str | None]:
        if not pcbs:
            return None, "the project lists no PcbDoc"
        source = doc.source_pcb or ""
        path = next((p for p in pcbs if source and _same_file(source, p)), None) or next(
            (p for p in pcbs if source and PureWindowsPath(p).name.lower()
             == PureWindowsPath(source).name.lower()), None) or pcbs[0]
        if path not in boards:
            try:
                boards[path] = read_board(path)
            except Exception as error:  # one board failing leaves its cross-checks undone
                boards[path] = (None, f"{type(error).__name__}: {error}")
        return boards[path]

    if bom is None:
        not_checked.append({"check": "bom_parts",
                            "reason": "no netlist is loaded: the BOM table's parts are not "
                                      "compared with the BOM (open the project in Altium "
                                      "Designer once, then set_project_dir)"})
    findings: list[dict] = []
    details = []
    for path in paths:
        name = PureWindowsPath(path).name  # a project's paths are Windows paths
        try:
            doc = read_drawing(path)
        except Exception as error:
            reason = error.strerror if isinstance(error, OSError) and error.strerror else str(error)
            reason = reason.replace(path, name)
            findings.append(_finding("unreadable", "error", name,
                                     f"The drawing could not be read ({type(error).__name__}: "
                                     f"{reason}).", [], FIXES["unreadable"]))
            details.append({"drawing": name, "error": f"{type(error).__name__}: {reason}"})
            continue
        board, board_error = board_for(doc)
        if board is None:
            not_checked.append({
                "check": "design cross-checks", "drawing": name,
                "reason": f"no board to compare with ({board_error}): the thickness, layer "
                          "count, material, copper, impedance, drill table, legend and stale "
                          "checks need the PcbDoc"})
        drawing = _Drawing(name, path, doc, project, board, outputs, active_variant, bom,
                           history)
        drawing.run()
        findings.extend(drawing.findings)
        details.append(drawing.detail())
        not_checked.extend(drawing.not_checked)
    findings.sort(key=lambda f: (_SEVERITY_RANK[f["severity"]], _CHECK_RANK[f["check"]],
                                 f["drawing"]))
    kinds: dict[str, int] = {}
    for row in details:
        if "kind" in row:
            kinds[row["kind"]] = kinds.get(row["kind"], 0) + 1
    result: dict = {
        "project": project.get("name"),
        "drawings_total": len(details),
        "drawing_kinds": kinds,
        "boards": [facts.summary() if facts is not None else
                   {"pcb_doc": PureWindowsPath(path).name, "error": error}
                   for path, (facts, error) in boards.items()],
        "active_variant": active_variant,
        "summary": _summary(findings),
        "findings": findings,
        "not_checked": not_checked,
        "notes": [
            "Notes, text, tables, document parameters and layer notes are scanned with "
            "keyword patterns: quote each item's found lines, and read the drawing "
            "(get_design_notes) before calling an item missing that is worded unusually.",
            "Draftsman tables are not live: the drill table, layer stack legend and "
            "impedance table are the board's and the Layer Stack Manager's at the drawing's "
            "last Import Changes, the BOM table the BOM's; the cross-checks compare those "
            "caches and the notes with the design as saved now.",
            "Thickness is compared with the layer stack's total (solder mask included) and "
            "its copper-to-copper height, within the stated tolerance (10% when none is "
            "stated); copper weights at 1 oz = 1.4 mil, as check_dfm counts them.",
            "Suggested note lines use the design's numbers where the files give them; "
            "<...> marks a value to agree with the fabricator or assembler, never a guess.",
            "When the board last changed: in a Git clone, a board with no change since its "
            "last commit has that commit's time (the latest it can have been saved; a "
            "board committed together with the drawing is not counted as newer), else the "
            "file's modification time (the copy or install time for copied or installed "
            f"files); a board changed less than {STALE_TOLERANCE_S} s after the drawing's "
            "last update is not counted as newer.",
        ],
        "details": details,
    }
    if not paths:
        result["message"] = "The project lists no Draftsman (.PCBDwf) documents."
    return result


def page_fab_drawing(model: dict, offset: int, limit: int, example_limit: int) -> dict:
    """One page of drawing details; findings (each with at most example_limit examples),
    not_checked and notes go with the first page."""
    offset = max(0, int(offset or 0))
    details = model["details"]
    if offset:
        keep = ("project", "drawings_total", "drawing_kinds")
        result = {key: model[key] for key in keep if key in model}
    else:
        result = {key: value for key, value in model.items() if key != "details"}
        page = []
        for finding in model["findings"]:
            finding = dict(finding)
            if len(finding["examples"]) > example_limit:
                finding["examples"] = finding["examples"][:example_limit]
                finding["examples_shown"] = example_limit
            page.append(finding)
        result["findings"] = page
    result["offset"] = offset
    result["has_more"] = offset + limit < len(details)
    if result["has_more"]:
        result["next_offset"] = offset + limit
    result["details"] = details[offset:offset + limit]
    return result


def _finding(check: str, severity: str, drawing: str, issue: str, examples: list[dict],
             fix: str, **fields) -> dict:
    finding = {"check": check, "severity": severity, "drawing": drawing,
               "count": len(examples) or 1, "issue": issue}
    finding.update(fields)
    finding["examples"] = examples
    finding["fix"] = fix
    if check in KB:
        finding["source"] = KB[check]
    return finding


def _summary(findings: list[dict]) -> dict:
    summary = {level: sum(1 for f in findings if f["severity"] == level)
               for level in ("error", "warning", "info")}
    by_check: dict[str, int] = {}
    for finding in findings:
        by_check[finding["check"]] = by_check.get(finding["check"], 0) + 1
    summary["by_check"] = by_check
    return summary


# ---------- text lines ----------

@dataclass(eq=False)
class NoteLine:
    text: str      # whitespace collapsed
    where: str
    source: str    # note | text | table | parameter | layer_note


def note_lines(doc: DraftsmanDoc) -> list[NoteLine]:
    lines: list[NoteLine] = []

    def add(text: str | None, where: str, source: str) -> None:
        text = " ".join((text or "").split())
        if text:
            lines.append(NoteLine(text, where, source))

    for name, value in doc.parameters.items():
        add(f"{name}: {value}", f"document parameter {name}", "parameter")
    for item in doc.items:
        page = f"page {item.page}"
        if item.kind == "note":
            title = f" '{_short(item.title, 30)}'" if item.title else ""
            add(item.title, f"{page}, note{title} title", "note")
            for n, text in enumerate(item.lines, start=1):
                add(text, f"{page}, note{title} line {n}", "note")
        elif item.kind == "text":
            add(item.text, f"{page}, text" + (" (template)" if item.from_template else ""),
                "text")
        elif item.kind == "table":
            title = f" '{_short(item.title, 30)}'" if item.title else ""
            add(item.title, f"{page}, table{title} title", "table")
            for n, row in enumerate(item.rows, start=1):
                add(" | ".join(cell for cell in row if cell), f"{page}, table{title} row {n}",
                    "table")
    for layer in doc.layer_notes:
        text = "; ".join(v for k, v in layer.items() if k in ("comment", "note"))
        add(text, f"layer stack note ({layer['layer']})", "layer_note")
    return lines


def _short(text: str | None, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit - 3].rstrip() + "..."


def _quote(line: NoteLine, match: re.Match | None) -> dict:
    text = line.text
    if len(text) > QUOTE_CHARS:
        start = match.start() if match else 0
        lo = max(0, min(start - QUOTE_CHARS // 3, len(text) - QUOTE_CHARS))
        text = ("..." if lo else "") + text[lo:lo + QUOTE_CHARS].strip() + (
            "..." if lo + QUOTE_CHARS < len(text) else "")
    return {"text": text, "where": line.where}


def _number(text: str) -> float:
    return float(text.replace(",", "."))


def _round(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(value, digits)


def _oz(thickness_mm: float) -> float:
    """Copper thickness to the nearest quarter ounce, as weights are quoted."""
    return max(0.25, round(thickness_mm / OZ_MM * 4) / 4)


# ---------- patterns ----------

_NUM = r"(\d+(?:[.,]\d+)?|[.,]\d+)"
_PM = r"(?:\+\s*/\s*[-−]|±|\+-|\+\s*/\s*-)"
_IPC_FAB_SPEC = re.compile(r"\bIPC[-\s]?(?:6012|6013|6018|6011|A[-\s]?600)\w*", re.I)
_IPC_ASSEMBLY_SPEC = re.compile(r"\bIPC[-\s]?A[-\s]?610\w*|\bJ[-\s]?STD[-\s]?001\w*", re.I)
_IPC_ANY = re.compile(r"\bIPC\b", re.I)
_ANY_SPEC = re.compile(r"\bIPC[-\s]?(?:[A-Z]{1,3}[-\s]?)?\d{3,4}\w*|\bJ[-\s]?STD[-\s]?\d{3}\w*",
                       re.I)
_CLASS = re.compile(r"\bclass\s*[:=-]?\s*(?:([123])(?![0-9])|(III|II|I)\b)", re.I)
_ROMAN = {"I": "1", "II": "2", "III": "3"}

_MATERIAL = re.compile(
    r"\b(FR[-\s]?4(?:\.\d)?|FR[-\s]?5|FR[-\s]?408\w*|CEM[-\s]?[13]|polyimide|kapton|PTFE|teflon|"
    r"rogers|RO\s?[34]\d{3}\w*|RT\s?/?\s?duroid\w*|isola|\d{3}HR\b|I[-\s]?Speed|I[-\s]?Tera|"
    r"tachyon|astra|megtron\s?\d*|panasonic|TU-?\d{3}\w*|IT-?\d{3}\w*|shengyi|"
    r"S1\d{3}\w*|nan\s?ya|NP-?\d{3}\w*|arlon|taconic|nelco|N4\d{3}\w*|ventec|"
    r"VT-?\d{2,3}\w*|EM-?\d{3}\w*|IPC[-\s]?4101[A-Z]?(?:\s*/\s*\d+)?)(?!\w)", re.I)
# A hyphen may follow a name (FR4-TG170, S1000-2, N4000-13): the token ends before it.
_MATERIAL_CONTEXT = re.compile(r"laminate|material|base|core|substrate|dielectric|prepreg",
                               re.I)
_MATERIAL_LOOSE = re.compile(r"\b(aluminium|aluminum|ceramic|IMS)\b", re.I)
_TG = re.compile(r"\bTg(?![a-z])[^0-9a-z]{0,6}(?:(?:min(?:imum)?|of|is)\.?[^0-9a-z]{0,4})?"
                 r"(\d{3})?|glass\s+transition|high[-\s]?Tg|\bIPC[-\s]?4101[A-Z]?\s*/\s*\d+", re.I)
_FLAME = re.compile(r"\bUL\s*-?\s*94|\b94\s*-?\s*V\s*-?\s*[0-2]\b|flammab|\bflame\b|"
                    r"\bUL\s*-?\s*796", re.I)
_FINISHES = (
    ("ENEPIG", re.compile(r"\bENEPIG\b|electroless\s+nickel[^.;]{0,40}palladium", re.I)),
    ("ENIG", re.compile(r"\bENIG\b|electroless\s+nickel|immersion\s+gold|\bNi\s*/\s*Au\b|"
                        r"chemical\s+(?:nickel\s+)?gold", re.I)),
    ("HASL", re.compile(r"\b(?:LF[-\s]?)?HASL\b|\bHAL\b|hot\s+air\s+(?:solder\s+)?level", re.I)),
    ("ImAg", re.compile(r"\bIm{1,2}[-\s]?Ag\b|\bIAg\b|immersion\s+silver|chemical\s+silver",
                        re.I)),
    ("ImSn", re.compile(r"\bIm{1,2}[-\s]?Sn\b|\bISn\b|immersion\s+tin|chemical\s+tin|"
                        r"white\s+tin", re.I)),
    ("OSP", re.compile(r"\bOSP\b|organic\s+solderability|\bentek\b", re.I)),
    ("hard gold", re.compile(r"hard\s+gold|electrolytic\s+gold|electroplated\s+gold|"
                             r"gold\s+fingers?|\bhard\s+Au\b", re.I)),
)
_LEAD_FREE = re.compile(r"\bLF[-\s]?HASL|\bHASL[-\s(]*LF\b|lead[-\s]?free|\bPb[-\s]?free",
                        re.I)
# "ENIG, no HASL" and "HASL is not acceptable" name a finish that is not wanted.
_NEGATED_BEFORE = re.compile(r"\b(?:no|not|non|without|instead\s+of|except|excluding|avoid)"
                             r"[-\s]+$", re.I)
_NEGATED_AFTER = re.compile(r"\W{0,3}(?:is\s+|are\s+)?(?:not\s+(?:allowed|permitted|acceptable|"
                            r"accepted|to\s+be\s+used)|prohibited|forbidden)", re.I)
# "Keep silkscreen clear of pads" is no colour.
_COLOUR = re.compile(r"\b(green|blue|red|black|white|yellow|purple|violet|orange|"
                     r"clear(?!\s+(?:of|from|off|to|away|around)\b)|transparent|grey|gray|"
                     r"amber)\b", re.I)
_MASK = re.compile(r"solder\s*-?\s*(?:mask|resist)|soldermask|\bLPI\b|\bmask\b|\bS/M\b", re.I)
_OTHER_MASK = re.compile(r"\b(?:peel\w*|paste|stencil|carbon|temporary)[-\s]+(?:\w+[-\s]+){0,2}$",
                         re.I)
_SILK = re.compile(r"silk\s*-?\s*screen|silkscreen|\blegend\b|nomenclature|marking\s+ink|"
                   r"\bink\b|component\s+print|\boverlay\b", re.I)
_THICKNESS = re.compile(r"\bthickness\b|\bthk\b|\bthick\b", re.I)
_BOARD_WORDS = re.compile(r"\b(?:board|pcb|pwb|finished|overall|total|final|nominal)\b", re.I)
_NOT_BOARD = re.compile(r"ohm|Ω|\b[OoОо]m\b|impedance|copper|\bcu\b|mask|silk|plating|"
                        r"gold|nickel|coating|trace|width|\boz\b|foil|stiffener|coverlay|"
                        r"dielectric|prepreg|core\b", re.I)
LENGTH = re.compile(_NUM + r"\s*(mm|mils?|thou|µm|um\b|microns?|in(?:ch(?:es)?)?\b|\"|'')?",
                     re.I)
# A length right before "thick" ("1.6 mm thick"), its unit required.
_LENGTH_BEFORE = re.compile(_NUM + r"\s*(mm|mils?|thou|µm|um|microns?|in(?:ch(?:es)?)?|\"|'')"
                            r"\s*-?\s*$", re.I)
# What may stand between "thickness" and a number without a unit for the number to be
# the thickness ("PCB thickness: 62", not "thickness per the stack on sheet 2").
_BARE_GAP = re.compile(r"(?:[\s:=()~≈-]|nom(?:inal)?\.?|approx(?:imately)?\.?|is|of)*", re.I)
_WORD = re.compile(r"[^\W\d_]{3,}")
# Words that may stand between a thickness and its own tolerance.
_VALUE_WORDS = {"nom", "nominal", "thick", "thk", "total", "finished", "overall", "approx",
                "and", "with", "mil", "mils", "inch", "inches", "thou", "microns"}
MAX_WORDS = re.compile(r"\bmax(?:imum)?\b|≤|<|\bup\s+to\b|\bnot\s+(?:to\s+)?exceed|"
                        r"\bat\s+most\b", re.I)
_MIN_WORDS = re.compile(r"\bmin(?:imum)?\b|≥|>|\bat\s+least\b", re.I)
_TOLERANCE = re.compile(_PM + r"\s*" + _NUM + r"\s*(%|mm|mils?|in\b|\")?|"
                        r"tol(?:erance)?\.?\s*:?\s*" + _PM + r"?\s*" + _NUM
                        + r"\s*(%|mm|mils?|in\b|\")", re.I)
_LAYERS = re.compile(r"\b(\d{1,2})\s*-?\s*(?:layers?|lyrs?|L)\b|"
                     r"\b(?:layer\s*count|number\s+of\s+(?:copper\s+)?layers)\s*[:=]?\s*(\d{1,2})\b|"
                     r"\b(two|four|six|eight|ten|twelve)[-\s]layers?\b|\b(double)[-\s]sided\b",
                     re.I)
_LAYER_WORDS = {"two": 2, "four": 4, "six": 6, "eight": 8, "ten": 10, "twelve": 12, "double": 2}
_LAYER_PART_BEFORE = re.compile(r"\b(?:inner|outer|internal|external|plane|signal|power|ground|"
                                r"flexible)\s*$|(?<!rigid-)(?<!rigid\s)\bflex\s*$", re.I)
_LAYER_PART_AFTER = re.compile(r"\s*-?\s*(?:flex(?:ible)?\b|rigid\b(?![-\s]*flex)|region\b|"
                               r"section\b|zone\b|portion\b|area\b)", re.I)
_DOUBLE_SIDED_PARTS = re.compile(r"assembl|populat|reflow|placement|\bSMT\b|\bSMD\b|"
                                 r"\bcomponents?\b|\bparts?\b|\bmount", re.I)
_NOT_LAYER_COUNT = re.compile(r"mask|silk|paste|coat|tape|overlay|prepreg|plies|ply\b|"
                              r"legend|label", re.I)
_OZ = re.compile(r"(\d+(?:[.,]\d+)?|½|1/2|1/3|1/4)\s*-?\s*oz\b", re.I)
_COPPER = re.compile(r"\b(?:finished|base|outer|inner)\s+copper\b|copper\s+(?:weight|"
                     r"thickness)|\boz\.?\s*(?:copper|cu)\b|\d\s*(?:µm|um|microns?)\s*"
                     r"(?:copper|cu)\b", re.I)
_IMPEDANCE = re.compile(r"impedance|\bZ0\b|\bZdiff\b|\bZo\b", re.I)
_OHM = re.compile(r"(\d{2,3}(?:[.,]\d+)?)\s*-?\s*(?:ohms?|Ω|[OoОо]m)\b", re.I)
_TOLERANCE_WORD = re.compile(_PM + r"|toleran", re.I)
_IMPEDANCE_CONTEXT = re.compile(r"single[-\s]?ended|differential|\bdiff\b|\bSE\b|trace|"
                                r"stripline|microstrip|coplanar|toleran|" + _PM, re.I)
_DRILL_TABLE_WORDS = re.compile(r"\bdrill\b|\bhole\s+(?:size|table|chart)", re.I)
_REGISTRATION = re.compile(r"registration|front[-\s]to[-\s]back|layer[-\s]to[-\s]layer", re.I)
_TOLERANCE_CONTEXT = re.compile(r"hole|drill|position|location|dimension|outline|profile|"
                                r"edge|datum|rout|linear|angular|unless\s+otherwise|bow|twist",
                                re.I)
_TOLERANCE_ANY = re.compile(r"toleran|\btol\b|" + _PM, re.I)
_DIGIT = re.compile(r"\d")
_NETLIST_TEST = re.compile(
    r"IPC[-\s]?D[-\s]?356|net\s*list\s+test|electrical(?:ly)?\s+test|\bE[-\s]?test|"
    r"continuity|isolation\s+test|flying\s+probe|bed[-\s]of[-\s]nails|"
    r"100\s*%\s*(?:electrical|net|tested)", re.I)
_FLEX_ITEMS = (
    ("IPC-6013", re.compile(r"\bIPC[-\s]?6013\w*", re.I)),
    ("coverlay", re.compile(r"cover\s*-?\s*lay|cover\s*-?\s*film|coverfilm", re.I)),
    ("stiffener", re.compile(r"stiffener", re.I)),
    ("bend", re.compile(r"\bbend\w*|flex(?:ible)?\s+(?:area|region|section|zone)|"
                        r"dynamic\s+flex|static\s+flex", re.I)),
    ("flex material", re.compile(r"polyimide|kapton|adhesive(?:less)?", re.I)),
)


def _tolerance_parts(match: re.Match) -> tuple[float, str | None]:
    """(amount, unit or None) of a _TOLERANCE match."""
    if match.group(1):
        return _number(match.group(1)), match.group(2)
    return _number(match.group(3)), match.group(4)


def length_mm(value: float, unit: str | None) -> tuple[float, bool]:
    """(mm, unit assumed): a bare number below 0.3 reads as inches, up to 10 as mm,
    above as mil."""
    unit = (unit or "").lower()
    if unit.startswith("mm"):
        return value, False
    if unit in ("µm", "um") or unit.startswith("micron"):
        return value / 1000, False
    if unit.startswith("mil") or unit == "thou":
        return value * MM_PER_MIL, False
    if unit.startswith("in") or unit in ('"', "''"):
        return value * 25.4, False
    if value < 0.3:
        return value * 25.4, True
    if value <= 10:
        return value, True
    return value * MM_PER_MIL, True


# ---------- one drawing ----------

class _Drawing:
    def __init__(self, name: str, path: str, doc: DraftsmanDoc, project: dict,
                 board: BoardFacts | None, outputs: list[dict], active_variant: str | None,
                 bom, history=None):
        self.name = name
        self.path = path
        self.doc = doc
        self.project = project
        self.board = board
        self.active_variant = active_variant
        self.bom = bom
        self.history = history
        self.outputs = [o for o in outputs if o.get("drawing") and _same_file(o["drawing"], path)]
        self.lines = note_lines(doc)
        self.views = [item for item in doc.items if item.kind == "view"]
        self.view_kinds = {item.view for item in self.views}
        self.findings: list[dict] = []
        self.not_checked: list[dict] = []
        self.checklist: list[dict] = []
        self.cross: dict[str, dict] = {}
        self.kind, self.also, self.signals = self.classify()
        # Values the notes state, for the cross-checks and contradictions.
        # {mm, tol (mm or None), assumed (unit), bound (None, "max", "min"), line, match}
        self.thickness_values: list[dict] = []
        self.finishes: dict[str, list[tuple[NoteLine, re.Match]]] = {}
        self.classes: dict[str, list[tuple[NoteLine, re.Match]]] = {}
        self.colours: dict[str, dict[str, list[tuple[NoteLine, re.Match]]]] = {"mask": {},
                                                                             "silkscreen": {}}

    # --- classification ---

    def classify(self) -> tuple[str, list[str], dict]:
        fab = asm = 0
        for item in self.views:
            if item.view in ("fabrication view", "drill table", "drill drawing",
                             "layer stack legend", "impedance table"):
                fab += 2
            elif item.view in ("assembly view", "bill of materials"):
                asm += 2
        fab_words = (_IPC_FAB_SPEC, _MATERIAL, _FLAME, _MASK, _NETLIST_TEST, _THICKNESS,
                     _IMPEDANCE, _REGISTRATION, *(p for _, p in _FINISHES))
        for line in self.lines:
            if line.source == "parameter":
                continue
            if any(p.search(line.text) for p in fab_words):
                fab += 1
            if ASSEMBLY_WORDS.search(line.text):
                asm += 1
        signals = {"fabrication": fab, "assembly": asm}
        if not fab and not asm:
            return "other", [], signals
        kind, other = ("fabrication", "assembly") if fab >= asm else ("assembly", "fabrication")
        also = [other] if min(fab, asm) >= 4 else []
        return kind, also, signals

    # --- checklist ---

    def item(self, item: str, status: str, required: bool, hits=(), detail: str | None = None,
             suggested: str | None = None, kind: str = "fabrication") -> dict:
        row: dict = {"item": item, "status": status}
        if required:
            row["required"] = True
        if hits:
            row["found"] = [_quote(line, match) for line, match in hits[:3]]
            if len(hits) > 3:
                row["found_total"] = len(hits)
        if detail:
            row["detail"] = detail
        if status == "missing" and suggested:
            row["suggested_note"] = suggested
        row["_kind"] = kind
        self.checklist.append(row)
        return row

    def search(self, pattern: re.Pattern, lines=None, require=None, exclude=None):
        hits = []
        for line in self.lines if lines is None else lines:
            if require is not None and not require.search(line.text):
                continue
            if exclude is not None and exclude.search(line.text):
                continue
            match = pattern.search(line.text)
            if match:
                hits.append((line, match))
        return hits

    def legend(self) -> list[dict]:
        """The cached stack rows when the drawing prints a layer stack legend."""
        return self.doc.layer_stack if "layer stack legend" in self.view_kinds else []

    def run(self) -> None:
        kinds = {self.kind, *self.also}
        if "fabrication" in kinds:
            self.fabrication_checklist()
        if "assembly" in kinds:
            self.assembly_checklist()
        self.required_findings()
        if "fabrication" in kinds:
            self.check_thickness()
            self.check_layer_count()
            self.check_materials()
            self.check_copper()
            self.check_impedance()
        self.check_contradictions()
        self.check_drill_table()
        self.check_legend()
        self.check_bom()
        self.check_source()

    def fabrication_checklist(self) -> None:
        board = self.board
        flex = bool(board and board.flex) or self.doc.flex
        # IPC class
        hits, classes = [], {}
        for line in self.lines:
            spec = _IPC_FAB_SPEC.search(line.text)
            klass = None
            if spec or (_IPC_ANY.search(line.text) and not _IPC_ASSEMBLY_SPEC.search(line.text)):
                klass = _spec_class(line.text, _IPC_FAB_SPEC)
            if spec or klass:
                hits.append((line, spec or klass))
            if klass:
                value = klass.group(1) or _ROMAN[klass.group(2).upper()]
                classes.setdefault(value, []).append((line, klass))
        self.classes = classes
        detail = (f"class {', '.join(sorted(classes))}" if classes else
                  "an IPC specification is named without a class" if hits else None)
        spec = "IPC-6013" if flex else "IPC-6012"
        self.item("ipc_class", "found" if classes else "missing", True, hits, detail,
                  f"Fabricate and inspect to {spec} Class <2 or 3> and accept to IPC-A-600 "
                  "of the same class (latest revisions).")
        # Laminate material
        hits = self.search(_MATERIAL)
        hits += [h for h in self.search(_MATERIAL_LOOSE, require=_MATERIAL_CONTEXT)
                 if h[0] not in {line for line, _ in hits}]
        legend_materials = sorted({row["material"] for row in self.legend()
                                   if _is_dielectric(row) and row.get("material")})
        stack = sorted(set(board.materials)) if board and board.materials else []
        detail = None
        if not hits and legend_materials:
            detail = ("named only in the layer stack legend: " + ", ".join(legend_materials)
                      + " (the Layer Stack Manager's dielectric materials)")
        laminate = ", ".join(stack) if stack else "<laminate>"
        self.item("material", "found" if hits or legend_materials else "missing", True, hits,
                  detail, f"Laminate: {laminate} per IPC-4101/<slash sheet>, or an equivalent "
                  "agreed with the fabricator.")
        # Tg
        hits = self.search(_TG)
        legend_tg = sorted({row["tg"] for row in self.legend() if row.get("tg")})
        detail = None
        if hits and all(not m.group(1) and "4101" not in m.group(0) for _, m in hits):
            detail = "Tg is mentioned without a value"
        if not hits and legend_tg:
            detail = ("only in the layer stack legend: Tg " + ", ".join(legend_tg)
                      + " (the stack's value; check the legend shows that column)")
        self.item("tg", "found" if hits or legend_tg else "missing", False, hits, detail,
                  "Laminate Tg >= <value> degC (per the IPC-4101 slash sheet).")
        # Flammability
        hits = self.search(_FLAME)
        self.item("flammability", "found" if hits else "missing", False, hits, None,
                  "Flammability rating UL 94 <V-0>.")
        # Surface finish
        hits, finishes = [], {}
        for line in self.lines:
            line_finishes = []
            for family, pattern in _FINISHES:
                match = next((m for m in pattern.finditer(line.text) if not _negated(line.text, m)),
                             None)
                if not match:
                    continue
                if family == "ENIG" and "ENEPIG" in line_finishes:
                    continue
                label = family
                if family == "HASL":
                    label = "LF-HASL" if _LEAD_FREE.search(line.text) else "HASL"
                line_finishes.append(family)
                finishes.setdefault(label, []).append((line, match))
                hits.append((line, match))
        self.finishes = finishes
        detail = ", ".join(sorted(finishes)) if finishes else (
            "no finish is named: an unspecified finish is often built as HASL (tin-lead) "
            "or immersion silver")
        self.item("surface_finish", "found" if finishes else "missing", True, hits, detail,
                  "Surface finish: <ENIG per IPC-4552 / immersion silver / LF-HASL>, both "
                  "sides.")
        # Mask and silkscreen colours
        mask_hits, silk_hits = self.colour_hits()
        for item, keyword, hits, suggestion in (
            ("mask_colour", _MASK, mask_hits,
             "Solder mask: LPI per IPC-SM-840, <colour>, both sides."),
            ("silkscreen_colour", _SILK, silk_hits,
             "Silkscreen: <colour> non-conductive epoxy ink, both sides; no ink on exposed "
             "copper."),
        ):
            named = self.search(keyword) if not hits else []
            detail = None
            if not hits and named:
                detail = "named without a colour"
            elif hits:
                kind = "mask" if item == "mask_colour" else "silkscreen"
                detail = ", ".join(sorted(self.colours[kind]))
            self.item(item, "found" if hits else "missing", False, hits or named, detail,
                      suggestion)
        # Thickness and tolerance
        hits, tolerances = [], []
        for line in self.lines:
            keyword = self.board_thickness_word(line)
            if keyword is None:
                continue
            start = keyword.end()
            value = LENGTH.search(line.text, start, start + 40)
            gap = line.text[start:value.start()] if value else ""
            if value and _TOLERANCE_ANY.search(gap):
                # "Thickness tolerance: +/- 10%": the tolerance of a thickness stated
                # elsewhere, not a thickness.
                tolerance = _TOLERANCE.search(line.text, start, start + 60)
                if tolerance:
                    amount, unit = _tolerance_parts(tolerance)
                    if unit == "%":
                        tolerances.append(("%", amount))
                    elif unit:
                        tolerances.append(("mm", length_mm(amount, unit)[0]))
                    hits.append((line, tolerance))
                continue
            if value and not value.group(2) and not _BARE_GAP.fullmatch(gap):
                value = None  # "thickness per the stack on sheet 2": no value of its own
            if not value:  # "1.6 mm thick"
                value = _LENGTH_BEFORE.search(line.text, max(0, keyword.start() - 15),
                                              keyword.start())
            if not value:
                hits.append((line, keyword))
                continue
            stated = _number(value.group(1))
            tolerance = _TOLERANCE.search(line.text, value.end(), value.end() + 40)
            if tolerance and any(w.group().lower() not in _VALUE_WORDS for w in _WORD.finditer(
                    line.text, value.end(), tolerance.start())):
                tolerance = None  # "1.6 mm, hole tolerance +/- 0.05 mm": another tolerance
            amount, unit = _tolerance_parts(tolerance) if tolerance else (None, None)
            # "1.6 +/- 0.16 mm": the tolerance's unit is the value's.
            mm, assumed = length_mm(stated, value.group(2) or (
                unit if unit and unit != "%" else None))
            if not 0.1 <= mm <= 10:
                continue
            tol_mm = None
            if tolerance:
                if unit == "%":
                    tol_mm = mm * amount / 100
                elif unit:
                    tol_mm = length_mm(amount, unit)[0]
                else:  # no unit: the value's own
                    tol_mm = amount * mm / stated
            lead = line.text[max(0, min(keyword.start(), value.start()) - 20):
                             max(keyword.end(), value.start())]
            bound = "max" if MAX_WORDS.search(lead) else "min" if _MIN_WORDS.search(lead) else None
            # Without a unit the number may be mm, inches or mil: the check takes the
            # reading nearest the stack (a tolerance in % or without a unit scales with it).
            readings = sorted({round(v, 4) for v in (stated, stated * 25.4, stated * MM_PER_MIL)
                               if 0.1 <= v <= 10}) if assumed else [mm]
            self.thickness_values.append({"mm": mm, "tol": tol_mm, "assumed": assumed,
                                          "readings": readings,
                                          "tol_scales": unit is None or unit == "%",
                                          "bound": bound, "line": line, "match": value})
            hits.append((line, value))
        stated = self.thickness_values
        if tolerances:
            kind, amount = tolerances[0]
            for row in stated:
                if row["tol"] is None and row["bound"] is None:
                    row["tol"] = row["mm"] * amount / 100 if kind == "%" else amount
        detail = None
        if stated and all(row["tol"] is None for row in stated):
            detail = "no tolerance is stated"
        elif hits and not stated:
            detail = "thickness is mentioned without a value"
        # A rigid-flex stack's total is no region's thickness.
        total = board.total_mm if board and board.substacks <= 1 else None
        number = f"{total:.2f} mm" if total else "<thickness> mm"
        source = " (the layer stack's total)" if total else ""
        self.item("thickness", "found" if stated else "missing", True, hits, detail,
                  f"Finished board thickness {number} +/- <tolerance, e.g. 10%>{source}.")
        # Copper weights
        hits = self.search(_OZ) + [h for h in self.search(_COPPER)
                                   if not _OZ.search(h[0].text)]
        legend_weights = sorted({row["weight"] for row in self.legend() if row.get("weight")})
        detail = None
        if not hits and legend_weights:
            detail = "only in the layer stack legend: " + ", ".join(legend_weights)
        self.item("copper_weight", "found" if hits or legend_weights else "missing", False,
                  hits, detail, self.copper_suggestion())
        # Impedance
        profiles = board.impedance if board else []
        table = "impedance table" in self.view_kinds or any(
            item.kind == "table" and any(_IMPEDANCE.search(t) or _OHM.search(t)
                                         for t in [item.title or ""] + [" ".join(r) for r in item.rows])
            for item in self.doc.items)
        notes = self.impedance_lines()
        applicable = bool(profiles) or bool(notes) or table or board is None
        if not applicable:
            self.item("impedance", "not_applicable", False, (),
                      "the Layer Stack Manager has no impedance profiles")
        else:
            detail = None
            if table:
                detail = "an impedance table is placed"
            elif notes and not any(_TOLERANCE_WORD.search(line.text) for line, _ in notes):
                detail = "impedance is noted without a tolerance"
            self.item("impedance", "found" if table or notes else "missing", False, notes,
                      detail, self.impedance_suggestion())
        # Drill table
        drill = sorted(self.view_kinds & {"drill table", "drill drawing"})
        tables = [item for item in self.doc.items if item.kind == "table" and
                  _DRILL_TABLE_WORDS.search(item.title or "")]
        detail = None
        if drill or tables:
            detail = ", ".join(drill + [f"table '{_short(t.title, 30)}'" for t in tables])
        self.item("drill_table", "found" if drill or tables else "missing", False, (), detail,
                  "Hole sizes are finished sizes, per the drill table; plated holes +/- <tol> "
                  "mm, non-plated +/- <tol> mm.")
        # Registration and tolerances
        hits = self.search(_REGISTRATION)
        for line in self.lines:
            if line in {h[0] for h in hits}:
                continue
            match = _TOLERANCE_ANY.search(line.text)
            if match and _TOLERANCE_CONTEXT.search(line.text) and _DIGIT.search(line.text) \
                    and not _IMPEDANCE.search(line.text) and not _THICKNESS.search(line.text):
                hits.append((line, match))
        self.item("registration_tolerances", "found" if hits else "missing", False, hits, None,
                  "Unless otherwise specified: profile tolerance +/- <tol> mm, hole position "
                  "+/- <tol> mm, layer-to-layer registration per IPC-6012.")
        # Electrical test
        hits = self.search(_NETLIST_TEST)
        self.item("netlist_test", "found" if hits else "missing", False, hits, None,
                  "100% electrical test (continuity and isolation) against the IPC-D-356 "
                  "netlist supplied with the data.")
        # Rigid-flex
        if not flex:
            self.item("rigid_flex", "not_applicable", False, (),
                      "the layer stack has no flex region")
        else:
            hits, found = [], []
            for label, pattern in _FLEX_ITEMS:
                these = self.search(pattern)
                if these:
                    found.append(label)
                    hits += these[:1]
            missing = [label for label, _ in _FLEX_ITEMS if label not in found]
            detail = ("found: " + ", ".join(found) + ("; not found: " + ", ".join(missing)
                                                      if missing else "")) if found else None
            self.item("rigid_flex", "found" if found else "missing", False, hits, detail,
                      "Rigid-flex per IPC-6013 Class <2 or 3>: flex core <polyimide, "
                      "adhesiveless>, coverlay <type>, stiffeners as drawn, minimum bend "
                      "radius <value> in the bend areas.")

    @staticmethod
    def board_thickness_word(line: NoteLine) -> re.Match | None:
        """The "thickness" of a line stating the board's thickness: not a line about
        impedance (trace thickness), and not one preceded by what else has a thickness
        (copper, mask, plating, a dielectric ...); in a long line, preceded by a board
        word (board, PCB, finished, total ...)."""
        if _IMPEDANCE.search(line.text) or _OHM.search(line.text):
            return None
        for keyword in _THICKNESS.finditer(line.text):
            before = line.text[max(0, keyword.start() - 25):keyword.start()]
            if _NOT_BOARD.search(before):
                continue
            if _BOARD_WORDS.search(before) or line.source == "parameter" or \
                    keyword.start() < 3 or len(line.text) <= 60:
                return keyword
        return None

    def impedance_lines(self) -> list[tuple[NoteLine, re.Match]]:
        """Lines about controlled impedance: naming it, or giving ohm values with a line
        type or tolerance, or inside a note that names it (a resistor value elsewhere is
        not)."""
        notes = {_note_key(line.where) for line in self.lines if _IMPEDANCE.search(line.text)}
        hits = []
        for line in self.lines:
            match = _IMPEDANCE.search(line.text)
            if match is None:
                ohm = _OHM.search(line.text)
                if ohm and (_IMPEDANCE_CONTEXT.search(line.text)
                            or _note_key(line.where) in notes):
                    match = ohm
            if match:
                hits.append((line, match))
        return hits

    def colour_hits(self):
        """(mask hits, silkscreen hits): a colour belongs to the nearest mask or silkscreen
        word before it in the line (within 150 characters), else the one right after it."""
        mask_hits, silk_hits = [], []
        for line in self.lines:
            # A peelable (blue) mask or a paste mask is no solder mask colour.
            masks = [m for m in _MASK.finditer(line.text) if not _OTHER_MASK.search(
                line.text, max(0, m.start() - 15), m.start())]
            words = sorted([(m.start(), "mask", m) for m in masks]
                           + [(m.start(), "silkscreen", m) for m in _SILK.finditer(line.text)],
                           key=lambda w: w[0])
            if not words:
                continue
            seen: set[str] = set()
            for colour in _COLOUR.finditer(line.text):
                before = [w for w in words if w[0] < colour.start()
                          and colour.start() - w[0] <= 150]
                after = [w for w in words if w[0] > colour.start() and w[0] - colour.end() <= 40]
                # "white silkscreen": a word right after the colour names what it colours.
                if after and after[0][0] - colour.end() <= 15:
                    owner = after[0]
                else:
                    owner = before[-1] if before else (after[0] if after else None)
                if owner is None:
                    continue
                kind = owner[1]
                self.colours[kind].setdefault(colour.group(1).lower(), []).append((line, colour))
                if kind not in seen:
                    (mask_hits if kind == "mask" else silk_hits).append((line, owner[2]))
                    seen.add(kind)
        return mask_hits, silk_hits

    def copper_suggestion(self) -> str:
        board = self.board
        if not board or not board.copper_mm:
            return "Copper weights: outer <oz> finished, inner <oz> (per the layer stack)."
        weights = [_oz(t) for _, t in board.copper_mm]
        outer = sorted({weights[0], weights[-1]})
        inner = sorted(set(weights[1:-1]))
        text = "Copper weights per the layer stack: outer " + "/".join(f"{w:g}" for w in outer) + " oz"
        if inner:
            text += ", inner " + "/".join(f"{w:g}" for w in inner) + " oz"
        return text + f" ({board.copper_layers} copper layers)."

    def impedance_suggestion(self) -> str:
        profiles = self.board.impedance if self.board else []
        targets = sorted({(p["target_ohm"], p["differential"], p["tolerance_pct"])
                          for p in profiles if p["target_ohm"]})
        if not targets:
            return ("Controlled impedance per the impedance table: <target> ohm +/- <tol>%; the "
                    "fabricator may adjust trace widths and dielectric heights to meet it.")
        listed = ", ".join(f"{t:g} ohm {'differential' if d else 'single-ended'}"
                           + (f" +/-{tol:g}%" if tol is not None else "")
                           for t, d, tol in targets)
        return (f"Controlled impedance: {listed} (the Layer Stack Manager's profiles, per the "
                "impedance table); the fabricator may adjust trace widths and dielectric "
                "heights to meet them.")

    def assembly_checklist(self) -> None:
        hits, classes = [], set()
        for line in self.lines:
            spec = _IPC_ASSEMBLY_SPEC.search(line.text)
            if spec:
                hits.append((line, spec))
                klass = _spec_class(line.text, _IPC_ASSEMBLY_SPEC)
                if klass:
                    classes.add(klass.group(1) or _ROMAN[klass.group(2).upper()])
        detail = (f"class {', '.join(sorted(classes))}" if classes else
                  "named without a class" if hits else None)
        self.item("assembly_standard", "found" if classes else "missing", False, hits, detail,
                  "Assemble and inspect to IPC-A-610 Class <2 or 3>; soldering per J-STD-001.",
                  kind="assembly")
        boms = [item for item in self.views if item.view == "bill of materials"]
        self.item("bom_table", "found" if boms else "missing", False, (),
                  f"{len(boms)} BOM table(s)" if boms else None,
                  "Parts per the Bill of Materials table (variant <name>).", kind="assembly")
        sides = {(item.side or "").lower() for item in self.views if item.view == "assembly view"}
        parts = self.board.parts if self.board else {}
        needed = sorted(side for side in ("top", "bottom") if parts.get(side))
        if not parts:
            status = "found" if sides else "missing"
            detail = "views: " + ", ".join(sorted(s for s in sides if s)) if sides else None
        else:
            missing = [side for side in needed if side not in sides]
            status = "missing" if missing else "found"
            detail = (f"parts on {' and '.join(needed)}; no assembly view of the "
                      f"{' and '.join(missing)} side" if missing else
                      "views: " + ", ".join(sorted(s for s in sides if s)))
        self.item("assembly_views", status, False, (), detail,
                  "Assembly views of the " + " and ".join(needed or ["top"]) + " side"
                  + ("s" if len(needed) > 1 else "") + " (Place > Board Assembly View).",
                  kind="assembly")

    def required_findings(self) -> None:
        required = [row for row in self.checklist if row["status"] == "missing" and row.get("required")]
        other = [row for row in self.checklist if row["status"] == "missing" and not row.get("required")]
        if required:
            self.findings.append(_finding(
                "required_items", "warning", self.name,
                f"The {self.kind} drawing does not state {', '.join(r['item'] for r in required)}: "
                "the fabricator has to ask, or builds its default.",
                [{"item": r["item"], "detail": r.get("detail"),
                  "suggested_note": r.get("suggested_note")} for r in required],
                FIXES["add_notes"]))
        if other:
            assembly = all(r["_kind"] == "assembly" for r in other)
            self.findings.append(_finding(
                "other_items", "info", self.name,
                "Items a drawing usually states that this one does not: "
                + ", ".join(r["item"] for r in other) + ".",
                [{"item": r["item"], "detail": r.get("detail"),
                  "suggested_note": r.get("suggested_note")} for r in other],
                FIXES["add_assembly_notes" if assembly else "add_notes"]))

    # --- cross-checks with the design ---

    def board_ready(self, check: str) -> bool:
        if self.board is None:
            self.cross[check] = {"result": "not_checked", "reason": "no readable PcbDoc"}
            return False
        if self.board.legacy:
            self.cross[check] = {"result": "not_checked",
                                 "reason": "legacy layer stack: no thicknesses or materials"}
            return False
        return True

    def check_thickness(self) -> None:
        if not self.thickness_values:
            return
        if not self.board_ready("thickness") or not self.board.total_mm:
            return
        if self.board.substacks > 1:
            self.cross["thickness"] = {"result": "not_checked",
                                       "reason": "several substacks (rigid-flex): the rigid "
                                                 "stack's height is not worked out"}
            return
        board = self.board
        results, wrong = [], []
        for value in self.thickness_values:
            mm, tol, bound = value["mm"], value["tol"], value["bound"]
            candidates = [v for v in (board.total_mm, board.laminate_mm) if v]
            if len(value["readings"]) > 1:  # no unit: the reading nearest the stack
                mm = min(value["readings"], key=lambda v: min(abs(v - c) for c in candidates))
                if tol is not None and value["tol_scales"]:
                    tol = tol * mm / value["mm"]
            row = {"stated_mm": round(mm, 3), "tolerance_mm": _round(tol, 3),
                   "stack_total_mm": round(board.total_mm, 3),
                   "copper_to_copper_mm": _round(board.laminate_mm, 3),
                   "quote": _quote(value["line"], value["match"])}
            if bound:
                row["bound"] = bound  # a maximum or minimum, not the nominal thickness
            if value["assumed"]:
                row["unit_assumed"] = True
            if bound == "max":
                ok = min(candidates) <= mm + 0.005
            elif bound == "min":
                ok = max(candidates) >= mm - 0.005
            else:
                allowed = tol if tol is not None else mm * DEFAULT_THICKNESS_TOLERANCE
                ok = min(abs(v - mm) for v in candidates) <= allowed + 0.005
            row["result"] = "match" if ok else "mismatch"
            results.append(row)
            if row["result"] == "mismatch":
                wrong.append(row)
        self.cross["thickness"] = {"result": "mismatch" if wrong else "match",
                                   "stated": results}
        if wrong:
            self.findings.append(_finding(
                "thickness", "error", self.name,
                "The drawing states a board thickness the layer stack does not build: "
                f"the stack totals {board.total_mm:.3f} mm ({_fmt(board.laminate_mm)} mm "
                "copper to copper).", wrong, FIXES["stack_numbers"]))

    def check_layer_count(self) -> None:
        stated = []
        for line in self.lines:
            if _NOT_LAYER_COUNT.search(line.text):
                continue
            for match in _LAYERS.finditer(line.text):
                # "inner 2 layers", "2 layer flex section": part of the stack, not its count;
                # "double-sided assembly": parts on both sides.
                if _LAYER_PART_BEFORE.search(line.text, max(0, match.start() - 15), match.start()) \
                        or _LAYER_PART_AFTER.match(line.text, match.end()) \
                        or (match.group(4) and _DOUBLE_SIDED_PARTS.search(line.text)):
                    continue
                if match.group(1) or match.group(2):
                    count = int(match.group(1) or match.group(2))
                else:
                    count = _LAYER_WORDS[(match.group(3) or match.group(4)).lower()]
                if 1 <= count <= 64:
                    stated.append((count, line, match))
        if not stated or self.board is None:
            if stated:
                self.cross["layer_count"] = {"result": "not_checked", "reason": "no readable PcbDoc"}
            return
        copper = self.board.copper_layers
        wrong = [{"stated": count, "copper_layers": copper, "quote": _quote(line, match)}
                 for count, line, match in stated if count != copper]
        self.cross["layer_count"] = {"result": "mismatch" if wrong else "match",
                                     "stated": sorted({c for c, _, _ in stated}),
                                     "copper_layers": copper}
        if wrong:
            self.findings.append(_finding(
                "layer_count", "error", self.name,
                f"The drawing states a layer count other than the stack's {copper} copper "
                "layers.", wrong, FIXES["stack_numbers"]))

    def check_materials(self) -> None:
        named: dict[str, tuple[NoteLine, re.Match]] = {}
        for line in self.lines:
            for match in _MATERIAL.finditer(line.text):
                token = match.group(1)
                if re.match(r"IPC", token, re.I):
                    continue  # a specification, not a material name
                named.setdefault(_norm(token), (line, match))
        if not named:
            return
        if not self.board_ready("materials") or not self.board.materials:
            if self.board is not None and not self.board.legacy:
                self.cross["materials"] = {"result": "not_checked",
                                           "reason": "the stack names no dielectric material"}
            return
        stack = sorted(set(self.board.materials))
        unmatched = [{"named": match.group(1), "stack_materials": stack,
                      "quote": _quote(line, match)}
                     for key, (line, match) in named.items()
                     if not any(_material_matches(key, _norm(m)) for m in stack)]
        self.cross["materials"] = {"result": "mismatch" if unmatched else "match",
                                   "named": sorted({m.group(1) for _, m in named.values()}),
                                   "stack_materials": stack}
        if unmatched:
            default = all(_norm(m) == "FR4" for m in stack)
            issue = ("The notes name a laminate the layer stack does not use"
                     + (": the stack keeps Altium's default dielectric FR-4, so its impedance "
                        "solutions use FR-4's Dk, not the named laminate's." if default
                        else "."))
            self.findings.append(_finding("materials", "warning", self.name, issue, unmatched,
                                          FIXES["materials"]))

    def check_copper(self) -> None:
        stated = []
        for line, _ in self.search(_OZ):
            for m in _OZ.finditer(line.text):
                raw = m.group(1)
                value = 0.5 if raw in ("½", "1/2") else (1 / 3 if raw == "1/3" else (
                    0.25 if raw == "1/4" else _number(raw)))
                stated.append((value, line, m))
        if not stated or not self.board_ready("copper_weight") or not self.board.copper_mm:
            return
        weights = sorted({_oz(t) for _, t in self.board.copper_mm})
        # Outer layers are often quoted finished (base plus plating): within 1 oz is no
        # contradiction.
        wrong = [{"stated_oz": round(v, 2), "stack_oz": weights, "quote": _quote(line, m)}
                 for v, line, m in stated if not any(abs(v - w) <= 1.0 for w in weights)]
        self.cross["copper_weight"] = {"result": "mismatch" if wrong else "match",
                                       "stated_oz": sorted({round(v, 2) for v, _, _ in stated}),
                                       "stack_oz": weights}
        if wrong:
            self.findings.append(_finding(
                "copper_weight", "warning", self.name,
                "The notes state a copper weight more than 1 oz from any of the stack's "
                f"copper layers ({', '.join(f'{w:g}' for w in weights)} oz).", wrong,
                FIXES["stack_numbers"]))

    def check_impedance(self) -> None:
        if self.board is None:
            return
        profiles = self.board.impedance
        targets = sorted({p["target_ohm"] for p in profiles if p["target_ohm"]})
        # Targets in the notes
        stated = []
        for line, _ in self.impedance_lines():
            for match in _OHM.finditer(line.text):
                stated.append((_number(match.group(1)), line, match))
        result: dict = {}
        if stated:
            unknown, listed = [], set()
            for v, line, m in stated:
                if v in listed or any(abs(v - t) <= 1.0 for t in targets):
                    continue
                listed.add(v)
                unknown.append({"stated_ohm": v, "lsm_targets_ohm": targets,
                                "quote": _quote(line, m)})
            unstated = [t for t in targets if not any(abs(v - t) <= 1.0 for v, _, _ in stated)]
            result["notes"] = {"stated_ohm": sorted({v for v, _, _ in stated}),
                               "lsm_targets_ohm": targets}
            if unstated:
                result["notes"]["lsm_targets_not_in_notes"] = unstated
            if unknown and not targets:
                # Nothing in the design to contradict: the widths were not solved in Altium.
                result["notes"]["result"] = "not_checked"
                self.findings.append(_finding(
                    "impedance", "info", self.name,
                    "The notes state impedance targets, but the Layer Stack Manager has no "
                    "impedance profiles to confirm them against.", unknown,
                    FIXES["impedance_notes"]))
            elif unknown:
                self.findings.append(_finding(
                    "impedance", "warning", self.name,
                    "The notes state impedance targets the Layer Stack Manager has no profile "
                    "for.", unknown, FIXES["impedance_notes"]))
            elif unstated and "impedance table" not in self.view_kinds:
                self.findings.append(_finding(
                    "impedance", "info", self.name,
                    "Layer Stack Manager impedance targets the notes do not mention.",
                    [{"lsm_target_ohm": t} for t in unstated], FIXES["impedance_notes"]))
        # The impedance table's cached lines
        if "impedance table" in self.view_kinds:
            result["table"] = self.compare_impedance_table(profiles)
        if result:
            mismatch = any(r.get("result") == "mismatch" for r in result.values()) or any(
                f["check"] == "impedance" and f["severity"] == "warning" for f in self.findings)
            unchecked = all(r.get("result") == "not_checked" for r in result.values())
            self.cross["impedance"] = {
                "result": "mismatch" if mismatch else "not_checked" if unchecked else "match",
                **result}

    def compare_impedance_table(self, profiles: list[dict]) -> dict:
        lines = self.doc.impedance_lines
        visible = set()
        for item in self.views:
            if item.view == "impedance table":
                visible |= {str(i) for i in item.options.get("visible_lines", [])}
        if visible:
            lines = [line for line in lines if line["id"] in visible]
        if not lines:
            self.findings.append(_finding(
                "impedance", "warning", self.name,
                "The impedance table has no cached lines: it prints empty or out of date.",
                [], FIXES["import_changes"]))
            return {"result": "mismatch", "reason": "no cached lines"}
        unmatched, changed = [], []
        used = set()
        for line in lines:
            differential = "coupled" in (line["type"] or "").lower()
            candidates = [(n, p) for n, p in enumerate(profiles)
                          if p["layer"].lower() == (line["layer"] or "").lower()
                          and p["differential"] == differential and p["target_ohm"]
                          and line["target_ohm"] is not None
                          and abs(p["target_ohm"] - line["target_ohm"]) <= 0.5]
            row = {"layer": line["layer"], "type": line["type"], "target_ohm": line["target_ohm"],
                   "table_width_mm": line["width_mm"]}
            if differential and line.get("gap_mm"):
                row["table_gap_mm"] = line["gap_mm"]
            if not candidates:
                unmatched.append(row)
                continue
            n, profile = min(candidates, key=lambda c: abs((c[1]["width_mm"] or 0)
                                                           - (line["width_mm"] or 0)))
            used.add(n)
            width_off = (profile["width_mm"] is not None and line["width_mm"] is not None
                         and abs(profile["width_mm"] - line["width_mm"]) > _SIZE_TOLERANCE_MM)
            gap_off = (differential and profile["gap_mm"] is not None and line.get("gap_mm")
                       and abs(profile["gap_mm"] - line["gap_mm"]) > _SIZE_TOLERANCE_MM)
            if width_off or gap_off:
                row.update(profile=profile["profile"], lsm_width_mm=profile["width_mm"])
                if differential:
                    row["lsm_gap_mm"] = profile["gap_mm"]
                changed.append(row)
        missing = [] if visible else [
            {"profile": p["profile"], "layer": p["layer"], "target_ohm": p["target_ohm"]}
            for n, p in enumerate(profiles) if n not in used]
        if unmatched or changed or missing:
            examples = ([dict(r, status="not in the Layer Stack Manager") for r in unmatched]
                        + [dict(r, status="width or gap changed") for r in changed]
                        + [dict(r, status="not in the table") for r in missing])
            self.findings.append(_finding(
                "impedance", "warning", self.name,
                "The impedance table's cached lines differ from the Layer Stack Manager's "
                "profiles: the table is from an older stack.", examples,
                FIXES["import_changes"]))
        return {"result": "mismatch" if unmatched or changed or missing else "match",
                "lines": len(lines), "lsm_profile_layers": len(profiles),
                "not_in_lsm": len(unmatched), "changed": len(changed),
                "not_in_table": len(missing)}

    def check_drill_table(self) -> None:
        if not self.view_kinds & {"drill table", "drill drawing"}:
            return
        if self.board is None or self.board.drills is None:
            self.cross["drill_table"] = {"result": "not_checked",
                                         "reason": "the board's holes could not be read"}
            return
        if self.board.embedded_boards:
            self.cross["drill_table"] = {
                "result": "not_checked",
                "reason": f"{self.board.pcb_doc} is a panel ({self.board.embedded_boards} "
                          "embedded board array(s)): the holes of its boards are not read"}
            return
        rows = [row for row in self.doc.drill_table if not row.get("backdrill")]
        if not rows:
            self.cross["drill_table"] = {"result": "mismatch", "reason": "no cached rows"}
            self.findings.append(_finding(
                "drill_table", "warning", self.name,
                "The drawing has a drill table or drill drawing but no cached holes: it "
                "prints empty or out of date.", [], FIXES["import_changes"]))
            return
        # Rows compare by type, size, plating and slot length, summed over their spans
        # (the drawing names layers its own way).
        table = _drill_sizes(rows, lambda row: "Via" if (row["type"] or "").lower() == "via"
                             else "Pad")
        holes = _drill_sizes(self.board.drills, lambda row: row["type"])
        differences = []
        for key, count in table.items():
            match = next((k for k in holes if _same_hole(k, key)), None)
            found = holes.pop(match) if match is not None else 0
            if found != count:
                differences.append({**_hole_key(key), "table_count": count, "board_count": found})
        for key, count in holes.items():
            differences.append({**_hole_key(key), "table_count": 0, "board_count": count})
        table_total = sum(r["count"] for r in rows)
        board_total = sum(r["count"] for r in self.board.drills)
        self.cross["drill_table"] = {"result": "mismatch" if differences else "match",
                                     "table_holes": table_total, "board_holes": board_total,
                                     "rows_differing": len(differences)}
        if differences:
            self.findings.append(_finding(
                "drill_table", "warning", self.name,
                f"The drill table (cached: {table_total} holes) differs from the board's "
                f"holes ({board_total}): it is from an older board.", differences,
                FIXES["import_changes"]))

    def check_legend(self) -> None:
        legend = self.legend()
        if not legend:
            return
        if not self.board_ready("layer_stack_legend"):
            return
        board = self.board
        problems = []
        copper = sum(1 for row in legend if row.get("type") in ("Signal", "Plane"))
        if copper and copper != board.copper_layers:
            problems.append({"what": "copper layers", "legend": copper,
                             "stack": board.copper_layers})
        stacks = {row.get("stack") for row in legend}
        # Like for like: the board's total counts copper, dielectrics and solder mask, not
        # the stack's other layer types (surface finish and other "Other" rows).
        total = sum(row.get("thickness_mm") or 0.0 for row in legend
                    if row.get("type") in _LEGEND_COUNTED)
        if len(stacks) == 1 and board.substacks <= 1 and total and board.total_mm and \
                abs(total - board.total_mm) > max(0.01, 0.01 * board.total_mm):
            problems.append({"what": "total thickness (mm)", "legend": round(total, 3),
                             "stack": round(board.total_mm, 3)})
        coppers = [n for n, row in enumerate(legend) if row.get("type") in ("Signal", "Plane")]
        inner = legend[coppers[0]:coppers[-1] + 1] if coppers else legend
        legend_materials = sorted({row["material"] for row in inner
                                   if _is_dielectric(row) and row.get("material")})
        stack_materials = sorted(set(board.materials))
        if legend_materials and stack_materials and \
                {_norm(m) for m in legend_materials} != {_norm(m) for m in stack_materials}:
            problems.append({"what": "dielectric materials", "legend": legend_materials,
                             "stack": stack_materials})
        self.cross["layer_stack_legend"] = {"result": "mismatch" if problems else "match",
                                            "legend_copper_layers": copper,
                                            "legend_total_mm": round(total, 3) if total else None}
        if problems:
            self.findings.append(_finding(
                "layer_stack_legend", "warning", self.name,
                "The layer stack legend (cached at the last update) differs from the board's "
                "layer stack: it prints an older stack.", problems, FIXES["import_changes"]))

    # --- BOM table ---

    def check_bom(self) -> None:
        boms = [item for item in self.views if item.view == "bill of materials"]
        if not boms:
            return
        drawing_variant = self.doc.document_variant or NO_VARIATIONS
        rows = []
        variant_examples, release_examples = [], []
        for item in boms:
            variant = (item.variant if item.variant and not item.variant.startswith("Document")
                       else drawing_variant)
            row: dict = {"page": item.page, "title": item.title, "variant": variant,
                         "data_source": item.options.get("data_source")}
            if item.variant and not item.variant.startswith("Document"):
                row["variant_from"] = "view"
            for out in self.outputs:
                if out["connected"] and not _same_variant(out["variant"], variant):
                    release_examples.append({"page": item.page, "title": item.title,
                                             "table_variant": variant,
                                             "outjob": out["outjob"], "output": out["output"],
                                             "outjob_variant": out["variant"]})
            if self.active_variant and not _same_variant(self.active_variant, variant):
                variant_examples.append({"page": item.page, "title": item.title,
                                         "table_variant": variant,
                                         "active_variant": self.active_variant})
            data = self.bom_set(item, variant)
            if data is None:
                row["cached_parts"] = 0
                rows.append(row)
                continue
            parts = {d: line["comment"] for line in data["lines"] for d in line["designators"]}
            row["cached_lines"] = len(data["lines"])
            row["cached_parts"] = sum(len(line["designators"]) for line in data["lines"])
            row["parts"] = self.compare_bom(item, variant, parts, data)
            rows.append(row)
        result = "match"
        if release_examples:
            result = "mismatch"
            self.findings.append(_finding(
                "bom_variant", "warning", self.name,
                "A BOM table shows another variant than the OutJob that releases this "
                "drawing: the released BOM table lists other parts than the OutJob's other "
                "outputs.", release_examples, FIXES["bom_variant"]))
        if variant_examples:
            self.findings.append(_finding(
                "bom_variant", "info", self.name,
                "A BOM table shows another variant than the active one.", variant_examples,
                FIXES["bom_variant"]))
        if any(r.get("parts", {}).get("result") == "mismatch" or not r.get("cached_parts")
               for r in rows):
            result = "mismatch"
        elif result == "match" and any(r.get("parts", {}).get("result") == "not_checked"
                                       for r in rows):
            result = "variant_only"  # the parts were not compared
        self.cross["bom_table"] = {"result": result, "tables": rows}

    def bom_set(self, item, variant: str) -> dict | None:
        """The cached data set a BOM table prints: same data source, view mode and
        variant (else the same data source and view mode, then the same data source)."""
        source = item.options.get("data_source")
        mode = item.options.get("view_mode")
        sets = [s for s in self.doc.bom_data if s["lines"]]

        def same(s, with_mode=True, with_variant=True):
            return ((not source or s["data_source"] == source)
                    and (not with_mode or not mode or s["view_mode"] == mode)
                    and (not with_variant or _same_variant(s["variant"] or NO_VARIATIONS, variant)))
        for args in ((True, True), (True, False), (False, False)):
            found = [s for s in sets if same(s, *args)]
            if found:
                return found[0]
        self.findings.append(_finding(
            "bom_parts", "warning", self.name,
            "A BOM table has no cached parts for its data source: it prints empty or out of "
            "date until Import Changes.",
            [{"page": item.page, "title": item.title, "data_source": source}],
            FIXES["import_changes"]))
        return None

    def compare_bom(self, item, variant: str, parts: dict[str, str], data: dict) -> dict:
        if self.bom is None:
            return {"result": "not_checked", "reason": "no netlist loaded"}
        expected = self.bom(variant)
        if expected is None or "error" in expected:
            reason = (expected or {}).get("message") or "no BOM for this variant"
            return {"result": "not_checked", "reason": reason}
        bom = expected["parts"]
        if item.options.get("show_not_fitted") or data.get("show_not_fitted"):
            # The table lists the variant's not-fitted parts too (marked not fitted).
            bom = {**expected.get("not_fitted", {}), **bom}
        only_table = sorted(set(parts) - set(bom), key=natural_key)
        only_bom = sorted(set(bom) - set(parts), key=natural_key)
        comments = sorted((d for d in set(parts) & set(bom)
                           if (parts[d] or "").strip() != (bom[d] or "").strip()),
                          key=natural_key)
        plate = item.options.get("data_source") == "Plate"
        out = {"result": "mismatch" if only_bom or comments or (only_table and not plate)
               else "match",
               "table_parts": len(parts), "bom_parts": len(bom)}
        examples = []
        if only_bom:
            out["in_bom_not_in_table"] = len(only_bom)
            examples.append({"page": item.page, "title": item.title,
                             "in_bom_not_in_table": only_bom[:20]})
        if only_table:
            out["in_table_not_in_bom"] = len(only_table)
            examples.append({"page": item.page, "title": item.title,
                             "in_table_not_in_bom": only_table[:20]})
        if comments:
            out["comment_differs"] = len(comments)
            examples.append({"page": item.page, "title": item.title, "comment_differs": [
                {"designator": d, "table": parts[d], "bom": bom[d]} for d in comments[:10]]})
        if out["result"] == "mismatch":
            self.findings.append(_finding(
                "bom_parts", "warning", self.name,
                f"The BOM table (variant {variant}, cached at the last Import Changes) lists "
                f"other parts than the BOM: {len(parts)} parts in the table, {len(bom)} in "
                "the BOM.", examples, FIXES["import_changes"]))
        elif only_table:
            self.findings.append(_finding(
                "bom_parts", "info", self.name,
                "The BOM table reads the board (data source Plate): it lists board-only parts "
                "(fiducials, mounting holes, logos) the schematic BOM does not.", examples,
                FIXES["import_changes"]))
        return out

    # --- source board and dates ---

    def check_source(self) -> None:
        doc = self.doc
        pcbs = self.project.get("pcb_doc_paths") or []
        if doc.source_pcb and pcbs and not any(_same_file(doc.source_pcb, p) for p in pcbs):
            same_name = PureWindowsPath(doc.source_pcb).name.lower() in {
                PureWindowsPath(p).name.lower() for p in pcbs}
            self.cross["source_pcb"] = {"result": "mismatch",
                                        "source_pcb": PureWindowsPath(doc.source_pcb).name,
                                        "same_name_elsewhere": same_name}
            self.findings.append(_finding(
                "source_pcb", "info" if same_name else "warning", self.name,
                "The drawing was last updated from another PcbDoc than the project's"
                + (" (a board of the same name in another folder: a copied project or an "
                   "earlier revision)." if same_name else "."),
                [{"source_pcb": doc.source_pcb,
                  "project_pcb": self.board.pcb_doc if self.board else
                  PureWindowsPath(pcbs[0]).name}],
                FIXES["source_pcb"]))
        elif doc.source_pcb:
            self.cross["source_pcb"] = {"result": "match"}
        updated = _parse_utc(doc.last_update)
        board = self.board
        if updated is None:
            self.cross["stale"] = {"result": "not_checked",
                                   "reason": "the drawing stores no last update time"}
            return
        if board is None:
            return
        changed = self.history(board.path) if self.history else None
        if changed is None and board.saved is not None:
            changed = {"time": board.saved, "basis": "file time", "commit": None}
        if changed is None:
            return
        drawn = self.history(self.path) if self.history else None
        newer = changed["time"] - updated.timestamp()
        # Committed together: the board may have been saved before or after the update
        # in that commit; the files cannot tell.
        same_commit = bool(changed["commit"] and drawn and drawn["commit"] == changed["commit"])
        stale = newer > STALE_TOLERANCE_S and not same_commit
        cached = sorted(k for k in self.view_kinds if k in (
            "drill table", "drill drawing", "layer stack legend", "impedance table",
            "bill of materials", "fabrication view", "assembly view"))
        self.cross["stale"] = {
            "result": "stale" if stale else "current",
            "drawing_updated_utc": updated.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "board_changed_utc": datetime.fromtimestamp(changed["time"], tz=timezone.utc)
                                         .strftime("%Y-%m-%dT%H:%M:%SZ"),
            "board_changed_basis": changed["basis"],
        }
        if same_commit and newer > STALE_TOLERANCE_S:
            self.cross["stale"]["note"] = ("the board and the drawing last changed in the same "
                                           "commit: whether the board changed after the "
                                           "update cannot be told")
        if stale:
            days = newer / 86400
            basis = ("was committed" if changed["basis"] == "last commit" else "was saved")
            self.findings.append(_finding(
                "stale", "warning", self.name,
                f"The board {basis} {_age(days)} after the drawing's last update (Import "
                "Changes): its views and tables may show the board as it was then.",
                [{"drawing_updated_utc": self.cross["stale"]["drawing_updated_utc"],
                  "board_changed_utc": self.cross["stale"]["board_changed_utc"],
                  "basis": changed["basis"], "cached_views": cached}],
                FIXES["import_changes"]))

    # --- notes against each other ---

    def check_contradictions(self) -> None:
        examples = []
        families = {label for label in self.finishes if label != "hard gold"}
        if len({"HASL" if f in ("HASL", "LF-HASL") else f for f in families}) > 1:
            examples.append({"what": "surface finish", "values": sorted(families),
                             "quotes": [_quote(*hits[0]) for label, hits in
                                        sorted(self.finishes.items()) if label in families]})
        nominal = [row for row in self.thickness_values if row["bound"] is None]
        values = sorted({round(row["mm"], 2) for row in nominal})
        if values and values[-1] - values[0] > max(0.05, 0.03 * values[-1]):
            examples.append({"what": "board thickness (mm)", "values": values,
                             "quotes": [_quote(row["line"], row["match"]) for row in nominal][:4]})
        if len(self.classes) > 1:
            examples.append({"what": "IPC class", "values": sorted(self.classes),
                             "quotes": [_quote(*hits[0]) for _, hits in
                                        sorted(self.classes.items())]})
        for kind, colours in self.colours.items():
            if len(colours) > 1:
                examples.append({"what": f"{kind} colour", "values": sorted(colours),
                                 "quotes": [_quote(*hits[0]) for _, hits in
                                            sorted(colours.items())]})
        if examples:
            self.findings.append(_finding(
                "contradiction", "warning", self.name,
                "The drawing states different values for the same requirement: the "
                "fabricator has to ask which one holds.", examples, FIXES["contradiction"]))

    # --- result ---

    def detail(self) -> dict:
        doc = self.doc
        row: dict = {"drawing": self.name, "kind": self.kind}
        if self.also:
            row["also"] = self.also
        row["signals"] = self.signals
        row["pages"] = len(doc.pages)
        if doc.source_pcb:
            row["source_pcb"] = PureWindowsPath(doc.source_pcb).name
        row["checked_against"] = self.board.pcb_doc if self.board else None
        if doc.last_update:
            row["last_update_utc"] = doc.last_update[:19] + "Z"
        row["variant"] = doc.document_variant or NO_VARIATIONS
        if self.outputs:
            row["printed_by"] = [{k: o[k] for k in ("outjob", "output", "variant", "connected")}
                                 for o in self.outputs]
        views: dict[str, int] = {}
        for item in self.views:
            views[item.view] = views.get(item.view, 0) + 1
        row["views"] = views
        row["checklist"] = [{k: v for k, v in item.items() if k != "_kind"}
                            for item in self.checklist]
        row["checklist_summary"] = {
            status: sum(1 for item in self.checklist if item["status"] == status)
            for status in ("found", "missing", "not_applicable")}
        suggestions = [item["suggested_note"] for item in self.checklist
                       if item["status"] == "missing" and item.get("suggested_note")]
        if suggestions:
            row["suggested_notes"] = suggestions
        row["cross_checks"] = self.cross
        return row


# ---------- helpers ----------

_LEGEND_COUNTED = {"Signal", "Plane", "Dielectric", "SolderMask", "Core", "Prepreg"}


def _negated(text: str, match: re.Match) -> bool:
    return bool(_NEGATED_BEFORE.search(text, max(0, match.start() - 15), match.start())
                or _NEGATED_AFTER.match(text, match.end()))


def _spec_class(text: str, ours: re.Pattern) -> re.Match | None:
    """The first class in text that belongs to a specification of ours: the nearest
    specification before it (else after it) is ours, or the line names none. "Board to
    IPC-6012 Class 2, assembly to IPC-A-610 Class 3" gives the board class 2, and the
    class of another specification (IPC-SM-840 Class 3) is no board class."""
    specs = [(m.start(), m.end(), True) for m in ours.finditer(text)]
    specs += [(m.start(), m.end(), False) for m in _ANY_SPEC.finditer(text)
              if not any(s < m.end() and m.start() < e for s, e, _ in specs)]
    for klass in _CLASS.finditer(text):
        before = [s for s in specs if s[1] <= klass.start()]
        after = [s for s in specs if s[0] >= klass.end()]
        owner = (max(before, key=lambda s: s[1]) if before else
                 min(after, key=lambda s: s[0]) if after else None)
        if owner is None or owner[2]:
            return klass
    return None


def _note_key(where: str) -> str:
    """The note a line belongs to ("page 1, note 'Notes:'" of "... line 3")."""
    return where.rsplit(" line ", 1)[0].removesuffix(" title")


def _is_dielectric(row: dict) -> bool:
    return row.get("type") not in ("Signal", "Plane", "Legend", "SolderMask", "PasteMask",
                                   "Overlay", None) and (row.get("material") or "").lower() \
        not in ("", "copper", "solder resist")


def _norm(text: str) -> str:
    return re.sub(r"[\s\-_./]", "", text).upper()


_FAMILIES = {
    "ROGERS": ("RO", "ROGERS", "RT", "DUROID", "CLTE", "TMM", "KAPPA", "CUCLAD"),
    "ISOLA": ("ISOLA", "370HR", "FR408", "ISPEED", "ITERA", "TACHYON", "ASTRA", "IS4", "185HR",
              "DE104", "PCL"),
    "PANASONIC": ("MEGTRON", "PANASONIC", "R5"),
    "MEGTRON": ("MEGTRON", "R5"),
    "POLYIMIDE": ("POLYIMIDE", "KAPTON", "PI", "AP", "PYRALUX"),
    "KAPTON": ("POLYIMIDE", "KAPTON", "PI", "PYRALUX"),
    "SHENGYI": ("SHENGYI", "S1", "SY"),
    "NANYA": ("NANYA", "NP"),
    "TEFLON": ("PTFE", "TEFLON", "RO", "RT"),
    "PTFE": ("PTFE", "TEFLON", "RO", "RT"),
}


# Stack materials that are no FR-4 grade laminate; FR-4 in a note is a grade, which most
# product names in a stack (IT-180A, S1000-2, 370HR, a "Core" or "Prepreg") are.
_NOT_FR4 = ("RO", "ROGERS", "RT", "DUROID", "PTFE", "TEFLON", "POLYIMIDE", "KAPTON", "PYRALUX",
            "ALUMIN", "CERAMIC", "TACONIC", "ARLON", "CLTE", "TMM", "CEM")


def _material_matches(named: str, stack: str) -> bool:
    if named in stack or stack in named:
        return True
    if re.fullmatch(r"FR4(?:\d*)?", named):
        return not stack.startswith(_NOT_FR4)
    return any(stack.startswith(prefix) for prefix in _FAMILIES.get(named, ()))


def _drill_sizes(rows: list[dict], kind) -> dict[tuple, int]:
    """(type, size mm, plated, slot mm) -> hole count."""
    sizes: dict[tuple, int] = {}
    for row in rows:
        if row["size_mm"] is None:
            continue
        key = (kind(row), round(row["size_mm"], 3), bool(row["plated"]),
               round(row.get("slot_length_mm") or 0.0, 3))
        sizes[key] = sizes.get(key, 0) + row["count"]
    return sizes


def _same_hole(a: tuple, b: tuple) -> bool:
    return a[0] == b[0] and a[2] == b[2] and abs(a[1] - b[1]) <= _SIZE_TOLERANCE_MM and \
        abs(a[3] - b[3]) <= _SIZE_TOLERANCE_MM


def _hole_key(key: tuple) -> dict:
    out = {"type": key[0], "size_mm": key[1], "plated": key[2]}
    if key[3]:
        out["slot_length_mm"] = key[3]
    return out


def _same_file(a: str, b: str) -> bool:
    if a.replace("/", "\\").lower().rstrip("\\") == b.replace("/", "\\").lower().rstrip("\\"):
        return True
    try:
        return Path(a).resolve() == Path(b).resolve() and Path(a).exists()
    except (OSError, ValueError):
        return False


def _same_variant(a: str | None, b: str | None) -> bool:
    def norm(name: str | None) -> str:
        name = (name or "").strip()
        return NO_VARIATIONS.lower() if name.lower() in (
            "", NO_VARIATIONS.lower(), "[all variants]", "no variations", "none") else name.lower()
    return norm(a) == norm(b)


def _parse_utc(timestamp: str | None) -> datetime | None:
    if not timestamp:
        return None
    try:
        value = datetime.fromisoformat(timestamp[:19])
    except ValueError:
        return None
    if value.year < 1990:  # 0001-01-01: never updated
        return None
    return value.replace(tzinfo=timezone.utc)


def _age(days: float) -> str:
    if days >= 2:
        return f"{days:.0f} days"
    hours = days * 24
    return f"{hours:.1f} hours" if hours >= 1 else f"{hours * 60:.0f} minutes"


def _fmt(value: float | None) -> str:
    return "?" if value is None else f"{value:.3f}"
