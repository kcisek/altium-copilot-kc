"""Release readiness: one "is this ready for review or fab?" report, and the
release_review prompt.

A fixed list of gates modelled on Altium's Project Releaser validations and the
board-finalising steps: DRC and ERC clean and newer than the design, the netlist newer
than the sheets, schematic and PCB in sync, no unrouted nets, polygons repoured, board
outline, fiducials and mounting holes, Not Fitted parts left out of the assembly
outputs, the OutJob complete for every variant and its outputs current, the Draftsman
fab notes against the stackup, the title blocks, the project files, the Altium 365
copy and test access (check_testpoints, advisory), then what cannot be checked offline
(footprints against the libraries, lifecycle and compliance, teardrops, via stitching,
the team's template).

Pure composition, no MCP: each gate calls the service functions its source tool uses,
over the loaded project, its cached PCB index (the board's connectivity and DFM results
are cached on it) and the design-notes cache, and quotes the result in one short
evidence line. The tool layer passes main.py's own helpers in (ReleaseInputs), so no
check is computed twice or differently.

Statuses: pass, fail, warn (met, with something to look at), not_verified (the data
source is missing or cannot be read offline, or the check failed: never a pass) and
not_applicable. Only blocking gates fail; a non-blocking gate's problem is a warn. The
verdict follows the Releaser: one failed blocking gate fails the release; otherwise a
blocking gate not verified leaves it not_ready_unverified; pass needs every blocking
gate passed, warned or not applicable (a missing source is never not_applicable). ERC
is blocking and always not_verified, because no ERC result is saved in the project
files: the best offline verdict names it.
"""

import logging
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import cached_property
from pathlib import Path, PureWindowsPath
from typing import Any, Callable

try:
    from parsers.drc_report import RULE
    from services.clone_status import clone_status as read_clone_status
    from services.connectivity import check_connectivity as review_connectivity
    from services.design_notes import resolve_special_string
    from services.dfm import GLOBAL_FIDUCIALS, review_dfm, side_counts
    from services.dfm_assembly import outline_box, parts as assembly_parts, placements
    from services.drc_results import assess, load_report, rule_kind
    from services.fab_drawing import build_fab_drawing
    from services.fab_profiles import load_profile
    from services.output_jobs import NO_VARIATIONS, ROLES, build_output_jobs, draftsman_outputs
    from services.pcb_health import run_health_check
    from services.project_integrity import build_project_integrity
    from services.project_tour import defined_variant_count
    from services.sch_pcb_sync import net_matching_options, run_sync_check
    from services.testpoints import review_testpoints
    from services.via_checks import LARGE_HOLE_MIL
except ImportError:
    from server.parsers.drc_report import RULE
    from server.services.clone_status import clone_status as read_clone_status
    from server.services.connectivity import check_connectivity as review_connectivity
    from server.services.design_notes import resolve_special_string
    from server.services.dfm import GLOBAL_FIDUCIALS, review_dfm, side_counts
    from server.services.dfm_assembly import outline_box, parts as assembly_parts, placements
    from server.services.drc_results import assess, load_report, rule_kind
    from server.services.fab_drawing import build_fab_drawing
    from server.services.fab_profiles import load_profile
    from server.services.output_jobs import (
        NO_VARIATIONS, ROLES, build_output_jobs, draftsman_outputs,
    )
    from server.services.pcb_health import run_health_check
    from server.services.project_integrity import build_project_integrity
    from server.services.project_tour import defined_variant_count
    from server.services.sch_pcb_sync import net_matching_options, run_sync_check
    from server.services.testpoints import review_testpoints
    from server.services.via_checks import LARGE_HOLE_MIL

STATUSES = ("pass", "fail", "warn", "not_verified", "not_applicable")
VERDICTS = ("fail", "not_ready_unverified", "pass")
EVIDENCE_CHARS = 320
NAMES_LISTED = 5
MM_PER_MIL = 0.0254

# Title-block parameters, in the order tried (sheet first, then project). Date is a
# stored value here: CurrentDate and ModifiedDate are computed by Altium when drawn.
REVISION_PARAMETERS = ("Revision", "Rev")
DATE_PARAMETERS = ("Date", "ReleaseDate", "RevisionDate", "DocumentDate")

# Assembly outputs whose variant decides whether Not Fitted parts are placed.
ASSEMBLY_TYPES = ROLES["bom"] | ROLES["pick_and_place"]
ALL_VARIANTS = "[All Variants]"  # get_output_jobs reads it as [No Variations]

# get_output_jobs checks that leave the release incomplete, warnings included; any other
# warning is a warn, info is ignored.
OUTJOB_BLOCKING_CHECKS = {
    "outjob_files", "no_fabrication_data", "no_nc_drill", "no_pick_and_place",
    "no_assembly_drawing", "no_bom", "bom_pnp_variant", "assembly_drawing_variant",
    "erc_settings_differ",
}
OUTJOB_CHECK_LABELS = {
    "outjob_files": "OutJob files missing or unreadable",
    "no_fabrication_data": "no fabrication data output",
    "no_nc_drill": "no drill data",
    "no_pick_and_place": "no pick-and-place output",
    "no_assembly_drawing": "no assembly drawing",
    "no_bom": "no BOM output",
    "bom_pnp_variant": "BOM and pick-and-place on different variants",
    "assembly_drawing_variant": "a variant without an assembly drawing",
    "erc_settings_differ": "ERC levels differ from Project Options (the release fails)",
    "no_erc_validation": "no ERC validation output",
    "no_drc_validation": "no DRC validation output",
    "no_odb_ipc2581": "Gerber only for a stack that needs ODB++ or IPC-2581",
    "wrong_document": "outputs on a missing or foreign data source",
    "output_path": "output path on another drive",
    "long_path": "paths near 260 characters",
    "fabrication_origin": "Gerber and drill or ODB++ origins differ",
    "overlay_not_in_stackup": "overlay layers missing from the stack",
    "board_cutouts": "board cutouts plotted on no mechanical layer",
    "draftsman_variant": "Draftsman views on their own variant",
    "not_connected": "outputs connected to no container",
}

# get_drc_report's assessment issues, by what they do to this gate.
DRC_OTHER_BOARD = {"other_board", "pcb_file_missing"}
DRC_STALE = {"older_than_board", "eco_after_report"}
DRC_UNUSABLE = {"report_incomplete", "report_error"}
DRC_CAUTIONS = {"rule_kind_not_checked", "report_counts_differ", "report_outside_output_folder"}
# rule_kind of the rule_kind_not_checked issue for shorts (Un-Routed Net, the other one,
# is checked from the copper by the unrouted_nets gate).
SHORT_CIRCUIT_LABEL = "Short-Circuit"
# Rules6 kind name, id and the report's name for the silkscreen rules.
SILK_RULES = (("SilkToSolderMaskClearance", 54, "Silk To Solder Mask"),
              ("SilkToSilkClearance", 55, "Silk to Silk"))
ROLE_LABELS = {
    "fabrication_data": "fabrication data", "nc_drill": "drill", "pick_and_place":
    "pick-and-place", "assembly_drawing": "assembly drawing", "bom": "BOM", "erc": "ERC",
    "drc": "DRC",
}

_MOUNTING_DESIGNATOR = re.compile(r"^(?:MH|MTG)\d", re.IGNORECASE)


@dataclass
class ReleaseInputs:
    """What the gates read. pcb_index() -> (PcbIndex, None) or (None, error dict), the
    PCB session's cached index; saved_after_netlist(project, compiled) -> (sheet names,
    other file names) saved after the netlist; sheets_saved_after(project, saved) ->
    sheet names saved after a time; dfm_variant() -> dfm.AssemblyContext of the active
    variant; fab_board_reader and fab_bom: check_fab_drawing's board and BOM readers
    (fab_bom None without a netlist); clone_status(project_dir) -> get_clone_status's
    dict; offline_erc() -> run_erc's summary counts (main._offline_erc_summary) or None,
    cited by the ERC gate as evidence only; verify_outputs() -> verify_outputs' model of
    the generated files (services.verify_outputs.run_verify_outputs) or None, cited by
    the dnp_parts and outputs_current gates."""

    project: dict
    netlist: dict | None
    variant_state: Any
    notes: Any  # design_notes.DesignNotesCache
    pcb_index: Callable[[], tuple]
    saved_after_netlist: Callable[[dict, float], tuple[list[str], list[str]]]
    sheets_saved_after: Callable[[dict, float], list[str]]
    dfm_variant: Callable[[], Any]
    fab_board_reader: Callable
    fab_bom: Callable | None = None
    clone_status: Callable[[str], dict] = field(default=read_clone_status)
    offline_erc: Callable[[], dict | None] | None = None
    verify_outputs: Callable[[], dict | None] | None = None
    fab_profile_setting: str | None = None  # the user's Fab profile setting


class GateResult(Exception):
    """Raised inside a gate to answer at once (a missing source, nothing to check)."""

    def __init__(self, status: str, evidence: str):
        super().__init__(evidence)
        self.status = status
        self.evidence = evidence


@dataclass(frozen=True)
class Gate:
    id: str
    title: str
    blocking: bool
    source_tool: str | None
    run: Callable[["ReleaseContext"], tuple[str, str]]


# ---------- shared sources ----------

class ReleaseContext:
    """The sources several gates share, each read once (cached_property: a test sets
    one by assigning it)."""

    def __init__(self, inputs: ReleaseInputs):
        self.inputs = inputs
        self.project = inputs.project
        self.netlist = inputs.netlist

    @cached_property
    def pcb_path(self) -> Path | None:
        paths = self.project.get("pcb_doc_paths") or []
        return Path(paths[0]) if paths else None

    @cached_property
    def board(self) -> tuple:
        """(index, error message): both None without a PcbDoc."""
        if self.pcb_path is None:
            return None, None
        index, error = self.inputs.pcb_index()
        if error:
            return None, error.get("message") or error.get("error") or "unreadable"
        return index, None

    def require_index(self):
        """The PCB index, or GateResult: not_applicable without a PcbDoc, not_verified
        when it cannot be read."""
        if self.pcb_path is None:
            raise GateResult("not_applicable", "The project lists no PcbDoc.")
        index, error = self.board
        if index is None:
            raise GateResult("not_verified", f"The PcbDoc could not be read: {error}")
        return index

    @cached_property
    def drc(self) -> dict | None:
        """The last Batch DRC report with its assessment, or None without one."""
        if self.pcb_path is None:
            return None
        report, path, _, _, notes = load_report(self.project, self.pcb_path)
        if report is None:
            return None
        index, _ = self.board
        issues = notes + assess(report, path, self.pcb_path, self.project,
                                index.pcb if index is not None else None)
        return {"report": report, "path": path, "issues": issues}

    @cached_property
    def offline_erc(self) -> dict | None:
        """run_erc's summary counts, None when not given or the sheets do not compile."""
        if self.inputs.offline_erc is None:
            return None
        try:
            return self.inputs.offline_erc()
        except Exception:
            return None

    @cached_property
    def verified(self) -> dict | None:
        """verify_outputs' model of the generated files; None when not given, when it
        failed or when it found no files."""
        if self.inputs.verify_outputs is None:
            return None
        try:
            model = self.inputs.verify_outputs()
        except Exception as error:  # evidence only: never stops a gate
            logging.warning("check_release_readiness: verify_outputs failed: %s", error)
            return None
        if not model or "error" in model or model.get("verdict") == "no_outputs":
            return None
        return model

    def verified_codes(self, check: str, codes: tuple[str, ...]) -> int:
        """How many findings of these codes verify_outputs reported for a check."""
        model = self.verified
        if model is None:
            return 0
        return sum(finding["count"] for finding in model.get("findings") or []
                   if finding["check"] == check and finding["code"] in codes)

    @cached_property
    def outjobs(self) -> dict:
        return build_output_jobs(self.project, self.inputs.notes.draftsman)

    @cached_property
    def netlist_stale(self) -> tuple[list[str], list[str]] | None:
        """Files saved after the netlist was compiled; None when unknown."""
        compiled = (self.netlist or {}).get("net_file_mtime")
        if not compiled:
            return None
        return self.inputs.saved_after_netlist(self.project, compiled)

    def assembly_outputs(self) -> list[dict]:
        """The connected BOM and pick-and-place outputs: [{outjob, output, variant}]."""
        rows = []
        for detail in self.outjobs.get("details") or []:
            for outputs in (detail.get("outputs_by_category") or {}).values():
                for row in outputs:
                    if row.get("state") == "connected" and row.get("type") in ASSEMBLY_TYPES:
                        rows.append({"outjob": detail["file"], "output": row["output"],
                                     "variant": row.get("variant") or NO_VARIATIONS})
        return rows

    def connected_types(self) -> set[str]:
        return {row.get("type") for detail in self.outjobs.get("details") or []
                for outputs in (detail.get("outputs_by_category") or {}).values()
                for row in outputs if row.get("state") == "connected"}


# ---------- helpers ----------

def _short(text: str, limit: int = EVIDENCE_CHARS) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _plural(count: int, word: str, many: str | None = None) -> str:
    return f"{count} {word if count == 1 else many or word + 's'}"


def _names(names: list[str], limit: int = NAMES_LISTED) -> str:
    shown = ", ".join(names[:limit])
    return shown + (f" and {len(names) - limit} more" if len(names) > limit else "")


def _utc(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _no_variations(name: str | None) -> bool:
    """Every part placed: [No Variations], or [All Variants], which get_output_jobs reads
    the same way (it shows every part)."""
    return (name or "") in ("", NO_VARIATIONS, ALL_VARIANTS)


def _outjob_or_raise(ctx: ReleaseContext) -> dict:
    model = ctx.outjobs
    if "error" in model:
        raise GateResult("not_verified", f"The OutJobs could not be read: {model['message']}")
    return model


def _listed_jobs(model: dict) -> list[dict]:
    return [job for job in model.get("outjobs") or [] if job.get("listed") and not job.get("error")]


# ---------- gates ----------

def gate_drc(ctx: ReleaseContext) -> tuple[str, str]:
    if ctx.pcb_path is None:
        return "not_applicable", "The project lists no PcbDoc."
    drc = ctx.drc
    if drc is None:
        return "not_verified", (
            f"No Batch DRC report for {ctx.pcb_path.name} ('Design Rule Check - "
            f"{ctx.pcb_path.stem}.html' or .drc) was found: run Tools > Design Rule Check "
            "with 'Create Report File' on.")
    report, path, issues = drc["report"], drc["path"], drc["issues"]
    kinds = {issue["issue"]: issue for issue in issues}
    total = report.totals.get(
        "rule_violations", sum(table.count for table in report.tables if table.type == RULE))
    head = f"{path.name} ({_utc(path.stat().st_mtime)}): {_plural(total, 'rule violation')}"
    waived = report.totals.get("waived")
    if waived:
        head += f", {waived} waived"
    other = next((kinds[key] for key in DRC_OTHER_BOARD if key in kinds), None)
    if other is not None:
        return "not_verified", f"{head}; {other['detail']}"
    if total or "violation_limit_reached" in kinds:
        rules = sorted(
            ((table.count, table.title) for table in report.tables
             if table.type == RULE and table.count), reverse=True)
        worst = "; ".join(f"{title.split(' (')[0]}: {count}" for count, title in rules[:3])
        stopped = ("; the run stopped at its violation limit"
                   if "violation_limit_reached" in kinds else "")
        return "fail", f"{head}{stopped}" + (f" ({worst})" if worst else "")
    stale = [kinds[key] for key in sorted(DRC_STALE) if key in kinds]
    if stale:
        return "fail", f"{head}, but not newer than the design: {stale[0]['detail']}"
    unusable = next((kinds[key] for key in sorted(DRC_UNUSABLE) if key in kinds), None)
    if unusable is not None:
        return "not_verified", f"{head}; {unusable['detail']}"
    # No other gate looks for shorts: a report that never checked them is no proof.
    shorts = next((issue for issue in issues if issue["issue"] == "rule_kind_not_checked"
                   and issue.get("rule_kind") == SHORT_CIRCUIT_LABEL), None)
    if shorts is not None:
        return "not_verified", f"{head}; {shorts['detail']}"
    cautions = [kinds[key]["detail"] for key in sorted(DRC_CAUTIONS) if key in kinds]
    if cautions:
        return "warn", f"{head}, not older than the board; " + " ".join(cautions)
    return "pass", f"{head}, not older than the board."


def gate_erc(ctx: ReleaseContext) -> tuple[str, str]:
    """Blocking and always not_verified: no ERC result is saved in the files. The offline
    approximation's counts (run_erc) are quoted as evidence, never as a pass."""
    if not ctx.project.get("sheets"):
        return "not_applicable", "The project has no schematic sheets."
    parts = ["ERC results are not saved in the files: unverifiable offline"]
    offline = ctx.offline_erc
    if offline is not None:
        levels = offline.get("by_level") or {}
        counts = ", ".join(_plural(levels.get(level, 0), word) for level, word in (
            ("Fatal Error", "fatal error"), ("Error", "error"), ("Warning", "warning"))
            if levels.get(level) or level != "Fatal Error")
        parts.append(f"run_erc (offline approximation): {counts}")
    model = ctx.outjobs
    if "error" in model:
        parts.append("the OutJobs could not be read")
    else:
        erc = (model.get("coverage") or {}).get("erc") or {}
        if erc.get("status") == "connected":
            parts.append(f"{_plural(len(erc.get('outputs') or []), 'ERC validation output')} "
                         "connected: the Project Releaser runs ERC")
        else:
            parts.append("no ERC validation output: a release does not run ERC")
        if "erc_settings_differ" in (model.get("summary") or {}).get("by_check", {}):
            parts.append("the OutJob's ERC levels differ from Project Options")
    parts.append("validate in Altium (Project > Validate PCB Project)")
    return "not_verified", "; ".join(parts) + "."


def gate_netlist_fresh(ctx: ReleaseContext) -> tuple[str, str]:
    sheets = ctx.project.get("sheets") or []
    if not sheets:
        return "not_applicable", "The project has no schematic sheets."
    if ctx.netlist is None:
        return "not_verified", (
            "Loaded without a netlist (Altium Designer not running and no up-to-date "
            "cached one), so its age against the sheets is unknown: open the project in "
            "Altium once, then set_project_dir.")
    stale = ctx.netlist_stale
    if stale is None:
        return "not_verified", "The compiled netlist's file time is unknown."
    names, others = stale
    compiled = _utc(ctx.netlist["net_file_mtime"])
    if names or others:
        what = []
        if names:
            what.append(f"{_plural(len(names), 'sheet')} ({_names(names)})")
        if others:
            what.append(_names(others))
        return "fail", (f"Saved after the netlist was compiled ({compiled}): "
                        + "; ".join(what) + ". Call refresh_netlist after saving in Altium.")
    if ctx.netlist.get("source") == "offline":
        return "pass", (f"Compiled offline by the copilot from the saved sheets ({compiled}), "
                        f"newer than all {_plural(len(sheets), 'sheet')}; Altium's own "
                        "compile did not run (ERC is its own gate).")
    return "pass", f"Compiled {compiled}, newer than all {_plural(len(sheets), 'sheet')}."


def gate_sch_pcb_sync(ctx: ReleaseContext) -> tuple[str, str]:
    if ctx.pcb_path is None:
        return "not_applicable", "The project lists no PcbDoc."
    if ctx.netlist is None:
        return "not_verified", (
            "Needs the compiled netlist, and the project was loaded without one: open it "
            "in Altium once, then set_project_dir.")
    if not ctx.netlist.get("components"):
        return "not_verified", "The netlist has no schematic parts to compare."
    index = ctx.require_index()
    newer = ctx.inputs.sheets_saved_after(ctx.project, ctx.pcb_path.stat().st_mtime)
    result = run_sync_check(
        index, ctx.netlist, is_dnp=ctx.inputs.variant_state.is_dnp, limit=1,
        sheets_saved_after_pcb=newer,
        net_matching=net_matching_options(ctx.project.get("prj_pcb_path")),
    )
    if "error" in result:
        return "not_verified", result.get("message") or result["error"]
    stale = ctx.netlist_stale
    note = (" (compared with a netlist older than some sheets: refresh_netlist)"
            if stale and (stale[0] or stale[1]) else "")
    parts, nets = result["components"], result["nets"]
    if result["in_sync"]:
        return "pass", (f"{parts['schematic']} schematic parts and {nets['schematic']} nets "
                        f"match the PCB{note}.")
    counts = result["severity_counts"]
    checks = sorted(result["summary"].items(), key=lambda item: -item[1])[:4]
    text = (f"Not in sync: {_plural(counts['error'], 'error')}, "
            f"{_plural(counts['warning'], 'warning')} ("
            + ", ".join(f"{check} {count}" for check, count in checks) + ")")
    if newer:
        text += f"; sheets saved after the PcbDoc: {_names(newer)} (Update PCB?)"
    return "fail", text + note + "."


def gate_unrouted_nets(ctx: ReleaseContext) -> tuple[str, str]:
    index = ctx.require_index()
    full = review_connectivity(index, checks=["unrouted"])
    if "error" in full:
        return "not_verified", full.get("message") or full["error"]
    summary = full["summary"]
    routable = summary["nets_with_2_or_more_pads"]
    not_routed = list(full.get("_not_routed_nets") or [])
    weak = list(full.get("nets_unrouted_in_altium") or [])
    connections = summary["connections"]
    if not_routed or weak:
        text = (f"{len(not_routed)} of {routable} nets not fully routed "
                f"({connections['missing']} of {connections['total']} connections missing")
        text += f"): {_names(not_routed)}" if not_routed else ")"
        if weak:
            text += (f"; {_plural(len(weak), 'net')} routed only through weak joins read "
                     f"un-routed in Altium ({_names(weak)})")
        return "fail", text + "."
    text = (f"All {routable} nets with two or more pads are routed ({connections['total']} "
            "connections)")
    assumed = summary.get("nets_connected_through_assumed_copper")
    if assumed:
        text += f"; {_plural(assumed, 'net')} connected through assumed copper (planes, pours)"
    return "pass", text + "."


def gate_polygons(ctx: ReleaseContext) -> tuple[str, str]:
    index = ctx.require_index()
    pours = len(index.pcb.pours or [])
    if not pours:
        return "not_applicable", "The board has no polygon pours."
    result = run_health_check(index.pcb, checks=["shelved_modified_polygons"], limit=1)
    if "error" in result:
        return "not_verified", result.get("message") or result["error"]
    count = result["summary"].get("shelved_modified_polygons", 0)
    if count:
        return "fail", (f"{_plural(count, 'polygon')} of {pours} shelved or modified since "
                        "the last pour: Altium stops output generation. Repour all and save.")
    return "pass", f"{_plural(pours, 'polygon pour')}, none shelved or modified."


def gate_board_outline(ctx: ReleaseContext) -> tuple[str, str]:
    index = ctx.require_index()
    box = outline_box(index.pcb)
    if box is None:
        return "fail", ("The PcbDoc has no board shape: no outline for the fabricator, the "
                        "edge clearances or the panel. Define it with Design > Board Shape.")
    width = (box[2] - box[0]) * MM_PER_MIL
    height = (box[3] - box[1]) * MM_PER_MIL
    return "pass", f"Board shape {width:.1f} x {height:.1f} mm (outline extent)."


def gate_fiducials(ctx: ReleaseContext) -> tuple[str, str]:
    index = ctx.require_index()
    variant = ctx.inputs.dfm_variant()
    # check_dfm's own side counts (mixed parts are SMD placements too).
    sides = side_counts(placements(index, variant.is_dnp).counts)
    smd = {side: sides[side]["smd"] for side in ("top", "bottom")}
    if not any(smd.values()):
        return "not_applicable", "No SMD placements: fiducials are for machine assembly."
    profile, error = load_profile(None, ctx.project.get("root_dir"),
                                  setting=ctx.inputs.fab_profile_setting)
    unreadable = ""
    if error:  # check_dfm answers this error; the fiducials are counted without limits
        profile, _ = load_profile("none", None)
        whose = ("the user's Fab profile setting" if error.get("setting")
                 else "the project's fab profile")
        unreadable = f"; {whose} is not usable ({error.get('error')})"
    full = review_dfm(index, profile, ["fiducials"], False, variant)
    if "error" in full:
        return "not_verified", full.get("message") or full["error"]
    entry = (full.get("results") or {}).get("fiducials") or {}
    if entry.get("status") == "not_checked":
        return "not_verified", entry.get("reason") or "check_dfm did not check fiducials."
    have, missing = entry.get("value") or {}, entry.get("missing_global") or {}
    wanted = entry.get("limit")
    rows = [f"{side} {have.get(side, 0)} for {smd[side]} SMD placements"
            for side in ("top", "bottom") if smd[side]]
    text = "Global fiducials: " + "; ".join(rows)
    if any(missing.get(side) for side in smd if smd[side]):
        basis = (f"the profile asks for {wanted} per assembled side" if wanted is not None
                 else f"assemblers typically want {GLOBAL_FIDUCIALS} per assembled side; "
                      "panel rails may carry them")
        return "warn", f"{text} ({basis}){unreadable}."
    return "pass", text + unreadable + "."


def gate_mounting_holes(ctx: ReleaseContext) -> tuple[str, str]:
    index = ctx.require_index()
    pcb = index.pcb
    parts = [part for part in assembly_parts(index)
             if part.mount == "mechanical" or (
                 part.mount in ("tht", "mixed") and not part.smd_pads
                 and _MOUNTING_DESIGNATOR.match(part.designator or ""))]
    bare = [hole for hole in getattr(pcb, "bare_holes", None) or () if hole.component is None]
    pads = [prim for prim in pcb.primitives
            if prim.kind == "pad" and prim.component is None
            and (prim.hole or 0.0) >= LARGE_HOLE_MIL]
    found = []
    if parts:
        found.append(_plural(len(parts), "mounting-hole part"))
    if pads:
        found.append(_plural(len(pads), "free pad") + " with a hole of 1 mm or more")
    if bare:
        found.append(_plural(len(bare), "free bare hole"))
    if found:
        return "pass", "Found " + ", ".join(found) + "."
    return "warn", ("No mounting hole found (a part of holes only, a free pad with a hole of "
                    "1 mm or more, or a bare hole): fine for a board held by its connectors "
                    "or edge, otherwise add them.")


def gate_dnp_parts(ctx: ReleaseContext) -> tuple[str, str]:
    variants = [variant for variant in ctx.inputs.variant_state.variants
                if variant.dnp_refdes or variant.dnp_uid]
    if not variants:
        return "not_applicable", "No variant marks a part Not Fitted."
    model = _outjob_or_raise(ctx)
    if not _listed_jobs(model):
        return "not_verified", "No readable OutJob: the assembly outputs cannot be checked."
    outputs = ctx.assembly_outputs()
    if not outputs:
        return "not_verified", ("No connected BOM or pick-and-place output to check: Not "
                                "Fitted parts stay on the PCB by design and only the "
                                "variant's outputs leave them out.")
    base = [row for row in outputs if _no_variations(row["variant"])]

    def counted(chosen) -> str:
        return ", ".join(f"{variant.name} ({len(variant.dnp_refdes or variant.dnp_uid)})"
                         for variant in chosen[:NAMES_LISTED])
    if base:
        labels = _names([f"{row['outjob']}: {row['output']}" for row in base], 3)
        on = _names(sorted({row["variant"] for row in base}))
        return "warn", (f"{_plural(len(base), 'assembly output')} on {on} ({labels}) "
                        f"list and place every part, also those Not Fitted in "
                        f"{counted(variants)}: set the variant unless the build is fully "
                        "fitted.")
    used = sorted({row["variant"] for row in outputs})
    left_out = [variant for variant in variants if variant.name in used]
    text = f"BOM and pick-and-place outputs use {_names(used)}"
    placed = (ctx.verified_codes("pick_place", ("dnp_present",))
              + ctx.verified_codes("bom", ("dnp_listed",)))
    if placed:
        # The generated files themselves list Not Fitted parts: they were generated
        # with another variant, or before the variant changed.
        return "warn", (f"{text}, but the generated files list Not Fitted parts as placed "
                        f"({_plural(placed, 'listing')}, verify_outputs): generate them again "
                        "with the variant.")
    if left_out:
        return "pass", (f"{text}, which leave out their Not Fitted parts ({counted(left_out)}); "
                        "those stay on the PCB by design.")
    return "pass", f"{text}, which mark no part Not Fitted: every part is placed."


def gate_outjob_coverage(ctx: ReleaseContext) -> tuple[str, str]:
    model = _outjob_or_raise(ctx)
    jobs = _listed_jobs(model)
    if not jobs:
        return "fail", ("The project lists no readable OutJob: nothing defines the release "
                        "outputs (fabrication, drill, BOM, pick-and-place, assembly drawing).")
    blocking, cautions = [], []
    for finding in model.get("findings") or []:
        check, severity = finding["check"], finding["severity"]
        if severity == "error" or (severity == "warning" and check in OUTJOB_BLOCKING_CHECKS):
            label = OUTJOB_CHECK_LABELS.get(check, check.replace("_", " "))
            if label not in blocking:
                blocking.append(label)
        elif severity == "warning" and check != "stale_outputs":
            label = OUTJOB_CHECK_LABELS.get(check, check.replace("_", " "))
            if label not in cautions:
                cautions.append(label)
    variants = ctx.inputs.variant_state.variants
    uncovered = []
    if defined_variant_count(variants):
        used = {row["variant"] for row in ctx.assembly_outputs()}
        if used:
            uncovered = [variant.name for variant in variants if variant.name not in used]
    head = f"{_plural(len(jobs), 'OutJob')}"
    if blocking:
        return "fail", f"{head}: " + "; ".join(blocking) + "."
    coverage = model.get("coverage") or {}
    connected = [label for role, label in ROLE_LABELS.items()
                 if (coverage.get(role) or {}).get("status") == "connected"]
    formats = (coverage.get("fabrication_data") or {}).get("formats") or []
    if formats and "fabrication data" in connected:
        connected[connected.index("fabrication data")] = f"fabrication data ({', '.join(formats)})"
    text = f"{head}, outputs connected: {', '.join(connected) or 'none'}"
    if uncovered:
        cautions.insert(0, f"no BOM or pick-and-place output for {_names(uncovered)}")
    if cautions:
        return "warn", f"{text}; " + "; ".join(cautions) + "."
    return "pass", text + "."


def gate_outputs_current(ctx: ReleaseContext) -> tuple[str, str]:
    model = _outjob_or_raise(ctx)
    if not _listed_jobs(model):
        return "not_applicable", "The project lists no readable OutJob."
    stale = next((finding for finding in model.get("findings") or []
                  if finding["check"] == "stale_outputs"), None)
    if stale is not None:
        example = (stale.get("examples") or [{}])[0]
        newer = [row.get("document") for row in example.get("documents_newer") or []]
        return "warn", (f"Generated outputs older than the design at "
                        f"{_plural(stale['count'], 'container')} (newest output "
                        f"{example.get('newest_output_utc')}; saved since: {_names(newer, 3)}): "
                        "generate again before sending them.")
    dated = [generated for detail in model.get("details") or []
             for container in detail.get("containers") or []
             for generated in [container.get("generated")]
             if generated and generated.get("newest_output_utc")]
    if not dated:
        return "not_verified", ("No generated outputs found at the OutJob containers' "
                                "folders: generate the OutJob or run the Project Releaser.")
    newest = max(row["newest_output_utc"] for row in dated)
    text = (f"Outputs at {_plural(len(dated), 'container')} are newer than the design "
            f"(newest {newest})")
    verified = ctx.verified
    if verified is not None:
        # File times do not show what changed since: the files' contents compared with
        # the design do. Differences make the outputs not current.
        warnings = verified["severity_counts"].get("warning", 0)
        results = verified.get("results") or {}
        labels = {"pick_place": "pick-and-place", "bom": "BOM", "netlist": "netlist",
                  "stackup": "stackup", "drill": "NC drill"}
        checks = [labels.get(check, check) for check, entry in results.items()
                  if entry.get("status") == "mismatch"]
        if warnings:
            return "warn", (f"{text}, but their contents differ from the design: "
                            f"{_plural(warnings, 'mismatch finding')} in {_names(checks)} "
                            "(verify_outputs): generate them again.")
        matched = [labels.get(check, check) for check, entry in results.items()
                   if entry.get("status") == "match"]
        if matched:
            return "pass", (f"{text}; the {_names(matched)} files match the design "
                            "(verify_outputs).")
    return "pass", text + "."


def gate_fab_drawing(ctx: ReleaseContext) -> tuple[str, str]:
    if ctx.pcb_path is None and not ctx.project.get("pcb_dwf_paths"):
        return "not_applicable", "The project lists no PcbDoc and no Draftsman drawing."
    if not ctx.project.get("pcb_dwf_paths"):
        # The fab notes still exist somewhere (a mechanical layer, a PDF): unread here.
        return "not_verified", ("The project has no Draftsman drawing: fab notes on a "
                                "mechanical layer or in a PDF cannot be checked against "
                                "the stackup offline. Check them in Altium, or add a "
                                "Draftsman fabrication drawing.")
    variant_state = ctx.inputs.variant_state
    active = variant_state.active.name if variant_state.variants else None
    model = build_fab_drawing(ctx.project, ctx.inputs.notes.draftsman,
                              ctx.inputs.fab_board_reader, draftsman_outputs(ctx.project),
                              active, ctx.inputs.fab_bom)
    if "error" in model:
        return "not_verified", model.get("message") or model["error"]
    boards = model.get("boards") or []
    if boards and all("error" in board for board in boards):
        return "not_verified", ("No board to compare the notes with: "
                                f"{boards[0].get('error')}")
    findings = model.get("findings") or []
    head = _plural(model.get("drawings_total", 0), "drawing")
    errors = [finding for finding in findings if finding["severity"] == "error"]
    if errors:
        first = errors[0]
        checks = sorted({finding["check"] for finding in errors})
        return "fail", (f"{head}: {_plural(len(errors), 'error')} ({', '.join(checks)}); "
                        f"{first['drawing']}: {first['issue']}")
    warnings = [finding for finding in findings if finding["severity"] == "warning"]
    if warnings:
        checks = sorted({finding["check"] for finding in warnings})
        return "warn", (f"{head}: notes agree with the stack; "
                        f"{_plural(len(warnings), 'warning')} ({', '.join(checks)}).")
    return "pass", f"{head}: notes agree with the stack, nothing missing."


def _title_value(names: tuple[str, ...], *parameter_sets: dict) -> str | None:
    """The first of names filled in (special strings resolved), else None."""
    for name in names:
        value = resolve_special_string(f"={name}", *parameter_sets)
        if value and value.strip() and value.strip() != "*":
            return value.strip()
    return None


def _consistency(label: str, values: dict[str, str | None], documents: str) -> str | None:
    """A warning text when the documents disagree or some leave the value out."""
    filled = {name: value for name, value in values.items() if value is not None}
    if not filled:
        return f"no {label} on the {documents} or in the project parameters"
    distinct: dict[str, list[str]] = {}
    for name, value in filled.items():
        distinct.setdefault(value, []).append(name)
    if len(distinct) > 1:
        shown = "; ".join(f"'{_short(value, 30)}' on {_names(names, 3)}"
                          for value, names in sorted(distinct.items(), key=lambda t: -len(t[1])))
        return f"{label} differs: {shown}"
    missing = [name for name, value in values.items() if value is None]
    if missing:
        return f"{label} missing on {_names(missing, 3)}"
    return None


def gate_title_block(ctx: ReleaseContext) -> tuple[str, str]:
    sheets = ctx.project.get("sheets") or []
    if not sheets:
        return "not_applicable", "The project has no schematic sheets."
    project_parameters = ctx.project.get("parameters") or {}
    revisions: dict[str, str | None] = {}
    dates: dict[str, str | None] = {}
    unread = 0
    for sheet in sheets:
        try:
            parameters = ctx.inputs.notes.sheet(sheet["path"]).sheet_parameters
        except Exception:
            unread += 1
            continue
        revisions[sheet["name"]] = _title_value(REVISION_PARAMETERS, parameters,
                                                project_parameters)
        dates[sheet["name"]] = _title_value(DATE_PARAMETERS, parameters, project_parameters)
    if not revisions:
        return "not_verified", "No sheet could be read."
    stated = 0
    for path in ctx.project.get("pcb_dwf_paths") or []:
        try:
            parameters = ctx.inputs.notes.draftsman(path).parameters
        except Exception:
            continue
        name = PureWindowsPath(path).name
        for names, values in ((REVISION_PARAMETERS, revisions), (DATE_PARAMETERS, dates)):
            value = _title_value(names, parameters)
            if value is not None:
                values[name] = value
                stated += 1
    problems = [text for text in (
        _consistency("revision", revisions, "sheets"),
        _consistency("date", dates, "sheets"),
    ) if text]
    if unread:
        problems.append(f"{_plural(unread, 'sheet')} could not be read")
    if problems:
        text = "; ".join(problems)
        return "warn", text[0].upper() + text[1:] + "."
    revision = next(iter(revisions.values()))
    date = next(iter(dates.values()))
    drawings = ("" if stated else "; Draftsman drawings state none of their own (they print "
                "the board's or project's)")
    return "pass", (f"Revision '{_short(revision, 30)}' and date '{_short(date, 30)}' on "
                    f"all {_plural(len(sheets), 'sheet')}{drawings}.")


def gate_project_integrity(ctx: ReleaseContext) -> tuple[str, str]:
    notes = ctx.inputs.notes
    model = build_project_integrity(ctx.project, notes.hierarchy, notes.integrity,
                                    ctx.netlist)
    if "error" in model:
        return "not_verified", model.get("message") or model["error"]
    summary = model["summary"]
    counts = summary["severity_counts"]
    by_check = summary.get("by_check") or {}
    unread = model.get("sheets_not_read") or []
    sheet_errors = (ctx.netlist or {}).get("sheet_errors") or []
    if counts["error"]:
        checks = [check for check, row in by_check.items() if row["worst"] == "error"]
        return "fail", (f"{_plural(counts['error'], 'error')} that break compile, sync or "
                        f"save: {', '.join(checks)}.")
    problems = []
    if counts["warning"]:
        checks = [check for check, row in by_check.items() if row["worst"] == "warning"]
        problems.append(f"{_plural(counts['warning'], 'warning')} ({', '.join(checks)})")
    if unread or sheet_errors:
        problems.append(f"{_plural(len(unread) or len(sheet_errors), 'sheet')} not read")
    if problems:
        return "warn", "; ".join(problems) + "."
    return "pass", f"{_plural(len(model.get('checks_run') or []), 'check')} clean."


def gate_clone_status(ctx: ReleaseContext) -> tuple[str, str]:
    status = ctx.inputs.clone_status(ctx.project["root_dir"])
    if status.get("altium365") is False:
        return "not_applicable", "Not an Altium 365 project clone."
    if status.get("error"):
        return "not_verified", status.get("message") or status["error"]
    problems = []
    if status.get("uncommitted_design_files"):
        problems.append(f"{_plural(status['uncommitted_design_files'], 'design file')} "
                        "changed and not committed")
    if status.get("ahead"):
        problems.append(f"{_plural(status['ahead'], 'local commit')} not in Altium 365")
    if status.get("behind"):
        problems.append(f"{_plural(status['behind'], 'commit')} in Altium 365 not in this copy")
    fetched = status.get("days_since_fetch")
    when = (f"last updated from Altium 365 {_plural(fetched, 'day')} ago" if fetched is not None
            else "last update from Altium 365 unknown")
    if problems:
        return "fail", "; ".join(problems) + f" ({when})."
    cautions = [row["message"] for row in status.get("warnings") or []]
    if status.get("uncommitted_count"):
        cautions.insert(0, f"{_plural(status['uncommitted_count'], 'other file')} not committed")
    if cautions:
        return "warn", f"Committed and level with Altium 365; {when}. " + " ".join(cautions)
    return "pass", f"Committed and level with Altium 365 ({when})."


def gate_silkscreen(ctx: ReleaseContext) -> tuple[str, str]:
    index = ctx.require_index()
    drc = ctx.drc
    if drc is None:
        return "not_verified", "No Batch DRC report to read silk-to-mask and silk-to-silk from."
    issues = {issue["issue"] for issue in drc["issues"]}
    if issues & (DRC_OTHER_BOARD | DRC_STALE | DRC_UNUSABLE):
        return "not_verified", "The DRC report is not usable for this board as saved (drc gate)."
    options = index.pcb.drc_options
    batch = options.batch_kinds if options is not None else None
    enabled = {rule.kind_id for rule in index.pcb.rules if rule.enabled}
    checked = [label for _, kind_id, label in SILK_RULES
               if kind_id in enabled and (batch is None or kind_id in batch)]
    if not checked:
        return "not_verified", ("Silk To Solder Mask and Silk to Silk are not checked by the "
                                "board's batch DRC (no enabled rule, or not ticked).")
    kinds = {kind for kind, _, _ in SILK_RULES}
    count = sum(table.count for table in drc["report"].tables
                if table.type == RULE and rule_kind(table.title)[0] in kinds)
    names = " and ".join(checked)
    if count:
        return "warn", (f"{_plural(count, 'silkscreen violation')} in the last DRC "
                        f"({names}).")
    if "violation_limit_reached" in issues:
        return "not_verified", (f"The last DRC stopped at its violation limit, so {names} "
                                "may read 0 without having been checked (drc gate).")
    return "pass", (f"No {names} violations in the last DRC (designator placement and "
                    "legibility are not checked).")


def gate_footprints(ctx: ReleaseContext) -> tuple[str, str]:
    text = ("Footprints are compared with their libraries in Altium (Tools > Update From "
            "PCB Libraries, or a Footprint Comparison Report) with the libraries or the "
            "workspace at hand: not read offline")
    if "error" not in ctx.outjobs and "Footprint Comparison Report" in ctx.connected_types():
        text += "; a Footprint Comparison Report output is connected (the Releaser runs it)"
    return "not_verified", text + "."


def gate_lifecycle(ctx: ReleaseContext) -> tuple[str, str]:
    return "not_verified", ("Component lifecycle states and environment compliance live in "
                            "the Altium 365 workspace (Part Choices, ActiveBOM, Component "
                            "State Checking), not in the design files.")


def gate_testpoints(ctx: ReleaseContext) -> tuple[str, str]:
    """check_testpoints on its default (ict) profile, advisory: warn on a warning finding
    (no probe sites, a rail or ground net without one, a site too small, too close or
    blocked), else pass with the coverage."""
    index = ctx.require_index()
    full = review_testpoints(index, is_dnp=ctx.inputs.dfm_variant().is_dnp)
    if "error" in full:
        return "not_verified", full.get("message") or full["error"]
    sites, coverage = full["sites"], full["coverage"]
    if not sites["probe_sites"]:
        return "warn", (f"No probe sites: no pad or via is marked as a Fabrication Testpoint "
                        f"and there are no TP parts ({_plural(coverage['nets'], 'net')}; "
                        "Assembly Testpoint marks are not read).")
    share = f"{coverage['percent']}%" if coverage["percent"] is not None else "no net of 2+ pads"
    text = (f"{_plural(sites['probe_sites'], 'probe site')} "
            f"{'reaches' if sites['probe_sites'] == 1 else 'reach'} "
            f"{coverage['with_access']} of {_plural(coverage['nets'], 'net')} "
            f"({share}; top {coverage['top']}, bottom {coverage['bottom']})")
    warnings = Counter(finding["check"] for finding in full["findings"]
                       if finding["severity"] == "warning")
    if warnings:
        checks = ", ".join(f"{check} {count}" for check, count in warnings.most_common())
        return "warn", (f"{text}; {_plural(sum(warnings.values()), 'warning')} on ICT "
                        f"guidance ({checks}).")
    return "pass", f"{text}; no warning on ICT guidance."


def gate_teardrops(ctx: ReleaseContext) -> tuple[str, str]:
    return "not_verified", ("Teardrops are not read from the PcbDoc: check Tools > "
                            "Teardrops in Altium.")


def gate_via_stitching(ctx: ReleaseContext) -> tuple[str, str]:
    return "not_verified", ("Stitching vias cannot be told from other vias reliably "
                            "offline: check_vias counts the vias per net (e.g. the ground "
                            "net).")


def gate_team_template(ctx: ReleaseContext) -> tuple[str, str]:
    return "not_verified", ("The team's template parameters (an Altium 365 project template "
                            "or a template .PrjPcb) are not in the project files: compare "
                            "get_design_notes' project parameters with it.")


GATES: tuple[Gate, ...] = (
    Gate("drc", "DRC report clean and newer than the board", True, "get_drc_report", gate_drc),
    Gate("erc", "ERC clean", True, "get_project_options", gate_erc),
    Gate("netlist_fresh", "Netlist newer than every schematic sheet", True,
         "check_sch_pcb_sync", gate_netlist_fresh),
    Gate("sch_pcb_sync", "Schematic and PCB in sync", True, "check_sch_pcb_sync",
         gate_sch_pcb_sync),
    Gate("unrouted_nets", "No unrouted nets", True, "check_connectivity", gate_unrouted_nets),
    Gate("polygons", "Polygons repoured (none shelved or modified)", True, "check_pcb_health",
         gate_polygons),
    Gate("board_outline", "Board outline present", True, "get_board_info", gate_board_outline),
    Gate("fiducials", "Global fiducials on each assembled side", False, "check_dfm",
         gate_fiducials),
    Gate("mounting_holes", "Mounting holes present", False, "get_assembly_guide",
         gate_mounting_holes),
    Gate("dnp_parts", "Not Fitted parts left out of the assembly outputs", False,
         "get_output_jobs", gate_dnp_parts),
    Gate("outjob_coverage", "OutJob complete for every variant", True, "get_output_jobs",
         gate_outjob_coverage),
    Gate("outputs_current", "Generated outputs newer than the design", False,
         "get_output_jobs", gate_outputs_current),
    Gate("fab_drawing", "Draftsman fab notes agree with the stackup", True,
         "check_fab_drawing", gate_fab_drawing),
    Gate("title_block", "Title-block revision and date filled in consistently", False,
         "get_design_notes", gate_title_block),
    Gate("project_integrity", "Project files intact", True, "check_project_integrity",
         gate_project_integrity),
    Gate("clone_status", "Altium 365 copy committed and up to date", True, "get_clone_status",
         gate_clone_status),
    Gate("silkscreen", "Silkscreen clear of mask and other silk (DRC)", False,
         "get_drc_report", gate_silkscreen),
    Gate("footprints_vs_libraries", "Footprints match the libraries", False, None,
         gate_footprints),
    Gate("lifecycle_compliance", "Component lifecycle and environment compliance", False,
         None, gate_lifecycle),
    Gate("testpoints", "Test access (testpoints)", False, "check_testpoints",
         gate_testpoints),
    Gate("teardrops", "Teardrops added", False, None, gate_teardrops),
    Gate("via_stitching", "Via stitching done", False, "check_vias", gate_via_stitching),
    Gate("team_template", "Project matches the team's template parameters", False, None,
         gate_team_template),
)


# ---------- the report ----------

def run_gate(gate: Gate, ctx: ReleaseContext) -> dict:
    """One gate's row; a check that raises is not_verified, and a non-blocking gate's
    fail is a warn."""
    start = time.perf_counter()
    try:
        status, evidence = gate.run(ctx)
    except GateResult as answer:
        status, evidence = answer.status, answer.evidence
    except Exception as error:  # one broken source must not stop the report
        logging.warning("check_release_readiness: gate %s failed: %s", gate.id, error)
        status = "not_verified"
        evidence = f"The check failed ({type(error).__name__}: {error})."
    if status not in STATUSES:
        status, evidence = "not_verified", f"Unknown status {status!r}: {evidence}"
    if status == "fail" and not gate.blocking:
        status = "warn"
    return {
        "id": gate.id,
        "title": gate.title,
        "status": status,
        "blocking": gate.blocking,
        "evidence": _short(evidence),
        "source_tool": gate.source_tool,
        "ms": round((time.perf_counter() - start) * 1000),
    }


def verdict(gates: list[dict]) -> dict:
    """The Releaser's verdict over the gate rows: fail when a blocking gate failed,
    not_ready_unverified when none failed but one is not verified, else pass."""
    failed = [g["id"] for g in gates if g["blocking"] and g["status"] == "fail"]
    unverified = [g["id"] for g in gates if g["blocking"] and g["status"] == "not_verified"]
    warnings = [g["id"] for g in gates if g["status"] == "warn"]
    advisory = [g["id"] for g in gates if not g["blocking"] and g["status"] == "not_verified"]
    if failed:
        result = "fail"
        summary = (f"FAIL: {_plural(len(failed), 'blocking gate')} failed ({', '.join(failed)})"
                   + (f" and {len(unverified)} not verified ({', '.join(unverified)})"
                      if unverified else "")
                   + ". One failed gate fails the release, as in Altium's Project Releaser.")
    elif unverified:
        result = "not_ready_unverified"
        summary = (f"NOT VERIFIED: no blocking gate failed, but {len(unverified)} cannot be "
                   f"verified from the files ({', '.join(unverified)}): confirm "
                   f"{'it' if len(unverified) == 1 else 'them'} in Altium before release.")
    else:
        result = "pass"
        summary = "PASS: no blocking gate failed or is unverified on the checks run"
        cautions = [g["id"] for g in gates if g["blocking"] and g["status"] == "warn"]
        summary += (f"; {_plural(len(cautions), 'blocking gate')} passed with a warn "
                    f"({', '.join(cautions)})." if cautions else ".")
    if advisory:
        summary += f" {_plural(len(advisory), 'advisory item')} not verified offline."
    return {
        "verdict": result,
        "verdict_summary": summary,
        "blocking_failed": failed,
        "blocking_not_verified": unverified,
        "warnings": warnings,
        "not_verified": advisory,
    }


NOTES = (
    "Each gate quotes its source tool's result in one line: call source_tool for the "
    "details before explaining or fixing a gate.",
    "not_verified is never a pass: the data source is missing, cannot be read offline, or "
    "the check failed. Say what would verify it.",
    "Read from the saved files, not from Altium: save in Altium before running it again. "
    "The Project Releaser's own validations (ERC, DRC, differences) are the final word.",
    "Not Fitted parts stay on the PCB by design: dnp_parts checks that the BOM and "
    "pick-and-place outputs use a variant that leaves them out.",
)


def build_release_readiness(inputs: ReleaseInputs, gates: tuple[Gate, ...] = GATES) -> dict:
    """Run every gate over the loaded project: the verdict, the gate rows (id, title,
    status, blocking, evidence, source_tool, ms) and notes."""
    start = time.perf_counter()
    ctx = ReleaseContext(inputs)
    rows = [run_gate(gate, ctx) for gate in gates]
    project = inputs.project
    variant_state = inputs.variant_state
    counts = {status: sum(1 for row in rows if row["status"] == status) for status in STATUSES}
    model: dict = {
        "project": project.get("name"),
        "pcb_doc": ctx.pcb_path.name if ctx.pcb_path is not None else None,
        "active_variant": variant_state.active.name if variant_state.variants else None,
        "load": "netlist" if inputs.netlist is not None else "netlist_free",
        **verdict(rows),
        "counts": counts,
        "gates": rows,
        "elapsed_ms": round((time.perf_counter() - start) * 1000),
        "notes": list(NOTES),
    }
    return model


def page_release_readiness(model: dict, offset: int, limit: int) -> dict:
    """One page of gate rows; the verdict, counts and notes go with every page."""
    offset = max(0, int(offset or 0))
    limit = max(1, int(limit or 1))
    rows = model["gates"]
    result = {key: value for key, value in model.items() if key not in ("gates", "notes")}
    result["gates_total"] = len(rows)
    result["offset"] = offset
    result["has_more"] = offset + limit < len(rows)
    if result["has_more"]:
        result["next_offset"] = offset + limit
    result["gates"] = rows[offset:offset + limit]
    result["notes"] = model["notes"]
    return result


# ---------- the release_review prompt ----------

# Every tool the prompt names; tests check each appears in it and is registered.
RELEASE_REVIEW_TOOLS = (
    "check_release_readiness",
    "get_drc_report",
    "run_erc",
    "get_project_options",
    "check_sch_pcb_sync",
    "refresh_netlist",
    "check_connectivity",
    "check_pcb_health",
    "get_board_info",
    "check_dfm",
    "get_assembly_guide",
    "get_output_jobs",
    "verify_outputs",
    "check_fab_drawing",
    "get_design_notes",
    "check_project_integrity",
    "get_clone_status",
    "check_vias",
    "check_testpoints",
    "diff_design",
    "run_review_checklist",
)

RELEASE_REVIEW_PROMPT = """\
Project: {name}
PCB: {pcb_doc}
Active variant: {active_variant}
Load: {load}

You are running a release go / no-go review: is this design ready for review or for
fabrication? It follows Altium's Project Releaser: one failed blocking gate fails the
release. The review is read-only: it changes no design file.

## Ground Rules

- not_verified is never a pass. Name its reason and how to confirm it in Altium (ERC:
  Project > Validate PCB Project and a clean Messages panel).
- Quote each gate's evidence as the tool gave it, and call its source_tool before
  explaining a failure or proposing a fix. Do not run other checks speculatively.
- The files are read as saved: when the user changes something, ask them to save in
  Altium (Ctrl+S), then call check_release_readiness again.
- refresh_netlist only after the user confirms they saved the schematic.
- Altium's Project Releaser and its validations are the final word.

---

### Phase 1 — Gates

Call check_release_readiness (page with next_offset while has_more). Present one table,
blocking gates first: | Gate | Blocking | Status | Evidence |
State the verdict and verdict_summary as given.

Then call run_review_checklist(checklist="release") once and show its coverage table:
the release checklist adds the items no gate covers (the offline ERC, the generated files
against the design, footprints against the libraries, release notes, suppliers).

### Phase 2 — Causes

For each blocking gate that failed, then each warn, call its source_tool once:
drc and silkscreen → get_drc_report; erc → run_erc (the offline approximation: its
errors, never a pass), get_project_options (ERC levels, No ERC directives) and
get_output_jobs (the ERC validation output); netlist_fresh and
sch_pcb_sync → check_sch_pcb_sync; unrouted_nets → check_connectivity; polygons →
check_pcb_health; board_outline → get_board_info; fiducials → check_dfm with checks
["fiducials"]; mounting_holes → get_assembly_guide; outjob_coverage → get_output_jobs;
dnp_parts and outputs_current → get_output_jobs, then verify_outputs when the evidence
cites it (the generated files against the design); fab_drawing → check_fab_drawing;
title_block → get_design_notes; project_integrity → check_project_integrity; clone_status →
get_clone_status; testpoints → check_testpoints (guidance values: ask for the test
house's numbers); via_stitching → check_vias. Give each its cause and the fix in Altium.

### Phase 3 — Go / No-Go

  Table 1 — Blocking items
  | Gate | Status | Cause | Fix in Altium |

  Table 2 — Not verified
  | Gate | Why | How to confirm |

  Table 3 — Warnings
  | Gate | Evidence | Suggestion |

Verdict: NO-GO when a blocking gate failed; NOT VERIFIED when none failed but a blocking
gate is not_verified (list what the user must confirm in Altium); GO only when the
verdict is pass, still listing the warnings and the not-verified advisory items as open.
Never list a not_verified gate as passed, in a table or in the summary.

Finish with the release record: run_review_checklist(checklist="release",
format="markdown") with decisions= giving your Yes, No or N/A and a one-line reason for
each needs-judgment item you settled. Show its 'Checklist coverage' table and the record:
sign-off belongs to the design state it names. Save it only where the user agrees; a
later review passes it as previous= to see what changed.

Ask: "Shall I dig into a gate, or draft the release notes with diff_design?"
"""

RELEASE_REVIEW_NETLIST_FREE_NOTE = """
---

Netlist-free load: this project was loaded without a netlist (Altium Designer was not
running), so netlist_fresh and sch_pcb_sync are not_verified and check_sch_pcb_sync
answers netlist_required. Say so in the report: opening the project in Altium once and
calling set_project_dir again verifies them.
"""


def build_release_review(project: dict, variant_name: str, *, netlist_free: bool = False) -> str:
    """The release-review prompt for a loaded project; netlist_free appends
    RELEASE_REVIEW_NETLIST_FREE_NOTE."""
    paths = project.get("pcb_doc_paths") or []
    pcb_doc = PureWindowsPath(paths[0]).name if paths else "(none)"
    review = RELEASE_REVIEW_PROMPT.format(
        name=project.get("name", "?"),
        pcb_doc=pcb_doc,
        active_variant=variant_name,
        load="netlist-free" if netlist_free else "netlist",
    )
    if netlist_free:
        review += RELEASE_REVIEW_NETLIST_FREE_NOTE
    return review
