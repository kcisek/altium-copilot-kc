"""Extension settings (roadmap #73): manifest.json's user_config against the MCPB v0.4
rules, the values reaching the server as Claude Desktop substitutes them (the reference
implementation of @anthropic-ai/mcpb, ported below), unset, empty and placeholder values,
each validation message, precedence (a tool argument, then the setting, then the old
default), the plugin switch, the check command, and unchanged results with nothing set."""

import json
import os
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import cli
import server.main as main
from tests.test_cli import isolated, make_project, run, run_json  # noqa: F401
from tests.test_circuit_calcs import res
from tests.test_custom_checks import project  # noqa: F401
from tests.test_dfm_tool import board  # noqa: F401
from tests.test_netlist_free_load import (  # noqa: F401
    LITE_VARIANT, PRJ_TEXT, _load, env, offline_env,
)
from tests.test_pcb_index import pcb_data  # noqa: F401
from tests.test_pull_resistors import _netlist

jsonschema = pytest.importorskip("jsonschema")

REPO = Path(__file__).resolve().parents[1]
MANIFEST = json.loads((REPO / "manifest.json").read_text(encoding="utf-8"))
EXAMPLE_PROFILE = REPO / "examples" / "fab_profile.json"
EXAMPLE_POLICY = REPO / "examples" / "derating_policy.json"
settings = main.app_settings  # the module main.py uses (either package path)
fab = sys.modules[main.load_profile.__module__]
derating = sys.modules[main.load_derating_policy.__module__]
checks = sys.modules[main.plugins_allowed.__module__]
circuit = sys.modules[main.prepare_circuit_calculations.__module__]
thermal = sys.modules[main.prepare_thermal.__module__]
VARIABLES = {item.key: item.variable for item in settings.FIELDS}

# The MCPB manifest schema v0.4 (github.com/modelcontextprotocol/mcpb,
# schemas/mcpb-manifest-v0.4.schema.json): a user_config option and server.mcp_config.
USER_CONFIG_OPTION = {
    "type": "object",
    "properties": {
        "type": {"type": "string",
                 "enum": ["string", "number", "boolean", "directory", "file"]},
        "title": {"type": "string"},
        "description": {"type": "string"},
        "required": {"type": "boolean"},
        "default": {"anyOf": [{"type": "string"}, {"type": "number"}, {"type": "boolean"},
                              {"type": "array", "items": {"type": "string"}}]},
        "multiple": {"type": "boolean"},
        "sensitive": {"type": "boolean"},
        "min": {"type": "number"},
        "max": {"type": "number"},
    },
    "required": ["type", "title", "description"],
    "additionalProperties": False,
}
STRINGS = {"type": "array", "items": {"type": "string"}}
ENV = {"type": "object", "additionalProperties": {"type": "string"}}
MCP_CONFIG = {
    "type": "object",
    "properties": {
        "command": {"type": "string"}, "args": STRINGS, "env": ENV,
        "platform_overrides": {"type": "object", "additionalProperties": {
            "type": "object", "properties": {"command": {"type": "string"}, "args": STRINGS,
                                             "env": ENV},
            "additionalProperties": False}},
    },
    "required": ["command"],
    "additionalProperties": False,
}
PLACEHOLDER = re.compile(r"\$\{user_config\.([^}]+)\}")


@pytest.fixture(autouse=True)
def fresh_settings():
    """No activated settings before or after a test (conftest clears the variables)."""
    settings.deactivate()
    yield
    settings.deactivate()


# ---------- the reference substitution (@anthropic-ai/mcpb src/shared/config.ts) ----------

def _replace(value, variables: dict):
    """replaceVariables: strings get each variable's text (an array is not put into a
    string: the placeholder stays); an array item that is exactly one user_config
    placeholder expands to the value, or to each value of an array; one with no value
    (missing, or an empty string: JavaScript's falsy) stays as written."""
    if isinstance(value, str):
        for key, replacement in variables.items():
            pattern = "${" + key + "}"
            if pattern in value and not isinstance(replacement, list):
                value = value.replace(pattern, replacement)
        return value
    if isinstance(value, list):
        out = []
        for item in value:
            match = re.fullmatch(r"\$\{(user_config\.[^}]+)\}", item) \
                if isinstance(item, str) else None
            if match:
                replacement = variables.get(match.group(1))
                if replacement is None or replacement == "":
                    out.append(item)
                elif isinstance(replacement, list):
                    out.extend(replacement)
                else:
                    out.append(replacement)
            else:
                out.append(_replace(item, variables))
        return out
    if isinstance(value, dict):
        return {key: _replace(item, variables) for key, item in value.items()}
    return value


def desktop_config(user_values: dict | None = None, manifest: dict = MANIFEST) -> dict:
    """getMcpConfigForManifest: the defaults, then the user's values, as text (booleans
    'true'/'false', arrays kept for expansion), substituted into mcp_config."""
    merged = {key: option["default"] for key, option in manifest["user_config"].items()
              if "default" in option}
    merged.update(user_values or {})
    variables: dict = {"__dirname": r"C:\ext\altium-copilot-kc"}
    for key, value in merged.items():
        if isinstance(value, list):
            variables[f"user_config.{key}"] = [str(item) for item in value]
        elif isinstance(value, bool):
            variables[f"user_config.{key}"] = "true" if value else "false"
        else:
            variables[f"user_config.{key}"] = str(value)
    return _replace(manifest["server"]["mcp_config"], variables)


def started(user_values: dict | None = None, manifest: dict = MANIFEST):
    """The settings of a server Claude Desktop starts with these form values."""
    config = desktop_config(user_values, manifest)
    return settings.load(config.get("env") or {}, config.get("args") or [])


def _without_defaults() -> dict:
    manifest = json.loads(json.dumps(MANIFEST))
    for option in manifest["user_config"].values():
        option.pop("default", None)
    return manifest


def _write(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return path


def _project_tree(root: Path) -> Path:
    for name in ("A/Alpha.PrjPcb", "B/sub/Beta.PrjPcb", ".hidden/Gamma.PrjPcb",
                 "__Previews/Delta.PrjPcb", "C/d1/d2/d3/d4/Deep.PrjPcb"):
        _write(root / name, "[Design]\n")
    return root


# ---------- the manifest ----------

def test_the_manifest_version_is_unchanged():
    assert MANIFEST["manifest_version"] == "0.4"
    assert MANIFEST["server"]["type"] == "binary"
    assert MANIFEST["server"]["entry_point"] == "server/dist/altium-copilot.exe"


def test_user_config_follows_the_mcpb_schema():
    assert isinstance(MANIFEST["user_config"], dict) and MANIFEST["user_config"]
    for key, option in MANIFEST["user_config"].items():
        jsonschema.validate(option, USER_CONFIG_OPTION)
        assert re.fullmatch(r"[a-z][a-z0-9_]*", key)
        assert option["title"].strip() and len(option["description"]) >= 60
        assert option.get("required") is False, key
        assert not option.get("sensitive"), "no secret is a setting"
        assert not re.search(r"token|password|secret|api_?key", key)
        if option.get("multiple"):
            assert option["type"] in ("directory", "file")
        assert "min" not in option and "max" not in option or option["type"] == "number"
        default = option.get("default")
        expected = {"boolean": bool, "string": str, "file": str, "number": (int, float),
                    "directory": list if option.get("multiple") else str}[option["type"]]
        assert isinstance(default, expected), key
    jsonschema.validate(MANIFEST["server"]["mcp_config"], MCP_CONFIG)


def test_placeholders_and_fields_match_both_ways():
    config = MANIFEST["server"]["mcp_config"]
    env, args = config["env"], config["args"]
    used = {name for text in [*env.values(), *args] for name in PLACEHOLDER.findall(text)}
    assert used == set(MANIFEST["user_config"])
    assert {item.key for item in settings.FIELDS} == set(MANIFEST["user_config"])
    for item in settings.FIELDS:
        option = MANIFEST["user_config"][item.key]
        assert option["title"] == item.title
        placeholder = "${user_config." + item.key + "}"
        if option.get("multiple"):
            # Only an args item that is exactly the placeholder expands to several values.
            assert placeholder not in env.values()
            index = args.index(placeholder)
            assert args[index - 1] == settings.FOLDERS_ARGUMENT
        else:
            assert env[item.variable] == placeholder
    assert set(env) == {item.variable for item in settings.FIELDS
                        if not MANIFEST["user_config"][item.key].get("multiple")}
    assert all(name.startswith("ALTIUM_COPILOT_") for name in VARIABLES.values())


def test_the_defaults_are_todays_behaviour():
    default = settings.Settings()
    for item in settings.FIELDS:
        option = MANIFEST["user_config"][item.key]
        value = getattr(default, item.attribute)
        if option["type"] == "boolean":
            assert option["default"] is value
        else:
            assert not option["default"] and not value


def test_conftest_clears_every_setting_variable():
    import conftest
    assert set(conftest.SETTING_VARIABLES) == set(VARIABLES.values())


def test_the_plugin_variable_is_custom_checks_own():
    assert VARIABLES["allow_plugins"] == checks.PLUGIN_ENV == "ALTIUM_COPILOT_ALLOW_PLUGINS"
    assert VARIABLES["fab_profile"] == fab.SETTING_VARIABLE
    assert VARIABLES["derating_policy"] == derating.SETTING_VARIABLE


# ---------- values reaching the server ----------

def test_an_untouched_form_sets_nothing():
    config = desktop_config()
    assert config["args"] == [settings.FOLDERS_ARGUMENT]  # an empty list expands to nothing
    assert config["env"]["ALTIUM_COPILOT_UNITS"] == ""
    assert config["env"]["ALTIUM_COPILOT_CHECK_UPDATES"] == "true"
    assert config["env"]["ALTIUM_COPILOT_ALLOW_PLUGINS"] == "false"
    result = started()
    assert result.is_default() and result.summary() is None
    assert result == settings.Settings()


def test_placeholders_left_by_the_host_are_unset():
    manifest = _without_defaults()
    config = desktop_config({}, manifest)
    assert config["args"] == [settings.FOLDERS_ARGUMENT, "${user_config.project_folders}"]
    assert config["env"]["ALTIUM_COPILOT_UNITS"] == "${user_config.units}"
    assert started({}, manifest).is_default()
    # Empty values: an env string is substituted to "", an args item keeps its placeholder.
    config = desktop_config({key: "" for key in MANIFEST["user_config"]}, manifest)
    assert config["env"]["ALTIUM_COPILOT_FAB_PROFILE"] == ""
    assert config["args"][-1] == "${user_config.project_folders}"
    assert settings.load(config["env"], config["args"]).is_default()


@pytest.mark.parametrize("value", [None, "", "   ", "${user_config.units}",
                                   " ${ user_config.units } "])
def test_unset_values(value):
    assert settings.unset(value)
    assert not settings.unset("mm") and not settings.unset("0")


def test_a_placeholder_inside_a_value_is_reported(tmp_path):
    """The host's string substitution (JavaScript's replace) puts the placeholder back for
    a value containing $&: such a value is not unset, and fails its check."""
    def host(key: str, value: str) -> str:  # String.prototype.replace's $& and $$
        placeholder = "${user_config." + key + "}"
        return value.replace("$$", "\0").replace("$&", placeholder).replace("\0", "$")

    variant = host("default_variant", "Prod$&")
    assert variant == "Prod${user_config.default_variant}" and not settings.unset(variant)
    problem = _problem({"ALTIUM_COPILOT_DEFAULT_VARIANT": variant}, "default_variant")
    assert "placeholder the host did not substitute" in problem["message"]
    assert settings.load({"ALTIUM_COPILOT_DEFAULT_VARIANT": variant}).default_variant is None
    profile = host("fab_profile", str(tmp_path / "fab$&.json"))
    problem = _problem({"ALTIUM_COPILOT_FAB_PROFILE": profile}, "fab_profile")
    assert "not a file on this PC" in problem["message"]
    # Kept, as any profile file that cannot be read: check_dfm answers the error.
    assert settings.load({"ALTIUM_COPILOT_FAB_PROFILE": profile}).fab_profile == profile
    assert "must be mil or mm" in _problem(
        {"ALTIUM_COPILOT_UNITS": host("units", "m$&")}, "units")["message"]


def test_every_field_reaches_the_server(tmp_path):
    one, two = (tmp_path / "Boards").resolve(), (tmp_path / "Archive").resolve()
    one.mkdir()
    two.mkdir()
    profile = _write(tmp_path / "fab" / "our_fab.json", {"limits": {"min_trace_mm": 0.1}})
    policy = _write(tmp_path / "policy.json", {"policy": {"ceramic_max_ratio": 0.6}})
    config = desktop_config({
        "project_folders": [str(one), str(two)], "units": "mm", "fab_profile": str(profile),
        "derating_policy": str(policy), "default_variant": "Lite", "check_updates": False,
        "allow_plugins": True,
    })
    assert config["args"] == [settings.FOLDERS_ARGUMENT, str(one), str(two)]
    result = settings.load(config["env"], config["args"])
    assert result.problems == []
    assert result.project_folders == (str(one), str(two))
    assert (result.units, result.fab_profile, result.derating_policy) == (
        "mm", str(profile), str(policy))
    assert (result.default_variant, result.check_updates, result.allow_plugins) == (
        "Lite", False, True)
    assert set(result.applied()) == set(MANIFEST["user_config"])
    # The variables alone, as Claude Code or a CI job sets them.
    environ = {**config["env"], "ALTIUM_COPILOT_PROJECT_FOLDERS": f"{one};{two}"}
    assert settings.load(environ, []) == result


def test_the_arguments_win_over_the_folder_variable(tmp_path):
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()
    environ = {"ALTIUM_COPILOT_PROJECT_FOLDERS": str(two)}
    assert settings.load(environ, [settings.FOLDERS_ARGUMENT, str(one)]).project_folders \
        == (str(one),)
    assert settings.load(environ, [settings.FOLDERS_ARGUMENT]).project_folders == (str(two),)
    assert settings.load(environ, [f"{settings.FOLDERS_ARGUMENT}={one}"]).project_folders \
        == (str(one),)
    assert settings.folder_arguments([settings.FOLDERS_ARGUMENT, "a", "b", "--other", "c"]) \
        == ["a", "b"]
    assert settings.folder_arguments(["--version"]) is None
    problem = settings.load({}, [settings.FOLDERS_ARGUMENT, str(tmp_path / "gone")]).problems[0]
    assert (problem["setting"], problem["variable"]) == ("project_folders",
                                                         settings.FOLDERS_ARGUMENT)


def test_values_are_trimmed_and_unquoted(tmp_path):
    result = settings.load({"ALTIUM_COPILOT_UNITS": " MM ",
                            "ALTIUM_COPILOT_DEFAULT_VARIANT": "  Prod ",
                            "ALTIUM_COPILOT_FAB_PROFILE": f'"{EXAMPLE_PROFILE}"',
                            "ALTIUM_COPILOT_CHECK_UPDATES": "FALSE",
                            "ALTIUM_COPILOT_ALLOW_PLUGINS": " 1 "})
    assert result.problems == []
    assert (result.units, result.default_variant, result.fab_profile) == (
        "mm", "Prod", str(EXAMPLE_PROFILE))
    assert result.check_updates is False and result.allow_plugins is True
    assert settings.load({"ALTIUM_COPILOT_FAB_PROFILE": "Altimade"}).fab_profile == "altimade"


# ---------- validation ----------

def _problem(environ: dict, key: str) -> dict:
    result = settings.load(environ, [])
    found = [problem for problem in result.problems if problem["setting"] == key]
    assert len(found) == 1, result.problems
    assert found[0]["variable"] == VARIABLES[key]
    assert found[0]["title"] == settings.BY_KEY[key].title
    return found[0]


def test_bad_units(tmp_path):
    problem = _problem({"ALTIUM_COPILOT_UNITS": "inch"}, "units")
    assert "must be mil or mm" in problem["message"] and problem["value"] == "inch"
    assert settings.load({"ALTIUM_COPILOT_UNITS": "inch"}).units is None


def test_bad_folders(tmp_path):
    file = _write(tmp_path / "a.txt", "x")
    good = tmp_path / "good"
    good.mkdir()
    (tmp_path / "other").mkdir()
    # The same folder spelt three ways: another case (a case-insensitive file system
    # only, as on Windows), through '..', and again as given.
    spellings = [str(tmp_path / "other" / ".." / "good"), str(good)]
    if os.path.normcase("A") == os.path.normcase("a"):
        spellings.append(str(good).upper())
    environ = {"ALTIUM_COPILOT_PROJECT_FOLDERS": ";".join(
        ["relative\\Boards", str(tmp_path / "missing"), str(file), str(good), *spellings])}
    result = settings.load(environ)
    messages = [problem["message"] for problem in result.problems]
    assert len(messages) == 3, messages
    assert "not an absolute folder path" in messages[0]
    assert "no such folder" in messages[1]
    assert messages[2] == "the path is a file, not a folder; it is not used."
    assert result.project_folders == (str(good),)  # one folder, however spelt
    many = [tmp_path / f"f{index}" for index in range(settings.MAX_FOLDERS + 2)]
    for folder in many:
        folder.mkdir()
    result = settings.load({"ALTIUM_COPILOT_PROJECT_FOLDERS": ";".join(map(str, many))})
    assert len(result.project_folders) == settings.MAX_FOLDERS
    assert "the first 20 are used" in result.problems[0]["message"]


def test_path_messages_say_what_is_wrong(tmp_path, monkeypatch):
    deep = tmp_path / ("d" * 100) / ("e" * 100) / ("f" * 100)
    monkeypatch.setattr(fab, "WINDOWS", True)  # Windows' MAX_PATH, on any runner
    assert fab.setting_path_problem(deep, "folder").startswith(
        f"is {len(str(deep))} characters long, more than Windows opens without long-path "
        "support")
    message = _problem({"ALTIUM_COPILOT_PROJECT_FOLDERS": str(deep)},
                       "project_folders")["message"]
    assert message.startswith("the path is ") and "use a shorter path" in message
    assert "characters long" in _problem({"ALTIUM_COPILOT_FAB_PROFILE": str(deep / "p.json")},
                                         "fab_profile")["message"]
    monkeypatch.setattr(fab, "WINDOWS", False)
    assert fab.setting_path_problem(deep, "folder").startswith("is not a folder on this PC")
    assert fab.setting_path_problem(tmp_path, "file") == "is a folder, not a file"
    assert "is a folder, not a file" in _problem(
        {"ALTIUM_COPILOT_DERATING_POLICY": str(tmp_path)}, "derating_policy")["message"]
    assert fab.setting_path_problem(Path("relative.json")) == "is not an absolute path"


def test_a_setting_file_reached_through_a_link_is_read(tmp_path):
    """The user chose the file: a link to a file elsewhere is followed (a project's own
    fab_profile.json and derating_policy.json never are)."""
    profile = _write(tmp_path / "real" / "our_fab.json", {"limits": {"min_trace_mm": 0.1}})
    policy = _write(tmp_path / "real" / "policy.json", {"policy": {"ceramic_max_ratio": 0.6}})
    links = tmp_path / "links"
    links.mkdir()
    try:
        os.symlink(profile, links / "fab.json")
        os.symlink(policy, links / "policy.json")
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links cannot be made here")
    result = settings.load({"ALTIUM_COPILOT_FAB_PROFILE": str(links / "fab.json"),
                            "ALTIUM_COPILOT_DERATING_POLICY": str(links / "policy.json")})
    assert result.problems == []
    assert fab.setting_profile(str(links / "fab.json")).chosen_by == "setting"
    info, error = derating.load_policy(None, setting=str(links / "policy.json"))
    assert error is None and info["values"]["ceramic_max_ratio"] == 0.6


def test_bad_fab_profiles_are_reported_and_kept(tmp_path):
    missing = tmp_path / "gone.json"
    problem = _problem({"ALTIUM_COPILOT_FAB_PROFILE": str(missing)}, "fab_profile")
    assert "ALTIUM_COPILOT_FAB_PROFILE" in problem["message"]
    assert "not a file on this PC" in problem["message"]
    assert "never checks against another profile" in problem["message"]
    # Kept: check_dfm answers the error instead of checking against Altimade.
    assert settings.load({"ALTIUM_COPILOT_FAB_PROFILE": str(missing)}).fab_profile \
        == str(missing)
    assert "not an absolute path" in _problem(
        {"ALTIUM_COPILOT_FAB_PROFILE": "fab.json"}, "fab_profile")["message"]
    broken = _write(tmp_path / "broken.json", '{"limits": {')
    assert "not valid JSON at line 1" in _problem(
        {"ALTIUM_COPILOT_FAB_PROFILE": str(broken)}, "fab_profile")["message"]
    unknown = _write(tmp_path / "unknown.json", {"limitz": {}})
    assert "unknown key 'limitz'" in _problem(
        {"ALTIUM_COPILOT_FAB_PROFILE": str(unknown)}, "fab_profile")["message"]
    huge = _write(tmp_path / "huge.json", {"limits": {"min_trace_mm": 5}})
    assert "min_trace" in _problem(
        {"ALTIUM_COPILOT_FAB_PROFILE": str(huge)}, "fab_profile")["message"]
    for good in (str(EXAMPLE_PROFILE), "altimade", "NONE"):
        assert settings.load({"ALTIUM_COPILOT_FAB_PROFILE": good}).problems == []


def test_bad_derating_policies(tmp_path):
    problem = _problem({"ALTIUM_COPILOT_DERATING_POLICY": str(tmp_path / "x.json")},
                       "derating_policy")
    assert "ALTIUM_COPILOT_DERATING_POLICY" in problem["message"]
    bad = _write(tmp_path / "bad.json", {"policy": {"ceramic_max_ratio": 2}})
    message = _problem({"ALTIUM_COPILOT_DERATING_POLICY": str(bad)},
                       "derating_policy")["message"]
    assert "ceramic_max_ratio must be a ratio" in message and "bad.json" in message
    unknown = _write(tmp_path / "unknown.json", {"rules": {}})
    assert "unknown key 'rules'" in _problem(
        {"ALTIUM_COPILOT_DERATING_POLICY": str(unknown)}, "derating_policy")["message"]
    assert settings.load({"ALTIUM_COPILOT_DERATING_POLICY": str(EXAMPLE_POLICY)}).problems \
        == []


def test_bad_switches_and_variant():
    problem = _problem({"ALTIUM_COPILOT_CHECK_UPDATES": "never"}, "check_updates")
    assert "must be true or false; true (the default) applies" in problem["message"]
    assert settings.load({"ALTIUM_COPILOT_CHECK_UPDATES": "never"}).check_updates is True
    problem = _problem({"ALTIUM_COPILOT_ALLOW_PLUGINS": "yes"}, "allow_plugins")
    assert "false (the default) applies" in problem["message"]
    assert settings.load({"ALTIUM_COPILOT_ALLOW_PLUGINS": "yes"}).allow_plugins is False
    long = "V" * (settings.MAX_VARIANT_CHARS + 1)
    assert "longer than" in _problem({"ALTIUM_COPILOT_DEFAULT_VARIANT": long},
                                     "default_variant")["message"]
    assert settings.load({"ALTIUM_COPILOT_DEFAULT_VARIANT": long}).default_variant is None


def test_nothing_ever_raises(tmp_path):
    garbage = {variable: value for variable, value in zip(
        VARIABLES.values(), ["\0bad", "x" * 5000, "<>:|?*", "\\\\?\\", "{", "maybe", "2"])}
    result = settings.load(garbage, [settings.FOLDERS_ARGUMENT, "\0", "", "C:"])
    assert len(result.problems) >= 5
    summary = result.summary()
    assert summary["problems"] == result.problems and "note" in summary
    assert all(len(problem["value"]) <= settings.MAX_SHOWN_CHARS for problem in result.problems)


# ---------- the plugin switch ----------

@pytest.mark.parametrize("value, runs", [
    ("1", True), ("true", True), ("TRUE", True), (" true ", True), ("0", False),
    ("false", False), ("yes", False), ("", False), ("${user_config.allow_plugins}", False),
])
def test_the_plugin_switch_keeps_its_meaning(value, runs):
    ok, reason = checks.plugins_allowed(True, {checks.PLUGIN_ENV: value})
    assert ok is runs
    assert settings.load({checks.PLUGIN_ENV: value}).allow_plugins is runs
    # The call must still ask: the setting alone never runs a plugin.
    assert checks.plugins_allowed(False, {checks.PLUGIN_ENV: value})[0] is False
    if not runs:  # the variable is there but off: the reason names the extension's switch
        for allow in (True, False):
            why = checks.plugins_allowed(allow, {checks.PLUGIN_ENV: value})[1]
            assert "Allow check-pack Python plugins in the extension's settings" in why
            assert f"{checks.PLUGIN_ENV}=1 or true where the server is started" in why


def test_the_plugin_reason_is_unchanged_without_the_variable():
    _, reason = checks.plugins_allowed(True, {})
    assert reason == (f"not run: the server's environment does not set {checks.PLUGIN_ENV}=1 "
                      "(the user sets it where the server is started; it cannot be set from "
                      "a call)")
    assert checks.plugins_allowed(False, {})[1] == (
        f"not run: call with allow_plugins=true, and start the server with "
        f"{checks.PLUGIN_ENV}=1 in its environment")


def test_the_plugin_setting_is_off_and_described_by_default():
    option = MANIFEST["user_config"]["allow_plugins"]
    assert option["type"] == "boolean" and option["default"] is False
    assert "Off by default" in option["description"]
    assert "only when you also ask" in option["description"]
    assert "trust" in option["description"]
    assert started().allow_plugins is False


# ---------- precedence and wiring ----------

def test_units_argument_then_setting_then_the_boards(board, monkeypatch):
    def units(**arguments):
        return json.loads(main.check_dfm(profile="none", **arguments))["units"]

    assert units() == "mil"  # the board's display unit
    monkeypatch.setenv("ALTIUM_COPILOT_UNITS", "mm")
    assert units() == "mm"
    assert units(units="mil") == "mil"
    assert settings.units_or_setting(None) == "mm"
    assert settings.units_or_setting("mil") == "mil"
    monkeypatch.setenv("ALTIUM_COPILOT_UNITS", "furlong")  # not applied
    assert units() == "mil"


def test_every_tool_with_units_reads_the_setting():
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(main))
    tools = 0
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        if not any(isinstance(item, ast.Call) and ast.unparse(item.func) == "mcp.tool"
                   for item in node.decorator_list):
            continue
        if "units" not in [arg.arg for arg in node.args.args + node.args.kwonlyargs]:
            continue
        tools += 1
        first = ast.unparse(node.body[1])
        assert first == "units = app_settings.units_or_setting(units)", node.name
    assert tools >= 35


def test_fab_profile_precedence(board, monkeypatch):
    root, _ = board
    profile = _write(root / "team" / "our_fab.json",
                     {"name": "Our fab", "limits": {"min_trace_mm": 0.09}})

    def chosen(**arguments):
        result = json.loads(main.check_dfm(**arguments))
        return result.get("error") or result["profile"]["chosen_by"], result

    assert chosen()[0] == "default"
    monkeypatch.setenv("ALTIUM_COPILOT_FAB_PROFILE", str(profile))
    by, result = chosen()
    assert by == "setting"
    assert result["profile"]["file"] == "our_fab.json"
    assert "(the user's Fab profile setting)" in result["scope"]
    assert chosen(profile="altimade")[0] == "argument"
    _write(root / "fab_profile.json", {"limits": {"min_trace_mm": 0.1}})
    assert chosen()[0] == "project_file"
    (root / "fab_profile.json").unlink()
    monkeypatch.setenv("ALTIUM_COPILOT_FAB_PROFILE", "none")
    assert chosen()[1]["profile"]["name"] == "none"
    monkeypatch.setenv("ALTIUM_COPILOT_FAB_PROFILE", str(root / "gone.json"))
    error, result = chosen()
    assert error == "profile_not_found"  # never Altimade in its place
    assert result["setting"] == "ALTIUM_COPILOT_FAB_PROFILE"
    assert result["hint"] == fab.SETTING_HINT
    assert chosen(profile="altimade")[0] == "argument"


def test_fab_profile_setting_in_the_review_and_the_release_gate(tmp_path):
    lines = main.sys.modules[main.build_manufacturing_review.__module__].profile_lines
    assert "defaults to Altimade" in lines(str(tmp_path))[0]
    assert "uses the user's Fab profile setting (a profile file)" in lines(
        str(tmp_path), str(EXAMPLE_PROFILE))[0]
    assert "(Altimade's published limits)" in lines(str(tmp_path), "altimade")[0]
    broken = lines(str(tmp_path), str(tmp_path / "gone.json"))[0]
    assert "setting cannot be used" in broken and "profile_not_found" in broken
    gate = sys.modules[main.build_release_readiness.__module__]
    assert "fab_profile_setting" in gate.ReleaseInputs.__dataclass_fields__
    assert "setting=ctx.inputs.fab_profile_setting" in __import__("inspect").getsource(
        gate.gate_fiducials)


def test_derating_policy_layers(tmp_path):
    setting = _write(tmp_path / "team" / "team_policy.json", {
        "name": "Team policy", "policy": {"ceramic_max_ratio": 0.6, "signal_max_v": 3.3}})
    project = tmp_path / "project"
    info, error = derating.load_policy(str(project), None, setting=str(setting))
    assert error is None and info["chosen_by"] == "setting"
    assert info["values"]["ceramic_max_ratio"] == 0.6
    assert (info["setting_file"], info["set_by_setting"], info["name"]) == (
        "team_policy.json", ["ceramic_max_ratio", "signal_max_v"], "Team policy")
    _write(project / "derating_policy.json", {"policy": {"ceramic_max_ratio": 0.7}})
    info, _ = derating.load_policy(str(project), {"signal_max_v": 2.5}, setting=str(setting))
    assert info["chosen_by"] == "setting+project_file+argument"
    assert (info["values"]["ceramic_max_ratio"], info["values"]["signal_max_v"]) == (0.7, 2.5)
    info, _ = derating.load_policy(str(project), None)  # no setting: as before
    assert info["chosen_by"] == "project_file" and "setting_file" not in info
    _, error = derating.load_policy(str(project), None, setting=str(tmp_path / "gone.json"))
    assert error["error"] == "invalid_policy"
    assert error["setting"] == "ALTIUM_COPILOT_DERATING_POLICY"
    assert list(error)[-1] == "hint"


def test_check_derating_reads_the_policy_setting(offline_env, monkeypatch, tmp_path):
    root, _, _ = offline_env
    _load(root)
    assert json.loads(main.check_derating())["policy"]["chosen_by"] == "default"
    setting = _write(tmp_path / "team.json", {"policy": {"ceramic_max_ratio": 0.6}})
    monkeypatch.setenv("ALTIUM_COPILOT_DERATING_POLICY", str(setting))
    result = json.loads(main.check_derating())
    assert result["policy"]["chosen_by"] == "setting"
    assert result["policy"]["values"]["ceramic_max_ratio"] == 0.6
    monkeypatch.setenv("ALTIUM_COPILOT_DERATING_POLICY", str(tmp_path / "gone.json"))
    assert json.loads(main.check_derating())["error"] == "invalid_policy"


def _resistor_limits_used(monkeypatch, module, call) -> tuple[float, list[str]]:
    """The 0603 resistor power limit module's Board was given, and the call's notes."""
    seen = []
    real = module.Board

    def board(netlist, plan, params, rails, vref, policy):
        seen.append(policy["resistor_power_w"]["0603"])
        return real(netlist, plan, params, rails, vref, policy)

    monkeypatch.setattr(module, "Board", board)
    result = call()
    return seen[-1], result["notes"]


@pytest.mark.parametrize("module_name", ["circuit", "thermal"])
def test_resistor_limits_follow_the_policy_layers(tmp_path, monkeypatch, module_name):
    """get_circuit_calculations and check_thermal read the resistor limits as
    check_derating does: the setting's file under the project's derating_policy.json. A
    setting that cannot be read is left out with a note: the project file still applies."""
    module = circuit if module_name == "circuit" else thermal
    prepare = (circuit.prepare_circuit_calculations if module is circuit
               else thermal.prepare_thermal)
    netlist = _netlist([res("R1", "5V", "GND", "400", footprint="0603")])
    project = tmp_path / "project"
    team = _write(tmp_path / "team.json", {"policy": {"resistor_power_w": {"0603": 0.5}}})
    gone = str(tmp_path / "gone.json")
    default = derating.DEFAULT_POLICY["resistor_power_w"]["0603"]

    def limit(setting=None):
        return _resistor_limits_used(monkeypatch, module, lambda: prepare(
            netlist, None, root_dir=str(project), policy_setting=setting))

    assert limit() == (default, [])
    assert limit(str(team)) == (0.5, [])
    rating, notes = limit(gone)
    assert rating == default and len(notes) == 1
    assert notes[0].startswith("The Derating policy setting (ALTIUM_COPILOT_DERATING_POLICY) "
                               "names")
    assert "The setting is left out here: the default resistor" in notes[0]
    _write(project / "derating_policy.json", {"policy": {"resistor_power_w": {"0603": 0.25}}})
    assert limit() == (0.25, [])
    assert limit(str(team)) == (0.25, [])  # the project file wins, key by key
    rating, notes = limit(gone)
    assert rating == 0.25  # the project's file is kept when the setting fails
    assert notes[0].endswith("The setting is left out here: derating_policy.json in the "
                             "project folder applies over the defaults.")
    # A broken project file: the defaults, as before the settings existed.
    _write(project / "derating_policy.json", "{")
    rating, notes = limit()
    assert rating == default and notes[0].startswith("derating_policy.json could not be read")
    rating, notes = limit(gone)
    assert rating == default and len(notes) == 2
    assert notes[0].endswith("The setting is left out here.")


def test_check_thermal_reads_the_policy_setting(offline_env, monkeypatch, tmp_path):
    root, _, _ = offline_env
    _load(root)
    seen = {}
    real = thermal.prepare_thermal

    def capture(*args, **kwargs):
        seen["setting"] = kwargs.get("policy_setting")
        return real(*args, **kwargs)

    monkeypatch.setattr(main, "prepare_thermal", capture)
    main.check_thermal()
    assert seen["setting"] is None
    setting = _write(tmp_path / "team.json", {"policy": {"ceramic_max_ratio": 0.6}})
    monkeypatch.setenv("ALTIUM_COPILOT_DERATING_POLICY", str(setting))
    main.check_thermal()
    assert seen["setting"] == str(setting)


def test_default_variant_is_made_active(env, monkeypatch):
    root, _, _ = env
    (root / "Board.PrjPcb").write_text(PRJ_TEXT + LITE_VARIANT, encoding="utf-8")
    assert _load(root)["variants"] == ["Prod", "Lite"]
    assert main._variant_state.active.name == "Prod"
    monkeypatch.setenv("ALTIUM_COPILOT_DEFAULT_VARIANT", "lite")
    result = _load(root)
    assert result["active_variant"] == "Lite"
    assert "the user's Default variant setting picks it" in result["default_variant_note"]
    assert json.loads(main.list_variants())["active_variant"] == "Lite"
    # The user's switch survives a refresh (which reloads a partial load).
    main.set_active_variant("Prod")
    assert json.loads(main.refresh_netlist())["active_variant"] == "Prod"
    monkeypatch.setenv("ALTIUM_COPILOT_DEFAULT_VARIANT", "Nope")
    result = _load(root)
    assert main._variant_state.active.name == "Prod"
    assert "names 'Nope', which this project does not define" in result[
        "default_variant_note"]
    assert "active_variant" not in result


def test_problems_reach_the_project_load(env, monkeypatch):
    root, _, _ = env
    monkeypatch.setenv("ALTIUM_COPILOT_UNITS", "inch")
    result = _load(root)
    assert result["loaded"] is True
    assert [problem["setting"] for problem in result["settings_problems"]] == ["units"]


def test_detect_reports_settings_folders_and_updates(tmp_path, monkeypatch):
    state = _write(tmp_path / "state.json", {"update_available": "999.0.0"})
    folders = _project_tree(tmp_path / "Projects")

    def detect() -> dict:
        with patch.object(main._altium, "get_status", return_value={"running": False}), \
                patch.object(main, "read_registry", return_value={"projects": []}), \
                patch.object(main, "STATE_PATH", state):
            return json.loads(main.detect_altium_project())

    plain = detect()
    assert plain["update_available"] == "999.0.0"
    assert "settings" not in plain and "project_folders" not in plain
    monkeypatch.setenv("ALTIUM_COPILOT_CHECK_UPDATES", "false")
    monkeypatch.setenv("ALTIUM_COPILOT_PROJECT_FOLDERS", f"{folders};{tmp_path / 'gone'}")
    result = detect()
    assert "update_available" not in result and "update_command" not in result
    assert result["settings"]["applied"] == {"project_folders": [str(folders)],
                                             "check_updates": False}
    assert result["settings"]["problems"][0]["setting"] == "project_folders"
    found = result["project_folders"]["projects"]
    assert [item["name"] for item in found] == ["Alpha.PrjPcb", "Beta.PrjPcb"]
    assert found[0]["dir"] == str(folders / "A")
    assert "set_project_dir(dir)" in result["project_folders"]["note"]


def test_an_extension_is_not_told_to_run_the_installer(tmp_path, monkeypatch):
    """install.ps1 would register a second copy beside the extension: the update notice
    of an extension (its manifest.json two folders above the executable) says to install
    a newer .mcpb instead."""
    state = _write(tmp_path / "state.json", {"update_available": "999.0.0"})
    extension = tmp_path / "extensions" / "altium-copilot-kc"
    exe = _write(extension / "server" / "dist" / "altium-copilot.exe", "")

    def detect() -> dict:
        with patch.object(main._altium, "get_status", return_value={"running": False}), \
                patch.object(main, "read_registry", return_value={"projects": []}), \
                patch.object(main, "STATE_PATH", state):
            return json.loads(main.detect_altium_project())

    monkeypatch.setattr(main.sys, "frozen", True, raising=False)
    monkeypatch.setattr(main.sys, "executable", str(exe))
    assert "update_command" in detect()  # no manifest.json beside it: install.ps1's copy
    _write(extension / "manifest.json", "{}")
    result = detect()
    assert result["update_available"] == "999.0.0" and "update_command" not in result
    assert ".mcpb" in result["update_note"] and "Do not suggest install.ps1" in result[
        "update_note"]
    monkeypatch.setenv("ALTIUM_COPILOT_CHECK_UPDATES", "false")
    assert "update_note" not in detect() and "update_available" not in detect()


def test_file_problems_are_reported_as_they_are_now(tmp_path, monkeypatch):
    """A Fab profile or Derating policy file fixed (or broken) after the start is
    reported as it is now: the tools read it at each use."""
    profile = _write(tmp_path / "fab.json", '{"limits": {')
    policy = _write(tmp_path / "policy.json", {"policy": {"ceramic_max_ratio": 0.6}})
    active = settings.activate([], {"ALTIUM_COPILOT_FAB_PROFILE": str(profile),
                                    "ALTIUM_COPILOT_DERATING_POLICY": str(policy),
                                    "ALTIUM_COPILOT_UNITS": "inch"})
    assert [p["setting"] for p in active.problems] == ["units", "fab_profile"]
    _write(profile, {"limits": {"min_trace_mm": 0.1}})
    _write(policy, {"policy": {"ceramic_max_ratio": 2}})
    assert [p["setting"] for p in active.problems_now()] == ["units", "derating_policy"]
    assert active.summary()["problems"] == active.problems_now()
    loaded = json.loads(main._apply_load_settings('{"loaded": false}'))
    assert [p["setting"] for p in loaded["settings_problems"]] == ["units", "derating_policy"]
    _write(policy, {"policy": {"ceramic_max_ratio": 0.6}})
    with patch.object(main._altium, "get_status", return_value={"running": False}), \
            patch.object(main, "read_registry", return_value={"projects": []}), \
            patch.object(main, "STATE_PATH", tmp_path / "state.json"):
        detected = json.loads(main.detect_altium_project())
    assert [p["setting"] for p in detected["settings"]["problems"]] == ["units"]


def test_the_units_note_comes_with_the_units_setting():
    assert "units_note" not in settings.load({"ALTIUM_COPILOT_CHECK_UPDATES": "false"}
                                             ).summary()
    note = settings.load({"ALTIUM_COPILOT_UNITS": "mil"}).summary()["units_note"]
    assert "uses 'mil' when the call gives none" in note
    assert "the numbers you pass" in note and "Check packs and review checklists" in note


def test_packs_and_checklists_keep_each_tools_own_unit(monkeypatch):
    """A pack's tool rule or a checklist's evidence call means the same numbers for every
    user: the Units setting does not apply inside them."""
    seen = []

    def check_dfm(units=None, **arguments):
        seen.append(settings.units_or_setting(units))
        return "{}"

    monkeypatch.setattr(main, "check_dfm", check_dfm)
    monkeypatch.setenv("ALTIUM_COPILOT_UNITS", "mil")
    main.check_dfm()
    main._custom_tools()["check_dfm"]()
    main._review_tools()["check_dfm"]()
    main._review_tools()["check_dfm"](units="mm")
    main.check_dfm()
    assert seen == ["mil", None, None, "mm", "mil"]
    # The wrappers keep the tools' signatures (packs and checklists check their arguments).
    import inspect
    assert inspect.signature(main._custom_tools()["check_decoupling"]) == inspect.signature(
        main.check_decoupling)
    with settings.tool_defaults():
        assert settings.units_or_setting(None) is None
    assert settings.units_or_setting(None) == "mil"


@pytest.mark.parametrize("wrapped", [True, False])
def test_a_pack_run_is_the_same_whatever_the_units(project, monkeypatch, wrapped):
    """A team's pack rule written for the tool's documented default (max_distance in mm)
    answers the same text with the Units setting; without the tool_defaults wrapper (the
    control) the same rule would read 3 mil and fail."""
    _write(project / "check_packs.json", {
        "id": "team", "title": "Team", "version": "1", "rules": [{
            "id": "DEC-T", "title": "Decoupling capacitors within 3 mm", "severity": "error",
            "tool": "check_decoupling", "args": {"max_distance": 3},
            "fail": [{"list": "findings", "where": {"check": "far_capacitor"},
                      "label": "capacitors farther than 3 mm"}]}]})

    def run() -> str:
        text = main.run_custom_checks()
        assert '"id": "DEC-T"' in text
        return re.sub(r'"elapsed_ms": \d+', '"elapsed_ms": 0', text)

    if not wrapped:
        monkeypatch.setattr(main, "_with_tool_defaults", lambda tool: tool)
    plain = run()
    monkeypatch.setattr(main, "_custom_cache", {})
    monkeypatch.setenv("ALTIUM_COPILOT_UNITS", "mil")
    assert (run() == plain) is wrapped


def test_cached_pack_and_checklist_runs_follow_the_setting_files(tmp_path, monkeypatch):
    profile = _write(tmp_path / "fab.json", {"limits": {"min_trace_mm": 0.1}})
    monkeypatch.setenv("ALTIUM_COPILOT_FAB_PROFILE", str(profile))
    first = settings.file_signature()
    assert first[0][0] == str(profile) and first[1] is None
    built = []
    monkeypatch.setattr(main, "build_review_checklist",
                        lambda *args, **kwargs: built.append(1) or {"built": len(built)})
    monkeypatch.setattr(main, "_review_cache", {})
    root = tmp_path / "Board"
    root.mkdir()
    project = {"root_dir": str(root), "prj_pcb_path": str(root / "Board.PrjPcb")}
    netlist: dict = {}

    def review():
        return main._review_model(project, netlist, None, {"id": "pcb"}, None, None, {})

    assert review() == review() == {"built": 1}
    _write(profile, {"limits": {"min_trace_mm": 0.125}})
    assert settings.file_signature() != first
    assert review() == {"built": 2}


def test_project_discovery_limits(tmp_path, monkeypatch):
    root = _project_tree(tmp_path / "Projects")
    found = settings.find_projects([str(root)])
    names = [item["name"] for item in found["projects"]]
    assert names == ["Alpha.PrjPcb", "Beta.PrjPcb"] and found["complete"]  # Deep: 5 levels
    monkeypatch.setattr(settings, "MAX_DEPTH", 5)
    assert "Deep.PrjPcb" in [item["name"] for item in settings.find_projects([str(root)])[
        "projects"]]
    monkeypatch.setattr(settings, "MAX_PROJECTS", 1)
    capped = settings.find_projects([str(root), str(root / "A")])
    assert len(capped["projects"]) == 1 and capped["complete"] is False
    monkeypatch.setattr(settings, "MAX_PROJECTS", 50)
    monkeypatch.setattr(settings, "MAX_SCANNED_FOLDERS", 2)
    assert settings.find_projects([str(root)])["complete"] is False
    # A project folder given twice (itself and its parent) is listed once.
    monkeypatch.setattr(settings, "MAX_SCANNED_FOLDERS", 2000)
    twice = settings.find_projects([str(root / "A"), str(root)])
    assert [item["name"] for item in twice["projects"]].count("Alpha.PrjPcb") == 1


def test_where_used_searches_the_project_folders(tmp_path, monkeypatch):
    folders = _project_tree(tmp_path / "Projects")
    other = _write(tmp_path / "Other" / "Other.PrjPcb", "[Design]\n").parent
    seen = {}

    def capture(inputs):
        seen["projects"] = inputs.projects
        return {"error": "captured", "message": "stop"}

    monkeypatch.setattr(main, "_require_project", lambda netlist=True: ({}, None, None))
    monkeypatch.setattr(main, "_library_context", lambda project, netlist: ([], None, {}, None))
    monkeypatch.setattr(main, "read_registry", lambda: {"projects": []})
    monkeypatch.setattr(main, "run_library_checks", capture)

    def projects(**arguments) -> list[str]:
        main.check_libraries(where_used="R0402", **arguments)
        return sorted(Path(path).name for path in seen["projects"])

    assert projects() == []
    monkeypatch.setenv("ALTIUM_COPILOT_PROJECT_FOLDERS", str(folders))
    assert projects() == ["Alpha.PrjPcb", "Beta.PrjPcb", "Deep.PrjPcb", "Delta.PrjPcb",
                          "Gamma.PrjPcb"]  # project_files walks every level, as folders= does
    assert projects(folders=[str(other)]) == ["Other.PrjPcb"]  # the argument wins


def test_the_server_start_reads_the_settings_once(monkeypatch, tmp_path):
    folder = tmp_path / "Boards"
    folder.mkdir()
    monkeypatch.setattr(main.mcp, "run", lambda: None)
    monkeypatch.setattr(main.sys, "stdout", main.sys.stdout)
    monkeypatch.setattr(main.sys, "stderr", main.sys.stderr)
    thread = MagicMock()
    monkeypatch.setattr(main.threading, "Thread", thread)
    monkeypatch.setenv("ALTIUM_COPILOT_CHECK_UPDATES", "false")
    monkeypatch.setenv("ALTIUM_COPILOT_UNITS", "cubits")
    with patch.object(main.logging, "warning") as warning:
        main._serve([settings.FOLDERS_ARGUMENT, str(folder)])
    thread.assert_not_called()  # no update check
    assert "cubits" not in str(warning.call_args) and "Units" in str(warning.call_args)
    active = settings.current()
    assert active.project_folders == (str(folder),)
    monkeypatch.setenv("ALTIUM_COPILOT_UNITS", "mm")  # read once: no change now
    assert settings.current().units is None
    settings.deactivate()
    monkeypatch.delenv("ALTIUM_COPILOT_CHECK_UPDATES")
    main._serve([])
    thread.assert_called_once()
    with patch.object(main, "_serve") as serve:
        assert main.run_entry([settings.FOLDERS_ARGUMENT, str(folder)]) == 0
    serve.assert_called_once_with([settings.FOLDERS_ARGUMENT, str(folder)])


# ---------- unchanged results ----------

DESKTOP_DEFAULTS = desktop_config()["env"]
PLACEHOLDERS = desktop_config({}, _without_defaults())["env"]


@pytest.mark.parametrize("environ", [DESKTOP_DEFAULTS, PLACEHOLDERS])
def test_results_are_unchanged_at_the_defaults(env, monkeypatch, tmp_path, environ):
    """detect_altium_project, set_project_dir, list_variants, get_board_info, check_dfm and
    get_board_view answer the same text with nothing set and with the extension's
    untouched form (its defaults, or its placeholders left in place)."""
    root, _, _ = env
    state = _write(tmp_path / "state.json", {"update_available": "999.0.0"})

    def answers() -> list[str]:
        with patch.object(main, "STATE_PATH", state):
            texts = [main.detect_altium_project(), main.set_project_dir(str(root))]
        texts += [main.list_variants(), main.get_board_info(), main.check_dfm(),
                  main.get_board_view(nets="GND")]
        return texts

    before = answers()
    for name, value in environ.items():
        monkeypatch.setenv(name, value)
    assert settings.current().is_default()
    assert answers() == before


def test_the_load_result_is_not_rewritten_without_settings():
    text = '{"loaded": true,  "odd":   "spacing"}'
    assert main._apply_load_settings(text) is text


# ---------- the check command ----------

TWO_VARIANTS = ("[ProjectVariant1]\nDescription=Prod\nVariationCount=0\n"
                "[ProjectVariant2]\nDescription=Lite\nVariationCount=0\n")


def _two_variant_project(root: Path) -> Path:
    prj = make_project(root)
    prj.write_text(prj.read_text(encoding="utf-8") + TWO_VARIANTS, encoding="utf-8")
    return prj


def test_the_check_command_reads_the_same_variables(tmp_path, isolated, monkeypatch):
    prj = _two_variant_project(tmp_path / "Board")
    argv = ["--checklist", "none", "--checks", "list_variants"]
    code, result = run_json(tmp_path, prj, *argv)
    assert code == 0 and result["project"]["variant"] == "Prod"
    assert "settings" not in result["options"]
    monkeypatch.setenv("ALTIUM_COPILOT_DEFAULT_VARIANT", "lite")
    monkeypatch.setenv("ALTIUM_COPILOT_CHECK_UPDATES", "sometimes")  # not read here
    code, result = run_json(tmp_path, prj, *argv)
    assert code == 0 and result["project"]["variant"] == "Lite"
    assert result["options"]["settings"] == {"default_variant": "lite"}
    code, result = run_json(tmp_path, prj, *argv, "--variant", "prod")
    assert result["project"]["variant"] == "Prod"  # the option wins
    monkeypatch.setenv("ALTIUM_COPILOT_DEFAULT_VARIANT", "Nope")
    code, result = run_json(tmp_path, prj, *argv)
    assert code == 0 and result["project"]["variant"] == "Prod"
    assert any("names 'Nope'" in note for note in result["notes"])


def test_a_setting_that_cannot_be_applied_fails_the_check_command(tmp_path, isolated,
                                                                  monkeypatch, capsys):
    prj = make_project(tmp_path / "Board")
    monkeypatch.setenv("ALTIUM_COPILOT_UNITS", "inch")
    code, result = run_json(tmp_path, prj, "--checklist", "none", "--checks", "run_erc")
    assert code == 2
    assert result["errors"][0]["error"] == "invalid_setting"
    assert "ALTIUM_COPILOT_UNITS (Units): must be mil or mm" in result["errors"][0]["message"]
    assert "ALTIUM_COPILOT_UNITS" in capsys.readouterr().err
    assert cli.UNUSED_SETTINGS == ("check_updates",)


def test_settings_a_run_can_do_without_are_notes(tmp_path, isolated, monkeypatch):
    """A plugin switch other than true or 1 leaves the plugins off, as it always did, and
    the project folders serve only a pack's where-used rule: notes, not exit 2."""
    prj = make_project(tmp_path / "Board")
    argv = ["--checklist", "none", "--checks", "run_erc"]
    base_code, base = run_json(tmp_path, prj, *argv)
    monkeypatch.setenv("ALTIUM_COPILOT_ALLOW_PLUGINS", "yes")
    monkeypatch.setenv("ALTIUM_COPILOT_PROJECT_FOLDERS", str(tmp_path / "gone"))
    code, result = run_json(tmp_path, prj, *argv)
    assert code == base_code != 2 and result["errors"] == []
    notes = [note for note in result["notes"] if note.startswith("Setting not applied")]
    assert len(notes) == 2
    assert "ALTIUM_COPILOT_PROJECT_FOLDERS (Project folders): no such folder" in notes[0]
    assert "ALTIUM_COPILOT_ALLOW_PLUGINS (Allow check-pack Python plugins)" in notes[1]
    assert "settings" not in result["options"]
