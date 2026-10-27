"""Constraints review: prompt text, typical interface targets and a board snapshot.

Pure text building over a PcbIndex: no MCP. The constraints_review tool returns
build_constraints_review(...) the way schematic_review returns its prompt.

The snapshot holds bounded facts only (layers, pairs, classes, rule counts,
stale and DRC-unchecked rule kinds). It names no interfaces: net-name
classification proved unreliable on the example boards (DisplayPort lanes read
as USB, a USB bridge's FIFO strobes read as SuperSpeed), so the prompt has
Claude identify interfaces from parts and pins instead.
"""

from pathlib import PureWindowsPath

try:
    from services.pcb_rules import BINARY_KINDS, NET_RULE_KINDS, stale_references
except ImportError:
    from server.services.pcb_rules import (
        BINARY_KINDS, NET_RULE_KINDS, stale_references,
    )

# ---------- typical interface targets ----------

# Fallback values only: the part's own datasheet or layout guide is the
# requirement. Summarised in our own words from the cited documents; sources
# disagree, sometimes by an order of magnitude, which the rows say.
INTERFACE_TARGETS: tuple[dict, ...] = (
    {
        "interface": "General (any high-speed pair)",
        "impedance": "Per interface below; reference a solid ground plane on an adjacent layer, not a power plane",
        "skew_length": "Same via count on both legs; break out of the BGA within about 0.25 in",
        "other": "Spacing at least 5W (about 30 mil) to other pairs and signals, at least 50 mil to clocks and periodic signals; at least 90 mil from a reference-plane edge and 1.5W from voids; no crossing of plane splits (a stitching capacitor of at most 1 µF if one is unavoidable); a ground stitching via within about 200 mil of every layer-change via",
        "source": "TI SPRAAR7J §2.4, §3.1–3.3, §3.9",
    },
    {
        "interface": "USB 2.0 (high speed)",
        "impedance": "90 Ω differential (81–99 Ω), about 45 Ω single-ended",
        "skew_length": "In-pair 50 mil per TI, while an Altium tuning article asks for a much tighter match (sources disagree); some SoCs also bound total length (4000–12000 mil)",
        "other": "At most 4 vias per trace",
        "source": "TI SPRAAR7J Tables A-1, A-2, A-7",
    },
    {
        "interface": "USB 3.x SuperSpeed",
        "impedance": "Device-specific: 90 Ω ±7% on AM57xx, 95 Ω ±5% on AM65xx",
        "skew_length": "In-pair 5 mil",
        "other": "At most 2 vias per trace",
        "source": "TI SPRAAR7J Tables A-2, A-5",
    },
    {
        "interface": "PCIe",
        "impedance": "100 Ω for Gen1–2, 85 or 100 Ω for Gen3, 85 Ω for Gen4–6; TI device tables give 100 Ω ±10% (AM57xx) and 95 Ω ±5% (AM65xx)",
        "skew_length": "In-pair 5 mil; lane-to-lane loose (e.g. 550 mil, or a ns-level budget)",
        "other": "AC-coupling capacitors of 176–265 nF on both legs of each TX pair, placed near the transmitter",
        "source": "Altium PCIe layout guide and AC-coupling article; TI SPRAAR7J",
    },
    {
        "interface": "SATA",
        "impedance": "100 Ω differential (85–115 Ω)",
        "skew_length": "In-pair 5 mil",
        "other": "—",
        "source": "TI SPRAAR7J Table A-2",
    },
    {
        "interface": "HDMI (TMDS)",
        "impedance": "100 Ω differential (90–110 Ω)",
        "skew_length": "In-pair 5 mil",
        "other": "—",
        "source": "TI SPRAAR7J Table A-2",
    },
    {
        "interface": "DisplayPort",
        "impedance": "100 Ω differential",
        "skew_length": "In-pair about 5 mil; lane-to-lane within about 2 UI",
        "other": "—",
        "source": "TI TDP142-Q1 datasheet, layout section (citing the DP design guide)",
    },
    {
        "interface": "Ethernet MDI (100BASE-TX / 1000BASE-T)",
        "impedance": "100 Ω ±10% differential, 50 Ω single-ended (one MCU vendor guide asks ±5%)",
        "skew_length": "ADIN1300 example: in-pair at most 20 mil at 1G and 50 mil at 100M, pair-to-pair about 1000 mil",
        "other": "—",
        "source": "Renesas R19AN0014; Microchip Ethernet layout guide; ADI ADIN1300 layout notes",
    },
    {
        "interface": "RGMII / RMII",
        "impedance": "No generic value: use the MAC and PHY datasheets",
        "skew_length": "Clock-to-data timing budget from the MAC and PHY datasheets (depends on the internal-delay mode)",
        "other": "—",
        "source": "MAC and PHY datasheets (no generic value in the reviewed guides)",
    },
    {
        "interface": "SGMII / multi-gigabit SerDes",
        "impedance": "About 100 Ω differential (SGMII 95 Ω ±5% on AM65xx); otherwise the FPGA or PHY PCB guide",
        "skew_length": "From the FPGA or PHY PCB guide",
        "other": "—",
        "source": "TI SPRAAR7J Table A-5; the FPGA or PHY PCB design guide",
    },
    {
        "interface": "DDR4",
        "impedance": "40 Ω single-ended, 80 Ω differential, ±10%",
        "skew_length": "Match only within a byte lane (DQ/DM to its DQS), never across bytes; DQS P/N 0.4 ps; DQS to DQ 2 ps; CK to address/control per the controller's limits. Convert ps with the layer's propagation delay (TI's rule of thumb is about 5 mil per ps)",
        "other": "At least one solid VSS reference directly adjacent, no plane cuts under the bus; equal via counts within a data group",
        "source": "TI SPRAD06C Tables 1-1, 2-6, 2-7",
    },
    {
        "interface": "DDR3",
        "impedance": "Controller-specific; e.g. AM335x about 50 Ω single-ended, differential twice that",
        "skew_length": "Controller-specific",
        "other": "—",
        "source": "AM335x datasheet §7.7.2.3, via a TI E2E thread (secondary source)",
    },
    {
        "interface": "DDR5 / LPDDR4 / LPDDR5",
        "impedance": "Controller-specific: no generic number",
        "skew_length": "Controller-specific (TI SPRAD66 shows the form for LPDDR4)",
        "other": "—",
        "source": "TI SPRAD66; the memory controller's layout guide",
    },
    {
        "interface": "LVDS",
        "impedance": "100 Ω differential, closely coupled",
        "skew_length": "In-pair under 5 mil (vendor example)",
        "other": "—",
        "source": "National LVDS Owner's Manual ch. 3; Pericom AN330",
    },
    {
        "interface": "MIPI D-PHY (CSI / DSI)",
        "impedance": "85–115 Ω differential and 50 Ω single-ended (AM62x); 95 Ω ±10% (AMD); 100 Ω (Intel); some SoCs ask 90 Ω",
        "skew_length": "Lane skew 40 ps (AM62x); P/N ±2 ps and clock-to-data ±8 ps (AMD); data-to-clock ±50 mil (Intel)",
        "other": "At most 2 vias (AM62x)",
        "source": "TI SPRAAR7J Table A-7; AMD UG863; Intel MIPI layout guidelines",
    },
    {
        "interface": "CAN",
        "impedance": "120 Ω characteristic impedance",
        "skew_length": "—",
        "other": "120 Ω termination at both ends of the bus; keep stubs short",
        "source": "TI SN65HVD234-Q1 and HVDA551-Q1 datasheets; TI SLLA270",
    },
    {
        "interface": "RS-485 / RS-422",
        "impedance": "120 Ω characteristic impedance",
        "skew_length": "Stub round-trip delay under 1/10 of the driver rise time",
        "other": "120 Ω termination at both ends of the trunk",
        "source": "TI SLLA272, SLLA595",
    },
)

_TARGET_COLUMNS = (
    ("interface", "Interface"),
    ("impedance", "Impedance"),
    ("skew_length", "Skew / length"),
    ("other", "Other"),
    ("source", "Source"),
)


def _cell(text: str) -> str:
    return " ".join(str(text).replace("|", "/").split()) or "—"


def render_interface_targets() -> str:
    """INTERFACE_TARGETS as a Markdown table."""
    lines = [
        "| " + " | ".join(title for _, title in _TARGET_COLUMNS) + " |",
        "|" + "---|" * len(_TARGET_COLUMNS),
    ]
    for row in INTERFACE_TARGETS:
        lines.append(
            "| " + " | ".join(_cell(row[key]) for key, _ in _TARGET_COLUMNS) + " |"
        )
    return "\n".join(lines)


# ---------- board snapshot ----------

# Rule kinds a constraints review reads, in the order the snapshot lists them.
# "ReturnPath" is Altium's RULEKIND string (AD25 rule-kind table); the corpus
# has no such rule, so its absence is the normal case.
REVIEW_RULE_KINDS = (
    "DiffPairsRouting", "Width", "Clearance", "MatchedLengths", "Length",
    "RoutingVias", "RoutingLayers", "ReturnPath",
)
_PROFILE_KINDS = ("DiffPairsRouting", "Width")
_STALE_KINDS = frozenset(NET_RULE_KINDS) | {"ReturnPath"}

SNAPSHOT_MAX_CLASSES = 20
SNAPSHOT_MAX_STALE_RULES = 10
_MAX_REFS_PER_RULE = 2
_MAX_NAME_CHARS = 60


def _short(text: str, limit: int = _MAX_NAME_CHARS) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _capped(items: list[str], limit: int, separator: str = ", ") -> str:
    shown = separator.join(items[:limit])
    if len(items) > limit:
        shown += f"{separator}+{len(items) - limit} more"
    return shown


def _real_classes(pcb, kind: str) -> list:
    return [
        pcb_class
        for pcb_class in pcb.classes
        if pcb_class.kind == kind
        and not pcb_class.superclass
        and not pcb_class.auto_generated
    ]


def _is_plane(v6_id: int) -> bool:
    return 39 <= v6_id <= 54


def _layers_line(index) -> str:
    """Copper layers in stack order; internal planes marked, since a
    reference plane must sit next to each routing layer."""
    source = getattr(index.pcb.board, "stackup_source", "?")
    copper = [
        (row["v6_id"], row["name"])
        for row in index.db.execute(
            "SELECT v6_id, name FROM layers "
            "WHERE kind = 'copper' AND v6_id IS NOT NULL ORDER BY stack_order"
        )
        if 1 <= row["v6_id"] <= 32 or _is_plane(row["v6_id"])
    ]
    routable = sum(1 for v6_id, _ in copper if not _is_plane(v6_id))
    if not routable:
        return f"- Copper: no routable signal layers found (stackup source: {source})"
    planes = len(copper) - routable
    counts = f"{routable} routable signal layer{'s' if routable != 1 else ''}" + (
        f" and {planes} internal plane layer{'s' if planes != 1 else ''}"
        if planes
        else ""
    )
    names = [
        _short(name) + (" (plane)" if _is_plane(v6_id) else "")
        for v6_id, name in copper
    ]
    return (
        f"- Copper: {counts}, in stack order: "
        + ", ".join(names)
        + f" (stackup source: {source})"
    )


def _pair_lines(index) -> list[str]:
    pcb = index.pcb
    pairs = pcb.diff_pairs
    if not pairs:
        line = "- Differential pairs: none defined on the PCB"
    else:
        unclassed = sum(1 for pair in pairs if not index.pair_classes_of(pair.name))
        line = f"- Differential pairs: {len(pairs)}; in no pair class: {unclassed}"
    pair_classes = _real_classes(pcb, "diff_pair")
    if not pair_classes:
        return [line, "- Pair classes: none"]
    known = index.rule_context.pairs
    entries = []
    for pcb_class in pair_classes:
        missing = sum(1 for member in pcb_class.members if member.lower() not in known)
        count = f"{len(pcb_class.members)}" + (
            f", {missing} unresolved" if missing else ""
        )
        entries.append(f"{_short(pcb_class.name)} ({count})")
    return [
        line,
        f"- Pair classes ({len(pair_classes)}; members in brackets): "
        + _capped(entries, SNAPSHOT_MAX_CLASSES),
    ]


def _class_line(index) -> str:
    pcb = index.pcb
    return (
        f"- Net classes: {len(_real_classes(pcb, 'net'))}; "
        f"xSignal classes: {len(_real_classes(pcb, 'xsignal'))}; "
        f"xSignals: {len(getattr(pcb, 'xsignals', None) or [])}"
    )


def _rules_by_kind(index) -> dict[str, list]:
    by_kind: dict[str, list] = {}
    for rule in index.pcb.rules:
        by_kind.setdefault(rule.kind, []).append(rule)
    return by_kind


def _rule_count_lines(by_kind: dict[str, list]) -> list[str]:
    counts = []
    for kind in REVIEW_RULE_KINDS:
        rules = by_kind.get(kind, [])
        disabled = sum(1 for rule in rules if not rule.enabled)
        counts.append(
            f"{kind} {len(rules)}" + (f" ({disabled} disabled)" if disabled else "")
        )
    profile = []
    for kind in _PROFILE_KINDS:
        rules = by_kind.get(kind, [])
        driven = sum(
            1
            for rule in rules
            if rule.properties.get("IMPEDANCEPROFILEDRIVEN", "").strip().upper()
            == "TRUE"
        )
        profile.append(f"{kind} {driven} of {len(rules)}")
    return [
        "- Rules per kind: " + ", ".join(counts),
        "- Impedance-profile-driven rules: " + ", ".join(profile),
    ]


def _impedance_line(index) -> str:
    try:
        from services.impedance import snapshot_line
    except ImportError:
        from server.services.impedance import snapshot_line
    return snapshot_line(index)


def _stale_order(rule) -> tuple[bool, int]:
    """Enabled rules of the review kinds first; file order otherwise."""
    rank = (
        REVIEW_RULE_KINDS.index(rule.kind)
        if rule.kind in REVIEW_RULE_KINDS
        else len(REVIEW_RULE_KINDS)
    )
    return (not rule.enabled, rank)


def _stale_line(index) -> str:
    ctx = index.rule_context
    stale_rules = []
    for rule in sorted(index.pcb.rules, key=_stale_order):
        if rule.kind not in _STALE_KINDS:
            continue
        refs = stale_references(rule.scope1, ctx)
        if rule.kind in BINARY_KINDS:
            refs += stale_references(rule.scope2, ctx)
        refs = list(dict.fromkeys(refs))
        if not refs:
            continue
        shown = _capped([_short(ref) for ref in refs], _MAX_REFS_PER_RULE)
        state = "" if rule.enabled else ", disabled"
        stale_rules.append(f"{_short(rule.name)} ({rule.kind}{state}): {shown}")
    if not stale_rules:
        return "- Routing rules with stale scope references: none"
    return (
        f"- Routing rules with stale scope references ({len(stale_rules)}; "
        "these terms match nothing on the board): "
        + _capped(stale_rules, SNAPSHOT_MAX_STALE_RULES, "; ")
    )


def _drc_line(index, by_kind: dict[str, list]) -> str:
    batch = getattr(index.pcb, "drc_rule_kinds", None)
    if batch is None:
        return "- Batch DRC rule set: not saved in the PcbDoc (unknown)"
    unchecked = [
        kind
        for kind in REVIEW_RULE_KINDS
        if by_kind.get(kind)
        and not {rule.kind_id for rule in by_kind[kind]} & batch
    ]
    if not unchecked:
        return "- Batch DRC rule set: includes every kind above that has rules"
    return (
        "- Batch DRC rule set leaves out: "
        + ", ".join(unchecked)
        + " (the designer's batch DRC never reports these kinds)"
    )


def constraints_snapshot(index) -> str:
    """Bounded facts about the board's constraints, one Markdown bullet per line.

    Lists the copper layers in stack order (routable signal layers and
    internal planes), pair and pair-class counts (first
    SNAPSHOT_MAX_CLASSES classes), net/xSignal class counts, rule counts per
    review kind (with disabled and profile-driven counts), rules with stale
    scope references (first SNAPSHOT_MAX_STALE_RULES) and review kinds left
    out of the saved batch DRC rule set. Names no interfaces.
    """
    by_kind = _rules_by_kind(index)
    lines = [
        _layers_line(index),
        *_pair_lines(index),
        _class_line(index),
        *_rule_count_lines(by_kind),
        _impedance_line(index),
        _stale_line(index),
        _drc_line(index, by_kind),
    ]
    return "\n".join(lines)


# ---------- prompt ----------

# Every tool the prompt names; tests check each appears in the prompt and is
# registered on the server.
CONSTRAINTS_REVIEW_TOOLS = (
    "get_board_info",
    "list_diff_pairs",
    "get_classes",
    "get_design_rules",
    "get_rules_for_net",
    "check_rule_compliance",
    "check_lengths",
    "check_diff_pairs",
    "check_diff_pair_geometry",
    "get_drc_report",
    "get_net_neighbors",
    "search_components",
    "get_component",
    "query_net",
    "get_net_pcb",
    "get_signal_length",
    "set_project_dir",
    "set_active_variant",
    "schematic_review",
    "run_review_checklist",
)

CONSTRAINTS_REVIEW_PROMPT = """\
Project: {name}
PCB: {pcb_doc}
Active variant: {active_variant}

Board snapshot (facts from the saved PcbDoc; no interface has been identified yet):
{snapshot}

You are starting a structured review of this board's high-speed constraints: is each
interface's requirement encoded as a design rule, and does the saved copper meet it?
Ask the user to choose a scope before doing anything:
  - The whole board (every high-speed interface you find)
  - One interface or bus (e.g. "USB", "DDR4", "PCIe", "Ethernet")
  - One pair class or net class from the snapshot

For large boards, consider reviewing each interface as an independent task dispatched in
parallel, one agent per interface. Each agent can self-initialize:
set_project_dir("{root_dir}") → set_active_variant("{active_variant}") → run Phases 2–3
for its interface with the targets confirmed in Phase 1, and return its table rows.
Merge all results into one Phase 4 report.

Do not proceed until the user answers.

---

## Ground Rules

- The requirement is the datasheet, hardware design guide or layout guide of the actual
  controller, PHY, memory or connector. The typical values at the end of this prompt are a
  fallback only: mark a target taken from them "typical — confirm". A gap against a typical
  value is a warning, never a critical issue on its own.
- Every finding cites evidence: a rule (name: value), a measured fact from a tool, or a
  datasheet value with its source. What you cannot evidence goes in the Warnings &
  Unverifiable table.
- Rules are read from the saved PcbDoc. Altium's own DRC result is get_drc_report: quote it
  only with its assessment (a stopped run, unchecked kinds, a report older than the board).
  Never say the board passes DRC or meets its constraints; say which rules and measurements
  you checked.
- Lengths follow Altium's Signal Length. check_lengths measures the copper path pad to pad
  plus the via barrel over the stackup span it traverses plus the pin/package length (pad to
  die) stored on the end pads; an xSignal runs through series parts and adds the pin/package
  length at its end pins. Nets without exactly 2 pads use routed copper plus via barrels and
  no pin/package length, like Altium's Routed Length. A pair's group length is the P/N
  average. Unrouted legs are reported as incomplete. list_diff_pairs and get_signal_length
  use the same measure. Pads without a pin/package length add nothing, so a die-to-die budget
  needs the package length from the datasheet. A result marked marginal is inconclusive, not
  a violation.
- Impedance comes only from the Layer Stack Manager: get_board_info lists each impedance
  profile with its solved width/gap and calculated impedance per layer and the rules using
  it. Without a profile the impedance cannot be confirmed from the files. Never present a
  closed-form impedance estimate as evidence.
- Work in the active variant ({active_variant}): a DNP AC-coupling capacitor, termination or
  series resistor on an interface net is a finding.
- Net names are hints, not evidence. Identify interfaces from parts and pin names.
- Quote lengths in the units the tools return. PCB data is the last saved PcbDoc: if the user
  changes rules or routing, ask them to save (Ctrl+S) before you re-check.

---

### Phase 1 — Identify the Interfaces and Their Targets

1. Parts — search_components for interface ICs and connectors (by description, then by mpn),
   e.g. 'USB|PHY|Ethernet|RJ45|magnetic|DDR|SDRAM|SODIMM|PCIe|M.2|HDMI|DisplayPort|SATA|LVDS|
   MIPI|CSI|DSI|CAN|RS-?485|SerDes|SFP|redriver|retimer'. Read pin names with get_component:
   pins are the evidence, net names only hints (a net named like a USB 3 lane can be a FIFO
   strobe of a USB bridge; DisplayPort lanes can look like USB).
2. Nets — list_diff_pairs; get_classes(kind="diff_pair"), get_classes(kind="net") and
   get_classes(kind="xsignal"); query_net for interface nets that are not in a pair. Follow
   series parts (AC-coupling caps, common-mode chokes, resistors) with get_component: each
   splits one signal into several nets.
3. Per interface record: standard and rate (from the datasheet), both endpoints
   (refdes:pin), series parts with their DNP state, nets and pairs, and their classes.
4. Targets — find the actual part's layout guide (web search for its datasheet, hardware
   design guide or layout application note) and record: differential and single-ended
   impedance with tolerance, in-pair skew, pair-to-pair or group skew, maximum length,
   maximum vias, spacing, reference-plane rules, AC-coupling value and placement. Convert
   time budgets (ps) to length with the layer's propagation delay and state the assumption.
   No guide found → use the typical table below and mark the target "typical — confirm".

Present a table: | Interface | Endpoints | Nets / Pairs | Targets (source) |
Ask about anything you could not determine.

5. Checklist — run_review_checklist(checklist="pcb", sections=["high_speed"]) once: show
   its coverage table. Its auto items (rule compliance, lengths, pairs, the DRC report)
   are answered from the tools; its needs-judgment items are what Phases 2-3 decide.

Done when: the user has confirmed or corrected the interface list and the targets.
Do not proceed to Phase 2 until then.

---

### Phase 2 — Constraint Audit (do the rules encode the targets?)

1. Pairs — every P/N signal of an interface is a defined pair, its positive net on the P/+
   pin. check_diff_pairs(pattern=...) reports undeclared halves, polarity at pins and
   asymmetric series parts; schematic Differential Pair directives are not compared with the
   PCB yet (see not_checked). Confirm each finding with get_component and the datasheet
   before calling it a defect: many receivers tolerate swapped polarity.
2. Rules — get_rules_for_net on a member net of each pair or group, and on the pair name:
   DiffPairsRouting, Width, Clearance, RoutingVias, RoutingLayers, MatchedLengths, Length
   (on an xSignal name it returns the xSignal's Length and MatchedLengths rules).
   Flag nets that only get the board default (scope All), classes whose rules conflict (say
   which wins by priority), and class names that state another impedance than the target
   (e.g. a USB 2.0 pair in an 85 Ω class). get_design_rules(kind=...) shows every rule of a
   kind with its values.
3. Impedance — a profile-driven rule names its impedance_profile: compare the profile's
   target and tolerance with the requirement, check its calculated impedance per layer
   (get_board_info impedance_profiles, within_tolerance) and its issues (rule widths that
   drifted from the profile, unused profiles). A fixed-width rule: quote layers_mil per layer
   with the stackup, say the impedance cannot be confirmed from the files, and ask for the
   Layer Stack Manager result or the fabricator's impedance report.
4. Lengths — every MatchedLengths rule whose scope matches applies (priority is ignored).
   Check membership (exactly the interface's nets; xSignals carry a signal through series
   parts), tolerance against the budget, and mode (within-pair or group). A tolerance far
   beyond any budget (e.g. 1000 mil) constrains nothing. Length rules: min/max against the
   maximum length. A rule that applies only conditionally does not prove coverage.
5. Spacing — Clearance candidates against the required spacing to other signals and to
   clocks. A board-default clearance means the spacing intent is not encoded.
6. Vias and layers — RoutingVias sizes and via-count limits; RoutingLayers: every allowed
   layer has an adjacent reference plane in the stackup. Is there a ReturnPath rule for the
   interface? None means no rule checks reference continuity.
7. Hygiene — stale scope references, disabled rules, and kinds the board's batch DRC leaves
   out (see the snapshot) that touch the interface.

Done when: every interface in scope has been checked on every item.
Do not proceed to Phase 3 until then.

---

### Phase 3 — Copper Spot Checks (does the saved layout meet them?)

1. check_rule_compliance(net=...) per interface net or pair: widths per layer against the
   rule applying to each object, via sizes, layers used against RoutingLayers.
2. check_lengths(rule=...) per MatchedLengths or Length rule touching the interface, or
   check_lengths() for every rule: lengths, spread against tolerance, in-pair skew,
   incomplete legs, worst offenders. For nets no length rule covers, compare list_diff_pairs
   skew or get_net_pcb lengths with the Phase 1 budget, and say they are approximate.
3. check_diff_pairs(pattern=...) per interface: in-pair skew and AC-coupling symmetry.
   get_signal_length(<pair>) gives the via count per leg (positive.vias / negative.vias) to
   compare with the via targets.
   check_diff_pair_geometry(pair=...) per interface: the coupled gap and widths per layer
   against the DiffPairsRouting rule, each leg's uncoupled length against Max Uncoupled
   Length (pad and via breakouts apart), and layer changes one leg makes without the other.
4. get_net_neighbors(net, distance=<required spacing>) on critical nets: intruders closer
   than required (the pair partner is expected) and broadside nets on adjacent layers. The
   reference should be ground; flag a power-only or signal-only reference and layer changes
   onto another reference. get_net_pcb shows one net's layers, vias and pads. Plane voids and
   splits cannot be verified from saved polygon outlines: say so.

Done when: every interface has measured results or a stated reason why not.
Do not proceed to Phase 4 until then.

---

### Phase 4 — Document

Present the findings as three Markdown tables:

  Table 1 — Interface Constraint Matrix
  | Interface | Nets / Pairs | Target (source) | Rule (name: value) | Copper (measured) | Result |

  Table 2 — Critical Issues
  | Interface | Net / Pair / Rule | Issue | Requirement (source) | Board Shows |

  Table 3 — Warnings & Unverifiable
  | Item | Observation | What would confirm it |

Result is one of: met, violated, not encoded (no rule), conditional, unverifiable.

Finish with the checklist: run_review_checklist(checklist="pcb", sections=["high_speed"],
format="markdown") with decisions= giving your Yes, No or N/A and a one-line reason for
each needs-judgment item you settled. Show its 'Checklist coverage' table and the review
record (it names the design state it belongs to); save the record only where the user
agrees.

Ask: "Any interface to dig deeper into, or should I review its circuit with
schematic_review?"

---

## Typical Targets (fallback only — confirm against the part's own guide)

Sources and devices disagree, sometimes by an order of magnitude (USB 2.0 in-pair skew;
PCIe at 85, 95 or 100 Ω). Use a row only when the part's own guide gives nothing, and say so.

{interface_targets}
"""


def build_constraints_review(project: dict, variant_name: str, index) -> str:
    """The constraints-review prompt for a loaded project and its PCB index."""
    paths = project.get("pcb_doc_paths") or []
    pcb_doc = PureWindowsPath(paths[0]).name if paths else "(none)"
    if len(paths) > 1:
        pcb_doc += (
            f" (the project lists {len(paths)} PcbDocs; only this one is reviewed)"
        )
    return CONSTRAINTS_REVIEW_PROMPT.format(
        name=project.get("name", "?"),
        pcb_doc=pcb_doc,
        active_variant=variant_name,
        root_dir=project.get("root_dir", ""),
        snapshot=constraints_snapshot(index),
        interface_targets=render_interface_targets(),
    )
