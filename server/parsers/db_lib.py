"""Reader for Altium database libraries (roadmap #53): .DbLib, .SVNDbLib and .DbLink.

All three are INI text written by Altium's database library editor (UTF-8, older files
Windows-1252; a Data Link .udl file is UTF-16). The sections:

[OutputDatabaseLinkFile]  Version.
[DatabaseLinks]  ConnectionString (an OLE DB connection string, or 'File Name=<x.udl>'
    for a Data Link file), AddMode / RemoveMode / UpdateMode (the library's default
    update options), LeftQuote / RightQuote (how names are quoted in SQL),
    QuoteTableNames, UseTableSchemaName and SchemaName, the editor's simple mode
    (LibraryDatabaseType 'Microsoft Access' or 'Microsoft Excel', LibraryDatabasePath,
    and DatabasePathRelative: that path is relative to the library's folder), and in a
    DbLib LibrarySearchPath / SearchSubDirectories (where symbol and footprint libraries
    named without a path are searched). An SVNDbLib adds its Subversion repository
    settings (keys with SVN or Repository in their name).
[Table<n>]  SchemaName, TableName, Enabled, UserWhere (1: the table matches a part by
    the WHERE clause UserWhereText, whose {Parameter} placeholders take the part's
    parameter values; else by its key field), BrowserOrder_*.
[FieldMap<n>]  Options, a '|KEY=VALUE' list: FieldName (the quoted [table].[field]),
    TableNameOnly, FieldNameOnly, FieldType (0 the key field, 1 a normal one),
    ParameterName (the design parameter it maps to, '[None]' when it maps to none; a
    system field in brackets: [Library Ref], [Library Path], [Footprint Ref],
    [Footprint Path], [Footprint Ref 2] ..., [Description], [PCB3D Ref], [Sim ...]),
    VisibleOnAdd and the field's own AddMode / RemoveMode / UpdateMode.

Update options are Altium's TParameterAddMode (0 Default, 1 Never, 2 Always, 3 add if
not blank in the database), TParameterRemoveMode (0 Default, 1 Never, 2 remove if blank
in the database) and TParameterUpdateMode (0 Default, 1 Never, 2 Always); a field set to
Default takes the library's. The key names come from Altium's own binaries (the editor
that writes them and the readers that parse them); a field map given as
FieldMap<n>=<list> inside a section, or as a section with its own keys, is read too.

A connection is never kept as text a tool could show: ConnectionInfo.describe() gives
its kind (access, excel, text, sql_server, mysql, postgresql, oracle, odbc_dsn, other),
the provider, the data source (file, server or DSN), whether credentials are present and
the string with every secret masked; the full string stays in a private field for the
query layer. Pure parsing: no database is opened here."""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

MAX_BYTES = 4 << 20
DB_KINDS = {".dblib": "DbLib", ".svndblib": "SVNDbLib", ".dblink": "DbLink"}

ADD_MODES = {"0": "default", "1": "never", "2": "always", "3": "if_not_blank"}
REMOVE_MODES = {"0": "default", "1": "never", "2": "if_blank"}
UPDATE_MODES = {"0": "default", "1": "never", "2": "always"}
# The library defaults when [DatabaseLinks] does not state them: what Altium's editor
# writes for a new library (seen on every database link file read so far).
DEFAULT_MODES = {"add": "if_not_blank", "remove": "never", "update": "always"}
_MODE_NAMES = {
    "add": {"default": "default", "never": "never", "always": "always",
            "ifnotblankinlibrary": "if_not_blank", "ifnotblank": "if_not_blank"},
    "remove": {"default": "default", "never": "never", "ifblankinlibrary": "if_blank",
               "ifblank": "if_blank"},
    "update": {"default": "default", "never": "never", "always": "always"},
}

# System fields a database field can map to ([Footprint Ref 2] and up are numbered).
SYSTEM_FIELDS = {
    "library ref": "symbol_ref", "library path": "symbol_path",
    "footprint ref": "footprint_ref", "footprint path": "footprint_path",
    "pcb3d ref": "pcb3d_ref", "pcb3d path": "pcb3d_path", "description": "description",
    "orcad library": "orcad_library", "orcad footprint": "orcad_footprint",
    "design item id": "design_item_id", "comment": "comment",
}
UNMAPPED = {"", "[none]", "none", "<choose parameter>"}
_NUMBERED_SYSTEM = re.compile(r"^(footprint (?:ref|path)|pcb3d (?:ref|path)) (\d+)$")
_SIM_SYSTEM = re.compile(r"^sim [a-z ]+$")

# Connection keys holding a secret or a user name (matched anywhere in the key, so
# Passwd, SVNUserName or RepositoryLogin count): their values never leave
# ConnectionInfo unmasked.
_SECRET_KEY = re.compile(r"pass|pwd|secret|token|credential|signature|account ?key|"
                         r"shared ?key|api ?key|access ?key|user|uid|login|logon",
                         re.IGNORECASE)
MASK = "****"
# user:password@ (or user@) in a URL, in any value; and Oracle's user/password@service.
_URL_USERINFO = re.compile(r"([A-Za-z][A-Za-z0-9+.\-]*://)[^/@\s;\"']*@")
_SLASH_LOGIN = re.compile(r"(^|[\s=(])[^/\\@\s;=()\"']+/[^/\\@\s;\"']+@")
_URL = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]+://")
# A connection-string segment without '=' that is kept as written when masking: the
# ISAM type names of an Extended Properties string. Any other one is masked.
_ISAM_SEGMENT = re.compile(r"^(?:excel \d+(?:\.\d+)?(?: xml| macro)?|text|html import|"
                           r"dbase (?:iii|iv|5\.0)|paradox \d\.x|lotus wk\d|exchange \d\.0)$",
                           re.IGNORECASE)
_DRIVER_KINDS = (
    # Text first: the text driver's name holds 'Access' (Microsoft Access Text Driver).
    (re.compile(r"text|csv", re.IGNORECASE), "text"),
    (re.compile(r"access", re.IGNORECASE), "access"),
    (re.compile(r"excel", re.IGNORECASE), "excel"),
    (re.compile(r"sql server|sqlsrv|odbc driver \d+ for sql", re.IGNORECASE), "sql_server"),
    (re.compile(r"mysql|mariadb", re.IGNORECASE), "mysql"),
    (re.compile(r"postgres", re.IGNORECASE), "postgresql"),
    (re.compile(r"oracle", re.IGNORECASE), "oracle"),
)
FILE_KINDS = ("access", "excel", "text")
NETWORK_KINDS = ("sql_server", "mysql", "postgresql", "oracle", "odbc_dsn", "other")
ACCESS_SUFFIXES = (".mdb", ".accdb")
EXCEL_SUFFIXES = (".xls", ".xlsx", ".xlsm", ".xlsb")


def db_kind(path: str) -> str:
    """DbLib, SVNDbLib or DbLink by extension; ''."""
    return DB_KINDS.get(os.path.splitext(path.strip())[1].lower(), "")


def decode_text(data: bytes) -> tuple[str, str]:
    """(text, encoding): UTF-16 with a byte-order mark (a .udl), UTF-8 (BOM optional),
    else Windows-1252 as older files are."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16"), "utf-16"
    try:
        return data.decode("utf-8-sig"), "utf-8"
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace"), "cp1252"


_TRIM = "".join(map(chr, range(33)))  # Delphi's Trim: control characters and space


def read_ini(text: str) -> list[tuple[str, list[tuple[str, str]]]]:
    """(section, [(key, value), ...]) in file order, as Altium's INI reader (Delphi's
    TMemIniFile) sees it: lines end at CR, LF or CRLF only (a U+2028 or form feed inside a
    value stays in it); keys and values stripped, comment lines (';' or '#') and lines
    without '=' skipped; a section name met again (any case) is the first section's, so
    the later copy is not read. Lines before the first section go to a section named ''."""
    sections: list[tuple[str, list[tuple[str, str]]]] = []
    current: list[tuple[str, str]] = []
    sections.append(("", current))
    seen: set[str] = set()
    for line in re.split(r"\r\n|\r|\n", text):
        stripped = line.strip(_TRIM)
        if not stripped or stripped[0] in ";#":
            continue
        header = re.fullmatch(r"\[(.+)\]", stripped)
        if header:
            name = header.group(1).strip(_TRIM)
            current = []
            if name.lower() not in seen:
                seen.add(name.lower())
                sections.append((name, current))
            continue
        key, sep, value = stripped.partition("=")
        if sep and key.strip(_TRIM):
            current.append((key.strip(_TRIM), value.strip(_TRIM)))
    return [(name, pairs) for name, pairs in sections if name or pairs]


def _values(pairs: list[tuple[str, str]]) -> dict[str, str]:
    """Lower-case key -> value, the first of a repeated key winning."""
    out: dict[str, str] = {}
    for key, value in pairs:
        out.setdefault(key.lower(), value)
    return out


def truthy(text: str | None) -> bool:
    return (text or "").strip().lower() in ("1", "true", "t", "yes", "y", "-1")


# ---------------------------------------------------------------------------
# Connection strings
# ---------------------------------------------------------------------------

def split_connection_string(text: str) -> list[tuple[str, str | None]]:
    """The KEY=VALUE pairs of an OLE DB or ODBC connection string, in order. A value may
    be quoted ("..." or '...', a doubled quote escapes it) or braced ({...}, ODBC); ';'
    inside either does not split. Keys are stripped; values stripped and unquoted
    (braces kept, as ODBC needs them). A segment without '=' (the 'Excel 8.0' of an
    Extended Properties string, or a stray part of a value) is its own pair (segment,
    None), never joined to the next key."""
    pairs: list[tuple[str, str | None]] = []
    position, size = 0, len(text)
    while position < size:
        while position < size and text[position] in "; \t\r\n":
            position += 1
        if position >= size:
            break
        equals = text.find("=", position)
        semicolon = text.find(";", position)
        if equals < 0 or 0 <= semicolon < equals:
            end = size if semicolon < 0 else semicolon
            segment = text[position:end].strip()
            if segment:
                pairs.append((segment, None))
            position = end
            continue
        key = text[position:equals].strip()
        position = equals + 1
        while position < size and text[position] in " \t":
            position += 1
        value_chars: list[str] = []
        if position < size and text[position] in "\"'":
            quote = text[position]
            position += 1
            while position < size:
                char = text[position]
                if char == quote:
                    if position + 1 < size and text[position + 1] == quote:
                        value_chars.append(quote)
                        position += 2
                        continue
                    position += 1
                    break
                value_chars.append(char)
                position += 1
            while position < size and text[position] != ";":
                position += 1
        elif position < size and text[position] == "{":
            while position < size:
                char = text[position]
                value_chars.append(char)
                position += 1
                if char == "}":
                    if position < size and text[position] == "}":
                        value_chars.append("}")
                        position += 1
                        continue
                    break
            while position < size and text[position] != ";":
                position += 1
        else:
            end = text.find(";", position)
            end = size if end < 0 else end
            value_chars.append(text[position:end])
            position = end
        if key:
            pairs.append((key, "".join(value_chars).strip()))
    return pairs


def join_connection_string(pairs: list[tuple[str, str | None]]) -> str:
    """KEY=VALUE pairs back into an OLE DB string, quoting values that need it (a
    segment without '=' is written as it was)."""
    parts = []
    for key, value in pairs:
        if value is None:
            parts.append(key)
        elif value.startswith("{") and value.endswith("}"):
            parts.append(f"{key}={value}")
        elif any(char in value for char in ";\"'") or value != value.strip():
            parts.append(f'{key}="{value.replace(chr(34), chr(34) * 2)}"')
        else:
            parts.append(f"{key}={value}")
    return ";".join(parts)


def join_odbc_string(pairs: list[tuple[str, str | None]]) -> str:
    """KEY=VALUE pairs back into an ODBC string. ODBC has no quote characters: a value
    is written as it is (a quote or apostrophe in a path included, which the Access
    driver needs), and only one holding ';', starting with '{' or with outer spaces is
    braced ({...}, a '}' doubled), as the ODBC rules say."""
    parts = []
    for key, value in pairs:
        if value is None:
            continue
        if value.startswith("{") and value.endswith("}"):
            parts.append(f"{key}={value}")
        elif ";" in value or value.startswith("{") or value != value.strip():
            parts.append(f"{key}={{{value.replace('}', '}}')}}}")
        else:
            parts.append(f"{key}={value}")
    return ";".join(parts)


_NOT_SECRET_KEYS = {"user instance"}  # SQL Server Express: a switch, not a login


def is_secret_key(key: str) -> bool:
    """A connection key whose value is a password, token, key or user name."""
    name = key.strip()
    return bool(_SECRET_KEY.search(name)) and name.lower() not in _NOT_SECRET_KEYS


def mask_text(text: str) -> str:
    """A value with the user:password of any URL in it (and an Oracle-style
    user/password@service) masked."""
    text = _URL_USERINFO.sub(r"\1" + MASK + "@", text or "")
    return _SLASH_LOGIN.sub(r"\1" + MASK + "@", text)


def has_login(text: str) -> bool:
    """Whether a value carries a user name or password (URL user:password@, or
    user/password@service)."""
    return bool(_URL_USERINFO.search(text or "") or _SLASH_LOGIN.search(text or ""))


def mask_connection_string(text: str) -> str:
    """The string with every secret value (passwords, tokens, user names, also inside
    a nested Extended Properties string, and a URL's user:password) replaced by ****;
    a segment that is no KEY=VALUE pair is masked too, unless it is an ISAM type name
    such as 'Excel 8.0'."""
    masked: list[tuple[str, str | None]] = []
    for key, value in split_connection_string(text):
        if value is None:
            masked.append((key if _ISAM_SEGMENT.match(key) else MASK, None))
        elif is_secret_key(key) and value:
            masked.append((mask_text(key), MASK))
        elif "=" in value and key.strip().lower() in ("extended properties", "connectstring"):
            inner = value[1:-1] if value.startswith("{") and value.endswith("}") else value
            masked.append((key, mask_connection_string(inner)))
        else:
            masked.append((mask_text(key), mask_text(value)))
    return join_connection_string(masked)


def _get(pairs: list[tuple[str, str]], *names: str) -> str:
    wanted = {name.lower() for name in names}
    return next((value for key, value in pairs if key.lower() in wanted and value), "")


def _unbrace(text: str) -> str:
    text = text.strip()
    return text[1:-1].replace("}}", "}") if text.startswith("{") and text.endswith("}") else text


@dataclass
class ConnectionInfo:
    """A database library's connection, reduced to what is safe to show (describe()).
    raw is the full string for the query layer only: it is never shown or logged."""

    kind: str = "none"
    provider: str = ""
    driver: str = ""
    data_source: str = ""  # as written: a file, folder, server or DSN name
    file: str = ""  # the resolved local file (a text source: folder) of a file kind
    server: str = ""
    database: str = ""
    dsn: str = ""
    network: bool = False  # a server or DSN, not a file
    credentials: bool = False
    windows_auth: bool = False
    udl: str = ""  # the Data Link file the string was read from
    extended: str = ""  # Extended Properties, masked
    error: str = ""
    masked: str = ""
    raw: str = field(default="", repr=False, compare=False)

    def describe(self) -> dict:
        out: dict = {"kind": self.kind}
        for name in ("provider", "driver", "data_source", "file", "server", "database",
                     "dsn", "udl", "extended", "error"):
            value = getattr(self, name)
            if value:
                out[name] = mask_text(value)
        out["network"] = self.network
        out["credentials"] = self.credentials
        if self.windows_auth:
            out["windows_auth"] = True
        if self.masked:
            out["connection_string_masked"] = self.masked
        return out


def _resolve_path(path: str, base: str) -> str:
    """A data-source path resolved against the library's folder (environment variables
    expanded); '' for ''."""
    path = os.path.expandvars(path.strip().strip('"'))
    if not path:
        return ""
    if os.sep == "/" and not path.startswith("\\\\"):
        path = path.replace("\\", "/")  # a Windows path read on another OS
    if re.match(r"^[A-Za-z]:[\\/]", path) or path.startswith(("\\\\", "//")):
        return os.path.normpath(path)
    if path.startswith(("\\", "/")) and re.match(r"^[A-Za-z]:", base or ""):
        return os.path.normpath(base[:2] + path)
    return os.path.normpath(os.path.join(base or ".", path))


def _file_kind(path: str, extended: str, hint: str = "") -> str:
    lower = path.lower()
    if re.search(r"\btext\b", extended, re.IGNORECASE):
        return "text"
    if lower.endswith(EXCEL_SUFFIXES) or re.search(r"\bexcel\b", extended, re.IGNORECASE):
        return "excel"
    if lower.endswith(ACCESS_SUFFIXES):
        return "access"
    if "excel" in hint.lower():
        return "excel"
    return "access"


def read_udl(path: str) -> str:
    """The connection string of a Data Link (.udl) file: its first line that is not the
    [oledb] header or a ';' comment. Raises OSError or ValueError."""
    data = Path(path).read_bytes()[:MAX_BYTES]
    text, _ = decode_text(data)
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith(";") and stripped.lower() != "[oledb]":
            return stripped
    raise ValueError("no connection string in the Data Link file")


def parse_connection(text: str, base: str = "", type_hint: str = "",
                     dsn_lookup=None, _depth: int = 0, udl_allowed=None) -> ConnectionInfo:
    """Describe a connection string (base: the library's folder, for relative paths;
    type_hint: LibraryDatabaseType; dsn_lookup(name) -> (driver, file) or None, for an
    ODBC data source name). 'File Name=<x.udl>' reads the Data Link file when
    udl_allowed(path) is true (default: always); else error udl_not_local and nothing
    is read (a Data Link file on a network share or in a OneDrive folder). Every text
    field but raw (and file, a local path) is masked; a login inside a value (a URL's
    user:password@) counts as credentials."""
    info = _parse_connection(text, base, type_hint, dsn_lookup, _depth, udl_allowed)
    values = [value or "" for _, value in split_connection_string(text)]
    values += [value or "" for _, value in split_connection_string(
        _unbrace(_get(split_connection_string(text), "extended properties")))]
    if any(has_login(value) for value in values):
        info.credentials = True
    for name in ("provider", "driver", "data_source", "server", "database", "dsn", "udl"):
        setattr(info, name, mask_text(getattr(info, name)))
    return info


def _parse_connection(text: str, base: str, type_hint: str, dsn_lookup, _depth: int,
                      udl_allowed) -> ConnectionInfo:
    info = ConnectionInfo(raw=text.strip())
    pairs = split_connection_string(text)
    if not pairs:
        return info
    info.masked = mask_connection_string(text)
    secret = [(key, value) for key, value in pairs if is_secret_key(key) and value]
    info.credentials = bool(secret)
    security = _get(pairs, "integrated security", "trusted_connection").lower()
    info.windows_auth = security in ("sspi", "true", "yes")
    udl = _get(pairs, "file name")
    if udl and _depth < 2:
        resolved = _resolve_path(udl, base)
        if udl_allowed is not None and not udl_allowed(resolved):
            info.kind, info.udl, info.error = "none", resolved, "udl_not_local"
            return info
        try:
            inner = parse_connection(read_udl(resolved), base, type_hint, dsn_lookup, _depth + 1,
                                     udl_allowed)
        except (OSError, ValueError) as error:
            info.kind, info.udl = "none", resolved
            info.error = ("udl_missing" if isinstance(error, FileNotFoundError)
                          else f"udl_unreadable ({type(error).__name__})")
            return info
        inner.udl = resolved
        inner.credentials = inner.credentials or info.credentials
        return inner
    provider = _get(pairs, "provider")
    info.provider = provider
    extended = _get(pairs, "extended properties")
    inner_pairs = split_connection_string(_unbrace(extended)) if "=" in extended else []
    info.extended = mask_connection_string(extended)
    if inner_pairs:
        info.credentials = info.credentials or any(
            is_secret_key(key) and value for key, value in inner_pairs)
    odbc_pairs = pairs + inner_pairs
    data_source = _get(pairs, "data source", "server", "address", "addr", "network address")
    info.data_source = data_source
    info.database = _get(odbc_pairs, "initial catalog", "database")
    low = provider.lower()
    if low.startswith(("microsoft.jet.oledb", "microsoft.ace.oledb")):
        info.kind = _file_kind(data_source, extended, type_hint)
        if _URL.match(data_source.strip()):
            info.network = True  # a file behind a URL: a remote source, not a local file
        else:
            info.file = _resolve_path(data_source, base)
        return info
    if low.startswith(("sqloledb", "sqlncli", "msoledbsql")):
        info.kind, info.network, info.server = "sql_server", True, data_source
        return info
    if low.startswith(("oraoledb", "msdaora")):
        info.kind, info.network, info.server = "oracle", True, data_source
        return info
    if "mysql" in low:
        info.kind, info.network, info.server = "mysql", True, data_source
        return info
    if "pg" in low[:4] or "postgres" in low:
        info.kind, info.network, info.server = "postgresql", True, data_source
        return info
    if low and not low.startswith("msdasql"):
        info.kind, info.network = "other", True
        return info
    # ODBC: MSDASQL (or no provider: ADO's default), a driver string or a DSN.
    driver = _unbrace(_get(odbc_pairs, "driver"))
    dsn = _get(odbc_pairs, "dsn") or (data_source if not driver else "")
    database_file = _get(odbc_pairs, "dbq", "defaultdir")
    info.driver, info.dsn = driver, dsn
    if not driver and dsn and dsn_lookup is not None:
        found = dsn_lookup(dsn)
        if found:
            driver, database_file = found
            info.driver = driver
    kind = next((name for pattern, name in _DRIVER_KINDS if driver and pattern.search(driver)), "")
    if kind in FILE_KINDS and database_file and _URL.match(database_file.strip()):
        info.kind, info.network = kind, True
        info.data_source = info.data_source or database_file
        return info
    if kind in FILE_KINDS and database_file:
        info.kind, info.file = kind, _resolve_path(database_file, base)
        info.data_source = info.data_source or database_file
        return info
    if kind and kind not in FILE_KINDS:
        info.kind, info.network = kind, True
        info.server = _get(odbc_pairs, "server", "host", "address")
        return info
    info.kind, info.network = ("odbc_dsn" if dsn else "other"), True
    return info


# ---------------------------------------------------------------------------
# Tables and field maps
# ---------------------------------------------------------------------------

def system_field(parameter: str) -> str:
    """The system field a ParameterName names ('' for a design parameter): symbol_ref,
    symbol_path, footprint_ref, footprint_ref_2 ..., footprint_path(_n), pcb3d_ref,
    pcb3d_path, description, design_item_id, comment, sim (any [Sim ...]), orcad_*."""
    text = parameter.strip()
    if not (text.startswith("[") and text.endswith("]")):
        return ""
    name = re.sub(r"\s+", " ", text[1:-1].strip().lower())
    if name in SYSTEM_FIELDS:
        return SYSTEM_FIELDS[name]
    numbered = _NUMBERED_SYSTEM.match(name)
    if numbered:
        base = SYSTEM_FIELDS[numbered.group(1)]
        number = int(numbered.group(2))
        return base if number <= 1 else f"{base}_{number}"
    if _SIM_SYSTEM.match(name):
        return "sim"
    return ""


def parameter_name(parameter: str) -> str:
    """The design parameter a ParameterName names (brackets of a non-system name
    removed); '' when unmapped or a system field."""
    text = parameter.strip()
    if text.lower() in UNMAPPED or system_field(text):
        return ""
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1].strip()
    return text


def _mode(kind: str, text: str) -> str:
    """An update option as written ('0'..'3' or an enum name) -> its name; '' when not
    given; the text itself when unknown."""
    value = (text or "").strip()
    if not value:
        return ""
    table = {"add": ADD_MODES, "remove": REMOVE_MODES, "update": UPDATE_MODES}[kind]
    if value in table:
        return table[value]
    name = re.sub(r"^eparameter(?:add|remove|update)_", "", value.lower())
    return _MODE_NAMES[kind].get(name.replace("_", ""), value)


@dataclass
class FieldMap:
    """One database field and the design parameter (or system field) it maps to."""

    table: str
    field: str
    parameter: str = ""  # ParameterName as written
    key: bool = False
    visible_on_add: bool | None = None
    add_mode: str = ""  # as given for this field ('' or default: the library's)
    remove_mode: str = ""
    update_mode: str = ""
    index: int = 0  # the FieldMap<n> number

    @property
    def system(self) -> str:
        return system_field(self.parameter)

    @property
    def design_parameter(self) -> str:
        return parameter_name(self.parameter)

    @property
    def mapped(self) -> bool:
        return bool(self.system or self.design_parameter)

    def modes(self, defaults: dict[str, str]) -> dict[str, str]:
        """The effective add / remove / update options ('default' takes the library's)."""
        own = {"add": self.add_mode, "remove": self.remove_mode, "update": self.update_mode}
        return {kind: (value if value and value != "default" else defaults.get(kind, ""))
                for kind, value in own.items()}

    def describe(self, defaults: dict[str, str]) -> dict:
        out: dict = {"field": self.field}
        if self.system:
            out["system_field"] = self.system
            out["parameter"] = self.parameter
        elif self.design_parameter:
            out["parameter"] = self.design_parameter
        else:
            out["parameter"] = None
        if self.key:
            out["key"] = True
        if self.visible_on_add is not None:
            out["visible_on_add"] = self.visible_on_add
        out["options"] = self.modes(defaults)
        return out


@dataclass
class TableLink:
    index: int
    name: str
    schema: str = ""
    enabled: bool = True
    user_where: bool = False
    where_text: str = ""
    fields: list[FieldMap] = field(default_factory=list)

    @property
    def key_fields(self) -> list[FieldMap]:
        return [item for item in self.fields if item.key]

    def system_fields(self, prefix: str) -> list[FieldMap]:
        """The fields mapped to a system field kind and its numbered copies, in order
        (footprint_ref, footprint_ref_2, ...)."""
        found = [item for item in self.fields
                 if item.system == prefix or item.system.startswith(prefix + "_")]
        return sorted(found, key=lambda item: (len(item.system), item.system))

    def by_parameter(self, name: str) -> FieldMap | None:
        wanted = name.strip().lower()
        return next((item for item in self.fields
                     if item.design_parameter.lower() == wanted), None)

    def lookup(self) -> dict:
        """How a part is matched: {'by': 'where', 'where': text} or {'by': 'key',
        'fields': [{'field', 'parameter'}]} or {'by': None}."""
        if self.user_where and self.where_text.strip():
            return {"by": "where", "where": self.where_text}
        keys = self.key_fields
        if keys:
            return {"by": "key", "fields": [
                {"field": item.field, "parameter": item.design_parameter or item.parameter}
                for item in keys]}
        return {"by": None}


@dataclass
class DatabaseLibrary:
    path: str
    kind: str
    encoding: str = "utf-8"
    version: str = ""
    connection: ConnectionInfo = field(default_factory=ConnectionInfo)
    defaults: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_MODES))
    defaults_assumed: list[str] = field(default_factory=list)
    left_quote: str = "["
    right_quote: str = "]"
    quote_table_names: bool = True
    use_table_schema_name: bool = False
    schema: str = ""
    database_type: str = ""
    database_path: str = ""
    path_relative: bool = False
    library_search_path: str = ""
    search_subdirectories: bool = False
    tables: list[TableLink] = field(default_factory=list)
    repository: dict[str, str] = field(default_factory=dict)  # SVNDbLib settings, masked
    orphan_fields: int = 0  # field maps naming no table of the library
    warnings: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return Path(self.path).name

    def table(self, name: str) -> TableLink | None:
        """A table by name (case-insensitive; 'schema.table' and a quoted name too)."""
        wanted = _bare_name(name, self.left_quote, self.right_quote)
        for table in self.tables:
            if table.name.lower() == wanted or (
                    table.schema and f"{table.schema}.{table.name}".lower() == wanted):
                return table
        tail = wanted.rsplit(".", 1)[-1]
        return next((table for table in self.tables if table.name.lower() == tail), None)

    @property
    def enabled_tables(self) -> list[TableLink]:
        return [table for table in self.tables if table.enabled]

    def describe(self, tables: bool = True) -> dict:
        out: dict = {"name": self.name, "kind": self.kind, "path": self.path,
                     "connection": self.connection.describe(),
                     "update_defaults": dict(self.defaults)}
        if self.defaults_assumed:
            out["update_defaults_assumed"] = list(self.defaults_assumed)
        if self.database_type:
            out["database_type"] = self.database_type
        if self.path_relative:
            out["database_path_relative"] = True
        if self.kind != "DbLink":
            out["library_search_path"] = self.library_search_path
            out["search_subdirectories"] = self.search_subdirectories
        if self.repository:
            out["repository"] = dict(self.repository)
        out["table_count"] = len(self.tables)
        out["enabled_tables"] = len(self.enabled_tables)
        if tables:
            out["tables"] = [self.describe_table(table) for table in self.tables]
        if self.orphan_fields:
            out["field_maps_without_table"] = self.orphan_fields
        if self.warnings:
            out["warnings"] = list(self.warnings)
        return out

    def describe_table(self, table: TableLink, fields: bool = True) -> dict:
        out: dict = {"table": table.name, "enabled": table.enabled}
        if table.schema:
            out["schema"] = table.schema
        out["lookup"] = table.lookup()
        out["field_count"] = len(table.fields)
        out["mapped_fields"] = sum(1 for item in table.fields if item.mapped)
        for kind in ("symbol_ref", "symbol_path", "footprint_ref", "footprint_path"):
            names = [item.field for item in table.system_fields(kind)]
            if names:
                out[kind + "_fields"] = names
        if fields:
            out["fields"] = [item.describe(self.defaults) for item in table.fields]
        return out


def _bare_name(name: str, left: str = "[", right: str = "]") -> str:
    """A table or field name without its quotes, lower-case."""
    text = name.strip()
    for open_quote, close_quote in ((left, right), ("[", "]"), ("`", "`"), ('"', '"')):
        if open_quote and text.startswith(open_quote) and text.endswith(close_quote):
            text = text[len(open_quote):len(text) - len(close_quote)]
            break
    return text.replace(f"{right}.{left}", ".").strip().lower()


def _split_field_name(text: str, left: str, right: str) -> tuple[str, str]:
    """(table, field) of a FieldName such as [Table].[Field] or Table.Field."""
    value = text.strip()
    if left and right and value.startswith(left) and value.endswith(right):
        inner = value[len(left):len(value) - len(right)]
        separator = f"{right}.{left}"
        if separator in inner:
            table, _, name = inner.rpartition(separator)
            return table, name
        return "", inner
    table, dot, name = value.rpartition(".")
    return (table, name) if dot else ("", value)


def _field_props(text: str) -> dict[str, str]:
    """A '|KEY=VALUE|...' list (leading '|' optional), keys lower-case."""
    props: dict[str, str] = {}
    for part in text.split("|"):
        key, sep, value = part.partition("=")
        if sep and key.strip():
            props.setdefault(key.strip().lower(), value.strip())
    return props


_FIELD_KEYS = {"fieldname", "tablenameonly", "fieldnameonly", "parametername", "fieldtype"}


def _field_map(props: dict[str, str], index: int, left: str, right: str,
               table_hint: str = "") -> FieldMap | None:
    if not (_FIELD_KEYS & set(props)):
        return None
    table, name = props.get("tablenameonly", ""), props.get("fieldnameonly", "")
    if not name or not table:
        split_table, split_name = _split_field_name(props.get("fieldname", ""), left, right)
        table, name = table or split_table or table_hint, name or split_name
    field_type = props.get("fieldtype", "").strip().lower()
    visible = props.get("visibleonadd")
    return FieldMap(
        table=table, field=name, parameter=props.get("parametername", ""),
        key=field_type in ("0", "key", "efieldtype_key"),
        visible_on_add=None if visible is None else truthy(visible),
        add_mode=_mode("add", props.get("addmode", "")),
        remove_mode=_mode("remove", props.get("removemode", "")),
        update_mode=_mode("update", props.get("updatemode", "")), index=index)


def _mask_repository(key: str, value: str) -> str:
    if is_secret_key(key):
        return MASK if value else ""
    return mask_text(value)  # user:password@ in a URL


def parse_db_lib(path: str, dsn_lookup=None, udl_allowed=None) -> DatabaseLibrary:
    """Read a .DbLib, .SVNDbLib or .DbLink file (raises OSError when it cannot be read).
    dsn_lookup, udl_allowed: see parse_connection."""
    data = Path(path).read_bytes()
    library = DatabaseLibrary(path=str(path), kind=db_kind(str(path)) or "DbLib")
    if len(data) > MAX_BYTES:
        library.warnings.append(f"larger than {MAX_BYTES >> 20} MB: only the start is read")
        data = data[:MAX_BYTES]
    text, library.encoding = decode_text(data)
    sections = read_ini(text)
    links: dict[str, str] = {}
    for name, pairs in sections:
        if name.lower() == "databaselinks":
            links = _values(pairs)
            break
    for name, pairs in sections:
        if name.lower() == "outputdatabaselinkfile":
            library.version = _values(pairs).get("version", "")
    library.left_quote = links.get("leftquote", "[")
    library.right_quote = links.get("rightquote", "]")
    library.quote_table_names = truthy(links.get("quotetablenames", "1"))
    library.use_table_schema_name = truthy(links.get("usetableschemaname", "0"))
    library.schema = links.get("schemaname", "")
    library.database_type = links.get("librarydatabasetype", "")
    library.database_path = links.get("librarydatabasepath", "")
    library.path_relative = truthy(links.get("databasepathrelative", "0"))
    library.library_search_path = links.get("librarysearchpath", "")
    library.search_subdirectories = truthy(links.get("searchsubdirectories", "0"))
    for kind in ("add", "remove", "update"):
        value = _mode(kind, links.get(f"{kind}mode", ""))
        if value and value != "default":
            library.defaults[kind] = value
        else:
            library.defaults_assumed.append(kind)
    base = str(Path(path).parent)
    connection_text = links.get("connectionstring", "")
    if library.path_relative and library.database_path and not re.match(
            r"^[A-Za-z]:[\\/]|^[\\/]{2}", library.database_path):
        # The editor's relative path wins: the connection string keeps the absolute
        # path of the PC the library was saved on.
        resolved = _resolve_path(library.database_path, base)
        pairs = split_connection_string(connection_text)
        if any(key.lower() == "data source" for key, _ in pairs):
            pairs = [(key, resolved if key.lower() == "data source" else value)
                     for key, value in pairs]
            connection_text = join_connection_string(pairs)
    library.connection = parse_connection(connection_text, base, library.database_type,
                                          dsn_lookup, udl_allowed=udl_allowed)
    for name, pairs in sections:
        if name.lower() in ("databaselinks", "outputdatabaselinkfile"):
            for key, value in pairs:
                if re.search(r"svn|repository", key, re.IGNORECASE):
                    library.repository[key] = _mask_repository(key, value)
            continue
        if re.search(r"svn|repository", name, re.IGNORECASE):
            for key, value in pairs:
                library.repository[f"{name}.{key}"] = _mask_repository(key, value)
    _read_tables(library, sections)
    return library


def _read_tables(library: DatabaseLibrary, sections) -> None:
    left, right = library.left_quote, library.right_quote
    loose: list[tuple[FieldMap, str]] = []
    for name, pairs in sections:
        table_match = re.fullmatch(r"Table(\d+)", name, re.IGNORECASE)
        field_match = re.fullmatch(r"FieldMap(\d+)", name, re.IGNORECASE)
        values = _values(pairs)
        if table_match:
            table = TableLink(
                index=int(table_match.group(1)), name=values.get("tablename", ""),
                schema=values.get("schemaname", ""),
                enabled=truthy(values.get("enabled", "true")),
                user_where=truthy(values.get("userwhere", "0")),
                where_text=values.get("userwheretext", ""))
            library.tables.append(table)
            for key, value in pairs:
                inline = re.fullmatch(r"FieldMap(\d+)", key, re.IGNORECASE)
                if inline:
                    item = _field_map(_field_props(value), int(inline.group(1)), left, right,
                                      table.name)
                    if item:
                        loose.append((item, table.name))
        elif field_match:
            props = _field_props(values["options"]) if "options" in values else {}
            props = props if _FIELD_KEYS & set(props) else values
            item = _field_map(props, int(field_match.group(1)), left, right)
            if item:
                loose.append((item, ""))
        else:
            for key, value in pairs:
                inline = re.fullmatch(r"FieldMap(\d+)", key, re.IGNORECASE)
                if inline and "=" in value:
                    item = _field_map(_field_props(value), int(inline.group(1)), left, right)
                    if item:
                        loose.append((item, ""))
    library.tables.sort(key=lambda table: table.index)
    for item, hint in loose:
        table = library.table(item.table or hint) if (item.table or hint) else None
        if table is None and len(library.tables) == 1 and not item.table:
            table = library.tables[0]
        if table is None:
            library.orphan_fields += 1
            continue
        table.fields.append(item)
    for table in library.tables:
        table.fields.sort(key=lambda item: item.index)


# ---------------------------------------------------------------------------
# WHERE clauses
# ---------------------------------------------------------------------------

class WhereError(ValueError):
    """A WHERE clause outside what the query layer runs (comparisons of a field with a
    value, AND, OR, NOT, parentheses, IS [NOT] NULL)."""


_COMPARE = ("=", "<>", "!=", "<=", ">=", "<", ">")
MAX_WHERE_DEPTH = 32  # nested AND / OR / NOT and parentheses; query_db.ps1's limit too


@dataclass
class _Token:
    kind: str  # ident, string, number, placeholder, op, word, lparen, rparen
    text: str


def _tokens(text: str, left: str, right: str, double_quoted_strings: bool = False
            ) -> list[_Token]:
    out: list[_Token] = []
    position, size = 0, len(text)
    # Access, Excel and text sources (Jet / ACE SQL) read "..." as a string, as '...';
    # elsewhere it quotes a name, unless the library quotes names with it anyway.
    strings = "'" + ('"' if double_quoted_strings and left != '"' else "")
    quotes = {left: right, "[": "]", "`": "`", '"': '"'}
    for char in strings:
        quotes.pop(char, None)
    while position < size:
        char = text[position]
        if char.isspace():
            position += 1
            continue
        if char in quotes:
            close = quotes[char]
            end = text.find(close, position + 1)
            if end < 0:
                raise WhereError("an unclosed quoted name")
            out.append(_Token("ident", text[position + 1:end]))
            position = end + 1
            continue
        if char in strings:
            quote = char
            chars = []
            position += 1
            while True:
                if position >= size:
                    raise WhereError("an unclosed string")
                if text[position] == quote:
                    if position + 1 < size and text[position + 1] == quote:
                        chars.append(quote)
                        position += 2
                        continue
                    position += 1
                    break
                chars.append(text[position])
                position += 1
            out.append(_Token("string", "".join(chars)))
            continue
        if char == "{":
            end = text.find("}", position)
            if end < 0:
                raise WhereError("an unclosed {placeholder}")
            out.append(_Token("placeholder", text[position + 1:end].strip()))
            position = end + 1
            continue
        if char == "(":
            out.append(_Token("lparen", char))
            position += 1
            continue
        if char == ")":
            out.append(_Token("rparen", char))
            position += 1
            continue
        if char == ".":
            out.append(_Token("dot", char))
            position += 1
            continue
        two = text[position:position + 2]
        if two in ("<>", "!=", "<=", ">="):
            out.append(_Token("op", two))
            position += 2
            continue
        if char in "=<>":
            out.append(_Token("op", char))
            position += 1
            continue
        number = re.match(r"-?\d+(?:\.\d+)?(?![A-Za-z_])", text[position:])
        if number:
            out.append(_Token("number", number.group()))
            position += len(number.group())
            continue
        word = re.match(r"[A-Za-z_][A-Za-z0-9_$#]*", text[position:])
        if word:
            out.append(_Token("word", word.group()))
            position += len(word.group())
            continue
        raise WhereError(f"'{char}' is not supported")
    return out


@dataclass
class WhereValue:
    """A comparison's value: kind 'string' (a quoted text whose {placeholders} take the
    part's parameter values), 'number', or 'placeholder' (a bare {Parameter})."""

    kind: str
    parts: list[tuple[str, str]]  # ("text", ...) or ("param", name)

    @property
    def placeholders(self) -> list[str]:
        return [value for kind, value in self.parts if kind == "param"]

    def render(self, values: dict[str, str]) -> str:
        return "".join(value if kind == "text" else values.get(value.lower(), "")
                       for kind, value in self.parts)


def _template(text: str) -> list[tuple[str, str]]:
    parts: list[tuple[str, str]] = []
    position = 0
    for match in re.finditer(r"\{([^{}]+)\}", text):
        if match.start() > position:
            parts.append(("text", text[position:match.start()]))
        parts.append(("param", match.group(1).strip()))
        position = match.end()
    if position < len(text):
        parts.append(("text", text[position:]))
    return parts


class _Parser:
    _KEYWORDS = {"and", "or", "not", "like", "is", "null"}

    def __init__(self, tokens: list[_Token]):
        self.tokens = tokens
        self.position = 0
        self.nesting = 0  # open parentheses and NOTs

    def peek(self, offset: int = 0) -> _Token | None:
        index = self.position + offset
        return self.tokens[index] if index < len(self.tokens) else None

    def word(self, offset: int = 0) -> str:
        token = self.peek(offset)
        return token.text.lower() if token is not None and token.kind == "word" else ""

    def take(self) -> _Token:
        token = self.peek()
        if token is None:
            raise WhereError("the clause ends too early")
        self.position += 1
        return token

    def expression(self):
        terms = [self.conjunction()]
        while self.word() == "or":
            self.take()
            terms.append(self.conjunction())
        return terms[0] if len(terms) == 1 else ("or", terms)

    def conjunction(self):
        terms = [self.negation()]
        while self.word() == "and":
            self.take()
            terms.append(self.negation())
        return terms[0] if len(terms) == 1 else ("and", terms)

    def negation(self):
        token = self.peek()
        if self.word() == "not" or (token is not None and token.kind == "lparen"):
            self.nesting += 1
            if self.nesting > MAX_WHERE_DEPTH:
                raise WhereError("the clause is nested too deeply")
            try:
                if self.word() == "not":
                    self.take()
                    return ("not", self.negation())
                self.take()
                inner = self.expression()
                closing = self.take()
                if closing.kind != "rparen":
                    raise WhereError("a '(' is not closed")
                return inner
            finally:
                self.nesting -= 1
        return self.comparison()

    def name(self) -> str | None:
        """A field name: a quoted name or a bare word, with an optional table prefix
        (table.field: the field is kept)."""
        token = self.peek()
        if token is None:
            return None
        if token.kind == "ident" or (token.kind == "word" and token.text.lower() not in self._KEYWORDS):
            self.take()
            name = token.text
            while self.peek() is not None and self.peek().kind == "dot":
                self.take()
                following = self.take()
                if following.kind not in ("ident", "word"):
                    raise WhereError("a name is expected after '.'")
                name = following.text
            return name
        return None

    def value(self) -> WhereValue | None:
        token = self.peek()
        if token is None:
            return None
        if token.kind == "string":
            self.take()
            return WhereValue("string", _template(token.text))
        if token.kind == "number":
            self.take()
            return WhereValue("number", [("text", token.text)])
        if token.kind == "placeholder":
            self.take()
            return WhereValue("placeholder", [("param", token.text)])
        return None

    def comparison(self):
        field_name = self.name()
        if field_name is None:
            value = self.value()
            if value is None:
                raise WhereError("a field name or value is expected")
            op = self.take()
            if op.kind != "op" or op.text not in ("=", "<>", "!="):
                raise WhereError("a value compared with a field must use = or <>")
            other = self.name()
            if other is None:
                raise WhereError("a comparison needs a field on one side")
            return ("cmp", other, "<>" if op.text == "!=" else op.text, value)
        if self.word() == "is":
            self.take()
            negate = self.word() == "not"
            if negate:
                self.take()
            if self.word() != "null":
                raise WhereError("IS must be followed by [NOT] NULL")
            self.take()
            return ("null", field_name, negate)
        negate_like = False
        if self.word() == "not" and self.word(1) == "like":
            self.take()
            negate_like = True
        if self.word() == "like":
            self.take()
            value = self.value()
            if value is None:
                raise WhereError("LIKE needs a value")
            return ("cmp", field_name, "not like" if negate_like else "like", value)
        op = self.take()
        if op.kind != "op" or op.text not in _COMPARE:
            raise WhereError("a comparison operator is expected")
        value = self.value()
        if value is None:
            raise WhereError("a field may only be compared with a value or {Parameter}")
        return ("cmp", field_name, "<>" if op.text == "!=" else op.text, value)


def where_depth(tree) -> int:
    """How deep a WHERE tree nests (a comparison: 0; AND, OR, NOT: one more than their
    deepest term), as query_db.ps1 counts it."""
    if tree[0] in ("and", "or"):
        return 1 + max(where_depth(child) for child in tree[1])
    if tree[0] == "not":
        return 1 + where_depth(tree[1])
    return 0


def parse_where(text: str, left: str = "[", right: str = "]",
                double_quoted_strings: bool = False):
    """A WHERE clause as a tree: ('and'|'or', [nodes]), ('not', node), ('cmp', field,
    op, WhereValue) with op one of = <> < > <= >= like, 'not like', or ('null', field,
    negate). double_quoted_strings: "..." is a string, as Access and Excel sources read
    it (else a quoted name). Raises WhereError for anything else (functions,
    sub-queries, IN, field to field comparisons, nesting deeper than MAX_WHERE_DEPTH)."""
    tokens = _tokens(text, left, right, double_quoted_strings)
    if not tokens:
        raise WhereError("the clause is empty")
    parser = _Parser(tokens)
    try:
        tree = parser.expression()
    except RecursionError as error:
        raise WhereError("the clause is nested too deeply") from error
    if parser.peek() is not None:
        raise WhereError(f"unexpected '{parser.peek().text}'")
    if where_depth(tree) > MAX_WHERE_DEPTH:
        raise WhereError("the clause is nested too deeply")
    return tree


def where_placeholders(tree) -> list[str]:
    """The {Parameter} names a WHERE tree uses, in order, once each."""
    found: list[str] = []

    def walk(node) -> None:
        if node[0] in ("and", "or"):
            for child in node[1]:
                walk(child)
        elif node[0] == "not":
            walk(node[1])
        elif node[0] == "cmp":
            for name in node[3].placeholders:
                if name.lower() not in (seen.lower() for seen in found):
                    found.append(name)

    walk(tree)
    return found


def where_fields(tree) -> list[str]:
    found: list[str] = []

    def walk(node) -> None:
        if node[0] in ("and", "or"):
            for child in node[1]:
                walk(child)
        elif node[0] == "not":
            walk(node[1])
        elif node[1] not in found:
            found.append(node[1])

    walk(tree)
    return found
