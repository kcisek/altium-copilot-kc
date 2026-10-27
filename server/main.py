# server/main.py
import json
import logging
import math
import os
import re
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Annotated, Any, Literal

vendor_dir = os.path.join(os.path.dirname(__file__), "vendor")
# When bundled with PyInstaller, vendor is embedded — no sys.path injection needed.
# Only inject when running from source and the vendor dir exists.
if os.path.exists(vendor_dir):
    sys.path.insert(0, vendor_dir)

import httpx
from mcp.server.fastmcp import FastMCP, Image
from mcp.types import ToolAnnotations
from pydantic import WithJsonSchema

try:
    from altium import AltiumClient, is_netlist_fresh, netlist_sources
    from parsers.prj_pcb import parse_prj_options, parse_prj_pcb, VariantState
    from services.registry import read_registry, upsert_registry_entry, mark_xfn_exported
    from services.page_netlist import build_sheet_context, MAX_RESULT_SIZE_CHARS
    from parsers.pcb_doc import parse_pcb_doc
    from parsers.sch_doc import read_sch_doc_image
    from services.pcb_index import PcbIndex
    from services.pcb_rules import (
        BINARY_KINDS, NET_RULE_KINDS, ROOM_KIND, resolve_kind, rule_header,
        rule_values, stale_references,
    )
    from services.pcb_length import (
        check_length_rules, net_length, pad_to_pad, pair_lengths, xsignal_length,
    )
    from services.pcb_compliance import check_compliance
    from services.diff_pair_checks import check_diff_pairs as run_diff_pair_checks
    from services.diff_pair_geometry import (
        prepare_diff_pair_geometry_cached, render_diff_pair_geometry,
    )
    from services.mechanical_fit import (
        export_rows as mechanical_export_rows,
        parse_sections as parse_mechanical_sections, prepare_handoff,
        prepare_mechanical_fit_cached, render_handoff, render_mechanical_fit, write_drawings,
    )
    from services.constraints_review import build_constraints_review
    from services.impedance import (
        PROPAGATION_UNIT_NOTE, impedance_profiles, profile_refs,
    )
    from services.sch_pcb_sync import net_matching_options, run_sync_check
    from services.directive_sync import (
        build_directive_sync_cached, detect_project_type, page_directive_sync,
    )
    from services.constraints_brief import (
        FORMATS as RULE_FILE_FORMATS, SECTIONS as CONSTRAINTS_SECTIONS,
        Assumptions as BriefAssumptions, build_brief_cached, build_rule_files, inline_texts,
        parse_sections as parse_brief_sections, render_brief, write_rule_files,
    )
    from services.drc_results import (
        load_report, assess, summarize, other_sections, DrcLinker, count_problem,
    )
    from services.bom import (
        BASE_VARIANT, build_parts as build_bom_parts, compare_variants as compare_bom_variants,
        group_lines as group_bom_lines, natural_key, needs_mpn, plan_variant, variant_names,
        to_csv as bom_csv,
    )
    from services.bom_checks import run_bom_checks
    from services.active_bom import (
        SHRINK_STEPS as ACTIVE_BOM_SHRINK_STEPS, bom_doc_findings as active_bom_findings,
        cache_summary as active_bom_cache, covered_designators as active_bom_covered,
        export_rows as active_bom_export_rows, find_lines as find_active_bom_lines,
        line_annotations as active_bom_annotations, open_model as open_active_bom,
        page as page_active_bom, parse_sections as parse_active_bom_sections,
    )
    from services.decoupling import DEFAULT_MAX_DISTANCE_MIL, run_decoupling_check
    from services.pull_resistors import run_pull_resistor_check
    from services.interfaces import (
        DEFAULT_MAX_DISTANCE_MIL as DEFAULT_TVS_MAX_DISTANCE_MIL, run_interface_check,
    )
    from services.power_tree import prepare_power_tree, render_power_tree
    from services.derating import (
        load_policy as load_derating_policy, parse_rail_map, prepare_derating,
        render_derating,
    )
    from services.logic_levels import prepare_logic_levels, render_logic_levels
    from services.circuit_calcs import (
        parse_vf, parse_vth, prepare_circuit_calculations, render_circuit_calculations,
    )
    from services.thermal import (
        DEFAULT_MAX_DISTANCE_MIL as DEFAULT_THERMAL_DISTANCE_MIL, Inputs as ThermalInputs,
        parse_values as parse_thermal_values, prepare_thermal, render_thermal,
    )
    from services.hv_clearance import (
        Options as HvOptions, parse_groups as parse_hv_groups, parse_hv,
        parse_min_distance as parse_hv_min_distance,
        parse_working_voltage as parse_hv_working_voltage, prepare_hv_cached, render_hv,
    )
    from services.power_paths import (
        DEFAULTS as POWER_DEFAULTS, parse_copper_oz, parse_currents as parse_power_currents,
        prepare_power_paths_cached, render_power_paths,
    )
    from services.return_path import prepare_return_path_cached, render_return_path
    from services.via_checks import page_result as page_via_result, review_vias
    from services.testpoints import (
        build_profile as build_testpoint_profile, marked_testpoint_nets,
        page_result as page_testpoint_result, review_testpoints,
    )
    from services.firmware_pins import (
        FORMATS as FIRMWARE_FORMATS, build_firmware_map, export_rows as firmware_export_rows,
    )
    from services.pcb_health import run_health_check
    from services.dfm import (
        DRILL_ROWS_LISTED, SHRINK_STEPS, AssemblyContext, page_dfm, review_dfm,
    )
    from services.manufacturing_review import build_manufacturing_review
    from services.assembly_guide import (
        ROW_LIMITS as GUIDE_ROW_LIMITS, GuideVariant, build_guide,
        page as page_guide, summary as guide_summary,
    )
    from services.fab_profiles import load_profile
    from services.connectivity import (
        check_connectivity as review_connectivity, drc_comparison,
        page_result as page_connectivity_result,
    )
    from services.clone_status import clone_status, head_state
    from parsers.pcb_doc import CLASS_KINDS
    from services.design_notes import (
        DesignNotesCache, NOTES_MAX_RESULT_SIZE_CHARS, build_design_notes,
        draftsman_documents, note_rows, resolve_document, sheet_notes_for_context,
    )
    from services.images import (
        MAX_IMAGE_BYTES, MAX_PASSTHROUGH_SIDE_PX, bmp_to_png, image_format, image_size,
    )
    from services.design_hierarchy import build_design_hierarchy, page_design_hierarchy
    from services.project_options import build_project_options, page_project_options
    from services.erc import build_erc, page_erc
    from services.project_integrity import build_project_integrity, page_project_integrity
    from services.output_jobs import (
        build_output_jobs, draftsman_outputs, output_locations, page_output_jobs,
        variant_outputs,
    )
    from services.variants import (
        GROUPS as VARIANT_DIFF_GROUPS, compare as compare_variant_views,
        documentation as variant_documentation, draftsman_variants,
        export_rows as variant_export_rows, group_rows as group_variant_rows,
        same_variant, shown_values as shown_variant_values,
        unvalidated as unvalidated_variant_rows, view_of as variant_view,
    )
    from services.fab_drawing import (
        board_facts as fab_board_facts, build_fab_drawing, note_lines, page_fab_drawing,
    )
    from services.design_diff import (
        DiffError, diff_folders, diff_revisions, page_design_diff,
    )
    from services.project_tour import build_project_tour, defined_variant_count
    from services.verify_outputs import (
        SHRINK_STEPS as VERIFY_SHRINK_STEPS, Tolerances as VerifyTolerances,
        page_verify_outputs, run_verify_outputs,
    )
    from services.libraries import (
        ItemLoader, LibraryCheckInputs, LibraryResolver, footprint_body_heights,
        installed_libraries, library_part,
        page_library_checks, page_library_part, project_files, project_libraries,
        registry_projects, run_library_checks,
    )
    from services import db_parts
    from services import multiboard
    from parsers.pin_table import PinTableError, parse_pin_file, parse_pin_text
    from services.pin_table import (
        check_pins as check_pin_rows, page as page_pin_table, placed_pins, symbol_pins,
        table_description,
    )
    from services.release_gate import (
        ReleaseInputs, build_release_readiness, build_release_review,
        page_release_readiness,
    )
    from services.review_checklist import (
        EVIDENCE_TOOLS as REVIEW_EVIDENCE_TOOLS, ChecklistError, apply_decisions,
        catalog as checklist_catalog, compare as compare_review_records, design_state,
        folder_signature, load_bundled as load_bundled_checklists, load_checklists,
        page_review, parse_decisions, read_previous as read_previous_record, render_record,
        run_review_checklist as build_review_checklist, select as select_checklist_items,
    )
    from services.custom_checks import (
        PLUGIN_FOLDER, PLUGIN_HINT, PACK_FILE, PACK_FOLDER, TOOL_RULE_TOOLS, PackError,
        choose as choose_custom_rules, load_packs as load_check_packs,
        page as page_custom_checks, pack_signature, plugin_files, plugin_id,
        plugins_allowed, rule_row, run_rules as run_custom_rules,
    )
    from services.design_api import DesignAPI
    from export import export_project, HIGH_FANOUT_THRESHOLD
except ImportError:
    from server.altium import AltiumClient, is_netlist_fresh, netlist_sources
    from server.parsers.prj_pcb import parse_prj_options, parse_prj_pcb, VariantState
    from server.parsers.pcb_doc import parse_pcb_doc
    from server.parsers.sch_doc import read_sch_doc_image
    from server.services.registry import read_registry, upsert_registry_entry, mark_xfn_exported
    from server.services.page_netlist import build_sheet_context, MAX_RESULT_SIZE_CHARS
    from server.services.pcb_index import PcbIndex
    from server.services.pcb_rules import (
        BINARY_KINDS, NET_RULE_KINDS, ROOM_KIND, resolve_kind, rule_header,
        rule_values, stale_references,
    )
    from server.services.pcb_length import (
        check_length_rules, net_length, pad_to_pad, pair_lengths, xsignal_length,
    )
    from server.services.pcb_compliance import check_compliance
    from server.services.diff_pair_checks import check_diff_pairs as run_diff_pair_checks
    from server.services.diff_pair_geometry import (
        prepare_diff_pair_geometry_cached, render_diff_pair_geometry,
    )
    from server.services.mechanical_fit import (
        export_rows as mechanical_export_rows,
        parse_sections as parse_mechanical_sections, prepare_handoff,
        prepare_mechanical_fit_cached, render_handoff, render_mechanical_fit, write_drawings,
    )
    from server.services.constraints_review import build_constraints_review
    from server.services.impedance import (
        PROPAGATION_UNIT_NOTE, impedance_profiles, profile_refs,
    )
    from server.services.sch_pcb_sync import net_matching_options, run_sync_check
    from server.services.directive_sync import (
        build_directive_sync_cached, detect_project_type, page_directive_sync,
    )
    from server.services.constraints_brief import (
        FORMATS as RULE_FILE_FORMATS, SECTIONS as CONSTRAINTS_SECTIONS,
        Assumptions as BriefAssumptions, build_brief_cached, build_rule_files, inline_texts,
        parse_sections as parse_brief_sections, render_brief, write_rule_files,
    )
    from server.services.drc_results import (
        load_report, assess, summarize, other_sections, DrcLinker, count_problem,
    )
    from server.services.bom import (
        BASE_VARIANT, build_parts as build_bom_parts, compare_variants as compare_bom_variants,
        group_lines as group_bom_lines, natural_key, needs_mpn, plan_variant, variant_names,
        to_csv as bom_csv,
    )
    from server.services.bom_checks import run_bom_checks
    from server.services.active_bom import (
        SHRINK_STEPS as ACTIVE_BOM_SHRINK_STEPS, bom_doc_findings as active_bom_findings,
        cache_summary as active_bom_cache, covered_designators as active_bom_covered,
        export_rows as active_bom_export_rows, find_lines as find_active_bom_lines,
        line_annotations as active_bom_annotations, open_model as open_active_bom,
        page as page_active_bom, parse_sections as parse_active_bom_sections,
    )
    from server.services.decoupling import DEFAULT_MAX_DISTANCE_MIL, run_decoupling_check
    from server.services.pull_resistors import run_pull_resistor_check
    from server.services.interfaces import (
        DEFAULT_MAX_DISTANCE_MIL as DEFAULT_TVS_MAX_DISTANCE_MIL, run_interface_check,
    )
    from server.services.power_tree import prepare_power_tree, render_power_tree
    from server.services.derating import (
        load_policy as load_derating_policy, parse_rail_map, prepare_derating,
        render_derating,
    )
    from server.services.logic_levels import prepare_logic_levels, render_logic_levels
    from server.services.circuit_calcs import (
        parse_vf, parse_vth, prepare_circuit_calculations, render_circuit_calculations,
    )
    from server.services.thermal import (
        DEFAULT_MAX_DISTANCE_MIL as DEFAULT_THERMAL_DISTANCE_MIL, Inputs as ThermalInputs,
        parse_values as parse_thermal_values, prepare_thermal, render_thermal,
    )
    from server.services.hv_clearance import (
        Options as HvOptions, parse_groups as parse_hv_groups, parse_hv,
        parse_min_distance as parse_hv_min_distance,
        parse_working_voltage as parse_hv_working_voltage, prepare_hv_cached, render_hv,
    )
    from server.services.power_paths import (
        DEFAULTS as POWER_DEFAULTS, parse_copper_oz, parse_currents as parse_power_currents,
        prepare_power_paths_cached, render_power_paths,
    )
    from server.services.return_path import prepare_return_path_cached, render_return_path
    from server.services.via_checks import page_result as page_via_result, review_vias
    from server.services.testpoints import (
        build_profile as build_testpoint_profile, marked_testpoint_nets,
        page_result as page_testpoint_result, review_testpoints,
    )
    from server.services.firmware_pins import (
        FORMATS as FIRMWARE_FORMATS, build_firmware_map, export_rows as firmware_export_rows,
    )
    from server.services.pcb_health import run_health_check
    from server.services.dfm import (
        DRILL_ROWS_LISTED, SHRINK_STEPS, AssemblyContext, page_dfm, review_dfm,
    )
    from server.services.manufacturing_review import build_manufacturing_review
    from server.services.assembly_guide import (
        ROW_LIMITS as GUIDE_ROW_LIMITS, GuideVariant, build_guide,
        page as page_guide, summary as guide_summary,
    )
    from server.services.fab_profiles import load_profile
    from server.services.connectivity import (
        check_connectivity as review_connectivity, drc_comparison,
        page_result as page_connectivity_result,
    )
    from server.services.clone_status import clone_status, head_state
    from server.parsers.pcb_doc import CLASS_KINDS
    from server.services.design_notes import (
        DesignNotesCache, NOTES_MAX_RESULT_SIZE_CHARS, build_design_notes,
        draftsman_documents, note_rows, resolve_document, sheet_notes_for_context,
    )
    from server.services.images import (
        MAX_IMAGE_BYTES, MAX_PASSTHROUGH_SIDE_PX, bmp_to_png, image_format, image_size,
    )
    from server.services.design_hierarchy import build_design_hierarchy, page_design_hierarchy
    from server.services.project_options import build_project_options, page_project_options
    from server.services.erc import build_erc, page_erc
    from server.services.project_integrity import (
        build_project_integrity, page_project_integrity,
    )
    from server.services.output_jobs import (
        build_output_jobs, draftsman_outputs, output_locations, page_output_jobs,
        variant_outputs,
    )
    from server.services.variants import (
        GROUPS as VARIANT_DIFF_GROUPS, compare as compare_variant_views,
        documentation as variant_documentation, draftsman_variants,
        export_rows as variant_export_rows, group_rows as group_variant_rows,
        same_variant, shown_values as shown_variant_values,
        unvalidated as unvalidated_variant_rows, view_of as variant_view,
    )
    from server.services.fab_drawing import (
        board_facts as fab_board_facts, build_fab_drawing, note_lines, page_fab_drawing,
    )
    from server.services.design_diff import (
        DiffError, diff_folders, diff_revisions, page_design_diff,
    )
    from server.services.project_tour import build_project_tour, defined_variant_count
    from server.services.verify_outputs import (
        SHRINK_STEPS as VERIFY_SHRINK_STEPS, Tolerances as VerifyTolerances,
        page_verify_outputs, run_verify_outputs,
    )
    from server.services.libraries import (
        ItemLoader, LibraryCheckInputs, LibraryResolver, footprint_body_heights,
        installed_libraries, library_part,
        page_library_checks, page_library_part, project_files, project_libraries,
        registry_projects, run_library_checks,
    )
    from server.services import db_parts
    from server.services import multiboard
    from server.parsers.pin_table import PinTableError, parse_pin_file, parse_pin_text
    from server.services.pin_table import (
        check_pins as check_pin_rows, page as page_pin_table, placed_pins, symbol_pins,
        table_description,
    )
    from server.services.release_gate import (
        ReleaseInputs, build_release_readiness, build_release_review,
        page_release_readiness,
    )
    from server.services.review_checklist import (
        EVIDENCE_TOOLS as REVIEW_EVIDENCE_TOOLS, ChecklistError, apply_decisions,
        catalog as checklist_catalog, compare as compare_review_records, design_state,
        folder_signature, load_bundled as load_bundled_checklists, load_checklists,
        page_review, parse_decisions, read_previous as read_previous_record, render_record,
        run_review_checklist as build_review_checklist, select as select_checklist_items,
    )
    from server.services.custom_checks import (
        PLUGIN_FOLDER, PLUGIN_HINT, PACK_FILE, PACK_FOLDER, TOOL_RULE_TOOLS, PackError,
        choose as choose_custom_rules, load_packs as load_check_packs,
        page as page_custom_checks, pack_signature, plugin_files, plugin_id,
        plugins_allowed, rule_row, run_rules as run_custom_rules,
    )
    from server.services.design_api import DesignAPI
    from server.export import export_project, HIGH_FANOUT_THRESHOLD

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")


def _manifest_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "manifest.json"
    return Path(__file__).parent.parent / "manifest.json"


def _read_version() -> str:
    try:
        return json.loads(_manifest_path().read_text(encoding="utf-8"))["version"]
    except Exception:
        return "0.0.0"


STATE_PATH = Path(
    os.environ.get("USERPROFILE") or str(Path.home())
) / ".ee-in-a-box" / "altium-copilot-kc-state.json"


def _is_newer(latest: str, current: str) -> bool:
    def _t(v: str) -> tuple:
        return tuple(int(x) for x in v.split("."))
    return _t(latest) > _t(current)


def _read_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")



_GITHUB_RELEASES_URL = "https://api.github.com/repos/kcisek/altium-copilot-kc/releases/latest"
_UPDATE_CHECK_INTERVAL_HOURS = 24


def _check_for_update(current_version: str) -> None:
    try:
        state = _read_state()
        last_checked = state.get("update_checked_at")
        if last_checked:
            delta = datetime.now(timezone.utc) - datetime.fromisoformat(last_checked)
            if delta.total_seconds() < _UPDATE_CHECK_INTERVAL_HOURS * 3600:
                return
        response = httpx.get(_GITHUB_RELEASES_URL, timeout=5)
        tag = response.json().get("tag_name", "").lstrip("v")
        if not tag:
            return
        state["update_checked_at"] = datetime.now(timezone.utc).isoformat()
        if _is_newer(tag, current_version):
            state["update_available"] = tag
        else:
            state.pop("update_available", None)
        _write_state(state)
    except Exception:
        return


QUERY_NET_MAX_RESULTS = 50
QUERY_NET_MAX_RESULT_SIZE_CHARS = 60_000

SERVER_INSTRUCTIONS = """\
You are an Altium Designer copilot. Use these tools to understand and discuss schematic projects.

## Session Start

At the start of every session, run these steps in order:

1. detect_altium_project — check if Altium is running and surface known projects
2. set_project_dir — load the project using the decision table below (required before any other tool)
3. list_variants — ask the user which variant to work in, then call set_active_variant
   (always ask — do not auto-select even if there is only one variant)
4. get_sheet_context — read the active tab, then get_design_notes (all pages); then
   summarize for the user: project name, sheet count, variant count, and what the
   board does per component descriptions, sheet names and notes.

### Decision table for set_project_dir

| Situation | Action |
|-----------|--------|
| Altium running + project in registry | Call set_project_dir(registry dir) silently |
| Altium running + not in registry, project_path non-empty | Confirm path with user, then call set_project_dir |
| Altium running + not in registry, project_path empty | Ask user for the folder path, then call set_project_dir |
| Altium not running + registry non-empty | Show projects sorted by last_used desc, let user pick, then call set_project_dir |
| Altium not running + registry empty | Ask user for the folder containing the .PrjPcb file |

## Switching Projects

Call set_project_dir with the new path, then repeat the session-start steps.

## General Rules

- Never answer from memory about component values, nets, or topology — always use the tools.
- Only state conclusions supported by tool results. If uncertain — call more tools,
  search a datasheet, or tell the user you are not sure. Do not shortcut to a guess.
- Always work in the context of the active variant. Components with dnp=true are not populated.
  If the user's question implies a different variant, ask before switching.
- Nets with many connections (>25 pins) are likely power or ground rails. query_net will return
  a sample and a warning — treat these as rails, not signals.
- schematic_review — call when the user explicitly asks to review or verify the schematic (e.g. "review this", "check my schematic", "do a design review", "is this correct?", "does this look right?"). Do not call for general questions about the circuit.
- brainstorm_circuits — call when the user wants to brainstorm, or is asking how to design, improve, or choose an approach for a circuit. Any question about topology, architecture, or how to add/change a sub-circuit should trigger this. Do not call for general questions or reviews.
- package_for_xfn — call when the user explicitly asks to export, package, or share the project with firmware, mechanical, test, or reliability engineers (e.g. "package this for the firmware team", "export for sharing", "create a pcb-copilot snapshot"). Do not call speculatively.
- When the user says they have changed or saved anything in the schematic, call `refresh_netlist`
  before answering questions about the updated design. Do not call it speculatively — only after
  the user confirms they have saved in Altium (Ctrl+S).

## Answering Circuit Questions

When the user asks about a component, net, or sub-circuit on a specific sheet:

1. **Start with get_sheet_context** — one call returns every component on the sheet with
   full pin-to-net data and one-hop cross-sheet neighbors (connected_to). This is almost
   always sufficient to answer the question.
2. **For cross-sheet tracing** — `connected_to` already gives you one hop for free
   (refdes, pin, and sheet name). When you need to follow a signal onto a different
   sheet, call get_sheet_context(sheet_name="SHEET") for that sheet — do NOT call
   get_component for each cross-sheet component one-by-one.
3. **For deeper tracing only** — call query_net or get_component when you need to go two
   hops deep on a specific net, or the net is high-fanout (>25 pins).

Do not call get_component or query_net individually for components already visible in a
sheet context you have already loaded.
Do not call get_sheet_context on the same sheet twice in the same conversation turn.

## Design Notes

Designers record intent the netlist cannot show: why a value was chosen, open questions,
DNP reasons, fabrication requirements. Use it.

- get_sheet_context includes the notes of that sheet; get_design_notes covers every sheet,
  the project parameters and the Draftsman (.PCBDwf) drawings. Pass document="<sheet or
  drawing name>" to read one document.
- "(near R4, U1)" lists the components drawn next to a note — a proximity hint, not a
  connection. Confirm with the circuit before relying on it.
- Notes can be stale or wrong. When a note contradicts the circuit (a value, a part
  number), point out the mismatch instead of trusting either side.
- Pictures ([image N] on sheets, [picture N] in drawings) are often datasheet excerpts,
  block diagrams or photos. Call get_design_image(document, index) when one may answer
  the question; do not guess what a picture shows from its file name.
- Draftsman drill tables and layer stack notes are cached from the board at the drawing's
  last update; prefer get_board_info / get_net_pcb for current PCB data, and mention it
  when source_pcb is flagged as not this project's PcbDoc.
- Review comments made in Altium 365 are stored in the cloud workspace and cannot be
  read by these tools. If the user asks about them, say so.

## Sheet Hierarchy

- get_design_hierarchy — "how is the design structured?", "why is this net called X?",
  "how does X get from sheet A to sheet B?", "why isn't this port connected?": the sheet
  tree (sheet symbols, device sheets, Repeat channels), each sheet's ports and its
  symbols' entries, and the Net Identifier Scope with what Automatic resolves to.
  Explain cross-sheet connections from scope.explanation and net names from
  scope.net_naming (name_precedence) plus each port's labels; never assume a scope.
  With a netlist, ports and entries name their net (nets: one per channel). Issues are
  read from the files, not Altium's compiler: quote issue_help and say Compile in
  Altium (Messages panel) is the final word. sheet= narrows to a subtree.

## Project Options

- get_project_options — "what are the project settings?", "is ERC configured right?",
  "why does compile not report X?", "where do outputs go?", "are any No ERCs hiding
  problems?", before an ERC review: the .PrjPcb settings (net scope and naming, diff-pair
  suffixes, class generation, multi-channel naming, ERC levels, Connection Matrix,
  Comparator, output path) and an audit. Quote each audit entry with its default and
  hides/risk; a flagged No ERC is a question for the designer, not a proven fault.
  offline_erc cites run_erc's counts, and offline_erc_hidden what a switched-off check
  hides here. Never guess what an unmapped key means. The saved file is read: Compile in
  Altium is final.

## Project Integrity

- check_project_integrity — "Update PCB wants to delete all my parts", "Constraint Manager
  won't save", "why do compiled designators differ from the sheet?", "is anything broken
  in the project files?", after importing or reannotating a design, before a release:
  legacy file extensions, documents missing on disk, a leftover .Annotation file, parts
  outside the sheet, compile masks, stale variant entries, hidden, non-ASCII or edge-space
  characters in names, bad bus ranges, ActiveBOM-reserved 'Manufacturer N' parameters and
  multi-channel formats that break bus names. Errors break compile, sync or save; warnings
  give silently wrong results. Quote each finding's fix and source; ERC and Compile in
  Altium are the final word.

## Output Jobs

- get_output_jobs — "what does my OutJob generate?", "are my release outputs complete?",
  "is anything missing for fab or assembly?", "are my Gerbers up to date?", before a
  release: each OutJob's containers (path, naming) and outputs by category (connected or
  not, order, data source, variant), coverage (fabrication data, NC drill,
  pick-and-place, assembly drawing, BOM, ERC, DRC) and findings (gaps, BOM and
  pick-and-place on different variants, wrong data source, ERC levels that differ from
  Project Options, paths on another drive or near 260 characters, Gerber vs ODB++ or
  drill origins, overlays missing from the stack, cutouts without a mechanical layer,
  Draftsman variants, outputs older than the design). Quote each finding's severity, fix
  and source, and not_checked; only a connected output is generated. Generating in
  Altium is the final word.

## Generated Outputs

- verify_outputs — "are my outputs right?", "does the pick-and-place / BOM / drill file
  match the board?", "check my release package or zip", before sending files to a
  fabricator or assembler: the files Altium generated (the OutJob folders, or path= a
  folder or .zip) against the saved design: pick-and-place parts, side, rotation and
  Center-X/Y; BOM designators, quantities and columns per variant (a BOM generated from
  ActiveBOM: its part numbers, suppliers and Line # against the BomDoc); IPC-D-356A and
  IPC-2581 nets pin by pin; the IPC-2581 stackup and layer pairs; NC drill tools, hole
  counts and positions per layer pair; files older than the design. A mismatch says the
  file and the saved design differ, not which is right: quote the file and the examples,
  and ask the user to save in Altium and generate again. Quote not_checked (Gerber and
  ODB++ contents are not read).

## Libraries

- get_library_part — "what footprint does U5 use and where is it from?", "what are this
  symbol's pins and pin types?", "how big are this footprint's pads?": a placed part's (or
  a library reference's or footprint's) symbol and footprint read offline from the
  project's libraries, installed libraries and search paths, with where each was found
  or why not, its pins, pads, outlines, Height and 3D bodies.
- check_libraries — "do my footprints still match the library?", "footprint comparison
  report", "run the component rule check", "can I swap U3 to footprint X?", "is this net
  tie built right?", "which parts have no 3D model?", "where else is this part used?":
  parts placed from libraries the project no longer has, each placed footprint against
  its library footprint (Altium's Footprint Comparison Report), pins on nets without a
  pad, Altium's SchLib and PcbLib Component Rule Checks, net ties, 3D bodies against the
  fallback Height and where-used across the registered projects (library= checks a whole
  library). A drift finding says the board and the library differ, not which is right:
  ask the user before suggesting Update From PCB Libraries. Workspace (Altium 365) parts
  are not readable offline: say so instead of calling them missing. DbLib parts take
  their symbol and footprint from a database: the two tools below read it.

## Database Libraries

- get_db_part — "what is the approved MPN and alternates for C12?", "is this part in our
  database at all?", "what does the database say about U3?": the database record behind
  a placed part (designator=), a Design Item ID or a key value, from the project's
  .DbLib, .SVNDbLib and .DbLink files: how it was matched (key field or WHERE clause) and
  how sure (confidence), every mapped field with its parameter, database and schematic
  values, the symbol and footprint the record names, and alternates (same Value and
  footprint). Quote matched_by and confidence; "not found" holds only for the libraries
  that were queried: name skipped ones and their reason.
- check_db_parameters — "are my parameters in sync with the database?", "what would
  Update Parameters From Database change?", "which parts are missing from the database?":
  every database-linked part against its record (would_change, would_add, would_remove,
  record_not_found, duplicate_records, symbol and footprint differences), per library.
  It is a preview: Altium's Update Parameters From Database applies it. Sources are
  opened read-only; servers, ODBC data sources, network shares, OneDrive folders and
  linked tables inside an Access file only with allow_network=true: ask the user before
  passing it. Never ask for or repeat a password; connection strings are shown masked.

## Multi-Board Systems

For a multi-board project (.PrjMbd beside the loaded project or in a folder above it;
system= names another): its boards (modules of the .MbsDoc, each child project compiled
offline; the loaded project is not changed) and connections (direct matings, wires,
cables, harnesses) pin by pin.

- trace_net_across_boards — "where does this net go on the other boards?", "what does
  J5 pin 3 reach on the backplane?" (net="J5-3" with board= its module), "is RESET_N
  carried to the daughter card?": from one board's net through every mated connector pin
  (a cable or harness through its plugs and wires) into the other boards, hop by hop,
  with each board net's other pins.
- check_multiboard — "check my multi-board connections", "do the mated pins carry the
  same signals?", before releasing a system: Altium's five multi-board checks (Different
  Net Names, No Net, No Mated Part, Entry Is Empty, Unresolved Conflict) with the
  .PrjMbd's report levels, plus checks Altium has no counterpart for (nets of one board
  shorted through another, rails and ground across the boards, unwired harness pins,
  connectors changed in a child project since the last import) and a summary per
  connection. Different names are normal when each board names a signal its own way:
  info when the names look like one signal; quote the warnings and errors (a ground
  against a rail, two voltages, another index, P against N, SDA against SCL, a swap) as
  questions, not faults. Altium's Validate in the multi-board schematic is the final
  word.
- get_system_connections — "give me the inter-board pin table", "which pins does C1
  mate?": every mated pin pair (connection, kind, each side's board, connector, pin and
  net), by connection= or board=.
Boards are named by module designator (M1), title or project. When the .MbsDoc is
missing or unreadable, ask the user how the connectors mate and pass mating=[{board_a,
connector_a, board_b, connector_b, pin_map}] (pin_map defaults to the same pin numbers);
never invent a mating.

## Pin Tables

- check_pin_table — "does U5's symbol match the datasheet pinout?", "check this pin table
  against the FPGA", "are any pins swapped?", when the user pastes or points to a pin table
  (table= the text as pasted, path= a .csv/.tsv/.txt/.xlsx or a vendor pinout file): the
  placed part (designator=) and its library symbol, or a symbol (lib_reference=), pin by
  pin. Quote errors first (swapped, shifted, different_name, missing_in_design), then
  warnings; likely_same_name is info: say why it differs (reason), not that it is wrong.
  Check table.columns, package, notes and unread_rows before trusting the result: a wrong
  column or package reads as many differences (verdict poor_match; package= picks
  another), and a partial table leaves pins unchecked (not_in_partial_table). The table
  is what the user gave: never invent one from memory; ask for the datasheet's pin table
  instead.

## Fabrication Drawings

- check_fab_drawing — "is my fab drawing complete?", "check the fabrication notes", "does
  the drawing match the board?", "is the drawing up to date?", before sending a drawing
  to a fabricator or assembler: each Draftsman drawing's kind, a checklist (found with
  the line quoted and where it is, missing with a suggested note line, not applicable)
  and cross-checks of what it states or caches against the stack, the impedance
  profiles, the holes, the BOM and the board's save time. Quote found lines with their
  place; offer suggested_notes as lines to add, with the user filling every <...> value
  (never invent one). A missing item may be worded unusually: read get_design_notes
  before calling it absent. Tables are caches from the last Import Changes.

## Release Readiness

- check_release_readiness — "is this ready to release?", "is it ready for review or fab?",
  "release check", before a release: one table of gates modelled on Altium's Project
  Releaser (DRC report fresh and clean, ERC, netlist and sync, unrouted nets, polygons,
  outline, fiducials, mounting holes, Not Fitted parts in the assembly outputs, OutJob
  per variant and output age, fab notes vs stackup, title blocks, project integrity,
  Altium 365 copy, test access), each with status, blocking, evidence and the source_tool to call for
  details. Quote verdict_summary first. A blocking gate that failed fails the release;
  not_verified is never a pass: say what would verify it (ERC always needs Altium; its
  evidence cites run_erc's offline counts, which never make it pass).
- release_review — a guided go / no-go: the gates, the causes from each source_tool, then
  the blocking items, what was not verified and the warnings.

## Review Checklists

- run_review_checklist — "run the review checklist", "what is left before sign-off?",
  "write the review record", "what changed since the last review?", and first in
  schematic_review, constraints_review, pcb_manufacturing_review and release_review: a
  checklist (schematic, pcb, release, bom, or a team's own; no checklist lists them) whose
  items each get Yes, No, N/A or needs judgment from the deterministic tools (auto: the
  tool decides; assisted: the evidence leans yes or no and the datasheet or the reviewer
  decides; judgment: a person), with evidence, finding ids and anchors (designators,
  nets, sheets), a coverage table and the design state (Git commit, else file SHA1s).
  Quote the coverage table and the No items first. Settle needs-judgment items from the
  evidence, the datasheets or the user, never by guessing; then call again with
  decisions={"ID": "no: reason"} and format='markdown' for the review record (sign-off
  belongs to the design state it names; save it only where the user agrees).
  previous=<a saved record> says what newly fails, what was fixed and what is still open.
  A team's overrides can add custom items: a check-pack rule decides them.

## Custom Check Packs

- run_custom_checks — "run our team checks", "run the check packs", "does it pass our
  rules?", in schematic_review when the project has packs, before a release: the team's
  own deterministic rules (check_packs.json or check_packs/*.json next to the .PrjPcb or
  in the repository root; path= for another pack file). Select-and-assert rules check
  parts, nets or pins (MPN present, TVS on off-board lines, decoupling within N mm, rated
  voltage headroom, one pull-up per I2C line, no small parts on the bottom side ...);
  tool rules turn an existing tool's findings into a pass/fail gate with the team's
  severity. Quote verdict and the failing rules first; could_not_evaluate and
  not_evaluated say what was missing (netlist, PCB, a voltage or rating): say so, never
  count them as passed. Python plugins (check_packs/plugins) run repository code: pass
  allow_plugins=true only when the user asks for them, and they run only if the user also
  started the server with ALTIUM_COPILOT_ALLOW_PLUGINS=1. A pack problem
  (invalid_check_pack) names the file and the place to fix.

## Project Tour

- project_tour — "walk me through this project", "I'm new to this board", "give me a
  tour", "onboard me", "write a project primer": a phased tour (hierarchy, schematic
  blocks and rails, PCB stackup, placement and key nets, outputs, variants and notes)
  ending in a Markdown project primer. Show the primer; save it only where the user
  agrees. A quick summary needs only the session-start steps.

## Design Diff

- diff_design — "what changed since the last release / commit?", "what did commit X
  change?", "write the release notes", "review this revision against the old one": an
  ECO-style changelog between two revisions of the project's Git clone (default: the
  last commit that changed a design file vs the files on disk; with no revisions it
  lists recent_commits, so offer them) or two folders (an archived release). Quote
  ict_hazards first (nets that kept their name but changed pins, designators now on
  another part): they break test fixtures and programs. Nets come from the PcbDoc: a
  schematic change not yet pushed to the board is not seen (check_sch_pcb_sync).

## Bill of Materials

- get_bom gives the BOM of the active variant (or another, or '[No Variations]'): lines
  with quantity, designators, Comment, footprint, manufacturer and MPN, from the compiled
  parts, without Not Fitted and no-BOM parts. Its compare_variants=true shows what each
  variant changes in the BOM; format csv gives the lines as CSV.
- check_bom before a release or when asked whether the BOM is right. mpn_coverage_percent
  low means the project keeps MPNs elsewhere: say so instead of listing every part as
  missing one. With a BomDoc it also checks its part choices (lifecycle_risk,
  single_source, no_solution, out_of_stock_at_qty, stale_cache, bomdoc_out_of_sync).
- get_active_bom — "what did we choose to buy?", "which parts are NRND / obsolete?",
  "approved alternates?", "is there stock for 500 boards?", "screws, bare board, glue?":
  the project's ActiveBOM document (.BomDoc) read offline: line numbers, design parts per
  line (variant applied), ranked manufacturer solutions with suppliers, stock, price at
  the production quantity and lifecycle, favourite suppliers, non-PCB lines and a join
  report (parts without a line, lines whose parts are gone, MPN differences). The need
  is per board times the BomDoc's production quantity; production_quantity= asks for
  another number of boards. Stock, price and lifecycle are a cache: always quote the
  cache date with them, and never present them as current (live supplier lookups are
  not made).
- get_component's mpn falls back to Comment; for part numbers in a BOM use get_bom.
  A line's varies lists values its parts disagree on (varies_count when there are
  more): mention them, the line is not one part.

## Variants

- get_component, get_sheet_context and search_components show the active variant's
  values: a part it varies has variant_changes (or a "variant <name>:" line) with the
  base design's values and any alternate part; a part it does not fit keeps its base
  values. Say which variant each value belongs to.
- compare_variants — "what's different between these two variants?", "what does the
  Lite variant change?", "is each variant documented?", before releasing a variant: every
  fitted / not fitted, value (Value, Comment), other parameter and alternate-part
  difference between a (default: active) and b (default: '[No Variations]'), bound by
  UniqueId like get_bom; its documentation block says per variant whether a connected
  BOM, pick-and-place and assembly-drawing output is set to it and which Draftsman
  drawings are set to it or name it in their notes (info only: get_output_jobs has the
  details). Quote binding stored_designators and stale_variant_entries when present.

## Pull-ups and Floating Nets

- check_pull_resistors — "are my pull-ups right?", "is anything floating?", "are the
  I2C pull-ups the right value?", reset, enable, boot or strap pins, or a schematic
  review: signals that need a defined state (reset, enable, boot and strap nets,
  open-drain and I2C lines, MOSFET gates driven from an IC, comparator outputs) with
  their pulls, and findings when none is fitted, only Not Fitted ones, or both up and
  down. I2C buses get the effective pull-up, Rmin, Rmax and a verdict (i2c_mode
  default fast: ask the bus speed; pass bus_capacitance_pf when known, else Cb is an
  estimate). A heuristic from names and pin types: pull-ups inside ICs are invisible,
  so present a missing pull as a question for the datasheet, not a fault.

## Connector and Interface Protection

- check_interfaces — "are my connectors protected?", "is there ESD on the USB port?",
  "is the shield tied right?", EMC of cables and connectors, or a schematic or layout
  review: per connector its exposure (external, internal, unknown, with the basis) and a
  per-pin table (class, ESD/TVS on the path and on which side of a series part, series
  parts, capacitors, pulls, first IC pin reached); findings for exposed pins without ESD,
  shields through RC or floating, ground pins that reach ground only through a part,
  sparse ground pins, low-speed lines with no series footprint and (with the PcbDoc) a
  TVS far from its connector. Quote exposure_basis and ask the user when a connector's
  exposure is unknown or looks wrong (external= and internal= correct it). Do not judge
  the TVS part (standoff, clamping voltage, capacitance), or a transceiver's own ESD
  claim, from the tool: look up the datasheet. shield_rc quotes guidance, not a fault.

## Power Tree

- get_power_tree — "how is this board powered?", "draw the power tree", "which regulator
  makes 3V3?", "how are EN and PG wired?", "is the sequencing right?", "is the input
  protected?", or the power part of a schematic review: sources (connector power pins,
  batteries), input protection (fuse, reverse polarity, TVS), converters (type and
  basis, EN, PG, feedback divider, capacitors, input filter), rails with what feeds
  them and their load counts, and findings. It is a draft read from names: say so, look
  up each converter's datasheet before trusting a state (an EN with an internal
  pull-up is not floating; held_off depends on the pin's polarity) and check
  implied_vref against the datasheet's Vref (or pass vref=). format='mermaid' gives a
  flowchart to show the user; rail= and part= narrow it; pages follow next_offset.

## Component Derating

- check_derating — "are my capacitors rated high enough?", "check derating", "will this
  cap see too much voltage?", "DC bias on my MLCCs?", "is this resistor dissipating too
  much?", LED or shunt resistors, or the stress part of a schematic review: each
  capacitor's applied voltage against its rating (ceramics 75%, 50% on a switcher's
  input or output; tantalums and electrolytics 80%), DC-bias loss bands for small
  high-value ceramics, resistor working voltage and power. Voltages come from rails=,
  net names, the power tree and resistor dividers (each with source and confidence);
  ratings from parameters, text, part numbers and footprints (each with source). Quote
  sources and say it is a heuristic: confirm findings in the datasheet. Ask the user
  for the voltages of rails_without_voltage and pass rails='VIN=24'; ask for the
  currents needs_current asks for. policy says which thresholds applied
  (derating_policy.json).

## Logic Levels

- check_logic_levels — "do my logic levels match?", "is this 1.8 V output OK into
  that 3.3 V input?", "can this 5 V signal damage the MCU?", FPGA bank voltages, level
  translators, I2C pull-up rails, or the interface part of a schematic review: each
  signal linking ICs on different rails, with each pin's I/O supply (VDDIO, VCCO_n bank
  pins, translator sides, else its only or highest digital supply, with basis and
  confidence) and direction (pin type, else name). Candidates: driver_low_for_receiver
  (VOH < VIH likely), receiver_overvoltage and open_drain_pullup_mismatch (above VDD +
  0.3 V). It only finds candidates: look up VOH, VIH, 5 V tolerance and absolute maximum
  in both datasheets before calling one a fault, and say so. Ask the user for the
  unknown_rails voltages and pass rails='VCCO=3.3'; part= lists each pin's rail.

## Circuit Calculations

- get_circuit_calculations — "check my dividers", "what does this feedback divider
  set?", "at what input does the UVLO trip?", "how much current through this LED or
  opto?", "is the ADC input filtered?", "what is this RC's cutoff?", or the calculation
  part of a schematic review: dividers (ratio, unloaded output, source impedance,
  tolerance range, what they feed: FB with implied Vref, EN with trip voltages, ADC,
  comparator, reference), LED and opto currents from rail, Vf and R (resistor power,
  opto CTR needed), ADC input RC cutoffs and other RC filters, each with its inputs and
  their sources, formula and confidence. Vf, Vref and thresholds are datasheet values:
  present results as calculations to check, not verdicts. Ask the user for the
  voltages_needed and pass rails=; vf= for LED forward voltages, vth= for EN,
  supervisor or comparator thresholds.

## Firmware Pin Map

- get_firmware_pin_map — "what is the MCU's pinout?", "give the firmware team a pin
  header or devicetree", "are TX and RX swapped?", "can we debug or program it?", "what
  are the boot pins set to?", "how is the flash reached?", or a review with the firmware
  team: candidate processors ranked with scores (the top one mapped; mcu= names others),
  and per pin its port and bit, net, the parts it reaches, pull and a likely function
  with its basis; findings for TX meeting TX, debug or programming pins reaching no
  header or test pad, boot and strap levels, external flash or EEPROM access, unused
  pins, shared nets and pins that change between variants. format csv, c_header or
  devicetree gives text for the firmware repo: present it as a starting point. All of it
  comes from names: check pin-mux questions against the datasheet.

## Electrical Rules Check (offline)

- run_erc — "why won't my project compile?", "what does ERC say?", "any ERC errors?",
  "are there floating inputs or net labels, single-pin nets, unconnected wires, unmatched
  ports, duplicate designators?", or first in a schematic review (schematic_review asks
  for it): an offline approximation of Altium's ERC (Messages panel), its violations
  named and worded as Altium does with level, sheet, object, location and a fix, judged
  by the project's Error Reporting levels and Connection Matrix (No Report types counted
  under suppressed, No ERC markers and compile masks honoured). Quote summary first,
  then the errors; say it is an approximation (Altium never saves its ERC result) and
  name not_checked when the user asks about one of those. Off-grid object can run to
  hundreds when a sheet's snap grid differs from the placement grid: a page gives it
  (and off-sheet objects) one row per sheet with a count; give the count, not the list.
  A Duplicate Part Designators note means the .Annotation file made the clash: check in
  Altium before re-annotating. Advisories (rail aliases, name casing, near-miss wire
  ends, ground nets) are info, not Altium violations. report="single_pin_nets" lists
  every single-pin net.

## PCB / Layout Questions

For questions about the physical board (where nets route, layers, stackup, crosstalk,
component locations, distances):

1. **Call get_board_info first** — it returns the stackup, origin, extents, units, and
   data freshness. State the origin and units when discussing any coordinates.
2. get_net_pcb — "where does net X route": layers, lengths, vias, pad endpoints.
3. get_net_neighbors — crosstalk questions: "what runs near X". Proximity results are
   geometric facts; judging whether coupling matters needs rise times, impedances, and
   termination knowledge — ask the user rather than guessing severity.
4. query_pcb_region — "what is at/near (x, y)".
5. get_component_placement — "where is U12, what's around it".
6. get_rules_for_net — "what width / clearance / via / diff-pair rule applies to X":
   the effective rule per kind with its values, resolved by priority the way Altium does.
7. get_design_rules — list or inspect the board's design rules by kind or name.
8. get_classes / list_diff_pairs — net, component and diff-pair classes; pairs with
   their length skew.
9. check_rule_compliance — "do the tracks and vias of X meet their rules": actual widths,
   via sizes, holes and routing layers against the rule applying to each object.
10. check_lengths — Length and MatchedLengths rules: members, measured lengths, spread
    against tolerance, in-pair skew. get_signal_length measures one net, pair or xSignal.
11. check_diff_pairs — undeclared pairs, polarity swaps at pins, AC-coupling symmetry,
    in-pair skew against within-pair rules.
12. check_sch_pcb_sync — "is the PCB in sync with the schematic?", "did I forget Update
    PCB?", before a release: an offline Show Differences (missing/extra parts, designator,
    footprint and comment changes, pads on the wrong net, renamed or one-sided nets) plus
    the ECO's 'Unknown Pin' failures. With netlist_stale, sheets were saved after the
    netlist was compiled: call refresh_netlist (those saves prove the user saved) and run
    it again. Otherwise name sheets saved after the PcbDoc as the likely cause. Altium's
    Show Differences is the final word.
13. get_drc_report — "what does my DRC say?", "any shorts?", "why does DRC fail?":
    Altium's last Batch DRC report, grouped by rule, component or net, linked to the board.
    Quote its assessment first (stopped at the violation limit, rule kinds not checked,
    older than the board); it is the last run's result, not the board as saved now.
14. constraints_review — call when the user asks to review or verify the PCB's high-speed
    constraints or the routing against them ("check my high-speed rules", "are my DDR
    length rules right?", "review the diff pairs before fab"): a whole-interface audit
    of rules and copper against each chip's layout guide. For one net use
    get_rules_for_net; for circuit correctness schematic_review.
15. check_decoupling — "are my ICs decoupled?", "is C12 close enough to U3?", decoupling
    or bypass capacitors in a schematic or layout review: rails with no capacitor to
    ground, only Not Fitted ones, or behind a ferrite without their own; with the PcbDoc,
    each IC rail's nearest capacitor and a warning above 5 mm (name the IC in ic= for
    its 5 nearest). It is advice: rails come from net and pin names, and the datasheet
    decides how many capacitors a part needs.
16. check_vias — "what vias does this board use?", a via or drill review (a fab's
    capability limits: check_dfm), aspect ratio, annular ring, via-in-pad, tenting,
    blind/buried/microvias, back drilling or via stubs: via types with counts per type
    and net class, depth and aspect ratio from the stack, drill pairs and back drills,
    with findings. via_types is the via part of the drill table (component holes and
    slots are not in it); holes are as drawn (finished), which fabricators drill larger.
    Back drilling is recomputed the way Altium writes the drill files; stubs of nets
    with no back-drill rule are advice.
17. check_connectivity — "is the board fully routed?", "what is left to route?", "why
    does DRC say un-routed?", floating copper, net antennae, routing progress: per net,
    whether its saved copper (tracks, vias, pads, the pour copper as last poured,
    regions, fills, assigned internal planes) joins all its pads, with each missing
    connection's nearest pads and distance, a near-miss copper gap, isolated copper and
    antennae; weak (edge-only) joins when checks names weak or the Un-Routed Net rule
    checks incomplete connections (then nets_unrouted_in_altium, routed here only
    through weak joins, read un-routed in Altium). It reads the copper saved
    now; quote altium_drc's report issues before comparing with Altium's last DRC run. Nets
    connected through assumed copper (planes, split-plane or pour outlines) are in
    assumed_copper_nets; say those connections are assumed.
18. check_pcb_health — "run a health check", board clean-up, or before fab outputs:
    Altium's PCB Health Check Monitor offline (Altium's categories, ids and levels:
    shelved/modified polygons, duplicate designators, zero-area and self-intersecting
    shapes, micro-segments, 360deg components, reference points outside the part)
    plus parts outside the board; altium_report compares the last DRC report's health
    counts. Quote not_checked: mirrored footprints, panels and fonts need Altium.
19. check_dfm — "is this board manufacturable?", "DFM check", "can <fab> make this?",
    before ordering: copper, holes and outline against a fab profile (altimade built
    in, fab_profile.json in the project folder, or limits). Say which profile was used
    (scope); never present Altimade's limits as another fabricator's: ask for that
    fab's numbers and pass profile="none" with limits. Answer from each result's status
    and severity together (status advisory: its only misses are info, such as
    Altimade's ordering limits while it is only the default; that is not a fabrication
    miss, so report it as a note), quote not_checked, and say "meets <profile>'s limits
    on the checks run", never just "manufacturable". Assembly (DFA: "can it be
    assembled?", fiducials, tombstoning, part spacing, too many unique parts) is the
    same tool for the active variant: part_counts, assembly_sides, part_spacing,
    part_to_edge, fiducials, tombstoning, fine_pitch, pick_and_place (centroids, board
    origin). A limit the profile does not set
    (part spacing, part to edge, pitch) is measured, not judged: ask the assembler.
20. pcb_manufacturing_review — "is this board ready to order?", "review it for
    manufacturing", "go/no-go before fab": a guided fabrication then assembly review
    ending in a go / no-go that lists what was not checked; it asks which fabricator
    first. One check against a fab profile is check_dfm.
21. get_assembly_guide — "how do I assemble / hand-solder this prototype?", "assembly
    guide", "build sheet", "soldering order": the variant's parts in soldering order
    (the SMD of both sides first, the side with shorter parts first, then through-hole;
    short to tall on each side) in BOM-line steps with Center-X/Y, rotation and pin-1
    or polarity hints, as a Markdown checklist (Done / Skipped) or CSV. Quote
    first_side_reason and left_out (Not Fitted parts, fiducials, parts nobody places);
    without a netlist the steps are approximate (grouping_approximate): say so.
22. check_testpoints — "can this board be tested?", ICT, bed-of-nails or flying-probe
    readiness, test coverage, "which nets have testpoints?", before a test engineer gets
    the board: probe sites (pads and vias marked as Fabrication Testpoints, TP parts) per
    side, the share of nets with access, nets a Testpoint Usage rule or the user's
    critical list (nets) needs without one, rails and ground probe counts, site size,
    spacing, edge clearance, sites under bodies or near tall parts, tooling holes. Its
    numbers are guidance (profile ict or flying_probe): say so, and ask for the test
    house's numbers (limits) before calling the board untestable. Assembly Testpoint
    marks are not read: a net with only those reads as uncovered.
23. check_thermal — "will this regulator run too hot?", "what is U5's junction
    temperature?", "are there enough thermal vias under the exposed pad?", "what sits
    near the hot parts?", or the thermal part of a design or layout review: hot parts
    with their basis (converters, their FETs and diodes, power-path FETs, shunts and
    power resistors, processors, FPGAs, PHYs, drivers, exposed pads), dissipation where
    computable (LDO (Vin - Vout) x I, switcher Vout x I x (1/eta - 1), V^2/R, LED Vf x I)
    and Tj = Ta + thetaJA x P; with the PcbDoc, the vias in each exposed pad or tab (count,
    drill, net, plane or pour reached) and hot parts within 5 mm of each other or of
    electrolytics, crystals, references and sensors. Loads, eta, thetaJA and Tj max come
    from the datasheets and the user: ask for inputs_needed and pass load= ('U5=0.3' or
    '3V3=0.5' amps), eta=, theta= ('U5=45' C/W), ta_max=, power= (W), never guess them.
    Present every number as an estimate: the datasheet and a thermal simulation decide.
24. check_hv_clearance — "is the HV / mains copper far enough from the rest?",
    "check creepage and clearance", "what spacing does 400 V need?", or the high-voltage
    part of a layout review: per pair of nets whose voltages differ by more than 15 V, the
    smallest copper distance on each layer against IPC-2221B Table 6-1 (B1 inner, B2
    outer, B3 above 3050 m, B4 coated, the A columns with standard='ipc2221_assembly') or
    IPC-9592B, with the location and the board's own Clearance rule beside it. Names are
    unreliable for high voltage: ask the user for the working voltages and pass hv=
    ('L=230AC,N=0,HV_BUS=400'; AC is RMS, judged at its peak), groups= and
    working_voltage= across an isolation barrier, min_distance= for an IEC 62368-1 or
    60664-1 figure; unknown_voltage_pairs lists what to ask about. It is a screening tool
    measuring straight-line clearance (creepage around slots is not computed): say so,
    and that the product's spacing and safety standards decide.
25. check_power_paths — "is this power trace wide enough?", "how hot does this track
    get?", "what is the IR drop to U5?", "enough vias for 3 A?", high-current routing, or
    the power-integrity part of a layout review: per rail its current (with its source),
    copper (tracks per layer, vias, pour share), narrowest width, IPC-2221 temperature
    rise, and the least-resistance track and via path from its source to each load with
    its resistance and drop. Currents come from current= ('3V3=0.5' or 'U5=0.3' amps), a
    Power Analyzer setup or 'Current Consumption' parameters: ask the user for
    needs_current's rails (and needs_voltage's voltages), never guess. A drop given as a
    low-high range, or a finding called an upper bound, needs the loads' own currents.
    Pour- and plane-fed rails are not estimated (say so); Power Analyzer by Keysight HTML
    results for this project, when present, replace the estimate: quote them first.
    IPC-2152 is chart-based and not applied. Present every number as an estimate.
26. check_return_path — "do my fast signals keep a continuous reference plane?", "does
    anything cross a plane split?", "which layer does this layer reference?", "is there a
    stitching via where the clock changes layers?", or the return-path part of a
    high-speed layout review: per signal layer its reference layers (impedance profile,
    else stack neighbours) and layers with no plane or pour reference; for the selected
    fast nets (selection.by_reason: confirm them, or name others with net= or net_class=)
    segments crossing a split, over a void or near the reference copper's edge, plane
    splits, and layer changes with no stitching via (or, where the reference net changes,
    no capacitor) within stitch_radius. Places marked likely rest on approximated copper
    (anti-pads, bridged hatch openings): say so. A geometric screen of the saved copper,
    not a field solver: ask the user to repour and save first.
27. check_diff_pair_geometry — "is my USB / PCIe pair routed at the right gap?", "how much
    of this pair is uncoupled?", "do both legs change layers together?", or the pair
    routing part of a high-speed layout review: per pair and layer the coupled gap
    (min / mean / max) and widths against the DiffPairsRouting rule (or the impedance
    profile), stretches closer or farther apart than the rule's gap (breakouts at pads and
    vias, stretches shorter than min_stretch, marginal ones and limits assumed from a
    profile no rule links are info), widths out of range, gap variation, each leg's
    uncoupled length against Max Uncoupled Length (its part inside a pad or via and up to
    50 mil beyond counted as breakout) and layer changes one leg makes without the other.
    Pairs marked inferred are only named as pairs (no rule applies). Page the pairs rows
    with pairs_offset. Altium's DRC checks widths and uncoupled length; the gap stretches
    are this tool's own screen.
28. check_mechanical_fit — "will this board fit the enclosure?", "how tall is the tallest
    part on the bottom?", "is anything over 3 mm under the lid?", "is there room for an M3
    washer at the mounting holes?", "are tall parts on the ICT side?": each part's height
    (3D body, else footprint Height, else library body; parts with no body are
    missing_3d_body), Height rules, the user's limits (height_limit_top / _bottom, regions)
    and a Draftsman note's limit, keepout overlaps, mounting holes with the free radius
    around each (against the screw's ISO head and washer: info by default, a warning with
    screw_head_diameter / washer_diameter), holes near the edge, tall parts on the probe
    side, and connectors with their board edge, offset, overhang and facing. Ask for the
    enclosure's limits when the user has them; quote limit sources.
29. get_mechanical_handoff — "export the outline for the mechanical engineer", "where are
    the mounting holes and connectors?", "give me a DXF of the board": outline with
    cutouts, thickness, mounting holes, connectors, keepout and room regions, rigid-flex
    regions, per-side summaries and part heights; DXF (R12) and SVG only when the user
    names an output_dir. Plan view only: not a STEP or IDX replacement.
30. check_directive_sync — "did my net classes / differential pairs / schematic rules reach
    the board?", "did Update PCB bring my directives over?", "why is this pair not on the
    PCB?": each ClassName, Differential Pair and Rule directive (on wires, buses, harnesses
    and blankets), bus net class and per-sheet component class and room against the
    PcbDoc's classes, pairs, rooms and schematic-defined rules, nets renamed on the PCB
    followed, with whether Class Generation enables each kind (a kind left out gives info
    findings). Quote project_type and summary first: in a Constraint Manager project
    (ConstraintManagerFlow) directives are ignored unless imported, so its findings are
    info. Rule text is decoded leniently: confirm rule_values_differ in Altium. Show
    Differences / Update PCB in Altium is the final word.
31. get_constraints_brief — "write a layout handoff", "what should the layout engineer
    know?", "which net classes / pairs / length matching does this design need?", "where
    do the decoupling caps, TVS diodes and crystals go?": a schematic-to-layout brief with
    each item's evidence and whether the PCB or the directives already cover it: pair
    classes by interface with typical impedance and skew (INTERFACE_TARGETS: say typical,
    never the requirement), power classes by rail current with an IPC-2221 width (state
    copper_oz and temp_rise; currents unknown means no width), HV classes with the
    IPC-2221B clearance, RF nets only with evidence, length-matching groups (pairs, DDR byte
    lanes) with their tolerance's source, decoupling / TVS / crystal / SMPS keep-tight
    placements (measured when the PcbDoc loads), zones per sheet and the designers'
    placement notes quoted; plus a constraint plan to agree with the user. A board rule
    that takes an item but asks less is "weaker", not coverage: say so. Quote needs_input:
    ask, never invent a requirement.
32. generate_rule_file — after the user agrees a plan (pass it as plan=; without plan= it
    rebuilds the brief's, so pass the same current= / hv= / copper_oz= / temp_rise=): a
    .RUL file (Import Rules; its import is untested: say so, suggest a copy of the board,
    selecting only the file's rule kinds and answering No to 'Clear existing rules prior
    to import?'; check the priorities against the board's), CSV class lists and a Markdown
    step list (classic: directives, Design > Classes, Import Rules; Constraint Manager
    projects: its own entries). Files only into the absolute output_dir the user names,
    never over a file; without it the text comes inline. It never edits a design file.

get_board_info lists the stack manager's impedance profiles: target, solved width/gap and
calculated impedance per layer, reference layers, the rules using each, and issues. Quote
these as the design's impedance intent; the fabricator's result is still the final word.

Lengths are measured like Altium's Signal Length: the copper path from pad centre to pad
centre plus the via barrel over the layers it traverses plus the pin/package length stored
on the end pads (xSignals: legs through series parts, pin/package length at the end pins).
Nets without exactly 2 pads use routed copper plus barrels and no pin/package length, like
Altium's Routed Length. A pair's group length is the P/N average. Results marked marginal
are too close to a limit to call. The checks read the saved
rules and copper, not Altium's DRC report (get_drc_report reads that); checked_by_batch_drc false means the designer's
own batch DRC does not check that kind — say so before calling it a problem.

Rule scopes are evaluated per net or pair. A rule marked conditional (with
"conditions") applies only to some of the net's objects — e.g. only its vias, only on
one layer, only inside a room, only its pour copper. Quote the condition instead of
claiming it applies. Binary rules (Clearance, ...) list candidates with orientations:
which one wins depends on the other object, so name the other object when you quote a
clearance. Widths and diff-pair gaps are per layer: quote layers_mil for the layer in
question. A diff-pair net's track width is checked by its DiffPairsRouting rule, not
the Width rule. stale_references are scope names that match nothing on the board, so
that part of the rule silently applies to nothing — worth pointing out. Rule values are
what is stored in the PcbDoc and do not show that the board passes: Altium's own DRC
result is get_drc_report, whose assessment says whether it can be trusted.

Net names are shared between schematic and PCB — pivot freely between query_net
(connectivity) and get_net_pcb (physical routing). PCB tools return both `refdes` (the
designator on the PCB) and `sch_refdes` (the netlist name, used by the schematic tools).
They differ when the board is out of sync with the schematic; PCB-only parts (mounting
holes, logos) have no `sch_refdes`. On multi-channel and
board-annotated designs the schematic sheet's own (logical) refdes is a third name:
get_component_placement reports it as `logical_refdes` and, when nothing else matches,
accepts it, returning every physical instance with its channel path.

PCB data reflects the last SAVE of the .PcbDoc in Altium and refreshes automatically —
if the user made layout changes, ask them to save (Ctrl+S), then call the tool again.
get_board_info reports both PCB and netlist timestamps; if they disagree wildly, warn
the user the schematic and layout may be out of sync.

Before relying on polygon results, ask the user to repour polygons and save the PcbDoc.
The tools model saved nominal polygon outlines, not final region/fill copper; voids,
cutouts, removed islands, and clearances must be verified in Altium. check_connectivity
is the exception: it reads the poured copper as saved (a pour changed since its last
repour is checked as it was poured, and an unpoured one connects nothing).

If the user wants to know where something is visually, give coordinates relative to the
origin and offer to interpret a screenshot or photo they paste for orientation — the
server never renders images.

## Behavioral Guidelines

- **Think Before Proposing:** State your assumptions explicitly. If multiple interpretations of the circuit exist, present them—don't pick silently.
- **Simplicity First:** When suggesting fixes, propose the minimum viable component change. Do not suggest speculative features or over-engineered architectures.
- **Surgical Changes:** Recommend touching only what must be fixed. Do not propose refactoring adjacent, working sub-circuits or nets unless they are directly causing the issue.
- **Goal-Driven Execution:** For complex tracing or multi-step analysis, state a brief plan (e.g., "1. Trace VCC -> verify. 2. Check IC21 inputs -> verify") and loop your tool usage until your success criteria are verified.

## Netlist Freshness

The netlist reflects the last-saved state of the schematic files. If the user has made recent
changes in Altium Designer, ask them to save first (Ctrl+S in Altium) before you answer —
otherwise your data may be stale.

Without Altium the netlist is compiled offline from the saved sheets (netlist_source
offline_compiler; no active tab, so pass sheet_name to get_sheet_context). Answer from it
as from Altium's, pass on compile_warnings, and say Altium's ERC did not run (run_erc
approximates it offline); refresh_netlist compiles
again, or takes Altium's netlist once Altium has the project open.

For an Altium 365 project, set_project_dir's altium365 block says where the local copy
stands (read offline, nothing fetched). Pass its warnings on before answering: an old last
fetch, a copy behind Altium 365, local changes not committed. get_clone_status re-reads it.

## Error Recovery

- netlist_required → the project was loaded without a netlist (partial load: the sheets
  could not be compiled offline, see compile_error, or there are none). Tell the user to fix
  the sheets and call refresh_netlist, or open the project in Altium Designer once, then
  set_project_dir again. Do not retry, and do not answer schematic questions (parts, values,
  pins, nets) from PCB data without saying it is PCB data only
- no_components_loaded (from set_project_dir, refresh_netlist or any component tool) → nets
  loaded but no schematic component metadata did. Tell the user, name any sheet_errors, and do
  not answer component questions from the nets alone or retry search_components in a loop
- sheet_errors in set_project_dir/refresh_netlist → those sheets failed to parse; their
  components are missing. Name the failing sheets and errors to the user
- designator_clashes in set_project_dir/refresh_netlist → on that sheet one refdes names
  two or more different components, and only the last one's metadata is loaded. Tell the
  user each sheet, refdes and lib_references (and designator_clash_count if more than are
  listed), and do not trust MPN, value or pin names of those refdes until they are renamed
  and the netlist is refreshed
- Component not found → call search_components with a regex to locate it
- Net not found → use query_net with a keyword pattern to discover the real name
  (e.g. query_net('MISO') or query_net('CAN')). Never guess net names or
  manually chase anonymous nets one by one.
- no_sheet_open → Altium is running but no active schematic sheet is detected (project may
  or may not be loaded); tell the user to open their project in Altium and open a schematic
  sheet, then try again
- active_document_outside_project → the user has a different project open in Altium;
  ask them to switch or call set_project_dir with the correct path
"""

mcp = FastMCP("altium-copilot-kc", instructions=SERVER_INSTRUCTIONS)
# FastMCP 1.27 reports its framework version unless the low-level server
# version is explicitly set.
mcp._mcp_server.version = _read_version()

# ---------- module-level state ----------
_altium: AltiumClient = AltiumClient()
_project: dict | None = None          # {name, root_dir, prj_pcb_path, sheets: [{name, path}], ...}
_variant_state: VariantState | None = None
_netlist_last_updated: str | None = None
_design_notes = DesignNotesCache()


# Tools that need the compiled netlist; after a netlist-free load they answer
# netlist_required. Every other tool works from the project files alone.
NETLIST_TOOLS = (
    "query_net", "get_component", "search_components", "get_sheet_context", "get_bom",
    "check_bom", "get_active_bom", "check_decoupling", "check_pull_resistors", "check_interfaces",
    "get_power_tree", "check_derating", "check_logic_levels", "get_circuit_calculations",
    "check_thermal", "check_hv_clearance", "check_power_paths", "get_firmware_pin_map",
    "run_erc", "check_sch_pcb_sync", "check_directive_sync", "get_constraints_brief",
    "generate_rule_file", "schematic_review", "package_for_xfn", "get_db_part",
    "check_db_parameters",
)


class ProjectNotReady(ValueError):
    """Raised by _require_project; payload is the structured tool error."""

    def __init__(self, payload: dict):
        super().__init__(payload["message"])
        self.payload = payload


def _netlist_required_error() -> dict:
    name = (_project or {}).get("name")
    return {
        "error": "netlist_required",
        "message": (
            f"Project '{name}' was loaded without a netlist: Altium Designer was not "
            "running, no up-to-date cached netlist was found and the sheets could not be "
            "compiled offline (see compile_error in the load response, or the project lists "
            "no sheet). This tool needs the schematic connectivity. Fix the sheets and call "
            "refresh_netlist, or open the project in Altium Designer once, then call "
            "set_project_dir again; later loads reuse the cached netlist without Altium. "
            "Board, design-rule, length, differential-pair, via, DRC report, design-notes, "
            "picture, sheet-hierarchy and variant tools work now."
        ),
    }


NETLIST_SOURCE_ALTIUM = "altium"
NETLIST_SOURCE_OFFLINE = "offline_compiler"
OFFLINE_NETLIST_NOTE = (
    "Compiled by the copilot from the saved sheets, following the project's net identifier "
    "scope and netlist options; Altium Designer was not involved, so no ERC ran. It matched "
    "Altium's own netlists and PCB pad nets on the projects it was validated on; "
    "compile_warnings lists what it could not settle. Open the project in Altium for the "
    "authoritative netlist."
)
_MAX_COMPILE_WARNINGS = 12  # compile_warnings listed in a load response


def _netlist_source(netlist: dict | None) -> str | None:
    """Where the loaded netlist came from: Altium's .NET or the offline compile."""
    if netlist is None:
        return None
    return NETLIST_SOURCE_OFFLINE if netlist.get("source") == "offline" else NETLIST_SOURCE_ALTIUM


def _offline(netlist: dict | None) -> bool:
    return _netlist_source(netlist) == NETLIST_SOURCE_OFFLINE


def _require_project(netlist: bool = True):
    """The loaded (project, netlist, variant_state). A netlist-free load has netlist
    None: with netlist=False it is returned as is, otherwise netlist_required is raised."""
    if _project is None or _variant_state is None:
        raise ProjectNotReady({
            "error": "no_project",
            "message": "No project loaded. Call set_project_dir first.",
        })
    if netlist and _altium._netlist is None:
        raise ProjectNotReady(_netlist_required_error())
    return _project, _altium._netlist, _variant_state


def _no_components_error(netlist: dict) -> dict | None:
    """Structured error when the netlist has no schematic component metadata, else None."""
    if netlist.get("components"):
        return None
    error = {
        "error": "no_components_loaded",
        "message": (
            f"The netlist has {len(netlist.get('nets') or {})} nets but no component metadata "
            "was loaded from the schematic sheets. Component, sheet and BOM answers are not "
            "available. Check sheet_errors, fix or re-save the failing sheets, then call "
            "refresh_netlist or set_project_dir again."
        ),
    }
    if netlist.get("sheet_errors"):
        error["sheet_errors"] = netlist["sheet_errors"]
    return error


_MAX_DESIGNATOR_CLASHES = 20  # designator_clashes listed in a load response


def _load_diagnostics(netlist: dict) -> dict:
    """Component count, per-sheet parse errors and coverage warnings for load responses."""
    components = netlist.get("components") or {}
    diagnostics: dict = {"component_count": len(components)}
    if netlist.get("nets") is not None or netlist.get("source"):
        diagnostics["netlist_source"] = _netlist_source(netlist)
    if _offline(netlist):
        diagnostics["net_identifier_scope"] = netlist.get("net_scope")
        warnings = netlist.get("compile_warnings") or []
        diagnostics["compile_warnings"] = warnings[:_MAX_COMPILE_WARNINGS]
    if netlist.get("sheet_errors"):
        diagnostics["sheet_errors"] = netlist["sheet_errors"]
    if netlist.get("annotation_errors"):
        diagnostics["annotation_errors"] = netlist["annotation_errors"]
    clashes = netlist.get("designator_clashes") or []
    if clashes:
        diagnostics["designator_clash_count"] = len(clashes)
        diagnostics["designator_clashes"] = clashes[:_MAX_DESIGNATOR_CLASHES]
        diagnostics["designator_clashes_hint"] = (
            "Each refdes names two or more different components on that sheet, one per "
            "entry in lib_references. Only the last one's metadata (MPN, description, "
            "value, pin names) is loaded; the others have none. Rename or re-annotate the "
            "duplicates in Altium, save, then call refresh_netlist."
            + (f" Only the first {_MAX_DESIGNATOR_CLASHES} are listed."
               if len(clashes) > _MAX_DESIGNATOR_CLASHES else "")
        )
    renamed = sum(1 for comp in components.values() if comp.get("logical_refdes"))
    if renamed:
        diagnostics["renamed_by_annotation"] = renamed
        diagnostics["renamed_by_annotation_hint"] = (
            "The project's .Annotation file gives these parts a board designator other than "
            "the one drawn on their sheet. Tools name board designators; logical_refdes is "
            "the sheet's."
        )
    if not components and netlist.get("pin_to_net"):
        error = _no_components_error(netlist)
        diagnostics["warning"] = error["error"]
        diagnostics["message"] = error["message"]
    elif components:
        missing = sorted(set(netlist.get("pin_to_net") or {}) - set(components))
        if missing:
            diagnostics["refdes_without_metadata"] = len(missing)
            diagnostics["refdes_without_metadata_sample"] = missing[:10]
            diagnostics["refdes_without_metadata_hint"] = (
                "These refdes are in the netlist but on no sheet listed in the saved .PrjPcb "
                "(or on a sheet in sheet_errors). If sheets were added in Altium, save the "
                "project, then call refresh_netlist."
            )
    return diagnostics


def _project_documents(prj_data) -> dict:
    """Board, Draftsman and parameter entries of the project state, from a parsed .PrjPcb."""
    return {
        "pcb_doc_paths": prj_data.pcb_doc_paths,
        "pcb_dwf_paths": prj_data.pcb_dwf_paths,
        "parameters": prj_data.parameters,
        "output_path": prj_data.output_path,
        "log_folder_path": prj_data.log_folder_path,
    }


def _design_info(prj_data) -> dict:
    """Project parameters and Draftsman documents for load responses (omitted when empty)."""
    info: dict = {}
    if prj_data.parameters:
        info["project_parameters"] = prj_data.parameters
    if prj_data.pcb_dwf_paths:
        info["draftsman_documents"] = [Path(p).name for p in prj_data.pcb_dwf_paths]
    return info


def _altium365_info(project_dir: str) -> dict:
    """The altium365 block of a load response: where the project's Altium 365 clone
    stands, read offline. Empty when the folder is not in one; never fails a load."""
    try:
        status = clone_status(project_dir)
    except Exception as error:
        logging.warning("Altium 365 clone status failed: %s", error)
        return {}
    if status.get("altium365") is False:
        return {}
    return {"altium365": status}


def _resolve_dnp_uids(variant_state: VariantState, netlist: dict | None) -> None:
    """Bind each variant's Not Fitted entries to the loaded parts the way get_bom does
    (UniqueId path, then its last segment, then the stored designator), so is_dnp and
    every DNP flag agree with the BOM. Without loaded parts, the stored designators and
    the UniqueIds of '?' designators stay as read."""
    netlist = netlist or {}
    uid_map = {
        comp.get("unique_id"): refdes
        for refdes, comp in netlist.get("components", {}).items()
        if comp.get("unique_id")
    }
    variant_state.resolve_dnp_uid(uid_map)
    names = variant_names(netlist)
    if not names:
        return
    for variant in variant_state.variants:
        plan = plan_variant(variant, netlist, names)
        if variant.variations:
            variant.dnp_refdes = sorted(plan.not_fitted, key=natural_key)
        # Varied parameters and alternate parts follow the same binding, for
        # get_component and get_sheet_context.
        variant.bind(plan.params, plan.alternates)


class PcbSession:
    """Lazy-loading, mtime-cached PCB index for the loaded project."""

    def __init__(self):
        self._index = None
        self._path: str | None = None
        self._file_signature: tuple[int, int] | None = None
        self._netlist_ref: dict | None = None

    def invalidate(self) -> None:
        """Drop parsed PCB state when its schematic mapping input changes."""
        self._index = None
        self._path = None
        self._file_signature = None
        self._netlist_ref = None

    def _load(self, path: str, netlist: dict):
        return PcbIndex(parse_pcb_doc(path), netlist)

    def get(self, project: dict, netlist: dict):
        """Return (index, None) or (None, structured_error)."""
        paths = project.get("pcb_doc_paths") or []
        if not paths:
            return None, {
                "error": "no_pcb_document",
                "message": (
                    f"Project '{project.get('name')}' lists no .PcbDoc. "
                    "PCB tools need a board document in the project."
                ),
            }
        path = paths[0]
        pcb_path = Path(path)
        if not pcb_path.exists():
            return None, {
                "error": "pcb_file_missing",
                "message": f"PCB document not found on disk: {path}",
            }
        stat = pcb_path.stat()
        file_signature = (stat.st_mtime_ns, stat.st_size)
        if (
            self._index is not None
            and self._path == path
            and self._file_signature == file_signature
            and self._netlist_ref is netlist
        ):
            return self._index, None
        try:
            index = self._load(path, netlist)
        except Exception as error:
            return None, {
                "error": "pcb_parse_failed",
                "message": str(error),
            }
        self._index = index
        self._path = path
        self._file_signature = file_signature
        self._netlist_ref = netlist
        return index, None


_pcb_session = PcbSession()


# ---------- detect_altium_project ----------

def _detect_altium_project_impl(info: dict, registry: dict) -> dict:
    if not info.get("running"):
        return {"running": False, "registry": registry}
    if not info.get("project_file"):
        return {
            "running": True,
            "warning": "no_sheet_open",
            "message": (
                "Altium is running but no active schematic sheet was detected. "
                "Open your project in Altium and make sure a schematic sheet is open, then try again."
            ),
            "registry": registry,
        }

    project_path = info.get("project_path", "")
    if not project_path:
        entry = next(
            (e for e in registry["projects"]
             if e["name"].lower() == info["project_file"].lower()),
            None,
        )
        if entry:
            project_path = str(Path(entry["dir"]) / info["project_file"])

    return {
        "running": True,
        "project_file": info["project_file"],
        "project_path": project_path,
        "registry": registry,
    }


@mcp.tool(title="Detect Altium Project", annotations=ToolAnnotations(readOnlyHint=True))
def detect_altium_project() -> str:
    """Detect whether Altium Designer is running, which project is open, and known projects."""
    info = _altium.get_status()
    registry = read_registry()
    result = _detect_altium_project_impl(info, registry)
    current = _read_version()
    result["server_version"] = current
    state = _read_state()
    update_available = state.get("update_available")
    if update_available and _is_newer(update_available, current):
        result["update_available"] = update_available
        result["update_command"] = "irm https://raw.githubusercontent.com/kcisek/altium-copilot-kc/main/install.ps1 | iex"
    return json.dumps(result, indent=2)


# ---------- get_clone_status ----------

@mcp.tool(title="Altium 365 Clone Status", annotations=ToolAnnotations(readOnlyHint=True))
def get_clone_status(project_dir: str | None = None) -> str:
    """Where a project's local copy stands against Altium 365, read offline from its Git
    clone: workspace and project GUID, when it was last updated from Altium 365 (last
    fetch), commits ahead of or behind Altium 365 as of that fetch, files changed locally
    and not committed, and the last commit. Nothing is fetched, so changes made in Altium
    365 since then are not seen; warnings flag an old fetch, a copy behind the workspace
    and local changes. set_project_dir returns the same block as altium365: call this
    after the user updates or saves the project in Altium. project_dir defaults to the
    loaded project's folder."""
    if project_dir is None:
        if _project is None:
            return json.dumps({
                "error": "no_project",
                "message": "No project loaded. Call set_project_dir first, or pass project_dir.",
            })
        project_dir = _project["root_dir"]
    if not Path(project_dir).is_dir():
        return json.dumps({"error": "folder_not_found",
                           "message": f"Folder not found: {project_dir}"})
    try:
        status = clone_status(project_dir)
    except Exception as error:
        logging.warning("Altium 365 clone status failed: %s", error)
        return json.dumps({"error": "clone_status_failed",
                           "message": f"{type(error).__name__}: {error}"})
    if status.get("altium365") is False:
        return json.dumps({
            "error": "not_an_altium365_clone",
            "reason": status["reason"],
            "message": (
                "The folder is not in a Git clone whose origin is an Altium 365 project "
                "(https://<workspace>.365.altium.com/git/<project GUID>.git), so there is "
                "no Altium 365 status to read."
            ),
        })
    return json.dumps(status, indent=2)


# ---------- set_project_dir ----------

def _project_context(project_dir: str) -> str | None:
    """The project folder's CLAUDE.md, if any."""
    claude_md = list(Path(project_dir).glob("CLAUDE.md")) + list(Path(project_dir).glob("claude.md"))
    return claude_md[0].read_text(encoding="utf-8") if claude_md else None


def _load_cached_netlist(project_dir: str, prj_pcb_path: str, prj_data, net_path: Path,
                         netlist_mtime: float | None = None) -> str:
    """Full load from an up-to-date .NET on disk, without Altium."""
    global _project, _variant_state, _netlist_last_updated
    project_name = Path(prj_pcb_path).stem
    sheets = [{"name": Path(p).stem, "path": p} for p in prj_data.sheet_paths]
    _project = None
    _variant_state = None
    _altium._netlist = None
    _netlist_last_updated = None
    _pcb_session.__init__()
    _altium.load_netlist_from_file(str(net_path), prj_data.sheet_paths, prj_data.annotation_paths)
    _project = {
        "name": project_name,
        "root_dir": project_dir,
        "prj_pcb_path": prj_pcb_path,
        "sheets": sheets,
        **_project_documents(prj_data),
    }
    _variant_state = VariantState(prj_data.variants)
    _resolve_dnp_uids(_variant_state, _altium._netlist)
    _netlist_last_updated = datetime.now(timezone.utc).isoformat()
    if netlist_mtime is None:
        upsert_registry_entry(Path(prj_pcb_path).name, project_dir)
    else:
        upsert_registry_entry(Path(prj_pcb_path).name, project_dir, netlist_mtime=netlist_mtime)
    response = {
        "loaded": True,
        "project": project_name,
        "sheets": [s["name"] for s in sheets],
        "sheet_count": len(sheets),
        "variants": [v.name for v in prj_data.variants],
        "variant_count": len(prj_data.variants),
        "netlist_updated_utc": _netlist_last_updated,
        **_load_diagnostics(_altium._netlist or {}),
        **_design_info(prj_data),
        **_altium365_info(project_dir),
    }
    context = _project_context(project_dir)
    if context is not None:
        response["project_context"] = context
    return json.dumps(response, indent=2)


def _load_unregistered_netlist(project_dir: str, prj_pcb_path: str) -> str | None:
    """Full load from a .NET that is up to date but not the one the registry recorded
    (Altium regenerated it on its own, the folder moved, a new registry), or None.
    With several candidates (e.g. an archived copy of the project in a subfolder) only
    the one in the project's output folder is trusted."""
    name = Path(prj_pcb_path).stem
    matches = list(Path(project_dir).rglob(f"{name}.NET")) or list(Path(project_dir).rglob(f"{name}.net"))
    if not matches:
        return None
    try:
        prj_data = parse_prj_pcb(prj_pcb_path)
        if len(matches) == 1:
            net_path = matches[0]
        else:
            output = PureWindowsPath(prj_data.output_path or f"Project Outputs for {name}")
            output_dir = (Path(*output.parts) if output.is_absolute()
                          else Path(prj_pcb_path).parent / Path(*output.parts)).resolve()
            net_path = next((m for m in matches if m.parent.resolve() == output_dir), None)
            if net_path is None:
                return None
        sources = netlist_sources(prj_pcb_path, prj_data.sheet_paths, prj_data.annotation_paths)
        if not prj_data.sheet_paths or not is_netlist_fresh(net_path, sources):
            return None
        return _load_cached_netlist(project_dir, prj_pcb_path, prj_data, net_path,
                                    netlist_mtime=net_path.stat().st_mtime)
    except Exception as e:
        logging.warning("Unregistered netlist not loaded: %s", e)
        return None


def _load_offline_netlist(project_dir: str, prj_pcb_path: str, netlist_stale: bool,
                          headless: bool = False) -> dict:
    """Full load with the netlist compiled offline from the saved sheets (sch_netlister):
    Altium Designer is not running and no up-to-date cached netlist exists. Every tool
    works. Falls back to the netlist-free load when the project lists no sheet or the
    compile fails. netlist_stale: a cached .NET exists but sheets were saved after it.
    headless (the check command, cli.py): the project registry is not written and the
    Altium 365 clone status is not read."""
    global _project, _variant_state, _netlist_last_updated
    _project = None
    _variant_state = None
    _altium._netlist = None
    _netlist_last_updated = None
    _pcb_session.__init__()

    prj_data = parse_prj_pcb(prj_pcb_path)
    if not prj_data.sheet_paths:
        return _load_netlist_free(project_dir, prj_pcb_path, netlist_stale, headless=headless)
    try:
        _altium.compile_offline(prj_pcb_path, prj_data.sheet_paths, prj_data.annotation_paths)
    except Exception as e:
        logging.warning("Offline netlist compile failed: %s", e)
        return _load_netlist_free(project_dir, prj_pcb_path, netlist_stale,
                                  compile_error=f"{type(e).__name__}: {e}", headless=headless)
    sheets = [{"name": Path(p).stem, "path": p} for p in prj_data.sheet_paths]
    _project = {
        "name": Path(prj_pcb_path).stem,
        "root_dir": project_dir,
        "prj_pcb_path": prj_pcb_path,
        "sheets": sheets,
        **_project_documents(prj_data),
    }
    _variant_state = VariantState(prj_data.variants)
    _resolve_dnp_uids(_variant_state, _altium._netlist)
    _netlist_last_updated = datetime.now(timezone.utc).isoformat()
    # No netlist_mtime: only Altium's .NET may serve a later cache shortcut.
    if not headless:
        upsert_registry_entry(Path(prj_pcb_path).name, project_dir)
    response = {
        "loaded": True,
        "load_mode": "offline_netlist",
        "project": _project["name"],
        "sheets": [s["name"] for s in sheets],
        "sheet_count": len(sheets),
        "variants": [v.name for v in prj_data.variants],
        "variant_count": len(prj_data.variants),
        "pcb_doc": Path(prj_data.pcb_doc_paths[0]).name if prj_data.pcb_doc_paths else None,
        "netlist_updated_utc": _netlist_last_updated,
        "netlist_note": OFFLINE_NETLIST_NOTE + (
            " A cached Altium netlist exists, but the project or its sheets were saved after "
            "it was generated, so the sheets were compiled instead." if netlist_stale else ""),
        "session_start": (
            "Altium Designer is not running, so there is no active tab: call "
            "get_sheet_context with sheet_name (e.g. the first sheet). Tell the user the "
            "netlist was compiled offline and pass on any compile_warnings."
        ),
        **_load_diagnostics(_altium._netlist or {}),
        **_design_info(prj_data),
        **({} if headless else _altium365_info(project_dir)),
    }
    if netlist_stale:
        response["cached_netlist_stale"] = True
    context = _project_context(project_dir)
    if context is not None:
        response["project_context"] = context
    return response


def _load_netlist_free(project_dir: str, prj_pcb_path: str, netlist_stale: bool,
                       compile_error: str | None = None, headless: bool = False) -> dict:
    """Load the project from its files alone, without a netlist: Altium Designer is not
    running, no up-to-date cached netlist exists and the sheets could not be compiled
    offline (compile_error) or there are none. Board, rule, notes, picture and variant
    tools work; the NETLIST_TOOLS answer netlist_required. headless: as for
    _load_offline_netlist."""
    global _project, _variant_state, _netlist_last_updated
    _project = None
    _variant_state = None
    _altium._netlist = None
    _netlist_last_updated = None
    _pcb_session.__init__()

    prj_data = parse_prj_pcb(prj_pcb_path)
    if not prj_data.sheet_paths and not prj_data.pcb_doc_paths:
        return {
            "error": "no_documents",
            "message": (
                "The .PrjPcb lists no .SchDoc sheet and no .PcbDoc, so there is nothing to "
                "load without Altium. Check the .PrjPcb file."
            ),
        }
    sheets = [{"name": Path(p).stem, "path": p} for p in prj_data.sheet_paths]
    _project = {
        "name": Path(prj_pcb_path).stem,
        "root_dir": project_dir,
        "prj_pcb_path": prj_pcb_path,
        "sheets": sheets,
        **_project_documents(prj_data),
    }
    _variant_state = VariantState(prj_data.variants)
    _resolve_dnp_uids(_variant_state, None)
    if not headless:
        upsert_registry_entry(Path(prj_pcb_path).name, project_dir)

    reason = ("the sheets could not be compiled offline" if compile_error
              else "the project lists no schematic sheet to compile")
    message = (
        "Partial load: Altium Designer is not running, no up-to-date cached netlist was "
        f"found and {reason}, so the project was loaded from its files without a netlist. "
        "The tools in netlist_required_tools need it and answer netlist_required; board, "
        "design-rule, length, differential-pair, via, DRC report, design-notes, picture, "
        "sheet-hierarchy and variant tools work. PCB parts are not linked to schematic parts "
        "(sch_refdes is empty) and DNP comes from the designators stored in the variants. "
        "To load fully, fix the sheets and call refresh_netlist, or open the project in "
        "Altium Designer once and call set_project_dir again; later loads reuse the cached "
        "netlist without Altium."
    )
    if not prj_data.pcb_doc_paths:
        message += " The project lists no .PcbDoc, so the board tools answer no_pcb_document."
    if netlist_stale:
        message += (
            " A cached netlist exists, but the project or its schematic sheets were saved "
            "after it was generated, so it is not used."
        )
    response = {
        "loaded": True,
        "partial": True,
        "load_mode": "netlist_free",
        "project": _project["name"],
        "sheets": [s["name"] for s in sheets],
        "sheet_count": len(sheets),
        "variants": [v.name for v in prj_data.variants],
        "variant_count": len(prj_data.variants),
        "pcb_doc": Path(prj_data.pcb_doc_paths[0]).name if prj_data.pcb_doc_paths else None,
        "netlist_updated_utc": None,
        "netlist_required_tools": list(NETLIST_TOOLS),
        "message": message,
        "session_start": (
            "Tell the user the load is partial and why. Skip get_sheet_context: summarize "
            "the board from get_design_notes and get_board_info instead."
        ),
        **_design_info(prj_data),
        **({} if headless else _altium365_info(project_dir)),
    }
    if netlist_stale:
        response["netlist_stale"] = True
    if compile_error:
        response["compile_error"] = compile_error
    context = _project_context(project_dir)
    if context is not None:
        response["project_context"] = context
    return response


def load_project_headless(prj_pcb_path: str) -> dict:
    """Load a project for the headless check command (cli.py, roadmap #50) without Altium
    Designer, the project registry or the state file: the netlist is compiled offline
    from the saved sheets (a cached .NET is not used: in a fresh checkout its save time
    cannot tell whether it is up to date), else the load is netlist-free. Returns the
    load response; it has an error when the .PrjPcb lists no document."""
    prj = Path(prj_pcb_path).resolve()
    return _load_offline_netlist(str(prj.parent), str(prj), False, headless=True)


@mcp.tool(title="Load Project", annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
def set_project_dir(project_dir: str) -> str:
    """Load an Altium project from disk. Must be called before any other tool.
    If the netlist cache is valid, loads from disk without requiring Altium to be open.
    Otherwise generates the netlist in Altium Designer, which must have the project open.
    When Altium is not running, compiles the netlist offline from the saved sheets
    (load_mode offline_netlist, netlist_source offline_compiler, with compile_warnings):
    every tool works. Only when that compile fails or there are no sheets does it load
    without a netlist (partial: true): the tools listed in netlist_required_tools then
    answer netlist_required."""
    global _project, _variant_state, _netlist_last_updated

    # Find .PrjPcb upfront — needed by both the cache shortcut and the Altium flow.
    prj_files = list(Path(project_dir).glob("*.PrjPcb")) + list(Path(project_dir).glob("*.prjpcb"))
    if not prj_files:
        return json.dumps({
            "error": "no_prjpcb",
            "message": f"No .PrjPcb file found in {project_dir}. Check the path."
        })
    prj_pcb_path = str(prj_files[0])
    project_name = Path(prj_pcb_path).stem

    # Cache shortcut — if registry has a validated netlist mtime and the .NET file matches,
    # load from disk without requiring Altium to be running or on a schematic sheet.
    # The .NET must also be at least as new as every sheet listed in the .PrjPcb (wherever
    # it lives in the tree) and the .PrjPcb itself — otherwise fall through and regenerate.
    _stale_cache = False
    _registry = read_registry()
    for _entry in _registry.get("projects", []):
        if Path(_entry.get("dir", "")).resolve() == Path(project_dir).resolve():
            _stored_mtime = _entry.get("netlist_mtime")
            if _stored_mtime is not None:
                _net_matches = (
                    list(Path(project_dir).rglob(f"{project_name}.NET")) or
                    list(Path(project_dir).rglob(f"{project_name}.net"))
                )
                if _net_matches:
                    try:
                        if abs(_net_matches[0].stat().st_mtime - _stored_mtime) < 0.01:
                            prj_data = parse_prj_pcb(prj_pcb_path)
                            _fresh = bool(prj_data.sheet_paths) and is_netlist_fresh(
                                _net_matches[0],
                                netlist_sources(
                                    prj_pcb_path, prj_data.sheet_paths, prj_data.annotation_paths
                                ),
                            )
                            _stale_cache = bool(prj_data.sheet_paths) and not _fresh
                            if _fresh:
                                return _load_cached_netlist(
                                    project_dir, prj_pcb_path, prj_data, _net_matches[0]
                                )
                    except Exception as e:
                        logging.warning("Registry cache shortcut failed: %s", e)
            break

    # Full Altium flow — required when cache is absent or stale. Without Altium the
    # netlist is compiled offline from the saved sheets.
    status = _altium.get_status()
    if not status.get("running"):
        loaded = None if _stale_cache else _load_unregistered_netlist(project_dir, prj_pcb_path)
        if loaded is not None:
            return loaded
        return json.dumps(_load_offline_netlist(project_dir, prj_pcb_path, _stale_cache),
                          indent=2)
    if status.get("warning") == "no_sheet_open":
        return json.dumps({
            "error": "no_sheet_open",
            "message": (
                "Altium is running but no active schematic sheet was detected. "
                "Open a schematic sheet in Altium, then try again."
            ),
        })
    if status.get("tab_type") == "other":
        active_tab = status.get("active_tab") or "unknown"
        return json.dumps({
            "error": "no_schematic_active",
            "message": (
                f"Altium is showing '{active_tab}', not a schematic sheet. "
                "Please click on a schematic sheet tab in Altium and try again."
            ),
        })

    altium_project_file = status.get("project_file", "")
    if altium_project_file.lower() != Path(prj_pcb_path).name.lower():
        return json.dumps({
            "error": "project_mismatch",
            "message": (
                f"Altium has '{altium_project_file}' open, but you asked to load "
                f"'{Path(prj_pcb_path).name}'. Open that project in Altium first, then try again."
            ),
            "altium_open": altium_project_file,
            "requested": Path(prj_pcb_path).name,
        })
    # Clear state before attempting generation — if it fails the server is
    # in a clean "no project loaded" state rather than silently serving stale data.
    _project = None
    _variant_state = None
    _altium._netlist = None
    _netlist_last_updated = None
    _pcb_session.__init__()

    prj_data = parse_prj_pcb(prj_pcb_path)

    if not prj_data.sheet_paths:
        return json.dumps({
            "error": "no_sheets",
            "message": "No .SchDoc sheets found in project. Check the .PrjPcb file."
        })

    sheets = [{"name": Path(p).stem, "path": p} for p in prj_data.sheet_paths]

    try:
        _altium.generate_netlist(
            prj_pcb_path,
            sheet_paths=prj_data.sheet_paths,
            annotation_paths=prj_data.annotation_paths,
        )
    except (RuntimeError, OSError) as e:
        return json.dumps({
            "error": "netlist_generation_failed",
            "message": str(e),
        })

    _project = {
        "name": Path(prj_pcb_path).stem,
        "root_dir": project_dir,
        "prj_pcb_path": prj_pcb_path,
        "sheets": sheets,
        **_project_documents(prj_data),
    }
    _variant_state = VariantState(prj_data.variants)
    _resolve_dnp_uids(_variant_state, _altium._netlist)
    _netlist_last_updated = datetime.now(timezone.utc).isoformat()

    _net_matches = (
        list(Path(project_dir).rglob(f"{project_name}.NET")) or
        list(Path(project_dir).rglob(f"{project_name}.net"))
    )
    _net_mtime = _net_matches[0].stat().st_mtime if _net_matches else None
    upsert_registry_entry(Path(prj_pcb_path).name, project_dir, netlist_mtime=_net_mtime)

    response = {
        "loaded": True,
        "project": _project["name"],
        "sheets": [s["name"] for s in sheets],
        "sheet_count": len(sheets),
        "variants": [v.name for v in prj_data.variants],
        "variant_count": len(prj_data.variants),
        "netlist_updated_utc": _netlist_last_updated,
        **_load_diagnostics(_altium._netlist or {}),
        **_design_info(prj_data),
        **_altium365_info(project_dir),
    }

    context = _project_context(project_dir)
    if context is not None:
        response["project_context"] = context

    return json.dumps(response, indent=2)


# ---------- refresh_netlist ----------

@mcp.tool(title="Refresh Netlist", annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
def refresh_netlist() -> str:
    """Refresh the netlist after the user has saved schematic changes in Altium.
    Call this when the user says they changed, added, or saved anything in the schematic.
    Do not call speculatively — only when the user confirms they have saved.
    After a netlist-free load (partial: true) it loads the project again, fully
    when Altium Designer now has it open or the sheets now compile. An offline netlist
    (netlist_source offline_compiler) is compiled again from the saved sheets, or
    generated by Altium Designer when it has the project open."""
    global _netlist_last_updated, _variant_state
    try:
        project, netlist, variant_state = _require_project(netlist=False)
    except ProjectNotReady as e:
        return json.dumps(e.payload)
    if netlist is None:
        return _reload_partial(project, variant_state)
    offline = _offline(netlist)
    if offline:
        try:
            status = _altium.get_status()
        except Exception as e:  # status unknown: compile offline again
            logging.warning("Altium status failed: %s", e)
            status = {}
        # Altium's netlist only when Altium has this very project open.
        offline = not (status.get("running") and (status.get("project_file") or "").lower()
                       == Path(project["prj_pcb_path"]).name.lower())
    # Re-read the sheet list so sheets added, moved or removed since load are picked up
    # and the sheet names stay in step with the components' sheet attribution.
    prj_data = None
    variant_note = None
    sheet_paths = [s["path"] for s in project.get("sheets", [])]
    annotation_paths = None
    try:
        prj_data = parse_prj_pcb(project["prj_pcb_path"])
        sheet_paths = prj_data.sheet_paths
        annotation_paths = prj_data.annotation_paths
    except Exception as e:
        logging.warning("Could not re-read .PrjPcb, using the sheet list from load: %s", e)
    try:
        if offline:
            _altium.compile_offline(project["prj_pcb_path"], sheet_paths, annotation_paths)
            regenerated = True
        else:
            # An offline netlist is replaced by Altium's even when its .NET was fresh.
            regenerated = _altium.generate_netlist(
                project["prj_pcb_path"], sheet_paths=sheet_paths,
                annotation_paths=annotation_paths,
            ) or _offline(netlist)
    except Exception as e:
        error = "compile_failed" if offline else "generate_failed"
        return json.dumps({"error": error, "message": f"{type(e).__name__}: {e}" if offline
                           else str(e)})
    if prj_data is not None:
        project["sheets"] = [{"name": Path(p).stem, "path": p} for p in prj_data.sheet_paths]
        project.update(_project_documents(prj_data))
        # Variants saved since load (Not Fitted parts, parameter variations, new
        # variants) come with the project file; the active one stays by name.
        active = variant_state.active.name
        variant_state = VariantState(prj_data.variants)
        if variant_state.get(active):
            variant_state.set_variant(active)
        else:
            variant_note = (
                f"The active variant '{active}' is no longer in the project (renamed or "
                f"deleted); '{variant_state.active.name}' is active now. Ask the user which "
                "variant to work in, then call set_active_variant."
            )
        _variant_state = variant_state
    _resolve_dnp_uids(variant_state, _altium._netlist)
    diagnostics = _load_diagnostics(_altium._netlist or {})
    if not regenerated:
        message = "Netlist is already up to date. If you expected changes, save in Altium and try refresh again."
        if variant_note:
            message += f"\n{variant_note}"
        if diagnostics.get("warning"):
            message += f"\nWarning ({diagnostics['warning']}): {diagnostics['message']}"
        for err in diagnostics.get("sheet_errors", []):
            message += f"\nSheet '{err['sheet']}' failed to load: {err['error']}"
        for err in diagnostics.get("annotation_errors", []):
            message += f"\nAnnotation file '{Path(err['path']).name}' not applied: {err['error']}"
        clashes = diagnostics.get("designator_clashes", [])
        for clash in clashes:
            message += (
                f"\nSheet '{clash['sheet']}': {clash['refdes']} names "
                f"{len(clash['lib_references'])} different components "
                f"({', '.join(clash['lib_references'])}); only the last one is loaded"
            )
        if diagnostics.get("designator_clash_count", 0) > len(clashes):
            message += (f"\n... and {diagnostics['designator_clash_count'] - len(clashes)} "
                        "more designator clashes")
        return message
    _pcb_session.invalidate()
    _netlist_last_updated = datetime.now(timezone.utc).isoformat()
    _rn_name = Path(project["prj_pcb_path"]).stem
    _rn_dir = project["root_dir"]
    if offline:
        # An offline compile never vouches for a .NET on disk.
        upsert_registry_entry(Path(project["prj_pcb_path"]).name, _rn_dir)
    else:
        _rn_net = (
            list(Path(_rn_dir).rglob(f"{_rn_name}.NET")) or
            list(Path(_rn_dir).rglob(f"{_rn_name}.net"))
        )
        upsert_registry_entry(
            Path(project["prj_pcb_path"]).name,
            _rn_dir,
            netlist_mtime=_rn_net[0].stat().st_mtime if _rn_net else None,
        )
    result = {
        "refreshed": True,
        "netlist_updated_utc": _netlist_last_updated,
        **diagnostics,
    }
    if offline:
        result["netlist_note"] = OFFLINE_NETLIST_NOTE
    if variant_note:
        result["variant_note"] = variant_note
    return json.dumps(result)


def _reload_partial(project: dict, variant_state: VariantState) -> str:
    """refresh_netlist after a netlist-free load: load the project again, fully once
    Altium has it open. The active variant stays; if the full load fails after it
    cleared the state, the partial load is restored."""
    active = variant_state.active.name
    result = json.loads(set_project_dir(project["root_dir"]))
    if "error" in result and _project is None:
        if _load_netlist_free(project["root_dir"], project["prj_pcb_path"], False).get("loaded"):
            result["partial_load_kept"] = True
            result["message"] = (
                result.get("message", "") + " The partial load is kept: board, rule, notes "
                "and variant tools still work."
            ).strip()
    if _variant_state is not None:
        if _variant_state.get(active):
            _variant_state.set_variant(active)
        else:
            result["variant_note"] = (
                f"The active variant '{active}' is no longer in the project (renamed or "
                f"deleted); '{_variant_state.active.name}' is active now. Ask the user which "
                "variant to work in, then call set_active_variant."
            )
        if "error" not in result:
            result["active_variant"] = _variant_state.active.name
    return json.dumps(result, indent=2)


# ---------- query_net ----------

def _query_net_impl(netlist: dict, pattern: str) -> str:
    nets = netlist["nets"]

    # Try regex first for discovery (e.g. 'UART' finds MCU_UART_TX and USB_UART_RX).
    # If regex is invalid or finds nothing, fall back to exact case-insensitive match
    # so PCB net names with metacharacters (+5V, VIN[0]) resolve without escaping.
    try:
        regex = re.compile(pattern, re.IGNORECASE)
        matches = [name for name in nets if regex.search(name)]
    except re.error:
        matches = []
    if not matches:
        exact_key = next((k for k in nets if k.lower() == pattern.lower()), None)
        if exact_key is not None:
            matches = [exact_key]

    if not matches:
        return json.dumps({"error": "net_not_found", "pattern": pattern,
                           "message": f"No nets matching '{pattern}'. Use a broader pattern or check the name."})

    if len(matches) > QUERY_NET_MAX_RESULTS:
        return json.dumps({
            "error": "too_many_matches",
            "message": f"Pattern matched {len(matches)} nets (limit: {QUERY_NET_MAX_RESULTS}). Be more specific.",
        })

    results = []
    for net_key in matches:
        all_connections = nets[net_key]
        if len(all_connections) > HIGH_FANOUT_THRESHOLD:
            results.append({
                "net": net_key,
                "pin_count": len(all_connections),
                "warning": "high_fanout",
                "message": (
                    f"Net '{net_key}' has {len(all_connections)} connections — likely a power rail or ground. "
                    "Showing a sample of 10 connections only."
                ),
                "pins_sample": [{"refdes": r, "pin": p} for r, p in all_connections[:10]],
            })
        else:
            results.append({
                "net": net_key,
                "pin_count": len(all_connections),
                "pins": [{"refdes": r, "pin": p} for r, p in all_connections],
            })

    source = {"netlist_source": NETLIST_SOURCE_OFFLINE} if _offline(netlist) else {}
    if len(results) == 1:
        return json.dumps({**results[0], **source}, indent=2)
    return json.dumps({"match_count": len(results), "nets": results, **source}, indent=2)


@mcp.tool(title="Query Net", annotations=ToolAnnotations(readOnlyHint=True),
          meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS})
def query_net(pattern: str) -> str:
    """Find nets by name and return every component pin connected to them.
    Accepts a case-insensitive regex pattern for discovery (e.g. 'UART' finds all
    UART nets, 'SPI_CLK|SPI_CS' finds both, 'GND' finds GND/AGND/DGND).
    If the pattern is not valid regex (e.g. +5V, VIN[0] contain metacharacters),
    it automatically falls back to an exact case-insensitive match — so common
    power rail names like +5V and +3V3 work without escaping.
    High-fanout nets (power rails, ground) are flagged and sampled at 10 pins.
    Use get_sheet_context for full pin-to-net data on a sheet; use this tool to
    discover net names or trace a specific net across sheets."""
    try:
        _, netlist, _ = _require_project()
    except ProjectNotReady as e:
        return json.dumps(e.payload)
    return _query_net_impl(netlist, pattern)


# ---------- get_component ----------

def _get_component_impl(netlist: dict, variant_state: VariantState, refdes: str) -> str:
    no_components = _no_components_error(netlist)
    if no_components:
        return json.dumps(no_components)
    components = netlist.get("components", {})
    matched = next((k for k in components if k.lower() == refdes.lower()), None)
    if matched is None:
        # On a board-annotated project, a sheet designator that names no board part.
        placed = [k for k, comp in components.items()
                  if (comp.get("logical_refdes") or "").lower() == refdes.lower()]
        if len(placed) > 1:
            return json.dumps({
                "error": "component_not_found",
                "message": (
                    f"'{refdes}' is a sheet designator placed {len(placed)} times; on the "
                    f"board it is {', '.join(placed)}. Call get_component with one of those."
                ),
                "board_designators": [
                    {"refdes": k, "sheet": components[k].get("sheet")} for k in placed
                ],
            })
        matched = placed[0] if placed else None
    if matched is None:
        return json.dumps({
            "error": "component_not_found",
            "message": f"Component '{refdes}' not found. Use search_components to find available components.",
        })

    comp = components[matched]
    detail = {"refdes": matched}
    if comp.get("logical_refdes"):
        detail["logical_refdes"] = comp["logical_refdes"]
    shown, changes = shown_variant_values(variant_state, matched, comp)
    result = {
        **detail,
        **shown,
        "dnp": variant_state.is_dnp(matched),
        "sheet": comp.get("sheet"),
    }
    if changes:
        # The active variant's values are shown above; variant_changes says what the
        # base design has.
        result["variant_changes"] = changes
    result["pins"] = comp.get("pins", {})
    return json.dumps(result, indent=2)


@mcp.tool(title="Get Component", annotations=ToolAnnotations(readOnlyHint=True))
def get_component(refdes: str) -> str:
    """Get full detail for one component: MPN, value, DNP status, and every pin with its net.
    Use this to drill into a specific component and trace its connections.
    refdes is the board designator; logical_refdes, when present, is the different one
    drawn on the sheet (board-level annotation), also accepted as refdes when no board
    part has that name.

    mpn, description and value are the active variant's: when the variant varies the
    part, variant_changes lists each changed field with its base value (fields), the
    varied parameters that differ from the base design's (parameter, value, base) and an
    alternate part (lib_reference, library, footprint, with base_lib_reference and
    base_footprint). A part the variant does not fit (dnp) keeps its base values."""
    try:
        _, netlist, variant_state = _require_project()
    except ProjectNotReady as e:
        return json.dumps(e.payload)
    return _get_component_impl(netlist, variant_state, refdes)


# ---------- search_components ----------

def _search_components_impl(netlist: dict, pattern: str, search_by: str,
                            variant_state: VariantState | None = None) -> str:
    if search_by not in ("refdes", "mpn", "description"):
        return json.dumps({
            "error": "invalid_search_by",
            "message": "search_by must be 'refdes', 'mpn', or 'description'",
        })

    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        return json.dumps({"error": "invalid_pattern", "message": f"Invalid regex: {e}"})

    no_components = _no_components_error(netlist)
    if no_components:
        return json.dumps(no_components)

    components = netlist.get("components", {})
    matches: dict = {}
    for refdes, comp in components.items():
        # The active variant's MPN, description and value, as get_component shows them.
        comp = {**comp, **shown_variant_values(variant_state, refdes, comp)[0]}
        if search_by == "refdes":
            target = refdes
        elif search_by == "mpn":
            target = comp.get("mpn") or ""
        else:
            target = comp.get("description") or ""

        if regex.search(target):
            matches[refdes] = comp

    if len(matches) == len(components):
        return json.dumps({
            "error": "too_many_matches",
            "message": f"Pattern matched all {len(components)} components. Be more specific.",
        })

    # Group by MPN
    groups: dict[str, dict] = {}
    for refdes, comp in matches.items():
        mpn_key = comp.get("mpn") or f"__no_mpn_{refdes}__"
        if mpn_key not in groups:
            groups[mpn_key] = {
                "mpn": comp.get("mpn"),
                "description": comp.get("description"),
                "count": 0,
                "refdes": [],
                "_values": set(),
            }
        groups[mpn_key]["count"] += 1
        groups[mpn_key]["refdes"].append(refdes)
        if comp.get("value"):
            groups[mpn_key]["_values"].add(comp["value"])

    results = []
    for group in groups.values():
        values = group.pop("_values")
        group["value"] = next(iter(values)) if len(values) == 1 else None
        results.append(group)

    return json.dumps({
        "results": results,
        "match_count": sum(g["count"] for g in results),
    }, indent=2)


@mcp.tool(title="Search Components", annotations=ToolAnnotations(readOnlyHint=True))
def search_components(pattern: str, search_by: str = "description") -> str:
    """Search components by regex pattern. search_by: 'refdes', 'mpn', or 'description'.
    Returns components grouped by MPN with counts. Use get_component for full pin detail.
    MPN, description and value are the active variant's, as get_component shows them."""
    try:
        _, netlist, variant_state = _require_project()
    except ProjectNotReady as e:
        return json.dumps(e.payload)
    return _search_components_impl(netlist, pattern, search_by, variant_state)


# ---------- get_sheet_context ----------

def _get_sheet_context_impl(project: dict, netlist: dict, variant_state: VariantState,
                             sheet_name: str | None, altium_status: dict,
                             offset: int = 0, notes_cache: DesignNotesCache | None = None) -> str:
    if sheet_name is None:
        if not altium_status.get("running"):
            return json.dumps({
                "warning": "altium_not_running",
                "message": "Altium Designer is not running. Open Altium and try again.",
            })
        active_tab = altium_status.get("active_tab", "")
        # Strip Altium's unsaved-changes marker (" *") from the tab title
        active_tab = active_tab.rstrip(" *").strip()
        tab_to_sheet = {Path(s["path"]).name.lower(): s["name"] for s in project["sheets"]}
        # Altium tab titles may omit the file extension — also index by stem
        tab_to_sheet.update({Path(s["path"]).stem.lower(): s["name"] for s in project["sheets"]})
        matched = tab_to_sheet.get(active_tab.lower())
        if not matched:
            return json.dumps({
                "warning": "active_document_outside_project",
                "active_filename": active_tab,
                "message": (
                    f'"{active_tab}" does not belong to project "{project["name"]}". '
                    "Switch to a sheet in this project."
                ),
            })
        sheet_name = matched
    else:
        matched = next((s["name"] for s in project["sheets"]
                        if s["name"].lower() == sheet_name.lower()), None)
        if matched is None:
            available = [s["name"] for s in project["sheets"]]
            return json.dumps({
                "error": "sheet_not_found",
                "message": f"Sheet '{sheet_name}' not found. Available: {available}",
            })
        sheet_name = matched

    no_components = _no_components_error(netlist)
    if no_components:
        return json.dumps(no_components)
    context = build_sheet_context(netlist, sheet_name, variant_state, offset)
    if _offline(netlist):
        header, _, rest = context.partition("\n")
        context = f"{header}\nnetlist_source:{NETLIST_SOURCE_OFFLINE}\n{rest}"
    sheet_error = next((e for e in netlist.get("sheet_errors") or []
                        if e.get("sheet", "").lower() == sheet_name.lower()), None)
    if sheet_error:
        context = (
            f"warning: sheet_load_failed — '{sheet_name}' could not be parsed "
            f"({sheet_error['error']}); its components are missing below.\n" + context
        )
    if notes_cache is not None and offset == 0:
        # Designer notes go between the header and the components, on the first page only.
        notes = sheet_notes_for_context(notes_cache, project, sheet_name)
        if notes:
            header, _, components = context.partition("\n\n")
            context = f"{header}\n\n{notes}\n\n{components}"
    return context


@mcp.tool(title="Get Sheet Context", annotations=ToolAnnotations(readOnlyHint=True),
          meta={"anthropic/maxResultSizeChars": MAX_RESULT_SIZE_CHARS})
def get_sheet_context(sheet_name: str | None = None, offset: int = 0) -> str:
    """Get all components on a schematic sheet with their pin-to-net connections and
    one-hop cross-sheet neighbors. Pass sheet_name to load any sheet by name — not just
    the active Altium tab. Use this as the FIRST tool for any question about a sheet, and
    again with sheet_name when following cross-sheet signals. Only call query_net or
    get_component afterward for high-fanout nets (>25 pins) or two-hop tracing.

    Components show the active variant's MPN, description and value; a part the variant
    varies (parameters, alternate part) has a "variant <name>:" line with the base
    design's values, and a part it does not fit ([DNP]) keeps its base values.

    The first page also carries a design_notes block: the designer's notes, text frames,
    labels, pictures, directives and title block on that sheet. "(near R4, U1)" names the
    components drawn next to a note — a proximity hint, not a connection. Notes can be
    stale: when one contradicts the circuit, point out the mismatch.

    Results are paginated by character budget. If the response contains has_more:true,
    call this tool again with the offset from the next: line and the same sheet_name,
    and repeat until has_more:false. Accumulate all pages before answering the user."""
    try:
        project, netlist, variant_state = _require_project()
    except ProjectNotReady as e:
        return json.dumps(e.payload)

    altium_status = _altium.get_status() if sheet_name is None else {}
    return _get_sheet_context_impl(project, netlist, variant_state, sheet_name, altium_status,
                                   offset, notes_cache=_design_notes)


# ---------- get_design_notes ----------

@mcp.tool(title="Get Design Notes", annotations=ToolAnnotations(readOnlyHint=True),
          meta={"anthropic/maxResultSizeChars": NOTES_MAX_RESULT_SIZE_CHARS})
def get_design_notes(document: str | None = None, offset: int = 0) -> str:
    """Read what designers wrote into the project besides the circuit itself: project
    parameters; on every schematic sheet the notes (author, collapsed state), text frames,
    free text labels, pictures, directives and title-block parameters; and in every
    Draftsman drawing (.PCBDwf) the fabrication/assembly notes, free text, tables, view
    titles, pictures, and the drill table and layer stack notes cached from the board.

    Pass document (a sheet name or Draftsman file name) to read one document. Schematic
    notes, text frames and labels list the components drawn next to them ("near ...") —
    a proximity hint, not a connection. Notes record intent, open questions and
    manufacturing requirements, but can be stale: report a note that contradicts the
    circuit instead of trusting either. Pictures ([image N], [picture N]) are often
    datasheet excerpts or block diagrams: call get_design_image to look at one rather
    than guessing from its file name. Draftsman drill tables and layer notes are cached
    from the board at the drawing's last update, and source_pcb is flagged when that was
    not this project's PcbDoc. Review comments made in Altium 365 live in the cloud
    workspace and cannot be read here.

    Results are paginated by character budget. If the response contains has_more:true,
    call again with the offset from the next: line until has_more:false."""
    try:
        project, _, _ = _require_project(netlist=False)
    except ProjectNotReady as e:
        return json.dumps(e.payload)
    return build_design_notes(_design_notes, project, document, offset)


# ---------- get_design_image ----------

def _linked_image_path(sheet_path: str, file_name: str | None) -> Path | None:
    """The picture file a schematic links to, if it is on disk.

    A relative link is resolved against the sheet's folder. An absolute link that no
    longer exists (e.g. the project was moved) falls back to its file name next to the sheet.
    """
    if not file_name:
        return None
    windows_path = PureWindowsPath(file_name)
    sheet_dir = Path(sheet_path).parent
    if windows_path.is_absolute():
        candidates = [Path(file_name), sheet_dir / windows_path.name]
    else:
        candidates = [sheet_dir / Path(*windows_path.parts)]
    return next((c for c in candidates if c.is_file()), None)


def _get_design_image_impl(project: dict, notes_cache: DesignNotesCache,
                           document: str, index: int) -> Image | dict:
    resolved = resolve_document(project, document)
    if resolved is None:
        available = [s["name"] for s in project.get("sheets") or []] + [
            d["name"] for d in draftsman_documents(project)
        ]
        return {"error": "document_not_found",
                "message": f"'{document}' is not a sheet or Draftsman document. Available: {available}"}
    kind, doc = resolved
    not_found = {"error": "image_not_found",
                 "message": f"'{doc['name']}' has no picture {index}. "
                            "get_design_notes lists the pictures of each document with their index."}
    try:
        if kind == "sheet":
            image = next((i for i in notes_cache.sheet(doc["path"]).items
                          if i.kind == "image" and i.image_index == index), None)
            if image is None:
                return not_found
            if image.embedded:
                data = read_sch_doc_image(doc["path"], image.file_name or "")
            else:
                linked = _linked_image_path(doc["path"], image.file_name)
                if linked is None:
                    return {"error": "linked_image_missing",
                            "message": f"Picture {index} on '{doc['name']}' links to "
                                       f"'{image.file_name}', which is not a readable image on disk."}
                data = linked.read_bytes()
        else:
            pictures = notes_cache.draftsman(doc["path"]).pictures
            if not 0 <= index < len(pictures):
                return not_found
            data = pictures[index]
        fmt = image_format(data)
        if fmt == "bmp":
            data, fmt = bmp_to_png(data), "png"
    except KeyError:
        return not_found
    except Exception as e:
        return {"error": "image_read_failed", "message": f"{type(e).__name__}: {e}"}
    if fmt is None:
        return {"error": "unsupported_image_format",
                "message": f"Picture {index} of '{doc['name']}' is not PNG, JPEG, GIF, WebP or BMP "
                           "(e.g. a linked TIFF or metafile), so it cannot be shown."}
    if len(data) > MAX_IMAGE_BYTES:
        return {"error": "image_too_large",
                "message": f"Picture {index} of '{doc['name']}' is {len(data)} bytes; "
                           f"the limit is {MAX_IMAGE_BYTES}."}
    size = image_size(data)
    if size and max(size) > MAX_PASSTHROUGH_SIDE_PX:
        return {"error": "image_too_large",
                "message": f"Picture {index} of '{doc['name']}' is {size[0]}x{size[1]} px; "
                           f"images over {MAX_PASSTHROUGH_SIDE_PX} px per side are rejected by the API."}
    return Image(data=data, format=fmt)


@mcp.tool(title="Get Design Image", annotations=ToolAnnotations(readOnlyHint=True),
          structured_output=False)
def get_design_image(document: str, index: int = 0) -> Image | str:
    """Look at a picture placed on a schematic sheet or in a Draftsman drawing — e.g. a
    pasted datasheet excerpt, block diagram, photo or logo. document is the sheet name or
    Draftsman file name, index the picture number that get_design_notes and
    get_sheet_context list as [image N] / [picture N]. Schematic pictures are stored as
    bitmaps and returned as PNG."""
    try:
        project, _, _ = _require_project(netlist=False)
    except ProjectNotReady as e:
        return json.dumps(e.payload)
    result = _get_design_image_impl(project, _design_notes, document, index)
    return json.dumps(result) if isinstance(result, dict) else result


# ---------- get_design_hierarchy ----------

@mcp.tool(
    title="Get Design Hierarchy",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def get_design_hierarchy(sheet: str | None = None, offset: int = 0) -> str:
    """The project's sheet hierarchy offline, like Altium's Report Project Hierarchy: the
    sheet tree from the top sheet(s) (sheets no sheet symbol places) down through the
    sheet symbols, with device sheets, Repeat(...) channels and their counts marked; per
    sheet its ports (name, I/O type, harness) and its sheet symbols with their entries
    (name, I/O type, harness; port_io when the child's port differs or is missing); and
    the project's Net
    Identifier Scope (the .PrjPcb setting, what Automatic resolves to for this project,
    how nets cross sheets under it, and the net naming options). Use it to explain why a
    net has its name and how it crosses sheets. With a compiled netlist each port and
    entry names the net (or one net per channel) it joins; labels lists the net labels
    and power ports on its wiring. Issues: symbols pointing at missing files, entries
    without a port in the child and ports without an entry, I/O and harness mismatches,
    ports on a top sheet under a hierarchical scope, duplicate symbols or entries,
    sheets placed several times, cycles, unwired ports and entries (issue_help explains
    each kind; findings of one kind on one symbol or sheet are listed together in
    entries or ports, and summary counts each one).

    sheet (a sheet name or file name) narrows the tree, sheets and issues to its
    subtree. Sheet details are paginated (offset from next_offset); the tree and issues
    come with the first page."""
    try:
        project, netlist, _ = _require_project(netlist=False)
    except ProjectNotReady as e:
        return json.dumps(e.payload)
    model = build_design_hierarchy(project, _design_notes.hierarchy, netlist, sheet)
    if "error" in model:
        return json.dumps(model)
    if netlist is not None:
        model["netlist_updated_utc"] = _netlist_last_updated
    # Sheet details shrink first; the issues (errors first) only when that is not enough.
    for limit, issue_limit in ((40, 150), (15, 150), (5, 150), (5, 60), (1, 60), (1, 20)):
        rendered = json.dumps(page_design_hierarchy(model, offset, limit, issue_limit),
                              indent=2)
        if len(rendered) <= QUERY_NET_MAX_RESULT_SIZE_CHARS:
            break
    return rendered


# ---------- get_project_options ----------

@mcp.tool(
    title="Get Project Options",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def get_project_options(offset: int = 0) -> str:
    """The project settings that change how Altium reads the design, offline from the
    .PrjPcb (Project Options): the Net Identifier Scope (and what Automatic resolves to)
    with the net naming options, diff-pair suffixes, class generation (project-wide and
    per sheet), multi-channel room naming and designator format, the ERC report level of
    each violation type (Error Reporting), the Connection Matrix, the Comparator's
    matching criteria and the output paths. The audit lists ERC checks switched off or
    downgraded from Altium's default (hides says what goes unreported), Connection
    Matrix cells relaxed for risky pin pairs (driver conflicts such as output to output,
    unconnected input pins), and every No ERC directive on the sheets with its sheet,
    location (mil), active flag, generic or specific mode, the net it sits on and its
    kind (power, high_fanout, signal, single_pin, wire_only, nothing), flagged when an
    active one sits on a power net or a net with several pins, where the violations it
    suppresses at its point may hide real problems. With a
    compiled netlist each No ERC names its net and the net's pin count; without one,
    the names on its wiring and that sheet's pins. Keys that no Altium source verifies
    are passed on raw under unmapped. offline_erc cites run_erc's counts as evidence (an
    offline approximation: Altium's ERC result is never saved), and an audit row of a
    check set to No Report says how many violations it hides in this design
    (offline_erc_hidden).

    No ERC directives are paginated (offset from next_offset), flagged first; the
    settings and audit come with the first page."""
    try:
        project, netlist, _ = _require_project(netlist=False)
    except ProjectNotReady as e:
        return json.dumps(e.payload)
    model = build_project_options(project, _design_notes.hierarchy, netlist)
    if "error" in model:
        return json.dumps(model)
    if netlist is not None:
        model["netlist_updated_utc"] = _netlist_last_updated
    offline_erc = _offline_erc_summary(project)
    if offline_erc is not None:
        # Evidence for the audit: what each switched-off check hides in this design.
        hidden = offline_erc["hidden_by_no_report"]
        for row in model["audit"]["erc_checks_off_or_downgraded"]:
            if row.get("check") in hidden:
                row["offline_erc_hidden"] = hidden[row["check"]]
        model["offline_erc"] = offline_erc
    for limit in (200, 80, 30, 10, 1):
        rendered = json.dumps(page_project_options(model, offset, limit), indent=2)
        if len(rendered) <= QUERY_NET_MAX_RESULT_SIZE_CHARS:
            break
    return rendered


# ---------- check_project_integrity ----------

# Load diagnostics that point at the same problems: sheets that did not parse, unreadable
# .Annotation files, designator clashes and netlist parts on no listed sheet.
_INTEGRITY_DIAGNOSTICS = (
    "sheet_errors", "annotation_errors", "designator_clash_count", "designator_clashes",
    "designator_clashes_hint", "refdes_without_metadata", "refdes_without_metadata_sample",
    "refdes_without_metadata_hint", "warning", "message",
)


@mcp.tool(
    title="Check Project Integrity",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def check_project_integrity(checks: list[str] | None = None, offset: int = 0) -> str:
    """Problems in the project folder, the .PrjPcb and the strings on the sheets that
    silently break compile, sync or saving, offline (no netlist needed). File checks:
    legacy_extension (schematics saved as .Sch/.S01/.S02: Update PCB can then remove every
    part; a .Pcb is renamed with them), missing_document (listed in the project, not on disk),
    annotation_file (a leftover .Annotation file from Board Level Annotation that makes
    the compiled designators differ from the editor's, entries for parts that are gone),
    outside_sheet_border (parts placed beyond the sheet size: compiled, not printed),
    compile_mask (parts a compile mask leaves out of the PCB and BOM) and variant_entry
    (variant entries whose designator or UniqueId no longer exists, e.g. after a PCB
    reannotation). Name checks on net labels, power ports, ports, sheet entries,
    designators and component parameters: hidden_characters (control, format and
    non-ASCII space characters: these make Constraint Manager saves fail), non_ascii,
    whitespace (leading or trailing spaces), net_name_syntax (brackets that are no
    Name[first..last] bus range), reserved_bom_parameter (local 'Manufacturer N' /
    'Manufacturer Part Number N' parameters, which the exported BOM overwrites from Part
    Choices, and the parts whose copilot MPN comes from one) and
    channel_designator_format (a multi-channel Designator Format that adds channel text
    after bus names on channel sheets).

    Each finding has a severity (error: breaks compile, sync or save; warning: a silently
    wrong result; info), its count, examples (sheet, designator, net, port or parameter,
    location in mil), how to fix it in Altium and the Altium knowledge-base article
    behind it (source). checks narrows to some of these. Findings are paginated (offset
    from next_offset), errors first; with a netlist, variant entries bind to the
    compiled designators and the load diagnostics come with the first page."""
    try:
        project, netlist, _ = _require_project(netlist=False)
    except ProjectNotReady as e:
        return json.dumps(e.payload)
    model = build_project_integrity(project, _design_notes.hierarchy, _design_notes.integrity,
                                    netlist, checks)
    if "error" in model:
        return json.dumps(model)
    if netlist is not None:
        model["netlist_updated_utc"] = _netlist_last_updated
        diagnostics = _load_diagnostics(netlist)
        kept = {key: diagnostics[key] for key in _INTEGRITY_DIAGNOSTICS if key in diagnostics}
        if kept:
            model["load_diagnostics"] = kept
    for limit, examples in ((60, 10), (30, 5), (15, 3), (5, 2), (1, 1)):
        rendered = json.dumps(page_project_integrity(model, offset, limit, examples), indent=2)
        if len(rendered) <= QUERY_NET_MAX_RESULT_SIZE_CHARS:
            break
    return rendered


# ---------- get_output_jobs ----------

@mcp.tool(
    title="Get Output Jobs",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def get_output_jobs(outjob: str | None = None, offset: int = 0) -> str:
    """The project's Output Jobs (.OutJob) offline: the OutJobs the .PrjPcb lists (and
    .OutJob files in the project folder it does not list, flagged), each with its variant
    and containers (PDF, folder structure, video, print job: release or manually managed
    base path, container and output sub-folders, file naming, the path Altium last
    resolved) and its outputs grouped by category (Fabrication, Assembly, Documentation,
    Report, Validation, Export): type, state (connected to a container or not, with its
    order there; only connected outputs are generated), data source document, variant,
    and mapped settings (Gerber, NC Drill, ODB++, IPC-2581, pick-and-place units, format
    and origin; ERC report options). coverage says per role (fabrication_data, nc_drill,
    pick_and_place, assembly_drawing, bom, erc, drc) whether a connected output covers
    it. Findings, each with severity, examples, an Altium fix and the knowledge-base
    article behind it where there is one: OutJobs missing, unreadable or not in the
    project; no fabrication data, no NC drill for Gerbers, no pick-and-place, assembly
    drawing, BOM, ERC or DRC validation output; Gerber only where the stackup is
    rigid-flex or has blind/buried drill pairs (no ODB++/IPC-2581); BOM and
    pick-and-place on different variants; a variant without an assembly drawing; data
    sources missing on disk, outside the project or a Draftsman drawing updated from
    another board; ERC validation levels and Connection Matrix differing from Project
    Options (the release uses the OutJob's); output, ECO log or container paths on
    another drive and paths near the Windows 260-character limit; Gerbers on the
    absolute origin next to ODB++ (always relative) and Gerber vs NC Drill origins;
    overlay layers missing from the layer stack; board cutouts with Gerbers plotting no
    mechanical layer; Draftsman views whose own variant overrides the drawing's, and
    drawings whose variant differs from the OutJob's; generated files at a container's
    folder older than the design documents
    (file times; nothing is written); outputs connected to no container. not_checked
    names what the files cannot tell (release folders, layer sets, DRC settings, long-path
    support).

    outjob (a file name or stem) narrows the details to one OutJob; coverage and
    findings stay project-wide. Details are paginated by OutJob (offset from
    next_offset); coverage, findings and notes come with the first page."""
    try:
        project, _, _ = _require_project(netlist=False)
    except ProjectNotReady as e:
        return json.dumps(e.payload)
    model = build_output_jobs(project, _design_notes.draftsman, outjob)
    if "error" in model:
        return json.dumps(model)
    for limit, examples in ((10, 25), (4, 10), (2, 5), (1, 3), (1, 1)):
        rendered = json.dumps(page_output_jobs(model, offset, limit, examples), indent=2)
        if len(rendered) <= QUERY_NET_MAX_RESULT_SIZE_CHARS:
            break
    return rendered


# ---------- check_fab_drawing ----------

def _fab_board_reader(project: dict, netlist: dict | None):
    """check_fab_drawing's board reader: a PcbDoc path of the project -> (BoardFacts,
    None) or (None, why not). The first PcbDoc comes from the PCB session (cached), any
    other is parsed for this call."""
    first = (project.get("pcb_doc_paths") or [None])[0]

    def read(path: str):
        if path == first:
            index, error = _pcb_session.get(project, netlist)
            if error:
                return None, error["message"]
        else:
            if not Path(path).is_file():
                return None, f"PCB document not found on disk: {Path(path).name}"
            try:
                index = PcbIndex(parse_pcb_doc(path), netlist)
            except Exception as error:
                return None, f"{type(error).__name__}: {error}"
        try:
            profile, _ = load_profile("none", None)
            drills = review_dfm(index, profile, ["drill_count"])["drills"]
        except Exception as error:  # only the drill-table cross-check needs it
            logging.warning("check_fab_drawing: drill table failed: %s", error)
            drills = None
        return fab_board_facts(index, drills, path), None
    return read


def _fab_bom(project: dict, netlist: dict | None, variant_state: VariantState):
    """What check_fab_drawing compares BOM tables with: variant name -> {"variant",
    "parts": {designator: Comment}, "not_fitted": {designator: Comment}} of the fitted and
    not-fitted BOM parts (as get_bom builds them), or a variant_not_found error; None
    without a netlist."""
    if netlist is None or not netlist.get("components"):
        return None
    parameters = project.get("parameters") or {}

    def read(variant: str) -> dict:
        plan, _, error = _bom_plan(netlist, variant_state, variant)
        if error:
            return error
        parts, _ = build_bom_parts(netlist, plan, parameters)
        return {"variant": plan.name,
                "parts": {part.designator: part.comment or "" for part in parts if part.fitted},
                "not_fitted": {part.designator: part.comment or ""
                               for part in parts if not part.fitted}}
    return read


@mcp.tool(
    title="Check Fab Drawing",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def check_fab_drawing(document: str | None = None, offset: int = 0) -> str:
    """Check the project's Draftsman drawings (.PCBDwf) offline: each drawing classified
    from its content (fabrication, assembly or other; also lists a second role), then a
    checklist read from its notes, free text, tables, document parameters and layer notes,
    each item found (with the note line quoted and where it is: page, note and line),
    missing (with a suggested note line built from the design's numbers where known;
    <...> marks a value to agree, never a guess) or not_applicable. Fabrication: IPC class
    (IPC-6012/6013, IPC-A-600), laminate material, Tg, flammability (UL 94 V-0), surface
    finish (ENIG, ENEPIG, HASL/LF-HASL, ImAg, ImSn, OSP, hard gold), solder mask and
    silkscreen colours, board thickness with tolerance, copper weights, impedance (a table
    or a note with tolerance, when the stack has impedance profiles), drill table,
    registration and tolerances, electrical test (IPC-D-356) and rigid-flex items on a
    flex stack. Assembly: IPC-A-610 / J-STD-001 class, a BOM table, views of each side
    with parts. Missing finish, thickness, material or IPC class is a warning (an
    unspecified finish is often built as tin-lead HASL or silver).

    Cross-checks against the design: thickness and layer count stated vs the layer stack
    (error when a number contradicts it), materials named vs the stack's dielectrics,
    copper weights, impedance targets in the notes and the impedance table's cached lines
    vs the Layer Stack Manager's profiles, the drill table vs the board's holes, the layer
    stack legend vs the stack, the BOM table's variant vs the OutJob that prints the
    drawing and the active variant, its cached parts vs get_bom (needs a netlist; without
    one not_checked says so), the board the drawing was made from, the board saved after
    the drawing's last update (stale: Import Changes), and notes that contradict each
    other (two finishes, thicknesses, IPC classes or colours). Draftsman tables are
    caches from the last Import Changes, not live.

    document (a drawing's file name or stem) checks one drawing. Findings (errors first)
    come with the first page; per-drawing details (checklist, cross_checks,
    suggested_notes) are paginated (offset from next_offset)."""
    try:
        project, netlist, variant_state = _require_project(netlist=False)
    except ProjectNotReady as e:
        return json.dumps(e.payload)
    active = variant_state.active.name if variant_state.variants else None
    model = build_fab_drawing(project, _design_notes.draftsman,
                              _fab_board_reader(project, netlist),
                              draftsman_outputs(project), active,
                              _fab_bom(project, netlist, variant_state), document)
    if "error" in model:
        return json.dumps(model)
    for limit, examples in ((10, 25), (4, 10), (2, 5), (1, 3), (1, 1)):
        rendered = json.dumps(page_fab_drawing(model, offset, limit, examples), indent=2)
        if len(rendered) <= QUERY_NET_MAX_RESULT_SIZE_CHARS:
            break
    return rendered


# ---------- list_variants ----------

def _list_variants_impl(variant_state: VariantState) -> str:
    variants = []
    for v in variant_state._variants:
        entry = {
            "name": v.name,
            "dnp_count": len(v.dnp_refdes),
            "dnp_components": v.dnp_refdes,
            "is_active": v.name == variant_state.active.name,
        }
        # Varied parameters and alternate parts of the parts the variant fits, by the
        # designator they apply to (bound like the BOM once the netlist is loaded); left
        # out when there are none.
        not_fitted = set(v.dnp_refdes)
        bound = getattr(v, "bound_params", None)
        params = bound if bound is not None else (getattr(v, "param_variations", None) or {})
        params = {designator: varied for designator, varied in params.items()
                  if designator not in not_fitted}
        if params:
            entry["varied_parameter_count"] = sum(len(p) for p in params.values())
            entry["varied_components"] = sorted(params, key=natural_key)
        bound_alternates = getattr(v, "bound_alternates", None)
        if bound_alternates is not None:
            alternates = [{"designator": designator,
                           **alternate["variation"].alternate_identity()}
                          for designator, alternate in sorted(
                              bound_alternates.items(), key=lambda kv: natural_key(kv[0]))]
        else:
            alternates = v.alternate_parts() if hasattr(v, "alternate_parts") else []
        alternates = [row for row in alternates if row["designator"] not in not_fitted]
        if alternates:
            entry["alternate_parts"] = [
                {key: row[key] for key in ("designator", "lib_reference", "library",
                                           "footprint", "same_footprint", "library_error")
                 if key in row}
                for row in alternates]
        if getattr(v, "allow_fabrication", False):
            entry["allow_fabrication"] = True
        if getattr(v, "parameters", None):
            entry["parameters"] = v.parameters
        variants.append(entry)
    return json.dumps({
        "active_variant": variant_state.active.name,
        "variants": variants,
    }, indent=2)


@mcp.tool(title="List Variants", annotations=ToolAnnotations(readOnlyHint=True))
def list_variants() -> str:
    """List all project variants and their DNP component lists.
    Call this at session start so the user can choose which variant to work in.
    A variant that varies more than fitting also lists varied_components (the parts it
    fits that have varied parameters, varied_parameter_count in all), alternate_parts
    (designator, lib_reference, library, footprint; same_footprint keeps the base
    part's; library_error when the .PrjPcbVariants file could not be read),
    allow_fabrication (it varies fabrication outputs too) and its parameters.
    compare_variants(a, b) lists every difference between two variants."""
    try:
        _, _, variant_state = _require_project(netlist=False)
    except ProjectNotReady as e:
        return json.dumps(e.payload)
    return _list_variants_impl(variant_state)


# ---------- set_active_variant ----------

def _set_active_variant_impl(variant_state: VariantState, variant_name: str) -> str:
    try:
        variant_state.set_variant(variant_name)
        active = variant_state.active
        return json.dumps({
            "active_variant": active.name,
            "dnp_components": active.dnp_refdes,
            "message": f"Switched to variant '{active.name}'. {len(active.dnp_refdes)} components are DNP.",
        }, indent=2)
    except ValueError as e:
        return json.dumps({"error": "variant_not_found", "message": str(e)})


@mcp.tool(title="Set Active Variant", annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
def set_active_variant(variant_name: str) -> str:
    """Switch the active variant. DNP annotations, varied values and alternate parts on
    get_component and get_sheet_context will reflect the new variant immediately."""
    try:
        _, _, variant_state = _require_project(netlist=False)
    except ProjectNotReady as e:
        return json.dumps(e.payload)
    return _set_active_variant_impl(variant_state, variant_name)


# ---------- compare_variants ----------

VARIANT_DIFF_LIMITS = (100, 40, 15, 5, 1)
VARIANT_STALE_SHOWN = 20
COMPARE_VARIANTS_NOTES = [
    "Altium's variation types: Fitted, Fitted with varied parameters, Not Fitted and "
    "Alternate Part; a part Not Fitted in both variants is left out, and one fitted in "
    "only one is listed under fitted with that side's variations.",
    "A value or parameter a variant does not vary is the base part's (an alternate "
    "part's: its copy in the project's .PrjPcbVariants file).",
    "Read from the saved .PrjPcb, sheets and OutJobs; Altium's Variant Management "
    "dialog is the final word.",
]
COMPARE_VARIANTS_UNBOUND_NOTE = (
    "No netlist is loaded: designators are the ones stored in the .PrjPcb (a renamed part "
    "keeps its old one; '?' designators show their UniqueId) and base values are not "
    "known. Load the netlist (refresh_netlist) to bind variations by UniqueId."
)
COMPARE_VARIANTS_UNVALIDATED_NOTE = (
    "Alternate parts with another library part or footprint, and Value variations, have "
    "no validated sample: check them in Altium's Variant Management dialog."
)


def _variant_or_base(variant_state: VariantState, name: str):
    """(variant or None for the base design, error or None)."""
    chosen = variant_state.get(name.strip())
    if chosen is not None:
        return chosen, None
    if name.strip().lower() in BOM_BASE_NAMES:
        return None, None
    return None, _variant_not_found(name, variant_state)


def _variant_documentation(project: dict, variant_state: VariantState,
                           notes_cache: DesignNotesCache) -> dict:
    """compare_variants' per-variant documentation check (info only)."""
    names = [variant.name for variant in variant_state.variants]
    drawings = []
    for path in project.get("pcb_dwf_paths") or []:
        try:
            doc = notes_cache.draftsman(path) if Path(path).is_file() else None
        except Exception as error:  # an unreadable drawing is reported as such
            logging.warning("Draftsman document not read for compare_variants: %s", error)
            doc = None
        drawings.append((PureWindowsPath(path).name, doc))
    drawing_info = draftsman_variants(drawings, names)
    try:
        model = variant_outputs(project, notes_cache.draftsman)
    except Exception as error:  # a broken OutJob must not fail the comparison
        model = {"error": "outjobs_unreadable", "message": f"{type(error).__name__}: {error}"}
    outputs = model.get("outputs") or []
    result: dict = {
        "per_variant": variant_documentation(names, outputs, drawing_info),
        "outjobs": model.get("outjobs", 0),
    }
    base = [o for o in outputs if o["connected"] and same_variant(o["variant"], BASE_VARIANT)]
    if base:
        result["outputs_on_base_design"] = len(base)
    if model.get("error"):
        result["outjob_error"] = model.get("message") or model["error"]
    if drawing_info:
        result["draftsman"] = drawing_info
    result["note"] = (
        "Info only: a connected OutJob output (BOM, pick and place, assembly drawing: a "
        "Draftsman drawing's own variant counts) set to each variant, and the Draftsman "
        "drawings set to it or whose notes name it. get_output_jobs has the details.")
    return result


def _compare_variants_impl(project: dict, netlist: dict | None, variant_state: VariantState,
                           a: str | None, b: str | None, offset: int,
                           notes_cache: DesignNotesCache | None) -> str:
    if not defined_variant_count(variant_state.variants):
        return json.dumps({
            "error": "no_variants",
            "message": "The project defines no variant (Project > Variants in Altium); "
                       "there is nothing to compare.",
        })
    if a is None or not a.strip():
        chosen_a = variant_state.active
    else:
        chosen_a, error = _variant_or_base(variant_state, a)
        if error:
            return json.dumps(error)
    chosen_b, error = (None, None) if b is None or not b.strip() else \
        _variant_or_base(variant_state, b)
    if error:
        return json.dumps(error)
    if netlist is not None:
        no_components = _no_components_error(netlist)
        if no_components:
            return json.dumps(no_components)
    view_a, view_b = variant_view(chosen_a, netlist), variant_view(chosen_b, netlist)
    rows = compare_variant_views(view_a, view_b, netlist)
    summary = {group: sum(1 for row in rows if row["group"] == group)
               for group in VARIANT_DIFF_GROUPS}
    header: dict = {
        "a": view_a.name,
        "b": view_b.name,
        "binding": "unique_id" if netlist is not None else "stored_designators",
        "summary": {group: count for group, count in summary.items() if count},
        "differences_total": len(rows),
    }
    offset = max(0, int(offset or 0))
    extras: dict = {}
    if offset == 0:
        stale = {side: view.stale for side, view in (("a", view_a), ("b", view_b))
                 if view.stale}
        if stale:
            extras["stale_variant_entries"] = {
                side: entries[:VARIANT_STALE_SHOWN] for side, entries in stale.items()}
            extras["stale_variant_entries_note"] = (
                "Variation lines that no longer fit their part (check_bom's "
                "stale_variant_entries); they were bound as described or not applied.")
        if notes_cache is not None:
            extras["documentation"] = _variant_documentation(project, variant_state,
                                                             notes_cache)
        notes = list(COMPARE_VARIANTS_NOTES)
        if netlist is None:
            notes.insert(0, COMPARE_VARIANTS_UNBOUND_NOTE)
        if unvalidated_variant_rows(rows):
            notes.append(COMPARE_VARIANTS_UNVALIDATED_NOTE)
        errors = sorted({getattr(alternate.get("variation"), "library_error", "")
                         for view in (view_a, view_b)
                         for alternate in view.alternates.values()} - {""})
        if errors:
            notes.append(
                f"The project's .PrjPcbVariants file could not be read ({errors[0]}): "
                "alternate parts show only what the variant itself stores.")
        extras["notes"] = notes
    rendered = ""
    for limit in VARIANT_DIFF_LIMITS:
        page = rows[offset:offset + limit]
        result = {**header, "offset": offset, "has_more": offset + limit < len(rows)}
        if result["has_more"]:
            result["next_offset"] = offset + limit
        result["differences"] = group_variant_rows(page)
        result.update(extras)
        if limit < VARIANT_DIFF_LIMITS[0]:
            result["differences_limited_to"] = limit
        rendered = json.dumps(result, indent=2)
        if len(rendered) <= QUERY_NET_MAX_RESULT_SIZE_CHARS:
            break
    return rendered


@mcp.tool(
    title="Compare Variants",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def compare_variants(a: str | None = None, b: str | None = None, offset: int = 0) -> str:
    """Every difference between two variants: a (default: the active one) and b
    (default: the base design, '[No Variations]'). Grouped by kind, designators in
    natural order: fitted (fitted in one only, with that side's varied parameters or
    alternate part), values (Value and Comment), parameters (any other varied parameter;
    Description and Footprint against the part's own), alternate_parts (the part each
    places: library reference, library, footprint; an alternate's library copy brings
    its own values and parameters) and variant_parameters. Each row gives a's and b's
    value and the base design's.

    The first page adds documentation: per variant, the connected OutJob outputs set to
    it (BOM, pick and place, assembly drawing) and outputs_missing, and the Draftsman
    drawings set to it or whose notes name it (info only); and stale_variant_entries.
    Variations bind to parts by UniqueId like get_bom (binding stored_designators
    without a netlist). Differences are paginated (offset from next_offset)."""
    try:
        project, netlist, variant_state = _require_project(netlist=False)
    except ProjectNotReady as error:
        return json.dumps(error.payload)
    return _compare_variants_impl(project, netlist, variant_state, a, b, offset,
                                  _design_notes)


def _convert_units(obj, to_mm: bool, inside_mil: bool = False):
    """Convert unit-explicit mil values and containers to millimeters."""
    if not to_mm:
        return obj
    if isinstance(obj, dict):
        converted = {}
        for key, value in obj.items():
            is_mil_key = key.endswith("_mil")
            new_key = key[:-4] + "_mm" if is_mil_key else key
            if (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
            ):
                converted[new_key] = (
                    round(value * 0.0254, 4)
                    if is_mil_key or inside_mil
                    else value
                )
            else:
                converted[new_key] = _convert_units(
                    value,
                    to_mm,
                    inside_mil or is_mil_key,
                )
        return converted
    if isinstance(obj, list):
        return [
            _convert_units(value, to_mm, inside_mil)
            for value in obj
        ]
    if (
        isinstance(obj, (int, float))
        and not isinstance(obj, bool)
        and inside_mil
    ):
        return round(obj * 0.0254, 4)
    return obj


PcbUnits = Literal["mil", "mm"]


def _pcb_units(index, units: PcbUnits | None) -> tuple[bool, str]:
    resolved = (units or index.pcb.board.display_unit).lower()
    if resolved not in {"mil", "mm"}:
        raise ValueError("units must be 'mil' or 'mm'")
    return resolved == "mm", resolved


def _pcb_units_or_error(index, units: PcbUnits | None):
    try:
        to_mm, resolved = _pcb_units(index, units)
    except (AttributeError, ValueError):
        return None, None, {
            "error": "invalid_units",
            "message": "units must be 'mil' or 'mm'.",
        }
    return to_mm, resolved, None


def _input_to_mil(value: float, units: str) -> float:
    return value / 0.0254 if units == "mm" else value


def _finite_nonnegative(value: float) -> bool:
    return math.isfinite(value) and value >= 0.0


NETLIST_FREE_PCB_NOTE = (
    "Not loaded (netlist-free load): PCB designators are not linked to schematic parts, "
    "so sch_refdes is empty, and DNP flags come from the designators stored in the "
    "project's variants."
)

PCB_POLYGON_GEOMETRY_NOTE = (
    "Repour polygons in Altium and save the PcbDoc before relying on polygon "
    "results. These tools model saved nominal polygon outlines and the tracks "
    "and arcs of hatched pours, not solid pours' region/fill copper; shelved "
    "pours have no copper. Verify voids, cutouts, removed islands, and "
    "clearances in Altium."
)


def _annotate_dnp(records: list[dict]) -> None:
    if _variant_state is None:
        return
    for record in records:
        refdes = record.get("sch_refdes") or record.get("refdes")
        record["dnp"] = bool(refdes and _variant_state.is_dnp(refdes))


@mcp.tool(
    title="Get Board Info",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def get_board_info(units: PcbUnits | None = None) -> str:
    """Return board geometry, stackup, counts, and data freshness.

    counts: tracks and arcs are routing; pour_copper is the tracks and arcs of
    hatched pours; shelved_pours are pours without copper.

    units controls returned coordinates and lengths; it defaults to the
    board's display unit.
    """
    try:
        project, netlist, _ = _require_project(netlist=False)
    except ProjectNotReady as error:
        return json.dumps(error.payload)
    index, error = _pcb_session.get(project, netlist)
    if error:
        return json.dumps(error)
    board = index.pcb.board
    to_mm, unit_name, units_error = _pcb_units_or_error(index, units)
    if units_error:
        return json.dumps(units_error)
    sides = {
        row["side"]: row["c"]
        for row in index.db.execute(
            "SELECT side, COUNT(*) c FROM components GROUP BY side"
        )
    }
    copper_bbox = index.db.execute(
        "SELECT MIN(minx) a, MIN(miny) b, "
        "MAX(maxx) c, MAX(maxy) d FROM prims"
    ).fetchone()
    if board.outline_vertices:
        outline_x = [
            vertex[0] for vertex in board.outline_vertices
        ]
        outline_y = [
            vertex[1] for vertex in board.outline_vertices
        ]
        board_bbox = {
            "minx_mil": min(outline_x),
            "miny_mil": min(outline_y),
            "maxx_mil": max(outline_x),
            "maxy_mil": max(outline_y),
        }
        extents_source = "board_outline"
    else:
        board_bbox = {
            "minx_mil": copper_bbox["a"],
            "miny_mil": copper_bbox["b"],
            "maxx_mil": copper_bbox["c"],
            "maxy_mil": copper_bbox["d"],
        }
        extents_source = "copper"
    stackup = [
        {
            "name": layer.name,
            "kind": layer.kind,
            "stack_order": layer.stack_order,
            "copper_thick_mil": layer.copper_thick_mil,
            "diel_height_mil": layer.diel_height_mil,
            "diel_const": layer.diel_const,
            "material": layer.material,
        }
        for layer in board.layers
        if layer.stack_order is not None
    ]
    pcb_path = Path(project["pcb_doc_paths"][0])
    result = {
        "pcb_doc": pcb_path.name,
        "last_saved_utc": datetime.fromtimestamp(
            pcb_path.stat().st_mtime,
            tz=timezone.utc,
        ).isoformat(),
        "units": unit_name,
        "origin_mil": {
            "x_mil": board.origin_x,
            "y_mil": board.origin_y,
        },
        "copper_extents_mil": {
            "minx_mil": copper_bbox["a"],
            "miny_mil": copper_bbox["b"],
            "maxx_mil": copper_bbox["c"],
            "maxy_mil": copper_bbox["d"],
        },
        "board_extents_mil": board_bbox,
        "extents_source": extents_source,
        "stackup_source": board.stackup_source,
        "stackup": stackup,
        **(
            {
                "impedance_profiles": impedance_profiles(index),
                "impedance_note": PROPAGATION_UNIT_NOTE,
            }
            if index.pcb.board.impedance_profiles
            else {}
        ),
        "netlist_updated_utc": _netlist_last_updated,
        "counts": index.counts(),
        "design_rules": _design_rule_counts(index),
        "components_per_side": sides,
        "warnings": index.pcb.warnings,
        "skipped_records": index.pcb.skipped_records,
    }
    if netlist is None:
        result["netlist"] = NETLIST_FREE_PCB_NOTE
    else:
        result["refdes_mapping_unmatched"] = index.unmatched_components()
        if result["refdes_mapping_unmatched"]:
            result["refdes_mapping_unmatched_sample"] = index.unmatched_refdes(10)
    renamed = index.renamed_components()
    if renamed:
        result["refdes_differs_from_logical"] = renamed
    if result["counts"]["pours"]:
        result["analysis_notes"] = [PCB_POLYGON_GEOMETRY_NOTE]
    if len(project["pcb_doc_paths"]) > 1:
        result["note"] = (
            f"Project lists {len(project['pcb_doc_paths'])} PcbDocs; "
            f"using '{pcb_path.name}'. Others: "
            + ", ".join(
                Path(path).name
                for path in project["pcb_doc_paths"][1:]
            )
        )
    return json.dumps(_convert_units(result, to_mm), indent=2)


def _pcb_index_or_error():
    try:
        project, netlist, _ = _require_project(netlist=False)
    except ProjectNotReady as error:
        return None, error.payload
    return _pcb_session.get(project, netlist)


def _resolve_layer_or_error(index, layer: str | None):
    if layer is None:
        return None, None
    resolved = index.resolve_layer(layer)
    if resolved is None:
        return None, {
            "error": "layer_not_found",
            "available_layers": [
                board_layer.name
                for board_layer in index.pcb.board.layers
                if board_layer.kind == "copper"
            ],
            "message": f"Layer '{layer}' not found.",
        }
    return resolved, None


def _safe_regex(pattern: str) -> bool:
    try:
        re.compile(pattern, re.IGNORECASE)
        return True
    except re.error:
        return False


@mcp.tool(
    title="Get Net PCB Routing",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def get_net_pcb(net: str, units: PcbUnits | None = None) -> str:
    """Return physical PCB routing details for exact or regex-matched nets.

    layers are routing: a hatched pour's tracks and arcs are not counted.
    pours lists the net's polygon pours with their names (several may share
    one), shelved ones (no copper) with shelved true.
    """
    index, error = _pcb_index_or_error()
    if error:
        return json.dumps(error)
    to_mm, _, units_error = _pcb_units_or_error(index, units)
    if units_error:
        return json.dumps(units_error)
    exact = next(
        (
            name
            for name in index.pcb.nets
            if name.lower() == net.lower()
        ),
        None,
    )
    matches = (
        [exact]
        if exact
        else (
            [
                name
                for name in index.pcb.nets
                if re.search(net, name, re.IGNORECASE)
            ]
            if _safe_regex(net)
            else []
        )
    )
    if not matches:
        return json.dumps(
            {
                "error": "net_not_found",
                "pattern": net,
                "message": f"No PCB nets matching '{net}'.",
            }
        )
    if len(matches) > QUERY_NET_MAX_RESULTS:
        return json.dumps(
            {
                "error": "too_many_matches",
                "message": (
                    f"Pattern matched {len(matches)} nets "
                    f"(limit {QUERY_NET_MAX_RESULTS}). Be more specific."
                ),
            }
        )
    if len(matches) > 1:
        compact = []
        for match in matches:
            summary = index.net_summary(match)
            compact.append(
                {
                    "net": summary["net"],
                    "layers": [
                        layer["layer"] for layer in summary["layers"]
                    ],
                    "total_length_mil": round(
                        sum(
                            layer["length_mil"]
                            for layer in summary["layers"]
                        ),
                        1,
                    ),
                    "via_count": summary["via_count"],
                    "pad_count": summary["pad_count"],
                }
            )
        return json.dumps(
            _convert_units(
                {
                    "match_count": len(compact),
                    "nets": compact,
                    "hint": (
                        "Call get_net_pcb with an exact name "
                        "for full detail."
                    ),
                },
                to_mm,
            ),
            indent=2,
        )
    summary = index.net_summary(matches[0])
    _annotate_dnp(summary["pads"])
    classes = index.net_classes_of(summary["net"])
    if classes:
        summary["net_classes"] = classes
    pairs = index.diff_pairs_of(summary["net"])
    if pairs:
        summary["diff_pairs"] = pairs
    if summary["pour_count"]:
        summary["geometry_note"] = PCB_POLYGON_GEOMETRY_NOTE
    return json.dumps(_convert_units(summary, to_mm), indent=2)


@mcp.tool(
    title="Get Net Neighbors",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def get_net_neighbors(
    net: str,
    distance: float | None = None,
    layer: str | None = None,
    units: PcbUnits | None = None,
) -> str:
    """Return same-layer proximity and adjacent-layer overlap for a PCB net.

    neighbors are copper-to-copper edge distances (tracks, arcs, vias, pads,
    and a hatched pour's tracks and arcs: pour_copper_segments counts those;
    they add no close_segments or parallel run).
    pours lists other nets' polygon pours on the net's layers, by their drawn
    outline only: inside_pour when the net's copper lies within the outline,
    min_outline_distance_mil when the outline comes within distance. An outline
    is not copper, and poured copper keeps the pour's clearance from the net.
    Shelved pours have no copper and are left out.

    An explicit distance is expressed in units (default: board display unit).
    Omitting distance uses a physical default of 10 mil.
    """
    index, error = _pcb_index_or_error()
    if error:
        return json.dumps(error)
    to_mm, input_units, units_error = _pcb_units_or_error(index, units)
    if units_error:
        return json.dumps(units_error)
    if distance is not None and not _finite_nonnegative(distance):
        return json.dumps(
            {
                "error": "invalid_distance",
                "message": "distance must be finite and nonnegative.",
            }
        )
    distance_mil = (
        10.0
        if distance is None
        else _input_to_mil(distance, input_units)
    )
    layer_id, layer_error = _resolve_layer_or_error(index, layer)
    if layer_error:
        return json.dumps(layer_error)
    result = index.net_neighbors(
        net,
        distance=distance_mil,
        layer=layer_id,
    )
    if result is None:
        return json.dumps(
            {
                "error": "net_not_found",
                "message": (
                    f"Net '{net}' not found on the PCB. "
                    "Use get_net_pcb with a pattern to discover names."
                ),
            }
        )
    partners = {
        pair["partner_net"].lower() for pair in index.diff_pairs_of(net)
    }
    if partners:
        for entry in result.get("neighbors", []) + result.get("broadside", []):
            if str(entry.get("net", "")).lower() in partners:
                entry["diff_pair_partner"] = True
    result["geometry_note"] = PCB_POLYGON_GEOMETRY_NOTE
    return json.dumps(_convert_units(result, to_mm), indent=2)


@mcp.tool(
    title="Query PCB Region",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def query_pcb_region(
    x: float,
    y: float,
    radius: float | None = None,
    layer: str | None = None,
    units: PcbUnits | None = None,
) -> str:
    """Return nets, components, and pours near a board coordinate.

    nets holds copper only (tracks, arcs, vias, pads; a hatched pour's tracks
    and arcs are kind "pour"). pours are polygon pours by their drawn outline:
    inside_pour, or the distance to the outline, which is not copper. Shelved
    pours have no copper and are left out.

    x, y, and an explicit radius are expressed in units (default: board
    display unit). Omitting radius uses a physical default of 50 mil.
    """
    index, error = _pcb_index_or_error()
    if error:
        return json.dumps(error)
    to_mm, input_units, units_error = _pcb_units_or_error(index, units)
    if units_error:
        return json.dumps(units_error)
    if not (math.isfinite(x) and math.isfinite(y)):
        return json.dumps(
            {
                "error": "invalid_coordinate",
                "message": "x and y must be finite.",
            }
        )
    if radius is not None and not _finite_nonnegative(radius):
        return json.dumps(
            {
                "error": "invalid_radius",
                "message": "radius must be finite and nonnegative.",
            }
        )
    x_mil = _input_to_mil(x, input_units)
    y_mil = _input_to_mil(y, input_units)
    radius_mil = (
        50.0
        if radius is None
        else _input_to_mil(radius, input_units)
    )
    layer_id, layer_error = _resolve_layer_or_error(index, layer)
    if layer_error:
        return json.dumps(layer_error)
    result = index.region_query(
        x_mil,
        y_mil,
        radius_mil,
        layer=layer_id,
    )
    _annotate_dnp(result["components"])
    result["geometry_note"] = PCB_POLYGON_GEOMETRY_NOTE
    return json.dumps(_convert_units(result, to_mm), indent=2)


@mcp.tool(
    title="Get Component Placement",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def get_component_placement(
    refdes: str,
    units: PcbUnits | None = None,
) -> str:
    """Return physical placement and nearby-component details.

    refdes is the designator on the PCB or the netlist refdes; a schematic
    sheet's logical refdes is accepted when neither matches.
    """
    index, error = _pcb_index_or_error()
    if error:
        return json.dumps(error)
    to_mm, _, units_error = _pcb_units_or_error(index, units)
    if units_error:
        return json.dumps(units_error)
    instances = index.component_detail(refdes)
    if not instances:
        try:
            regex = re.compile(refdes, re.IGNORECASE)
            candidates = sorted(
                {
                    row["refdes"]
                    for row in index.db.execute(
                        "SELECT DISTINCT refdes FROM components "
                        "WHERE refdes IS NOT NULL"
                    )
                    if regex.search(row["refdes"])
                }
            )[:10]
        except re.error:
            candidates = []
        return json.dumps(
            {
                "error": "component_not_found",
                "message": f"'{refdes}' not on the PCB.",
                "suggestions": candidates,
            }
        )
    _annotate_dnp(instances)
    for instance in instances:
        _annotate_dnp(instance["nearest_components"])
    return json.dumps(
        _convert_units({"instances": instances}, to_mm),
        indent=2,
    )



# ---------- design rules, classes, differential pairs ----------

MAX_RULE_RESULTS = 60
MAX_CLASS_RESULTS = 200
MAX_CLASS_MEMBERS = 200
MAX_DIFF_PAIRS = 100


def _design_rule_counts(index) -> dict:
    rules = index.pcb.rules
    return {
        "rules": sum(1 for rule in rules if rule.kind != ROOM_KIND),
        "disabled_rules": sum(
            1 for rule in rules if rule.kind != ROOM_KIND and not rule.enabled
        ),
        "rooms": sum(1 for rule in rules if rule.kind == ROOM_KIND),
        "net_classes": sum(
            1
            for pcb_class in index.pcb.classes
            if pcb_class.kind == "net" and not pcb_class.superclass
        ),
        "diff_pairs": len(index.pcb.diff_pairs),
    }


def _rule_stale(index, rule) -> list[str]:
    stale = stale_references(rule.scope1, index.rule_context)
    if rule.kind in BINARY_KINDS:
        stale += stale_references(rule.scope2, index.rule_context)
    return list(dict.fromkeys(stale))


def _rule_detail(index, rule) -> dict:
    detail = rule_header(rule)
    values = rule_values(
        rule,
        index.signal_layers(),
        index.pcb.board.substacks,
        profile_refs(index.pcb.board),
    )
    if values:
        detail["values"] = values
    stale = _rule_stale(index, rule)
    if stale:
        detail["stale_references"] = stale
    return detail


def _page(items: list, offset: int, limit: int) -> tuple[list, dict]:
    offset = max(0, offset)
    page = items[offset : offset + limit]
    meta = {"total": len(items), "offset": offset, "has_more": offset + limit < len(items)}
    if meta["has_more"]:
        meta["next_offset"] = offset + limit
    return page, meta


def _get_design_rules_impl(index, kind, name, offset, to_mm) -> dict:
    rules = sorted(index.pcb.rules, key=lambda rule: (rule.kind, rule.priority))
    if name:
        matches = [rule for rule in rules if rule.name.lower() == name.lower()]
        if not matches:
            suggestions = [
                rule.name for rule in rules if name.lower() in rule.name.lower()
            ][:10]
            return {
                "error": "rule_not_found",
                "message": f"No design rule named '{name}'.",
                "suggestions": suggestions,
            }
        return {"rules": [_rule_detail(index, rule) for rule in matches]}
    if kind:
        kinds = {rule.kind.lower(): rule.kind for rule in rules}
        if kind.lower() not in kinds:
            return {
                "error": "invalid_rule_kind",
                "message": f"No rules of kind '{kind}' on this board.",
                "available_kinds": sorted(set(kinds.values())),
            }
        selected = [rule for rule in rules if rule.kind == kinds[kind.lower()]]
        page, meta = _page(selected, offset, MAX_RULE_RESULTS)
        return {
            "kind": kinds[kind.lower()],
            **meta,
            "rules": [_rule_detail(index, rule) for rule in page],
        }
    listed = [rule for rule in rules if rule.kind != ROOM_KIND]
    page, meta = _page(listed, offset, MAX_RULE_RESULTS)
    headers = []
    for rule in page:
        header = rule_header(rule)
        stale = _rule_stale(index, rule)
        if stale:
            header["stale_references"] = stale
        headers.append(header)
    kinds_count: dict[str, int] = {}
    for rule in listed:
        kinds_count[rule.kind] = kinds_count.get(rule.kind, 0) + 1
    counts = _design_rule_counts(index)
    return {
        "rule_count": counts.pop("rules"),
        **counts,
        "kinds": kinds_count,
        **meta,
        "rules": headers,
        "hint": (
            "Pass kind (e.g. 'Width') or name for values. Rooms are "
            "RoomDefinition rules: kind='RoomDefinition'."
        ),
    }


@mcp.tool(
    title="Get Design Rules",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def get_design_rules(
    kind: str | None = None,
    name: str | None = None,
    offset: int = 0,
    units: PcbUnits | None = None,
) -> str:
    """List the PCB design rules saved in the PcbDoc, or show one kind or rule with values.

    With no arguments: rule counts per kind and every rule's name, priority (1 = highest),
    enabled flag and scope query. kind (e.g. 'Clearance', 'Width', 'DiffPairsRouting',
    'RoutingVias', 'RoomDefinition') or name returns full values: widths, gaps, clearances
    including the object-to-object matrix, per-layer values for the board's signal layers.
    stale_references lists scope names that match no net, class, pair or room. Lengths use
    units (default: board display unit). Paginated: pass offset from next_offset."""
    index, error = _pcb_index_or_error()
    if error:
        return json.dumps(error)
    to_mm, _, units_error = _pcb_units_or_error(index, units)
    if units_error:
        return json.dumps(units_error)
    result = _get_design_rules_impl(index, kind, name, offset, to_mm)
    if "error" in result:
        return json.dumps(result)
    return json.dumps(_convert_units(result, to_mm), indent=2)


def _net_or_pair_not_found(index, name: str) -> dict:
    suggestions = [
        net for net in index.pcb.nets if name.lower() in net.lower()
    ][:10] + [
        pair.name
        for pair in index.pcb.diff_pairs
        if name.lower() in pair.name.lower()
    ][:5]
    return {
        "error": "net_not_found",
        "message": f"No PCB net or differential pair named '{name}'.",
        "suggestions": suggestions,
    }


# Rule kinds that apply to a whole xSignal (its routed legs through series
# parts); every other kind applies to its member nets.
XSIGNAL_RULE_KINDS = ("Length", "MatchedLengths")


def _xsignal_classes_of(index, name: str) -> list[str]:
    key = name.lower()
    return [
        pcb_class.name
        for pcb_class in index.pcb.classes
        if pcb_class.kind == "xsignal"
        and not pcb_class.superclass
        and key in {member.lower() for member in pcb_class.members}
    ]


def _get_rules_for_net_impl(index, net, kinds) -> dict:
    target = index.rule_target(net)
    if target is None:
        missing = _net_or_pair_not_found(index, net)
        missing["message"] = (
            f"No PCB net, differential pair or xSignal named '{net}'."
        )
        missing["suggestions"] += [
            xsignal.name
            for xsignal in index.pcb.xsignals
            if net.lower() in xsignal.name.lower()
        ][:5]
        return missing
    ctx = index.rule_context
    also_pair = target.kind == "net" and net.lower() in ctx.pairs
    also_xsignal = target.kind != "xsignal" and net.lower() in ctx.xsignals
    by_kind: dict[str, list] = {}
    for rule in index.pcb.rules:
        if rule.kind != ROOM_KIND:
            by_kind.setdefault(rule.kind, []).append(rule)
    not_for_xsignal: list[str] = []
    if kinds:
        lookup = {kind.lower(): kind for kind in by_kind}
        wanted = (
            list(by_kind)
            if any(kind.lower() == "all" for kind in kinds)
            else [lookup[kind.lower()] for kind in kinds if kind.lower() in lookup]
        )
        unknown = [
            kind for kind in kinds
            if kind.lower() != "all" and kind.lower() not in lookup
        ]
        if unknown and not wanted:
            return {
                "error": "invalid_rule_kind",
                "message": f"No rules of kind {unknown} on this board.",
                "available_kinds": sorted(by_kind),
            }
        if target.kind == "xsignal":
            not_for_xsignal = [
                kind for kind in wanted if kind not in XSIGNAL_RULE_KINDS
            ]
            wanted = [kind for kind in wanted if kind in XSIGNAL_RULE_KINDS]
    else:
        wanted = [kind for kind in NET_RULE_KINDS if kind in by_kind]
        if target.kind == "diff_pair":
            wanted = [
                kind for kind in ("DiffPairsRouting", "MatchedLengths")
                if kind in by_kind
            ]
        elif target.kind == "xsignal":
            wanted = [kind for kind in XSIGNAL_RULE_KINDS if kind in by_kind]

    def describe(rule):
        return _rule_detail(index, rule)

    results = []
    for kind in wanted:
        if kind == "DiffPairsRouting" and target.kind == "net":
            for pair in index.diff_pairs_of(target.name):
                pair_target = index.rule_target(pair["name"], kind="diff_pair")
                resolved = resolve_kind(
                    by_kind[kind], pair_target, index.rule_context, describe
                )
                resolved["diff_pair"] = pair["name"]
                results.append(resolved)
            continue
        results.append(
            resolve_kind(by_kind[kind], target, index.rule_context, describe)
        )
    pair_rules = [
        entry["effective"]["name"]
        for entry in results
        if entry["kind"] == "DiffPairsRouting" and entry.get("effective")
    ]
    if pair_rules:
        for entry in results:
            if entry["kind"] == "Width":
                entry["note"] = (
                    "This net is in a differential pair: Altium checks the "
                    "width of its pair tracks with DiffPairsRouting "
                    f"{', '.join(pair_rules)} (per-layer widths there), not "
                    "with this Width rule."
                )
    result: dict = {"target": target.name, "type": target.kind}
    if target.kind == "net":
        classes = index.net_classes_of(target.name)
        if classes:
            result["net_classes"] = classes
        pairs = index.diff_pairs_of(target.name)
        if pairs:
            result["diff_pairs"] = pairs
    elif target.kind == "diff_pair":
        _, positive, negative = ctx.pairs[target.name.lower()]
        result["positive_net"] = positive
        result["negative_net"] = negative
        result["pair_classes"] = index.pair_classes_of(target.name)
        result["hint"] = (
            "Track widths come from the DiffPairsRouting rule above. Clearance, "
            "via and length rules apply to the member nets: call "
            "get_rules_for_net on each net."
        )
    elif target.kind == "xsignal":
        key = target.name.lower()
        result["nets"] = sorted(
            ctx.net_names.get(member, member) for member in ctx.xsignals.get(key, ())
        )
        xsignal = index.xsignals.get(key)
        if xsignal is not None:
            result["pins"] = list(xsignal.pins)
        classes = _xsignal_classes_of(index, target.name)
        if classes:
            result["xsignal_classes"] = classes
        if not_for_xsignal:
            result["kinds_not_resolved"] = not_for_xsignal
        result["hint"] = (
            "An xSignal is checked by Length and MatchedLengths rules (listed in "
            "rules; check_lengths(target=...) measures it). Width, clearance, "
            "via and layer rules apply to its member nets: call "
            "get_rules_for_net on each net."
        )
    result["rules"] = results
    notes = []
    if also_pair:
        notes.append(
            f"'{net}' is also a differential pair name; its pair rules are "
            "listed under DiffPairsRouting when the net belongs to it, or call "
            "list_diff_pairs."
        )
    if also_xsignal:
        notes.append(
            f"'{net}' is also an xSignal name; call check_lengths(target=...) "
            "for the xSignal's length rules."
        )
    if notes:
        result["note"] = " ".join(notes)
    return result


@mcp.tool(
    title="Get Rules for Net",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def get_rules_for_net(
    net: str,
    kinds: list[str] | None = None,
    units: PcbUnits | None = None,
) -> str:
    """Resolve which PCB design rules apply to a net (or differential pair) and their values.

    Per rule kind, returns the effective rule (first enabled rule by priority whose scope
    matches), plus higher-priority rules that apply only to some of the net's objects
    (conditions: only its vias, only on a layer, only in a room...). Binary kinds such as
    Clearance list candidates in priority order, since the winner depends on the other
    object; each lists orientations (which side of the rule the net is on, and what the
    other object must be). Diff-pair routing rules are resolved for the pairs the net
    belongs to; a pair net's track width comes from them, not from Width. An xSignal
    name gets its Length and MatchedLengths rules and its member nets.
    kinds narrows the kinds (default: routing-related kinds; ['all'] for every kind).
    Lengths use units (default: board display unit)."""
    index, error = _pcb_index_or_error()
    if error:
        return json.dumps(error)
    to_mm, _, units_error = _pcb_units_or_error(index, units)
    if units_error:
        return json.dumps(units_error)
    result = _get_rules_for_net_impl(index, net, kinds)
    if "error" in result:
        return json.dumps(result)
    rendered = json.dumps(_convert_units(result, to_mm), indent=2)
    if len(rendered) > QUERY_NET_MAX_RESULT_SIZE_CHARS:
        # Keep every rule's identity and the winners' values; drop values of
        # lower-priority binary candidates, which get_design_rules can show.
        for entry in result["rules"]:
            for candidate in entry.get("candidates", [])[1:]:
                candidate.pop("values", None)
        result["values_omitted"] = (
            "Response too large: values are shown only for the first candidate "
            "of each binary kind. Call get_design_rules(name=...) for the rest."
        )
        rendered = json.dumps(_convert_units(result, to_mm), indent=2)
    return rendered


def _class_members(index, pcb_class, offset: int) -> dict:
    if pcb_class.superclass:
        return {"members": f"implicit: every {pcb_class.kind} on the board"}
    members, meta = _page(pcb_class.members, offset, MAX_CLASS_MEMBERS)
    ctx = index.rule_context
    unresolved: list[str] = []
    if pcb_class.kind == "net":
        rendered = members
        unresolved = [
            member for member in pcb_class.members
            if member.lower() not in ctx.net_names
        ]
    elif pcb_class.kind == "diff_pair":
        rendered = []
        for member in members:
            pair = ctx.pairs.get(member.lower())
            if pair:
                rendered.append(
                    {"pair": pair[0], "positive_net": pair[1], "negative_net": pair[2]}
                )
            else:
                rendered.append({"pair": member})
        unresolved = [
            member for member in pcb_class.members
            if member.lower() not in ctx.pairs
        ]
    elif pcb_class.kind == "component":
        rendered = []
        for member in members:
            found = index.find_by_designator(member)
            record = {"refdes": member}
            if found and found[0].get("sch_refdes"):
                record["sch_refdes"] = found[0]["sch_refdes"]
            rendered.append(record)
        _annotate_dnp(rendered)
        unresolved = [
            member for member in pcb_class.members
            if not index.find_by_designator(member)
        ]
    else:
        rendered = members
    result = {"member_count": len(pcb_class.members), **meta, "members": rendered}
    if unresolved:
        result["unresolved_members"] = unresolved[:MAX_CLASS_MEMBERS]
    return result


@mcp.tool(
    title="Get Classes",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def get_classes(
    kind: str | None = None,
    name: str | None = None,
    offset: int = 0,
) -> str:
    """List the PCB's object classes, or show one class's members.

    kind filters: net, component, diff_pair, pad, polygon, xsignal, layer, from_to,
    design_channel. Without name: each class with its member count; superclasses
    ('All Nets'...) have implicit membership; auto_generated marks Altium's side/board
    component classes. With name: members (component members are PCB designators, with DNP for
    the active variant; unresolved_members match no net, pair or designator). Paginated:
    pass offset from next_offset."""
    index, error = _pcb_index_or_error()
    if error:
        return json.dumps(error)
    valid_kinds = sorted(set(CLASS_KINDS.values()))
    if kind and kind.lower() not in valid_kinds:
        return json.dumps({
            "error": "invalid_class_kind",
            "message": f"Unknown class kind '{kind}'.",
            "available_kinds": valid_kinds,
        })
    classes = [
        pcb_class for pcb_class in index.pcb.classes
        if not kind or pcb_class.kind == kind.lower()
    ]
    if name:
        matches = [c for c in classes if c.name.lower() == name.lower()]
        if not matches:
            return json.dumps({
                "error": "class_not_found",
                "message": f"No class named '{name}'.",
                "suggestions": [
                    c.name for c in classes if name.lower() in c.name.lower()
                ][:10],
            })
        return json.dumps(
            {
                "classes": [
                    {
                        "name": c.name,
                        "kind": c.kind,
                        **({"superclass": True} if c.superclass else {}),
                        **({"auto_generated": True} if c.auto_generated else {}),
                        **_class_members(index, c, offset),
                    }
                    for c in matches
                ]
            },
            indent=2,
        )
    listing = []
    for c in classes:
        entry = {"name": c.name, "kind": c.kind}
        if c.superclass:
            entry["superclass"] = True
        else:
            entry["member_count"] = len(c.members)
        if c.auto_generated:
            entry["auto_generated"] = True
        listing.append(entry)
    page, meta = _page(listing, offset, MAX_CLASS_RESULTS)
    kinds_count: dict[str, int] = {}
    for c in classes:
        kinds_count[c.kind] = kinds_count.get(c.kind, 0) + 1
    return json.dumps({"kinds": kinds_count, **meta, "classes": page}, indent=2)


def _list_diff_pairs_impl(index, pattern, offset) -> dict:
    pairs = index.pcb.diff_pairs
    if pattern:
        if not _safe_regex(pattern):
            return {"error": "invalid_pattern", "message": f"Invalid regex: {pattern}"}
        pairs = [
            pair for pair in pairs
            if re.search(pattern, pair.name, re.IGNORECASE)
            or re.search(pattern, pair.positive_net, re.IGNORECASE)
            or re.search(pattern, pair.negative_net, re.IGNORECASE)
        ]
    routing_rules = [
        rule for rule in index.pcb.rules if rule.kind == "DiffPairsRouting"
    ]
    page, meta = _page(pairs, offset, MAX_DIFF_PAIRS)
    rendered = []
    for pair in page:
        positive_length = index.routed_length(pair.positive_net)
        negative_length = index.routed_length(pair.negative_net)
        entry: dict = {
            "name": pair.name,
            "positive_net": pair.positive_net,
            "negative_net": pair.negative_net,
        }
        classes = index.pair_classes_of(pair.name)
        if classes:
            entry["classes"] = classes
        lengths = pair_lengths(index, pair.name)
        if "error" not in lengths:
            entry["length_mil"] = {
                "positive": lengths["positive"].get("length_mil"),
                "negative": lengths["negative"].get("length_mil"),
            }
            if lengths.get("skew_mil") is not None:
                entry["length_skew_mil"] = lengths["skew_mil"]
            if lengths.get("status") != "ok":
                entry["length_status"] = lengths.get("status")
        if positive_length is not None and negative_length is not None:
            entry["routed_length_mil"] = {
                "positive": positive_length,
                "negative": negative_length,
            }
        if routing_rules:
            resolved = resolve_kind(
                routing_rules,
                index.rule_target(pair.name, kind="diff_pair"),
                index.rule_context,
                rule_header,
            )
            effective = resolved.get("effective")
            entry["routing_rule"] = effective["name"] if effective else None
            if resolved.get("overridden_for_some_objects_by"):
                entry["routing_rule_conditional_overrides"] = [
                    rule["name"] for rule in resolved["overridden_for_some_objects_by"]
                ]
        rendered.append(entry)
    return {
        **meta,
        "diff_pairs": rendered,
        "note": (
            "length_mil is the copper path pad centre to pad centre plus the via "
            "barrel over the layers it traverses plus the end pads' pin/package "
            "length, like Altium's Signal Length (multi-pad legs: routed copper "
            "plus barrels, no pin/package length). routed_length_mil "
            "is all track and arc copper of each leg, including stubs. Use "
            "check_diff_pairs for skew against the in-pair rule and "
            "get_rules_for_net for the routing rule's gap and widths."
        ),
    }


@mcp.tool(
    title="List Differential Pairs",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def list_diff_pairs(
    pattern: str | None = None,
    offset: int = 0,
    units: PcbUnits | None = None,
) -> str:
    """List the PCB's differential pairs with nets, classes, routed length skew and routing rule.

    pattern is a case-insensitive regex on the pair or net names. Lengths use units
    (default: board display unit). Paginated: pass offset from next_offset."""
    index, error = _pcb_index_or_error()
    if error:
        return json.dumps(error)
    to_mm, _, units_error = _pcb_units_or_error(index, units)
    if units_error:
        return json.dumps(units_error)
    result = _list_diff_pairs_impl(index, pattern, offset)
    if "error" in result:
        return json.dumps(result)
    return json.dumps(_convert_units(result, to_mm), indent=2)


# ---------- rule compliance, lengths, diff-pair checks ----------

def _split_names(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [part.strip() for part in value.split(",") if part.strip()]


COMPLIANCE_GROUP_LIMIT = 50


def _trim_violation_groups(result: dict, offset: int, limit: int) -> None:
    """Cut every kind's violation groups to limit, keeping paging consistent."""
    for entry in result.get("kinds", []):
        groups = entry.get("violations")
        if not isinstance(groups, list) or len(groups) <= limit:
            continue
        entry["violations"] = groups[:limit]
        entry["has_more"] = True
        entry["next_offset"] = offset + limit


@mcp.tool(
    title="Check Rule Compliance",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def check_rule_compliance(
    net: str | None = None,
    kinds: list[str] | None = None,
    clearance_spot_check: bool = False,
    offset: int = 0,
    units: PcbUnits | None = None,
) -> str:
    """Check the saved copper against the design rules: track widths per layer (pair
    nets against their DiffPairsRouting widths), via diameter/hole, via hole size and
    annular ring (smallest over a full-stack via's pads, naming the layer), routing
    layers and Length windows, resolving the rule for EACH object
    (room, layer and via scopes decided per object).

    net is a net or diff-pair name (comma-separated for several); omit for the whole
    board. kinds narrows to Width, DiffPairsRouting, RoutingVias, HoleSize,
    MinimumAnnularRing, RoutingLayers, Length, Clearance. Diff-pair nets' track widths
    are checked under DiffPairsRouting, not Width (widths only; uncoupled length is not
    checked). clearance_spot_check adds an approximate clearance check for up to 10
    nets. Violations are grouped with one example location, worst first, up to 50
    groups per kind; if a kind has has_more true, call again with kinds=[that kind]
    and offset=next_offset (offset skips that many groups of every kind).
    checked_by_batch_drc says whether the board's own batch DRC checks that kind.
    Lengths use units (default: board display unit). This reads the saved file, not
    Altium's DRC report."""
    index, error = _pcb_index_or_error()
    if error:
        return json.dumps(error)
    to_mm, _, units_error = _pcb_units_or_error(index, units)
    if units_error:
        return json.dumps(units_error)
    offset = max(0, offset)
    result = check_compliance(
        index,
        _split_names(net),
        kinds,
        length_of=lambda name: net_length(index, name),
        clearance=clearance_spot_check,
        limit=COMPLIANCE_GROUP_LIMIT,
        offset=offset,
    )
    if "error" in result:
        return json.dumps(result)
    # A board-wide call with many failing kinds: list fewer groups per kind
    # until the answer fits; counts and summaries stay complete.
    rendered = json.dumps(_convert_units(result, to_mm), indent=2)
    for limit in (20, 10, 3, 1):
        if len(rendered) <= QUERY_NET_MAX_RESULT_SIZE_CHARS:
            break
        _trim_violation_groups(result, offset, limit)
        result["violations_per_kind_limited_to"] = limit
        result["hint"] = (
            "Response trimmed to fit: call check_rule_compliance(kinds=[<kind>], "
            "offset=<next_offset>) to page one kind's violation groups."
        )
        rendered = json.dumps(_convert_units(result, to_mm), indent=2)
    return rendered


@mcp.tool(
    title="Check Lengths",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def check_lengths(
    rule: str | None = None,
    target: str | None = None,
    offset: int = 0,
    units: PcbUnits | None = None,
) -> str:
    """Evaluate Length and MatchedLengths rules on the saved copper, as Altium does:
    Length windows per object; matched-length groups (xSignals, pairs or nets) against
    the longest member or the target source; within-pair skew. Lengths are the copper
    path pad to pad plus the via barrel over the layers traversed plus the end pads'
    pin/package length (xSignals through series parts, pin/package length at the end
    pins); nets without exactly 2 pads use routed copper plus barrels. A pair's group
    length is the P/N average.

    rule picks one rule; target (net, pair, xSignal or xSignal class) shows the rules
    covering it; with neither, every rule is listed but only members whose status is
    not ok are shown (summaries count all members). Stale scopes,
    conditional members and approximations are reported. Paginated per rule: pass
    offset from members_page.next_offset. Lengths use units (default: board display
    unit)."""
    index, error = _pcb_index_or_error()
    if error:
        return json.dumps(error)
    to_mm, _, units_error = _pcb_units_or_error(index, units)
    if units_error:
        return json.dumps(units_error)
    # Big boards with many failing groups: shrink the members listed per rule
    # until the answer fits; every rule's summary and pagination stay intact.
    for limit in (50, 10, 3, 1):
        result = check_length_rules(
            index, rule=rule, target=target, offset=offset, limit=limit
        )
        if "error" in result:
            return json.dumps(result)
        if limit < 50:
            result["members_per_rule_limited_to"] = limit
            result["hint"] = (
                "Response trimmed to fit: call check_lengths(rule=...) for one rule's "
                "members, paging with offset."
            )
        rendered = json.dumps(_convert_units(result, to_mm), indent=2)
        if len(rendered) <= QUERY_NET_MAX_RESULT_SIZE_CHARS:
            break
    return rendered


@mcp.tool(
    title="Get Signal Length",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def get_signal_length(
    name: str,
    to_pin: str | None = None,
    units: PcbUnits | None = None,
) -> str:
    """Measure one signal on the saved copper: a net (2 pads: pad-to-pad path; more:
    routed copper plus via barrels), a differential pair (both legs and skew) or an
    xSignal (legs through series parts). With to_pin, name is a pin like 'U1-5' and the
    path between the two pins is measured. Via barrels are included. Pin/package length
    stored on the end pads is added to pad-to-pad paths and at an xSignal's end pins,
    like Altium's Signal Length; routed copper of multi-pad nets has none, like Routed
    Length. Lengths use units (default: board display unit)."""
    index, error = _pcb_index_or_error()
    if error:
        return json.dumps(error)
    to_mm, _, units_error = _pcb_units_or_error(index, units)
    if units_error:
        return json.dumps(units_error)
    ctx = index.rule_context
    key = name.lower()
    if to_pin:
        result = pad_to_pad(index, None, name, to_pin)
    elif key in ctx.net_names:
        result = net_length(index, ctx.net_names[key])
    elif key in ctx.pairs:
        result = pair_lengths(index, ctx.pairs[key][0])
    elif key in ctx.xsignals:
        result = xsignal_length(index, name)
    else:
        result = _net_or_pair_not_found(index, name)
        result["message"] = f"No PCB net, differential pair or xSignal named '{name}'."
    if "error" in result:
        return json.dumps(result)
    return json.dumps(_convert_units(result, to_mm), indent=2)


@mcp.tool(
    title="Check Differential Pairs",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def check_diff_pairs(
    pattern: str | None = None,
    checks: list[str] | None = None,
    offset: int = 0,
    units: PcbUnits | None = None,
) -> str:
    """Check differential-pair consistency: undeclared P/N net pairs (naming), polarity
    swaps at component pins (with a hint when the interface tolerates inversion), series
    parts and AC coupling symmetric on both legs (value, footprint, DNP in the active
    variant), and in-pair skew against within-pair MatchedLengths rules.

    pattern is a regex on pair or net names; checks narrows to naming, polarity, series,
    skew. Findings are paginated (offset from next_offset), warnings first. Lengths use
    units (default: board display unit)."""
    index, error = _pcb_index_or_error()
    if error:
        return json.dumps(error)
    to_mm, _, units_error = _pcb_units_or_error(index, units)
    if units_error:
        return json.dumps(units_error)
    _, netlist, variant_state = _require_project(netlist=False)
    result = run_diff_pair_checks(
        index,
        netlist,
        is_dnp=variant_state.is_dnp,
        checks=checks,
        pattern=pattern,
        pair_lengths=lambda pair: pair_lengths(index, pair),
        offset=offset,
    )
    if "error" in result:
        return json.dumps(result)
    return json.dumps(_convert_units(result, to_mm), indent=2)


def _sheets_saved_after(project: dict, saved: float) -> list[str]:
    """The project's sheets saved after the given time (a missing sheet is skipped)."""
    newer = []
    for sheet in project.get("sheets", []):
        try:
            if Path(sheet["path"]).stat().st_mtime > saved:
                newer.append(sheet["name"])
        except OSError:
            continue
    return newer


def _saved_after_netlist(project: dict, compiled: float) -> tuple[list[str], list[str]]:
    """Files saved after the netlist was compiled, as refresh_netlist judges them (the
    .PrjPcb, its sheets and .Annotation files, other sheets in those folders): sheet
    names, and the other file names."""
    prj_path = project.get("prj_pcb_path")
    try:
        prj = parse_prj_pcb(prj_path)
        sources = netlist_sources(prj_path, prj.sheet_paths, prj.annotation_paths)
    except Exception:
        sources = [Path(sheet["path"]) for sheet in project.get("sheets", [])]
    sheets: list[str] = []
    others: list[str] = []
    seen: set[str] = set()
    for source in sources:
        source = Path(source)
        key = os.path.normcase(str(source.resolve()))
        if key in seen:
            continue
        seen.add(key)
        try:
            if source.stat().st_mtime <= compiled:
                continue
        except OSError:
            continue
        if source.suffix.lower() == ".schdoc":
            sheets.append(source.stem)
        else:
            others.append(source.name)
    return sheets, others


def _utc(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


@mcp.tool(
    title="Check Schematic-PCB Sync",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def check_sch_pcb_sync(checks: list[str] | None = None, offset: int = 0) -> str:
    """Compare the schematic with the saved PCB offline, like Altium's Project > Show
    Differences: parts missing from or extra on the PCB, designator, footprint and comment
    differences, pads whose net differs from their pin's, renamed nets, nets on one side
    only, and schematic pins whose placed footprint has no pad (Update PCB stops with
    'Unknown Pin'). Parts link by unique ID, then designator; nets by shared members, then
    name (the project's Comparator settings). The schematic side is the compiled netlist
    from the last load or refresh_netlist; netlist_stale lists sheets saved after it was
    compiled, whose changes are not compared until refresh_netlist. in_sync covers every
    check, also those left out by checks.

    checks narrows to components, pins, nets. Findings are paginated (offset from
    next_offset), errors first; each names the Altium difference type in altium_names."""
    try:
        project, netlist, variant_state = _require_project()
    except ProjectNotReady as e:
        return json.dumps(e.payload)
    index, error = _pcb_index_or_error()
    if error:
        return json.dumps(error)
    no_components = _no_components_error(netlist)
    if no_components:
        return json.dumps(no_components)
    pcb_path = Path(project["pcb_doc_paths"][0])
    pcb_saved = pcb_path.stat().st_mtime
    newer = _sheets_saved_after(project, pcb_saved)
    compiled = netlist.get("net_file_mtime")
    stale, stale_others = _saved_after_netlist(project, compiled) if compiled else ([], [])
    options = net_matching_options(project.get("prj_pcb_path"))
    for limit in (100, 30, 10):
        result = run_sync_check(
            index,
            netlist,
            is_dnp=variant_state.is_dnp,
            checks=checks,
            offset=offset,
            limit=limit,
            sheets_saved_after_pcb=newer,
            net_matching=options,
        )
        if "error" in result:
            return json.dumps(result)
        context: dict = {
            "pcb_doc": pcb_path.name,
            "pcb_saved_utc": _utc(pcb_saved),
            "netlist_updated_utc": _netlist_last_updated,
        }
        if _offline(netlist):
            context["netlist_source"] = NETLIST_SOURCE_OFFLINE
        if compiled:
            context["netlist_compiled_utc"] = _utc(compiled)
        if stale or stale_others:
            context["netlist_stale"] = True
            if stale:
                context["sheets_saved_after_netlist"] = stale
            if stale_others:
                context["other_files_saved_after_netlist"] = stale_others
            context["hint"] = (
                "These files were saved after the netlist was compiled: their changes "
                "are not compared. Call refresh_netlist, then check again."
            )
        context["sheets_saved_after_pcb"] = newer
        if netlist.get("sheet_errors"):
            context["sheet_errors"] = netlist["sheet_errors"][:20]
        result = {**context, **result}
        if len(project["pcb_doc_paths"]) > 1:
            result["note"] = (
                f"Project lists {len(project['pcb_doc_paths'])} PcbDocs; compared with "
                f"'{pcb_path.name}'."
            )
        if limit < 100:
            result["findings_limited_to"] = limit
        rendered = json.dumps(result, indent=2)
        if len(rendered) <= QUERY_NET_MAX_RESULT_SIZE_CHARS:
            break
    return rendered


# ---------- check_directive_sync ----------

DirectiveKind = Literal["net_class", "diff_pair", "rule", "component_class", "room"]
# (findings, directive rows) per page, shrunk until the page fits.
DIRECTIVE_SYNC_STEPS = ((100, 60), (60, 40), (40, 25), (25, 15), (15, 10), (10, 5), (5, 3),
                        (2, 2), (1, 1))


@mcp.tool(
    title="Check Directive Sync",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def check_directive_sync(
    kind: DirectiveKind | None = None,
    sheet: str | None = None,
    offset: int = 0,
    directives_offset: int = 0,
) -> str:
    """Did the schematic's directives reach the PCB? Call it for "are my net classes /
    differential pairs / schematic rules on the board?", "did Update PCB bring my
    directives over?", "why is this pair not on the PCB?", or the constraint part of a
    schematic-to-layout review. Offline, from the sheets as saved now and the saved PcbDoc.

    Schematic side, per item with its sheets and nets: net classes from Parameter Set
    directives with ClassName (on a wire, a bus's members, a signal harness's nets or the
    nets under a blanket), differential pairs from Differential Pair directives (named by
    the _P / _N suffixes and the project's own; with a ClassName, a differential pair
    class), design rules from Rule parameters (kind, name, scope and values such as width,
    clearance and pair gap, decoded leniently), net classes for buses, and per-sheet
    component classes and rooms, each with whether the project's Class Generation options
    enable it. PCB side: net, pair and component classes with their members, differential
    pairs, rooms and the rules marked as defined by the schematic; nets renamed on the PCB
    are followed (check_sch_pcb_sync's matching), and parts Update PCB may not place
    (mechanical, graphical, no footprint) are left out of component classes. project_type
    says classic or Constraint Manager (the .PrjPcb's [Design] ConstraintManagerFlow
    switch, a Settings/Constraints file, the PcbDoc's constraint data; the switch at 0
    means classic): in a Constraint Manager project directives are ignored unless
    imported, so every finding is info.

    Findings: directive_class_missing, bus_class_missing, class_name_differs (a class
    matched by its members under another name), class_members_differ (members missing
    from the PCB class: warning; PCB members no directive gives: info),
    diff_pair_missing, diff_pair_nets_differ, diff_pair_name_differs (same nets, another
    name: info), directive_rule_missing, rule_values_differ, rule_text_not_decoded,
    component_class_missing, room_missing and stale_pcb_item (a PCB rule marked as from
    the schematic, pair or class that no directive gives now: info). Where Class
    Generation leaves a kind out, Update PCB neither adds nor changes its classes, so its
    findings are info (option_off). summary counts per kind the items, transferred,
    missing, differing, disabled by options and those from directives.

    kind narrows to net_class, diff_pair, rule, component_class or room; sheet to items
    on one sheet. Findings page with offset (next_offset), the directive rows with
    directives_offset (next_directives_offset); the page shrinks to fit."""
    try:
        project, netlist, _ = _require_project()
    except ProjectNotReady as e:
        return json.dumps(e.payload)
    index, error = _pcb_index_or_error()
    if error:
        return json.dumps(error)
    prj_path = project["prj_pcb_path"]
    sheet_paths = [s["path"] for s in project.get("sheets") or []] or None
    try:
        compiled = _altium.compiled_netlist(prj_path, sheet_paths)
    except Exception as e:
        return json.dumps({"error": "compile_failed", "message": f"{type(e).__name__}: {e}"})
    try:
        options = parse_prj_options(prj_path)
    except (OSError, UnicodeDecodeError) as e:
        return json.dumps({"error": "project_unreadable",
                           "message": f"{type(e).__name__}: {e}"})
    pcb_path = project["pcb_doc_paths"][0]
    model = build_directive_sync_cached(
        compiled, index, options, netlist, net_matching_options(prj_path),
        detect_project_type(prj_path, pcb_path))
    if "error" in model:
        return json.dumps(model)
    context: dict = {"pcb_doc": Path(pcb_path).name, "compiled_from": "the sheets as saved now"}
    if len(project["pcb_doc_paths"]) > 1:
        context["note"] = (f"Project lists {len(project['pcb_doc_paths'])} PcbDocs; compared "
                           f"with '{Path(pcb_path).name}'.")
    if compiled.sheet_errors:
        context["sheet_errors"] = compiled.sheet_errors[:20]
    for limit, rows in DIRECTIVE_SYNC_STEPS:
        page = page_directive_sync(model, kind=kind, sheet=sheet, offset=offset, limit=limit,
                                   rows_offset=directives_offset, rows_limit=rows)
        if "error" in page:
            return json.dumps(page)
        result = {**context, **page}
        if limit != DIRECTIVE_SYNC_STEPS[0][0]:
            result["page_limited_to"] = limit
        rendered = json.dumps(result, indent=2)
        if len(rendered) <= QUERY_NET_MAX_RESULT_SIZE_CHARS:
            break
    return rendered


# ---------- get_constraints_brief / generate_rule_file ----------

ConstraintsSection = Literal["net_classes", "length_matching", "placement", "zones",
                             "designer_notes", "plan"]
RuleFileFormat = Literal["rul", "csv", "steps"]
# (items per list, members per item) per page, shrunk until the page fits.
CONSTRAINTS_BRIEF_STEPS = ((100, 30), (60, 20), (40, 12), (25, 8), (15, 5), (10, 3), (5, 2),
                           (2, 1), (1, 1))
CONSTRAINTS_PLAN_PAGES = (40, 20, 10, 5, 2, 1)  # plan classes per page when it is too long
CONSTRAINTS_PLAN_WHOLE_MIN = 15  # below this many items per list the plan becomes a pointer
RULE_FILE_INLINE_STEPS = (40_000, 20_000, 10_000, 4_000, 1_500)


def _constraints_inputs(project: dict, netlist: dict | None, variant_state) -> dict:
    """What build_brief reads besides the netlist: the PcbDoc (when it loads), the offline
    compile (directives, schematic positions), the project options and type, the notes,
    check_directive_sync's summary, the variant's plan; and the context notes."""
    out: dict = {"index": None, "compiled": None, "options": None, "project_type": None,
                 "notes": [], "directive_summary": None, "context": {}}
    prj_path = project.get("prj_pcb_path")
    pcb_paths = project.get("pcb_doc_paths") or []
    if pcb_paths:
        index, error = _pcb_session.get(project, netlist)
        if error:
            out["context"]["pcb_unavailable"] = (
                f"{error['error']}: {error.get('message', '')}".strip(": "))
        else:
            out["index"] = index
            out["context"]["pcb_doc"] = Path(pcb_paths[0]).name
            if len(pcb_paths) > 1:
                out["context"]["note"] = (f"Project lists {len(pcb_paths)} PcbDocs; read "
                                          f"'{Path(pcb_paths[0]).name}'.")
    else:
        out["context"]["pcb_unavailable"] = "the project lists no PcbDoc"
    if prj_path:
        try:
            out["options"] = parse_prj_options(prj_path)
        except (OSError, UnicodeDecodeError) as error:
            out["context"]["options_unreadable"] = type(error).__name__
        out["project_type"] = detect_project_type(prj_path, pcb_paths[0] if pcb_paths else None)
        if netlist is not None:
            sheet_paths = [s["path"] for s in project.get("sheets") or []] or None
            try:
                out["compiled"] = _altium.compiled_netlist(prj_path, sheet_paths)
            except Exception as error:  # the compile adds directives and positions only
                out["context"]["compile_failed"] = f"{type(error).__name__}: {error}"
    try:
        out["notes"], _ = note_rows(_design_notes, project)
    except Exception as error:  # notes are an extra source
        out["context"]["notes_unreadable"] = type(error).__name__
    if out["index"] is not None and out["compiled"] is not None and netlist is not None:
        try:
            model = build_directive_sync_cached(
                out["compiled"], out["index"], out["options"], netlist,
                net_matching_options(prj_path), out["project_type"])
            if "error" not in model:
                out["directive_summary"] = model["summary"]
        except Exception as error:  # the directive comparison is an extra source
            out["context"]["directive_sync_failed"] = type(error).__name__
    return out


@mcp.tool(
    title="Get Constraints Brief",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def get_constraints_brief(
    sections: list[ConstraintsSection] | None = None,
    sheet: str | None = None,
    units: PcbUnits | None = None,
    copper_oz: str | None = None,
    temp_rise: float | None = None,
    current: str | None = None,
    hv: str | None = None,
    offset: int = 0,
) -> str:
    """Schematic-to-layout constraints brief, offline and read-only: what the layout
    engineer needs from the schematic, each item with its evidence and whether the PCB or
    the schematic directives already cover it (a PCB rule covers an item only when its
    values and matching mode ask at least as much; one that takes the members but asks
    less is listed under "weaker", and the plan names it in board_rules). Call it for
    "write a layout handoff",
    "which net classes, differential pairs and length matching does this need?", "which
    decoupling caps go where?", "what should I tell the PCB designer?", or before a
    constraints review on a new layout.

    net_classes: differential pairs (PCB pairs, Differential Pair directives, pair naming)
    grouped by interface (USB 2 / 3, PCIe, SATA, HDMI, DisplayPort, Ethernet MDI, SerDes,
    DDR strobes and clocks, LVDS, MIPI, CAN, RS-485, clocks) from their names, the parts on
    their legs and the processor pin roles, with typical impedance and in-pair skew from
    constraints_review's INTERFACE_TARGETS (typical, cited: never the requirement); power
    classes by rail current (check_power_paths' estimate; current= as it takes it, e.g.
    'VIN=3,3V3=0.5,U5=0.3') with the IPC-2221 minimum width outside and inside at
    temp_rise (C, default 10) and copper_oz (default the stack's, else 1 oz; '2' or
    'outer=2,inner=1'); rails with no known current form one class with no width;
    high-voltage classes (above 30 V peak, from rail names, the power tree, switch nodes or
    hv= as check_hv_clearance takes it) with the IPC-2221B B2 clearance (B1 inside); RF
    nets only when an RF part's RF pin, an RF net name or an antenna corroborates them.
    length_matching: pair classes' in-pair tolerance, DDR byte lanes (DQ, DM and their DQS,
    one lane per side of series resistors: DDR4 2 ps, about 10 mil, typical) and
    address/command groups with their channel's clocks, RGMII groups with their clocks and
    schematic buses; a group without a sourced tolerance gets none; MatchedLengths rules
    through xSignals count as coverage. placement: decoupling capacitors per IC and rail
    (check_decoupling's pairing; at a pin only when the schematic draws them at it, else
    one per supply pin), TVS/ESD parts at their connectors
    (check_interfaces), crystal keep-tight groups (load capacitors, resistors, oscillator
    pins) and switching-converter groups (input capacitors, switch-node inductor and
    diode, bootstrap capacitor, output capacitors, feedback divider), each measured on the
    PcbDoc when it loads (placement_guidance: what each kind asks, with the screening
    distance its within_limit uses). zones: each sheet's functional zones (digital, power, analog, io,
    filtering, rf) with the parts or name behind each; designer_notes: sheet notes that
    state placement intent, quoted. plan: the proposed constraint plan (classes, and rules
    with values in units) to agree with the user and pass to generate_rule_file; rules the
    PCB already covers are left out (existing.rules_already_covered). needs_input says
    what to ask for (currents, voltages, impedance profiles).

    Heuristic, from names and the other checks: say so, quote typical values as typical,
    and ask rather than invent a requirement. sections narrows the output, sheet narrows
    the items to one sheet; lists page together with offset (next_offset) and shrink to
    fit. The plan is never cut: whole on the first page, or a pointer to
    sections=['plan'] when the page cannot hold it (a plan too long alone pages by class,
    each rule beside its class). Lengths in units, default mm."""
    try:
        project, netlist, variant_state = _require_project()
    except ProjectNotReady as error:
        return json.dumps(error.payload)
    no_components = _no_components_error(netlist)
    if no_components:
        return json.dumps(no_components)
    if units not in (None, "mm", "mil"):
        return json.dumps({"error": "invalid_units", "message": "units must be 'mil' or 'mm'."})
    resolved = units or "mm"
    chosen, error = parse_brief_sections(sections)
    if error:
        return json.dumps(error)
    if temp_rise is not None and not _in_range(temp_rise, 1.0, 100.0):
        return json.dumps({"error": "invalid_temp_rise",
                           "message": "temp_rise is the allowed temperature rise in C, 1 to "
                                      "100 (default 10)."})
    ounces, error = parse_copper_oz(copper_oz)
    if error:
        return json.dumps(error)
    currents, error = parse_power_currents(current)
    if error:
        return json.dumps(error)
    levels, error = parse_hv(hv)
    if error:
        return json.dumps(error)
    if sheet is not None:
        names = {s["name"].lower() for s in project.get("sheets") or []}
        wanted = sheet.strip().lower()
        if wanted.endswith(".schdoc"):
            wanted = wanted[:-len(".schdoc")]
        if wanted not in names:
            return json.dumps({"error": "sheet_not_found",
                               "message": f"No sheet '{sheet}'. Sheets: "
                                          f"{[s['name'] for s in project.get('sheets') or []]}"})
    inputs = _constraints_inputs(project, netlist, variant_state)
    plan, _, _ = _bom_plan(netlist, variant_state, None)
    assumptions = BriefAssumptions(copper_oz=ounces,
                                   temp_rise_c=10.0 if temp_rise is None else float(temp_rise),
                                   current=currents, hv=levels, units=resolved)
    try:
        model = build_brief_cached(
            netlist, plan, project.get("parameters") or {}, index=inputs["index"],
            compiled=inputs["compiled"], options=inputs["options"],
            project_type=inputs["project_type"], notes=inputs["notes"],
            directive_summary=inputs["directive_summary"], assumptions=assumptions)
    except Exception as error:
        return json.dumps({"error": "brief_failed", "message": f"{type(error).__name__}: {error}"})
    context = {"netlist_updated_utc": _netlist_last_updated, "units": resolved,
               **inputs["context"]}
    to_mm = resolved == "mm"
    wanted = chosen or list(CONSTRAINTS_SECTIONS)
    # (items per list, members per item, the plan whole / as a pointer / classes per page):
    # the plan is never cut inside; a page that cannot hold it whole points to it, and a
    # plan alone too long pages by class with each rule beside its class.
    attempts = [(limit, members, None) for limit, members in CONSTRAINTS_BRIEF_STEPS]
    if "plan" in wanted and len(wanted) > 1:
        # Rather a pointer to the plan than lists cut to a few items.
        attempts = [(limit, members, None) for limit, members in CONSTRAINTS_BRIEF_STEPS
                    if limit >= CONSTRAINTS_PLAN_WHOLE_MIN] + [
            (limit, members, "pointer") for limit, members in CONSTRAINTS_BRIEF_STEPS]
    elif wanted == ["plan"]:
        attempts += [(count, 1, count) for count in CONSTRAINTS_PLAN_PAGES]
    for limit, members, plan_limit in attempts:
        result = _convert_units({**context, **render_brief(
            model, chosen, sheet=sheet, offset=offset, limit=limit, member_limit=members,
            plan_limit=plan_limit)}, to_mm)
        if limit != CONSTRAINTS_BRIEF_STEPS[0][0] or plan_limit is not None:
            result["page_limited_to"] = limit
        rendered = json.dumps(result, indent=2)
        if len(rendered) <= QUERY_NET_MAX_RESULT_SIZE_CHARS:
            break
    return rendered


@mcp.tool(
    title="Generate Rule File",
    # It writes files only into the folder the user names, never over a file, and never
    # touches a design file.
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def generate_rule_file(
    plan: dict | str | None = None,
    output_dir: str | None = None,
    formats: list[RuleFileFormat] | None = None,
    units: PcbUnits | None = None,
    copper_oz: str | None = None,
    temp_rise: float | None = None,
    current: str | None = None,
    hv: str | None = None,
) -> str:
    """Turn an agreed constraint plan into files the engineer applies in Altium; it never
    edits a design file. Call it after the user agrees get_constraints_brief's plan: pass
    plan= the agreed JSON (units, classes {name, kind net|diff_pair, members}, rules {name,
    kind Width|Clearance|DiffPairsRouting|MatchedLengths|Length, scope {net_class|
    diff_pair_class|net|diff_pair: name} or 'All' or {query}, scope2 for Clearance only,
    values, optional inner and priority}), or nothing to rebuild the brief's plan as
    proposed; then pass the same current=, hv=, copper_oz= and temp_rise= the brief was
    asked with (plan_assumptions says which were used).

    rul: a .RUL file, one rule record per line in the PcbDoc's own Rules6 format (Import
    Rules in the PCB Rules and Constraints Editor), unique names (renamed when the board
    already has one), priorities numbered per kind around the plan's own, scopes
    InNetClass('X') / InDifferentialPairClass('X') / InNet('X'), values in mil to the 4
    decimals Altium stores; parsed back with the PcbDoc's rule reader (every key and value)
    before it is given, and withheld with the mismatches when it does not read back as
    written. Its import into Altium is untested: say so, suggest importing on a copy of the
    board first, selecting only the file's rule kinds and answering No to 'Clear existing
    rules prior to import?' (import_steps). priorities and board_priorities say where the
    new rules sit against the board's rules of their kind. csv: class, kind, member (pairs
    with their nets). steps: a Markdown list: classic projects get Parameter Set
    (ClassName) and Differential Pair directives, Design > Classes and Import Rules;
    Constraint Manager projects get the Constraint Manager's entries.

    output_dir (an absolute folder the user names, '~' allowed; created when missing)
    receives <PcbDoc>-constraints.rul, -net-classes.csv and -constraints-steps.md, an
    existing file kept and the new one numbered (-2); nothing is written without it: the
    texts come inline (cut at a line when long). formats picks rul, csv, steps (default
    all); units is the plan's units when it gives none (default mm)."""
    if isinstance(plan, str):
        try:
            plan = json.loads(plan)
        except ValueError as error:
            return json.dumps({"error": "invalid_plan",
                               "message": f"plan is not JSON: {error}"})
    try:
        project, netlist, variant_state = _require_project(netlist=plan is None)
    except ProjectNotReady as error:
        return json.dumps(error.payload)
    if units not in (None, "mm", "mil"):
        return json.dumps({"error": "invalid_units", "message": "units must be 'mil' or 'mm'."})
    chosen = list(dict.fromkeys(formats or RULE_FILE_FORMATS))
    if unknown := [kind for kind in chosen if kind not in RULE_FILE_FORMATS]:
        return json.dumps({"error": "invalid_formats",
                           "message": f"Unknown formats {unknown}; use {list(RULE_FILE_FORMATS)}."})
    if output_dir is not None:
        folder = Path(str(output_dir).strip()).expanduser()
        if not str(output_dir).strip() or not folder.is_absolute() or (
                folder.exists() and not folder.is_dir()):
            return json.dumps({"error": "output_dir_invalid",
                               "message": f"output_dir '{output_dir}' is not an absolute folder "
                                          "path (e.g. C:\\Users\\me\\handoff or ~/handoff)."})
        output_dir = str(folder)
    given = {"copper_oz": copper_oz, "temp_rise": temp_rise, "current": current, "hv": hv}
    if plan is not None and any(value is not None for value in given.values()):
        return json.dumps({"error": "assumptions_need_no_plan",
                           "message": "current, hv, copper_oz and temp_rise rebuild the "
                                      "brief's plan: pass them without plan= (a given plan "
                                      "already holds its values)."})
    if temp_rise is not None and not _in_range(temp_rise, 1.0, 100.0):
        return json.dumps({"error": "invalid_temp_rise",
                           "message": "temp_rise is the allowed temperature rise in C, 1 to "
                                      "100 (default 10)."})
    ounces, error = parse_copper_oz(copper_oz)
    if error:
        return json.dumps(error)
    currents, error = parse_power_currents(current)
    if error:
        return json.dumps(error)
    levels, error = parse_hv(hv)
    if error:
        return json.dumps(error)
    inputs = _constraints_inputs(project, netlist, variant_state) if plan is None else None
    if plan is None:
        no_components = _no_components_error(netlist)
        if no_components:
            return json.dumps(no_components)
        variant_plan, _, _ = _bom_plan(netlist, variant_state, None)
        assumptions = BriefAssumptions(
            copper_oz=ounces, temp_rise_c=10.0 if temp_rise is None else float(temp_rise),
            current=currents, hv=levels, units=units or "mm")
        try:
            model = build_brief_cached(
                netlist, variant_plan, project.get("parameters") or {}, index=inputs["index"],
                compiled=inputs["compiled"], options=inputs["options"],
                project_type=inputs["project_type"], notes=inputs["notes"],
                directive_summary=inputs["directive_summary"], assumptions=assumptions)
        except Exception as error:
            return json.dumps({"error": "brief_failed",
                               "message": f"{type(error).__name__}: {error}"})
        plan = model["plan"]
        if not plan["classes"] and not plan["rules"]:
            return json.dumps({"error": "empty_plan",
                               "message": "The brief proposes no class and no rule for this "
                                          "design: pass plan= with the constraints agreed."})
        index, project_type, context = (inputs["index"], inputs["project_type"],
                                        dict(inputs["context"]))
        context["plan_source"] = "get_constraints_brief's proposed plan"
        context["plan_assumptions"] = {
            key: value for key, value in model["assumptions"].items()
            if key in ("units", "temp_rise_c", "current_given", "hv_given")}
        context["plan_assumptions"]["copper_oz"] = {
            kind: f"{info['oz']:g} oz ({info['source']})"
            for kind, info in model["assumptions"]["copper"].items()}
        if not currents and not levels:
            context["plan_assumptions_note"] = (
                "No current= or hv= was given: power width and high-voltage clearance "
                "rules appear only for currents and voltages the files give. Pass the "
                "values the brief was asked with.")
    else:
        index, context = None, {"plan_source": "given"}
        pcb_paths = project.get("pcb_doc_paths") or []
        if pcb_paths:
            index, error = _pcb_session.get(project, netlist)
            if error:
                index = None
                context["pcb_unavailable"] = (
                    f"{error['error']}: {error.get('message', '')}".strip(": "))
            else:
                context["pcb_doc"] = Path(pcb_paths[0]).name
        project_type = detect_project_type(project.get("prj_pcb_path"),
                                           pcb_paths[0] if pcb_paths else None)
    pcb_paths = project.get("pcb_doc_paths") or []
    stem = Path(pcb_paths[0]).stem if pcb_paths else project.get("name") or "board"
    kind = (project_type or {}).get("type", "classic")
    result = build_rule_files(plan, index=index, project_type=kind, stem=stem,
                              project=project.get("name"), default_units=units or "mm",
                              formats=tuple(chosen))
    if "error" in result:
        return json.dumps(result)
    context["project_type"] = kind
    if output_dir is not None:
        try:
            files = write_rule_files(result, output_dir)
        except (OSError, ValueError) as error:
            return json.dumps({"error": "write_failed",
                               "message": f"The files could not be written: {error}"})
        body = {key: value for key, value in result.items() if key not in ("texts",)}
        body["files"] = files
        return json.dumps({**context, **body}, indent=2)
    for limit in RULE_FILE_INLINE_STEPS:
        shown, cut = inline_texts(result, limit)
        body = {key: value for key, value in result.items() if key not in ("texts",)}
        body["inline"] = shown
        if cut:
            body["inline_truncated"] = cut
            body["inline_note"] = ("Cut at a line to fit (whole records only; a text with no "
                                   "line that fits is left out): name an output_dir to get "
                                   "the whole files.")
        body["written"] = "nothing: no output_dir was given"
        rendered = json.dumps({**context, **body}, indent=2)
        if len(rendered) <= QUERY_NET_MAX_RESULT_SIZE_CHARS:
            break
    return rendered


# ---------- diff_design ----------

DiffSection = Literal[
    "ict_hazards", "project", "components", "variants", "bom", "nets", "placement",
    "rules", "classes", "diff_pairs", "stackup", "routing",
]


@mcp.tool(
    title="Diff Design",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def diff_design(
    rev_a: str | None = None,
    rev_b: str | None = None,
    project_dir_a: str | None = None,
    project_dir_b: str | None = None,
    section: DiffSection | None = None,
    units: PcbUnits | None = None,
    offset: int = 0,
) -> str:
    """What changed between two states of the project, as an ECO-style changelog for a
    review or release notes. Sources: two revisions of the Git clone holding the loaded
    project (rev_a older, rev_b newer: a commit hash, branch, tag, HEAD~n, or 'working'
    for the files on disk; rev_b defaults to working, rev_a to the last commit that
    changed a design file, or to rev_b's parent when rev_b is a commit), or two project
    folders (project_dir_a, project_dir_b; b defaults to the loaded project's folder),
    e.g. an archived release. With no revisions the recent commits that changed design
    files are listed (recent_commits) so the user can pick. Revisions are read offline
    with read-only git commands into a temporary folder that is deleted; the clone is
    never written.

    Sections: project (sheets, project parameters), components (added, removed,
    re-designated, revalued: Comment/Value, footprint, parameters), variants (Not Fitted
    and alternate parts per variant, varied parameters), bom (line changes per variant;
    variants whose changes equal the base BOM's are named in
    bom_variants_same_as_base), nets (from the PcbDoc: added, removed, renamed, merged,
    split, pins moved between nets), placement (parts added, removed, moved, rotated,
    flipped, footprint or designator changed on the board), rules, classes, diff_pairs,
    stackup (layers, thickness, impedance profiles) and routing (per-net routed length
    and via count). ict_hazards come first: nets that kept their name while their pins
    changed, and designators now on a different part. Components match by UniqueId,
    nets by shared pins then name, as Altium's comparator does. Purely graphical edits
    are not reported. Nets come from the PcbDoc: schematic wiring not pushed to the
    board is not seen (check_sch_pcb_sync). Lengths in units (default: the board's).
    Paginated: section= narrows to one section, offset from next_offset; summary and
    notes come with the first page."""
    try:
        if project_dir_a or project_dir_b:
            loaded = _project or {}
            folder_b = project_dir_b or loaded.get("root_dir")
            if not project_dir_a or not folder_b:
                return json.dumps({
                    "error": "folders_required",
                    "message": "Pass project_dir_a (and project_dir_b, or load the project "
                               "to compare with) to compare two folders.",
                })
            prj_name = Path(loaded["prj_pcb_path"]).name if loaded.get("prj_pcb_path") else None
            model = diff_folders(project_dir_a, folder_b, prj_name, prj_name, units)
        else:
            if _project is None:
                return json.dumps({
                    "error": "no_project",
                    "message": "No project loaded. Call set_project_dir first, or pass "
                               "project_dir_a and project_dir_b.",
                })
            model = diff_revisions(_project["prj_pcb_path"], rev_a, rev_b, units)
    except DiffError as error:
        return json.dumps(error.payload)
    except Exception as error:
        logging.warning("Design diff failed: %s", error)
        return json.dumps({"error": "diff_failed",
                           "message": f"{type(error).__name__}: {error}"})
    to_mm = model.get("units") == "mm"
    for limit in (200, 80, 30, 10, 1):
        page = _convert_units(page_design_diff(model, offset, limit, section), to_mm)
        rendered = json.dumps(page, indent=2)
        if len(rendered) <= QUERY_NET_MAX_RESULT_SIZE_CHARS:
            break
    return rendered


@mcp.tool(
    title="Check Decoupling",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def check_decoupling(
    ic: str | None = None,
    rail: str | None = None,
    checks: list[str] | None = None,
    max_distance: float | None = None,
    units: PcbUnits | None = None,
    offset: int = 0,
) -> str:
    """Check decoupling offline: which capacitors decouple each IC's power pins, and the
    gaps. Call it when the user asks about decoupling or bypass capacitors, whether an
    IC's power pins are decoupled, or for a design or layout review. Per rail: no
    capacitor to ground, only capacitors Not Fitted in the active variant, a rail joined
    through a ferrite or resistor to capacitors with none of its own (warnings); only
    capacitors to other nets (bootstrap, another rail, a 0 ohm link), no small (<1 uF)
    capacitor, unreadable values (info); no_bulk_capacitor only when named in checks
    (bulk is often upstream; each rail's counts are in rails). With a PcbDoc, pairs gives
    each IC rail's nearest capacitor (and nearest under 1 uF) with its distance, and a
    warning when the nearest fitted capacitor is farther than max_distance (straight
    line, pad edge to pad edge); power pads far from every capacitor, and an
    opposite-side capacitor with no via or pour found at its pad, are info. Naming ICs in
    ic lists their 5 nearest capacitors ('is C12 close enough to U3?').

    It is advice, not DRC: rails are inferred from net and pin names, ICs are U and IC
    parts fitted in the variant (ic also takes others, e.g. a connector), the datasheet
    decides how many capacitors a part needs, and regions and planes are not read. ic and
    rail take comma-separated names; checks narrows to the names in summary. Findings
    are paginated (offset from next_offset), warnings first. max_distance and every
    distance are in units, default mm (max_distance default 5 mm)."""
    try:
        project, netlist, variant_state = _require_project()
    except ProjectNotReady as error:
        return json.dumps(error.payload)
    no_components = _no_components_error(netlist)
    if no_components:
        return json.dumps(no_components)
    index, pcb_unavailable = None, None
    if units not in (None, "mm", "mil"):
        return json.dumps({"error": "invalid_units", "message": "units must be 'mil' or 'mm'."})
    resolved = units or "mm"  # decoupling guidance is in mm, whatever the board shows
    to_mm = resolved == "mm"
    if project.get("pcb_doc_paths"):
        index, error = _pcb_session.get(project, netlist)
        if error:
            pcb_unavailable = f"{error['error']}: {error.get('message', '')}".strip(": ")
    else:
        pcb_unavailable = "the project lists no PcbDoc"
    max_mil = DEFAULT_MAX_DISTANCE_MIL
    if max_distance is not None:
        if not _finite_nonnegative(max_distance) or max_distance == 0:
            return json.dumps({"error": "invalid_max_distance",
                               "message": "max_distance must be a positive number."})
        max_mil = _input_to_mil(max_distance, resolved)
    plan, _, _ = _bom_plan(netlist, variant_state, None)
    for limit, rail_limit in ((100, 60), (40, 20), (15, 5)):
        result = run_decoupling_check(
            netlist, plan, project.get("parameters") or {}, index=index,
            pcb_unavailable=pcb_unavailable, checks=checks, ics=_split_names(ic),
            rails=_split_names(rail), max_distance_mil=max_mil, offset=offset,
            limit=limit, rail_limit=rail_limit,
        )
        if "error" in result:
            return json.dumps(result)
        context: dict = {"netlist_updated_utc": _netlist_last_updated}
        if index is not None:
            context.update({"pcb_doc": Path(project["pcb_doc_paths"][0]).name,
                            "units": resolved})
            if len(project["pcb_doc_paths"]) > 1:
                context["note"] = (
                    f"Project lists {len(project['pcb_doc_paths'])} PcbDocs; measured on "
                    f"'{Path(project['pcb_doc_paths'][0]).name}'."
                )
        result = _convert_units({**context, **result}, to_mm)
        if limit < 100:
            result["findings_limited_to"] = limit
        rendered = json.dumps(result, indent=2)
        if len(rendered) <= QUERY_NET_MAX_RESULT_SIZE_CHARS:
            break
    return rendered


# Hot parts and findings per page, tried in turn until the result fits; the board is read
# once and only rendered per step.
THERMAL_STEPS = (100, 60, 40, 25, 15, 10, 5, 2, 1)


@mcp.tool(
    title="Check Thermal",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def check_thermal(
    part: str | None = None,
    checks: list[str] | None = None,
    load: str | None = None,
    power: str | None = None,
    theta: str | None = None,
    ta: float | None = None,
    ta_max: float | None = None,
    tj_max: str | None = None,
    eta: str | None = None,
    iq: str | None = None,
    rails: str | None = None,
    vref: str | None = None,
    max_distance: float | None = None,
    units: PcbUnits | None = None,
    offset: int = 0,
) -> str:
    """Thermal estimate and thermal-via audit, offline: which parts dissipate, their
    dissipation and junction temperature where the inputs allow, and on the PcbDoc the vias
    in each exposed or thermal pad and the hot parts sitting close together. Call it when
    the user asks about heat, hot parts, thermal design, junction temperature, thetaJA,
    thermal vias or exposed pads, whether a regulator or FET runs too hot, what sits
    near a hot part, or for the thermal part of a design or layout review.

    Hot parts, each with its basis: converters from the power tree (linear regulators and
    LDOs, switchers and modules, chargers, load switches, eFuses), the FETs a converter's
    gate pins drive and the diodes on its switch node, transistors and diodes in series with
    a rail, current-sense shunts, resistors computed at 0.25 W or half their rating, LEDs at
    0.2 W, processors, FPGAs and Ethernet PHYs, LED and motor drivers and power amplifiers,
    and with a PcbDoc any part with an exposed pad or thermal tab (its largest SMD pad, or a
    fill or region its footprint draws such as a PowerPAK's drain paddle, 2.5 times the
    median of its other pads or more and 0.5 mm^2 or more: in the middle, or at one edge
    such as a DPAK or SOT-223 tab; connectors and mechanical parts left out; more than four
    such pads are a pad array, its large ground pads audited as one, pad_array says so).
    Dissipation where computable: LDO/linear P = (Vin - Vout) x I_out (+ Vin x Iq),
    switcher P = Vout x I_out x (1/eta - 1), shunt I^2 x R, diode Vf x I (its stated Vf,
    else a typical one), resistor V^2/R (check_derating's voltages; not a shunt's), LED
    Vf x I (get_circuit_calculations);
    otherwise power.needs and inputs_needed say what to ask for. With thetaJA,
    Tj = Ta + thetaJA x P at ta and at ta_max (worst case).

    Findings: tj_over_max (Tj at the worst-case Ta above tj_max: warning), tj_near_max
    (above 80% of it: info), thermal_pad_no_vias (a hot part's exposed pad with no via
    inside: warning, info when the part is not known to dissipate or four or more vias of
    its net sit within 1 mm of it or a tab sits in a pour), thermal_vias_wrong_net (a via
    in the pad on another net: warning), thermal_pad_few_vias (fewer than one via per
    1.5 mm^2, or 4 on pads over 4 mm^2, a common guideline: info), thermal_pad_unconnected
    (a pad with no net, or vias reaching no plane or pour of its net on another layer;
    vias landing in a shelved or unpoured pour are listed in
    reaches_pours_without_copper instead: info), hot_neighbours (two hot parts within
    max_distance, default 5 mm, edge to edge between their pad boxes, same or opposite
    side; not parts of one power stage, nor a part only named in part=: info) and
    sensitive_near_hot (an electrolytic, crystal, reference, temperature sensor or battery
    near a hot part, unless a crystal, reference or sensor is wired to it by a net other
    than a supply: info).
    Without a PcbDoc the PCB checks are skipped (not_checked) and the rest runs.

    Everything is an estimate: say so. thetaJA, Tj max and the loads come from the
    datasheets and the user (Claude's datasheet lookup or the user's numbers), never
    guessed; the datasheet and a thermal simulation decide. Pass load ('U5=0.3,3V3=0.5':
    amps per designator or output rail), power ('U1=1.2' W per designator, overriding the
    estimate), theta ('U5=45' C/W), ta (C, default 25), ta_max (worst-case ambient, e.g.
    85), tj_max (per designator or one for all, default 125), eta ('U7=0.88' or one for
    all), iq (a regulator's ground current, A), rails ('VIN=24') and vref as in
    get_power_tree. part (comma-separated) narrows to those parts (and adds any part named
    there); checks narrows to the names in checks_run. Hot parts (hottest first) and
    findings (warnings first) page together (offset from next_offset). max_distance and
    every distance are in units, default mm."""
    try:
        project, netlist, variant_state = _require_project()
    except ProjectNotReady as error:
        return json.dumps(error.payload)
    no_components = _no_components_error(netlist)
    if no_components:
        return json.dumps(no_components)
    if units not in (None, "mm", "mil"):
        return json.dumps({"error": "invalid_units", "message": "units must be 'mil' or 'mm'."})
    resolved = units or "mm"
    to_mm = resolved == "mm"
    max_mil = DEFAULT_THERMAL_DISTANCE_MIL
    if max_distance is not None:
        if not _finite_nonnegative(max_distance) or max_distance == 0:
            return json.dumps({"error": "invalid_max_distance",
                               "message": "max_distance must be a positive number."})
        max_mil = _input_to_mil(max_distance, resolved)
    ambient = 25.0 if ta is None else ta
    if not (math.isfinite(ambient) and -60.0 <= ambient <= 150.0):
        return json.dumps({"error": "invalid_ta",
                           "message": "ta is the ambient temperature in C, -60 to 150."})
    if ta_max is not None and not (math.isfinite(ta_max) and ambient <= ta_max <= 200.0):
        return json.dumps({"error": "invalid_ta_max",
                           "message": "ta_max is the worst-case ambient in C, from ta to 200."})
    values = {}
    for name, text in (("load", load), ("power", power), ("theta", theta),
                       ("tj_max", tj_max), ("eta", eta), ("iq", iq)):
        values[name], error = parse_thermal_values(text, name)
        if error:
            return json.dumps(error)
    vrefs, error = _parse_vref(vref)
    if error:
        return json.dumps(error)
    user_rails, error = parse_rail_map(rails)
    if error:
        return json.dumps(error)
    inputs = ThermalInputs(
        load=values["load"] or {}, power=values["power"] or {}, iq=values["iq"] or {},
        theta=values["theta"] or {}, tj_max=values["tj_max"], eta=values["eta"],
        ta=float(ambient), ta_max=float(ta_max) if ta_max is not None else None)
    index, pcb_unavailable = None, None
    if project.get("pcb_doc_paths"):
        index, error = _pcb_session.get(project, netlist)
        if error:
            pcb_unavailable = f"{error['error']}: {error.get('message', '')}".strip(": ")
    else:
        pcb_unavailable = "the project lists no PcbDoc"
    plan, _, _ = _bom_plan(netlist, variant_state, None)
    prepared = prepare_thermal(
        netlist, plan, project.get("parameters") or {}, index=index,
        pcb_unavailable=pcb_unavailable, parts=_split_names(part), checks=checks,
        rails=user_rails, vref=vrefs, inputs=inputs, max_distance_mil=max_mil,
        units=resolved, root_dir=project.get("root_dir"))
    if "error" in prepared:
        return json.dumps(prepared)
    context: dict = {"netlist_updated_utc": _netlist_last_updated}
    if index is not None:
        context.update({"pcb_doc": Path(project["pcb_doc_paths"][0]).name, "units": resolved})
        if len(project["pcb_doc_paths"]) > 1:
            context["note"] = (
                f"Project lists {len(project['pcb_doc_paths'])} PcbDocs; measured on "
                f"'{Path(project['pcb_doc_paths'][0]).name}'."
            )
    for step in THERMAL_STEPS:
        result = _convert_units({**context, **render_thermal(prepared, offset=offset,
                                                             limit=step)}, to_mm)
        if step != THERMAL_STEPS[0]:
            result["page_limited_to"] = step
        rendered = json.dumps(result, indent=2)
        if len(rendered) <= QUERY_NET_MAX_RESULT_SIZE_CHARS:
            break
    return rendered


# Findings and pairs per page, tried in turn until the result fits.
HV_STEPS = (100, 60, 40, 25, 15, 10, 5, 2, 1)


@mcp.tool(
    title="Check HV Clearance",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def check_hv_clearance(
    net: str | None = None,
    layer: str | None = None,
    checks: list[str] | None = None,
    hv: str | None = None,
    rails: str | None = None,
    groups: str | None = None,
    working_voltage: str | None = None,
    threshold: float | None = None,
    standard: Literal["ipc2221", "ipc2221_assembly", "ipc9592"] | None = None,
    coating: bool = False,
    altitude: float | None = None,
    min_distance: str | None = None,
    vref: str | None = None,
    units: PcbUnits | None = None,
    offset: int = 0,
) -> str:
    """Voltage-aware copper spacing screen on the PcbDoc, offline: the smallest
    copper-to-copper distance on each layer between nets at different potentials against
    IPC-2221B Table 6-1 (or IPC-9592B), with where it occurs. Call it when the user asks
    about high voltage, mains, creepage or clearance by voltage, isolation spacing, whether
    the HV or 400 V copper is far enough from the rest, or for a safety-spacing part of a
    layout review.

    Net voltages: check_derating's (rails= wins, grounds 0 V, names, the power tree, vref=),
    plus hv= for working voltages that are not DC rails ('L=230AC,N=0,HV_BUS=400'; AC is RMS
    and taken at its peak, x 1.414; '325pk' a peak, '230AC@120' a phase; a converter's
    switch node swings 0 V to its peak, also when given in rails= or hv=). groups= declares
    isolated groups ('PRI:L,N,HV_*,PGND;SEC:GND,3V3', wildcards) with their own reference:
    pairs across groups take working_voltage= ('250AC' or '400'), and are not judged
    without it (a note says so). A pair is judged when both voltages are known
    and differ by more than threshold (default 15 V); its copper (tracks, arcs, pads and
    vias on the layer, poured copper, regions, fills; shelved and unpoured pours have none,
    internal planes are not measured) is searched from its high-voltage side.

    Requirement per layer from the peak voltage: standard 'ipc2221' (default) B1 on
    internal layers, B2 on external ones, B3 above 3050 m (altitude= in m), B4 with
    coating=true, plus the per-volt increment above 500 V; 'ipc2221_assembly' the A
    columns (A6/A7 for a component pad, A5 coated); 'ipc9592' IPC-9592B's formulas.
    min_distance= replaces the standard ('2.5' every pair, 'HV_BUS=4' a net's pairs,
    'L&N=3' one pair, in units; unknown pairs' requirement too): IEC 62368-1 / IEC
    60664-1 clearances and creepages are not encoded, ask the user for them. Findings:
    spacing_below_standard (warning: layer, location, voltage, band, requirement, and
    slots or cutouts in the path), spacing_below_rule (info, one per board Clearance rule
    that allows less than some pairs need, listing them), unknown_voltage_pairs (info:
    nets that look like high voltage by name, connector power input, mains connector or
    part, or a switch node, with no known voltage, and unknown-voltage copper near a
    high-voltage net: ask for hv=), creepage_not_computed (info). pairs lists every pair
    measured with its distance per layer and the board's Clearance rule.

    A screening tool: say so, and that the product's spacing and safety standards decide;
    voltages from names are guesses, so ask for hv= before calling a board safe. net
    (comma-separated) narrows the search and the result to pairs with those nets, layer to
    one copper layer, checks to the names in checks_run. Findings and pairs page together
    (offset from next_offset; the result is kept for paging). Distances in units, default
    mm."""
    try:
        project, netlist, variant_state = _require_project()
    except ProjectNotReady as error:
        return json.dumps(error.payload)
    no_components = _no_components_error(netlist)
    if no_components:
        return json.dumps(no_components)
    if units not in (None, "mm", "mil"):
        return json.dumps({"error": "invalid_units", "message": "units must be 'mil' or 'mm'."})
    resolved = units or "mm"
    to_mm = resolved == "mm"
    if threshold is not None and not (math.isfinite(threshold) and 0.0 <= threshold <= 10000.0):
        return json.dumps({"error": "invalid_threshold",
                           "message": "threshold is the smallest voltage difference judged, "
                                      "0 to 10000 V (default 15)."})
    if altitude is not None and not (math.isfinite(altitude) and -500.0 <= altitude <= 20000.0):
        return json.dumps({"error": "invalid_altitude",
                           "message": "altitude is the operating altitude in metres, -500 to "
                                      "20000 (above 3050 m external layers take B3)."})
    if standard not in (None, "ipc2221", "ipc2221_assembly", "ipc9592"):
        return json.dumps({"error": "invalid_standard",
                           "message": "standard must be 'ipc2221', 'ipc2221_assembly' or "
                                      "'ipc9592'."})
    user_rails, error = parse_rail_map(rails)
    if error:
        return json.dumps(error)
    hv_levels, error = parse_hv(hv)
    if error:
        return json.dumps(error)
    group_list, error = parse_hv_groups(groups)
    if error:
        return json.dumps(error)
    working, error = parse_hv_working_voltage(working_voltage)
    if error:
        return json.dumps(error)
    overrides, error = parse_hv_min_distance(min_distance, 1 / 0.0254 if to_mm else 1.0)
    if error:
        return json.dumps(error)
    vrefs, error = _parse_vref(vref)
    if error:
        return json.dumps(error)
    if not project.get("pcb_doc_paths"):
        return json.dumps({"error": "no_pcb_document",
                           "message": "check_hv_clearance measures copper on the PcbDoc and the "
                                      "project lists none: add the board to the project."})
    index, error = _pcb_session.get(project, netlist)
    if error:
        return json.dumps(error)
    layer_id = None
    if layer is not None:
        layer_id, error = _resolve_layer_or_error(index, layer)
        if error:
            return json.dumps(error)
        if layer_id not in index.signal_layers():
            return json.dumps({"error": "layer_not_signal",
                               "available_layers": list(index.signal_layers().values()),
                               "message": f"Layer '{layer}' is not a signal layer: internal "
                                          "planes are not measured."})
    plan, _, _ = _bom_plan(netlist, variant_state, None)
    prepared = prepare_hv_cached(
        netlist, plan, project.get("parameters") or {}, index=index, rails=user_rails,
        hv=hv_levels, vref=vrefs, groups=group_list, working_voltage=working,
        threshold=15.0 if threshold is None else float(threshold),
        options=HvOptions(standard or "ipc2221", bool(coating),
                          0.0 if altitude is None else float(altitude)),
        min_distance=overrides, nets=_split_names(net), layer=layer_id, checks=checks,
        units=resolved)
    if "error" in prepared:
        return json.dumps(prepared)
    paths = project["pcb_doc_paths"]
    context: dict = {"netlist_updated_utc": _netlist_last_updated,
                     "pcb_doc": Path(paths[0]).name, "units": resolved}
    if len(paths) > 1:
        context["note"] = f"Project lists {len(paths)} PcbDocs; measured on '{Path(paths[0]).name}'."
    for step in HV_STEPS:
        result = _convert_units({**context, **render_hv(prepared, offset=offset, limit=step)},
                                to_mm)
        if step != HV_STEPS[0]:
            result["page_limited_to"] = step
        rendered = json.dumps(result, indent=2)
        if len(rendered) <= QUERY_NET_MAX_RESULT_SIZE_CHARS:
            break
    return rendered


# Rails and findings per page, tried in turn until the result fits.
POWER_PATH_STEPS = (100, 60, 40, 25, 15, 10, 5, 2, 1)


def _in_range(value: float | None, low: float, high: float) -> bool:
    return value is None or (math.isfinite(value) and low <= value <= high)


@mcp.tool(
    title="Check Power Paths",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def check_power_paths(
    rail: str | None = None,
    net: str | None = None,
    checks: list[str] | None = None,
    current: str | None = None,
    eta: str | None = None,
    rails: str | None = None,
    copper_oz: str | None = None,
    plating: float | None = None,
    temperature: float | None = None,
    stub_length: float | None = None,
    rise_info: float | None = None,
    rise_warning: float | None = None,
    drop_info: float | None = None,
    drop_warning: float | None = None,
    setup: str | None = None,
    units: PcbUnits | None = None,
    offset: int = 0,
) -> str:
    """Power path estimate on the PcbDoc, offline: per rail its current, its copper (tracks
    and arcs per layer with widths and lengths, vias with drills, pads, pour and plane
    share), the narrowest routed width, IPC-2221 temperature rise of its tracks, and the DC
    resistance and IR drop from its source to each load. Call it when the user asks whether
    a power trace is wide enough, how hot a track gets, how much current a trace or via
    carries, the IR or voltage drop to a part, high-current or power routing, enough vias
    for a current, or for the power-integrity part of a layout review.

    Currents per rail, each with its source: current= wins ('VIN=3,3V3=0.5' amps per rail;
    'U5=0.3' per load designator, on its supply rail; 'U5@3V3' picks the rail; a
    converter's designator is its output current, so its output rail carries it ('U3@5V' on
    its input rail is its input current); a connector's, battery's or series part's
    designator is the current it supplies or passes); else the sum of what draws from the
    rail: the power tree's loads, outlet connectors (power pins on a rail made on the board)
    and parts current= or the setup name with a pin on it: a load's current from current=,
    from a Power Analyzer or PDN Analyzer setup in the project (used when there is one for
    this board, or setup= names it; resistive loads V / R; converters listed there on their
    input), or from a 'Current Consumption' (or similar) part parameter; a converter draws
    its output current (linear regulators, switches) or Vout x Iout / (eta x Vin)
    (switchers, eta= as in check_thermal); a rail fed through a ferrite, fuse, 0 ohm link,
    diode or FET carries the current of the rail it feeds. Some loads unknown: a partial sum
    (lower bound). Entries applying to no rail are listed in notes. rails= gives voltages
    ('VIN=24') as in check_derating.

    Copper: rails whose pours, regions or planes hold most of their copper area are
    pour-fed: their resistance and drop are not estimated (pour_fed_not_estimated), their
    narrowest track still listed. Narrowest width per layer and overall leaves out stubs
    into pads shorter than stub_length (default 1 mm, in units; 0 keeps them); stubs are not
    judged for their rise either (hottest_neck lists the worst). Copper thickness per layer
    from the stackup (1 oz assumed where it gives none; copper_oz= '2', 'outer=2,inner=1' or
    per layer name). IPC-2221: dT = (I / (k x A^0.725))^(1/0.44), A in mil^2, k 0.048
    external, 0.024 internal, with the capacity at 10 C and 20 C rise; a rise past its
    charts' 100 C is reported as beyond_chart, not as a figure. IPC-2152 differs
    (chart-based, often less conservative) and is not applied: say so.

    Resistance: tracks and arcs rho x L / (w x t) over their length outside the pads and vias
    they end on (pad and via copper 0 ohm), rho = 1.72e-8 ohm m at 20 C x (1 + 0.00393 x
    (temperature - 20)) (temperature in C, default 20); vias rho x h / (pi x ((d/2 + p)^2 -
    (d/2)^2)), h from the stackup, plating p in um (default 25), parallel vias between the
    same copper combined. IR drop: the least-resistance track and via path (Dijkstra) from
    the source pads (converter output or its inductor, connector power pins, the series part
    feeding the rail) to each load's farthest supply pad. The paths form a tree: each piece
    carries the currents of the loads downstream of it and the drop is the sum of R x I
    along the path (drop_mv); with loads' currents unknown it is a range (drop_low_mv: the
    known loads; drop_high_mv: the rail's current through the pieces whose loads are
    unknown; a piece every load's path uses carries the whole rail current). Findings, each
    judged at the current its pieces carry (an upper bound alone gives at most info):
    temp_rise_high (rise above rise_info C, default 10: info; above rise_warning, default
    20: warning), ir_drop_high (above drop_info % of the rail voltage, default 2: info;
    above drop_warning, default 5: warning), path_through_few_vias (a layer change whose
    vias, counting parallel ones nearby, carry more than IPC-2221 allows on their barrels at
    those rises; one per set of vias), power_analyzer_fail (a report's failing net: warning;
    its warning status: info), needs_current, needs_voltage (a drop in mV on a rail of
    unknown voltage: ask for rails=) and pour_fed_not_estimated (info; also lists loads with
    no track path).

    Power Analyzer by Keysight: a <Project>.pdnaK setup is reported (read when plain text),
    the PDN Analyzer's .pdna / .pidc_config setups are listed (sources, loads, VRMs), and
    HTML reports under PowerAnalyzerByKeysight_Output\\HTMLReport are parsed: those whose
    folder (<Project> [PDNA]_<date>) or pages name this project or PcbDoc replace the
    estimate for the rails they cover with their Vdrop, Idensity, Ivia_max and status
    (quote them first); others are listed, not applied. Everything else is an estimate: say
    so, never guess a current (ask for needs_current's rails), and let Power Analyzer, the
    datasheets or a measurement decide. rail (comma-separated rails) or net (PCB nets)
    narrow it; checks to names in checks_run. Findings and rails page together (offset from
    next_offset). Lengths in
    units, default mm."""
    try:
        project, netlist, variant_state = _require_project()
    except ProjectNotReady as error:
        return json.dumps(error.payload)
    no_components = _no_components_error(netlist)
    if no_components:
        return json.dumps(no_components)
    if units not in (None, "mm", "mil"):
        return json.dumps({"error": "invalid_units", "message": "units must be 'mil' or 'mm'."})
    resolved = units or "mm"
    to_mm = resolved == "mm"
    checks_of = (
        ("plating", plating, 5.0, 100.0, "plating is the via barrel's plating thickness in "
                                         "um, 5 to 100 (default 25)."),
        ("temperature", temperature, -55.0, 200.0, "temperature is the copper temperature "
                                                   "in C for its resistivity, -55 to 200 "
                                                   "(default 20)."),
        ("stub_length", stub_length, 0.0, 2000.0 if resolved == "mil" else 50.0,
         "stub_length is the length in units below which a track into a pad is left out "
         "of the narrowest width (0 keeps every track; default 1 mm)."),
        ("rise_info", rise_info, 1.0, 200.0, "rise_info is a temperature rise in C, 1 to "
                                             "200 (default 10)."),
        ("rise_warning", rise_warning, 1.0, 200.0, "rise_warning is a temperature rise in "
                                                   "C, 1 to 200 (default 20)."),
        ("drop_info", drop_info, 0.01, 100.0, "drop_info is a drop in % of the rail "
                                              "voltage, 0.01 to 100 (default 2)."),
        ("drop_warning", drop_warning, 0.01, 100.0, "drop_warning is a drop in % of the "
                                                    "rail voltage, 0.01 to 100 (default 5)."),
    )
    for name, value, low, high, message in checks_of:
        if not _in_range(value, low, high):
            return json.dumps({"error": f"invalid_{name}", "message": message})
    currents, error = parse_power_currents(current)
    if error:
        return json.dumps(error)
    etas, error = parse_thermal_values(eta, "eta")
    if error:
        return json.dumps(error)
    ounces, error = parse_copper_oz(copper_oz)
    if error:
        return json.dumps(error)
    user_rails, error = parse_rail_map(rails)
    if error:
        return json.dumps(error)
    if not project.get("pcb_doc_paths"):
        return json.dumps({"error": "no_pcb_document",
                           "message": "check_power_paths measures copper on the PcbDoc and the "
                                      "project lists none: add the board to the project."})
    index, error = _pcb_session.get(project, netlist)
    if error:
        return json.dumps(error)
    plan, _, _ = _bom_plan(netlist, variant_state, None)
    # One threshold given past the other's default moves that default with it.
    info = POWER_DEFAULTS["rise_info_c"] if rise_info is None else float(rise_info)
    warning = POWER_DEFAULTS["rise_warning_c"] if rise_warning is None else float(rise_warning)
    if warning < info:
        if rise_warning is None:
            warning = info
        elif rise_info is None:
            info = warning
    drop_low = POWER_DEFAULTS["drop_info_pct"] if drop_info is None else float(drop_info)
    drop_high = POWER_DEFAULTS["drop_warning_pct"] if drop_warning is None else \
        float(drop_warning)
    if drop_high < drop_low:
        if drop_warning is None:
            drop_high = drop_low
        elif drop_info is None:
            drop_low = drop_high
    prepared = prepare_power_paths_cached(
        netlist, plan, project.get("parameters") or {}, index=index, rails=user_rails,
        current=currents, eta=etas, copper_oz=ounces,
        plating_um=25.0 if plating is None else float(plating),
        temperature_c=20.0 if temperature is None else float(temperature),
        stub_mil=POWER_DEFAULTS["stub_mil"] if stub_length is None else
        _input_to_mil(float(stub_length), resolved),
        rise_info_c=info, rise_warning_c=warning, drop_info_pct=drop_low,
        drop_warning_pct=drop_high, rail_names=_split_names(rail), nets=_split_names(net),
        checks=checks, setup=setup, root_dir=project.get("root_dir"),
        pcb_doc=Path(project["pcb_doc_paths"][0]).name, project_name=project.get("name"),
        units=resolved)
    if "error" in prepared:
        return json.dumps(prepared)
    paths = project["pcb_doc_paths"]
    context: dict = {"netlist_updated_utc": _netlist_last_updated,
                     "pcb_doc": Path(paths[0]).name, "units": resolved}
    if len(paths) > 1:
        context["note"] = f"Project lists {len(paths)} PcbDocs; measured on '{Path(paths[0]).name}'."
    for step in POWER_PATH_STEPS:
        result = _convert_units({**context, **render_power_paths(prepared, offset=offset,
                                                                 limit=step)}, to_mm)
        if step != POWER_PATH_STEPS[0]:
            result["page_limited_to"] = step
        rendered = json.dumps(result, indent=2)
        if len(rendered) <= QUERY_NET_MAX_RESULT_SIZE_CHARS:
            break
    return rendered


# (findings, places per finding, selected nets listed) per page, tried in turn until the
# result fits.
RETURN_PATH_STEPS = ((100, 10, 50), (60, 5, 30), (40, 3, 20), (25, 2, 10), (15, 1, 5),
                     (10, 1, 5), (5, 1, 3), (2, 1, 1), (1, 1, 1))


@mcp.tool(
    title="Check Return Path (Reference Planes)",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def check_return_path(
    net: str | None = None,
    net_class: str | None = None,
    layer: str | None = None,
    checks: list[str] | None = None,
    edge_distance: float | None = None,
    stitch_radius: float | None = None,
    units: PcbUnits | None = None,
    offset: int = 0,
) -> str:
    """Return-path and reference-plane review of the saved PcbDoc, offline: "do my fast
    signals have a continuous reference plane?", "does anything cross a plane split?",
    "which layer does this layer reference?", "are there stitching vias where the clock
    changes layers?", signal integrity and EMI of high-speed routing, or the return-path
    part of a layout review.

    layers: per signal layer its reference layers (the impedance profile's, else the
    nearest copper above and below) with kind (internal plane, large pour: one net's area
    copper covering 15% of the board, or signal), nets, dielectric height and the edge
    distance used; no_adjacent_reference flags a routed layer with no plane or large pour
    reference (warning when it carries selected nets). plane_splits lists internal planes
    split among nets (split plane polygons, with areas) and pour layers used as references
    whose copper is split among nets.

    Selected nets (selection, with reasons): net (nets or differential pairs) and net_class
    (net classes) name them; by default the members of differential pairs, of net classes
    with a high-speed name, of xSignals and of the scopes of impedance-profile-driven and
    length rules, and nets named like clocks or high-speed buses (power and ground names,
    and sideband nets such as enables, resets and I2C, left out). Each of their tracks and
    arcs, chained where they meet, is walked over the copper of every plane or pour
    reference of its layer (its own vias' and pads' clearances, and the cut-out under its
    own pads, left out). Reference copper is a plane's nets, or on a pour layer the nets
    listed for it and supply or ground nets; any other net's copper is a gap. Findings,
    grouped per net, layer and reference with their places (occurrences, longest first):
    segment_crosses_split (the reference net under it changes, e.g. GND to PWR, across a
    gap of at most 100 mil: warning), segment_over_void (no reference copper under part of
    it, with the cause: a split line, drawn void, plane edge or pullback, outside the split
    planes, merged via clearances, no copper, another net's copper; warning, but info for
    one via's or pad's clearance (hole_clearance: its anti-pad on a plane, the clearance
    measured in the pour), the cut-out under the net's own pad, or where the stripline's
    other reference stays continuous), segment_near_plane_edge (reference copper of another
    net, or none, within edge_distance of the track on either side; default 3x the
    dielectric height to that reference: info), plane_split (info) and
    missing_stitching_via: at each layer change of a selected net whose two layers share no
    reference layer, no via of the reference net through both reference layers within
    stitch_radius (default 1 mm; info), or, where the reference net changes (GND to a power
    plane), no capacitor between each pair of reference nets with a pad within it
    (warning; pairs_without_capacitor); a differential pair's two vias are judged together.
    layer_changes counts every layer change by outcome. Places resting on approximated
    copper (anti-pad sizes, default pullback, bridged hatch openings, assumed pour
    outlines) are marked likely: say so. Quote approximations when the user relies on a
    result.

    The copper is read as saved: ask the user to repour polygons and save first. It is a
    geometric screen, not a field solver: Altium's Return Path rule is not decoded.
    layer narrows the findings to one signal or reference layer; checks to names in
    checks_run (aliases layers, splits, voids, edges, plane_splits, stitching).
    edge_distance and stitch_radius use units (default: board display unit). Findings are
    paginated (offset from next_offset)."""
    index, error = _pcb_index_or_error()
    if error:
        return json.dumps(error)
    to_mm, resolved, units_error = _pcb_units_or_error(index, units)
    if units_error:
        return json.dumps(units_error)
    limits = {}
    for name, value in (("edge_distance", edge_distance), ("stitch_radius", stitch_radius)):
        if value is None:
            continue
        if not _finite_nonnegative(value) or value == 0 or \
                _input_to_mil(value, resolved) > 10000.0:
            return json.dumps({
                "error": f"invalid_{name}",
                "message": f"{name} must be a positive length in {resolved} (at most 10000 "
                           "mil).",
            })
        limits[name] = _input_to_mil(float(value), resolved)
    prepared = prepare_return_path_cached(
        index, nets=_split_names(net), net_classes=_split_names(net_class), layer=layer,
        checks=checks, edge_distance_mil=limits.get("edge_distance"),
        stitch_radius_mil=limits.get("stitch_radius"), units=resolved)
    if "error" in prepared:
        return json.dumps(prepared)
    project, _, _ = _require_project(netlist=False)
    paths = project.get("pcb_doc_paths") or []
    context: dict = {"units": resolved}
    if paths:
        context["pcb_doc"] = Path(paths[0]).name
        if len(paths) > 1:
            context["note"] = f"Project lists {len(paths)} PcbDocs; checked '{Path(paths[0]).name}'."
    for limit, occurrences, nets in RETURN_PATH_STEPS:
        result = _convert_units({**context, **render_return_path(
            prepared, offset=offset, limit=limit, occurrences=occurrences, nets=nets)}, to_mm)
        if limit != RETURN_PATH_STEPS[0][0]:
            result["page_limited_to"] = limit
        rendered = json.dumps(result, indent=2)
        if len(rendered) <= QUERY_NET_MAX_RESULT_SIZE_CHARS:
            break
    return rendered


# (findings, stretches or vias per finding, pairs listed) per page, tried in turn until the
# result fits.
DIFF_PAIR_GEOMETRY_STEPS = ((100, 10, 50), (60, 5, 30), (40, 3, 20), (25, 2, 10), (15, 1, 5),
                            (10, 1, 3), (5, 1, 2), (2, 1, 1), (1, 1, 1))


@mcp.tool(
    title="Check Differential Pair Geometry",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def check_diff_pair_geometry(
    pair: str | None = None,
    net_class: str | None = None,
    layer: str | None = None,
    checks: list[str] | None = None,
    coupling_distance: float | None = None,
    min_stretch: float | None = None,
    via_distance: float | None = None,
    units: PcbUnits | None = None,
    offset: int = 0,
    pairs_offset: int = 0,
) -> str:
    """Differential-pair routing geometry of the saved PcbDoc, offline: "is my USB / PCIe
    pair routed at the right gap?", "how much of this pair is uncoupled?", "do both legs
    change layers together?", or the pair-routing part of a high-speed layout review.

    Pairs: the PCB's differential pairs, plus nets named as complementary legs (X_P / X_N,
    D+ / D-) that are not one, marked inferred (no DiffPairsRouting rule applies to them, so
    their findings are info). Each leg's tracks and arcs are sampled along their centreline
    (arcs on the true arc); where the other leg runs parallel beside it on the same layer
    within the coupling distance (default 3x the layer's max gap, at least 2x the width;
    3x the width without a gap limit) it is coupled, and the edge-to-edge gap is measured.
    Limits per track: the DiffPairsRouting rule that applies to it (scope and priority,
    per-layer min / preferred / max width and gap, Max Uncoupled Length), a profile-driven
    rule's impedance profile for what the rule leaves out, else the board's only
    differential impedance profile on that layer, else none (limit_source none: measured,
    not judged).

    pairs lists each pair (those with findings first) with per layer its routed and
    coupled length, coupled gap and widths, limits and coupling distance; each leg's
    coupled_in_gap_range and uncoupled length (routed length less the length coupled
    within the rule's gap range), the uncoupled length split into at_pads and at_vias (an
    uncoupled run at a pad or via, inside it and up to 50 mil beyond its edge: its
    breakout) and elsewhere; and its
    layer changes (vias joining its tracks on two layers) with how many via pairs match
    (matched). Findings: gap_below_min / gap_above_max (stretches with location, length,
    gap and excess beyond the limit: a warning when at least min_stretch long, default 20
    mil; info when shorter, a breakout of at most 50 mil starting within 50 mil of a pad or
    via, marginal: at most 0.25 mil beyond the limit, or judged against an impedance
    profile no rule links to the pair), width_out_of_range (tracks and arcs, an arc
    counted once: warning; info against an unlinked profile; also_reported_by
    check_rule_compliance when judged by the rule's own width), gap_varies (info: the 5th to
    95th percentile spread of the in-range gap on a layer), uncoupled_length_high (the
    longer leg over Max Uncoupled Length: warning; without one over a quarter of its routed
    length: info), unmatched_layer_change (a via of one leg with no via of the other
    joining two of the same layers within via_distance, default 2 mm: warning),
    pair_not_routed and partially_routed (info). Altium's batch DRC checks pair widths and
    uncoupled length; gap stretches, gap variation and layer changes are this tool's own
    screen.

    pair names pairs or their nets (comma-separated); net_class a differential-pair or net
    class. layer narrows the findings and layer rows to one signal layer; checks to names
    in checks_run (aliases gap, width, uncoupled, vias, routing). coupling_distance,
    min_stretch and via_distance use units (default: board display unit). Findings are
    paginated (offset from next_offset), and the pairs rows apart (pairs_offset from
    next_pairs_offset; pairs_total says when fewer pairs are listed; an offset past the
    findings leaves room for more pairs), or name the pairs with pair=."""
    index, error = _pcb_index_or_error()
    if error:
        return json.dumps(error)
    to_mm, resolved, units_error = _pcb_units_or_error(index, units)
    if units_error:
        return json.dumps(units_error)
    limits = {}
    for name, value, zero_ok in (("coupling_distance", coupling_distance, False),
                                 ("min_stretch", min_stretch, True),
                                 ("via_distance", via_distance, False)):
        if value is None:
            continue
        if not _finite_nonnegative(value) or (value == 0 and not zero_ok) or \
                _input_to_mil(value, resolved) > 10000.0:
            return json.dumps({
                "error": f"invalid_{name}",
                "message": f"{name} must be a {'non-negative' if zero_ok else 'positive'} "
                           f"length in {resolved} (at most 10000 mil).",
            })
        limits[name] = _input_to_mil(float(value), resolved)
    prepared = prepare_diff_pair_geometry_cached(
        index, pairs=_split_names(pair), net_classes=_split_names(net_class), layer=layer,
        checks=checks, coupling_distance_mil=limits.get("coupling_distance"),
        min_stretch_mil=limits.get("min_stretch"), via_distance_mil=limits.get("via_distance"),
        units=resolved)
    if "error" in prepared:
        return json.dumps(prepared)
    project, _, _ = _require_project(netlist=False)
    paths = project.get("pcb_doc_paths") or []
    context: dict = {"units": resolved}
    if paths:
        context["pcb_doc"] = Path(paths[0]).name
        if len(paths) > 1:
            context["note"] = f"Project lists {len(paths)} PcbDocs; checked '{Path(paths[0]).name}'."
    for limit, stretches, pairs_listed in DIFF_PAIR_GEOMETRY_STEPS:
        result = _convert_units({**context, **render_diff_pair_geometry(
            prepared, offset=offset, limit=limit, stretches=stretches, pairs=pairs_listed,
            pairs_offset=pairs_offset)}, to_mm)
        if limit != DIFF_PAIR_GEOMETRY_STEPS[0][0]:
            result["page_limited_to"] = limit
        rendered = json.dumps(result, indent=2)
        if len(rendered) <= QUERY_NET_MAX_RESULT_SIZE_CHARS:
            break
    return rendered


# ---------- mechanical fit and handoff ----------

MECH_REGIONS_SCHEMA = {
    "anyOf": [
        {"type": "array", "items": {"type": "object", "properties": {
            "name": {"type": "string"},
            "polygon": {"type": "array", "items": {"type": "array", "items": {"type": "number"}}},
            "rect": {"type": "array", "items": {"type": "number"}},
            "side": {"type": "string", "enum": ["top", "bottom", "both"]},
            "max_height": {"type": "number"},
            "keepout": {"type": "boolean"},
        }}},
        {"type": "null"},
    ],
}
# (findings, part rows, connectors, regions, holes) per page, tried in turn until it fits.
MECHANICAL_FIT_STEPS = ((100, 100, 60, 40, 40), (60, 50, 30, 20, 20), (40, 25, 20, 10, 10),
                        (20, 10, 10, 5, 5), (10, 5, 5, 3, 3), (5, 2, 3, 2, 2), (2, 1, 1, 1, 1),
                        (1, 0, 0, 0, 0))
# (part rows, connectors, regions, holes, polygons) of a handoff page.
MECHANICAL_HANDOFF_STEPS = ((100, 60, 40, 40, True), (50, 30, 20, 20, True),
                            (25, 20, 10, 10, True), (10, 10, 5, 5, False),
                            (5, 5, 3, 3, False), (0, 0, 0, 0, False))
MECH_MAX_LENGTH_MIL = 1000.0 / 0.0254  # 1 m
_MECH_LIBRARY_PARTS = 300  # parts without any height looked up in the libraries at most


def _mechanical_lengths(resolved: str, **values) -> tuple[dict, dict | None]:
    """{name: mil} of the lengths given (non-negative, at most 1 m), or an error."""
    lengths = {}
    for name, value in values.items():
        if value is None:
            continue
        if not isinstance(value, (int, float)) or isinstance(value, bool) or \
                not _finite_nonnegative(float(value)) or \
                _input_to_mil(float(value), resolved) > MECH_MAX_LENGTH_MIL:
            return {}, {"error": f"invalid_{name}",
                        "message": f"{name} must be a length of 0 or more in {resolved} "
                                   "(at most 1 m)."}
        lengths[name] = _input_to_mil(float(value), resolved)
    return lengths, None


def _mechanical_inputs(project: dict, netlist: dict | None, variant_state, index,
                       notes: bool = True) -> dict:
    """What the mechanical services read beyond the PcbDoc: the variant's Not Fitted
    test, the netlist's part texts (connector kinds), the library footprints' bodies of
    parts with no height on the board, and the Draftsman documents' note lines."""
    is_dnp = variant_state.is_dnp if variant_state is not None else None
    texts: dict[int, str] = {}
    components = (netlist or {}).get("components") or {}
    if components:
        for row in index.db.execute("SELECT id, sch_refdes FROM components"):
            meta = components.get(row["sch_refdes"]) if row["sch_refdes"] else None
            if not meta:
                continue
            board = index.pcb.components[row["id"]]
            texts[row["id"]] = " ".join(str(part) for part in (
                meta.get("description"), meta.get("value"), meta.get("lib_reference"),
                meta.get("mpn"), board.comment, board.footprint) if part)
    library_heights = index.__dict__.get("_mechanical_library_heights")
    if library_heights is None:
        library_heights = {}
        bodies = {body.component for body in index.pcb.bodies or ()
                  if body.component is not None and (body.overall_height_mil or 0.0) > 0}
        wanted = [component for component in index.pcb.components
                  if component.index not in bodies and not (component.height_mil or 0.0) > 0
                  and component.kind not in (2, 3, 4) and component.footprint]
        if wanted and project.get("prj_pcb_path"):
            try:
                libraries = project_libraries(project["prj_pcb_path"],
                                              installed=_installed_libraries())
                library_heights = footprint_body_heights(wanted[:_MECH_LIBRARY_PARTS], libraries)
            except Exception as error:  # the libraries are an extra source, never a failure
                logging.warning("library bodies not read for the mechanical check: %s", error)
        index.__dict__["_mechanical_library_heights"] = library_heights
    documents = []
    for path in (project.get("pcb_dwf_paths") or []) if notes else []:
        try:
            if Path(path).is_file():
                documents.append((Path(path).name, note_lines(_design_notes.draftsman(path))))
        except Exception as error:  # an unreadable drawing gives no limit
            logging.warning("Draftsman document not read for the mechanical check: %s", error)
    return {"is_dnp": is_dnp, "texts": texts, "library_heights": library_heights,
            "note_documents": documents}


def _mechanical_context(project: dict, resolved: str) -> dict:
    paths = project.get("pcb_doc_paths") or []
    context: dict = {"units": resolved}
    if paths:
        context["pcb_doc"] = Path(paths[0]).name
        if len(paths) > 1:
            context["note"] = f"Project lists {len(paths)} PcbDocs; checked '{Path(paths[0]).name}'."
    return context


@mcp.tool(
    title="Check Mechanical Fit",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def check_mechanical_fit(
    height_limit_top: float | None = None,
    height_limit_bottom: float | None = None,
    regions: Annotated[Any, WithJsonSchema(MECH_REGIONS_SCHEMA)] = None,
    probe_side: Literal["top", "bottom", "both"] | None = None,
    tall_part_height: float | None = None,
    screw_head_diameter: float | None = None,
    washer_diameter: float | None = None,
    clearance_margin: float | None = None,
    checks: list[str] | None = None,
    side: Literal["top", "bottom"] | None = None,
    units: PcbUnits | None = None,
    offset: int = 0,
    parts_offset: int = 0,
) -> str:
    """Mechanical fit of the saved PcbDoc, offline: "will it fit the enclosure?", "what is
    the tallest part on each side?", "is anything over 3 mm under the lid?", "which parts
    have no 3D model?", "is there room for the screw heads at the mounting holes?", "are
    tall parts on the ICT probe side?", "how far do the connectors stick out?".

    Heights: each part's tallest 3D body on its side (overall height, standoff, model),
    else the footprint's Height, else its library footprint's body (height_source body,
    footprint_height, library_body, none); parts with no body are missing_3d_body (info;
    a warning when nothing gives a height and a limit applies; card-edge fingers and
    fiducials need none). Not Fitted parts of the active variant are listed apart (their
    mounting holes stay, fitted false). Limits, the tightest applying: height_limit_top /
    height_limit_bottom; regions ([{name, polygon: [[x, y], ...] or rect: [x1, y1, x2,
    y2], side: top|bottom|both, max_height, keepout}] in units relative to the board
    origin; max_height 0 or keepout true: no parts); free 3D bodies (an enclosure part
    placed on the board: its standoff is the limit); Altium's Height rules (the rule that
    applies to each part, as Altium decides it); a maximum component height stated in the
    project's Draftsman notes (only when they agree; quoted; a note for one place is quoted
    in limits.draftsman_local_notes, not applied). Findings: height_over_limit and
    height_under_min (warning, with the limit's source and the margin), part_in_keepout
    (a body or courtyard over a no-parts region, warning); summary.parts_with_height_limit
    counts the parts a real limit applies to (0: nothing says the board fits).

    Mounting holes (free pads and bare holes of 1.5 mm or more, mounting-hole parts):
    drill, plated, net, pad, the metric screw that fits, and the free radius per face to
    the nearest copper of another net, other part's body or the board edge (limited_by;
    exposed_free_radius to parts and pads or untented vias only; own_net_copper apart;
    pad-less parts covering the hole are its hardware). mounting_hole_clearance: a part or
    exposed copper inside half the larger of the head and washer plus clearance_margin
    (defaults: the screw's ISO 7045 pan head and ISO 7089 washer, margin 0.5 mm: info;
    screw_head_diameter / washer_diameter given: warning, without a washer when only the
    head is given); copper under the solder mask only is one grouped info finding.
    hole_near_edge: the wall closer to the edge than the board thickness, at most 1 mm
    (info; half of it: warning); a hole in a board cutout is off_board. ICT: the probe
    side from the test points (or probe_side); parts at least tall_part_height (5 mm) tall
    on it are tall_part_on_probe_side (info; warning with probe_side) with the probe
    points within 5 mm. connectors: edge (left/right/top/bottom of the outline, outline,
    cutout),
    edge_offset_mil (negative: overhang), body and pad distances to it, overhang_mil and
    facing (+X, -Y ...) when at an edge. regions lists rooms, keepouts and the user's with
    their tallest part; summary.sides the tallest parts, height classes and area used.

    checks narrows the findings (aliases heights, keepout, holes, ict); side narrows
    findings, parts and connectors to one side. Lengths in units (default: board display
    unit), locations relative to origin_mil. Findings page with offset (next_offset), part
    rows (tallest first) with parts_offset (next_parts_offset)."""
    index, error = _pcb_index_or_error()
    if error:
        return json.dumps(error)
    to_mm, resolved, units_error = _pcb_units_or_error(index, units)
    if units_error:
        return json.dumps(units_error)
    lengths, error = _mechanical_lengths(
        resolved, height_limit_top=height_limit_top, height_limit_bottom=height_limit_bottom,
        tall_part_height=tall_part_height, screw_head_diameter=screw_head_diameter,
        washer_diameter=washer_diameter, clearance_margin=clearance_margin)
    if error:
        return json.dumps(error)
    project, netlist, variant_state = _require_project(netlist=False)
    inputs = _mechanical_inputs(project, netlist, variant_state, index)
    prepared = prepare_mechanical_fit_cached(
        index, **inputs, height_limit_top_mil=lengths.get("height_limit_top"),
        height_limit_bottom_mil=lengths.get("height_limit_bottom"), regions=regions,
        probe_side=probe_side, tall_part_height_mil=lengths.get("tall_part_height"),
        screw_head_mil=lengths.get("screw_head_diameter"),
        washer_mil=lengths.get("washer_diameter"), margin_mil=lengths.get("clearance_margin"),
        checks=checks, units=resolved)
    if "error" in prepared:
        return json.dumps(prepared)
    context = _mechanical_context(project, resolved)
    for limit, parts_limit, connectors, regions_limit, holes in MECHANICAL_FIT_STEPS:
        result = _convert_units({**context, **render_mechanical_fit(
            prepared, offset=offset, limit=limit, parts_offset=parts_offset,
            parts_limit=parts_limit, connectors_limit=connectors, regions_limit=regions_limit,
            holes_limit=holes, side=side)}, to_mm)
        if limit != MECHANICAL_FIT_STEPS[0][0]:
            result["page_limited_to"] = limit
        rendered = json.dumps(result, indent=2)
        if len(rendered) <= QUERY_NET_MAX_RESULT_SIZE_CHARS:
            break
    return rendered


@mcp.tool(
    title="Get Mechanical Handoff",
    # It writes the drawings only into the folder the user names, never over a file.
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def get_mechanical_handoff(
    sections: list[str] | None = None,
    output_dir: str | None = None,
    formats: list[Literal["dxf", "svg"]] | None = None,
    units: PcbUnits | None = None,
    parts_offset: int = 0,
) -> str:
    """The board for the mechanical engineer, offline from the saved PcbDoc: "export the
    outline for mechanical", "where are the mounting holes and connectors?", "give me a
    DXF of the board outline", "what is the board thickness?". Sections (all by default,
    or sections=[...]): board (thickness from the layer stack, a rigid-flex board's
    rigid one; size, extent, area, copper layers), outline (polygon_mil and cutouts with
    arcs as chords), mounting_holes (as check_mechanical_fit: drill, plated, net, screw,
    free radius, edge distance), connectors (position, rotation, side, edge, edge offset,
    overhang, facing, kind), regions (Keep-Out layer and copper keepout areas, rooms, free
    3D bodies, named mechanical-layer areas, with polygons), rigid_flex (layer stacks and
    board regions with flex, thickness, bending lines and polygons), sides (per side:
    parts, tallest, height
    classes, area used and its share of the board) and parts (every fitted part's height,
    source, standoff, extent; tallest first per side; page with parts_offset).

    output_dir (an existing folder the user names; nothing is written without it)
    receives <PcbDoc>-mechanical.dxf (AutoCAD R12 ASCII, mm, layers OUTLINE, CUTOUTS,
    HOLES, CONNECTORS, KEEPOUTS, ROOMS, BOARD_REGIONS, PARTS_TOP, PARTS_BOTTOM, LABELS) and
    .svg (mm), or the formats asked; an existing file of that name is kept and the new
    one numbered (-mechanical-2.dxf).
    Coordinates are relative to the board origin (origin_mil), viewed from the top. Plan
    view only: not a STEP or IDF / IDX replacement."""
    index, error = _pcb_index_or_error()
    if error:
        return json.dumps(error)
    to_mm, resolved, units_error = _pcb_units_or_error(index, units)
    if units_error:
        return json.dumps(units_error)
    chosen, error = parse_mechanical_sections(sections)
    if error:
        return json.dumps(error)
    if output_dir is not None and not Path(output_dir).is_dir():
        return json.dumps({"error": "output_dir_not_found",
                           "message": f"output_dir '{output_dir}' is not an existing folder."})
    project, netlist, variant_state = _require_project(netlist=False)
    inputs = _mechanical_inputs(project, netlist, variant_state, index, notes=False)
    handoff = prepare_handoff(index, is_dnp=inputs["is_dnp"], texts=inputs["texts"],
                              library_heights=inputs["library_heights"])
    context = _mechanical_context(project, resolved)
    if output_dir is not None:
        paths = project.get("pcb_doc_paths") or []
        stem = Path(paths[0]).stem if paths else project.get("name") or "board"
        try:
            context["drawings"] = write_drawings(handoff, output_dir, stem,
                                                 tuple(dict.fromkeys(formats or ("dxf", "svg"))))
        except OSError as error:
            return json.dumps({"error": "write_failed",
                               "message": f"The drawings could not be written: {error}"})
        context["drawings_units"] = "mm, relative to the board origin"
    for parts_limit, connectors, regions_limit, holes, polygons in MECHANICAL_HANDOFF_STEPS:
        result = _convert_units({**context, **render_handoff(
            handoff, chosen, parts_offset=parts_offset, parts_limit=parts_limit,
            connectors_limit=connectors, regions_limit=regions_limit, holes_limit=holes,
            polygons=polygons)}, to_mm)
        if parts_limit != MECHANICAL_HANDOFF_STEPS[0][0]:
            result["page_limited_to"] = parts_limit
        if not polygons:
            result["polygons_omitted"] = ("too large for one answer: ask for the outline or "
                                          "regions section alone, or write the DXF/SVG")
        rendered = json.dumps(result, indent=2)
        if len(rendered) <= QUERY_NET_MAX_RESULT_SIZE_CHARS:
            break
    return rendered


@mcp.tool(
    title="Check Pull Resistors",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def check_pull_resistors(
    net: str | None = None,
    checks: list[str] | None = None,
    i2c_mode: Literal["standard", "fast", "fast_plus"] | None = None,
    bus_capacitance_pf: float | None = None,
    offset: int = 0,
) -> str:
    """Audit pull-ups, pull-downs and floating nets offline, and size I2C pull-ups. Call it
    when the user asks about pull-ups or pull-downs, floating pins or nets, reset, enable,
    boot or strap pins, open-drain or I2C lines, whether I2C pull-ups are the right value,
    or for a schematic review. It finds signals that need a defined state (reset, enable,
    shutdown, boot, strap and mode nets; open-drain nets: OpenCollector pins, I2C, INT,
    ALERT, PGOOD, FAULT; MOSFET gates and transistor bases driven from an IC; comparator
    outputs) and the resistors from each to a rail or ground fitted in the active variant;
    nets joined by a series resistor under 1 kohm count as one signal. Findings: no pull
    on an OpenCollector pin, an I2C line or a MOSFET gate driven from an IC (warnings, info
    when the signal leaves the board through a connector), on reset, enable or other named
    nets (info), pulls that are all Not Fitted (warning, info when the signal leaves the
    board), both up and down or several pull-ups in parallel (info); connector_no_pull
    (connector pins straight to IC inputs) only when named in checks. I2C buses pair SDA
    and SCL by name: effective pull-up, Rmin = (VDD - 0.4 V) / IOL (3 mA, 20 mA
    fast-plus) with VDD from the rail's name, Rmax = tr / (0.8473 x Cb) (tr 1000/300/120
    ns) and a verdict ok, too_low, too_high (also when the rail's voltage is unknown) or
    unknown. i2c_mode is standard, fast (default) or fast_plus; bus_capacitance_pf
    replaces the estimate (10 pF per pin and capacitors to ground, plus about 0.12 pF/mm
    of routing when the PcbDoc loads).

    It is a heuristic, not ERC: signals come from net and pin names and the library's pin
    electrical types, so nets named otherwise are missed; pull-ups inside ICs are
    invisible (a missing pull is a question for the datasheet); pins left unconnected and
    unused op-amp sections are not checked. net takes comma-separated names; checks
    narrows to the names in summary. Findings are paginated (offset from next_offset),
    warnings first."""
    try:
        project, netlist, variant_state = _require_project()
    except ProjectNotReady as error:
        return json.dumps(error.payload)
    no_components = _no_components_error(netlist)
    if no_components:
        return json.dumps(no_components)
    if bus_capacitance_pf is not None and not (
        _finite_nonnegative(bus_capacitance_pf) and bus_capacitance_pf > 0
    ):
        return json.dumps({"error": "invalid_bus_capacitance",
                           "message": "bus_capacitance_pf must be a positive number."})

    def index_loader():
        if not project.get("pcb_doc_paths"):
            return None, "the project lists no PcbDoc"
        index, error = _pcb_session.get(project, netlist)
        if error:
            return None, f"{error['error']}: {error.get('message', '')}".strip(": ")
        return index, None

    plan, _, _ = _bom_plan(netlist, variant_state, None)
    for limit, net_limit, bus_limit in ((100, 60, 20), (40, 20, 8), (15, 5, 3)):
        result = run_pull_resistor_check(
            netlist, plan, project.get("parameters") or {}, checks=checks,
            nets=_split_names(net), i2c_mode=i2c_mode, bus_capacitance_pf=bus_capacitance_pf,
            index_loader=index_loader, offset=offset, limit=limit, net_limit=net_limit,
            bus_limit=bus_limit,
        )
        if "error" in result:
            return json.dumps(result)
        result = {"netlist_updated_utc": _netlist_last_updated, **result}
        if limit < 100:
            result["findings_limited_to"] = limit
        rendered = json.dumps(result, indent=2)
        if len(rendered) <= QUERY_NET_MAX_RESULT_SIZE_CHARS:
            break
    return rendered


@mcp.tool(
    title="Check Interfaces",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def check_interfaces(
    connector: str | None = None,
    checks: list[str] | None = None,
    external: str | None = None,
    internal: str | None = None,
    max_distance: float | None = None,
    units: PcbUnits | None = None,
    offset: int = 0,
) -> str:
    """Audit connector and interface protection offline: ESD/TVS, series parts, shields
    and ground pins per connector. Call it when the user asks whether connectors or I/O
    are protected, about ESD, TVS diodes, surge, shield or chassis connections, EMC of
    cables and connectors, or for a schematic or layout review. Each connector fitted in
    the active variant is judged external (USB, RJ45, D-Sub, terminal block, jack, coax,
    SFP port ...), internal (board-to-board, FPC, debug or test header, card edge,
    configuration jumper) or unknown, with the basis; external= and internal=
    (comma-separated designators) correct that guess. Each signal pin is followed through
    series resistors, 0 ohm links, ferrites, inductors, common-mode chokes and series
    capacitors to the first IC, transistor, transformer or other connector, recording the
    ESD/TVS devices on the way (connector side or behind a series element), series
    elements, capacitors to ground and pulls; LED and magnetics centre-tap pins are not
    counted as lines. Findings: no_esd (warning on external connectors; info when unknown,
    behind integrated magnetics, on RF coax or an SFP/QSFP port, or when the parts reached
    claim ESD-protected pins in their text), shield_floating (warning on external),
    ground_indirect (a ground pin reaching board ground only through a capacitor,
    resistor or ferrite: warning on external with no DC path, else info),
    esd_behind_series, shield_rc (a shield reaching ground only through an RC network,
    quoting the guidance, not a fault), sparse_ground (few ground pins per signal pin) and
    no_series_element (low-speed external lines with no series resistor, 0 ohm or ferrite
    footprint), all info; with a PcbDoc, tvs_far_from_connector when a TVS pad more than
    2 mm from the connector pin is nearer the IC (warning beyond 5 mm on external
    connectors) or farther than max_distance (default 10 mm); straight line in plan view,
    and opposite_side marks a TVS on the other side of the board. Internal connectors get
    findings only when named in connector=.

    It is a heuristic, not ERC: parts are recognised by designator prefix and the words
    in their text and part numbers (a TVS known only by an unlisted part number is
    missed), and choosing the protection part (standoff and clamping voltage,
    capacitance, direction) needs its datasheet. connector takes comma-separated
    designators and adds their per-pin tables (pin_tables; cut to their signal pins when
    they do not fit, as pin_tables_truncated says; the tables of all connectors are also
    given when they fit); checks narrows to the names in summary. Findings are paginated
    (offset from next_offset), warnings first. Distances, in the fields and the text, are
    in units, default mm."""
    try:
        project, netlist, variant_state = _require_project()
    except ProjectNotReady as error:
        return json.dumps(error.payload)
    no_components = _no_components_error(netlist)
    if no_components:
        return json.dumps(no_components)
    if units not in (None, "mm", "mil"):
        return json.dumps({"error": "invalid_units", "message": "units must be 'mil' or 'mm'."})
    resolved = units or "mm"
    to_mm = resolved == "mm"
    max_mil = DEFAULT_TVS_MAX_DISTANCE_MIL
    if max_distance is not None:
        if not _finite_nonnegative(max_distance) or max_distance == 0:
            return json.dumps({"error": "invalid_max_distance",
                               "message": "max_distance must be a positive number."})
        max_mil = _input_to_mil(max_distance, resolved)
    index, pcb_unavailable = None, None
    if project.get("pcb_doc_paths"):
        index, error = _pcb_session.get(project, netlist)
        if error:
            pcb_unavailable = f"{error['error']}: {error.get('message', '')}".strip(": ")
    else:
        pcb_unavailable = "the project lists no PcbDoc"
    plan, _, _ = _bom_plan(netlist, variant_state, None)
    named = _split_names(connector)
    # (findings, connector rows, pin tables, rows per table): named connectors keep their
    # tables, cut to their signal pins last; otherwise the tables go first.
    steps = ((100, 60, True, None), (40, 20, True, None), (15, 8, True, 200),
             (15, 8, True, 60), (15, 8, True, 20)) if named else (
        (100, 60, True, None), (100, 60, ("external",), None), (100, 60, False, None),
        (40, 20, False, None), (15, 8, False, None))
    for limit, connector_limit, pin_tables, table_rows in steps:
        result = run_interface_check(
            netlist, plan, project.get("parameters") or {}, index=index,
            pcb_unavailable=pcb_unavailable, checks=checks, connectors=named,
            external=_split_names(external), internal=_split_names(internal),
            max_distance_mil=max_mil, offset=offset, limit=limit,
            connector_limit=connector_limit, pin_tables=pin_tables, table_rows=table_rows,
            units=resolved,
        )
        if "error" in result:
            return json.dumps(result)
        context: dict = {"netlist_updated_utc": _netlist_last_updated}
        if index is not None:
            context.update({"pcb_doc": Path(project["pcb_doc_paths"][0]).name,
                            "units": resolved})
            if len(project["pcb_doc_paths"]) > 1:
                context["note"] = (
                    f"Project lists {len(project['pcb_doc_paths'])} PcbDocs; measured on "
                    f"'{Path(project['pcb_doc_paths'][0]).name}'."
                )
        result = _convert_units({**context, **result}, to_mm)
        if pin_tables is not True:
            result["pin_tables_omitted"] = (
                "The per-pin tables did not fit"
                + (" (external connectors' are given)" if result.get("pin_tables") else "")
                + ": name connectors in connector= to get theirs."
            )
        elif table_rows is not None:
            result["pin_tables_omitted"] = (
                "The named connectors' full pin tables did not fit: pin_tables_truncated "
                "says what each one gives; name fewer connectors for more."
            )
        if limit < 100:
            result["findings_limited_to"] = limit
        rendered = json.dumps(result, indent=2)
        if len(rendered) <= QUERY_NET_MAX_RESULT_SIZE_CHARS:
            break
    return rendered


# Rows per page (converters, findings, rails, sources and power inputs page together),
# tried in turn until the result fits; for the Mermaid flowchart, (load nodes, lines) the
# same way. The tree is built once and only rendered per step.
POWER_TREE_STEPS = (100, 40, 15, 5, 2, 1)
POWER_TREE_MERMAID_STEPS = ((True, None), (False, None), (False, 600), (False, 300),
                            (False, 120))
_VREF_ITEM = re.compile(r"\s*([A-Za-z_][\w.-]*)\s*[=:]\s*([0-9]*\.?[0-9]+)\s*[vV]?\s*")
_VREF_ALL = re.compile(r"\s*([0-9]*\.?[0-9]+)\s*[vV]?\s*")


def _parse_vref(text: str | None) -> tuple[dict[str, float] | float | None, dict | None]:
    """'U5=0.8,U7=0.6' -> {'U5': 0.8, 'U7': 0.6}; '0.8' or '0.8V' -> 0.8; None when
    empty. The error when it reads as neither or a value is not 0 < V < 10."""
    error = {"error": "invalid_vref",
             "message": "vref takes a feedback reference in volts per converter, e.g. "
                        "'U5=0.8,U7=0.6', or one number for all, e.g. '0.6'."}
    if text is None or not text.strip():
        return None, None
    if match := _VREF_ALL.fullmatch(text):
        value = float(match.group(1))
        return (value, None) if 0 < value < 10 else (None, error)
    values: dict[str, float] = {}
    for item in text.split(","):
        match = _VREF_ITEM.fullmatch(item)
        if not match or not 0 < float(match.group(2)) < 10:
            return None, error
        values[match.group(1)] = float(match.group(2))
    return values, None


@mcp.tool(
    title="Get Power Tree",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def get_power_tree(
    rail: str | None = None,
    part: str | None = None,
    checks: list[str] | None = None,
    vref: str | None = None,
    format: Literal["json", "mermaid"] = "json",
    offset: int = 0,
) -> str:
    """Power tree and power-path checks, offline: sources, protection, converters, rails
    and loads. Call it when the user asks how the board is powered, for the power tree or
    a power diagram, which regulator makes a rail, regulator enable or power-good wiring,
    power sequencing, feedback dividers and output voltages, input protection (fuse,
    reverse polarity, TVS), input filters on switchers, or for the power part of a
    schematic review.

    Converters fitted in the active variant are recognised by their words and part
    number and their pin names (VIN, VOUT, SW, FB, EN, PG ...): LDOs and linear
    regulators, buck, boost, buck-boost, SEPIC, flyback and inverting switchers and
    controllers (output traced through the inductor on SW, past the diode of a boost,
    through external MOSFETs), charge pumps, PMICs (an edge per output), DC/DC and power
    modules (also on a connector's designator), load switches, eFuses, hot-swap and
    ideal-diode controllers and chargers; references and supervisors are listed, not
    taken for converters, and MOSFET drivers are left out. Rails are the nets
    check_decoupling reads as rails, the converters' inputs and outputs, connector and
    battery power pins and unnamed nets that only carry power between them; edges are
    converters and fitted series parts between rails (0 ohm links and shunts, resistors
    up to 100 ohm or into VREF inputs, ferrites and inductors, fuses, diodes, MOSFETs or
    transistors, switches, jumpers), oriented from the sources (connector power pins,
    batteries, converter and reference outputs, ICs' own regulator pins). Loads are parts
    with a supply pin on a rail, counted per rail. Per converter: EN (tied to its input
    or another rail, pulled up or down, an undervoltage divider with its ratio, driven by
    a GPIO, another converter's PG or a supervisor, or floating; held_off when held at
    the level that keeps the part off), PG (pulled up to which rail, read by which EN,
    reset or GPIO, or unused), input and output capacitors, the feedback divider (Rtop,
    Rbot, Vout/Vref, and the Vref the rail name implies, or with vref the output it sets;
    a leg a jumper switches in is given too) and soft-start, current-limit and mode pins.
    Per power input (a connector power pin not fed from the board, or a battery; one row
    per net): fuse, reverse-polarity element (forward diode, P-FET with its drain or N-FET
    with its source toward the input, ideal diode, crowbar), TVS or clamp, bulk
    capacitance and inrush parts on its way in; the input findings cover external
    connectors and power-only ones of unknown exposure. Findings: rail_without_source
    (warning; info when the rail reaches a connector, one part only, or one part only
    through a resistor above 100 ohm), enable_floating (warning), enable_held_off
    (warning; info for one channel of a multi-output part), fb_divider_mismatch (warning
    when a given vref sets an output more than 5% from the rail name's voltage, else info
    with the implied Vref), no_input_fuse, no_reverse_protection, no_input_tvs,
    smps_input_unfiltered (no LC or pi filter on a switcher's input),
    no_output_capacitor, no_input_capacitor, converter_unknown_rail, pgood_unused and
    sequencing (EN/PG chains), all info; enable_tied_note (EN tied to its own input, no
    UVLO divider) only when named in checks.

    It is a heuristic draft read from names, not a simulation: internal pull-ups,
    fixed-output versions, Vref and thresholds are datasheet facts, so check each
    converter against its datasheet and say so. vref takes volts per converter
    ('U5=0.8,U7=0.6') or one number for all; rail and part (comma-separated) narrow the
    tree and findings to those rails or parts and their neighbours; checks narrows to
    the names in summary. format='mermaid' returns a flowchart (sources -> protection ->
    converters -> rails -> load counts) to render or redraw. Converters, findings
    (warnings first), rails (those with loads and no source first), sources and power
    inputs page together (offset from next_offset)."""
    try:
        project, netlist, variant_state = _require_project()
    except ProjectNotReady as error:
        return json.dumps(error.payload)
    no_components = _no_components_error(netlist)
    if no_components:
        return json.dumps(no_components)
    if format not in ("json", "mermaid"):
        return json.dumps({"error": "invalid_format",
                           "message": "format must be 'json' or 'mermaid'."})
    vrefs, error = _parse_vref(vref)
    if error:
        return json.dumps(error)
    plan, _, _ = _bom_plan(netlist, variant_state, None)
    prepared = prepare_power_tree(netlist, plan, project.get("parameters") or {},
                                  checks=checks, rails=_split_names(rail),
                                  parts=_split_names(part), vref=vrefs)
    if "error" in prepared:
        return json.dumps(prepared)
    steps = POWER_TREE_MERMAID_STEPS if format == "mermaid" else POWER_TREE_STEPS
    for step in steps:
        if format == "mermaid":
            loads, lines = step
            result = render_power_tree(prepared, format, mermaid_loads=loads,
                                       mermaid_lines=lines)
        else:
            result = render_power_tree(prepared, format, offset=offset, limit=step)
        result = {"netlist_updated_utc": _netlist_last_updated, **result}
        if step is not steps[0]:
            result["page_limited_to" if format == "json" else "mermaid_shrunk"] = (
                step if format == "json" else
                "load nodes left out" + (f", cut to {step[1]} lines" if step[1] else ""))
        rendered = json.dumps(result, indent=2)
        if len(rendered) <= QUERY_NET_MAX_RESULT_SIZE_CHARS:
            break
    return rendered


# Findings (and named parts) per page, tried in turn until the result fits; the parts are
# read once and only rendered per step.
DERATING_STEPS = (100, 40, 15, 5, 2, 1)
# check_derating's policy as a client sees it: an object of policy keys, each a number, a
# case code or an object of case codes to numbers. Typed Any so that the service checks
# every value and answers invalid_policy with the valid keys.
DERATING_POLICY_SCHEMA = {
    "anyOf": [
        {"type": "object", "additionalProperties": {"anyOf": [
            {"type": "number"}, {"type": "string"},
            {"type": "object", "additionalProperties": {"type": "number"}},
        ]}},
        {"type": "null"},
    ],
}


@mcp.tool(
    title="Check Derating",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def check_derating(
    part: str | None = None,
    net: str | None = None,
    checks: list[str] | None = None,
    rails: str | None = None,
    vref: str | None = None,
    policy: Annotated[Any, WithJsonSchema(DERATING_POLICY_SCHEMA)] = None,
    offset: int = 0,
) -> str:
    """Component derating check, offline: each capacitor's voltage rating against the
    voltage across it, small ceramics' DC-bias loss, and resistors' working voltage and
    power. Call it when the user asks about voltage ratings, derating, whether capacitors
    are rated high enough, DC bias, resistor power or voltage, LED or shunt resistors, or
    for the component-stress part of a schematic review.

    Net voltages carry their source and confidence: rails (the user's, e.g.
    'VIN=24,VBUS=5', or 'VIN,VIN_F=24' for names sharing one value; they win), grounds
    (0 V), a voltage in the net name (3V3, +5V, VDD_1V8, 1P8V, -12V, VNEG_5V, HV_400V;
    VDD33-style names at medium confidence, VDD12/VCC15 left unknown; not EN, PG,
    SENSE or AC nets, nor a converter's bootstrap, soft-start, compensation, feedback or
    gate net such as BST_5V), then the power tree (an output a given vref sets through its feedback
    divider; rails joined to a known one through 0 ohm links, shunts, ferrites, fuses or
    inductors; a buck's switch node peaks at its input, low confidence), then the
    unloaded midpoint of a two-resistor divider between known voltages. Switching
    converters' inputs, outputs and switch nodes are ripple nets (every converter role
    named). Each fitted part's rating, dielectric, technology, capacitance, case size
    and power rating come from its parameters, then its value, comment and description,
    then its part number (Murata, TDK, Samsung, KEMET, Yageo, AVX, Taiyo Yuden, Walsin
    ceramics; KEMET, AVX and POSCAP tantalums; Yageo, Vishay, Panasonic and Stackpole
    resistors), then its footprint for the size; each field names its source, and
    sources that disagree are listed (the value most kinds of source agree on is used; on
    a tie a parameter, then the part number, then text; a lower rating that ties is
    judged too and its warnings stay warnings). A resistor array's elements pair pins 1
    and N, 2 and N-1, at the lowest stated power (per element).

    Findings: over_rated (warning), ceramic_voltage (above 75% of rated, or 50% on a
    ripple net: warning; C0G too), tantalum_voltage (above 80%, MnO2 and polymer:
    warning), electrolytic_voltage (above 80%: warning), capacitor_voltage (technology
    not read: warning above the most lenient limit, info above the strictest), dc_bias
    (a Class II ceramic in 0603 or smaller with a high value for its size at 30% of its
    rating or more: info with an approximate loss band; the datasheet's DC-bias curve
    decides),
    resistor_voltage (above the typical working voltage of its size, 50 V for 0402:
    warning; info when only one end's voltage is known), resistor_power (dV^2/R against
    the stated or typical rating: warning above it, info above half of it; a 0 ohm link
    between two voltages: warning), needs_current (LED series resistors, shunts, series
    supply resistors and resistors from a rail to a net of unknown voltage whose power
    cannot be bounded: info saying what to get from the datasheet; ask the user for the
    current) and unknown_rating (one info row listing the capacitors with a known voltage
    but no rating read). Thresholds come from a policy: the defaults, then
    derating_policy.json in the project folder, then policy (e.g.
    {"ceramic_max_ratio": 0.6}); policy in the result says which applied.

    It is a heuristic, not a simulation: voltages are nominal (no tolerance, transients
    or ripple peaks), ratings are read from text and can be wrong, and resistor limits are
    typical for the size: confirm each finding in the datasheet and say so.
    rails_without_voltage lists rails whose voltage is not known, most capacitors first:
    ask the user and pass rails. part and net (comma-separated) narrow the findings and
    list those parts with every parsed field, its source and the voltage across; checks
    narrows to the names in summary; vref ('U5=0.8') goes to the power tree. Findings
    (warnings first) and the listed parts page together (offset from next_offset)."""
    try:
        project, netlist, variant_state = _require_project()
    except ProjectNotReady as error:
        return json.dumps(error.payload)
    no_components = _no_components_error(netlist)
    if no_components:
        return json.dumps(no_components)
    vrefs, error = _parse_vref(vref)
    if error:
        return json.dumps(error)
    user_rails, error = parse_rail_map(rails)
    if error:
        return json.dumps(error)
    policy_info, error = load_derating_policy(project.get("root_dir"), policy)
    if error:
        return json.dumps(error)
    plan, _, _ = _bom_plan(netlist, variant_state, None)
    prepared = prepare_derating(netlist, plan, project.get("parameters") or {},
                                checks=checks, parts=_split_names(part),
                                nets=_split_names(net), rails=user_rails, vref=vrefs,
                                policy=policy_info)
    if "error" in prepared:
        return json.dumps(prepared)
    for step in DERATING_STEPS:
        result = {"netlist_updated_utc": _netlist_last_updated,
                  **render_derating(prepared, offset=offset, limit=step)}
        if step != DERATING_STEPS[0]:
            result["page_limited_to"] = step
        rendered = json.dumps(result, indent=2)
        if len(rendered) <= QUERY_NET_MAX_RESULT_SIZE_CHARS:
            break
    return rendered


# Findings (and named parts or signals) per page, tried in turn until the result fits; the
# design is read once and only rendered per step.
LOGIC_LEVEL_STEPS = (100, 40, 15, 5, 2, 1)


@mcp.tool(
    title="Check Logic Levels",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def check_logic_levels(
    part: str | None = None,
    net: str | None = None,
    checks: list[str] | None = None,
    rails: str | None = None,
    vref: str | None = None,
    offset: int = 0,
) -> str:
    """Driver/receiver logic-level compatibility candidates, offline: for every signal
    that links ICs powered from different rails, each pin's I/O supply and the pairs whose
    levels likely do not meet. Call it when the user asks whether logic levels match,
    about 1.8 V, 3.3 V and 5 V parts talking to each other, VOH/VIH, 5 V tolerant inputs,
    FPGA bank voltages (VCCO, VCCIO), level translators or isolators, I2C pull-up rails,
    or for the interface part of a schematic review.

    I/O rail per IC pin, with basis and confidence: an FPGA's (or any part's) bank supply
    when the pin name gives the bank (VCCO_14 for IO_L1P_T0_14 or DONE_0, VCCIO3G for
    LVDS3G_6p or CLK_3G_1p); a level translator's or isolator's side (A/B pins on
    VCCA/VCCB, 1/2 on VCC1/VCC2 or VDD1/VDD2, VL/VCC; DIR and OE on the A side, else the
    half of the pins its side's supply sits in); a clock part's outputs on VDDO; else the
    part's only supply (high), its one I/O supply (VDDIO, VCCIO, VIO, OVDD, VDDQ, VL ...:
    high), its one digital supply among analog or core ones (medium; low beside a lone
    VDDIO2-style port supply) or the highest of its digital supplies (low); power inputs
    (VIN, PVIN, VDDH) only when nothing else is named, and a bank pin never takes its
    part's core supply. Voltages come from rails (the user's, 'VCCO=3.3,VIO=1.8';
    they win), net names, the power tree (vref, e.g. 'U5=0.8') and divider midpoints.
    Signals are nets joined through series resistors under 1 kohm and 0 ohm links; a
    series resistor of 1 kohm or more is crossed once (a pull-down behind it divides the
    level). Pin direction: the electrical type when not Passive, else the name for the
    part's class (a processor's TX, MOSI, SCK, CS drive; a SPI peripheral's SDO drives;
    a transceiver's TXD receives; I2C lines are open-drain; peripherals' INT and ALERT
    drive, their RESET, EN and CS receive); a GPIO named only by its port takes the
    direction its peers leave. Open-drain lines (OpenCollector pins, I2C or interrupt
    names with a pull-up) take the pull-up's rail as their high level, which every pin
    on the line sees.

    Candidates: driver_low_for_receiver (a push-pull driver's rail below 70% of a
    receiver's, the usual CMOS VIH: info, warning at or below 50%, e.g. 1.8 V into 5 V);
    receiver_overvoltage (a driver's or a pull-up's rail more than 0.3 V above a receiver's,
    the usual VDD + 0.3 V absolute maximum: warning, info when the pin is 5 V tolerant by
    its name (FT, 5VT), its description or its 74LVC/AHC/VHC family, or a series resistor
    of 1 kohm or more limits the current; an output pin under such a pull-up is judged
    too); open_drain_pullup_mismatch (a pull-up rail above a receiver's by more than
    0.3 V: warning, info on an open-drain output; below its VIH: info); unknown_rail (one
    row: the I/O and pull-up rails of unknown voltage on signals between ICs, every rail
    of a pair at once, with the rails behind a 0 ohm link or ferrite under the one they
    follow, to ask the user about and pass in rails; supply pins on nets named like
    signals and parts with no supply pin read are listed apart); translator_ok (level
    translators and isolators, listed as handled when their two sides are read). A pair
    is a warning only when one end's direction is certain (typed or named) and both rails
    are read with medium or high confidence: two bidirectional or unknown pins are judged
    both ways at info. Inputs of regulators, supervisors and gate or motor drivers and of
    parts powered above 5.5 V have fixed thresholds (info only). Left out: analog nets
    (and a sensor's VOUT), power nets, crystal lines (a crystal pin an oscillator or
    clock buffer drives is judged at info), differential pairs (by partner net or pair-
    half pin name; single-ended PERST or GTX_CLK pins are judged), bus transceivers'
    line-side pins (CANH/CANL, A/B) and nets reaching one IC; lines across a series
    resistor of 1 kohm or more are counted apart (series_resistor_links).

    It only produces candidates: VOH, VIH, 5 V tolerance and absolute maximum ratings are
    datasheet facts, so confirm each candidate in both datasheets before calling it a
    fault, and say so. part and net (comma-separated) narrow the findings; part also lists
    each named IC's supplies, banks and sides, and every pin's rail, direction and basis
    (part_pins), net each named signal's pins, pull-ups and series crossings. checks
    narrows to the names in summary. Findings (warnings first), the listed parts, their
    part_pins and the listed signals page together (offset from next_offset)."""
    try:
        project, netlist, variant_state = _require_project()
    except ProjectNotReady as error:
        return json.dumps(error.payload)
    no_components = _no_components_error(netlist)
    if no_components:
        return json.dumps(no_components)
    vrefs, error = _parse_vref(vref)
    if error:
        return json.dumps(error)
    user_rails, error = parse_rail_map(rails)
    if error:
        return json.dumps(error)
    plan, _, _ = _bom_plan(netlist, variant_state, None)
    prepared = prepare_logic_levels(netlist, plan, project.get("parameters") or {},
                                    checks=checks, parts=_split_names(part),
                                    nets=_split_names(net), rails=user_rails, vref=vrefs)
    if "error" in prepared:
        return json.dumps(prepared)
    for step in LOGIC_LEVEL_STEPS:
        result = {"netlist_updated_utc": _netlist_last_updated,
                  **render_logic_levels(prepared, offset=offset, limit=step)}
        if step != LOGIC_LEVEL_STEPS[0]:
            result["page_limited_to"] = step
        rendered = json.dumps(result, indent=2)
        if len(rendered) <= QUERY_NET_MAX_RESULT_SIZE_CHARS:
            break
    return rendered


# Calculations and findings per page, tried in turn until the result fits; the board is
# read once and only rendered per step. Its rows are long (inputs with their sources), so
# the steps are finer than other tools' to use the budget.
CIRCUIT_CALC_STEPS = (100, 60, 40, 25, 15, 10, 5, 2, 1)


@mcp.tool(
    title="Get Circuit Calculations",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def get_circuit_calculations(
    part: str | None = None,
    net: str | None = None,
    kinds: list[str] | None = None,
    rails: str | None = None,
    vref: str | None = None,
    vf: str | None = None,
    vth: str | None = None,
    offset: int = 0,
) -> str:
    """Circuit calculators, offline: resistor dividers, LED and optocoupler currents, ADC
    input filters and RC low-pass filters, each with its inputs (every value with where it
    came from), its formula, its result and a confidence. Call it when the user asks to
    check or recalculate a divider, a feedback or UVLO divider's output or trip voltage,
    an LED's or optocoupler's current or its resistor, whether ADC inputs are filtered, an
    RC filter's cutoff, or for the calculation part of a schematic review.

    dividers: a midpoint with a resistor leg (one resistor, parallel ones or a series
    chain, also through a test point or connector pin) to ground or a lower rail and one
    to a rail, a net of known voltage or a driven signal; each tap of a resistor stack
    (window comparators, EN with a hysteresis tap) is a midpoint of its own. Skipped: a
    midpoint a push-pull output drives (a pull-up/pull-down pair) and a series resistor
    up to 1 kohm from a driving pin into a pull-down (kept and labelled at a transistor's
    gate or base). Ratio Rbot/(Rtop+Rbot), the unloaded midpoint voltage when the top's
    voltage is known, Rtop||Rbot and the current, a min/max ratio when every tolerance is
    read (Tolerance parameter, '1%' in text, part-number letter), legs a jumper or switch
    selects (their ratio), and what it feeds: a converter's FB (Vout/Vref, the Vref the
    top's voltage implies, rails= included, or the output vref sets), EN/UVLO (V_top at
    trip = V_th / ratio for vth, else 1.2 V and 0.8 V as examples only), a supervisor or
    comparator (with vth), a reference IC such as a TL431 (vref per designator), an ADC
    input, an op-amp (non-inverting gain), a reference pin, a transistor, a processor or
    IC pin, a connector. leds: each LED's (and LED+/LED- pins on another part, such as a
    display's backlight) supply side (rail, GPIO with its I/O rail, high-side transistor)
    and sink side (ground, a sinking pin, low-side transistor) and I = (V_supply - V_sink
    - Vf)/R - Vf/R_across (parallel resistors combined; a resistor across the LED takes its
    share, and can hold it off) with Vf from vf, a stated forward voltage, the colour
    (typical: red 2.0 V, yellow/amber 2.1 V, green 2.2 V, true green and blue/white 3.0 V,
    IR 1.3 V), else at 2.0 V and 3.0 V; each resistor's power at that current. optos: the
    input diode's I_F (Vf 1.2 V typical), an R or C across it, the output's pull-up and
    rail, the saturation current and the CTR it needs. adc_inputs: processor analog pins
    and ADC parts' inputs with their series R (or divider impedance), shunt C and fc =
    1/(2 pi R C). rc_filters: other series-R-then-C nodes between ICs, with fc.

    Findings: led_overcurrent (I above the stated or a typical 20 mA rating: warning when
    Vf is known and I > 30 mA or above a rating a parameter states, else info),
    diode_shunted_off (a resistor across an LED or opto input holds it below Vf),
    divider_output_above_input_rating (the midpoint above the reached pin's rail + 0.3 V:
    warning) and divider_output_below_ground (below -0.3 V), resistor_overpower (at the
    computed current: warning above the rating; with an unknown Vf, when even 3.0 V
    overloads it), adc_no_filter and opto_no_input_shunt (info).

    Vf, Vref, EN thresholds, GPIO current limits and ADC sample capacitors are datasheet
    values: present results as calculations to check against the datasheet, not verdicts,
    and say so. voltages_needed lists nets whose voltage the calculations lack: ask the
    user and pass rails ('VIN=24,VBAT=3.7'; names before a value share it). vf takes
    forward voltages per designator or colour ('LED1=2.9,red=1.9'), vth thresholds per
    designator or one for all ('U3=1.2'), vref Vrefs ('U5=0.8'). part and net
    (comma-separated) narrow to the calculations touching them; kinds narrows to
    dividers, leds, optos, adc_inputs, rc_filters. Calculations and findings page together
    (offset from next_offset)."""
    try:
        project, netlist, variant_state = _require_project()
    except ProjectNotReady as error:
        return json.dumps(error.payload)
    no_components = _no_components_error(netlist)
    if no_components:
        return json.dumps(no_components)
    vrefs, error = _parse_vref(vref)
    if error:
        return json.dumps(error)
    user_rails, error = parse_rail_map(rails)
    if error:
        return json.dumps(error)
    forward, error = parse_vf(vf)
    if error:
        return json.dumps(error)
    thresholds, error = parse_vth(vth)
    if error:
        return json.dumps(error)
    plan, _, _ = _bom_plan(netlist, variant_state, None)
    prepared = prepare_circuit_calculations(
        netlist, plan, project.get("parameters") or {}, kinds=kinds,
        parts=_split_names(part), nets=_split_names(net), rails=user_rails, vref=vrefs,
        vf=forward, vth=thresholds, root_dir=project.get("root_dir"))
    if "error" in prepared:
        return json.dumps(prepared)
    for step in CIRCUIT_CALC_STEPS:
        result = {"netlist_updated_utc": _netlist_last_updated,
                  **render_circuit_calculations(prepared, offset=offset, limit=step)}
        if step != CIRCUIT_CALC_STEPS[0]:
            result["page_limited_to"] = step
        rendered = json.dumps(result, indent=2)
        if len(rendered) <= QUERY_NET_MAX_RESULT_SIZE_CHARS:
            break
    return rendered


# Rows (and findings) per page, tried in turn until the result fits: text formats carry
# short lines, JSON rows long ones.
FIRMWARE_PAGE_LIMITS = {"table": (100, 50, 25, 10, 5), "text": (400, 200, 100, 50, 25)}


@mcp.tool(
    title="Get Firmware Pin Map",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def get_firmware_pin_map(
    mcu: str | None = None,
    format: Literal["table", "csv", "c_header", "devicetree"] = "table",
    checks: list[str] | None = None,
    offset: int = 0,
) -> str:
    """Firmware pin map of a processor and a firmware-facing pinout review, offline. Call it
    when the user asks for a pin map, pinout or pin assignment of an MCU, SoC, FPGA or
    radio module, a pin table or header for firmware, which pin does what, UART TX/RX
    swaps, whether SWD/JTAG or programming pins reach a header or test pads, boot or strap
    pins and their default level, how external flash or EEPROM is reached, unused pins, or
    for a schematic review with the firmware team.

    Without mcu it ranks candidates (U/IC and module parts fitted in the active variant)
    by GPIO-named pins (PA0, PB12, P0.12, P1_05, PIO0_7, GPIO12, IO12, RB3, PTA1 ...),
    IO-typed pins and processor words (an FPGA counts and is labelled one), returns them
    with scores, and maps the top one; mcu takes comma-separated designators. Each pin row:
    number, full name (alternate functions), port and bit parsed from the name, electrical
    type, net, the parts it reaches directly and through up to two series parts, its pull,
    and a likely function (UART, SPI, QSPI, I2C, SWD, JTAG, USB, CAN, Ethernet, analog,
    PWM/timer, crystal, boot/strap, reset, LED, button ...) with its basis; power and
    ground pins are summarised per net. Checks (findings, warnings first): uart_crossing
    (on UART lines and CAN lines to a transceiver, TX meets TX or RX meets RX: warning
    when both ends are certain by pin type, the pin's own name or the part (a USB-UART
    bridge, another MCU or module whose pin lists no other function, a transceiver whose
    TXD takes the MCU's TX), info for connector pins and net-name-only cases), debug_access (warning when no SWD, JTAG or vendor programming
    pin reaches a connector, test point or testpoint pad, also when they are unconnected
    and only USB is; info for partial sets, a JTAG chain, USB only, a port reached only
    through another processor's GPIOs, or a part named in mcu= that is no processor),
    boot_straps (each boot, strap, mode and reset pin with its resistor and default level:
    tied, pulled, a divider with its voltage, set by a switch, driven, or the internal
    default; warning for a mid-rail divider and for an STM32's BOOT0 held high or an
    ESP32's GPIO0 held low with nothing to change it), external_memory (flash, EEPROM,
    FRAM and FPGA configuration memories on the MCU: bus, pins, chip select, WP/HOLD/RESET
    state, and whether its bus reaches a header or pads for programming), unused_pins (the
    debug port's pins left out of the advice), conflicting_assignment (two MCU pins on one
    net, a net named for a function the pin does not list), variant_changes (parts on a
    pin's path fitted in some variants only, needs two or more variants). format: table
    (JSON rows, default), csv (every pin), c_header (#define NET_PORT / NET_PIN: GPIOA only
    on STM32-family parts, PTA for Kinetis, PORTB for PIC, else the port letter or number)
    or devicetree (a Zephyr-style fragment with controller labels for STM32, Kinetis, nRF,
    LPC, i.MX RT, ESP32 and RP2040; other parts' pins and unknown port schemes are
    comments, never invented).

    It is a heuristic, not ERC: most library pins are typed Passive, so names carry most
    of it; the datasheet, the pin-mux table and the firmware decide. checks narrows to the
    names in summary. Rows and findings page together (offset from next_offset)."""
    try:
        project, netlist, variant_state = _require_project()
    except ProjectNotReady as error:
        return json.dumps(error.payload)
    no_components = _no_components_error(netlist)
    if no_components:
        return json.dumps(no_components)
    if format not in FIRMWARE_FORMATS:
        return json.dumps({"error": "invalid_format",
                           "message": f"format must be one of {list(FIRMWARE_FORMATS)}."})
    plan, names, _ = _bom_plan(netlist, variant_state, None)
    plans = [plan_variant(variant, netlist, names) for variant in variant_state.variants]

    def testpoint_loader():
        if not project.get("pcb_doc_paths"):
            return None, "the project lists no PcbDoc"
        index, error = _pcb_session.get(project, netlist)
        if error:
            return None, f"{error['error']}: {error.get('message', '')}".strip(": ")
        return marked_testpoint_nets(index), None

    built = build_firmware_map(
        netlist, plan, project.get("parameters") or {}, mcu=_split_names(mcu), checks=checks,
        format=format, variant_plans=plans, testpoint_loader=testpoint_loader,
    )
    if isinstance(built, dict):
        return json.dumps(built)
    limits = FIRMWARE_PAGE_LIMITS["table" if format == "table" else "text"]
    for limit in limits:
        result = {"netlist_updated_utc": _netlist_last_updated, **built.page(offset, limit)}
        if limit < limits[0]:
            result["page_limited_to"] = limit
        rendered = json.dumps(result, indent=2)
        if len(rendered) <= QUERY_NET_MAX_RESULT_SIZE_CHARS:
            break
    return rendered


# (violations, advisories) per page, tried in turn until the result fits.
ERC_PAGE_LIMITS = ((100, 40), (50, 20), (25, 10), (10, 5), (5, 3), (1, 1))
# The last ERC model and the compile it was built from (a compile of unchanged files,
# .PrjPcb included, is the same object, so the model is reused with it).
_erc_cache: tuple | None = None


def _offline_erc(project: dict) -> tuple:
    """(CompiledNetlist, build_erc model) of the saved sheets, from the cached offline
    compile and the project's ERC levels and Connection Matrix. Raises when the project
    cannot be compiled."""
    global _erc_cache
    sheet_paths = [s["path"] for s in project.get("sheets") or []] or None
    compiled = _altium.compiled_netlist(project["prj_pcb_path"], sheet_paths)
    if _erc_cache is not None and _erc_cache[0] is compiled:
        return compiled, _erc_cache[1]
    options = parse_prj_options(project["prj_pcb_path"])
    model = build_erc(compiled.erc, options.erc_levels, options.connection_matrix)
    _erc_cache = (compiled, model)
    return compiled, model


def _offline_erc_summary(project: dict) -> dict | None:
    """run_erc's counts for other tools to cite as evidence (None when the sheets cannot
    be compiled): reported by level and type, and what No Report levels and No ERC
    markers suppress."""
    try:
        _, model = _offline_erc(project)
        summary = model["summary"]
        return {
            "source": ("run_erc: an offline approximation of Altium's ERC (Altium's own "
                       "result is never saved in the files)"),
            "reported": summary["reported"], "by_level": summary["by_level"],
            "by_violation": summary["by_violation"][:12],
            "hidden_by_no_report": summary["suppressed"]["no_report"],
            "suppressed_by_no_erc": summary["suppressed"]["no_erc"],
        }
    except Exception as e:  # evidence only: the citing tool still answers without it
        logging.info("offline ERC summary unavailable: %s", e)
        return None


@mcp.tool(
    title="Run ERC",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def run_erc(
    checks: list[str] | None = None,
    sheet: str | None = None,
    min_level: Literal["warning", "error", "fatal", "all"] = "warning",
    report: Literal["violations", "single_pin_nets"] = "violations",
    offset: int = 0,
) -> str:
    """Offline approximation of Altium's ERC (Compile / Validate, the Messages panel). Call
    it when the user asks why the project will not compile, what ERC says, about ERC
    errors or warnings, floating input pins or net labels, single-pin nets, nets with
    several names, unconnected wires, ports or sheet entries with no match, duplicate or
    unannotated designators, unused parts of a multi-part component, pin-type conflicts
    (two outputs on one net), nets with no driving source, off-grid objects, or first in
    a schematic review. Altium's own ERC result is never saved in the project files, so
    the saved sheets are compiled offline and checked here; Compile in Altium is final.

    Violations use Altium's violation-type names and groups (Components, Documents, Nets,
    Others) and its message wording, each with level, sheet, object, net, location in mil
    and a suggested fix. Nets: Nets with only one pin, Nets with multiple names, Duplicate
    Nets, Global Power-Object scope changes (a power port wired to a port or off-sheet
    connector, Hierarchical scope), Floating net labels, Floating power objects,
    Unconnected wires, Port with no matching ports (Flat and Global scope), Nets
    containing floating input pins (an Input pin that is its net's only pin), Nets with
    no driving source (an input pin and no IO, Output, Open Collector, HiZ, Open Emitter
    or Power pin), Net Parameters with no value or no name, Missing Positive/Negative Net
    in Differential Pair; judged by the Connection Matrix: Nets with possible connection
    problems, Nets containing multiple similar objects (pins against pins on the net,
    ports and sheet entries against their own sheet and matching port), Unconnected
    objects in net (a pin alone on its net, a port or sheet entry nothing touches on its
    sheet). Components: Duplicate Part Designators (with a note when the .Annotation file
    makes the clash), Un-Designated parts requiring annotation, Unused sub-part in
    component, Components containing duplicate sub-parts, Components with duplicate pins,
    Sheet Symbol with duplicate entries. Documents: Port not linked to parent sheet symbol
    and Sheet Entry not linked to child sheet (hierarchical scopes), Missing child sheet
    for sheet symbol, Duplicate sheet numbers, Sheet Names Clash, Sheets containing
    duplicate ports. Others: Off-grid object (the sheet's snap grid, when snap is on),
    Object not completely within sheet boundaries, Floating Directive Object;
    not_checked lists the rest. Levels are the project's Error Reporting levels (Altium's
    defaults where unset; Duplicate sheet numbers and Port with no matching ports, with
    no documented default, are assumed No Report); Connection Matrix violations take the
    matrix cell's level. Errors come first; off-grid and off-sheet violations come last
    in their level, one row per sheet with a count and examples unless checks names
    them. Types set to No Report, and violations under an active No ERC (on the object,
    or anywhere on the net for a violation about the whole net), are counted in
    summary.suppressed, not listed (min_level="all" lists them, marked); compile-masked
    parts and objects give none.
    Advisories (level info, not Altium violations): near_duplicate_power_aliases (3V3,
    3.3V, VCC_3V3 on separate nets), label_casing (names differing only in case),
    near_miss_wire_ends (a wire end a few mil short of a pin: drawn, not connected),
    ground_nets (each ground net with its pin count).

    checks narrows to violation names, groups or advisory names; sheet to one sheet;
    min_level warning (default), error, fatal or all. report="single_pin_nets" lists every
    single-pin net with its pin and sheet whatever its level. Paginated (offset from
    next_offset); the summary, levels and advisories come with the first page."""
    try:
        project, netlist, _ = _require_project()
    except ProjectNotReady as error:
        return json.dumps(error.payload)
    no_components = _no_components_error(netlist)
    if no_components:
        return json.dumps(no_components)
    try:
        compiled, model = _offline_erc(project)
    except Exception as e:
        return json.dumps({"error": "compile_failed", "message": f"{type(e).__name__}: {e}"})
    context: dict = {"project": project.get("name"), "net_identifier_scope": compiled.scope,
                     "netlist_updated_utc": _netlist_last_updated,
                     "compiled_from": "the sheets as saved now"}
    if not _offline(netlist):
        context["netlist_note"] = (
            "The loaded netlist is Altium's; the ERC compiled the saved sheets offline, so "
            "a net may be named differently where compile_warnings say the offline "
            "compile could not settle it.")
    if not offset and report == "violations":
        warnings = [{"kind": w["kind"], "count": w["count"]} for w in compiled.warnings]
        if warnings:
            context["compile_warnings"] = warnings
        if compiled.sheet_errors:
            context["sheet_errors"] = compiled.sheet_errors
    for limit, advisories in ERC_PAGE_LIMITS:
        page = page_erc(model, checks=checks, sheet=sheet, min_level=min_level, report=report,
                        offset=offset, limit=limit, advisory_limit=advisories)
        if "error" in page:
            return json.dumps(page)
        result = {**context, **page}
        if limit < ERC_PAGE_LIMITS[0][0]:
            result["page_limited_to"] = limit
        rendered = json.dumps(result, indent=2)
        if len(rendered) <= QUERY_NET_MAX_RESULT_SIZE_CHARS:
            break
    return rendered


@mcp.tool(
    title="Check Vias",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def check_vias(
    net: str | None = None,
    checks: list[str] | None = None,
    max_aspect_ratio: float | None = None,
    max_microvia_aspect_ratio: float | None = None,
    min_annular_ring: float | None = None,
    max_stub: float | None = None,
    units: PcbUnits | None = None,
    offset: int = 0,
) -> str:
    """Via inventory and drill review of the PcbDoc: "what vias does this board use?",
    blind/buried/microvias, aspect ratio, annular ring, via-in-pad, tenting, back
    drilling, via stubs. A fabricator's capability limits (is the board manufacturable,
    can my fab make it): check_dfm.

    via_types: each type (span by stack layers, hole as drawn, pad) with count, nets,
    copper-to-copper depth, aspect ratio, smallest annular ring, tenting, in-pad and
    back-drilled counts; net_classes counts types per class; drill_pairs and back_drill
    give the stack's drill pairs, back drills and back-drill rules. Findings, warnings
    first: aspect_ratio (above max_aspect_ratio, default 10:1, info above 8:1 and for a
    blind via above 1:1; microvias above max_microvia_aspect_ratio, default 1:1, info
    above 0.8:1; stacked microvias), annular_ring (below the Minimum Annular Ring rule
    or min_annular_ring, whichever is larger; info under 0.1 mm, microvias 0.05 mm),
    via_style (outside the Routing Via Style sizes), drill_pairs (blind/buried span
    without a drill pair), via_in_pad (fill and cap advice; warning for another net's
    pad or a ViasUnderSMD rule forbidding it), tenting, back_drill (stub above the rule's
    max even when back drilled; info for differential pairs, or the nets in net, with
    stubs above max_stub, default 40 mil). Back drilling is recomputed like Altium: a
    via in the rule's scope is drilled when it has no connection on the layers removed.

    net narrows to comma-separated nets, pairs or net classes; checks narrows the
    findings, paginated (offset from next_offset). min_annular_ring, max_stub and
    lengths use units (default: board display unit). Plugging and filling are not read."""
    index, error = _pcb_index_or_error()
    if error:
        return json.dumps(error)
    to_mm, resolved, units_error = _pcb_units_or_error(index, units)
    if units_error:
        return json.dumps(units_error)
    limits = {}
    for name, value in (
        ("max_aspect_ratio", max_aspect_ratio),
        ("max_microvia_aspect_ratio", max_microvia_aspect_ratio),
        ("min_annular_ring", min_annular_ring),
        ("max_stub", max_stub),
    ):
        if value is None:
            continue
        if not _finite_nonnegative(value) or (value == 0 and name != "min_annular_ring"):
            kind = "non-negative" if name == "min_annular_ring" else "positive"
            return json.dumps({
                "error": f"invalid_{name}",
                "message": f"{name} must be a {kind} number.",
            })
        limits[name] = value
    full = review_vias(
        index,
        checks=checks,
        nets=_split_names(net),
        max_aspect_ratio=limits.get("max_aspect_ratio"),
        max_microvia_aspect_ratio=limits.get("max_microvia_aspect_ratio"),
        min_annular_ring_mil=(
            _input_to_mil(min_annular_ring, resolved) if "min_annular_ring" in limits else None
        ),
        max_stub_mil=_input_to_mil(max_stub, resolved) if "max_stub" in limits else None,
        units=resolved,
    )
    if "error" in full:
        return json.dumps(full)
    project, _, _ = _require_project(netlist=False)
    paths = project.get("pcb_doc_paths") or []
    context: dict = {"units": resolved}
    if paths:
        context["pcb_doc"] = Path(paths[0]).name
        if len(paths) > 1:
            context["note"] = (
                f"Project lists {len(paths)} PcbDocs; reviewed '{Path(paths[0]).name}'."
            )
    for limit, type_limit, class_limit in ((100, 60, 30), (40, 20, 10), (15, 10, 5), (5, 5, 3)):
        result = _convert_units(
            {**context, **page_via_result(full, offset, limit, type_limit, class_limit)},
            to_mm,
        )
        if limit < 100:
            result["findings_limited_to"] = limit
        rendered = json.dumps(result, indent=2)
        if len(rendered) <= QUERY_NET_MAX_RESULT_SIZE_CHARS:
            break
    return rendered


TESTPOINT_LIMITS_SCHEMA = {
    "anyOf": [
        {"type": "object", "additionalProperties": {"anyOf": [
            {"type": "number"}, {"type": "null"},
        ]}},
        {"type": "null"},
    ],
}


@mcp.tool(
    title="Check Testpoints (Test Access)",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def check_testpoints(
    nets: str | None = None,
    profile: str | None = None,
    probe_side: Literal["auto", "top", "bottom", "both"] | None = None,
    limits: Annotated[Any, WithJsonSchema(TESTPOINT_LIMITS_SCHEMA)] = None,
    checks: list[str] | None = None,
    units: PcbUnits | None = None,
    offset: int = 0,
) -> str:
    """Test access (DFT) review of the saved PcbDoc: "can this board be tested?", "ICT /
    bed-of-nails / flying-probe readiness", "which nets have testpoints?", "test
    coverage", before handing the board to a test engineer. Probe sites are pads and vias
    with Altium's Fabrication Testpoint property (top / bottom) and the pads of TP parts
    (designator TP..., or a testpoint footprint); Assembly Testpoint properties are not
    read. sites counts them; coverage gives the share of nets (2+ pads) with a site, per
    side, and the probe side; usage resolves the Testpoint Usage rules per net.

    Findings, warnings first: coverage (no sites at all), required_nets (nets a Testpoint
    Usage rule requires, or that nets names, without a site: Altium's default rule over
    All is info unless the batch DRC checks it), probe_side (nets reachable only from the
    other side of a single-sided fixture), rails (power-named or high-fanout nets with
    fewer than 2 or more than 10 sites), ground (about 1 site per 20 connections), pad_size
    (1.0 mm preferred, 0.9 mm acceptable), spacing (centres 2.5 mm, 1.8 mm minimum),
    edge_clearance (3.2 mm), under_component (under another part's 3D body on the probed
    side; info when the Testpoint rule allows it), tall_parts (within 5 mm of a part over
    5 mm), solder_mask (tented testpoint vias), style_rule (outside the Testpoint rule's
    sizes, holes or sides) and tooling_holes (2 non-plated holes for the fixture).

    profile: "ict" (default: bed of nails, one probe side, auto = the side reaching more
    nets) or "flying_probe" (both sides, looser numbers); pad_size, spacing,
    edge_clearance, under_component and tall_parts judge the probe side's sites only. The numbers are guidance, not a
    standard (profile.guidance): when the user has the test house's numbers pass them in
    limits ({"spacing_min_mm": 1.27, "rail_max_sites": null}: lengths end in _mm or _mil,
    counts take no suffix, null stops judging one). probe_side overrides the profile's.
    nets names critical nets, pairs or classes (comma-separated) that must have access.
    checks narrows the findings, paginated (offset from next_offset). Lengths use units
    (default: board display unit); at_mil locations are relative to origin_mil."""
    index, error = _pcb_index_or_error()
    if error:
        return json.dumps(error)
    to_mm, resolved, units_error = _pcb_units_or_error(index, units)
    if units_error:
        return json.dumps(units_error)
    test_profile, error = build_testpoint_profile(profile, limits, probe_side)
    if error:
        return json.dumps(error)
    project, _, variant_state = _require_project(netlist=False)
    full = review_testpoints(
        index, test_profile, checks, _split_names(nets), resolved,
        variant_state.is_dnp if variant_state is not None else None,
    )
    if "error" in full:
        return json.dumps(full)
    paths = project.get("pcb_doc_paths") or []
    context: dict = {"units": resolved}
    if paths:
        context["pcb_doc"] = Path(paths[0]).name
        if len(paths) > 1:
            context["note"] = (
                f"Project lists {len(paths)} PcbDocs; reviewed '{Path(paths[0]).name}'."
            )
    for limit in (100, 40, 15, 5):
        result = _convert_units({**context, **page_testpoint_result(full, offset, limit)},
                                to_mm)
        if limit < 100:
            result["findings_limited_to"] = limit
        rendered = json.dumps(result, indent=2)
        if len(rendered) <= QUERY_NET_MAX_RESULT_SIZE_CHARS:
            break
    return rendered


@mcp.tool(
    title="Check Connectivity",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def check_connectivity(
    net: str | None = None,
    net_class: str | None = None,
    layer: str | None = None,
    checks: list[str] | None = None,
    compare_drc: bool = True,
    offset: int = 0,
    units: PcbUnits | None = None,
) -> str:
    """Copper connectivity of the saved PcbDoc, like Altium's Un-Routed Net and Net
    Antennae rules: "is the board fully routed?", "what is left to route?", "why does
    DRC say un-routed?", dead copper, antennae, routing progress.

    Per net, whether its copper joins all its pads: tracks, arcs, vias, pads and touching
    copper, the pour copper saved in the file (solid pours' regions, hatched pours'
    tracks), copper regions, fills, and internal planes assigned to the net. summary
    counts nets routed / partly routed / unrouted with percentages and connections
    complete (Altium's routing completion). Findings, warnings first: unrouted (each
    missing connection between the nearest pads of two copper islands, distance_mil,
    and copper_gap_mil when the islands' copper comes within 25 mil: a near miss; the
    closest near misses in near_misses), isolated_copper (copper of a net reaching none of its
    pads: floating tracks and vias, dead pour islands as info), antenna (open-ended
    tracks and tracks ending on a via that connects nothing else, with stub_mil and
    open_track_mil; over_tolerance against an enabled Net Antennae rule, null without
    one), weak_connection (copper that only touches where a net needs it, or a pad or via
    connected directly to a pour with its centre off the pour's outline, which Altium's
    'Check for incomplete connections' rejects). That option of the board's Un-Routed
    Net rule is read (rules.UnRoutedNet, summary.incomplete_connections_checked): where
    it is on, weak connections are warnings and the nets routed here only through them
    read un-routed in Altium (nets_unrouted_in_altium). With
    compare_drc, altium_drc puts Altium's last Batch DRC report beside it
    (get_drc_report reads that report).

    net (nets or differential pairs) and net_class take comma-separated names; layer
    narrows the findings (not the summary) to one copper layer; checks narrows to
    unrouted, isolated, antennae, weak (weak is listed by default only when the option
    is or may be on; the summary always counts it). Findings are paginated (offset from
    next_offset). Lengths use units (default: board display unit)."""
    index, error = _pcb_index_or_error()
    if error:
        return json.dumps(error)
    to_mm, resolved, units_error = _pcb_units_or_error(index, units)
    if units_error:
        return json.dumps(units_error)
    nets = _split_names(net)
    classes = _split_names(net_class)
    full = review_connectivity(index, nets=nets, net_classes=classes, layer=layer, checks=checks)
    if "error" in full:
        return json.dumps(full)
    project, _, _ = _require_project(netlist=False)
    paths = project.get("pcb_doc_paths") or []
    context: dict = {"units": resolved}
    if paths:
        pcb_path = Path(paths[0])
        context["pcb_doc"] = pcb_path.name
        context["pcb_saved_utc"] = _utc(pcb_path.stat().st_mtime) if pcb_path.is_file() else None
        if len(paths) > 1:
            context["note"] = f"Project lists {len(paths)} PcbDocs; checked '{pcb_path.name}'."
        if compare_drc:
            context["altium_drc"] = _connectivity_drc(
                project, pcb_path, index, full, bool(nets or classes)
            )
    for limit, net_limit, connection_limit in (
        (100, 200, 20), (40, 100, 10), (15, 50, 5), (5, 20, 3), (2, 10, 1),
    ):
        result = _convert_units(
            {**context, **page_connectivity_result(
                full, offset, limit, net_limit, connection_limit
            )},
            to_mm,
        )
        if limit < 100:
            result["findings_limited_to"] = limit
            result["connections_per_finding_limited_to"] = connection_limit
        rendered = json.dumps(result, indent=2)
        if len(rendered) <= QUERY_NET_MAX_RESULT_SIZE_CHARS:
            break
    return rendered


def _connectivity_drc(project: dict, pcb_path: Path, index, full: dict, filtered: bool) -> dict:
    """Altium's last Batch DRC report beside check_connectivity: its Un-Routed Net and
    Net Antennae counts and the issues that limit trust in it."""
    report, report_path, _, _, notes = load_report(project, pcb_path)
    if report is None:
        return {
            "report": None,
            "note": "No Batch DRC report was found for this board (get_drc_report says "
            "where it looks).",
        }
    issues = notes + assess(report, report_path, pcb_path, project, index.pcb)
    block: dict = {
        "report": report_path.name,
        "report_saved_utc": _utc(report_path.stat().st_mtime),
        **drc_comparison(report, full),
    }
    relevant = [
        {"issue": issue["issue"], "severity": issue["severity"], "detail": issue["detail"]}
        for issue in issues if issue["severity"] in ("error", "warning")
    ]
    if relevant:
        block["report_issues"] = relevant
    if filtered:
        block["note"] = (
            "The report's counts are the whole board's; the net lists cover the nets "
            "checked."
        )
    return block


@mcp.tool(
    title="Check PCB Health",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def check_pcb_health(
    checks: list[str] | None = None,
    units: PcbUnits | None = None,
    offset: int = 0,
) -> str:
    """Run Altium's PCB Health Check Monitor offline on the saved PcbDoc, plus a few
    board-hygiene checks. Call it when the user asks for a PCB health check, board
    clean-up or hygiene, or before generating fabrication outputs (a fabricator's
    capability limits: check_dfm). Each finding names
    Altium's category and id, with Altium's level as severity (High error, Medium
    warning, Low info): shelved_modified_polygons (stops output generation in Altium),
    duplicate_designators, zero_area_polygons, zero_area_regions,
    self_intersecting_regions, micro_segments_board and micro_segments_copper (region
    edges under 0.03 mil), slot_holes, broken_xsignals, rotation_360,
    reference_point_outside, non_signal_layer_components, unused_xsignals,
    invalid_rules; micro_segments_graphics only when named in checks (Altium runs it in
    debug mode). Not Altium items: components_outside_board (warning when the part has
    netted pads, often parked parts), duplicate_copper, outline_micro_segments.
    altium_report puts the last saved Batch DRC report's health counts next to these.
    not_checked lists what cannot be done offline (mirrored footprints, panels, fonts,
    From-Tos) and checks turned off in the board's own Health Check settings.

    checks narrows to some of these; findings are paginated (offset from next_offset),
    errors first; many objects in one check become one finding listing them.
    Coordinates are relative to the board origin, in units (default: board display
    unit)."""
    index, error = _pcb_index_or_error()
    if error:
        return json.dumps(error)
    to_mm, resolved, units_error = _pcb_units_or_error(index, units)
    if units_error:
        return json.dumps(units_error)
    project, _, _ = _require_project(netlist=False)
    paths = project.get("pcb_doc_paths") or []
    context: dict = {"units": resolved}
    report_health = None
    if paths:
        pcb_path = Path(paths[0])
        context["pcb_doc"] = pcb_path.name
        if len(paths) > 1:
            context["note"] = (
                f"Project lists {len(paths)} PcbDocs; checked '{pcb_path.name}'."
            )
        try:
            report, report_path, _, _, report_notes = load_report(project, pcb_path)
        except Exception:  # a broken report must not stop the health check
            report, report_path, report_notes = None, None, []
        if report is not None:
            context["altium_report_file"] = report_path.name
            report_saved = report_path.stat().st_mtime
            context["altium_report_saved_utc"] = _utc(report_saved)
            cautions = [note["detail"] for note in report_notes if note.get("detail")]
            tables = [table for table in report.tables if table.type == "health"]
            broken = [problem for problem in report.problems if not count_problem(problem)]
            listed = sum(table.count for table in tables)
            if broken:
                cautions.append(
                    "The report is incomplete (empty or cut short), so it is not compared: "
                    "run the Batch DRC again. " + "; ".join(broken[:3])
                )
            elif tables or "health_issues" in report.totals:
                report_health = {table.title: table.count for table in tables}
                if report.totals.get("health_issues", listed) > listed:
                    cautions.append(
                        f"The report counts {report.totals['health_issues']} health issues "
                        f"but its tables list {listed}: the comparison may be incomplete."
                    )
                if report_saved < pcb_path.stat().st_mtime:
                    cautions.append(
                        "The report is older than the saved board: its counts may be out "
                        "of date."
                    )
            else:
                option = index.pcb.drc_options.include_health if index.pcb.drc_options else None
                cautions.append(
                    "The report has no PCB health section, so there is nothing to compare: "
                    + ("'Report PCB Health Issues' is off in the Batch DRC report options."
                       if option is False else
                       "'Report PCB Health Issues' was off, or the Altium version that ran "
                       "it does not offer it.")
                )
            if cautions:
                context["altium_report_notes"] = cautions
    for limit in (100, 40, 15, 5):
        result = run_health_check(
            index.pcb, checks=checks, to_mm=to_mm, offset=offset, limit=limit,
            report_health=report_health,
        )
        if "error" in result:
            return json.dumps(result)
        result = _convert_units({**context, **result}, to_mm)
        if limit < 100:
            result["findings_limited_to"] = limit
        rendered = json.dumps(result, indent=2)
        if len(rendered) <= QUERY_NET_MAX_RESULT_SIZE_CHARS:
            break
    return rendered


# check_dfm's limits as a client sees them: an object of limit keys, each a number, a
# list of numbers or null. The argument itself is typed Any, so that fab_profiles
# checks every value: a wrong type answers invalid_limits with the valid keys instead of
# a pydantic error, and true/false or "0.1" are refused instead of read as numbers.
DFM_LIMITS_SCHEMA = {
    "anyOf": [
        {"type": "object", "additionalProperties": {"anyOf": [
            {"type": "number"}, {"type": "array", "items": {"type": "number"}},
            {"type": "null"},
        ]}},
        {"type": "null"},
    ],
}


@mcp.tool(
    title="Check Manufacturability (DFM)",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def check_dfm(
    checks: list[str] | None = None,
    profile: str | None = None,
    limits: Annotated[Any, WithJsonSchema(DFM_LIMITS_SCHEMA)] = None,
    units: PcbUnits | None = None,
    offset: int = 0,
) -> str:
    """Design-for-manufacture (DFM) and assembly (DFA) check of the saved PcbDoc against
    a fabricator's or assembler's capability limits: "is this board manufacturable?",
    "DFM check", "can my fab / Altimade / JLCPCB / PCBWay make this?", "check against my
    fab's capabilities", before ordering boards; "can it be assembled?", "DFA check",
    fiducials, tombstoning, part spacing, parts near the edge, "too many unique parts
    for Altimade?", "is the pick-and-place file right?". The answer is "meets
    <profile>'s limits on the checks run", never "manufacturable" in general: name
    the profile (scope says which), and never present
    Altimade's result as another fabricator's. When the user names another fab or
    assembler, ask for its numbers (or a fab_profile.json) and pass profile="none" with
    limits. Not: the board's own rules against its copper (check_rule_compliance);
    Altium's DRC result (get_drc_report); via inventory, back drills and stubs
    (check_vias); Altium's health check (check_pcb_health); the parts list (get_bom,
    check_bom); a full manufacturing review (pcb_manufacturing_review).

    profile: "altimade" (built in: Altium's Altimade ordering service, its published
    limits), "none" (only limits apply) or a .json file in the project folder ("acme"
    for acme.json). Default: the project's fab_profile.json (chosen_by project_file),
    else altimade (chosen_by default). limits sets single values on top, with the keys
    of the file and of the echoed profile: lengths end in _mm or _mil
    ({"min_spacing_mm": 0.1}), ratios and counts take no suffix ({"max_aspect_ratio": 8,
    "layer_counts": [2, 4]}); a value is a number or a list of numbers, never text, and
    null removes a limit. min_hole_to_copper falls back to min_spacing and
    min_hole_to_edge to min_copper_to_edge. The errors profile_not_found,
    invalid_profile and invalid_limits say what to fix: tell the user, and never
    substitute another profile or switch a limit's unit without the fab's table.

    checks: trace_width, spacing (hole walls to copper too), hole_size, annular_ring,
    aspect_ratio (copper-to-copper depth over the hole as drawn), hole_to_hole
    (overlapping holes whatever the profile), copper_to_edge (holes to the edge too),
    layer_count, copper_weight, board_size, drill_count, rules_vs_profile (a board rule
    looser than the profile, so Altium's DRC would let it through). Assembly, for the
    active variant (its Not Fitted parts left out): part_counts (placements and unique
    parts against max_placements / max_unique_parts, Altimade's ordering limits; unique
    parts are the BOM's lines with a netlist, else approximate), assembly_sides (SMD on
    both sides, THT on the bottom: extra steps, info), part_spacing (same-side parts by
    3D body outline and pad copper; overlapping parts), part_to_edge, fiducials
    (global per assembled side, local near fine-pitch parts), tombstoning (small
    passives whose pads meet their copper unequally), fine_pitch and pick_and_place
    (Center-X/Y as Altium's pick-and-place file writes it, the midpoint of the outermost
    pad centres: fitted parts without pads, written at the absolute origin, are a
    warning when in the BOM or fiducials; parts whose Center-X/Y is off their 3D body,
    a board origin away from the outline's lower-left corner and Ref-X/Y far from
    Center-X/Y are info; no limit needed). An assembly limit
    the profile does not set (min_part_spacing, min_part_to_edge, min_global_fiducials,
    local_fiducial_pitch, min_pitch) is measured, not judged: ask the assembler.
    results gives each check's status with its severity, limit and worst value: answer
    from status and severity together. Status is fail (a miss of severity error or
    warning), marginal (every such miss within tolerance_percent of its limit, default
    2% and at most 1 mil, e.g. 0.1 mm against 4 mil: a warning), advisory (only info
    misses, such as Altimade's ordering limits while Altimade is only the default: not a
    fabrication miss, report it as a note), pass, or not_checked with a reason.
    board_minimums is what the board needs from any fab or assembler, whatever the
    profile; drills is the drill table. Findings are classes (a width, a family of
    gaps, a drill row or via type) with counts (spacing counts places: spots where two
    nets' copper comes too close) and the worst examples, errors first. Quote
    not_checked (solder mask, silkscreen, plane pullback, panels, courtyards, paste,
    pick-and-place rotations).

    checks narrows to some of these; findings are paginated (offset from next_offset).
    Example locations (at_mil) are relative to the board origin: add origin_mil for
    query_pcb_region. Lengths use units (default: board display unit); limits carry
    their own unit suffix."""
    index, error = _pcb_index_or_error()
    if error:
        return json.dumps(error)
    to_mm, resolved, units_error = _pcb_units_or_error(index, units)
    if units_error:
        return json.dumps(units_error)
    project, netlist, variant_state = _require_project(netlist=False)
    fab_profile, error = load_profile(profile, project.get("root_dir"), limits)
    if error:
        return json.dumps(error)
    # Cached on the index per (checks, units, profile, variant): paging and repeat calls
    # reuse it.
    full = review_dfm(index, fab_profile, checks, to_mm,
                      _dfm_variant(project, netlist, variant_state))
    if "error" in full:
        return json.dumps(full)
    paths = project.get("pcb_doc_paths") or []
    context: dict = {"units": resolved}
    if paths:
        context["pcb_doc"] = Path(paths[0]).name
        if len(paths) > 1:
            context["note"] = (
                f"Project lists {len(paths)} PcbDocs; checked '{Path(paths[0]).name}'."
            )
    # Every answer needs the scope, and example locations need the origin: both lead.
    context["origin_mil"] = full["origin_mil"]
    context["scope"] = full["scope"]
    return _dfm_page(context, full, offset, to_mm)


def _dfm_variant(project: dict, netlist: dict | None,
                 variant_state: VariantState) -> AssemblyContext:
    """check_dfm's assembly context: the active variant's Not Fitted parts and, with a
    netlist, the BOM line count of its fitted parts (grouped as get_bom groups them)."""
    active = variant_state.active
    unique = None
    if netlist is not None and netlist.get("components"):
        plan, _, _ = _bom_plan(netlist, variant_state, None)
        parts, _ = build_bom_parts(netlist, plan, project.get("parameters") or {})
        unique = len(group_bom_lines([part for part in parts if part.fitted]))
    return AssemblyContext(
        variant=active.name, is_dnp=variant_state.is_dnp,
        dnp_key=tuple(sorted(set(active.dnp_refdes))), unique_parts=unique,
        netlist_free=netlist is None,
    )


DFM_FINDINGS_OMITTED = (
    "The result is too large even at 5 findings per page, so findings are left out: call "
    "again with checks narrowed to one check."
)


def _dfm_page(context: dict, full: dict, offset: int, to_mm: bool) -> str:
    """The largest page of check_dfm's result that fits the result size: fewer findings
    and examples per step, then the drill table cut to its first rows, and at worst the
    verdict without findings. Never longer than the limit."""
    steps = [(limit, examples, None) for limit, examples in SHRINK_STEPS]
    steps.append((*SHRINK_STEPS[-1], DRILL_ROWS_LISTED))
    for limit, examples, drill_rows in steps:
        result = _convert_units(
            {**context, **page_dfm(full, offset, limit, examples, drill_rows)}, to_mm,
        )
        if (limit, examples) != SHRINK_STEPS[0]:
            result["findings_limited_to"] = limit
            result["examples_per_finding"] = examples
        rendered = json.dumps(result, indent=2)
        if len(rendered) <= QUERY_NET_MAX_RESULT_SIZE_CHARS:
            return rendered
    counts = {"total": len(full["findings"]), "drills_total": len(full["drills"]),
              "findings_omitted": DFM_FINDINGS_OMITTED}
    brief = {
        **context,
        **{key: full[key] for key in (
            "checks_run", "profile", "board", "board_minimums", "results", "severity_counts",
        )},
        **counts,
    }
    rendered = json.dumps(_convert_units(brief, to_mm), indent=2)
    if len(rendered) <= QUERY_NET_MAX_RESULT_SIZE_CHARS:
        return rendered
    statuses = {check: entry["status"] for check, entry in full["results"].items()}
    return json.dumps({
        "units": context["units"], "scope": full["scope"], "results": statuses,
        "severity_counts": full["severity_counts"], **counts,
    }, indent=2)


BOM_LINE_LIMIT = 100
BOM_BASE_NAMES = ("[no variations]", "no variations", "none", "base")


def _bom_plan(netlist: dict, variant_state: VariantState, variant: str | None):
    """The variant plan for a BOM call: the active variant, a named one, or none (a
    project's own variant named e.g. 'Base' wins over the base-design aliases)."""
    names = variant_names(netlist)
    if variant is None:
        chosen = variant_state.active
    else:
        chosen = variant_state.get(variant.strip())
        if chosen is None and variant.strip().lower() in BOM_BASE_NAMES:
            return plan_variant(None, netlist, names), names, None
        if chosen is None:
            return None, names, _variant_not_found(variant, variant_state)
    return plan_variant(chosen, netlist, names), names, None


def _variant_not_found(variant: str, variant_state: VariantState) -> dict:
    return {
        "error": "variant_not_found",
        "message": f"No variant '{variant}'. Variants: "
        + ", ".join(v.name for v in variant_state.variants)
        + f", or '{BASE_VARIANT}'.",
    }


def _bom_header(netlist: dict, plan, parts: list, excluded: dict) -> dict:
    fitted = [part for part in parts if part.fitted]
    counted = [part for part in fitted if needs_mpn(part)]
    header: dict = {
        "variant": plan.name,
        "netlist_updated_utc": _netlist_last_updated,
        "compiled_part_list": bool(netlist.get("parts")),
        "parts": {
            "in_bom": len(fitted),
            "not_fitted": len(parts) - len(fitted),
            "excluded_by_kind": {kind: len(names) for kind, names in excluded.items()},
        },
        "mpn_coverage_percent": (
            round(100 * sum(1 for part in counted if part.mpn) / len(counted))
            if counted else None
        ),
    }
    if plan.stale:
        header["stale_variant_entries"] = len(plan.stale)
    alternates = [part for part in fitted if part.alternate_part]
    if alternates and all(part.alternate_library for part in alternates):
        header["alternate_parts_note"] = (
            "Alternate parts take their parameters (Comment, MPN) and description from "
            "their copy in the project's .PrjPcbVariants file, with the variant's "
            "parameter variations on top, and their footprint from the variant."
        )
    elif alternates:
        header["alternate_parts_note"] = (
            "Alternate parts take their footprint from the variant; the library data "
            "(Comment, description, MPN) of those not found in the project's "
            ".PrjPcbVariants file is not read, only the variant's parameter variations."
        )
        errors = sorted({getattr(alternate.get("variation"), "library_error", "")
                         for alternate in plan.alternates.values()} - {""})
        if errors:
            header["alternate_parts_note"] += (
                f" The .PrjPcbVariants file could not be read ({errors[0]}).")
    return header


@mcp.tool(
    title="Get BOM",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def get_bom(
    variant: str | None = None,
    group_by: Literal["mpn", "comment"] = "mpn",
    include_not_fitted: bool = False,
    compare_variants: bool = False,
    format: Literal["json", "csv"] = "json",
    offset: int = 0,
    active_bom: bool = False,
) -> str:
    """Bill of materials for a variant (default: the active one; '[No Variations]' for
    the base design): lines with quantity, designators, Comment, description, value,
    footprint, manufacturer and manufacturer part number. Built from the compiled parts
    (compile-masked parts are not in it); Graphical, Net Tie and Standard (No BOM) parts
    and the variant's Not Fitted parts are left out, its parameter variations applied
    (an alternate part takes its copy's data from the project's .PrjPcbVariants file).
    The MPN comes from part parameters, never from Comment ('N/A' and the like are
    shown as mpn_placeholder). group_by mpn groups by MPN, else Comment and footprint;
    comment always by Comment and footprint. A field the line's parts disagree on is in
    varies with its values; logical_designators maps a part to its sheet designator.

    include_not_fitted lists the Not Fitted parts as lines; compare_variants adds what
    each variant changes in the BOM (Not Fitted parts, varied values, alternate parts;
    the compare_variants tool lists every difference between two variants); format csv
    returns the page as CSV text. Lines are paginated (offset from next_offset).

    active_bom adds, from the project's ActiveBOM document (.BomDoc), each line's BomDoc
    line numbers (bom_doc_lines) and chosen solutions (bom_doc_chosen: manufacturer, MPN,
    lifecycle and cache date; CSV columns BomDoc Line # and BomDoc MPN) and a bom_doc
    header; without it the output is as before. get_active_bom gives the whole BomDoc."""
    try:
        project, netlist, variant_state = _require_project()
    except ProjectNotReady as error:
        return json.dumps(error.payload)
    no_components = _no_components_error(netlist)
    if no_components:
        return json.dumps(no_components)
    plan, names, error = _bom_plan(netlist, variant_state, variant)
    if error:
        return json.dumps(error)
    project_params = project.get("parameters") or {}
    parts, excluded = build_bom_parts(netlist, plan, project_params)
    fitted = [part for part in parts if part.fitted]
    not_fitted = [part for part in parts if not part.fitted]
    lines = group_bom_lines(fitted, group_by)
    header = _bom_header(netlist, plan, parts, excluded)
    extra_columns = ()
    if active_bom:
        model, error = _active_bom_model(project, netlist, plan, parts)
        if model is not None:
            try:
                brief = _active_bom_brief(model)
                _annotate_bom_lines(lines, model)
            except Exception as failure:  # the BomDoc must never break the BOM
                model, error = None, _bom_doc_failure(failure)
                for line in lines:
                    for key in ("bom_doc_lines", "bom_doc_chosen", "bom_doc_mpns"):
                        line.pop(key, None)
        if model is None:
            header["bom_doc_error"] = error
        else:
            header["bom_doc"] = brief
            extra_columns = (("BomDoc Line #", "bom_doc_lines"), ("BomDoc MPN", "bom_doc_mpns"))
    offset = max(0, int(offset or 0))
    for limit in (BOM_LINE_LIMIT, 40, 15):
        page = lines[offset : offset + limit]
        result = {**header, "lines_total": len(lines), "offset": offset,
                  "has_more": offset + limit < len(lines)}
        if result["has_more"]:
            result["next_offset"] = offset + limit
        if format == "csv":
            result["csv"] = bom_csv(page, extra_columns)
        else:
            result["lines"] = [{key: value for key, value in line.items()
                                if key != "bom_doc_mpns"} for line in page]
        if not_fitted:
            if include_not_fitted:
                result["not_fitted"] = group_bom_lines(not_fitted, group_by)
            else:
                result["not_fitted_designators"] = [part.designator for part in not_fitted]
        if compare_variants:
            real = [
                v for v in variant_state.variants
                if v.variations or v.param_variations or v.dnp_refdes
            ]
            result["variants"] = compare_bom_variants(netlist, real, names, project_params)
        if limit < BOM_LINE_LIMIT:
            result["lines_limited_to"] = limit
        rendered = json.dumps(result, indent=2)
        if len(rendered) <= QUERY_NET_MAX_RESULT_SIZE_CHARS:
            break
    return rendered


@mcp.tool(
    title="Check BOM",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def check_bom(
    variant: str | None = None,
    checks: list[str] | None = None,
    offset: int = 0,
) -> str:
    """Offline BOM hygiene checks for a variant (default: the active one): duplicate
    designators, special strings ('=Value') that name no parameter, fitted parts whose
    Value or Comment says DNP, parts without a footprint, one MPN with different values,
    descriptions or footprints, an MPN without a manufacturer, parts without an MPN
    (only where the project keeps MPNs in parameters), placeholder descriptions, variant
    entries that no longer fit their parts (designator changed, part gone), passives that
    could share one part number, and sheet parts left out of the compiled design.

    With an ActiveBOM document (.BomDoc) in the project its part choices are checked too
    (get_active_bom shows them): lifecycle_risk (NRND, EOL, last-time-buy or Obsolete on
    the chosen solution: warning; on an alternate only: info), single_source (lines with
    one manufacturer: info), no_solution (no part choice and no MPN parameter),
    out_of_stock_at_qty (no supplier of the chosen solution had the production need in
    stock: warning; info when no figure has a cache date), stale_cache (supply data cached
    over a year ago: info), bomdoc_out_of_sync (parts without a BomDoc line, lines without
    parts and why, lines saved for another revision of the item: info; the chosen MPN
    differs from the parts' MPN parameter: warning), and the MPN checks over every ranked
    MPN; a part whose line has a part choice counts as having an MPN. Stock, price and
    lifecycle are the BomDoc's cache: quote cache_date with them. A finding about one
    BomDoc line gives its line number (absent when unnumbered) and design_item_id.

    checks narrows to some of these (names as in summary); BomDoc checks asked for on a
    project without a BomDoc are listed in checks_not_run. Findings are paginated (offset
    from next_offset), errors first."""
    try:
        project, netlist, variant_state = _require_project()
    except ProjectNotReady as error:
        return json.dumps(error.payload)
    no_components = _no_components_error(netlist)
    if no_components:
        return json.dumps(no_components)
    plan, _, error = _bom_plan(netlist, variant_state, variant)
    if error:
        return json.dumps(error)
    parts, excluded = build_bom_parts(netlist, plan, project.get("parameters") or {})
    header = _bom_header(netlist, plan, parts, excluded)
    model, error = _active_bom_model(project, netlist, plan, parts)
    bom_findings, chosen = None, None
    if model is not None:
        try:
            brief = _active_bom_brief(model)
            bom_findings, chosen = active_bom_findings(model), active_bom_covered(model)
            header["bom_doc"] = brief
        except Exception as failure:  # the BomDoc must never break the BOM checks
            bom_findings, chosen, error = None, None, _bom_doc_failure(failure)
    if bom_findings is None and error and error.get("error") != "no_bom_doc":
        header["bom_doc_error"] = error
    for limit in (100, 40, 15):
        result = run_bom_checks(parts, netlist, plan, checks=checks, offset=offset,
                                limit=limit, bom_doc_findings=bom_findings,
                                chosen_in_bom_doc=chosen)
        if "error" in result:
            return json.dumps(result)
        result = {**header, **result}
        if limit < 100:
            result["findings_limited_to"] = limit
        rendered = json.dumps(result, indent=2)
        if len(rendered) <= QUERY_NET_MAX_RESULT_SIZE_CHARS:
            break
    return rendered


def _active_bom_model(project: dict, netlist: dict, plan, parts: list, name: str | None = None,
                      production_quantity: int | None = None):
    """(ActiveBom model, error) of the project's BomDoc joined to a variant's BOM parts
    (services/active_bom.open_model); with several BomDocs the one the OutJobs' BOM
    outputs name wins. error "no_bom_doc" when the project has none."""

    def outjob_documents() -> list[str]:
        rows = output_locations(project).get("outputs") or []
        return [row.get("document") or "" for row in rows]

    try:
        return open_active_bom(project, parts, netlist, plan, plan.name, name=name,
                               outjob_documents=outjob_documents,
                               production_quantity=production_quantity)
    except Exception as failure:  # the BomDoc must never break the BOM tools
        return None, _bom_doc_failure(failure)


def _bom_doc_failure(failure: Exception) -> dict:
    """The bom_doc_error of a BomDoc step that raised (logged, not shown in full)."""
    logging.warning("BomDoc not read: %s", failure)
    return {"error": "bom_doc_unreadable",
            "message": f"The BomDoc could not be read ({type(failure).__name__}); the BOM "
                       "is checked without it."}


def _active_bom_brief(model) -> dict:
    """The BomDoc a BOM tool read: its file and how it was chosen, and the cache dates."""
    cache = active_bom_cache(model)
    brief = model.choice.describe() if model.choice is not None else {}
    brief["cache"] = {key: cache[key] for key in ("oldest", "newest") if key in cache}
    brief["lines"] = len(model.all_lines)
    return brief


def _annotate_bom_lines(lines: list[dict], model) -> None:
    """get_bom's active_bom columns: each line's BomDoc line numbers and chosen solutions
    (by its designators)."""
    annotations = active_bom_annotations(model)
    for line in lines:
        numbers, chosen = [], []
        for designator in line["designators"]:
            entry = annotations.get(designator.upper())
            if entry is None:
                continue
            if entry["line"] not in numbers:
                numbers.append(entry["line"])
            if entry["chosen"] and entry["chosen"] not in chosen:
                chosen.append(entry["chosen"])
        line["bom_doc_lines"] = numbers
        line["bom_doc_chosen"] = chosen
        line["bom_doc_mpns"] = " / ".join(entry.get("mpn") or "" for entry in chosen)


ActiveBomSection = Literal["header", "lines", "solutions", "non_pcb", "join"]
ACTIVE_BOM_LINES_OMITTED = (
    "The page is too large even at one line: lines are left out; call again with "
    "line=<a line number> for one line's solutions.")


@mcp.tool(
    title="Get Active BOM",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def get_active_bom(
    variant: str | None = None,
    name: str | None = None,
    line: str | None = None,
    sections: list[ActiveBomSection] | None = None,
    offset: int = 0,
    production_quantity: int | None = None,
) -> str:
    """The project's ActiveBOM document (.BomDoc) read offline: the engineer's sourcing
    decisions joined to the design parts of a variant (default: the active one;
    '[No Variations]' for the base design). Call it for "what did we choose to buy?",
    "which parts are NRND, EOL or obsolete?", "do we have approved alternates?", "is
    there stock for the production run?", "what does this line cost from each
    supplier?", "which lines are not on the PCB (bare board, screws, glue)?". Not: the BOM
    lines from the schematic alone (get_bom), the BOM checks (check_bom, which checks these
    part choices too), a total BOM cost (not computed).

    header: production quantity, currency, target and actual unit price, favourite
    suppliers (and whether their order is used), solutions per item, risky-solution
    filtering, custom columns, column aliases and export options (the BomDoc stores no
    variant). lines (paged, in line-number order): line number (null: not numbered; an
    exported BOM leaves Line # empty), Design Item ID, library source, description,
    comment, note, BOM-check status, the design parts it covers (variant applied: Not
    Fitted parts listed apart), quantity per board and at the production quantity, the
    chosen (first-ranked) solution with its lifecycle, stock (stock_total: each
    supplier's largest figure, summed), its suppliers and best price (from offers the need
    reaches the first break of; best_price_below_minimum_order when only a larger order
    gets a price), and the cache dates. solutions adds every ranked solution (rank,
    manufacturer, MPN, lifecycle) with each part choice: supplier, SKU, stock and
    enough_stock for the production need, unit price at that need (the lowest price of the
    breaks it reaches, and that break's quantity; below_minimum_order when the need is
    under the first break, and extended_price then buys that break's quantity), price
    breaks, lifecycle and cache date (a part choice with no supplier is a manufacturer part
    only: no_supplier, no stock). non_pcb: BOM items added in the BomDoc, with no design
    parts. join: how lines map to parts (by Design Item ID; one item split into several
    lines is told apart by its stored parameter values), parts with no line, lines whose
    parts are gone and why, other revisions, line numbers given to several lines, and
    lines whose chosen MPN differs from the parts' MPN parameter. A line without part
    choices takes its parts' MPN parameter as a component_parameters solution, as an
    ActiveBOM export does.

    The need is a line's quantity per board times the BomDoc's production quantity;
    production_quantity= (boards) asks for another run ("stock for 500 boards?"):
    production_quantity_used and production_quantity_from say which was used.

    Stock, prices and lifecycle are what ActiveBOM cached (cache: oldest and newest
    date): quote the cache date with any of them and never present them as current.
    name picks a BomDoc (a project with several, or one in the folder but not in the
    project); line= one line by number, Design Item ID (an unnumbered line), ref ('#12':
    a line whose number or ID names another line too has one) or designator, with all its
    solutions (line_ambiguous lists the refs when a number or ID names several lines);
    sections narrows the result (header, lines, solutions, non_pcb, join).
    Lines are paginated (offset from next_offset)."""
    try:
        project, netlist, variant_state = _require_project()
    except ProjectNotReady as error:
        return json.dumps(error.payload)
    no_components = _no_components_error(netlist)
    if no_components:
        return json.dumps(no_components)
    wanted, problem = parse_active_bom_sections(sections)
    if problem:
        return json.dumps({"error": "invalid_sections", "message": problem})
    if production_quantity is not None and not (
            isinstance(production_quantity, int) and 0 < production_quantity <= 10**9):
        return json.dumps({"error": "invalid_production_quantity",
                           "message": "production_quantity is a number of boards from 1 up; "
                                      "leave it out for the BomDoc's."})
    plan, _, error = _bom_plan(netlist, variant_state, variant)
    if error:
        return json.dumps(error)
    parts, _ = build_bom_parts(netlist, plan, project.get("parameters") or {})
    model, error = _active_bom_model(project, netlist, plan, parts, name, production_quantity)
    if model is None:
        return json.dumps(error)
    chosen_line = None
    if line is not None and str(line).strip():
        found = find_active_bom_lines(model, str(line))
        if not found:
            return json.dumps({
                "error": "line_not_found",
                "message": f"No BomDoc line '{line}': give a line number, a Design Item ID "
                           "(an unnumbered line), a line's ref ('#12') or a designator of the "
                           "line.",
            })
        if len(found) > 1:
            return json.dumps({
                "error": "line_ambiguous",
                "message": f"'{line}' names {len(found)} BomDoc lines (the BomDoc gives them "
                           "one number, or splits one Design Item ID into several lines): "
                           "pass the ref of the line you want, or one of its designators.",
                "lines": [{"ref": entry.label, "line": entry.item.line_number,
                           "design_item_id": entry.item.design_item_id,
                           "designators": [part.designator for part in entry.fitted][:10]}
                          for entry in found[:20]],
            })
        chosen_line = found[0]
    result: dict = {}
    for limit, breaks in ACTIVE_BOM_SHRINK_STEPS:
        result = page_active_bom(model, wanted, offset, limit, breaks, chosen_line)
        if (limit, breaks) != ACTIVE_BOM_SHRINK_STEPS[0]:
            result["lines_limited_to"] = limit
            result["price_breaks_per_choice"] = breaks
        rendered = json.dumps(result, indent=2)
        if len(rendered) <= QUERY_NET_MAX_RESULT_SIZE_CHARS:
            return rendered
    brief = {key: value for key, value in result.items()
             if key not in ("lines", "line", "non_pcb_lines", "join")}
    brief["lines_omitted"] = ACTIVE_BOM_LINES_OMITTED
    return json.dumps(brief, indent=2)


GUIDE_APPROXIMATE_NOTE = (
    "No netlist with schematic parts is loaded: steps group parts by footprint and the "
    "PCB's Comment (approximate BOM lines, no MPN), and Not Fitted parts come from the "
    "designators stored in the project's variants."
)
GUIDE_UNAPPLIED_NOTE = (
    "Not Fitted entries of variant '{variant}' with no designator (only a UniqueId): "
    "{count}. Without the schematic parts they cannot be bound, so their parts may be "
    "listed as placed: check the variant in Altium. Open the project in Altium Designer "
    "once, then call set_project_dir again for a guide that leaves them out."
)
GUIDE_TEXT_OMITTED = (
    "The page is too large even at 5 rows, so its rows are left out: call again with side "
    "set to top or bottom."
)


def _guide_variant(project: dict, netlist: dict | None, variant_state: VariantState,
                   variant: str | None):
    """(GuideVariant, error) for get_assembly_guide: with schematic parts the variant's
    BOM as get_bom builds it (Not Fitted parts bound by UniqueId, parameter variations
    applied), else the designators stored in the project's variant."""
    if netlist is not None and netlist.get("components"):
        plan, _, error = _bom_plan(netlist, variant_state, variant)
        if error:
            return None, error
        parts, excluded = build_bom_parts(netlist, plan, project.get("parameters") or {})
        no_bom = frozenset(name for names in excluded.values() for name in names)
        return GuideVariant(plan.name, plan.not_fitted.__contains__,
                            {part.designator: part for part in parts}, no_bom), None
    chosen = variant_state.active if variant is None else variant_state.get(variant.strip())
    if chosen is None:
        if variant.strip().lower() not in BOM_BASE_NAMES:
            return None, _variant_not_found(variant, variant_state)
        return GuideVariant(BASE_VARIANT), None
    return GuideVariant(chosen.name, frozenset(chosen.dnp_refdes).__contains__,
                        unapplied=len(chosen.dnp_uid or ())), None


@mcp.tool(
    title="Get Assembly Guide",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def get_assembly_guide(
    variant: str | None = None,
    side: Literal["top", "bottom", "both"] | None = None,
    format: Literal["markdown", "csv"] = "markdown",
    units: PcbUnits | None = None,
    offset: int = 0,
) -> str:
    """Assembly sequence for a prototype build of a variant (default: the active one;
    '[No Variations]' for the base design) from the saved PcbDoc: "how do I assemble /
    hand-solder this board?", "assembly guide", "build sheet", "placement order",
    "soldering checklist". Parts are grouped into steps per side and process by BOM line
    (get_bom's grouping, MPN first; without a netlist by footprint and Comment, marked
    approximate) and ordered: the SMD of both sides first, the side whose tallest SMD
    part is shorter (else the one with fewer SMD parts, else the bottom) first, so the
    taller side is soldered last; then the through-hole parts (top, then bottom); on
    each side short to tall (3D body height, else at most a chip part's width, parts
    with no height last). first_side_reason and sequence_rules say why: quote them. Each
    step has its quantity, Comment, footprint, MPN and height, and per part the
    designator, Center-X/Y (as Altium's pick-and-place file writes it) and Ref-X/Y from
    the board origin, rotation and, for polarized parts (diodes, LEDs, polarized
    capacitors, ICs, transistors, connectors), where pin 1, the cathode or + lies from
    the part's centre ('pin 1 top-left', seen from the side being assembled: the bottom
    flipped left to right) or 'no pin 1 marked'. Left out and listed: the variant's Not
    Fitted parts, fiducials, no-BOM kinds, mounting holes, parts without pads and, with
    a netlist, PCB parts not in the BOM and BOM parts not on the board. Quote notes
    (without schematic parts: approximate steps, and Not Fitted entries known only by
    UniqueId that could not be applied) and polarized_parts.note (diodes and polarized
    capacitors with numbered pads: pin 1 is not said to be the cathode or +). No board
    map is drawn (get_component_placement and query_pcb_region locate a part).

    format markdown is a printable checklist (Done and Skipped columns; the first page
    opens with the sequence, the last lists what was left out); csv has one part per row
    with every column. side narrows to top or bottom. Rows are paginated (offset from
    next_offset). Coordinates and heights use units (default: board display unit). Not:
    the pick-and-place data check (check_dfm pick_and_place); the parts list (get_bom)."""
    index, error = _pcb_index_or_error()
    if error:
        return json.dumps(error)
    to_mm, resolved, units_error = _pcb_units_or_error(index, units)
    if units_error:
        return json.dumps(units_error)
    if side not in (None, "top", "bottom", "both"):
        return json.dumps({"error": "invalid_side", "message": "side is top, bottom or both."})
    if format not in ("markdown", "csv"):
        return json.dumps({"error": "invalid_format",
                           "message": "format is markdown or csv."})
    project, netlist, variant_state = _require_project(netlist=False)
    chosen, error = _guide_variant(project, netlist, variant_state, variant)
    if error:
        return json.dumps(error)
    side = None if side == "both" else side
    guide = build_guide(index, chosen, side)
    paths = project.get("pcb_doc_paths") or []
    header: dict = {"units": resolved}
    if paths:
        header["pcb_doc"] = Path(paths[0]).name
    header["origin_mil"] = [round(value, 3) for value in guide.origin]
    header.update(guide_summary(guide, to_mm))
    notes = [GUIDE_APPROXIMATE_NOTE] if guide.approximate else []
    if chosen.unapplied:
        notes.append(GUIDE_UNAPPLIED_NOTE.format(count=chosen.unapplied, variant=chosen.name))
    if notes:
        header["notes"] = notes
    header = _convert_units(header, to_mm)
    title = f"Assembly guide: {project.get('name')}, variant '{guide.variant}'"
    if side:
        title += f", {side} side"
    offset = max(0, int(offset or 0))
    for limit in GUIDE_ROW_LIMITS:
        result = {**header, **page_guide(guide, offset, limit, format, to_mm, title)}
        if limit < GUIDE_ROW_LIMITS[0]:
            result["rows_limited_to"] = limit
        rendered = json.dumps(result, indent=2)
        if len(rendered) <= QUERY_NET_MAX_RESULT_SIZE_CHARS:
            return rendered
    brief = {key: value for key, value in result.items() if key not in ("markdown", "csv")}
    brief.pop("left_out", None)
    brief["text_omitted"] = GUIDE_TEXT_OMITTED
    return json.dumps(brief, indent=2)


DRC_NOT_READ = (
    "This is Altium's last Batch DRC run, not a check of the board as saved now "
    "(check_rule_compliance and check_lengths measure the saved copper). ERC (the "
    "Messages panel), OutJob validation outputs and the DRC XML report are not read."
)


@mcp.tool(
    title="Get DRC Report",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def get_drc_report(
    group_by: Literal["rule", "component", "net", "part"] = "rule",
    rule: str | None = None,
    offset: int = 0,
    units: PcbUnits | None = None,
) -> str:
    """Read Altium's last Batch DRC report for the project's board ('Design Rule Check -
    <PcbDoc>.html' or '.drc' in the project's output folder): violations grouped by rule,
    component or net (group_by), each linked to its components, nets and board location;
    PCB health issues, waived violations and the report's warnings. The assessment comes
    first and says whether to trust the report: incomplete, the DRC stopped at its
    violation limit (later rules, shorts included, then read 0), Un-Routed Net or
    Short-Circuit not checked, older than the board or an Update PCB ran since, made for
    another board, or rules changed since.

    rule (a rule description or part of it, a rule kind such as Clearance, or a health,
    waived or warning title) lists all its violations. Groups and violations are
    paginated (offset from next_offset). location_* is absolute (board origin in
    origin_*, the frame query_pcb_region takes); a message's own coordinates are relative
    to the origin. Locations use units (default: board display unit)."""
    try:
        project, netlist, _ = _require_project(netlist=False)
    except ProjectNotReady as error:
        return json.dumps(error.payload)
    if group_by == "part":
        group_by = "component"
    if group_by not in ("rule", "component", "net"):
        return json.dumps({
            "error": "invalid_group_by",
            "message": "group_by is one of rule, component, net.",
        })
    paths = project.get("pcb_doc_paths") or []
    if not paths:
        return json.dumps({
            "error": "no_pcb_document",
            "message": "The project lists no .PcbDoc, so there is no DRC report to read.",
        })
    pcb_path = Path(paths[0])
    report, report_path, others, searched, notes = load_report(project, pcb_path)
    if report is None:
        return json.dumps({
            "error": "no_drc_report",
            "message": (
                f"No 'Design Rule Check - {pcb_path.stem}.html' or '.drc' was found. In "
                "Altium run Tools > Design Rule Check with 'Create Report File' on; the "
                "report goes to the project's output folder."
            ),
            "searched": searched,
        })
    index = None
    if pcb_path.is_file():
        index, _ = _pcb_session.get(project, netlist)
    to_mm = units == "mm"
    if index is not None:
        to_mm, _, units_error = _pcb_units_or_error(index, units)
        if units_error:
            return json.dumps(units_error)
    pcb = index.pcb if index is not None else None
    assessment = notes + assess(report, report_path, pcb_path, project, pcb)
    linker = DrcLinker(index)
    options = getattr(pcb, "drc_options", None)
    folder = Path(project["prj_pcb_path"]).parent
    header: dict = {
        "report": {
            "file": str(report_path.relative_to(folder))
            if report_path.is_relative_to(folder) else str(report_path),
            "format": report.format,
            "made_for": re.split(r"[\\/]", report.pcb_file)[-1] if report.pcb_file else None,
            "generated": " ".join(part for part in (report.date, report.time) if part),
            "saved_utc": _utc(report_path.stat().st_mtime),
            "elapsed": report.elapsed,
        },
        "pcb_doc": pcb_path.name,
        "pcb_saved_utc": _utc(pcb_path.stat().st_mtime) if pcb_path.is_file() else None,
        "totals": report.totals,
    }
    if pcb is not None:
        header["origin_mil"] = {
            "x_mil": pcb.board.origin_x or 0.0, "y_mil": pcb.board.origin_y or 0.0,
        }
    if options is not None:
        header["drc_settings"] = {
            "stop_after_violations": options.max_violations,
            "create_report_file": options.make_report,
            "report_health_issues": options.include_health,
        }
    if index is None:
        header["note"] = (
            "The PcbDoc could not be read: violations are not linked to components, board "
            "copper or locations; nets and pairs shown come only from the message text."
        )
        if group_by != "rule":
            header["grouping_note"] = (
                f"Grouping by {group_by} needs the PcbDoc; grouped by rule instead."
            )
            group_by = "rule"
    header["assessment"] = assessment
    for limit in (None, 15, 5):
        body = summarize(report, linker, group_by=group_by, rule=rule, offset=offset,
                         limit=limit)
        if "error" in body:
            return json.dumps(body)
        result = {**header, **body, **other_sections(report, linker, limit=limit or 20)}
        if others:
            result["other_reports"] = [path.name for path in others]
        result["not_read"] = DRC_NOT_READ
        if limit:
            result["trimmed_to"] = limit
        rendered = json.dumps(_convert_units(result, to_mm), indent=2)
        if len(rendered) <= QUERY_NET_MAX_RESULT_SIZE_CHARS:
            break
    return rendered


# ---------- verify_outputs ----------

def _verify_board(project: dict, netlist: dict | None):
    """(index, error message): the PCB session's index; both None without a PcbDoc."""
    if not project.get("pcb_doc_paths"):
        return None, None
    index, error = _pcb_session.get(project, netlist)
    return index, (error or {}).get("message")


def _verify_outputs_model(project: dict, netlist: dict | None, variant_state: VariantState,
                          **arguments) -> tuple[dict, dict]:
    """(model, context) of verify_outputs over the loaded project; the model may be an
    error. Shared with check_release_readiness."""
    index, board_error = _verify_board(project, netlist)
    model = run_verify_outputs(project, index, netlist, variant_state, **arguments)
    paths = project.get("pcb_doc_paths") or []
    context: dict = {"project": project.get("name")}
    if paths:
        context["pcb_doc"] = Path(paths[0]).name
        if len(paths) > 1:
            context["note"] = (f"Project lists {len(paths)} PcbDocs; checked against "
                               f"'{Path(paths[0]).name}'.")
    if board_error:
        context["pcb_error"] = board_error
    if variant_state.variants:
        context["active_variant"] = variant_state.active.name
    return model, context


@mcp.tool(
    title="Verify Outputs",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def verify_outputs(
    path: str | None = None,
    variant: str | None = None,
    checks: list[str] | None = None,
    position_tolerance: float | None = None,
    rotation_tolerance: float | None = None,
    drill_tolerance: float | None = None,
    thickness_tolerance: float | None = None,
    units: PcbUnits | None = None,
    offset: int = 0,
) -> str:
    """Cross-check the files Altium generated against the saved design, offline: "are my
    outputs right?", "does the pick-and-place match the board?", "check my release
    package", "is this BOM / drill file / IPC netlist up to date?", before sending files
    to a fabricator or assembler. Not: what the OutJob is set to generate (get_output_jobs),
    the design's own checks (check_dfm, check_bom).

    Files: path= a folder, a release .zip (read in memory, one nested zip level) or one
    file (relative to the project folder or absolute); without it, the folders the
    project's OutJobs generate into (a container folder named by a date/time expression
    is searched from its base: the newest generation per kind by the folder names' date,
    else file time; older ones are counted) and the Output Path. Each file is classified
    by content (a workbook is a BOM once a BOM header row is found) and matched to the
    OutJob output whose folder holds it; identical files are checked once.

    checks (default all): pick_place (every fitted part of the file's variant present
    once, Not Fitted parts absent, side, rotation mod 360 as the component's own on both
    sides, Center-X/Y against the midpoint of the part's pad centres relative to the
    board origin, Ref-X/Y and Pad-X/Y when present, footprint; Comment and Description
    as info), bom (designators equal the variant's BOM parts as get_bom builds them, Not
    Fitted lines, quantity = designator count per line, Comment, Description, Value,
    Footprint, Manufacturer and MPN columns, a column named after a parameter with its
    special strings resolved; a BOM that comes from an ActiveBOM document is checked
    against that BomDoc: its MPN against the line's chosen solution (a line without part
    choices: against the parts' MPN parameter, info), Manufacturer against it and its
    aliases (info), Supplier and Supplier Part Number against its part choices and Line #
    against the BomDoc's line number (info when the BomDoc repeats line numbers); the
    variant from the file's
    title, its OutJob output (info when the file's parts are exactly another variant's),
    its name, else the best fit; .xlsx and .csv are read, .xls is not), netlist
    (IPC-D-356A and IPC-2581 pins against the PcbDoc's pad nets: pins missing, extra or
    unconnected on one side, nets split, merged or renamed, each with designator, pin and
    XY; IPC-2581 components and populate flags), stackup (IPC-2581's stackup: copper
    count, names, order and thickness, the dielectrics between the outer copper layers:
    thickness, Dk and material; the layers .LDP and .DRR layer pairs name), drill (per
    drill set: a .DRR report's layer pair with the drill files it names, else an .LDP
    pair's, else a folder's other drill files as the through holes; Excellon tools vs the
    board's pad, via and bare holes: size within the tool's written precision plus
    drill_tolerance, plating, hole and slot counts, hole positions; the .DRR report vs
    its drill files; a report naming another PcbDoc is called out, not compared) and
    staleness (a design file the output comes from saved after it, the .PrjPcb's
    variants included: file times, info).

    variant checks every file against that variant instead of the file's own (default:
    each file's variant). Tolerances in units (default the board's display unit):
    position_tolerance (default 0.01 mm), drill_tolerance (default 0.01 mil on top of
    the written precision); rotation_tolerance in degrees (default 0.01; a file writing
    whole degrees matches within 0.5), thickness_tolerance in percent (default 10).
    verdict is consistent, mismatches or no_outputs; findings (warnings first: each a
    mismatch with its file and examples; info: a stale file, an OutJob output with no
    file, Comment or Description differences) are paginated (offset from next_offset).
    A mismatch says the file and the saved design differ, not which is right: quote the
    file and ask the user to save in Altium and generate again. Quote not_checked
    (Gerber and ODB++ contents, Board Stack Reports, .xls). Positions (at_mil) are
    relative to the board origin."""
    try:
        project, netlist, variant_state = _require_project(netlist=False)
    except ProjectNotReady as error:
        return json.dumps(error.payload)
    if units not in (None, "mm", "mil"):
        return json.dumps({"error": "invalid_units", "message": "units must be 'mil' or 'mm'."})
    index, _ = _verify_board(project, netlist)
    resolved = (units or (index.pcb.board.display_unit if index is not None else "mm")).lower()
    if resolved not in ("mm", "mil"):
        resolved = "mm"
    to_mm = resolved == "mm"
    tolerances = VerifyTolerances()
    for name, value, most in (("position_tolerance", position_tolerance, None),
                              ("rotation_tolerance", rotation_tolerance, 180.0),
                              ("drill_tolerance", drill_tolerance, None),
                              ("thickness_tolerance", thickness_tolerance, 100.0)):
        if value is None:
            continue
        if not (isinstance(value, (int, float)) and _finite_nonnegative(float(value))
                and (most is None or value <= most)):
            return json.dumps({"error": "invalid_tolerance",
                               "message": f"{name} must be a number of at least 0"
                                          + (f" and at most {most:g}." if most else ".")})
    if position_tolerance is not None:
        tolerances.position_mil = _input_to_mil(float(position_tolerance), resolved)
    if rotation_tolerance is not None:
        tolerances.rotation_deg = float(rotation_tolerance)
    if drill_tolerance is not None:
        tolerances.drill_mil = _input_to_mil(float(drill_tolerance), resolved)
    if thickness_tolerance is not None:
        tolerances.thickness_percent = float(thickness_tolerance)
    model, context = _verify_outputs_model(
        project, netlist, variant_state, path=path, variant=variant, checks=checks,
        tolerances=tolerances)
    if "error" in model:
        return json.dumps(model)
    context["units"] = resolved
    first = VERIFY_SHRINK_STEPS[0]
    for limit, examples in VERIFY_SHRINK_STEPS:
        result = _convert_units({**context, **page_verify_outputs(model, offset, limit,
                                                                   examples)}, to_mm)
        if (limit, examples) != first:
            result["findings_limited_to"] = limit
            result["examples_per_finding"] = examples
        rendered = json.dumps(result, indent=2)
        if len(rendered) <= QUERY_NET_MAX_RESULT_SIZE_CHARS:
            return rendered
    brief = {**context, **{key: model[key] for key in (
        "source", "found", "checks_run", "verdict", "severity_counts") if key in model},
        "results": model["results"], "findings_total": len(model["findings"]),
        "files_total": len(model["files"]),
        "findings_omitted": "The result is too large even at 5 findings per page: call "
                            "again with checks narrowed to one check."}
    return json.dumps(_convert_units(brief, to_mm), indent=2)


# ---------- library parts and checks (roadmap #52) ----------

_installed_library_paths: list[str] | None = None


def _installed_libraries() -> list[str]:
    """Altium's installed libraries on this PC, found once per server run."""
    global _installed_library_paths
    if _installed_library_paths is None:
        try:
            _installed_library_paths = installed_libraries()
        except Exception as error:
            logging.warning("installed libraries not read: %s", error)
            _installed_library_paths = []
    return list(_installed_library_paths)


def _library_context(project: dict, netlist: dict | None):
    """(libraries, PCB index or None, sch_refdes or None, PCB error message or None)."""
    libraries = project_libraries(project["prj_pcb_path"], installed=_installed_libraries())
    index, error = None, None
    if project.get("pcb_doc_paths"):
        index, error = _pcb_session.get(project, netlist)
    sch_refdes = None
    if index is not None and netlist:
        sch_refdes = {row["id"]: row["sch_refdes"] for row in index.db.execute(
            "SELECT id, sch_refdes FROM components") if row["sch_refdes"]}
    return libraries, index, sch_refdes, (error or {}).get("message")


def _library_units(index, units: PcbUnits | None) -> tuple[str | None, dict | None]:
    if units not in (None, "mm", "mil"):
        return None, {"error": "invalid_units", "message": "units must be 'mil' or 'mm'."}
    resolved = (units or (index.pcb.board.display_unit if index is not None else "mm")).lower()
    return (resolved if resolved in ("mm", "mil") else "mm"), None


def _library_header(project: dict, netlist: dict | None, index, pcb_error: str | None,
                    resolved: str) -> dict:
    header: dict = {"project": project.get("name"), "units": resolved}
    paths = project.get("pcb_doc_paths") or []
    if paths:
        header["pcb_doc"] = Path(paths[0]).name
    if pcb_error:
        header["pcb_error"] = pcb_error
    if not netlist:
        header["netlist"] = ("Not loaded (netlist-free load): schematic parts, their pins on "
                             "nets and the symbols they were placed from are not read; the "
                             "PCB's parts are checked.")
    return header


LIBRARY_PART_STEPS = (400, 200, 100, 50, 20, 5)


@mcp.tool(
    title="Get Library Part",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def get_library_part(
    designator: str | None = None,
    lib_reference: str | None = None,
    footprint: str | None = None,
    library: str | None = None,
    units: PcbUnits | None = None,
    offset: int = 0,
) -> str:
    """The library symbol and footprint behind a part, read offline from the library
    files: "what footprint does U5 use and where does it come from?", "what are the
    pins and pin types of this symbol?", "how big are the pads of this footprint?",
    "does the placed footprint still match the library?". Not: the placed part's nets
    and values (get_component), its position and neighbours (get_component_placement).

    designator: a placed part (netlist or PCB designator): its schematic library links,
    its placement (x, y, rotation, side, Height, Component Type), the symbol it was
    placed from and the footprint the board uses, each with the library file it was
    found in and how (origin: project, search path, installed, workspace cache;
    in_named_library false when found by searching every library, as a footprint link
    set to Any does; case_differs) or why not (reason: library file missing, name not
    found, workspace part, database library part); symbol_elsewhere is a same-name
    symbol in another library when its own is missing. Or lib_reference (a library
    component's name or Design Item ID) and/or footprint, searched in every library the
    project can use, or only in library= (a library file name or path: a symbol in a
    SchLib or IntLib, a footprint in a PcbLib or IntLib; a same-name item elsewhere is
    given as symbol_elsewhere or footprint_elsewhere).

    symbol: description, default designator, type, parts, footprint links, other model
    types, parameters and every pin (designator, name, electrical type, part: 0 on
    every part; hidden; display_mode for alternate graphics). footprint: description,
    Height (the fallback 3D height), pads with position in the footprint's own frame,
    layer, shape, size, rotation, hole, slot and plating, bare holes, outlines on the
    library's courtyard, assembly and 3D-body layers (courtyard says when the library
    declares no courtyard layer kinds), 3D bodies with model, height and standoff
    (bodies_summary: count, models, tallest), and 3d (3d_model, extruded_body,
    height_only, no_height). checks: the symbol's and the footprint's component rule
    checks, pins_vs_pads, and for a placed part its used pins without a library copper
    pad (a bare hole has no copper), footprint_drift (the placed footprint against the
    library's, as check_libraries) and 3d on the board. Pins, pads and bodies page
    together (offset from next_offset); lengths in units (default the board's display
    unit). Workspace (Altium 365) and database-library parts are not readable offline:
    not_available_offline says so (get_db_part reads a database part's record)."""
    try:
        project, netlist, _ = _require_project(netlist=False)
    except ProjectNotReady as error:
        return json.dumps(error.payload)
    if not (designator or lib_reference or footprint):
        return json.dumps({"error": "nothing_to_look_up",
                           "message": "Pass designator=, lib_reference= or footprint=."})
    if designator and (lib_reference or footprint or library):
        return json.dumps({"error": "invalid_arguments",
                           "message": "designator= looks up a placed part; lib_reference=, "
                                      "footprint= and library= look up library items. Pass one "
                                      "or the other."})
    libraries, index, sch_refdes, pcb_error = _library_context(project, netlist)
    resolved, error = _library_units(index, units)
    if error:
        return json.dumps(error)
    try:
        model = library_part(libraries, netlist, index.pcb if index is not None else None,
                             sch_refdes, designator=designator, lib_reference=lib_reference,
                             footprint=footprint, library=library)
    except Exception as error:
        logging.warning("get_library_part failed: %s", error)
        return json.dumps({"error": "library_read_failed",
                           "message": f"{type(error).__name__}: {error}"})
    if "error" in model:
        return json.dumps(model)
    header = _library_header(project, netlist, index, pcb_error, resolved)
    to_mm = resolved == "mm"
    for limit in LIBRARY_PART_STEPS:
        result = _convert_units({**header, **page_library_part(model, offset, limit)}, to_mm)
        if limit != LIBRARY_PART_STEPS[0]:
            result["page_limited_to"] = limit
        rendered = json.dumps(result, indent=2)
        if len(rendered) <= QUERY_NET_MAX_RESULT_SIZE_CHARS:
            return rendered
    # Too large even at the smallest page: where the items come from, without their pins,
    # pads, bodies, outlines, parameters and checks.
    brief = {**header, **{key: model[key] for key in (
        "designator", "schematic", "placement", "symbol_elsewhere", "footprint_elsewhere",
        "not_available_offline") if key in model}}
    for key, dropped in (("symbol", ("pins", "parameters", "footprint_links")),
                         ("footprint", ("pads", "bodies", "outlines"))):
        if key in model:
            brief[key] = {name: value for name, value in model[key].items()
                          if name not in dropped}
    brief["omitted"] = ("The result is too large even at 5 pins and pads per page: the pins, "
                        "pads, 3D bodies, outlines, parameters and checks are left out. Call "
                        "check_libraries(part=...) for the checks.")
    return json.dumps(_convert_units(brief, to_mm), indent=2)


LIBRARY_CHECK_STEPS = (100, 50, 25, 10, 5, 1)


@mcp.tool(
    title="Check Libraries",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def check_libraries(
    checks: list[str] | None = None,
    part: str | None = None,
    library: str | None = None,
    footprint: str | None = None,
    where_used: str | None = None,
    folders: list[str] | None = None,
    units: PcbUnits | None = None,
    offset: int = 0,
) -> str:
    """Check the libraries behind the project's parts, offline: "do my footprints still
    match the library?", "run the footprint comparison report", "were footprints edited
    on the board?", "are my libraries clean?", "run the component rule check", "can I
    swap U3 to footprint X?", "is this net tie built right?", "which parts have no 3D
    model?", "where else is this part used?", before a library update or a release.

    Each part's symbol and footprint are resolved in the libraries the project can use
    (its own, the installed ones, then its search paths), as Altium does. checks (default
    every one but where_used):
    resolution (parts placed from a library the project no longer has, with a
    same-name symbol elsewhere; names not found; footprints taken from another library
    than the named one; Workspace and database parts, which are not readable offline);
    footprint_drift (each placed footprint against its library footprint, as Altium's
    Footprint Comparison Report: pad count, pads missing or extra by designator, every
    pad offset from the reference point (origin moved), the footprint turned or
    mirrored, pads moved with the delta, pad size, angle, shape, corner radius, hole,
    slot, plating and layer, then Height, 3D bodies and the other primitives' counts as
    info; positions in the footprint's own frame, a bottom-side part flipped back; each
    finding groups the parts with the same difference; not compared: mask and paste
    expansions, hole offsets, slot angles, texts and the shapes of other primitives);
    pads_cover_pins (every pin on a net has a copper pad in the placed footprint and in
    the library footprint: errors, as Update PCB's Unknown Pin; a bare hole, a pad
    without copper, does not count: on_bare_holes; board pads carrying a net with no
    pin; footprint= checks a proposed swap of the part= parts: swap lists per part
    whether the new footprint keeps every used pin, the pins it loses and its pads
    without pins);
    symbol_rules (Altium's SchLib Component Rule Check: duplicate pins, pins without a
    designator or name, gaps in the pin numbering, no description, default designator
    or footprint, IC pins all Passive; and each symbol against the footprint it is used
    with: pins without pads, pads without pins, a large pad without a pin flagged as an
    exposed pad with no net);
    footprint_rules (Altium's PcbLib Component Rule Check: pads without or sharing a
    designator, duplicate primitives, mirrored, reference point outside the pads,
    shorted copper (pads of different designators touching or joined by copper, a
    warning on the library item; expected in a net tie; on_different_nets names the
    parts net_ties reports) and unconnected copper, no description, no courtyard on a
    library with courtyard layers);
    net_ties (net-tie parts: the footprint's copper joins its pads, the schematic, PCB
    and library agree on the type, the tie joins different nets on the board; a part
    that is not a net tie whose library footprint joins pads on different nets: an
    error when the placed copy joins them too, as DRC's short, a warning when only the
    library's does, as an update from the library would);
    height_3d (per placed part: a 3D model, an extruded body, only the footprint's
    Height, or neither); where_used (where_used='<symbol or footprint name>': the
    registered projects, and the projects under folders=, that place it, with counts
    and designators; with library=, only parts placed from that library).
    part= (comma-separated designators, library references or footprint names) narrows
    every check; library= (a library file name or path) runs symbol_rules,
    footprint_rules, net_ties and height_3d on every item of that library instead.
    Findings (errors first, items many share grouped per library) are paginated (offset
    from next_offset); lengths in units (default the board's display unit). A drift
    finding says the board and the library differ, not which is right. Workspace (Altium
    365) parts and DbLib parts are not readable offline: not_available_offline and
    not_readable_offline_parts say so (check_db_parameters checks DbLib parts against
    their database)."""
    try:
        project, netlist, _ = _require_project(netlist=False)
    except ProjectNotReady as error:
        return json.dumps(error.payload)
    for folder in folders or []:
        if not Path(folder).is_dir():
            return json.dumps({"error": "folder_not_found", "message": f"Folder not found: {folder}"})
    libraries, index, sch_refdes, pcb_error = _library_context(project, netlist)
    resolved, error = _library_units(index, units)
    if error:
        return json.dumps(error)
    projects: list[str] = []
    if where_used or (checks and "where_used" in checks):
        projects = list(dict.fromkeys([*registry_projects(read_registry()),
                                       *project_files(folders or [])]))
    try:
        model = run_library_checks(LibraryCheckInputs(
            libraries, netlist, index.pcb if index is not None else None, sch_refdes,
            checks=checks, parts=_split_names(part), library=library, footprint=footprint,
            where_used=where_used, projects=projects))
    except Exception as error:
        logging.warning("check_libraries failed: %s", error)
        return json.dumps({"error": "library_check_failed",
                           "message": f"{type(error).__name__}: {error}"})
    if "error" in model:
        return json.dumps(model)
    header = _library_header(project, netlist, index, pcb_error, resolved)
    to_mm = resolved == "mm"
    for limit in LIBRARY_CHECK_STEPS:
        result = _convert_units({**header, **page_library_checks(model, offset, limit)}, to_mm)
        if limit != LIBRARY_CHECK_STEPS[0]:
            result["findings_limited_to"] = limit
        rendered = json.dumps(result, indent=2)
        if len(rendered) <= QUERY_NET_MAX_RESULT_SIZE_CHARS:
            return rendered
    brief = {**header, **{key: model[key] for key in (
        "checks_run", "summary", "severity_counts") if key in model},
        "findings_total": len(model["findings"]),
        "findings_omitted": "The result is too large even at 1 finding per page: call again "
                            "with checks= narrowed to one check or part= to a few parts."}
    return json.dumps(_convert_units(brief, to_mm), indent=2)


# ---------- database library parts (roadmap #53) ----------

DB_PART_STEPS = ((10, 10, 200), (5, 5, 60), (3, 3, 20), (1, 1, 5), (1, 0, 0))
DB_CHECK_STEPS = (100, 50, 25, 10, 5, 1)
DB_SECURITY_NOTE = (
    "Database sources are opened read-only (SELECT with parameters, a row limit and a "
    "timeout); connection strings are never shown unmasked. Servers, ODBC data sources "
    "that are not a local file, network shares and OneDrive folders are queried only with "
    "allow_network=true.")


def _db_failure(tool: str, failure: Exception) -> str:
    """The error of a database tool that raised: the exception's type only (its message
    could quote a connection string or a path)."""
    logging.warning("%s failed: %s", tool, type(failure).__name__)
    return json.dumps({"error": "database_lookup_failed",
                       "message": f"The database lookup failed ({type(failure).__name__})."})


@mcp.tool(
    title="Get Database Part",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def get_db_part(
    designator: str | None = None,
    design_item: str | None = None,
    key: str | None = None,
    library: str | None = None,
    table: str | None = None,
    variant: str | None = None,
    allow_network: bool = False,
    alternates_by: list[str] | None = None,
    offset: int = 0,
) -> str:
    """The database record behind a part, read from the project's database libraries
    (.DbLib, .SVNDbLib) and database link files (.DbLink): "what is the approved MPN and
    alternates for C12?", "is this part in our database at all?", "what does the database
    say about U3?", "which symbol and footprint does the record name?". Not: the library
    symbol or footprint files themselves (get_library_part), every part's drift
    (check_db_parameters).

    designator= a placed part: its record by the table's key field (the part's parameter
    or system field the key maps to, else its Design Item ID) or WHERE clause
    ({Parameter} placeholders take the part's values), in the table the part names (a
    DbLib part; a DbLink part with UseDBTableName) or the enabled tables in order, the
    first table with a record winning (also_found_in_tables lists the others).
    design_item= a Design Item ID (a placed part that has it, else looked up as a key
    value); key= a key value looked up in every enabled table (narrow with library= and
    table=). Each match gives the library and how it was found (found_by: project,
    installed, search path, part's library path), matched_by (key field and value, or the
    WHERE clause and its values), confidence (exact; design_item_id: found by the Design
    Item ID standing in for a key parameter the part lacks or holds another value in;
    ambiguous: several records, or the key parameter and the Design Item ID name
    different records, key_conflict), every mapped field with the parameter it maps to,
    the database and schematic values, its update options (with visible_on_add) and what
    Update Parameters From Database would do (status: same, would_change, would_add,
    would_remove, differs_not_updated, blank_in_database, key; a footprint field after
    the first: part_model or not_on_part; a library path the part does not record:
    not_recorded), the unmapped fields, the symbol and footprint the record names against
    the placed ones, and drift findings. records_truncated: more records than the 5 a
    lookup reads. alternates: other records of the table with the same Value and
    [Footprint Ref] (alternates_by= other parameters or system fields; records with the
    part's own key value are left out), at most 10. The variant (default: the active one)
    says whether the part is fitted and looks up its alternate part.

    A library whose source cannot be read is listed in skipped with the reason
    (no_driver, network_not_allowed, file_missing, onedrive_or_share, unreadable). Data
    sources are opened read-only; a database server, an ODBC data source that is not a
    local file, a network share or a OneDrive folder (also a path elsewhere in the
    connection, a Data Link file, or a linked table or saved query inside a local Access
    file: problem query_failed, not_local_table) only with allow_network=true. The
    connection is described (kind, provider, data source, credentials present) with every
    secret masked. Matches are paginated (offset from next_offset)."""
    try:
        project, netlist, variant_state = _require_project()
    except ProjectNotReady as error:
        return json.dumps(error.payload)
    no_components = _no_components_error(netlist)
    if no_components:
        return json.dumps(no_components)
    if not any(str(value or "").strip() for value in (designator, design_item, key)):
        return json.dumps({"error": "nothing_to_look_up",
                           "message": "Pass designator= (a placed part), design_item= (a "
                                      "Design Item ID) or key= (a key field value)."})
    if alternates_by is not None and not (
            isinstance(alternates_by, list) and all(isinstance(name, str) for name in alternates_by)):
        return json.dumps({"error": "invalid_alternates_by",
                           "message": "alternates_by is a list of parameter names or system "
                                      "fields such as '[Footprint Ref]'."})
    plan, _, error = _bom_plan(netlist, variant_state, variant)
    if error:
        return json.dumps(error)
    try:
        model = db_parts.build_part(db_parts.PartInputs(
            project["prj_pcb_path"], netlist, plan, _installed_libraries(),
            designator=designator, design_item=design_item, key=key, library=library,
            table=table, allow_network=bool(allow_network), alternates_by=alternates_by))
        if "error" not in model:
            db_parts.add_alternates(model, alternates_by, bool(allow_network))
    except Exception as failure:  # a database problem must never break the server
        return _db_failure("get_db_part", failure)
    if "error" in model:
        return json.dumps(model)
    header = {"variant": plan.name, "allow_network": bool(allow_network),
              "security": DB_SECURITY_NOTE}
    offset = max(0, int(offset or 0))
    matches = model.get("matches") or []
    result: dict = {}
    for limit, alternates, unmapped in DB_PART_STEPS:
        page = dict(model)
        page["matches"] = matches[offset:offset + limit]
        result = {**header, **db_parts.page_part(page, alternates, unmapped)}
        result["matches_total"] = len(matches)
        result["offset"] = offset
        result["has_more"] = offset + limit < len(matches)
        if result["has_more"]:
            result["next_offset"] = offset + limit
        if (limit, alternates, unmapped) != DB_PART_STEPS[0]:
            result["limited_to"] = {"matches": limit, "alternates": alternates,
                                    "unmapped_fields": unmapped}
        rendered = json.dumps(result, indent=2)
        if len(rendered) <= QUERY_NET_MAX_RESULT_SIZE_CHARS:
            return rendered
    brief = {key_: value for key_, value in result.items() if key_ not in ("matches", "alternates")}
    brief["matches_omitted"] = ("The record is too large even trimmed: call again with "
                                "table= or library= to narrow it.")
    return json.dumps(brief, indent=2)


@mcp.tool(
    title="Check Database Parameters",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def check_db_parameters(
    variant: str | None = None,
    library: str | None = None,
    table: str | None = None,
    allow_network: bool = False,
    offset: int = 0,
) -> str:
    """Every part placed from a database library (.DbLib, .SVNDbLib) or linked through a
    database link file (.DbLink) against its database record: a read-only preview of
    Altium's Update Parameters From Database. "Are my parameters in sync with the
    database?", "which parts are not in our database?", "what would Update Parameters
    From Database change?", before a release or a BOM export. Nothing is changed: run
    the update in Altium to apply it.

    Findings (warnings first; per part, library and table): would_change (the part's
    value differs and the field's update option is Always; a parameter on the sheet with
    an empty value counts as present), would_add (the part lacks a mapped parameter the
    update adds: Always, or If not blank with a value; with visible_on_add), would_remove
    (blank in the database, remove option If blank), differs_not_updated (update option
    Never), blank_in_database (kept), case-only differences as info; the key field's own
    parameter is compared too when the record was found by the Design Item ID.
    record_not_found, duplicate_records (several records match: the first is compared;
    more_records when more than the 5 read), key_conflict (the key parameter and the
    Design Item ID name different records: the key parameter's is compared),
    found_in_several_tables (info: the first table in the library's order is used),
    symbol_differs ([Library Ref] against the placed symbol), footprint_differs
    ([Footprint Ref] against the current footprint: info when it is another of the
    part's models), library_path_differs (only when the part records a library file),
    description_differs, key_missing (a DbLib part without the key value),
    table_not_in_library, table_disabled, no_lookup, where_unsupported (a WHERE clause
    beyond comparisons with AND, OR, NOT, LIKE, IS NULL), bad_name (a table or field name
    that cannot be quoted safely: only its lookups are left out), query_failed (with the
    error, e.g. not_local_table: a linked table or saved query in a local Access file,
    queried only with allow_network=true), library_not_found (parts name a database
    library the project cannot find), library_unreadable, library_skipped (with the
    reason: no_driver, network_not_allowed, file_missing, onedrive_or_share,
    unreadable, or the query layer's code), no_tables, and alternate_record_not_found
    (the variant's alternate part). Lookup problems that need no query are reported
    even when the source is skipped; a database link file's table problems once per
    table with its parts. A DbLink links a part by its key parameter: parts without it
    are counted (parts_without_key), not reported; parts restricted to a table it lacks
    or has disabled (UseDBTableName) are counted (parts_table_not_in_link,
    parts_table_disabled).

    libraries: each library with found_by (project, installed, search path, part's
    library path), the connection described with every secret masked, update defaults,
    tables (lookup by key field or WHERE clause, mapped fields, symbol and footprint
    fields), parts linked, records found, queries and seconds, finding counts. variant
    (default: the active one) marks Not Fitted parts (fitted false) and the values its
    parameter variations set (variant_value); parameters compare on the base design, as
    the update does. library= and table= narrow the check; allow_network=true also queries
    database servers, ODBC data sources that are not a local file, network shares and
    OneDrive folders (read-only). Findings are paginated (offset from next_offset)."""
    try:
        project, netlist, variant_state = _require_project()
    except ProjectNotReady as error:
        return json.dumps(error.payload)
    no_components = _no_components_error(netlist)
    if no_components:
        return json.dumps(no_components)
    plan, _, error = _bom_plan(netlist, variant_state, variant)
    if error:
        return json.dumps(error)
    inputs = db_parts.CheckInputs(project["prj_pcb_path"], netlist, plan,
                                  _installed_libraries(), library=library, table=table,
                                  allow_network=bool(allow_network))
    key = (project["prj_pcb_path"], id(netlist), _netlist_last_updated, plan.name,
           (library or "").strip().lower(), (table or "").strip().lower(), bool(allow_network),
           db_parts.library_signature(project["prj_pcb_path"]))
    try:
        model = db_parts.cached_check(key, lambda: db_parts.build_check(inputs))
    except Exception as failure:  # a database problem must never break the server
        return _db_failure("check_db_parameters", failure)
    if "error" in model:
        return json.dumps(model)
    header = {"variant": plan.name, "allow_network": bool(allow_network),
              "security": DB_SECURITY_NOTE}
    offset = max(0, int(offset or 0))
    result: dict = {}
    for limit in DB_CHECK_STEPS:
        result = {**header, **db_parts.page_check(model, offset, limit)}
        if limit != DB_CHECK_STEPS[0]:
            result["findings_limited_to"] = limit
        rendered = json.dumps(result, indent=2)
        if len(rendered) <= QUERY_NET_MAX_RESULT_SIZE_CHARS:
            return rendered
    brief = {**header, "summary": model["summary"],
             "findings_total": len(model["findings"]),
             "findings_omitted": "The result is too large even at 1 finding per page: call "
                                 "again with library= or table= to narrow it."}
    return json.dumps(brief, indent=2)


# ---------- multi-board systems (roadmap #54) ----------

MULTIBOARD_TRACE_STEPS = ((40, 10), (20, 5), (10, 3), (5, 1), (2, 0))
MULTIBOARD_CHECK_STEPS = ((100, 8), (50, 5), (25, 3), (10, 2), (5, 1), (1, 0))
MULTIBOARD_PIN_STEPS = (150, 100, 50, 25, 10, 5, 1)
MultiboardMating = Annotated[
    Any,
    WithJsonSchema({"anyOf": [{"type": "array", "items": {"type": "object"}}, {"type": "null"}],
                    "description": "Connections when the .MbsDoc is missing or unreadable: "
                                   "[{board_a, connector_a, board_b, connector_b, pin_map?, "
                                   "kind?, name?}]"}),
]


def _multiboard_system(system: str | None, mating) -> tuple[Any, dict | None]:
    """(the multi-board system model, None) or (None, the structured error). The loaded
    project's netlist is used for its own board; the loaded project is not changed."""
    if mating is not None and not isinstance(mating, list):
        return None, {"error": "invalid_mating", "message": multiboard.MATING_HELP}
    project = _project
    loaded = None
    if project is not None and _altium._netlist is not None:
        loaded = (project["prj_pcb_path"], _altium._netlist)
    try:
        path, found_by = multiboard.locate_system(
            system, project["prj_pcb_path"] if project is not None else None)
        model = multiboard.cached_system(path, found_by, mating, loaded, _netlist_last_updated)
    except multiboard.MultiboardError as error:
        return None, error.payload
    except Exception as failure:  # a damaged system must never break the server
        logging.warning("Multi-board system failed: %s", type(failure).__name__)
        return None, {"error": "multiboard_failed",
                      "message": f"The multi-board system could not be read "
                                 f"({type(failure).__name__})."}
    if model.source == "none":
        return None, {
            "error": "no_multiboard_schematic",
            "message": ("The multi-board schematic (.MbsDoc) is "
                        f"{model.mbs_status.replace('_', ' ')}: {model.mbs_message} Its "
                        "connections are unknown. Ask the user how the boards' connectors mate "
                        "and pass mating=. " + multiboard.MATING_HELP),
            **multiboard.header(model),
        }
    return model, None


def _multiboard_args(model, board: str | None, connection: str | None):
    """(boards, connection, None) or (None, None, error) for the board= and connection=
    filters."""
    try:
        return (multiboard.resolve_boards(model, board),
                multiboard.resolve_connection(model, connection), None)
    except multiboard.MultiboardError as error:
        return None, None, error.payload


@mcp.tool(
    title="Trace Net Across Boards",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def trace_net_across_boards(
    net: str,
    board: str | None = None,
    system: str | None = None,
    mating: MultiboardMating = None,
    offset: int = 0,
) -> str:
    """Follow a net from one board of a multi-board system (.PrjMbd) through every mated
    connector pin into the other boards: "where does RESET_N go on the daughter card?",
    "what does J5 pin 3 reach on the backplane?", "is 3V3 carried to the module, and on how
    many pins?", "which boards share this ground?". Not: one board's own net (query_net),
    the whole pin table (get_system_connections), the checks (check_multiboard).

    The system is the .PrjMbd in the loaded project's folder or a folder above it that
    lists the project (system= a .PrjMbd or its folder for another). Each board is a module
    of the multi-board schematic (.MbsDoc) with its child project compiled offline (the
    loaded project's own netlist for its board; the loaded project is not changed). net=
    a net name on board= (a module designator such as M1, its title or its project; default:
    the loaded project's board, else the board with that net: net_on_several_boards when
    the connections do not join the boards that have it), or a module connector's pin
    (J5-3, J5.3, "J5 pin 3": the trace starts from that pin's net, start.from_pin;
    pin_has_no_net lists what an unconnected pin mates). From that board net the
    trace follows every mated pin of the module's connectors through its connection (a
    direct mating pin to pin; a cable or harness through its plugs and wires, a splice
    reaching several boards) to the other board's pin and net, then on from that net,
    recursively. system_net: the boards reached and every board net's name (different
    names on mated pins are normal: hops carry names: reason and severity, as
    check_multiboard reads them). board_nets: each board net with its depth (hops from the
    start), class (ground, power, signal, auto: a name Altium generated, Net<part>_<pin>),
    the voltage its name gives and its other pins (other_pins_total; connector pins are in
    the hops). hops: per connection and pair of connectors, the pins mated (pin_count; via:
    the plug pins of a cable or harness), status no_net when the other pin has no net;
    unwired: pins whose cable or harness pin no wire continues (status unwired) or whose
    wire ends in a plug pin no board mates (dead_end); pairs_between_reached_nets counts
    splice links between nets already reached; two nets of one board joined elsewhere show
    as two board_nets of that board. mating= stands in for a missing or unreadable .MbsDoc
    (see check_multiboard). Hops are paginated (offset from next_offset)."""
    model, error = _multiboard_system(system, mating)
    if error:
        return json.dumps(error)
    try:
        start, name, start_pin = multiboard.find_start(
            model, net or "", board, _project["prj_pcb_path"] if _project else None)
    except multiboard.MultiboardError as failure:
        return json.dumps(failure.payload)
    offset = max(0, int(offset or 0))
    header = multiboard.header(model)
    result: dict = {}
    for limit, pins in MULTIBOARD_TRACE_STEPS:
        traced = multiboard.trace(model, start, name, pins, start_pin)
        result = {**header, **multiboard.page_trace(traced, offset, limit)}
        if (limit, pins) != MULTIBOARD_TRACE_STEPS[0]:
            result["limited_to"] = {"hops": limit, "pins_per_hop": pins}
        rendered = json.dumps(result, indent=2)
        if len(rendered) <= QUERY_NET_MAX_RESULT_SIZE_CHARS:
            return rendered
    brief = {key: value for key, value in result.items() if key not in ("hops", "board_nets")}
    brief["hops_omitted"] = "The trace is too large even trimmed: page it with offset."
    return json.dumps(brief, indent=2)


@mcp.tool(
    title="Check Multi-Board System",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def check_multiboard(
    system: str | None = None,
    board: str | None = None,
    connection: str | None = None,
    mating: MultiboardMating = None,
    offset: int = 0,
) -> str:
    """The checks of a multi-board system (.PrjMbd) that Altium's Connection Manager does
    not highlight: "check my multi-board connections", "do the mated pins carry the same
    signals?", "is anything unmated?", "does ground reach every board?", before releasing
    a system of boards. Not: one board's own ERC (run_erc), the pin table
    (get_system_connections), one net's path (trace_net_across_boards).

    The system and its boards are found as trace_net_across_boards finds them (system=,
    the loaded project is not changed). Each finding has its check, severity (error,
    warning, info), the connection, both pins (board, connector, pin, net) or the board
    and connector, and a message. Five checks are Altium's own multi-board violations and
    carry altium_check with the level the .PrjMbd's Error Reporting sets (altium_level; a
    warning of a check set to No Report is shown as info, errors stay errors):
    different_net_names (Different Net Names: two mated pins whose boards name the net
    differently; info when the names look like one signal: a board prefix or shared tail,
    a one-letter board suffix, leading zeros, an abbreviation, a common synonym (CLK and
    SCK), a name Altium generated, nets numbered per board, two names of one rail, TX
    against RX; warning or error when they suggest different signals: ground_to_power
    and different_voltages are errors, ground_to_signal, power_to_signal, index_differs
    (another index on the same stem or bus: a shifted or swapped pin, another bus, or an
    intended remap), polarity_differs (P against N, DP against DM, H against L),
    active_low_differs (RESET_N against RESET), same_group_other_signal (SPI_MOSI against
    SPI_MISO), swapped (X mates Y while Y mates X) and unrelated_names warnings); no_net
    (No Net, per pin as Altium counts it: every entry pin with no net on its board; a
    warning when the other side carries a ground or rail, else info grouped as a signal on
    one side, open on both sides or mating no board; summary.no_net_pins is the count);
    no_mated_part (No Mated Part: an entry no connection mates, or whose cable or harness
    reaches no other module); entry_is_empty (Entry Is Empty: an entry with no pins or no
    connector part, or a connector whose child part lost System=Connector, which the next
    import would empty); stored_conflict (Unresolved Conflict: what Altium recorded in the
    .MbsDoc, quoted, not recomputed; stored_conflicts compares them with the findings
    here). The others have no Altium counterpart: unwired_pin (a pin mated to a cable or
    harness pin no wire continues, or whose wire ends in a plug no board mates);
    board_nets_joined (two nets of one board joined through other boards: a warning for
    signals, info for grounds or rails); power_rails (rails of different voltages or a
    ground and a rail joined through more than one hop; ground_not_shared: a board whose
    ground reaches none of the boards it connects to); unmated_pins (pins with a net that
    no connection mates: info for mounting and shield pins); connector_not_in_system (a
    System=Connector part the multi-board schematic does not place, or no mating names);
    connector_not_in_project (a connector or pins the child project no longer has: they
    keep the nets stored at the last import); net_names_unsynchronized (the child
    project's net now differs from the one the .MbsDoc stored at its last import);
    board_not_loaded (a child project that did not compile: its pins keep the stored
    nets). Info findings of one kind on one connection are grouped (count, examples).
    connections: per connection its kind, boards, connectors, pin pair counts by status
    and finding counts; power_rails: every system net with a rail or ground, its board nets
    with class and voltage and the pins per connection carrying it. board= and
    connection= narrow the findings.

    mating= replaces the .MbsDoc's connections (use it when the .MbsDoc is missing or in
    a format that is not read: ask the user, never guess): [{board_a, connector_a,
    board_b, connector_b, pin_map?, kind?, name?}], boards by module designator, title or
    project, connectors by designator, pin_map {'1': '2'} or [['1', '2']] from board_a's
    pins to board_b's (default: the same pin numbers), kind direct, wire, cable or
    harness; mating a connector with itself is refused, and one connector in two direct
    matings or one pair of connectors listed twice is warned about (system.warnings).
    Findings are paginated (offset from next_offset)."""
    model, error = _multiboard_system(system, mating)
    if error:
        return json.dumps(error)
    boards, wanted, error = _multiboard_args(model, board, connection)
    if error:
        return json.dumps(error)
    checked = multiboard.check(model, boards, wanted)
    header = multiboard.header(model)
    offset = max(0, int(offset or 0))
    result: dict = {}
    for limit, examples in MULTIBOARD_CHECK_STEPS:
        result = {**header, **multiboard.page_check(checked, offset, limit, examples)}
        if (limit, examples) != MULTIBOARD_CHECK_STEPS[0]:
            result["limited_to"] = {"findings": limit, "examples": examples}
        rendered = json.dumps(result, indent=2)
        if len(rendered) <= QUERY_NET_MAX_RESULT_SIZE_CHARS:
            return rendered
    brief = {key: value for key, value in result.items()
             if key not in ("findings", "power_rails", "connections")}
    brief["findings_omitted"] = ("The result is too large even at 1 finding per page: call "
                                 "again with board= or connection=.")
    return json.dumps(brief, indent=2)


@mcp.tool(
    title="Get System Connections",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def get_system_connections(
    system: str | None = None,
    board: str | None = None,
    connection: str | None = None,
    net: str | None = None,
    mating: MultiboardMating = None,
    offset: int = 0,
) -> str:
    """The inter-board pin table of a multi-board system (.PrjMbd): "give me the
    inter-board connection table", "which pins does C1 mate?", "what is on the
    backplane connector's pins?", "export the board-to-board pinout". Not: one net's path
    across several boards (trace_net_across_boards), the checks (check_multiboard).

    The system and its boards are found as trace_net_across_boards finds them (system=,
    the loaded project is not changed). connections: each connection with its kind
    (direct, wire, cable, harness), boards, connectors, plugs (cable and harness) and pin
    pair count. rows: one per pair of board pins a connection joins end to end
    (connection, kind, a and b: board, connector, pin, pin_name, net), a cable or harness
    followed through its plugs and wires (via; a splice gives one row per pair of ends),
    with status same_net, different_names (names: reason and severity, as check_multiboard
    reads them), no_net (one side has no net), unconnected (neither has), unwired (b null:
    the cable or harness pin has no wire on) or dead_end (b null: the wire ends in a plug
    pin no board mates). Rows are ordered by connection, board
    and pin; board= (module designator, title or project), connection= and net= (either
    side's net) narrow them. mating= stands in for a missing or unreadable .MbsDoc (see
    check_multiboard). Rows are paginated (offset from next_offset)."""
    model, error = _multiboard_system(system, mating)
    if error:
        return json.dumps(error)
    boards, wanted, error = _multiboard_args(model, board, connection)
    if error:
        return json.dumps(error)
    rows = multiboard.pin_rows(model, boards, wanted, net)
    header = multiboard.header(model)
    connections = [row for row in multiboard.connection_list(model)
                   if (not wanted or row["connection"] == wanted)
                   and (not boards or set(row["boards"]) & set(boards))]
    offset = max(0, int(offset or 0))
    result: dict = {}
    for limit in MULTIBOARD_PIN_STEPS:
        result = {**header, "connections": connections,
                  **multiboard.page_rows(rows, offset, limit)}
        if limit != MULTIBOARD_PIN_STEPS[0]:
            result["rows_limited_to"] = limit
        rendered = json.dumps(result, indent=2)
        if len(rendered) <= QUERY_NET_MAX_RESULT_SIZE_CHARS:
            return rendered
    brief = {key: value for key, value in result.items() if key != "rows"}
    brief["rows_omitted"] = "The table is too large even at 1 row per page."
    return json.dumps(brief, indent=2)


# ---------- check_pin_table (roadmap #22) ----------

PIN_TABLE_STEPS = (200, 100, 50, 20, 10, 5)
PIN_TABLE_MAX_TEXT = 8 << 20  # characters of a pasted table


def _netlist_component(netlist: dict | None, designator: str) -> tuple[str | None, dict | None]:
    """(netlist designator, component) for a designator (any case; a sheet designator of a
    board-annotated part when it names one part)."""
    components = (netlist or {}).get("components") or {}
    wanted = designator.strip().casefold()
    name = next((k for k in components if k.casefold() == wanted), None)
    if name is None:
        placed = [k for k, comp in components.items()
                  if (comp.get("logical_refdes") or "").casefold() == wanted]
        name = placed[0] if len(placed) == 1 else None
    return name, components.get(name) if name else None


def _pin_table_symbol(project: dict, netlist: dict | None, designator: str | None,
                      component: dict | None, lib_reference: str | None) -> tuple:
    """(SchLibComponent or None, what was looked up and where it was found or why not)."""
    libraries = project_libraries(project["prj_pcb_path"], installed=_installed_libraries())
    resolver = LibraryResolver(libraries)
    if lib_reference:
        hit, reason = resolver.find_symbol([lib_reference])
        looked_up = {"lib_reference": lib_reference}
    elif component is not None:
        names = [component.get("design_item_id") or "", component.get("lib_reference") or ""]
        hit, reason = resolver.find_symbol(
            names, component.get("library") or "", bool(component.get("workspace_part")),
            bool(component.get("database_table")))
        looked_up = {"lib_reference": component.get("lib_reference"),
                     "library": component.get("library")}
    else:
        # A netlist-free load: the PCB's part names its library component.
        index = None
        if project.get("pcb_doc_paths"):
            index, _ = _pcb_session.get(project, netlist)
        wanted = (designator or "").strip().casefold()
        board = next((c for c in (index.pcb.components if index is not None else [])
                      if (c.designator or c.refdes or "").casefold() == wanted), None)
        if board is None:
            return None, {"found": False, "reason": "the part is not on the PCB and no netlist "
                                                    "is loaded"}
        result = resolver.resolve_pcb([board])[0]
        hit, reason = result.symbol, result.symbol_reason
        looked_up = {"lib_reference": board.lib_reference, "library": board.component_library}
    if hit is None:
        return None, {**looked_up, "found": False, "reason": reason}
    loader = ItemLoader()
    symbol = loader.symbol(hit)
    if symbol is None:
        error = loader.errors.get(hit.path)
        return None, {**looked_up, "found": False,
                      "reason": f"library unreadable: {error}" if error else "name not found"}
    found = {**looked_up, "found": True, "name": hit.name, "library_file": Path(hit.path).name,
             "origin": hit.origin, "parts": symbol.part_count}
    if not hit.named:
        found["in_named_library"] = False
    return symbol, found


def _pin_table_target(project, netlist, variant_state, designator, lib_reference, compare):
    """(sides, target description, notes) or an error dict."""
    target: dict = {}
    notes: list[str] = []
    sides = []
    component = None
    if designator:
        name, component = _netlist_component(netlist, designator)
        if netlist is not None and component is None:
            return {"error": "component_not_found",
                    "message": f"Component '{designator}' not found. Use search_components to "
                               "find it."}
        target["designator"] = name or designator
        if component is not None:
            target["lib_reference"] = component.get("lib_reference")
            if compare in ("both", "placed"):
                sides.append(placed_pins(component))
            _, changes = shown_variant_values(variant_state, name, component)
            alternate = (changes or {}).get("alternate_part")
            if alternate:
                notes.append(
                    f"The active variant {changes.get('variant')} fits the alternate part "
                    f"{alternate.get('lib_reference') or '?'} here: compared is the base "
                    "design's part (pass lib_reference= to check the alternate's symbol).")
        elif compare == "placed":
            return _netlist_required_error()
        else:
            notes.append("No netlist is loaded: the placed part's pins were not compared, only "
                         "its library symbol.")
    if compare in ("both", "library"):
        try:
            symbol, found = _pin_table_symbol(project, netlist, designator, component,
                                              lib_reference)
        except Exception as error:  # an unreadable library is reported, not raised
            logging.warning("check_pin_table symbol lookup failed: %s", error)
            symbol, found = None, {"found": False, "reason": f"{type(error).__name__}: {error}"}
        target["library_symbol"] = found
        if symbol is not None:
            sides.append(symbol_pins(symbol))
        elif compare == "library" or not sides:
            return {"error": "library_symbol_not_found",
                    "message": "The library symbol could not be read offline ("
                               f"{found.get('reason')}): Workspace (Altium 365) and database "
                               "library parts are not readable offline. Pass designator= with "
                               "compare='placed' to check the placed part's pins.",
                    "library_symbol": found}
        else:
            notes.append(f"The library symbol was not compared ({found.get('reason')}): only "
                         "the placed part's pins were.")
    target["compared"] = [side.side for side in sides]
    return sides, target, notes


@mcp.tool(
    title="Check Pin Table",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def check_pin_table(
    designator: str | None = None,
    lib_reference: str | None = None,
    table: str | None = None,
    path: str | None = None,
    package: str | None = None,
    compare: Literal["both", "placed", "library"] = "both",
    offset: int = 0,
) -> str:
    """Compare a datasheet or vendor pin table with a part's pins, offline: "does U5's
    symbol match the datasheet pinout?", "check this pin table against the FPGA", "are
    any pins swapped or misnamed?", before the board is built or after a library change.

    The part: designator= (the placed part: every pin's name and electrical type, units
    merged, with the nets of connected pins; and its library symbol, resolved as
    get_library_part does), or lib_reference= (a library symbol in the project's
    libraries; with designator= it replaces the placed part's own symbol). compare: both
    (default), placed or library. The table: table= (text pasted from a datasheet or
    spreadsheet: tabs, commas, semicolons, pipes or runs of spaces; header row optional)
    or path= (.csv, .tsv, .txt or .xlsx; relative to the project folder). Vendor pinout
    files are recognised: AMD/Xilinx package files, Intel pin-out files (a section or
    column per package), STM32CubeMX pinout CSVs and datasheet tables with a column per
    package; package= picks one (default: the package that matches the part best, with
    each package's score).

    Pins are paired by number (BGA balls in natural order; a prefix such as P1..P208 on
    one side only is dropped or added). Names are compared after normalisation (case,
    separators, I/O, active-low notations nRESET, RESETn, RESET#, RESET_N, ~RESET,
    overline, alternate functions): exact, equivalent (summary count), likely_same (info,
    grouped by reason: an alternate function, an index or qualifier more, an abbreviation)
    or different; a name that resembles its pin's but is another pin's own name
    (CLK_P / CLK_N, CE / CEO) is different. Findings (errors first; in: the sides that
    have them, each finding once): swapped (two pins' names exchanged), shifted (a run of
    names offset by a constant), different_name (why: when another pin holds the name),
    missing_in_design, extra_in_design (shield, mounting and NC pins exempt; a table of
    fewer than half the pins lists the rest once as not_in_partial_table info),
    duplicate_number, duplicate_name, type_mismatch (power vs signal, input vs output;
    milder differences as type_differs info), hidden_signal_pin and hidden_power_pins
    (symbol), power_pin_unconnected and nc_pin_connected (placed), nc_not_in_design,
    exposed_pad_numbered_differently. table says how the table was read
    (format, columns, packages, unread_rows with line and reason); placed_vs_library
    says how the placed part differs from its symbol. Findings page with offset
    (next_offset). Workspace (Altium 365) and database-library symbols are not readable
    offline: compare='placed' checks the placed part."""
    try:
        project, netlist, variant_state = _require_project(netlist=False)
    except ProjectNotReady as error:
        return json.dumps(error.payload)
    if not (designator or lib_reference):
        return json.dumps({"error": "nothing_to_check",
                           "message": "Pass designator= (a placed part) or lib_reference= (a "
                                      "library symbol)."})
    if bool(table) == bool(path):
        return json.dumps({"error": "invalid_arguments",
                           "message": "Pass the pin table as table= (pasted text) or path= (a "
                                      "file), one of them."})
    if compare not in ("both", "placed", "library"):
        return json.dumps({"error": "invalid_arguments",
                           "message": "compare must be 'both', 'placed' or 'library'."})
    if compare == "placed" and not designator:
        return json.dumps({"error": "invalid_arguments",
                           "message": "compare='placed' needs designator= (a placed part)."})
    try:
        if table:
            if len(table) > PIN_TABLE_MAX_TEXT:
                raise PinTableError("table_too_large", "The pasted table is too large: pass "
                                    "the file as path= instead.")
            source = parse_pin_text(table)
        else:
            file = Path(path)
            if not file.is_absolute():
                file = Path(project["root_dir"]) / file
            source = parse_pin_file(str(file))
    except PinTableError as error:
        return json.dumps({"error": error.code, "message": str(error)})
    target = _pin_table_target(project, netlist, variant_state, designator, lib_reference,
                               compare)
    if isinstance(target, dict):
        return json.dumps(target)
    sides, described, notes = target
    try:
        model = check_pin_rows(source, sides, package)
    except ValueError as error:
        return json.dumps({"error": "package_not_found",
                           "message": f"No package '{package}' in the table; it lists: {error}.",
                           "packages": source.packages})
    model["notes"] = notes + model["notes"]
    table_info = table_description(source, model.pop("_table"))
    selection = {key: value for key, value in model.pop("table_used").items()
                 if key in ("how", "pins_matched_by_package", "note")}
    if selection:
        table_info["package_selection"] = selection
    header = {"project": project.get("name"), "target": described, "table": table_info}
    if not netlist:
        header["netlist"] = "Not loaded (netlist-free load): placed pins are not read."
    for limit in PIN_TABLE_STEPS:
        result = {**header, **page_pin_table(model, offset, limit)}
        if limit != PIN_TABLE_STEPS[0]:
            result["findings_limited_to"] = limit
        rendered = json.dumps(result, indent=2)
        if len(rendered) <= QUERY_NET_MAX_RESULT_SIZE_CHARS:
            return rendered
    table_info.pop("unread_rows", None)
    brief = {**header, "summary": model["summary"], "notes": model["notes"],
             "findings_total": len(model["findings"]),
             "findings_omitted": "The result is too large even at 5 findings per page: the "
                                 "summary counts each kind; call again with compare='placed' "
                                 "or 'library', or a smaller table."}
    return json.dumps(brief, indent=2)


# ---------- check_release_readiness ----------

@mcp.tool(
    title="Check Release Readiness",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def check_release_readiness(offset: int = 0) -> str:
    """One "is this ready for review or fab?" report, offline, modelled on Altium's
    Project Releaser: a fixed list of gates, each with status (pass, fail, warn,
    not_verified, not_applicable), blocking, one line of evidence from the check behind
    it and source_tool (the tool to call for the details). Gates: Batch DRC report clean
    and newer than the board (get_drc_report), ERC (blocking and always not_verified: no
    ERC result is saved in the files; quotes run_erc's offline counts as evidence and
    says whether an OutJob ERC validation output makes the Releaser run it), netlist
    newer than every sheet, schematic and PCB in sync
    (check_sch_pcb_sync; both need the netlist), no unrouted nets (check_connectivity),
    polygons not shelved or modified (check_pcb_health), board outline, global fiducials
    per assembled side (check_dfm), mounting holes, Not Fitted parts left out of the BOM
    and pick-and-place outputs (Altium places them on the PCB by design), the OutJob
    complete for every variant and its generated outputs newer than the design
    (get_output_jobs) and matching it where verify_outputs reads them (both advisory),
    Draftsman fab notes against the stackup (check_fab_drawing), title-block revision
    and date across sheets and drawings (get_design_notes), project
    integrity (check_project_integrity), the Altium 365 copy committed and level
    (get_clone_status), silkscreen DRC, test access on ICT guidance (check_testpoints,
    advisory); and not_verified with the reason: footprints vs libraries, lifecycle and
    compliance, teardrops, via stitching, the team's template.

    verdict: fail when a blocking gate failed (one failed check fails the release, as in
    the Releaser), not_ready_unverified when none failed but a blocking gate is
    not_verified, pass only when every blocking gate passed, warned or is not applicable
    (verdict_summary names the blocking warns). A missing data source is not_verified,
    never a pass; a non-blocking gate's problem is a warn. Gate rows are paginated
    (offset from next_offset); the verdict comes with every page."""
    try:
        project, netlist, variant_state = _require_project(netlist=False)
    except ProjectNotReady as e:
        return json.dumps(e.payload)
    inputs = ReleaseInputs(
        project=project,
        netlist=netlist,
        variant_state=variant_state,
        notes=_design_notes,
        pcb_index=lambda: _pcb_session.get(project, netlist),
        saved_after_netlist=_saved_after_netlist,
        sheets_saved_after=_sheets_saved_after,
        dfm_variant=lambda: _dfm_variant(project, netlist, variant_state),
        fab_board_reader=_fab_board_reader(project, netlist),
        fab_bom=_fab_bom(project, netlist, variant_state),
        clone_status=clone_status,
        offline_erc=lambda: _offline_erc_summary(project),
        verify_outputs=lambda: _verify_outputs_model(project, netlist, variant_state)[0],
    )
    model = build_release_readiness(inputs)
    for limit in (len(model["gates"]) or 1, 12, 6, 3, 1):
        rendered = json.dumps(page_release_readiness(model, offset, limit), indent=2)
        if len(rendered) <= QUERY_NET_MAX_RESULT_SIZE_CHARS:
            break
    return rendered


# ---------- run_review_checklist (roadmap #46) ----------

REVIEW_ITEM_STEPS = (80, 40, 20, 10, 5, 2)
# (evidence characters, anchors per kind, design files listed, item text characters, open
# items with their text) of the Markdown record.
REVIEW_RECORD_STEPS = ((220, 6, 60, 300, True), (160, 4, 30, 200, True),
                       (110, 3, 10, 120, False), (70, 2, 0, 80, False), (40, 1, 0, 60, False))
REVIEW_CACHE_SECONDS = 300
REVIEW_CACHE_SIZE = 4
REVIEW_RECORD_HINT = (
    "Call again with format='markdown' (and decisions= for the needs-judgment items you "
    "settled) for the review record: the coverage table, every item with its evidence, "
    "the design state and a sign-off table. Nothing is saved: offer to save it where the "
    "user agrees, and pass it as previous= in a later review.")
_bundled_checklists: dict | None = None
_review_cache: dict = {}  # key -> (time, model)


def _checklists_bundled() -> dict:
    """The bundled checklists, read once per server run."""
    global _bundled_checklists
    if _bundled_checklists is None:
        _bundled_checklists = load_bundled_checklists()
    return _bundled_checklists


def _review_tools() -> dict:
    """The evidence tools, as the MCP tool functions themselves: a checklist item gets
    exactly what a direct call with its arguments returns; run_custom_checks answers the
    custom items (check-pack rules)."""
    tools = {name: globals()[name] for name in REVIEW_EVIDENCE_TOOLS}
    tools["run_custom_checks"] = run_custom_checks
    return tools


def _review_model(project: dict, netlist: dict | None, variant: str | None, checklist: dict,
                  sections: list[str] | None, items: list[str] | None, state: dict) -> dict:
    """The checklist run, reused for REVIEW_CACHE_SECONDS while the design state, every
    file under the project folder (OutJobs, DRC reports, outputs, fab profile), the
    netlist, the variant and the checklist are the same (format='markdown' right after
    the JSON call does not run the tools again). A folder of more than
    FOLDER_SIGNATURE_FILES files is not cached."""
    folder = folder_signature(project.get("root_dir") or ".")
    key = (project.get("prj_pcb_path"), json.dumps(checklist, sort_keys=True),
           tuple(sections or ()), tuple(items or ()), state.get("id"),
           state.get("fingerprint"), folder, variant, id(netlist), _netlist_last_updated,
           pack_signature(project.get("root_dir")))
    now = datetime.now(timezone.utc).timestamp()
    for old in [k for k, (stamp, _) in _review_cache.items()
                if now - stamp > REVIEW_CACHE_SECONDS]:
        del _review_cache[old]
    if folder is not None and key in _review_cache:
        return _review_cache[key][1]
    model = build_review_checklist(project, netlist, checklist, tools=_review_tools(),
                                   sections=sections, items=items, variant=variant,
                                   state=state)
    if folder is None:
        return model
    while len(_review_cache) >= REVIEW_CACHE_SIZE:
        del _review_cache[next(iter(_review_cache))]
    _review_cache[key] = (now, model)
    return model


@mcp.tool(
    title="Run Review Checklist",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def run_review_checklist(
    checklist: str | None = None,
    sections: list[str] | None = None,
    items: list[str] | None = None,
    previous: str | None = None,
    decisions: dict[str, str] | None = None,
    overrides: bool = True,
    format: Literal["json", "markdown"] = "json",
    offset: int = 0,
) -> str:
    """Run a design-review checklist against the saved project, offline: "run the
    schematic checklist", "what is left before sign-off?", "write the review record",
    "what changed since the last review?", and first in schematic_review,
    constraints_review, pcb_manufacturing_review and release_review.

    checklist: schematic (power, signals and interfaces, components and ratings,
    connectors and protection, testability and production, style), pcb (placement, power
    and thermal, high speed and constraints, manufacturing, testability), release
    (validation, generated outputs, documentation, configuration) or bom (part data,
    sourcing, variants), or a team's own; none lists the checklists with their sections
    and item counts. sections= and items= (ids, in any case) narrow it. Each item is auto
    (a deterministic tool decides), assisted (a tool gives evidence: status needs
    judgment with lean yes or no, the datasheet or the reviewer decides), judgment (a
    person) or custom (a rule of the team's check packs decides, through
    run_custom_checks). The tools run in-process with the arguments the item names, as a direct call
    would (active variant, every page an item needs read), once per call; each item row
    gives status (yes, no, n/a, needs_judgment), evidence (counts and the first finding's
    text; for a Yes, what was checked), tool and args, findings (finding ids with
    counts), anchors (designators, nets, sheets), could_not_run when its tool failed or
    needs what the load lacks (a netlist), and unresolved when a field the item reads is
    not in the tool's result (then needs judgment, never Yes). coverage counts items by
    status per section; design_state is the Git commit (and whether any of the PrjPcb,
    sheets, PcbDocs and drawings differs from it, wherever it is) or the SHA1s of those
    files, so a sign-off names the state it belongs to.

    decisions={"SCH-PWR-02": "no: U3 EN floats (datasheet p. 12)"} records the review's
    own Yes, No or N/A for items (the tool's status is kept as tool_status). format
    'markdown' returns the review record: header, design state, the 'Checklist coverage'
    table, every item with status and evidence, the open items, a sign-off table and a
    machine-readable block; nothing is written. A record that does not fit one result
    even shortened is a record_too_large error: call per section. previous= (a record
    saved earlier, path absolute or relative to the project folder) adds what changed:
    design state, newly failing, fixed (No to Yes), still open, other changes. A run is
    reused for 5 minutes while the design files and every file in the project folder are
    unchanged. Team overrides: review_checklists.json or review_checklists/*.json next to
    the .PrjPcb or in the Git repository root above it (replace a checklist, add sections
    and items, custom items naming a check-pack rule among them, disable items by id;
    arguments are checked against the tool's parameters); overrides=false ignores them. Items are paginated (offset from
    next_offset)."""
    try:
        project, netlist, variant_state = _require_project(netlist=False)
    except ProjectNotReady as error:
        return json.dumps(error.payload)
    if format not in ("json", "markdown"):
        return json.dumps({"error": "invalid_format",
                           "message": "format is 'json' or 'markdown'."})
    try:
        checklists, override_info = load_checklists(
            project.get("root_dir"), overrides=overrides, bundled=_checklists_bundled(),
            tools=_review_tools())
    except ChecklistError as error:
        return json.dumps(error.error)
    name = (checklist or "").strip().lower()
    if not name:
        if sections or items or previous or decisions or format == "markdown":
            return json.dumps({"error": "checklist_required",
                               "message": "Name the checklist (checklist=) to run.",
                               "checklists": sorted(checklists)})
        return json.dumps(checklist_catalog(checklists, override_info), indent=2)
    chosen = checklists.get(name)
    if chosen is None:
        return json.dumps({"error": "unknown_checklist",
                           "message": f"No checklist '{checklist.strip()[:60]}'.",
                           "checklists": sorted(checklists)})
    try:
        selected = select_checklist_items(chosen, sections, items)
        decided = parse_decisions(decisions, selected)
        prior = read_previous_record(previous, project.get("root_dir")) if previous else None
    except ChecklistError as error:
        return json.dumps(error.error)
    if not selected:
        return json.dumps({"error": "no_items",
                           "message": f"The selection leaves no item of the '{name}' "
                                      "checklist to run (disabled by an override?)."})
    state = design_state(project, head_state)
    variant = variant_state.active.name if variant_state.variants else None
    model = _review_model(project, netlist, variant, chosen, sections, items, state)
    model = {**model, "overrides_applied": override_info.get("applied") or []}
    model = apply_decisions(model, decided)
    comparison = compare_review_records(model, prior) if prior else None
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    if format == "markdown":
        for evidence_chars, anchors, files, text_chars, open_detail in REVIEW_RECORD_STEPS:
            record = render_record(model, generated_utc=generated, generator=_read_version(),
                                   comparison=comparison, evidence_chars=evidence_chars,
                                   anchors_shown=anchors, files_shown=files,
                                   text_chars=text_chars, open_detail=open_detail)
            if len(record) <= QUERY_NET_MAX_RESULT_SIZE_CHARS:
                return record
        # A cut record would lose its machine-readable block (previous= reads it).
        return json.dumps({
            "error": "record_too_large",
            "message": f"The review record of {len(model['items'])} items is larger than one "
                       "result even shortened: call again with sections= (or items=) for "
                       "one record per part (each names the same design state).",
            "sections": [section["id"] for section in model["sections"]]})
    for limit in REVIEW_ITEM_STEPS:
        page = page_review(model, offset, limit, comparison=comparison)
        page["record"] = REVIEW_RECORD_HINT
        if limit != REVIEW_ITEM_STEPS[0]:
            page["items_limited_to"] = limit
        rendered = json.dumps(page, indent=2)
        if len(rendered) <= QUERY_NET_MAX_RESULT_SIZE_CHARS:
            return rendered
    brief = page_review(model, offset, 1, files_listed=0, comparison=comparison, full=False)
    brief["items_limited_to"] = 1
    brief["omitted"] = ("The result is too large even at 1 item per page: call again with "
                        "sections= or items= to narrow it.")
    return json.dumps(brief, indent=2)


# ---------- run_custom_checks (roadmap #49) ----------

# (rule row detail, findings per page), tried in turn until the result fits. Every rule
# keeps a row (id, pack, status and counts at least) on every page: a checklist's custom
# item reads its rule's status from it.
CUSTOM_CHECK_STEPS = (("full", 100), ("full", 40), ("full", 15), ("compact", 15),
                      ("compact", 5), ("minimal", 5), ("minimal", 1))
CUSTOM_CACHE_SECONDS = 300
CUSTOM_CACHE_SIZE = 4
_custom_cache: dict = {}  # key -> (time, model)


def _custom_tools() -> dict:
    """The tools a pack's tool rule may run, as the MCP tool functions themselves."""
    return {name: globals()[name] for name in TOOL_RULE_TOOLS}


def _custom_model(key: tuple | None, build) -> dict:
    """The rules' run, reused for CUSTOM_CACHE_SECONDS while the key (project, netlist,
    variant, every file in the project folder, the pack and plugin files, the call's
    choices) is the same, so the next pages do not run the rules again. key None (a
    project folder too large to sign) is not cached."""
    now = datetime.now(timezone.utc).timestamp()
    for old in [k for k, (stamp, _) in _custom_cache.items()
                if now - stamp > CUSTOM_CACHE_SECONDS]:
        del _custom_cache[old]
    if key is not None and key in _custom_cache:
        return _custom_cache[key][1]
    model = build()
    if key is not None:
        while len(_custom_cache) >= CUSTOM_CACHE_SIZE:
            del _custom_cache[next(iter(_custom_cache))]
        _custom_cache[key] = (now, model)
    return model


def _custom_run(pack: str | None, rules: list[str] | None, path: str | None,
                allow_plugins: bool) -> tuple[dict | None, dict | None, dict | None]:
    """run_custom_checks' run before paging: (error payload, None, None), or (None, the
    result's header, the model of run_rules). The check command (cli.py) reads every
    rule and finding from the model."""
    try:
        project, netlist, variant_state = _require_project(netlist=False)
    except ProjectNotReady as error:
        return error.payload, None, None
    root = project.get("root_dir")
    tools = _custom_tools()
    try:
        packs, pack_info = load_check_packs(root, path, tools)
    except PackError as error:
        return error.error, None, None
    plugins = plugin_files(root) if root else []
    plugin_ids = [plugin_id(item) for item in plugins]
    if not packs and not plugins:
        return ({
            "error": "no_check_packs",
            "message": f"No check pack was found: add {PACK_FILE} or {PACK_FOLDER}/*.json "
                       "next to the .PrjPcb or in the root of the Git repository above it, "
                       "or pass path= to a pack file.",
            "hint": "The README's 'Custom check packs' section and examples/check_packs.json "
                    "show the format."}, None, None)
    try:
        chosen_packs, chosen, plugins_wanted = choose_custom_rules(packs, pack, rules,
                                                                   plugin_ids)
    except PackError as error:
        return error.error, None, None
    if not plugins_wanted:
        plugins = []
    elif rules:
        named = {name.strip().lower() for name in rules if isinstance(name, str)}
        plugins = [item for item in plugins if plugin_id(item).lower() in named]
    run_plugins, plugin_reason = plugins_allowed(allow_plugins)

    def build() -> dict:
        api, no_design = None, None
        if netlist is None:
            no_design = ("netlist_required: the project was loaded without a netlist, so "
                         "parts, nets and pins cannot be selected (tool rules still run)")
        elif _no_components_error(netlist):
            no_design = "no_components_loaded: the netlist has no component data"
        else:
            plan, _, _ = _bom_plan(netlist, variant_state, None)
            api = DesignAPI(netlist, plan, project.get("parameters") or {},
                            pcb=lambda: _pcb_session.get(project, netlist))
        return run_custom_rules(chosen, api=api, tools=tools, packs=chosen_packs,
                                no_design=no_design, plugins=plugins,
                                plugin_reason=None if run_plugins else plugin_reason)

    folder = folder_signature(root or ".")
    variant = variant_state.active.name if variant_state.variants else None
    key = None if folder is None else (
        project.get("prj_pcb_path"), id(netlist), _netlist_last_updated, variant, folder,
        pack_signature(root, path), path, pack, json.dumps(rules), run_plugins)
    model = _custom_model(key, build)
    header: dict = {
        "project": project.get("name"),
        "variant": variant,
        "load": "netlist" if netlist is not None else "netlist_free",
        "units": "mm",
        "packs": [{"id": item["id"], "title": item["title"], "version": item["version"],
                   "file": item["file"], "rules": len(item["rules"]),
                   **({"rails": item["rails"]} if item.get("rails") else {})}
                  for item in chosen_packs],
    }
    if pack_info.get("replaced"):
        header["replaced"] = pack_info["replaced"]
    if plugin_ids:
        header["plugins"] = {"folder": f"{PACK_FOLDER}/{PLUGIN_FOLDER}", "found": plugin_ids,
                             "run": bool(run_plugins and plugins),
                             **({"reason": plugin_reason} if not run_plugins else {}),
                             "note": PLUGIN_HINT}
    return None, header, model


@mcp.tool(
    title="Run Custom Checks",
    annotations=ToolAnnotations(readOnlyHint=True),
    meta={"anthropic/maxResultSizeChars": QUERY_NET_MAX_RESULT_SIZE_CHARS},
)
def run_custom_checks(
    pack: str | None = None,
    rules: list[str] | None = None,
    path: str | None = None,
    allow_plugins: bool = False,
    offset: int = 0,
) -> str:
    """Run the team's own deterministic checks (check packs) on the saved project,
    offline: "run our team checks", "run the check packs", "does the board pass our
    rules?", "run rule X", before a review or a release.

    Packs are JSON files the team keeps with the design: check_packs.json or
    check_packs/*.json next to the .PrjPcb or in the Git repository root above it, plus
    path= (a pack file, absolute or relative to the project folder). Each has an id,
    title, version, optional rails (net voltages its rules assume) and rules of two kinds.
    Select-and-assert rules select parts, nets or pins (designator, prefix, kind such as
    capacitor, tvs, ic or connector, fitted in the active variant, parameters, text, side,
    case size, net class such as rail, ground, i2c, diff_pair, off_board or external,
    voltage, one net per line) and assert on each (has_parameter, has, parameter_matches,
    matches, count of parts of a kind on the net or its line, connected within N series
    parts, nearest part of a kind on the PCB in mm pad edge to pad edge, rating_ratio of a
    capacitor's rated voltage to the voltage across it, side, case_size, none). Tool rules
    run a read-only tool in-process (run_erc, check_interfaces, check_bom, check_dfm ...)
    with arguments and test its findings or summary with the review checklists'
    conditions (fail, pass, na), so any check becomes a pass/fail gate with the team's
    severity. Python plugins (check_packs/plugins/*.py next to the project) run only with
    allow_plugins=true AND ALTIUM_COPILOT_ALLOW_PLUGINS=1 in the server's environment:
    they are repository code run with the server's permissions, so never pass
    allow_plugins without the user's say-so.

    pack= narrows to pack ids (comma-separated; 'plugins' for the plugin files), rules= to
    rule ids (a disabled rule runs only when named). Findings are ERC-style, errors first:
    check (the rule id), pack, severity, designators, nets, pins, location (sheet, PCB
    side and position in mm) and issue (the rule's message). rules gives each rule's
    status (pass, fail, not_applicable, could_not_evaluate with the reason: no netlist, no
    PCB, a rail of unknown voltage, a tool error), how many objects it checked and could
    not evaluate (no PCB placement, voltage or rating read); every rule keeps a row on
    every page (shortened, rule_detail, when the page must shrink). verdict is fail (an
    error finding), warn, incomplete (a rule could not be evaluated or left objects not
    evaluated) or pass; summary counts findings by pack and severity. A pack problem is
    invalid_check_pack with the file and the place in it. Findings are paginated (offset
    from next_offset); the run is reused for the next pages for 5 minutes while the
    project folder and the pack files are unchanged. The checks are heuristics on names
    and text like the tools they reuse: confirm a finding before changing the design."""
    error, header, model = _custom_run(pack, rules, path, allow_plugins)
    if error is not None:
        return json.dumps(error)
    trailer: dict = {"elapsed_ms": model["elapsed_ms"]}
    if model["tool_runs"]:
        trailer["tool_runs"] = model["tool_runs"]
    for detail, limit in CUSTOM_CHECK_STEPS:
        result = {**header, **page_custom_checks(model, offset, limit, rows=detail),
                  **trailer}
        if (detail, limit) != CUSTOM_CHECK_STEPS[0]:
            result["findings_limited_to"] = limit
        rendered = json.dumps(result, indent=2)
        if len(rendered) <= QUERY_NET_MAX_RESULT_SIZE_CHARS:
            return rendered
    # Not even one finding fits: the verdict and every rule's status, no findings.
    brief = {**header, "verdict": model["verdict"],
             "severity_counts": model["severity_counts"], "summary": model["summary"],
             "rules": [rule_row(row, "minimal") for row in model["rules"]],
             "rule_detail": "minimal", "total": len(model["findings"]),
             "omitted": "The findings do not fit even one per page: call again with pack= "
                        "or rules= to narrow it."}
    brief.pop("packs", None)
    rendered = json.dumps(brief, indent=2)
    if len(rendered) > QUERY_NET_MAX_RESULT_SIZE_CHARS:
        brief.pop("rules")
        brief["rule_status"] = {row["id"]: row["status"] for row in model["rules"]}
        rendered = json.dumps(brief, indent=2)
    return rendered


SCHEMATIC_REVIEW_PROMPT = """\
Project: {name}
Sheets: {sheets}
Active variant: {active_variant}

You are starting a structured schematic review.

First call run_erc once: the offline approximation of Altium's ERC (Altium never saves
its own ERC result; say so). Show its summary (counts by level and violation type) and
its Errors and Fatal Errors in one short table (Violation | Sheet | Object | Message);
mention the warning count and the advisories (near-miss wire ends, rail aliases) without
listing every warning. These are the review's first evidence, not its conclusions.

Then call run_review_checklist(checklist="schematic") once and show its coverage table
and its No items. Auto items are answered by the tools; the needs-judgment items (with
their evidence and lean) are what Phases 1-2 decide. If the project has check packs
(check_packs.json or a check_packs folder), call run_custom_checks once too and add its
failing rules to the evidence: they are the team's own rules.

Then ask the user to choose a scope:
  - A specific sheet (e.g. "{first_sheet}")
  - The full project ({n} sheets, one at a time)

For large projects, consider reviewing each sheet as an independent task dispatched
in parallel — one agent per sheet or functional block. Each agent can self-initialize:
set_project_dir("{root_dir}") → set_active_variant("{active_variant}") →
get_sheet_context(sheet_name="<assigned sheet>") → run_erc(sheet="<assigned sheet>"),
then run Phases 1–2 and return their tables. Merge all results into a single Phase 3
report with an added "Sheet" column.

Do not proceed until the user answers.

---

### Phase 1 — Understand Before You Judge

Call get_sheet_context. Each pin already includes connected_to —
the full cross-sheet neighbor list. Use this to trace every signal
source-to-destination. Call query_net only if you need to go two hops deep on a specific net.

Its design_notes block holds the designer's notes for the sheet. Treat them as stated
intent to verify: a note that contradicts the circuit (a value, a part number, a DNP
reason) is a finding. Look at pictures with get_design_image when they may hold the
reference design or datasheet excerpt the circuit was built from.

For each IC that has external components influencing its operating
behavior, look up its manufacturer datasheet and confirm the parameters
those components set. The datasheet application circuit will tell you
which ones matter.

If you cannot find a datasheet for an IC, say so before proceeding —
do not assume pin functions or parameter values.

Then state explicitly:
  - What you believe this circuit does
  - What each key IC's operating parameters are, sourced from its datasheet
  - Anything you cannot determine from the netlist or datasheets —
    name it, ask about it

Done when: the user has confirmed or corrected your understanding.
Do not proceed to Phase 2 until then.

---

### Phase 2 — Audit (Evidenced Issues Only)

Assume the designer made mistakes. Your job is to find them.

Ground truth rule: the manufacturer datasheet overrides everything —
Altium pin names, net labels, user descriptions. Every finding must
trace to a datasheet value or a measurable netlist fact.
Report only what you can evidence. Do not speculate.

At minimum, check:
1. Pin assignments — does each IC pin connect to what the datasheet
   application circuit requires?
2. Passive values — do component values produce correct operating
   parameters per datasheet formulas?
3. Signal continuity — use connected_to on each critical pin to trace
   source-to-destination; verify correct termination and that every
   expected endpoint appears
4. Cross-sheet nets — use query_net with broad patterns to enumerate
   all cross-sheet references; flag dangling or unexpected appearances
5. Failure modes — for each sub-circuit: what happens if inputs are out
   of range, or a passive is off by 10%?
6. ERC — run_erc(sheet="<sheet>", min_level="warning") for the sheet under review:
   explain each Error and each connectivity warning (floating net labels or power
   ports, unconnected wires, single-pin nets, ports or sheet entries with no match,
   near_miss_wire_ends) from the circuit, and say whether it is a real fault or
   intended (an intended one wants a No ERC or a level change, not a redesign).
   Off-grid and off-sheet warnings are housekeeping: count them, do not list them.
7. Power — get_power_tree (rail= or part= for the sheet's rails and regulators): how each
   rail is fed, each regulator's EN, PG and sequencing, its feedback divider against the
   datasheet's Vref (vref=), input and output capacitors, input protection and SMPS input
   filters. Its states are read from names: confirm each against the datasheet before
   calling it a fault.
8. Ratings — check_derating (net= for the sheet's rails, rails='VIN=24' for inputs whose
   names carry no voltage): capacitor voltage ratings against the rail (ripple nets
   need 2x), DC bias on small ceramics, resistor power and working voltage, and the
   LED and shunt currents to look up. Confirm each rating in the datasheet.
9. Logic levels — check_logic_levels (part= for the sheet's ICs, rails= for I/O rails
   whose names carry no voltage): drivers whose rail is below a receiver's VIH, inputs
   driven or pulled up above their supply + 0.3 V, FPGA banks and translator sides.
   Confirm VOH, VIH and absolute maximum ratings in both datasheets.
10. Calculations — get_circuit_calculations (part= or net= for the sheet): recalculate
   every divider (FB against the datasheet's Vref, EN/UVLO trip voltages against its
   threshold, ADC inputs within their rail), LED and optocoupler currents against their
   ratings (Vf from the datasheet), and a filter on every ADC input. Results are
   calculations from datasheet-dependent inputs, not verdicts.

For circuit types that require it, apply first principles beyond this
list. The topology determines what matters.

Done when: you have worked through the checklist and any additional
checks the circuit demands. Do not proceed to Phase 3 until then.

---

### Phase 3 — Document

Present findings as three Markdown tables:

  Table 1 — Critical Issues
  | Component | Issue | Datasheet Requires | Schematic Shows |

  Table 2 — Warnings & Nitpicks (ERC warnings judged in Phase 2 included)
  | Component | Observation |

  Table 3 — Verified Critical Nets
  | Net | Source → Destination | Result |

Finish with the checklist record: run_review_checklist(checklist="schematic",
format="markdown") with decisions= giving your Yes, No or N/A and a one-line reason for
each needs-judgment item you settled. Show its 'Checklist coverage' table and the record
(tied to the design state it names); save it only where the user agrees.

Ask: "Any sub-circuits to dive deeper into, or alternative
architectures to brainstorm?"
"""


@mcp.tool(title="Constraints Review", annotations=ToolAnnotations(readOnlyHint=True))
def constraints_review() -> str:
    """Start a structured review of the PCB's high-speed constraints: finds the board's
    interfaces from parts and pins (USB, PCIe, Ethernet, DDR, HDMI/DP, MIPI, LVDS, SATA,
    CAN, RS-485, SerDes), checks pairs, classes, impedance-driven rules, matched-length
    groups, clearances, reference layers and stale rules against each chip's layout
    guide, then spot-checks the saved copper, with the pcb checklist's high-speed items
    (run_review_checklist) and their review record. Call when the user asks to review or verify
    the layout's constraints, design rules or high-speed routing ('check my high-speed
    rules', 'are my DDR length rules right?', 'review the diff pairs before fab'). Not for
    one net's rule (get_rules_for_net) or a schematic review (schematic_review)."""
    try:
        project, netlist, variant_state = _require_project(netlist=False)
    except ProjectNotReady:
        return json.dumps(REVIEW_NO_PROJECT_ERROR)
    index, error = _pcb_index_or_error()
    if error:
        error["hint"] = (
            "constraints_review needs the project's PcbDoc; for the circuit itself "
            "use schematic_review."
        )
        return json.dumps(error)
    review = build_constraints_review(project, variant_state.active.name, index)
    if netlist is None:
        review += CONSTRAINTS_REVIEW_NETLIST_FREE_NOTE
    return review


@mcp.tool(title="PCB Manufacturing Review", annotations=ToolAnnotations(readOnlyHint=True))
def pcb_manufacturing_review() -> str:
    """Start a structured manufacturing-readiness review of the saved PCB before ordering:
    fabrication against the user's fabricator (check_dfm with its profile, check_vias,
    check_pcb_health, check_connectivity, get_drc_report), then assembly (check_dfm's
    assembly checks, get_bom and check_bom), ending in a go / no-go that lists what was
    not checked and the review records of the pcb manufacturing and testability and the
    bom checklists (run_review_checklist). It asks which fabricator first and never presents Altimade's limits as
    another fab's. Call when the user asks for a manufacturing, DFM/DFA or production
    readiness review ('is this board ready to order?', 'review it for manufacturing',
    'go/no-go before fab', 'can it be built and assembled?'). Not for one check against a
    fab profile (check_dfm), the high-speed constraints (constraints_review) or the
    circuit (schematic_review)."""
    try:
        project, netlist, variant_state = _require_project(netlist=False)
    except ProjectNotReady:
        return json.dumps(REVIEW_NO_PROJECT_ERROR)
    index, error = _pcb_index_or_error()
    if error:
        error["hint"] = (
            "pcb_manufacturing_review needs the project's PcbDoc; for the parts list alone "
            "use get_bom and check_bom."
        )
        return json.dumps(error)
    return build_manufacturing_review(
        project, variant_state.active.name, index, netlist_free=netlist is None,
    )


@mcp.tool(title="Release Review", annotations=ToolAnnotations(readOnlyHint=True))
def release_review() -> str:
    """Start a release go / no-go review of the loaded project, modelled on Altium's
    Project Releaser: run check_release_readiness, drill into each failing gate with its
    source tool, then give a go / no-go with the blocking items, what was not verified
    (never reported as passed) and the warnings, with the release checklist
    (run_review_checklist) and its review record. Call when the user asks whether the
    design is ready to release, for review or for fabrication ('is this ready to
    release?', 'release check', 'go/no-go before release', 'can I send this to fab?').
    Not for one fabricator's capabilities (pcb_manufacturing_review, check_dfm) or the
    circuit (schematic_review)."""
    try:
        project, netlist, variant_state = _require_project(netlist=False)
    except ProjectNotReady:
        return json.dumps(REVIEW_NO_PROJECT_ERROR)
    return build_release_review(project, variant_state.active.name,
                                netlist_free=netlist is None)


REVIEW_NO_PROJECT_ERROR = {
    "error": "no_project",
    "message": (
        "No project loaded. Run the session-start flow first: "
        "detect_altium_project → set_project_dir → list_variants → set_active_variant."
    ),
}

CONSTRAINTS_REVIEW_NETLIST_FREE_NOTE = """
---

Netlist-free load: this project was loaded without a netlist (Altium Designer was not
running), so search_components, get_component and query_net answer netlist_required.
Find the interfaces from the board instead: list_diff_pairs, get_classes and net names,
then get_component_placement for the parts on those nets (its pins list each pad's net).
The PCB holds no part descriptions or pin names: ask the user which parts are the
interface ICs, and say in the report that pins were not checked against the schematic.
"""


@mcp.tool(title="Schematic Review", annotations=ToolAnnotations(readOnlyHint=True))
def schematic_review() -> str:
    """Start a structured schematic review: run_erc first (the offline approximation of
    Altium's ERC, its errors shown before anything else) and the schematic checklist
    (run_review_checklist), then a scope question, understanding, an evidenced audit per
    sheet (ERC findings judged from the circuit), three tables and the checklist's review
    record. Call when the user explicitly asks to review or verify the
    schematic (e.g. 'review this', 'check my schematic', 'do a design review', 'is this
    correct?', 'does this look right?'). Do not call for general questions."""
    try:
        _require_project()
    except ProjectNotReady as e:
        if e.payload["error"] == "netlist_required":
            return json.dumps(e.payload)
        return json.dumps(REVIEW_NO_PROJECT_ERROR)

    sheets = [s["name"] for s in _project["sheets"]]
    active_variant = _variant_state.active.name
    first_sheet = sheets[0] if sheets else "Power_Supply"
    n = len(sheets)
    name = _project["name"]

    prompt = SCHEMATIC_REVIEW_PROMPT.format(
        name=name,
        sheets=", ".join(sheets),
        active_variant=active_variant,
        first_sheet=first_sheet,
        n=n,
        root_dir=_project["root_dir"],
    )
    if _offline(_altium._netlist):
        prompt += (
            "\n\nNote: this project's netlist was compiled offline by the copilot from the "
            "saved sheets (netlist_source offline_compiler): Altium's compiler and ERC did not "
            "run (run_erc is the offline approximation). Say so in the review and pass on the "
            "load's compile_warnings.")
    return prompt


@mcp.tool(title="Project Tour", annotations=ToolAnnotations(readOnlyHint=True))
def project_tour() -> str:
    """Start a guided tour of the loaded project for a newcomer, in the order Altium
    teaches a design: project and sheet hierarchy (get_design_hierarchy, list_variants),
    each schematic block and the power rails, the PCB (stackup and impedance, placement,
    key nets), outputs and manufacturing state (DRC report, health, BOM), then variants
    and design notes. It ends in a Markdown project primer with a fixed outline, which
    Claude shows and saves only where the user agrees. Adapts to the load: a
    netlist-free load tours what the files show, a project without a PcbDoc skips the
    board, a PCB-only project the schematic. Call when the user is new to the project or
    asks for a walk-through ('walk me through this design', 'give me a tour', 'onboard
    me', 'write a project primer'). Not
    for a quick summary (the session start covers it) or a review (schematic_review,
    constraints_review, pcb_manufacturing_review)."""
    try:
        project, netlist, variant_state = _require_project(netlist=False)
    except ProjectNotReady:
        return json.dumps(REVIEW_NO_PROJECT_ERROR)
    return build_project_tour(
        project, variant_state.active.name, defined_variant_count(variant_state.variants),
        netlist_free=netlist is None,
    )



BRAINSTORM_CIRCUITS_PROMPT = """\
You are starting a structured circuit brainstorming session.
Follow these phases in order. Ask one question at a time — never more than one per message.

## Ground Rules

- Do not state component values, voltage ratings, current limits, pin assignments, or any other
  specs from memory. If you need to reference a specific part, use web search to find its datasheet first.
- Do not recommend a specific component unless you have verified its key specs from a datasheet
  or authoritative source during this session.
- If you are uncertain about a topology's behavior or limitations, say so and search before advising.

---

## Phase 0 — Context (skip if no project is loaded)

If a project is loaded, use the available tools before asking the user anything:
- Call get_sheet_context on the active or most relevant sheet.
- If the user mentioned a specific sub-circuit or net, call query_net and get_component to understand it.
- Summarize what is already on the relevant sheet(s) in one short paragraph: what the circuit does,
  key components, and any obvious constraints visible in the design.

If no project is loaded, skip to Phase 1.

---

## Phase 1 — Problem statement

Ask the user one open-ended question: "What does this circuit need to do?"

Do not assume a topology, domain, or electrical spec yet. If the answer is vague or could be
interpreted multiple ways, name what is confusing and ask again before moving on.

---

## Phase 2 — Constraints (one question at a time)

Based on the circuit type that emerged from Phase 1, identify which specs would most change your
topology recommendation. Ask about them one at a time — do not send a list of questions.

Stop asking once you have enough to propose meaningful options. State any assumptions you are
making before moving to Phase 3.

---

## Phase 3 — Propose 2–3 topologies

Lead with the simplest viable option. For each topology:
- Name it clearly.
- List key tradeoffs: complexity, cost, parts count, performance.
- Give one reason to choose it and one reason to skip it.

State your assumptions explicitly. Do not recommend an approach before completing Phase 2.

---

## Phase 4 — Fit check (skip if no project is loaded)

Before the user commits to an approach, check the existing design:
- Use search_components to find components that might already serve a similar role.
- Use query_net to check whether relevant power rails or signal nets already exist.

Do not guess from memory — verify with tools. If the design already has something relevant, say so.

---

## Phase 5 — Design summary

Once the user selects an approach:
- Summarize: topology chosen, key specs agreed, suggested components with brief reasoning.
- List any open questions that remain before layout can begin.

Do not suggest implementation steps or specific part numbers until the user approves this summary.
"""


@mcp.tool(title="Brainstorm Circuits", annotations=ToolAnnotations(readOnlyHint=True))
def brainstorm_circuits() -> str:
    """Start a structured circuit brainstorming session. Call when the user is asking
    how to design, improve, or choose an approach for a circuit — not just when they
    say "brainstorm". Works with or without a loaded project."""
    return BRAINSTORM_CIRCUITS_PROMPT


# ---------- package_for_xfn ----------

def _firmware_rows(project: dict, netlist: dict, variant_state) -> tuple[list[dict], str | None]:
    """The firmware_pins table's rows for the active variant, and why not when the map
    could not be built (the snapshot is written either way)."""
    try:
        plan = plan_variant(getattr(variant_state, "active", None), netlist,
                            variant_names(netlist))
        return firmware_export_rows(netlist, plan, project.get("parameters") or {}), None
    except Exception as error:  # a heuristic must never block the snapshot
        logging.warning("Firmware pin map not built for the snapshot: %s", error)
        return [], f"{type(error).__name__}: {error}"


def _mechanical_rows(project: dict, netlist: dict, variant_state,
                     index) -> tuple[dict | None, str | None]:
    """The mech_* tables' rows of the snapshot (get_mechanical_handoff of the PcbDoc), and
    why not when they could not be built (the snapshot is written either way)."""
    try:
        inputs = _mechanical_inputs(project, netlist, variant_state, index, notes=False)
        handoff = prepare_handoff(index, is_dnp=inputs["is_dnp"], texts=inputs["texts"],
                                  library_heights=inputs["library_heights"])
        paths = project.get("pcb_doc_paths") or []
        return mechanical_export_rows(handoff, Path(paths[0]).name if paths else None), None
    except Exception as error:  # the handoff must never block the snapshot
        logging.warning("Mechanical tables not built for the snapshot: %s", error)
        return None, f"{type(error).__name__}: {error}"


def _bom_doc_rows(project: dict, netlist: dict, variant_state) -> tuple[dict | None, str | None]:
    """The bom_solutions and bom_non_pcb_lines tables' rows of the snapshot (the active
    variant's ActiveBOM), and why not when there is no readable BomDoc (the snapshot is
    written either way)."""
    try:
        plan = plan_variant(getattr(variant_state, "active", None), netlist,
                            variant_names(netlist))
        parts, _ = build_bom_parts(netlist, plan, project.get("parameters") or {})
        model, error = _active_bom_model(project, netlist, plan, parts)
        if model is None:
            # Only a BomDoc that is there but unreadable is worth a warning.
            unreadable = (error or {}).get("error") == "bom_doc_unreadable"
            return None, (error or {}).get("message") if unreadable else None
        return active_bom_export_rows(model), None
    except Exception as error:  # the BomDoc must never block the snapshot
        logging.warning("BomDoc tables not built for the snapshot: %s", error)
        return None, f"{type(error).__name__}: {error}"


def _package_for_xfn_impl(
    project: dict,
    netlist: dict,
    variant_state,
    version: str,
    notes_cache: DesignNotesCache | None = None,
    pcb_index=None,
    pcb_error: str | None = None,
) -> str:
    # Never ship a hollow snapshot: nets present but no component metadata.
    no_components = netlist.get("pin_to_net") and _no_components_error(netlist)
    if no_components:
        return json.dumps(no_components)
    notes, notes_errors = note_rows(notes_cache, project) if notes_cache is not None else ([], [])
    firmware, firmware_error = _firmware_rows(project, netlist, variant_state)
    try:
        variations = variant_export_rows(getattr(variant_state, "variants", []), netlist)
    except Exception as error:  # never block the snapshot
        logging.warning("Variant variations not exported: %s", error)
        variations = []
    mechanical, mechanical_error = (None, pcb_error)
    if pcb_index is not None:
        mechanical, mechanical_error = _mechanical_rows(project, netlist, variant_state,
                                                        pcb_index)
    bom_doc, bom_doc_error = (None, None)
    if project.get("prj_pcb_path") and netlist.get("components"):
        bom_doc, bom_doc_error = _bom_doc_rows(project, netlist, variant_state)
    try:
        db_path = export_project(project, netlist, variant_state, version, notes, firmware,
                                 variations, mechanical, bom_doc)
    except Exception as e:
        return json.dumps({"error": "export_failed", "message": str(e)})
    prj_pcb_name = Path(project["prj_pcb_path"]).name
    incomplete_warning = "".join(
        f"\nWarning: sheet '{err['sheet']}' failed to load ({err['error']}); "
        "its components are missing from this snapshot."
        for err in netlist.get("sheet_errors") or []
    )
    incomplete_warning += "".join(
        f"\nWarning: the notes of {err['document_kind']} '{err['document']}' could not be read "
        f"({err['error']}); they are missing from this snapshot."
        for err in notes_errors
    )
    if firmware:
        mcus = list(dict.fromkeys(row["mcu"] for row in firmware))
        incomplete_warning += (
            f"\nFirmware pin map: {len(firmware)} pins of {', '.join(mcus)} in the "
            "firmware_pins table (a heuristic from pin and net names: functions and ports are "
            "a starting point; get_firmware_pin_map reviews them).")
    elif firmware_error:
        incomplete_warning += (
            f"\nWarning: the firmware pin map could not be built ({firmware_error}); the "
            "firmware_pins table is empty.")
    if mechanical:
        incomplete_warning += (
            f"\nMechanical handoff: the board outline, {len(mechanical['mech_mounting_holes'])} "
            f"mounting holes, {len(mechanical['mech_connectors'])} connectors, "
            f"{len(mechanical['mech_regions'])} regions and {len(mechanical['mech_parts'])} "
            "part heights in the mech_* tables (mm, relative to the board origin; plan view, "
            "not a STEP or IDX replacement).")
    elif mechanical_error:
        incomplete_warning += (
            f"\nWarning: the mechanical tables could not be built ({mechanical_error}); the "
            "mech_* tables are empty.")
    if bom_doc:
        incomplete_warning += (
            f"\nActiveBOM: {len(bom_doc['bom_solutions'])} solution and supplier rows in "
            f"bom_solutions and {len(bom_doc['bom_non_pcb_lines'])} non-PCB lines in "
            "bom_non_pcb_lines (stock, price and lifecycle as cached in the BomDoc, with "
            "their cache dates).")
    elif bom_doc_error:
        incomplete_warning += (
            f"\nWarning: the project's BomDoc could not be read ({bom_doc_error}); the bom_* "
            "tables are empty.")
    if _offline(netlist):
        incomplete_warning += (
            "\nNote: the nets in this snapshot were compiled offline by the copilot from the "
            "saved sheets (Altium Designer was not running, so no Altium compile and no ERC "
            "ran); tell the team, or export again once the project is open in Altium.")
    registry_warning = ""
    try:
        mark_xfn_exported(prj_pcb_name)
    except Exception as error:
        logging.warning(
            "Snapshot exported but registry update failed: %s",
            error,
        )
        registry_warning = (
            "\nWarning: snapshot export succeeded, but the local registry "
            f"could not be updated: {error}"
        )
    return (
        f"Exported to: {db_path}\n"
        "Share this file with your cross-functional team via Slack or a shared drive.\n"
        "Do not commit it to Git — it is a binary snapshot and will cause repository bloat."
        f"{incomplete_warning}"
        f"{registry_warning}"
    )


@mcp.tool(
    title="Package for Cross-Functional Team",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False),
)
def package_for_xfn() -> str:
    """Export the current schematic project to a portable .db snapshot file for the
    cross-functional team. Only call this tool when the user explicitly asks to package
    or export the project for sharing with firmware, mechanical, test, or reliability
    engineers. Do not call speculatively. The snapshot holds sheets, variants (and their
    varied parameters and alternate parts: variant_variations), components,
    nets, pins, design notes, the firmware pin map (firmware_pins: pin, name, port, bit,
    net, function, role, pull, the parts each pin reaches; a heuristic) of each fitted MCU,
    radio or module candidate of get_firmware_pin_map in the active variant (at most four,
    else its top candidate) and, with a PcbDoc, the mechanical handoff of
    get_mechanical_handoff for the active variant (mm, relative to the board origin):
    mech_board (thickness, size, area, per-side summaries), mech_outline (outline, cutouts,
    rigid-flex board regions), mech_mounting_holes (drill, plated, net, screw, free
    radius, fitted), mech_connectors (position, rotation, side, edge, offset, overhang, facing),
    mech_regions (keepouts, rooms, free 3D bodies) and mech_parts (each part's height and
    source, standoff and extent; Not Fitted parts with fitted 0) and, with an ActiveBOM
    document, bom_solutions (per line its ranked solutions and their part choices:
    manufacturer, MPN, supplier, SKU, stock, unit price, lifecycle and cache date, for
    the active variant) and bom_non_pcb_lines (BOM items with no design parts)."""
    try:
        project, netlist, variant_state = _require_project()
    except ProjectNotReady as e:
        if e.payload["error"] == "netlist_required":
            return json.dumps(e.payload)
        return json.dumps({
            "error": "no_project",
            "message": "No project loaded. Ask the user to open a project in Altium first.",
        })
    pcb_index, pcb_error = None, None
    if project.get("pcb_doc_paths"):
        pcb_index, error = _pcb_session.get(project, netlist)
        if pcb_index is None:
            pcb_error = (error or {}).get("message") or "the PcbDoc could not be read"
    return _package_for_xfn_impl(project, netlist, variant_state, _read_version(), _design_notes,
                                 pcb_index, pcb_error)


# ---------- entry point ----------

def _serve() -> None:
    """Run the MCP server on stdio (and the daily release check)."""
    threading.Thread(target=_check_for_update, args=(_read_version(),), daemon=True).start()
    try:
        mcp.run()
    finally:
        # After the MCP stdio transport closes, Python's exit sequence tries to
        # flush sys.stdout, which is now a closed pipe → ValueError.  Redirect to
        # devnull so the process exits cleanly and Claude Desktop doesn't see a
        # crashed server on next load.
        try:
            sys.stdout = open(os.devnull, "w")
            sys.stderr = open(os.devnull, "w")
        except Exception:
            pass


def run_entry(argv: list[str]) -> int:
    """The executable's entry point: 'check ...' runs the headless checks (cli.py, roadmap
    #50: no MCP server, no update check, no registry or state file), --version prints the
    version, anything else starts the MCP server exactly as before."""
    if argv[:1] == ["check"]:
        try:
            import cli
        except ImportError:
            from server import cli
        return cli.main(argv[1:], server=sys.modules[__name__])
    if "--version" in argv:
        print(f"altium-copilot-kc v{_read_version()}")  # noqa: T201
        return 0
    _serve()
    return 0


if __name__ == "__main__":
    sys.exit(run_entry(sys.argv[1:]))
