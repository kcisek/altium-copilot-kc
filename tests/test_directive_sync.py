"""check_directive_sync (services/directive_sync.py): schematic directives against the PCB.

Synthetic projects only: sheets written by tests/sch_fixture.py and compiled offline, a
netlist built from that compile, and a PcbData with the classes, pairs and rules each test
needs. Nets are lines of a generic layout: U1 pin i -- wire with a net label -- R<i> pin
1, R<i> pin 2 on GND."""
import base64
import json
import struct
import zlib
from pathlib import Path
from unittest.mock import patch

import pytest

import server.main as main
from parsers.pcb_doc import DiffPair, PcbClass, PcbComponent, PcbData, PcbRule
from parsers.prj_pcb import PrjOptions, parse_prj_options
from services.directive_sync import (
    KINDS, build_directive_sync, build_directive_sync_cached, class_generation,
    decode_rule_text, detect_project_type, page_directive_sync,
)
from services.pcb_index import PcbIndex
from services.sch_netlister import compile_netlist
from tests.library_fixture import write_ole
from tests.sch_fixture import Sheet, project
from tests.test_pcb_index import _board, _pad

GND = "GND"


# ---------- building a design ----------

class Design:
    """Schematic lines and the PCB made from them."""

    def __init__(self):
        self.sheets: dict[str, Sheet] = {}
        self.lines: dict[str, list[tuple[str, int]]] = {}  # sheet -> [(net, y)]
        self.uids: dict[str, str] = {}

    def sheet(self, name: str = "Main.SchDoc") -> Sheet:
        if name not in self.sheets:
            self.sheets[name] = Sheet()
            self.lines[name] = []
        return self.sheets[name]

    def line(self, net: str, sheet: str = "Main.SchDoc", directive=None, x0: int = 50):
        """One net: a U pin and R pin 1 joined by a wire carrying a net label, R pin 2 on
        GND; directive (parameters) sits on the wire. Returns the wire's y."""
        s = self.sheet(sheet)
        y = 100 + 20 * len(self.lines[sheet])
        self.lines[sheet].append((net, y))
        designator = f"R{sum(len(lines) for lines in self.lines.values())}"
        self.uids[designator] = s.part(designator, [("1", x0 + 50, y), ("2", x0 + 100, y)])
        s.wire((x0, y), (x0 + 50, y))
        s.label(net, x0 + 20, y)
        s.power(GND, x0 + 100, y)
        if directive is not None:
            s.parameter_set(x0 + 35, y, directive)
        return y

    def finish(self, tmp_path: Path, prefix: str = "U", prj_extra: str = "",
               sheet_options: dict | None = None):
        """Place each sheet's U part (pin i on line i), write the project and compile it.
        Per-sheet component classes and rooms are off unless sheet_options turns them on.
        Returns (prj path, compiled, netlist)."""
        for index, (name, s) in enumerate(self.sheets.items(), 1):
            lines = self.lines[name]
            if lines:
                designator = f"{prefix}{index}"
                self.uids[designator] = s.part(
                    designator, [(str(i + 1), 50, y) for i, (_, y) in enumerate(lines)])
        prj, read = project(tmp_path, self.sheets)
        text = Path(prj).read_text(encoding="utf-8")
        defaults = {"ClassGenCCAutoEnabled": 0, "ClassGenCCAutoRoomEnabled": 0}
        for name in self.sheets:
            options = {**defaults, **(sheet_options or {}).get(name, {})}
            keys = "".join(f"{key}={value}\n" for key, value in options.items())
            text = text.replace(f"DocumentPath={name}\n", f"DocumentPath={name}\n{keys}")
        Path(prj).write_text(text + prj_extra, encoding="utf-8")
        compiled = compile_netlist(prj, read_sheet=read)
        return prj, compiled, netlist_of(compiled)


def netlist_of(compiled) -> dict:
    """The loaded-netlist shape the tools get, from an offline compile."""
    components = {}
    for sheet, metas in compiled.sheets:
        for designator, meta in metas.items():
            components[designator] = {
                "unique_id": meta.unique_id, "unique_ids": list(meta.unique_ids) or
                [meta.unique_id], "pin_designators": list(meta.pin_designators),
                "footprint": meta.footprint, "footprints": list(meta.footprints),
                "kind": meta.kind, "sheet": sheet, "params": dict(meta.params), "pins": {},
            }
    return {"nets": {name: list(pins) for name, pins in compiled.nets.items()},
            "pin_to_net": {ref: dict(pins) for ref, pins in compiled.pin_to_net.items()},
            "parts": {ref: dict(part) for ref, part in compiled.parts.items()},
            "components": components}


def _component(index: int, designator: str, uid: str) -> PcbComponent:
    return PcbComponent(index=index, refdes=designator, source_uid_tail=uid,
                        source_unique_id=f"\\{uid}", channel_path=None, x=index * 100.0, y=0.0,
                        rotation=0.0, side="top", footprint=None, designator=designator,
                        comment=None)


def pcb_of(netlist: dict, uids: dict[str, str], rename: dict | None = None, classes=(),
           pairs=(), rules=(), extra_nets=()) -> PcbData:
    """The PCB Update PCB would make from the netlist (nets renamed by rename), with the
    classes, pairs and rules given."""
    rename = rename or {}
    nets = sorted({rename.get(net, net) for net in netlist["nets"]} | set(extra_nets))
    components, pads = [], []
    for index, (designator, pins) in enumerate(sorted(netlist["pin_to_net"].items())):
        components.append(_component(index, designator, uids[designator]))
        for i, (pin, net) in enumerate(sorted(pins.items())):
            pads.append(_pad(nets.index(rename.get(net, net)), index, index * 100.0, i * 10.0,
                             name=pin))
    return PcbData(board=_board(), nets=nets, components=components, primitives=pads,
                   pours=[], layer_name_to_v6={"top layer": 1, "top": 1},
                   classes=list(classes), diff_pairs=list(pairs), rules=list(rules))


def net_class(name: str, *members: str, kind: str = "net") -> PcbClass:
    return PcbClass(name=name, kind=kind, members=list(members), superclass=False,
                    auto_generated=False, unique_id=None)


def rule(kind: str, name: str, scope: str, uid: str | None = None, from_schematic=True,
         **properties) -> PcbRule:
    return PcbRule(name=name, kind=kind, kind_id=0, enabled=True, priority=1, scope1=scope,
                   scope2="All", net_scope=None, comment="", unique_id=uid,
                   from_schematic=from_schematic, properties=dict(properties))


def run(prj, compiled, netlist, pcb: PcbData, project_type=None) -> dict:
    index = PcbIndex(pcb, netlist)
    return build_directive_sync(compiled, index, parse_prj_options(prj), netlist,
                                project_type=project_type or detect_project_type(prj))


def checks(model: dict) -> list[tuple[str, str]]:
    return [(f["check"], f["severity"]) for f in model["findings"]]


def rows(model: dict, kind: str) -> dict[str, dict]:
    return {row["name"]: row for row in model["rows"] if row["kind"] == kind}


CLASS_HS = [("ClassName", "HS")]
PAIR = [("DifferentialPair", "True")]


# ---------- net class directives ----------

def test_a_net_class_directive_on_the_pcb_is_transferred(tmp_path):
    design = Design()
    design.line("SIG_A", directive=CLASS_HS)
    design.line("SIG_B", directive=CLASS_HS)
    design.line("SIG_C")
    prj, compiled, netlist = design.finish(tmp_path)
    model = run(prj, compiled, netlist, pcb_of(netlist, design.uids,
                                                classes=[net_class("HS", "SIG_A", "SIG_B")]))
    assert model["project_type"]["type"] == "classic"
    assert rows(model, "net_class")["HS"]["status"] == "transferred"
    assert rows(model, "net_class")["HS"]["net_list"] == ["SIG_A", "SIG_B"]
    assert model["summary"]["net_class"]["transferred"] == 1
    assert model["in_sync"] and not model["findings"]


def test_a_missing_net_class_is_a_warning(tmp_path):
    design = Design()
    design.line("SIG_A", directive=CLASS_HS)
    prj, compiled, netlist = design.finish(tmp_path)
    model = run(prj, compiled, netlist, pcb_of(netlist, design.uids))
    assert checks(model) == [("directive_class_missing", "warning")]
    assert model["findings"][0]["sheets"] == ["Main"]
    assert model["summary"]["net_class"]["missing"] == 1 and not model["in_sync"]


def test_class_generation_off_makes_a_missing_class_info(tmp_path):
    design = Design()
    design.line("SIG_A", directive=CLASS_HS)
    prj, compiled, netlist = design.finish(
        tmp_path, prj_extra="[PrjClassGen]\nNetClassManualEnabled=0\n")
    model = run(prj, compiled, netlist, pcb_of(netlist, design.uids))
    (finding,) = model["findings"]
    assert (finding["check"], finding["severity"]) == ("directive_class_missing", "info")
    assert "Generate user-defined net classes" in finding["issue"] and "off" in finding["issue"]
    assert rows(model, "net_class")["HS"]["status"] == "not_generated"
    assert model["summary"]["net_class"]["disabled_by_options"] == 1
    assert model["class_generation"]["project"]["user_defined_net_classes"] is False


def test_class_generation_off_makes_differences_of_a_found_class_info(tmp_path):
    design = Design()
    for net in ("SIG_A", "SIG_B", "SIG_C"):
        design.line(net, directive=CLASS_HS)
    design.line("SIG_D")
    prj, compiled, netlist = design.finish(
        tmp_path, prj_extra="[PrjClassGen]\nNetClassManualEnabled=0\n")
    option = "Generate user-defined net classes"
    fewer = run(prj, compiled, netlist, pcb_of(netlist, design.uids,
                                                classes=[net_class("HS", "SIG_A")]))
    (finding,) = fewer["findings"]
    assert (finding["check"], finding["severity"]) == ("class_members_differ", "info")
    assert finding["option_off"] == option and finding["missing_from_pcb_class"] == [
        "SIG_B", "SIG_C"]
    assert "does not change the class" in finding["issue"]
    assert "would make them match" not in finding["issue"]
    assert rows(fewer, "net_class")["HS"]["option_off"] == option and fewer["in_sync"]
    renamed = run(prj, compiled, netlist, pcb_of(netlist, design.uids, classes=[
        net_class("HS_OLD", "SIG_A", "SIG_B", "SIG_C")]))
    (finding,) = renamed["findings"]
    assert (finding["check"], finding["severity"]) == ("class_name_differs", "info")
    assert "does not rename it" in finding["issue"] and finding["option_off"] == option


def test_extra_pcb_members_point_to_class_directives_on_no_net(tmp_path):
    design = Design()
    design.line("SIG_A", directive=CLASS_HS)
    design.line("SIG_B")
    design.line("SIG_C", directive=[("ClassName", "OTHER")])
    design.sheet().parameter_set(900, 900, CLASS_HS)
    design.sheet().parameter_set(900, 700, [("ClassName", "GONE")])
    prj, compiled, netlist = design.finish(tmp_path)
    model = run(prj, compiled, netlist, pcb_of(netlist, design.uids, classes=[
        net_class("HS", "SIG_A", "SIG_B"), net_class("OTHER", "SIG_C"),
        net_class("GONE", "SIG_B")]))
    found = {f["name"]: f for f in model["findings"]}
    assert found["HS"]["check"] == "class_members_differ"
    assert found["HS"]["directives_of_class_on_no_net"] == 1
    assert "sit on no net" in found["HS"]["issue"]
    # A class whose every directive is on no net reads as a PCB item no directive gives.
    assert found["GONE"]["check"] == "stale_pcb_item"
    assert found["GONE"]["directives_of_class_on_no_net"] == 1
    assert model["summary"]["directives_on_no_net"] == 2


def test_member_differences_are_reported_both_ways(tmp_path):
    design = Design()
    design.line("SIG_A", directive=CLASS_HS)
    design.line("SIG_B", directive=CLASS_HS)
    design.line("SIG_C")
    prj, compiled, netlist = design.finish(tmp_path)
    model = run(prj, compiled, netlist, pcb_of(netlist, design.uids,
                                                classes=[net_class("HS", "SIG_A", "SIG_C")]))
    (finding,) = model["findings"]
    assert (finding["check"], finding["severity"]) == ("class_members_differ", "warning")
    assert finding["missing_from_pcb_class"] == ["SIG_B"]
    assert finding["extra_on_pcb"] == ["SIG_C"]
    # Only extra PCB members: info.
    model = run(prj, compiled, netlist, pcb_of(
        netlist, design.uids, classes=[net_class("HS", "SIG_A", "SIG_B", "SIG_C")]))
    assert checks(model) == [("class_members_differ", "info")]


def test_nets_renamed_on_the_pcb_are_followed(tmp_path):
    design = Design()
    design.line("SIG_A", directive=CLASS_HS)
    design.line("SIG_B")
    prj, compiled, netlist = design.finish(tmp_path)
    pcb = pcb_of(netlist, design.uids, rename={"SIG_A": "SIG_A_OLD"},
                 classes=[net_class("HS", "SIG_A_OLD")])
    model = run(prj, compiled, netlist, pcb)
    assert rows(model, "net_class")["HS"]["status"] == "transferred"
    assert model["renamed_nets"] == {"SIG_A": "SIG_A_OLD"}
    assert model["summary"]["nets_renamed_on_pcb"] == 1


def test_a_class_name_differing_in_case_still_matches(tmp_path):
    design = Design()
    design.line("SIG_A", directive=CLASS_HS)
    prj, compiled, netlist = design.finish(tmp_path)
    model = run(prj, compiled, netlist, pcb_of(netlist, design.uids,
                                                classes=[net_class("hs", "SIG_A")]))
    row = rows(model, "net_class")["HS"]
    assert row["status"] == "transferred" and row["name_case_differs"] is True


def _three_class_nets(tmp_path, prj_extra=""):
    design = Design()
    for net in ("SIG_A", "SIG_B", "SIG_C"):
        design.line(net, directive=CLASS_HS)
    design.line("SIG_D")
    return design, *design.finish(tmp_path, prj_extra=prj_extra)


def test_a_class_renamed_on_the_pcb_is_matched_by_its_members(tmp_path):
    design, prj, compiled, netlist = _three_class_nets(tmp_path)
    renamed = net_class("HS_OLD", "SIG_A", "SIG_B", "SIG_C")
    model = run(prj, compiled, netlist, pcb_of(netlist, design.uids, classes=[renamed]))
    row = rows(model, "net_class")["HS"]
    assert (row["status"], row["pcb_name"], row["matched_by"]) == (
        "differs", "HS_OLD", "members")
    assert checks(model) == [("class_name_differs", "warning")]
    # Two shared nets of three are below Min Matched Members (3): missing, not renamed.
    partly = [net_class("HS_OLD", "SIG_A", "SIG_B", "SIG_D")]
    small = run(prj, compiled, netlist, pcb_of(netlist, design.uids, classes=partly))
    assert ("directive_class_missing", "warning") in checks(small)
    # The project's Comparator row lowers the bar.
    design, prj, compiled, netlist = _three_class_nets(tmp_path / "loose", prj_extra=(
        "[Comparison Options]\nComparisonOptions1=Kind=Net Class|MinPercent=50|MinMatch=2\n"))
    model = run(prj, compiled, netlist, pcb_of(netlist, design.uids, classes=partly))
    assert rows(model, "net_class")["HS"]["pcb_name"] == "HS_OLD"


# ---------- differential pairs ----------

def test_a_pair_directive_on_both_legs_is_found_by_its_nets(tmp_path):
    design = Design()
    design.line("D_P", directive=PAIR)
    design.line("D_N", directive=PAIR)
    prj, compiled, netlist = design.finish(tmp_path)
    for pcb_name in ("D", "d"):  # a name differing only in case is the same pair
        model = run(prj, compiled, netlist, pcb_of(
            netlist, design.uids, pairs=[DiffPair(pcb_name, "D_P", "D_N", None)]))
        row = rows(model, "diff_pair")["D"]
        assert (row["status"], row["pcb_name"]) == ("transferred", pcb_name)
        assert (row["positive"], row["negative"]) == ("D_P", "D_N")
        assert not model["findings"]
    # The same nets under another name: found by its nets, and Update PCB renames it.
    model = run(prj, compiled, netlist, pcb_of(netlist, design.uids,
                                                pairs=[DiffPair("USB", "D_P", "D_N", None)]))
    row = rows(model, "diff_pair")["D"]
    assert (row["status"], row["pcb_name"]) == ("differs", "USB")
    (finding,) = model["findings"]
    assert (finding["check"], finding["severity"]) == ("diff_pair_name_differs", "info")
    assert finding["pcb_name"] == "USB" and "rename" in finding["issue"]
    assert model["in_sync"]


def test_a_missing_pair_is_a_warning(tmp_path):
    design = Design()
    design.line("D_P", directive=PAIR)
    design.line("D_N", directive=PAIR)
    prj, compiled, netlist = design.finish(tmp_path)
    model = run(prj, compiled, netlist, pcb_of(netlist, design.uids))
    (finding,) = model["findings"]
    assert (finding["check"], finding["severity"]) == ("diff_pair_missing", "warning")
    assert (finding["positive"], finding["negative"]) == ("D_P", "D_N")


def test_swapped_and_different_pair_nets_differ(tmp_path):
    design = Design()
    design.line("D_P", directive=PAIR)
    design.line("D_N", directive=PAIR)
    design.line("E_N")
    prj, compiled, netlist = design.finish(tmp_path)
    swapped = run(prj, compiled, netlist, pcb_of(netlist, design.uids,
                                                  pairs=[DiffPair("D", "D_N", "D_P", None)]))
    assert checks(swapped) == [("diff_pair_nets_differ", "warning")]
    assert "swapped" in swapped["findings"][0]["issue"]
    other = run(prj, compiled, netlist, pcb_of(netlist, design.uids,
                                                pairs=[DiffPair("D", "D_P", "E_N", None)]))
    (finding,) = other["findings"]
    assert finding["check"] == "diff_pair_nets_differ"
    assert (finding["pcb_positive"], finding["pcb_negative"]) == ("D_P", "E_N")


def test_a_directive_on_one_leg_makes_no_pair(tmp_path):
    design = Design()
    design.line("D_P", directive=PAIR)
    design.line("D_N")
    prj, compiled, netlist = design.finish(tmp_path)
    model = run(prj, compiled, netlist, pcb_of(netlist, design.uids))
    (finding,) = model["findings"]
    assert (finding["check"], finding["severity"]) == ("diff_pair_missing", "warning")
    assert "only the positive net" in finding["issue"]


def test_a_pair_net_without_a_polarity_suffix_is_reported(tmp_path):
    design = Design()
    design.line("CLKA", directive=PAIR)
    prj, compiled, netlist = design.finish(tmp_path)
    model = run(prj, compiled, netlist, pcb_of(netlist, design.uids))
    (finding,) = model["findings"]
    assert finding["check"] == "diff_pair_missing" and "polarity suffix" in finding["issue"]


def test_custom_suffixes_from_the_project_name_pairs(tmp_path):
    design = Design()
    design.line("CK_T", directive=PAIR)
    design.line("CK_C", directive=PAIR)
    prj, compiled, netlist = design.finish(
        tmp_path, prj_extra="[DiffPairSuffix1]\nPositiveSuffix=_T\nNegativeSuffix=_C\n")
    model = run(prj, compiled, netlist, pcb_of(netlist, design.uids,
                                                pairs=[DiffPair("CK", "CK_T", "CK_C", None)]))
    assert rows(model, "diff_pair")["CK"]["status"] == "transferred"
    assert {"positive": "_T", "negative": "_C"} in model["differential_pair_suffixes"]


def test_a_pair_directive_with_a_class_name_makes_a_pair_class(tmp_path):
    design = Design()
    params = [*PAIR, ("ClassName", "PAIRS90")]
    design.line("D_P", directive=params)
    design.line("D_N", directive=params)
    prj, compiled, netlist = design.finish(tmp_path)
    pair = DiffPair("D", "D_P", "D_N", None)
    model = run(prj, compiled, netlist, pcb_of(
        netlist, design.uids, pairs=[pair],
        classes=[net_class("PAIRS90", "D", kind="diff_pair")]))
    pair_rows = [row for row in model["rows"] if row["kind"] == "diff_pair"]
    assert {(row["name"], row["status"]) for row in pair_rows} == {
        ("D", "transferred"), ("PAIRS90", "transferred")}
    assert not model["findings"]
    missing = run(prj, compiled, netlist, pcb_of(netlist, design.uids, pairs=[pair]))
    assert checks(missing) == [("directive_class_missing", "warning")]


def test_a_pair_class_follows_the_user_defined_net_classes_switch(tmp_path):
    design = Design()
    params = [*PAIR, ("ClassName", "PAIRS90")]
    design.line("D_P", directive=params)
    design.line("D_N", directive=params)
    prj, compiled, netlist = design.finish(
        tmp_path, prj_extra="[PrjClassGen]\nNetClassManualEnabled=0\n")
    pair = DiffPair("D", "D_P", "D_N", None)
    model = run(prj, compiled, netlist, pcb_of(netlist, design.uids, pairs=[pair]))
    (finding,) = model["findings"]
    assert (finding["check"], finding["severity"]) == ("directive_class_missing", "info")
    assert finding["option_off"] == "Generate user-defined net classes"
    row = next(r for r in model["rows"] if r["source"] == "directive_pair_class")
    assert row["status"] == "not_generated" and model["in_sync"]
    other = run(prj, compiled, netlist, pcb_of(netlist, design.uids, pairs=[pair], classes=[
        net_class("PAIRS90", "X", kind="diff_pair")]))
    (finding,) = other["findings"]
    assert (finding["check"], finding["severity"]) == ("class_members_differ", "info")
    assert "does not change the class" in finding["issue"]


# ---------- rules ----------

def test_rule_text_decoding_reads_properties_and_descriptions():
    properties = decode_rule_text("RULEKIND=Width~2NAME=W_A~2MINLIMIT=10mil~2MAXLIMIT=0.5mm"
                                  "~2SCOPE1EXPRESSION=InNet('A')")
    assert properties["format"] == "properties" and properties["kind"] == "Width"
    assert properties["name"] == "W_A" and properties["scope"] == "InNet('A')"
    assert properties["fields"]["min_width"] == 10.0
    assert properties["fields"]["max_width"] == pytest.approx(19.685, abs=0.001)
    semicolons = decode_rule_text("RuleKind=Clearance;Gap=8mil")
    assert semicolons["kind"] == "Clearance" and semicolons["fields"] == {"clearance": 8.0}
    description = decode_rule_text(
        "Differential Pairs Routing (Min Gap = 4mil) (Preferred Gap = 5mil) (Max Gap = 6mil)"
        " (Max Uncoupled Length = 500mil)")
    assert description["format"] == "description"
    assert description["kind"] == "DiffPairsRouting"
    assert description["fields"] == {"min_gap": 4.0, "preferred_gap": 5.0, "max_gap": 6.0,
                                     "max_uncoupled_length": 500.0}
    width = decode_rule_text("Width Constraint (Min=10mil, Max=20mil, Pref=15mil)")
    assert width["fields"] == {"min_width": 10.0, "max_width": 20.0, "preferred_width": 15.0}
    layered = decode_rule_text("RULEKIND=DiffPairsRouting|TOPLAYER_MINGAP=5mil")
    assert layered["fields"] == {"min_gap@TOPLAYER": 5.0}
    unknown = decode_rule_text("something else entirely")
    assert unknown["kind"] is None and unknown["format"] is None


def test_rule_values_are_read_whole_with_their_units():
    def fields(text):
        return decode_rule_text(text)["fields"]

    assert fields("Width Constraint (Min Width = 1e1mil)") == {"min_width": 10.0}
    assert fields("Width Constraint (Min Width = 0.5cm)")["min_width"] == pytest.approx(
        196.85, abs=0.01)
    assert fields("Width Constraint (Min Width = 254um) (Max Width = .5mm)") == pytest.approx(
        {"min_width": 10.0, "max_width": 19.685}, abs=0.001)
    assert fields("Width Constraint (Max Width = 0.01inch)") == pytest.approx(
        {"max_width": 10.0})
    assert fields("Width Constraint (Max Width = 20mil.)") == {"max_width": 20.0}
    # A value with an unknown unit is not read as its leading digits.
    assert fields("Width Constraint (Min Width = 10x) (Max Width = 12km)") == {}
    assert fields("RULEKIND=Width|MINLIMIT=1e1mil|MAXLIMIT=10x") == {"min_width": 10.0}


def test_a_rule_parameter_matches_the_pcb_rule_and_compares_values(tmp_path):
    design = Design()
    text = "Width Constraint (Min Width = 10mil) (Max Width = 20mil) (Preferred Width = 15mil)"
    design.line("SIG_A", directive=[("Rule", text)])
    prj, compiled, netlist = design.finish(tmp_path)
    same = rule("Width", "Width_SIG_A", "InNet('SIG_A')", MINLIMIT="10mil", MAXLIMIT="20mil",
                PREFEREDWIDTH="15mil")
    model = run(prj, compiled, netlist, pcb_of(netlist, design.uids, rules=[same]))
    row = rows(model, "rule")["Width"]
    assert (row["status"], row["pcb_name"], row["rule_kind"]) == (
        "transferred", "Width_SIG_A", "Width")
    assert not model["findings"]
    wider = rule("Width", "Width_SIG_A", "InNet('SIG_A')", MINLIMIT="10mil", MAXLIMIT="25mil",
                 PREFEREDWIDTH="15mil")
    model = run(prj, compiled, netlist, pcb_of(netlist, design.uids, rules=[wider]))
    (finding,) = model["findings"]
    assert (finding["check"], finding["severity"]) == ("rule_values_differ", "warning")
    assert finding["differences"] == [{"field": "max_width", "schematic_mil": 20.0,
                                       "pcb_mil": 25.0}]


def test_a_rule_is_matched_by_unique_id_first(tmp_path):
    design = Design()
    design.line("SIG_A", directive=[("Rule", "Clearance Constraint (Gap = 8mil)", "RID1")])
    prj, compiled, netlist = design.finish(tmp_path)
    by_id = rule("Clearance", "Anything", "InNetClass('X')", uid="RID1",
                 from_schematic=False, GENERICCLEARANCE="8mil")
    model = run(prj, compiled, netlist, pcb_of(netlist, design.uids, rules=[by_id]))
    assert rows(model, "rule")["Clearance"]["pcb_name"] == "Anything"
    assert not model["findings"]


def test_missing_and_unreadable_rules(tmp_path):
    design = Design()
    design.line("SIG_A", directive=[("Rule", "Width Constraint (Min Width = 10mil)")])
    design.line("SIG_B", directive=[("Rule", "no rule here")])
    prj, compiled, netlist = design.finish(tmp_path)
    model = run(prj, compiled, netlist, pcb_of(netlist, design.uids))
    assert checks(model) == [("directive_rule_missing", "warning"),
                             ("rule_text_not_decoded", "info")]
    assert model["summary"]["rule"]["not_decoded"] == 1
    assert any("Rule parameters are decoded leniently" in note for note in model["notes"])


def test_diff_pair_rule_values_compare_per_layer(tmp_path):
    design = Design()
    text = "Differential Pairs Routing (Min Gap = 4mil) (Max Gap = 7mil)"
    design.line("D_P", directive=[*PAIR, ("Rule", text)])
    design.line("D_N", directive=PAIR)
    prj, compiled, netlist = design.finish(tmp_path)
    pcb_rule = rule("DiffPairsRouting", "DP_D", "InDifferentialPair('D')", MINLIMIT="4mil",
                    MOSTFREQGAP="5mil", MAXLIMIT="6mil")
    model = run(prj, compiled, netlist, pcb_of(netlist, design.uids, rules=[pcb_rule],
                                                pairs=[DiffPair("D", "D_P", "D_N", None)]))
    (finding,) = model["findings"]
    assert finding["check"] == "rule_values_differ"
    (difference,) = finding["differences"]
    assert difference["field"] == "max_gap" and difference["schematic_mil"] == 7.0
    pcb = difference["pcb_mil"]
    assert set(pcb.values() if isinstance(pcb, dict) else [pcb]) == {6.0}


def test_a_rule_directive_on_a_bus_owns_the_rule_of_each_member_net(tmp_path):
    design = Design()
    design.line("D0")
    design.line("D1")
    sheet = design.sheet()
    sheet.bus((300, 100), (300, 200))
    sheet.label("D[0..1]", 300, 150)
    sheet.parameter_set(300, 120, [("Rule", "Width Constraint (Min Width = 10mil)")])
    prj, compiled, netlist = design.finish(tmp_path, prj_extra=(
        "[PrjClassGen]\nNetClassAutoBusEnabled=0\n"))
    rules = [rule("Width", "Width_D0", "InNet('D0')", MINLIMIT="10mil"),
             rule("Width", "Width_D1", "InNet('D1')", MINLIMIT="12mil")]
    model = run(prj, compiled, netlist, pcb_of(netlist, design.uids, rules=rules))
    row = rows(model, "rule")["Width"]
    assert row["pcb_rules"] == ["Width_D0", "Width_D1"]
    (finding,) = model["findings"]  # neither rule is stale
    assert finding["check"] == "rule_values_differ"
    assert finding["differences"] == [{"field": "min_width", "schematic_mil": 10.0,
                                       "pcb_mil": 12.0, "pcb_rule": "Width_D1"}]
    assert "pcb_items_without_directive" not in model["summary"]


def test_stale_pcb_items_are_info_and_power_port_supply_rules_are_not(tmp_path):
    design = Design()
    design.line("D_P", directive=PAIR)
    design.line("D_N", directive=PAIR)
    design.line("SIG_A", directive=CLASS_HS)
    design.line("SIG_B")
    prj, compiled, netlist = design.finish(tmp_path)
    pcb = pcb_of(netlist, design.uids,
                 pairs=[DiffPair("D", "D_P", "D_N", None), DiffPair("X", "SIG_B", "SIG_A", None)],
                 classes=[net_class("HS", "SIG_A"), net_class("OLD", "SIG_B")],
                 rules=[rule("Width", "Gone", "InNet('SIG_B')"),
                        rule("SupplyNets", "Supply", "InNet('GND')", VOLTAGE="0"),
                        rule("Width", "Mine", "All", from_schematic=False)])
    model = run(prj, compiled, netlist, pcb)
    stale = {(f["kind"], f["name"]) for f in model["findings"] if f["check"] == "stale_pcb_item"}
    assert stale == {("rule", "Gone"), ("diff_pair", "X"), ("net_class", "OLD")}
    assert all(f["severity"] == "info" for f in model["findings"])
    assert model["summary"]["pcb_items_without_directive"] == {
        "rules": 1, "pairs": 1, "net_classes": 1, "supply_net_rules_from_power_ports": 1}


# ---------- coverage: buses, harnesses, blankets ----------

def test_a_directive_on_a_bus_covers_its_members_and_the_bus_gets_a_class(tmp_path):
    design = Design()
    design.line("D0")
    design.line("D1")
    sheet = design.sheet()
    sheet.bus((300, 100), (300, 200))
    sheet.label("D[0..1]", 300, 150)
    sheet.parameter_set(300, 120, [("ClassName", "DATA")])
    prj, compiled, netlist = design.finish(tmp_path)
    (item,) = compiled.erc.parameter_sets
    assert (item.via, item.nets, item.attached) == ("bus", ["D0", "D1"], True)
    (bus,) = compiled.erc.buses
    assert (bus.name, bus.nets) == ("D[0..1]", ["D0", "D1"])
    model = run(prj, compiled, netlist, pcb_of(netlist, design.uids, classes=[
        net_class("DATA", "D0", "D1"), net_class("D[0..1]", "D0", "D1")]))
    assert {(r["name"], r["source"], r["status"]) for r in model["rows"]
            if r["kind"] == "net_class"} == {("DATA", "directive", "transferred"),
                                             ("D[0..1]", "bus", "transferred")}
    missing = run(prj, compiled, netlist, pcb_of(netlist, design.uids, classes=[
        net_class("DATA", "D0", "D1")]))
    assert checks(missing) == [("bus_class_missing", "info")]


def test_a_directive_on_a_signal_harness_covers_its_entries_nets(tmp_path):
    design = Design()
    sheet = design.sheet()
    harness_point, (sda, scl) = sheet.harness_connector((400, 300), (20, 40),
                                                        [("SDA", 1), ("SCL", 2)])
    for net, (x, y) in (("SDA", sda), ("SCL", scl)):
        uid = sheet.part(f"J{net}", [("1", x - 20, y), ("2", x - 30, y)])
        design.uids[f"J{net}"] = uid
        sheet.wire((x - 20, y), (x, y))
        sheet.label(net, x - 10, y)
        sheet.power(GND, x - 30, y)
    sheet.harness_line(harness_point, (harness_point[0] + 40, harness_point[1]))
    sheet.parameter_set(harness_point[0] + 20, harness_point[1], [("ClassName", "I2C")])
    prj, compiled, netlist = design.finish(tmp_path)
    (item,) = compiled.erc.parameter_sets
    assert item.via == "harness" and sorted(item.nets) == ["SCL", "SDA"]


def test_a_directive_on_a_blanket_edge_covers_the_nets_under_it(tmp_path):
    design = Design()
    design.line("SIG_A")
    design.line("SIG_B")
    design.line("SIG_C")
    sheet = design.sheet()
    sheet.blanket((40, 90), (110, 90), (110, 130), (40, 130))
    sheet.parameter_set(40, 110, [("ClassName", "HS")])
    prj, compiled, netlist = design.finish(tmp_path)
    (item,) = compiled.erc.parameter_sets
    assert item.via == "blanket" and item.attached
    assert sorted(item.nets) == ["SIG_A", "SIG_B"]
    model = run(prj, compiled, netlist, pcb_of(netlist, design.uids,
                                                classes=[net_class("HS", "SIG_A", "SIG_B")]))
    assert rows(model, "net_class")["HS"]["status"] == "transferred"


def test_directives_on_no_net_are_counted_not_compared(tmp_path):
    design = Design()
    design.line("SIG_A")
    design.sheet().parameter_set(900, 900, CLASS_HS)
    prj, compiled, netlist = design.finish(tmp_path)
    model = run(prj, compiled, netlist, pcb_of(netlist, design.uids))
    assert model["summary"]["directives_on_no_net"] == 1
    assert model["directives_on_no_net"][0]["via"] == "nothing"
    assert not rows(model, "net_class")


# ---------- per-sheet component classes and rooms ----------

CHILD_ON = {"Child.SchDoc": {"ClassGenCCAutoEnabled": 1, "ClassGenCCAutoRoomEnabled": 1}}


def _hierarchy(tmp_path, sheet_options=CHILD_ON, prj_extra=""):
    design = Design()
    top = design.sheet("Top.SchDoc")
    top.symbol("U_Child", "Child.SchDoc")
    design.line("SIG_A", sheet="Top.SchDoc")
    design.line("SIG_B", sheet="Child.SchDoc")
    design.line("SIG_C", sheet="Child.SchDoc")
    prj, compiled, netlist = design.finish(tmp_path, sheet_options=sheet_options,
                                           prj_extra=prj_extra)
    return design, prj, compiled, netlist


def test_a_child_sheet_gets_a_component_class_and_a_room(tmp_path):
    design, prj, compiled, netlist = _hierarchy(tmp_path)
    child_parts = sorted(p.physical for p in compiled.erc.parts if p.sheet == "Child")
    assert [i.designator for i in compiled.erc.instances] == ["", "U_Child"]
    room = rule("RoomDefinition", "U_Child", "InComponentClass('U_Child')",
                from_schematic=False)
    model = run(prj, compiled, netlist, pcb_of(
        netlist, design.uids, classes=[net_class("U_Child", *child_parts, kind="component")],
        rules=[room]))
    assert rows(model, "component_class")["U_Child"]["status"] == "transferred"
    assert rows(model, "room")["U_Child"]["status"] == "transferred"
    assert "Top" not in rows(model, "component_class")  # the top of a hierarchy has none
    assert not model["findings"]


def test_missing_component_class_and_rooms(tmp_path):
    design, prj, compiled, netlist = _hierarchy(tmp_path)
    model = run(prj, compiled, netlist, pcb_of(netlist, design.uids))
    assert checks(model) == [("component_class_missing", "warning"), ("room_missing", "info")]
    room = next(f for f in model["findings"] if f["check"] == "room_missing")
    assert room["classes"] == ["U_Child"] and room["count"] == 1


def test_sheet_options_off_leave_classes_and_rooms_out(tmp_path):
    design, prj, compiled, netlist = _hierarchy(tmp_path, sheet_options={
        "Child.SchDoc": {"ClassGenCCAutoEnabled": 0, "ClassGenCCAutoRoomEnabled": 0}})
    model = run(prj, compiled, netlist, pcb_of(netlist, design.uids))
    assert not model["findings"]
    assert rows(model, "component_class")["U_Child"]["status"] == "not_generated"
    assert model["summary"]["component_class"]["disabled_by_options"] == 1


def test_component_class_members_follow_the_board(tmp_path):
    design, prj, compiled, netlist = _hierarchy(tmp_path)
    model = run(prj, compiled, netlist, pcb_of(
        netlist, design.uids, classes=[net_class("U_Child", "R2", kind="component")]))
    finding = next(f for f in model["findings"] if f["check"] == "class_members_differ")
    assert finding["kind"] == "component_class" and finding["severity"] == "warning"
    assert finding["missing_from_pcb_class"] == ["R3", "U2"]


def test_parts_update_pcb_may_not_place_are_in_no_component_class(tmp_path):
    design = Design()
    top = design.sheet("Top.SchDoc")
    top.symbol("U_Child", "Child.SchDoc")
    top.symbol("U_Mech", "Mech.SchDoc", at=(300, 500))
    design.line("SIG_A", sheet="Top.SchDoc")
    design.line("SIG_B", sheet="Child.SchDoc")
    design.line("SIG_C", sheet="Child.SchDoc")
    design.sheet("Child.SchDoc").part("MH1", [], kind=1, at=(500, 500))  # mechanical
    design.sheet("Mech.SchDoc").part("MH2", [], at=(500, 500))  # no footprint
    on = {"ClassGenCCAutoEnabled": 1, "ClassGenCCAutoRoomEnabled": 0}
    prj, compiled, netlist = design.finish(
        tmp_path, sheet_options={"Child.SchDoc": on, "Mech.SchDoc": on})
    assert {"MH1", "MH2"} <= {p.physical for p in compiled.erc.parts}
    child = sorted(p.physical for p in compiled.erc.parts
                   if p.sheet == "Child" and p.physical != "MH1")
    model = run(prj, compiled, netlist, pcb_of(
        netlist, design.uids, classes=[net_class("U_Child", *child, kind="component")]))
    row = rows(model, "component_class")["U_Child"]
    assert row["status"] == "transferred" and row["parts_not_on_pcb"] == ["MH1"]
    assert row["parts"] == len(child)
    # A sheet none of whose parts reach the PCB gets no class.
    assert "U_Mech" not in rows(model, "component_class")
    assert not model["findings"]
    # A PCB class may list such a part all the same: not an extra member either.
    listed = run(prj, compiled, netlist, pcb_of(netlist, design.uids, classes=[
        net_class("U_Child", *child, "MH1", kind="component")]))
    assert rows(listed, "component_class")["U_Child"]["status"] == "transferred"
    assert not listed["findings"]


def test_a_room_follows_the_schematic_class_name_when_matched_by_members(tmp_path):
    design, prj, compiled, netlist = _hierarchy(tmp_path)
    child_parts = sorted(p.physical for p in compiled.erc.parts if p.sheet == "Child")
    room = rule("RoomDefinition", "OLD_CHILD", "InComponentClass('OLD_CHILD')",
                from_schematic=False)
    model = run(prj, compiled, netlist, pcb_of(
        netlist, design.uids, classes=[net_class("OLD_CHILD", *child_parts, kind="component")],
        rules=[room]))
    assert rows(model, "component_class")["U_Child"]["pcb_name"] == "OLD_CHILD"
    assert set(rows(model, "room")) == {"U_Child"}
    room_row = rows(model, "room")["U_Child"]
    assert (room_row["status"], room_row["pcb_component_class"]) == ("transferred", "OLD_CHILD")
    assert checks(model) == [("class_name_differs", "info")]


def test_component_class_generation_off_leaves_a_renamed_class_alone(tmp_path):
    for value, severity in ((1, "warning"), (0, "info")):
        design = Design()
        design.sheet("Top.SchDoc").symbol("U_Child", "Child.SchDoc")
        design.line("SIG_A", sheet="Top.SchDoc")
        for net in ("SIG_B", "SIG_C", "SIG_D", "SIG_E"):
            design.line(net, sheet="Child.SchDoc")
        prj, compiled, netlist = design.finish(tmp_path / str(value), sheet_options={
            "Child.SchDoc": {"ClassGenCCAutoEnabled": value, "ClassGenCCAutoRoomEnabled": 0}})
        child = sorted(p.physical for p in compiled.erc.parts if p.sheet == "Child")
        model = run(prj, compiled, netlist, pcb_of(netlist, design.uids, classes=[
            net_class("OLD_CHILD", *child[:-1], kind="component")]))  # 4 of its 5 parts
        found = {f["check"]: f for f in model["findings"]}
        assert set(found) == {"class_name_differs", "class_members_differ"}
        assert found["class_members_differ"]["severity"] == severity
        if value:
            assert "would rename it" in found["class_name_differs"]["issue"]
            continue
        assert all(f["severity"] == "info" and "option_off" in f for f in found.values())
        assert "does not rename it" in found["class_name_differs"]["issue"]
        assert "does not change the class" in found["class_members_differ"]["issue"]


def test_user_defined_component_classes_from_a_class_name_parameter(tmp_path):
    design = Design()
    design.line("SIG_A")
    prj, compiled, netlist = design.finish(
        tmp_path, prj_extra="[PrjClassGen]\nCompClassManualEnabled=1\n")
    netlist["components"]["R1"]["params"]["classname"] = "POWER_PARTS"
    model = run(prj, compiled, netlist, pcb_of(netlist, design.uids))
    finding = next(f for f in model["findings"] if f["check"] == "component_class_missing")
    assert finding["source"] == "component_parameter" and finding["parts"] == ["R1"]


# ---------- Constraint Manager ----------

def test_a_constraint_manager_project_makes_every_finding_info(tmp_path):
    design = Design()
    design.line("SIG_A", directive=CLASS_HS)
    design.line("D_P", directive=PAIR)
    design.line("D_N", directive=PAIR)
    prj, compiled, netlist = design.finish(tmp_path)
    folder = tmp_path / "Settings" / "Constraints"
    folder.mkdir(parents=True)
    (folder / "Constraints.xml").write_text(
        "<Constraints><NetClass/><NetClass/><DiffPair/></Constraints>", encoding="utf-8")
    kind = detect_project_type(prj)
    assert kind["type"] == "constraint_manager"
    assert kind["constraints_xml"]["top_level_elements"] == {"NetClass": 2, "DiffPair": 1}
    assert kind["evidence"][0]["source"] == "constraints_file"
    model = run(prj, compiled, netlist, pcb_of(netlist, design.uids), project_type=kind)
    assert {f["severity"] for f in model["findings"]} == {"info"}
    assert {f["reason"] for f in model["findings"]} == {"constraint_manager_project"}
    assert model["in_sync"]
    assert model["summary"]["directives_ignored_unless_imported"] == 2
    assert any("Constraint Manager project" in note for note in model["notes"])


def _constraint_stream(content: bytes) -> bytes:
    text = base64.b64encode(zlib.compress(content)).decode("ascii") + "\0"
    data = text.encode("utf-16-le")
    return struct.pack("<I", len(data)) + data


def test_the_pcb_constraint_stream_tells_a_stub_from_constraint_data(tmp_path):
    stub = tmp_path / "Stub.PcbDoc"
    write_ole(stub, {"ConstraintManager/Data": _constraint_stream(b"")})
    kind = detect_project_type(None, str(stub))
    assert kind["type"] == "classic" and kind["pcb_constraint_stream"]["stub"] is True
    full = tmp_path / "Full.PcbDoc"
    write_ole(full, {"ConstraintManager/Data": _constraint_stream(b"<Root><A/><A/></Root>")})
    kind = detect_project_type(None, str(full))
    assert kind["type"] == "constraint_manager"
    assert kind["pcb_constraint_stream"]["outline"]["top_level_elements"] == {"A": 2}
    assert detect_project_type(None, str(tmp_path / "missing.PcbDoc"))["type"] == "classic"


def test_the_constraint_manager_flow_switch_is_evidence(tmp_path):
    prj = tmp_path / "Board.PrjPcb"
    for value in ("1", "2", "-1", "TRUE"):
        prj.write_text(f"[Design]\nConstraintManagerFlow={value}\n", encoding="utf-8")
        kind = detect_project_type(str(prj))
        assert kind["type"] == "constraint_manager"
        assert kind["evidence"] == [{"source": "project_option", "section": "Design",
                                     "key": "ConstraintManagerFlow", "value": value}]
        assert kind["constraint_manager_flow"]["value"] == value
    # Altium writes 0 in a classic project.
    prj.write_text("[Design]\nConstraintManagerFlow=0\n", encoding="utf-8")
    kind = detect_project_type(str(prj))
    assert kind["type"] == "classic" and kind["constraint_manager_flow"]["value"] == "0"
    # The switch at 0 outweighs Constraint Manager traces left in the folder.
    folder = tmp_path / "Settings" / "Constraints"
    folder.mkdir(parents=True)
    (folder / "Constraints.xml").write_text("<Constraints/>", encoding="utf-8")
    kind = detect_project_type(str(prj))
    assert kind["type"] == "classic" and kind["evidence"] == []
    assert kind["contrary_evidence"][0]["source"] == "constraints_file"
    prj.write_text("[Design]\nOther=1\n", encoding="utf-8")  # no switch: the file decides
    assert detect_project_type(str(prj))["type"] == "constraint_manager"
    (folder / "Constraints.xml").unlink()
    assert detect_project_type(str(prj))["type"] == "classic"
    (tmp_path / "Constraints.xml").write_text("<Constraints/>", encoding="utf-8")
    kind = detect_project_type(str(prj))
    assert kind["type"] == "constraint_manager"
    assert kind["constraints_xml"]["file"] == "Constraints.xml"


def test_other_options_naming_the_constraint_manager_are_listed_not_evidence(tmp_path):
    prj = tmp_path / "Board.PrjPcb"
    prj.write_text("[Design]\nConstraintManagerFlow=0\nShowConstraintManagerHints=1\n"
                   "[Preferences]\nConstraintManagerFlow=1\n[ConstraintManager Settings]\n"
                   "Anything=1\n", encoding="utf-8")
    kind = detect_project_type(str(prj))
    assert kind["type"] == "classic" and kind["evidence"] == []
    assert kind["other_project_options"] == [
        {"section": "Design", "key": "ShowConstraintManagerHints", "value": "1"},
        {"section": "Preferences", "key": "ConstraintManagerFlow", "value": "1"},
        {"section": "ConstraintManager Settings"}]


def test_class_generation_decodes_options_and_defaults():
    options = PrjOptions(class_generation={"NetClassAutoBusEnabled": "0"},
                         sheet_class_generation=[("Sub\\Power.SchDoc", {
                             "ClassGenCCAutoEnabled": "1", "ClassGenCCAutoRoomEnabled": "0",
                             "ClassGenNCAutoScope": "None"})],
                         comparison=[{"Kind": "Rule", "InclAllRules": "0"}])
    generation = class_generation(options)
    assert generation["project"]["bus_net_classes"] is False
    assert "user_defined_net_classes" in generation["defaulted"]
    assert generation["sheets"]["power"] == {"component_class": True, "room": False,
                                             "net_classes": "None", "structure_class": False}
    assert generation["comparator"] == {"include_rules_without_unique_id": False}


# ---------- paging, filters, errors ----------

def _many_pairs(tmp_path, count: int = 12):
    design = Design()
    for i in range(count):
        design.line(f"L{i}_P", directive=PAIR)
        design.line(f"L{i}_N", directive=PAIR)
    prj, compiled, netlist = design.finish(tmp_path)
    return design, prj, compiled, netlist


def test_findings_and_rows_page_and_filter(tmp_path):
    design, prj, compiled, netlist = _many_pairs(tmp_path)
    model = run(prj, compiled, netlist, pcb_of(netlist, design.uids))
    page = page_directive_sync(model, offset=0, limit=5, rows_offset=0, rows_limit=4)
    assert len(page["findings"]) == 5 and page["total"] == 12 and page["next_offset"] == 5
    assert len(page["directives"]) == 4 and page["next_directives_offset"] == 4
    # 12 pairs and the sheet's component class (its generation is off).
    assert page["directives_total"] == 13
    last = page_directive_sync(model, offset=10, limit=5, rows_offset=12, rows_limit=4)
    assert len(last["findings"]) == 2 and not last["has_more"]
    assert last["directives"][0]["status"] == "not_generated"
    assert "next_directives_offset" not in last and last["directives_offset"] == 12
    narrowed = page_directive_sync(model, kind="net_class")
    assert narrowed["total"] == 0 and narrowed["filtered"] == {"kind": "net_class"}
    assert page_directive_sync(model, sheet="Main")["total"] == 12
    assert page_directive_sync(model, sheet="main.SchDoc")["total"] == 12
    assert page_directive_sync(model, sheet="Other")["total"] == 0
    assert page_directive_sync(model, kind="bogus")["error"] == "invalid_kind"
    assert set(KINDS) == set(model["summary"]) - {"parameter_sets"}


def test_the_checklist_item_leans_yes_only_on_directives_that_reached_the_board(tmp_path):
    from services.review_checklist import bundled_dir, evaluate_item, load_bundled

    item = next(item for checklist in load_bundled(bundled_dir()).values()
                for item in checklist["items"] if item["id"] == "SCH-SIG-09")

    def lean(model):
        return evaluate_item(item, {"data": page_directive_sync(model)}).get("lean")

    design = Design()
    design.line("D_P", directive=PAIR)
    design.line("D_N", directive=PAIR)
    prj, compiled, netlist = design.finish(tmp_path / "pairs")
    pair = DiffPair("D", "D_P", "D_N", None)
    model = run(prj, compiled, netlist, pcb_of(netlist, design.uids, pairs=[pair]))
    assert model["summary"]["diff_pair"]["from_directives"] == 1
    assert lean(model) == "yes"
    assert lean(run(prj, compiled, netlist, pcb_of(netlist, design.uids))) == "no"
    # Bus net classes alone are not directives: no lean.
    design = Design()
    design.line("D0")
    design.line("D1")
    design.sheet().bus((300, 100), (300, 200))
    design.sheet().label("D[0..1]", 300, 150)
    prj, compiled, netlist = design.finish(tmp_path / "bus")
    model = run(prj, compiled, netlist, pcb_of(netlist, design.uids, classes=[
        net_class("D[0..1]", "D0", "D1")]))
    assert model["summary"]["net_class"] == {**model["summary"]["net_class"], "items": 1,
                                             "transferred": 1, "from_directives": 0}
    assert lean(model) is None


def test_no_compile_is_an_error():
    assert build_directive_sync(None, None, None)["error"] == "no_netlist"


def test_the_cache_reuses_a_model_for_the_same_compile_and_netlist(tmp_path):
    design, prj, compiled, netlist = _many_pairs(tmp_path, 1)
    index = PcbIndex(pcb_of(netlist, design.uids), netlist)
    options = parse_prj_options(prj)
    first = build_directive_sync_cached(compiled, index, options, netlist)
    assert build_directive_sync_cached(compiled, index, options, netlist) is first
    assert build_directive_sync_cached(compiled, index, options, dict(netlist)) is not first


# ---------- the MCP tool ----------

@pytest.fixture
def tool_env(tmp_path):
    """main's project, netlist, PCB index and offline compile stand-ins."""
    design, prj, compiled, netlist = _many_pairs(tmp_path, 30)
    index = PcbIndex(pcb_of(netlist, design.uids), netlist)
    loaded = {"name": "Board", "prj_pcb_path": prj, "root_dir": str(tmp_path),
              "pcb_doc_paths": [str(tmp_path / "Board.PcbDoc")],
              "sheets": [{"name": "Main", "path": str(tmp_path / "Main.SchDoc")}]}

    class Altium:
        def compiled_netlist(self, *_):
            return compiled

    with patch.object(main, "_require_project", return_value=(loaded, netlist, None)), \
         patch.object(main, "_pcb_index_or_error", return_value=(index, None)), \
         patch.object(main, "_altium", Altium()):
        yield


def test_the_tool_pages_and_shrinks_to_fit(tool_env):
    result = json.loads(main.check_directive_sync())
    assert result["pcb_doc"] == "Board.PcbDoc" and result["total"] == 30
    assert result["summary"]["diff_pair"]["missing"] == 30
    assert "page_limited_to" not in result
    with patch.object(main, "QUERY_NET_MAX_RESULT_SIZE_CHARS", 9000):
        small = json.loads(main.check_directive_sync())
    assert small["page_limited_to"] < 100 and len(json.dumps(small, indent=2)) <= 9000
    assert small["next_offset"] == small["page_limited_to"]
    second = json.loads(main.check_directive_sync(offset=small["next_offset"]))
    assert second["offset"] == small["next_offset"]
    rows_page = json.loads(main.check_directive_sync(directives_offset=20))
    assert rows_page["directives_offset"] == 20
    assert len(rows_page["directives"]) == rows_page["directives_total"] - 20
    assert json.loads(main.check_directive_sync(kind="room"))["total"] == 0


def test_the_tool_reports_missing_pcb_netlist_and_compile(tool_env):
    with patch.object(main, "_pcb_index_or_error", return_value=(
            None, {"error": "no_pcb_document", "message": "none"})):
        assert json.loads(main.check_directive_sync())["error"] == "no_pcb_document"
    with patch.object(main, "_require_project",
                      side_effect=main.ProjectNotReady(main._netlist_required_error())):
        assert json.loads(main.check_directive_sync())["error"] == "netlist_required"

    class Broken:
        def compiled_netlist(self, *_):
            raise RuntimeError("no sheet of the project could be read")

    with patch.object(main, "_altium", Broken()):
        result = json.loads(main.check_directive_sync())
    assert result["error"] == "compile_failed" and "RuntimeError" in result["message"]


def test_the_tool_is_documented_everywhere():
    root = Path(__file__).parents[1]
    manifest = json.loads((root / "manifest.json").read_text("utf-8"))
    assert "check_directive_sync" in {tool["name"] for tool in manifest["tools"]}
    assert "| `check_directive_sync` |" in (root / "README.md").read_text("utf-8")
    assert main.SERVER_INSTRUCTIONS.index("check_directive_sync") > 4096
    assert "check_directive_sync" in main.NETLIST_TOOLS
    changelog = (root / "CHANGELOG.md").read_text("utf-8")
    start = changelog.index("## [Unreleased]")
    end = changelog.index("\n## [", start + 1)
    assert "check_directive_sync" in changelog[start:end]
