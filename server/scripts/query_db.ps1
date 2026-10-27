# Read-only database queries for get_db_part and check_db_parameters (roadmap #53).
#
# The request is one JSON object on stdin (UTF-8), never on the command line, so a
# connection string that holds a password is not visible to other processes:
#   {"attempts": [{"kind": "oledb" | "odbc", "connection": "..."}, ...],
#    "quote": ["[", "]"], "timeout": 15, "local_only": true | false,
#    "queries": [{"table": "T", "schema": "", "where": <node> | null, "limit": 5}, ...]}
# local_only (a local Access file when network sources are not allowed): a query runs
# only on a base table of the file (TABLE_TYPE 'TABLE' in the provider's table list);
# a linked table, pass-through or saved query, which can read another database or a
# server, gets {"error": "not_local_table"} without being opened.
# A <node> is {"and": [nodes]}, {"or": [nodes]}, {"not": node},
#   {"cmp": [field, op, value]} (op = <> < > <= >= like "not like"; a string or number
#   value, or null for = / <> NULL) or {"null": [field, negate]}.
#
# The attempts are tried in order until one connection opens (the caller passes
# read-only connections: Mode=Read, ReadOnly=1). Each query runs as
#   SELECT * FROM <table> WHERE <...>
# with every value a ? parameter and every name quoted (a name holding a quote,
# semicolon or control character is refused), so nothing from the request is run as
# SQL text; rows are read up to limit and the reader is cancelled after that
# (truncated). The connection is closed at the end.
#
# The response (stdout, UTF-8): {"ok": true, "attempt": i, "attempts": [errors of the
# attempts before it], "results": [{"columns": [...], "rows": [[...]], "truncated": b}
# or {"error": code, "type": exception type}]}, or {"ok": false, "error": code,
# "attempts": [...]}. An error never carries the provider's message, which can quote
# the connection string or a path.
#
# The work is done in C# (Add-Type) so that values are typed and JSON is written by
# .NET's serializer: no PowerShell pipeline wraps or unrolls them.

$ErrorActionPreference = 'Stop'

Add-Type -ReferencedAssemblies System.Data, System.Web.Extensions, System.Xml -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Data;
using System.Data.Common;
using System.Data.Odbc;
using System.Data.OleDb;
using System.Globalization;
using System.Web.Script.Serialization;

public static class AltiumCopilotDbQuery {
    static readonly string[] Ops = { "=", "<>", "<", ">", "<=", ">=", "like", "not like" };
    const int MaxDepth = 32;
    const int MaxLimit = 1000;

    public static string Run(string requestText) {
        JavaScriptSerializer serializer = new JavaScriptSerializer();
        serializer.MaxJsonLength = int.MaxValue;
        serializer.RecursionLimit = 100;
        Dictionary<string, object> response = new Dictionary<string, object>();
        Dictionary<string, object> request;
        try {
            request = serializer.DeserializeObject(requestText) as Dictionary<string, object>;
            if (request == null) throw new ArgumentException("request");
        } catch (Exception e) {
            response["ok"] = false;
            response["error"] = "bad_request";
            response["type"] = e.GetType().Name;
            return serializer.Serialize(response);
        }
        int timeout = Math.Max(1, Math.Min(ToInt(request, "timeout", 15), 300));
        string left = "[", right = "]";
        object[] quote = Get(request, "quote") as object[];
        if (quote != null && quote.Length == 2 && quote[0] is string && quote[1] is string) {
            string l = (string)quote[0], r = (string)quote[1];
            if ((l == "[" && r == "]") || (l == "`" && r == "`") || (l == "\"" && r == "\"")) {
                left = l;
                right = r;
            }
        }
        object[] attempts = Get(request, "attempts") as object[] ?? new object[0];
        List<object> attemptErrors = new List<object>();
        DbConnection connection = null;
        int used = -1;
        for (int i = 0; i < attempts.Length && connection == null; i++) {
            DbConnection candidate = null;
            try {
                Dictionary<string, object> attempt = attempts[i] as Dictionary<string, object>;
                string kind = Get(attempt, "kind") as string;
                string text = Get(attempt, "connection") as string;
                if (string.IsNullOrEmpty(text)) throw new ArgumentException("connection");
                if (kind == "odbc") {
                    OdbcConnection odbc = new OdbcConnection(text);
                    odbc.ConnectionTimeout = timeout;
                    candidate = odbc;
                } else if (kind == "oledb") {
                    candidate = new OleDbConnection(text);
                } else {
                    throw new ArgumentException("kind");
                }
                candidate.Open();
                connection = candidate;
                used = i;
            } catch (Exception e) {
                if (candidate != null) candidate.Dispose();
                attemptErrors.Add(Error(e));
            }
        }
        response["attempts"] = attemptErrors;
        if (connection == null) {
            response["ok"] = false;
            response["error"] = attemptErrors.Count > 0 ? Pick(attemptErrors) : "no_attempt";
            return serializer.Serialize(response);
        }
        List<object> results = new List<object>();
        try {
            // local_only: only the file's own tables (TABLE_TYPE 'TABLE'), never a linked
            // table, a pass-through or a saved query, which can read another database.
            HashSet<string> local = null;
            if (Get(request, "local_only") is bool && (bool)Get(request, "local_only")) {
                local = LocalTables(connection);
            }
            object[] queries = Get(request, "queries") as object[] ?? new object[0];
            foreach (object item in queries) {
                results.Add(RunQuery(connection, item as Dictionary<string, object>, left, right, timeout, local));
            }
        } finally {
            try { connection.Close(); } catch (Exception) { }
            connection.Dispose();
        }
        response["ok"] = true;
        response["attempt"] = used;
        response["results"] = results;
        return serializer.Serialize(response);
    }

    static object Get(Dictionary<string, object> map, string key) {
        if (map == null || !map.ContainsKey(key)) return null;
        return map[key];
    }

    static int ToInt(Dictionary<string, object> map, string key, int fallback) {
        object value = Get(map, key);
        if (value is int) return (int)value;
        if (value is decimal) return (int)(decimal)value;
        if (value is double) return (int)(double)value;
        return fallback;
    }

    // The most telling error of the attempts: anything but a missing provider.
    static string Pick(List<object> errors) {
        foreach (object item in errors) {
            string code = (string)((Dictionary<string, object>)item)["error"];
            if (code != "provider_not_registered" && code != "unsupported_provider") return code;
        }
        return (string)((Dictionary<string, object>)errors[0])["error"];
    }

    // The names of the base tables of the source; an empty set when the provider cannot
    // list them (every query is then refused).
    static HashSet<string> LocalTables(DbConnection connection) {
        HashSet<string> names = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        try {
            DataTable tables = connection.GetSchema("Tables");
            foreach (DataRow row in tables.Rows) {
                string type = Convert.ToString(row["TABLE_TYPE"], CultureInfo.InvariantCulture);
                if (string.Equals(type, "TABLE", StringComparison.OrdinalIgnoreCase))
                    names.Add(Convert.ToString(row["TABLE_NAME"], CultureInfo.InvariantCulture));
            }
        } catch (Exception) {
            names.Clear();
        }
        return names;
    }

    static Dictionary<string, object> RunQuery(DbConnection connection, Dictionary<string, object> query,
                                               string left, string right, int timeout,
                                               HashSet<string> local) {
        Dictionary<string, object> result = new Dictionary<string, object>();
        try {
            if (query == null) throw new ArgumentException("query");
            string table = Get(query, "table") as string;
            string schema = Get(query, "schema") as string;
            if (local != null && (!string.IsNullOrEmpty(schema) || table == null || !local.Contains(table))) {
                result["error"] = "not_local_table";
                result["type"] = "Refused";
                return result;
            }
            int limit = Math.Max(1, Math.Min(ToInt(query, "limit", 5), MaxLimit));
            List<object> parameters = new List<object>();
            string from = (string.IsNullOrEmpty(schema) ? "" : Quote(schema, left, right) + ".")
                          + Quote(table, left, right);
            string sql = "SELECT * FROM " + from;
            Dictionary<string, object> where = Get(query, "where") as Dictionary<string, object>;
            if (where != null) sql += " WHERE " + Render(where, parameters, left, right, 0);
            using (DbCommand command = connection.CreateCommand()) {
                command.CommandText = sql;
                try { command.CommandTimeout = timeout; } catch (Exception) { }
                for (int i = 0; i < parameters.Count; i++) {
                    DbParameter parameter = command.CreateParameter();
                    parameter.ParameterName = "p" + i.ToString(CultureInfo.InvariantCulture);
                    parameter.Value = parameters[i];
                    command.Parameters.Add(parameter);
                }
                bool truncated = false;
                List<object> columns = new List<object>();
                List<object> rows = new List<object>();
                using (DbDataReader reader = command.ExecuteReader()) {
                    for (int i = 0; i < reader.FieldCount; i++) columns.Add(reader.GetName(i));
                    while (reader.Read()) {
                        if (rows.Count >= limit) {
                            truncated = true;
                            try { command.Cancel(); } catch (Exception) { }
                            break;
                        }
                        List<object> row = new List<object>();
                        for (int i = 0; i < reader.FieldCount; i++) row.Add(Format(reader.GetValue(i)));
                        rows.Add(row);
                    }
                }
                result["columns"] = columns;
                result["rows"] = rows;
                result["truncated"] = truncated;
            }
        } catch (Exception e) {
            return Error(e);
        }
        return result;
    }

    static string Quote(string name, string left, string right) {
        if (string.IsNullOrEmpty(name) || name.Length > 128) throw new ArgumentException("bad_name");
        foreach (char c in name) {
            if (char.IsControl(c) || c == ';' || left.IndexOf(c) >= 0 || right.IndexOf(c) >= 0)
                throw new ArgumentException("bad_name");
        }
        return left + name + right;
    }

    static string Render(Dictionary<string, object> node, List<object> parameters, string left,
                         string right, int depth) {
        if (node == null || depth > MaxDepth) throw new ArgumentException("bad_where");
        foreach (string join in new string[] { "and", "or" }) {
            object[] children = Get(node, join) as object[];
            if (children == null) continue;
            if (children.Length == 0) throw new ArgumentException("bad_where");
            List<string> parts = new List<string>();
            foreach (object child in children)
                parts.Add("(" + Render(child as Dictionary<string, object>, parameters, left, right, depth + 1) + ")");
            return string.Join(join == "and" ? " AND " : " OR ", parts.ToArray());
        }
        if (node.ContainsKey("not")) {
            return "NOT (" + Render(node["not"] as Dictionary<string, object>, parameters, left, right, depth + 1) + ")";
        }
        object[] cmp = Get(node, "cmp") as object[];
        if (cmp != null && cmp.Length == 3) {
            string field = Quote(cmp[0] as string, left, right);
            string op = cmp[1] as string;
            if (op == null || Array.IndexOf(Ops, op) < 0) throw new ArgumentException("bad_where");
            object value = cmp[2];
            if (value == null) {
                if (op == "=") return field + " IS NULL";
                if (op == "<>") return field + " IS NOT NULL";
                throw new ArgumentException("bad_where");
            }
            if (value is string) {
                parameters.Add((string)value);
            } else if (value is int || value is decimal || value is double || value is long) {
                parameters.Add(Convert.ToDouble(value, CultureInfo.InvariantCulture));
            } else {
                throw new ArgumentException("bad_where");
            }
            return field + " " + op.ToUpperInvariant() + " ?";
        }
        object[] isNull = Get(node, "null") as object[];
        if (isNull != null && isNull.Length == 2) {
            string field = Quote(isNull[0] as string, left, right);
            return field + ((isNull[1] is bool && (bool)isNull[1]) ? " IS NOT NULL" : " IS NULL");
        }
        throw new ArgumentException("bad_where");
    }

    static object Format(object value) {
        if (value == null || value is DBNull) return null;
        if (value is double) return ((double)value).ToString("R", CultureInfo.InvariantCulture);
        if (value is float) return ((float)value).ToString("R", CultureInfo.InvariantCulture);
        if (value is decimal) return ((decimal)value).ToString(CultureInfo.InvariantCulture);
        if (value is DateTime) return ((DateTime)value).ToString("s", CultureInfo.InvariantCulture);
        if (value is bool) return ((bool)value) ? "True" : "False";
        if (value is byte[]) return "<binary " + ((byte[])value).Length.ToString(CultureInfo.InvariantCulture) + " bytes>";
        if (value is Guid) return ((Guid)value).ToString("B").ToUpperInvariant();
        return Convert.ToString(value, CultureInfo.InvariantCulture);
    }

    // A fixed code for an exception; its message is read here and never returned.
    static Dictionary<string, object> Error(Exception e) {
        string text = "";
        for (Exception x = e; x != null; x = x.InnerException) text += " " + x.Message;
        text = text.ToLowerInvariant();
        string code = "query_failed";
        if (e is ArgumentException && (e.Message == "bad_name" || e.Message == "bad_where")) code = e.Message;
        else if (text.Contains("msdasql") || text.Contains("does not support the microsoft ole db provider for odbc")) code = "unsupported_provider";
        else if (text.Contains("not registered") || text.Contains("data source name not found") || text.Contains("im002") || text.Contains("specified driver could not be loaded")) code = "provider_not_registered";
        else if (text.Contains("could not find installable isam")) code = "driver_option_unsupported";
        else if (text.Contains("could not find file") || text.Contains("not a valid path") || text.Contains("cannot find the file") || text.Contains("could not find the file")) code = "file_not_found";
        else if (text.Contains("exclusively locked") || text.Contains("already in use") || text.Contains("could not lock") || text.Contains("locked")) code = "file_locked";
        else if (text.Contains("login failed") || text.Contains("not a valid password") || text.Contains("password") || text.Contains("authentication")) code = "login_failed";
        else if (text.Contains("could not find the object") || text.Contains("invalid object name") || text.Contains("cannot find the input table") || text.Contains("doesn't exist") || text.Contains("does not exist")) code = "table_not_found";
        else if (text.Contains("no value given for one or more required parameters") || text.Contains("invalid column name") || text.Contains("too few parameters") || text.Contains("unknown column")) code = "field_not_found";
        else if (text.Contains("data type mismatch") || text.Contains("conversion failed")) code = "type_mismatch";
        else if (text.Contains("timeout") || text.Contains("timed out")) code = "timeout";
        else if (text.Contains("network") || text.Contains("server was not found") || text.Contains("server does not exist")) code = "network_error";
        else if (text.Contains("unrecognized database format") || text.Contains("not a database") || text.Contains("external table is not in the expected format")) code = "unreadable";
        Dictionary<string, object> error = new Dictionary<string, object>();
        error["error"] = code;
        error["type"] = e.GetType().Name;
        return error;
    }
}
'@

$stdin = [Console]::OpenStandardInput()
$reader = New-Object System.IO.StreamReader($stdin, (New-Object System.Text.UTF8Encoding($false)))
$request = $reader.ReadToEnd()
$response = [AltiumCopilotDbQuery]::Run($request)
$bytes = (New-Object System.Text.UTF8Encoding($false)).GetBytes($response)
$out = [Console]::OpenStandardOutput()
$out.Write($bytes, 0, $bytes.Length)
$out.Flush()
