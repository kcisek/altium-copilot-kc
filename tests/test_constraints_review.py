"""Constraints review prompt, typical interface targets and board snapshot."""

import asyncio
import re
import string
from copy import deepcopy

import pytest

import server.main as main_mod
from server.parsers.pcb_doc import DiffPair, PcbClass, PcbLayer, PcbRule, XSignal
from server.services.constraints_review import (
    CONSTRAINTS_REVIEW_PROMPT,
    CONSTRAINTS_REVIEW_TOOLS,
    INTERFACE_TARGETS,
    SNAPSHOT_MAX_CLASSES,
    SNAPSHOT_MAX_STALE_RULES,
    build_constraints_review,
    constraints_snapshot,
    render_interface_targets,
)
from server.services.pcb_index import PcbIndex
from tests.test_pcb_index import NETLIST, pcb_data  # noqa: F401

# Rules6 kind ids as saved by Altium (RULESETTOCHECK uses them).
KIND_IDS = {
    "Clearance": 0, "Width": 2, "Length": 3, "MatchedLengths": 4,
    "RoutingLayers": 9, "RoutingVias": 11, "DiffPairsRouting": 51,
    "NetAntennae": 56, "RoomDefinition": 1,
}


def _rule(kind, priority, scope1="All", scope2="All", name=None, enabled=True, **props):
    return PcbRule(
        name=name or kind,
        kind=kind,
        kind_id=KIND_IDS.get(kind, 99),
        enabled=enabled,
        priority=priority,
        scope1=scope1,
        scope2=scope2,
        net_scope="DifferentNets" if kind == "Clearance" else "AnyNet",
        comment="",
        unique_id=None,
        from_schematic=False,
        properties=props,
    )


def _class(name, kind, members=(), superclass=False, auto_generated=False):
    return PcbClass(
        name=name,
        kind=kind,
        members=list(members),
        superclass=superclass,
        auto_generated=auto_generated,
        unique_id=None,
    )


PROJECT = {
    "name": "Demo",
    "root_dir": "C:\\work\\Demo",
    "prj_pcb_path": "C:\\work\\Demo\\Demo.PrjPcb",
    "sheets": [],
    "pcb_doc_paths": ["C:\\work\\Demo\\Board.PcbDoc"],
}


@pytest.fixture
def review_index(pcb_data):
    data = deepcopy(pcb_data)
    data.diff_pairs = [DiffPair("CLK", "CLK_A", "CLK_B", "PAIRUID")]
    data.classes = [
        _class("All Nets", "net", superclass=True),
        _class("Clocks", "net", ["CLK_A", "CLK_B"]),
        _class("All Differential Pairs", "diff_pair", superclass=True),
        _class("HS", "diff_pair", ["CLK", "OLD_PAIR"]),
        _class("All xSignals", "xsignal", superclass=True),
        _class("CLK_XS", "xsignal", ["CLK_PATH"]),
        _class("Top Side Components", "component", ["U3"], auto_generated=True),
    ]
    data.xsignals = [XSignal("CLK_PATH", ["U3-1", "U3-1"])]
    data.rules = [
        _rule("Width", 1, "InNetClass('Clocks')", name="Width_Clocks",
              MINLIMIT="4mil", PREFEREDWIDTH="5mil", MAXLIMIT="6mil"),
        _rule("Width", 2, name="Width", MINLIMIT="5mil", PREFEREDWIDTH="8mil",
              MAXLIMIT="20mil"),
        _rule("Width", 3, "InNetClass('Gone')", name="Width_Stale", MINLIMIT="9mil"),
        _rule("Width", 4, "InNet('NOPE')", name="Width_Off", enabled=False),
        _rule("Clearance", 1, name="Clearance", GENERICCLEARANCE="6mil"),
        _rule("DiffPairsRouting", 1, "InDifferentialPairClass('HS')", name="DP_HS",
              MINLIMIT="4mil", MOSTFREQGAP="5mil", MAXLIMIT="6mil",
              IMPEDANCEPROFILEDRIVEN="TRUE", IMPEDANCEPROFILEVALUE="90"),
        _rule("DiffPairsRouting", 2, name="DiffPairsRouting",
              MINLIMIT="10mil", MOSTFREQGAP="10mil", MAXLIMIT="10mil"),
        _rule("MatchedLengths", 1, "InDifferentialPairClass('DDR_GONE')",
              name="ML_Stale", TOLERANCE="5mil"),
        _rule("RoutingVias", 1, name="RoutingVias", WIDTH="20mil"),
        _rule("NetAntennae", 1, name="NetAntennae", enabled=False),
        _rule("RoomDefinition", 1, "InComponentClass('Nope')", name="RF",
              LAYER="TOP", VX0="0mil", VY0="0mil", VX1="10mil", VY1="0mil",
              VX2="10mil", VY2="10mil"),
    ]
    data.drc_rule_kinds = {0, 2, 3, 4}
    return PcbIndex(data, NETLIST)


def _prompt(index, project=None, variant="Prod"):
    return build_constraints_review(project or PROJECT, variant, index)


# ---------- prompt structure ----------


def test_prompt_placeholders_are_exactly_the_build_arguments():
    fields = {
        field
        for _, field, _, _ in string.Formatter().parse(CONSTRAINTS_REVIEW_PROMPT)
        if field is not None
    }
    assert fields == {
        "name", "pcb_doc", "active_variant", "root_dir", "snapshot", "interface_targets",
    }


def test_phases_and_gates(review_index):
    prompt = _prompt(review_index)
    for phase in ("Phase 1", "Phase 2", "Phase 3", "Phase 4"):
        assert f"### {phase} — " in prompt
    assert "Do not proceed until the user answers." in prompt
    assert prompt.count("Done when:") == 3
    for gate in ("Phase 2", "Phase 3", "Phase 4"):
        assert f"Do not proceed to {gate} until then." in prompt
    # The scope question comes before any phase.
    assert prompt.index("Do not proceed until the user answers.") < prompt.index(
        "### Phase 1"
    )
    for header in (
        "Table 1 — Interface Constraint Matrix",
        "| Interface | Nets / Pairs | Target (source) | Rule (name: value) | Copper (measured) | Result |",
        "Table 2 — Critical Issues",
        "| Interface | Net / Pair / Rule | Issue | Requirement (source) | Board Shows |",
        "Table 3 — Warnings & Unverifiable",
        "| Item | Observation | What would confirm it |",
    ):
        assert header in prompt


def test_ground_rules(review_index):
    prompt = _prompt(review_index)
    lower = prompt.lower()
    assert "datasheet" in lower and "layout guide" in lower
    assert "fallback only" in lower
    assert '"typical — confirm"' in prompt
    assert "never a critical issue on its own" in lower
    assert "Altium's own DRC result is get_drc_report" in prompt
    assert "Never say the board passes" in prompt
    assert "via\n  barrel" in prompt or "via barrel" in prompt
    assert "pin/package length" in prompt
    assert "active variant (Prod)" in prompt
    assert "DNP" in prompt
    assert "Net names are hints, not evidence" in prompt


def _flat(prompt: str) -> str:
    return re.sub(r"\s+", " ", prompt)


def test_length_rules_match_the_length_engine(review_index):
    # Pin/package length is included like Altium's Signal Length; it is not
    # bounded to "a few mil" or said to be excluded.
    flat = _flat(_prompt(review_index))
    assert "plus the pin/package length (pad to die) stored on the end pads" in flat
    assert "adds the pin/package length at its end pins" in flat
    assert "no pin/package length, like Altium's Routed Length" in flat
    assert "group length is the P/N average" in flat
    assert "few mil" not in flat
    assert "excluded" not in flat


def test_prompt_promises_only_what_check_diff_pairs_returns(review_index):
    flat = _flat(_prompt(review_index))
    # check_diff_pairs has no per-leg via counts and does not compare schematic
    # directives: check_directive_sync does.
    assert "via count per leg, AC-coupling" not in flat
    assert "schematic directives missing on the PCB" not in flat
    assert "Differential Pair directives are not compared with the PCB yet" not in flat
    assert 'check_directive_sync(kind="diff_pair") says whether each schematic' in flat
    assert "get_signal_length(<pair>) gives the via count per leg" in flat
    step = flat[flat.index("3. check_diff_pairs(pattern=...) per interface"):]
    assert step.startswith(
        "3. check_diff_pairs(pattern=...) per interface: in-pair skew and AC-coupling symmetry."
    )


def test_prompt_offers_no_save_tool(review_index):
    prompt = _prompt(review_index)
    assert "save_" not in prompt
    assert "markdown file" not in prompt.lower()


def test_context_lines(review_index):
    prompt = _prompt(review_index)
    assert "Project: Demo" in prompt
    assert "PCB: Board.PcbDoc" in prompt
    assert "Active variant: Prod" in prompt
    assert 'set_project_dir("C:\\work\\Demo")' in prompt
    assert 'set_active_variant("Prod")' in prompt
    assert "- Differential pairs: 1; in no pair class: 0" in prompt
    assert "HS (2, 1 unresolved)" in prompt
    assert "Width_Stale (Width): innetclass('Gone')" in prompt
    assert "ML_Stale (MatchedLengths): indifferentialpairclass('DDR_GONE')" in prompt


def test_multiple_pcb_docs_are_named(review_index):
    project = dict(PROJECT, pcb_doc_paths=["a/Main.PcbDoc", "b/Panel.PcbDoc"])
    prompt = _prompt(review_index, project)
    assert "PCB: Main.PcbDoc (the project lists 2 PcbDocs; only this one is reviewed)" in prompt


def test_format_safe_with_braces(review_index):
    data = deepcopy(review_index.pcb)
    data.classes.append(_class("{4F2A-GUID}", "diff_pair", ["CLK"]))
    data.rules.append(
        _rule("Width", 9, "InNetClass('{x}')", name="W{0}{name}", MINLIMIT="1mil")
    )
    index = PcbIndex(data, NETLIST)
    project = dict(PROJECT, name="Demo {0} {name}", root_dir="C:\\{x}\\Demo")
    prompt = build_constraints_review(project, "V{1}", index)
    assert "Project: Demo {0} {name}" in prompt
    assert "{4F2A-GUID} (1)" in prompt
    assert "W{0}{name} (Width): innetclass('{x}')" in prompt
    assert 'set_project_dir("C:\\{x}\\Demo")' in prompt
    assert "Active variant: V{1}" in prompt


# ---------- snapshot ----------


def test_snapshot_lines(review_index):
    snapshot = constraints_snapshot(review_index)
    lines = snapshot.splitlines()
    assert all(line.startswith("- ") for line in lines)
    assert lines[0] == (
        "- Copper: 4 routable signal layers, in stack order: "
        "Top Layer, L2-GND, L3-SIG, Bottom Layer (stackup source: v9)"
    )
    assert "- Net classes: 1; xSignal classes: 1; xSignals: 1" in lines
    assert (
        "- Rules per kind: DiffPairsRouting 2, Width 4 (1 disabled), Clearance 1, "
        "MatchedLengths 1, Length 0, RoutingVias 1, RoutingLayers 0, ReturnPath 0"
    ) in lines
    assert "- Impedance-profile-driven rules: DiffPairsRouting 1 of 2, Width 0 of 4" in lines
    # Superclasses and auto-generated classes are not counted or listed.
    assert "All Differential Pairs" not in snapshot
    assert "Top Side Components" not in snapshot
    # Rooms and non-routing kinds are not stale routing rules.
    assert "RF" not in snapshot


def test_snapshot_marks_internal_planes_in_stack_order(pcb_data):
    data = deepcopy(pcb_data)
    data.board.layers.append(PcbLayer(39, "GND-PLANE", "copper", 5))
    snapshot = constraints_snapshot(PcbIndex(data, NETLIST))
    assert snapshot.splitlines()[0] == (
        "- Copper: 4 routable signal layers and 1 internal plane layer, in stack "
        "order: Top Layer, L2-GND, L3-SIG, GND-PLANE (plane), Bottom Layer "
        "(stackup source: v9)"
    )


def test_snapshot_marks_disabled_stale_rules(review_index):
    snapshot = constraints_snapshot(review_index)
    assert "Width_Off (Width, disabled): innet('NOPE')" in snapshot
    assert "stale scope references (3;" in snapshot
    # Enabled rules first, in review-kind order; disabled ones last.
    assert (
        snapshot.index("Width_Stale (")
        < snapshot.index("ML_Stale (")
        < snapshot.index("Width_Off (")
    )


def test_snapshot_batch_drc_kinds(review_index):
    snapshot = constraints_snapshot(review_index)
    assert (
        "- Batch DRC rule set leaves out: DiffPairsRouting, RoutingVias"
        in snapshot
    )
    data = deepcopy(review_index.pcb)
    data.drc_rule_kinds = None
    assert "- Batch DRC rule set: not saved in the PcbDoc (unknown)" in (
        constraints_snapshot(PcbIndex(data, NETLIST))
    )
    data.drc_rule_kinds = {0, 2, 3, 4, 11, 51}
    assert "- Batch DRC rule set: includes every kind above that has rules" in (
        constraints_snapshot(PcbIndex(data, NETLIST))
    )


def test_snapshot_bare_board(pcb_data):
    snapshot = constraints_snapshot(PcbIndex(pcb_data, NETLIST))
    assert "- Differential pairs: none defined on the PCB" in snapshot
    assert "- Pair classes: none" in snapshot
    assert "- Net classes: 0; xSignal classes: 0; xSignals: 0" in snapshot
    assert "- Routing rules with stale scope references: none" in snapshot
    assert "ReturnPath 0" in snapshot


def test_snapshot_names_no_interfaces(pcb_data):
    data = deepcopy(pcb_data)
    data.diff_pairs = [DiffPair("USB_D", "CLK_A", "CLK_B", None)]
    snapshot = constraints_snapshot(PcbIndex(data, NETLIST))
    assert "- Differential pairs: 1; in no pair class: 1" in snapshot
    assert "USB" not in snapshot
    assert "CLK_A" not in snapshot


def test_snapshot_caps_lists(review_index):
    data = deepcopy(review_index.pcb)
    data.classes += [
        _class(f"PAIRCLASS_{number:03d}", "diff_pair", ["CLK"]) for number in range(200)
    ]
    data.rules += [
        _rule("Width", 10 + number, f"InNetClass('GONE_{number:02d}')",
              name=f"Width_Stale_{number:02d}", MINLIMIT="1mil")
        for number in range(50)
    ]
    snapshot = constraints_snapshot(PcbIndex(data, NETLIST))
    class_line = next(
        line for line in snapshot.splitlines() if line.startswith("- Pair classes")
    )
    assert class_line.startswith("- Pair classes (201;")
    assert class_line.count("(1)") + class_line.count("(2, 1 unresolved)") == (
        SNAPSHOT_MAX_CLASSES
    )
    assert class_line.endswith("+181 more")
    stale_line = next(
        line for line in snapshot.splitlines() if "stale scope references" in line
    )
    assert "(53;" in stale_line
    assert stale_line.count("(Width") + stale_line.count("(MatchedLengths") == (
        SNAPSHOT_MAX_STALE_RULES
    )
    assert stale_line.endswith("+43 more")
    assert "PAIRCLASS_199" not in snapshot
    assert len(snapshot) < 3000


def test_snapshot_shortens_long_names(review_index):
    data = deepcopy(review_index.pcb)
    data.classes.append(_class("P" * 500, "diff_pair", ["CLK"]))
    snapshot = constraints_snapshot(PcbIndex(data, NETLIST))
    assert "P" * 500 not in snapshot
    assert "P" * 59 + "…" in snapshot


# ---------- interface targets ----------


REQUIRED_INTERFACES = (
    "USB 2.0", "USB 3", "PCIe", "Ethernet", "DDR3", "DDR4", "DDR5", "LVDS", "HDMI",
    "DisplayPort", "MIPI", "SATA", "CAN", "RS-485", "SerDes",
)


def test_interface_targets_complete_and_sourced():
    keys = {"interface", "impedance", "skew_length", "other", "source"}
    for row in INTERFACE_TARGETS:
        assert set(row) == keys
        assert all(str(row[key]).strip() for key in keys), row["interface"]
        assert len(row["source"].strip()) >= 5, row["interface"]
    names = " / ".join(row["interface"] for row in INTERFACE_TARGETS)
    for interface in REQUIRED_INTERFACES:
        assert interface in names, interface
    assert len({row["interface"] for row in INTERFACE_TARGETS}) == len(INTERFACE_TARGETS)


def test_render_interface_targets_is_a_valid_table():
    table = render_interface_targets()
    lines = table.splitlines()
    assert lines[0] == "| Interface | Impedance | Skew / length | Other | Source |"
    assert lines[1] == "|---|---|---|---|---|"
    assert len(lines) == 2 + len(INTERFACE_TARGETS)
    for line, row in zip(lines[2:], INTERFACE_TARGETS):
        assert line.startswith(f"| {row['interface']} |")
        assert line.count("|") == 6
        assert row["source"] in line


def test_targets_are_in_the_prompt_as_fallback(review_index):
    prompt = _prompt(review_index)
    assert "## Typical Targets (fallback only — confirm against the part's own guide)" in prompt
    assert render_interface_targets() in prompt
    assert prompt.index("### Phase 4") < prompt.index("## Typical Targets")


# ---------- tool references ----------


REQUIRED_TOOLS = (
    "get_board_info", "list_diff_pairs", "get_classes", "get_design_rules",
    "get_rules_for_net", "check_rule_compliance", "check_lengths", "check_diff_pairs",
    "get_net_neighbors", "search_components", "get_component", "query_net", "get_net_pcb",
)


def test_tools_tuple_matches_prompt(review_index):
    prompt = _prompt(review_index)
    assert set(REQUIRED_TOOLS) <= set(CONSTRAINTS_REVIEW_TOOLS)
    assert len(set(CONSTRAINTS_REVIEW_TOOLS)) == len(CONSTRAINTS_REVIEW_TOOLS)
    for tool in CONSTRAINTS_REVIEW_TOOLS:
        assert re.search(rf"\b{tool}\b", CONSTRAINTS_REVIEW_PROMPT), tool
    # Anything written as a call in the prompt is a tracked tool.
    called = set(re.findall(r"\b([a-z]+(?:_[a-z]+)+)\(", prompt))
    assert called <= set(CONSTRAINTS_REVIEW_TOOLS), called - set(CONSTRAINTS_REVIEW_TOOLS)


def test_registered_tools_named_in_prompt_are_tracked():
    registered = {tool.name for tool in asyncio.run(main_mod.mcp.list_tools())}
    named = {
        tool for tool in registered
        if re.search(rf"\b{tool}\b", CONSTRAINTS_REVIEW_PROMPT)
    }
    assert named <= set(CONSTRAINTS_REVIEW_TOOLS)


def test_prompt_tools_are_registered_once_integrated():
    registered = {tool.name for tool in asyncio.run(main_mod.mcp.list_tools())}
    if "constraints_review" not in registered:
        pytest.skip("constraints_review is not registered on the server yet")
    assert set(CONSTRAINTS_REVIEW_TOOLS) <= registered
