"""ActiveBOM documents (.BomDoc, roadmap #43): the parser (parsers/bom_doc.py), the join to
a variant's parts and the supply checks (services/active_bom.py), get_active_bom and what
check_bom, get_bom, verify_outputs and package_for_xfn take from it. Every BomDoc here is
synthetic, written in the record layout Altium uses, with made-up makers and part
numbers."""

import json
import sqlite3
import sys
from contextlib import closing
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

import server.main as main_mod
from server.parsers import bom_doc as bd
from server.parsers.prj_pcb import VariantDefinition, VariantState, Variation
from server.services import active_bom as ab
from server.services import verify_outputs as vo
from server.services.bom import build_parts, plan_variant, variant_names
from server.services.bom_checks import BOMDOC_CHECKS, CHECKS, run_bom_checks
from tests.test_output_jobs import _outjob, _output

NOW = datetime(2026, 6, 1, 12, 0)
OLE_EPOCH = datetime(1899, 12, 30)


def ole(moment: datetime) -> str:
    return repr((moment - OLE_EPOCH).total_seconds() / 86400)


def record(kind: str, **fields) -> str:
    return "|RECORD=" + kind + "".join(f"|{key}={value}" for key, value in fields.items())


def choice(maker="Acme", mpn="ACME-R10K-01", supplier="PartsHub", sku="PH-0001", stock=500,
           breaks=((1, 0.10), (100, 0.05), (1000, 0.02)), lifecycle="Production",
           updated=NOW - timedelta(days=30), rank=3, **extra) -> str:
    fields = {
        "PARTCHOICETYPE": "AUTO_PCL", "SUPPLIERSOURCE": "", "MANUFACTURER": maker,
        "MANUFACTURERPARTNO": mpn, "SUPPLIER": supplier, "SUPPLIERPARTNO": sku,
        "PROVIDERNAME": "", "PROVIDERGUID": "", "DESCRIPTION": "made-up part", "RANK": rank,
        "AVAILABLEQUANTITY": "" if stock is None else stock, "BESTUNITPRICE": 0,
        "CURRENCY": "USD", "PRODUCTPHOTOURL": "", "LIFECYCLESTATUSID": lifecycle,
        "LIFECYCLESTATUS": {"Production": "Volume Production", "NRND":
                            "Not Recommended for New Design"}.get(lifecycle, lifecycle),
        "LASTUPDATEDAT": ole(updated) if updated else "", "LASTUPDATED": "Updated 1 month ago",
        "PRICECOUNT": len(breaks),
    }
    for index, (quantity, price) in enumerate(breaks, 1):
        fields[f"P{index}"], fields[f"Q{index}"], fields[f"L{index}"] = price, quantity, ""
    fields.update(extra)
    return record("PartChoice", **fields)


def item(design_item_id, line=None, item_type="Automatic-NonVault", source="Demo.SchLib",
         description="", comment="", parameters="", choices=(), solutions=(), note="",
         status="Unknown", unique_id=None, others=()) -> list[str]:
    rows = [record(
        "CatalogItem", ITEMTYPE=item_type,
        UNIQUEID=unique_id if unique_id is not None else f"{source}\\{design_item_id}",
        DESIGNITEMID=design_item_id, ITEMSOURCE=source, STATUS=status,
        DESCRIPTION=description, USERCOMMENTS=comment, NOTE=note,
        LINENUMBER="" if line is None else line, SELECTEDPARAMETERSHASH="",
        COMPONENTPARAMETERS=parameters, CUSTOMPARAMETERS="")]
    rows += list(choices)
    rows += [record("Solution", MANUFACTURER=maker, MANUFACTURERPARTNO=mpn, RANK=rank)
             for maker, mpn, rank in solutions]
    rows += list(others)
    rows.append(record("PartChoiceGroups", GROUPBYFIELDCOUNT=0))
    return rows


def bom_doc(*items, production=1, currency="USD", suppliers="", use_order="False",
            extra=()) -> bytes:
    rows = [
        record("BOM", VERSION=2, FILENAME="C:\\Work\\Demo.$$$", KIND="ALTIUM_DESIGNER_LIVEBOM",
               DATE="01.05.2026", TIME="10:00:00", CURRENCY=currency,
               PRODUCTIONQUANTITY=production, PERUNITTARGETPRICE=12.5, PERUNITACTUALPRICE=0),
        record("GeneralOptions", OPENEXPORTED="False", ADDTOPROJECT="False", FORCEFIT="False",
               NOTFITTED="False", TEMPLATEFILENAME="", BATCHMODE=0, SUPPLIERPRODQTY=0,
               SUPPLIERAUTOQTY="False", SUPPLIERUSECACHEDPRICING="False", SUPPLIERCURRENCY="",
               SOLUTIONSPERITEM=2, SUPPLIERSPERSOLUTION=1, DISPLAYTOTALROW="False",
               DISPLAYSUBTOTALSFORGROUPS="False", INCLUDEPCBPARAMETERS="False",
               INCLUDEDBPARAMETERS="False", INCLUDEVAULTPARAMETERS="False",
               FILTEROUTRISKYSOLUTIONS="True", DESIGNATORGROUPING="No"),
        record("SortOrderOptions", SORTORDER=""), record("GroupOrderOptions", GROUPORDER=""),
        record("VisibleOrderOptions", VISIBLEORDER=""),
        record("VisibleOrderOptions2", BASEVIEW="Line #=-1!Comment=120!Designator=-1",
               FLATVIEW="", CONSOLIDATEDVIEW=""),
        record("CustomColumns", CUSTOMCOLUMNS="Assembly Note=1!Approved=1"),
        record("ColumnsAliases", COLUMNSALIASES="Comment=Name"),
        record("FilterOptions", FILTER=""),
        record("CheckGroupCompParamsOptions",
               CHECKGROUPCOMPPARAMS="Comment=0,Description=0,Value=0,Footprint=1"),
        record("ManufacturerLinkOptions", MANUFACTURERLINKPARAMETERS=""),
        record("FavoriteSuppliers", PARTSOURCES="", SUPPLIERS=suppliers,
               USESUPPLIERORDER=use_order),
        record("CheckOptions", CHECKLEVELS=""),
        record("LineNumberingOptions", STARTVALUE=10, INCREMENT=10),
        *extra,
        record("Catalog", DBLINKFILEPATH=""),
    ]
    for rows_of_item in items:
        rows.extend(rows_of_item)
    return ("\r\n".join(rows) + "\r\n").encode("cp1252")


# ---------------------------------------------------------------- the design

def component(design_item_id, comment, footprint="R0402", kind=0, **params) -> dict:
    return {"kind": kind, "design_item_id": design_item_id, "lib_reference": design_item_id,
            "footprint": footprint, "description": f"{comment} part",
            "params": {"comment": comment, **params}, "pins": {}, "unique_id": "",
            "unique_ids": []}


def design_netlist() -> dict:
    """R1/R2: one resistor item at 10k, R3 the same item at 22k (split by Value); C1/C2 a
    capacitor; U1 a regulator from a Workspace item now at revision -2; J1 a connector
    with no BomDoc line; MH1 a no-BOM part."""
    resistor = {"manufacturer part number 1": "ACME-R10K-01", "manufacturer 1": "Acme"}
    return {"components": {
        "R1": component("RES-ITEM", "10k", value="10k", **resistor),
        "R2": component("RES-ITEM", "10k", value="10k", **resistor),
        "R3": component("RES-ITEM", "22k", value="22k",
                        **{"manufacturer part number 1": "ACME-R22K-01",
                           "manufacturer 1": "Acme"}),
        "C1": component("CAP-ITEM", "100n", "C0402", value="100n",
                        **{"manufacturer part number 1": "GLOBEX-C100N"}),
        "C2": component("CAP-ITEM", "100n", "C0402", value="100n",
                        **{"manufacturer part number 1": "GLOBEX-C100N"}),
        "U1": component("CMP-9001-00042-2", "REG", "REGPKG-5"),
        "J1": component("HDR-ITEM", "HDR", "HDR1X4"),
        "MH1": component("HOLE-ITEM", "MH", "MH3", kind=5),
    }, "pin_to_net": {}, "nets": {}}


def design_doc(**kwargs) -> bytes:
    return bom_doc(
        item("RES-ITEM", 10, description="Resistor 10k", comment="10k",
             parameters="Comment=10k,Value=10k,Footprint=R0402",
             choices=[choice(), choice(supplier="ChipMart", sku="CM-77", stock=0)]),
        item("RES-ITEM", 20, description="Resistor 22k", comment="22k",
             parameters='Comment=22k,Value=22k,Footprint=R0402,"Note=two words"',
             choices=[choice(mpn="ACME-R22K-01", sku="PH-0002", stock=0,
                             updated=NOW - timedelta(days=800))]),
        item("CAP-ITEM", 30, description="Capacitor", comment="100n",
             choices=[choice("Globex", "GLOBEX-C100N", sku="PH-0100", stock=1,
                             lifecycle="NRND"),
                      choice("Initech", "INI-C100N-X", sku="PH-0101", stock=5000)],
             solutions=[("Initech", "INI-C100N-X", 5)]),
        item("CMP-9001-00042-1", 40, item_type="Automatic-Vault", source="Workspace",
             unique_id="ITEMGUID\\REVGUID", description="Regulator",
             choices=[choice("Umbrella", "UMB-REG-5", sku="PH-0200", stock=10,
                             lifecycle="Obsolete")]),
        item("OLD-ITEM", 50, description="Removed part", choices=[choice(mpn="OLD-1")]),
        item("PCB-001", 60, item_type="Manual-NonVault", description="Bare board",
             comment="PCB", choices=[choice("Fabco", "FAB-PCB-1", sku="FB-1", stock=None,
                                            breaks=((5, 20.0),))]),
        **kwargs)


def parts_of(netlist, variant=None):
    plan = plan_variant(variant, netlist, variant_names(netlist))
    parts, _ = build_parts(netlist, plan, {})
    return parts, plan


def model_of(data: bytes | None = None, netlist=None, variant=None) -> ab.ActiveBom:
    netlist = netlist or design_netlist()
    doc = bd.parse_bom_doc_bytes(data or design_doc(), "Board.BomDoc")
    parts, plan = parts_of(netlist, variant)
    return ab.build_active_bom(doc, parts, netlist, plan, plan.name, now=NOW)


def by_number(model, number):
    return next(line for line in model.all_lines if line.item.line_number == number)


# ---------------------------------------------------------------- the parser

def test_every_record_kind_is_read():
    doc = bd.parse_bom_doc_bytes(design_doc(extra=[record("FutureOptions", MODE="x")]))
    assert doc.header["PRODUCTIONQUANTITY"] == "1" and doc.currency == "USD"
    assert doc.production_quantity == 1
    for kind in bd.OPTION_KINDS:
        assert kind in doc.options, kind
    assert doc.options["GeneralOptions"]["SOLUTIONSPERITEM"] == "2"
    assert [record.kind for record in doc.other_records] == ["FutureOptions"]
    assert doc.record_counts["CatalogItem"] == 6 and doc.record_counts["Solution"] == 1
    first = doc.items[0]
    assert (first.line_number, first.design_item_id, first.item_source, first.item_type) == (
        10, "RES-ITEM", "Demo.SchLib", "Automatic-NonVault")
    assert first.component_parameters == {"Comment": "10k", "Value": "10k",
                                          "Footprint": "R0402"}
    assert doc.items[1].component_parameters["Note"] == "two words"
    assert first.groups == [{"GROUPBYFIELDCOUNT": "0"}]
    pc = first.part_choices[0]
    assert (pc.manufacturer, pc.mpn, pc.supplier, pc.sku, pc.stock, pc.rank) == (
        "Acme", "ACME-R10K-01", "PartsHub", "PH-0001", 500, 3)
    assert [(b.quantity, b.price) for b in pc.price_breaks] == [(1, 0.1), (100, 0.05),
                                                                (1000, 0.02)]
    assert pc.lifecycle_id == "Production" and pc.lifecycle == "Volume Production"
    assert abs((pc.last_updated - (NOW - timedelta(days=30))).total_seconds()) < 1
    cap = doc.items[2]
    assert [(s.manufacturer, s.mpn, s.rank) for s in cap.solutions] == [
        ("Initech", "INI-C100N-X", 5)]
    assert not doc.items[5].automatic and doc.items[0].automatic


def test_unknown_records_and_keys_are_kept_raw():
    doc = bd.parse_bom_doc_bytes(bom_doc(item(
        "X-1", 1, choices=[choice(NEWKEY="kept", P21=0.01, Q21=50000)],
        others=[record("ItemLink", ID="7", TARGET="elsewhere")])))
    line = doc.items[0]
    assert [(r.kind, r.fields["ID"]) for r in line.other_records] == [("ItemLink", "7")]
    pc = line.part_choices[0]
    assert pc.extra == {"NEWKEY": "kept"}
    assert pc.price_breaks[-1].quantity == 50000  # P21/Q21: breaks beyond 20 read too


def test_pipes_and_line_breaks_inside_values():
    text = (record("BOM", PRODUCTIONQUANTITY=1) + "\r\n" + record("Catalog") + "\r\n"
            + "|RECORD=CatalogItem|DESIGNITEMID=A|DESCRIPTION=Header 2|4 row|NOTE=first line"
            + "\r\nsecond line|LINENUMBER=3\r\n")
    doc = bd.parse_bom_doc_bytes(text.encode())
    (line,) = doc.items
    assert line.description == "Header 2|4 row"
    assert line.note == "first line\nsecond line" and line.line_number == 3
    assert doc.continuation_lines == 1


def test_fields_after_a_multi_line_description_are_read():
    # Altium writes DESCRIPTION before USERCOMMENTS, NOTE, LINENUMBER and the parameters:
    # a line break in it must not swallow them.
    text = ("|RECORD=BOM|PRODUCTIONQUANTITY=1\r\n|RECORD=Catalog|DBLINKFILEPATH=\r\n"
            "|RECORD=CatalogItem|ITEMTYPE=Automatic-NonVault|DESIGNITEMID=RES-10K"
            "|DESCRIPTION=Resistor\r\n10k 1%\r\n\r\nthick film|USERCOMMENTS=10k|NOTE="
            "|LINENUMBER=7|SELECTEDPARAMETERSHASH=|COMPONENTPARAMETERS=Value=10k"
            "|CUSTOMPARAMETERS=\r\n"
            + record("PartChoice", MANUFACTURER="Acme", MANUFACTURERPARTNO="ACME-1") + "\r\n\r\n")
    doc = bd.parse_bom_doc_bytes(text.encode())
    (line,) = doc.items
    assert line.description == "Resistor\n10k 1%\n\nthick film"
    assert (line.user_comments, line.line_number) == ("10k", 7)
    assert line.component_parameters == {"Value": "10k"}
    assert [choice.mpn for choice in line.part_choices] == ["ACME-1"]
    assert doc.continuation_lines == 2


def test_numbers_that_are_not_finite_are_missing():
    # Delphi writes NAN for a price it has not got: never a NaN in the JSON.
    data = bom_doc(item("RES-ITEM", 1, parameters="Value=10k", choices=[
        choice(breaks=(), BESTUNITPRICE="NAN", updated=None, LASTUPDATEDAT="NAN"),
        choice(supplier="ChipMart", sku="CM-1", breaks=((1, "INF"), (10, 0.2)))]))
    (first, second) = bd.parse_bom_doc_bytes(data).items[0].part_choices
    assert first.best_unit_price is None and first.last_updated is None
    assert [(b.quantity, b.price) for b in second.price_breaks] == [(10, 0.2)]
    assert bd.to_float("nan") is None and bd.to_int("NaN") is None
    model = model_of(data)
    page = ab.page(model, ["lines", "solutions"], 0, 50, 4)
    json.loads(json.dumps(page), parse_constant=lambda name: pytest.fail(name))


def test_price_break_fields_and_unknown_numbered_keys():
    data = bom_doc(item("A", 1, choices=[choice(
        breaks=((1, 0.5), (10, 0.4)), L1="CT", L2="CT", XY1="kept", R7="lone")]))
    pc = bd.parse_bom_doc_bytes(data).items[0].part_choices[0]
    assert [b.attributes for b in pc.price_breaks] == [{"L": "CT", "XY": "kept"}, {"L": "CT"}]
    assert pc.extra == {"R7": "lone"}  # no P7/Q7: not a price break, kept as written


def test_encodings_and_utf8_keys():
    ansi = (record("BOM", PRODUCTIONQUANTITY=1) + "\r\n"
            + record("CatalogItem", DESIGNITEMID="C-1", DESCRIPTION="1µF ±10%") + "\r\n")
    doc = bd.parse_bom_doc_bytes(ansi.encode("cp1252"))
    assert doc.items[0].description == "1µF ±10%" and doc.encoding == "cp1252"
    utf8 = b"\xef\xbb\xbf" + ansi.encode("utf-8")
    doc = bd.parse_bom_doc_bytes(utf8)
    assert doc.items[0].description == "1µF ±10%" and doc.encoding == "utf-8"
    mixed = ansi.encode("cp1252") + record("CatalogItem", DESIGNITEMID="Ω-2").encode("utf-8")
    assert bd.parse_bom_doc_bytes(mixed).encoding == "mixed"
    keyed = (record("BOM") + "\r\n|RECORD=CatalogItem|DESIGNITEMID=?|%UTF8%DESIGNITEMID="
             + "Ω-3").encode("utf-8")
    assert bd.parse_bom_doc_bytes(keyed).items[0].design_item_id == "Ω-3"
    aliases = (record("BOM") + "\r\n" + record("CatalogItem", DESIGNITEMID="A") + "\r\n"
               + record("PartChoice", MANUFACTURER="Acme",
                        MANUFACTURERALIASES="Acme Corp¦ACME Inc")).encode("cp1252")
    assert bd.parse_bom_doc_bytes(aliases).items[0].part_choices[0].aliases == [
        "Acme Corp", "ACME Inc"]


def test_odd_values_read_leniently():
    doc = bd.parse_bom_doc_bytes(bom_doc(item("A", "x", choices=[
        choice(stock=-1, updated=None, breaks=((10, 0.5), (1, 0.9))),
        record("PartChoice", MANUFACTURER="Acme", RANK="", LASTUPDATEDAT="0",
               BESTUNITPRICE="0,25", P1="abc", Q1="5")])))
    line = doc.items[0]
    assert line.line_number is None and line.line_number_text == "x"
    first, second = line.part_choices
    assert first.stock is None and first.last_updated is None  # -1: no stock figure
    assert [b.quantity for b in first.price_breaks] == [1, 10]  # sorted by quantity
    assert second.rank is None and second.last_updated is None
    assert second.best_unit_price == 0.25 and second.price_breaks == []


def test_not_a_bom_doc_is_refused(tmp_path):
    with pytest.raises(bd.BomDocError):
        bd.parse_bom_doc_bytes(b"")
    with pytest.raises(bd.BomDocError):
        bd.parse_bom_doc_bytes(b"[Design]\r\nVersion=1\r\n")
    path = tmp_path / "Bad.BomDoc"
    path.write_bytes(b"\x00\x01binary")
    with pytest.raises(bd.BomDocError):
        bd.parse_bom_doc(path)


def test_comma_text_and_option_lists():
    assert bd.comma_text('A=1,"B=two words","C=say ""hi""",D=') == {
        "A": "1", "B": "two words", "C": 'say "hi"', "D": ""}
    assert bd.split_list("Line #=68!Comment=120!") == [("Line #", "68"), ("Comment", "120")]
    assert bd.ole_date("45000") == datetime(2023, 3, 15)
    assert bd.ole_date("") is None and bd.ole_date("-3") is None


def test_a_large_bom_doc_parses_well_under_a_second():
    items = [item(f"ITEM-{n}", n, description="Chip resistor " * 8, parameters="Comment=1k",
                  choices=[choice(sku=f"SKU-{n}-{k}", breaks=[(q, 0.01) for q in range(1, 13)])
                           for k in range(12)])
             for n in range(300)]
    data = bom_doc(*items)
    assert len(data) > 2_300_000
    start = time.perf_counter()
    doc = bd.parse_bom_doc_bytes(data)
    assert time.perf_counter() - start < 1.0
    assert sum(len(line.part_choices) for line in doc.items) == 300 * 12


# ---------------------------------------------------------------- the join

def test_lines_join_parts_by_design_item_id_and_parameters():
    model = model_of()
    assert [part.designator for part in by_number(model, 10).members] == ["R1", "R2"]
    assert [part.designator for part in by_number(model, 20).members] == ["R3"]
    assert by_number(model, 10).basis == "component_parameters"
    assert [part.designator for part in by_number(model, 30).members] == ["C1", "C2"]
    assert by_number(model, 30).basis == "design_item_id"
    regulator = by_number(model, 40)
    assert [p.designator for p in regulator.members] == ["U1"]
    assert regulator.basis == "other_revision"
    assert [line.item.line_number for line in model.orphans] == [50]
    assert [line.item.line_number for line in model.non_pcb] == [60]
    # J1 has no line; MH1 is Standard (No BOM), not a BOM part.
    assert [part.designator for part in model.parts_without_line] == ["J1"]


def test_a_tie_between_split_lines_is_flagged():
    data = bom_doc(item("RES-ITEM", 1, parameters="Value=47k"),
                   item("RES-ITEM", 2, parameters="Value=56k"))
    model = model_of(data)
    first = by_number(model, 1)
    assert first.ambiguous == ["R1", "R2", "R3"]
    assert ab.join_report(model)["ambiguous"][0]["line"] == 1


def test_the_variant_filters_parts_and_its_variations_apply():
    netlist = design_netlist()
    variant = VariantDefinition(
        name="Lite", variations=[Variation(designator="R2", unique_id="", kind=1)],
        param_variations={"C1": {"manufacturer part number 1": (
            "Manufacturer Part Number 1", "INI-C100N-X")}})
    model = model_of(netlist=netlist, variant=variant)
    resistors = by_number(model, 10)
    assert [p.designator for p in resistors.fitted] == ["R1"]
    assert [p.designator for p in resistors.not_fitted] == ["R2"]
    assert resistors.quantity == 1
    row = ab.line_row(resistors, model, False, 0)
    assert row["designators"] == ["R1"] and row["not_fitted"] == ["R2"]
    # C1 varied to the BomDoc's chosen part, C2 still differs from it.
    (differs,) = [entry for entry in ab.mpn_differences(model) if entry["line"] == 30]
    assert differs["designators"] == ["C2"] and differs["alternate"] is True


def test_an_alternate_part_joins_by_its_own_design_item_id():
    netlist = design_netlist()
    alternate = Variation(designator="J1", unique_id="", kind=2, alternate_part="HDR-ALT",
                          alt={"DesignItemID": "HDR-ALT-ITEM", "IsSameFootprint": "True"})
    variant = VariantDefinition(name="Alt", variations=[alternate])
    data = design_doc() + ("\r\n".join(item("HDR-ALT-ITEM", 70, description="Alt header"))
                           + "\r\n").encode()
    model = model_of(data, netlist, variant)
    assert [p.designator for p in by_number(model, 70).members] == ["J1"]
    assert model.parts_without_line == []


# ---------------------------------------------------------------- solutions and prices

def test_solutions_are_ranked_and_pinned_suppliers_lead():
    model = model_of()
    caps = by_number(model, 30)
    assert [(s.manufacturer, s.rank, s.ranked_by_designer) for s in caps.solutions] == [
        ("Initech", 5, True), ("Globex", 3, False)]
    data = bom_doc(item("CAP-ITEM", 1, choices=[
        choice("Globex", "GLOBEX-C100N", supplier="First"),
        choice("Globex", "GLOBEX-C100N", supplier="Pinned", PINNED="True", PRIORITY=1)]))
    (solution,) = by_number(model_of(data), 1).solutions
    assert [view.choice.supplier for view in solution.choices] == ["Pinned", "First"]


def test_a_line_without_part_choices_takes_its_parts_mpn():
    data = bom_doc(item("RES-ITEM", 1, parameters="Value=10k"),
                   item("RES-ITEM", 2, parameters="Value=22k"))
    model = model_of(data)
    (solution,) = by_number(model, 1).solutions
    assert (solution.mpn, solution.manufacturer, solution.source) == (
        "ACME-R10K-01", "Acme", "component_parameters")
    assert ab.covered_designators(model) == set()


@pytest.mark.parametrize("need, expected", [
    (1, (0.10, 1, False)), (150, (0.05, 100, False)), (5000, (0.02, 1000, False)),
])
def test_price_at_the_production_need(need, expected):
    pc = bd.parse_bom_doc_bytes(bom_doc(item("A", 1, choices=[choice()]))).items[0] \
        .part_choices[0]
    assert ab.price_at(pc, need) == expected


def test_below_the_first_break_and_without_breaks():
    pc = bd.parse_bom_doc_bytes(bom_doc(item("A", 1, choices=[
        choice(breaks=((10, 0.4), (100, 0.3))),
        choice(breaks=(), BESTUNITPRICE="0.7")]))).items[0].part_choices
    assert ab.price_at(pc[0], 3) == (0.4, 10, True)
    assert ab.price_at(pc[1], 3) == (0.7, None, False)


def test_merged_price_tables_give_the_lowest_usable_price():
    # Two suppliers' tables back to back in one part choice (Q restarts at 1).
    merged, repeated = bd.parse_bom_doc_bytes(bom_doc(item("A", 1, choices=[
        choice(breaks=((1, 0.9), (2500, 0.0072), (10000, 0.0057), (1, 1.0), (3000, 0.0074))),
        choice(breaks=((1152, 3.1148), (1152, 3.1093)))]))).items[0].part_choices
    assert ab.price_at(merged, 3000) == (0.0072, 2500, False)
    assert ab.price_at(merged, 20000) == (0.0057, 10000, False)
    assert ab.price_at(repeated, 1152) == (3.1093, 1152, False)  # not the file's order
    assert ab.price_at(repeated, 10) == (3.1093, 1152, True)


def test_best_price_needs_an_orderable_offer():
    data = bom_doc(item("RES-ITEM", 1, parameters="Value=10k", choices=[
        choice(supplier="Bulk", sku="B-1", breaks=((5000, 0.01),)),
        choice(supplier="Retail", sku="R-1", breaks=((1, 0.10),))]))
    model = model_of(data)  # R1-R3: a need of 3
    row = ab.line_row(by_number(model, 1), model, True, 4)
    assert (row["chosen"]["best_unit_price"], row["chosen"]["best_price_supplier"]) == (
        0.1, "Retail")
    bulk, retail = row["ranked_solutions"][0]["part_choices"]
    # Under the first break the minimum order is bought.
    assert bulk["below_minimum_order"] and bulk["extended_price"] == 50
    assert retail["extended_price"] == 0.3
    only_bulk = model_of(bom_doc(item("RES-ITEM", 1, parameters="Value=10k", choices=[
        choice(supplier="Bulk", sku="B-1", breaks=((5000, 0.01),))])))
    chosen = ab.line_row(by_number(only_bulk, 1), only_bulk, False, 0)["chosen"]
    assert chosen["best_price_below_minimum_order"] and chosen["minimum_order_quantity"] == 5000


def test_a_part_choice_without_a_supplier_holds_no_stock():
    data = bom_doc(item("RES-ITEM", 1, parameters="Value=10k", choices=[
        choice(supplier="", sku="", stock=0, breaks=(), updated=None)]))
    model = model_of(data, variant=None)
    line = by_number(model, 1)
    assert line.solutions[0].choices[0].choice.stock is None
    row = ab.line_row(line, model, True, 4)
    assert row["chosen"]["suppliers"] == 0 and "stock_total" not in row["chosen"]
    assert row["ranked_solutions"][0]["part_choices"][0]["no_supplier"] is True
    assert findings_of(model, "out_of_stock_at_qty") == []


def test_stock_of_one_supplier_counts_once():
    data = bom_doc(item("CAP-ITEM", 1, choices=[
        choice("Globex", "G-1", supplier="Hub", sku="H-1-CT", stock=58),
        choice("Globex", "G-1", supplier="Hub", sku="H-1-DR", stock=58),
        choice("Globex", "G-1", supplier="Mart", sku="M-1", stock=10)]))
    model = model_of(data)
    chosen = ab.line_row(by_number(model, 1), model, False, 0)["chosen"]
    assert chosen["stock_total"] == 68 and chosen["suppliers"] == 2


def test_production_quantity_scales_the_need_and_stock():
    model = model_of(design_doc(production=250))
    resistors = by_number(model, 10)
    assert resistors.quantity == 2
    row = ab.line_row(resistors, model, True, 2)
    assert row["quantity_at_production"] == 500
    first = row["ranked_solutions"][0]["part_choices"]
    assert first[0]["enough_stock"] is True and first[0]["unit_price"] == 0.05
    assert first[0]["extended_price"] == 25 and first[0]["price_breaks_total"] == 3
    assert first[1]["enough_stock"] is False
    assert all("cache_date" in choice_row for choice_row in first)


def test_cache_dates_per_document_and_line():
    model = model_of()
    cache = ab.cache_summary(model)
    assert cache["oldest"] == ab.iso(NOW - timedelta(days=800))
    assert cache["newest"] == ab.iso(NOW - timedelta(days=30))
    assert cache["older_than_365_days"] == 1 and "cached" in cache["note"]
    row = ab.line_row(by_number(model, 20), model, False, 0)
    assert row["cache_dates"]["oldest"] == ab.iso(NOW - timedelta(days=800))
    assert row["chosen"]["cache_date"] == row["cache_dates"]["newest"]


# ---------------------------------------------------------------- the page

def test_the_page_has_header_lines_non_pcb_and_join():
    model = model_of()
    page = ab.page(model, list(ab.DEFAULT_SECTIONS), 0, 50, 4)
    header = page["header"]
    assert header["production_quantity"] == 1 and header["currency"] == "USD"
    assert header["target_unit_price"] == 12.5 and header["exclude_risky_solutions"] is True
    assert header["custom_columns"] == ["Assembly Note", "Approved"]
    assert header["solutions_per_item"] == 2 and header["variant_stored"] is False
    assert header["line_numbering"] == {"start": 10, "increment": 10}
    assert [row["line"] for row in page["lines"]] == [10, 20, 30, 40, 50]
    assert "ranked_solutions" not in page["lines"][0]
    assert page["lines"][2]["chosen"]["mpn"] == "INI-C100N-X"
    assert page["lines"][4]["designators"] == [] and page["lines"][4]["join"] == "no_parts"
    (board,) = page["non_pcb_lines"]
    assert (board["description"], board["quantity"]) == ("Bare board", None)
    join = page["join"]
    assert join["parts_without_line"]["designators"] == ["J1"]
    assert join["lines_without_parts"][0]["line"] == 50
    assert join["other_revision"][0]["design_item_id"] == "CMP-9001-00042-1"
    assert page["summary"]["lifecycle"]["Obsolete"] == 1
    assert ab.parse_sections("lines, join") == (["lines", "join"], None)
    assert ab.parse_sections(["bogus"])[0] is None


def test_find_line_by_number_item_or_designator():
    model = model_of()
    assert ab.find_line(model, "30").item.line_number == 30
    assert ab.find_line(model, "res-item").item.line_number == 10
    assert ab.find_line(model, "r3").item.line_number == 20
    assert ab.find_line(model, "nothing") is None


# ---------------------------------------------------------------- check_bom's checks

def findings_of(model, check=None):
    return [f for f in ab.bom_doc_findings(model) if check is None or f["check"] == check]


def test_lifecycle_risk_on_the_chosen_and_on_an_alternate():
    model = model_of()
    risks = {(f["line"], f["severity"]): f for f in findings_of(model, "lifecycle_risk")}
    assert set(risks) == {(30, "info"), (40, "warning")}
    assert risks[(40, "warning")]["lifecycle"] == "Obsolete"
    assert risks[(40, "warning")]["cache_date"].startswith("2026-05-02")
    assert "cached 2026-05-02" in risks[(40, "warning")]["issue"]


def test_out_of_stock_at_the_production_need():
    model = model_of(design_doc(production=600))
    stock = {f["line"]: f for f in findings_of(model, "out_of_stock_at_qty")}
    assert set(stock) == {10, 20, 40}  # line 30's chosen solution has 5000
    assert stock[10]["need"] == 1200 and stock[10]["stock"] == 500
    assert stock[20]["cache_date"] == ab.iso(NOW - timedelta(days=800))
    assert stock[10]["severity"] == "warning"


def test_single_source_no_solution_and_stale_cache():
    model = model_of()
    (single,) = findings_of(model, "single_source")
    assert single["severity"] == "info" and single["lines"] == [10, 20, 40, 60]
    (stale,) = findings_of(model, "stale_cache")
    assert stale["severity"] == "info" and stale["count"] == 1
    assert findings_of(model, "no_solution") == []
    data = bom_doc(item("RES-ITEM", 1, parameters="Value=10k", choices=[choice()]),
                   item("HDR-ITEM", 2), item("CAP-ITEM", 3, choices=[choice()]))
    netlist = design_netlist()
    netlist["components"]["J1"]["params"] = {"comment": "HDR"}
    (missing,) = findings_of(model_of(data, netlist), "no_solution")
    assert missing["severity"] == "warning" and missing["designators"] == ["J1"]
    sparse = bom_doc(item("RES-ITEM", 1), item("HDR-ITEM", 2), item("CAP-ITEM", 3),
                     item("CMP-9001-00042-2", 4, choices=[choice()]))
    (few,) = findings_of(model_of(sparse, netlist), "no_solution")
    assert few["severity"] == "info" and few["count"] == 1 and few["designators"] == ["J1"]


def test_bomdoc_out_of_sync_reports_the_join():
    model = model_of()
    by_reason = {f["reason"]: f for f in findings_of(model, "bomdoc_out_of_sync")}
    assert by_reason["parts_without_line"]["designators"] == ["J1"]
    assert by_reason["parts_without_line"]["severity"] == "info"
    assert by_reason["lines_without_parts"]["lines"] == [50]
    differs = by_reason["mpn_differs"]
    assert differs["severity"] == "warning" and differs["line"] == 30
    assert differs["mpn"] == "INI-C100N-X" and differs["part_mpn"] == "GLOBEX-C100N"


def test_other_revision_lines_and_orphans_say_why():
    model = model_of()
    by_reason = {f["reason"]: f for f in findings_of(model, "bomdoc_out_of_sync")}
    revision = by_reason["other_revision"]
    assert revision["severity"] == "info" and revision["designators"] == ["U1"]
    assert revision["design_item_ids"] == ["CMP-9001-00042-1"] and revision["lines"] == [40]
    assert by_reason["lines_without_parts"]["why"] == "no_part_has_id"
    netlist = design_netlist()
    netlist["components"]["MH2"] = component("OLD-ITEM", "MH", "MH3", kind=5)  # no BOM
    split = bom_doc(item("RES-ITEM", 1, parameters="Value=10k"),
                    item("RES-ITEM", 2, parameters="Value=22k"),
                    item("RES-ITEM", 3, parameters="Value=99k"),
                    item("OLD-ITEM", 4), item("GONE-ITEM", 5))
    model = model_of(split, netlist)
    why = {line.item.line_number: line.orphan_reason for line in model.orphans}
    assert why == {3: "split_line_unused", 4: "only_on_parts_not_in_bom",
                   5: "no_part_has_id"}
    reasons = {f["why"]: f for f in findings_of(model, "bomdoc_out_of_sync")
               if f["reason"] == "lines_without_parts"}
    assert set(reasons) == set(why.values()) and reasons["split_line_unused"]["lines"] == [3]
    report = ab.join_report(model)
    assert {entry["why"] for entry in report["lines_without_parts"]} == set(why.values())
    row = ab.line_row(by_number(model, 3), model, False, 0)
    assert row["why_no_parts"] == ab.ORPHAN_REASONS["split_line_unused"]


def test_only_a_short_suffix_is_a_revision():
    assert ab.revision_base("cmp-9001-00042-2") == "cmp-9001-00042"
    assert ab.revision_base("part-0001-00045") is None
    assert ab.revision_base("res-10k") is None
    data = bom_doc(item("PART-0001-00045", 1, item_type="Automatic-Vault", choices=[choice()]))
    netlist = design_netlist()
    netlist["components"]["J1"]["design_item_id"] = "PART-0001-00046"
    model = model_of(data, netlist)
    assert model.lines == [] and "J1" in [p.designator for p in model.parts_without_line]


def test_unnumbered_lines_are_named_by_design_item_id():
    data = bom_doc(item("RES-ITEM", None, parameters="Value=10k",
                        choices=[choice(lifecycle="NRND")]))
    model = model_of(data)
    (risk,) = findings_of(model, "lifecycle_risk")
    assert risk["issue"].startswith("Unnumbered line RES-ITEM:") and "line" not in risk
    assert risk["design_item_id"] == "RES-ITEM"
    (single,) = findings_of(model, "single_source")
    assert "lines" not in single and single["unnumbered_lines"] == ["RES-ITEM"]
    assert ab.find_line(model, "RES-ITEM") is by_number(model, None)


def test_the_lifecycle_risk_is_dated_by_the_choice_that_reports_it():
    data = bom_doc(item("RES-ITEM", 1, parameters="Value=10k", choices=[
        choice(supplier="PartsHub", updated=NOW - timedelta(days=5)),
        choice(supplier="ChipMart", sku="CM-1", lifecycle="Obsolete",
               updated=NOW - timedelta(days=900))]))
    (risk,) = findings_of(model_of(data), "lifecycle_risk")
    assert risk["cache_date"] == ab.iso(NOW - timedelta(days=900))
    assert risk["other_lifecycles"] == ["Production"] and "other suppliers" in risk["issue"]


def test_duplicate_line_numbers_are_reported():
    data = bom_doc(item("RES-ITEM", 5, parameters="Value=10k", choices=[choice()]),
                   item("CAP-ITEM", 5, choices=[choice("Globex", "GLOBEX-C100N")]))
    model = model_of(data)
    report = ab.join_report(model)
    assert report["duplicate_line_numbers"] == [
        {"line": 5, "design_item_ids": ["RES-ITEM", "CAP-ITEM"], "refs": ["#1", "#2"]}]
    assert ab.summary(model)["duplicate_line_numbers"] == 1
    assert len(ab.find_lines(model, "5")) == 2
    assert ab.find_lines(model, "C1")[0].item.design_item_id == "CAP-ITEM"
    (second,) = ab.find_lines(model, "#2")
    assert second.item.design_item_id == "CAP-ITEM" and second.title == "Line 5 (#2 in the file)"
    assert ab.line_row(second, model, False, 0)["ref"] == "#2"
    # Unnumbered lines of one split item are told apart the same way.
    split = model_of(bom_doc(item("RES-ITEM", None, parameters="Value=10k", choices=[choice()]),
                             item("RES-ITEM", None, parameters="Value=22k", choices=[choice()])))
    assert [line.label for line in split.all_lines] == ["#1", "#2"]
    assert ab.find_lines(split, "#2")[0].members[0].designator == "R3"
    # An unnumbered line whose Design Item ID a numbered line shares gets a ref too.
    mixed = model_of(bom_doc(item("RES-ITEM", 4, parameters="Value=10k", choices=[choice()]),
                             item("RES-ITEM", None, parameters="Value=22k", choices=[choice()])))
    assert [line.label for line in mixed.all_lines] == ["4", "#2"]
    for line in mixed.all_lines:
        assert ab.find_lines(mixed, line.label) == [line]
    assert all(entry["line_numbers_duplicated"]
               for entry in ab.expected_columns(model).values())


def test_out_of_stock_without_a_cache_date_is_info():
    data = bom_doc(item("RES-ITEM", 1, parameters="Value=10k", choices=[
        choice(stock=0, updated=None)]))
    (stock,) = findings_of(model_of(data), "out_of_stock_at_qty")
    assert stock["severity"] == "info" and "no cache date" in stock["issue"]


def test_mpn_checks_cover_every_ranked_mpn():
    data = bom_doc(
        item("RES-ITEM", 1, parameters="Value=10k",
             choices=[choice(maker="", mpn="NOMAKER-1"), choice("Acme", "SHARED-1")]),
        item("RES-ITEM", 2, parameters="Value=22k", choices=[choice("Acme", "SHARED-1")]))
    model = model_of(data)
    (no_maker,) = findings_of(model, "mpn_without_manufacturer")
    assert no_maker["mpn"] == "NOMAKER-1" and no_maker["source"] == "bom_doc"
    (shared,) = findings_of(model, "mpn_inconsistent")
    assert shared["field"] == "value" and shared["lines"] == [1, 2]
    assert shared["severity"] == "warning"


def test_run_bom_checks_adds_the_bom_doc_checks_only_with_a_bom_doc():
    netlist = design_netlist()
    parts, plan = parts_of(netlist)
    without = run_bom_checks(parts, netlist, plan)
    assert without["checks_run"] == list(CHECKS)
    model = model_of()
    with_doc = run_bom_checks(parts, netlist, plan, bom_doc_findings=ab.bom_doc_findings(model),
                              chosen_in_bom_doc=ab.covered_designators(model))
    assert with_doc["checks_run"] == list(CHECKS + BOMDOC_CHECKS)
    assert with_doc["summary"]["lifecycle_risk"] == 2
    narrowed = run_bom_checks(parts, netlist, plan, checks=["stale_cache"],
                              bom_doc_findings=ab.bom_doc_findings(model))
    assert narrowed["summary"] == {"stale_cache": 1}
    # Asked for without a BomDoc: not run, and said so (not "run, nothing found").
    alone = run_bom_checks(parts, netlist, plan, checks=["lifecycle_risk", "no_footprint"])
    assert "error" not in alone and alone["checks_run"] == ["no_footprint"]
    assert set(alone["checks_not_run"]) == {"lifecycle_risk"}


def test_a_ranked_mpn_inconsistency_is_not_reported_twice():
    netlist = design_netlist()
    netlist["components"]["R3"]["params"]["manufacturer part number 1"] = "ACME-R10K-01"
    data = bom_doc(item("RES-ITEM", 2, parameters="Value=22k", choices=[choice()]),
                   item("RES-ITEM", 1, parameters="Value=10k", choices=[choice()]))
    model = model_of(data, netlist)
    (from_doc,) = findings_of(model, "mpn_inconsistent")
    assert from_doc["lines"] == [1, 2]  # in line order
    parts, plan = parts_of(netlist)
    result = run_bom_checks(parts, netlist, plan, bom_doc_findings=ab.bom_doc_findings(model),
                            chosen_in_bom_doc=ab.covered_designators(model))
    values = [f for f in result["findings"]
              if f["check"] == "mpn_inconsistent" and f["field"] == "value"]
    assert len(values) == 1 and "source" not in values[0]
    assert values[0]["bom_doc_lines"] == [1, 2]


def test_parts_with_a_bom_doc_choice_count_as_having_an_mpn():
    netlist = design_netlist()
    for name in ("R1", "R2", "R3", "C1", "C2"):
        netlist["components"][name]["params"] = {"comment": "x"}
    parts, plan = parts_of(netlist)
    plain = run_bom_checks(parts, netlist, plan, checks=["missing_mpn"])
    assert "ActiveBOM, which is not read" in plain["findings"][0]["issue"]
    model = model_of(netlist=netlist)
    result = run_bom_checks(parts, netlist, plan, checks=["missing_mpn"],
                            bom_doc_findings=[], chosen_in_bom_doc=ab.covered_designators(model))
    assert {d for f in result["findings"] for d in f.get("designators", [])} == {"J1"}


# ---------------------------------------------------------------- locating the BomDoc

PRJ = "[Design]\nVersion=1.0\n[Document1]\nDocumentPath=Board.SchDoc\n"


def write_project(folder: Path, *bom_docs: str, unlisted=(), outjob=None) -> dict:
    folder.mkdir(parents=True, exist_ok=True)
    text = PRJ
    for index, name in enumerate(bom_docs, 2):
        text += f"[Document{index}]\nDocumentPath={name}\n"
        if not name.startswith("Missing"):
            (folder / name).write_bytes(design_doc())
    if outjob:
        text += f"[Document{len(bom_docs) + 2}]\nDocumentPath=Board.OutJob\n"
        (folder / "Board.OutJob").write_text(outjob, encoding="utf-8")
    for name in unlisted:
        (folder / name).write_bytes(design_doc())
    (folder / "Board.PrjPcb").write_text(text, encoding="utf-8")
    return {"name": "Board", "root_dir": str(folder), "prj_pcb_path": str(folder / "Board.PrjPcb"),
            "sheets": [], "pcb_doc_paths": [], "parameters": {}}


def test_the_project_bom_doc_is_found(tmp_path):
    project = write_project(tmp_path / "one", "Board.BomDoc", unlisted=["Spare.BomDoc"])
    choice_ = ab.locate_bom_docs(project)
    assert Path(choice_.chosen).name == "Board.BomDoc" and choice_.in_project
    assert choice_.describe()["not_in_project"] == ["Spare.BomDoc"]
    spare = ab.locate_bom_docs(project, name="Spare")
    assert Path(spare.chosen).name == "Spare.BomDoc" and not spare.in_project
    assert ab.locate_bom_docs(project, name="Nope.BomDoc").error["error"] == "bom_doc_not_found"


def test_several_bom_docs_prefer_the_one_the_outjob_uses(tmp_path):
    project = write_project(tmp_path / "two", "A.BomDoc", "B.BomDoc")
    first = ab.locate_bom_docs(project)
    assert Path(first.chosen).name == "A.BomDoc" and first.describe()["other_bom_docs"] == [
        "B.BomDoc"]
    by_outjob = ab.locate_bom_docs(project, outjob_documents=lambda: ["", "B.BomDoc"])
    assert Path(by_outjob.chosen).name == "B.BomDoc"
    assert "OutJob" in by_outjob.chosen_by


def test_no_bom_doc_says_why(tmp_path):
    none = ab.locate_bom_docs(write_project(tmp_path / "none"))
    assert none.error["error"] == "no_bom_doc"
    unlisted = ab.locate_bom_docs(write_project(tmp_path / "loose", unlisted=["Loose.BomDoc"]))
    assert "not in the project" in unlisted.error["message"]
    missing = ab.locate_bom_docs(write_project(tmp_path / "gone", "Missing.BomDoc"))
    assert "not on disk" in missing.error["message"]


# ---------------------------------------------------------------- the MCP tool

@pytest.fixture
def abom_project(monkeypatch, tmp_path):
    project = write_project(tmp_path / "Board", "Board.BomDoc")
    netlist = design_netlist()
    lite = VariantDefinition(name="Lite", variations=[
        Variation(designator="R2", unique_id="", kind=1)])
    monkeypatch.setattr(main_mod, "_project", project)
    monkeypatch.setattr(main_mod._altium, "_netlist", netlist)
    monkeypatch.setattr(main_mod, "_variant_state", VariantState([lite]))
    return Path(project["root_dir"])


def test_get_active_bom_tool(abom_project):
    result = json.loads(main_mod.get_active_bom())
    assert result["variant"] == "Lite" and result["bom_doc"]["file"] == "Board.BomDoc"
    assert result["bom_doc"]["chosen_by"] == "the project's only BomDoc"
    assert result["summary"]["lines"] == 6 and result["lines_total"] == 5
    resistors = result["lines"][0]
    assert resistors["designators"] == ["R1"] and resistors["not_fitted"] == ["R2"]
    assert result["cache"]["oldest"] and "cached" in result["cache"]["note"]
    base = json.loads(main_mod.get_active_bom(variant="[No Variations]"))
    assert base["lines"][0]["designators"] == ["R1", "R2"]
    one = json.loads(main_mod.get_active_bom(line="C2"))
    assert one["line"]["line"] == 30 and len(one["line"]["ranked_solutions"]) == 2
    solutions = json.loads(main_mod.get_active_bom(sections=["solutions"]))
    assert "header" not in solutions and "ranked_solutions" in solutions["lines"][0]


def test_get_active_bom_for_another_number_of_boards(abom_project):
    plain = json.loads(main_mod.get_active_bom(sections=["solutions"]))
    assert (plain["production_quantity_used"], plain["production_quantity_from"]) == (
        1, "bom_doc")
    big = json.loads(main_mod.get_active_bom(sections=["solutions"], production_quantity=600))
    assert (big["production_quantity_used"], big["production_quantity_from"]) == (
        600, "argument")
    resistors = big["lines"][0]  # line 10 in Lite: R1 only
    assert resistors["line"] == 10 and resistors["quantity_at_production"] == 600
    first = resistors["ranked_solutions"][0]["part_choices"][0]
    assert first["enough_stock"] is False and first["unit_price"] == 0.05
    assert json.loads(main_mod.get_active_bom(production_quantity=0))["error"] == \
        "invalid_production_quantity"


def test_get_active_bom_pages_and_shrinks(abom_project):
    paged = json.loads(main_mod.get_active_bom(offset=4))
    assert [row["line"] for row in paged["lines"]] == [50] and paged["has_more"] is False
    with patch.object(main_mod, "QUERY_NET_MAX_RESULT_SIZE_CHARS", 9000):
        small = json.loads(main_mod.get_active_bom(sections=["lines", "solutions"]))
    assert small["lines_limited_to"] < 50 and small["next_offset"] == small["lines_limited_to"]
    with patch.object(main_mod, "QUERY_NET_MAX_RESULT_SIZE_CHARS", 1500):
        tiny = json.loads(main_mod.get_active_bom())
    assert "lines" not in tiny and "lines_omitted" in tiny


def test_get_active_bom_errors(abom_project, tmp_path):
    assert json.loads(main_mod.get_active_bom(sections=["bogus"]))["error"] == \
        "invalid_sections"
    assert json.loads(main_mod.get_active_bom(line="99"))["error"] == "line_not_found"
    assert json.loads(main_mod.get_active_bom(variant="Nope"))["error"] == "variant_not_found"
    assert json.loads(main_mod.get_active_bom(name="Other"))["error"] == "bom_doc_not_found"
    (abom_project / "Board.BomDoc").write_bytes(b"garbage")
    unreadable = json.loads(main_mod.get_active_bom())
    assert unreadable["error"] == "bom_doc_unreadable" and "BomDocError" in unreadable["message"]
    main_mod._project["prj_pcb_path"] = str(write_project(tmp_path / "bare")["prj_pcb_path"])
    assert json.loads(main_mod.get_active_bom())["error"] == "no_bom_doc"
    with patch.object(main_mod, "_require_project",
                      side_effect=main_mod.ProjectNotReady(main_mod._netlist_required_error())):
        assert json.loads(main_mod.get_active_bom())["error"] == "netlist_required"


def test_get_active_bom_line_that_names_several_lines(abom_project):
    (abom_project / "Board.BomDoc").write_bytes(bom_doc(
        item("RES-ITEM", 5, parameters="Value=10k", choices=[choice()]),
        item("CAP-ITEM", 5, choices=[choice("Globex", "GLOBEX-C100N")])))
    ambiguous = json.loads(main_mod.get_active_bom(line="5"))
    assert ambiguous["error"] == "line_ambiguous" and len(ambiguous["lines"]) == 2
    assert [entry["ref"] for entry in ambiguous["lines"]] == ["#1", "#2"]
    one = json.loads(main_mod.get_active_bom(line="C1"))
    assert one["line"]["design_item_id"] == "CAP-ITEM"
    by_ref = json.loads(main_mod.get_active_bom(line="#2"))
    assert by_ref["line"]["design_item_id"] == "CAP-ITEM" and by_ref["line"]["ref"] == "#2"


def test_a_bom_doc_that_breaks_the_model_leaves_the_bom_tools_working(abom_project):
    service = sys.modules[main_mod.open_active_bom.__module__]  # the copy main uses
    with patch.object(service, "build_active_bom", side_effect=RuntimeError("boom")):
        result = json.loads(main_mod.check_bom())
        assert result["bom_doc_error"]["error"] == "bom_doc_unreadable"
        assert "lifecycle_risk" not in result["checks_run"]
        assert json.loads(main_mod.get_bom(active_bom=True))["bom_doc_error"]
        assert json.loads(main_mod.get_active_bom())["error"] == "bom_doc_unreadable"
    with patch.object(main_mod, "active_bom_findings", side_effect=ValueError("bad")):
        result = json.loads(main_mod.check_bom())
        assert result["bom_doc_error"]["error"] == "bom_doc_unreadable"
        assert "bom_doc" not in result and "lifecycle_risk" not in result["checks_run"]


def test_review_checklist_counts_a_line_without_a_solution_as_missing(abom_project):
    (abom_project / "Board.BomDoc").write_bytes(bom_doc(
        item("RES-ITEM", 1, parameters="Value=10k", choices=[choice()]),
        item("RES-ITEM", 2, parameters="Value=22k", choices=[choice(mpn="ACME-R22K-01")]),
        item("CAP-ITEM", 3, choices=[choice("Globex", "GLOBEX-C100N")]),
        item("CMP-9001-00042-2", 4, choices=[choice("Umbrella", "UMB-REG-5")]),
        item("HDR-ITEM", 5)))
    for name in ("C1", "C2"):  # only J1 lacks a part number now
        main_mod._altium._netlist["components"][name]["params"]["manufacturer 1"] = "Globex"
    checks = json.loads(main_mod.check_bom())
    assert "missing_mpn" not in checks["summary"] and checks["summary"]["no_solution"] == 1
    assert "mpn_without_manufacturer" not in checks["summary"]
    result = json.loads(main_mod.run_review_checklist(checklist="bom"))
    item_01 = next(entry for entry in result["items"] if entry["id"] == "BOM-DAT-01")
    assert item_01["status"] == "no", item_01


def test_check_bom_tool_with_and_without_a_bom_doc(abom_project, tmp_path):
    result = json.loads(main_mod.check_bom())
    assert result["bom_doc"]["file"] == "Board.BomDoc" and result["bom_doc"]["cache"]["oldest"]
    assert set(BOMDOC_CHECKS) <= set(result["checks_run"])
    assert result["summary"]["lifecycle_risk"] == 2
    main_mod._project["prj_pcb_path"] = str(write_project(tmp_path / "bare")["prj_pcb_path"])
    plain = json.loads(main_mod.check_bom())
    assert plain["checks_run"] == list(CHECKS) and "bom_doc" not in plain
    assert "bom_doc_error" not in plain


def test_get_bom_adds_bom_doc_columns_only_when_asked(abom_project):
    plain = json.loads(main_mod.get_bom())
    assert "bom_doc" not in plain and all("bom_doc_lines" not in l for l in plain["lines"])
    rich = json.loads(main_mod.get_bom(active_bom=True))
    assert rich["bom_doc"]["file"] == "Board.BomDoc"
    lines = {line["designators"][0]: line for line in rich["lines"]}
    assert lines["C1"]["bom_doc_lines"] == [30]
    assert lines["C1"]["bom_doc_chosen"][0]["mpn"] == "INI-C100N-X"
    assert lines["J1"]["bom_doc_lines"] == [] and "bom_doc_mpns" not in lines["C1"]
    csv_text = json.loads(main_mod.get_bom(active_bom=True, format="csv"))["csv"]
    header, *rows = csv_text.splitlines()
    assert header.endswith("BomDoc Line #,BomDoc MPN")
    assert any(row.endswith(",30,INI-C100N-X") for row in rows)


def test_package_for_xfn_writes_the_bom_tables(abom_project):
    with patch.object(main_mod, "mark_xfn_exported"):
        result = main_mod._package_for_xfn_impl(
            main_mod._project, main_mod._altium._netlist, main_mod._variant_state, "0.0.1")
    assert "ActiveBOM:" in result
    db = abom_project / "Board-pcb-copilot.db"
    with closing(sqlite3.connect(db)) as conn:
        rows = conn.execute("SELECT line, position, manufacturer, mpn, supplier, lifecycle, "
                            "cache_date, designators FROM bom_solutions ORDER BY id").fetchall()
        non_pcb = conn.execute("SELECT line, description FROM bom_non_pcb_lines").fetchall()
        tables = {name for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"components", "nets", "mech_parts"} <= tables
    caps = [row for row in rows if row[0] == 30]
    assert [(row[1], row[2]) for row in caps] == [(1, "Initech"), (2, "Globex")]
    assert caps[1][5] == "NRND" and caps[0][6].startswith("2026-05-02")
    assert json.loads(rows[0][7]) == ["R1"]
    assert non_pcb == [(60, "Bare board")]
    with closing(sqlite3.connect(db)) as conn:
        priced = conn.execute(
            "SELECT unit_price, price_break_quantity, below_minimum_order, extended_price "
            "FROM bom_solutions WHERE line = 10 ORDER BY id").fetchone()
    assert priced == (0.1, 1.0, 0, 0.1)


# ---------------------------------------------------------------- verify_outputs

PUBLISH = "ReleaseManaged3=1\nOutputPathMedia3=[Media Name]\nOutputPathOutputer3=[Output Type]\n"
HEADER = ["Line #", "Comment", "Designator", "Quantity", "Manufacturer 1",
          "Manufacturer Part Number 1", "Supplier 1", "Supplier Part Number 1"]


def bom_file(rows) -> bytes:
    lines = [HEADER] + [list(row) for row in rows]
    return ("\r\n".join(",".join(f'"{cell}"' for cell in row) for row in lines) + "\r\n").encode()


GOOD_ROWS = [
    ["10", "10k", "R1, R2", "2", "Acme", "ACME-R10K-01", "PartsHub", "PH-0001"],
    ["20", "22k", "R3", "1", "Acme", "ACME-R22K-01", "PartsHub", "PH-0002"],
    ["30", "100n", "C1, C2", "2", "Initech", "INI-C100N-X", "PartsHub", "PH-0101"],
    ["40", "REG", "U1", "1", "Umbrella", "UMB-REG-5", "PartsHub", "PH-0200"],
    ["", "HDR", "J1", "1", "", "", "", ""],
]


def verify_project(tmp_path, rows, data: bytes | None = None):
    outjob = _outjob([_output("BOM_PartType", "Bill of Materials", category="Report",
                              doc="Board.BomDoc")], publish=PUBLISH)
    project = write_project(tmp_path / "Verify", "Board.BomDoc", outjob=outjob)
    if data is not None:
        (Path(project["root_dir"]) / "Board.BomDoc").write_bytes(data)
    folder = Path(project["root_dir"]) / "Project Outputs for Board" / "Folder" / "BOM"
    folder.mkdir(parents=True)
    (folder / "BOM.csv").write_bytes(bom_file(rows))
    netlist = design_netlist()
    for name in ("C1", "C2"):  # the BomDoc's chosen part, so only the file is checked
        netlist["components"][name]["params"]["manufacturer part number 1"] = "INI-C100N-X"
    return vo.run_verify_outputs(project, None, netlist, VariantState([]))


def codes(model):
    return {(f["check"], f["code"]): f for f in model["findings"]}


def test_an_activebom_bom_matching_its_bom_doc(tmp_path):
    model = verify_project(tmp_path, GOOD_ROWS)
    bom = [row for row in model["files"] if row["check"] == "bom"][0]
    assert bom["bom_doc"] == "Board.BomDoc" and "part_choices_not_read" not in bom
    assert model["verdict"] == "consistent", model["findings"]


def test_an_activebom_bom_differing_from_its_bom_doc(tmp_path):
    rows = [list(row) for row in GOOD_ROWS]
    rows[0][5] = "ACME-OTHER"            # MPN
    rows[1][0] = "21"                    # line number
    rows[2][6] = "Elsewhere"             # supplier
    rows[3][7] = "XX-1"                  # supplier part number
    rows[3][4] = "Umbrella Corp"         # manufacturer spelling: info
    model = verify_project(tmp_path, rows)
    found = codes(model)
    assert found[("bom", "field_mpn")]["severity"] == "warning"
    assert found[("bom", "line_number")]["examples"][0]["design"] == 20
    assert found[("bom", "field_supplier")]["examples"][0]["design"] == ["PartsHub"]
    assert ("bom", "field_supplier_part_number") in found
    assert found[("bom", "field_manufacturer")]["severity"] == "info"
    assert model["verdict"] == "mismatches"


def test_a_parameter_fallback_mpn_difference_is_info(tmp_path):
    rows = [list(row) for row in GOOD_ROWS]
    rows[4][5] = "HDR-OTHER-9"  # J1: no line, no part choice; ActiveBOM's fallback varies
    found = codes(verify_project(tmp_path, rows))
    assert found[("bom", "field_mpn_parameter")]["severity"] == "info"
    assert ("bom", "field_mpn") not in found


def test_line_numbers_of_a_bom_doc_that_repeats_them_are_info(tmp_path):
    data = bom_doc(
        item("RES-ITEM", 10, parameters="Comment=10k,Value=10k", choices=[choice()]),
        item("RES-ITEM", 20, parameters="Comment=22k,Value=22k",
             choices=[choice(mpn="ACME-R22K-01", sku="PH-0002")]),
        item("CAP-ITEM", 20, choices=[choice("Initech", "INI-C100N-X", sku="PH-0101")]),
        item("CMP-9001-00042-1", 40, item_type="Automatic-Vault",
             choices=[choice("Umbrella", "UMB-REG-5", sku="PH-0200")]))
    rows = [list(row) for row in GOOD_ROWS]
    rows[2][0] = "30"  # the export numbered its lines again
    found = codes(verify_project(tmp_path, rows, data))
    assert found[("bom", "line_number")]["severity"] == "info"
    assert "more than one line" in found[("bom", "line_number")]["message"]


# ---------------------------------------------------------------- documentation

def test_the_tool_is_documented_everywhere():
    root = Path(__file__).parents[1]
    manifest = json.loads((root / "manifest.json").read_text("utf-8"))
    readme = (root / "README.md").read_text("utf-8")
    changelog = (root / "CHANGELOG.md").read_text("utf-8")
    start = changelog.index("## [Unreleased]")
    end = changelog.index("\n## [", start + 1)
    assert "get_active_bom" in {entry["name"] for entry in manifest["tools"]}
    assert "| `get_active_bom` |" in readme
    assert main_mod.SERVER_INSTRUCTIONS.index("get_active_bom") > 4096
    assert "get_active_bom" in main_mod.NETLIST_TOOLS
    assert "get_active_bom" in changelog[start:end]


def test_no_solution_and_missing_mpn_do_not_repeat_each_other():
    netlist = design_netlist()
    data = bom_doc(
        item("RES-ITEM", 1, parameters="Value=10k", choices=[choice()]),
        item("RES-ITEM", 2, parameters="Value=22k", choices=[choice(mpn="ACME-R22K-01")]),
        item("CAP-ITEM", 3, choices=[choice("Globex", "GLOBEX-C100N")]),
        item("CMP-9001-00042-2", 4, choices=[choice("Umbrella", "UMB-REG-5")]),
        item("HDR-ITEM", 5))
    parts, plan = parts_of(netlist)
    model = model_of(data, netlist)
    result = run_bom_checks(parts, netlist, plan, bom_doc_findings=ab.bom_doc_findings(model),
                            chosen_in_bom_doc=ab.covered_designators(model))
    by_check = {}
    for finding in result["findings"]:
        by_check.setdefault(finding["check"], []).append(finding)
    assert [f["designators"] for f in by_check["no_solution"]] == [["J1"]]
    assert "missing_mpn" not in by_check  # U1 has a BomDoc part choice, J1 is named above
    # A BomDoc that keeps no supply data at all: missing_mpn alone speaks.
    bare = bom_doc(item("RES-ITEM", 1, parameters="Value=10k"), item("HDR-ITEM", 2))
    assert findings_of(model_of(bare, netlist), "no_solution") == []
