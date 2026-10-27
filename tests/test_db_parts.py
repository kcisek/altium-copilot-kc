"""Database libraries (roadmap #53): the .DbLib / .SVNDbLib / .DbLink parser
(parsers/db_lib.py), the read-only query layer (services/db_query.py, query_db.ps1), the
part-to-record links and the parameter drift (services/db_parts.py), and the tools
get_db_part and check_db_parameters.

Every library, database and part here is synthetic: generic tables of made-up resistors
and capacitors written in the layout Altium's database library editor uses. The query
layer is faked by a runner that evaluates each request's comparison tree on in-memory
tables (no database driver); the live tests at the end run query_db.ps1 against an
Access database they create, and skip where Windows PowerShell or the Access provider is
missing (CI)."""

import io
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

import server.main as main_mod
from server.parsers import db_lib as dl
from server.parsers.prj_pcb import VariantDefinition, VariantState, Variation
from server.parsers.sch_doc import components_from_stream
from server.services import db_parts as dp
from server.services.bom import plan_variant
from tests.test_sch_doc_parser import _stream

dq = dp.db_query  # the query module db_parts (and the server) use
SECRET = "Zq7-Secret-Pass-91x"
USER = "lib_reader_7"


# ---------------------------------------------------------------------------
# Synthetic libraries
# ---------------------------------------------------------------------------

def field_map(number: int, table: str, name: str, parameter: str, key: bool = False,
              add: int = 0, remove: int = 0, update: int = 0, visible: int = 0) -> str:
    """A [FieldMap<n>] section as Altium's editor writes it: an Options list."""
    return (f"[FieldMap{number}]\nOptions=|FieldName=[{table}].[{name}]|TableNameOnly={table}"
            f"|FieldNameOnly={name}|FieldType={0 if key else 1}|ParameterName={parameter}"
            f"|VisibleOnAdd={visible}|AddMode={add}|RemoveMode={remove}|UpdateMode={update}\n")


RESISTOR_FIELDS = (
    ("Part Number", "Part Number", True), ("Value", "Value", False),
    ("Tolerance", "Tolerance", False), ("Library Ref", "[Library Ref]", False),
    ("Footprint Ref", "[Footprint Ref]", False), ("Footprint Ref 2", "[Footprint Ref 2]", False),
    ("Manufacturer", "Manufacturer", False), ("MPN", "Manufacturer Part Number", False),
    ("Description", "[Description]", False), ("Notes", "[None]", False),
)
CAPACITOR_FIELDS = (
    ("Part Number", "Part Number", False), ("Value", "Value", False),
    ("Voltage", "Voltage", False), ("Library Ref", "[Library Ref]", False),
    ("Footprint Ref", "[Footprint Ref]", False), ("MPN", "Manufacturer Part Number", False),
)


def dblib_text(source: str, kind_lines: str = "", tables: str | None = None,
               maps: str | None = None, defaults=(3, 1, 2)) -> str:
    add, remove, update = defaults
    text = ("[OutputDatabaseLinkFile]\nVersion=1.1\n[DatabaseLinks]\n"
            f"ConnectionString={source}\n"
            f"AddMode={add}\nRemoveMode={remove}\nUpdateMode={update}\nViewMode=0\n"
            "LeftQuote=[\nRightQuote=]\nQuoteTableNames=1\nUseTableSchemaName=0\n"
            "DefaultColumnType=VARCHAR(255)\nLibraryDatabaseType=\nLibraryDatabasePath=\n"
            "DatabasePathRelative=0\nTopPanelCollapsed=0\n" + kind_lines)
    if tables is None:
        tables = ("[Table1]\nSchemaName=\nTableName=Resistors\nEnabled=True\nUserWhere=0\n"
                  "UserWhereText=\n"
                  "[Table2]\nSchemaName=\nTableName=Capacitors\nEnabled=True\nUserWhere=1\n"
                  "UserWhereText=[Part Number] = '{Part Number}'\n")
    if maps is None:
        maps, number = "", 1
        for name, parameter, key in RESISTOR_FIELDS:
            maps += field_map(number, "Resistors", name, parameter, key)
            number += 1
        for name, parameter, _ in CAPACITOR_FIELDS:
            maps += field_map(number, "Capacitors", name, parameter,
                              remove=2 if name == "Voltage" else 0)
            number += 1
    return text + tables + maps


def resistor(number: str, value: str, footprint: str, tolerance="1%", maker="Acme",
             mpn=None, description=None, footprint2=None, notes=None) -> dict:
    return {"Part Number": number, "Value": value, "Tolerance": tolerance,
            "Library Ref": "RES", "Footprint Ref": footprint, "Footprint Ref 2": footprint2,
            "Manufacturer": maker, "MPN": mpn or f"AC-{number}",
            "Description": description or f"Resistor {value}", "Notes": notes}


def capacitor(number: str, value: str, voltage, footprint: str) -> dict:
    return {"Part Number": number, "Value": value, "Voltage": voltage, "Library Ref": "CAP",
            "Footprint Ref": footprint, "MPN": f"AC-{number}"}


TABLES = {
    "Resistors": [
        resistor("RES-0001", "10k", "R0603"),
        resistor("RES-0002", "10k", "R0603", maker="Other"),
        resistor("RES-0003", "10k", "R0603", tolerance="5%", maker="Third"),
        resistor("RES-0004", "4k7", "R0402", footprint2="R0603"),
        resistor("RES-0005", "100R", "R0805"),
        resistor("RES-0005", "100R", "R0805", maker="Twin"),
        resistor("RES-0006", "1k", "R0603", tolerance=None),
    ],
    "Capacitors": [
        capacitor("CAP-0001", "100nF", "16V", "C0402"),
        capacitor("CAP-0002", "1uF", None, "C0603"),
    ],
}


class FakeDb:
    """A stand-in for query_db.ps1: evaluates each query's comparison tree on in-memory
    tables (db_query.evaluate, the in-process reader's own logic) and keeps every request."""

    def __init__(self, tables=None, fail: str | None = None, error_tables=()):
        self.tables = TABLES if tables is None else tables
        self.fail = fail
        self.error_tables = set(error_tables)
        self.requests: list[dict] = []

    def __call__(self, payload: bytes, timeout: float) -> bytes:
        request = json.loads(payload.decode("utf-8"))
        self.requests.append(request)
        if self.fail:
            return json.dumps({"ok": False, "error": self.fail, "attempts": [
                {"error": self.fail, "type": "OleDbException"}]}).encode()
        results = []
        for query in request["queries"]:
            rows = self.tables.get(query["table"])
            if rows is None or query["table"] in self.error_tables:
                results.append({"error": "table_not_found", "type": "OleDbException"})
                continue
            columns = list(rows[0]) if rows else []
            try:
                hits = [row for row in rows if dq.evaluate(query["where"], row) is True]
            except KeyError:
                results.append({"error": "field_not_found", "type": "OleDbException"})
                continue
            limit = query["limit"]
            results.append({"columns": columns,
                             "rows": [[row[name] for name in columns] for row in hits[:limit]],
                             "truncated": len(hits) > limit})
        return json.dumps({"ok": True, "attempt": 0, "attempts": [],
                           "results": results}).encode("utf-8")


def component(lib_ref="RES", design_item="", table="Resistors", footprint="R0603",
              params=None, library="Parts.DbLib", description="", footprints=None, **extra):
    meta = {"lib_reference": lib_ref, "design_item_id": design_item, "library": library,
            "database_table": table, "footprint": footprint,
            "footprints": footprints or ([footprint] if footprint else []),
            "params": dict(params or {}), "description": description}
    meta.update(extra)
    return meta


def netlist_of(components: dict) -> dict:
    return {"components": components, "nets": {}, "parts": {}}


def write_project(folder: Path, files: dict[str, str], documents=(), search_paths=(),
                  database=True) -> str:
    """A .PrjPcb listing documents (and search paths), the library files, and an empty
    stand-in database file Parts.mdb the fake runner never opens."""
    folder.mkdir(parents=True, exist_ok=True)
    text = "[Design]\nVersion=1.0\n[Document1]\nDocumentPath=Board.SchDoc\n"
    for index, name in enumerate(documents, 2):
        text += f"[Document{index}]\nDocumentPath={name}\n"
    for index, path in enumerate(search_paths, 1):
        text += f"[SearchPath{index}]\nPath={path}\nIncludeSubFolders=1\n"
    (folder / "Board.PrjPcb").write_text(text, encoding="utf-8")
    for name, body in files.items():
        target = folder / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    if database:
        (folder / "Parts.mdb").write_bytes(b"stand-in")
    return str(folder / "Board.PrjPcb")


def access_source(folder: Path, password: str | None = None) -> str:
    text = f"Provider=Microsoft.ACE.OLEDB.12.0;Data Source={folder / 'Parts.mdb'}"
    if password:
        text += f";Jet OLEDB:Database Password={password}"
    return text + ";Persist Security Info=False"


@pytest.fixture
def fake_db(monkeypatch):
    db = FakeDb()
    monkeypatch.setattr(dq, "runner", db)
    monkeypatch.setattr(dq, "path_location", lambda path: "local")
    dp.clear_cache()
    yield db
    dp.clear_cache()


def standard_parts() -> dict:
    return {
        "R1": component(design_item="RES-0001", params={
            "part number": "RES-0001", "value": "10k", "tolerance": "1%", "manufacturer": "Acme",
            "manufacturer part number": "AC-RES-0001"}, description="Resistor 10k"),
        "R2": component(design_item="RES-0004", params={
            "part number": "RES-0004", "value": "4.7k", "manufacturer part number": "AC-RES-0004"},
            description="Resistor 4k7"),
        "R3": component(design_item="RES-0005", footprint="R0805",
                        params={"part number": "RES-0005"}),
        "R4": component(design_item="RES-9999", params={"part number": "RES-9999"}),
        "C1": component("CAP", "CAP-0002", "Capacitors", "C0603", params={
            "part number": "CAP-0002", "value": "1uF", "voltage": "10V",
            "manufacturer part number": "AC-CAP-0002"}),
        "C2": component("CAPX", "CAP-0001", "Capacitors", "C0603", params={
            "part number": "CAP-0001", "value": "100nF", "voltage": "16V",
            "manufacturer part number": "AC-CAP-0001"}),
        "J1": component("CONN", "", "", "HDR2", library="Connectors.SchLib",
                        params={"part number": "RES-0001"}),
    }


def check(prj: str, parts: dict, **extra) -> dict:
    return dp.build_check(dp.CheckInputs(prj, netlist_of(parts), installed=[], **extra))


def by_kind(model: dict, kind: str) -> list[dict]:
    return [entry for entry in model["findings"] if entry["kind"] == kind]


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def test_parse_dblib_settings_tables_and_field_maps(tmp_path):
    path = tmp_path / "Parts.DbLib"
    path.write_text(dblib_text(access_source(tmp_path),
                               "LibrarySearchPath=Symbols\nSearchSubDirectories=1\n"),
                    encoding="utf-8")
    library = dl.parse_db_lib(str(path))
    assert library.kind == "DbLib" and library.version == "1.1" and library.encoding == "utf-8"
    assert library.defaults == {"add": "if_not_blank", "remove": "never", "update": "always"}
    assert library.defaults_assumed == []
    assert library.library_search_path == "Symbols" and library.search_subdirectories
    assert [table.name for table in library.tables] == ["Resistors", "Capacitors"]
    resistors, capacitors = library.tables
    assert resistors.lookup() == {"by": "key", "fields": [
        {"field": "Part Number", "parameter": "Part Number"}]}
    assert capacitors.lookup() == {"by": "where", "where": "[Part Number] = '{Part Number}'"}
    assert len(resistors.fields) == len(RESISTOR_FIELDS)
    systems = {item.field: item.system for item in resistors.fields}
    assert systems["Library Ref"] == "symbol_ref" and systems["Footprint Ref"] == "footprint_ref"
    assert systems["Footprint Ref 2"] == "footprint_ref_2" and systems["Description"] == "description"
    notes = next(item for item in resistors.fields if item.field == "Notes")
    assert not notes.mapped and notes.design_parameter == ""
    assert [item.field for item in resistors.system_fields("footprint_ref")] == [
        "Footprint Ref", "Footprint Ref 2"]
    voltage = capacitors.by_parameter("voltage")
    assert voltage.remove_mode == "if_blank" and voltage.modes(library.defaults) == {
        "add": "if_not_blank", "remove": "if_blank", "update": "always"}
    described = library.describe()
    assert described["tables"][0]["symbol_ref_fields"] == ["Library Ref"]
    assert described["tables"][0]["fields"][0] == {
        "field": "Part Number", "parameter": "Part Number", "key": True, "visible_on_add": False,
        "options": {"add": "if_not_blank", "remove": "never", "update": "always"}}
    assert library.table("[resistors]") is resistors and library.table("dbo.Capacitors") is capacitors


def test_field_maps_inline_and_as_plain_sections(tmp_path):
    tables = ("[Table1]\nTableName=Parts\nEnabled=1\nUserWhere=0\n"
              "FieldMap1=FieldName=[Parts].[Code]|FieldType=0|ParameterName=Code\n")
    maps = ("[FieldMap2]\nFieldName=[Parts].[Value]\nTableNameOnly=Parts\nFieldNameOnly=Value\n"
            "FieldType=1\nParameterName=Value\nUpdateMode=eParameterUpdate_Never\n"
            "AddMode=Always\n[FieldMap3]\nOptions=|FieldNameOnly=Orphan|TableNameOnly=Gone"
            "|FieldType=1|ParameterName=X\n")
    path = tmp_path / "Inline.DbLink"
    path.write_text(dblib_text("", tables=tables, maps=maps, defaults=(0, 0, 0)),
                    encoding="utf-8")
    library = dl.parse_db_lib(str(path))
    assert library.kind == "DbLink" and library.connection.kind == "none"
    assert library.defaults == dl.DEFAULT_MODES
    assert library.defaults_assumed == ["add", "remove", "update"]
    table = library.tables[0]
    assert [(item.field, item.key) for item in table.fields] == [("Code", True), ("Value", False)]
    assert table.fields[1].update_mode == "never" and table.fields[1].add_mode == "always"
    assert library.orphan_fields == 1 and library.describe()["field_maps_without_table"] == 1


@pytest.mark.parametrize("kind,text,expected", [
    ("add", "3", "if_not_blank"), ("add", "eParameterAdd_IfNotBlankInLibrary", "if_not_blank"),
    ("remove", "2", "if_blank"), ("remove", "Never", "never"), ("update", "0", "default"),
    ("update", "", ""), ("update", "7", "7"),
])
def test_update_option_names(kind, text, expected):
    assert dl._mode(kind, text) == expected


def test_encodings_and_svn_repository_settings(tmp_path):
    path = tmp_path / "Old.SVNDbLib"
    text = dblib_text("", "SVNRepositoryURL=svn://user:pw@svnhost/libs\nSVNPassword=hunter\n")
    path.write_bytes(text.replace("Resistors", "Résistances").encode("cp1252"))
    library = dl.parse_db_lib(str(path))
    assert library.encoding == "cp1252" and library.kind == "SVNDbLib"
    assert library.tables[0].name == "Résistances"
    assert library.repository == {"SVNRepositoryURL": "svn://****@svnhost/libs",
                                  "SVNPassword": "****"}
    bom = tmp_path / "Bom.DbLib"
    bom.write_bytes(b"\xef\xbb\xbf" + dblib_text("").encode("utf-8"))
    assert dl.parse_db_lib(str(bom)).tables[0].name == "Resistors"


def test_relative_database_path_wins_over_the_saved_absolute_one(tmp_path):
    folder = tmp_path / "lib"
    folder.mkdir()
    (folder / "Data").mkdir()
    lines = ("LibraryDatabaseType=Microsoft Access\nLibraryDatabasePath=Data\\Parts.accdb\n"
             "DatabasePathRelative=1\n")
    text = dblib_text("Provider=Microsoft.ACE.OLEDB.12.0;Data Source=Q:\\Old\\Parts.accdb",
                      lines).replace("LibraryDatabaseType=\nLibraryDatabasePath=\n"
                                     "DatabasePathRelative=0\n", "")
    (folder / "Rel.DbLib").write_text(text, encoding="utf-8")
    library = dl.parse_db_lib(str(folder / "Rel.DbLib"))
    assert library.path_relative and library.database_type == "Microsoft Access"
    assert os.path.normcase(library.connection.file) == os.path.normcase(
        os.path.normpath(str(folder / "Data" / "Parts.accdb")))


def test_split_connection_string_quotes_and_braces():
    pairs = dl.split_connection_string(
        'Provider=MSDASQL.1;Extended Properties="DRIVER={X Driver; v2};SERVER=h;PWD=a;b"'
        ";Driver={My {}} Driver};Name='It''s';;Empty=")
    assert pairs == [("Provider", "MSDASQL.1"),
                     ("Extended Properties", "DRIVER={X Driver; v2};SERVER=h;PWD=a;b"),
                     ("Driver", "{My {}} Driver}"), ("Name", "It's"), ("Empty", "")]
    assert dl.join_connection_string([("A", "x;y"), ("B", "{z;}"), ("C", "plain")]) == \
        'A="x;y";B={z;};C=plain'


def _connection(text: str, base: str = "", lookup=None) -> dl.ConnectionInfo:
    return dl.parse_connection(text, base, dsn_lookup=lookup)


@pytest.mark.parametrize("text,kind,network", [
    ("Provider=Microsoft.Jet.OLEDB.4.0;Data Source=Parts.mdb", "access", False),
    ("Provider=Microsoft.ACE.OLEDB.12.0;Data Source=Book.xlsx;Extended Properties="
     '"Excel 12.0 Xml;HDR=YES"', "excel", False),
    ("Provider=Microsoft.ACE.OLEDB.12.0;Data Source=Book.xls", "excel", False),
    ("Provider=Microsoft.ACE.OLEDB.12.0;Data Source=Folder;Extended Properties=\"text;HDR=Yes\"",
     "text", False),
    ("Provider=SQLOLEDB.1;Data Source=dbhost\\SQLEXPRESS;Initial Catalog=Parts", "sql_server", True),
    ("Provider=MSOLEDBSQL;Server=dbhost;Database=Parts", "sql_server", True),
    ("Provider=OraOLEDB.Oracle;Data Source=orahost", "oracle", True),
    ("Provider=MSDASQL.1;Extended Properties=\"DRIVER={MySQL ODBC 8.0 Unicode Driver};"
     "SERVER=dbhost;DATABASE=parts\"", "mysql", True),
    ("Driver={Microsoft Access Driver (*.mdb, *.accdb)};DBQ=Parts.accdb", "access", False),
    ("DSN=SharedParts", "odbc_dsn", True),
    ("Provider=Some.Unknown.Provider;Data Source=x", "other", True),
    ("", "none", False),
])
def test_connection_kinds(tmp_path, text, kind, network):
    info = _connection(text, str(tmp_path))
    assert (info.kind, info.network) == (kind, network)
    if kind in ("access", "excel", "text"):
        assert info.file.startswith(str(tmp_path))


def test_a_dsn_on_a_local_file_is_a_file_source(tmp_path):
    lookup = {"LocalParts": ("Microsoft Access Driver (*.mdb, *.accdb)",
                             str(tmp_path / "Local.accdb"))}.get
    info = _connection("Provider=MSDASQL.1;Data Source=LocalParts", str(tmp_path), lookup)
    assert info.kind == "access" and not info.network and info.file.endswith("Local.accdb")
    assert info.dsn == "LocalParts"


def test_credentials_are_masked_everywhere(tmp_path):
    text = (f"Provider=SQLOLEDB.1;Password={SECRET};Persist Security Info=True;User ID={USER};"
            "Initial Catalog=Parts;Data Source=dbhost")
    info = _connection(text)
    assert info.credentials and info.kind == "sql_server" and info.server == "dbhost"
    shown = json.dumps(info.describe()) + repr(info) + info.masked
    assert SECRET not in shown and USER not in shown and "****" in info.masked
    nested = _connection('Provider=MSDASQL.1;Extended Properties="DRIVER={MySQL ODBC 8.0 Driver};'
                         f'SERVER=h;UID={USER};PWD={SECRET}"')
    assert nested.credentials and SECRET not in json.dumps(nested.describe())
    assert USER not in nested.masked and SECRET not in nested.extended
    windows = _connection("Provider=SQLOLEDB.1;Integrated Security=SSPI;Data Source=h")
    assert windows.windows_auth and not windows.credentials


def test_udl_files_are_read_the_same_way(tmp_path):
    udl = tmp_path / "Parts.udl"
    udl.write_bytes(("[oledb]\r\n; Everything after this line is an OLE DB initstring\r\n"
                     f"Provider=SQLOLEDB.1;Password={SECRET};User ID={USER};Data Source=dbhost\r\n"
                     ).encode("utf-16"))
    info = _connection("File Name=Parts.udl", str(tmp_path))
    assert info.kind == "sql_server" and info.network and info.credentials
    assert info.udl == str(udl) and SECRET not in json.dumps(info.describe())
    local = tmp_path / "Local.udl"
    local.write_bytes("[oledb]\r\nProvider=Microsoft.ACE.OLEDB.12.0;Data Source=Parts.mdb\r\n"
                      .encode("utf-16"))
    assert _connection(f"File Name={local}", str(tmp_path)).kind == "access"
    missing = _connection("File Name=Gone.udl", str(tmp_path))
    assert missing.error == "udl_missing" and missing.kind == "none"


def test_db_kind_and_library_kind_agree():
    assert dl.db_kind("x.DbLib") == "DbLib" and dl.db_kind("x.svndblib") == "SVNDbLib"
    assert dl.db_kind("x.DBLINK") == "DbLink" and dl.db_kind("x.SchLib") == ""


# ---------------------------------------------------------------------------
# WHERE clauses
# ---------------------------------------------------------------------------

def test_where_clauses_parse_into_comparisons():
    tree = dl.parse_where("[Part Number] = '{Part Number}'")
    assert tree[0] == "cmp" and tree[1] == "Part Number" and tree[2] == "="
    assert tree[3].placeholders == ["Part Number"]
    tree = dl.parse_where("([Value] = '{Value}' AND Parts.[Case] LIKE '%{Footprint}%') "
                          "OR NOT [Obsolete] = 1 OR [Notes] IS NOT NULL")
    assert tree[0] == "or" and tree[1][0][0] == "and"
    assert tree[1][0][1][1][1] == "Case" and tree[1][0][1][1][2] == "like"
    assert tree[1][1][0] == "not" and tree[1][2] == ("null", "Notes", True)
    assert dl.where_placeholders(tree) == ["Value", "Footprint"]
    assert dl.where_fields(tree) == ["Value", "Case", "Obsolete", "Notes"]
    swapped = dl.parse_where("'{Code}' = [Code] AND [Rating] >= {Volts}")
    assert swapped[1][0][1] == "Code" and swapped[1][1][3].kind == "placeholder"
    node = dp.bind_where(swapped, {"code": "X-1", "volts": "16"})
    assert node == {"and": [{"cmp": ["Code", "=", "X-1"]}, {"cmp": ["Rating", ">=", 16.0]}]}
    assert dq.check_where(node)


@pytest.mark.parametrize("text", [
    "UPPER([Code]) = '{Code}'", "[Code] IN ('a', 'b')", "[Code] = [Other]",
    "[Code] = 'open", "[Code] = '{Code}'; DROP TABLE Parts", "", "[Code] =",
    "[Code] = (SELECT 1)",
])
def test_unsupported_where_clauses_are_refused(text):
    with pytest.raises(dl.WhereError):
        dl.parse_where(text)


# ---------------------------------------------------------------------------
# Query layer
# ---------------------------------------------------------------------------

def test_names_and_trees_are_checked():
    assert dq.check_name("Part Number") and not dq.check_name("a]b")
    assert not dq.check_name("x;DROP") and not dq.check_name("") and not dq.check_name("x" * 129)
    assert dq.check_where({"and": [{"cmp": ["A", "=", "1"]}, {"null": ["B", False]}]})
    assert not dq.check_where({"cmp": ["A", "drop", "1"]})
    assert not dq.check_where({"cmp": ["A", "<", None]})
    assert not dq.check_where({"sql": "SELECT 1"})
    assert not dq.check_where({"and": []})


def test_run_queries_refuses_bad_queries_before_any_process(monkeypatch):
    calls = []
    monkeypatch.setattr(dq, "runner", lambda payload, timeout: calls.append(payload))
    plan = dq.SourcePlan("driver", attempts=[("oledb", "x")])
    for query in (dq.Query("Parts]; DROP TABLE x"), dq.Query("Parts", {"cmp": ["a'b]", "=", "1"]}),
                  dq.Query("Parts", {"raw": "1=1"})):
        with pytest.raises(dq.QueryError) as error:
            dq.run_queries(plan, [query])
        assert error.value.code == "bad_query"
    assert calls == []


def test_requests_are_structured_selects_with_limits(monkeypatch):
    db = FakeDb()
    monkeypatch.setattr(dq, "runner", db)
    plan = dq.SourcePlan("driver", attempts=[("oledb", f"Provider=X;Password={SECRET}")])
    results = dq.run_queries(plan, [
        dq.Query("Resistors", {"cmp": ["Value", "=", "10k"]}, limit=2),
        dq.Query("Missing"), dq.Query("Resistors", {"cmp": ["Nope", "=", "1"]})])
    request = db.requests[0]
    assert set(request) == {"attempts", "quote", "timeout", "local_only", "queries"}
    assert request["local_only"] is False
    assert request["timeout"] == dq.COMMAND_TIMEOUT and request["quote"] == ["[", "]"]
    assert request["queries"][0] == {"table": "Resistors", "schema": "",
                                     "where": {"cmp": ["Value", "=", "10k"]}, "limit": 2}
    assert len(results[0].rows) == 2 and results[0].truncated
    assert results[1].error == "table_not_found" and results[2].error == "field_not_found"


def test_the_connection_goes_through_stdin_never_the_command_line(monkeypatch):
    seen = {}

    def run(args, input=None, **kwargs):
        seen["args"], seen["input"] = args, input
        return subprocess.CompletedProcess(args, 0, json.dumps(
            {"ok": True, "attempt": 0, "attempts": [], "results": [
                {"columns": [], "rows": [], "truncated": False}]}).encode(), b"")

    monkeypatch.setattr(dq, "powershell", lambda: "powershell.exe")
    monkeypatch.setattr(dq.subprocess, "run", run)
    monkeypatch.setattr(dq, "runner", dq.powershell_runner)
    plan = dq.SourcePlan("driver", attempts=[("oledb", f"Provider=X;Password={SECRET}")])
    dq.run_queries(plan, [dq.Query("Parts")])
    assert SECRET not in " ".join(seen["args"]) and SECRET.encode() in seen["input"]
    assert seen["args"][-1].endswith("query_db.ps1") and "-NonInteractive" in seen["args"]


def test_failures_become_fixed_codes(monkeypatch):
    plan = dq.SourcePlan("driver", attempts=[("oledb", "x")])
    monkeypatch.setattr(dq, "runner", FakeDb(fail="provider_not_registered"))
    with pytest.raises(dq.QueryError) as error:
        dq.run_queries(plan, [dq.Query("Parts")])
    assert error.value.code == dq.SKIP_NO_DRIVER
    monkeypatch.setattr(dq, "runner", FakeDb(fail="login_failed"))
    with pytest.raises(dq.QueryError) as error:
        dq.run_queries(plan, [dq.Query("Parts")])
    assert error.value.code == "login_failed"
    monkeypatch.setattr(dq, "runner", lambda payload, timeout: b"not json")
    with pytest.raises(dq.QueryError) as error:
        dq.run_queries(plan, [dq.Query("Parts")])
    assert error.value.code == "bad_response"
    monkeypatch.setattr(dq, "runner", dq.powershell_runner)
    monkeypatch.setattr(dq, "powershell", lambda: None)
    with pytest.raises(dq.QueryError) as error:
        dq.run_queries(plan, [dq.Query("Parts")])
    assert error.value.code == dq.SKIP_NO_DRIVER


def test_access_attempts_are_read_only(tmp_path):
    info = _connection(f"Provider=Microsoft.Jet.OLEDB.4.0;Data Source={tmp_path / 'P.mdb'};"
                       f"Jet OLEDB:Database Password={SECRET}")
    attempts = dq.connection_attempts(info)
    assert [kind for kind, _ in attempts] == ["oledb", "oledb", "oledb", "odbc"]
    assert "Mode=Read" in attempts[0][1] and "Microsoft.Jet.OLEDB.4.0" in attempts[0][1]
    assert "Microsoft.ACE.OLEDB.16.0" in attempts[1][1] and SECRET in attempts[1][1]
    assert "ReadOnly=1" in attempts[3][1] and f"PWD={SECRET}" in attempts[3][1]
    excel = dq.connection_attempts(_connection(
        f"Provider=Microsoft.ACE.OLEDB.12.0;Data Source={tmp_path / 'B.xls'}"))
    assert [kind for kind, _ in excel] == ["oledb", "oledb", "odbc"]
    assert "Excel 8.0" in excel[1][1] and "Mode=Read" in excel[0][1]
    dsn = dq.connection_attempts(_connection(
        f"Provider=MSDASQL.1;Data Source=Parts;User ID={USER};Password={SECRET}"))
    assert dsn == [("odbc", f"DSN=Parts;UID={USER};PWD={SECRET}")]


def test_plan_source_skips_with_reasons(tmp_path, monkeypatch):
    (tmp_path / "Parts.mdb").write_bytes(b"x")

    def library(source: str) -> dl.DatabaseLibrary:
        path = tmp_path / "L.DbLib"
        path.write_text(dblib_text(source), encoding="utf-8")
        return dl.parse_db_lib(str(path))

    local = library(access_source(tmp_path))
    assert dq.plan_source(local, location_of=lambda path: "local")[0].mode == "driver"
    assert dq.plan_source(local, location_of=lambda path: "share") == (None, dq.SKIP_SHARE)
    assert dq.plan_source(local, True, location_of=lambda path: "share")[0] is not None
    server = library(f"Provider=SQLOLEDB.1;Data Source=dbhost;Password={SECRET}")
    assert dq.plan_source(server) == (None, dq.SKIP_NETWORK)
    allowed, _ = dq.plan_source(server, allow_network=True)
    assert allowed.location == "server" and allowed.attempts[0][0] == "oledb"
    gone = library("Provider=Microsoft.ACE.OLEDB.12.0;Data Source=Gone.mdb")
    assert dq.plan_source(gone, location_of=lambda path: "local") == (None, dq.SKIP_FILE_MISSING)
    assert dq.plan_source(library("")) == (None, dq.SKIP_NO_CONNECTION)
    assert dq.plan_source(library("File Name=Gone.udl")) == (None, dq.SKIP_FILE_MISSING)
    (tmp_path / "Book.xlsx").write_bytes(b"x")
    book = library("Provider=Microsoft.ACE.OLEDB.12.0;Data Source=Book.xlsx;"
                   'Extended Properties="Excel 12.0 Xml;HDR=YES"')
    assert dq.plan_source(book, location_of=lambda path: "local")[0].mode == "xlsx"


def test_path_location(monkeypatch, tmp_path):
    monkeypatch.setattr(dq, "_SYNC_ROOTS", None)  # read again, with the variable below
    assert dq.path_location("\\\\server\\share\\Parts.mdb") == "share"
    assert dq.path_location("//server/share/Parts.mdb") == "share"
    monkeypatch.setenv("OneDriveCommercial", str(tmp_path / "Cloud"))
    assert dq.path_location(str(tmp_path / "Cloud" / "Parts.mdb")) == "onedrive"
    assert dq.path_location(str(tmp_path / "OneDrive - Team" / "Parts.mdb")) == "onedrive"
    assert dq.path_location(str(tmp_path / "Local" / "Parts.mdb")) == "local"


def test_evaluate_follows_sql_logic():
    row = {"Value": "10k", "Case": "R0603", "Rating": "16", "Notes": None}
    assert dq.evaluate({"cmp": ["value", "=", "10K"]}, row) is True  # any case
    assert dq.evaluate({"cmp": ["Case", "like", "R06%"]}, row) is True
    assert dq.evaluate({"cmp": ["Case", "not like", "R04__"]}, row) is True
    assert dq.evaluate({"cmp": ["Rating", ">", 9]}, row) is True
    assert dq.evaluate({"cmp": ["Notes", "=", "x"]}, row) is None
    assert dq.evaluate({"not": {"cmp": ["Notes", "=", "x"]}}, row) is None
    assert dq.evaluate({"or": [{"cmp": ["Notes", "=", "x"]}, {"null": ["Notes", False]}]}, row)
    assert dq.evaluate({"cmp": ["Notes", "=", None]}, row) is True
    with pytest.raises(KeyError):
        dq.evaluate({"cmp": ["Missing", "=", "1"]}, row)


def xlsx(sheets: dict[str, list[list]]) -> bytes:
    """A small workbook: shared strings, numbers as Excel stores them."""
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
                    elif value:
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


def test_workbooks_are_read_without_a_driver(tmp_path, monkeypatch):
    monkeypatch.setattr(dq, "runner", lambda payload, timeout: pytest.fail("no process"))
    book = tmp_path / "Parts.xlsx"
    book.write_bytes(xlsx({"Notes": [["read me"]], "Resistors": [
        [], ["Part Number", "Value", "", "Value"], ["RES-0001", "10k", "x", "dup"],
        ["RES-0002", "", "", ""], ["RES-0003", 10, "", ""]]}))
    plan = dq.SourcePlan("xlsx", path=str(book))
    first, missing, numeric, bad = dq.run_queries(plan, [
        dq.Query("Resistors$", {"cmp": ["Part Number", "=", "res-0001"]}),
        dq.Query("Gone$"),
        dq.Query("'Resistors$'", {"cmp": ["Value", "=", 10]}),
        dq.Query("Resistors$", {"cmp": ["Nope", "=", "1"]})])
    assert first.columns == ["Part Number", "Value", "F3", "F4"]
    assert first.rows == [["RES-0001", "10k", "x", "dup"]]
    assert missing.error == "table_not_found" and bad.error == "field_not_found"
    assert numeric.rows == [["RES-0003", "10", None, None]]
    blank = dq.run_queries(plan, [dq.Query("Resistors$", {"null": ["Value", False]})])[0]
    assert [row[0] for row in blank.rows] == ["RES-0002"]


# ---------------------------------------------------------------------------
# The schematic side
# ---------------------------------------------------------------------------

def test_sheets_keep_the_database_fields():
    parts = components_from_stream(_stream(
        "|RECORD=1|LibReference=RES|UniqueID=AAAAAAAA|SourceLibraryName=Parts.DbLib"
        "|DatabaseLibraryName=Parts.DbLib|DatabaseTableName=Resistors|UseDBTableName=T"
        "|DesignItemId=RES-0001",
        "|RECORD=34|OwnerIndex=0|Text=R1|Name=Designator",
        "|RECORD=1|LibReference=CAP|UniqueID=BBBBBBBB|SourceLibraryName=Caps.SchLib",
        "|RECORD=34|OwnerIndex=2|Text=C1|Name=Designator"))
    r1, c1 = parts["R1"], parts["C1"]
    assert (r1.database_library, r1.database_table, r1.use_db_table_name) == (
        "Parts.DbLib", "Resistors", True)
    assert (c1.database_library, c1.use_db_table_name) == ("", False)
    from server.altium import _make_enriched
    enriched = _make_enriched(r1, {})
    assert enriched["database_library"] == "Parts.DbLib" and enriched["use_db_table_name"]


# ---------------------------------------------------------------------------
# Finding the libraries and linking parts
# ---------------------------------------------------------------------------

def test_libraries_are_found_with_their_source(tmp_path, fake_db):
    installed = tmp_path / "Installed" / "Shared.DbLink"
    installed.parent.mkdir()
    installed.write_text(dblib_text(""), encoding="utf-8")
    prj = write_project(tmp_path / "Board", {
        "Parts.DbLib": dblib_text(access_source(tmp_path / "Board")),
        "Libs/Search.DbLink": dblib_text(""),
        "Elsewhere/Placed.DbLib": dblib_text(""),
    }, documents=["Parts.DbLib", "Gone.DbLib"], search_paths=["Libs\\*.DbLink"])
    placed = str(tmp_path / "Board" / "Elsewhere" / "Placed.DbLib")
    parts = [dp.Part("R1", library="Parts.DbLib"), dp.Part("R2", library="Missing.DbLib"),
             dp.Part("R3", library="Placed.DbLib", library_path=placed),
             dp.Part("R4", database_library="Missing.DbLib", library="R.SchLib")]
    found, missing = dp.find_libraries(prj, parts, [str(installed)])
    origins = {library.name: library.origins for library in found}
    assert origins == {"Parts.DbLib": ["project"], "Gone.DbLib": ["project"],
                       "Shared.DbLink": ["installed"], "Search.DbLink": ["search path"],
                       "Placed.DbLib": ["part's library path"]}
    assert next(library for library in found if library.name == "Gone.DbLib").error == \
        "file_missing"
    assert next(library for library in found if library.name == "Parts.DbLib").named_by == ["R1"]
    assert missing == [{"library": "Missing.DbLib", "parts": ["R2", "R4"], "part_count": 2}]


def test_check_matches_by_key_and_where_and_reports_drift(tmp_path, fake_db):
    folder = tmp_path / "Board"
    prj = write_project(folder, {"Parts.DbLib": dblib_text(access_source(folder))},
                        documents=["Parts.DbLib"])
    model = check(prj, standard_parts())
    summary = model["summary"]
    assert summary["parts"] == 7 and summary["database_library_parts"] == 6
    assert summary["parts_linked"] == 6 and summary["records_found"] == 5
    assert summary["libraries_queried"] == 1
    # R2: Value differs and the field updates: would_change; Tolerance and Manufacturer
    # are blank on the part: would_add (if not blank).
    change = by_kind(model, "would_change")
    assert [(f["designator"], f["parameter"], f["schematic"], f["database"]) for f in change] == [
        ("R2", "Value", "4.7k", "4k7")]
    added = {(f["designator"], f["parameter"]) for f in by_kind(model, "would_add")}
    assert ("R2", "Tolerance") in added and ("R3", "Value") in added
    # C1: Voltage blank in the database and its remove option is If blank.
    assert [(f["designator"], f["parameter"]) for f in by_kind(model, "would_remove")] == [
        ("C1", "Voltage")]
    duplicate = by_kind(model, "duplicate_records")[0]
    assert duplicate["designator"] == "R3" and duplicate["records"] == 2
    missing = by_kind(model, "record_not_found")[0]
    assert missing["designator"] == "R4" and missing["matched_by"]["fields"][0]["value"] == \
        "RES-9999"
    symbol = by_kind(model, "symbol_differs")[0]
    assert (symbol["designator"], symbol["schematic"], symbol["database"]) == ("C2", "CAPX", "CAP")
    footprints = {f["designator"]: f for f in by_kind(model, "footprint_differs")}
    assert footprints["C2"]["severity"] == "warning" and footprints["C2"]["database"] == "C0402"
    # R2 sits on R0603, the record's second footprint: info.
    assert footprints["R2"]["severity"] == "info"
    assert footprints["R2"]["database_all"] == ["R0402", "R0603"]
    # The WHERE-clause table asked with C1's own Part Number.
    queries = [query for request in fake_db.requests for query in request["queries"]]
    assert {"table": "Capacitors", "schema": "", "where": {
        "cmp": ["Part Number", "=", "CAP-0002"]}, "limit": dp.LOOKUP_LIMIT} in queries
    assert all(set(query) == {"table", "schema", "where", "limit"} for query in queries)
    row = model["libraries"][0]
    assert row["status"] == "queried" and row["parts_linked"] == 6 and row["records_found"] == 5
    assert row["tables"][1]["lookup"]["by"] == "where" and row["connection"]["kind"] == "access"
    severities = [f["severity"] for f in model["findings"]]
    assert severities == sorted(severities, key=dp.SEVERITY_ORDER.get)


def test_each_update_option(tmp_path, fake_db):
    folder = tmp_path / "Board"
    maps = "".join([
        field_map(1, "Parts", "Code", "Code", key=True),
        field_map(2, "Parts", "A", "A", update=2), field_map(3, "Parts", "B", "B", update=1),
        field_map(4, "Parts", "C", "C", add=2), field_map(5, "Parts", "D", "D", add=1),
        field_map(6, "Parts", "E", "E", add=3), field_map(7, "Parts", "F", "F", remove=2),
        field_map(8, "Parts", "G", "G", remove=1), field_map(9, "Parts", "H", "H"),
        field_map(10, "Parts", "I", "I", add=2),
    ])
    tables = "[Table1]\nTableName=Parts\nEnabled=True\nUserWhere=0\n"
    prj = write_project(folder, {"P.DbLib": dblib_text(access_source(folder), tables=tables,
                                                       maps=maps)}, documents=["P.DbLib"])
    fake_db.tables = {"Parts": [{"Code": "X1", "A": "new", "B": "new", "C": "c", "D": "d",
                                 "E": None, "F": None, "G": "", "H": "Same", "I": None}]}
    part = component(lib_ref="P", design_item="X1", table="Parts", footprint="",
                     library="P.DbLib", params={"code": "X1", "a": "old", "b": "old",
                                                "f": "kept?", "g": "kept", "h": "SAME"})
    model = check(prj, {"U1": part})
    kinds = {(f["kind"], f.get("parameter")): f for f in model["findings"]}
    assert kinds[("would_change", "A")]["severity"] == "warning"
    assert kinds[("differs_not_updated", "B")]["severity"] == "info"
    assert ("would_add", "C") in kinds and ("would_add", "D") not in kinds
    assert ("would_add", "E") not in kinds  # blank in the database: not added
    assert ("would_add", "I") in kinds  # Always adds even a blank
    assert kinds[("would_remove", "F")]["schematic"] == "kept?"
    assert kinds[("blank_in_database", "G")]["severity"] == "info"
    case = kinds[("would_change", "H")]
    assert case["case_only"] is True and case["severity"] == "info"


def test_design_item_id_stands_in_for_a_missing_or_stale_key(tmp_path, fake_db):
    folder = tmp_path / "Board"
    prj = write_project(folder, {"Parts.DbLib": dblib_text(access_source(folder))},
                        documents=["Parts.DbLib"])
    parts = {"R1": component(design_item="RES-0001", params={}),
             "R2": component(design_item="RES-0006", params={"part number": "RES-0099"}),
             "R3": component(design_item="", params={})}
    model = check(prj, parts)
    assert model["summary"]["records_found"] == 2
    assert [f["designator"] for f in by_kind(model, "key_missing")] == ["R3"]
    part = dp.build_part(dp.PartInputs(prj, netlist_of(parts), installed=[], designator="R2"))
    match = part["matches"][0]
    assert match["confidence"] == "design_item_id"
    assert match["matched_by"]["fields"][0]["source"] == "design item id"


def test_tables_named_by_parts(tmp_path, fake_db):
    folder = tmp_path / "Board"
    tables = ("[Table1]\nTableName=Resistors\nEnabled=True\nUserWhere=0\n"
              "[Table2]\nTableName=Capacitors\nEnabled=False\nUserWhere=1\n"
              "UserWhereText=[Part Number] = '{Part Number}'\n"
              "[Table3]\nTableName=Odd\nEnabled=True\nUserWhere=1\nUserWhereText=LEN([X]) = 1\n"
              "[Table4]\nTableName=Bare\nEnabled=True\nUserWhere=0\n")
    prj = write_project(folder, {"Parts.DbLib": dblib_text(access_source(folder), tables=tables)},
                        documents=["Parts.DbLib"])
    parts = {
        "R1": component(design_item="RES-0001", table="Gone", params={"part number": "RES-0001"}),
        "C1": component("CAP", "CAP-0001", "Capacitors", "C0402",
                        params={"part number": "CAP-0001"}),
        "U1": component("IC", "X", "Odd", "", params={}),
        "U2": component("IC", "Y", "Bare", "", params={}),
        "U3": component("IC", "Z", "Bare", "", params={}, library="", database_library=""),
    }
    model = check(prj, parts)
    kinds = {(f["kind"], f.get("designator")) for f in model["findings"]}
    assert ("table_not_in_library", "R1") in kinds and ("table_disabled", "C1") in kinds
    assert ("where_unsupported", "U1") in kinds and ("no_lookup", "U2") in kinds
    # U3 names only the table: the one library holding it links it.
    assert ("no_lookup", "U3") in kinds
    # R1 searched in the enabled tables instead and found there.
    assert model["summary"]["records_found"] >= 1


def test_database_link_files_link_parts_by_their_key(tmp_path, fake_db):
    folder = tmp_path / "Board"
    tables = "[Table1]\nTableName=Resistors\nEnabled=True\nUserWhere=0\n"
    maps = "".join(field_map(number, "Resistors", name, parameter, key)
                   for number, (name, parameter, key) in enumerate(RESISTOR_FIELDS, 1))
    prj = write_project(folder, {"Parts.DbLink": dblib_text(access_source(folder), tables=tables,
                                                            maps=maps),
                                 "Empty.DbLink": dblib_text("", tables="", maps="")},
                        documents=["Parts.DbLink", "Empty.DbLink"])
    parts = {
        "R1": component(library="Res.SchLib", table="", params={
            "part number": "RES-0001", "value": "10k", "tolerance": "1%", "manufacturer": "Acme",
            "manufacturer part number": "AC-RES-0001"}, description="Resistor 10k"),
        "R2": component(library="Res.SchLib", table="", params={"part number": "RES-0404"}),
        "R3": component(library="Res.SchLib", table="", params={"value": "1k"}),
        "R4": component(library="Res.SchLib", table="Resistors", use_db_table_name=True,
                        params={"part number": "RES-0004"}, footprint="R0402"),
    }
    model = check(prj, parts)
    row = next(row for row in model["libraries"] if row["name"] == "Parts.DbLink")
    assert row["parts_linked"] == 3 and row["parts_without_key"] == 1
    assert row["records_found"] == 2
    assert [f["designator"] for f in by_kind(model, "record_not_found")] == ["R2"]
    empty = next(f for f in model["findings"] if f["kind"] == "no_tables")
    assert empty["library"] == "Empty.DbLink"
    assert next(row for row in model["libraries"] if row["name"] == "Empty.DbLink")[
        "data_source"] == dq.SKIP_NO_CONNECTION
    assert not [f for f in model["findings"] if f.get("designator") == "R3"]


def test_skipped_sources_say_why(tmp_path, monkeypatch, fake_db):
    folder = tmp_path / "Board"
    prj = write_project(folder, {
        "Server.DbLib": dblib_text(f"Provider=SQLOLEDB.1;Data Source=dbhost;Password={SECRET};"
                                   f"User ID={USER}"),
        "Gone.DbLib": dblib_text("Provider=Microsoft.ACE.OLEDB.12.0;Data Source=Gone.mdb"),
        "Share.DbLib": dblib_text(access_source(folder)),
    }, documents=["Server.DbLib", "Gone.DbLib", "Share.DbLib"])
    monkeypatch.setattr(dq, "path_location",
                        lambda path: "share" if path.endswith("Parts.mdb") else "local")
    parts = {f"R{index}": component(design_item="RES-0001", library=name,
                                    params={"part number": "RES-0001"})
             for index, name in enumerate(("Server.DbLib", "Gone.DbLib", "Share.DbLib"), 1)}
    model = check(prj, parts)
    reasons = {f["library"]: f["reason"] for f in by_kind(model, "library_skipped")}
    assert reasons == {"Server.DbLib": dq.SKIP_NETWORK, "Gone.DbLib": dq.SKIP_FILE_MISSING,
                       "Share.DbLib": dq.SKIP_SHARE}
    assert fake_db.requests == []
    assert SECRET not in json.dumps(model) and USER not in json.dumps(model)
    allowed = check(prj, parts, allow_network=True)
    assert {f["library"] for f in by_kind(allowed, "library_skipped")} == {"Gone.DbLib"}
    assert len(fake_db.requests) == 2
    monkeypatch.setattr(dq, "runner", FakeDb(fail="provider_not_registered"))
    no_driver = check(prj, parts, allow_network=True)
    assert {f["reason"] for f in by_kind(no_driver, "library_skipped")} == {
        dq.SKIP_FILE_MISSING, dq.SKIP_NO_DRIVER}


def test_query_errors_and_unreadable_libraries(tmp_path, fake_db, monkeypatch):
    folder = tmp_path / "Board"
    prj = write_project(folder, {"Parts.DbLib": dblib_text(access_source(folder))},
                        documents=["Parts.DbLib", "Gone.DbLib"])
    fake_db.error_tables = {"Resistors"}
    model = check(prj, {"R1": component(design_item="RES-0001",
                                        params={"part number": "RES-0001"})})
    failed = by_kind(model, "query_failed")[0]
    assert failed["error"] == "table_not_found" and failed["designator"] == "R1"
    gone = by_kind(model, "library_unreadable")[0]
    assert gone["library"] == "Gone.DbLib" and gone["error"] == "file_missing"
    assert "not on disk" in gone["detail"]

    def broken(path, dsn_lookup=None, udl_allowed=None):
        raise ValueError("bad file")

    monkeypatch.setattr(dp, "parse_db_lib", broken)
    unreadable = check(prj, {})
    assert {f["error"] for f in by_kind(unreadable, "library_unreadable")} == {
        "file_missing", "unreadable (ValueError)"}


def test_variants_mark_not_fitted_parts_and_alternates(tmp_path, fake_db):
    folder = tmp_path / "Board"
    prj = write_project(folder, {"Parts.DbLib": dblib_text(access_source(folder))},
                        documents=["Parts.DbLib"])
    parts = standard_parts()
    netlist = netlist_of(parts)
    lite = VariantDefinition(name="Lite", variations=[
        Variation(designator="R2", unique_id="", kind=1),
        Variation(designator="R1", unique_id="", kind=2, alternate_part="RES", alt={
            "DesignItemID": "RES-0404", "SourceLibraryName": "Parts.DbLib",
            "UseDBTableName": "True", "DatabaseTableName": "Resistors"})],
        param_variations={"R2": {"value": ("Value", "4k7")}})
    plan = plan_variant(lite, netlist, list(parts))
    model = dp.build_check(dp.CheckInputs(prj, netlist, plan, installed=[]))
    change = by_kind(model, "would_change")[0]
    assert change["fitted"] is False and change["variant_value"] == "4k7"
    alternate = by_kind(model, "alternate_record_not_found")[0]
    assert alternate["designator"] == "R1" and alternate["design_item_id"] == "RES-0404"


def test_a_workbook_library_end_to_end_without_a_driver(tmp_path, monkeypatch):
    monkeypatch.setattr(dq, "runner", lambda payload, timeout: pytest.fail("no process"))
    folder = tmp_path / "Board"
    folder.mkdir()
    (folder / "Parts.xlsx").write_bytes(xlsx({"Resistors": [
        ["Part Number", "Value", "Library Ref", "Footprint Ref"],
        ["RES-0001", "10k", "RES", "R0603"], ["RES-0002", "10k", "RES", "R0603"],
        ["RES-0004", "4k7", "RES", "R0402"]]}))
    tables = "[Table1]\nTableName=Resistors$\nEnabled=True\nUserWhere=0\n"
    maps = "".join([field_map(1, "Resistors$", "Part Number", "Part Number", key=True),
                    field_map(2, "Resistors$", "Value", "Value"),
                    field_map(3, "Resistors$", "Library Ref", "[Library Ref]"),
                    field_map(4, "Resistors$", "Footprint Ref", "[Footprint Ref]")])
    source = ("Provider=Microsoft.ACE.OLEDB.12.0;Data Source=Parts.xlsx;"
              'Extended Properties="Excel 12.0 Xml;HDR=YES;IMEX=1"')
    prj = write_project(folder, {"Book.DbLib": dblib_text(source, tables=tables, maps=maps)},
                        documents=["Book.DbLib"], database=False)
    parts = {"R1": component(library="Book.DbLib", table="Resistors$", design_item="RES-0001",
                             params={"part number": "RES-0001", "value": "10k"}),
             "R2": component(library="Book.DbLib", table="Resistors$", design_item="RES-0004",
                             params={"part number": "RES-0004", "value": "4.7k"})}
    model = check(prj, parts)
    assert model["libraries"][0]["status"] == "queried"
    assert model["libraries"][0]["connection"]["kind"] == "excel"
    assert [(f["designator"], f["kind"]) for f in model["findings"]] == [
        ("R2", "footprint_differs"), ("R2", "would_change")]
    part = _part(prj, parts, designator="R1")
    assert [row["Part Number"] for row in part["alternates"]["rows"]] == ["RES-0002"]


def test_library_and_table_filters(tmp_path, fake_db):
    folder = tmp_path / "Board"
    prj = write_project(folder, {"Parts.DbLib": dblib_text(access_source(folder)),
                                 "Other.DbLink": dblib_text("", tables="", maps="")},
                        documents=["Parts.DbLib", "Other.DbLink"])
    only = check(prj, standard_parts(), table="Capacitors")
    assert {f.get("table") for f in only["findings"] if f.get("table")} == {"Capacitors"}
    narrowed = check(prj, standard_parts(), library="Other")
    assert [row["name"] for row in narrowed["libraries"]] == ["Other.DbLink"]
    assert check(prj, standard_parts(), library="Nope.DbLib")["error"] == "library_not_found"


def test_no_database_libraries(tmp_path, fake_db):
    prj = write_project(tmp_path / "Board", {}, database=False)
    model = check(prj, {"R1": component(library="Res.SchLib", table="")})
    assert model["libraries"] == [] and model["findings"] == []
    assert "uses no database library" in model["note"]


# ---------------------------------------------------------------------------
# One part
# ---------------------------------------------------------------------------

def _part(prj, parts, **extra):
    model = dp.build_part(dp.PartInputs(prj, netlist_of(parts), installed=[], **extra))
    if "error" not in model:
        dp.add_alternates(model, extra.get("alternates_by"), extra.get("allow_network", False))
    return model


def test_get_db_part_record_alternates_and_references(tmp_path, fake_db):
    folder = tmp_path / "Board"
    prj = write_project(folder, {"Parts.DbLib": dblib_text(access_source(folder))},
                        documents=["Parts.DbLib"])
    model = _part(prj, standard_parts(), designator="r1")
    assert model["query"] == {"designator": "R1"} and model["records_found"] == 1
    match = model["matches"][0]
    assert match["confidence"] == "exact" and match["linked_by"] == "source library name"
    fields = {row["field"]: row for row in match["fields"]}
    assert fields["Part Number"]["status"] == "key"
    assert fields["Value"] == {"field": "Value", "parameter": "Value", "database": "10k",
                               "schematic": "10k", "options": {
                                   "add": "if_not_blank", "remove": "never", "update": "always",
                                   "visible_on_add": False},
                               "status": "same"}
    assert fields["Library Ref"]["status"] == "same" and "Notes" in match["unmapped_fields"]
    assert match["symbol"] == {"database": "RES", "placed": "RES", "same": True}
    assert match["footprint"]["database"] == ["R0603"] and match["footprint"]["same"]
    alternates = model["alternates"]
    assert [row["Part Number"] for row in alternates["rows"]] == ["RES-0002", "RES-0003"]
    assert [term["parameter"] for term in alternates["by"]] == ["Value", "[Footprint Ref]"]
    by_tolerance = _part(prj, standard_parts(), designator="R1",
                         alternates_by=["Value", "Tolerance"])
    assert [row["Part Number"] for row in by_tolerance["alternates"]["rows"]] == ["RES-0002"]
    unmapped = _part(prj, standard_parts(), designator="R1", alternates_by=["Nothing"])
    assert "unavailable" in unmapped["alternates"]


def test_get_db_part_by_design_item_and_key(tmp_path, fake_db):
    folder = tmp_path / "Board"
    prj = write_project(folder, {"Parts.DbLib": dblib_text(access_source(folder))},
                        documents=["Parts.DbLib"])
    by_item = _part(prj, standard_parts(), design_item="cap-0002")
    assert by_item["part"]["designator"] == "C1" and by_item["records_found"] == 1
    assert by_item["matches"][0]["matched_by"]["by"] == "where"
    by_key = _part(prj, standard_parts(), key="RES-0005")
    assert by_key["records_found"] == 2 and by_key["duplicates"] is True
    assert {match["table"] for match in by_key["matches"]} == {"Resistors"}
    in_where_table = _part(prj, standard_parts(), key="CAP-0001", table="Capacitors")
    assert in_where_table["matches"][0]["matched_by"]["values"] == {"Part Number": "CAP-0001"}
    nowhere = _part(prj, standard_parts(), key="NOPE-1")
    assert nowhere["records_found"] == 0 and nowhere["matches"] == []
    unknown_item = _part(prj, standard_parts(), design_item="RES-0003")
    assert unknown_item["query"]["key"] == "RES-0003" and unknown_item["records_found"] == 1


def test_get_db_part_says_why_a_part_is_not_linked(tmp_path, fake_db):
    folder = tmp_path / "Board"
    prj = write_project(folder, {"Parts.DbLib": dblib_text(access_source(folder))},
                        documents=["Parts.DbLib"])
    parts = {**standard_parts(), "U9": component(library="Gone.DbLib", params={})}
    plain = _part(prj, parts, designator="J1")
    assert "not placed from a database library" in plain["not_linked"]
    gone = _part(prj, parts, designator="U9")
    assert "Gone.DbLib" in gone["not_linked"]
    missing = _part(prj, parts, designator="R4")
    assert missing.get("record_not_found") is True
    assert _part(prj, parts, designator="Q9")["error"] == "part_not_found"
    assert _part(prj, parts, key="  ")["error"] == "nothing_to_look_up"


def test_page_part_trims_alternates_and_unmapped_fields():
    model = {"query": {}, "matches": [{"unmapped_fields": {f"F{i}": i for i in range(10)},
                                       "_link": object(), "position": 0}],
             "alternates": {"rows": [{"a": i} for i in range(8)], "more": False}}
    page = dp.page_part(model, 3, 4)
    assert len(page["alternates"]["rows"]) == 3 and page["alternates"]["more"] is True
    assert len(page["matches"][0]["unmapped_fields"]) == 4
    assert page["matches"][0]["unmapped_fields_total"] == 10
    assert "_link" not in page["matches"][0] and "position" not in page["matches"][0]
    assert len(model["alternates"]["rows"]) == 8  # the model keeps everything


# ---------------------------------------------------------------------------
# The MCP tools
# ---------------------------------------------------------------------------

@pytest.fixture
def db_project(monkeypatch, tmp_path, fake_db):
    folder = tmp_path / "Board"
    prj = write_project(folder, {"Parts.DbLib": dblib_text(access_source(folder, SECRET))},
                        documents=["Parts.DbLib"])
    project = {"name": "Board", "root_dir": str(folder), "prj_pcb_path": prj, "sheets": [],
               "pcb_doc_paths": [], "parameters": {}}
    monkeypatch.setattr(main_mod, "_project", project)
    monkeypatch.setattr(main_mod._altium, "_netlist", netlist_of(standard_parts()))
    monkeypatch.setattr(main_mod, "_variant_state", VariantState([]))
    monkeypatch.setattr(main_mod, "_installed_library_paths", [])
    return fake_db


def test_check_db_parameters_tool(db_project):
    result = json.loads(main_mod.check_db_parameters())
    assert result["summary"]["records_found"] == 5 and result["allow_network"] is False
    assert result["findings_total"] == len(result["findings"]) and result["has_more"] is False
    assert "read-only" in result["security"]
    text = main_mod.check_db_parameters()
    assert SECRET not in text and "****" in text
    paged = json.loads(main_mod.check_db_parameters(offset=3))
    assert paged["offset"] == 3 and paged["findings"] == result["findings"][3:]
    requests = len(db_project.requests)
    json.loads(main_mod.check_db_parameters(offset=1))
    assert len(db_project.requests) == requests  # pages read one cached model
    assert json.loads(main_mod.check_db_parameters(variant="Nope"))["error"] == \
        "variant_not_found"


def test_check_db_parameters_shrinks(db_project, monkeypatch):
    full = json.loads(main_mod.check_db_parameters())
    monkeypatch.setattr(main_mod, "QUERY_NET_MAX_RESULT_SIZE_CHARS",
                        len(json.dumps(full, indent=2)) - 200)
    dp.clear_cache()
    small = json.loads(main_mod.check_db_parameters())
    assert small["findings_limited_to"] < 100 and small["has_more"] is True
    assert small["next_offset"] == small["findings_limited_to"]
    monkeypatch.setattr(main_mod, "QUERY_NET_MAX_RESULT_SIZE_CHARS", 500)
    brief = json.loads(main_mod.check_db_parameters())
    assert "findings_omitted" in brief and brief["findings_total"] == full["findings_total"]


def test_get_db_part_tool(db_project, monkeypatch):
    result = json.loads(main_mod.get_db_part("C2"))
    assert result["matches_total"] == 1 and result["matches"][0]["table"] == "Capacitors"
    assert result["libraries"][0]["connection"]["credentials"] is True
    assert SECRET not in main_mod.get_db_part("R1")
    assert json.loads(main_mod.get_db_part())["error"] == "nothing_to_look_up"
    assert json.loads(main_mod.get_db_part("R1", alternates_by="Value"))["error"] == \
        "invalid_alternates_by"
    assert json.loads(main_mod.get_db_part("Q9"))["error"] == "part_not_found"
    assert json.loads(main_mod.get_db_part("R1", library="Nope"))["error"] == "library_not_found"
    key = json.loads(main_mod.get_db_part(key="RES-0005"))
    assert key["matches_total"] == 2
    second = json.loads(main_mod.get_db_part(key="RES-0005", offset=1))
    assert len(second["matches"]) == 1 and second["has_more"] is False
    full = json.loads(main_mod.get_db_part("R1"))
    monkeypatch.setattr(main_mod, "QUERY_NET_MAX_RESULT_SIZE_CHARS",
                        len(json.dumps(full, indent=2)) - 50)
    small = json.loads(main_mod.get_db_part("R1"))
    assert small["limited_to"]["alternates"] < 10
    assert len(small["alternates"]["rows"]) < len(full["alternates"]["rows"])
    monkeypatch.setattr(main_mod, "QUERY_NET_MAX_RESULT_SIZE_CHARS", 300)
    assert "matches_omitted" in json.loads(main_mod.get_db_part("R1"))


def test_tool_failures_never_carry_the_message(db_project, monkeypatch):
    def explode(*args, **kwargs):
        raise RuntimeError(f"Provider=X;Password={SECRET}")

    monkeypatch.setattr(main_mod.db_parts, "build_part", explode)
    monkeypatch.setattr(main_mod.db_parts, "build_check", explode)
    dp.clear_cache()
    for text in (main_mod.get_db_part("R1"), main_mod.check_db_parameters()):
        result = json.loads(text)
        assert result["error"] == "database_lookup_failed" and SECRET not in text
        assert "RuntimeError" in result["message"]


def test_tools_need_a_project(monkeypatch):
    monkeypatch.setattr(main_mod, "_project", None)
    assert json.loads(main_mod.get_db_part("R1"))["error"] == "no_project"
    assert json.loads(main_mod.check_db_parameters())["error"] == "no_project"


def test_instructions_describe_the_tools_after_the_cut():
    position = main_mod.SERVER_INSTRUCTIONS.index("## Database Libraries")
    assert position > 4096
    section = main_mod.SERVER_INSTRUCTIONS[position:position + 2000]
    assert "get_db_part" in section and "check_db_parameters" in section
    assert "allow_network=true" in section


def test_tools_are_registered_read_only():
    import asyncio

    tools = {tool.name: tool for tool in asyncio.run(main_mod.mcp.list_tools())}
    for name in ("get_db_part", "check_db_parameters"):
        assert tools[name].annotations.readOnlyHint is True
        assert name in main_mod.NETLIST_TOOLS
    manifest = json.loads((Path(__file__).parents[1] / "manifest.json").read_text(encoding="utf-8"))
    assert {"get_db_part", "check_db_parameters"} <= {tool["name"] for tool in manifest["tools"]}


# ---------------------------------------------------------------------------
# Review fixes: parsing and masking
# ---------------------------------------------------------------------------

def test_ini_lines_end_at_cr_or_lf_and_a_repeated_section_is_read_once():
    text = ("[Table1]\r\nTableName=A\u2028B\x0cC\r\nUserWhereText=[X] = '1'\u0085OR 1\r"
            "[table1]\nTableName=Second\n[Table2]\nTableName=T2\n")
    sections = dl.read_ini(text)
    assert [name for name, _ in sections] == ["Table1", "Table2"]
    assert sections[0][1] == [("TableName", "A\u2028B\x0cC"),
                              ("UserWhereText", "[X] = '1'\u0085OR 1")]


@pytest.mark.parametrize("text", [
    'Provider=Microsoft.ACE.OLEDB.12.0;Data Source=x.xls;Extended Properties="Excel 8.0;'
    f'UID={USER};PWD={SECRET}"',
    f"Provider=SQLOLEDB;Persist Security Info;User ID={USER};Data Source=h",
    f"Provider=SQLOLEDB;Password=ab;{SECRET};User ID={USER};Data Source=h",
    f"Provider=SQLOLEDB;Passwd={SECRET};Data Source=h",
    f"Provider=Microsoft.ACE.OLEDB.12.0;Data Source=https://{USER}:{SECRET}@host/db.accdb",
    f"Provider=OraOLEDB.Oracle;Data Source={USER}/{SECRET}@orahost",
    f"Driver={{SQL Server}};Server=h;Login={USER};AccessKey={SECRET}",
])
def test_masking_covers_unusual_forms(text):
    info = dl.parse_connection(text)
    shown = json.dumps(info.describe()) + repr(info) + info.masked + info.extended
    assert SECRET not in shown and USER not in shown
    assert info.credentials


def test_masking_keeps_isam_names_and_masks_svn_user_keys(tmp_path):
    info = dl.parse_connection('Provider=Microsoft.ACE.OLEDB.12.0;Data Source=B.xls;'
                               'Extended Properties="Excel 8.0;HDR=YES"')
    assert info.extended == "Excel 8.0;HDR=YES" and not info.credentials
    path = tmp_path / "Repo.SVNDbLib"
    path.write_text(dblib_text("", f"SVNUserName={USER}\nRepositoryLogin={USER}\n"
                               f"SVNRepositoryURL=https://{USER}@svnhost/libs\n")
                    + f"[Repository]\nLogin={USER}\nURL=svn://{USER}:{SECRET}@svnhost\n",
                    encoding="utf-8")
    described = json.dumps(dl.parse_db_lib(str(path)).describe())
    assert USER not in described and SECRET not in described and "svnhost" in described


def test_a_udl_off_this_pc_is_not_read_unless_allowed(tmp_path, monkeypatch):
    calls = []

    def recorder(path):
        calls.append(path)
        return "Provider=Microsoft.ACE.OLEDB.12.0;Data Source=Parts.mdb"

    monkeypatch.setattr(sys.modules[dp.parse_db_lib.__module__], "read_udl", recorder)
    prj = write_project(tmp_path / "Board", {
        "U.DbLib": dblib_text("File Name=\\\\srv\\share\\Parts.udl")}, documents=["U.DbLib"])
    dp.clear_cache()
    model = check(prj, {"R1": component(library="U.DbLib")})
    assert calls == []
    row = model["libraries"][0]
    assert row["connection"]["error"] == "udl_not_local"
    assert [f["reason"] for f in by_kind(model, "library_skipped")] == [] or \
        row["data_source"] == dq.SKIP_SHARE
    check(prj, {"R1": component(library="U.DbLib")}, allow_network=True)
    assert len(calls) == 1


def test_onedrive_sync_roots_and_cloud_files(monkeypatch, tmp_path):
    site = str(tmp_path / "Org Name" / "Site - Documents")
    monkeypatch.setattr(dq, "_SYNC_ROOTS", [os.path.normcase(os.path.normpath(site))])
    assert dq.path_location(os.path.join(site, "Parts.accdb")) == "onedrive"
    assert dq.path_location(str(tmp_path / "Other" / "Parts.accdb")) == "local"
    monkeypatch.setattr(dq, "_cloud_file", lambda path: True)
    assert dq.path_location(str(tmp_path / "Other" / "Parts.accdb")) == "onedrive"
    assert dq.path_location("https://host/Parts.accdb") == "share"


def test_every_path_in_the_connection_is_checked(tmp_path):
    (tmp_path / "P.accdb").write_bytes(b"x")
    path = tmp_path / "S.DbLib"
    path.write_text(dblib_text(f"Provider=Microsoft.ACE.OLEDB.12.0;Data Source={tmp_path / 'P.accdb'};"
                               "Jet OLEDB:System database=\\\\srv\\share\\secured.mdw"),
                    encoding="utf-8")
    library = dl.parse_db_lib(str(path))
    assert dq.connection_paths(library.connection)[-1] == "\\\\srv\\share\\secured.mdw"
    assert dq.plan_source(library) == (None, dq.SKIP_SHARE)
    allowed, _ = dq.plan_source(library, allow_network=True)
    assert allowed is not None and not allowed.local_only


def test_local_access_sources_query_only_their_own_tables(tmp_path, monkeypatch):
    (tmp_path / "P.accdb").write_bytes(b"x")
    (tmp_path / "B.xls").write_bytes(b"x")

    def library(source: str) -> dl.DatabaseLibrary:
        path = tmp_path / "L.DbLib"
        path.write_text(dblib_text(source), encoding="utf-8")
        return dl.parse_db_lib(str(path))

    access = library(f"Provider=Microsoft.ACE.OLEDB.12.0;Data Source={tmp_path / 'P.accdb'}")
    local = lambda path: "local"
    plan = dq.plan_source(access, location_of=local)[0]
    assert plan.local_only and plan.base_tables_only
    assert dq.plan_source(access, True, location_of=local)[0].local_only is False
    excel = library(f"Provider=Microsoft.ACE.OLEDB.12.0;Data Source={tmp_path / 'B.xls'}")
    excel_plan = dq.plan_source(excel, location_of=local)[0]
    assert excel_plan.local_only and not excel_plan.base_tables_only
    db = FakeDb()
    monkeypatch.setattr(dq, "runner", db)
    first, elsewhere, last = dq.run_queries(plan, [
        dq.Query("Resistors"), dq.Query("\\\\srv\\share\\parts.csv"), dq.Query("Capacitors")])
    assert db.requests[0]["local_only"] is True
    assert [query["table"] for query in db.requests[0]["queries"]] == ["Resistors", "Capacitors"]
    assert elsewhere.error == "not_local_table" and first.rows and last.rows
    dq.run_queries(excel_plan, [dq.Query("Resistors")])
    assert db.requests[1]["local_only"] is False


def test_odbc_strings_are_not_quoted_the_ole_db_way():
    assert dl.join_odbc_string([("DBQ", "C:\\O'Folder\\P.accdb"), ("ReadOnly", "1")]) == \
        "DBQ=C:\\O'Folder\\P.accdb;ReadOnly=1"
    assert dl.join_odbc_string([("PWD", "a;b}c"), ("Driver", "{X}")]) == "PWD={a;b}}c};Driver={X}"
    info = dl.parse_connection('Provider=Microsoft.ACE.OLEDB.12.0;Data Source="C:\\O\'F\\P.accdb"')
    odbc = [text for kind, text in dq.connection_attempts(info) if kind == "odbc"]
    assert odbc == [f"Driver={{Microsoft Access Driver (*.mdb, *.accdb)}};DBQ={info.file};"
                    "ReadOnly=1"] and "'" in info.file


def test_text_sources_are_text_with_ace_fallbacks(tmp_path):
    odbc = dl.parse_connection(f"Driver={{Microsoft Access Text Driver (*.txt, *.csv)}};"
                               f"Dbq={tmp_path}")
    assert odbc.kind == "text" and not odbc.network and odbc.file == str(tmp_path)
    assert [kind for kind, _ in dq.connection_attempts(odbc)] == ["odbc", "oledb", "oledb"]
    jet = dl.parse_connection(f'Provider=Microsoft.Jet.OLEDB.4.0;Data Source={tmp_path};'
                              'Extended Properties="text;HDR=Yes;FMT=Delimited"')
    attempts = dq.connection_attempts(jet)
    assert [kind for kind, _ in attempts] == ["oledb", "oledb", "oledb", "odbc"]
    assert "Microsoft.ACE.OLEDB.16.0" in attempts[1][1] and "text;HDR=Yes" in attempts[1][1]
    assert "Access Text Driver" in attempts[3][1]


def test_double_quoted_strings_for_access_sources(tmp_path, fake_db):
    tree = dl.parse_where('[Part Number] = "{Part Number}"', double_quoted_strings=True)
    assert tree[3].kind == "string" and tree[3].placeholders == ["Part Number"]
    with pytest.raises(dl.WhereError):
        dl.parse_where('[Part Number] = "{Part Number}"')  # a name elsewhere
    folder = tmp_path / "Board"
    tables = ("[Table1]\nTableName=Capacitors\nEnabled=True\nUserWhere=1\n"
              'UserWhereText=[Part Number] = "{Part Number}"\n')
    maps = field_map(1, "Capacitors", "Value", "Value")
    prj = write_project(folder, {"P.DbLib": dblib_text(access_source(folder), tables=tables,
                                                       maps=maps)}, documents=["P.DbLib"])
    model = check(prj, {"C1": component("CAP", "CAP-0002", "Capacitors", "C0603", library="P.DbLib",
                                        params={"part number": "CAP-0002", "value": "2uF"})})
    assert [(f["kind"], f["database"]) for f in model["findings"]] == [("would_change", "1uF")]


@pytest.mark.parametrize("depth,ok", [(32, True), (33, False), (3000, False)])
def test_where_nesting_is_limited_like_the_script(depth, ok):
    text = "NOT " * depth + "[A] = '1'"
    if ok:
        tree = dl.parse_where(text)
        assert dl.where_depth(tree) == depth and dq.check_where(dp.bind_where(tree, {}))
    else:
        with pytest.raises(dl.WhereError):
            dl.parse_where(text)
    with pytest.raises(dl.WhereError):
        dl.parse_where("(" * 3000 + "[A] = '1'" + ")" * 3000)
    node = {"cmp": ["A", "=", "1"]}
    for _ in range(33):
        node = {"not": node}
    assert not dq.check_where(node)


def test_xlsx_values_read_as_the_excel_driver_returns_them(tmp_path):
    from server.parsers import fab_outputs

    book = xlsx({"Parts": [["Code", "When", "Small", "Space"], ["A", 45293, 1e-07, " "],
                           ["B", 45355.2125810185, 0.25, ""]]})
    styles = ('<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
              '<cellXfs><xf numFmtId="0"/><xf numFmtId="14"/></cellXfs></styleSheet>')
    data = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(book)) as source, zipfile.ZipFile(data, "w") as target:
        for item in source.infolist():
            content = source.read(item)
            if item.filename == "xl/worksheets/sheet1.xml":
                content = content.replace(b'<c r="B2">', b'<c r="B2" s="1">').replace(
                    b'<c r="B3">', b'<c r="B3" s="1">')
            target.writestr(item, content)
        target.writestr("xl/styles.xml", styles)
    rows = dict(fab_outputs.xlsx_sheets(data.getvalue(), driver_like=True))["Parts"]
    assert rows[1][1:3] == ["2024-01-02T00:00:00", "1E-07"]
    assert rows[2][1] == "2024-03-04T05:06:07"
    assert dict(fab_outputs.xlsx_sheets(data.getvalue()))["Parts"][1][1] == "45293"
    (tmp_path / "B.xlsx").write_bytes(data.getvalue())
    plan = dq.SourcePlan("xlsx", path=str(tmp_path / "B.xlsx"))
    spaced, blank, trailing = dq.run_queries(plan, [
        dq.Query("Parts$", {"cmp": ["Space", "=", " "]}),
        dq.Query("Parts$", {"null": ["Space", False]}),
        dq.Query("Parts$", {"cmp": ["Code", "=", "A  "]})])
    assert [row[0] for row in spaced.rows] == ["A"] and [row[0] for row in blank.rows] == ["B"]
    assert [row[0] for row in trailing.rows] == ["A"]


# ---------------------------------------------------------------------------
# Review fixes: links and drift
# ---------------------------------------------------------------------------

def test_lookup_problems_are_reported_without_a_query(tmp_path, fake_db):
    folder = tmp_path / "Board"
    tables = ("[Table1]\nTableName=Resistors\nEnabled=True\nUserWhere=1\n"
              "UserWhereText=[Part Number] IN ('{Part Number}')\n")
    maps = field_map(1, "Resistors", "Value", "Value")
    prj = write_project(folder, {"P.DbLib": dblib_text(access_source(folder), tables=tables,
                                                       maps=maps),
                                 "L.DbLink": dblib_text(access_source(folder), tables=tables,
                                                        maps=maps)},
                        documents=["P.DbLib", "L.DbLink"])
    parts = {"R1": component(library="P.DbLib", params={"part number": "RES-0001"}),
             "J1": component(library="J.SchLib", table="", params={"part number": "RES-0001"}),
             "J2": component(library="J.SchLib", table="", params={"part number": "RES-0002"})}
    model = check(prj, parts)
    assert fake_db.requests == []
    unsupported = by_kind(model, "where_unsupported")
    assert {(f.get("designator"), f["library"]) for f in unsupported} == {
        ("R1", "P.DbLib"), (None, "L.DbLink")}
    table_level = next(f for f in unsupported if f["library"] == "L.DbLink")
    assert table_level["parts"] == ["J1", "J2"] and table_level["part_count"] == 2


def test_an_unsafe_name_skips_only_its_own_lookups(tmp_path, fake_db):
    folder = tmp_path / "Board"
    bad = "Parts]; DELETE FROM [Parts"
    tables = (f"[Table1]\nTableName={bad}\nEnabled=True\nUserWhere=0\n"
              "[Table2]\nTableName=Resistors\nEnabled=True\nUserWhere=0\n")
    maps = (field_map(1, bad, "Code", "Part Number", key=True)
            + field_map(2, "Resistors", "Part Number", "Part Number", key=True)
            + field_map(3, "Resistors", "Value", "Value"))
    prj = write_project(folder, {"C.DbLib": dblib_text(access_source(folder), tables=tables,
                                                       maps=maps)}, documents=["C.DbLib"])
    model = check(prj, {
        "R1": component(library="C.DbLib", table=bad, design_item="X", params={"part number": "X"}),
        "R2": component(library="C.DbLib", design_item="RES-0001",
                        params={"part number": "RES-0001", "value": "1k"})})
    assert model["libraries"][0]["status"] == "queried"
    assert [f["designator"] for f in by_kind(model, "bad_name")] == ["R1"]
    assert [f["designator"] for f in by_kind(model, "would_change")] == ["R2"]
    assert all(bad not in json.dumps(request) for request in fake_db.requests)


def test_duplicates_say_when_the_read_was_cut_and_are_no_alternates(tmp_path, fake_db):
    fake_db.tables = {"Resistors": [resistor("RES-0007", "1k", "R0603", maker=f"M{index}")
                                    for index in range(7)] + [resistor("RES-0008", "1k", "R0603")]}
    folder = tmp_path / "Board"
    prj = write_project(folder, {"Parts.DbLib": dblib_text(access_source(folder))},
                        documents=["Parts.DbLib"])
    parts = {"R1": component(design_item="RES-0007", params={"part number": "RES-0007"})}
    duplicate = by_kind(check(prj, parts), "duplicate_records")[0]
    assert duplicate["records"] == dp.LOOKUP_LIMIT and duplicate["more_records"] is True
    model = _part(prj, parts, designator="R1")
    assert model["records_truncated"] is True
    assert {match["confidence"] for match in model["matches"]} == {"ambiguous"}
    assert [row["Part Number"] for row in model["alternates"]["rows"]] == ["RES-0008"]


def test_blank_parameters_count_as_present():
    parts = components_from_stream(_stream(
        "|RECORD=1|LibReference=RES|UniqueID=AAAAAAAA|SourceLibraryName=Parts.DbLib",
        "|RECORD=34|OwnerIndex=0|Text=R1|Name=Designator",
        "|RECORD=41|OwnerIndex=0|Name=Manufacturer",
        "|RECORD=41|OwnerIndex=0|Name=Value|Text=10k"))
    r1 = parts["R1"]
    assert "manufacturer" not in r1.params and r1.blank_params == ["manufacturer"]
    from server.altium import _make_enriched
    assert _make_enriched(r1, {})["blank_params"] == ["manufacturer"]
    netlist = netlist_of({"R1": component(params={"value": "10k"}, blank_params=["manufacturer"])})
    part = dp.make_part("R1", netlist)
    assert part.params["manufacturer"] == "" and part.value("Manufacturer") == ""


def test_blank_parameters_change_instead_of_being_added(tmp_path, fake_db):
    folder = tmp_path / "Board"
    maps = "".join([field_map(1, "Parts", "Code", "Code", key=True),
                    field_map(2, "Parts", "Maker", "Manufacturer", update=2),
                    field_map(3, "Parts", "Power", "Power", add=1, update=2),
                    field_map(4, "Parts", "Extra", "Extra", add=2)])
    tables = "[Table1]\nTableName=Parts\nEnabled=True\nUserWhere=0\n"
    prj = write_project(folder, {"P.DbLib": dblib_text(access_source(folder), tables=tables,
                                                       maps=maps)}, documents=["P.DbLib"])
    fake_db.tables = {"Parts": [{"Code": "X1", "Maker": "Acme", "Power": "0.1W", "Extra": None}]}
    model = check(prj, {"U1": component(lib_ref="P", design_item="X1", table="Parts", footprint="",
                                        library="P.DbLib", params={"code": "X1"},
                                        blank_params=["manufacturer", "power", "extra"])})
    kinds = sorted((f["kind"], f.get("parameter")) for f in model["findings"])
    assert kinds == [("would_change", "Manufacturer"), ("would_change", "Power")]


def test_library_paths_are_compared_only_for_library_files(tmp_path, fake_db):
    folder = tmp_path / "Board"
    maps = (field_map(1, "Resistors", "Part Number", "Part Number", key=True)
            + field_map(2, "Resistors", "Library Ref", "[Library Path]"))
    tables = "[Table1]\nTableName=Resistors\nEnabled=True\nUserWhere=0\n"
    prj = write_project(folder, {"Parts.DbLib": dblib_text(access_source(folder), tables=tables,
                                                           maps=maps)}, documents=["Parts.DbLib"])
    parts = {name: component(design_item="RES-0001", params={"part number": "RES-0001"},
                             library_path=path)
             for name, path in (("R1", "*"), ("R2", str(folder / "Parts.DbLib")), ("R3", ""),
                                ("R4", "C:\\Libs\\Other.SchLib"), ("R5", "Libs\\RES.SchLib"))}
    model = check(prj, parts)
    assert [f["designator"] for f in by_kind(model, "library_path_differs")] == ["R4", "R5"]
    fields = {row["field"]: row for row in _part(prj, parts, designator="R1")["matches"][0]["fields"]}
    assert fields["Library Ref"]["status"] == "not_recorded"


def test_the_key_parameter_is_compared_when_the_design_item_id_found_the_record(tmp_path, fake_db):
    folder = tmp_path / "Board"
    prj = write_project(folder, {"Parts.DbLib": dblib_text(access_source(folder))},
                        documents=["Parts.DbLib"])
    parts = {"R1": component(design_item="RES-0002", params={"part number": "RES-0099"}),
             "R2": component(design_item="RES-0002", params={}),
             "R3": component(design_item="RES-0002", params={"part number": "RES-0001"}),
             "R4": component(design_item="RES-0002", params={"part number": "RES-0002"})}
    model = check(prj, parts)
    key = {(f["designator"], f["kind"]): f for f in model["findings"]
           if f.get("parameter") == "Part Number"}
    assert key[("R1", "would_change")]["database"] == "RES-0002"
    assert ("R2", "would_add") in key and not any(name == "R4" for name, _ in key)
    conflict = by_kind(model, "key_conflict")
    assert [(f["designator"], f["key_parameter_value"], f["design_item_id"]) for f in conflict] == [
        ("R3", "RES-0001", "RES-0002")]
    confidence = {name: _part(prj, parts, designator=name)["matches"][0]["confidence"]
                  for name in parts}
    assert confidence == {"R1": "design_item_id", "R2": "design_item_id", "R3": "ambiguous",
                          "R4": "exact"}


def test_footprint_fields_are_taken_in_number_order(tmp_path, fake_db):
    fake_db.tables = {"Inductors": [{"Code": "L1", "FP2": "L0805", "FP1": "L0603"}]}
    folder = tmp_path / "Board"
    maps = (field_map(1, "Inductors", "Code", "Code", key=True)
            + field_map(2, "Inductors", "FP2", "[Footprint Ref 2]")
            + field_map(3, "Inductors", "FP1", "[Footprint Ref]"))
    tables = "[Table1]\nTableName=Inductors\nEnabled=True\nUserWhere=0\n"
    prj = write_project(folder, {"I.DbLib": dblib_text(access_source(folder), tables=tables,
                                                       maps=maps)}, documents=["I.DbLib"])
    parts = {"L1": component(library="I.DbLib", table="Inductors", design_item="L1",
                             footprint="L0603", params={"code": "L1"})}
    assert not by_kind(check(prj, parts), "footprint_differs")
    match = _part(prj, parts, designator="L1")["matches"][0]
    rows = {row["field"]: row["status"] for row in match["fields"]}
    assert rows == {"Code": "key", "FP2": "not_on_part", "FP1": "same"}
    assert match["drift"] == [] and match["footprint"]["same"] is True


def test_database_link_table_scope_follows_use_db_table_name(tmp_path, fake_db):
    fake_db.tables = {"First": [{"Part Number": "CON-0001", "Value": "a"}],
                      "Second": [{"Part Number": "CON-0001", "Value": "b"},
                                 {"Part Number": "CON-0002", "Value": "c"}]}
    folder = tmp_path / "Board"
    tables = ("[Table1]\nTableName=First\nEnabled=True\nUserWhere=0\n"
              "[Table2]\nTableName=Second\nEnabled=True\nUserWhere=0\n")
    maps = "".join(field_map(number, table, name, name, key=name == "Part Number")
                   for number, (table, name) in enumerate(
                       [("First", "Part Number"), ("First", "Value"),
                        ("Second", "Part Number"), ("Second", "Value")], 1))
    prj = write_project(folder, {"Two.DbLink": dblib_text(access_source(folder), tables=tables,
                                                          maps=maps)}, documents=["Two.DbLink"])
    parts = {"J1": component(library="C.SchLib", table="", params={"part number": "CON-0001",
                                                                    "value": "a"}),
             "J2": component(library="C.SchLib", table="First", use_db_table_name=False,
                             params={"part number": "CON-0002", "value": "c"}),
             "J3": component(library="C.SchLib", table="First", use_db_table_name=True,
                             params={"part number": "CON-0002"}),
             "J4": component(library="C.SchLib", table="Other", use_db_table_name=True,
                             params={"part number": "CON-0002"})}
    model = check(prj, parts)
    assert not by_kind(model, "duplicate_records")
    several = by_kind(model, "found_in_several_tables")
    assert [(f["designator"], f["table"], f["also_in"]) for f in several] == [
        ("J1", "First", ["Second"])]
    assert [f["designator"] for f in by_kind(model, "record_not_found")] == ["J3"]
    row = model["libraries"][0]
    assert row["parts_table_not_in_link"] == 1 and row["excluded_parts"] == ["J4"]
    assert row["records_found"] == 2
    assert "restricted to its table 'Other'" in _part(prj, parts, designator="J4")["not_linked"]
    j1 = _part(prj, parts, designator="J1")
    assert j1["records_found"] == 1 and j1["matches"][0]["also_found_in_tables"] == ["Second"]


def test_a_key_on_a_system_field_links_parts(tmp_path, fake_db):
    fake_db.tables = {"K": [{"Symbol": "CONN4", "Value": "v"}]}
    folder = tmp_path / "Board"
    maps = field_map(1, "K", "Symbol", "[Library Ref]", key=True) + field_map(2, "K", "Value", "Value")
    prj = write_project(folder, {"K.DbLink": dblib_text(access_source(folder),
                                                        tables="[Table1]\nTableName=K\nEnabled=True\n"
                                                               "UserWhere=0\n", maps=maps)},
                        documents=["K.DbLink"])
    model = check(prj, {"J1": component("CONN4", library="C.SchLib", table="", params={})})
    assert model["summary"]["parts_linked"] == 1 and model["summary"]["records_found"] == 1
    assert [(f["kind"], f["parameter"]) for f in model["findings"]] == [("would_add", "Value")]


def test_visible_on_add_and_the_database_link_symbol_wording(tmp_path, fake_db):
    folder = tmp_path / "Board"
    maps = (field_map(1, "Resistors", "Part Number", "Part Number", key=True)
            + field_map(2, "Resistors", "Manufacturer", "Manufacturer", visible=1)
            + field_map(3, "Resistors", "Library Ref", "[Library Ref]"))
    tables = "[Table1]\nTableName=Resistors\nEnabled=True\nUserWhere=0\n"
    prj = write_project(folder, {"P.DbLib": dblib_text(access_source(folder), tables=tables,
                                                       maps=maps),
                                 "P.DbLink": dblib_text(access_source(folder), tables=tables,
                                                        maps=maps)},
                        documents=["P.DbLib", "P.DbLink"])
    parts = {"R1": component(library="P.DbLib", design_item="RES-0001",
                             params={"part number": "RES-0001"}),
             "R2": component("RESX", library="R.SchLib", table="",
                             params={"part number": "RES-0001", "manufacturer": "Acme"})}
    model = check(prj, parts)
    added = by_kind(model, "would_add")[0]
    assert added["designator"] == "R1" and added["visible_on_add"] is True
    symbol = by_kind(model, "symbol_differs")[0]
    assert symbol["designator"] == "R2" and "Update From Libraries" not in symbol["detail"]
    fields = _part(prj, parts, designator="R1")["matches"][0]["fields"]
    assert next(row for row in fields if row["field"] == "Manufacturer")["options"][
        "visible_on_add"] is True


# ---------------------------------------------------------------------------
# Live: query_db.ps1 on an Access database (Windows with the Access provider only)
# ---------------------------------------------------------------------------

def _make_access_database(path: Path) -> bool:
    """Create a small .accdb with ADOX and fill it through OLE DB (test setup only)."""
    if os.name != "nt" or not shutil.which("powershell"):
        return False
    script = path.parent / "make_db.ps1"
    script.write_text(
        "param([string]$Path)\n$ErrorActionPreference='Stop'\n"
        "$cs = \"Provider=Microsoft.ACE.OLEDB.12.0;Data Source=$Path\"\n"
        "$cat = New-Object -ComObject ADOX.Catalog\n[void]$cat.Create($cs)\n"
        "$c = New-Object System.Data.OleDb.OleDbConnection($cs)\n$c.Open()\n"
        "function X($s) { $m = $c.CreateCommand(); $m.CommandText = $s; [void]$m.ExecuteNonQuery() }\n"
        "X 'CREATE TABLE Resistors ([Part Number] TEXT(50), [Value] TEXT(50), [Library Ref] TEXT(50), "
        "[Footprint Ref] TEXT(50), [Power] DOUBLE)'\n"
        "X \"INSERT INTO Resistors VALUES ('RES-0001','10k','RES','R0603',0.1)\"\n"
        "X \"INSERT INTO Resistors VALUES ('RES-0002','10k','RES','R0603',0.25)\"\n"
        "X \"INSERT INTO Resistors VALUES ('RES-0003','4k7','RES','R0402',0.1)\"\n"
        "$c.Close()\n'ok'\n", encoding="utf-8")
    try:
        result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                                 "-ExecutionPolicy", "Bypass", "-File", str(script),
                                 "-Path", str(path)], capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and b"ok" in result.stdout and path.is_file()


@pytest.fixture(scope="module")
def live_database(tmp_path_factory):
    folder = tmp_path_factory.mktemp("live_db")
    database = folder / "Parts.accdb"
    if not _make_access_database(database):
        pytest.skip("Windows PowerShell with the Access OLE DB provider is not available")
    return database


@pytest.mark.skipif(sys.platform != "win32", reason="query_db.ps1 needs Windows PowerShell")
def test_live_query_script(live_database, monkeypatch):
    monkeypatch.setattr(dq, "runner", dq.powershell_runner)
    info = dl.parse_connection(f"Provider=Microsoft.Jet.OLEDB.4.0;Data Source={live_database}")
    plan = dq.SourcePlan("driver", path=str(live_database), attempts=dq.connection_attempts(info))
    exact, numeric, like, missing, field_error = dq.run_queries(plan, [
        dq.Query("Resistors", {"cmp": ["Part Number", "=", "RES-0001"]}),
        dq.Query("Resistors", {"cmp": ["Power", ">", 0.2]}),
        dq.Query("Resistors", {"cmp": ["Value", "like", "10%"]}, limit=1),
        dq.Query("Nope"),
        dq.Query("Resistors", {"cmp": ["Nope", "=", "x"]})])
    assert exact.columns[:2] == ["Part Number", "Value"] and exact.rows[0][:2] == ["RES-0001", "10k"]
    assert exact.rows[0][4] == "0.1"
    assert [row[0] for row in numeric.rows] == ["RES-0002"]
    assert len(like.rows) == 1 and like.truncated
    assert missing.error == "table_not_found" and field_error.error == "field_not_found"
    odbc = dq.SourcePlan("driver", attempts=[a for a in plan.attempts if a[0] == "odbc"])
    via_odbc = dq.run_queries(odbc, [dq.Query("Resistors", {"cmp": ["Value", "=", "4k7"]})])[0]
    assert [row[0] for row in via_odbc.rows] == ["RES-0003"]
    gone = dq.SourcePlan("driver", attempts=[("oledb", "Provider=Nope.Provider.1;Data Source=x")])
    with pytest.raises(dq.QueryError) as error:
        dq.run_queries(gone, [dq.Query("Resistors")])
    assert error.value.code == dq.SKIP_NO_DRIVER


@pytest.mark.skipif(sys.platform != "win32", reason="query_db.ps1 needs Windows PowerShell")
def test_live_check_end_to_end(live_database, tmp_path, monkeypatch):
    monkeypatch.setattr(dq, "runner", dq.powershell_runner)
    folder = tmp_path / "Board"
    folder.mkdir()
    shutil.copy(live_database, folder / "Parts.accdb")
    tables = "[Table1]\nTableName=Resistors\nEnabled=True\nUserWhere=0\n"
    maps = "".join([field_map(1, "Resistors", "Part Number", "Part Number", key=True),
                    field_map(2, "Resistors", "Value", "Value"),
                    field_map(3, "Resistors", "Library Ref", "[Library Ref]"),
                    field_map(4, "Resistors", "Footprint Ref", "[Footprint Ref]")])
    source = "Provider=Microsoft.Jet.OLEDB.4.0;Data Source=Parts.accdb"
    prj = write_project(folder, {"Parts.DbLib": dblib_text(source, tables=tables, maps=maps)},
                        documents=["Parts.DbLib"], database=False)
    parts = {"R1": component(design_item="RES-0001", params={"part number": "RES-0001",
                                                            "value": "22k"}),
             "R2": component(design_item="RES-0404", params={"part number": "RES-0404"})}
    model = check(prj, parts)
    assert model["libraries"][0]["status"] == "queried"
    assert [(f["designator"], f["database"]) for f in by_kind(model, "would_change")] == [
        ("R1", "10k")]
    assert [f["designator"] for f in by_kind(model, "record_not_found")] == ["R2"]
    before = (folder / "Parts.accdb").stat().st_mtime_ns
    part = _part(prj, parts, designator="R1")
    assert [row["Part Number"] for row in part["alternates"]["rows"]] == ["RES-0002"]
    assert (folder / "Parts.accdb").stat().st_mtime_ns == before  # opened read-only


def _link_back_end(front: Path, back: Path) -> bool:
    """Add to front a linked table 'Linked' (ADOX Jet OLEDB:Create Link) on back's
    Resistors and a saved query 'Saved' (test setup only)."""
    script = front.parent / "link_db.ps1"
    script.write_text(
        "param([string]$Front, [string]$Back)\n$ErrorActionPreference='Stop'\n"
        "$cs = \"Provider=Microsoft.ACE.OLEDB.12.0;Data Source=$Front\"\n"
        "$cat = New-Object -ComObject ADOX.Catalog\n$cat.ActiveConnection = $cs\n"
        "$t = New-Object -ComObject ADOX.Table\n$t.Name = 'Linked'\n$t.ParentCatalog = $cat\n"
        "$t.Properties.Item('Jet OLEDB:Create Link').Value = $true\n"
        "$t.Properties.Item('Jet OLEDB:Link Datasource').Value = $Back\n"
        "$t.Properties.Item('Jet OLEDB:Remote Table Name').Value = 'Resistors'\n"
        "$cat.Tables.Append($t)\n$cat.ActiveConnection.Close()\n"
        "$c = New-Object System.Data.OleDb.OleDbConnection($cs)\n$c.Open()\n"
        "$m = $c.CreateCommand(); $m.CommandText = 'CREATE VIEW Saved AS SELECT * FROM Resistors'\n"
        "[void]$m.ExecuteNonQuery()\n$c.Close()\n'ok'\n", encoding="utf-8")
    try:
        result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                                 "-ExecutionPolicy", "Bypass", "-File", str(script),
                                 "-Front", str(front), "-Back", str(back)],
                                capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and b"ok" in result.stdout


@pytest.mark.skipif(sys.platform != "win32", reason="query_db.ps1 needs Windows PowerShell")
def test_live_linked_tables_and_saved_queries_need_allow_network(live_database, tmp_path,
                                                                 monkeypatch):
    monkeypatch.setattr(dq, "runner", dq.powershell_runner)
    folder = tmp_path / "Board"
    folder.mkdir()
    front, back = folder / "Front.accdb", tmp_path / "Back.accdb"
    shutil.copy(live_database, front)
    shutil.copy(live_database, back)
    if not _link_back_end(front, back):
        pytest.skip("ADOX could not create a linked table here")
    tables = "".join(f"[Table{index}]\nTableName={name}\nEnabled=True\nUserWhere=0\n"
                     for index, name in enumerate(("Linked", "Saved", "Resistors"), 1))
    maps = "".join(field_map(number, name, field, field, key=field == "Part Number")
                   for number, (name, field) in enumerate(
                       [(table, field) for table in ("Linked", "Saved", "Resistors")
                        for field in ("Part Number", "Value")], 1))
    prj = write_project(folder, {"Front.DbLib": dblib_text(
        "Provider=Microsoft.ACE.OLEDB.12.0;Data Source=Front.accdb", tables=tables, maps=maps)},
        documents=["Front.DbLib"], database=False)
    parts = {name: component(library="Front.DbLib", table=table, design_item="RES-0001",
                             params={"part number": "RES-0001", "value": "10k"})
             for name, table in (("R1", "Linked"), ("R2", "Saved"), ("R3", "Resistors"))}
    model = check(prj, parts)
    failed = {f["designator"]: f["error"] for f in by_kind(model, "query_failed")}
    assert failed == {"R1": "not_local_table", "R2": "not_local_table"}
    assert "allow_network=true" in by_kind(model, "query_failed")[0]["detail"]
    assert model["summary"]["records_found"] == 1
    allowed = check(prj, parts, allow_network=True)
    assert allowed["summary"]["records_found"] == 3 and not by_kind(allowed, "query_failed")
    # An apostrophe in the folder: the ODBC driver gets the path as it is.
    odd = tmp_path / "O'Folder"
    odd.mkdir()
    shutil.copy(live_database, odd / "P.accdb")
    info = dl.parse_connection(f'Provider=Microsoft.ACE.OLEDB.12.0;Data Source="{odd / "P.accdb"}"')
    odbc = dq.SourcePlan("driver", attempts=[a for a in dq.connection_attempts(info)
                                             if a[0] == "odbc"])
    rows = dq.run_queries(odbc, [dq.Query("Resistors", {"cmp": ["Value", "=", "4k7"]})])[0].rows
    assert [row[0] for row in rows] == ["RES-0003"]
