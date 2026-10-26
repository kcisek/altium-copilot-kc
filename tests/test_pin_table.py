"""check_pin_table (roadmap #22): pin tables from datasheets and vendor pinout files
against a part's pins. Every table, part and name here is made up; the vendor samples
are a few rows written in the public header layouts of those formats."""
import asyncio
import io
import json
import zipfile
from pathlib import Path

import pytest

import server.main as main
from parsers.prj_pcb import VariantDefinition, VariantState
from server.parsers import pin_table as pt
from server.parsers.pin_table import (
    PinTableError, function_key, name_kind, normalize_name, parse_pin_file, parse_pin_text,
    pin_key, pin_sort_key, split_functions, split_numbers, type_kind,
)
from server.parsers.sch_lib import LibPin, SchLibComponent
from server.services import pin_table as ps
from tests.library_fixture import binary_pin, sch_component, sch_lib_streams, write_ole

REPO = Path(__file__).parents[1]

# A made-up 12-pin controller: number, name, Altium electrical type.
PART = [("1", "VDD", "Power"), ("2", "PA0", "IO"), ("3", "PA1", "IO"), ("4", "PA2", "IO"),
        ("5", "PA3", "IO"), ("6", "PA4", "IO"), ("7", "PA5", "IO"), ("8", "VSS", "Power"),
        ("9", "NRST", "Input"), ("10", "BOOT0", "Input"), ("11", "PB0", "IO"),
        ("12", "PB1", "IO")]


def design(pins=PART, side="placed", nets=None, hidden=()):
    """DesignPins from (number, name, type) rows; nets: number -> net (placed side)."""
    held = {}
    for number, name, electrical in pins:
        net = (nets or {}).get(number)
        held[pin_key(number)] = ps.DesignPin(
            number=number, name=name, electrical=electrical, net=net,
            connected=(bool(net) if nets is not None else None), hidden=number in hidden)
    return ps.DesignPins(side, held)


def text(rows, header=("Pin", "Name", "Type"), sep="\t") -> str:
    lines = [sep.join(header)] if header else []
    lines += [sep.join(row) for row in rows]
    return "\n".join(lines)


def check(table_text, *sides, package=None):
    return ps.check_pins(parse_pin_text(table_text), list(sides) or [design()], package)


def kinds(model) -> list[str]:
    return [finding["kind"] for finding in model["findings"]]


def finding(model, kind):
    return next(f for f in model["findings"] if f["kind"] == kind)


# ---------------------------------------------------------------- normalisation

def test_pin_numbers_and_ball_order():
    assert pin_key(" a01 ") == "A1" and pin_key("007") == "7"
    balls = ["AA1", "B1", "A10", "A2", "Y1", "A1", "EP", "12", "3", "AB2"]
    assert sorted(balls, key=pin_sort_key) == ["3", "12", "A1", "A2", "A10", "B1", "Y1", "AA1",
                                               "AB2", "EP"]
    assert split_numbers("4, 9, 15") == ["4", "9", "15"]
    assert split_numbers("12-15") == ["12", "13", "14", "15"]
    assert split_numbers("12–14") == ["12", "13", "14"]
    assert split_numbers("PA0") == ["PA0"]  # a ball-like token
    assert split_numbers("VDD") is None and split_numbers("A") is None
    assert split_numbers("A", strict=False) == ["A"] and split_numbers("+", strict=False) == ["+"]
    assert split_numbers("5 (1)") == ["5"]  # a footnote mark


@pytest.mark.parametrize("name", ["nRESET", "~RESET", "!RESET", "/RESET", "RESET#", "RESET_N",
                                  "RESET_B", "R\\E\\S\\E\\T\\",
                                  "R̅E̅S̅E̅T̅"])
def test_active_low_notations_read_as_one(name):
    assert function_key(name) == ("RESET", True)


def test_function_keys():
    assert function_key("RESET") == ("RESET", False)
    assert function_key("I/O") == ("IO", False) and function_key("N/C") == ("NC", False)
    assert function_key("pa09") == ("PA9", False)
    assert function_key("IN-")[0] == "IN_-" and function_key("D+")[0] == "D_+"
    assert function_key("IN−")[0] == "IN_-"  # a minus sign copied from a PDF
    assert function_key("Data-Out.1")[0] == "DATA_OUT_1"
    assert function_key("nCONFIG") == ("CONFIG", True)
    assert function_key("LVDS_6n") == ("LVDS_6N", False)  # a differential n, not active low


def test_split_functions():
    assert split_functions("PA9/SERX9_TXO") == ["PA9", "SERX9_TXO"]
    assert split_functions("I/O") == ["I/O"] and split_functions("/RESET") == ["/RESET"]
    assert split_functions("I/O, VREF 7, L9P_Y") == ["I/O", "VREF 7", "L9P_Y"]
    assert split_functions("IO (DIN, D0)") == ["IO", "DIN", "D0"]
    assert split_functions("PC1-TAMPER (4)") == ["PC1-TAMPER"]  # a footnote mark
    assert split_functions("D(3)") == ["D_3"]  # an index in parentheses
    assert split_functions("OE//RESET") == ["OE", "/RESET"]
    assert split_functions("53") == ["53"]
    assert normalize_name("RESET_N, PA0") == "~RESET, PA0"


def test_name_and_type_classes():
    assert [name_kind(n) for n in ("GND", "VSSA", "VCCO_34", "MGTAVCC", "NC", "DNU", "PA0",
                                   "AVDD", "GNDADC")] == [
        "ground", "ground", "power", "power", "nc", "nc", None, "power", "ground"]
    assert [type_kind(t) for t in ("I/O FT", "S", "G", "OD", "A", "Power", "OpenCollector",
                                   "HiZ", "I/O1", "I (5V tolerant)", "Bidirectional",
                                   "open drain output", "HR", "")] == [
        "io", "power", "ground", "open_drain", "analog", "power", "open_drain", "output",
        "unknown", "input", "io", "open_drain", "unknown", "unknown"]


# ---------------------------------------------------------------- reading tables

@pytest.mark.parametrize("delimiter, rendered", [
    ("tab", text(PART)),
    ("comma", text(PART, sep=",")),
    ("semicolon", text(PART, sep=";")),
    ("pipe", "| Pin | Name | Type |\n|---|:--|---|\n"
     + "\n".join(f"| {a} | {b} | {c} |" for a, b, c in PART)),
    ("spaces", "\n".join(f"{a:<6}{b:<10}{c}" for a, b, c in [("Pin", "Name", "Type"), *PART])),
    ("space", "\n".join(f"{a} {b} {c}" for a, b, c in PART)),
])
def test_every_delimiter(delimiter, rendered):
    source = parse_pin_text(rendered)
    assert source.delimiter == delimiter
    (table,) = source.tables
    assert [(r.number, r.name) for r in table.rows] == [(a, b) for a, b, _ in PART]
    assert check(rendered)["findings"] == []


def test_header_spellings_and_function_as_name():
    source = parse_pin_text("Ball\tSignal Name\tDirection\nA1\tCLK\tI\nA2\tDATA\tI/O\n")
    table = source.tables[0]
    assert table.columns == {"number": "Ball", "name": "Signal Name", "type": "Direction"}
    assert [r.kind for r in table.rows] == ["input", "io"]
    # The layout of an older FPGA datasheet: Function is the name, pins carry a P.
    source = parse_pin_text("Function\tBank\tPin\tVREF Option\nGND\t-\tP1\t-\n"
                            "I/O, L1P\t0\tP2\tAll\nVCCO\t-\tP3\t-\n")
    table = source.tables[0]
    assert table.columns["name"] == "Function" and table.columns["bank"] == "Bank"
    assert [r.kind for r in table.rows] == ["ground", "unknown", "power"]
    assert [r.bank for r in table.rows] == ["", "0", ""]


def test_tables_copied_out_of_a_pdf():
    # Single spaces between the cells, a group title above the header, a dash for no type.
    source = parse_pin_text("PIN\nNAME NO. I/O DESCRIPTION\nVCC 1 — Supply voltage\n"
                            "GND 2 — Ground\nSDA 3 I/O Serial data\nSCL 4 I Serial clock\n")
    assert source.delimiter == "space"
    assert [(r.number, r.name, r.kind) for r in source.tables[0].rows] == [
        ("1", "VCC", "power"), ("2", "GND", "ground"), ("3", "SDA", "io"), ("4", "SCL", "input")]
    # The package names on a second header row under a spanning "Pin number" cell.
    source = parse_pin_text("Pin number\t\tPin name\tType\nLQFP32\tQFN28\t\t\n1\t1\tVDD\tS\n"
                            "2\t2\tPA0\tI/O\n3\t-\tPA1\tI/O\n")
    assert source.packages == ["LQFP32", "QFN28"]
    assert [len(t.rows) for t in source.tables] == [3, 2]
    assert source.tables[1].not_in_package == 1 and source.tables[0].unread_total == 0


def test_single_spaces_with_multi_word_headers_and_cells():
    # A header of two-word names, as a PDF copy gives it.
    source = parse_pin_text("Pin Number Pin Name Type\n1 VDD S\n2 PA0 I/O\n3 PA1 I/O\n")
    assert source.tables[0].columns == {"number": "Pin Number", "name": "Pin Name",
                                        "type": "Type"}
    assert [(r.number, r.name) for r in source.tables[0].rows] == [
        ("1", "VDD"), ("2", "PA0"), ("3", "PA1")]
    # Rows with more words than columns: the words go where a pin number and a type word
    # stay in their columns, the name taking as few as it can (a bare index goes with it).
    source = parse_pin_text(
        "Pin No. Pin Name Type Description\n1 VDD S Supply voltage\n2 MNT 1 - Mounting pad\n"
        "3 I/O, L1P I/O Differential pair\n4 nCF Open Drain Status output\n")
    assert [(r.number, r.name, r.type) for r in source.tables[0].rows] == [
        ("1", "VDD", "S"), ("2", "MNT 1", ""), ("3", "I/O, L1P", "I/O"),
        ("4", "nCF", "Open Drain")]
    assert "4 rows had more words" in source.notes[0]
    source = parse_pin_text("Pin Name Description\n1 VDD Supply voltage input\n"
                            "2 MNT 1 Mounting pad\n")
    assert [r.name for r in source.tables[0].rows] == ["VDD", "MNT 1"]
    # A row no sharing fits is listed as unread, not read with a wrong name.
    source = parse_pin_text("Pin Name Type\n1 VDD S\n2 PA0 I/O\n3 PA1 Fast Mode X\n")
    assert [r.number for r in source.tables[0].rows] == ["1", "2"]
    assert source.tables[0].unread[0].line == 4
    assert "paste the table with tabs" in source.tables[0].unread[0].reason


def test_a_bus_written_with_parentheses_against_footnote_marks():
    source = parse_pin_text("Pin\tName\n1\tD(0)\n2\tD(1)\n3\tPA0(1)\n4\tPC13-TAMPER(4)\n"
                            "5\tPB1 (2)\n")
    assert [r.name for r in source.tables[0].rows] == ["D(0)", "D(1)", "PA0", "PC13-TAMPER",
                                                       "PB1"]
    part = design([("1", "D0", "IO"), ("2", "D1", "IO"), ("3", "PA0", "IO"),
                   ("4", "PC13-TAMPER", "IO"), ("5", "PB1", "IO")])
    assert ps.check_pins(source, [part])["summary"]["verdict"] == "match"
    assert pt.clean_name("RESET*") == "RESET" and normalize_name("RESET*") == "~RESET"


def test_a_data_row_holding_name_is_no_header():
    rows = text([("1", "PA0", "IO"), ("2", "RENAMED_X", "IO"), ("3", "PA2", "IO")])
    source = parse_pin_text(rows)
    assert len(source.tables) == 1 and len(source.tables[0].rows) == 3


def test_headerless_tables_are_inferred():
    source = parse_pin_text("PA0\t2\tI/O\nPA1\t3\tI/O\nVDD\t1\tS\nGND\t4\tG\n")
    table = source.tables[0]
    assert [(r.number, r.name, r.kind) for r in table.rows][:3] == [
        ("2", "PA0", "io"), ("3", "PA1", "io"), ("1", "VDD", "power")]
    assert "no header row" in source.notes[0]
    # Names that read like types (GND, OUT, IN) are still names next to the numbers.
    source = parse_pin_text("1\tGND\tPower\n2\tOUT\tOutput\n3\tIN\tInput\n")
    assert [(r.name, r.kind) for r in source.tables[0].rows] == [
        ("GND", "ground"), ("OUT", "output"), ("IN", "input")]


def test_pin_lists_ranges_and_footnotes():
    source = parse_pin_text("Pin\tName\n1\tPA0(1)\n4, 9, 15\tVSS\n20-22\tVDD\n23\tPB0*\n")
    rows = source.tables[0].rows
    assert [(r.number, r.name) for r in rows] == [
        ("1", "PA0"), ("4", "VSS"), ("9", "VSS"), ("15", "VSS"), ("20", "VDD"), ("21", "VDD"),
        ("22", "VDD"), ("23", "PB0")]


def test_rows_that_cannot_be_read_are_listed():
    source = parse_pin_text("Pin\tName\tType\n1\tVDD\tP\nNotes: see below\n2\t\tI\n"
                            "3 4 x\tPA0\tI/O\n4\tPA1\tI/O\n")
    table = source.tables[0]
    assert [r.number for r in table.rows] == ["1", "4"]
    assert [(u.line, u.reason) for u in table.unread] == [
        (3, "not a pin number: Notes: see below"), (4, "no pin name"),
        (5, "not a pin number: 3 4 x")]
    assert table.unread_total == 3 and source.delimiter == "tab"
    described = ps.table_description(source, table)
    assert described["unread_rows_total"] == 3
    assert described["unread_rows"][1] == {"line": 4, "text": "2\t\tI", "reason": "no pin name"}


def test_amd_xilinx_package_file():
    sample = (
        "Device/Package xcdemo1pkg10 01/01/2026 12:00:00\n\n"
        "Pin   Pin Name           Memory Byte Group  Bank  VCCAUX Group  Super Logic Region  "
        "I/O Type  No-Connect\n"
        "A1    GND                NA                 NA    NA            NA                  NA        NA\n"
        "A2    IO_L1P_T0_14       0                  14    NA            NA                  HR        NA\n"
        "A3    IO_L1N_T0_14       0                  14    NA            NA                  HR        NA\n"
        "B1    VCCO_14            NA                 14    NA            NA                  NA        NA\n"
        "B2    PROGRAM_B_0        NA                 0     NA            NA                  CONFIG    NA\n"
        "B3    IO_L2P_T0_14       0                  14    NA            NA                  HR        YES\n"
        "Total Number of Pins 6\n")
    source = parse_pin_text(sample)
    assert source.format == pt.FORMAT_XILINX and source.device == "xcdemo1pkg10"
    table = source.tables[0]
    assert table.declared_pins == 6 and len(table.rows) == 6
    assert [r.kind for r in table.rows] == ["ground", "unknown", "unknown", "power", "unknown",
                                            "nc"]
    assert table.columns["number"] == "Pin" and "type" not in table.columns
    assert any("bank type" in note for note in source.notes)
    csv_sample = ("Pin,Pin Name,Memory Byte Group,Bank,I/O Type,Super Logic Region\n"
                  "A1,GND,NA,NA,NA,NA\nA2,IO_L1P_T0_14,0,14,HR,NA\n")
    assert parse_pin_text(csv_sample).format == pt.FORMAT_XILINX


INTEL_SAMPLE = (
    '"Pin Information for the Demo DM100 Device\n Version 1.0\n"\t\t\t\t\t\n'
    "Bank Number\tVREF\tPin Name/Function\tOptional Function(s)\tConfiguration Function\t"
    "Dedicated Tx/Rx Channel\tF100\tDQS for X8\n"
    "3A\tVREFB3AN0\tIO\tCLK0p\t\tDIFFIO_1p\tA1\tDQ1\n"
    "3A\tVREFB3AN0\tIO\tCLK0n\tDATA0\tDIFFIO_1n\tA2\tDQ1\n"
    '1C\t\t"RX_CH0p,REFCLK0p"\t\t\t\t\t\n'
    "\t\tGND\t\t\t\tB1\t\n"
    "\t\tnCONFIG\t\tnCONFIG\t\tB2\t\n"
    "Bank Number\tVREF\tPin Name/Function\tOptional Function(s)\tConfiguration Function\t"
    "Dedicated Tx/Rx Channel\tU200\tDQS for X8\n"
    "3A\tVREFB3AN0\tIO\tCLK0p\t\tDIFFIO_1p\tC3\tDQ1\n"
    "3A\tVREFB3AN0\tIO\tCLK0n\tDATA0\tDIFFIO_1n\tC4\tDQ1\n"
    '1C\t\t"RX_CH0p,REFCLK0p"\t\t\t\tD1\t\n'
    "\t\tGND\t\t\t\tD2\t\n"
    "\t\tnCONFIG\t\tnCONFIG\t\tD3\t\n"
    "Notes:\n")


def test_intel_pinout_file_with_a_section_per_package():
    source = parse_pin_text(INTEL_SAMPLE)
    assert source.format == pt.FORMAT_INTEL and source.device == "Demo DM100"
    assert source.packages == ["F100", "U200"]
    small, large = source.tables
    assert [r.number for r in small.rows] == ["A1", "A2", "B1", "B2"]
    assert small.not_in_package == 1 and large.not_in_package == 0
    assert small.rows[1].alternates == ["CLK0n", "DATA0", "DIFFIO_1n"]
    assert [r.kind for r in small.rows] == ["unknown", "unknown", "ground", "unknown"]
    assert large.rows[2].name == "RX_CH0p,REFCLK0p" and large.unread_total == 1
    assert "Optional Function(s)" in small.columns["alternates"]
    assert "DQS for X8" not in small.columns["alternates"]


def test_intel_style_package_columns_in_one_header():
    sample = ("Bank Number\tVREFB Group\tPin Name/Function\tOptional Function(s)\tE44\tF64\n"
              "1\tB1\tIO\tDIFF1p\t1\tA1\n1\tB1\tIO\tDIFF1n\t2\tA2\n\t\tVCC\t\t3\tB1\n"
              "\t\tGND\t\t\tB2\n")
    source = parse_pin_text(sample)
    assert source.packages == ["E44", "F64"]
    assert [len(t.rows) for t in source.tables] == [3, 4]
    assert source.tables[0].not_in_package == 1


def test_stm32cubemx_pinout_csv():
    sample = ('"Position","Name","Type","Signal","Label"\n"1","VBAT","Power","",""\n'
              '"2","PC13-TAMPER","I/O","GPIO_Output","LED"\n"3","NRST","Reset","",""\n'
              '"4","BOOT0","Boot","",""\n"5","PA0","MonoIO","ADC_IN0",""\n'
              '"6","VSS","Power","",""\n')
    source = parse_pin_text(sample)
    assert source.format == pt.FORMAT_STM32 and source.delimiter == "comma"
    rows = source.tables[0].rows
    assert [r.kind for r in rows] == ["power", "io", "input", "input", "io", "ground"]
    assert rows[1].alternates == ["GPIO_Output", "LED"]


def test_datasheet_columns_per_package_and_package_selection():
    sample = ("Pin name\tType\tLQFP12\tQFN10\n" + "".join(
        f"{name}\t{'S' if kind == 'Power' else 'I/O'}\t{number}\t{qfn}\n"
        for (number, name, kind), qfn in zip(PART, ["1", "2", "3", "4", "5", "6", "7", "8", "9",
                                                    "10", "-", "-"])))
    source = parse_pin_text(sample)
    assert source.packages == ["LQFP12", "QFN10"]
    model = ps.check_pins(source, [design()])
    assert model["table_used"]["package"] == "LQFP12"
    assert model["table_used"]["pins_matched_by_package"] == {"LQFP12": 12, "QFN10": 10}
    assert model["findings"][0]["kind"] != "missing_in_design"
    picked = ps.check_pins(source, [design()], "qfn-10")
    assert picked["table_used"]["package"] == "QFN10" and picked["table_used"]["how"] == "package="
    assert {f["pin"] for f in picked["findings"] if f["kind"] == "extra_in_design"} == {"11", "12"}
    with pytest.raises(ValueError, match="LQFP12, QFN10"):
        ps.check_pins(source, [design()], "BGA99")
    single = ps.check_pins(parse_pin_text(text(PART)), [design()], "LQFP12")
    assert "package= was not used" in single["table_used"]["note"]


def xlsx(sheets: dict[str, list[list]]) -> bytes:
    """A workbook of several sheets: strings shared, numbers as Excel stores them."""
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    rel = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    strings: list[str] = []
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        entries, rels = [], []
        for index, (name, rows) in enumerate(sheets.items(), start=1):
            cells = []
            for r, row in enumerate(rows, start=1):
                refs = []
                for c, value in enumerate(row):
                    ref = f"{chr(65 + c)}{r}"
                    if isinstance(value, (int, float)):
                        refs.append(f'<c r="{ref}"><v>{value}</v></c>')
                    elif value != "":
                        strings.append(value)
                        refs.append(f'<c r="{ref}" t="s"><v>{len(strings) - 1}</v></c>')
                cells.append(f'<row r="{r}">{"".join(refs)}</row>')
            archive.writestr(f"xl/worksheets/sheet{index}.xml",
                             f'<worksheet {ns}><sheetData>{"".join(cells)}</sheetData></worksheet>')
            entries.append(f'<sheet name="{name}" sheetId="{index}" r:id="rId{index}"/>')
            rels.append(f'<Relationship Id="rId{index}" Target="worksheets/sheet{index}.xml" '
                        'Type="x"/>')
        archive.writestr("xl/workbook.xml",
                         f'<workbook {ns} {rel}><sheets>{"".join(entries)}</sheets></workbook>')
        archive.writestr("xl/_rels/workbook.xml.rels",
                         '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
                         f'relationships">{"".join(rels)}</Relationships>')
        archive.writestr("xl/sharedStrings.xml", f"<sst {ns}>" + "".join(
            f"<si><t>{s}</t></si>" for s in strings) + "</sst>")
    return buffer.getvalue()


def test_xlsx_workbooks(tmp_path):
    book = tmp_path / "pins.xlsx"
    book.write_bytes(xlsx({"Cover": [["Demo part pinout"], ["Revision", 2]],
                           "Pinout": [["Pin No.", "Pin Name", "I/O"],
                                      *[[int(n), name, "I/O" if t == "IO" else t]
                                        for n, name, t in PART]]}))
    source = parse_pin_file(str(book))
    assert source.sheet == "Pinout" and source.source == "pins.xlsx"
    assert [r.number for r in source.tables[0].rows][:3] == ["1", "2", "3"]
    assert ps.check_pins(source, [design()])["findings"] == []


def test_text_files_in_any_encoding(tmp_path):
    utf16 = tmp_path / "pins.csv"
    utf16.write_bytes(text(PART, sep=",").encode("utf-16"))
    assert len(parse_pin_file(str(utf16)).tables[0].rows) == 12
    ansi = tmp_path / "pins.txt"
    ansi.write_bytes("Pin\tName\n1\tVDD \xb5\n2\tPA0\n".encode("cp1252"))
    assert parse_pin_file(str(ansi)).tables[0].rows[0].name == "VDD µ"


def test_tables_that_cannot_be_read(tmp_path, monkeypatch):
    def code(call):
        with pytest.raises(PinTableError) as error:
            call()
        return error.value.code

    assert code(lambda: parse_pin_text("  \n")) == "empty_table"
    assert code(lambda: parse_pin_text("just some words\nand more words")) == "no_pins_read"
    assert code(lambda: parse_pin_file(str(tmp_path / "none.csv"))) == "file_not_found"
    for name, data, expected in (("a.xls", b"\xd0\xcf\x11\xe0", "xls_not_supported"),
                                 ("a.pdf", b"%PDF-1.4", "pdf_not_supported"),
                                 ("a.bin", b"\0\1\2\3" * 10, "not_a_table"),
                                 ("a.xlsx", b"PK\3\4broken", "unreadable_workbook")):
        (tmp_path / name).write_bytes(data)
        assert code(lambda name=name: parse_pin_file(str(tmp_path / name))) == expected
    empty_book = tmp_path / "empty.xlsx"
    empty_book.write_bytes(xlsx({"Sheet1": [["nothing here"]]}))
    assert code(lambda: parse_pin_file(str(empty_book))) == "no_pins_read"
    monkeypatch.setattr(pt, "MAX_TABLE_BYTES", 10)
    (tmp_path / "big.csv").write_text(text(PART), encoding="utf-8")
    assert code(lambda: parse_pin_file(str(tmp_path / "big.csv"))) == "file_too_large"


# ---------------------------------------------------------------- classifying names

@pytest.mark.parametrize("design_name, table_name, alternates, expected", [
    ("PA0", "PA0", (), "exact"),
    ("pa0", "PA0", (), "exact"),
    ("~RESET", "RESET#", (), "equivalent"),
    ("nRST", "RST_N", (), "equivalent"),
    ("D[3]", "D3", (), "equivalent"),
    ("IO, nCEO_1", "io, nceo-1", (), "equivalent"),  # another case and separators
    ("RESET_N", "reset-n", (), "equivalent"),
    ("~RESET", "RESET", (), "likely_same"),
    ("IO", "IO", ("LVDS_1p", "CLK0p"), "exact"),
    ("IO", "I/O", ("LVDS_1p",), "equivalent"),
    ("IO, LVDS_1p", "IO", ("LVDS_1p", "CLK0p"), "equivalent"),
    ("SERX9_TXO", "PA9", ("SERX9_TXO", "TIM1_CH2"), "likely_same"),
    ("I/O, VREF 7", "I/O", (), "likely_same"),
    ("RESET", "RESET#", (), "likely_same"),
    ("VSS", "GND", (), "likely_same"),
    ("VSS_1", "VSS", (), "likely_same"),
    ("VCCO", "VCCO_0", (), "likely_same"),
    ("PROG_B", "PROGRAM_B", (), "likely_same"),
    ("RST", "RESET", (), "likely_same"),
    ("CLK", "CLOCK", (), "likely_same"),
    ("VDD_3V3", "VDD", (), "likely_same"),
    ("OUT_1", "1OUT", (), "likely_same"),
    ("D(3)", "D3", (), "equivalent"),
    ("RESETn", "nRESET", (), "equivalent"),
    ("TX_UART", "UART_TX", (), "likely_same"),
    ("OUT", "1OUT", (), "likely_same"),
    ("I/O, VREF 7, L9P_Y", "I/O, VREF Bank 7, L9P", (), "likely_same"),
    ("UART_TX", "UART_RX", (), "different"),
    ("PA3", "PA4", (), "different"),
    ("IO_L1P_14", "IO_L1N_14", (), "different"),
    ("VCCO_0", "VCCO_1", (), "different"),
    ("VCCINT", "VCCAUX", (), "different"),
    ("IO, LVDS_6p", "IO", ("LVDS_1n",), "different"),
    # Close but other pins: no abbreviation shorter than three letters or by one letter,
    # no name plus a word, no polarity or channel letter, no supply abbreviations.
    ("D3", "DATA3", (), "different"),
    ("CE", "CEO", (), "different"),
    ("IN", "INT", (), "different"),
    ("RS", "RST", (), "different"),
    ("DQ0", "DQS0", (), "different"),
    ("C1", "CS1", (), "different"),
    ("STAT", "START", (), "different"),
    ("INT", "INIT", (), "different"),
    ("RESETn", "RSET", (), "different"),
    ("VDD", "VDDA", (), "different"),
    ("VDD", "VDDIO", (), "different"),
    ("CLK", "CLK_EN", (), "different"),
    ("CLK_P", "CLK_N", (), "different"),
    ("OUT_A", "OUT_B", (), "different"),
    ("TX0_P", "TX0_N", (), "different"),
    ("C1/CS1_N", "C2/CS1_N", (), "different"),
    ("nIO_PULLUP", "IO, LVDS3A_1n", (), "different"),
    ("3.3 V", "3.4 V", (), "different"),
    ("IN-", "IN", (), "different"),  # a sign is no separator
    ("VIN-", "VIN", (), "different"),
    ("Q\\", "Q#", (), "equivalent"),  # a one-letter complement (Q-bar)
    ("Q\\", "nQ", (), "equivalent"),
    ("OE/R\\E\\S\\E\\T\\", "OE/RESET#", (), "equivalent"),
    ("OE/R\\E\\S\\E\\T\\", "OE//RESET", (), "equivalent"),
    ("", "PA0", (), "unnamed"),
])
def test_classify(design_name, table_name, alternates, expected):
    assert ps.classify(design_name, table_name, alternates)[0] == expected


# ---------------------------------------------------------------- findings

def test_a_table_of_the_part_itself_matches():
    part = design()
    assert check(text(PART))["summary"]["verdict"] == "match"
    model = ps.check_pins(pt.PinSource("self", "generic", None, [ps.table_from_pins(part)]),
                          [part])
    assert model["findings"] == [] and model["summary"]["by_side"]["placed"]["exact"] == 12


def test_missing_extra_and_exempt_pins():
    rows = [row for row in PART if row[0] != "6"] + [("13", "PC0", "IO"), ("14", "NC", "NC")]
    part = design(PART + [("SH1", "SHIELD", "Passive"), ("15", "NC", "Passive"),
                          ("16", "SHDN", "Input")])
    model = check(text(rows), part)
    assert {f["pin"] for f in model["findings"] if f["kind"] == "missing_in_design"} == {"13"}
    assert finding(model, "missing_in_design")["severity"] == "error"
    assert {f["pin"] for f in model["findings"] if f["kind"] == "extra_in_design"} == {"6", "16"}
    assert [p["pin"] for p in finding(model, "extra_exempt")["pins"]] == ["15", "SH1"]
    assert finding(model, "nc_not_in_design")["pins"] == [{"pin": "14", "table": "NC"}]
    assert model["summary"]["verdict"] == "differences"
    # Signals that start like a shield or mounting name are not exempt.
    part = design(PART + [("13", "SH_CP", "Input"), ("14", "SH/LD", "Input"),
                          ("15", "SHIELD_GND", "Passive"), ("MH2", "", "Passive"),
                          ("16", "MH_1", "Passive")])
    model = check(text(PART), part)
    assert {f["pin"] for f in model["findings"] if f["kind"] == "extra_in_design"} == {
        "13", "14"}
    assert [p["pin"] for p in finding(model, "extra_exempt")["pins"]] == ["15", "16", "MH2"]


def test_a_partial_table_lists_the_pins_it_leaves_out_once():
    part = design([(str(n), f"SIG{n}", "IO") for n in range(1, 41)])
    model = check(text([(str(n), f"SIG{n}", "I/O") for n in range(1, 6)]), part)
    assert model["summary"]["verdict"] == "match" and kinds(model) == ["not_in_partial_table"]
    grouped = finding(model, "not_in_partial_table")
    assert grouped["severity"] == "info" and grouped["count"] == 35
    assert any("a partial table" in note for note in model["notes"])
    # A table of most of the pins still warns about each one it leaves out.
    model = check(text([(str(n), f"SIG{n}", "I/O") for n in range(1, 39)]), part)
    assert [f["pin"] for f in model["findings"] if f["kind"] == "extra_in_design"] == [
        "39", "40"]


def test_swapped_pins():
    rows = [list(row) for row in PART]
    rows[3][1], rows[9][1] = rows[9][1], rows[3][1]  # PA2 and BOOT0
    model = check(text(rows))
    swapped = finding(model, "swapped")
    assert swapped["pins"] == ["4", "10"] and swapped["severity"] == "error"
    assert "different_name" not in kinds(model)
    assert model["summary"]["by_side"]["placed"]["swapped_pairs"] == 1


@pytest.mark.parametrize("pins, table_names, pair", [
    # A differential pair, two channels, a pin and its complement, CE and CEO: names
    # that resemble each other, exchanged, are a swap and never a naming difference.
    ([("1", "VDD", "Power"), ("2", "CLK_P", "Input"), ("3", "CLK_N", "Input")],
     ["VDD", "CLK_N", "CLK_P"], ["2", "3"]),
    ([("1", "OUT_A", "Output"), ("2", "IN_A", "Input"), ("3", "OUT_B", "Output")],
     ["OUT_B", "IN_A", "OUT_A"], ["1", "3"]),
    ([("1", "DIFF1", "IO"), ("2", "D\\I\\F\\F\\1\\", "IO"), ("3", "VCC", "Power")],
     ["DIFF1#", "DIFF1", "VCC"], ["1", "2"]),
    ([("1", "VCC", "Power"), ("2", "CE", "Input"), ("3", "CEO", "Output")],
     ["VCC", "CEO", "CE"], ["2", "3"]),
])
def test_close_names_exchanged_are_swaps(pins, table_names, pair):
    rows = [(number, name, "") for (number, _, _), name in zip(pins, table_names)]
    model = check(text(rows, header=("Pin", "Name", "Note")), design(pins))
    assert model["summary"]["verdict"] == "differences"
    assert finding(model, "swapped")["pins"] == pair
    assert "likely_same_name" not in kinds(model)


def test_a_name_that_is_another_pins_is_different():
    # The table copies the next pin's name into pin 1: its name only extends pin 1's.
    pins = [("1", "I/O, VREF 4", "IO"), ("2", "I/O, VREF 4, L26P", "IO"), ("3", "PA0", "IO")]
    model = check(text([("1", "I/O, VREF 4, L26P", "I/O"), ("2", "I/O, VREF 4, L26P", "I/O"),
                        ("3", "PA0", "I/O")]), design(pins))
    different = finding(model, "different_name")
    assert different["pin"] == "1" and different["why"] == (
        "the design gives the table's name to pin 2")
    # A one-pin rename to a name the part does not have is checked on its own merits.
    model = check(text([("1", "VDD", "P"), ("2", "CLK_N", "I")]),
                  design([("1", "VDD", "Power"), ("2", "CLK_P", "Input")]))
    assert finding(model, "different_name")["pin"] == "2"
    # Names many pins share belong to no one pin: VSS_1 against VSS stays likely_same.
    pins = [("1", "VSS_1", "Power"), ("2", "VSS", "Power"), ("3", "PA0", "IO")]
    model = check(text([("1", "VSS", "G"), ("2", "VSS", "G"), ("3", "PA0", "I/O")]),
                  design(pins))
    assert "different_name" not in kinds(model)
    assert finding(model, "likely_same_name")["pins"][0]["pin"] == "1"
    # Q and Q-bar: a table that lost its overlines (Q twice) reads as a marking
    # difference; one that keeps them and exchanges the two is a swap.
    flip_flop = design([("1", "D", "Input"), ("5", "Q", "Output"), ("6", "Q\\", "Output")])
    lost = check(text([("1", "D", "I"), ("5", "Q", "O"), ("6", "Q", "O")]), flip_flop)
    assert kinds(lost) == ["likely_same_name"]
    exchanged = check(text([("1", "D", "I"), ("5", "Q#", "O"), ("6", "Q", "O")]), flip_flop)
    assert kinds(exchanged) == ["swapped"]


def test_a_shift_is_not_read_as_a_swap():
    pins = [("1", "VCC", "Power"), ("2", "I/O, VREF 2, L13N", "IO"), ("3", "I/O, L13P", "IO"),
            ("4", "I/O, VREF 2", "IO"), ("5", "DIN", "Input"), ("6", "GND", "Power")]
    names = ["VCC", "I/O, X1", "I/O, VREF 2, L13N", "I/O, L13P", "I/O, VREF 2", "GND"]
    model = check(text([(n, name, "") for (n, _, _), name in zip(pins, names)],
                       header=("Pin", "Name", "Note")), design(pins))
    assert finding(model, "shifted")["pins"] == ["2", "3", "4"]
    assert "swapped" not in kinds(model)


def test_a_shifted_run():
    rows = [list(row) for row in PART]
    rows[2][1] = "PX9"  # a pin inserted at 3: PA1..PA4 move one pin later
    for index, name in zip(range(3, 7), ("PA1", "PA2", "PA3", "PA4")):
        rows[index][1] = name
    model = check(text(rows))
    shifted = finding(model, "shifted")
    assert shifted["first"] == "3" and shifted["last"] == "6" and shifted["offset"] == 1
    assert shifted["count"] == 4 and shifted["severity"] == "error"
    assert "later" in shifted["message"]
    assert [f["pin"] for f in model["findings"] if f["kind"] == "different_name"] == ["7"]


def test_different_and_likely_same_names():
    rows = [list(row) for row in PART]
    rows[1][1] = "PA0/ADC_IN0"  # the design leaves out an alternate: equivalent
    rows[2][1] = "PA7"  # different
    rows[7][1] = "GND"  # another ground spelling
    rows[8][1] = "NRST#"  # active low marked differently
    model = check(text(rows))
    different = finding(model, "different_name")
    assert different["pin"] == "3" and different["design"]["name"] == "PA1"
    reasons = {f["reason"]: [p["pin"] for p in f["pins"]]
               for f in model["findings"] if f["kind"] == "likely_same_name"}
    assert reasons == {"another ground or no-connect name": ["8"],
                       "the active-low marking differs": ["9"]}
    counts = model["summary"]["by_side"]["placed"]
    assert counts["equivalent"] == 1 and counts["likely_same"] == 2
    assert counts["equivalent_examples"][0]["pin"] == "2"


def test_types():
    rows = [list(row) for row in PART]
    rows[0][2] = "I/O"  # the table makes the supply pin a signal: warning
    rows[1][2] = "Input"  # IO typed where the table says input: info
    rows[8][2] = "Output"  # an input the table calls an output: warning
    rows[10][2] = "Power"
    model = check(text(rows), design(PART[:10] + [("11", "PB0", "Passive"), PART[11]]))
    assert sorted(f["pin"] for f in model["findings"] if f["kind"] == "type_mismatch") == [
        "1", "9"]
    soft = {(f["table_class"], f["design_type"]): [p["pin"] for p in f["pins"]]
            for f in model["findings"] if f["kind"] == "type_differs"}
    assert soft == {("input", "IO"): ["2"], ("power", "Passive"): ["11"]}
    assert ps.type_relation("output", "open_drain") is None
    assert ps.type_relation("nc", "power") == "warning"
    assert ps.type_relation("analog", "passive") is None


def test_duplicates():
    model = check(text(PART + [("5", "PA9", "IO")]))
    duplicate = finding(model, "duplicate_number")
    assert duplicate["side"] == "table" and duplicate["names"] == ["PA3", "PA9"]
    rows = [list(row) for row in PART]
    rows[5][1] = "PA3"  # the table repeats PA3 at pin 6, the design has PA4 there
    model = check(text(rows))
    names = [f for f in model["findings"] if f["kind"] == "duplicate_name"]
    assert [(f["side"], f["pins"]) for f in names] == [("table", ["5", "6"])]
    # One of three same-named pins left out of the table is that pin's finding only.
    supply = design([("1", "VIN", "Power"), ("2", "VIN", "Power"), ("3", "VIN", "Power"),
                     ("4", "SW", "Passive"), ("5", "SW", "Passive"), ("6", "EN", "Input")])
    model = check(text([("1", "VIN", "P"), ("2", "VIN", "P"), ("4", "SW", ""),
                        ("5", "SW", ""), ("6", "EN", "I")]), supply)
    assert kinds(model) == ["extra_in_design"]
    symbol = SchLibComponent("DEMO", "demo", part_count=2, pins=[
        LibPin("1", "A", "IO", 1, 0, False, 0, 0, 0, 0, 0, 0),
        LibPin("1", "B", "IO", 2, 0, False, 0, 0, 0, 0, 0, 0),
        LibPin("2", "C", "IO", 1, 0, False, 0, 0, 0, 0, 0, 0)])
    pins = ps.symbol_pins(symbol)
    assert pins.duplicates == [{"pin": "1", "names": ["A", "B"], "parts": [1, 2]}]
    model = check(text([("1", "A", "IO"), ("2", "C", "IO")]), pins)
    assert finding(model, "duplicate_number")["side"] == "library"


def test_multi_unit_symbols_merge_their_parts():
    def pin(designator, name, part, mode=0, hidden=False, electrical="IO"):
        return LibPin(designator, name, electrical, part, mode, hidden, 0, 0, 0, 0, 0, 0)

    symbol = SchLibComponent("DUAL", "dual", part_count=2, pins=[
        pin("1", "OUTA", 1), pin("2", "INA-", 1), pin("3", "INA+", 1),
        pin("7", "OUTB", 2), pin("6", "INB-", 2), pin("5", "INB+", 2),
        pin("8", "V+", 1, electrical="Power"), pin("8", "V+", 2, electrical="Power"),
        pin("4", "V-", 0, hidden=True, electrical="Power"),
        pin("1", "OUT_ALT", 1, mode=1)])
    pins = ps.symbol_pins(symbol)
    assert sorted(pins.pins, key=pin_sort_key) == ["1", "2", "3", "4", "5", "6", "7", "8"]
    assert pins.pins["8"].parts == [1, 2] and pins.duplicates == []
    assert pins.pins["1"].name == "OUTA" and pins.pins["4"].hidden is True
    rows = [("1", "OUT A", "O"), ("2", "IN A-", "I"), ("3", "IN A+", "I"), ("4", "V-", "G"),
            ("5", "IN B+", "I"), ("6", "IN B-", "I"), ("7", "OUT B", "O"), ("8", "V+", "P")]
    model = check(text(rows), pins)
    assert finding(model, "hidden_power_pins")["pins"] == [{"pin": "4", "design": "V-"}]
    assert "different_name" not in kinds(model) and "missing_in_design" not in kinds(model)
    # A datasheet that numbers its units (1OUT) against a symbol that letters them (OUTA).
    numbered = check(text([("1", "1OUT", "O"), ("7", "2OUT", "O")]), pins)
    assert {f["pin"] for f in numbered["findings"] if f["kind"] == "different_name"} == {
        "1", "7"}


def test_hidden_symbol_pins():
    part = design([("1", "VDD", "Power"), ("2", "SDA", "IO"), ("3", "GND", "Power")],
                  side="library", hidden=("1", "2"))
    model = check(text([("1", "VDD", "P"), ("2", "SDA", "I/O"), ("3", "GND", "G")]), part)
    assert finding(model, "hidden_power_pins")["pins"] == [{"pin": "1", "design": "VDD"}]
    assert finding(model, "hidden_signal_pin")["pin"] == "2"


def test_connections_of_the_placed_part():
    nets = {"2": "N1", "3": "N2", "8": "GND", "9": "RST", "10": "B0", "11": "N3", "12": "N4",
            "4": "N5", "5": "N6", "6": "N7", "7": "N8"}
    rows = [list(row) for row in PART] + [["13", "NC", "NC"]]
    part = design(PART + [("13", "NC", "Passive")], nets={**nets, "13": "N9"})
    model = check(text(rows), part)
    assert finding(model, "power_pin_unconnected")["pin"] == "1"
    assert finding(model, "nc_pin_connected")["pins"] == [{"pin": "13", "design": "NC",
                                                          "net": "N9"}]
    component = {"pin_designators": ["1", "2"], "pin_types": {"1": "Power", "2": "IO"},
                 "pin_names": {"1": "VDD", "2": "PA0"}, "pins": {}}
    unwired = ps.placed_pins(component)
    assert unwired.pins["1"].connected is None and "on a net" in unwired.notes[0]
    model = check(text(PART[:2]), unwired)
    assert "power_pin_unconnected" not in kinds(model)
    assert any("connections were not checked" in note for note in model["notes"])


def test_placed_pins_from_the_netlist():
    component = {"pin_designators": ["1", "2", "3"], "pin_types": {"1": "Power", "2": "IO"},
                 "pin_names": {"1": "VDD", "2": "PA0"},
                 "pins": {"1": {"name": "VDD", "net": "3V3"}, "2": {"name": "PA0", "net": "A"},
                          "40": {"name": "", "net": "X"}}}
    pins = ps.placed_pins(component)
    assert sorted(pins.pins) == ["1", "2", "3"]
    assert pins.pins["1"].net == "3V3" and pins.pins["3"].connected is False
    assert "another part compiled with the same designator" in pins.notes[0]
    assert sorted(ps.placed_pins({"pins": {"1": {"name": "A", "net": "N"}}}).pins) == ["1"]


def test_an_exposed_pad_numbered_differently():
    rows = [list(row) for row in PART] + [["EP", "Exposed pad (GND)", "G"]]
    part = design(PART + [("13", "GND", "Power")])
    model = check(text(rows), part)
    paired = finding(model, "exposed_pad_numbered_differently")
    assert paired["pins"][0]["pin"] == "13" and paired["pins"][0]["table_pin"] == "EP"
    assert "missing_in_design" not in kinds(model) and "extra_in_design" not in kinds(model)


def test_bga_findings_in_natural_order():
    balls = [("A1", "VCC", "P"), ("A2", "D0", "I/O"), ("A10", "D1", "I/O"), ("B1", "D2", "I/O"),
             ("AA1", "D3", "I/O"), ("AB2", "GND", "G")]
    model = check(text(balls), design([("A1", "VCC", "Power")]))
    assert [f["pin"] for f in model["findings"] if f["kind"] == "missing_in_design"] == [
        "A2", "A10", "B1", "AA1", "AB2"]


def test_a_numbering_prefix_is_dropped():
    rows = [(f"P{n}", name, kind) for n, name, kind in PART]
    model = check(text(rows))
    assert model["findings"] == []
    assert any("table's pin numbers carry the prefix 'P'" in note for note in model["notes"])
    # The other way round: the design numbers its pins P1..P12, the table 1..12.
    model = check(text(PART), design([(f"P{n}", name, kind) for n, name, kind in PART]))
    assert model["findings"] == []
    assert any("design's pin numbers carry the prefix 'P'" in note for note in model["notes"])


def test_placed_and_library_sides_merge():
    placed = design()
    library = design([row if row[0] != "12" else ("12", "PB2", "IO") for row in PART],
                     side="library")
    model = check(text(PART), placed, library)
    assert finding(model, "different_name")["in"] == ["library"]
    assert model["summary"]["by_side"]["placed"]["exact"] == 12
    differs = model["placed_vs_library"]
    assert differs["same"] is False and differs["renamed"] == [
        {"pin": "12", "placed": "PB1", "library": "PB2"}]
    rows = [list(row) for row in PART]
    rows[2][1] = "PA7"
    rows[8][2] = "Output"
    both = check(text(rows), placed, design(PART, side="library"))
    assert finding(both, "different_name")["in"] == ["placed", "library"]
    # A placed pin on a net (and a hidden symbol pin) is still one finding of both sides,
    # counted once, with the net and the hidden mark kept.
    wired = design(nets={number: f"N{number}" for number, _, _ in PART})
    both = check(text(rows), wired, design(PART, side="library", hidden=("3",)))
    different = [f for f in both["findings"] if f["kind"] == "different_name"]
    assert len(different) == 1 and different[0]["in"] == ["placed", "library"]
    assert different[0]["design"] == {"name": "PA1", "type": "IO", "net": "N3", "hidden": True}
    assert finding(both, "type_mismatch")["in"] == ["placed", "library"]
    assert both["summary"]["severity_counts"] == {"error": 1, "warning": 1}
    assert check(text(PART), placed, design(PART, side="library"))["placed_vs_library"] == {
        "same": True}


def test_a_table_for_another_part_is_a_poor_match():
    other = [(n, f"Q{n}X", "I/O") for n, _, _ in PART]
    model = check(text(other))
    assert model["summary"]["verdict"] == "poor_match"
    assert model["notes"][0].startswith("Only 0 of the table's 12 pins")


def test_paging():
    model = check(text([(str(n), f"SIG{n}", "I/O") for n in range(1, 40)]))
    page = ps.page(model, 5, 10)
    assert page["offset"] == 5 and len(page["findings"]) == 10 and page["next_offset"] == 15
    assert page["findings_total"] == len(model["findings"]) and "_table" not in page
    last = ps.page(model, len(model["findings"]) - 1, 10)
    assert last["has_more"] is False and "next_offset" not in last


# ---------------------------------------------------------------- the MCP tool

def _symbol_library(path: Path) -> None:
    pins = [binary_pin(n, name, electrical={"Power": 7, "IO": 1, "Input": 0}[kind])
            for n, name, kind in PART]
    pins[0] = binary_pin("1", "VDD", electrical=7, conglomerate=0x1C)  # hidden
    write_ole(path, sch_lib_streams(
        ("DEMO12", sch_component("DEMO12", pins, "made-up controller", designator="U?"))))


def _component(name_of=None, library="Parts.SchLib", workspace=False):
    names = {n: (name_of or {}).get(n, name) for n, name, _ in PART}
    return {"lib_reference": "DEMO12", "design_item_id": "DEMO12", "library": library,
            "workspace_part": workspace, "database_table": "", "kind": 0,
            "pin_designators": [n for n, _, _ in PART],
            "pin_types": {n: kind for n, _, kind in PART}, "pin_names": names,
            "pins": {n: {"name": names[n], "net": f"N{n}"} for n, _, _ in PART}}


@pytest.fixture
def loaded(monkeypatch, tmp_path):
    root = tmp_path / "Board"
    (root / "Libs").mkdir(parents=True)
    _symbol_library(root / "Libs" / "Parts.SchLib")
    (root / "Board.PrjPcb").write_text(
        "[Design]\n[Document1]\nDocumentPath=Board.SchDoc\n"
        "[Document2]\nDocumentPath=Libs\\Parts.SchLib\n", encoding="utf-8")
    netlist = {"components": {
        "U1": _component({"12": "PB9"}),  # the placed part was renamed on the sheet
        "U2": _component(library="Some Workspace", workspace=True),
    }}
    project = {"name": "Board", "root_dir": str(root), "prj_pcb_path": str(root / "Board.PrjPcb"),
               "sheets": [], "pcb_doc_paths": [], "parameters": {}}
    monkeypatch.setattr(main, "_project", project)
    monkeypatch.setattr(main._altium, "_netlist", netlist)
    monkeypatch.setattr(main, "_variant_state", VariantState([VariantDefinition(name="Default")]))
    monkeypatch.setattr(main, "_installed_library_paths", [])
    return root


def call(**kwargs) -> dict:
    return json.loads(main.check_pin_table(**kwargs))


def test_tool_compares_the_placed_part_and_its_symbol(loaded):
    result = call(designator="u1", table=text(PART))
    assert result["project"] == "Board"
    target = result["target"]
    assert target["designator"] == "U1" and target["compared"] == ["placed", "library"]
    assert target["library_symbol"]["found"] is True
    assert target["library_symbol"]["library_file"] == "Parts.SchLib"
    assert result["table"]["columns"] == {"number": "Pin", "name": "Name", "type": "Type"}
    assert result["table"]["format"] == "generic" and result["table"]["delimiter"] == "tab"
    different = [f for f in result["findings"] if f["kind"] == "different_name"]
    assert [(f["pin"], f["in"]) for f in different] == [("12", ["placed"])]
    assert finding(result, "hidden_power_pins")["in"] == ["library"]
    assert result["placed_vs_library"]["counts"]["renamed"] == 1
    assert result["summary"]["verdict"] == "differences"
    assert result["findings_total"] == len(result["findings"]) and result["has_more"] is False
    placed = call(designator="U1", table=text(PART), compare="placed")
    assert placed["target"]["compared"] == ["placed"] and "library_symbol" not in placed["target"]


def test_tool_for_a_library_symbol(loaded):
    result = call(lib_reference="DEMO12", table=text(PART))
    assert result["target"] == {"library_symbol": result["target"]["library_symbol"],
                                "compared": ["library"]}
    assert result["summary"]["verdict"] == "match"
    with_part = call(designator="U2", lib_reference="DEMO12", table=text(PART))
    assert with_part["target"]["compared"] == ["placed", "library"]


def test_tool_reads_files_relative_to_the_project(loaded):
    (loaded / "pins.csv").write_text(text(PART, sep=","), encoding="utf-8")
    result = call(lib_reference="DEMO12", path="pins.csv")
    assert result["table"]["source"] == "pins.csv" and result["table"]["delimiter"] == "comma"
    (loaded / "pins.xlsx").write_bytes(xlsx({"Pins": [["Pin", "Name"],
                                                      *[[n, name] for n, name, _ in PART]]}))
    book = call(lib_reference="DEMO12", path=str(loaded / "pins.xlsx"))
    assert book["table"]["sheet"] == "Pins" and book["summary"]["verdict"] == "match"


def test_tool_with_a_workspace_part(loaded):
    result = call(designator="U2", table=text(PART))
    assert result["target"]["compared"] == ["placed"]
    assert result["target"]["library_symbol"]["reason"] == "workspace part"
    assert any("library symbol was not compared" in note for note in result["notes"])
    error = call(designator="U2", table=text(PART), compare="library")
    assert error["error"] == "library_symbol_not_found"


def test_tool_notes_an_alternate_part(loaded, monkeypatch):
    monkeypatch.setattr(main, "shown_variant_values", lambda *args: (
        {}, {"variant": "Lite", "alternate_part": {"lib_reference": "DEMO12B"}}))
    result = call(designator="U1", table=text(PART))
    assert any("fits the alternate part DEMO12B" in note for note in result["notes"])


def test_tool_errors(loaded, monkeypatch):
    assert call(table=text(PART))["error"] == "nothing_to_check"
    assert call(designator="U1")["error"] == "invalid_arguments"
    assert call(designator="U1", table="x", path="y")["error"] == "invalid_arguments"
    assert call(lib_reference="DEMO12", table=text(PART), compare="placed")["error"] == \
        "invalid_arguments"
    assert call(designator="U1", table=text(PART), compare="all")["error"] == "invalid_arguments"
    assert call(designator="U9", table=text(PART))["error"] == "component_not_found"
    assert call(designator="U1", table="   ")["error"] == "empty_table"
    assert call(designator="U1", path="missing.csv")["error"] == "file_not_found"
    missing = call(lib_reference="NOPE", table=text(PART))
    assert missing["error"] == "library_symbol_not_found"
    packages = "Pin name\tLQFP12\tQFN10\nVDD\t1\t1\nPA0\t2\t2\n"
    package = call(designator="U1", table=packages, package="BGA9")
    assert package["error"] == "package_not_found" and package["packages"] == ["LQFP12", "QFN10"]
    chosen = call(designator="U1", table=packages)["table"]
    assert chosen["package"] == "LQFP12" and chosen["packages"] == ["LQFP12", "QFN10"]
    assert chosen["package_selection"]["pins_matched_by_package"] == {"LQFP12": 2, "QFN10": 2}
    monkeypatch.setattr(main, "PIN_TABLE_MAX_TEXT", 5)
    assert call(designator="U1", table=text(PART))["error"] == "table_too_large"
    monkeypatch.setattr(main, "_project", None)
    assert call(designator="U1", table=text(PART))["error"] == "no_project"


def test_tool_without_a_netlist(loaded, monkeypatch):
    monkeypatch.setattr(main._altium, "_netlist", None)
    result = call(designator="U1", table=text(PART))
    assert result["error"] == "library_symbol_not_found"
    assert "not on the PCB" in result["library_symbol"]["reason"]
    assert call(designator="U1", table=text(PART), compare="placed")["error"] == \
        "netlist_required"
    symbol = call(lib_reference="DEMO12", table=text(PART))
    assert symbol["summary"]["verdict"] == "match" and "netlist" in symbol


def test_tool_pages_and_shrinks(loaded, monkeypatch):
    rows = [(str(n), f"SIG{n}", "I/O") for n in range(1, 260)]
    full = call(lib_reference="DEMO12", table=text(rows))
    shown = len(full["findings"])
    assert full["findings_total"] > shown and full["has_more"] and full["next_offset"] == shown
    rest = call(lib_reference="DEMO12", table=text(rows), offset=full["findings_total"] - 3)
    assert len(rest["findings"]) == 3 and rest["has_more"] is False
    monkeypatch.setattr(main, "QUERY_NET_MAX_RESULT_SIZE_CHARS", 1_000_000)
    whole = call(lib_reference="DEMO12", table=text(rows))
    assert len(whole["findings"]) == 200 and "findings_limited_to" not in whole
    size = len(main.check_pin_table(lib_reference="DEMO12", table=text(rows)))
    monkeypatch.setattr(main, "QUERY_NET_MAX_RESULT_SIZE_CHARS", size - 1)
    smaller = call(lib_reference="DEMO12", table=text(rows))
    assert smaller["findings_limited_to"] < 200
    monkeypatch.setattr(main, "QUERY_NET_MAX_RESULT_SIZE_CHARS", 600)
    brief = call(lib_reference="DEMO12", table=text(rows))
    assert "findings_omitted" in brief and brief["findings_total"] == full["findings_total"]
    assert "findings" not in brief and brief["summary"]["verdict"] == "poor_match"


def test_tool_is_registered_and_documented():
    tools = {tool.name: tool for tool in asyncio.run(main.mcp.list_tools())}
    tool = tools["check_pin_table"]
    assert tool.annotations.readOnlyHint is True and tool.title == "Check Pin Table"
    assert tool.meta["anthropic/maxResultSizeChars"] == main.QUERY_NET_MAX_RESULT_SIZE_CHARS
    assert set(tool.inputSchema["properties"]) == {"designator", "lib_reference", "table", "path",
                                                   "package", "compare", "offset"}
    assert "check_pin_table" not in main.NETLIST_TOOLS
    position = main.SERVER_INSTRUCTIONS.index("## Pin Tables")
    assert position > 4096 and "check_pin_table" in main.SERVER_INSTRUCTIONS[position:]
    manifest = json.loads((REPO / "manifest.json").read_text(encoding="utf-8"))
    assert "check_pin_table" in {entry["name"] for entry in manifest["tools"]}
    assert "| `check_pin_table` |" in (REPO / "README.md").read_text(encoding="utf-8")
    assert "`check_pin_table`" in (REPO / "CHANGELOG.md").read_text(encoding="utf-8")
