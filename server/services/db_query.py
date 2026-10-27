"""Read-only queries on a database library's data source (roadmap #53).

Nothing here writes. Every query is SELECT * FROM <table> WHERE <comparisons>, built
from a table name and a comparison tree whose names are checked and quoted and whose
values are parameters, never SQL text, with a row limit and a timeout. Two ways in:

- .xlsx and .xlsm workbooks are read in-process with the workbook reader the BOM checks
  use (parsers/fab_outputs.xlsx_sheets): a table 'Sheet1$' is the worksheet Sheet1, its
  first row holds the field names (HDR=YES, as Altium's Excel connections use) and an
  empty header is F<column>, as the Excel driver names it. No driver is needed.
- Everything else goes to server/scripts/query_db.ps1 (Windows PowerShell 5.1 with the
  .NET Framework's System.Data.OleDb and System.Data.Odbc), one process per library with
  all of its queries, the request (connection included) written to its stdin so that it
  never appears on a command line. Files are opened read-only (Mode=Read for OLE DB,
  ReadOnly=1 for the ODBC drivers): the library's own provider first, then the ACE 16
  and 12 providers, then the Access or Excel ODBC driver (the Jet provider old libraries
  name has no 64-bit build).

Network sources (SQL Server, MySQL, PostgreSQL, Oracle, an ODBC data source that is not
a local file, an unknown provider) and files on a network share or in a OneDrive folder
(also a SharePoint or Teams library OneDrive syncs, or a cloud-only file) are never
opened unless allow_network is true: plan_source says why a source is skipped. That
covers every path in the connection (a workgroup file, a Data Link file), not only the
data source, and inside a local Access file only its own tables are read (a linked
table or a saved query, which can read another database or a server, is refused per
query: not_local_table). The connection string is never logged or returned; errors are
fixed codes."""

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

try:
    from parsers.db_lib import (
        EXCEL_SUFFIXES, MAX_WHERE_DEPTH, ConnectionInfo, DatabaseLibrary,
        join_connection_string, join_odbc_string, split_connection_string,
    )
    from parsers.fab_outputs import xlsx_sheets
except ImportError:
    from server.parsers.db_lib import (
        EXCEL_SUFFIXES, MAX_WHERE_DEPTH, ConnectionInfo, DatabaseLibrary,
        join_connection_string, join_odbc_string, split_connection_string,
    )
    from server.parsers.fab_outputs import xlsx_sheets

if getattr(sys, "frozen", False):
    SCRIPTS_DIR = Path(sys._MEIPASS) / "scripts"
else:
    SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
QUERY_SCRIPT = "query_db.ps1"

COMMAND_TIMEOUT = 15  # seconds per SQL command (and ODBC login)
PROCESS_TIMEOUT = 45  # seconds for one library's PowerShell process, plus per query below
PROCESS_TIMEOUT_PER_QUERY = 0.2
PROCESS_TIMEOUT_MAX = 300
MAX_QUERIES = 5000  # per library per call
MAX_WORKBOOK_BYTES = 64 << 20

# Why a data source was not queried.
SKIP_NO_CONNECTION = "no_connection"
SKIP_FILE_MISSING = "file_missing"
SKIP_SHARE = "onedrive_or_share"
SKIP_NETWORK = "network_not_allowed"
SKIP_NO_DRIVER = "no_driver"
SKIP_UNREADABLE = "unreadable"
SKIP_MESSAGES = {
    SKIP_NO_CONNECTION: "the library names no data source (empty connection string)",
    SKIP_FILE_MISSING: "the data source file is not on this PC",
    SKIP_SHARE: "the data source is on a network share or in a OneDrive folder; pass "
                "allow_network=true to open it",
    SKIP_NETWORK: "the data source is a database server or an ODBC data source that is not "
                  "a local file; pass allow_network=true to query it (read-only)",
    SKIP_NO_DRIVER: "no database driver could open it here (Windows PowerShell with the "
                    "Access or Excel OLE DB provider or ODBC driver is needed)",
    SKIP_UNREADABLE: "the data source could not be opened",
    # Codes the query layer reports for a whole library.
    "bad_query": "a table or field name cannot be quoted safely, so nothing was queried",
    "too_many_queries": "more lookups than one call runs; narrow with library= or table=",
    "login_failed": "the data source refused the login (a password or user is needed)",
    "file_not_found": "the data source file was not found when opening it",
    "file_locked": "the data source file is locked by another program",
    "timeout": "the data source did not answer in time",
    "network_error": "the database server could not be reached",
    "bad_response": "the query script returned an unreadable answer",
    "script_failed": "the query script failed (Windows PowerShell)",
    "query_failed": "the data source could not be queried",
    "bad_request": "the query script refused the request",
}
# Codes the query layer reports for one query.
QUERY_ERRORS = {
    "not_local_table": "the table is a linked table or a saved query in the Access file, "
                       "which can read another database or a server; pass "
                       "allow_network=true to query it",
    "table_not_found": "the table is not in the data source",
    "field_not_found": "a field the lookup uses is not in the table",
    "type_mismatch": "a value does not match the field's type",
    "bad_name": "a table or field name cannot be quoted safely",
    "bad_where": "the comparison cannot be run",
}
_IDENTIFIER_BAD = re.compile(r"[\x00-\x1f\x7f;\[\]`\"]")
_OPS = ("=", "<>", "<", ">", "<=", ">=", "like", "not like")
_DRIVER_ERRORS = {"provider_not_registered", "unsupported_provider", "driver_option_unsupported",
                  "no_powershell"}
ACCESS_ODBC = "Microsoft Access Driver (*.mdb, *.accdb)"
EXCEL_ODBC = "Microsoft Excel Driver (*.xls, *.xlsx, *.xlsm, *.xlsb)"
TEXT_ODBC = "Microsoft Access Text Driver (*.txt, *.csv)"


class QueryError(Exception):
    """A whole batch failed; code is a fixed label (never a provider message)."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(code)
        self.code = code
        self.detail = detail


@dataclass
class Query:
    """SELECT * FROM table WHERE where (a node tree, values already bound), at most
    limit rows."""

    table: str
    where: dict | None = None
    limit: int = 5
    schema: str = ""

    def payload(self) -> dict:
        return {"table": self.table, "schema": self.schema, "where": self.where,
                "limit": self.limit}


@dataclass
class QueryResult:
    columns: list[str] = field(default_factory=list)
    rows: list[list[str | None]] = field(default_factory=list)
    truncated: bool = False
    error: str = ""

    def records(self) -> list[dict[str, str | None]]:
        return [dict(zip(self.columns, row)) for row in self.rows]


@dataclass
class SourcePlan:
    """How a library's data source is read: 'xlsx' (in-process) or 'driver'
    (query_db.ps1 with attempts, each (kind, connection)). local_only (a local file with
    allow_network false): nothing beyond the file is read, so a table named like a path
    is refused and, with base_tables_only (an Access file, whose linked tables and saved
    queries can reach another database or a server), only its own tables are queried."""

    mode: str
    path: str = ""
    attempts: list[tuple[str, str]] = field(default_factory=list, repr=False)
    quote: tuple[str, str] = ("[", "]")
    location: str = "local"
    local_only: bool = False
    base_tables_only: bool = False


def check_name(name: str) -> bool:
    """A table or field name the query layer will quote: 1-128 characters, no quote,
    semicolon or control character."""
    return bool(name) and len(name) <= 128 and not _IDENTIFIER_BAD.search(name)


def check_where(node, depth: int = 0) -> bool:
    """A comparison tree of the shape query_db.ps1 runs (see Query), nested at most
    MAX_WHERE_DEPTH deep."""
    if node is None:
        return True
    if not isinstance(node, dict) or len(node) != 1 or depth > MAX_WHERE_DEPTH:
        return False
    (kind, value), = node.items()
    if kind in ("and", "or"):
        return isinstance(value, list) and bool(value) and all(
            check_where(item, depth + 1) for item in value)
    if kind == "not":
        return check_where(value, depth + 1)
    if kind == "cmp":
        return (isinstance(value, list) and len(value) == 3 and isinstance(value[0], str)
                and check_name(value[0]) and value[1] in _OPS
                and (value[2] is None or isinstance(value[2], (str, int, float)))
                and (value[2] is not None or value[1] in ("=", "<>")))
    if kind == "null":
        return (isinstance(value, list) and len(value) == 2 and isinstance(value[0], str)
                and check_name(value[0]) and isinstance(value[1], bool))
    return False


# ---------------------------------------------------------------------------
# Where a source is, and how it is opened
# ---------------------------------------------------------------------------

_SYNC_ROOTS: list[str] | None = None


def sync_roots() -> list[str]:
    """The folders OneDrive syncs on this PC: its own (environment variables) and every
    SharePoint or Teams library it mounts (the sync engine's MountPoint values in the
    registry), normalised; read once."""
    global _SYNC_ROOTS
    if _SYNC_ROOTS is not None:
        return _SYNC_ROOTS
    roots = [os.environ.get(name) for name in ("OneDrive", "OneDriveCommercial",
                                               "OneDriveConsumer")]
    if os.name == "nt":
        try:
            import winreg

            base = r"Software\SyncEngines\Providers\OneDrive"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, base) as providers:
                index = 0
                while True:
                    try:
                        name = winreg.EnumKey(providers, index)
                    except OSError:
                        break
                    index += 1
                    try:
                        with winreg.OpenKey(providers, name) as provider:
                            roots.append(str(winreg.QueryValueEx(provider, "MountPoint")[0]))
                    except OSError:
                        continue
        except (ImportError, OSError):
            pass
    _SYNC_ROOTS = sorted({os.path.normcase(os.path.normpath(root)) for root in roots
                          if root and root.strip()})
    return _SYNC_ROOTS


# A cloud placeholder: opening or reading it downloads it (RECALL_ON_DATA_ACCESS,
# RECALL_ON_OPEN, OFFLINE).
_CLOUD_ATTRIBUTES = 0x400000 | 0x40000 | 0x1000


def _cloud_file(path: str) -> bool:
    """Whether Windows marks the file as a cloud placeholder (attributes only: the file
    is not opened, so nothing is downloaded)."""
    if os.name != "nt":
        return False
    try:
        import ctypes

        attributes = ctypes.windll.kernel32.GetFileAttributesW(str(path)) & 0xFFFFFFFF
    except (AttributeError, OSError, ValueError, TypeError):
        return False
    return attributes != 0xFFFFFFFF and bool(attributes & _CLOUD_ATTRIBUTES)


def path_location(path: str) -> str:
    """'share' (a UNC path, a URL or a mapped network drive), 'onedrive' (a folder
    OneDrive syncs, SharePoint and Teams libraries included, or a cloud-only file) or
    'local', from the path and the file's attributes: nothing is opened."""
    text = path.strip()
    if text.startswith(("\\\\", "//")) or re.match(r"^[A-Za-z][A-Za-z0-9+.\-]+://", text):
        return "share"
    normal = os.path.normcase(os.path.normpath(text))
    if any(normal == root or normal.startswith(root.rstrip(os.sep) + os.sep)
           for root in sync_roots()):
        return "onedrive"
    if any(part.lower().startswith("onedrive") for part in re.split(r"[\\/]", text)):
        return "onedrive"
    if os.name == "nt" and re.match(r"^[A-Za-z]:", text):
        try:
            import ctypes

            if ctypes.windll.kernel32.GetDriveTypeW(text[:2] + "\\") == 4:  # DRIVE_REMOTE
                return "share"
        except (AttributeError, OSError):
            pass
    if _cloud_file(text):
        return "onedrive"
    return "local"


def _odbc_dsn_lookup(name: str) -> tuple[str, str] | None:
    """(driver, DBQ file) of a user or system ODBC data source on this PC, or None."""
    if os.name != "nt" or not name:
        return None
    try:
        import winreg
    except ImportError:
        return None
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, rf"Software\ODBC\ODBC.INI\{name}") as key:
                driver_path = winreg.QueryValueEx(key, "Driver")[0]
                try:
                    database = winreg.QueryValueEx(key, "DBQ")[0]
                except OSError:
                    database = ""
            driver = ""
            with winreg.OpenKey(hive, r"Software\ODBC\ODBC.INI\ODBC Data Sources") as sources:
                try:
                    driver = winreg.QueryValueEx(sources, name)[0]
                except OSError:
                    driver = os.path.basename(str(driver_path))
            return str(driver), str(database)
        except OSError:
            continue
    return None


dsn_lookup = _odbc_dsn_lookup


def _with(pairs: list[tuple[str, str]], **values: str) -> list[tuple[str, str]]:
    """pairs with the named keys (Data_Source -> 'Data Source') set, replacing any."""
    names = {key.replace("_", " ").lower(): (key.replace("_", " "), value)
             for key, value in values.items()}
    out = [(key, value) for key, value in pairs if key.lower() not in names]
    return out + list(names.values())


def _secret_pairs(pairs: list[tuple[str, str]], *names: str) -> list[tuple[str, str]]:
    wanted = {name.lower() for name in names}
    return [(key, value) for key, value in pairs if key.lower() in wanted and value]


def connection_attempts(info: ConnectionInfo) -> list[tuple[str, str]]:
    """The connections query_db.ps1 tries in order, each ('oledb' | 'odbc', string),
    read-only where the provider allows it."""
    pairs = split_connection_string(info.raw)
    provider = info.provider.lower()
    attempts: list[tuple[str, str]] = []
    own_oledb = provider.startswith(("microsoft.jet.oledb", "microsoft.ace.oledb"))
    if info.kind in ("access", "excel", "text") and info.file:
        password = _secret_pairs(pairs, "jet oledb:database password")
        if info.kind == "excel":
            excel_xls = info.file.lower().endswith(".xls")
            extended = "Excel 8.0;HDR=YES;IMEX=1" if excel_xls else "Excel 12.0;HDR=YES;IMEX=1"
        elif info.kind == "text":
            given = next((value for key, value in pairs
                          if key.lower() == "extended properties" and value), "")
            extended = given if re.search(r"\btext\b", given, re.IGNORECASE) else \
                "text;HDR=YES;FMT=Delimited"
        else:
            extended = ""
        driver = {"access": ACCESS_ODBC, "excel": EXCEL_ODBC, "text": TEXT_ODBC}[info.kind]
        odbc = [("Driver", "{" + driver + "}"), ("DBQ", info.file), ("ReadOnly", "1")]
        if password and info.kind == "access":
            odbc.append(("PWD", password[0][1]))
        # An ODBC string (a driver or a data source name) tries its driver first; an OLE
        # DB string its own provider, then the ACE providers, then the driver.
        odbc_first = not provider or provider.startswith("msdasql")
        if odbc_first:
            attempts.append(("odbc", join_odbc_string(odbc)))
        if own_oledb:
            attempts.append(("oledb", join_connection_string(
                _with(pairs, Data_Source=info.file, Mode="Read"))))
        for version in ("16.0", "12.0"):
            ace = [("Provider", f"Microsoft.ACE.OLEDB.{version}"), ("Data Source", info.file),
                   ("Mode", "Read"), *password]
            if extended:
                ace.append(("Extended Properties", extended))
            if not provider.startswith(f"microsoft.ace.oledb.{version}"):
                attempts.append(("oledb", join_connection_string(ace)))
        if not odbc_first:
            attempts.append(("odbc", join_odbc_string(odbc)))
        return attempts
    if provider.startswith("msdasql") or not provider:
        # ODBC through OLE DB: .NET's OleDb refuses MSDASQL, so the ODBC string itself
        # (Extended Properties holds it; ADO's Data Source, User ID and Password are the
        # DSN, UID and PWD).
        extended = next((value for key, value in pairs
                         if key.lower() == "extended properties"), "")
        odbc = split_connection_string(extended.strip("{}")) if "=" in extended else []
        present = {key.lower() for key, _ in odbc}
        renames = {"data source": "DSN", "user id": "UID", "uid": "UID", "password": "PWD",
                   "pwd": "PWD", "initial catalog": "Database"}
        for key, value in pairs:
            low = key.lower()
            if low in ("provider", "persist security info", "extended properties"):
                continue
            name = renames.get(low, key)
            if name == "DSN" and {"dsn", "driver"} & present:
                continue
            if name.lower() not in present:
                odbc.append((name, value))
                present.add(name.lower())
        return [("odbc", join_odbc_string(odbc))]
    return [("oledb", info.raw)]


_PATH_LIKE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|//|[A-Za-z][A-Za-z0-9+.\-]+://)")


def connection_paths(info: ConnectionInfo) -> list[str]:
    """Every absolute path or URL the connection names (data source, workgroup file,
    DBQ, DefaultDir ..., inside Extended Properties too), braces and quotes removed."""
    found = []
    pending = [info.raw]
    while pending:
        for key, value in split_connection_string(pending.pop()):
            text = (value or "").strip()
            if text.startswith("{") and text.endswith("}"):
                text = text[1:-1]
            if "=" in text and key.strip().lower() in ("extended properties", "connectstring"):
                pending.append(text)
            elif _PATH_LIKE.match(text):
                found.append(text)
    return found


def plan_source(library: DatabaseLibrary, allow_network: bool = False,
                location_of=None) -> tuple[SourcePlan | None, str]:
    """(plan, '') for a source that can be queried, else (None, a SKIP_* reason).
    location_of(path) -> 'local' | 'share' | 'onedrive' (default path_location)."""
    location_of = location_of or path_location
    info = library.connection
    if info.error:
        if info.error.startswith("udl_missing"):
            return None, SKIP_FILE_MISSING
        return None, SKIP_SHARE if info.error == "udl_not_local" else SKIP_UNREADABLE
    if info.kind == "none" or not info.raw:
        return None, SKIP_NO_CONNECTION
    quote = (library.left_quote, library.right_quote)
    if quote not in (("[", "]"), ("`", "`"), ('"', '"')):
        quote = ("[", "]")
    if info.network:
        if not allow_network:
            return None, SKIP_NETWORK
        return SourcePlan("driver", attempts=connection_attempts(info), quote=quote,
                          location="server"), ""
    path = info.file
    if not path:
        return None, SKIP_NO_CONNECTION
    location = location_of(path)
    if location != "local" and not allow_network:
        return None, SKIP_SHARE
    if not allow_network and any(location_of(other) != "local"
                                 for other in connection_paths(info)):
        return None, SKIP_SHARE  # a workgroup file or another path on a share
    exists = os.path.isdir(path) if info.kind == "text" else os.path.isfile(path)
    if not exists:
        return None, SKIP_FILE_MISSING
    if info.kind == "excel" and path.lower().endswith((".xlsx", ".xlsm")):
        return SourcePlan("xlsx", path=path, quote=quote, location=location), ""
    return SourcePlan("driver", path=path, attempts=connection_attempts(info), quote=quote,
                      location=location, local_only=not allow_network,
                      base_tables_only=info.kind == "access"), ""


# ---------------------------------------------------------------------------
# Running queries
# ---------------------------------------------------------------------------

def powershell() -> str | None:
    """Windows PowerShell's path, or None (not Windows, or not installed)."""
    return shutil.which("powershell") if os.name == "nt" else None


def powershell_runner(payload: bytes, timeout: float) -> bytes:
    """Run query_db.ps1 with payload on stdin; its stdout. Raises QueryError."""
    executable = powershell()
    if not executable:
        raise QueryError("no_powershell")
    args = [executable, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", str(SCRIPTS_DIR / QUERY_SCRIPT)]
    try:
        result = subprocess.run(args, input=payload, capture_output=True,  # noqa: S603
                                timeout=timeout,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired as error:
        raise QueryError("timeout") from error
    except OSError as error:
        raise QueryError("no_powershell") from error
    if result.returncode != 0:
        raise QueryError("script_failed")
    return result.stdout


runner = powershell_runner


def run_queries(plan: SourcePlan, queries: list[Query]) -> list[QueryResult]:
    """Results in query order. Raises QueryError when the source cannot be opened at
    all (code: no_driver, file_not_found, login_failed, unreadable, timeout ...)."""
    if len(queries) > MAX_QUERIES:
        raise QueryError("too_many_queries")
    for query in queries:
        if not check_name(query.table) or (query.schema and not check_name(query.schema)) \
                or not check_where(query.where):
            raise QueryError("bad_query")
    if not queries:
        return []
    if plan.mode == "xlsx":
        return _run_xlsx(plan.path, queries)
    if plan.local_only:
        # A table named like a path (a text source's file elsewhere) is not opened.
        refused = {index for index, query in enumerate(queries)
                   if re.search(r"[\\/:]", query.table + query.schema)}
        if refused:
            kept = [query for index, query in enumerate(queries) if index not in refused]
            answers = iter(run_queries(plan, kept) if kept else [])
            return [QueryResult(error="not_local_table") if index in refused else next(answers)
                    for index in range(len(queries))]
    request = {"attempts": [{"kind": kind, "connection": text} for kind, text in plan.attempts],
               "quote": list(plan.quote), "timeout": COMMAND_TIMEOUT,
               "local_only": bool(plan.local_only and plan.base_tables_only),
               "queries": [query.payload() for query in queries]}
    timeout = min(PROCESS_TIMEOUT_MAX, PROCESS_TIMEOUT + PROCESS_TIMEOUT_PER_QUERY * len(queries))
    started = time.monotonic()
    try:
        raw = runner(json.dumps(request).encode("utf-8"), timeout)
    except QueryError as error:
        if error.code in _DRIVER_ERRORS:
            raise QueryError(SKIP_NO_DRIVER) from error
        raise
    try:
        response = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError as error:
        raise QueryError("bad_response") from error
    if not isinstance(response, dict):
        raise QueryError("bad_response")
    if not response.get("ok"):
        code = str(response.get("error") or "unreadable")
        logging.info("database source not opened: %s", code)
        raise QueryError(SKIP_NO_DRIVER if code in _DRIVER_ERRORS else code)
    results = []
    for item in response.get("results") or []:
        if not isinstance(item, dict):
            results.append(QueryResult(error="bad_response"))
        elif item.get("error"):
            results.append(QueryResult(error=str(item["error"])))
        else:
            results.append(QueryResult(
                columns=[str(name) for name in item.get("columns") or []],
                rows=[[None if value is None else str(value) for value in row]
                      for row in item.get("rows") or []],
                truncated=bool(item.get("truncated"))))
    while len(results) < len(queries):
        results.append(QueryResult(error="bad_response"))
    logging.debug("database queries: %d in %.1f s", len(queries), time.monotonic() - started)
    return results


# ---------------------------------------------------------------------------
# Workbooks, read in-process
# ---------------------------------------------------------------------------

_WORKBOOKS: dict[tuple[str, int, int], list[tuple[str, list[list[str]]]]] = {}


def _workbook(path: str) -> list[tuple[str, list[list[str]]]]:
    try:
        stat = os.stat(path)
    except OSError as error:
        raise QueryError("file_not_found") from error
    key = (os.path.normcase(path), stat.st_size, stat.st_mtime_ns)
    if key not in _WORKBOOKS:
        if stat.st_size > MAX_WORKBOOK_BYTES:
            raise QueryError("unreadable")
        try:
            sheets = xlsx_sheets(Path(path).read_bytes(), driver_like=True)
        except (OSError, ValueError) as error:
            raise QueryError("unreadable") from error
        _WORKBOOKS.clear()
        _WORKBOOKS[key] = sheets
    return _WORKBOOKS[key]


def sheet_table(rows: list[list[str]]) -> tuple[list[str], list[list[str | None]]]:
    """(columns, rows) of a worksheet: the first row with a value names the columns (an
    empty or repeated name becomes F<n>), later rows are records (an empty cell read as
    NULL; a cell holding only spaces is text, as the Excel driver reads it)."""
    start = next((index for index, row in enumerate(rows) if any(cell != "" for cell in row)),
                 None)
    if start is None:
        return [], []
    header = rows[start]
    width = max(len(row) for row in rows[start:])
    columns: list[str] = []
    for index in range(width):
        name = header[index].strip() if index < len(header) else ""
        if not name or name.lower() in (seen.lower() for seen in columns):
            name = f"F{index + 1}"
        columns.append(name)
    records = []
    for row in rows[start + 1:]:
        if not any(cell != "" for cell in row):
            continue
        cells = [row[index] if index < len(row) else "" for index in range(width)]
        records.append([cell if cell != "" else None for cell in cells])
    return columns, records


def _sheet_name(table: str) -> str:
    name = table.strip()
    if len(name) > 1 and name[0] == name[-1] == "'":
        name = name[1:-1]
    return name[:-1] if name.endswith("$") else name


def _like(pattern: str) -> re.Pattern:
    out = []
    for char in pattern:
        out.append(".*" if char == "%" else "." if char == "_" else re.escape(char))
    return re.compile("".join(out), re.IGNORECASE | re.DOTALL)


def _number(text) -> float | None:
    try:
        return float(str(text).strip())
    except (TypeError, ValueError):
        return None


def _compare(cell: str | None, op: str, value) -> bool | None:
    """SQL's comparison of a cell with a value (None: unknown, as NULL compares). Text
    compares without regard to case or trailing spaces, as the Access and Excel drivers
    do."""
    if value is None:
        return (cell is None) if op == "=" else (cell is not None)
    if cell is None:
        return None
    if op in ("like", "not like"):
        hit = bool(_like(str(value)).fullmatch(cell))
        return hit if op == "like" else not hit
    left, right = _number(cell), _number(value)
    if isinstance(value, (int, float)) or (left is not None and right is not None
                                           and op not in ("=", "<>")):
        if left is None or right is None:
            return None
        a, b = left, right
    else:
        a, b = cell.rstrip(" ").casefold(), str(value).rstrip(" ").casefold()
    return {"=": a == b, "<>": a != b, "<": a < b, ">": a > b, "<=": a <= b,
            ">=": a >= b}[op]


def evaluate(node, record: dict[str, str | None]) -> bool | None:
    """A comparison tree on one record, with SQL's three-valued logic (None: unknown).
    A field the record lacks raises KeyError."""
    if node is None:
        return True
    (kind, value), = node.items()
    if kind == "and":
        results = [evaluate(item, record) for item in value]
        return False if False in results else (None if None in results else True)
    if kind == "or":
        results = [evaluate(item, record) for item in value]
        return True if True in results else (None if None in results else False)
    if kind == "not":
        inner = evaluate(value, record)
        return None if inner is None else not inner
    if kind == "null":
        name, negate = value
        cell = _cell(record, name)
        return (cell is not None) if negate else (cell is None)
    name, op, wanted = value
    return _compare(_cell(record, name), op, wanted)


def _cell(record: dict[str, str | None], name: str) -> str | None:
    if name in record:
        return record[name]
    folded = name.casefold()
    for key, value in record.items():
        if key.casefold() == folded:
            return value
    raise KeyError(name)


def _run_xlsx(path: str, queries: list[Query]) -> list[QueryResult]:
    sheets = _workbook(path)
    tables: dict[str, tuple[list[str], list[list[str | None]]]] = {}
    results = []
    for query in queries:
        wanted = _sheet_name(query.table).casefold()
        found = next((rows for name, rows in sheets if name.casefold() == wanted), None)
        if found is None:
            results.append(QueryResult(error="table_not_found"))
            continue
        if wanted not in tables:
            tables[wanted] = sheet_table(found)
        columns, rows = tables[wanted]
        result = QueryResult(columns=list(columns))
        try:
            for row in rows:
                if evaluate(query.where, dict(zip(columns, row))) is True:
                    if len(result.rows) >= query.limit:
                        result.truncated = True
                        break
                    result.rows.append(list(row))
        except KeyError:
            result = QueryResult(error="field_not_found")
        results.append(result)
    return results


def is_workbook(path: str) -> bool:
    return path.lower().endswith(EXCEL_SUFFIXES)
