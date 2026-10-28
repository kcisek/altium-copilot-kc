"""Fabricator capability profiles for check_dfm: the built-in Altimade limits, profile
files in the project folder and the limits argument, in one key vocabulary.

Pure logic, no MCP. The keys are the same in a profile file, in the limits argument and
in the echoed profile: a length ends in _mm or _mil (min_trace_mm, min_spacing_mil), and
so does a board size (a number, or [a, b] either way round); ratios, percentages and
counts take no suffix (max_aspect_ratio, tolerance_percent, max_drill_sizes, layer_counts,
copper_oz, max_placements). Inside, every length is in mil under its bare name (min_trace).
The assembly limits (max_unique_parts, max_placements, min_part_spacing, min_part_to_edge,
min_global_fiducials, local_fiducial_pitch, min_pitch) are an assembler's: only the two
part counts are published by Altimade, the others stay unset until someone gives them.

With no profile argument and no fab_profile.json in the project folder, the user's Fab
profile setting (ALTIUM_COPILOT_FAB_PROFILE, the extension's settings in Claude Desktop:
a profile file anywhere on the PC, or altimade or none) is used before Altimade, chosen_by
setting. A setting that cannot be used is an error, as a broken project file is: another
profile is never substituted.

A specific limit falls back to its generic one unless it is set or removed: the via, PTH
and NPTH drills to min_drill, the via and PTH rings to min_annular_ring, the via and pad
hole-to-hole distances to min_hole_to_hole, a hole wall to copper to min_spacing and a
hole to the board edge to min_copper_to_edge. Layers apply lowest first: the base profile
(based_on "altimade", else nothing), the file's limits, then the limits argument. null
removes a limit set below and stops its fallback, so {"min_npth_drill_mm": null} leaves
non-plated holes unchecked while the other holes keep min_drill.

Every value is checked here, whatever its source (a file, or the limits argument, which
main.py types loosely so that a wrong type reaches this check): bools, strings and other
types are refused, and a length outside what any fab offers is refused, with the other
unit suggested only when the value read in it is plausible.

Altimade is Altium's single-click ordering service; its page lists the limits a board
must meet before it can be ordered. Values come from the page's mm figures, the smaller
ones where its mil and mm disagree (0.075 mm against 2.955 mil, 0.101 mm against
3.995 mil), and each keeps its text as published so that a client can quote it. The
service-only limits (layer counts, board size, copper weight, unique parts and
placements) say what Altimade will order, not what a fabricator or assembler can build:
info when Altimade is only the default, error (copper weight: warning) when someone
chose it. Altimade also checks a minimum component pitch but publishes no number, so
min_pitch is not set by it.
"""

import json
import math
import os
import re
from pathlib import Path

MM = 1 / 0.0254  # mil per mm
DEFAULT_FILE = "fab_profile.json"
BUILTIN_NAMES = ("altimade", "none")
CHOSEN_BY = ("argument", "project_file", "setting", "default")
SETTING_VARIABLE = "ALTIUM_COPILOT_FAB_PROFILE"
NAME_CHARS = 80
SOURCE_CHARS = 200
MAX_FILE_BYTES = 64 * 1024
MAX_PATH_CHARS = 259  # a longer path opens on Windows only with long-path support
WINDOWS = os.name == "nt"
FILES_LISTED = 20

# name -> kind. length: mil, keys name_mm / name_mil; size: a board size, the same
# keys, stored as (short, long) mil; ratio, percent, count, layers (copper layer
# counts accepted), weight (copper oz): no suffix.
LIMIT_KEYS: dict[str, str] = {
    "min_trace": "length",
    "min_spacing": "length",
    "min_annular_ring": "length",
    "min_via_annular_ring": "length",
    "min_pth_annular_ring": "length",
    "min_drill": "length",
    "min_via_drill": "length",
    "min_pth_drill": "length",
    "min_npth_drill": "length",
    "max_drill": "length",
    "min_hole_to_hole": "length",
    "min_via_hole_to_hole": "length",
    "min_pad_hole_to_hole": "length",
    "min_hole_to_copper": "length",  # a hole wall to copper of another object
    "min_copper_to_edge": "length",
    "min_hole_to_edge": "length",  # a hole wall to the board outline or a cutout
    "min_route_size": "length",  # read as the narrowest routed slot; info only
    "drill_allowance": "length",  # drilled = finished + allowance, for a second ratio
    "max_aspect_ratio": "ratio",
    "max_microvia_aspect_ratio": "ratio",
    "tolerance_percent": "percent",
    "layer_counts": "layers",
    "min_board_size": "size",
    "max_board_size": "size",
    "max_drill_sizes": "count",
    "copper_oz": "weight",
    # Assembly (DFA). Counts of fitted parts: distinct parts (BOM lines) and placements.
    "max_unique_parts": "count",
    "max_placements": "count",
    "min_part_spacing": "length",  # same-side neighbours, body or pad copper edge to edge
    "min_part_to_edge": "length",  # a part's pad copper or body to the board edge
    "min_global_fiducials": "count",  # per assembled side
    "local_fiducial_pitch": "length",  # parts at or below this pad pitch want local ones
    "min_pitch": "length",  # the finest pad pitch the assembler places
}
FALLBACKS = {
    "min_via_annular_ring": "min_annular_ring",
    "min_pth_annular_ring": "min_annular_ring",
    "min_via_drill": "min_drill",
    "min_pth_drill": "min_drill",
    "min_npth_drill": "min_drill",
    "min_via_hole_to_hole": "min_hole_to_hole",
    "min_pad_hole_to_hole": "min_hole_to_hole",
    "min_hole_to_copper": "min_spacing",
    "min_hole_to_edge": "min_copper_to_edge",
}
DEFAULTS = {"tolerance_percent": 2.0, "drill_allowance": 0.0}
# Service-only limits and their severity when the profile was chosen.
SERVICE_LIMITS = {
    "layer_counts": "error",
    "min_board_size": "error",
    "max_board_size": "error",
    "copper_oz": "warning",
    "max_unique_parts": "error",
    "max_placements": "error",
}
_SUFFIXED = frozenset({"length", "size"})
VALID_KEYS: tuple[str, ...] = tuple(
    key
    for name, kind in LIMIT_KEYS.items()
    for key in ((f"{name}_mm", f"{name}_mil") if kind in _SUFFIXED else (name,))
)
# Larger values are above any fab's capability, mostly mil typed as mm; they would
# also make the gap searches slow (spacing at 1 mm: about 8 s on Altium's MiniPC,
# against 1.6 s at 0.127 mm). 1 mm leaves room for heavy-copper spacing.
_MAX_MM = {
    "min_trace": 1.0,
    "min_spacing": 1.0,
    "min_annular_ring": 1.0,
    "min_via_annular_ring": 1.0,
    "min_pth_annular_ring": 1.0,
    "min_drill": 10.0,
    "min_via_drill": 10.0,
    "min_pth_drill": 10.0,
    "min_npth_drill": 10.0,
    "max_drill": 10.0,
    "min_hole_to_hole": 1.0,
    "min_via_hole_to_hole": 1.0,
    "min_pad_hole_to_hole": 1.0,
    "min_hole_to_copper": 1.0,
    "min_copper_to_edge": 5.0,
    "min_hole_to_edge": 5.0,
    "min_route_size": 10.0,
    "drill_allowance": 1.0,
    "min_board_size": 2000.0,
    "max_board_size": 2000.0,
    # Assembly: 5 mm between parts covers rework room around a BGA, 10 mm to the edge a
    # conveyor rail; the pitches cover 0.1 inch headers.
    "min_part_spacing": 5.0,
    "min_part_to_edge": 10.0,
    "local_fiducial_pitch": 2.54,
    "min_pitch": 2.54,
}
# Smaller values (other than 0, which a minimum may be) are below any fab's capability,
# mostly mm typed as mil: such a limit would pass nearly any board. 0.0127 mm is 0.5 mil.
_MIN_MM = {
    "min_trace": 0.0127,
    "min_spacing": 0.0127,
    "min_annular_ring": 0.0127,
    "min_via_annular_ring": 0.0127,
    "min_pth_annular_ring": 0.0127,
    "min_drill": 0.025,
    "min_via_drill": 0.025,
    "min_pth_drill": 0.025,
    "min_npth_drill": 0.025,
    "max_drill": 0.1,
    "min_hole_to_hole": 0.0127,
    "min_via_hole_to_hole": 0.0127,
    "min_pad_hole_to_hole": 0.0127,
    "min_hole_to_copper": 0.0127,
    "min_copper_to_edge": 0.0127,
    "min_hole_to_edge": 0.0127,
    "min_route_size": 0.1,
    "drill_allowance": 0.0127,
    "min_board_size": 0.2,
    "max_board_size": 25.0,
    # Assembly: no assembler asks for parts closer than 0.05 mm or a part edge clearance
    # below 0.1 mm (copper to edge already needs more), and no pad pitch is under 0.1 mm.
    "min_part_spacing": 0.05,
    "min_part_to_edge": 0.1,
    "local_fiducial_pitch": 0.1,
    "min_pitch": 0.1,
}
# Counts with a plausible ceiling: an assembler asks for a few global fiducials per side
# (three is usual); more is a typo that would fail every board.
_MAX_COUNT = {"min_global_fiducials": 10}
# The other unit is suggested only when the value read in it is in range and at least
# this many mil, so that following the suggestion never gives a near-zero limit.
HINT_FLOOR_MIL = 1.0
MAX_TOLERANCE_PERCENT = 20.0
MAX_LAYERS = 64
MAX_COPPER_OZ = 20.0
_FILE_KEYS = ("name", "based_on", "source", "source_updated", "comment", "limits")

# Letters, digits, space, _ . - only: no folders, drives or alternate streams (':').
_FILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _.-]{0,80}(\.json)?")
# Windows device names, with or without an extension.
_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{n}" for n in range(10)}
    | {f"LPT{n}" for n in range(10)}
)

ALTIMADE_URL = (
    "https://www.altium.com/documentation/altium-designer/preparing-for-manufacture/"
    "single-click-ordering-with-altimade"
)
# The page's table "Constraint" / "Range/value accepted by Altimade" (updated
# 2026-10-13): name -> (value, mil for lengths; text as published; row).
_ALTIMADE_LIMITS = {
    "min_trace": (0.075 * MM, "2.955 mil (0.075mm)", "Minimum trace width"),
    "min_spacing": (0.127 * MM, "5 mil (0.127mm)", "Minimum spacing"),
    "min_annular_ring": (0.1016 * MM, "4 mil (0.1016mm)", "Minimum annular ring"),
    "min_copper_to_edge": (0.254 * MM, "10 mil (0.254mm)", "Clearances from edge of board"),
    "min_drill": (0.101 * MM, "3.995 mil (0.101mm)", "Minimum drill size"),
    "max_aspect_ratio": (10.0, "10", "Max drill ratio"),
    "min_route_size": (1.0 * MM, "39.37 mil (1.0mm)", "Minimum route size"),
    "min_board_size": (
        (2.54 * MM, 2.54 * MM), "0.10 - 14.88 in (2.54 - 377.952mm)", "Board width, Board height"),
    "max_board_size": (
        (377.952 * MM, 377.952 * MM), "0.10 - 14.88 in (2.54 - 377.952mm)",
        "Board width, Board height"),
    "layer_counts": ((2, 4, 6, 8), "2, 4, 6, 8", "Layer count"),
    "copper_oz": (1.0, "1 oz", "Inner copper weight, Outer copper weight"),
    # Assembly rows (violations "Max Unique Component Count Exceeded", "Max Total
    # Component Placement Count Exceeded"). Its pitch check ("Min Detected Component
    # Pitch Is Too Low") has no published number: min_pitch stays unset.
    "max_unique_parts": (200, "200", "Max number of unique components"),
    "max_placements": (600, "600", "Max total placements"),
}
ALTIMADE = {
    "name": "Altimade",
    "source": ALTIMADE_URL,
    "source_updated": "2026-10-13",
    "limits": {
        name: {"value": value, "published": published, "row": row, "source": ALTIMADE_URL}
        for name, (value, published, row) in _ALTIMADE_LIMITS.items()
    },
    # Published rows this tool does not compare with.
    "not_checked": {
        "board_thickness": (
            "Altimade's thickness row (0.2 / 0.4 / 0.6 mm for 2 / 4 / 6 layers, no 8-layer "
            "value) is ambiguous; confirm the board thickness with Altimade."),
        "paste_silkscreen": (
            "Altimade's minimum paste aperture 12 mil (0.3048mm) and minimum silkscreen "
            "size 5 mil (0.127mm): paste and silkscreen are not checked by this tool."),
    },
}
NOT_FOUND_HINT = (
    "Ask the user for the fabricator's limits and pass them in limits (suffixed keys, e.g. "
    '{"min_spacing_mm": 0.1}) with profile=\'none\', or save them as <name>.json in the '
    "project folder. Never substitute another profile."
)
SETTING_HINT = (
    "The user's Fab profile setting names this profile: show the user what is wrong and ask "
    "them to fix the file or the setting (Claude Desktop: the extension's settings; "
    f"elsewhere the {SETTING_VARIABLE} environment variable), or pass profile= for this "
    "call. Never substitute another profile."
)
INVALID_PROFILE_HINT = (
    "Show the user what to fix in the file (with the line and column when given) and call "
    "again once it is fixed. Never substitute another profile: pass profile='altimade' or "
    "'none' only when the user asks to ignore the file."
)


class ProfileError(ValueError):
    """A profile file or limits argument that cannot be used; .error is the result dict."""

    def __init__(self, error: dict):
        super().__init__(error["message"])
        self.error = error


def external_key(name: str, to_mm: bool) -> str:
    """The key a client reads and passes for limit name in these units: min_trace_mm or
    min_trace_mil for a length or board size, the bare name otherwise."""
    if LIMIT_KEYS[name] in _SUFFIXED:
        return f"{name}_mm" if to_mm else f"{name}_mil"
    return name


class Profile:
    """The limits check_dfm compares with: the base profile, a file and the limits
    argument applied in that order. Lengths are mil; get() returns None for a limit not
    set (the check then does not run). name and source are user text from a file:
    echo them, never put them in issue texts."""

    def __init__(self, key: str, name: str, chosen_by: str, *, source: str | None = None,
                 source_updated: str | None = None, based_on: str | None = None,
                 file: str | None = None):
        self.key = key  # "altimade", "none" or the file name
        self.name = name
        self.chosen_by = chosen_by
        self.source = source
        self.source_updated = source_updated
        self.based_on = based_on
        self.file = file
        self.not_checked: dict[str, str] = {}
        self.argument_count = 0
        # name -> value, None where a layer removed it; origin: builtin, file, argument.
        self._values: dict[str, object] = {}
        self._origins: dict[str, str] = {}
        self._published: dict[str, str] = {}
        self._sources: dict[str, str] = {}

    def _apply(self, values: dict, origin: str, source: str | None = None) -> None:
        for name, value in values.items():
            self._values[name] = value
            self._origins[name] = origin
            self._published.pop(name, None)
            self._sources.pop(name, None)
            if source:
                self._sources[name] = source

    def _inherit_altimade(self, notes: bool = True) -> None:
        """Altimade's limits as the base layer; notes: also its not_checked texts, which
        speak of ordering from Altimade (the built-in profile only, not a file based on
        it)."""
        for name, entry in ALTIMADE["limits"].items():
            self._values[name] = entry["value"]
            self._origins[name] = "builtin"
            self._published[name] = entry["published"]
            self._sources[name] = entry["source"]
        if notes:
            self.not_checked.update(ALTIMADE["not_checked"])

    # ---------- lookups ----------

    def resolved(self, key: str) -> str | None:
        """The limit whose value get(key) returns: key itself, its generic limit when
        key is not set, or None when neither is set (or key was removed)."""
        if key not in LIMIT_KEYS:
            raise KeyError(key)
        while key is not None:
            if key in self._values:
                return key if self._values[key] is not None else None
            key = FALLBACKS.get(key)
        return None

    def get(self, key: str):
        """The limit (mil for lengths; (short, long) mil for board sizes; sorted ints
        for layer_counts; a number otherwise), following the fallbacks; None when not
        set, except tolerance_percent (default 2) and drill_allowance (default 0)."""
        name = self.resolved(key)
        return self._values[name] if name is not None else DEFAULTS.get(key)

    def is_set(self, key: str) -> bool:
        """True when the profile or the arguments give key a value (defaults do not
        count)."""
        return self.resolved(key) is not None

    def origin(self, key: str) -> str | None:
        """Where get(key) comes from: builtin, file, argument, default (a default
        value) or None (not set)."""
        name = self.resolved(key)
        if name is not None:
            return self._origins[name]
        return "default" if key in DEFAULTS else None

    def published(self, key: str) -> str | None:
        """The built-in profile's text for get(key) as its source publishes it
        ("4 mil (0.1016mm)"); None for values from a file or the arguments."""
        name = self.resolved(key)
        return self._published.get(name) if name is not None else None

    def source_of(self, key: str) -> str | None:
        """The source of get(key): the built-in page's URL, or the file's source."""
        name = self.resolved(key)
        return self._sources.get(name) if name is not None else None

    def effective(self) -> dict:
        """Every limit that is set, fallbacks resolved: name -> get(name)."""
        return {name: self.get(name) for name in LIMIT_KEYS if self.is_set(name)}

    def severity(self, key: str) -> str:
        """Severity of a miss on a service-only limit (SERVICE_LIMITS): info when the
        value is Altimade's and Altimade is only the default, else error (copper_oz:
        warning)."""
        chosen = SERVICE_LIMITS[key]
        if self.chosen_by == "default" and self.origin(key) == "builtin":
            return "info"
        return chosen

    def cache_key(self) -> tuple:
        """Everything a computed result depends on: the values, their origins (which
        set service severities) and chosen_by."""
        return (self.chosen_by, tuple(sorted(
            (name, value, self._origins[name]) for name, value in self._values.items()
        )))

    # ---------- echo ----------

    @property
    def scope(self) -> str:
        """One sentence saying what the results were checked against."""
        count = self.argument_count
        extra = (f", with {count} limit{'s' if count != 1 else ''} from the limits argument"
                 if count else "")
        if self.key == "altimade":
            if self.chosen_by == "default":
                return ("Checked against Altimade's published limits (default: no "
                        f"fabricator chosen){extra}; other fabricators differ. Pass profile "
                        "or limits for yours.")
            return (f"Checked against Altimade's published limits (page updated "
                    f"{ALTIMADE['source_updated']}){extra}; other fabricators differ.")
        if self.key == "none":
            if not count:
                return "Profile none and no limits: no fabricator limit is checked."
            return (f"Checked only against the limits argument ({count} "
                    f"limit{'s' if count != 1 else ''}, profile none).")
        where = ("the project's " + self.file if self.chosen_by == "project_file"
                 else "the profile file " + self.file)
        if self.chosen_by == "setting":
            where += " (the user's Fab profile setting)"
        based = " (based on Altimade's published limits)" if self.based_on == "altimade" else ""
        return f"Checked against the limits in {where}{based}{extra}."

    def echo(self, to_mm: bool) -> dict:
        """The profile for the result, in the output units: {name, chosen_by, source,
        source_updated, scope, [file, based_on,] limits: rows}. A row is {limit (the key
        to pass back, e.g. min_trace_mm), value_mm | value_mil | value, [published,]
        origin}; lengths are rounded to 4 decimals in mm and 3 in mil, so do not run
        the rows through a mil-to-mm conversion again. Removed limits show null.
        tolerance_percent is always listed."""
        rows = []
        for name, kind in LIMIT_KEYS.items():
            if name in self._values:
                value, origin = self._values[name], self._origins[name]
            elif name == "tolerance_percent":
                value, origin = None, "default"
            else:
                continue
            if value is None and name in DEFAULTS:
                value, origin = DEFAULTS[name], "default"
            row: dict = {"limit": external_key(name, to_mm)}
            if kind in _SUFFIXED:
                row["value_mm" if to_mm else "value_mil"] = _length_out(value, to_mm)
            else:
                row["value"] = list(value) if isinstance(value, tuple) else value
            if name in self._published:
                row["published"] = self._published[name]
            row["origin"] = origin
            rows.append(row)
        out = {
            "name": self.name,
            "chosen_by": self.chosen_by,
            "source": self.source,
            "source_updated": self.source_updated,
            "scope": self.scope,
        }
        if self.file:
            out["file"] = self.file
        if self.based_on:
            out["based_on"] = self.based_on
        out["limits"] = rows
        return out


def _length_out(value, to_mm: bool):
    if value is None:
        return None
    if isinstance(value, tuple):
        return [_length_out(part, to_mm) for part in value]
    return round(value / MM, 4) if to_mm else round(value, 3)


# ---------- limits ----------


def _number(value) -> float | None:
    """value as a finite float; None for anything else: bools, strings, lists, objects,
    NaN, infinities and integers too large for a float."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _not_a_number(value) -> str:
    """What value is instead of a number, for an error message."""
    if isinstance(value, bool):
        return "true/false"
    if isinstance(value, str):
        return "text"
    if isinstance(value, dict):
        return "an object"
    if isinstance(value, list):
        return "a list"
    if isinstance(value, (int, float)):
        return "a usable number (too large, NaN or infinite)"
    return type(value).__name__


def _whole(value) -> int | None:
    number = _number(value)
    return int(number) if number is not None and number == int(number) else None


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _limit_error(origin: str, file: str | None, message: str, **extra) -> ProfileError:
    if origin == "argument":
        return ProfileError({"error": "invalid_limits", "message": message, **extra})
    return _profile_error(file, message, **extra)


def _profile_error(file: str | None, message: str, **extra) -> ProfileError:
    return ProfileError({
        "error": "invalid_profile",
        "message": f"{file}: {message} Fix the file, then call again.",
        "file": file,
        **extra,
        "hint": INVALID_PROFILE_HINT,
    })


def _split_key(key: str) -> tuple[str, str | None]:
    for unit in ("mm", "mil"):
        if key.endswith("_" + unit) and key[: -len(unit) - 1] in LIMIT_KEYS:
            return key[: -len(unit) - 1], unit
    return key, None


def _out_of_range(name: str, mil: list[float]) -> str | None:
    """"above" or "below" when a length (mil) is outside what any fab offers, else None.
    0 is in range: a minimum of 0 is a choice, not a unit slip."""
    if max(mil) > _MAX_MM[name] * MM + 1e-9:
        return "above"
    if any(0 < value < _MIN_MM[name] * MM - 1e-9 for value in mil):
        return "below"
    return None


def _mm_and_mil(mm: float) -> str:
    return f"{mm:g} mm = {round(mm * MM, 3):g} mil"


def _range_error(name: str, key: str, unit: str, raw, numbers: list[float],
                 side: str) -> str:
    """The message for a length outside any fab's range. It suggests the other unit only
    when the value read in it is in range and at least HINT_FLOOR_MIL, so following the
    suggestion cannot turn a slip into a near-zero limit that passes every board."""
    other = "mil" if unit == "mm" else "mm"
    read = [number * (MM if other == "mm" else 1.0) for number in numbers]
    hint = "."
    if _out_of_range(name, read) is None and min(read) >= HINT_FLOOR_MIL:
        hint = (f"; did you mean {name}_{other}? Check the unit in the fab's table (or ask "
                "the user) before calling again.")
    bound = (_MAX_MM if side == "above" else _MIN_MM)[name]
    return (f"{key} = {_clip(raw, 40)} is {side} any fab capability "
            f"({'at most' if side == 'above' else 'at least'} {_mm_and_mil(bound)}){hint}")


def _value(name: str, key: str, unit: str | None, raw):
    """raw checked for its kind and converted (mil for lengths); a message on error."""
    kind = LIMIT_KEYS[name]
    if kind in _SUFFIXED:
        scale = MM if unit == "mm" else 1.0
        parts = raw if kind == "size" and isinstance(raw, list) else [raw]
        if kind == "size" and isinstance(raw, list) and len(raw) != 2:
            return None, f"{key} must be a number or a list of two numbers [a, b]."
        numbers = [_number(part) for part in parts]
        if any(number is None for number in numbers):
            shape = "a number or [a, b]" if kind == "size" else "a number"
            wrong = next(part for part, number in zip(parts, numbers) if number is None)
            return None, (f"{key} must be {shape} (null removes it), not "
                          f"{_not_a_number(wrong)}.")
        positive = kind == "size" or name.startswith("max_")
        if any(number < 0 or (positive and number == 0) for number in numbers):
            return None, f"{key} must be {'positive' if positive else 'zero or more'}."
        mil = [number * scale for number in numbers]
        side = _out_of_range(name, mil)
        if side:
            return None, _range_error(name, key, unit, raw, numbers, side)
        if kind == "size":
            short, long = (mil[0], mil[0]) if len(mil) == 1 else sorted(mil)
            return (short, long), None
        return mil[0], None
    if kind == "layers":
        parts = raw if isinstance(raw, list) else [raw]
        counts = [_whole(part) for part in parts]
        if not parts or any(count is None or not 1 <= count <= MAX_LAYERS for count in counts):
            return None, (f"{key} must be a list of copper layer counts, whole numbers "
                          f"1-{MAX_LAYERS}.")
        return tuple(sorted(set(counts))), None
    if kind == "count":
        count = _whole(raw)
        cap = _MAX_COUNT.get(name)
        if cap is not None and (count is None or not 1 <= count <= cap):
            return None, f"{key} must be a whole number from 1 to {cap}."
        if count is None or count < 1:
            return None, f"{key} must be a whole number of at least 1."
        return count, None
    number = _number(raw)
    if number is None:
        return None, f"{key} must be a number (null removes it), not {_not_a_number(raw)}."
    if kind == "percent" and not 0 <= number <= MAX_TOLERANCE_PERCENT:
        return None, f"{key} must be between 0 and {MAX_TOLERANCE_PERCENT:g}."
    if kind == "weight" and not 0 < number <= MAX_COPPER_OZ:
        return None, f"{key} must be an oz weight above 0 and at most {MAX_COPPER_OZ:g}."
    if kind == "ratio" and number <= 0:
        return None, f"{key} must be positive."
    return number, None


def parse_limits(raw, origin: str = "argument", file: str | None = None) -> dict:
    """Limits in the suffixed vocabulary -> {name: value} (mil for lengths, None to
    remove). Keys starting with "_" and "comment" are ignored. Raises ProfileError:
    invalid_limits for the argument, invalid_profile for a file (origin "file")."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise _limit_error(origin, file, "limits must be an object of limit keys and values.")
    values: dict = {}
    given: dict[str, str] = {}
    for key, value in raw.items():
        key = str(key)
        if key.startswith("_") or key == "comment":
            continue
        shown = _clip(key, 60)
        name, unit = _split_key(key)
        kind = LIMIT_KEYS.get(name)
        if kind is None:
            raise _limit_error(origin, file, f"unknown limit '{shown}'.",
                               valid_keys=list(VALID_KEYS))
        if kind in _SUFFIXED and unit is None:
            what = "a board size" if kind == "size" else "a length"
            raise _limit_error(origin, file, (
                f"'{shown}' is {what}: use {name}_mm or {name}_mil."))
        if kind not in _SUFFIXED and unit is not None:
            raise _limit_error(origin, file, f"'{shown}' takes no unit suffix: use {name}.")
        if name in given:
            raise _limit_error(origin, file, (
                f"{name} is given twice ({given[name]} and {shown}); give it once."))
        given[name] = shown
        if value is None:
            values[name] = None
            continue
        converted, message = _value(name, shown, unit, value)
        if message:
            raise _limit_error(origin, file, message)
        values[name] = converted
    return values


# ---------- files ----------


def _reserved(file_name: str) -> bool:
    return file_name.split(".")[0].rstrip(" ").upper() in _RESERVED


def profile_files(root_dir) -> tuple[list[str], int]:
    """The .json files in the project folder a profile argument can name: at most
    FILES_LISTED of them, names with "fab" or "profile" first, and how many there are
    (fab_profile.json included when present)."""
    if root_dir is None:
        return [], 0
    try:
        names = sorted(
            (path.name for path in Path(root_dir).glob("*.json")
             if _FILE_NAME.fullmatch(path.name) and path.is_file()),
            key=lambda name: ("fab" not in name.lower() and "profile" not in name.lower(),
                              name.lower(), name),
        )
    except OSError:
        return [], 0
    return names[:FILES_LISTED], len(names)


def _not_found(name: str, root_dir, reason: str) -> ProfileError:
    files, total = profile_files(root_dir)
    return ProfileError({
        "error": "profile_not_found",
        "message": reason,
        "profile": _clip(name, NAME_CHARS),
        "builtin": list(BUILTIN_NAMES),
        "files": files,
        "files_total": total,
        "hint": NOT_FOUND_HINT,
    })


def _profile_path(name: str, root_dir) -> Path:
    """The file a profile argument names in the project folder; ProfileError when the
    name is not a plain file name or no such file exists."""
    shown = _clip(name, NAME_CHARS)
    file_name = name if name.lower().endswith(".json") else name + ".json"
    if not _FILE_NAME.fullmatch(name):
        raise _not_found(name, root_dir, (
            f"'{shown}' is not a built-in profile or a profile file name: a file name is "
            "letters, digits, spaces, '_', '.' and '-' (no folder, drive or ':'), for a "
            ".json file in the project folder."))
    if _reserved(file_name):
        raise _not_found(name, root_dir, (
            f"'{shown}' is not a built-in profile, and a Windows device name (CON, PRN, "
            "AUX, NUL, COM0-9, LPT0-9) cannot be a profile file: save the limits under "
            "another name."))
    if root_dir is None:
        raise _not_found(name, root_dir, (
            f"No built-in profile '{shown}', and no project folder is loaded to look for "
            f"{file_name}."))
    path = Path(root_dir) / file_name
    try:
        exists = path.is_file()
    except OSError:
        exists = False
    if not exists:
        raise _not_found(name, root_dir, (
            f"No built-in profile '{shown}' and no {file_name} in the project folder."))
    return path


def _no_duplicates(pairs: list) -> dict:
    seen: dict = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"duplicate key '{_clip(key, 60)}'")
        seen[key] = value
    return seen


def _no_constants(text: str):
    raise ValueError(f"{text} is not a number here")


def _short_int(text: str) -> int:
    # A limit never needs 16 digits; longer ones would overflow a float or meet
    # Python's integer-string limit, whose message speaks of Python, not the file.
    digits = len(text.lstrip("-"))
    if digits > 15:
        raise ValueError(f"a number in the file is too long ({digits} digits)")
    return int(text)


def read_project_json(path: Path, root_dir) -> dict:
    """The file's JSON object, read only when it sits directly in the project folder
    (no link out of it); utf-8 with or without a BOM."""
    file = path.name
    try:
        root = Path(root_dir).resolve()
        resolved = path.resolve(strict=True)
        if resolved.parent != root or not resolved.is_file():
            raise _profile_error(file, "the file is not in the project folder (a link out "
                                       "of it?).")
        if resolved.stat().st_size > MAX_FILE_BYTES:
            raise _profile_error(file, f"the file is larger than {MAX_FILE_BYTES // 1024} KB.")
        text = resolved.read_bytes().decode("utf-8-sig")
    except UnicodeDecodeError:
        raise _profile_error(file, "the file is not UTF-8 text.") from None
    except OSError as e:
        raise _profile_error(file, f"the file could not be read ({type(e).__name__}).") from None
    try:
        data = json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=_no_constants,
                          parse_int=_short_int)
    except json.JSONDecodeError as e:
        raise _profile_error(file, (
            f"not valid JSON at line {e.lineno}, column {e.colno}: {e.msg}."),
            line=e.lineno, column=e.colno) from None
    except ValueError as e:
        raise _profile_error(file, f"{e}.") from None
    except RecursionError:
        raise _profile_error(file, "the JSON is nested too deeply.") from None
    if not isinstance(data, dict):
        raise _profile_error(file, "the file must hold one JSON object {...}.")
    return data


def _text(data: dict, key: str, file: str, limit: int) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise _profile_error(file, f"{key} must be a string.")
    return _clip(value, limit) or None


def _file_profile(path: Path, root_dir, chosen_by: str) -> Profile:
    file = path.name
    data = read_project_json(path, root_dir)
    for key in data:
        if not key.startswith("_") and key not in _FILE_KEYS:
            raise _profile_error(file, f"unknown key '{_clip(key, 60)}'.",
                                 valid_keys=list(_FILE_KEYS))
    based_on = data.get("based_on")
    if based_on is not None:
        if not isinstance(based_on, str) or based_on.strip().lower() not in BUILTIN_NAMES:
            raise _profile_error(file, "based_on must be 'altimade' or 'none' (or left out "
                                       "to inherit nothing).")
        based_on = based_on.strip().lower()
    limits = parse_limits(data.get("limits"), "file", file)
    profile = Profile(
        file,
        _text(data, "name", file, NAME_CHARS) or _clip(path.stem, NAME_CHARS),
        chosen_by,
        source=_text(data, "source", file, SOURCE_CHARS),
        source_updated=_text(data, "source_updated", file, 40),
        based_on=based_on if based_on == "altimade" else None,
        file=file,
    )
    if profile.based_on == "altimade":
        profile._inherit_altimade(notes=False)
    profile._apply(limits, "file", profile.source)
    return profile


def _setting_error(error: ProfileError) -> ProfileError:
    """error, naming the setting and with the setting's hint."""
    details = {key: value for key, value in error.error.items() if key != "hint"}
    return ProfileError({**details, "setting": SETTING_VARIABLE, "hint": SETTING_HINT})


def setting_path_problem(path: Path, kind: str = "file") -> str | None:
    """Why a path a setting names cannot be used, worded to follow "which", or None for
    an existing file (kind "file") or folder (kind "folder")."""
    if not path.is_absolute():
        return "is not an absolute path"
    try:
        if path.is_file() if kind == "file" else path.is_dir():
            return None
        other = path.is_dir() if kind == "file" else path.is_file()
    except (OSError, ValueError):
        other = False
    if other:
        return "is a folder, not a file" if kind == "file" else "is a file, not a folder"
    text = str(path)
    if WINDOWS and len(text) > MAX_PATH_CHARS and not text.startswith("\\\\?\\"):
        return (f"is {len(text)} characters long, more than Windows opens without long-path "
                "support (use a shorter path)")
    return f"is not a {kind} on this PC (moved, renamed or on a drive not connected?)"


def setting_folder(path: Path) -> Path:
    """The folder a setting's file really sits in: the user chose the file, so a link to
    a file elsewhere is followed (a project's own files are never)."""
    try:
        return path.resolve().parent
    except (OSError, RuntimeError, ValueError):
        return path.parent


def setting_profile(value: str, root_dir=None) -> Profile:
    """The profile the Fab profile setting names (chosen_by setting): altimade or none
    (any case), or the absolute path of a profile file, read like a project's file.
    ProfileError (profile_not_found, invalid_profile) naming the setting otherwise."""
    text = str(value).strip()
    if text.lower() in BUILTIN_NAMES:
        return builtin_profile(text.lower(), "setting")
    shown = _clip(text, SOURCE_CHARS)
    path = Path(text)
    reason = setting_path_problem(path)
    if reason:
        raise _setting_error(_not_found(text, root_dir, (
            f"The Fab profile setting ({SETTING_VARIABLE}) names '{shown}', which {reason}.")))
    try:
        return _file_profile(path, setting_folder(path), "setting")
    except ProfileError as error:
        raise _setting_error(error) from None


def builtin_profile(name: str, chosen_by: str = "argument") -> Profile:
    """A built-in profile: "altimade" (Altimade's published limits) or "none" (no
    limits)."""
    if name == "altimade":
        profile = Profile("altimade", ALTIMADE["name"], chosen_by, source=ALTIMADE["source"],
                          source_updated=ALTIMADE["source_updated"])
        profile._inherit_altimade()
        return profile
    if name == "none":
        return Profile("none", "none", chosen_by)
    raise KeyError(name)


def load_profile(profile: str | None, root_dir, limits: dict | None = None,
                 setting: str | None = None) -> tuple[Profile | None, dict | None]:
    """The profile check_dfm compares with, as (profile, None) or (None, error dict).

    profile: "altimade" or "none" (any case), or a file in root_dir named "acme" or
    "acme.json" (chosen_by argument). None or blank: root_dir/fab_profile.json when it
    exists (chosen_by project_file), else the Fab profile setting when it is given
    (setting: a file's absolute path, altimade or none; chosen_by setting), else altimade
    (chosen_by default). limits (same
    suffixed keys, null removes) apply on top, whatever their types: every value is
    checked here. Errors: invalid_limits, profile_not_found (builtin names, the .json
    files found, files_total and a hint), invalid_profile (file, line and column for bad
    JSON, and a hint)."""
    try:
        overrides = parse_limits(limits, "argument")
        name = profile.strip() if isinstance(profile, str) else None
        if not name:
            default = Path(root_dir) / DEFAULT_FILE if root_dir is not None else None
            try:
                found = default is not None and default.is_file()
            except OSError:
                found = False
            if found:
                result = _file_profile(default, root_dir, "project_file")
            elif setting:
                result = setting_profile(setting, root_dir)
            else:
                result = builtin_profile("altimade", "default")
        elif name.lower() in BUILTIN_NAMES:
            result = builtin_profile(name.lower(), "argument")
        else:
            result = _file_profile(_profile_path(name, root_dir), root_dir, "argument")
    except ProfileError as e:
        return None, e.error
    result._apply(overrides, "argument")
    result.argument_count = len(overrides)
    return result, None
