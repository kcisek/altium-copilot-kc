"""Test access for check_testpoints over a PcbIndex: which nets an in-circuit test (ICT)
fixture or a flying probe can reach, from which side, and what stands in the way.

Pure logic, no MCP, from the PcbDoc alone (no netlist needed). Lengths are mils under
*_mil keys, so callers can convert units; issue texts are written in the units asked for;
example locations (at_mil) are relative to the board origin (origin_mil).

Probe sites: pads and vias with Altium's Fabrication Testpoint property on a side
(Primitive.test_fab_top / test_fab_bottom), and every pad of a TP part (a designator TP
not followed by a letter, or a footprint named testpoint or test point), each on the board
faces its copper is on (a through-hole pad and a through via on both). A mark on a face
without copper (a top pad marked bottom, a via whose pad is removed there) is no site.
Altium's Assembly Testpoint properties are not decoded, so assembly test access is read
from the same sites. A site probed on one face is a probe point; sites off the board are
left out.

Nets: those with at least two pads. A net has access on a face when it has a probe point
there; coverage is the share of nets with access, per face. The probe side is the
profile's (ict: auto, the side reaching more nets, then the side with more probe points,
else the bottom; flying_probe: both). Connections: a net's pads other than TP parts'.
Rails: power-named nets (check_decoupling's name rules: a name with a signal token such as
EN, PG or SENSE is a signal) and nets of at least rail_min_connections connections, ground
left out; ground: ground-named nets. Rails and ground count the distinct sites on the
probe side(s).

Usage rules (Fabrication and Assembly Testpoint Usage) are resolved per net by priority
like Altium (a rule over All decides at once). Nets a rule requires without access are
grouped per rule: a warning when the rule's scope is not All or the batch DRC checks its
kind, else info (Altium creates a Required rule over All, which its batch DRC leaves
unchecked by default). The Testpoint style rule (sizes, holes, sides, under component)
applies to the marked sites; its grid is not checked.

Geometry: pad size, spacing, edge clearance, bodies and tall parts are judged on the probe
side's points only (a single-sided fixture never lands on the other face); solder mask
and the Testpoint rule on every marked face. The board edge (outline and cutouts) is
dfm_geometry's; component bodies and heights are dfm_assembly's (a part's own body never
blocks its own pad, a TP part is no tall part, and Not Fitted parts are left out when
is_dnp is given); tooling holes are non-plated holes of at least tooling_hole_min that
belong to no part or to a part of holes only (a mounting hole).

Profiles hold guidance values, not a standard or an Altium rule (GUIDANCE says where they
come from); the test house's numbers win, passed in limits.
"""

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass

try:
    from parsers.pcb_doc import MULTI_LAYER, pad_copper_on
    from services import dfm_assembly as assembly
    from services import dfm_geometry as geometry
    from services.decoupling import net_kind
    from services.fab_profiles import MM
    from services.pcb_geometry import point_in_polygon, point_polygon_distance
    from services.pcb_rules import (
        TESTPOINT_STYLE_KINDS,
        TESTPOINT_USAGE_KINDS,
        ObjectResolver,
        RuleTarget,
        evaluate_scope,
        rule_header,
        rule_values,
    )
    from services.via_checks import _selection
except ImportError:
    from server.parsers.pcb_doc import MULTI_LAYER, pad_copper_on
    from server.services import dfm_assembly as assembly
    from server.services import dfm_geometry as geometry
    from server.services.decoupling import net_kind
    from server.services.fab_profiles import MM
    from server.services.pcb_geometry import point_in_polygon, point_polygon_distance
    from server.services.pcb_rules import (
        TESTPOINT_STYLE_KINDS,
        TESTPOINT_USAGE_KINDS,
        ObjectResolver,
        RuleTarget,
        evaluate_scope,
        rule_header,
        rule_values,
    )
    from server.services.via_checks import _selection

CHECKS = (
    "coverage",
    "required_nets",
    "probe_side",
    "rails",
    "ground",
    "pad_size",
    "spacing",
    "edge_clearance",
    "under_component",
    "tall_parts",
    "solder_mask",
    "style_rule",
    "tooling_holes",
)
PROBE_SIDES = ("auto", "top", "bottom", "both")
DEFAULT_PROFILE = "ict"

# Lengths in mil, counts plain; None: not judged.
PROFILES: dict[str, dict] = {
    "ict": {
        "probe_side": "auto",
        "pad_preferred_mil": 1.0 * MM,
        "pad_min_mil": 0.9 * MM,
        "spacing_preferred_mil": 2.5 * MM,
        "spacing_min_mil": 1.8 * MM,
        "edge_clearance_mil": 3.2 * MM,
        "tall_part_height_mil": 5.0 * MM,
        "tall_part_clearance_mil": 5.0 * MM,
        "rail_min_connections": 25,
        "rail_min_sites": 2,
        "rail_max_sites": 10,
        "ground_connections_per_site": 20,
        "tooling_holes_min": 2,
        "tooling_hole_min_mil": 2.0 * MM,
    },
    "flying_probe": {
        "probe_side": "both",
        "pad_preferred_mil": 0.6 * MM,
        "pad_min_mil": 0.3 * MM,
        "spacing_preferred_mil": 1.27 * MM,
        "spacing_min_mil": 0.5 * MM,
        "edge_clearance_mil": 3.0 * MM,
        "tall_part_height_mil": 5.0 * MM,
        "tall_part_clearance_mil": 3.0 * MM,
        "rail_min_connections": 25,
        "rail_min_sites": 1,
        "rail_max_sites": None,
        "ground_connections_per_site": None,
        "tooling_holes_min": 0,
        "tooling_hole_min_mil": 2.0 * MM,
    },
}
DESCRIPTIONS = {
    "ict": "In-circuit test on a bed-of-nails fixture: every net probed at once from one "
           "side (the probe side) at a fixed probe pitch; tooling holes locate the board.",
    "flying_probe": "Flying-probe test: a few probes land one site at a time from both "
                    "sides, so smaller and closer sites are fine; the board is clamped "
                    "at its edges.",
}
GUIDANCE = (
    "Profile values are guidance, not a standard or an Altium rule. ict: common "
    "bed-of-nails design-for-test rules of thumb (1.0 mm targets, 0.9 mm acceptable; 2.5 mm "
    "probe pitch, 1.8 mm with smaller probes; 3.2 mm (0.125 in) from the board edge; 2 to 10 "
    "probes per rail and about one ground probe per 20 connections; two non-plated tooling "
    "holes; a keep-out around parts taller than 5 mm on the probe side). flying_probe: "
    "looser numbers for probes that land one at a time. The test house's numbers win: pass "
    "them in limits."
)
_LENGTH_KEYS = tuple(key[:-4] for key in PROFILES["ict"] if key.endswith("_mil"))
_COUNT_KEYS = tuple(
    key for key in PROFILES["ict"] if not key.endswith("_mil") and key != "probe_side"
)
_PROFILE_ALIASES = {"flying": "flying_probe", "flyingprobe": "flying_probe",
                    "bed_of_nails": "ict", "in_circuit": "ict"}

SAMPLE = 5  # examples per grouped finding
NET_SAMPLE = 20  # nets listed per grouped finding
TOOLING_LISTED = 10
EPS_MIL = 0.001
_TOP, _BOTTOM = 1, 32
TP_DESIGNATOR = re.compile(r"^TP(?![A-Za-z])", re.IGNORECASE)
TP_FOOTPRINT = re.compile(r"test[ _-]?points?", re.IGNORECASE)
_SEVERITY_ORDER = {"warning": 0, "info": 1}
_CELL_MIL = 200.0
_FABRICATION_USAGE = ("FabricationTestPointUsage", "TestPointUsage")
_FABRICATION_STYLE = ("FabricationTestpoint", "Testpoint")


def _plural(count: int, word: str) -> str:
    return f"{count} {word if count == 1 else word + 's'}"


def _supply_kind(name: str) -> str | None:
    """ground, power or None by check_decoupling's name rules: a supply name with a signal
    token (3V3_EN, PWR_PG, VBUS_DET, GND_SENSE) is a signal, as there."""
    kind = net_kind(name)
    if kind["signal"]:
        return None
    return "ground" if kind["ground"] else "power" if kind["power"] else None


def _round(value: float | None, digits: int = 3) -> float | None:
    return None if value is None else round(value, digits)


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _limits_error(message: str) -> dict:
    return {
        "error": "invalid_limits",
        "message": message,
        "valid_keys": [f"{key}_mm" for key in _LENGTH_KEYS] + list(_COUNT_KEYS),
    }


def build_profile(name: str | None = None, limits=None,
                  probe_side: str | None = None) -> tuple[dict | None, dict | None]:
    """(profile, None) or (None, error): a built-in profile with limits applied on top.
    Lengths in limits end in _mm or _mil, counts take no suffix; null removes a value."""
    key = (name or DEFAULT_PROFILE).strip().lower().replace("-", "_").replace(" ", "_")
    key = _PROFILE_ALIASES.get(key, key)
    if key not in PROFILES:
        return None, {
            "error": "invalid_profile",
            "message": f"Unknown testpoint profile '{name}'.",
            "valid_profiles": list(PROFILES),
        }
    values = dict(PROFILES[key])
    if probe_side is not None:
        side = probe_side.strip().lower()
        if side not in PROBE_SIDES:
            return None, {
                "error": "invalid_probe_side",
                "message": f"probe_side must be one of {', '.join(PROBE_SIDES)}.",
            }
        values["probe_side"] = side
    if limits is not None and not isinstance(limits, dict):
        return None, _limits_error("limits must be an object of name: number.")
    changed = []
    for raw_key, raw in (limits or {}).items():
        text = str(raw_key).strip().lower()
        base, _, unit = text.rpartition("_")
        if unit in ("mm", "mil") and base in _LENGTH_KEYS:
            target, scale, whole = f"{base}_mil", (MM if unit == "mm" else 1.0), False
        elif text in _COUNT_KEYS:
            target, scale, whole = text, 1.0, True
        else:
            return None, _limits_error(
                f"Unknown limit '{raw_key}': lengths end in _mm or _mil, counts take no "
                "suffix."
            )
        if raw is None:
            values[target] = None
            changed.append(target)
            continue
        number = _number(raw)
        if number is None or number < 0 or (whole and number != int(number)):
            kind = "a whole number" if whole else "a number"
            return None, _limits_error(f"Limit '{raw_key}' must be {kind}, 0 or more.")
        values[target] = int(number) if whole else number * scale
        changed.append(target)
    for low, high in (("pad_min_mil", "pad_preferred_mil"),
                      ("spacing_min_mil", "spacing_preferred_mil"),
                      ("rail_min_sites", "rail_max_sites")):
        if values.get(low) is not None and values.get(high) is not None and \
                values[low] > values[high] + EPS_MIL:
            return None, _limits_error(f"{low} is larger than {high}.")
    profile = {"name": key, "chosen_by": "argument" if name else "default",
               "description": DESCRIPTIONS[key], **values,
               "probe_side_chosen_by": "argument" if probe_side is not None else "profile"}
    if changed:
        profile["changed_by_limits"] = sorted(set(changed))
    return profile, None


@dataclass
class Probe:
    """A probe site on one board face (a probe point)."""

    row: int  # prims row id (pcb.primitives[row - 1])
    kind: str  # pad | via
    net: int | None
    component: int | None
    x: float
    y: float
    side: str  # top | bottom
    size_mil: float  # smallest copper dimension on that face
    hole_mil: float
    marked: bool  # Altium's Fabrication Testpoint property on this face
    tp_part: bool
    tented: bool | None  # vias: the solder mask covers it on this face


def tp_components(index) -> set[int]:
    """Components that are testpoints: designator TP1, TP_3V3, ..., or a testpoint
    footprint."""
    found = set()
    for number, component in enumerate(index.pcb.components):
        designator = index.designator(number) or ""
        if TP_DESIGNATOR.match(designator) or TP_FOOTPRINT.search(component.footprint or ""):
            found.add(number)
    return found


def marked_testpoint_nets(index) -> set[str]:
    """Net names with a probe site on the board: a pad or via marked as a Fabrication
    Testpoint on either side, or a pad of a TP part (tp_components)."""
    parts = tp_components(index)
    nets = index.pcb.nets
    found = set()
    for prim in index.pcb.primitives:
        if prim.kind not in ("pad", "via") or prim.net is None or not 0 <= prim.net < len(nets):
            continue
        if prim.test_fab_top or prim.test_fab_bottom or (
                prim.kind == "pad" and prim.component in parts):
            found.add(nets[prim.net])
    return found


def _faces(prim) -> dict[str, float]:
    """side -> the smallest copper dimension (mil) of a pad or via on the board faces."""
    faces: dict[str, float] = {}
    if prim.kind == "via":
        for side, layer in (("top", _TOP), ("bottom", _BOTTOM)):
            if layer not in (prim.layer, prim.layer_to):
                continue
            stack = getattr(prim, "via", None)
            size = stack.pad_diameter(layer, prim.width) if stack is not None else prim.width
            if size:
                faces[side] = size
        return faces
    for side, layer in (("top", _TOP), ("bottom", _BOTTOM)):
        if prim.layer not in (layer, MULTI_LAYER):
            continue
        copper = pad_copper_on(prim.layer_copper, layer) if prim.layer == MULTI_LAYER else None
        width, height = (
            (copper.width, copper.height) if copper else (prim.width, prim.height or prim.width)
        )
        size = min(width, height)
        if size > 0 and size > (prim.hole or 0.0) + EPS_MIL:
            faces[side] = size
    return faces


class _Review:
    def __init__(self, index, profile: dict, checks: set, critical: set[str] | None,
                 units: str, is_dnp=None):
        self.index = index
        self.pcb = index.pcb
        self.profile = profile
        self.checks = checks
        self.units = units
        self.is_dnp = is_dnp
        self.origin = (self.pcb.board.origin_x or 0.0, self.pcb.board.origin_y or 0.0)
        self.findings: list[dict] = []
        self.notes: list[str] = []
        self.nets = self.pcb.nets
        self.tp = tp_components(index)
        self.edge = geometry.copper_model(index).edge
        self.off_board = 0
        self.marks_without_copper = 0
        self.probes = self._collect()
        self._count_nets()
        self.critical = {
            number for number, name in enumerate(self.nets)
            if critical is not None and name.lower() in critical
        }
        self.probe_side = self._probe_side()
        self.ctx = index.rule_context
        self.batch = getattr(self.pcb, "drc_rule_kinds", None)
        self.style_rules = self._enabled(_FABRICATION_STYLE)
        self._resolvers: dict = {}
        self._values: dict[int, dict] = {}

    def values(self, rule) -> dict:
        key = id(rule)
        if key not in self._values:
            self._values[key] = rule_values(rule, {})
        return self._values[key]

    # --- formatting ---

    def _len(self, mil: float) -> str:
        if self.units == "mm":
            return f"{mil * 0.0254:.3f} mm"
        return f"{mil:.2f} mil"

    def _at(self, x: float, y: float) -> list[float]:
        return [round(x - self.origin[0], 3), round(y - self.origin[1], 3)]

    def net_name(self, net: int | None) -> str | None:
        return self.nets[net] if net is not None and 0 <= net < len(self.nets) else None

    def _designator(self, component: int | None) -> str | None:
        if component is None or not 0 <= component < len(self.pcb.components):
            return None
        return self.index.designator(component)

    def example(self, probe: Probe, **extra) -> dict:
        record: dict = {"at_mil": self._at(probe.x, probe.y), "side": probe.side,
                        "kind": probe.kind}
        net = self.net_name(probe.net)
        if net:
            record["net"] = net
        designator = self._designator(probe.component) if probe.kind == "pad" else None
        if designator:
            pad = self.pcb.primitives[probe.row - 1].pad_name
            record["pad"] = f"{designator}-{pad}" if pad else designator
        record["size_mil"] = _round(probe.size_mil)
        record["source"] = (
            "testpoint_and_tp_part" if probe.marked and probe.tp_part
            else "testpoint" if probe.marked else "tp_part"
        )
        record.update({key: value for key, value in extra.items() if value is not None})
        return record

    def finding(self, check: str, severity: str, issue: str, **fields) -> None:
        if check not in self.checks:
            return
        record = {"check": check, "severity": severity}
        record.update({key: value for key, value in fields.items() if value is not None})
        record["issue"] = issue
        self.findings.append(record)

    # --- collection ---

    def _collect(self) -> list[Probe]:
        probes = []
        on_board = self.edge.on_board if self.edge.has_outline else None
        for row, prim in enumerate(self.pcb.primitives, start=1):
            if prim.kind not in ("pad", "via"):
                continue
            tp_part = prim.kind == "pad" and prim.component in self.tp
            marks = {"top": bool(getattr(prim, "test_fab_top", False)),
                     "bottom": bool(getattr(prim, "test_fab_bottom", False))}
            if not tp_part and not any(marks.values()):
                continue
            faces = _faces(prim)
            self.marks_without_copper += sum(
                1 for side, marked in marks.items() if marked and side not in faces
            )
            if on_board is not None and not on_board(prim.x1, prim.y1):
                self.off_board += 1
                continue
            stack = getattr(prim, "via", None)
            for side, size in faces.items():
                if not (tp_part or marks[side]):
                    continue
                tented = None
                if prim.kind == "via" and stack is not None:
                    tented = stack.tent_top if side == "top" else stack.tent_bottom
                probes.append(Probe(
                    row=row, kind=prim.kind, net=prim.net, component=prim.component,
                    x=prim.x1, y=prim.y1, side=side, size_mil=size,
                    hole_mil=prim.hole or 0.0, marked=marks[side], tp_part=tp_part,
                    tented=tented,
                ))
        return probes

    def _count_nets(self) -> None:
        self.pads = Counter()
        self.connections = Counter()
        for prim in self.pcb.primitives:
            if prim.kind != "pad" or prim.net is None:
                continue
            self.pads[prim.net] += 1
            if prim.component not in self.tp:
                self.connections[prim.net] += 1
        self.considered = sorted(net for net, count in self.pads.items() if count >= 2)
        self.access: dict[int, set[str]] = defaultdict(set)
        self.net_sites: dict[tuple[int, str], set[int]] = defaultdict(set)
        for probe in self.probes:
            if probe.net is not None:
                self.access[probe.net].add(probe.side)
                self.net_sites[(probe.net, probe.side)].add(probe.row)

    def _probe_side(self) -> tuple[str, str]:
        """(side, chosen_by): top, bottom or both. auto: the side reaching more nets, then
        the side with more probe points, else the bottom."""
        side = self.profile.get("probe_side") or "auto"
        if side != "auto":
            return side, self.profile.get("probe_side_chosen_by", "profile")

        def reach(face: str) -> tuple[int, int]:
            nets = sum(1 for net in self.considered if face in self.access.get(net, ()))
            return nets, sum(1 for probe in self.probes if probe.side == face)

        return ("top" if reach("top") > reach("bottom") else "bottom"), "auto"

    def probe_sides(self) -> tuple[str, ...]:
        side = self.probe_side[0]
        return ("top", "bottom") if side == "both" else (side,)

    def probed(self) -> list[Probe]:
        """The probe points on the probe side(s): a single-sided fixture never lands on
        the other face, so the fixture geometry checks leave it out."""
        sides = self.probe_sides()
        return [probe for probe in self.probes if probe.side in sides]

    def site_count(self, net: int) -> int:
        rows: set[int] = set()
        for side in self.probe_sides():
            rows |= self.net_sites.get((net, side), set())
        return len(rows)

    # --- rules ---

    def _enabled(self, kinds) -> list:
        return sorted((rule for rule in self.pcb.rules if rule.kind in kinds and rule.enabled),
                      key=lambda rule: rule.priority)

    def batch_checked(self, rule) -> bool | None:
        if self.batch is None:
            return None
        return rule.kind_id in self.batch

    def _target(self, net: int | None) -> RuleTarget:
        name = self.net_name(net)
        if name is None:
            return RuleTarget(kind="net", name="", nets=set(), pairs=set())
        return self.index.rule_target(name, kind="net") or RuleTarget(
            kind="net", name=name, nets={name.lower()}, pairs=set())

    def usage_rule(self, rules: list, net: int) -> tuple[object | None, bool]:
        """(the usage rule applying to a net, undecided): scope All decides at once."""
        target = None
        undecided = False
        for rule in rules:
            if rule.scope1.strip().lower() == "all":
                return rule, undecided
            if target is None:
                target = self._target(net)
            matches = evaluate_scope(rule.scope1, target, self.ctx)["matches"]
            if matches == "yes":
                return rule, undecided
            if matches != "no":
                undecided = True
        return None, undecided

    def style_rule(self, probe: Probe):
        rules = self.style_rules
        if not rules:
            return None
        if rules[0].scope1.strip().lower() == "all":
            return rules[0]
        key = probe.net
        if key not in self._resolvers:
            self._resolvers[key] = ObjectResolver(rules, self._target(probe.net), self.ctx)
        row = self.index.db.execute("SELECT * FROM prims WHERE id = ?", (probe.row,)).fetchone()
        if row is None:
            return None
        rule, _ = self._resolvers[key].resolve(self.index.object_facts(row))
        return rule

    def rule_report(self) -> list[dict]:
        signal = self.index.signal_layers()
        rows = []
        kinds = (*TESTPOINT_USAGE_KINDS, *TESTPOINT_STYLE_KINDS)
        for rule in sorted((rule for rule in self.pcb.rules if rule.kind in kinds),
                           key=lambda rule: (kinds.index(rule.kind), rule.priority)):
            entry = rule_header(rule)
            entry["values"] = rule_values(rule, signal)
            checked = self.batch_checked(rule)
            if checked is not None:
                entry["checked_by_batch_drc"] = checked
            rows.append(entry)
        return rows

    # --- checks ---

    def check_coverage(self) -> None:
        if self.probes:
            return
        self.finding(
            "coverage", "warning",
            "No probe sites: no pad or via carries a Fabrication Testpoint property (Assembly "
            "Testpoint marks are not read) and there are no TP parts, so no net has test "
            "access. Mark testpoints in Altium (Testpoint Manager, or a pad's or via's "
            "Testpoint properties) or add TP parts.",
            nets=len(self.considered),
        )

    def check_required(self) -> list[dict]:
        """Usage-rule and critical-net findings; returns the per-kind usage summary."""
        summary = []
        groups: dict[tuple, dict] = {}
        marked_nets = {probe.net for probe in self.probes if probe.marked}
        for label, kinds in (("fabrication", _FABRICATION_USAGE),
                             ("assembly", ("AssemblyTestPointUsage",))):
            rules = self._enabled(kinds)
            if not rules:
                continue
            counts = Counter()
            for net in self.considered:
                rule, undecided = self.usage_rule(rules, net)
                if undecided and rule is None:
                    counts["undecided"] += 1
                if rule is None:
                    continue
                usage = self.values(rule).get("usage")
                counts[usage] += 1
                if usage == "required" and not self.access.get(net):
                    counts["required_without_access"] += 1
                    key = ("missing", rule.name, rule.kind)
                    groups.setdefault(key, {"rule": rule, "nets": []})["nets"].append(net)
                elif usage == "prohibited" and label == "fabrication" and net in marked_nets:
                    counts["prohibited_with_testpoint"] += 1
                    key = ("prohibited", rule.name, rule.kind)
                    groups.setdefault(key, {"rule": rule, "nets": []})["nets"].append(net)
            summary.append({"usage": label, "rules": [rule.name for rule in rules],
                            **{key: counts[key] for key in sorted(counts)}})
        merged: dict[tuple, dict] = {}
        for (problem, _, _), group in groups.items():
            rule = group["rule"]
            severity = "warning" if (
                problem == "prohibited" or rule.scope1.strip().lower() != "all"
                or self.batch_checked(rule)
            ) else "info"
            key = (problem, severity, tuple(group["nets"]))
            entry = merged.setdefault(key, {"rules": [], "kinds": []})
            entry["rules"].append(rule.name)
            entry["kinds"].append(rule.kind)
        for (problem, severity, nets), entry in merged.items():
            names = sorted(self.net_name(net) or "" for net in nets)
            rules = " and ".join(f"'{name}'" for name in entry["rules"])
            plural = "" if len(entry["rules"]) > 1 else "s"
            if problem == "prohibited":
                issue = (f"{rules} prohibit{plural} testpoints on these nets, but they carry one: "
                         "remove the Testpoint property or change the rule.")
            elif severity == "info":
                unchecked = (", not checked by the board's batch DRC"
                             if self.batch is not None else "")
                issue = (f"{rules} require{plural} a testpoint on every net (scope All, like "
                         f"Altium's default rule{unchecked}); these nets have no probe site. "
                         "Narrow the rule to the nets that need test access, or add sites.")
            else:
                issue = (f"{rules} require{plural} a testpoint on these nets, and they have no "
                         "probe site.")
            self.finding("required_nets", severity, issue, rules=entry["rules"],
                         rule_kinds=entry["kinds"], count=len(names), nets=names[:NET_SAMPLE])
        sides = self.probe_sides()
        for net in sorted(self.critical, key=lambda net: self.net_name(net) or ""):
            reached = self.access.get(net, set())
            if not reached:
                self.finding("required_nets", "warning",
                             "A net asked for (nets) has no probe site.",
                             net=self.net_name(net))
            elif not reached & set(sides):
                self.finding("required_nets", "warning",
                             f"A net asked for (nets) is reachable only from the "
                             f"{'/'.join(sorted(reached))}; the fixture probes the "
                             f"{sides[0]}.", net=self.net_name(net))
        return summary

    def check_probe_side(self) -> None:
        if self.probe_side[0] == "both":
            return
        side = self.probe_side[0]
        other = "top" if side == "bottom" else "bottom"
        nets = sorted(self.net_name(net) or "" for net in self.considered
                      if self.access.get(net) == {other})
        if not nets:
            return
        self.finding(
            "probe_side", "info",
            f"{_plural(len(nets), 'net')} {'is' if len(nets) == 1 else 'are'} reachable only "
            f"from the {other}: a single-sided fixture "
            f"probing the {side} cannot reach them (a dual-sided fixture costs more; or move "
            f"or add sites on the {side}).",
            probe_side=side, count=len(nets), nets=nets[:NET_SAMPLE],
        )

    def check_rails(self) -> None:
        if not self.probes:
            return
        profile = self.profile
        low, high = profile.get("rail_min_sites"), profile.get("rail_max_sites")
        fanout = profile.get("rail_min_connections")
        for net in self.considered:
            name = self.net_name(net) or ""
            kind = _supply_kind(name)
            if kind == "ground":
                continue
            connections = self.connections[net]
            if not (kind == "power" or (fanout is not None and connections >= fanout)):
                continue
            sites = self.site_count(net)
            basis = "power-named" if kind == "power" else f"{connections} connections"
            if low is not None and sites < low:
                severity = "warning" if sites == 0 else "info"
                self.finding(
                    "rails", severity,
                    f"Rail ({basis}) with {_plural(sites, 'probe site')}: "
                    f"guidance is at least {low}, so the fixture can measure it and share "
                    "the probe current.",
                    net=name, sites=sites, connections=connections,
                )
            elif high is not None and sites > high:
                self.finding(
                    "rails", "info",
                    f"Rail ({basis}) with {sites} probe sites: guidance is at most {high}; "
                    "more use up fixture probes and add force.",
                    net=name, sites=sites, connections=connections,
                )

    def check_ground(self) -> None:
        if not self.probes:
            return
        per_site = self.profile.get("ground_connections_per_site")
        for net in self.considered:
            name = self.net_name(net) or ""
            if _supply_kind(name) != "ground":
                continue
            connections = self.connections[net]
            wanted = max(1, math.ceil(connections / per_site)) if per_site else 1
            sites = self.site_count(net)
            if sites >= wanted:
                continue
            basis = (f"about 1 per {per_site} connections" if per_site
                     else "at least one, as the measurement return")
            self.finding(
                "ground", "warning" if sites == 0 else "info",
                f"Ground net with {_plural(sites, 'probe site')} for "
                f"{connections} connections: guidance is {basis} ({wanted}), spread over "
                "the board.",
                net=name, sites=sites, wanted=wanted, connections=connections,
            )

    def check_pad_size(self) -> None:
        preferred = self.profile.get("pad_preferred_mil")
        minimum = self.profile.get("pad_min_mil")
        groups: dict[tuple, list] = defaultdict(list)
        for probe in self.probed():
            if minimum is not None and probe.size_mil < minimum - EPS_MIL:
                severity = "warning"
            elif preferred is not None and probe.size_mil < preferred - EPS_MIL:
                severity = "info"
            else:
                continue
            groups[(severity, round(probe.size_mil, 3), probe.kind)].append(
                (probe.size_mil, self.example(probe)))
        for (severity, size, kind), members in sorted(groups.items()):
            limit = minimum if severity == "warning" else preferred
            word = "acceptable minimum" if severity == "warning" else "preferred size"
            self.finding(
                "pad_size", severity,
                f"Probe sites ({kind}s) of {self._len(size)}: below the profile's {word} "
                f"({self._len(limit)}); small targets need finer probes and miss more often.",
                size_mil=size, count=len(members),
                examples=[example for _, example in members[:SAMPLE]],
            )

    def _cells(self, x0: float, y0: float, x1: float, y1: float):
        for cx in range(math.floor(x0 / _CELL_MIL), math.floor(x1 / _CELL_MIL) + 1):
            for cy in range(math.floor(y0 / _CELL_MIL), math.floor(y1 / _CELL_MIL) + 1):
                yield cx, cy

    def check_spacing(self) -> None:
        preferred = self.profile.get("spacing_preferred_mil")
        minimum = self.profile.get("spacing_min_mil")
        reach = max(value for value in (preferred, minimum, 0.0) if value is not None)
        if reach <= 0:
            return
        size = max(reach, 1.0)
        pairs: dict[str, list] = defaultdict(list)
        probed = self.probed()
        for side in self.probe_sides():
            grid: dict[tuple, list[Probe]] = defaultdict(list)
            for probe in (probe for probe in probed if probe.side == side):
                cx, cy = math.floor(probe.x / size), math.floor(probe.y / size)
                for dx in (-1, 0, 1):
                    for dy in (-1, 0, 1):
                        for other in grid.get((cx + dx, cy + dy), ()):
                            distance = math.hypot(probe.x - other.x, probe.y - other.y)
                            if distance >= reach - EPS_MIL:
                                continue
                            severity = (
                                "warning" if minimum is not None
                                and distance < minimum - EPS_MIL else "info"
                            )
                            if severity == "info" and (
                                    preferred is None or distance >= preferred - EPS_MIL):
                                continue
                            pairs[severity].append((distance, other, probe))
                grid[(cx, cy)].append(probe)
        for severity in ("warning", "info"):
            found = sorted(pairs.get(severity, []), key=lambda item: item[0])
            if not found:
                continue
            limit = minimum if severity == "warning" else preferred
            word = "minimum" if severity == "warning" else "preferred"
            examples = []
            for distance, first, second in found[:SAMPLE]:
                examples.append({
                    "distance_mil": _round(distance), "side": first.side,
                    "first": self.example(first), "second": self.example(second),
                })
            self.finding(
                "spacing", severity,
                f"Probe sites closer than the profile's {word} centre spacing "
                f"({self._len(limit)}) on one side: neighbouring probes need room "
                "(smaller probes, or move a site).",
                count=len(found), closest_mil=_round(found[0][0]), examples=examples,
            )

    def check_edge(self) -> None:
        clearance = self.profile.get("edge_clearance_mil")
        if not clearance:
            return
        if not self.edge.has_outline:
            self.notes.append("No board outline: edge clearance not checked.")
            return
        found = []
        seen: set[int] = set()
        for probe in self.probed():
            if probe.row in seen:  # a through-hole site's other face: the same place
                continue
            seen.add(probe.row)
            _, best = self.edge.nearest(geometry._point(probe.x, probe.y), clearance,
                                        clearance - EPS_MIL)
            if best is not None:
                found.append((best[0], probe))
        if not found:
            return
        found.sort(key=lambda item: item[0])
        self.finding(
            "edge_clearance", "warning",
            f"Probe sites closer than {self._len(clearance)} to the board edge (centre to "
            "the outline or a cutout): the edge is where the fixture or the clamps hold the "
            "board.",
            count=len(found), closest_mil=_round(found[0][0]),
            examples=[self.example(probe, edge_distance_mil=_round(distance))
                      for distance, probe in found[:SAMPLE]],
        )

    def _bodies(self):
        """(side, cell) -> [(part, body, box, tall)] of the fitted parts' bodies."""
        height = self.profile.get("tall_part_height_mil")
        clearance = self.profile.get("tall_part_clearance_mil") or 0.0
        grid: dict[tuple, list] = defaultdict(list)
        for part in assembly.parts(self.index):
            if not part.bodies or not part.fitted(self.is_dnp):
                continue
            for body in part.bodies:
                xs = [point[0] for point in body.outline]
                ys = [point[1] for point in body.outline]
                box = (min(xs), min(ys), max(xs), max(ys))
                # A TP part (a test loop is often 5 to 7 mm tall) is test hardware, a
                # target itself: no tall-part keep-out.
                tall = (height is not None and part.component not in self.tp
                        and (body.overall_height_mil or 0.0) >= height - EPS_MIL)
                grow = clearance if tall else 0.0
                entry = (part, body, box, tall)
                for cell in self._cells(box[0] - grow, box[1] - grow, box[2] + grow,
                                        box[3] + grow):
                    grid[(part.side, cell)].append(entry)
        return grid

    def check_bodies(self) -> None:
        """under_component and tall_parts."""
        grid = self._bodies()
        clearance = self.profile.get("tall_part_clearance_mil") or 0.0
        under: dict[tuple, list] = defaultdict(list)
        near: dict[int, list] = defaultdict(list)
        for probe in self.probed():
            cell = (math.floor(probe.x / _CELL_MIL), math.floor(probe.y / _CELL_MIL))
            seen = set()
            covered = None
            nearest = None
            for part, body, box, tall in grid.get((probe.side, cell), ()):
                if id(body) in seen or part.component == probe.component:
                    continue
                seen.add(id(body))
                inside = (box[0] <= probe.x <= box[2] and box[1] <= probe.y <= box[3]
                          and point_in_polygon(probe.x, probe.y, body.outline))
                if inside:
                    if covered is None or (body.overall_height_mil or 0.0) > (
                            covered[1].overall_height_mil or 0.0):
                        covered = (part, body)
                    continue
                if tall and clearance:
                    distance = point_polygon_distance(probe.x, probe.y, body.outline)
                    if distance < clearance - EPS_MIL and (
                            nearest is None or distance < nearest[0]):
                        nearest = (distance, part, body)
            if covered is not None:
                part, body = covered
                allowed = None
                if probe.marked:
                    rule = self.style_rule(probe)
                    if rule is not None:
                        allowed = (rule.name,
                                   self.values(rule).get("allow_under_component"))
                severity = "info" if allowed and allowed[1] else "warning"
                under[(severity, part.component, allowed)].append(
                    self.example(probe, height_mil=_round(body.overall_height_mil)))
            elif nearest is not None:
                distance, part, body = nearest
                near[part.component].append(
                    (distance, self.example(probe, distance_mil=_round(distance))))
        for (severity, component, allowed), examples in under.items():
            designator = self._designator(component)
            issue = (f"Probe sites under {designator}'s body on the side they are probed "
                     "from: a probe cannot reach them while the part is fitted.")
            if allowed and allowed[1]:
                issue += (f" The Testpoint rule '{allowed[0]}' allows testpoints under "
                          "components (fine for bare-board test).")
            self.finding("under_component", severity, issue, part=designator,
                         count=len(examples), examples=examples[:SAMPLE])
        height = self.profile.get("tall_part_height_mil")
        for component, members in near.items():
            members.sort(key=lambda item: item[0])
            part = assembly.parts(self.index)[component]
            self.finding(
                "tall_parts", "info",
                f"Probe sites within {self._len(clearance)} of {part.designator}, a part "
                f"{self._len(part.height_mil or 0.0)} tall on the same side (guidance keeps "
                f"sites that far from parts over {self._len(height or 0.0)}: the fixture "
                "plate needs a pocket around it, and a probe can glance off it).",
                part=part.designator, height_mil=_round(part.height_mil),
                count=len(members), closest_mil=_round(members[0][0]),
                examples=[example for _, example in members[:SAMPLE]],
            )

    def check_solder_mask(self) -> None:
        tented = [probe for probe in self.probes if probe.tented]
        if not tented:
            return
        self.finding(
            "solder_mask", "info",
            "Vias probed on a side where they are tented: the solder mask covers the copper "
            "there, so a probe cannot make contact. Remove the tenting on that side (or "
            "confirm in the solder mask Gerber that the testpoint is open).",
            count=len(tented), examples=[self.example(probe) for probe in tented[:SAMPLE]],
        )

    def check_style_rule(self) -> None:
        groups: dict[tuple, list] = defaultdict(list)
        for probe in self.probes:
            if not probe.marked:
                continue
            rule = self.style_rule(probe)
            if rule is None:
                continue
            values = self.values(rule)
            problems = []
            if values.get("min_size_mil") is not None and \
                    probe.size_mil < values["min_size_mil"] - EPS_MIL:
                problems.append(("size_below_rule", values["min_size_mil"]))
            if values.get("max_size_mil") is not None and \
                    probe.size_mil > values["max_size_mil"] + EPS_MIL:
                problems.append(("size_above_rule", values["max_size_mil"]))
            if probe.hole_mil > 0:
                if values.get("min_hole_mil") is not None and \
                        probe.hole_mil < values["min_hole_mil"] - EPS_MIL:
                    problems.append(("hole_below_rule", values["min_hole_mil"]))
                if values.get("max_hole_mil") is not None and \
                        probe.hole_mil > values["max_hole_mil"] + EPS_MIL:
                    problems.append(("hole_above_rule", values["max_hole_mil"]))
            if values.get(f"allow_{probe.side}") is False:
                problems.append(("side_not_allowed", None))
            for problem, limit in problems:
                groups[(rule.name, problem, limit)].append(
                    self.example(probe, hole_mil=_round(probe.hole_mil) or None))
        texts = {
            "size_below_rule": "smaller than the rule's minimum size ({})",
            "size_above_rule": "larger than the rule's maximum size ({})",
            "hole_below_rule": "with a hole smaller than the rule's minimum ({})",
            "hole_above_rule": "with a hole larger than the rule's maximum ({})",
            "side_not_allowed": "on a side the rule does not allow",
        }
        for (name, problem, limit), examples in groups.items():
            detail = texts[problem].format(self._len(limit) if limit is not None else "")
            self.finding(
                "style_rule", "warning" if problem == "side_not_allowed" else "info",
                f"Testpoints {detail}: the Testpoint rule '{name}' (Altium's Testpoint "
                "Manager and DRC use it).",
                rule=name, problem=problem, limit_mil=_round(limit), count=len(examples),
                examples=examples[:SAMPLE],
            )

    def tooling(self) -> list:
        minimum = self.profile.get("tooling_hole_min_mil") or 0.0
        parts = assembly.parts(self.index)
        holes = []
        for hole in geometry.holes(self.index):
            if hole.plated is not False or hole.hole_mil < minimum - EPS_MIL:
                continue
            if hole.component is not None and 0 <= hole.component < len(parts) and \
                    parts[hole.component].mount != "mechanical":
                continue
            if self.edge.has_outline and not self.edge.on_board(hole.x, hole.y):
                continue
            holes.append(hole)
        return holes

    def check_tooling(self, holes: list) -> None:
        wanted = self.profile.get("tooling_holes_min")
        minimum = self.profile.get("tooling_hole_min_mil") or 0.0
        if wanted and len(holes) < wanted:
            self.finding(
                "tooling_holes", "warning",
                f"{_plural(len(holes), 'non-plated hole')} of at least "
                f"{self._len(minimum)} outside soldered parts: a bed-of-nails fixture "
                f"locates the board on {wanted} tooling holes (typically 3.2 mm, "
                "non-plated, near diagonally opposite corners).",
                count=len(holes), wanted=wanted,
            )
            return
        box = assembly.outline_box(self.pcb)
        if wanted and len(holes) >= 2 and box is not None:
            spread = max(math.hypot(a.x - b.x, a.y - b.y)
                         for number, a in enumerate(holes) for b in holes[number + 1:])
            diagonal = math.hypot(box[2] - box[0], box[3] - box[1])
            if spread < diagonal / 2.0:
                self.finding(
                    "tooling_holes", "info",
                    f"The tooling holes are at most {self._len(spread)} apart, under half "
                    f"the board's diagonal ({self._len(diagonal)}): holes near opposite "
                    "corners locate the board better.",
                    count=len(holes), spread_mil=_round(spread),
                )

    # --- report ---

    def coverage(self) -> dict:
        nets = self.considered
        total = len(nets)

        def share(count: int) -> float | None:
            return round(100.0 * count / total, 1) if total else None

        access = [self.access.get(net, set()) for net in nets]
        with_access = sum(1 for sides in access if sides)
        top = sum(1 for sides in access if "top" in sides)
        bottom = sum(1 for sides in access if "bottom" in sides)
        side, chosen_by = self.probe_side
        result = {
            "nets": total,
            "with_access": with_access,
            "percent": share(with_access),
            "top": top,
            "top_percent": share(top),
            "bottom": bottom,
            "bottom_percent": share(bottom),
            "both_sides": sum(1 for sides in access if len(sides) == 2),
            "top_only": sum(1 for sides in access if sides == {"top"}),
            "bottom_only": sum(1 for sides in access if sides == {"bottom"}),
            "probe_side": side,
            "probe_side_chosen_by": chosen_by,
        }
        if side != "both":
            result["probe_side_percent"] = result[f"{side}_percent"]
        return result

    def sites(self) -> dict:
        rows = {probe.row for probe in self.probes}
        marked = {probe.row for probe in self.probes if probe.marked}
        tp = {probe.row for probe in self.probes if probe.tp_part}
        result = {
            "probe_sites": len(rows),
            "probe_points": len(self.probes),
            "testpoint_marked": len(marked),
            "tp_part_pads": len(tp),
            "tp_parts": len(self.tp),
            "by_side": dict(Counter(probe.side for probe in self.probes)),
            "by_kind": dict(Counter(probe.kind for probe in self.probes)),
            "nets_with_sites": len({probe.net for probe in self.probes
                                    if probe.net is not None}),
        }
        if self.off_board:
            result["off_board"] = self.off_board
        if self.marks_without_copper:
            result["marks_without_copper"] = self.marks_without_copper
        return result


def review_testpoints(
    index,
    profile: dict | None = None,
    checks: list[str] | None = None,
    nets: list[str] | None = None,
    units: str = "mil",
    is_dnp=None,
) -> dict:
    """The whole review, unpaged (page_result pages it), or an error. profile comes from
    build_profile (default: the ict profile as built in)."""
    if profile is None:
        profile, _ = build_profile()
    selected = list(dict.fromkeys(checks)) if checks else list(CHECKS)
    invalid = [check for check in selected if check not in CHECKS]
    if invalid:
        return {
            "error": "invalid_checks",
            "message": f"Unknown checks: {', '.join(invalid)}.",
            "valid_checks": list(CHECKS),
        }
    critical, unknown = _selection(index, nets)
    if unknown:
        return {
            "error": "net_not_found",
            "message": (
                "No PCB net, differential pair or net class named "
                + ", ".join(f"'{name}'" for name in unknown) + "."
            ),
        }
    review = _Review(index, profile, set(selected), critical, units, is_dnp)
    review.check_coverage()
    usage = review.check_required()
    review.check_probe_side()
    review.check_rails()
    review.check_ground()
    if "pad_size" in selected:
        review.check_pad_size()
    if "spacing" in selected:
        review.check_spacing()
    if "edge_clearance" in selected:
        review.check_edge()
    if {"under_component", "tall_parts"} & set(selected):
        review.check_bodies()
    review.check_solder_mask()
    if "style_rule" in selected:
        review.check_style_rule()
    holes = review.tooling()
    review.check_tooling(holes)

    findings = sorted(
        review.findings,
        key=lambda finding: (
            _SEVERITY_ORDER[finding["severity"]],
            CHECKS.index(finding["check"]),
            -finding.get("count", 1),
            str(finding.get("net", "")),
            str(finding.get("part", "")),
        ),
    )
    shown = {key: value for key, value in profile.items()
             if key not in ("name", "chosen_by", "description", "changed_by_limits",
                            "probe_side_chosen_by")}
    result: dict = {
        "checks_run": selected,
        "profile": {
            "name": profile["name"],
            "chosen_by": profile["chosen_by"],
            "description": profile["description"],
            "values": shown,
            **({"changed_by_limits": profile["changed_by_limits"]}
               if profile.get("changed_by_limits") else {}),
            "guidance": GUIDANCE,
        },
        "origin_mil": [_round(review.origin[0]), _round(review.origin[1])],
        "sites": review.sites(),
        "coverage": review.coverage(),
        "usage": usage,
        "tooling_holes": {
            "count": len(holes),
            "holes": [
                {"at_mil": review._at(hole.x, hole.y), "hole_mil": _round(hole.hole_mil)}
                for hole in holes[:TOOLING_LISTED]
            ],
        },
        "testpoint_rules": review.rule_report(),
    }
    if critical is not None:
        result["nets_asked"] = len(review.critical)
    result["severity_counts"] = dict(Counter(finding["severity"] for finding in findings))
    result["findings_by_check"] = dict(Counter(finding["check"] for finding in findings))
    result["findings"] = findings
    notes = list(review.notes)
    if not review.probes:
        notes.append("With no probe sites, rails and ground are not judged per net.")
    notes += [
        "Assembly Testpoint properties are not read from the PcbDoc (only Fabrication "
        "Testpoint top/bottom are decoded); assembly test access counts the same sites, "
        "and a pad marked only as an assembly testpoint is missed unless it is on a TP part.",
        "Altium's Testpoint Usage DRC counts only pads and vias with a Testpoint property; "
        "here TP parts' pads count as probe sites too.",
        "Not checked: the testpoint grid, probe-side component height limits of a specific "
        "fixture, and test coverage of the parts themselves (that is the test program's).",
    ]
    if is_dnp is not None:
        notes.append("Bodies of parts Not Fitted in the active variant do not block probes.")
    result["notes"] = notes
    return result


def probe_sides(index, probe_side: str | None = None) -> tuple[str | None, str, list[Probe]]:
    """(side, chosen_by, probe points) of an in-circuit test as check_testpoints decides
    it: probe_side as given (chosen_by "given"), else the ict profile's auto choice (the
    side reaching more nets, then the side with more probe points). side is None when the
    board has no probe site and none is given. side may be "both"."""
    profile, _ = build_profile(DEFAULT_PROFILE)
    if probe_side:
        profile = {**profile, "probe_side": probe_side, "probe_side_chosen_by": "given"}
    review = _Review(index, profile, set(), None, "mil")
    if not review.probes and not probe_side:
        return None, "no_probe_sites", []
    side, chosen_by = review.probe_side
    return side, chosen_by, list(review.probes)


def page_result(full: dict, offset: int = 0, limit: int = 100) -> dict:
    """A page of review_testpoints' result: findings from offset."""
    result = dict(full)
    offset = max(0, offset)
    findings = full["findings"]
    notes = result.pop("notes")
    result["findings"] = findings[offset : offset + limit]
    result["total"] = len(findings)
    result["offset"] = offset
    result["has_more"] = offset + limit < len(findings)
    if result["has_more"]:
        result["next_offset"] = offset + limit
    result["notes"] = notes
    return result
