"""Loads without Altium: set_project_dir without Altium and without an up-to-date cached
netlist compiles the netlist offline from the saved sheets, and every tool works
(offline_env). Only when that compile fails or there are no sheets is the load
netlist-free (env): PCB, rule, notes, picture and variant tools work and the netlist
tools answer netlist_required."""
import asyncio
import json
import os
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest
from mcp.server.fastmcp import Image

import server.main as main
from altium import AltiumClient
from parsers.prj_pcb import VariantDefinition, VariantState
from server.services.pcb_index import PcbIndex
from tests.draftsman_fixture import FIXTURE as DRAWING
from tests.test_pcb_index import NETLIST, pcb_data  # noqa: F401

FIXTURES = Path(__file__).parent / "fixtures"

# R1 is Not Fitted in the Prod variant; its UniqueId tail matches the PCB part.
PRJ_TEXT = (
    "[Design]\nVersion=1.0\n"
    "[Document1]\nDocumentPath=source\\sheet1.SchDoc\n"
    "[Document2]\nDocumentPath=source\\sheet2.SchDoc\n"
    "[Document3]\nDocumentPath=Board.PcbDoc\n"
    "[Document4]\nDocumentPath=Fab.PCBDwf\n"
    "[ProjectVariant1]\nDescription=Prod\nVariationCount=1\n"
    "Variation1=Designator=R1|UniqueId=\\ABCDEFGH\\RRRR|Kind=1|AlternatePart=\n"
)

# Tools that need no project at all, or load one.
LOADERS = {"detect_altium_project", "set_project_dir", "refresh_netlist", "brainstorm_circuits"}

NETLIST_TOOL_CALLS = {
    "query_net": lambda: main.query_net("GND"),
    "get_component": lambda: main.get_component("R1"),
    "search_components": lambda: main.search_components("R"),
    "get_sheet_context": lambda: main.get_sheet_context("sheet1"),
    "get_bom": lambda: main.get_bom(),
    "check_bom": lambda: main.check_bom(),
    "get_active_bom": lambda: _get_active_bom(),
    "check_decoupling": lambda: main.check_decoupling(),
    "check_pull_resistors": lambda: main.check_pull_resistors(),
    "check_interfaces": lambda: main.check_interfaces(),
    "get_power_tree": lambda: main.get_power_tree(),
    "check_derating": lambda: main.check_derating(),
    "check_logic_levels": lambda: main.check_logic_levels(),
    "get_circuit_calculations": lambda: main.get_circuit_calculations(),
    "check_thermal": lambda: main.check_thermal(),
    "check_hv_clearance": lambda: main.check_hv_clearance(),
    "check_power_paths": lambda: main.check_power_paths(),
    "get_firmware_pin_map": lambda: main.get_firmware_pin_map(),
    "run_erc": lambda: main.run_erc(),
    "check_sch_pcb_sync": lambda: main.check_sch_pcb_sync(),
    "check_directive_sync": lambda: main.check_directive_sync(),
    "get_constraints_brief": lambda: main.get_constraints_brief(),
    "generate_rule_file": lambda: main.generate_rule_file(),
    "schematic_review": lambda: main.schematic_review(),
    "package_for_xfn": lambda: main.package_for_xfn(),
    "get_db_part": lambda: main.get_db_part("R1"),
    "check_db_parameters": lambda: main.check_db_parameters(),
}


def _get_active_bom() -> str:
    """get_active_bom on a BomDoc in the project folder that the project does not list
    (the fixture's project lists none), read by name."""
    if main._project is not None:
        (Path(main._project["root_dir"]) / "Spare.BomDoc").write_bytes(
            b"|RECORD=BOM|VERSION=2|PRODUCTIONQUANTITY=1\r\n|RECORD=Catalog|DBLINKFILEPATH=\r\n")
    return main.get_active_bom(name="Spare.BomDoc")


WORKING_TOOL_CALLS = {
    "get_board_info": lambda: main.get_board_info(),
    "get_net_pcb": lambda: main.get_net_pcb("CLK_A"),
    "get_net_neighbors": lambda: main.get_net_neighbors("CLK_A"),
    "query_pcb_region": lambda: main.query_pcb_region(100.0, 100.0),
    "get_component_placement": lambda: main.get_component_placement("R1"),
    "get_board_view": lambda: main.get_board_view(nets="CLK_A", parts="R1"),
    "get_board_view_geometry": lambda: main.get_board_view_geometry(
        json.loads(main.get_board_view())["view"]["geometry_rev"]),
    "get_design_rules": lambda: main.get_design_rules(),
    "get_rules_for_net": lambda: main.get_rules_for_net("CLK_A"),
    "get_classes": lambda: main.get_classes(),
    "list_diff_pairs": lambda: main.list_diff_pairs(),
    "check_rule_compliance": lambda: main.check_rule_compliance(),
    "check_lengths": lambda: main.check_lengths(),
    "get_signal_length": lambda: main.get_signal_length("CLK_A"),
    "check_diff_pairs": lambda: main.check_diff_pairs(),
    "check_vias": lambda: main.check_vias(),
    "check_return_path": lambda: main.check_return_path(),
    "check_diff_pair_geometry": lambda: main.check_diff_pair_geometry(),
    "check_mechanical_fit": lambda: main.check_mechanical_fit(),
    "get_mechanical_handoff": lambda: main.get_mechanical_handoff(),
    "check_testpoints": lambda: main.check_testpoints(),
    "check_connectivity": lambda: main.check_connectivity(),
    "check_pcb_health": lambda: main.check_pcb_health(),
    "check_dfm": lambda: main.check_dfm(),
    "get_assembly_guide": lambda: main.get_assembly_guide(),
    "get_drc_report": lambda: main.get_drc_report(),
    "constraints_review": lambda: main.constraints_review(),
    "pcb_manufacturing_review": lambda: main.pcb_manufacturing_review(),
    "project_tour": lambda: main.project_tour(),
    "get_design_notes": lambda: main.get_design_notes(),
    "get_design_image": lambda: main.get_design_image("Fab.PCBDwf"),
    "get_design_hierarchy": lambda: main.get_design_hierarchy(),
    "get_project_options": lambda: main.get_project_options(),
    "check_project_integrity": lambda: main.check_project_integrity(),
    "get_output_jobs": lambda: main.get_output_jobs(),
    "check_fab_drawing": lambda: main.check_fab_drawing(),
    "verify_outputs": lambda: main.verify_outputs(),
    "get_library_part": lambda: main.get_library_part("R1"),
    "check_libraries": lambda: main.check_libraries(),
    # A library symbol needs no netlist: one installed library holding it.
    "check_pin_table": lambda: _check_pin_table(),
    "check_release_readiness": lambda: main.check_release_readiness(),
    "release_review": lambda: main.release_review(),
    "run_review_checklist": lambda: main.run_review_checklist("pcb"),
    # The example pack: its select rules say netlist_required, its tool rule runs.
    "run_custom_checks": lambda: main.run_custom_checks(
        path=str(Path(__file__).parents[1] / "examples" / "check_packs.json")),
    # The project folder against itself: no Git clone is needed in folder mode.
    "diff_design": lambda: main.diff_design(project_dir_a=main._project["root_dir"]),
    "list_variants": lambda: main.list_variants(),
    "set_active_variant": lambda: main.set_active_variant("Prod"),
    "compare_variants": lambda: main.compare_variants("Prod"),
    "get_clone_status": lambda: main.get_clone_status(),
    "trace_net_across_boards": lambda: main.trace_net_across_boards("GND"),
    "check_multiboard": lambda: main.check_multiboard(),
    "get_system_connections": lambda: main.get_system_connections(),
}


def _check_pin_table() -> str:
    """check_pin_table on a made-up symbol of an installed library written beside the
    project (the environment restores the installed library list afterwards)."""
    from tests.library_fixture import binary_pin, sch_component, sch_lib_streams, write_ole

    library = Path(main._project["root_dir"]) / "Installed.SchLib"
    write_ole(library, sch_lib_streams(("PARTX", sch_component(
        "PARTX", [binary_pin("1", "IN"), binary_pin("2", "OUT")]))))
    main._installed_library_paths = [str(library)]
    return main.check_pin_table(lib_reference="PARTX", table="Pin\tName\n1\tIN\n2\tOUT\n")


def _write_project(root: Path, prj_text: str = PRJ_TEXT) -> Path:
    src = root / "source"
    src.mkdir(parents=True)
    for name in ("sheet1.SchDoc", "sheet2.SchDoc"):
        shutil.copy(FIXTURES / name, src / name)
    shutil.copy(DRAWING, root / "Fab.PCBDwf")
    (root / "Board.PcbDoc").write_bytes(b"fake")
    prj = root / "Board.PrjPcb"
    prj.write_text(prj_text, encoding="utf-8")
    return prj


COMPILE_ERROR = "RuntimeError: sheets unreadable"


def _environment(tmp_path, pcb_data, compiles: bool):
    """Isolated main state, Altium not running, an empty registry, and a PcbDoc that
    parses to the synthetic pcb_data. loads records the netlist each PCB parse gets.
    compiles False makes the offline compile fail, so loads are netlist-free."""
    root = tmp_path / "Board"
    _write_project(root)
    loads: list = []

    def load(path, netlist):
        loads.append(netlist)
        return PcbIndex(pcb_data, netlist)

    session = main.PcbSession()
    session._load = load
    with patch.object(main, "_project", None), \
         patch.object(main, "_variant_state", None), \
         patch.object(main, "_netlist_last_updated", None), \
         patch.object(main, "_pcb_session", session), \
         patch.object(main, "_design_notes", main.DesignNotesCache()), \
         patch.object(main, "_installed_library_paths", []), \
         patch.object(main, "_altium", AltiumClient()), \
         patch.object(main, "read_registry", return_value={"projects": []}), \
         patch.object(main, "upsert_registry_entry") as upsert, \
         patch.object(main._altium, "get_status", return_value={"running": False}):
        if compiles:
            yield root, upsert, loads
        else:
            with patch.object(main._altium, "compile_offline",
                              side_effect=RuntimeError("sheets unreadable")):
                yield root, upsert, loads


@pytest.fixture
def env(tmp_path, pcb_data):
    """Altium not running and the offline compile failing: a netlist-free load."""
    yield from _environment(tmp_path, pcb_data, compiles=False)


@pytest.fixture
def offline_env(tmp_path, pcb_data):
    """Altium not running: the sheets are compiled offline."""
    yield from _environment(tmp_path, pcb_data, compiles=True)


def _load(root: Path) -> dict:
    return json.loads(main.set_project_dir(str(root)))


def test_load_without_altium_is_partial(env):
    root, upsert, _ = env
    result = _load(root)

    assert result["loaded"] is True
    assert result["partial"] is True
    assert result["load_mode"] == "netlist_free"
    assert result["project"] == "Board"
    assert result["sheets"] == ["sheet1", "sheet2"]
    assert result["variants"] == ["Prod"]
    assert result["pcb_doc"] == "Board.PcbDoc"
    assert result["draftsman_documents"] == ["Fab.PCBDwf"]
    assert result["netlist_updated_utc"] is None
    assert result["netlist_required_tools"] == list(main.NETLIST_TOOLS)
    assert "Altium Designer is not running" in result["message"]
    assert "could not be compiled offline" in result["message"]
    assert result["compile_error"] == COMPILE_ERROR
    assert "get_sheet_context" in result["session_start"]
    assert "netlist_stale" not in result
    assert main._altium._netlist is None
    # Registered without a netlist mtime, so a later cache shortcut is never faked.
    upsert.assert_called_once_with("Board.PrjPcb", str(root))


def test_load_reads_project_context(env):
    root, _, _ = env
    (root / "CLAUDE.md").write_text("# Board notes", encoding="utf-8")
    assert _load(root)["project_context"] == "# Board notes"


def test_every_tool_is_classified():
    registered = {tool.name for tool in asyncio.run(main.mcp.list_tools())}
    assert set(main.NETLIST_TOOLS) == set(NETLIST_TOOL_CALLS)
    assert registered == LOADERS | set(NETLIST_TOOL_CALLS) | set(WORKING_TOOL_CALLS)


@pytest.mark.parametrize("tool", sorted(NETLIST_TOOL_CALLS))
def test_netlist_tools_answer_netlist_required(env, tool):
    root, _, _ = env
    _load(root)
    result = json.loads(NETLIST_TOOL_CALLS[tool]())
    assert result["error"] == "netlist_required"
    assert "Project 'Board' was loaded without a netlist" in result["message"]
    assert "the sheets could not be compiled offline" in result["message"]
    assert "open the project in Altium Designer once" in result["message"]


@pytest.mark.parametrize("tool", sorted(WORKING_TOOL_CALLS))
def test_other_tools_work_without_netlist(env, tool):
    root, _, loads = env
    _load(root)
    result = WORKING_TOOL_CALLS[tool]()
    if isinstance(result, Image):
        return
    try:
        error = json.loads(result).get("error")
    except ValueError:  # the review tools return a prompt, not JSON
        error = None
    # There is no DRC report in the project folder, it is no Altium 365 clone and no
    # multi-board project lists it; everything else answers.
    assert error in (None, "no_drc_report", "not_an_altium365_clone", "no_multiboard_system")
    assert all(netlist is None for netlist in loads)


def test_board_info_says_parts_are_not_linked(env):
    root, _, _ = env
    _load(root)
    result = json.loads(main.get_board_info())
    assert "netlist-free load" in result["netlist"]
    assert "refdes_mapping_unmatched" not in result
    assert result["netlist_updated_utc"] is None


def test_dnp_comes_from_the_variants_stored_designators(env):
    root, _, _ = env
    _load(root)
    main.set_active_variant("Prod")
    assert json.loads(main.list_variants())["variants"][0]["dnp_components"] == ["R1"]
    placement = json.loads(main.get_component_placement("R1"))["instances"][0]
    assert placement["sch_refdes"] is None
    assert placement["dnp"] is True


def test_design_notes_and_pictures(env):
    root, _, _ = env
    _load(root)
    notes = main.get_design_notes()
    assert "sheet:sheet1 notes:" in notes
    assert "draftsman:Fab.PCBDwf" in notes
    assert isinstance(main.get_design_image("Fab.PCBDwf"), Image)


def test_fab_drawing_checks_all_but_the_bom_parts(env):
    root, _, _ = env
    _load(root)
    result = json.loads(main.check_fab_drawing())
    assert result["drawings_total"] == 1 and result["details"][0]["kind"] == "fabrication"
    assert result["boards"][0]["pcb_doc"] == "Board.PcbDoc"
    assert [n["check"] for n in result["not_checked"]] == ["bom_parts"]
    assert "no netlist is loaded" in result["not_checked"][0]["reason"]


def test_constraints_review_explains_the_missing_netlist(env):
    root, _, _ = env
    _load(root)
    review = main.constraints_review()
    assert "Netlist-free load" in review
    assert "get_component_placement" in review
    manufacturing = main.pcb_manufacturing_review()
    assert "Netlist-free load" in manufacturing and "approximate" in manufacturing
    tour = main.project_tour()
    assert "Load: netlist-free" in tour and "(limited: netlist-free load)" in tour
    assert "PCB: Board.PcbDoc" in tour and "### Phase 3 — PCB\n" in tour


def test_stale_cached_netlist_is_not_used(env):
    root, _, _ = env
    net = root / "Project Outputs for Board" / "Board.NET"
    net.parent.mkdir()
    net.write_text("(\nGND\nR1-1\n)\n", encoding="utf-8")
    registry = {"projects": [{"dir": str(root), "netlist_mtime": net.stat().st_mtime}]}
    # sheet1 saved after the netlist was generated
    os.utime(root / "source" / "sheet1.SchDoc", (net.stat().st_mtime + 100,) * 2)
    with patch.object(main, "read_registry", return_value=registry):
        result = _load(root)
    assert result["partial"] is True
    assert result["netlist_stale"] is True
    assert main._altium._netlist is None


def test_project_without_pcbdoc(env):
    root, _, _ = env
    prj = root / "Board.PrjPcb"
    prj.write_text(PRJ_TEXT.replace("[Document3]\nDocumentPath=Board.PcbDoc\n", ""),
                   encoding="utf-8")
    result = _load(root)
    assert result["partial"] is True
    assert result["pcb_doc"] is None
    assert "no .PcbDoc" in result["message"]
    assert json.loads(main.get_board_info())["error"] == "no_pcb_document"
    assert json.loads(main.list_variants())["active_variant"] == "Prod"
    tour = main.project_tour()
    assert "### Phase 3 — PCB (skipped: no PcbDoc)" in tour and "get_board_info" not in tour


def test_pcb_only_project_tour_skips_the_schematic(env):
    # No .SchDoc: the load is netlist-free, and Altium's full load would answer no_sheets,
    # so the tour neither walks sheets nor promises a full tour after opening Altium.
    root, _, _ = env
    (root / "Board.PrjPcb").write_text(
        "[Design]\nVersion=1.0\n[Document1]\nDocumentPath=Board.PcbDoc\n", encoding="utf-8")
    assert _load(root)["loaded"]
    tour = main.project_tour()
    assert "Schematic sheets: 0\n" in tour and "PCB: Board.PcbDoc\n" in tour
    assert "### Phase 2 — Schematic Blocks (skipped: no schematic sheets)" in tour
    assert "enables the full tour" not in tour and "get_sheet_context" not in tour
    assert "### Phase 3 — PCB\n" in tour


def test_without_a_project_tools_still_say_no_project(env):
    for call in (main.query_net, main.get_board_info, main.get_design_notes):
        args = ("GND",) if call is main.query_net else ()
        assert json.loads(call(*args))["error"] == "no_project"
    assert json.loads(main.schematic_review())["error"] == "no_project"
    assert json.loads(main.constraints_review())["error"] == "no_project"
    assert json.loads(main.pcb_manufacturing_review())["error"] == "no_project"
    assert json.loads(main.project_tour())["error"] == "no_project"


LITE_VARIANT = "[ProjectVariant2]\nDescription=Lite\nVariationCount=0\n"
ALTIUM_HAS_BOARD = {"running": True, "project_file": "Board.PrjPcb", "tab_type": "schematic"}


def _load_with_lite_active(root: Path) -> None:
    (root / "Board.PrjPcb").write_text(PRJ_TEXT + LITE_VARIANT, encoding="utf-8")
    _load(root)
    main.set_active_variant("Lite")


def test_refresh_netlist_without_altium_stays_partial(env):
    root, _, _ = env
    _load_with_lite_active(root)
    result = json.loads(main.refresh_netlist())
    assert result["partial"] is True
    assert result["active_variant"] == "Lite"
    assert main._project["name"] == "Board"
    assert json.loads(main.list_variants())["active_variant"] == "Lite"


def test_refresh_netlist_reports_a_variant_that_is_gone(env):
    root, _, _ = env
    _load_with_lite_active(root)
    (root / "Board.PrjPcb").write_text(PRJ_TEXT, encoding="utf-8")
    result = json.loads(main.refresh_netlist())
    assert result["active_variant"] == "Prod"
    assert "'Lite' is no longer in the project" in result["variant_note"]


def test_failed_full_load_keeps_the_partial_load(env):
    root, _, _ = env
    _load_with_lite_active(root)
    with patch.object(main._altium, "get_status", return_value=ALTIUM_HAS_BOARD), \
         patch.object(main._altium, "generate_netlist", side_effect=RuntimeError("Altium busy")):
        result = json.loads(main.refresh_netlist())
    assert result["error"] == "netlist_generation_failed"
    assert result["partial_load_kept"] is True
    assert "partial load is kept" in result["message"]
    assert json.loads(main.get_board_info()).get("error") is None
    assert json.loads(main.list_variants())["active_variant"] == "Lite"


def test_unregistered_netlist_in_the_output_folder_wins(env):
    # An archived copy of the project carries its own, newer Board.NET.
    root, _, _ = env
    old = root / "Archive" / "Project Outputs for Board" / "Board.NET"
    net = root / "Project Outputs for Board" / "Board.NET"
    for path, text in ((old, "(\nVCC\nR2-1\n)\n"), (net, "(\nGND\nR1-1\n)\n")):
        path.parent.mkdir(parents=True)
        path.write_text(text, encoding="utf-8")
    newest = max(f.stat().st_mtime for f in root.rglob("*") if f.is_file() and f.suffix != ".NET")
    os.utime(net, (newest + 100, newest + 100))
    os.utime(old, (newest + 200, newest + 200))
    result = _load(root)
    assert "partial" not in result
    assert json.loads(main.query_net("GND"))["pin_count"] == 1
    assert json.loads(main.query_net("VCC"))["error"] == "net_not_found"


def test_ambiguous_unregistered_netlists_load_partially(env):
    root, _, _ = env
    for folder in ("CopyA", "CopyB"):
        path = root / folder / "Board.NET"
        path.parent.mkdir()
        path.write_text("(\nGND\nR1-1\n)\n", encoding="utf-8")
        newest = max(f.stat().st_mtime for f in root.rglob("*") if f.is_file())
        os.utime(path, (newest + 100, newest + 100))
    assert _load(root)["partial"] is True


def test_up_to_date_netlist_missing_from_the_registry_loads_fully(env):
    # E.g. Altium regenerated the netlist itself, or the folder moved: the registry
    # has no matching netlist_mtime, but the .NET is newer than every source.
    root, upsert, _ = env
    net = root / "Project Outputs for Board" / "Board.NET"
    net.parent.mkdir()
    net.write_text("(\nGND\nR1-1\n)\n", encoding="utf-8")
    newest = max(f.stat().st_mtime for f in root.rglob("*") if f.is_file() and f != net)
    os.utime(net, (newest + 100, newest + 100))
    result = _load(root)
    assert result["loaded"] is True
    assert "partial" not in result
    assert json.loads(main.query_net("GND"))["pin_count"] == 1
    upsert.assert_called_once_with("Board.PrjPcb", str(root), netlist_mtime=net.stat().st_mtime)


def test_refresh_netlist_loads_fully_once_altium_has_the_project(env):
    root, _, _ = env
    _load(root)

    def generate(prj_pcb_path, sheet_paths=None, annotation_paths=None):
        main._altium._netlist = {
            "nets": {"GND": [("R1", "1")]},
            "pin_to_net": {"R1": {"1": "GND"}},
            "components": {"R1": {"pins": {"1": {"name": "1", "net": "GND"}}}},
        }
        return True

    (root / "Board.PrjPcb").write_text(PRJ_TEXT + LITE_VARIANT, encoding="utf-8")
    _load(root)
    main.set_active_variant("Lite")
    with patch.object(main._altium, "get_status", return_value=ALTIUM_HAS_BOARD), \
         patch.object(main._altium, "generate_netlist", side_effect=generate):
        result = json.loads(main.refresh_netlist())
    assert result["loaded"] is True
    assert "partial" not in result
    assert result["active_variant"] == "Lite"
    assert json.loads(main.query_net("GND"))["pin_count"] == 1


def test_altium_running_on_another_project_keeps_the_partial_load(env):
    root, _, _ = env
    _load(root)
    status = {"running": True, "project_file": "Other.PrjPcb", "tab_type": "schematic"}
    with patch.object(main._altium, "get_status", return_value=status):
        result = json.loads(main.refresh_netlist())
    assert result["error"] == "project_mismatch"
    assert main._project["name"] == "Board"
    assert json.loads(main.get_board_info()).get("error") is None


def test_full_load_netlist_reaches_the_pcb_index(tmp_path, pcb_data):
    # A regular load still hands the netlist to the PCB index (sch_refdes resolved).
    root = tmp_path / "Board"
    _write_project(root)
    session = main.PcbSession()
    session._load = lambda path, netlist: PcbIndex(pcb_data, netlist)
    with patch.object(main, "_project", {"name": "Board", "root_dir": str(root),
                                         "prj_pcb_path": str(root / "Board.PrjPcb"),
                                         "sheets": [],
                                         "pcb_doc_paths": [str(root / "Board.PcbDoc")]}), \
         patch.object(main, "_variant_state", VariantState([VariantDefinition(name="Default")])), \
         patch.object(main, "_pcb_session", session), \
         patch.object(main, "_altium", AltiumClient()):
        main._altium._netlist = NETLIST
        result = json.loads(main.get_board_info())
    assert result["refdes_mapping_unmatched"] == 0
    assert "netlist" not in result


# ---------- the offline netlist (the default load without Altium) ----------

def test_load_without_altium_compiles_the_sheets_offline(offline_env):
    root, upsert, _ = offline_env
    result = _load(root)

    assert result["loaded"] is True
    assert "partial" not in result and "netlist_required_tools" not in result
    assert result["load_mode"] == "offline_netlist"
    assert result["netlist_source"] == "offline_compiler"
    assert result["net_identifier_scope"] == "Flat"
    assert isinstance(result["compile_warnings"], list)
    assert "Altium Designer was not involved" in result["netlist_note"]
    assert "sheet_name" in result["session_start"]
    assert result["sheets"] == ["sheet1", "sheet2"] and result["pcb_doc"] == "Board.PcbDoc"
    assert result["component_count"] == 127 and result["netlist_updated_utc"]
    netlist = main._altium._netlist
    assert netlist["source"] == "offline" and netlist["net_file"] is None
    assert netlist["net_file_mtime"] > 0
    # Registered without a netlist mtime: an offline compile never vouches for a .NET.
    upsert.assert_called_once_with("Board.PrjPcb", str(root))


@pytest.mark.parametrize("tool", sorted(NETLIST_TOOL_CALLS))
def test_netlist_tools_work_with_the_offline_netlist(offline_env, tool):
    root, _, _ = offline_env
    _load(root)
    with patch.object(main, "mark_xfn_exported"):
        result = NETLIST_TOOL_CALLS[tool]()
    try:
        error = json.loads(result).get("error")
    except ValueError:  # sheet context text and the review prompts
        error = None
    assert error is None


def test_offline_netlist_names_its_source_and_links_the_pcb(offline_env):
    root, _, loads = offline_env
    _load(root)
    net = json.loads(main.query_net("^GND_BATT$"))
    assert net["netlist_source"] == "offline_compiler" and net["pin_count"] > 25
    context = main.get_sheet_context("sheet1")
    assert context.splitlines()[1] == "netlist_source:offline_compiler"
    board = json.loads(main.get_board_info())
    assert "netlist" not in board and "refdes_mapping_unmatched" in board
    assert loads and loads[-1] is main._altium._netlist


def test_the_snapshot_and_the_review_say_the_nets_were_compiled_offline(offline_env):
    # The team snapshot leaves the session: whoever runs package_for_xfn must be able to
    # tell its receivers that no Altium compile (and no ERC) is behind its nets.
    root, _, _ = offline_env
    _load(root)
    with patch.object(main, "mark_xfn_exported"):
        exported = main.package_for_xfn()
    assert "compiled offline" in exported and "no ERC" in exported
    assert "compiled offline" in main.schematic_review()


def test_refresh_compiles_the_sheets_again(offline_env):
    root, _, _ = offline_env
    _load(root)
    first = main._altium._netlist
    # sheet2 dropped from the project: its parts leave the netlist.
    (root / "Board.PrjPcb").write_text(PRJ_TEXT.replace(
        "[Document2]\nDocumentPath=source\\sheet2.SchDoc\n", ""), encoding="utf-8")
    result = json.loads(main.refresh_netlist())
    assert result["refreshed"] is True and result["netlist_source"] == "offline_compiler"
    assert "netlist_note" in result
    assert main._altium._netlist is not first
    assert {c["sheet"] for c in main._altium._netlist["components"].values()} == {"sheet1"}
    assert main._project["sheets"] == [{"name": "sheet1",
                                        "path": str(root / "source" / "sheet1.SchDoc")}]


def test_refresh_recompiles_when_altium_has_another_project(offline_env):
    root, _, _ = offline_env
    _load(root)
    status = {"running": True, "project_file": "Other.PrjPcb", "tab_type": "schematic"}
    with patch.object(main._altium, "get_status", return_value=status), \
         patch.object(main._altium, "generate_netlist") as generate:
        result = json.loads(main.refresh_netlist())
    assert result["netlist_source"] == "offline_compiler"
    generate.assert_not_called()


def test_refresh_takes_altiums_netlist_once_it_has_the_project(offline_env):
    root, _, _ = offline_env
    _load(root)

    def generate(prj_pcb_path, sheet_paths=None, annotation_paths=None):
        main._altium._netlist = {
            "nets": {"GND": [("R1", "1"), ("R2", "1")]}, "source": "altium",
            "pin_to_net": {"R1": {"1": "GND"}, "R2": {"1": "GND"}},
            "components": {"R1": {"pins": {"1": {"name": "1", "net": "GND"}}}},
        }
        return False  # the .NET on disk was fresh: still a change of source

    with patch.object(main._altium, "get_status", return_value=ALTIUM_HAS_BOARD), \
         patch.object(main._altium, "generate_netlist", side_effect=generate):
        result = json.loads(main.refresh_netlist())
    assert result["refreshed"] is True and result["netlist_source"] == "altium"
    assert "netlist_source" not in json.loads(main.query_net("GND"))


def test_stale_cached_netlist_gives_way_to_the_offline_compile(offline_env):
    root, _, _ = offline_env
    net = root / "Project Outputs for Board" / "Board.NET"
    net.parent.mkdir()
    net.write_text("(\nGND\nR1-1\n)\n", encoding="utf-8")
    registry = {"projects": [{"dir": str(root), "netlist_mtime": net.stat().st_mtime}]}
    os.utime(root / "source" / "sheet1.SchDoc", (net.stat().st_mtime + 100,) * 2)
    with patch.object(main, "read_registry", return_value=registry):
        result = _load(root)
    assert result["load_mode"] == "offline_netlist" and result["cached_netlist_stale"] is True
    assert "compiled instead" in result["netlist_note"]
    assert main._altium._netlist["source"] == "offline"


def test_a_fresh_altium_netlist_still_wins_over_the_offline_compile(offline_env):
    root, _, _ = offline_env
    net = root / "Project Outputs for Board" / "Board.NET"
    net.parent.mkdir()
    net.write_text("(\nGND\nR1-1\n)\n", encoding="utf-8")
    newest = max(f.stat().st_mtime for f in root.rglob("*") if f.is_file() and f != net)
    os.utime(net, (newest + 100, newest + 100))
    result = _load(root)
    assert result["netlist_source"] == "altium" and "load_mode" not in result
    assert main._altium._netlist["source"] == "altium"
