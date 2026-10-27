"""get_constraints_brief and generate_rule_file (services/constraints_brief.py).

Synthetic designs only, with generic names: a netlist dict built from part tables (as
tests/test_decoupling.py does), an optional PcbData made from it, and plans written by
hand."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import server.main as main
from parsers.pcb_doc import (
    DiffPair, PcbClass, PcbComponent, PcbData, PcbRule, XSignal, decode_board, decode_rules,
    parse_text_properties,
)
from services.bom import VariantPlan
from services.constraints_brief import (
    DDR4_DQ_TO_DQS_MIL, DEFAULT_MAX_UNCOUPLED_MIL, FORMATS, MIL_PER_MM, PLAN_SCHEMA, SECTIONS,
    TYPICAL_PAIRS, Assumptions, build_brief, build_brief_cached, build_rule_files,
    inline_texts, parse_sections, record_line, render_brief, round_trip, rule_record,
    validate_plan, write_rule_files,
)
from services.constraints_review import INTERFACE_TARGETS
from services.hv_clearance import ipc2221_mm
from services.pcb_index import PcbIndex
from services.pcb_rules import rule_values
from services.power_paths import OZ_MIL, ipc2221_width
from tests.test_impedance import BOTTOM, L2, L3, PROFILE_PROPS, TOP, TRACES
from tests.test_pcb_index import _board, _pad

GND = "GND"


def part(ref, pins, description="", sheet="Main", value=None):
    """(refdes, {pin: (pin name, net)}, info)."""
    return ref, pins, {"description": description, "sheet": sheet, "value": value}


DESIGN = [
    # A processor with USB, a crystal and a DDR4 byte lane.
    part("U1", {"1": ("VDD", "+3V3"), "2": ("VSS", GND), "3": ("USB_DP", "USB_D_P"),
                "4": ("USB_DM", "USB_D_N"), "5": ("OSC_IN", "XIN"), "6": ("OSC_OUT", "XOUT"),
                **{str(10 + n): (f"DDR_DQ{n}", f"DDR_DQ{n}") for n in range(8)},
                "20": ("DDR_DM0", "DDR_DM0"), "21": ("DDR_DQS0_P", "DDR_DQS0_P"),
                "22": ("DDR_DQS0_N", "DDR_DQS0_N"), "23": ("VDD", "+3V3")},
         "Microcontroller, ARM Cortex-M7 MCU", sheet="Processor"),
    part("C1", {"1": ("1", "+3V3"), "2": ("2", GND)}, "Chip Capacitor", "Processor", "100nF"),
    part("C2", {"1": ("1", "+3V3"), "2": ("2", GND)}, "Chip Capacitor", "Processor", "10uF"),
    part("Y1", {"1": ("1", "XIN"), "2": ("2", "XOUT")}, "Crystal 8 MHz", "Processor"),
    part("C3", {"1": ("1", "XIN"), "2": ("2", GND)}, "Chip Capacitor", "Processor", "18pF"),
    part("C4", {"1": ("1", "XOUT"), "2": ("2", GND)}, "Chip Capacitor", "Processor", "18pF"),
    part("U4", {**{str(1 + n): (f"DQ{n}", f"DDR_DQ{n}") for n in range(8)},
                "9": ("DM", "DDR_DM0"), "10": ("DQS_T", "DDR_DQS0_P"),
                "11": ("DQS_C", "DDR_DQS0_N"), "12": ("VDD", "+3V3"), "13": ("VSS", GND)},
         "DDR4 SDRAM 8Gb", sheet="Memory"),
    # USB connector with its ESD array.
    part("J1", {"1": ("VBUS", "VBUS"), "2": ("D-", "USB_D_N"), "3": ("D+", "USB_D_P"),
                "5": ("GND", GND)}, "USB Micro-B receptacle connector", "Interface"),
    part("D1", {"1": ("IO1", "USB_D_P"), "2": ("GND", GND), "3": ("IO2", "USB_D_N")},
         "TVS diode array, ESD protection", "Interface"),
    # A buck converter from a 12 V input to +3V3.
    part("J2", {"1": ("1", "+12V"), "2": ("2", GND)}, "DC power jack connector", "Power"),
    part("U2", {"1": ("VIN", "+12V"), "2": ("SW", "SW"), "3": ("FB", "FB"), "4": ("GND", GND),
                "5": ("EN", "+12V"), "6": ("BST", "BST")},
         "Synchronous buck converter, step-down regulator", sheet="Power"),
    part("L1", {"1": ("1", "SW"), "2": ("2", "+3V3")}, "Power inductor 4.7uH", "Power"),
    part("C5", {"1": ("1", "+12V"), "2": ("2", GND)}, "Chip Capacitor", "Power", "10uF"),
    part("C6", {"1": ("1", "+3V3"), "2": ("2", GND)}, "Chip Capacitor", "Power", "22uF"),
    part("C7", {"1": ("1", "BST"), "2": ("2", "SW")}, "Chip Capacitor", "Power", "100nF"),
    part("R1", {"1": ("1", "+3V3"), "2": ("2", "FB")}, "Chip Resistor", "Power", "100k"),
    part("R2", {"1": ("1", "FB"), "2": ("2", GND)}, "Chip Resistor", "Power", "22k"),
    # A radio with its antenna path.
    part("U5", {"1": ("VDD", "+3V3"), "2": ("GND", GND), "3": ("RF_OUT", "RF_OUT")},
         "Bluetooth low energy radio transceiver 2.4 GHz", sheet="Radio"),
    part("C8", {"1": ("1", "RF_OUT"), "2": ("2", "ANT_FEED")}, "Chip Capacitor", "Radio",
         "10pF"),
    part("J4", {"1": ("1", "ANT_FEED"), "2": ("2", GND)}, "U.FL coax connector", "Radio"),
    # A 48 V input to a hot-swap controller.
    part("J3", {"1": ("1", "+48V"), "2": ("2", GND)}, "Terminal block 2 position", "Input"),
    part("U3", {"1": ("VCC", "+48V"), "2": ("GND", GND), "3": ("OUT", "+48V_OUT")},
         "Hot swap controller", sheet="Input"),
]


def netlist_of(parts=DESIGN) -> dict:
    components, pin_to_net, nets, compiled = {}, {}, {}, {}
    for ref, pins, info in parts:
        entry = {"pins": {}, "params": {}, "kind": 0, "description": info["description"],
                 "value": info["value"], "sheet": info["sheet"]}
        if info["value"]:
            entry["params"] = {"value": info["value"]}
        for pin, (name, net) in pins.items():
            entry["pins"][pin] = {"name": name, "net": net}
            pin_to_net.setdefault(ref, {})[pin] = net
            nets.setdefault(net, []).append((ref, pin))
        components[ref] = entry
        compiled[ref] = {"footprint": "", "comment": info["value"] or ""}
    return {"nets": nets, "pin_to_net": pin_to_net, "components": components,
            "parts": compiled}


def pcb_of(netlist: dict, pairs=(), classes=(), rules=(), positions=None) -> PcbData:
    """The board Update PCB would make: one component per part (x from positions, in mil,
    else 1000 mil apart), one pad per pin."""
    nets = sorted(netlist["nets"])
    components, pads = [], []
    for number, (ref, pins) in enumerate(sorted(netlist["pin_to_net"].items())):
        x = (positions or {}).get(ref, number * 1000.0)
        components.append(PcbComponent(index=number, refdes=ref, source_uid_tail=None,
                                       source_unique_id=None, channel_path=None, x=x, y=0.0,
                                       rotation=0.0, side="top", footprint=None,
                                       designator=ref, comment=None))
        for offset, (pin, net) in enumerate(sorted(pins.items())):
            pads.append(_pad(nets.index(net), number, x + offset * 20.0, 0.0, name=pin))
    return PcbData(board=_board(), nets=nets, components=components, primitives=pads, pours=[],
                   layer_name_to_v6={"top layer": 1, "top": 1}, classes=list(classes),
                   diff_pairs=list(pairs), rules=list(rules))


def pcb_rule(kind: str, name: str, scope: str = "All", priority: int = 1,
             **properties) -> PcbRule:
    return PcbRule(name=name, kind=kind, kind_id=0, enabled=True, priority=priority,
                   scope1=scope, scope2="All", net_scope=None, comment="", unique_id="AAAAAAAA",
                   from_schematic=False, properties=dict(properties))


def pcb_class(name: str, kind: str, *members: str) -> PcbClass:
    return PcbClass(name=name, kind=kind, members=list(members), superclass=False,
                    auto_generated=False, unique_id=None)


def brief(parts=DESIGN, index=None, **kwargs) -> dict:
    netlist = netlist_of(parts)
    assumptions = kwargs.pop("assumptions", Assumptions(units="mil"))
    return build_brief(netlist, None, {}, index=index, assumptions=assumptions, **kwargs)


def classes_of(model: dict) -> dict[str, dict]:
    return {entry["name"]: entry for entry in model["net_classes"]}


def without(names: set[str]) -> list:
    return [item for item in DESIGN if item[0] not in names]


# ---------- net classes ----------

def test_pairs_are_grouped_by_interface_with_typical_targets():
    model = brief()
    found = classes_of(model)
    usb = found["DIFF_USB2"]
    assert [pair["pair"] for pair in usb["pairs"]] == ["USB_D"]
    assert usb["pairs"][0] == {"pair": "USB_D", "positive": "USB_D_P", "negative": "USB_D_N"}
    typical = usb["typical"]
    assert typical["differential_ohm"] == 90.0 and typical["in_pair_tolerance_mil"] == 50.0
    assert typical["typical_only"] is True and typical["row"] == "USB 2.0 (high speed)"
    assert typical["source"].startswith("TI SPRAAR7J")
    assert any("pair naming" in basis for basis in usb["evidence"])
    ddr = found["DIFF_DDR4_DQS"]
    assert ddr["typical"]["differential_ohm"] == 80.0
    assert ddr["typical"]["in_pair_tolerance_mil"] == pytest.approx(2.0)
    # No PcbDoc: nothing about the PCB is claimed.
    assert "pcb_pairs" not in usb["exists"]


def test_every_typical_row_names_an_interface_targets_row():
    rows = {row["interface"] for row in INTERFACE_TARGETS}
    for key, (name, row, ohm, skew, impedance, skew_text) in TYPICAL_PAIRS.items():
        assert name.startswith("DIFF_") and impedance and skew_text, key
        assert row is None or row in rows, key
        if row is None:
            assert ohm is None and skew is None, key


def test_pin_roles_name_an_interface_the_net_names_do_not_give():
    parts = [part("U1", {"1": ("USB_DP", "LINE_A_P"), "2": ("USB_DM", "LINE_A_N"),
                         "3": ("GND", GND)}, "Microcontroller"),
             part("U2", {"1": ("1", "LINE_A_P"), "2": ("2", "LINE_A_N"), "3": ("3", GND)},
                  "Hub")]
    usb = classes_of(brief(parts))["DIFF_USB2"]
    assert any("processor pin role: U1 USB_DP" in basis for basis in usb["evidence"])


def test_usb3_lanes_and_usb2_lines_on_one_port_are_split():
    parts = [part("U1", {"1": ("SSTXP", "USB3_SSTX_P"), "2": ("SSTXN", "USB3_SSTX_N"),
                         "3": ("DP", "USB3_D_P"), "4": ("DM", "USB3_D_N"), "5": ("GND", GND)},
                  "USB 3.0 bridge"),
             part("J1", {"1": ("1", "USB3_SSTX_P"), "2": ("2", "USB3_SSTX_N"),
                         "3": ("3", "USB3_D_P"), "4": ("4", "USB3_D_N"), "5": ("5", GND)},
                  "USB 3.0 Type-A receptacle connector")]
    found = classes_of(brief(parts))
    assert [pair["pair"] for pair in found["DIFF_USB3"]["pairs"]] == ["USB3_SSTX"]
    assert [pair["pair"] for pair in found["DIFF_USB2"]["pairs"]] == ["USB3_D"]


def test_pairs_through_magnetics_are_ethernet():
    parts = [part("J1", {"1": ("1", "TX_P"), "2": ("2", "TX_N"), "3": ("3", GND)},
                  "Header 3 position"),
             part("T1", {"1": ("1", "TX_P"), "2": ("2", "TX_N"), "3": ("3", "LINE_P"),
                         "4": ("4", "LINE_N")}, "LAN magnetics transformer module")]
    eth = classes_of(brief(parts))["DIFF_ETH"]
    assert {pair["pair"] for pair in eth["pairs"]} == {"TX", "LINE"}
    assert any("magnetics" in basis for basis in eth["evidence"])


def test_a_wide_inner_track_points_to_a_pour():
    found = classes_of(brief(assumptions=Assumptions(units="mil", current={"+3V3": 9.0})))
    assert "pour or plane" in found["PWR_10A"]["width"]["inner_note"]


def test_power_classes_by_current_with_an_ipc2221_width():
    model = brief(assumptions=Assumptions(units="mil", current={"+3V3": 1.5, "+12V": 0.4}))
    found = classes_of(model)
    two = found["PWR_2A"]
    assert two["members"] == ["+3V3"] and two["role"] == "power"
    width = two["width"]
    assert width["current_a"] == 2.0 and width["temp_rise_c"] == 10.0
    expected = ipc2221_width(2.0, 10.0, OZ_MIL, True)
    assert expected <= width["outer_min_width_mil"] <= expected + 0.1
    inner = ipc2221_width(2.0, 10.0, OZ_MIL, False)
    assert inner <= width["inner_min_width_mil"] <= inner + 0.1
    assert width["outer_copper"]["source"] == "assumed 1 oz (35 um)"
    assert "IPC-2221" in width["source"]
    assert found["PWR_0A5"]["members"] == ["+12V"]
    rule = next(rule for rule in model["plan"]["rules"] if rule["name"] == "Width_PWR_2A")
    assert rule["kind"] == "Width" and rule["scope"] == {"net_class": "PWR_2A"}
    assert rule["values"]["min_width"] == width["outer_min_width_mil"]
    assert rule["inner"]["min_width"] == width["inner_min_width_mil"]
    assert rule["values"]["max_width"] >= rule["values"]["preferred_width"]


def test_rails_without_a_current_get_a_class_and_no_width():
    model = brief()
    power = classes_of(model)["PWR"]
    assert power["current_unknown"] is True and power["width"] is None
    assert "+3V3" in power["members"] and GND not in power["members"]
    assert not any(rule["kind"] == "Width" for rule in model["plan"]["rules"])
    need = next(need for need in model["needs_input"] if need["item"] == "PWR")
    assert need["needs"] == "rail currents" and "current=" in need["how"]


def test_copper_and_rise_assumptions_change_the_width():
    thin = classes_of(brief(assumptions=Assumptions(units="mil", current={"+3V3": 3.0})))
    thick = classes_of(brief(assumptions=Assumptions(units="mil", current={"+3V3": 3.0},
                                                     copper_oz=2.0, temp_rise_c=20.0)))
    assert thick["PWR_3A"]["width"]["outer_min_width_mil"] < \
        thin["PWR_3A"]["width"]["outer_min_width_mil"]
    assert thick["PWR_3A"]["width"]["outer_copper"] == {
        "thickness_mil": round(2 * OZ_MIL, 3), "oz": 2.0, "source": "copper_oz"}


def test_high_voltage_nets_get_the_ipc2221_clearance():
    model = brief()
    hv = classes_of(model)["HV_50V"]
    assert "+48V" in hv["members"] and hv["role"] == "high_voltage"
    outer, _ = ipc2221_mm("B2", 48.0)
    inner, _ = ipc2221_mm("B1", 48.0)
    assert hv["clearance"]["outer_mil"] == pytest.approx(outer * MIL_PER_MM, abs=0.01)
    assert hv["clearance"]["inner_mil"] == pytest.approx(inner * MIL_PER_MM, abs=0.01)
    assert "IPC-2221B" in hv["clearance"]["source"]
    rule = next(rule for rule in model["plan"]["rules"] if rule["kind"] == "Clearance")
    assert rule["scope"] == {"net_class": "HV_50V"} and rule["scope2"] == "All"
    assert rule["values"]["clearance"] == pytest.approx(outer * MIL_PER_MM, abs=0.01)


def test_hv_given_by_the_user_and_name_only_hv_asks():
    model = brief(assumptions=Assumptions(units="mil", hv={"+48V_OUT": (400.0, 0.0, 0.0)}))
    assert "+48V_OUT" in classes_of(model)["HV_500V"]["members"]
    parts = [*DESIGN, part("J9", {"1": ("1", "MAINS_L"), "2": ("2", "MAINS_N")}, "Header")]
    asked = brief(parts)
    need = next(need for need in asked["needs_input"] if need["item"] == "HV")
    assert "working voltages" == need["needs"] and "hv=" in need["how"]


def test_rf_nets_follow_the_matching_network_only_with_evidence():
    rf = classes_of(brief())["RF_50"]
    assert rf["members"] == ["ANT_FEED", "RF_OUT"]
    assert rf["typical"]["single_ended_ohm"] == 50.0 and rf["typical"]["typical_only"]
    assert any("RF pin U5 RF_OUT" in basis for basis in rf["evidence"])
    # A coax connector alone (a clock on an SMA, say) is no RF evidence.
    plain = [item for item in DESIGN if item[0] not in ("U5", "C8", "J4")] + [
        part("U5", {"1": ("VDD", "+3V3"), "2": ("GND", GND), "3": ("CLKOUT", "CLK_SMA")},
             "Clock generator"),
        part("J4", {"1": ("1", "CLK_SMA"), "2": ("2", GND)}, "SMA coax connector")]
    assert "RF_50" not in classes_of(brief(plain))


# ---------- length matching ----------

def test_ddr_byte_lane_and_in_pair_tolerances():
    model = brief()
    found = {item["name"]: item for item in model["length_matching"]}
    lane = found["DDR_BYTE0"]
    assert lane["kind"] == "byte_lane"
    assert lane["members"] == [*(f"DDR_DQ{n}" for n in range(8)), "DDR_DM0", "DDR_DQS0_P",
                               "DDR_DQS0_N"]
    assert lane["tolerance"]["tolerance_mil"] == DDR4_DQ_TO_DQS_MIL
    assert lane["tolerance"]["typical_only"] and lane["tolerance"]["row"] == "DDR4"
    usb = found["DIFF_USB2"]
    assert usb["kind"] == "in_pair" and usb["tolerance"]["tolerance_mil"] == 50.0
    rules = {rule["name"]: rule for rule in model["plan"]["rules"]}
    assert rules["MatchedLengths_DDR_BYTE0"]["values"] == {
        "tolerance": DDR4_DQ_TO_DQS_MIL, "within_pair": False}
    assert rules["MatchedLengths_DIFF_USB2"]["scope"] == {"diff_pair_class": "DIFF_USB2"}
    assert rules["MatchedLengths_DIFF_USB2"]["typical"] is True
    plan_classes = {cls["name"]: cls for cls in model["plan"]["classes"]}
    assert plan_classes["DDR_BYTE0"]["members"] == lane["members"]


def test_a_lane_without_a_known_generation_gets_no_tolerance():
    parts = [(ref, pins, {**info, "description": info["description"].replace("DDR4 ", "")})
             for ref, pins, info in DESIGN]
    model = brief(parts)
    lane = next(item for item in model["length_matching"] if item["kind"] == "byte_lane")
    assert lane["tolerance"] is None and "controller" in lane["why_no_tolerance"]
    assert not any(rule["name"].startswith("MatchedLengths_DDR") for rule in
                   model["plan"]["rules"])


# ---------- placement ----------

def test_decoupling_capacitors_go_to_the_ic_supply_pins():
    model = brief()
    rows = {(row.get("ic"), row["rail"]): row for row in model["placement"]["decoupling"]}
    row = rows[("U1", "+3V3")]
    assert row["pins"] == ["1", "23"]
    assert [cap["designator"] for cap in row["capacitors"]] == ["C1", "C2"]
    assert row["capacitors"][0]["small"] is True and row["capacitors"][1]["small"] is False
    assert "close as possible" in model["placement_guidance"]["decoupling"]["requirement"]
    assert row["sheets"] == ["Processor"]


def test_tvs_crystal_and_smps_groups():
    model = brief()
    (tvs,) = model["placement"]["tvs"]
    assert tvs["connector"] == "J1" and tvs["device"] == "D1"
    assert sorted(tvs["pins"]) == ["2", "3"]
    guidance = model["placement_guidance"]
    assert "2 mm" in guidance["tvs"]["requirement"]
    assert guidance["tvs"]["limit_mil"] == pytest.approx(2 * MIL_PER_MM, abs=0.001)
    (crystal,) = model["placement"]["crystal"]
    assert crystal["crystal"] == "Y1" and crystal["load_capacitors"] == ["C3", "C4"]
    assert {(pin["part"], pin["name"]) for pin in crystal["oscillator_pins"]} == {
        ("U1", "OSC_IN"), ("U1", "OSC_OUT")}
    assert all(pin["named_as_oscillator_pin"] for pin in crystal["oscillator_pins"])
    (smps,) = model["placement"]["smps"]
    assert smps["converter"] == "U2" and smps["type"] == "buck"
    assert smps["inductors"] == ["L1"] and smps["input_capacitors"] == ["C5"]
    assert smps["output_capacitors"] == ["C6"] and smps["bootstrap_capacitors"] == ["C7"]
    assert smps["feedback_divider"] == ["R1", "R2"]
    assert "AN-1149" in guidance["smps"]["requirement"]
    assert "measured_as" not in guidance


def test_pcb_distances_are_measured_when_the_board_loads():
    netlist = netlist_of()
    index = PcbIndex(pcb_of(netlist, positions={"U1": 0.0, "C1": 60.0, "C2": 5000.0,
                                                "J1": 10000.0, "D1": 10040.0}), netlist)
    model = build_brief(netlist, None, {}, index=index, assumptions=Assumptions(units="mil"))
    row = next(row for row in model["placement"]["decoupling"]
               if row.get("ic") == "U1" and row["rail"] == "+3V3")
    measured = {item["designator"]: item["distance_mil"] for item in row["pcb"]["measured"]}
    assert measured["C1"] < measured["C2"]
    # Only the small capacitor is held to the distance.
    assert row["pcb"]["farthest"] == "C1" and row["pcb"]["within_limit"] is True
    (tvs,) = model["placement"]["tvs"]
    assert tvs["pcb"]["within_limit"] is True


# ---------- zones and notes ----------

def test_zones_per_sheet_with_their_basis():
    zones = {zone["sheet"]: zone for zone in brief()["zones"]}
    assert zones["Power"]["zone"] == "power"
    assert "U2" in zones["Power"]["labels"][0]["basis"]
    assert zones["Interface"]["zone"] == "io"
    assert zones["Radio"]["zone"] == "rf"
    assert zones["Processor"]["zone"] == "digital"
    assert zones["Memory"]["zone"] == "digital"


def test_designer_notes_with_placement_intent_are_quoted():
    rows = [
        {"document_kind": "sheet", "document": "Power", "kind": "note",
         "text": "Place C5 close to U2 pin 1, short loop.", "author": "A", "near_refdes": ["U2"]},
        {"document_kind": "sheet", "document": "Power", "kind": "note",
         "text": "Revision B adds a fuse."},
        {"document_kind": "sheet", "document": "Power", "kind": "title_block",
         "text": "Keep this away from the edge"},
        {"document_kind": "draftsman", "document": "Fab", "kind": "note", "text": "Route all"},
    ]
    notes = brief(notes=rows)["designer_notes"]
    assert notes == [{"sheet": "Power", "kind": "note",
                      "quote": "Place C5 close to U2 pin 1, short loop.", "sheets": ["Power"],
                      "author": "A", "near": ["U2"]}]


# ---------- existing coverage on the PCB ----------

def test_existing_pcb_classes_pairs_and_rules_are_reported_and_reused():
    netlist = netlist_of()
    pcb = pcb_of(netlist,
                 pairs=[DiffPair("USB", "USB_D_P", "USB_D_N", None)],
                 classes=[pcb_class("USB_90", "diff_pair", "USB")],
                 rules=[pcb_rule("MatchedLengths", "USB_SKEW",
                                 "InDifferentialPairClass('USB_90')", TOLERANCE="10mil",
                                 CHECKNETSINDIFFPAIR="TRUE", CHECKDIFFPAIRVSDIFFPAIR="FALSE",
                                 CHECKXSIGNALS="FALSE", CHECKOTHERS="FALSE"),
                        pcb_rule("Width", "Width", MINLIMIT="6mil", MAXLIMIT="6mil",
                                 PREFEREDWIDTH="6mil")])
    model = build_brief(netlist, None, {}, index=PcbIndex(pcb, netlist),
                        assumptions=Assumptions(units="mil"))
    found = classes_of(model)
    usb = found["USB_90"]
    assert usb["proposed_name"] == "DIFF_USB2"
    assert usb["exists"]["pcb_pairs"] == "1 of 1 declared on the PCB"
    assert usb["exists"]["same_members"] == ["USB_90"]
    assert usb["exists"]["pcb_rules"]["MatchedLengths"] == {"rules": ["USB_SKEW"],
                                                            "covered": 1, "of": 1}
    # Covered: left out of the plan and said so.
    assert not any(rule["name"].endswith("USB_90") for rule in model["plan"]["rules"])
    assert {"rule": "MatchedLengths_USB_90", "why": "covered by USB_SKEW"} in \
        model["existing"]["rules_already_covered"]
    plan_class = next(cls for cls in model["plan"]["classes"] if cls["name"] == "USB_90")
    assert plan_class["exists_on_pcb"] == "USB_90"
    assert model["existing"]["pcb"]["differential_pairs"] == 1


def test_a_board_impedance_profile_gives_the_pair_rule():
    netlist = netlist_of()
    pcb = pcb_of(netlist, pairs=[DiffPair("USB", "USB_D_P", "USB_D_N", None)])
    profiles = decode_board([{
        "V9_STACK_LAYER0_NAME": "Top Layer", "V9_STACK_LAYER0_LAYERID": str(TOP),
        "V9_STACK_LAYER2_NAME": "L2-GND", "V9_STACK_LAYER2_LAYERID": str(L2),
        "V9_STACK_LAYER3_NAME": "L3-SIG", "V9_STACK_LAYER3_LAYERID": str(L3),
        "V9_STACK_LAYER4_NAME": "Bottom Layer", "V9_STACK_LAYER4_LAYERID": str(BOTTOM),
        **PROFILE_PROPS, **TRACES}]).impedance_profiles
    pcb.board.impedance_profiles = profiles
    for layer in pcb.board.layers:
        layer.v7_id = {1: TOP, 2: L2, 3: L3, 32: BOTTOM}.get(layer.v6_id)
    index = PcbIndex(pcb, netlist)
    model = build_brief(netlist, None, {}, index=index, assumptions=Assumptions(units="mil"))
    usb = classes_of(model)["DIFF_USB2"]
    assert usb["impedance_profile"] == "D90 (USB)"
    assert not any(need["item"] == "DIFF_USB2" for need in model["needs_input"])
    rule = next(rule for rule in model["plan"]["rules"] if rule["kind"] == "DiffPairsRouting")
    assert rule["values"]["preferred_width"] == 4.5 and rule["values"]["preferred_gap"] == 5.0
    assert rule["inner"]["preferred_width"] == 4.0 and rule["inner"]["preferred_gap"] == 6.0
    assert rule["impedance_profile"] == "D90 (USB)" and rule["typical"] is True
    result = build_rule_files(json.loads(json.dumps(model["plan"])), index=index)
    assert result["round_trip"]["mismatches"] == []
    line = next(line for line in result["texts"]["rul"].split("\r\n")
                if "RULEKIND=DiffPairsRouting" in line)
    assert "IMPEDANCEPROFILEDRIVEN=TRUE" in line and "IMPEDANCEPROFILEID={D90}" in line
    assert "MIDLAYER2_PREFGAP=6mil" in line and "TOPLAYER_PREFGAP=5mil" in line


def test_without_a_profile_the_impedance_is_asked_for():
    model = brief()
    need = next(need for need in model["needs_input"] if need["item"] == "DIFF_USB2")
    assert "impedance profile" in need["how"]


def test_a_proposed_class_name_the_pcb_uses_for_other_members_is_renamed():
    netlist = netlist_of()
    pcb = pcb_of(netlist, classes=[pcb_class("DIFF_USB2", "diff_pair", "OTHER")])
    model = build_brief(netlist, None, {}, index=PcbIndex(pcb, netlist))
    assert "DIFF_USB2_2" in classes_of(model)
    assert any("DIFF_USB2" in note and "other members" in note for note in model["notes"])


def test_directives_and_project_type_are_reported():
    model = brief(project_type={"type": "constraint_manager",
                                "evidence": [{"source": "project_option"}]})
    assert model["existing"]["project_type"] == "constraint_manager"
    assert any("Constraint Manager" in note for note in model["notes"])


# ---------- rendering ----------

def test_render_pages_sections_and_sheets():
    model = brief()
    page = render_brief(model, sections=["zones"], limit=2)
    assert set(page) >= {"zones", "existing", "summary", "totals"}
    assert "net_classes" not in page and len(page["zones"]) == 2
    assert page["has_more"] and page["next_offset"] == 2
    second = render_brief(model, sections=["zones"], offset=2, limit=2)
    assert second["zones"][0]["sheet"] != page["zones"][0]["sheet"]
    narrowed = render_brief(model, sheet="Power.SchDoc")
    assert [zone["sheet"] for zone in narrowed["zones"]] == ["Power"]
    assert narrowed["placement"]["smps"] and not narrowed["placement"]["crystal"]
    small = render_brief(model, sections=["length_matching"], member_limit=3)
    lane = next(item for item in small["length_matching"] if item["kind"] == "byte_lane")
    assert len(lane["members"]) == 3 and lane["members_total"] == 11
    plan = render_brief(model, sections=["plan"], member_limit=1)["plan"]
    assert len(next(cls for cls in plan["classes"] if cls["name"] == "DDR_BYTE0")["members"]) \
        == 11


def test_parse_sections():
    assert parse_sections(None) == (None, None)
    assert parse_sections(["Plan", "zones"]) == (["plan", "zones"], None)
    assert parse_sections(["bogus"])[1]["error"] == "invalid_sections"
    assert set(SECTIONS) == {"net_classes", "length_matching", "placement", "zones",
                             "designer_notes", "plan"}


def test_the_cache_reuses_a_model():
    netlist = netlist_of()
    first = build_brief_cached(netlist, None, {}, assumptions=Assumptions())
    assert build_brief_cached(netlist, None, {}, assumptions=Assumptions()) is first
    assert build_brief_cached(netlist, None, {}, assumptions=Assumptions(units="mil")) \
        is not first
    # A variant plan rebuilt with the same content reuses the model.
    lite = build_brief_cached(netlist, VariantPlan(name="Lite", not_fitted={"C2"}), {})
    assert build_brief_cached(netlist, VariantPlan(name="Lite", not_fitted={"C2"}), {}) is lite
    assert build_brief_cached(netlist, VariantPlan(name="Lite", not_fitted={"C1"}), {}) \
        is not lite


# ---------- the plan ----------

PLAN = {
    "units": "mil",
    "classes": [
        {"name": "PWR_2A", "kind": "net", "members": ["+3V3", "+12V"]},
        {"name": "DIFF_USB2", "kind": "diff_pair",
         "members": [{"pair": "USB_D", "positive": "USB_D_P", "negative": "USB_D_N"}]},
    ],
    "rules": [
        {"name": "Width_PWR_2A", "kind": "Width", "scope": {"net_class": "PWR_2A"},
         "values": {"min_width": 30, "preferred_width": 40, "max_width": 250},
         "inner": {"min_width": 80, "preferred_width": 90, "max_width": 250}},
        {"name": "Clearance_HV", "kind": "Clearance", "scope": {"net": "+48V"},
         "scope2": "All", "values": {"clearance": 23.62}},
        {"name": "Pairs_USB", "kind": "DiffPairsRouting",
         "scope": {"diff_pair_class": "DIFF_USB2"},
         "values": {"min_width": 4, "preferred_width": 4.5, "max_width": 5, "min_gap": 5,
                    "preferred_gap": 5.5, "max_gap": 6, "max_uncoupled_length": 500}},
        {"name": "Skew_USB", "kind": "MatchedLengths",
         "scope": {"diff_pair_class": "DIFF_USB2"}, "values": {"tolerance": 50}},
        {"name": "Length_CLK", "kind": "Length", "scope": {"query": "InNet('CLK')"},
         "values": {"min_length": 100, "max_length": 2000}, "priority": 3},
    ],
}


def test_a_valid_plan_normalises_to_mil():
    normal, problems = validate_plan(PLAN)
    assert problems == []
    rules = {rule["name"]: rule for rule in normal["rules"]}
    assert rules["Width_PWR_2A"]["scope1"] == "InNetClass('PWR_2A')"
    assert rules["Pairs_USB"]["scope1"] == "InDifferentialPairClass('DIFF_USB2')"
    assert rules["Clearance_HV"]["scope1"] == "InNet('+48V')"
    assert rules["Skew_USB"]["values"]["within_pair"] is True
    in_mm, _ = validate_plan({**PLAN, "units": "mm"})
    width = next(rule for rule in in_mm["rules"] if rule["kind"] == "Width")
    assert width["values"]["min_width"] == pytest.approx(30 * MIL_PER_MM)


@pytest.mark.parametrize("change, message", [
    ({"units": "inch"}, "units must be 'mm' or 'mil'"),
    ({"rules": [{"name": "X", "kind": "Bogus", "scope": "All", "values": {}}]},
     "unknown rule kind 'Bogus'"),
    ({"classes": [{"name": "EMPTY", "kind": "net", "members": []}]},
     "a class needs at least one member"),
    ({"classes": [{"name": "A'B", "kind": "net", "members": ["N"]}]}, "ASCII name"),
    ({"classes": [{"name": "A", "kind": "bus", "members": ["N"]}]}, "kind must be one of"),
    ({"rules": [{"name": "W", "kind": "Width", "scope": "All",
                 "values": {"min_width": 10, "max_width": 5}}]}, "min_width <= preferred_width"),
    ({"rules": [{"name": "W", "kind": "Width", "scope": "All",
                 "values": {"min_width": 10}}]}, "missing ['max_width']"),
    ({"rules": [{"name": "W", "kind": "Width", "scope": "All",
                 "values": {"min_width": 1, "max_width": 2, "gap": 3}}]}, "unknown ['gap']"),
    ({"rules": [{"name": "W", "kind": "Width", "scope": {"room": "R"},
                 "values": {"min_width": 1, "max_width": 2}}]}, "a scope is 'All'"),
    ({"rules": [{"name": "W", "kind": "Width", "scope": {"query": "InNet('A'"},
                 "values": {"min_width": 1, "max_width": 2}}]}, "does not parse"),
    ({"rules": [{"name": "W", "kind": "Clearance", "scope": "All",
                 "values": {"clearance": -1}}]}, "0 or more"),
    ({"rules": [{"name": "L", "kind": "Length", "scope": "All",
                 "values": {"min_length": 9, "max_length": 1}}]}, "min_length is above"),
    ({"rules": [{"name": "W", "kind": "Width", "scope": "All", "priority": 0,
                 "values": {"min_width": 1, "max_width": 2}}]}, "priority is a whole number"),
    ({"classes": [], "rules": []}, "no classes and no rules"),
])
def test_bad_plans_are_refused_with_the_problem(change, message):
    normal, problems = validate_plan({**PLAN, **change})
    assert normal is None and any(message in problem for problem in problems), problems


def test_duplicates_and_kind_conflicts_are_refused():
    twice = {**PLAN, "rules": [PLAN["rules"][1], PLAN["rules"][1]]}
    assert any("used twice" in problem for problem in validate_plan(twice)[1])
    conflict = {**PLAN, "rules": [{"name": "W", "kind": "Width",
                                   "scope": {"net_class": "DIFF_USB2"},
                                   "values": {"min_width": 1, "max_width": 2}}]}
    assert any("is a diff_pair class in the plan" in problem
               for problem in validate_plan(conflict)[1])
    assert validate_plan("not a plan")[1] == [
        "the plan is a JSON object with 'units', 'classes' and 'rules'"]


# ---------- the .RUL file ----------

def records_of(text: str) -> list:
    lines = [line for line in text.split("\r\n") if line]
    return decode_rules([(0, parse_text_properties(line.encode("ascii"))) for line in lines])


def test_the_rul_file_reads_back_as_written():
    result = build_rule_files(PLAN, project="Board")
    assert result["round_trip"] == {"records": 5, "read_back": 5, "identical": 5,
                                    "mismatches": []}
    text = result["texts"]["rul"]
    assert text.endswith("\r\n") and text.isascii()
    lines = [line for line in text.split("\r\n") if line]
    assert len(lines) == 5 and all(line.startswith("|SELECTION=FALSE|") for line in lines)
    rules = {rule.name: rule for rule in records_of(text)}
    width = rules["Width_PWR_2A"]
    assert width.kind == "Width" and width.scope1 == "InNetClass('PWR_2A')"
    layers = {1: "Top Layer", 2: "Mid-Layer 1", 32: "Bottom Layer"}
    values = rule_values(width, layers)
    assert values["layers_mil"]["Top Layer"] == {"min_width": 30.0, "preferred_width": 40.0,
                                                 "max_width": 250.0}
    assert values["layers_mil"]["Mid-Layer 1"]["min_width"] == 80.0
    clearance = rule_values(rules["Clearance_HV"], layers)
    assert clearance["clearance_mil"] == 23.62
    skew = rule_values(rules["Skew_USB"], layers)
    assert skew["tolerance_mil"] == 50.0 and skew["within_pair"] is True
    assert rules["Length_CLK"].priority == 3 and rules["Length_CLK"].scope1 == "InNet('CLK')"
    assert [item["priority"] for item in result["priorities"]] == [1, 1, 1, 1, 3]
    assert "untested" in result["import_untested"]


def test_a_record_that_does_not_read_back_is_withheld():
    normal, _ = validate_plan(PLAN)
    rule = normal["rules"][1]
    record = rule_record(rule, 1, "ABCDEFGH", ([], {}), {})
    record["GENERICCLEARANCE"] = "1mil"
    check = round_trip(["|" + "|".join(f"{k}={v}" for k, v in record.items())], [rule], [1])
    assert check["identical"] == 0
    assert "clearance" in check["mismatches"][0]["problems"][0]
    with patch("services.constraints_brief.round_trip",
               return_value={"records": 5, "read_back": 5, "identical": 4,
                             "mismatches": [{"rule": "W", "problems": ["x"]}]}):
        result = build_rule_files(PLAN)
    assert "rul" not in result["texts"] and "rul_not_written" in result
    assert "csv" in result["texts"]


def test_rule_names_avoid_the_boards_and_layer_keys_follow_its_rules():
    netlist = netlist_of()
    guid = "{11111111-2222-3333-4444-555555555555}"
    existing = pcb_rule("Width", "Width_PWR_2A", priority=1,
                        MINLIMIT="6mil", MAXLIMIT="6mil", PREFEREDWIDTH="6mil",
                        SUBSTACK1=guid, **{f"TOPLAYER_{guid}_MINWIDTH": "6mil",
                                           f"TOPLAYER_{guid}_MAXWIDTH": "6mil",
                                           f"TOPLAYER_{guid}_PREFWIDTH": "6mil",
                                           "MIDLAYER7_MINWIDTH": "10mil"})
    index = PcbIndex(pcb_of(netlist, rules=[existing]), netlist)
    result = build_rule_files(PLAN, index=index)
    assert result["renamed_rules"] == [{"from": "Width_PWR_2A", "to": "Width_PWR_2A_2"}]
    assert result["round_trip"]["mismatches"] == []
    line = next(line for line in result["texts"]["rul"].split("\r\n")
                if "RULEKIND=Width" in line)
    assert "NAME=Width_PWR_2A_2" in line and f"SUBSTACK1={guid}" in line
    assert f"TOPLAYER_{guid}_MINWIDTH=30mil" in line and "MIDLAYER7_MINWIDTH=80mil" in line
    assert "UNIQUEID=AAAAAAAA" not in line
    assert result["board_rules_scoped_all"] == {"Width": ["Width_PWR_2A"]}


def test_csv_lists_class_members():
    result = build_rule_files(PLAN)
    lines = result["texts"]["csv"].split("\r\n")
    assert lines[0] == "class,kind,member,positive_net,negative_net,exists_on_pcb"
    assert "PWR_2A,net,+3V3,,," in lines
    assert "DIFF_USB2,diff_pair,USB_D,USB_D_P,USB_D_N," in lines


def test_steps_for_a_classic_and_a_constraint_manager_project():
    classic = build_rule_files(PLAN, project="Board")["texts"]["steps"]
    assert "Parameter Set directive" in classic and "ClassName = PWR_2A" in classic
    assert "Differential Pair directive" in classic and "Import Rules" in classic
    assert "Board-constraints.rul" in classic or "board-constraints.rul" in classic
    assert "untested" in classic
    cm = build_rule_files(PLAN, project_type="constraint_manager")["texts"]["steps"]
    assert "Constraint Manager" in cm and "Parameter Set directive" not in cm
    assert "create the net class PWR_2A" in cm
    assert "Physical constraints: track width" in cm


def test_formats_pick_the_texts():
    result = build_rule_files(PLAN, formats=("csv",))
    assert set(result["texts"]) == {"csv"}
    assert set(FORMATS) == {"rul", "csv", "steps"}


def test_files_are_never_overwritten(tmp_path):
    result = build_rule_files(PLAN, stem="Board")
    folder = tmp_path / "out" / "deeper"
    first = write_rule_files(result, str(folder))
    assert Path(first["rul"]).name == "Board-constraints.rul"
    second = write_rule_files(result, str(folder))
    assert Path(second["rul"]).name == "Board-constraints-2.rul"
    assert Path(second["csv"]).name == "Board-net-classes-2.csv"
    assert Path(first["rul"]).read_bytes() == Path(second["rul"]).read_bytes()
    assert b"\r\n" in Path(first["rul"]).read_bytes()


def test_inline_texts_are_cut_at_a_line():
    result = build_rule_files(PLAN)
    record = len(result["texts"]["rul"].split("\r\n")[0]) + 2
    shown, cut = inline_texts(result, record * 2 + 10)
    assert shown["rul"].endswith("\r\n") and shown["rul"].count("\r\n") == 2
    assert cut["rul"]["lines"] == 5 and cut["rul"]["lines_shown"] == 2
    # A text with no whole line within the limit is left out, never cut mid-record.
    shown, cut = inline_texts(result, 50)
    assert "rul" not in shown and cut["rul"]["lines_shown"] == 0
    assert all(len(text) <= 50 and text.endswith("\r\n") for text in shown.values())


def test_the_brief_plan_writes_a_file_that_reads_back():
    model = brief(assumptions=Assumptions(units="mm", current={"+3V3": 1.5}))
    plan = json.loads(json.dumps(model["plan"]))
    assert plan["schema"] == PLAN_SCHEMA and plan["units"] == "mm"
    result = build_rule_files(plan)
    assert result["round_trip"]["identical"] == len(plan["rules"]) > 0
    names = {rule.name for rule in records_of(result["texts"]["rul"])}
    assert {"Width_PWR_2A", "Clearance_HV_50V", "MatchedLengths_DIFF_USB2"} <= names


# ---------- the MCP tools ----------

@pytest.fixture
def tool_env(tmp_path):
    """main's project and netlist stand-ins: no PcbDoc, no sheets to compile."""
    netlist = netlist_of()
    loaded = {"name": "Board", "prj_pcb_path": str(tmp_path / "Board.PrjPcb"),
              "root_dir": str(tmp_path), "pcb_doc_paths": [],
              "sheets": [{"name": sheet, "path": str(tmp_path / f"{sheet}.SchDoc")}
                         for sheet in ("Processor", "Memory", "Interface", "Power", "Radio",
                                       "Input")]}
    (tmp_path / "Board.PrjPcb").write_text("[Design]\nVersion=1.0\n", encoding="utf-8")

    class Altium:
        def compiled_netlist(self, *_):
            raise RuntimeError("no sheet")

    with patch.object(main, "_require_project", return_value=(loaded, netlist, None)), \
         patch.object(main, "_bom_plan", return_value=(VariantPlan(name="[No Variations]"),
                                                       None, None)), \
         patch.object(main, "_altium", Altium()):
        yield tmp_path


def test_the_brief_tool_answers_and_shrinks(tool_env):
    result = json.loads(main.get_constraints_brief(current="+3V3=1.5"))
    assert result["units"] == "mm" and result["pcb_unavailable"] == "the project lists no PcbDoc"
    assert "compile_failed" in result
    widths = classes_of(result)["PWR_2A"]["width"]
    assert "outer_min_width_mm" in widths
    with patch.object(main, "QUERY_NET_MAX_RESULT_SIZE_CHARS", 6000):
        small = json.loads(main.get_constraints_brief())
    assert small["page_limited_to"] < 100
    assert len(json.dumps(small, indent=2)) <= 6000 or small["page_limited_to"] == 1
    zones = json.loads(main.get_constraints_brief(sections=["zones"], sheet="Power"))
    assert [zone["sheet"] for zone in zones["zones"]] == ["Power"]
    mil = json.loads(main.get_constraints_brief(units="mil", sections=["plan"]))
    assert mil["plan"]["units"] == "mil"


@pytest.mark.parametrize("kwargs, error", [
    ({"units": "inch"}, "invalid_units"),
    ({"sections": ["bogus"]}, "invalid_sections"),
    ({"temp_rise": 0}, "invalid_temp_rise"),
    ({"copper_oz": "lots"}, "invalid_copper_oz"),
    ({"current": "nonsense"}, "invalid_current"),
    ({"sheet": "Nowhere"}, "sheet_not_found"),
])
def test_the_brief_tool_refuses_bad_arguments(tool_env, kwargs, error):
    assert json.loads(main.get_constraints_brief(**kwargs))["error"] == error


def test_the_rule_tool_returns_text_inline_without_output_dir(tool_env):
    result = json.loads(main.generate_rule_file(plan=PLAN))
    assert result["written"] == "nothing: no output_dir was given"
    assert result["inline"]["rul"].startswith("|SELECTION=FALSE|")
    assert result["round_trip"]["identical"] == 5 and result["plan_source"] == "given"
    assert not list(tool_env.glob("*.rul"))
    as_text = json.loads(main.generate_rule_file(plan=json.dumps(PLAN), formats=["csv"]))
    assert set(as_text["inline"]) == {"csv"}
    from_brief = json.loads(main.generate_rule_file(units="mil"))
    assert from_brief["plan_source"] == "get_constraints_brief's proposed plan"
    assert from_brief["round_trip"]["mismatches"] == []


def test_the_rule_tool_writes_only_into_output_dir(tool_env):
    out = tool_env / "handoff"
    result = json.loads(main.generate_rule_file(plan=PLAN, output_dir=str(out)))
    assert sorted(Path(path).name for path in result["files"].values()) == [
        "Board-constraints-steps.md", "Board-constraints.rul", "Board-net-classes.csv"]
    again = json.loads(main.generate_rule_file(plan=PLAN, output_dir=str(out)))
    assert Path(again["files"]["rul"]).name == "Board-constraints-2.rul"
    assert sorted(path.name for path in tool_env.iterdir()) == ["Board.PrjPcb", "handoff"]


def test_the_rule_tool_reports_errors(tool_env):
    assert json.loads(main.generate_rule_file(plan="{not json"))["error"] == "invalid_plan"
    bad = json.loads(main.generate_rule_file(plan={"units": "mil", "classes": [],
                                                   "rules": []}))
    assert bad["error"] == "invalid_plan" and bad["problems"]
    assert json.loads(main.generate_rule_file(plan=PLAN, formats=["pdf"]))["error"] == \
        "invalid_formats"
    file_path = tool_env / "a_file.txt"
    file_path.write_text("x", encoding="utf-8")
    assert json.loads(main.generate_rule_file(plan=PLAN, output_dir=str(file_path)))[
        "error"] == "output_dir_invalid"
    with patch.object(main, "_require_project",
                      side_effect=main.ProjectNotReady(main._netlist_required_error())):
        assert json.loads(main.get_constraints_brief())["error"] == "netlist_required"
        assert json.loads(main.generate_rule_file())["error"] == "netlist_required"


def test_the_tools_are_documented_everywhere():
    root = Path(__file__).parents[1]
    manifest = json.loads((root / "manifest.json").read_text("utf-8"))
    readme = (root / "README.md").read_text("utf-8")
    changelog = (root / "CHANGELOG.md").read_text("utf-8")
    start = changelog.index("## [Unreleased]")
    end = changelog.index("\n## [", start + 1)
    for tool in ("get_constraints_brief", "generate_rule_file"):
        assert tool in {entry["name"] for entry in manifest["tools"]}
        assert f"| `{tool}` |" in readme
        assert main.SERVER_INSTRUCTIONS.index(tool) > 4096
        assert tool in main.NETLIST_TOOLS
        assert tool in changelog[start:end]


# ---------- coverage judged by the board rules' values and mode ----------

def test_a_board_rule_that_asks_less_does_not_cover_a_class():
    netlist = netlist_of()
    narrow = pcb_rule("Width", "Width_RAILS", "InNet('+3V3')", priority=1,
                      MINLIMIT="6mil", MAXLIMIT="10mil", PREFEREDWIDTH="6mil")
    small = pcb_rule("Clearance", "Clearance_IN", "InNet('+48V')", priority=1,
                     GAP="5mil", GENERICCLEARANCE="5mil")
    index = PcbIndex(pcb_of(netlist, rules=[narrow, small]), netlist)
    model = build_brief(netlist, None, {}, index=index,
                        assumptions=Assumptions(units="mil", current={"+3V3": 4.0}))
    found = classes_of(model)
    width = found["PWR_5A"]["exists"]["pcb_rules"]["Width"]
    assert width["covered"] == 0 and width["weaker"][0]["rule"] == "Width_RAILS"
    assert "IPC-2221 asks at least" in width["weaker"][0]["why"]
    rules = {rule["name"]: rule for rule in model["plan"]["rules"]}
    assert "Width_PWR_5A" in rules and "Width_RAILS" in rules["Width_PWR_5A"]["board_rules"][0]
    clearance = found["HV_50V"]["exists"]["pcb_rules"]["Clearance"]
    assert clearance["covered"] == 0 and "IPC-2221B asks" in clearance["weaker"][0]["why"]
    assert "Clearance_HV_50V" in rules
    # A rule asking at least the IPC width on every layer covers the class.
    wide = pcb_rule("Width", "Width_RAILS", "InNet('+3V3')", priority=1,
                    MINLIMIT="2000mil", MAXLIMIT="3000mil", PREFEREDWIDTH="2000mil")
    index = PcbIndex(pcb_of(netlist, rules=[wide]), netlist)
    model = build_brief(netlist, None, {}, index=index,
                        assumptions=Assumptions(units="mil", current={"+3V3": 4.0}))
    assert {"rule": "Width_PWR_5A", "why": "covered by Width_RAILS"} in \
        model["existing"]["rules_already_covered"]


def test_an_in_pair_target_needs_a_within_pair_rule():
    netlist = netlist_of()
    group = pcb_rule("MatchedLengths", "USB_GROUP", "InDifferentialPairClass('USB_90')",
                     TOLERANCE="1mil", CHECKNETSINDIFFPAIR="FALSE",
                     CHECKDIFFPAIRVSDIFFPAIR="TRUE", CHECKXSIGNALS="TRUE", CHECKOTHERS="TRUE")
    pcb = pcb_of(netlist, pairs=[DiffPair("USB", "USB_D_P", "USB_D_N", None)],
                 classes=[pcb_class("USB_90", "diff_pair", "USB")], rules=[group])
    model = build_brief(netlist, None, {}, index=PcbIndex(pcb, netlist),
                        assumptions=Assumptions(units="mil"))
    matched = classes_of(model)["USB_90"]["exists"]["pcb_rules"]["MatchedLengths"]
    assert matched["covered"] == 0 and "not within each pair" in matched["weaker"][0]["why"]
    assert any(rule["name"] == "MatchedLengths_USB_90" for rule in model["plan"]["rules"])


def test_an_xsignal_matched_lengths_rule_covers_a_byte_lane():
    netlist = netlist_of()
    pins = [f"U1-{10 + n}" for n in range(8)] + ["U1-20", "U1-21", "U1-22"]
    xsignals = [XSignal(name=f"LANE_{n}", pins=[pin, pin.replace("U1-", "U4-")])
                for n, pin in enumerate(pins)]
    rule = pcb_rule("MatchedLengths", "LANE_MATCH", "InxSignalClass('XS_LANE')",
                    TOLERANCE="5mil", CHECKNETSINDIFFPAIR="FALSE",
                    CHECKDIFFPAIRVSDIFFPAIR="FALSE", CHECKXSIGNALS="TRUE", CHECKOTHERS="FALSE")
    pcb = pcb_of(netlist, classes=[pcb_class("XS_LANE", "xsignal",
                                             *(x.name for x in xsignals))], rules=[rule])
    pcb.xsignals = xsignals
    model = build_brief(netlist, None, {}, index=PcbIndex(pcb, netlist),
                        assumptions=Assumptions(units="mil"))
    lane = next(item for item in model["length_matching"] if item["kind"] == "byte_lane")
    covered = lane["exists"]["pcb_rules"]["MatchedLengths"]
    assert covered["covered"] == covered["of"] == 11 and covered["via_xsignal"] == 11
    assert {"rule": "MatchedLengths_DDR_BYTE0",
            "why": "covered by LANE_MATCH (through xSignals)"} in \
        model["existing"]["rules_already_covered"]


# ---------- length-matching groups ----------

def test_byte_lanes_are_built_per_segment_of_series_resistors():
    parts = [part("U1", {**{str(n): (f"DQ{n}", f"DQ{n}") for n in range(8)},
                         "8": ("DM0", "DM0"), "9": ("DQS0_T", "DQS0_P"),
                         "10": ("DQS0_C", "DQS0_N")}, "Memory controller"),
             part("U4", {**{str(n): (f"DQ{n}", f"DQ{n}R") for n in range(8)},
                         "8": ("DM", "DM0R"), "9": ("DQS_T", "DQS0R_P"),
                         "10": ("DQS_C", "DQS0R_N")}, "DDR4 SDRAM 8Gb")]
    parts += [part(f"R{n}", {"1": ("1", f"DQ{n}"), "2": ("2", f"DQ{n}R")}, "Resistor")
              for n in range(8)]
    lanes = {item["name"]: item for item in brief(parts)["length_matching"]
             if item["kind"] == "byte_lane"}
    assert set(lanes) == {"DDR_BYTE0", "DDR_BYTE0_R"}
    assert lanes["DDR_BYTE0"]["members"] == [*(f"DQ{n}" for n in range(8)), "DM0",
                                             "DQS0_P", "DQS0_N"]
    assert lanes["DDR_BYTE0_R"]["members"] == [*(f"DQ{n}R" for n in range(8)), "DM0R",
                                               "DQS0R_P", "DQS0R_N"]
    assert any("other side of series parts" in basis
               for basis in lanes["DDR_BYTE0"]["evidence"])


def test_address_command_takes_its_own_channel_clock_and_chip_select():
    def channel(prefix: str, ref: str, memory: str) -> list:
        nets = [f"{prefix}A{n}" for n in range(10)] + [f"{prefix}BA0", f"{prefix}S0"]
        pins = {str(n): (net, net) for n, net in enumerate(nets)}
        pins.update({"40": ("CK_T", f"{prefix}CK_P"), "41": ("CK_C", f"{prefix}CK_N"),
                     **{str(50 + n): (f"DQ{n}", f"{prefix}DQ{n}") for n in range(4)}})
        return [part(ref, pins, "Memory controller"),
                part(memory, {"1": ("CK_T", f"{prefix}CK_P"), "2": ("CK_C", f"{prefix}CK_N")},
                     "DDR4 SDRAM 8Gb")]

    model = brief(channel("MEM_", "U1", "U4") + channel("MEM_X_", "U2", "U5"))
    groups = {item["name"]: item for item in model["length_matching"]
              if item["kind"] == "address_command"}
    assert "MEM_CK_P" in groups["MEM_ADDR_CMD"]["members"]
    assert "MEM_X_CK_P" not in groups["MEM_ADDR_CMD"]["members"]
    assert "MEM_X_CK_P" in groups["MEM_X_ADDR_CMD"]["members"]
    assert "MEM_S0" in groups["MEM_ADDR_CMD"]["members"]


def test_rgmii_groups_take_their_clock():
    tx = ["ETH_TXD0", "ETH_TXD1", "ETH_TXD2", "ETH_TXD3", "ETH_TX_EN", "ETH_GTX_CLK"]
    rx = ["ETH_RXD0", "ETH_RXD1", "ETH_RXD2", "ETH_RXD3", "ETH_RX_DV", "ETH_RX_CLK"]
    pins = {str(n): (net, net) for n, net in enumerate(tx + rx)}
    model = brief([part("U7", pins, "Ethernet MAC controller"),
                   part("U8", pins, "Gigabit Ethernet PHY transceiver")])
    groups = {item["name"]: item for item in model["length_matching"]
              if item["kind"] == "parallel_bus"}
    assert "ETH_GTX_CLK" in groups["ETH_TX"]["members"]
    assert "ETH_RX_CLK" in groups["ETH_RX"]["members"]
    assert any("clock ETH_GTX_CLK" in basis for basis in groups["ETH_TX"]["evidence"])


# ---------- placement and zones ----------

def compiled_at(points: dict) -> SimpleNamespace:
    """A stand-in offline compile placing pins on one sheet: {(ref, pin): (x, y) mil}."""
    objects = [SimpleNamespace(kind="pin", key=key, sheet="Processor", copy=0, x_mil=x,
                               y_mil=y) for key, (x, y) in points.items()]
    erc = SimpleNamespace(nets=[SimpleNamespace(objects=objects)], parts=[],
                          parameter_sets=[], buses=[])
    return SimpleNamespace(erc=erc)


def test_a_capacitor_goes_to_a_pin_only_when_drawn_at_it():
    compiled = compiled_at({("U1", "1"): (0.0, 0.0), ("U1", "23"): (0.0, 2000.0),
                            ("C1", "1"): (0.0, 100.0), ("C2", "1"): (3000.0, 1000.0)})
    model = build_brief(netlist_of(), None, {}, compiled=compiled,
                        assumptions=Assumptions(units="mil"))
    row = next(row for row in model["placement"]["decoupling"]
               if row.get("ic") == "U1" and row["rail"] == "+3V3")
    caps = {cap["designator"]: cap for cap in row["capacitors"]}
    assert caps["C1"]["next_to_pin"] == "1" and caps["C1"]["basis"].startswith("drawn at U1")
    # Drawn as far from both supply pins: placed at the IC, not at one pin.
    assert "next_to_pin" not in caps["C2"]
    assert caps["C2"]["basis"] == "drawn nearest to U1 (not at one of its pins on the rail)"
    assert row["per_pin"].startswith("1 small capacitor for 2 supply pins")


def test_zones_name_controller_sheets_and_coax_without_rf_is_io():
    parts = [part("U1", {"1": ("PA0", "SIG")}, "Generic part", sheet="Main_Controller"),
             part("J4", {"1": ("1", "CLK_SMA"), "2": ("2", GND)}, "SMA coax connector",
                  sheet="Clock_Out"),
             part("U5", {"1": ("CLKOUT", "CLK_SMA"), "2": ("VDD", "+3V3")}, "Clock generator",
                  sheet="Clock_Out")]
    zones = {zone["sheet"]: zone for zone in brief(parts)["zones"]}
    assert zones["Main_Controller"]["zone"] == "digital"
    labels = {label["label"] for label in zones["Clock_Out"]["labels"]}
    assert "rf" not in labels and "io" in labels


# ---------- assumptions ----------

def test_copper_oz_by_layer_name_is_used_or_said_to_be_ignored():
    netlist = netlist_of()
    index = PcbIndex(pcb_of(netlist), netlist)
    outer = next(iter(index.signal_layers().values()))
    model = build_brief(netlist, None, {}, index=index, assumptions=Assumptions(
        units="mil", current={"+3V3": 1.5}, copper_oz={outer.casefold(): 3.0}))
    assert model["assumptions"]["copper"]["outer"]["oz"] == 3.0
    assert model["assumptions"]["copper"]["outer"]["source"] == "copper_oz"
    unread = brief(assumptions=Assumptions(units="mil", copper_oz={"top layer": 2.0}))
    assert any("copper_oz names no signal layer ['top layer']" in note
               for note in unread["notes"])


def test_pcb_pair_legs_take_the_netlist_spelling():
    netlist = netlist_of()
    pcb = pcb_of(netlist, pairs=[DiffPair("USB", "usb_d_p", "usb_d_n", None)])
    model = build_brief(netlist, None, {}, index=PcbIndex(pcb, netlist),
                        assumptions=Assumptions(units="mil"))
    usb = classes_of(model)["DIFF_USB2"]
    assert usb["pairs"] == [{"pair": "USB", "positive": "USB_D_P", "negative": "USB_D_N"}]
    assert "Interface" in usb["sheets"]


# ---------- the plan on a page ----------

def test_the_plan_is_never_split_from_its_classes():
    model = brief(assumptions=Assumptions(units="mil", current={"+3V3": 1.5}))
    whole = render_brief(model, limit=1)["plan"]
    assert whole["classes"] == model["plan"]["classes"]
    assert whole["rules"] == model["plan"]["rules"]
    assert "omitted" in render_brief(model, offset=1, limit=1)["plan"]
    pointer = render_brief(model, plan_limit="pointer")["plan"]
    assert "sections=['plan']" in pointer["omitted"]
    seen = []
    offset = 0
    while True:
        page = render_brief(model, sections=["plan"], offset=offset, limit=1, plan_limit=1)
        names = {cls["name"] for cls in page["plan"]["classes"]}
        for rule in page["plan"]["rules"]:
            scope = rule["scope"]
            target = scope.get("net_class") or scope.get("diff_pair_class")
            assert target in names or offset == 0
            seen.append(rule["name"])
        if not page["has_more"]:
            break
        offset = page["next_offset"]
    assert sorted(seen) == sorted(rule["name"] for rule in model["plan"]["rules"])


# ---------- the .RUL records ----------

def test_records_take_altiums_precision_keys_and_order():
    plan = {"units": "mm", "rules": [
        {"name": "W", "kind": "Width", "scope": "All",
         "values": {"min_width": 0.25, "preferred_width": 0.5, "max_width": 1.0}},
        {"name": "C", "kind": "Clearance", "scope": "All", "values": {"clearance": 0.6}},
        {"name": "D", "kind": "DiffPairsRouting", "scope": "All",
         "values": {"min_width": 0.1, "max_width": 0.2, "min_gap": 0.1, "max_gap": 0.2}}]}
    result = build_rule_files(plan)
    lines = result["texts"]["rul"].split("\r\n")
    width = parse_text_properties(lines[0].encode("ascii"))
    assert width["MINLIMIT"] == "9.8425mil" and width["MAXLIMIT"] == "39.3701mil"
    clearance = parse_text_properties(lines[1].encode("ascii"))
    assert list(clearance)[-4:] == ["GAP", "GENERICCLEARANCE",
                                    "IGNOREPADTOPADCLEARANCEINFOOTPRINT", "OBJECTCLEARANCES"]
    assert clearance["OBJECTCLEARANCES"] == ""
    pair = parse_text_properties(lines[2].encode("ascii"))
    assert list(pair)[-1] == "MAXUNCOUPLEDLENGTH"
    assert pair["MAXUNCOUPLEDLENGTH"] == f"{DEFAULT_MAX_UNCOUPLED_MIL:g}mil"
    assert any("max uncoupled length" in note and "Altium's default" in note
               for note in result["notes"])
    assert result["round_trip"]["mismatches"] == []


def test_a_profile_driven_record_carries_the_profile_value():
    netlist = netlist_of()
    pcb = pcb_of(netlist, pairs=[DiffPair("USB", "USB_D_P", "USB_D_N", None)])
    pcb.board.impedance_profiles = decode_board([{
        "V9_STACK_LAYER0_NAME": "Top Layer", "V9_STACK_LAYER0_LAYERID": str(TOP),
        **PROFILE_PROPS, **TRACES}]).impedance_profiles
    plan = {"units": "mil", "classes": [{"name": "P", "kind": "diff_pair", "members": ["USB"]}],
            "rules": [{"name": "D", "kind": "DiffPairsRouting",
                       "scope": {"diff_pair_class": "P"}, "impedance_profile": "D90 (USB)",
                       "values": {"min_width": 4, "max_width": 5, "min_gap": 5, "max_gap": 6,
                                  "max_uncoupled_length": 300}}]}
    result = build_rule_files(plan, index=PcbIndex(pcb, netlist))
    record = parse_text_properties(result["texts"]["rul"].split("\r\n")[0].encode("ascii"))
    keys = list(record)
    assert record["IMPEDANCEPROFILEDRIVEN"] == "TRUE"
    assert record["IMPEDANCEPROFILEVALUE"] == "90.000000"
    assert keys.index("IMPEDANCEPROFILEID") > max(
        n for n, key in enumerate(keys) if key.endswith("WIDTH"))
    assert keys[-1] == "MAXUNCOUPLEDLENGTH" and record["MAXUNCOUPLEDLENGTH"] == "300mil"


def test_priorities_never_repeat_within_a_kind():
    width = {"min_width": 1, "max_width": 2}
    result = build_rule_files({"units": "mil", "rules": [
        {"name": "a", "kind": "Width", "scope": "All", "values": width},
        {"name": "b", "kind": "Width", "scope": "All", "values": width, "priority": 1},
        {"name": "c", "kind": "Width", "scope": "All", "values": width}]})
    assert [item["priority"] for item in result["priorities"]] == [2, 1, 3]
    clash = validate_plan({"units": "mil", "rules": [
        {"name": "a", "kind": "Width", "scope": "All", "values": width, "priority": 2},
        {"name": "b", "kind": "Width", "scope": "All", "values": width, "priority": 2}]})
    assert any("cannot share priority 2" in problem for problem in clash[1])


@pytest.mark.parametrize("rule, message", [
    ({"kind": "Width", "scope": {"net": "Vµ"}, "values": {"min_width": 1, "max_width": 2}},
     "non-ASCII"),
    ({"kind": "Width", "scope": {"query": "InNet('Vµ')"},
      "values": {"min_width": 1, "max_width": 2}}, "non-ASCII"),
    ({"kind": "Width", "scope": "All", "values": {"min_width": 0.25, "preferred_width": 0.5,
                                                  "max_width": 2.54},
      "inner": {"max_width": 0.2}}, "inner min_width <= preferred_width <= max_width"),
    ({"kind": "DiffPairsRouting", "scope": "All", "inner": {"min_gap": 0.5},
      "values": {"min_width": 0.1, "max_width": 0.2, "min_gap": 0.1, "max_gap": 0.25}},
     "inner min_gap <= preferred_gap <= max_gap"),
    ({"kind": "Width", "scope": "All", "scope2": {"net": "X"},
      "values": {"min_width": 1, "max_width": 2}}, "scope2 is for Clearance rules only"),
    ({"kind": "Clearance", "scope": "All", "values": {"clearance": 1e9}}, "beyond any board"),
    ({"kind": "Width", "scope": "All", "values": {"min_width": 0.000001, "max_width": 1}},
     "rounds to 0"),
])
def test_rules_that_cannot_be_written_are_refused(rule, message):
    result = build_rule_files({"units": "mm", "rules": [{"name": "R", **rule}]})
    assert result["error"] == "invalid_plan"
    assert any(message in problem for problem in result["problems"]), result["problems"]


def test_the_round_trip_compares_every_key_written():
    normal, _ = validate_plan({"units": "mil", "rules": [
        {"name": "D", "kind": "DiffPairsRouting", "scope": "All",
         "values": {"min_width": 4, "max_width": 5, "min_gap": 5, "max_gap": 6}}]})
    rule = normal["rules"][0]
    rule["values"]["max_uncoupled_length"] = 500.0
    template = ([("TOPLAYER", None), *((f"MIDLAYER{n}", None) for n in range(1, 31)),
                 ("BOTTOMLAYER", None)], {})
    record = rule_record(rule, 1, "ABCDEFGH", template, {})
    assert round_trip([record_line(record)], [rule], [1], written=[record])["identical"] == 1
    for key, value in (("ENABLED", "FALSE"), ("MOSTFREQGAP", "99mil"), ("NETSCOPE", "Bogus")):
        changed = {**record, key: value}
        check = round_trip([record_line(changed)], [rule], [1], written=[record])
        assert check["identical"] == 0 and key in " ".join(check["mismatches"][0]["problems"])


def test_names_priorities_and_classes_are_checked_against_the_board():
    netlist = netlist_of()
    board = [pcb_rule("Width", "Width", priority=2, MINLIMIT="6mil", MAXLIMIT="6mil",
                      PREFEREDWIDTH="6mil"),
             pcb_rule("Width", "Width_RAILS", "InNet('+3V3')", priority=1, MINLIMIT="6mil",
                      MAXLIMIT="6mil", PREFEREDWIDTH="6mil")]
    index = PcbIndex(pcb_of(netlist, rules=board), netlist)
    width = {"min_width": 10, "max_width": 20}
    plan = {"units": "mil", "classes": [{"name": "RAILS", "kind": "net", "members": ["+3V3"]}],
            "rules": [{"name": "width", "kind": "Width", "scope": {"net_class": "RAILS"},
                       "values": width},
                      {"name": "Width_2", "kind": "Width", "scope": {"net_class": "NOPE"},
                       "values": width}]}
    result = build_rule_files(plan, index=index)
    # The plan's own Width_2 is kept out of the rename pool.
    assert result["renamed_rules"] == [{"from": "width", "to": "width_3"}]
    first = result["priorities"][0]
    assert first["overlapping_board_rules"] == [{"rule": "Width_RAILS", "priority": 1}]
    assert first["same_number_on_board"] == ["Width_RAILS"]
    assert result["board_priorities"]["Width"]["scoped_all"] == [{"rule": "Width",
                                                                  "priority": 2}]
    assert any("NOPE, which is not in the plan nor on the PCB" in note
               for note in result["notes"])
    steps = result["texts"]["steps"]
    assert "Choose Design Rule Type" in steps and "Clear existing rules prior to import?" in steps
    assert "Width_RAILS (1) also take its nets" in steps
    assert "Clear existing rules prior to import?" in result["import_steps"]


def test_without_a_board_names_are_not_checked_and_said_so():
    result = build_rule_files({"units": "mil", "rules": [
        {"name": "Clearance", "kind": "Clearance", "scope": "All",
         "values": {"clearance": 10}}]})
    assert any("No PcbDoc was read" in note for note in result["notes"])
    assert any("Altium's default rule names" in note for note in result["notes"])


def test_output_dir_must_be_absolute_and_paths_come_back_absolute(tmp_path, monkeypatch):
    result = build_rule_files(PLAN, stem="Board")
    with pytest.raises(ValueError):
        write_rule_files(result, "relative_folder")
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    written = write_rule_files(result, "~/handoff")
    assert all(Path(path).is_absolute() and Path(path).parent == tmp_path / "handoff"
               for path in written.values())


def test_the_rule_tool_rebuilds_the_plan_with_the_briefs_assumptions(tool_env):
    result = json.loads(main.generate_rule_file(current="+3V3=1.5", formats=["rul"],
                                                units="mil"))
    assert "Width_PWR_2A" in {item["rule"] for item in result["priorities"]}
    assert result["plan_assumptions"]["current_given"] == ["+3V3"]
    plain = json.loads(main.generate_rule_file(formats=["rul"], units="mil"))
    assert "plan_assumptions_note" in plain
    both = json.loads(main.generate_rule_file(plan=PLAN, current="+3V3=1.5"))
    assert both["error"] == "assumptions_need_no_plan"
    relative = json.loads(main.generate_rule_file(plan=PLAN, output_dir="handoff"))
    assert relative["error"] == "output_dir_invalid"


def test_the_brief_tool_keeps_the_plan_whole(tool_env):
    with patch.object(main, "QUERY_NET_MAX_RESULT_SIZE_CHARS", 9000):
        page = json.loads(main.get_constraints_brief(current="+3V3=1.5"))
        plan = json.loads(main.get_constraints_brief(sections=["plan"],
                                                     current="+3V3=1.5"))["plan"]
    assert "omitted" in page["plan"] or len(page["plan"]["rules"]) == \
        page["summary"]["plan"]["rules"]
    names = {cls["name"] for cls in plan["classes"]}
    assert all((rule["scope"].get("net_class") or rule["scope"].get("diff_pair_class"))
               in names for rule in plan["rules"])
