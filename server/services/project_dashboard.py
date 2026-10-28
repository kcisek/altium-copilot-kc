"""Project dashboard (roadmap #74): one overview of the loaded project.

The release gates (check_release_readiness), board and project facts (get_board_info,
list_variants) and, for each of 33 checks, its counts, what it examined and its first
findings, as of the saved files. One compact answer for the model, and the data the
project dashboard view (server/ui/project_dashboard.html) draws.

Pure composition, like release_gate.py: no MCP. The tool layer passes the tool functions
in (main._review_tools(): the MCP tool functions, each run inside
app_settings.tool_defaults() so the Units setting never changes a card), NETLIST_TOOLS,
the parts of the state key it alone knows (settings, check packs) and the netlist's
freshness against the sheets, read on every call. Each check runs in-process with its
default arguments through one review_checklist.ToolRunner per call, which reads its
first page only (the release: every page of gates); the result is freshly parsed JSON,
so no adapter can change a tool's cached model. normalise() turns a first page into a
card: the tool's own totals (never re-derived from the rows listed; a missing required
key makes the card unreadable), what it examined (and, for a check that needs a voltage,
a dissipation, a current or a rule it can evaluate, how much it could judge), its scope,
the first findings worst first and one state of a closed set. clean needs a current
result, readable counts all 0, something examined (and judged), nothing left unchecked
and no tag that limits the result. The answer leaves out what the catalogue says (each
card's title, group, view): the view holds the catalogue.

Cards are cached per project (DashboardCache, LRU) under a state key hashed part by part
(design files, the project folder, Git, the netlist, the variant, the settings, the
check packs and a generation bumped by refresh), so a card computed under an older key
comes back stale with the first part that changed. A default call runs the release
first (never skipped, never budgeted), then the cards in ORDER while the time budget
lasts (BUDGET_S, checked before each card starts: a running check cannot be stopped);
the slow cards start only before SLOW_START_S. The cards it leaves out come back stale
from the cache or not_run, with the call that runs them.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

try:
    from services import review_checklist as rc
    from services.board_view import FINDING_SOURCES
except ImportError:
    from server.services import review_checklist as rc
    from server.services.board_view import FINDING_SOURCES

TOOL = "get_project_dashboard"
RELEASE_TOOL = "check_release_readiness"
FACT_TOOLS = ("get_board_info", "list_variants")
FACT_ARGS = {"get_board_info": {"units": "mm"}}
BUDGET_S = 20.0       # soft: checked before each card starts, never during one
SLOW_START_S = 10.0   # a slow card starts only while less time than this has passed
RELEASE_PAGES = 5     # pages of gate rows read (23 gates fit one today)
MAX_RESULT_CHARS = 16_000
FINDINGS_STORED = 20  # findings kept per card (the most a narrowed answer lists)
TEXT_CHARS = 200
REASON_CHARS = 160
NA_REASON_CHARS = 100  # a not-applicable card's reason (the same words on many cards)
NOTE_CHARS = 200
SUMMARY_CHARS = 300
EVIDENCE_STORED = 320
SCOPE_LISTED = 3      # not_checked ids listed
IDS_LISTED = 3        # finding ids listed with their counts
ANCHORS = (("parts", "designators", 5), ("nets", "nets", 5), ("sheets", "sheets", 3))
SEVERITIES = ("error", "warning", "info")

GROUPS = (
    ("schematic", "Schematic"),
    ("bom", "BOM and libraries"),
    ("sync", "Schematic and PCB"),
    ("pcb", "PCB"),
    ("manufacturing", "Manufacturing and outputs"),
    ("system", "System"),
)


@dataclass(frozen=True)
class CardSpec:
    """One card: the tool, its title and group, how its counts are read (rule), what it
    examined (subject: a path into the result and how to count it, with its noun), the
    view that draws its result, and whether it is slow or advisory. board is read from
    board_view.FINDING_SOURCES: the board view can place the tool's findings."""
    tool: str
    title: str
    group: str
    rule: str
    subject: tuple[str, str]
    noun: str
    view: str | None = None
    slow: bool = False
    advisory: bool = False
    args: dict = field(default_factory=dict)

    @property
    def board(self) -> bool:
        return self.tool in FINDING_SOURCES


def _card(tool, title, group, rule, subject, noun, **extra) -> CardSpec:
    return CardSpec(tool, title, group, rule, subject, noun, **extra)


# Subjects: (path, how) with how "int" (an int), "len" (a list's length), "count" (an int
# or a list's length) or "sum" (a dict of ints, summed).
CATALOGUE = (
    _card("run_erc", "ERC (offline)", "schematic", "erc", ("checks_run", "len"), "checks",
          view="run_erc"),
    _card("check_project_integrity", "Project integrity", "schematic", "summary_sc",
          ("checks_run", "len"), "checks"),
    _card("get_power_tree", "Power tree", "schematic", "sc", ("rails_total", "int"), "rails",
          view="get_power_tree"),
    _card("check_pull_resistors", "Pull resistors", "schematic", "sc", ("signals_total", "int"),
          "signals"),
    # The signals between ICs it looked at (JUDGED: those whose rails are known).
    _card("check_logic_levels", "Logic levels", "schematic", "sc", ("summary.signals.checked", "int"),
          "signals"),
    # summary.parts_checked is a dict of counts per kind (capacitors, resistors, LEDs).
    _card("check_derating", "Derating", "schematic", "sc", ("summary.parts_checked", "sum"), "parts"),
    _card("check_decoupling", "Decoupling", "schematic", "sc", ("rails_total", "int"), "rails"),
    _card("check_interfaces", "Interfaces and ESD", "schematic", "sc", ("connectors_total", "int"),
          "connectors"),
    _card("check_bom", "BOM", "bom", "sc", ("parts.in_bom", "int"), "parts", view="get_bom"),
    _card("check_libraries", "Libraries", "bom", "sc", ("parts.schematic", "int"), "parts"),
    _card("check_db_parameters", "Database parameters", "bom", "summary_sc",
          ("summary.database_library_parts", "int"), "database-library parts"),
    _card("check_sch_pcb_sync", "Schematic-PCB sync", "sync", "sc", ("components.schematic", "int"),
          "components"),
    _card("check_directive_sync", "Directives", "sync", "sc", ("directives_total", "int"),
          "directives"),
    _card("check_connectivity", "Connectivity", "sync", "sc", ("summary.nets_checked", "int"), "nets"),
    _card("get_drc_report", "Batch DRC report", "pcb", "drc", ("rules_checked", "count"), "rules",
          view="get_drc_report"),
    _card("check_pcb_health", "PCB health", "pcb", "sc", ("checks_run", "len"), "checks"),
    _card("check_rule_compliance", "Rule compliance", "pcb", "compliance", ("kinds", "len"),
          "rule kinds", slow=True),
    _card("check_lengths", "Length rules", "pcb", "lengths", ("rule_count", "int"), "rules"),
    _card("check_diff_pairs", "Differential pairs", "pcb", "sc", ("pairs_total", "int"), "pairs"),
    _card("check_diff_pair_geometry", "Diff-pair geometry", "pcb", "summary_sc",
          ("summary.pairs_analysed", "int"), "pairs", slow=True),
    _card("check_vias", "Vias", "pcb", "sc", ("summary.vias", "int"), "vias"),
    _card("check_return_path", "Return paths", "pcb", "summary_sc",
          ("summary.segments_checked", "int"), "segments", slow=True),
    _card("check_power_paths", "Power paths", "pcb", "summary_sc", ("summary.rails.analysed", "int"),
          "rails", slow=True),
    _card("check_thermal", "Thermal", "pcb", "sc", ("parts_total", "int"), "parts", slow=True),
    _card("check_hv_clearance", "HV clearance", "pcb", "summary_sc", ("summary.hv_nets_total", "int"),
          "HV nets"),
    _card("check_dfm", "DFM and DFA", "manufacturing", "sc", ("checks_run", "len"), "checks",
          slow=True),
    _card("check_testpoints", "Testpoints", "manufacturing", "sc", ("checks_run", "len"), "checks",
          advisory=True),
    _card("check_mechanical_fit", "Mechanical fit", "manufacturing", "summary_sc",
          ("parts_total", "int"), "parts"),
    _card("check_fab_drawing", "Fab drawing", "manufacturing", "summary_levels",
          ("drawings_total", "int"), "drawings"),
    _card("get_output_jobs", "Output jobs", "manufacturing", "summary_sc", ("outjobs", "len"),
          "OutJobs"),
    _card("verify_outputs", "Generated outputs", "manufacturing", "sc", ("checks_run", "len"),
          "checks"),
    _card("check_multiboard", "Multi-board", "system", "summary_findings",
          ("summary.pin_pairs", "int"), "pin pairs"),
    _card("run_custom_checks", "Team check packs", "system", "sc", ("rules", "len"), "rules"),
)
SPECS = {spec.tool: spec for spec in CATALOGUE}
CARD_TOOLS = tuple(spec.tool for spec in CATALOGUE)
SLOW = frozenset(spec.tool for spec in CATALOGUE if spec.slow)

# What a check could judge of what it examined, where the check needs an input the files
# may not give (a voltage, a dissipation, a current): the paths whose ints add up to that
# count, and what to say when it is 0. A card whose check judged none of the things it
# examined is partial, never clean (no findings because nothing could be judged).
JUDGED = {
    "check_derating": (("summary.capacitors.applied_voltage_known",
                        "summary.resistors.voltage_across_known"),
                       "no part's voltage is known; give the rails' voltages with rails="),
    "check_logic_levels": (("summary.signals.judged",),
                           "no signal's rail voltages are known; give them with rails="),
    "check_thermal": (("summary.dissipation.computed", "summary.dissipation.given"),
                      "no part's dissipation is known, so no junction temperature was "
                      "computed; see its inputs_needed"),
    "check_power_paths": (("summary.rails.by_status.estimated",
                           "summary.rails.by_status.power_analyzer"),
                          "no rail's copper path was estimated; see its summary.rails.by_status"),
    "run_custom_checks": (("summary.rules.pass", "summary.rules.fail"),
                          "no rule could be evaluated; see each rule's reason"),
}
# A tool's own verdict that says it could not evaluate everything: never clean.
INCOMPLETE_VERDICTS = {"run_custom_checks": ("incomplete",)}
# Rows of these tools can group several of the things their counts count (an ERC row per
# sheet for off-grid objects, a DRC rule's violations, a rule kind, a length rule).
GROUPED_ROWS = frozenset({"run_erc", "get_drc_report", "check_rule_compliance", "check_lengths"})

# The default call's order after the release and the facts: the release's own sources
# first (warm from it), then the schematic, BOM and PCB checks, the slow ones last,
# cheapest first.
ORDER = (
    "check_project_integrity", "run_erc", "check_sch_pcb_sync", "check_connectivity",
    "get_drc_report", "check_pcb_health", "check_fab_drawing", "get_output_jobs",
    "verify_outputs", "check_testpoints",
    "check_bom", "get_power_tree", "check_pull_resistors", "check_decoupling",
    "check_interfaces", "check_logic_levels", "check_derating", "check_directive_sync",
    "check_libraries", "check_db_parameters",
    "check_diff_pairs", "check_lengths", "check_vias", "check_mechanical_fit",
    "check_hv_clearance", "check_multiboard", "run_custom_checks",
    "check_thermal", "check_power_paths", "check_diff_pair_geometry", "check_rule_compliance",
    "check_dfm", "check_return_path",
)
RUN_ORDER = (RELEASE_TOOL, *FACT_TOOLS, *ORDER)
ACCEPTED = (*CARD_TOOLS, RELEASE_TOOL, *FACT_TOOLS)

# Tools near the checks that are not cards, and why.
EXCLUDED = {
    "check_release_readiness": "the Release readiness block",
    "check_pin_table": "it needs a pin table or a part to compare: ask Claude with one",
    "compare_variants": "a link from the Variants facts when two or more variants are defined",
    "get_bom": "the BOM card is check_bom; its Open view shows get_bom",
    "get_board_view": "a link from the overview and from cards whose findings the board view "
                      "can place",
    "get_clone_status": "the release gate clone_status quotes it",
    "project_tour": "a prompt, offered as an action",
    "release_review": "a prompt, offered as an action",
}

NOTES = (
    "counts: each tool's own totals over its whole result (units differ: ERC objects, "
    "length rule members, DRC rule violations); in_checks_run adds them up.",
    "subject: what the check examined; judged: how many of those it could judge.",
    "findings: the first of the tool's page 1, worst first; ids: rows per id on page 1 "
    "(run_erc: objects per violation).",
    "A card's as_of is given when not state.cards_as_of_utc, its variant when not the answer's.",
    "run_erc is the offline approximation of Altium's ERC: never clean.",
    "Call a card's tool for its details.",
)

# Error codes: the check does not apply to this project, or cannot run on this load.
NOT_APPLICABLE = ("no_pcb_document", "no_variants", "no_multiboard_system", "no_check_packs",
                  "not_an_altium365_clone", "nothing_to_check")
UNAVAILABLE = {
    "netlist_required": "Needs the netlist, which this load does not have: fix the sheets and "
                        "call refresh_netlist, or open the project in Altium Designer once and "
                        "load it again.",
    "no_components_loaded": "The netlist has no component data: fix or re-save the sheets "
                            "named in sheet_errors, then call refresh_netlist.",
}
NO_DRC_REPORT = "No Batch DRC report was found: run the Batch DRC in Altium (Tools > Design " \
                "Rule Check) and save its report."
# Tags that keep a card from being clean (advisory and heuristic do not).
LIMITING_TAGS = ("offline_approximation", "netlist_older_than_sheets", "report_untrusted",
                 "at_least")
UNCACHED = ("failed", "unreadable")
STATES = ("errors", "warnings", "notes", "clean", "partial", "nothing_to_check", "not_verified",
          "not_applicable", "unavailable", "failed", "unreadable", "not_run", "stale")

# The state key's parts, in the order stale_because names them.
PARTS = (
    ("design", "design files saved"),
    ("folder", "project folder changed"),
    ("git", "Git state changed"),
    ("netlist", "netlist changed"),
    ("variant", "variant changed"),
    ("settings", "settings changed"),
    ("packs", "check packs changed"),
    ("generation", "refreshed"),
)


def clip(text, limit: int) -> str:
    text = " ".join(str(text if text is not None else "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def severity(level) -> str:
    """A finding's or count's level as error, warning or info."""
    text = str(level or "").strip().lower()
    if text in ("fatal error", "fatal", "error", "fail"):
        return "error"
    if text.startswith("warn"):
        return "warning"
    return "info"


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def _utc(stamp: float) -> str:
    return datetime.fromtimestamp(stamp, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------- inputs, state key and cache ----------

@dataclass
class Inputs:
    """What the tool layer passes in. settings and packs are the state key's parts the
    dashboard cannot read itself (JSON-able values: the settings files' signature and the
    settings applied; the check packs' signature)."""
    project: dict
    netlist: dict | None
    load: str                      # netlist, offline_netlist or netlist_free
    variant: str | None
    defined_variants: int
    netlist_updated_utc: str | None
    tools: dict[str, Callable[..., Any]]
    netlist_tools: tuple[str, ...] = ()
    settings: Any = None
    packs: Any = None
    # () -> (files saved after the loaded netlist was compiled, a note saying which), read
    # on every call (the release gate netlist_fresh's rule): a narrowed call runs no
    # release, and a cached release may be from older files.
    netlist_freshness: Callable[[], tuple[bool, str | None]] | None = None
    clock: Callable[[], float] = time.perf_counter
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    budget_s: float = BUDGET_S
    slow_start_s: float = SLOW_START_S


def _digest(value) -> str:
    text = json.dumps(value, sort_keys=True, default=str)
    return hashlib.sha1(text.encode("utf-8", "surrogatepass")).hexdigest()[:16]  # a change detector


def _git_stats(root: Path) -> list:
    """(name, size, mtime_ns) of HEAD, index and logs/HEAD in the repository's .git folder:
    a commit, a checkout or staging changes them (folder_signature skips .git)."""
    repo = rc.repository_root(root)
    if repo is None:
        return []
    git = repo / ".git"
    if not git.is_dir():
        return ["gitfile"]
    out = []
    for name in ("HEAD", "index", "logs/HEAD"):
        try:
            stat = (git / name).stat()
            out.append([name, stat.st_size, stat.st_mtime_ns])
        except OSError:
            out.append([name, None])
    return out


def state_parts(inputs: Inputs, generation: int = 0) -> tuple[dict[str, str], dict]:
    """The state key's parts, each hashed on its own (so stale_because can name the first
    that differs), and what the state block says: whether the folder is watched and the
    newest design-file save."""
    project = inputs.project
    root = Path(project.get("root_dir") or ".")
    design, newest = [], None
    for path in rc.design_files(project):
        try:
            stat = path.stat()
        except OSError:
            design.append([str(path), "missing"])
            continue
        design.append([str(path), stat.st_size, stat.st_mtime_ns])
        newest = stat.st_mtime if newest is None else max(newest, stat.st_mtime)
    folder = rc.folder_signature(root)
    raw = {
        "design": design,
        "folder": folder if folder is not None else "unwatched",
        "git": _git_stats(root),
        "netlist": [id(inputs.netlist) if inputs.netlist is not None else None,
                    inputs.netlist_updated_utc, inputs.load],
        "variant": inputs.variant,
        "settings": inputs.settings,
        "packs": inputs.packs,
        "generation": generation,
    }
    parts = {name: _digest(raw[name]) for name, _ in PARTS}
    info = {"folder_watched": folder is not None,
            "latest_save_utc": _utc(newest) if newest is not None else None}
    return parts, info


def state_key(parts: dict[str, str]) -> str:
    return hashlib.sha1("|".join(parts[name] for name, _ in PARTS).encode()).hexdigest()[:16]


def stale_because(card_parts: dict | None, parts: dict[str, str]) -> str:
    """The first part of the state key that differs, in words."""
    for name, words in PARTS:
        if (card_parts or {}).get(name) != parts[name]:
            return words
    return "refreshed"


class DashboardCache:
    """Cards per project, the projects kept least recently used first (at most size):
    {prj_pcb_path: {generation, cards: {tool: card}}}. Failed and unreadable cards are
    never put in."""

    def __init__(self, size: int = 4):
        self.size = size
        self._entries: OrderedDict = OrderedDict()

    def entry(self, project_key: str) -> dict:
        entry = self._entries.get(project_key)
        if entry is None:
            entry = {"generation": 0, "cards": {}}
            self._entries[project_key] = entry
        self._entries.move_to_end(project_key)
        while len(self._entries) > self.size:
            self._entries.popitem(last=False)
        return entry

    def projects(self) -> list[str]:
        return list(self._entries)

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)


# ---------- normalisation ----------

class _Unreadable(Exception):
    """A key the card needs is missing from the tool's result (its shape changed)."""


_MISSING = object()


def _dig(data, path: str, default=_MISSING):
    value = data
    for key in path.split("."):
        if not isinstance(value, dict) or key not in value or value[key] is None:
            if default is _MISSING:
                raise _Unreadable(path)
            return default
        value = value[key]
    return value


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _int(value, path: str) -> int:
    if not _is_int(value):
        raise _Unreadable(path)
    return value


def _fold(levels, path: str) -> dict:
    """{level: count} folded onto error, warning and info; a missing level is 0."""
    if not isinstance(levels, dict):
        raise _Unreadable(path)
    counts = dict.fromkeys(SEVERITIES, 0)
    for level, count in levels.items():
        counts[severity(level)] += _int(count, f"{path}.{level}")
    return counts


def _unevaluated(rule: dict) -> bool:
    """A length rule check_lengths could not judge in full: not evaluated (delay units, no
    members) or with members incomplete or unknown."""
    summary = rule.get("summary") if isinstance(rule.get("summary"), dict) else {}
    status = str(rule.get("status") or "")
    return bool(status.startswith("not_evaluated") or status == "no_members"
                or summary.get("incomplete") or summary.get("unknown"))


def counts_of(spec: CardSpec, data: dict) -> dict:
    """The tool's own totals over its whole result as {error, warning, info}."""
    rule = spec.rule
    if rule == "sc":
        return _fold(_dig(data, "severity_counts"), "severity_counts")
    if rule == "summary_sc":
        return _fold(_dig(data, "summary.severity_counts"), "summary.severity_counts")
    if rule == "summary_levels":
        return {level: _int(_dig(data, f"summary.{level}"), f"summary.{level}")
                for level in SEVERITIES}
    if rule == "summary_findings":
        return _fold(_dig(data, "summary.findings"), "summary.findings")
    if rule == "erc":
        by_level = _dig(data, "summary.by_level")
        if not isinstance(by_level, dict):
            raise _Unreadable("summary.by_level")
        level = lambda name: _int(by_level.get(name, 0), f"summary.by_level.{name}")
        total = data.get("advisories_total")
        info = total if isinstance(total, int) and not isinstance(total, bool) \
            else len(data.get("advisories") or [])
        return {"error": level("Fatal Error") + level("Error"), "warning": level("Warning"),
                "info": info}
    if rule == "drc":
        totals = _dig(data, "totals")
        violations = _int(_dig(data, "totals.rule_violations"), "totals.rule_violations")
        if not isinstance(_dig(data, "assessment"), list):
            raise _Unreadable("assessment")
        other = lambda key: _int(totals.get(key) or 0, f"totals.{key}")
        return {"error": violations, "warning": other("health_issues") + other("warnings"),
                "info": other("waived")}
    if rule == "compliance":
        count = _int(_dig(data, "violation_count"), "violation_count")
        if not isinstance(_dig(data, "kinds"), list):
            raise _Unreadable("kinds")
        return {"error": count, "warning": 0, "info": 0}
    if rule == "lengths":
        rules = _dig(data, "rules")
        _int(_dig(data, "rule_count"), "rule_count")
        if not isinstance(rules, list):
            raise _Unreadable("rules")
        violations = unevaluated = 0
        for row in rules:
            if not isinstance(row, dict):
                continue
            summary = row.get("summary") if isinstance(row.get("summary"), dict) else {}
            violations += _int(summary.get("violations") or 0, "rules[].summary.violations")
            if _unevaluated(row):
                unevaluated += 1
        return {"error": violations, "warning": 0, "info": unevaluated}
    raise ValueError(f"unknown rule {rule}")


def subject_of(spec: CardSpec, data: dict) -> int | None:
    """How many things the check examined, an int; None when the result does not say."""
    path, how = spec.subject
    value = _dig(data, path, None)
    if how in ("int", "count") and _is_int(value):
        return value
    if how in ("len", "count") and isinstance(value, list):
        return len(value)
    if how == "sum" and isinstance(value, dict) and value and all(
            _is_int(item) for item in value.values()):
        return sum(value.values())
    return None


def judged_of(spec: CardSpec, data: dict) -> int | None:
    """How many of the things examined the check could judge (JUDGED's paths added up: a
    key missing from its block counts 0); None when the result does not say."""
    paths = (JUDGED.get(spec.tool) or ((), ""))[0]
    total = 0
    for path in paths:
        parent, _, leaf = path.rpartition(".")
        block = _dig(data, parent, None)
        if not isinstance(block, dict):
            return None
        value = block.get(leaf, 0)
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        total += value
    return total


def _scope_id(item) -> str:
    if isinstance(item, dict):
        item = item.get("check") or item.get("kind") or item.get("name") or "?"
    return clip(item, 40)


def scope_of(data: dict) -> dict | None:
    """not_checked as {count, first: the first ids}; None when nothing is left out."""
    value = data.get("not_checked")
    if isinstance(value, dict):
        ids = [str(key) for key in value]
    elif isinstance(value, list):
        ids = [_scope_id(item) for item in value]
    else:
        return None
    return {"count": len(ids), "first": ids[:SCOPE_LISTED]} if ids else None


def _source_rows(spec: CardSpec, data: dict) -> list[dict]:
    """The finding rows of page 1 in the tool's order, each with its severity (_sev)."""
    rows: list[dict] = []
    rule = spec.rule

    def add(row, level):
        if isinstance(row, dict):
            rows.append({**row, "_sev": severity(level)})

    if rule == "erc":
        for row in data.get("violations") or []:
            add(row, row.get("level") if isinstance(row, dict) else None)
        for row in data.get("advisories") or []:
            add(row, row.get("level", "info") if isinstance(row, dict) else None)
    elif rule == "compliance":
        for kind in data.get("kinds") or []:
            if isinstance(kind, dict) and kind.get("violation_count"):
                count, groups = kind["violation_count"], kind.get("violation_groups") or 0
                add({"check": str(kind.get("kind") or "rule"),
                     "message": f"{_plural(count, 'violation')} in {_plural(groups, 'group')}"},
                    "error")
    elif rule == "drc":
        for group in data.get("groups") or []:
            if isinstance(group, dict) and group.get("count"):
                add({"check": str(group.get("rule_kind") or "rule"),
                     "message": f"{group.get('rule')}: {_plural(group['count'], 'violation')}"},
                    "error")
        for issue in data.get("assessment") or []:
            if isinstance(issue, dict):
                add({"check": str(issue.get("issue") or "assessment"),
                     "message": issue.get("detail")}, issue.get("severity"))
    elif rule == "lengths":
        for row in data.get("rules") or []:
            if not isinstance(row, dict):
                continue
            summary = row.get("summary") if isinstance(row.get("summary"), dict) else {}
            name = row.get("name") or row.get("rule")
            if summary.get("violations"):
                add({"check": "length_rule", "rule": name,
                     "message": f"{name}: {_plural(summary['violations'], 'member')} outside "
                                "the rule"}, "error")
            elif _unevaluated(row):
                add({"check": "length_rule_not_evaluated", "rule": name,
                     "message": f"{name}: not evaluated in full "
                                f"({row.get('status') or 'members incomplete or unknown'})"},
                    "info")
    else:
        for row in data.get("findings") or []:
            if isinstance(row, dict):
                add(row, row.get("severity", row.get("level")))
    return rows


def _row(tool: str, row: dict) -> dict:
    ids = rc.finding_ids([row])
    text = next((row[key] for key in ("issue", "message", "evidence", "detail")
                 if isinstance(row.get(key), str) and row[key].strip()), "")
    anchors = rc.anchors_of([row], tool)
    at = {name: anchors[source][:limit] for name, source, limit in ANCHORS if anchors.get(source)}
    out = {"sev": row["_sev"], "check": next(iter(ids), ""), "text": clip(text, TEXT_CHARS)}
    if at:
        out["at"] = at
    return out


def _facts(spec: CardSpec, data: dict) -> list[list] | None:
    facts = []
    if spec.tool == "run_erc":
        suppressed = (data.get("summary") or {}).get("suppressed") or {}
        total = sum(value for group in suppressed.values() if isinstance(group, dict)
                    for value in group.values() if isinstance(value, int))
        facts.append(["Suppressed (No Report, No ERC)", _plural(total, "violation")])
    elif spec.tool == "check_bom":
        if isinstance(data.get("mpn_coverage_percent"), (int, float)):
            facts.append(["MPN coverage", f"{data['mpn_coverage_percent']} %"])
        not_fitted = (data.get("parts") or {}).get("not_fitted")
        if isinstance(not_fitted, int):
            facts.append(["Not Fitted", _plural(not_fitted, "part")])
    elif spec.tool == "check_connectivity":
        routed = (data.get("summary") or {}).get("routed_pct")
        if isinstance(routed, (int, float)):
            facts.append(["Routed", f"{routed} % of the nets"])
    elif spec.tool == "verify_outputs":
        if data.get("verdict"):
            facts.append(["Verdict", clip(data["verdict"], 60)])
    return facts or None


def _failed(reason: str) -> dict:
    return {"state": "failed", "reason": clip(reason, REASON_CHARS)}


def normalise(spec: CardSpec, run: dict, ctx: dict | None = None) -> dict:
    """One card from a ToolRunner result ({data, pages} or {error, message, ...}): state,
    counts, subject, scope, findings worst first (at most FINDINGS_STORED), the first
    finding ids, tags, reason and facts. ctx is not read today (the dynamic tags and
    clean rule are applied when the card is shown: present())."""
    del ctx
    if "error" in run:
        code = str(run.get("error"))
        message = run.get("message") or code
        if code == "tool_failed":
            return _failed(f"{run.get('exception') or 'Exception'}: {message}")
        if code == "not_json":
            return _failed(f"{spec.tool} did not return JSON")
        if spec.tool == "get_drc_report" and code == "no_drc_report":
            return {"state": "not_verified", "reason": NO_DRC_REPORT}
        if code in NOT_APPLICABLE:
            return {"state": "not_applicable", "reason": clip(message, NA_REASON_CHARS)}
        if code in UNAVAILABLE:
            return {"state": "unavailable", "reason": UNAVAILABLE[code]}
        return _failed(f"{code}: {message}")
    data = run.get("data")
    if not isinstance(data, dict):
        return _failed(f"{spec.tool} did not return a JSON object")
    try:
        counts = counts_of(spec, data)
    except _Unreadable as error:
        return {"state": "unreadable",
                "reason": f"this tool's result changed shape ({error.args[0]} is missing): ask "
                          f"Claude to run {spec.tool}"}
    card: dict = {"counts": counts}
    subject = subject_of(spec, data)
    if subject is not None:
        card["subject"] = subject
    judged = judged_of(spec, data) if spec.tool in JUDGED else None
    if judged is not None and judged != subject:
        card["judged"] = judged
    scope = scope_of(data)
    if scope:
        card["not_checked"] = scope
    tags = []
    if spec.advisory:
        tags.append("advisory")
    if "heuristic" in data:
        tags.append("heuristic")
    if spec.rule == "erc":
        tags.append("offline_approximation")
    untrusted = []
    if spec.rule == "drc":
        issues = [issue for issue in data.get("assessment") or [] if isinstance(issue, dict)]
        untrusted = [str(issue.get("issue")) for issue in issues
                     if severity(issue.get("severity")) in ("error", "warning")]
        if untrusted:
            tags.append("report_untrusted")
        if any(issue.get("issue") == "violation_limit_reached" for issue in issues):
            tags.append("at_least")
    if tags:
        card["tags"] = tags
    rows = _source_rows(spec, data)
    rank = {level: index for index, level in enumerate(SEVERITIES)}
    ordered = sorted(rows, key=lambda row: rank[row["_sev"]])  # stable: the tool's order
    card["findings"] = [_row(spec.tool, row) for row in ordered[:FINDINGS_STORED]]
    # The rows of the whole result where the tool lists findings (a page holds the first).
    if spec.rule == "erc":
        # Violation rows (total) and advisories, the two lists the card's rows come from.
        violations, advisories = data.get("total"), data.get("advisories_total")
        violations = violations if _is_int(violations) else len(data.get("violations") or [])
        advisories = advisories if _is_int(advisories) else len(data.get("advisories") or [])
        total = violations + advisories
    elif spec.rule in ("sc", "summary_sc", "summary_levels", "summary_findings"):
        total = data.get("total", data.get("findings_total"))
    else:
        total = None
    card["findings_total"] = total if _is_int(total) and total >= len(rows) else len(rows)
    if spec.rule == "erc":
        # The objects per violation over the whole result (a page-1 row can group many).
        by_violation = [row for row in (data.get("summary") or {}).get("by_violation") or []
                        if isinstance(row, dict) and _is_int(row.get("count"))]
        ranked = sorted(by_violation, key=lambda row: -row["count"])
        ids = {str(row.get("violation")): row["count"] for row in ranked}
    else:
        ids = rc.finding_ids([{k: v for k, v in row.items() if k != "_sev"} for row in rows])
    if ids:
        card["ids"] = [[name, count] for name, count in list(ids.items())[:IDS_LISTED]]
    worst = next((level for level in SEVERITIES if counts[level]), None)
    if worst and not any(row["_sev"] == worst for row in rows):
        word = {"error": "error", "warning": "warning", "info": "note"}[worst]
        card["first_note"] = (f"page 1 shows none of the {_plural(counts[worst], word)}: "
                              "open the check")
    facts = _facts(spec, data)
    if facts:
        card["facts"] = facts
    if worst:
        card["state"] = {"error": "errors", "warning": "warnings", "info": "notes"}[worst]
    elif untrusted:
        card["state"] = "not_verified"
        card["reason"] = (f"the report is not to be trusted ({', '.join(untrusted[:3])}): run "
                          "the Batch DRC again in Altium")
    elif subject == 0:
        card["state"] = "nothing_to_check"
        card["reason"] = f"nothing to check: 0 {spec.noun}"
    elif spec.rule == "erc":
        card["state"] = "partial"
        card["reason"] = ("no findings, but this is the offline approximation of Altium's ERC: "
                          "Compile in Altium is the final word")
    elif scope:
        checks_run = data.get("checks_run")
        examined = (f"the {_plural(len(checks_run), 'check')} run" if isinstance(checks_run, list)
                    else f"{subject} {spec.noun}" if subject is not None else "what was checked")
        card["state"] = "partial"
        card["reason"] = f"no findings in {examined}, {scope['count']} not checked"
    elif subject is None:
        card["state"] = "partial"
        card["reason"] = "no findings, but what was examined is unknown"
    elif spec.tool in JUDGED and judged is None:
        card["state"] = "partial"
        card["reason"] = "no findings, but how much it could judge is unknown"
    elif spec.tool in JUDGED and judged == 0:
        card["state"] = "partial"
        card["reason"] = (f"no findings, but none of the {subject} {spec.noun} could be judged: "
                          f"{JUDGED[spec.tool][1]}")
    elif data.get("verdict") in INCOMPLETE_VERDICTS.get(spec.tool, ()):
        card["state"] = "partial"
        card["reason"] = (f"no findings, but {spec.tool}'s verdict is {data['verdict']}: a rule "
                          "could not be evaluated or left objects not evaluated")
    else:
        card["state"] = "clean"
    return card


def normalise_release(run: dict) -> dict:
    """The release block from check_release_readiness (every page of gate rows)."""
    if "error" in run:
        code = str(run.get("error"))
        if code == "tool_failed":
            return _failed(f"{run.get('exception') or 'Exception'}: {run.get('message')}")
        return _failed(f"{code}: {run.get('message') or code}")
    data = run.get("data")
    try:
        if not isinstance(data, dict):
            raise _Unreadable("result")
        verdict = _dig(data, "verdict")
        gates = _dig(data, "gates")
        counts = _dig(data, "counts")
        if not isinstance(gates, list) or not isinstance(counts, dict):
            raise _Unreadable("gates")
        rows = [row for row in gates if isinstance(row, dict) and "id" in row and "status" in row]
    except _Unreadable as error:
        return {"state": "unreadable",
                "reason": f"this tool's result changed shape ({error.args[0]} is missing): ask "
                          f"Claude to run {RELEASE_TOOL}"}
    by_id = {row["id"]: row for row in rows}
    failed = [str(item) for item in data.get("blocking_failed") or []]
    card = {
        "state": "ran",
        "verdict": str(verdict),
        "verdict_summary": clip(data.get("verdict_summary") or "", SUMMARY_CHARS),
        "counts": {str(key): value for key, value in counts.items() if isinstance(value, int)},
        "blocking_failed": [{"id": gate, "title": clip((by_id.get(gate) or {}).get("title") or gate, 120),
                             "evidence": clip((by_id.get(gate) or {}).get("evidence") or "",
                                              EVIDENCE_STORED)} for gate in failed],
        "blocking_not_verified": [str(item) for item in data.get("blocking_not_verified") or []],
        "gates": [[row["id"], row["status"], bool(row.get("blocking")), row.get("source_tool"),
                   clip(row.get("title") or row["id"], 120)] for row in rows],
        "gates_total": data.get("gates_total", len(rows)),
    }
    if data.get("truncated") or (isinstance(card["gates_total"], int) and card["gates_total"] > len(rows)):
        card["gates_note"] = f"{len(rows)} of {card['gates_total']} gates read"
    return card


def normalise_fact(tool: str, run: dict) -> dict:
    """The overview's facts from get_board_info (mm) or list_variants."""
    if "error" in run:
        code = str(run.get("error"))
        reason = run.get("message") or code
        if code == "tool_failed":
            reason = f"{run.get('exception') or 'Exception'}: {reason}"
        state = "not_applicable" if code in NOT_APPLICABLE else "failed"
        return {"state": state, "reason": clip(reason, REASON_CHARS)}
    data = run.get("data") or {}
    if tool == "list_variants":
        variants = [row for row in data.get("variants") or [] if isinstance(row, dict)]
        active = next((row for row in variants if row.get("is_active")), None)
        return {"state": "ran", "facts": {
            "active": data.get("active_variant"),
            "dnp": active.get("dnp_count") if active else None,
            "names": [str(row.get("name")) for row in variants][:20]}}
    facts: dict = {}
    stackup = data.get("stackup")
    if isinstance(stackup, list):
        facts["copper_layers"] = sum(1 for row in stackup
                                     if isinstance(row, dict) and row.get("kind") == "copper")
    extents = data.get("board_extents_mm")
    if isinstance(extents, dict) and all(isinstance(extents.get(key), (int, float)) for key in
                                         ("minx_mm", "miny_mm", "maxx_mm", "maxy_mm")):
        facts["board_mm"] = [round(extents["maxx_mm"] - extents["minx_mm"], 1),
                             round(extents["maxy_mm"] - extents["miny_mm"], 1)]
    sides = data.get("components_per_side")
    if isinstance(sides, dict):
        facts["parts_top"] = sides.get("top")
        facts["parts_bottom"] = sides.get("bottom")
    counts = data.get("counts") if isinstance(data.get("counts"), dict) else {}
    facts["nets"] = counts.get("nets")
    facts["vias"] = counts.get("vias")
    rules = data.get("design_rules") if isinstance(data.get("design_rules"), dict) else {}
    facts["rules"] = rules.get("rules")
    facts["diff_pairs"] = rules.get("diff_pairs")
    facts["last_saved_utc"] = data.get("last_saved_utc")
    return {"state": "ran", "facts": {key: value for key, value in facts.items() if value is not None}}


# ---------- running ----------

def _args_of(tool: str) -> dict:
    if tool in SPECS:
        return dict(SPECS[tool].args)
    return dict(FACT_ARGS.get(tool, {}))


def _needs() -> dict:
    needs = {rc.ToolRunner.key(tool, _args_of(tool)): set() for tool in RUN_ORDER}
    needs[rc.ToolRunner.key(RELEASE_TOOL, {})] = {"gates"}
    return needs


def _compute(tool: str, runner: rc.ToolRunner, inputs: Inputs, as_of: str) -> dict:
    """One card, run now. as_of is the call's: the cards one call runs share it (the state
    key was read when the call started)."""
    start = inputs.clock()
    run = runner.run(tool, _args_of(tool))
    ms = round((inputs.clock() - start) * 1000)
    if tool == RELEASE_TOOL:
        card = normalise_release(run)
    elif tool in FACT_TOOLS:
        card = normalise_fact(tool, run)
    else:
        card = normalise(SPECS[tool], run)
    card["tool"] = tool
    card["ms"] = ms
    card["as_of"] = as_of
    card["variant"] = inputs.variant
    return card


def parse_checks(checks) -> tuple[list[str] | None, dict | None]:
    """The names asked for (stripped, lower-cased, de-duplicated, in RUN_ORDER), None for
    the default call (None or an empty list); or the unknown_checks error."""
    if checks is None:
        return None, None
    if isinstance(checks, str):
        checks = [checks]
    names: list[str] = []
    for item in checks:
        name = str(item).strip().lower()
        if name and name not in names:
            names.append(name)
    if not names:
        return None, None
    unknown = [name for name in names if name not in ACCEPTED]
    if unknown:
        return None, {"error": "unknown_checks",
                      "message": f"Unknown checks: {', '.join(clip(name, 60) for name in unknown[:10])}. "
                                 "Name cards by their tool.",
                      "accepted": list(ACCEPTED)}
    return [tool for tool in RUN_ORDER if tool in names], None


def build(inputs: Inputs, cache: DashboardCache, checks=None, refresh: bool = False) -> dict:
    """The dashboard model: the cards the call asked for, computed now when not current
    (the default call within the time budget), else from the cache. An error dict for
    unknown names."""
    names, error = parse_checks(checks)
    if error:
        return error
    project = inputs.project
    entry = cache.entry(str(project.get("prj_pcb_path") or project.get("root_dir") or ""))
    if refresh and names is None:
        entry["generation"] += 1
    parts, info = state_parts(inputs, entry["generation"])
    key = state_key(parts)
    cards: dict = entry["cards"]
    runner = rc.ToolRunner(inputs.tools, max_pages=RELEASE_PAGES, clock=inputs.clock,
                           needs=_needs())
    as_of = inputs.now().strftime("%Y-%m-%dT%H:%M:%SZ")
    start = inputs.clock()
    computed: dict[str, dict] = {}
    left_out: list[str] = []
    for tool in (RUN_ORDER if names is None else names):
        old = cards.get(tool)
        if old is not None and old.get("key") == key and not (refresh and names is not None):
            continue
        if names is None and tool != RELEASE_TOOL:
            elapsed = inputs.clock() - start
            if elapsed >= inputs.budget_s or (tool in SLOW and elapsed >= inputs.slow_start_s):
                left_out.append(tool)
                continue
        card = _compute(tool, runner, inputs, as_of)
        card["key"] = key
        card["parts"] = dict(parts)
        if old is not None and old.get("counts") is not None and card.get("counts") is not None \
                and old["counts"] != card["counts"]:
            card["was"] = {"counts": old["counts"], "as_of": old.get("as_of")}
        computed[tool] = card
        if card["state"] not in UNCACHED:
            cards[tool] = card
    spent = round((inputs.clock() - start) * 1000)
    return _model(inputs, names, refresh, key, parts, info, cards, computed, left_out, spent, as_of)


# ---------- the model ----------

def _netlist_state(inputs: Inputs) -> tuple[bool, str | None]:
    if inputs.netlist_freshness is None:
        return False, None
    stale, note = inputs.netlist_freshness()
    return bool(stale), (clip(note, NOTE_CHARS) if stale and note else None)


def present(spec: CardSpec, card: dict | None, key: str, parts: dict, netlist_stale: bool,
            netlist_tools, left_out=(), variant: str | None = None) -> dict:
    """A card as the answer shows it: current or stale (with the first part of the state
    that changed), the dynamic tags, and clean only when nothing limits it. The catalogue's
    own facts (title, group, view, board, slow) are not repeated: the view holds them; a
    card's variant is given when it is not the answer's (variant)."""
    out: dict = {"tool": spec.tool}
    if card is None:
        out.update(state="not_run", current=False,
                   reason="time budget" if spec.tool in left_out else "not run yet")
    else:
        current = card.get("key") == key
        state = card["state"]
        tags = list(card.get("tags") or [])
        reason = card.get("reason")
        if netlist_stale and spec.tool in netlist_tools and "counts" in card:
            tags.append("netlist_older_than_sheets")
        if state == "clean" and "netlist_older_than_sheets" in tags:
            state = "partial"
            reason = "no findings, but the netlist is older than the sheets: refresh the netlist"
        out["state"] = state
        out["current"] = current
        if not current:
            out["stale_because"] = stale_because(card.get("parts"), parts)
            if state == "clean":
                out["state"] = "stale"
                out["was_state"] = "clean"
        for name in ("counts",):
            if name in card:
                out[name] = card[name]
        if "subject" in card:
            out["subject"] = {"count": card["subject"], "noun": spec.noun}
        for name in ("judged", "not_checked", "findings", "findings_total", "first_note", "ids"):
            if card.get(name) or (name == "judged" and name in card):
                out[name] = card[name]
        if tags:
            out["tags"] = tags
        if reason:
            out["reason"] = reason
        for name in ("was", "facts"):
            if card.get(name):
                out[name] = card[name]
        out["as_of"] = card.get("as_of")
        out["ms"] = card.get("ms")
        if card.get("variant") != variant:
            out["variant"] = card.get("variant")
    return out


def _release_block(card: dict | None, key: str) -> dict:
    if card is None:
        return {"state": "not_run", "current": False}
    out = {key_: value for key_, value in card.items()
           if key_ not in ("key", "parts", "tool", "variant")}
    out["current"] = card.get("key") == key
    return out


def _overview(inputs: Inputs, board: dict | None, variants: dict | None, key: str,
              parts: dict) -> dict:
    project = inputs.project
    out: dict = {
        "sheets": len(project.get("sheets") or []),
        "drawings": len(project.get("pcb_dwf_paths") or []),
    }
    notes = []
    for card, name in ((board, "Board"), (variants, "Variant")):
        if card and card.get("state") == "ran" and card.get("key") != key:
            notes.append(f"{name} facts are from older files "
                         f"({stale_because(card.get('parts'), parts)}).")
    if board and board.get("state") == "ran":
        out["board"] = board["facts"]
    elif board:
        notes.append(f"Board facts left out: {board.get('reason') or board.get('state')}")
    else:
        notes.append("Board facts not run (time budget).")
    if variants and variants.get("state") == "ran":
        facts = variants["facts"]
        out["variants"] = {"defined": inputs.defined_variants, "active": facts.get("active"),
                           "dnp": facts.get("dnp")}
    elif variants:
        notes.append(f"Variant facts left out: {variants.get('reason') or variants.get('state')}")
    else:
        notes.append("Variant facts not run (time budget).")
    if notes:
        out["notes"] = notes
    return out


def _cards_as_of(rows: list[dict]) -> str | None:
    """The as_of most of these cards share (the latest on a tie); each card then gives its
    own only when it differs."""
    stamps = Counter(row["as_of"] for row in rows if row.get("as_of"))
    if not stamps:
        return None
    return max(stamps, key=lambda stamp: (stamps[stamp], stamp))


def _model(inputs, names, refresh, key, parts, info, cards, computed, left_out, spent,
           as_of) -> dict:
    project = inputs.project
    pick = lambda tool: computed.get(tool) or cards.get(tool)
    release = pick(RELEASE_TOOL)
    netlist_stale, netlist_note = _netlist_state(inputs)
    shown = CATALOGUE if names is None else [SPECS[tool] for tool in names if tool in SPECS]
    rows = [present(spec, pick(spec.tool), key, parts, netlist_stale, inputs.netlist_tools,
                    left_out, inputs.variant) for spec in shown]
    all_rows = rows if names is None else [
        present(spec, pick(spec.tool), key, parts, netlist_stale, inputs.netlist_tools,
                variant=inputs.variant)
        for spec in CATALOGUE]
    cards_as_of = _cards_as_of(rows)
    for row in rows:
        if "as_of" in row and row["as_of"] == cards_as_of:
            del row["as_of"]
    by_state: dict = {}
    for row in all_rows:
        by_state[row["state"]] = by_state.get(row["state"], 0) + 1
    totals = dict.fromkeys(SEVERITIES, 0)
    for row in all_rows:
        if row.get("current") and row.get("counts"):
            for level in SEVERITIES:
                totals[level] += row["counts"].get(level, 0)
    pcb_paths = project.get("pcb_doc_paths") or []
    state = {
        "key": key,
        "as_of_utc": as_of,
        "cards_as_of_utc": cards_as_of,
        "latest_save_utc": info["latest_save_utc"],
        "netlist_updated_utc": inputs.netlist_updated_utc,
        "netlist_older_than_sheets": netlist_stale,
    }
    if netlist_note:
        state["netlist_note"] = netlist_note
    state["folder_watched"] = info["folder_watched"]
    model = {
        "project": project.get("name"),
        "variant": inputs.variant,
        "load": inputs.load,
        "pcb_doc": Path(pcb_paths[0]).name if pcb_paths else None,
        "args": {"checks": names, "refresh": bool(refresh)},
        "state": state,
    }
    # The overview comes with a default call and with any call that names a fact tool.
    if names is None or any(tool in names for tool in FACT_TOOLS):
        model["overview"] = _overview(inputs, pick("get_board_info"), pick("list_variants"),
                                      key, parts)
    model["release"] = _release_block(release, key)
    model["summary"] = {
        "cards": len(CATALOGUE),
        "current": sum(1 for row in all_rows if row.get("current")),
        "stale": sum(1 for row in all_rows if not row.get("current") and row["state"] != "not_run"),
        "by_state": by_state,
        "in_checks_run": totals,
    }
    model["cards"] = rows
    if names is not None:
        others = [row for row in all_rows if row["tool"] not in names]
        model["others"] = {
            "current": sum(1 for row in others if row.get("current")),
            "stale": sum(1 for row in others if not row.get("current") and row["state"] != "not_run"),
            "not_run": sum(1 for row in others if row["state"] == "not_run"),
        }
    ordered = [tool for tool in left_out if tool not in SLOW] + [tool for tool in left_out if tool in SLOW]
    model["not_run"] = {
        "checks": ordered,
        "reason": "time budget" if ordered else None,
        "next": f"{TOOL}(checks={json.dumps(ordered)})" if ordered else None,
    }
    model["budget"] = {
        "limit_s": inputs.budget_s if names is None else None,
        "spent_ms": spent,
        "stopped_at": left_out[0] if left_out else None,
    }
    model["excluded"] = dict(EXCLUDED)
    model["notes"] = list(NOTES)
    return model


# ---------- the text ----------

@dataclass(frozen=True)
class Step:
    """One rung of the size ladder: findings per card, finding ids per card, characters
    of a finding's text and of a failed gate's evidence, which cards keep their findings
    (all, those with warnings or errors, those with errors), and whether the scope's
    first ids, the gate titles and the excluded list are kept."""
    findings: int
    ids: int = IDS_LISTED
    text: int = TEXT_CHARS
    evidence: int = 200
    which: str = "all"
    extras: bool = True


# Tried in turn until the answer fits MAX_RESULT_CHARS.
LADDER = (
    Step(2),
    Step(1, text=160),
    Step(1, ids=0, text=160, extras=False),
    Step(1, ids=0, text=120, extras=False),
    Step(1, ids=0, text=120, which="warning", extras=False),
    Step(1, ids=0, text=80, which="warning", extras=False),
    Step(1, ids=0, text=80, which="error", extras=False),
    Step(1, ids=0, text=80, evidence=160, which="error", extras=False),
    Step(0, ids=0, text=80, evidence=160, which="error", extras=False),
)
# A narrowed answer with three names or fewer lists up to 20 findings per card.
NARROW_LADDER = (Step(20), Step(10), Step(5, text=160), *LADDER[1:])


def _shrunk(model: dict, step: Step, narrow: bool) -> dict:
    out = dict(model)
    cards = []
    for card in model["cards"]:
        card = dict(card)
        keep = step.findings
        counts = card.get("counts") or {}
        worst = counts.get("error") or (step.which == "warning" and counts.get("warning"))
        if step.which != "all" and not (card.get("current") and worst):
            keep = 0
        rows = card.get("findings")
        if rows:
            if keep:
                card["findings"] = [{**row, "text": clip(row.get("text"), step.text)}
                                    for row in rows[:keep]]
            else:
                card.pop("findings")
        if card.get("ids"):
            if step.ids:
                card["ids"] = card["ids"][:step.ids]
            else:
                card.pop("ids")
        if not step.extras and card.get("not_checked"):
            card["not_checked"] = {"count": card["not_checked"]["count"]}
        cards.append(card)
    out["cards"] = cards
    release = dict(model["release"])
    if release.get("blocking_failed"):
        release["blocking_failed"] = [{**row, "evidence": clip(row.get("evidence"), step.evidence)}
                                      for row in release["blocking_failed"]]
    if not step.extras and release.get("gates"):
        release["gates"] = [row[:4] for row in release["gates"]]
    if narrow and RELEASE_TOOL not in (model["args"]["checks"] or ()):
        # A narrowed answer that does not name the release gives its verdict only.
        release = {name: release[name] for name in ("verdict", "counts", "current", "state", "reason")
                   if name in release}
    out["release"] = release
    if not step.extras:
        out.pop("excluded", None)
    return out


_ROW_LISTS = ("cards", "gates", "blocking_failed")  # one compact row per line


def dumps(value, depth: int = 0) -> str:
    """JSON indented by one space for the answer's blocks, with each card and gate row on
    one compact line: the model reads a row at a time, and the answer stays small."""
    pad = " " * (depth + 1)
    if isinstance(value, dict) and depth < 2 and value:
        items = [f"{pad}{json.dumps(key)}: "
                 + (dumps(item, depth + 1) if key in _ROW_LISTS or isinstance(item, dict)
                    else json.dumps(item, separators=(",", ":")))
                 for key, item in value.items()]
        return "{\n" + ",\n".join(items) + "\n" + " " * depth + "}"
    if isinstance(value, list) and value and depth >= 1:
        return ("[\n" + ",\n".join(pad + json.dumps(item, separators=(",", ":")) for item in value)
                + "\n" + " " * depth + "]")
    return json.dumps(value, separators=(",", ":"))


def render(model: dict, max_chars: int = MAX_RESULT_CHARS) -> str:
    """The answer's text: JSON (indent 1, a card per line), never paged, at most
    max_chars, the findings and their text shortened step by step until it fits."""
    if "error" in model:
        return json.dumps(model)
    narrow = model["args"]["checks"] is not None
    ladder = NARROW_LADDER if narrow and len(model["args"]["checks"]) <= 3 else LADDER
    text = ""
    for step in ladder:
        text = dumps(_shrunk(model, step, narrow))
        if len(text) <= max_chars:
            return text
    # Past the ladder: the excluded list and the notes go, then the gate rows.
    out = _shrunk(model, ladder[-1], narrow)
    out.pop("excluded", None)
    out["notes"] = ["Shortened to fit: call with checks= for a card's findings."]
    text = dumps(out)
    if len(text) > max_chars and "gates" in out["release"]:
        out["release"] = {name: value for name, value in out["release"].items() if name != "gates"}
        text = dumps(out)
    return text
