"""Per-object design-rule compliance checks over a PcbIndex.

Pure logic, no MCP: for every copper object (track, arc, via) of the selected
nets, find the rule of each kind that applies to that object (object-level
scopes such as WithinRoom, OnLayer and IsVia are decided per object) and test
the object against it.

Calibration against Altium batch-DRC reports (see the compliance mapping
notes): Width on nets outside differential pairs, DiffPairsRouting per-layer
widths on pair-member tracks and RoutingVias matched Altium exactly on every
fresh report. HoleSize is exact for vias, and so is MinimumAnnularRing for
simple-stack vias (pads are not checked here). A top-middle-bottom or
full-stack via's ring is the smallest (pad - hole) / 2 over the signal layers
of its span that keep a pad, as check_vias measures it; no batch-DRC report
has confirmed that for such vias. RoutingLayers agrees on every report (all
clean). Length and the clearance spot-check are approximate.

All lengths are mils under *_mil keys, so callers can convert units.
"""

from collections import defaultdict
from dataclasses import replace

try:
    from services.pcb_rules import (
        ObjectResolver,
        RuleTarget,
        evaluate_scope,
        rule_values,
    )
except ImportError:
    from server.services.pcb_rules import (
        ObjectResolver,
        RuleTarget,
        evaluate_scope,
        rule_values,
    )

EPS_MIL = 0.001
ROUNDING_MIL = 0.01
# Edge distances of tracks on Altium's 1/10000-mil grid come out up to ~0.002
# mil short of a clearance they meet exactly (MiniPC: 3,815 such pairs);
# Altium does not flag them.
CLEARANCE_TOLERANCE_MIL = 0.01
UNDECIDED_EXAMPLES = 5
CLEARANCE_MAX_NETS = 10
CLEARANCE_MAX_OBJECTS = 20_000

SUPPORTED_KINDS = (
    "Width",
    "DiffPairsRouting",
    "RoutingVias",
    "HoleSize",
    "MinimumAnnularRing",
    "RoutingLayers",
    "Length",
    "Clearance",
)

# Rules6 kind ids (PcbRule.kind_id) as listed in RULESETTOCHECK; used when the
# board has no rule of the kind to read the id from.
KIND_IDS = {
    "Clearance": 0,
    "Width": 2,
    "Length": 3,
    "MatchedLengths": 4,
    "RoutingLayers": 9,
    "RoutingCorners": 10,
    "RoutingVias": 11,
    "MinimumAnnularRing": 19,
    "HoleSize": 42,
    "DiffPairsRouting": 51,
    "NetAntennae": 56,
}

_TRACK_KINDS = ("Width", "DiffPairsRouting", "RoutingLayers")
_VIA_KINDS = ("RoutingVias", "HoleSize", "MinimumAnnularRing")

_NOT_CHECKED = (
    {
        "kind": "RoutingCorners",
        "reason": "Not a DRC rule in Altium: only autorouters use it.",
    },
    {
        "kind": "NetAntennae",
        "reason": (
            "Needs fill, region and poured copper, which are not parsed; a "
            "track-only check gives false antennae."
        ),
    },
    {
        "kind": "HoleSize / MinimumAnnularRing (pads)",
        "reason": (
            "Pad slot lengths and per-layer pad sizes are not parsed; only "
            "via holes and rings are checked."
        ),
    },
    {
        "kind": "MatchedLengths",
        "reason": "Group/skew matching: see check_lengths.",
    },
)

# Hints in the output name the MCP tool that exposes this check
# (server/main.py check_rule_compliance) and its parameters.
_TOOL = "check_rule_compliance"
_CLEARANCE_HINT = (
    f"call {_TOOL}(net='A,B', clearance_spot_check=True) with at most "
    f"{CLEARANCE_MAX_NETS} nets"
)
_UNCOUPLED_NOT_CHECKED = {
    "kind": "DiffPairsRouting (max uncoupled length)",
    "reason": (
        "Only the per-layer min/max widths of pair-member tracks and arcs are "
        "checked. Altium's batch DRC for this kind also measures each pair's "
        "uncoupled length (copper where the pair's gap is outside the rule's gap "
        "range) against Max Uncoupled Length; that is not checked here: "
        "check_diff_pair_geometry measures it, with the coupled gap per layer."
    ),
}
_PAIRS_ADDED = (
    "Width does not cover diff-pair member tracks and arcs: their widths are "
    "checked by their DiffPairsRouting rule, so DiffPairsRouting was added."
)

# Scope terms that ask which footprint owns an object. At net level they are
# decided from the pads the net connects to, which says nothing about a via or
# track that a footprint owns, so scopes using them are decided per object
# ("incomponent" also covers InComponentClass).
_OWNERSHIP_TERMS = (
    "incomponent", "inanycomponent", "hasfootprint", "haspad", "inpadclass",
    "comppincount",
)
# A hatched pour's tracks and arcs are InPolygon, the net's routing is not:
# one net's objects can differ (inpoly also covers inpolygon, inpolygonclass).
_POLYGON_TERMS = (
    "inpoly", "ispoly", "innamedpolygon", "isnamedpolygon", "issplitplane",
)


def _per_object_scope(scope: str | None) -> bool:
    text = (scope or "").lower()
    return any(term in text for term in _OWNERSHIP_TERMS + _POLYGON_TERMS)


# ---------- three-valued helpers ----------


def _and3(left: str, right: str) -> str:
    if left == "no" or right == "no":
        return "no"
    if left == "yes" and right == "yes":
        return "yes"
    return "conditional"


def _or3(left: str, right: str) -> str:
    if left == "yes" or right == "yes":
        return "yes"
    if left == "no" and right == "no":
        return "no"
    return "conditional"


def _net_scope_applies(
    net_scope: str | None, target_a: RuleTarget, target_b: RuleTarget
) -> bool:
    """Does a binary rule's connective checking cover two different nets?"""
    scope = (net_scope or "").lower()
    if "pair" in scope and "same" in scope:
        return bool(target_a.pairs & target_b.pairs)
    if "pair" in scope and "different" in scope:
        return bool(target_a.pairs and target_b.pairs) and not (
            target_a.pairs & target_b.pairs
        )
    return scope != "samenet"


def _resolve_binary(rules, targets, ctx, side) -> tuple:
    """(rule, undecided) for an object or net pair; see resolve_clearance_rule.

    side(position, which, endpoint) -> "yes" | "no" | "conditional" evaluates
    scope1 (which=1) or scope2 (which=2) of rules[position] on targets[0] or
    targets[1], so callers can cache verdicts.
    """
    undecided: list[dict] = []
    for position, rule in enumerate(rules):
        if not _net_scope_applies(rule.net_scope, targets[0], targets[1]):
            continue
        forward = _and3(side(position, 1, 0), side(position, 2, 1))
        if forward == "yes":
            return rule, undecided
        backward = _and3(side(position, 1, 1), side(position, 2, 0))
        verdict = _or3(forward, backward)
        if verdict == "yes":
            return rule, undecided
        if verdict == "conditional":
            conditions: list[str] = []
            for which, endpoint in ((1, 0), (2, 1), (1, 1), (2, 0)):
                scope = rule.scope1 if which == 1 else rule.scope2
                result = evaluate_scope(scope, targets[endpoint], ctx)
                conditions.extend(result.get("conditions", []))
            undecided.append(
                {"rule": rule.name, "conditions": list(dict.fromkeys(conditions))}
            )
    return None, undecided


def resolve_clearance_rule(rules, target_a, target_b, ctx) -> tuple:
    """Resolve a binary rule kind (Clearance) for two nets or two objects.

    rules: rules of one binary kind. target_a / target_b: RuleTargets of
    different nets, each with obj set for an object-level resolution.
    Returns (rule | None, undecided): the first enabled rule by priority whose
    scope1 matches a and scope2 matches b, or scope1 matches b and scope2
    matches a, honouring NETSCOPE (SameNet rules never apply to two nets;
    pair-scoped connective checking needs the nets' pairs). undecided lists
    higher-priority rules that matched only conditionally (the result is then
    not certain).
    """
    ordered = sorted(
        (rule for rule in rules if rule.enabled), key=lambda rule: rule.priority
    )
    targets = (target_a, target_b)

    def side(position, which, endpoint):
        rule = ordered[position]
        scope = rule.scope1 if which == 1 else rule.scope2
        return evaluate_scope(scope, targets[endpoint], ctx)["matches"]

    return _resolve_binary(ordered, targets, ctx, side)


def _undecided_entry(rule, side: dict) -> dict:
    entry = {"rule": rule.name, "conditions": side.get("conditions", [])}
    if side.get("parse_error"):
        entry["parse_error"] = side["parse_error"]
    return entry


class _Resolver(ObjectResolver):
    """ObjectResolver that decides more scopes on each object.

    Net-level verdicts are reused for every object only when object facts
    cannot change them: scopes are decided per object for a target without
    nets (no-net vias, whose scope facts are the object's alone) and, for a
    net, when they ask which footprint owns the object (_OWNERSHIP_TERMS) or
    whether it is pour copper (_POLYGON_TERMS).
    """

    def __init__(self, rules: list, target: RuleTarget, ctx):
        super().__init__(rules, target, ctx)
        self.net_level = [
            "conditional"
            if not target.nets
            or (target.kind == "net" and _per_object_scope(rule.scope1))
            else verdict
            for rule, verdict in zip(self.rules, self.net_level)
        ]


# ---------- per-kind report ----------


class _KindReport:
    """Counts, per-rule usage and grouped violations of one rule kind."""

    def __init__(self, kind: str):
        self.kind = kind
        self.objects = 0
        self.by_rule: dict[tuple, dict] = {}
        self.no_rule = 0
        self.undecided = 0
        self.undecided_examples: list[dict] = []
        self._undecided_nets: set = set()
        self.violations = 0
        self.groups: dict[tuple, dict] = {}
        self.extra: dict = {}
        self.approximate = False
        self.skipped: str | None = None

    def _rule_entry(self, rule) -> dict:
        key = (rule.name, rule.priority)
        if key not in self.by_rule:
            self.by_rule[key] = {
                "rule": rule.name,
                "priority": rule.priority,
                "objects": 0,
                "violations": 0,
            }
        return self.by_rule[key]

    def checked(self, rule) -> None:
        self.objects += 1
        if rule is None:
            self.no_rule += 1
        else:
            self._rule_entry(rule)["objects"] += 1

    def not_decided(self, net: str | None, undecided: list[dict]) -> None:
        self.objects += 1
        self.undecided += 1
        if (
            len(self.undecided_examples) < UNDECIDED_EXAMPLES
            and net not in self._undecided_nets
        ):
            self._undecided_nets.add(net)
            self.undecided_examples.append({"net": net, "rules": undecided})

    def violation(self, rule, key: tuple, record: dict, worst=None) -> None:
        """Count one violating object; group it with equal ones.

        worst(existing, record) -> bool replaces the group's example when the
        new record is worse (records of a group may differ, e.g. clearance).
        """
        self.violations += 1
        if rule is not None:
            self._rule_entry(rule)["violations"] += 1
        group = self.groups.get(key)
        if group is None:
            record["count"] = 1
            self.groups[key] = record
            return
        count = group["count"] + 1
        if worst is not None and worst(group, record):
            record["count"] = count
            self.groups[key] = record
        else:
            group["count"] = count

    def extra_count(self, name: str, amount: int = 1) -> None:
        self.extra[name] = self.extra.get(name, 0) + amount

    def render(self, checked_by_batch_drc, limit: int, offset: int = 0) -> dict:
        groups = sorted(
            self.groups.values(),
            key=lambda group: (
                -group.get("margin_mil", 0.0),
                -group["count"],
                str(group.get("net")),
            ),
        )
        result: dict = {
            "kind": self.kind,
            "checked_by_batch_drc": checked_by_batch_drc,
        }
        if self.skipped:
            result["skipped"] = self.skipped
            return result
        result.update(
            {
                "objects_checked": self.objects,
                "by_rule": sorted(
                    self.by_rule.values(),
                    key=lambda entry: (-entry["objects"], entry["priority"]),
                ),
                "no_rule_objects": self.no_rule,
                "undecided": {
                    "count": self.undecided,
                    "examples": self.undecided_examples,
                },
                "violation_count": self.violations,
                "violation_groups": len(groups),
                "violations": groups[offset : offset + limit],
                "has_more": offset + limit < len(groups),
            }
        )
        if offset:
            result["offset"] = offset
        if result["has_more"]:
            result["next_offset"] = offset + limit
        if self.approximate:
            result["approximate"] = True
        result.update(self.extra)
        return result


def _limit_check(value: float, low, high) -> tuple | None:
    """(side, margin) when value is outside [low, high] beyond EPS_MIL."""
    if low is not None and value < low - EPS_MIL:
        return "below_min", low - value
    if high is not None and value > high + EPS_MIL:
        return "above_max", value - high
    return None


def _round(value):
    return None if value is None else round(value, 4)


def _margin_fields(record: dict, margin: float) -> dict:
    record["margin_mil"] = round(margin, 4)
    if margin < ROUNDING_MIL:
        record["rounding"] = True
    return record


def smallest_via_pad(prim, layers) -> tuple[float, int | None]:
    """(diameter, v6 layer) of a via's smallest pad over the signal layers
    (v6 1..32) in layers that keep a pad; its annular ring there is
    (diameter - hole) / 2. The layer is None when every such layer has the
    same pad (a simple stack). With no layer that keeps a pad, the via's
    nominal diameter and None. Shared by check_rule_compliance and check_vias
    so both report the same ring."""
    stack = getattr(prim, "via", None)
    pads = []
    for layer in layers:
        if not 1 <= layer <= 32:
            continue
        diameter = (
            stack.pad_diameter(layer, prim.width) if stack is not None else prim.width
        )
        if diameter:
            pads.append((diameter, layer))
    if not pads:
        return prim.width, None
    diameter, layer = min(pads, key=lambda pad: pad[0])
    if all(abs(size - diameter) <= EPS_MIL for size, _ in pads):
        return diameter, None
    return diameter, layer


# ---------- checker ----------


class _Checker:
    def __init__(self, index, limit: int):
        self.index = index
        self.ctx = index.rule_context
        self.limit = limit
        self.signal = index.signal_layers()
        self.substacks = getattr(index.pcb.board, "substacks", None)
        self.rules: dict[str, list] = defaultdict(list)
        for rule in index.pcb.rules:
            self.rules[rule.kind].append(rule)
        self._values: dict[int, dict] = {}
        self._targets: dict = {}
        self._resolvers: dict = {}
        self._pairs: dict = {}
        self._copper_order: list[int] = []
        self._copper_position: dict[int, int] | None = None
        # No-net objects (free vias): a target without nets, whose scopes the
        # resolver decides on each object.
        self._free = RuleTarget(kind="net", name="", nets=set(), pairs=set())

    # --- lookups ---

    def values(self, rule) -> dict:
        key = id(rule)
        if key not in self._values:
            self._values[key] = rule_values(rule, self.signal, self.substacks)
        return self._values[key]

    def enabled(self, kind: str) -> list:
        return [rule for rule in self.rules.get(kind, []) if rule.enabled]

    def target(self, net: str | None) -> RuleTarget:
        if net is None:
            return self._free
        key = net.lower()
        if key not in self._targets:
            self._targets[key] = self.index.rule_target(
                net, kind="net"
            ) or RuleTarget(kind="net", name=key, nets={key}, pairs=set())
        return self._targets[key]

    def pair_of(self, net: str) -> str | None:
        key = net.lower()
        if key not in self._pairs:
            pairs = self.index.diff_pairs_of(net)
            self._pairs[key] = pairs[0]["name"] if pairs else None
        return self._pairs[key]

    def resolver(self, kind: str, net: str | None, pair: str | None = None):
        key = (kind, pair, net.lower() if net else None)
        if key not in self._resolvers:
            if pair is not None:
                pair_target = self.index.rule_target(pair, kind="diff_pair")
                target = (
                    replace(pair_target, member=net.lower())
                    if pair_target is not None
                    else self.target(net)
                )
            else:
                target = self.target(net)
            self._resolvers[key] = _Resolver(
                self.rules.get(kind, []), target, self.ctx
            )
        return self._resolvers[key]

    def layer_label(self, v6_id) -> str:
        return self.signal.get(v6_id) or self.index.layer_name(v6_id)

    def batch_drc(self, kind: str):
        kinds = getattr(self.index.pcb, "drc_rule_kinds", None)
        if kinds is None:
            return None
        ids = {rule.kind_id for rule in self.rules.get(kind, [])}
        if not ids and kind in KIND_IDS:
            ids = {KIND_IDS[kind]}
        if not ids:
            return None
        return bool(ids & set(kinds))

    # --- object enumeration ---

    def _rows(self, sql: str, params: list, net_ids: list | None):
        if net_ids is None:
            return self.index.db.execute(sql, params).fetchall()
        rows = []
        for start in range(0, len(net_ids), 500):
            chunk = net_ids[start : start + 500]
            rows.extend(
                self.index.db.execute(
                    sql + f" AND p.net_id IN ({','.join('?' * len(chunk))})",
                    params + chunk,
                ).fetchall()
            )
        rows.sort(key=lambda row: row["id"])
        return rows

    def routed_objects(self, net_ids: list | None):
        """(row, facts, x, y) per copper track and arc of a net on a signal
        layer; an arc (all its chords) is one object, described by its middle
        chord."""
        layers = sorted(self.signal)
        if not layers:
            return
        rows = self._rows(
            "SELECT p.*, n.name AS net_name FROM prims p "
            "JOIN nets n ON n.id = p.net_id "
            "WHERE p.kind IN ('track','arc') AND p.pour_id IS NULL "
            f"AND p.layer IN ({','.join('?' * len(layers))})",
            list(layers),
            net_ids,
        )
        arcs: dict = {}
        for row in rows:
            if row["kind"] == "arc" and row["source_id"] is not None:
                arcs.setdefault(row["source_id"], []).append(row)
                continue
            yield self._described(row)
        for chords in arcs.values():
            yield self._described(chords[len(chords) // 2])

    def _described(self, row):
        facts = self.index.object_facts(row)
        x = (row["x1"] + row["x2"]) / 2.0
        y = (row["y1"] + row["y2"]) / 2.0
        return row, facts, x, y

    def vias(self, net_ids: list | None):
        sql = (
            "SELECT p.*, n.name AS net_name FROM prims p "
            "LEFT JOIN nets n ON n.id = p.net_id WHERE p.kind = 'via'"
        )
        for row in self._rows(sql, [], net_ids):
            yield row, self.index.object_facts(row)

    def via_pad(self, row) -> tuple[float, int | None]:
        """smallest_via_pad of a via row over the copper layers of its span
        (no layers, so the nominal diameter, when an end is not in the stack,
        as in check_vias); the row's diameter and None when its Primitive is
        not found."""
        # PcbIndex inserts pcb.primitives in order, so prims row N holds
        # primitive N - 1.
        primitives = self.index.pcb.primitives
        number = row["id"] - 1
        prim = primitives[number] if 0 <= number < len(primitives) else None
        if (
            prim is None
            or prim.kind != "via"
            or (prim.x1, prim.y1) != (row["x1"], row["y1"])
        ):
            return row["width"], None
        if self._copper_position is None:
            self._copper_order = self.index._copper_layers_in_stack_order()
            self._copper_position = {
                layer: position for position, layer in enumerate(self._copper_order)
            }
        ends = [self._copper_position.get(layer) for layer in (prim.layer, prim.layer_to)]
        layers = []
        if None not in ends:
            low, high = sorted(ends)
            layers = self._copper_order[low : high + 1]
        return smallest_via_pad(prim, layers)

    # --- Width / DiffPairsRouting / RoutingLayers ---

    def width_limits(self, rule, layer_name: str) -> tuple:
        values = self.values(rule)
        entry = values.get("layers_mil", {}).get(layer_name)
        if entry is not None:
            return entry.get("min_width"), entry.get("max_width")
        if rule.kind == "DiffPairsRouting":
            return None, None
        return values.get("min_width_mil"), values.get("max_width_mil")

    def check_width(self, report, rule, row, net, layer_name, x, y, **flags):
        low, high = self.width_limits(rule, layer_name)
        if low is None and high is None:
            report.extra_count("objects_without_width_values")
        outside = _limit_check(row["width"], low, high)
        if outside is None:
            return
        side, margin = outside
        width = _round(row["width"])
        record = {"net": net, "rule": rule.name}
        record.update(flags)
        record.update(
            {
                "object": row["kind"],
                "layer": layer_name,
                "actual_mil": width,
                "min_mil": _round(low),
                "max_mil": _round(high),
                "side": side,
            }
        )
        _margin_fields(record, margin)
        record["x_mil"] = round(x, 3)
        record["y_mil"] = round(y, 3)
        key = (net, rule.name, layer_name, width, side, bool(flags))
        report.violation(rule, key, record)

    def run_routed(self, reports: dict, net_ids) -> None:
        width = reports.get("Width")
        pairs = reports.get("DiffPairsRouting")
        layers = reports.get("RoutingLayers")
        for row, facts, x, y in self.routed_objects(net_ids):
            net = row["net_name"]
            layer_name = self.layer_label(row["layer"])
            if facts.in_pour:
                # A hatched pour's tracks and arcs: Width rules on Altium's own
                # examples leave them out with Not InPolygon, so its Width
                # rule tests them (the docs do not say). They are not a pair's
                # routing, nor routing a layer must allow.
                if width is not None:
                    self._width_rule(width, row, facts, net, layer_name, x, y)
                continue
            if width is not None or pairs is not None:
                self._width_object(width, pairs, row, facts, net, layer_name, x, y)
            if layers is not None:
                rule, undecided = self.resolver("RoutingLayers", net).resolve(facts)
                if undecided:
                    layers.not_decided(net, undecided)
                    continue
                layers.checked(rule)
                if rule is None:
                    continue
                allowed = self.values(rule).get("routing_layers", {})
                if allowed.get(layer_name) is False:
                    record = {
                        "net": net,
                        "rule": rule.name,
                        "object": row["kind"],
                        "layer": layer_name,
                        "allowed_layers": [
                            name for name, enabled in allowed.items() if enabled
                        ],
                        "x_mil": round(x, 3),
                        "y_mil": round(y, 3),
                    }
                    layers.violation(rule, (net, rule.name, layer_name), record)

    def _width_object(self, width, pairs, row, facts, net, layer_name, x, y):
        pair = self.pair_of(net)
        if pair is not None:
            rule, undecided = self.resolver(
                "DiffPairsRouting", net, pair
            ).resolve(facts)
            # A pair net's tracks belong to DiffPairsRouting (check_compliance
            # adds that kind whenever Width is asked for pair nets); those with
            # no pair rule are its no-rule objects.
            if undecided:
                if pairs is not None:
                    pairs.not_decided(net, undecided)
                return
            if pairs is not None:
                pairs.checked(rule)
            if rule is not None:
                if pairs is not None:
                    self.check_width(
                        pairs, rule, row, net, layer_name, x, y, diff_pair=pair
                    )
                return
            if width is None:
                return
            # No pair rule applies: whether Altium then uses Width is not
            # confirmed by any DRC report.
            rule, undecided = self.resolver("Width", net).resolve(facts)
            if undecided:
                width.not_decided(net, undecided)
                return
            width.checked(rule)
            width.extra_count("unverified_fallback_objects")
            if rule is not None:
                self.check_width(
                    width, rule, row, net, layer_name, x, y,
                    diff_pair=pair, unverified_fallback=True,
                )
            return
        if width is None:
            return
        self._width_rule(width, row, facts, net, layer_name, x, y)

    def _width_rule(self, width, row, facts, net, layer_name, x, y):
        """Check one object against the Width rule that applies to it."""
        rule, undecided = self.resolver("Width", net).resolve(facts)
        if undecided:
            width.not_decided(net, undecided)
            return
        width.checked(rule)
        flags = {}
        if facts.in_pour:
            width.extra_count("pour_copper_objects")
            flags["pour_copper"] = True
        if rule is not None:
            self.check_width(width, rule, row, net, layer_name, x, y, **flags)

    # --- RoutingVias / HoleSize / MinimumAnnularRing ---

    def run_vias(self, reports: dict, net_ids) -> None:
        for row, facts in self.vias(net_ids):
            net = row["net_name"]
            span = (
                f"{self.layer_label(row['layer'])} -> "
                f"{self.layer_label(row['layer_to'])}"
            )
            base = {
                "net": net,
                "object": "via",
                "span": span,
                "x_mil": round(row["x1"], 3),
                "y_mil": round(row["y1"], 3),
            }
            for kind, report in reports.items():
                rule, undecided = self.resolver(kind, net).resolve(facts)
                if undecided:
                    report.not_decided(net, undecided)
                    continue
                report.checked(rule)
                if rule is None:
                    continue
                checker = {
                    "RoutingVias": self._via_size,
                    "HoleSize": self._via_hole,
                    "MinimumAnnularRing": self._via_ring,
                }[kind]
                checker(report, rule, row, base)

    def _via_size(self, report, rule, row, base) -> None:
        values = self.values(rule)
        diameter, hole = row["width"], row["hole"] or 0.0
        problems = []
        margins = []
        for name, actual, low, high in (
            ("diameter", diameter, values.get("min_diameter_mil"),
             values.get("max_diameter_mil")),
            ("hole", hole, values.get("min_hole_mil"), values.get("max_hole_mil")),
        ):
            outside = _limit_check(actual, low, high)
            if outside is not None:
                problems.append(f"{name}_{outside[0]}")
                margins.append(outside[1])
        if not problems:
            return
        record = {"net": base["net"], "rule": rule.name}
        record.update(
            {
                "object": "via",
                "span": base["span"],
                "diameter_mil": _round(diameter),
                "hole_mil": _round(hole),
                "min_diameter_mil": _round(values.get("min_diameter_mil")),
                "max_diameter_mil": _round(values.get("max_diameter_mil")),
                "min_hole_mil": _round(values.get("min_hole_mil")),
                "max_hole_mil": _round(values.get("max_hole_mil")),
                "problems": problems,
            }
        )
        _margin_fields(record, max(margins))
        record["x_mil"] = base["x_mil"]
        record["y_mil"] = base["y_mil"]
        key = (base["net"], rule.name, _round(diameter), _round(hole))
        report.violation(rule, key, record)

    def _via_hole(self, report, rule, row, base) -> None:
        values = self.values(rule)
        diameter, hole = row["width"], row["hole"] or 0.0
        if values.get("absolute_values", True):
            mode = "absolute"
            low, high = values.get("min_hole_mil"), values.get("max_hole_mil")
        else:
            mode = "percent_of_diameter"
            low_pct, high_pct = values.get("min_percent"), values.get("max_percent")
            low = diameter * low_pct / 100.0 if low_pct is not None else None
            high = diameter * high_pct / 100.0 if high_pct is not None else None
        outside = _limit_check(hole, low, high)
        if outside is None:
            return
        side, margin = outside
        record = {"net": base["net"], "rule": rule.name}
        record.update(
            {
                "object": "via",
                "span": base["span"],
                "actual_mil": _round(hole),
                "diameter_mil": _round(diameter),
                "min_mil": _round(low),
                "max_mil": _round(high),
                "mode": mode,
                "side": side,
            }
        )
        _margin_fields(record, margin)
        record["x_mil"] = base["x_mil"]
        record["y_mil"] = base["y_mil"]
        key = (base["net"], rule.name, _round(hole), _round(diameter), side)
        report.violation(rule, key, record)

    def _via_ring(self, report, rule, row, base) -> None:
        """The smallest ring over the span's layers that keep a pad; a
        top-middle-bottom or full-stack via names the layer and its pad."""
        minimum = self.values(rule).get("min_ring_mil")
        diameter, hole = row["width"], row["hole"] or 0.0
        pad, layer = self.via_pad(row)
        ring = (pad - hole) / 2.0
        outside = _limit_check(ring, minimum, None)
        if outside is None:
            return
        layer_name = self.layer_label(layer) if layer is not None else None
        record = {"net": base["net"], "rule": rule.name}
        record.update(
            {
                "object": "via",
                "span": base["span"],
                "actual_mil": _round(ring),
                "diameter_mil": _round(diameter),
                "hole_mil": _round(hole),
                "min_mil": _round(minimum),
                "side": outside[0],
            }
        )
        if layer_name is not None:
            record["layer"] = layer_name
            record["pad_diameter_mil"] = _round(pad)
        _margin_fields(record, outside[1])
        record["x_mil"] = base["x_mil"]
        record["y_mil"] = base["y_mil"]
        key = (base["net"], rule.name, _round(ring), layer_name)
        report.violation(rule, key, record)

    # --- Length ---

    def run_length(self, report: _KindReport, nets: list[str], length_of) -> None:
        report.approximate = True
        report.extra["counts"] = "nets"
        rules = sorted(self.enabled("Length"), key=lambda rule: rule.priority)
        if not rules:
            return
        measures: dict[str, int] = {}
        for net in nets:
            target = self.target(net)
            rule, undecided = None, []
            for candidate in rules:
                matches = evaluate_scope(candidate.scope1, target, self.ctx)
                if matches["matches"] == "yes":
                    rule = candidate
                    break
                if matches["matches"] != "no":
                    # Conditional, or a scope that cannot be parsed: Altium
                    # may apply this rule, so a lower one is not certain.
                    undecided.append(_undecided_entry(candidate, matches))
            if rule is None and not undecided:
                continue  # nets without a Length rule are not counted
            if undecided:
                report.not_decided(net, undecided)
                continue
            report.checked(rule)
            values = self.values(rule)
            if values.get("use_delay_units"):
                report.extra_count("not_evaluated_delay_units")
                continue
            measured = length_of(net) or {}
            length = measured.get("length_mil")
            status = measured.get("status", "ok")
            measure = measured.get("measure")
            if measure:
                measures[measure] = measures.get(measure, 0) + 1
            if length is None or status != "ok":
                report.extra_count("unmeasured")
                continue
            low, high = values.get("min_length_mil"), values.get("max_length_mil")
            outside = _limit_check(length, low, high)
            if outside is None:
                continue
            side, margin = outside
            record = {
                "net": net,
                "rule": rule.name,
                "actual_mil": round(length, 1),
                "min_mil": _round(low),
                "max_mil": _round(high),
                "side": side,
            }
            if measure:
                record["measure"] = measure
            _margin_fields(record, margin)
            report.violation(rule, (net, rule.name), record)
        if measures:
            report.extra["measures"] = measures

    # --- Clearance spot-check ---

    def run_clearance(self, report: _KindReport, nets: list[str]) -> None:
        report.approximate = True
        report.extra["counts"] = "object pairs within the search distance"
        index = self.index
        rules = sorted(self.enabled("Clearance"), key=lambda rule: rule.priority)
        if not rules:
            return
        values = [self.values(rule) for rule in rules]
        reach = max(
            [value.get("clearance_mil") or 0.0 for value in values]
            + [
                max(value.get("object_clearances_mil", {}).values(), default=0.0)
                for value in values
            ]
        )
        net_ids = [index.net_id(net) for net in nets]
        rows = self._rows(
            "SELECT p.*, n.name AS net_name FROM prims p "
            "JOIN nets n ON n.id = p.net_id "
            "WHERE p.kind IN ('track','arc','via','pad') AND p.pour_id IS NULL",
            [],
            [net_id for net_id in net_ids if net_id is not None],
        )
        # An arc (all its chords) is one object, as for Width.
        chords_of: dict[tuple, list] = {}
        for row in rows:
            chords_of.setdefault(object_id(row), []).append(row)
        if len(chords_of) > CLEARANCE_MAX_OBJECTS:
            report.extra["truncated_objects"] = len(chords_of) - CLEARANCE_MAX_OBJECTS
            chords_of = dict(list(chords_of.items())[:CLEARANCE_MAX_OBJECTS])
        layer_cache: dict[int, frozenset] = {}

        def layers_of(row):
            key = row["id"]
            if key not in layer_cache:
                layer_cache[key] = frozenset(index._primitive_layers(row))
            return layer_cache[key]

        # Pass 1: the closest chord pair of every object pair that shares a
        # layer within the search distance (ends keep the first orientation).
        closest: dict[tuple, list] = {}
        seen: set = set()
        for chords in chords_of.values():
            for row in chords:
                layers = layers_of(row)
                for candidate in self._clearance_candidates(row, reach):
                    chord_key = (
                        min(row["id"], candidate["id"]),
                        max(row["id"], candidate["id"]),
                    )
                    if chord_key in seen:
                        continue
                    seen.add(chord_key)
                    shared = layers & layers_of(candidate)
                    if not shared:
                        continue
                    # Measured on the shared layers (a multi-layer pad's
                    # copper can differ per layer); shared keeps the ones
                    # the gap is smallest on.
                    distance, at = index._closest_layers(row, candidate, shared)
                    if distance >= reach:
                        continue
                    shared = frozenset(at)
                    ids = (object_id(row), object_id(candidate))
                    pair_key = tuple(sorted(ids))
                    best = closest.get(pair_key)
                    if best is None:
                        closest[pair_key] = [distance, row, candidate, shared, ids]
                    elif distance < best[0]:
                        ends = (row, candidate) if ids[0] == best[4][0] else (
                            candidate, row
                        )
                        best[:4] = [distance, *ends, shared]

        # Pass 2: one rule resolution and one check per object pair.
        facts_cache: dict[tuple, object] = {}
        verdicts: dict[tuple, str] = {}
        net_level: dict[tuple, str] = {}
        per_object = [
            (_per_object_scope(rule.scope1), _per_object_scope(rule.scope2))
            for rule in rules
        ]

        def facts_of(row):
            # Scope facts of an arc come from its middle chord, as for Width.
            key = object_id(row)
            if key not in facts_cache:
                chords = chords_of.get(key) or self._arc_chords(row)
                facts_cache[key] = index.object_facts(chords[len(chords) // 2])
            return facts_cache[key]

        for distance, row, candidate, shared, _ids in closest.values():
            ends = (row, candidate)
            targets = (
                self.target(row["net_name"]),
                self.target(candidate["net_name"]),
            )

            def side(position, which, endpoint, ends=ends, targets=targets):
                rule = rules[position]
                scope = rule.scope1 if which == 1 else rule.scope2
                if not per_object[position][which - 1]:
                    net_key = (position, which, ends[endpoint]["net_id"])
                    if net_key not in net_level:
                        net_level[net_key] = evaluate_scope(
                            scope, targets[endpoint], self.ctx
                        )["matches"]
                    if net_level[net_key] in ("yes", "no"):
                        return net_level[net_key]
                object_key = (position, which, object_id(ends[endpoint]))
                if object_key not in verdicts:
                    verdicts[object_key] = evaluate_scope(
                        scope,
                        replace(targets[endpoint], obj=facts_of(ends[endpoint])),
                        self.ctx,
                    )["matches"]
                return verdicts[object_key]

            rule, undecided = _resolve_binary(
                rules,
                (
                    replace(targets[0], obj=facts_of(row)),
                    replace(targets[1], obj=facts_of(candidate)),
                ),
                self.ctx,
                side,
            )
            if undecided:
                report.objects += 1
                report.undecided += 1
                if len(report.undecided_examples) < UNDECIDED_EXAMPLES:
                    report.undecided_examples.append(
                        {
                            "net": row["net_name"],
                            "other_net": candidate["net_name"],
                            "rules": undecided,
                        }
                    )
                continue
            report.checked(rule)
            if rule is None:
                continue
            value = values[rules.index(rule)]
            if (
                value.get("ignore_pad_to_pad_in_footprint")
                and row["kind"] == "pad"
                and candidate["kind"] == "pad"
                and row["component"] is not None
                and row["component"] == candidate["component"]
            ):
                report.extra_count("pad_pairs_in_footprint_ignored")
                continue
            kinds = (_clearance_kind(row), _clearance_kind(candidate))
            matrix = value.get("object_clearances_mil", {})
            required = matrix.get(
                f"{kinds[0]}-{kinds[1]}",
                matrix.get(f"{kinds[1]}-{kinds[0]}", value.get("clearance_mil")),
            )
            if required is None or distance >= required - CLEARANCE_TOLERANCE_MIL:
                continue
            layer_name = (
                self.layer_label(next(iter(shared)))
                if len(shared) == 1
                else "Multi-Layer"
            )
            record = {
                "net": row["net_name"],
                "other_net": candidate["net_name"],
                "rule": rule.name,
                "objects": "-".join(kinds),
                "layer": layer_name,
                "actual_mil": round(distance, 4),
                "required_mil": _round(required),
            }
            _margin_fields(record, required - distance)
            # Located at the closest chord (or the via / pad centre).
            x, y = _anchor(row)
            record["x_mil"] = round(x, 3)
            record["y_mil"] = round(y, 3)
            key = (
                row["net_name"],
                candidate["net_name"],
                rule.name,
                layer_name,
                tuple(sorted(kinds)),
            )
            report.violation(
                rule,
                key,
                record,
                worst=lambda old, new: new["actual_mil"] < old["actual_mil"],
            )
        report.extra["target_objects"] = len(chords_of)
        report.extra["search_distance_mil"] = round(reach, 4)

    def _arc_chords(self, row) -> list:
        """Every chord of the arc a chord row belongs to, in id order."""
        if row["kind"] != "arc" or row["source_id"] is None:
            return [row]
        return self.index.db.execute(
            "SELECT p.*, n.name AS net_name FROM prims p "
            "LEFT JOIN nets n ON n.id = p.net_id "
            "WHERE p.net_id = ? AND p.kind = 'arc' AND p.source_id = ? "
            "AND p.pour_id IS NULL "
            "ORDER BY p.id",
            (row["net_id"], row["source_id"]),
        ).fetchall() or [row]

    def _clearance_candidates(self, row, reach: float):
        index = self.index
        box = (
            row["minx"] - reach,
            row["maxx"] + reach,
            row["miny"] - reach,
            row["maxy"] + reach,
        )
        if getattr(index, "_has_rtree", False):
            sql = (
                "SELECT p.*, n.name AS net_name FROM prims p "
                "JOIN prims_rtree r ON r.id = p.id "
                "JOIN nets n ON n.id = p.net_id "
                "WHERE p.net_id != ? AND p.pour_id IS NULL "
                "AND p.kind IN ('track','arc','via','pad') "
                "AND r.maxx >= ? AND r.minx <= ? AND r.maxy >= ? AND r.miny <= ?"
            )
        else:
            sql = (
                "SELECT p.*, n.name AS net_name FROM prims p "
                "JOIN nets n ON n.id = p.net_id "
                "WHERE p.net_id != ? AND p.pour_id IS NULL "
                "AND p.kind IN ('track','arc','via','pad') "
                "AND p.maxx >= ? AND p.minx <= ? AND p.maxy >= ? AND p.miny <= ?"
            )
        return index.db.execute(sql, (row["net_id"], *box)).fetchall()


def object_id(row) -> tuple:
    """Identity of the copper object a prims row belongs to: an arc's chords
    share their source_id (tagged so it cannot clash with a primitive id)."""
    if row["kind"] == "arc" and row["source_id"] is not None:
        return ("arc", row["source_id"])
    return ("prim", row["id"])


def _anchor(row) -> tuple:
    """Location of a row: a via or pad centre, a track or chord midpoint."""
    if row["kind"] in ("via", "pad"):
        return row["x1"], row["y1"]
    return (row["x1"] + row["x2"]) / 2.0, (row["y1"] + row["y2"]) / 2.0


def _clearance_kind(row) -> str:
    """Object-matrix name of a primitive (ClearanceObj_<name>); a hatched
    pour's tracks and arcs are its polygon (Poly)."""
    if row["kind"] == "pad":
        return "THPad" if (row["hole"] or 0.0) > 0 else "SMDPad"
    if row["poured_by"] is not None:
        return "Poly"
    return {"track": "Track", "arc": "Arc", "via": "Via"}[row["kind"]]


# ---------- entry point ----------


def _select_kinds(checker: _Checker, kinds, clearance: bool):
    lookup = {kind.lower(): kind for kind in SUPPORTED_KINDS}
    if kinds:
        unknown = [kind for kind in kinds if kind.lower() not in lookup]
        if unknown:
            return None, [], {
                "error": "invalid_kind",
                "message": f"Cannot check rule kind(s) {unknown}.",
                "supported_kinds": list(SUPPORTED_KINDS),
            }
        selected = list(dict.fromkeys(lookup[kind.lower()] for kind in kinds))
        return selected, [], None
    selected = []
    without_rules = []
    for kind in SUPPORTED_KINDS:
        if kind == "Clearance" and not clearance:
            continue
        if checker.enabled(kind):
            selected.append(kind)
        else:
            without_rules.append(kind)
    return selected, without_rules, None


def _select_nets(index, nets):
    """(net names | None for all, unknown names, expanded pairs)."""
    if nets is None:
        return None, [], []
    ctx = index.rule_context
    selected: list[str] = []
    unknown: list[str] = []
    expanded: list[str] = []
    for name in nets:
        key = name.lower()
        if key in ctx.net_names:
            selected.append(ctx.net_names[key])
        elif key in ctx.pairs:
            display, positive, negative = ctx.pairs[key]
            expanded.append(display)
            selected.extend(
                ctx.net_names.get(net.lower(), net) for net in (positive, negative)
            )
        else:
            unknown.append(name)
    return list(dict.fromkeys(selected)), unknown, expanded


def _default_length(index):
    def measure(net: str) -> dict:
        length = index.routed_length(net)
        return {
            "length_mil": length,
            "status": "ok" if length else "unrouted",
            "measure": "routed_copper",
        }

    return measure


def _selects_pair_nets(index, checker: _Checker, names) -> bool:
    if names is None:
        return bool(index.rule_context.pairs)
    return any(checker.pair_of(net) is not None for net in names)


def check_compliance(
    index,
    nets: list[str] | None = None,
    kinds: list[str] | None = None,
    length_of=None,
    clearance: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """Check the copper of the selected nets against the board's rules.

    nets: net or differential-pair names (a pair checks both nets); None
    checks every net. kinds: subset of SUPPORTED_KINDS (case-insensitive);
    None runs every supported kind the board has an enabled rule for
    (Clearance only with clearance=True). Width never covers diff-pair member
    tracks: when Width is asked for pair nets, DiffPairsRouting is added
    (kinds_added). length_of(net) -> {"length_mil", "status" ("ok" when
    measured), "measure"} measures a net for Length rules; by default the
    net's routed track/arc length. clearance=True (or kinds=['Clearance'])
    runs an object-pair clearance spot-check for at most CLEARANCE_MAX_NETS
    nets. limit and offset page the violation groups of every kind (has_more,
    next_offset per kind).

    Returns {"nets", "kinds": [per-kind report], "violation_count",
    "not_checked", "approximations", ...} or {"error", "message", ...}.
    Hints in the output name the MCP tool check_rule_compliance and its
    parameters (net, kinds, clearance_spot_check, offset).
    """
    checker = _Checker(index, max(1, limit))
    offset = max(0, offset)
    selected_kinds, without_rules, error = _select_kinds(checker, kinds, clearance)
    if error:
        return error
    names, unknown, expanded = _select_nets(index, nets)
    if nets is not None and not nets:
        return {
            "error": "net_not_found",
            "message": "No net names given; omit net to check every net.",
        }
    if nets is not None and not names:
        suggestions = [
            net for net in index.pcb.nets
            if any(name.lower() in net.lower() for name in nets)
        ][:10]
        return {
            "error": "net_not_found",
            "message": f"No PCB net or differential pair named {unknown}.",
            "suggestions": suggestions,
        }
    kinds_added: list[dict] = []
    if (
        "Width" in selected_kinds
        and "DiffPairsRouting" not in selected_kinds
        and checker.enabled("DiffPairsRouting")
        and _selects_pair_nets(index, checker, names)
    ):
        selected_kinds.insert(selected_kinds.index("Width") + 1, "DiffPairsRouting")
        kinds_added.append({"kind": "DiffPairsRouting", "reason": _PAIRS_ADDED})
    run_clearance = "Clearance" in selected_kinds
    net_ids = (
        None
        if names is None
        else [net_id for net_id in (index.net_id(net) for net in names) if net_id is not None]
    )
    reports = {kind: _KindReport(kind) for kind in selected_kinds}
    if "DiffPairsRouting" in reports:
        reports["DiffPairsRouting"].extra["checks"] = ["width"]

    routed = {kind: reports[kind] for kind in _TRACK_KINDS if kind in reports}
    if routed:
        checker.run_routed(routed, net_ids)
    via_reports = {kind: reports[kind] for kind in _VIA_KINDS if kind in reports}
    if via_reports:
        checker.run_vias(via_reports, net_ids)
    if "Length" in reports:
        checker.run_length(
            reports["Length"],
            names if names is not None else list(index.pcb.nets),
            length_of or _default_length(index),
        )
    not_checked = [dict(entry) for entry in _NOT_CHECKED]
    if "DiffPairsRouting" in reports:
        not_checked.append(dict(_UNCOUPLED_NOT_CHECKED))
    if run_clearance:
        report = reports["Clearance"]
        if names is None or len(names) > CLEARANCE_MAX_NETS:
            report.skipped = (
                "The clearance spot-check needs a small net selection: "
                f"{_CLEARANCE_HINT}."
            )
        else:
            checker.run_clearance(report, names)
    else:
        not_checked.append(
            {"kind": "Clearance", "reason": f"Opt-in spot-check: {_CLEARANCE_HINT}."}
        )
    if names is None and routed:
        no_net = _no_net_routing(index, checker.signal)
        if no_net:
            not_checked.append(
                {
                    "kind": "Width / RoutingLayers (no-net copper)",
                    "objects": no_net,
                    "reason": (
                        "No-net tracks and arcs are skipped: they have no net to "
                        "resolve a rule for (keepouts on signal layers are not "
                        "read as copper at all). Hatched-pour tracks and arcs "
                        "are not among them: they take their pour's net."
                    ),
                }
            )

    rendered = [
        reports[kind].render(checker.batch_drc(kind), checker.limit, offset)
        for kind in selected_kinds
    ]
    result: dict = {"nets": "all" if names is None else names}
    if expanded:
        result["expanded_pairs"] = expanded
    if unknown:
        result["unknown_nets"] = unknown
    result["violation_count"] = sum(
        entry.get("violation_count", 0) for entry in rendered
    )
    result["kinds"] = rendered
    if kinds_added:
        result["kinds_added"] = kinds_added
    if without_rules:
        result["kinds_without_rules"] = without_rules
    if any(entry.get("has_more") for entry in rendered):
        result["next_page"] = (
            "Kinds with has_more true have more violation groups: repeat the "
            f"{_TOOL} call with kinds=[<kind>] and offset=<that kind's "
            "next_offset>."
        )
    result["not_checked"] = not_checked
    result["approximations"] = _approximations(
        checker, selected_kinds, reports, length_of is None
    )
    return result


def _no_net_routing(index, signal: dict) -> int:
    layers = sorted(signal)
    if not layers:
        return 0
    row = index.db.execute(
        "SELECT SUM(kind = 'track') tracks, "
        "COUNT(DISTINCT CASE WHEN kind = 'arc' THEN source_id END) arcs "
        "FROM prims WHERE net_id IS NULL AND pour_id IS NULL "
        "AND poured_by IS NULL "
        f"AND kind IN ('track','arc') AND layer IN ({','.join('?' * len(layers))})",
        layers,
    ).fetchone()
    return (row["tracks"] or 0) + (row["arcs"] or 0)


def _scopes_mention(checker: _Checker, kinds: list[str], terms: tuple) -> bool:
    for kind in kinds:
        for rule in checker.enabled(kind):
            scopes = (rule.scope1 + " " + rule.scope2).lower()
            if any(term in scopes for term in terms):
                return True
    return False


def _approximations(
    checker: _Checker, kinds: list[str], reports: dict, default_length: bool
) -> list[str]:
    notes: list[str] = []
    object_kinds = [kind for kind in kinds if kind not in ("Length",)]
    if _scopes_mention(
        checker, object_kinds, ("withinroom", "touchesroom", "inroom", "inregion")
    ):
        notes.append(
            "WithinRoom/InRegion for a track or arc is decided by its centre "
            "point (an arc by its middle chord); objects straddling a room "
            "boundary may resolve to another rule than in Altium. Rooms apply "
            "on every layer, whatever their side."
        )
    if _scopes_mention(checker, object_kinds, ("incomponentclass",)):
        notes.append(
            "InComponentClass on tracks, arcs and vias is assumed to match "
            "objects of the class's components."
        )
    if "Width" in kinds:
        connected = [
            rule.name
            for rule in checker.enabled("Width")
            if rule.properties.get("CHECKCONNECTEDCOPPER", "").upper() == "TRUE"
        ]
        if connected:
            notes.append(
                "Width rules checking physically connected copper are tested "
                f"per segment: {', '.join(connected)}."
            )
        if reports["Width"].extra.get("pour_copper_objects"):
            notes.append(
                "A hatched pour's tracks and arcs are checked against Width "
                "(pour_copper_objects; their violations say pour_copper), as "
                "Width rules scoped Not InPolygon on Altium's examples imply "
                "Altium does (its docs do not say); InPolygon scopes select "
                "them. Solid pour copper (regions) is not read."
            )
        if reports["Width"].extra.get("unverified_fallback_objects"):
            notes.append(
                "Pair-member tracks with no applicable DiffPairsRouting rule are "
                "checked against Width (unverified_fallback): no DRC report "
                "confirms that Altium does so."
            )
    if any(kind in kinds for kind in ("Width", "DiffPairsRouting")) and any(
        "substack_layers_mil" in checker.values(rule)
        for kind in ("Width", "DiffPairsRouting")
        for rule in checker.enabled(kind)
    ):
        notes.append(
            "Rigid-flex substack per-layer widths are not applied; the primary "
            "stack's per-layer values are used."
        )
    if any(kind in kinds for kind in _VIA_KINDS):
        notes.append(
            "Vias are checked with their main diameter; per-layer (full-stack) "
            "via diameters are not parsed."
        )
    if "Length" in kinds:
        notes.append(
            (
                "Length is approximate: the measure is the net's routed track and "
                "arc length, without via barrels or pin/package length."
                if default_length
                else "Length is approximate: two-pad nets use the pad-to-pad copper "
                "path plus via barrels and pin/package length (Altium's Signal "
                "Length); other nets use routed copper plus via barrels (Altium's "
                "Routed Length, no pin/package term)."
            )
        )
    if "Clearance" in kinds:
        notes.append(
            "Clearance spot-check: tracks, arcs, vias and pads of nets only, "
            "a hatched pour's tracks and arcs as Poly; solid pours, fills, "
            "regions, text, holes, keepouts and no-net copper are "
            "not modelled; octagonal pads are rectangles and through-hole pads "
            "have each layer's size and shape (top-middle-bottom and full "
            "stacks); gaps within "
            f"{CLEARANCE_TOLERANCE_MIL} mil of the limit are not reported. The "
            "object matrix (Track/Arc/Via/SMDPad/THPad/Poly) is applied per object "
            "pair; counts are object pairs."
        )
    return notes
