"""Extension settings (roadmap #73): what the user sets in Claude Desktop's settings form
for the extension (manifest.json user_config), or in the server's environment anywhere
else (Claude Code's MCP config, a CI job), read once when the server starts.

Each setting has one environment variable, ALTIUM_COPILOT_<NAME>, which manifest.json's
server.mcp_config.env sets from ${user_config.<key>}. The project folders are the
exception: a field that takes several folders can only reach the server as arguments
(the manifest's args expand it to one argument per folder: --project-folders A B), so
it also has --project-folders, and its variable takes folders separated by ';'.

A value is unset when it is missing, empty or blank, or is a placeholder the host did
not substitute (exactly ${user_config.<key>}: Claude Desktop leaves it in place for an
optional field that has no value); a value with a placeholder inside other text is
checked like any other and fails. Every value is checked here (paths absolute and of
the right kind, units mil or mm, booleans true or false, the profile and policy files
read and parsed); a problem is reported, never raised: the server always starts, and a
value that fails is treated as unset, except the Fab profile and Derating policy files,
which the tools read again at each use (and problems_now checks again): check_dfm and
check_derating answer an error for them (they never check against another profile
instead). With nothing set, every tool answers exactly as before the settings existed.

Precedence wherever a setting is used: the tool's argument, then the setting, then the
tool's own default (the Fab profile: the project's fab_profile.json before the setting;
the Derating policy: the setting's file under the project's derating_policy.json, key by
key). A check pack's tool rules and a review checklist's evidence calls run inside
tool_defaults(): the Units setting does not apply there, so the numbers a pack or a
checklist gives and tests mean the same for every user.

Pure logic, no MCP: main.py activates the settings at start (activate) and its tools
read current().
"""

import contextlib
import contextvars
import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

try:
    from services.custom_checks import PLUGIN_ENV
    from services.derating import DEFAULT_POLICY
    from services.derating import SETTING_VARIABLE as POLICY_VARIABLE
    from services.derating import setting_layer as policy_setting_layer
    from services.fab_profiles import SETTING_VARIABLE as PROFILE_VARIABLE
    from services.fab_profiles import ProfileError, setting_path_problem, setting_profile
except ImportError:
    from server.services.custom_checks import PLUGIN_ENV
    from server.services.derating import DEFAULT_POLICY
    from server.services.derating import SETTING_VARIABLE as POLICY_VARIABLE
    from server.services.derating import setting_layer as policy_setting_layer
    from server.services.fab_profiles import SETTING_VARIABLE as PROFILE_VARIABLE
    from server.services.fab_profiles import (
        ProfileError, setting_path_problem, setting_profile,
    )

FOLDERS_ARGUMENT = "--project-folders"
UNITS = ("mil", "mm")
TRUE = ("true", "1")
FALSE = ("false", "0")
MAX_FOLDERS = 20
MAX_VARIANT_CHARS = 120
MAX_SHOWN_CHARS = 200
# Project discovery under the project folders (detect_altium_project): at most this many
# projects listed, folders visited and levels below each folder.
MAX_PROJECTS = 50
MAX_SCANNED_FOLDERS = 2000
MAX_DEPTH = 4
_PLACEHOLDER = re.compile(r"\$\{\s*user_config\.[^}]*\}")


@dataclass(frozen=True)
class Field:
    """One setting: its manifest user_config key, its environment variable, the
    Settings attribute it fills and its title in the settings form."""

    key: str
    variable: str
    attribute: str
    title: str


FIELDS = (
    Field("project_folders", "ALTIUM_COPILOT_PROJECT_FOLDERS", "project_folders",
          "Project folders"),
    Field("units", "ALTIUM_COPILOT_UNITS", "units", "Units"),
    Field("fab_profile", PROFILE_VARIABLE, "fab_profile", "Fab profile"),
    Field("derating_policy", POLICY_VARIABLE, "derating_policy", "Derating policy"),
    Field("default_variant", "ALTIUM_COPILOT_DEFAULT_VARIANT", "default_variant",
          "Default variant"),
    Field("check_updates", "ALTIUM_COPILOT_CHECK_UPDATES", "check_updates",
          "Check for updates"),
    Field("allow_plugins", PLUGIN_ENV, "allow_plugins", "Allow check-pack Python plugins"),
)
BY_KEY = {item.key: item for item in FIELDS}
# Settings naming a file the tools read again at each use.
FILE_SETTINGS = ("fab_profile", "derating_policy")


@dataclass
class Settings:
    """The settings in effect. Unset values keep the defaults below, which are the
    server's behaviour without settings."""

    project_folders: tuple[str, ...] = ()
    units: str | None = None
    fab_profile: str | None = None  # an absolute path, or altimade / none
    derating_policy: str | None = None  # an absolute path
    default_variant: str | None = None
    check_updates: bool = True
    allow_plugins: bool = False
    problems: list[dict] = field(default_factory=list)

    def applied(self) -> dict:
        """The settings that differ from the defaults: key -> value."""
        default = Settings()
        out: dict = {}
        for item in FIELDS:
            value = getattr(self, item.attribute)
            if value != getattr(default, item.attribute):
                out[item.key] = list(value) if isinstance(value, tuple) else value
        return out

    def is_default(self) -> bool:
        return not self.applied() and not self.problems

    def problems_now(self) -> list[dict]:
        """The problems as they are now: those found at the start, except that the Fab
        profile and Derating policy files are read again (their tools read them at each
        use, so a file fixed or broken since the start is reported as it is now)."""
        out: list[dict] = []
        for item in FIELDS:
            if item.key not in FILE_SETTINGS:
                out += [problem for problem in self.problems
                        if problem["setting"] == item.key]
                continue
            value = getattr(self, item.attribute)
            if value is not None:
                check = _fab_profile if item.key == "fab_profile" else _derating_policy
                check(item, value, out)
        return out

    def summary(self) -> dict | None:
        """detect_altium_project's settings block: what is applied and what could not
        be (problems_now); None when nothing is set and nothing is wrong."""
        if self.is_default():
            return None
        out: dict = {}
        applied = self.applied()
        if applied:
            out["applied"] = applied
        problems = self.problems_now()
        if problems:
            out["problems"] = problems
        out["note"] = (
            "The user's settings (Claude Desktop: the extension's settings; elsewhere the "
            "ALTIUM_COPILOT_* environment variables of the server). An argument given in a "
            "tool call wins over a setting. Tell the user about each problem and how to fix "
            "it: a changed setting applies when the server restarts, a fixed profile or "
            "policy file at its next use."
        )
        if self.units:
            out["units_note"] = (
                f"Every tool that takes units uses '{self.units}' when the call gives none: "
                "the lengths, coordinates and distances in results, and the numbers you pass "
                "(max_distance, x, y, radius...). Pass units= whenever a number comes in the "
                "other unit. Check packs and review checklists keep each tool's own default "
                "unit, whatever this setting."
            )
        return out


# ---------- reading raw values ----------

def unset(value) -> bool:
    """True for a value that sets nothing: None, empty or blank, or exactly a placeholder
    the host left unsubstituted. A placeholder inside other text (Claude Desktop's string
    substitution puts one back for a value containing $&) is a value, and fails its
    check."""
    if value is None:
        return True
    text = str(value).strip()
    return not text or bool(_PLACEHOLDER.fullmatch(text))


def _clean(value: str) -> str:
    """The value trimmed, without one pair of surrounding double quotes."""
    text = str(value).strip()
    if len(text) >= 2 and text[0] == text[-1] == '"':
        text = text[1:-1].strip()
    return text


def _shown(value: str) -> str:
    text = str(value)
    return text if len(text) <= MAX_SHOWN_CHARS else text[:MAX_SHOWN_CHARS - 3] + "..."


def folder_arguments(argv: list[str] | None) -> list[str] | None:
    """The folders given after --project-folders (each argument one folder, up to the
    next option), or after --project-folders=; None when the option is not there."""
    if not argv:
        return None
    found: list[str] | None = None
    items = [str(item) for item in argv]
    index = 0
    while index < len(items):
        item = items[index]
        if item == FOLDERS_ARGUMENT:
            found = found if found is not None else []
            index += 1
            while index < len(items) and not items[index].startswith("--"):
                found.append(items[index])
                index += 1
            continue
        if item.startswith(FOLDERS_ARGUMENT + "="):
            found = found if found is not None else []
            found.append(item.split("=", 1)[1])
        index += 1
    return found


# ---------- validation ----------

def _problem(item: Field, value, message: str) -> dict:
    return {"setting": item.key, "title": item.title, "variable": item.variable,
            "value": _shown(value), "message": message}


def _absolute(text: str) -> Path | None:
    try:
        path = Path(text)
        return path if path.is_absolute() else None
    except (TypeError, ValueError):
        return None


def _same_place(path: Path) -> str:
    """A key that two spellings of one folder share (8.3 short names, '..', links)."""
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError, ValueError):
        resolved = path
    return os.path.normcase(str(resolved))


def _folders(raw: list[str], item: Field, problems: list[dict]) -> tuple[str, ...]:
    folders: list[str] = []
    keys: set[str] = set()
    for value in raw:
        if unset(value):
            continue
        text = _clean(value)
        path = _absolute(text)
        reason = setting_path_problem(path, "folder") if path is not None else None
        if path is None:
            problems.append(_problem(item, text, "not an absolute folder path; the folder "
                                                 "is not used."))
        elif reason and reason.startswith("is not a folder"):
            problems.append(_problem(item, text, "no such folder on this PC (moved, renamed "
                                                 "or on a drive not connected?); it is not "
                                                 "used."))
        elif reason:
            problems.append(_problem(item, text, f"the path {reason}; it is not used."))
        elif (key := _same_place(path)) not in keys:
            keys.add(key)
            folders.append(str(path))
    if len(folders) > MAX_FOLDERS:
        problems.append(_problem(item, f"{len(folders)} folders",
                                 f"more than {MAX_FOLDERS} folders; the first "
                                 f"{MAX_FOLDERS} are used."))
        folders = folders[:MAX_FOLDERS]
    return tuple(folders)


def _boolean(item: Field, value, default: bool, problems: list[dict]) -> bool:
    text = str(value).strip().lower()
    if text in TRUE:
        return True
    if text in FALSE:
        return False
    problems.append(_problem(item, value, f"must be true or false; {str(default).lower()} "
                                          "(the default) applies."))
    return default


def _fab_profile(item: Field, value, problems: list[dict]) -> str:
    text = _clean(value)
    try:
        setting_profile(text)
    except ProfileError as error:
        problems.append(_problem(item, text, (
            f"{error.error.get('message')} check_dfm answers this error until it is fixed "
            "(it never checks against another profile instead).")))
    return text.lower() if text.lower() in ("altimade", "none") else text


def _derating_policy(item: Field, value, problems: list[dict]) -> str:
    text = _clean(value)
    policy = {key: (dict(entry) if isinstance(entry, dict) else entry)
              for key, entry in DEFAULT_POLICY.items()}
    _, _, error = policy_setting_layer(policy, text)
    if error:
        problems.append(_problem(item, text, (
            f"{error.get('message')} check_derating answers this error until it is fixed; "
            "get_circuit_calculations and check_thermal leave the setting out (the "
            "project's derating_policy.json still applies).")))
    return text


def load(environ=None, argv: list[str] | None = None) -> Settings:
    """The settings from the environment (environ, default os.environ) and the server's
    arguments; problems are listed in Settings.problems, never raised."""
    environ = os.environ if environ is None else environ
    settings = Settings()
    problems = settings.problems

    item = BY_KEY["project_folders"]
    raw = [value for value in folder_arguments(argv) or [] if not unset(value)]
    if raw:  # the arguments (Claude Desktop's form) win; their problems name the option
        item = replace(item, variable=FOLDERS_ARGUMENT)
    else:
        text = environ.get(item.variable)
        raw = [] if unset(text) else [part for part in str(text).split(";") if part.strip()]
    settings.project_folders = _folders(raw, item, problems)

    item = BY_KEY["units"]
    value = environ.get(item.variable)
    if not unset(value):
        text = _clean(value).lower()
        if text in UNITS:
            settings.units = text
        else:
            problems.append(_problem(item, value, "must be mil or mm (or empty: each "
                                                  "board's own unit); not applied."))

    item = BY_KEY["fab_profile"]
    value = environ.get(item.variable)
    if not unset(value):
        settings.fab_profile = _fab_profile(item, value, problems)

    item = BY_KEY["derating_policy"]
    value = environ.get(item.variable)
    if not unset(value):
        settings.derating_policy = _derating_policy(item, value, problems)

    item = BY_KEY["default_variant"]
    value = environ.get(item.variable)
    if not unset(value):
        text = _clean(value)
        if len(text) > MAX_VARIANT_CHARS:
            problems.append(_problem(item, text, f"longer than {MAX_VARIANT_CHARS} "
                                                 "characters; not applied."))
        elif _PLACEHOLDER.search(text):
            problems.append(_problem(item, text, (
                "holds a ${user_config...} placeholder the host did not substitute (Claude "
                "Desktop puts one back for a value containing $&); not applied.")))
        else:
            settings.default_variant = text

    item = BY_KEY["check_updates"]
    value = environ.get(item.variable)
    if not unset(value):
        settings.check_updates = _boolean(item, value, True, problems)

    item = BY_KEY["allow_plugins"]
    value = environ.get(item.variable)
    if not unset(value):
        # custom_checks reads the variable itself at each run (plugin_env_enabled): the
        # same rule, so what is reported here is what run_custom_checks does.
        settings.allow_plugins = _boolean(item, value, False, problems)
    return settings


# ---------- the settings in effect ----------

_active: Settings | None = None


def activate(argv: list[str] | None = None, environ=None) -> Settings:
    """Read the settings once, at server start; current() returns them from then on."""
    global _active
    _active = load(environ, argv)
    return _active


def deactivate() -> None:
    """Forget the activated settings (tests): current() reads the environment again."""
    global _active
    _active = None


def current() -> Settings:
    """The activated settings; before activate() (tests, the check command), the
    environment as it is now."""
    return _active if _active is not None else load()


_TOOL_DEFAULTS = contextvars.ContextVar("altium_copilot_tool_defaults", default=False)


@contextlib.contextmanager
def tool_defaults():
    """Inside, the Units setting does not apply: a check pack's tool rules and a review
    checklist's evidence calls (their arguments and the results their conditions test)
    keep each tool's own default unit, the same for every user."""
    token = _TOOL_DEFAULTS.set(True)
    try:
        yield
    finally:
        _TOOL_DEFAULTS.reset(token)


def units_or_setting(units: str | None) -> str | None:
    """A tool's units argument, else the Units setting (not inside tool_defaults); None
    leaves the tool's own default (the board's display unit, or mm)."""
    if units is not None or _TOOL_DEFAULTS.get():
        return units
    return current().units


def file_signature(settings: Settings | None = None) -> tuple:
    """The Fab profile and Derating policy settings' files as (path, mtime, size): a
    cached result read with them is stale once this changes."""
    settings = settings or current()
    out: list = []
    for value in (settings.fab_profile, settings.derating_policy):
        if value is None:
            out.append(None)
            continue
        try:
            stat = Path(value).stat()
            out.append((value, stat.st_mtime_ns, stat.st_size))
        except (OSError, ValueError):
            out.append((value, None))
    return tuple(out)


# ---------- project discovery ----------

def find_projects(folders: tuple[str, ...] | list[str]) -> dict:
    """The .PrjPcb files in and under the folders (MAX_DEPTH levels, hidden and
    __-prefixed folders skipped; at most MAX_SCANNED_FOLDERS folders visited and
    MAX_PROJECTS listed): {"projects": [{name, dir}], "complete": bool}."""
    projects: list[dict] = []
    seen: set[str] = set()
    visited = 0
    complete = True
    for folder in folders:
        stack = [(Path(folder), 0)]
        while stack:
            path, depth = stack.pop()
            visited += 1
            if visited > MAX_SCANNED_FOLDERS:
                return {"projects": projects, "complete": False}
            try:
                entries = sorted(path.iterdir(), key=lambda entry: entry.name.lower())
            except OSError:
                continue
            subfolders = []
            for entry in entries:
                try:
                    if entry.is_dir():
                        if not entry.name.startswith((".", "__")) and depth < MAX_DEPTH:
                            subfolders.append(entry)
                        continue
                    if entry.suffix.lower() != ".prjpcb" or not entry.is_file():
                        continue
                except OSError:
                    continue
                key = _same_place(entry)  # one file reached by two spellings is one
                if key in seen:
                    continue
                seen.add(key)
                if len(projects) >= MAX_PROJECTS:
                    complete = False
                    break
                projects.append({"name": entry.name, "dir": str(entry.parent)})
            if not complete:
                return {"projects": projects, "complete": False}
            stack.extend((sub, depth + 1) for sub in reversed(subfolders))
    return {"projects": projects, "complete": complete}
