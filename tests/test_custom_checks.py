"""Team-defined check packs (run_custom_checks, roadmap #49): the read-only design API,
pack files and their validation, discovery, every selector and assertion, tool rules,
opt-in Python plugins, the review-checklist integration, paging and the MCP tool. The
boards are synthetic with generic part and net names."""
import asyncio
import json
import re
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import server.main as main
import services.custom_checks as cc
import services.review_checklist as rc
from parsers.prj_pcb import VariantDefinition, VariantState
from server.services.bom import VariantPlan
from server.services.pcb_index import PcbIndex
from services.design_api import DesignAPI
from tests.test_decoupling import _pcb
from tests.test_pull_resistors import _netlist

REPO = Path(__file__).resolve().parents[1]
EXAMPLE = REPO / "examples" / "check_packs.json"
GND = ("GND", "GND")
MIL = 1 / 0.0254  # mil per mm


def cap(ref, net, text, value, other="GND", **params):
    info = {"value": value, "description": text}
    if params:
        info["params"] = {key.replace("_", " "): val for key, val in params.items()}
    return (ref, {"1": ("1", net), "2": ("2", other)}, info)


def res(ref, a, b, value, text="RES 0402"):
    return (ref, {"1": ("1", a), "2": ("2", b)}, {"value": value, "description": f"{text} {value}"})


# A USB connector (user-facing) with a TVS on one line and a TVS behind a 0 ohm link on the
# other, a pin header (board level) with no protection, an MCU and an EEPROM on an I2C bus
# with one pull-up per line, decoupling capacitors (0201), capacitors on a 48 V net rated
# 63 V and 100 V, one on VIN (its voltage only from the pack's rails), a test point.
BOARD = [
    ("J1", {"1": ("VBUS", "VBUS_5V"), "2": ("GND", "GND"), "3": ("D+", "USB_P"),
            "4": ("D-", "USB_N")}, {"description": "USB 2.0 connector receptacle"}),
    ("D1", {"1": ("1", "USB_P"), "2": ("2", "GND")}, {"description": "TVS diode ESD protection"}),
    res("R5", "USB_N", "USB_N_IC", "0R"),
    ("D2", {"1": ("1", "USB_N_IC"), "2": ("2", "GND")},
     {"description": "TVS diode ESD protection"}),
    ("J2", {"1": ("1", "IO_X"), "2": ("2", "GND")}, {"description": "Pin header 1x2"}),
    ("U1", {"1": ("VDD", "3V3", "Power"), "2": ("GND", "GND", "Power"),
            "3": ("SDA", "SDA", "IO"), "4": ("SCL", "SCL", "IO"), "5": ("PA0", "IO_X", "IO"),
            "6": ("USBDP", "USB_P", "IO"), "7": ("USBDM", "USB_N_IC", "IO"),
            "8": ("VDDA", "3V3", "Power"), "9": ("NC", None)},
     {"description": "Microcontroller", "params": {"manufacturer": "Maker A",
                                                   "manufacturer part number": "MCU-100"}}),
    ("U2", {"1": ("VCC", "3V3", "Power"), "2": ("GND", "GND", "Power"),
            "3": ("SDA", "SDA", "IO"), "4": ("SCL", "SCL", "IO")}, {"description": "EEPROM"}),
    res("R1", "SDA", "3V3", "4k7"),
    res("R2", "SCL", "3V3", "4k7"),
    cap("C1", "3V3", "CAP 100nF 16V X7R 0201", "100nF", manufacturer="Maker B",
        manufacturer_part_number="CAP-0201-104"),
    cap("C2", "3V3", "CAP 1uF 10V X5R 0201", "1uF"),
    cap("C3", "HV_48V", "CAP 1uF 63V X7R 1206", "1uF"),
    cap("C4", "HV_48V", "CAP 1uF 100V X7R 1210", "1uF"),
    cap("C5", "VIN", "CAP 10uF 25V X7R 1206", "10uF"),
    ("TP1", {"1": ("1", "IO_X")}, {"description": "Test point"}),
]


def board_netlist(board=BOARD):
    parts = []
    for ref, pins, info in board:
        connected = {pin: spec for pin, spec in pins.items() if spec[1] is not None}
        parts.append((ref, connected, info))
    netlist = _netlist(parts)
    for ref, pins, _ in board:  # unconnected pins are listed, with no net
        netlist["components"][ref]["pin_designators"] = list(pins)
        netlist["components"][ref]["pin_names"] = {pin: spec[0] for pin, spec in pins.items()}
    return netlist


def placements():
    """U1 on top with C1 next to its pin 1 (80 mil gap) and pin 8 far away; C2 on the
    bottom; C1 and C2 are 0201s; U2 and the rest are not on the board."""
    return [
        ("U1", "top", 0.0, 0.0, {"1": ("3V3", 0, 0), "2": ("GND", 0, 40),
                                 "8": ("3V3", 0, 600)}),
        ("C1", "top", 100.0, 0.0, {"1": ("3V3", 0, 0), "2": ("GND", 0, 40)}),
        ("C2", "bottom", 2000.0, 0.0, {"1": ("3V3", 0, 0), "2": ("GND", 0, 40)}),
        ("J1", "top", 3000.0, 0.0, {"1": ("VBUS_5V", 0, 0), "2": ("GND", 0, 40),
                                    "3": ("USB_P", 0, 80), "4": ("USB_N", 0, 120)}),
        ("D1", "top", 3100.0, 80.0, {"1": ("USB_P", 0, 0), "2": ("GND", 0, 40)}),
    ]


def api_for(board=BOARD, plan=None, pcb=True, rails=None) -> DesignAPI:
    netlist = board_netlist(board)
    loader = None
    if pcb:
        index = PcbIndex(_pcb(placements()), netlist)
        loader = lambda: (index, None)
    return DesignAPI(netlist, plan, {}, pcb=loader, rails=rails)


def pack(*rules, **extra) -> dict:
    return {"id": "demo", "title": "Demo pack", "version": "1", "rules": list(rules), **extra}


def rule(rule_id="R-01", **fields) -> dict:
    return {"id": rule_id, **fields}


def run(*rules, api=None, tools=None, **extra) -> dict:
    validated = cc.validate_pack(pack(*rules, **extra), "demo.json", tools)
    return cc.run_rules(validated["rules"], api=api or api_for(), tools=tools or {},
                        packs=[validated])


def outcome(*rules, **kwargs) -> tuple[dict, list[dict]]:
    model = run(*rules, **kwargs)
    return model["rules"][0], model["findings"]


def invalid(callable_, *args, **kwargs) -> dict:
    with pytest.raises(cc.PackError) as info:
        callable_(*args, **kwargs)
    return info.value.error


def write(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return path


# ---------- the design API ----------

def test_parts_kinds_fitted_and_bom_data():
    plan = VariantPlan(name="Lite", not_fitted={"C2"},
                       params={"C5": {"value": ("Value", "22uF")}})
    api = api_for(plan=plan)
    assert api.version == 1 and api.variant == "Lite"
    kinds = {part.designator: part.kind for part in api.parts}
    assert kinds == {"C1": "capacitor", "C2": "capacitor", "C3": "capacitor",
                     "C4": "capacitor", "C5": "capacitor", "D1": "tvs", "D2": "tvs",
                     "J1": "connector", "J2": "connector", "R1": "resistor", "R2": "resistor",
                     "R5": "resistor", "TP1": "test_point", "U1": "ic", "U2": "ic"}
    assert [part.designator for part in api.parts][:3] == ["C1", "C2", "C3"]  # natural order
    assert api.part("c2").fitted is False and api.part("C1").fitted is True
    assert api.part("C5").value == "22uF"  # the variant's parameter variation
    u1 = api.part("U1")
    assert (u1.manufacturer, u1.mpn) == ("Maker A", "MCU-100") and u1.in_bom
    assert u1.parameter("Manufacturer_Part_Number") == "MCU-100"
    assert u1.parameter("missing") is None
    assert [pin.ref for pin in u1.pins][-1] == "U1.9" and u1.pins[-1].net is None
    pin = u1.pins[0]
    assert (pin.name, pin.type, pin.role, pin.net) == ("VDD", "Power", "supply", "3V3")
    assert "3V3" in u1.nets and None not in u1.nets
    assert api.part("C1").case_size == "0201" and api.part("C3").case_size == "1206"
    assert u1.case_size is None  # not a passive: its text is no case code
    assert api.part("nope") is None and api.net("nope") is None


def test_objects_are_read_only():
    api = api_for()
    part = api.part("C1")
    with pytest.raises(AttributeError):
        part.value = "1uF"
    with pytest.raises(TypeError):
        part.parameters["value"] = "1uF"
    with pytest.raises(AttributeError):
        part.pins[0].net = "OTHER"


def test_net_classes():
    api = api_for()

    def classes(name):
        return api.net_classes(name)

    assert classes("GND") >= {"ground", "connector"}
    assert {"rail", "supply"} <= classes("3V3")
    assert {"signal", "i2c"} <= classes("SDA") and "i2c" in classes("SCL")
    assert {"diff_pair", "off_board", "external", "connector"} <= classes("USB_P")
    assert {"off_board", "connector"} <= classes("IO_X") and "external" not in classes("IO_X")
    assert "off_board" not in classes("USB_N_IC") and "diff_pair" not in classes("USB_N_IC")
    assert {"single_pin", "rail"} <= classes("VIN") and api.net("VIN").designators == ("C5",)
    assert "single_pin" not in classes("3V3")


def test_switched_pull_supply():
    board = [part for part in BOARD if part[0] not in ("R1", "R2")] + [
        res("R1", "SDA", "PULL_EN", "4k7"), res("R2", "SCL", "PULL_EN", "4k7"),
        ("U3", {"1": ("PB0", "PULL_EN", "IO")}, {"description": "GPIO expander"})]
    api = api_for(board)
    assert "supply" in api.net_classes("PULL_EN") and "rail" not in api.net_classes("PULL_EN")


def test_voltages_ratings_and_rails():
    api = api_for()
    assert api.voltage("3V3").volts == 3.3 and api.voltage("3V3").source == "name"
    assert api.voltage("GND").volts == 0.0
    assert api.voltage("VIN") is None and api.voltage("nope") is None
    assert api.rated_voltage("C3") == 63.0 and api.voltage_across("C3") == 48.0
    assert "text" in api.rating_source("C3")
    assert api.voltage_across("C5") is None and api.rated_voltage("U1") is None
    railed = api.with_rails({"VIN": 24})
    assert railed.voltage("VIN").source == "user" and railed.voltage_across("C5") == 24.0
    assert railed.part("C5") is api.part("C5")  # the parts are shared
    assert api.voltage("VIN") is None
    assert api.with_rails(None) is api
    assert api.net("3V3").voltage.volts == 3.3 and api.part("C3").rated_voltage == 63.0


def test_reachable_through_series_parts():
    api = api_for()
    assert api.reachable(["USB_N"], 0) == {"USB_N": 0}
    assert api.reachable(["USB_N"], 1) == {"USB_N": 0, "USB_N_IC": 1}
    assert api.reachable(["USB_N"], 1, ["ferrite"]) == {"USB_N": 0}
    # Rails and grounds are never crossed: a pull-up does not join SDA to 3V3.
    assert api.reachable(["SDA"], 3) == {"SDA": 0}


def test_pcb_placement_pads_and_distance():
    api = api_for()
    assert api.has_pcb and api.pcb_error is None
    placement = api.placement("C2")
    assert placement.side == "bottom" and placement.x_mm == pytest.approx(50.8)
    assert api.part("C1").side == "top" and api.part("U2").placement is None
    assert len(api.pads("U1")) == 3 and len(api.pads("U1", "8")) == 1
    gap = api.distance_mm(api.pads("U1", "1"), api.pads("C1", "1"))
    assert gap == pytest.approx(80 / MIL, abs=0.01)
    assert api.distance_mm([], api.pads("C1")) is None
    no_board = api_for(pcb=False)
    assert not no_board.has_pcb and no_board.placement("C1") is None
    broken = DesignAPI(board_netlist(), pcb=lambda: (None, {"error": "pcb_parse_failed"}))
    assert broken.pcb_error == "pcb_parse_failed" and broken.pads("C1") == []


def test_switched_pull_supply_behind_a_series_resistor():
    """Pull-ups on the far side of series resistors, to a supply a GPIO switches:
    check_pull_resistors joins each resistor's two nets into one line, and so does the
    supply class."""
    board = [part for part in BOARD if part[0] not in ("R1", "R2")] + [
        res("R6", "SDA", "SDA_J", "33R"), res("R7", "SCL", "SCL_J", "33R"),
        res("R1", "SDA_J", "PULL_EN", "4k7"), res("R2", "SCL_J", "PULL_EN", "4k7"),
        ("U3", {"1": ("PB0", "PULL_EN", "IO")}, {"description": "GPIO expander"})]
    api = api_for(board)
    assert "supply" in api.net_classes("PULL_EN") and "rail" not in api.net_classes("PULL_EN")
    assert api.line("SDA") == ("SDA", "SDA_J") and api.net("SDA_J").line == ("SDA_J", "SDA")
    assert api.line("3V3") == ("3V3",) and api.line("nope") == ()


def test_off_board_lines_are_those_no_esd_asks_protection_for():
    """A connector line into another connector or into a transformer is no off_board
    line (check_interfaces' no_esd does not ask protection for it). A net's connectors
    are those it leaves the board through, the user-facing ones first."""
    board = BOARD + [
        ("J6", {"1": ("1", "PASS"), "2": ("2", "SHARED"), "3": ("3", "GND")},
         {"description": "USB 2.0 connector receptacle"}),
        ("J7", {"1": ("1", "PASS"), "2": ("2", "SHARED"), "3": ("3", "GND")},
         {"description": "Pin header 1x3"}),
        ("U3", {"1": ("IN", "SHARED", "IO"), "2": ("RX", "MAG_B", "IO"),
                "3": ("VDD", "3V3", "Power"), "4": ("GND", "GND", "Power")},
         {"description": "Transceiver"}),
        ("J8", {"1": ("1", "MAG_A"), "2": ("2", "GND")}, {"description": "RJ45 jack"}),
        ("T1", {"1": ("1", "MAG_A"), "2": ("2", "GND"), "3": ("3", "MAG_B"),
                "4": ("4", "GND")}, {"description": "LAN transformer"})]
    api = api_for(board)
    assert {"off_board", "external"} <= api.net_classes("SHARED")
    assert api.connectors_of("SHARED") == ("J6",) and api.net("SHARED").connectors == ("J6",)
    assert api.net_classes("PASS") & {"off_board", "external"} == set()
    assert api.connectors_of("PASS") == ("J6", "J7")  # no line in: every connector on it
    assert "off_board" not in api.net_classes("MAG_A") and api.part("T1").kind == "transformer"
    assert api.connectors_of("USB_P") == ("J1",) and api.connectors_of("3V3") == ()


def test_diff_pair_is_never_a_rail_or_a_ground():
    board = BOARD + [
        ("U3", {"1": ("V+", "V+", "Power"), "2": ("V-", "V-", "Power"),
                "3": ("GND", "GND_P", "Power"), "4": ("GND", "GND_N", "Power")},
         {"description": "Op amp"}),
        cap("C7", "V+", "CAP 100nF", "100nF"), cap("C8", "V-", "CAP 100nF", "100nF")]
    api = api_for(board)
    for net in ("V+", "V-", "GND_P", "GND_N"):
        assert "diff_pair" not in api.net_classes(net), net
    assert "diff_pair" in api.net_classes("USB_P")


def test_nearest_measures_only_the_parts_that_can_be_nearer(monkeypatch):
    api = api_for()
    index = api._pcb[0]
    measured = []
    real = index._primitive_distance

    def counting(first, second, layers=None):
        measured.append(1)
        return real(first, second, layers)

    monkeypatch.setattr(index, "_primitive_distance", counting)
    pads = api.pads("U1", "1")
    candidates = {ref: api.pads(ref) for ref in ("C1", "C2", "J1", "D1")}
    nearest = api.nearest(pads, candidates)
    brute = min((api.distance_mm(pads, found), ref) for ref, found in candidates.items())
    assert nearest == (brute[1], brute[0]) == ("C1", pytest.approx(80 / MIL, abs=0.01))
    measured.clear()
    api.nearest(pads, candidates)
    assert len(measured) < sum(len(pads) * len(found) for found in candidates.values())
    assert api.nearest([], candidates) is None and api.nearest(pads, {"C3": []}) is None


# ---------- pack files ----------

def test_a_pack_validates_and_normalises():
    data = pack(rule("A-1", title="Has MPN", severity="error",
                     select={"parts": {"kind": "esd", "prefix": "d"}},
                     **{"assert": [{"has": "mpn"}]}),
                rails={"VIN": 24, "_note": "comment"}, version=2, _comment="x")
    validated = cc.validate_pack(data, "p.json")
    assert validated["version"] == "2" and validated["rails"] == {"VIN": 24.0}
    rule_ = validated["rules"][0]
    assert rule_["pack"] == "demo" and rule_["kind"] == "select" and rule_["objects"] == "parts"
    assert rule_["filter"]["kind"] == frozenset({"tvs"})  # the alias
    assert rule_["filter"]["prefix"] == frozenset({"D"}) and rule_["filter"]["fitted"] is True
    assert rule_["assert"] == [{"name": "has", "fields": ("mpn",)}]
    assert rule_["on_unknown"] == "skip" and rule_["disabled"] is False


def _select(objects="parts", flt=None, assertions=None, **fields):
    return pack(rule(select={objects: flt if flt is not None else {}},
                     **{"assert": assertions or [{"none": True}]}, **fields))


@pytest.mark.parametrize("data, where, words", [
    ({**pack(rule(select={"parts": {}}, **{"assert": [{"none": True}]})), "id": "Bad"},
     "id", "not valid"),
    ({k: v for k, v in pack(rule(select={"parts": {}}, **{"assert": [{"none": True}]})).items()
      if k != "version"}, "", "'version' is required"),
    ({**pack(rule(select={"parts": {}}, **{"assert": [{"none": True}]})), "extra": 1},
     "", "unknown key 'extra'"),
    (pack(), "rules", "1 to"),
    (pack(rule("bad id!")), "rules[0].id", "not valid"),
    (pack(rule()), "rules[0] (R-01)", "needs 'select'"),
    (pack(rule(select={"parts": {}}, tool="run_erc", **{"assert": [{"none": True}]})),
     "rules[0] (R-01)", "not both"),
    (_select(severity="fatal"), "rules[0] (R-01).severity", "not a severity"),
    (_select(on_unknown="ask"), "rules[0] (R-01).on_unknown", "'skip'"),
    (_select(flt={"kind": "gizmo"}), "rules[0] (R-01).select.parts.kind", "not one of"),
    (_select(flt={"value": "("}), "rules[0] (R-01).select.parts.value", "regular expression"),
    (_select(flt={"color": "red"}), "rules[0] (R-01).select.parts", "unknown key 'color'"),
    (_select(flt={"other_end": "rail"}), "rules[0] (R-01).select.parts.other_end",
     "assertion's parts filter"),
    (_select(flt={"case_size": "0302"}), "rules[0] (R-01).select.parts.case_size",
     "imperial case code"),
    (_select(flt={"side": "left"}), "rules[0] (R-01).select.parts.side", "'top' or 'bottom'"),
    (_select(flt={"net_voltage": {"over": 5}}), "rules[0] (R-01).select.parts.net_voltage",
     "unknown key 'over'"),
    (_select("nets", {"class": "bus"}), "rules[0] (R-01).select.nets.class", "not one of"),
    (_select("pins", {"type": "Analog"}), "rules[0] (R-01).select.pins.type", "not one of"),
    (pack(rule(select={"parts": {}, "nets": {}}, **{"assert": [{"none": True}]})),
     "rules[0] (R-01).select", "exactly one of"),
    (_select(assertions=[{"count": {"min": 1}}]), "rules[0] (R-01).assert[0].count",
     "applies to nets and pins"),
    (_select("nets", assertions=[{"count": {"parts": {}}}]), "rules[0] (R-01).assert[0].count",
     "'min', 'max' or both"),
    (_select("nets", assertions=[{"count": {"min": 3, "max": 1}}]),
     "rules[0] (R-01).assert[0].count", "larger than"),
    (_select("nets", assertions=[{"count": {"min": 1, "within_hops": 9}}]),
     "rules[0] (R-01).assert[0].count.within_hops", "0 to 5"),
    (_select("nets", assertions=[{"connected": {"through": ["wire"]}}]),
     "rules[0] (R-01).assert[0].connected.through", "not one of"),
    (_select(assertions=[{"nearest": {"parts": {}}}]), "rules[0] (R-01).assert[0].nearest",
     "'max_mm' is required"),
    (_select(assertions=[{"rating_ratio": {"max": 2}}]),
     "rules[0] (R-01).assert[0].rating_ratio", "\"min\""),
    (_select(assertions=[{"has": "colour"}]), "rules[0] (R-01).assert[0].has", "not one of"),
    (_select("nets", assertions=[{"matches": {"value": "x"}}]),
     "rules[0] (R-01).assert[0].matches", "not a field of nets"),
    (_select(assertions=[{"has": "mpn", "side": "top"}]), "rules[0] (R-01).assert[0]",
     "exactly one of"),
    (_select(assertions=[{"none": False}]), "rules[0] (R-01).assert[0].none", "must be true"),
    (_select(message="{designator.__class__}"), "rules[0] (R-01).message",
     "not a message field"),
    (_select(message="{designator!r}"), "rules[0] (R-01).message", "no conversion"),
    (_select(message="{oops"), "rules[0] (R-01).message", "not a valid message template"),
    (pack(rule(tool="set_project_dir", fail=[{"path": "x"}])), "rules[0] (R-01).tool",
     "not a tool a rule can run"),
    (pack(rule(tool="run_review_checklist", fail=[{"path": "x"}])), "rules[0] (R-01).tool",
     "not a tool a rule can run"),
    (pack(rule(tool="run_erc", args={"offset": 3}, fail=[{"path": "x"}])),
     "rules[0] (R-01).args", "set by the runner"),
    (pack(rule(tool="run_erc")), "rules[0] (R-01)", "needs 'fail'"),
    (pack(rule(tool="run_erc", fail=[{"path": "x", "op": "~"}])),
     "rules[0] (R-01).fail[0].op", "not an operator"),
    (pack(rule(tool="run_erc", fail=[{"path": "x"}], na_errors="x")),
     "rules[0] (R-01).na_errors", "error codes"),
    ({**pack(rule(select={"parts": {}}, **{"assert": [{"none": True}]})), "rails": {"V": "x"}},
     "rails.V", "must be a number"),
    ({**pack(rule(select={"parts": {}}, **{"assert": [{"none": True}]})),
      "rails": {"V": 5000}}, "rails.V", "between -1000 and 1000"),
])
def test_validation_errors_name_the_file_and_place(data, where, words):
    error = invalid(cc.validate_pack, data, "team.json")
    assert error["error"] == "invalid_check_pack" and error["file"] == "team.json"
    assert error["where"] == where, error
    assert words in error["message"], error["message"]


def test_rule_ids_are_unique_and_filters_nest_only_so_deep():
    data = pack(rule("A", select={"parts": {}}, **{"assert": [{"none": True}]}),
                rule("a", select={"parts": {}}, **{"assert": [{"none": True}]}))
    assert "used twice" in invalid(cc.validate_pack, data, "f")["message"]
    deep: dict = {}
    for _ in range(cc.MAX_DEPTH + 2):
        deep = {"not": deep}
    error = invalid(cc.validate_pack, _select(flt=deep), "f")
    assert "nested too deeply" in error["message"]


def test_tool_rule_arguments_are_checked_against_the_tool():
    def check_bom(variant=None, checks=None, offset=0):
        return "{}"

    data = pack(rule(tool="check_bom", args={"colour": "red"}, fail=[{"path": "x"}]))
    error = invalid(cc.validate_pack, data, "f", {"check_bom": check_bom})
    assert "does not take these arguments" in error["message"]


def test_reading_errors_say_where(tmp_path):
    write(tmp_path / cc.PACK_FILE, '{\n "id": "x",\n "rules": [1,\n}')
    error = invalid(cc.load_packs, tmp_path)
    assert error["error"] == "invalid_check_pack" and error["line"] == 4
    assert error["file"] == "check_packs.json (project folder)"
    assert "nothing ran" in error["hint"]


# ---------- discovery ----------

def simple(pack_id, *rule_ids, title="T") -> dict:
    return {"id": pack_id, "title": title, "version": "1", "rules": [
        rule(rule_id, select={"parts": {}}, **{"assert": [{"none": True}]})
        for rule_id in rule_ids]}


def test_discovery_order_replacement_and_path(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    project = repo / "boards" / "main"
    write(repo / cc.PACK_FILE, simple("shared", "S-1", title="Repository"))
    write(repo / cc.PACK_FOLDER / "b.json", simple("extra", "E-1"))
    write(project / cc.PACK_FILE, simple("local", "L-1"))
    write(project / cc.PACK_FOLDER / "a.json", simple("shared", "S-2", title="Project"))
    files = [label for _, _, label in cc.pack_files(project)]
    assert files == ["check_packs.json (repository root)", "check_packs/b.json (repository root)",
                     "check_packs.json (project folder)", "check_packs/a.json (project folder)"]
    packs, info = cc.load_packs(project)
    assert [p["id"] for p in packs] == ["shared", "extra", "local"]
    assert packs[0]["title"] == "Project" and [r["id"] for r in packs[0]["rules"]] == ["S-2"]
    assert info["replaced"] == [{"pack": "shared", "file": "check_packs.json (repository root)",
                                 "by": "check_packs/a.json (project folder)"}]
    write(project / "more" / "other.json", simple("other", "O-1"))
    packs, info = cc.load_packs(project, "more/other.json")
    assert packs[-1]["id"] == "other" and info["files"][-1] == "other.json (path)"
    # The same file named again is read once.
    packs, _ = cc.load_packs(project, str(project / cc.PACK_FILE))
    assert [p["id"] for p in packs].count("local") == 1
    assert invalid(cc.load_packs, project, "missing.json")["error"] == "pack_not_found"
    write(project / "notes.txt", "x")
    assert invalid(cc.load_packs, project, "notes.txt")["error"] == "pack_not_found"


def test_rule_ids_are_unique_across_packs(tmp_path):
    write(tmp_path / cc.PACK_FILE, simple("one", "R-1"))
    write(tmp_path / cc.PACK_FOLDER / "x.json", simple("two", "r-1"))
    error = invalid(cc.load_packs, tmp_path)
    assert "unique across packs" in error["message"]


def test_too_many_pack_files(tmp_path):
    for n in range(cc.MAX_PACK_FILES + 1):
        write(tmp_path / cc.PACK_FOLDER / f"p{n:02d}.json", simple(f"p{n}", f"R-{n}"))
    assert "more than" in invalid(cc.load_packs, tmp_path)["message"]


def test_choosing_packs_and_rules():
    one = cc.validate_pack(simple("one", "A-1", "A-2"), "f1")
    two = cc.validate_pack(simple("two", "B-1"), "f2")
    two["rules"][0]["disabled"] = True
    packs = [one, two]
    chosen, rules, plugins = cc.choose(packs, None, None)
    assert [r["id"] for r in rules] == ["A-1", "A-2"] and plugins  # disabled left out
    chosen, rules, _ = cc.choose(packs, "two", ["b-1"])
    assert [p["id"] for p in chosen] == ["two"] and [r["id"] for r in rules] == ["B-1"]
    assert invalid(cc.choose, packs, "three", None)["error"] == "unknown_pack"
    error = invalid(cc.choose, packs, None, ["A-1", "Z-9"])
    assert error["error"] == "unknown_rule" and error["unknown"] == ["Z-9"]
    _, rules, plugins = cc.choose(packs, "plugins", None, ["mine"])
    assert rules == [] and plugins is True
    _, rules, plugins = cc.choose(packs, "one", None, ["mine"])
    assert plugins is False
    _, rules, _ = cc.choose(packs, None, ["mine"], ["mine"])
    assert rules == []


# ---------- selecting ----------

def selected(objects: str, flt: dict, api=None) -> list[str]:
    validated = cc._filter(objects, flt, "f", "w")
    found = cc.select(api or api_for(), objects, validated)
    return [getattr(item, "designator", None) or getattr(item, "ref", None)
            if objects != "nets" else item.name for item in found]


@pytest.mark.parametrize("flt, expected", [
    ({"designator": "^C[12]$"}, ["C1", "C2"]),
    ({"prefix": ["R", "TP"]}, ["R1", "R2", "R5", "TP1"]),
    ({"kind": ["esd"]}, ["D1", "D2"]),
    ({"description": "eeprom"}, ["U2"]),
    ({"text": "maker a"}, ["U1"]),
    ({"value": "^4k7$"}, ["R1", "R2"]),
    ({"mpn": "CAP-"}, ["C1"]),
    ({"manufacturer": "."}, ["C1", "U1"]),
    ({"parameter": {"Manufacturer Part Number": "^MCU"}}, ["U1"]),
    ({"parameter": {"manufacturer": True}, "kind": "capacitor"}, ["C1"]),
    ({"parameter": {"manufacturer": False}, "kind": "ic"}, ["U2"]),
    ({"case_size": ["0201"]}, ["C1", "C2"]),
    ({"case_size": {"min": "1206"}}, ["C3", "C4", "C5"]),
    ({"case_size": {"max": "0402", "not_in": ["0201"]}}, ["R1", "R2", "R5"]),
    ({"side": "bottom"}, ["C2"]),
    ({"net": {"name": "^HV"}}, ["C3", "C4"]),
    ({"net_voltage": {"above": 24}}, ["C3", "C4"]),
    ({"net_voltage": {"min": 3.3, "below": 10}, "kind": "capacitor"}, ["C1", "C2"]),
    ({"kind": "capacitor", "not": {"case_size": "0201"}}, ["C3", "C4", "C5"]),
    ({"in_bom": False}, []),
])
def test_part_filters(flt, expected):
    assert selected("parts", flt) == expected


def test_fitted_default_and_null():
    plan = VariantPlan(name="Lite", not_fitted={"C2"})
    api = api_for(plan=plan)
    assert selected("parts", {"case_size": "0201"}, api) == ["C1"]
    assert selected("parts", {"case_size": "0201", "fitted": None}, api) == ["C1", "C2"]
    assert selected("parts", {"case_size": "0201", "fitted": False}, api) == ["C2"]
    # A not filter does not imply fitted: not (C*) leaves nothing, unfitted C2 included.
    assert selected("parts", {"case_size": "0201", "fitted": None,
                              "not": {"designator": "^C"}}, api) == []


@pytest.mark.parametrize("flt, expected", [
    ({"name": "^USB"}, ["USB_N", "USB_N_IC", "USB_P"]),
    ({"class": "i2c"}, ["SCL", "SDA"]),
    ({"class": "external", "not": {"class": ["ground", "rail"]}}, ["USB_N", "USB_P"]),
    ({"class": "power"}, ["3V3", "HV_48V", "VBUS_5V", "VIN"]),
    ({"voltage": {"above": 10}}, ["HV_48V"]),
    ({"has_part": {"kind": "tvs"}}, ["GND", "USB_N_IC", "USB_P"]),
    ({"pin_count": {"max": 1}}, ["VBUS_5V", "VIN"]),
])
def test_net_filters(flt, expected):
    assert selected("nets", flt) == expected


@pytest.mark.parametrize("flt, expected", [
    ({"part": {"kind": "ic"}, "role": "supply"}, ["U1.1", "U1.8", "U2.1"]),
    ({"part": {"designator": "U1"}, "type": "Power"}, ["U1.1", "U1.2", "U1.8"]),
    ({"name": "^SD", "net": {"class": "i2c"}}, ["U1.3", "U2.3"]),
    ({"part": {"designator": "U1"}, "number": "^9$"}, []),
    ({"part": {"designator": "U1"}, "connected": False}, ["U1.9"]),
    ({"part": {"designator": "U2"}, "not": {"role": ["ground", "supply"]}}, ["U2.3", "U2.4"]),
])
def test_pin_filters(flt, expected):
    assert selected("pins", flt) == expected


# ---------- assertions ----------

def test_has_parameter_has_and_parameter_matches():
    row, findings = outcome(rule(
        select={"parts": {"kind": "ic"}},
        **{"assert": [{"has_parameter": ["Manufacturer", "Manufacturer Part Number"]}]}))
    assert row["status"] == "fail" and row["checked"] == 2 and row["findings"] == 1
    assert findings[0]["designators"] == ["U2"]
    assert findings[0]["issue"] == "U2: no Manufacturer, Manufacturer Part Number"
    row, _ = outcome(rule(select={"parts": {"designator": "U1"}},
                          **{"assert": [{"has_parameter": {"any": ["MPN", "manufacturer"]}}]}))
    assert row["status"] == "pass"
    row, findings = outcome(rule(select={"parts": {"kind": "capacitor"}},
                                 **{"assert": [{"has": ["mpn", "manufacturer"]}]},
                                 message="{designator} lacks {missing}"))
    assert row["findings"] == 4 and findings[0]["issue"] == "C2 lacks manufacturer, mpn"
    row, findings = outcome(rule(select={"parts": {"kind": "ic"}},
                                 **{"assert": [{"parameter_matches": {"manufacturer": "^Maker"}}]}))
    assert findings[0]["issue"] == "U2: no manufacturer"
    row, findings = outcome(rule(select={"parts": {"designator": "U1"}},
                                 **{"assert": [{"parameter_matches": {"manufacturer": "^Z"}}]}))
    assert "does not match /^Z/" in findings[0]["issue"]


def test_matches_on_parts_nets_and_pins():
    _, findings = outcome(rule(select={"parts": {"kind": "resistor"}},
                               **{"assert": [{"matches": {"footprint": "0402"}}]}))
    assert [f["designators"] for f in findings] == [["R1"], ["R2"], ["R5"]]
    assert "footprint empty does not match" in findings[0]["issue"]
    row, _ = outcome(rule(select={"nets": {"class": "i2c"}},
                          **{"assert": [{"matches": {"name": "^S(DA|CL)$"}}]}))
    assert row["status"] == "pass"
    _, findings = outcome(rule(select={"pins": {"part": {"designator": "U2"}}},
                               **{"assert": [{"matches": {"type": "Power"}}]}))
    assert [f["pins"] for f in findings] == [["U2.3"], ["U2.4"]]


def test_count_with_hops_and_other_end():
    row, findings = outcome(rule(select={"nets": {"class": "i2c"}}, **{"assert": [
        {"count": {"parts": {"kind": "resistor", "other_end": "rail"}, "min": 1, "max": 1}}]}))
    assert row["status"] == "pass" and row["checked"] == 2
    board = BOARD + [res("R3", "SCL", "3V3", "10k")]
    row, findings = outcome(rule(select={"nets": {"class": "i2c"}}, **{"assert": [
        {"count": {"parts": {"kind": "resistor", "other_end": "supply"}, "min": 1, "max": 1,
                   "label": "pull-up"}}]}), api=api_for(board))
    assert row["findings"] == 1 and findings[0]["nets"] == ["SCL"]
    assert findings[0]["designators"] == ["R2", "R3"]
    assert findings[0]["issue"] == ("SCL: found 2 pull-up on the net (R2, R3); exactly 1 "
                                    "expected")
    # Within hops: the TVS behind R5 counts from USB_N only with one hop.
    flt = {"nets": {"name": "^USB_N$"}}
    row, _ = outcome(rule(select=flt, **{"assert": [{"count": {"parts": {"kind": "tvs"},
                                                               "min": 1}}]}))
    assert row["status"] == "fail"
    row, _ = outcome(rule(select=flt, **{"assert": [{"count": {"parts": {"kind": "tvs"},
                                                               "min": 1, "within_hops": 1}}]}))
    assert row["status"] == "pass"
    row, _ = outcome(rule(select={"pins": {"part": {"designator": "U1"}, "name": "^SDA$"}},
                          **{"assert": [{"count": {"parts": {"kind": "ic"}, "min": 2}}]}))
    assert row["status"] == "pass"


def test_connected_on_nets_pins_and_parts():
    rules = rule(select={"nets": {"class": "off_board", "not": {"class": ["ground", "rail"]}}},
                 **{"assert": [{"connected": {"parts": {"kind": "tvs"}}}]},
                 message="{net} ({connectors}): {failed}")
    row, findings = outcome(rules)
    assert row["checked"] == 3 and row["findings"] == 1
    assert findings[0]["issue"] == "IO_X (J2): no tvs within 1 series part"
    row, _ = outcome(rule(select={"nets": {"name": "^USB_N$"}}, **{"assert": [
        {"connected": {"parts": {"kind": "tvs"}, "through": ["ferrite"]}}]}))
    assert row["status"] == "fail"
    row, _ = outcome(rule(select={"parts": {"designator": "J1"}}, **{"assert": [
        {"connected": {"parts": {"kind": "tvs"}, "within_hops": 0}}]}))
    assert row["status"] == "pass"
    row, _ = outcome(rule(select={"parts": {"designator": "D1"}}, **{"assert": [
        {"connected": {"parts": {"kind": "tvs"}, "within_hops": 0}}]}))
    assert row["status"] == "fail"  # itself does not count
    row, _ = outcome(rule(select={"pins": {"part": {"designator": "U1"}, "connected": False}},
                          **{"assert": [{"connected": {"parts": {}}}]}))
    assert row["status"] == "could_not_evaluate" and row["reason"] == "not connected"


def test_nearest_on_the_pcb():
    rules = rule(select={"pins": {"part": {"kind": "ic"}, "role": "supply"}}, **{"assert": [
        {"nearest": {"parts": {"kind": "capacitor", "other_end": "ground"}, "max_mm": 5}}]})
    row, findings = outcome(rules)
    # U1.1 has C1 2 mm away; U1.8 is 15 mm from it; U2 is not on the board.
    assert row["status"] == "fail" and row["checked"] == 3 and row["findings"] == 1
    assert row["not_evaluated"] == 1 and row["not_evaluated_reasons"] == {"not on the PCB": 1}
    finding = findings[0]
    assert finding["pins"] == ["U1.8"] and finding["designators"] == ["U1", "C1"]
    assert finding["issue"] == ("U1.8 (VDDA) on 3V3: nearest capacitor to ground on the "
                                "same net is C1, 14.94 mm away (max 5 mm)")
    assert finding["location"]["side"] == "top" and finding["location"]["x_mm"] == 0.0
    row, findings = outcome({**rules, "on_unknown": "fail"})
    assert row["findings"] == 2 and "not on the PCB" in findings[1]["issue"]
    row, _ = outcome(rules, api=api_for(pcb=False))
    assert row["status"] == "could_not_evaluate" and row["reason"] == "no PCB"
    # Any net: the nearest TVS to the connector.
    row, findings = outcome(rule(select={"parts": {"kind": "connector"}}, **{"assert": [
        {"nearest": {"parts": {"kind": "tvs"}, "max_mm": 1, "same_net": False}}]},
        message="{designator}: {nearest} at {distance_mm:.1f} mm"))
    assert row["checked"] == 2 and row["not_evaluated"] == 1  # J2 is not on the board
    assert findings[0]["issue"] == "J1: D1 at 2.0 mm"
    row, findings = outcome(rule(select={"parts": {"designator": "C3"}}, **{"assert": [
        {"nearest": {"parts": {"kind": "tvs"}, "max_mm": 1}}]}))
    assert row["status"] == "could_not_evaluate"  # C3 is not on the board


def test_rating_ratio():
    rules = rule(select={"parts": {"kind": "capacitor", "net_voltage": {"above": 24}}},
                 **{"assert": [{"rating_ratio": {"min": 2}}]},
                 message="{designator} on {net}: {rated_voltage} V for {voltage} V "
                         "({ratio:.2f}x)")
    row, findings = outcome(rules)
    assert row["checked"] == 2 and row["findings"] == 1
    assert findings[0]["issue"] == "C3 on HV_48V: 63 V for 48 V (1.31x)"
    # The pack's rails give VIN its voltage: C5 is 25 V on 30 V.
    row, findings = outcome(rules, rails={"VIN": 30})
    assert row["checked"] == 3 and [f["designators"] for f in findings] == [["C3"], ["C5"]]
    row, _ = outcome(rule(select={"parts": {"designator": "C5"}},
                          **{"assert": [{"rating_ratio": {"min": 2}}]}))
    assert row["status"] == "could_not_evaluate" and row["reason"] == "voltage across it unknown"
    # Without rails, C5's rail (VIN) has no known voltage: it is not evaluated, not passed.
    row, _ = outcome(rules)
    assert row["not_evaluated"] == 1
    assert row["not_evaluated_reasons"] == {cc.VOLTAGE_UNKNOWN: 1}
    row, _ = outcome(rule(select={"parts": {"designator": "C5", "net_voltage": {"above": 24}}},
                          **{"assert": [{"rating_ratio": {"min": 2}}]}))
    assert row["status"] == "could_not_evaluate"
    assert row["reason"] == "net voltage unknown on VIN: give them in the pack's rails"
    assert row["voltage_unknown_rails"] == ["VIN"]
    row, _ = outcome(rule(select={"nets": {"voltage": {"above": 24}}},
                          **{"assert": [{"none": True}]}))
    assert row["checked"] == 1 and row["not_evaluated"] == 1  # VIN; VBUS_5V is 5 V
    row, _ = outcome(rule(select={"parts": {"designator": "U1"}},
                          **{"assert": [{"rating_ratio": {"min": 2}}]}))
    assert row["reason"] == "not a capacitor"
    unrated = [part for part in BOARD if part[0] != "C4"] + [
        cap("C4", "HV_48V", "CAP 1uF X7R 1210", "1uF")]
    row, _ = outcome(rule(select={"parts": {"designator": "C4"}},
                          **{"assert": [{"rating_ratio": {"min": 2}}]}), api=api_for(unrated))
    assert row["reason"] == "no voltage rating read"


def test_voltage_ranges_with_rails_of_unknown_voltage():
    api = api_for()
    c3, c5, r1 = api.part("C3"), api.part("C5"), api.part("R1")
    assert cc.part_voltage_match(api, c3, {"above": 24}) is True
    assert cc.part_voltage_match(api, c5, {"above": 24}) is None  # VIN: a rail, unknown
    assert cc.part_voltage_match(api, r1, {"max": 5}) is True  # SDA (signal) taken as low
    railed = api.with_rails({"VIN": 48})
    assert cc.part_voltage_match(railed, c5, {"above": 24}) is True
    assert cc.part_voltage_match(railed, c5, {"max": 24}) is False
    board = BOARD + [cap("C6", "VIN", "CAP 1uF 100V 0805", "1uF", other="HV_48V")]
    api = api_for(board)
    # 48 V is known on one side: above 24 holds whatever VIN is; below 60 cannot be told.
    assert cc.part_voltage_match(api, api.part("C6"), {"above": 24}) is True
    assert cc.part_voltage_match(api, api.part("C6"), {"below": 60}) is None
    assert cc.net_voltage_match(api, api.net("VIN"), {"min": 0}) is None
    assert cc.net_voltage_match(api, api.net("SDA"), {"min": 0}) is False


def test_side_case_size_and_none():
    row, findings = outcome(rule(title="No 0201 on the bottom",
                                 select={"parts": {"case_size": ["0201", "01005"]}},
                                 **{"assert": [{"side": "top"}]}))
    assert row["status"] == "fail" and findings[0]["issue"] == "C2: on the bottom side"
    assert findings[0]["location"] == {"side": "bottom", "x_mm": 50.8, "y_mm": 0.0}
    row, findings = outcome(rule(title="No 0201 on the bottom",
                                 select={"parts": {"case_size": "0201", "side": "bottom"}},
                                 **{"assert": [{"none": True}]}))
    assert findings[0]["issue"] == "C2: No 0201 on the bottom"
    row, _ = outcome(rule(select={"parts": {"case_size": "01005"}},
                          **{"assert": [{"none": True}]}))
    assert row["status"] == "pass" and row["checked"] == 0  # nothing forbidden is there
    row, findings = outcome(rule(select={"parts": {"kind": "capacitor"}},
                                 **{"assert": [{"case_size": {"min": "0402"}}]}))
    assert [f["designators"] for f in findings] == [["C1"], ["C2"]]
    row, _ = outcome(rule(select={"parts": {"kind": "ic"}},
                          **{"assert": [{"case_size": {"in": ["0402"]}}]}))
    assert row["status"] == "could_not_evaluate" and row["reason"] == "case size not read"


def test_nothing_selected_is_not_applicable_and_netlist_free_cannot_evaluate():
    row, _ = outcome(rule(select={"parts": {"kind": "relay"}}, **{"assert": [{"has": "mpn"}]}))
    assert row["status"] == "not_applicable" and row["reason"] == "nothing selected"
    validated = cc.validate_pack(pack(rule(select={"parts": {}}, **{"assert": [{"has": "mpn"}]})),
                                 "f")
    model = cc.run_rules(validated["rules"], api=None, tools={}, packs=[validated],
                         no_design="netlist_required: no netlist")
    assert model["rules"][0]["status"] == "could_not_evaluate"
    assert model["rules"][0]["reason"] == "netlist_required: no netlist"
    assert model["verdict"] == "incomplete"


def test_a_failing_rule_never_stops_the_others(monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("bad data")

    monkeypatch.setattr(cc, "check_assertion", broken)
    model = run(rule("A", select={"parts": {}}, **{"assert": [{"has": "mpn"}]}),
                rule("B", tool="check_bom", fail=[{"path": "x"}]))
    assert model["rules"][0]["status"] == "could_not_evaluate"
    assert "RuntimeError" in model["rules"][0]["reason"]
    assert model["rules"][1]["status"] == "could_not_evaluate"  # no check_bom stub given


def test_message_templates_render_missing_values_and_formats():
    assert cc.render("{designator} {ratio:.1f} {voltage}", {"designator": "C1", "ratio": 2.345,
                                                             "voltage": None}) == "C1 2.3 ?"
    assert cc.render("{nets}", {"nets": [f"N{n}" for n in range(12)]}) == \
        "N0, N1, N2, N3, N4, N5, N6, N7, N8, N9 +2"
    assert cc.render("{ratio:.2f}", {"ratio": "text"}) == "text"
    assert cc.render("{value}", {"value": 1.23456789}) == "1.235"


def test_summary_verdict_and_order():
    model = run(rule("W-1", select={"parts": {"kind": "ic"}}, **{"assert": [{"has": "mpn"}]}),
                rule("E-1", severity="error", select={"parts": {"designator": "C3"}},
                     **{"assert": [{"rating_ratio": {"min": 2}}]}),
                rule("I-1", severity="info", select={"parts": {"designator": "R1"}},
                     **{"assert": [{"has": "mpn"}]}))
    assert model["verdict"] == "fail"
    assert model["severity_counts"] == {"error": 1, "warning": 1, "info": 1}
    assert [f["check"] for f in model["findings"]] == ["E-1", "W-1", "I-1"]
    assert model["summary"]["by_pack"] == {"demo": {"error": 1, "warning": 1, "info": 1}}
    assert model["summary"]["rules"] == {"pass": 0, "fail": 3, "not_applicable": 0,
                                         "could_not_evaluate": 0}
    row = model["rules"][1]
    assert row["first"].startswith("C3:") and row["anchors"] == {"designators": ["C3"],
                                                                 "nets": ["HV_48V"]}
    assert row["severity_counts"] == {"error": 1}
    warn = run(rule("W-1", select={"parts": {"kind": "ic"}}, **{"assert": [{"has": "mpn"}]}))
    assert warn["verdict"] == "warn"
    clean = run(rule("P-1", select={"parts": {"designator": "U1"}},
                     **{"assert": [{"has": "mpn"}]}))
    assert clean["verdict"] == "pass"


def test_findings_are_capped_per_rule(monkeypatch):
    monkeypatch.setattr(cc, "MAX_FINDINGS_PER_RULE", 3)
    row, findings = outcome(rule(select={"pins": {}}, **{"assert": [{"none": True}]}))
    assert len(findings) == 3 and row["findings_capped"] and row["findings"] > 3


def test_unknown_voltages_are_not_evaluated_in_every_filter():
    """A rail of unknown voltage leaves an object open wherever the voltage filter sits:
    top level, a pin's net, a part's net, has_part, not. A forbidding rule never passes
    on it."""
    board = BOARD + [("U3", {"1": ("VIN", "VIN", "Power"), "2": ("GND", "GND", "Power")},
                      {"description": "Regulator IC"})]
    api = api_for(board)
    rules = {
        "top": {"nets": {"voltage": {"above": 24}, "not": {"name": "^HV"}}},
        "pin_net": {"pins": {"role": "supply", "net": {"voltage": {"above": 24}}}},
        "part_net": {"parts": {"kind": "capacitor", "net": {"voltage": {"above": 24}},
                               "not": {"designator": "^C[34]$"}}},
        "has_part": {"nets": {"has_part": {"designator": "^C5$",
                                           "net_voltage": {"above": 24}}}},
        "not": {"parts": {"designator": "^C5$", "not": {"net_voltage": {"max": 24}}}},
    }
    for name, select in rules.items():
        row, findings = outcome(rule(select=select, **{"assert": [{"none": True}]}), api=api)
        assert row["status"] == "could_not_evaluate" and not findings, name
        assert row["reason"].startswith(cc.VOLTAGE_UNKNOWN + " on VIN"), name
        assert row["voltage_unknown_rails"] == ["VIN"], name
    # With the rail's voltage given, the same rules decide.
    row, findings = outcome(rule(select=rules["pin_net"], **{"assert": [{"none": True}]}),
                            api=api, rails={"VIN": 48})
    assert row["status"] == "fail" and [f["pins"] for f in findings] == [["U3.1"]]
    # In an assertion's filter: a part an unknown voltage leaves open may be the one.
    row, _ = outcome(rule(select={"nets": {"name": "^VIN$"}}, **{"assert": [
        {"connected": {"parts": {"kind": "capacitor", "net_voltage": {"above": 24}},
                       "within_hops": 0}}]}), api=api)
    assert row["status"] == "could_not_evaluate" and row["reason"] == cc.VOLTAGE_UNKNOWN


def test_a_side_filter_without_a_pcb_is_not_evaluated():
    rules = rule(title="No 0201 on the bottom",
                 select={"parts": {"case_size": "0201", "side": "bottom"}},
                 **{"assert": [{"none": True}]})
    row, _ = outcome(rules, api=api_for(pcb=False))
    assert row["status"] == "could_not_evaluate" and row["reason"] == cc.NO_PCB_SIDE
    assert row["not_evaluated"] == 2
    row, findings = outcome(rules)
    assert row["status"] == "fail" and [f["designators"] for f in findings] == [["C2"]]


def test_one_per_line_and_on_line_follow_check_pull_resistors():
    """An I2C line joined through 0 ohm and damping resistors is one line: its pull-up
    two resistors away counts, and a line without one is one finding, not one per
    segment."""
    def lines(board):
        return outcome(rule(select={"nets": {"class": "i2c", "one_per_line": True}},
                            **{"assert": [{"count": {
                                "parts": {"kind": "resistor", "other_end": "supply"},
                                "min": 1, "max": 1, "on_line": True, "label": "pull-up"}}]}),
                       api=api_for(board))

    base = [part for part in BOARD if part[0] not in ("R1", "R2", "U2")]
    eeprom = ("U2", {"1": ("VCC", "3V3", "Power"), "2": ("GND", "GND", "Power"),
                     "3": ("SDA", "SDA_E", "IO"), "4": ("SCL", "SCL_E", "IO")},
              {"description": "EEPROM"})
    linked = base + [eeprom, res("R6", "SDA", "SDA_M", "0R"), res("R7", "SDA_M", "SDA_E", "33R"),
                     res("R8", "SCL", "SCL_E", "0R"),
                     res("R1", "SDA_E", "3V3", "4k7"), res("R2", "SCL_E", "3V3", "4k7")]
    row, findings = lines(linked)
    assert row["status"] == "pass" and row["checked"] == 2  # SDA and SCL, not SDA_E, SCL_E
    bare = [part for part in linked if part[0] not in ("R1", "R2")]
    row, findings = lines(bare)
    assert [f["nets"] for f in findings] == [["SCL"], ["SDA"]]
    assert findings[1]["issue"] == "SDA: found 0 pull-up on the line; exactly 1 expected"
    # Without on_line only the net itself counts.
    row, _ = outcome(rule(select={"nets": {"name": "^SDA$"}}, **{"assert": [{"count": {
        "parts": {"kind": "resistor", "other_end": "supply"}, "min": 1}}]}),
        api=api_for(linked))
    assert row["status"] == "fail"


def test_template_fields_for_parts_and_pins():
    _, findings = outcome(rule(select={"parts": {"kind": "capacitor", "case_size": "0201"}},
                               **{"assert": [{"none": True}]},
                               message="{designator} {side} {voltage} V [{connectors}]"))
    assert [f["issue"] for f in findings] == ["C1 top 3.3 V [?]", "C2 bottom 3.3 V [?]"]
    _, findings = outcome(rule(select={"pins": {"part": {"designator": "J1"}}},
                               **{"assert": [{"none": True}]},
                               message="{designator}.{pin} {side} {voltage} {connectors}"))
    assert [f["issue"] for f in findings] == ["J1.1 top 5 J1", "J1.2 top 0 J1, J2",
                                              "J1.3 top ? J1", "J1.4 top ? J1"]
    _, findings = outcome(rule(select={"parts": {"designator": "C3"}},
                               **{"assert": [{"none": True}]}, message="{side}"),
                          api=api_for(pcb=False))
    assert findings[0]["issue"] == "?"


def test_distances_near_the_limit_show_enough_decimals():
    assert cc._mm(5.002, 5) == "5.002" and cc._mm(7.5, 5) == "7.50"
    assert cc._mm(4.995, 5) == "4.995"
    row, findings = outcome(rule(select={"pins": {"part": {"designator": "U1"}, "number": "^1$"}},
                                 **{"assert": [{"nearest": {"parts": {"kind": "capacitor"},
                                                            "max_mm": 2.03}}]}))
    assert findings[0]["issue"].endswith("C1, 2.032 mm away (max 2.03 mm)")


def test_the_verdict_is_incomplete_when_objects_were_not_evaluated():
    model = run(rule(select={"parts": {"kind": "capacitor"}},
                     **{"assert": [{"rating_ratio": {"min": 1}}]}))
    row = model["rules"][0]
    assert row["status"] == "pass" and row["not_evaluated"] == 1  # C5: VIN unknown
    assert model["verdict"] == "incomplete"


# ---------- tool rules ----------

class Stub:
    """A stub check_bom and run_erc counting calls."""

    def __init__(self, bom=None, erc=None):
        self.calls: list[str] = []
        self.bom = bom if bom is not None else {
            "severity_counts": {"error": 0, "warning": 2, "info": 0},
            "findings": [
                {"check": "missing_mpn", "severity": "warning", "designators": ["R1", "R2"],
                 "issue": "No manufacturer part number on 4k7 (0402)."},
                {"check": "placeholder_description", "severity": "warning",
                 "designators": ["C9"], "nets": ["NET_A"], "issue": "Description is empty."}]}
        self.erc = erc if erc is not None else {"error": "netlist_required",
                                                "message": "no netlist"}

    def check_bom(self, variant=None, checks=None, offset=0):
        self.calls.append("check_bom")
        return json.dumps(self.bom)

    def run_erc(self, checks=None, sheet=None, min_level="warning", report="violations",
                offset=0):
        self.calls.append("run_erc")
        return json.dumps(self.erc)

    def tools(self):
        return {"check_bom": self.check_bom, "run_erc": self.run_erc}


def test_tool_rules_turn_findings_into_a_gate():
    stub = Stub()
    model = run(
        rule("T-1", severity="error", tool="check_bom",
             fail=[{"list": "findings", "where": {"check": "missing_mpn"}, "label": "no MPN"}]),
        rule("T-2", tool="check_bom", fail=[{"path": "severity_counts.warning", "op": ">",
                                             "value": 1, "label": "BOM warnings"}],
             message="{label} ({tool})"),
        rule("T-3", tool="check_bom", **{"pass": [{"path": "severity_counts.warning",
                                                   "op": "==", "value": 0,
                                                   "label": "no warnings"}]}),
        rule("T-4", tool="check_bom", fail=[{"list": "findings", "where": {"check": "nope"}}]),
        api=api_for(), tools=stub.tools())
    rows = {row["id"]: row for row in model["rules"]}
    assert stub.calls == ["check_bom"]  # one run for every rule with the same arguments
    assert rows["T-1"]["status"] == "fail" and rows["T-1"]["kind"] == "tool"
    first = next(f for f in model["findings"] if f["check"] == "T-1")
    assert first == {"check": "T-1", "pack": "demo", "severity": "error",
                     "designators": ["R1", "R2"],
                     "issue": "No manufacturer part number on 4k7 (0402)."}
    t2 = next(f for f in model["findings"] if f["check"] == "T-2")
    assert t2["issue"] == "BOM warnings (check_bom)"
    t3 = next(f for f in model["findings"] if f["check"] == "T-3")
    assert t3["issue"] == "Not met: no warnings"
    assert rows["T-4"]["status"] == "pass" and rows["T-4"]["checked"] == 1


def test_tool_rule_not_applicable_errors_and_unresolved_fields():
    stub = Stub(erc={"error": "no_pcb_document", "message": "No PcbDoc."})
    model = run(rule("N-1", tool="run_erc", fail=[{"path": "summary.errors"}]),
                rule("N-2", tool="check_bom", na=[{"path": "severity_counts.error", "op": "==",
                                                   "value": 0, "label": "no BOM errors"}],
                     fail=[{"list": "findings"}]),
                rule("N-3", tool="check_bom", fail=[{"list": "lines"}]),
                rule("N-4", tool="run_erc", na_errors=[], fail=[{"path": "summary.errors"}]),
                tools=stub.tools())
    rows = {row["id"]: row for row in model["rules"]}
    assert rows["N-1"]["status"] == "not_applicable" and "No PcbDoc" in rows["N-1"]["reason"]
    assert rows["N-2"]["status"] == "not_applicable" and rows["N-2"]["reason"] == "no BOM errors"
    assert rows["N-3"]["status"] == "could_not_evaluate"
    assert "lines not in the check_bom result" in rows["N-3"]["reason"]
    assert rows["N-4"]["status"] == "could_not_evaluate"
    assert rows["N-4"]["reason"].startswith("no_pcb_document")


def test_tool_rules_read_every_page_their_conditions_need():
    pages = [{"findings": [{"check": "a", "severity": "warning", "issue": "first one"}],
              "next_offset": 1},
             {"findings": [{"check": "a", "severity": "warning", "issue": "second one"}]}]

    def check_bom(variant=None, checks=None, offset=0):
        return json.dumps(pages[offset])

    model = run(rule("P-1", tool="check_bom", fail=[{"list": "findings"}]),
                tools={"check_bom": check_bom})
    assert model["rules"][0]["findings"] == 2
    assert model["tool_runs"][0]["pages"] == 2


def test_tool_rules_read_a_missing_summary_count_as_zero():
    """A tool's summary by check lists only the checks that found something: on a clean
    board the count is missing, and a rule on it passes (the path is named in the row)."""
    def tool(summary):
        return {"check_decoupling": lambda **_: json.dumps({"summary": summary,
                                                             "findings": []})}

    rules = [rule("Z-1", tool="check_decoupling",
                  fail=[{"path": "summary.no_capacitor", "op": ">", "value": 0}]),
             rule("Z-2", tool="check_decoupling",
                  fail=[{"path": "summary.no_capacitor", "op": "truthy"}]),
             rule("Z-3", tool="check_decoupling",
                  fail=[{"path": "overview.no_capacitor", "op": ">", "value": 0}])]
    model = run(*rules, tools=tool({}))
    rows = {row["id"]: row for row in model["rules"]}
    assert rows["Z-1"]["status"] == "pass"
    assert rows["Z-1"]["counted_as_zero"] == ["summary.no_capacitor"]
    assert rows["Z-2"]["status"] == "pass"  # truthy: an absent path is falsy anyway
    assert rows["Z-3"]["status"] == "could_not_evaluate"  # no such object: not a count
    model = run(*rules[:1], tools=tool({"no_capacitor": 2, "far": 1}))
    assert model["rules"][0]["status"] == "fail" and model["findings"][0]["issue"] == \
        "summary.no_capacitor > 0: 2"
    # The checklist keeps a missing path unresolved.
    assert rc.evaluate({"path": "summary.x", "op": ">", "value": 0}, {"summary": {}})[
        "unresolved"] == ["summary.x"]


def test_a_tool_rule_keeps_its_findings_when_another_path_is_missing():
    stub = Stub()
    model = run(rule("U-1", tool="check_bom", fail=[
        {"list": "findings", "where": {"check": "missing_mpn"}},
        {"path": "totals.lines", "op": ">", "value": 0}]), tools=stub.tools())
    row = model["rules"][0]
    assert row["status"] == "fail" and row["findings"] == 1
    assert "totals.lines not in the check_bom result" in row["unresolved"]
    model = run(rule("U-2", tool="check_bom", **{"pass": [
        {"path": "totals.lines", "op": ">", "value": 0}]}), tools=stub.tools())
    assert model["rules"][0]["status"] == "could_not_evaluate" and not model["findings"]


# ---------- plugins ----------

PLUGIN = '''
TITLE = "Plugin check"
SEVERITY = "info"

def check(api):
    print("noise")  # never reaches the MCP stream
    found = []
    for part in api.parts:
        if part.kind == "capacitor" and part.case_size == "0201":
            found.append({"message": f"{part.designator} is tiny", "designators": part.designator,
                          "nets": list(part.nets), "location": {"note": "x"}})
    found.append("a plain text finding")
    found.append({"nothing": 1})
    found.append({"message": "custom id", "check": "PLG-7", "severity": "error"})
    return found
'''


def test_plugin_files_come_only_from_the_project_folder(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    project = repo / "board"
    write(repo / cc.PACK_FOLDER / cc.PLUGIN_FOLDER / "root.py", "def check(api): return []")
    write(project / cc.PACK_FOLDER / cc.PLUGIN_FOLDER / "b_rule.py", "def check(api): return []")
    write(project / cc.PACK_FOLDER / cc.PLUGIN_FOLDER / "_helper.py", "x = 1")
    write(project / cc.PACK_FOLDER / cc.PLUGIN_FOLDER / "notes.txt", "x")
    assert [path.name for path in cc.plugin_files(project)] == ["b_rule.py"]
    assert cc.plugin_id(Path("my rule!.py")) == "my_rule_"
    assert cc.plugin_files(tmp_path / "missing") == []


@pytest.mark.parametrize("allow, env, runs, words", [
    (True, "1", True, None),
    (False, "1", False, "allow_plugins=true"),
    (True, "", False, "does not set ALTIUM_COPILOT_ALLOW_PLUGINS=1"),
    (True, "yes", False, "does not set"),
    (False, "", False, "and start the server"),
])
def test_plugins_need_both_switches(allow, env, runs, words):
    ok, reason = cc.plugins_allowed(allow, {cc.PLUGIN_ENV: env} if env else {})
    assert ok is runs
    assert (reason is None) if runs else words in reason


def test_a_plugin_runs_and_its_findings_are_checked(tmp_path, capsys):
    path = write(tmp_path / "tiny.py", PLUGIN)
    row, findings = cc.run_plugin(path, api_for())
    assert capsys.readouterr().out == ""
    assert row["status"] == "fail" and row["kind"] == "plugin" and row["pack"] == "plugins"
    assert row["title"] == "Plugin check" and row["severity"] == "info"
    assert row["bad_items"] == 1 and row["output_chars"] == 6
    assert [f["issue"] for f in findings] == ["C1 is tiny", "C2 is tiny",
                                              "a plain text finding", "custom id"]
    assert findings[0]["designators"] == ["C1"] and findings[0]["nets"] == ["3V3", "GND"]
    assert findings[0]["severity"] == "info" and findings[0]["check"] == "tiny"
    assert findings[3]["check"] == "PLG-7" and findings[3]["severity"] == "error"
    assert not any(name.startswith("altium_copilot_plugin_") for name in sys.modules)


@pytest.mark.parametrize("source, words", [
    ("def check(api):\n    return 1 / 0\n", "ZeroDivisionError at line 2"),
    ("x = 1\n", "defines no check(api)"),
    ("def check(api):\n    return 'text'\n", "must return a list"),
    ("import sys\nsys.exit(3)\n", "SystemExit at line 2"),
    ("def check(api)\n", "SyntaxError"),
    ("def check(api):\n    return api.part('U1').missing\n", "AttributeError at line 2"),
])
def test_plugin_errors_are_contained(tmp_path, source, words):
    path = write(tmp_path / "broken.py", source)
    row, findings = cc.run_plugin(path, api_for())
    assert row["status"] == "could_not_evaluate" and findings == []
    assert words in row["reason"], row["reason"]


def test_a_plugin_without_a_design_is_not_run(tmp_path):
    marker = tmp_path / "ran.txt"
    path = write(tmp_path / "p.py", f"open(r'{marker}', 'w').close()\ndef check(api): return []")
    row, _ = cc.run_plugin(path, None, "netlist_required: none")
    assert row["status"] == "could_not_evaluate" and not marker.exists()


def _link_folder(link: Path, target: Path) -> None:
    """A directory link (a junction on Windows, which needs no privilege), or skip."""
    try:
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
        return
    except (ImportError, OSError, AttributeError):
        pass
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("directory links cannot be made here")


@pytest.mark.parametrize("linked", ["plugins", "check_packs"])
def test_plugin_folders_linked_outside_the_project_give_none(tmp_path, linked):
    outside = tmp_path / "outside"
    write(outside / cc.PLUGIN_FOLDER / "evil.py", "def check(api): return []")
    project = tmp_path / "board"
    if linked == "plugins":
        (project / cc.PACK_FOLDER).mkdir(parents=True)
        _link_folder(project / cc.PACK_FOLDER / cc.PLUGIN_FOLDER, outside / cc.PLUGIN_FOLDER)
    else:
        project.mkdir()
        _link_folder(project / cc.PACK_FOLDER, outside)
    assert (project / cc.PACK_FOLDER / cc.PLUGIN_FOLDER / "evil.py").is_file()
    assert cc.plugin_files(project) == []


def test_plugins_run_only_when_their_pack_is_chosen():
    packs = [cc.validate_pack(simple("one", "A-1"), "one.json")]
    assert cc.choose(packs, None, ["good"], ["good"]) == (packs, [], True)
    assert cc.choose(packs, "plugins", None, ["good"])[2] is True
    error = invalid(cc.choose, packs, "one", ["good"], ["good"])
    assert error["error"] == "unknown_rule" and error["unknown"] == ["good"]
    error = invalid(cc.validate_pack, simple("plugins", "A-1"), "p.json")
    assert error["where"] == "id" and "names the Python plugin files" in error["message"]


# ---------- the MCP tool ----------

@pytest.fixture
def project(tmp_path, monkeypatch):
    """main with a loaded synthetic project (variant Lite leaves C2 out), its PCB and no
    pack yet."""
    root = tmp_path / "Board"
    root.mkdir()
    netlist = board_netlist()
    index = PcbIndex(_pcb(placements()), netlist)

    class Session:
        def get(self, project, netlist):
            return index, None

    monkeypatch.setattr(main, "_project", {
        "name": "Board", "root_dir": str(root), "prj_pcb_path": str(root / "Board.PrjPcb"),
        "sheets": [], "pcb_doc_paths": [str(root / "Board.PcbDoc")], "parameters": {}})
    monkeypatch.setattr(main._altium, "_netlist", netlist)
    variant = VariantDefinition(name="Lite", dnp_refdes=["C2"])
    monkeypatch.setattr(main, "_variant_state", VariantState([variant]))
    monkeypatch.setattr(main, "_pcb_session", Session())
    monkeypatch.setattr(main, "_custom_cache", {})
    monkeypatch.delenv(cc.PLUGIN_ENV, raising=False)
    return root


def call(**kwargs) -> dict:
    return json.loads(main.run_custom_checks(**kwargs))


def test_tool_errors(project, monkeypatch):
    assert call()["error"] == "no_check_packs"
    write(project / cc.PACK_FILE, {"id": "x"})
    error = call()
    assert error["error"] == "invalid_check_pack" and error["file"].startswith("check_packs.json")
    write(project / cc.PACK_FILE, simple("ok", "A-1"))
    assert call(pack="nope")["error"] == "unknown_pack"
    assert call(rules=["Z-1"])["error"] == "unknown_rule"
    assert call(path="none.json")["error"] == "pack_not_found"
    monkeypatch.setattr(main, "_project", None)
    assert call()["error"] == "no_project"


def test_tool_runs_the_example_pack(project):
    result = call(path=str(EXAMPLE))
    assert result["project"] == "Board" and result["variant"] == "Lite"
    assert result["load"] == "netlist" and result["units"] == "mm"
    assert result["packs"] == [{"id": "team-basics", "title": result["packs"][0]["title"],
                                "version": "1", "file": "check_packs.json (path)",
                                "rules": 7}]
    rows = {row["id"]: row for row in result["rules"]}
    assert set(rows) == {"TB-ESD-01", "TB-DEC-01", "TB-HV-01", "TB-I2C-01", "TB-ASM-01",
                         "TB-BOM-01", "TB-TOOL-01"}
    # The USB lines are protected (one behind R5): pass.
    assert rows["TB-ESD-01"]["status"] == "pass" and rows["TB-ESD-01"]["checked"] == 2
    # U1.8 is far from every capacitor; U2 is not on the board; C2 is not fitted.
    assert rows["TB-DEC-01"]["status"] == "fail" and rows["TB-DEC-01"]["not_evaluated"] == 1
    assert rows["TB-HV-01"]["status"] == "fail"
    assert rows["TB-I2C-01"]["status"] == "pass" and rows["TB-I2C-01"]["checked"] == 2
    assert rows["TB-ASM-01"]["status"] == "pass"  # C2, the bottom 0201, is not fitted
    assert rows["TB-BOM-01"]["status"] == "fail"
    assert rows["TB-TOOL-01"]["kind"] == "tool" and rows["TB-TOOL-01"]["status"] == "pass"
    assert result["verdict"] == "fail" and result["severity_counts"]["error"] == 1
    hv = next(f for f in result["findings"] if f["check"] == "TB-HV-01")
    assert hv["issue"] == ("C3 on HV_48V: rated 63 V for 48 V across it (1.31x, 2x wanted)")
    assert result["findings"][0]["check"] == "TB-HV-01"  # errors first
    assert "tool_runs" in result and result["tool_runs"][0]["tool"] == "check_interfaces"


def test_each_example_rule_finds_what_it_describes(project, monkeypatch):
    """Every example rule fails on a board built to break it."""
    broken = [part for part in BOARD if part[0] not in ("D1", "R2")] + [
        res("R2", "SCL", "IO_X", "4k7")]
    netlist = board_netlist(broken)
    monkeypatch.setattr(main._altium, "_netlist", netlist)
    monkeypatch.setattr(main, "_variant_state", VariantState([]))
    result = call(path=str(EXAMPLE))
    rows = {row["id"]: row["status"] for row in result["rules"]}
    assert rows == {"TB-ESD-01": "fail", "TB-DEC-01": "fail", "TB-HV-01": "fail",
                    "TB-I2C-01": "fail", "TB-ASM-01": "fail", "TB-BOM-01": "fail",
                    "TB-TOOL-01": "fail"}
    esd = [f["issue"] for f in result["findings"] if f["check"] == "TB-ESD-01"]
    assert esd == ["USB_P (connector J1): no TVS/ESD part within 1 series part"]
    i2c = [f["nets"] for f in result["findings"] if f["check"] == "TB-I2C-01"]
    assert i2c == [["SCL"]]


def test_tool_narrows_to_packs_and_rules(project):
    write(project / cc.PACK_FILE, simple("one", "A-1", "A-2"))
    write(project / cc.PACK_FOLDER / "two.json", simple("two", "B-1"))
    assert [r["id"] for r in call()["rules"]] == ["A-1", "A-2", "B-1"]
    assert [r["id"] for r in call(pack="two")["rules"]] == ["B-1"]
    assert [r["id"] for r in call(rules=["a-2"])["rules"]] == ["A-2"]


def test_tool_netlist_free_runs_tool_rules_only(project, monkeypatch):
    monkeypatch.setattr(main._altium, "_netlist", None)
    result = call(path=str(EXAMPLE))
    assert result["load"] == "netlist_free" and result["verdict"] == "incomplete"
    for row in result["rules"]:
        assert row["status"] == "could_not_evaluate"
    assert result["rules"][0]["reason"].startswith("netlist_required")
    assert result["rules"][-1]["reason"].startswith("netlist_required")  # check_interfaces


def test_tool_pages_and_shrinks(project, monkeypatch):
    write(project / cc.PACK_FILE, pack(rule("ALL", select={"pins": {}},
                                            **{"assert": [{"none": True}]})))
    full = call()
    assert full["total"] > 30 and not full["has_more"]
    text = main.run_custom_checks()
    monkeypatch.setattr(main, "QUERY_NET_MAX_RESULT_SIZE_CHARS", len(text) // 2)
    small = call()
    assert small["findings_limited_to"] < 100 and small["has_more"]
    rest = call(offset=small["next_offset"])
    assert rest["findings"][0] != small["findings"][0]
    monkeypatch.setattr(main, "QUERY_NET_MAX_RESULT_SIZE_CHARS", 2500)
    one = call()
    assert one["findings_limited_to"] <= 5 and len(main.run_custom_checks()) <= 2500
    assert one["rules"][0]["status"] == "fail"
    monkeypatch.setattr(main, "QUERY_NET_MAX_RESULT_SIZE_CHARS", 1000)
    brief = call()
    assert "omitted" in brief and brief["total"] == full["total"] and "findings" not in brief
    assert brief["rules"] == [{"id": "ALL", "pack": "demo", "status": "fail",
                               "checked": full["total"], "findings": full["total"]}]
    monkeypatch.setattr(main, "QUERY_NET_MAX_RESULT_SIZE_CHARS", 300)
    assert call()["rule_status"] == {"ALL": "fail"}


def test_tool_shortens_rule_rows_but_keeps_every_rule(project, monkeypatch):
    """Every rule keeps a row with its status on every page, however large the result:
    a checklist's custom item reads it there."""
    passing = [rule(f"P-{n:02d}", title="A rule that passes " * 5,
                    select={"parts": {"designator": "^U1$"}}, **{"assert": [{"has": "mpn"}]})
               for n in range(30)]
    write(project / cc.PACK_FILE, pack(*passing, rule("F-1", select={"parts": {}},
                                                      **{"assert": [{"none": True}]})))
    full = main.run_custom_checks()
    rows_only = json.loads(full)
    rows_only["findings"] = rows_only["findings"][:1]
    limit = len(json.dumps(rows_only, indent=2)) - 200
    monkeypatch.setattr(main, "QUERY_NET_MAX_RESULT_SIZE_CHARS", limit)
    result = call()
    assert result["rule_detail"] in ("compact", "minimal") and len(result["rules"]) == 31
    assert {r["status"] for r in result["rules"][:30]} == {"pass"}
    assert all("title" not in r for r in result["rules"])
    assert result["summary"]["rules"]["pass"] == 30 and result["findings"]
    assert cc.rule_row({"id": "A", "pack": "p", "status": "fail", "checked": 1,
                        "findings": 1, "first": "x" * 300, "title": "t", "ms": 3},
                       "compact") == {"id": "A", "pack": "p", "status": "fail", "checked": 1,
                                      "findings": 1, "first": "x" * 159 + "…"}


def test_tool_plugins_are_refused_without_both_switches(project, monkeypatch):
    marker = project / "ran.txt"
    write(project / cc.PACK_FOLDER / cc.PLUGIN_FOLDER / "mark.py",
          f"open(r'{marker}', 'w').close()\n" + PLUGIN)
    result = call()  # plugins alone count as packs found
    assert result["plugins"]["found"] == ["mark"] and result["plugins"]["run"] is False
    assert "allow_plugins=true" in result["plugins"]["reason"] and result["rules"] == []
    assert call(allow_plugins=True)["plugins"]["run"] is False and not marker.exists()
    monkeypatch.setenv(cc.PLUGIN_ENV, "1")
    assert call()["plugins"]["run"] is False and not marker.exists()
    result = call(allow_plugins=True)
    assert result["plugins"]["run"] is True and marker.exists()
    row = result["rules"][0]
    assert row["id"] == "mark" and row["status"] == "fail" and row["findings"] == 4
    assert result["summary"]["by_pack"]["plugins"]["error"] == 1
    assert call(allow_plugins=True, rules=["mark"])["rules"][0]["id"] == "mark"
    marker.unlink()
    write(project / cc.PACK_FILE, simple("one", "A-1"))
    result = call(allow_plugins=True, pack="one")
    assert [r["id"] for r in result["rules"]] == ["A-1"] and not marker.exists()


def test_tool_reuses_the_run_for_the_next_pages(project, monkeypatch):
    write(project / cc.PACK_FILE, pack(rule("ALL", select={"pins": {}},
                                            **{"assert": [{"none": True}]})))
    runs = []
    real = main.run_custom_rules

    def counting(*args, **kwargs):
        runs.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(main, "run_custom_rules", counting)
    monkeypatch.setattr(main, "QUERY_NET_MAX_RESULT_SIZE_CHARS", 4000)
    first = call()
    second = call(offset=first["next_offset"])
    assert len(runs) == 1 and second["findings"] != first["findings"]
    call(rules=["all"])  # other choices: another run
    assert len(runs) == 2
    write(project / cc.PACK_FILE, pack(rule("ALL", select={"pins": {}}, severity="info",
                                            **{"assert": [{"none": True}]})))
    assert call()["severity_counts"]["info"] == first["total"] and len(runs) == 3


def test_the_example_bom_rule_asks_mounting_hardware_for_its_mpn(project, monkeypatch):
    board = BOARD + [("HS1", {"1": ("1", "GND")}, {"description": "Heat sink TO-220"})]
    monkeypatch.setattr(main._altium, "_netlist", board_netlist(board))
    result = call(path=str(EXAMPLE), rules=["TB-BOM-01"])
    assert any(f["designators"] == ["HS1"] for f in result["findings"])


def test_custom_items_read_the_status_of_a_result_too_large_for_rows():
    def run_custom_checks(pack=None, rules=None, path=None, allow_plugins=False, offset=0):
        return json.dumps({"verdict": "warn", "rule_status": {"A-1": "fail", "A-2": "pass"},
                           "omitted": "too large"})

    checklist = custom_checklist(custom_item("I-1", "A-1"), custom_item("I-2", "a-2"))
    model = rc.run_review_checklist({"name": "B"}, None, checklist,
                                    tools={"run_custom_checks": run_custom_checks})
    assert [row["status"] for row in model["items"]] == ["no", "yes"]
    assert model["items"][1]["evidence"] == "a-2: 0 checked, none failed"


def test_tool_is_registered_and_documented():
    tools = {tool.name: tool for tool in asyncio.run(main.mcp.list_tools())}
    tool = tools["run_custom_checks"]
    assert tool.annotations.readOnlyHint is True and tool.title == "Run Custom Checks"
    assert tool.meta["anthropic/maxResultSizeChars"] == main.QUERY_NET_MAX_RESULT_SIZE_CHARS
    assert set(tool.inputSchema["properties"]) == {"pack", "rules", "path", "allow_plugins",
                                                   "offset"}
    assert "ALTIUM_COPILOT_ALLOW_PLUGINS=1" in tool.description
    assert "run_custom_checks" not in main.NETLIST_TOOLS
    position = main.SERVER_INSTRUCTIONS.index("## Custom Check Packs")
    assert position > 4096 and "run_custom_checks" in main.SERVER_INSTRUCTIONS[position:]
    assert "run_custom_checks" in main.SCHEMATIC_REVIEW_PROMPT
    manifest = json.loads((REPO / "manifest.json").read_text(encoding="utf-8"))
    assert "run_custom_checks" in {entry["name"] for entry in manifest["tools"]}
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    assert "| `run_custom_checks` |" in readme and "### Custom check packs" in readme
    assert "ALTIUM_COPILOT_ALLOW_PLUGINS=1" in readme
    assert "`run_custom_checks`" in (REPO / "CHANGELOG.md").read_text(encoding="utf-8")
    # Tool rules may run the checklist's evidence tools, never the runners themselves.
    assert set(cc.TOOL_RULE_TOOLS) <= set(rc.EVIDENCE_TOOLS)
    assert "run_custom_checks" not in rc.EVIDENCE_TOOLS


def test_readme_pack_is_valid():
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    block = re.search(r"### Custom check packs.*?```json\n(.*?)```", readme, re.DOTALL)[1]
    validated = cc.validate_pack(json.loads(block), "readme")
    assert [r["id"] for r in validated["rules"]] == ["TB-BOM-01", "TB-HV-01", "TB-TOOL-01"]


def test_example_pack_validates_against_the_real_tools():
    data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    validated = cc.validate_pack(data, "example", main._custom_tools())
    assert len(validated["rules"]) == 7
    assert sum(r["kind"] == "select" for r in validated["rules"]) == 6


# ---------- the review checklist ----------

def custom_checklist(*items) -> dict:
    return rc.validate_checklist({
        "id": "team", "title": "Team review", "scope": "schematic",
        "sections": [{"id": "rules", "title": "Rules"}],
        "items": list(items)}, "test", "bundled")


def custom_item(item_id, rule_id) -> dict:
    return {"id": item_id, "section": "rules", "kind": "custom", "text": f"Rule {rule_id}.",
            "rule": rule_id}


@pytest.mark.parametrize("item, words", [
    ({"id": "X-1", "section": "rules", "kind": "custom", "text": "x"}, "rule id"),
    ({"id": "X-1", "section": "rules", "kind": "custom", "text": "x", "rule": "A",
      "evidence": {"tool": "run_erc"}}, "not evidence"),
    ({"id": "X-1", "section": "rules", "kind": "judgment", "text": "x", "rule": "A"},
     "goes with kind 'custom'"),
])
def test_custom_item_validation(item, words):
    with pytest.raises(rc.ChecklistError) as info:
        custom_checklist(item)
    assert words in info.value.error["message"]


def custom_result(rules, plugins=None) -> dict:
    return {"verdict": "warn", "rules": rules, **({"plugins": plugins} if plugins else {})}


def test_custom_items_map_rule_status():
    calls = []

    def run_custom_checks(pack=None, rules=None, path=None, allow_plugins=False, offset=0):
        calls.append(list(rules))
        known = {"A-1", "A-2", "A-3", "A-4", "A-5"}
        unknown = [r for r in rules if r not in known and r != "plug"]
        if unknown:
            return json.dumps({"error": "unknown_rule", "message": "no", "unknown": unknown})
        return json.dumps(custom_result([
            {"id": "A-1", "pack": "p", "status": "pass", "checked": 4, "findings": 0},
            {"id": "A-2", "pack": "p", "status": "fail", "checked": 4, "findings": 2,
             "first": "C1: no mpn", "severity_counts": {"warning": 2},
             "anchors": {"designators": ["C1", "C2"]}},
            {"id": "A-3", "pack": "p", "status": "not_applicable", "checked": 0,
             "findings": 0, "reason": "nothing selected"},
            {"id": "A-4", "pack": "p", "status": "could_not_evaluate", "checked": 3,
             "findings": 0, "reason": "no PCB"},
            {"id": "A-5", "pack": "p", "status": "pass", "checked": 3, "findings": 0,
             "not_evaluated": 1, "not_evaluated_reasons": {"not on the PCB": 1}},
        ], plugins={"found": ["plug"], "run": False, "reason": "not run: x"}))

    checklist = custom_checklist(*(custom_item(f"I-{n}", rule_id) for n, rule_id in
                                   enumerate(["A-1", "A-2", "A-3", "A-4", "A-5", "Z-9", "plug"])))
    model = rc.run_review_checklist({"name": "B"}, None, checklist,
                                    tools={"run_custom_checks": run_custom_checks})
    rows = {row["rule"]: row for row in model["items"]}
    assert calls == [["A-1", "A-2", "A-3", "A-4", "A-5", "plug", "Z-9"],
                     ["A-1", "A-2", "A-3", "A-4", "A-5", "plug"]]
    assert rows["A-1"]["status"] == "yes" and rows["A-1"]["evidence"] == \
        "A-1 (p): 4 checked, none failed"
    assert rows["A-2"]["status"] == "no" and rows["A-2"]["anchors"] == {"designators": ["C1", "C2"]}
    assert rows["A-2"]["evidence"] == "A-2 (p): 2 finding(s) (warning x2) — C1: no mpn"
    assert rows["A-2"]["findings"] == {"A-2": 2} and rows["A-2"]["tool"] == "run_custom_checks"
    assert rows["A-3"]["status"] == "n/a"
    assert rows["A-4"]["status"] == "needs_judgment" and rows["A-4"]["could_not_run"] == "no PCB"
    assert rows["A-5"]["status"] == "needs_judgment" and "could not be evaluated" in \
        rows["A-5"]["evidence"]
    assert rows["Z-9"]["could_not_run"] == "unknown_rule"
    assert rows["plug"]["could_not_run"] == "plugins_not_run"
    assert model["coverage"][-1] == {"section": "all", "title": "All", "yes": 1, "no": 1,
                                     "n/a": 1, "needs_judgment": 4, "total": 7}


def test_custom_items_without_the_tool_or_packs():
    checklist = custom_checklist(custom_item("I-1", "A-1"))
    model = rc.run_review_checklist({"name": "B"}, None, checklist, tools={})
    assert model["items"][0]["could_not_run"] == "tool_not_available"

    def none(pack=None, rules=None, path=None, allow_plugins=False, offset=0):
        return json.dumps({"error": "no_check_packs", "message": "No check pack was found."})

    model = rc.run_review_checklist({"name": "B"}, None, checklist,
                                    tools={"run_custom_checks": none})
    assert model["items"][0]["could_not_run"] == "no_check_packs"
    assert model["items"][0]["evidence"] == "Could not run: No check pack was found."

    def unknown(pack=None, rules=None, path=None, allow_plugins=False, offset=0):
        return json.dumps({"error": "unknown_rule", "message": "no", "unknown": rules})

    model = rc.run_review_checklist({"name": "B"}, None, checklist,
                                    tools={"run_custom_checks": unknown})
    assert model["items"][0]["could_not_run"] == "unknown_rule"


def test_review_checklist_override_adds_a_pack_rule(project, monkeypatch):
    write(project / cc.PACK_FILE, pack(
        rule("HV-1", select={"parts": {"kind": "capacitor", "net_voltage": {"above": 24}}},
             **{"assert": [{"rating_ratio": {"min": 2}}]}),
        rule("BOM-1", select={"parts": {"designator": "U1"}}, **{"assert": [{"has": "mpn"}]}),
        rails={"VIN": 12}))
    write(project / rc.OVERRIDE_FILE, {
        "add_sections": [{"checklist": "team", "id": "packs", "title": "Pack rules"}],
        "add": [{"checklist": "team", **custom_item("TEAM-2", "BOM-1"), "section": "packs"}]})
    checklist = custom_checklist(custom_item("TEAM-1", "HV-1"))
    monkeypatch.setattr(main, "_bundled_checklists", {"team": checklist})
    monkeypatch.setattr(main, "_review_cache", {})
    monkeypatch.setattr(main, "head_state", lambda folder, paths=None: None)
    result = json.loads(main.run_review_checklist(checklist="team"))
    rows = {row["id"]: row for row in result["items"]}
    assert rows["TEAM-1"]["status"] == "no" and rows["TEAM-1"]["anchors"]["designators"] == ["C3"]
    assert rows["TEAM-2"]["status"] == "yes"
    record = main.run_review_checklist(checklist="team", format="markdown")
    assert "| TEAM-1 |" in record and "run_custom_checks" in record
    # A pack edit is seen at once: the cached run is not reused.
    write(project / cc.PACK_FILE, pack(
        rule("HV-1", select={"parts": {"kind": "capacitor", "net_voltage": {"above": 24}}},
             **{"assert": [{"rating_ratio": {"min": 1.2}}]}),
        rule("BOM-1", select={"parts": {"designator": "U1"}}, **{"assert": [{"has": "mpn"}]}),
        rails={"VIN": 12}))
    with patch.object(rc, "folder_signature", lambda root, max_files=None: "same"):
        result = json.loads(main.run_review_checklist(checklist="team"))
    assert {row["id"]: row["status"] for row in result["items"]}["TEAM-1"] == "yes"
