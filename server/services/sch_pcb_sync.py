"""Schematic-vs-PCB synchronisation check: an offline Project > Show Differences.

The schematic side is the compiled netlist: the Protel .NET part list (every compiled
part with its footprint and comment; compile-masked parts are not in it) and nets,
enriched from the sheets with each part's unique IDs, footprint models, kind and pin
designators. The PCB side is the saved PcbDoc. Matching follows Altium's comparator:
components link by unique ID (the full path of sheet-symbol IDs where the .Annotation
file gives it, as for channel copies), and by designator only when no unique ID links
them (the 'Failed to Match Unique Identifiers' case); nets match by shared members
first (Min Match %, Min Matched Members) and by name second. A pin whose net changed is
one finding here, where Altium lists it under Extra Pins in Nets on both sides.
"""

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

MECHANICAL, GRAPHICAL, JUMPER = 1, 2, 6
KIND_NAMES = {
    0: "standard", 1: "mechanical", 2: "graphical", 3: "net tie (in BOM)",
    4: "net tie", 5: "standard (no BOM)", 6: "jumper",
}

# Each check and the Altium Comparator difference (or ECO message) it corresponds to.
ALTIUM_NAMES = {
    "unknown_pin": "ECO error 'Unknown Pin' (Add Pins To Nets)",
    "pad_net": "Extra Pins in Nets",
    "missing_from_pcb": "Extra Components (schematic side)",
    "extra_on_pcb": "Extra Components (PCB side)",
    "designator": "Different Designators",
    "footprint": "Different Footprints",
    "comment": "Different Comments",
    "net_only_in_schematic": "Extra Nets (schematic side)",
    "net_only_on_pcb": "Extra Nets (PCB side)",
    "net_name": "Changed Net Names",
    "component_link": "Failed to Match Unique Identifiers (matched by designator)",
}
CHECK_GROUPS = {
    "components": (
        "missing_from_pcb", "extra_on_pcb", "designator", "footprint", "comment",
        "component_link",
    ),
    "pins": ("unknown_pin", "pad_net"),
    "nets": ("net_only_in_schematic", "net_only_on_pcb", "net_name"),
}
_CHECK_RANK = {check: rank for rank, check in enumerate(ALTIUM_NAMES)}
_SEVERITY_RANK = {"error": 0, "warning": 1, "info": 2}
DEFAULT_LIMIT = 100

# Altium's installation defaults for net matching (Project Options > Comparator).
DEFAULT_NET_MATCHING = {
    "min_match_percent": 75,
    "min_matched_members": 3,
    "name_matching": True,
    "source": "Altium defaults",
}
_UNNAMED_NET = re.compile(r"^__unnamed_\d+$")
# Altium names an unnamed net Net<designator>_<pin>: the name follows the designator.
_AUTO_NET = re.compile(r"^Net[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9]+$")

NOT_COMPARED = (
    "Parameters, net classes, component classes, rooms, rules and differential pairs "
    "are not compared (see get_classes, list_diff_pairs, get_design_rules)."
)
PIN_MAP_NOTE = (
    "Symbol pin maps (a pin mapped to a differently named pad) are not read: such a "
    "pin shows as an unknown pin. Pin and pad designators match exactly, else "
    "case-insensitively."
)
IDENTICAL_NETS_NOTE = (
    "Nets whose members are identical are matched even below Min Matched Members, and "
    "a different name is reported as a renamed net; Altium lists such small nets as an "
    "extra net on each side, and Update PCB has the same effect."
)


@dataclass
class _Part:
    """One compiled schematic part."""

    name: str
    footprint: str | None = None
    comment: str | None = None
    unique_ids: list[str] = field(default_factory=list)
    footprints: list[str] = field(default_factory=list)
    kind: int = 0
    sheet: str | None = None
    logical: str | None = None
    pin_designators: list[str] = field(default_factory=list)
    # Without the compiled part list: each unique ID's footprint from the sheets.
    footprint_by_uid: dict[str, str] = field(default_factory=dict)
    # Full UniqueId paths from the .Annotation file (channel copies differ only here).
    unique_paths: list[str] = field(default_factory=list)
    # False when no sheet gave this part metadata (e.g. its sheet failed to parse).
    has_metadata: bool = True


def net_matching_options(prj_pcb_path: str | None) -> dict:
    """The project's Net row of [Comparison Options], else Altium's defaults.

    A row reads 'Kind=Net|MinPercent=75|MinMatch=3|...|UseName=-1'. The keys are not
    documented; they line up with Altium's Comparator tab (UseName -1 = match names
    after members, 0 = never).
    """
    options = dict(DEFAULT_NET_MATCHING)
    if not prj_pcb_path:
        return options
    try:
        text = Path(prj_pcb_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return options
    for line in text.splitlines():
        if not line.startswith("ComparisonOptions") or "Kind=Net|" not in line:
            continue
        fields = dict(
            item.split("=", 1) for item in line.split("=", 1)[1].split("|") if "=" in item
        )
        if fields.get("Kind") != "Net":
            continue
        try:
            options["min_match_percent"] = int(fields.get("MinPercent", 75))
            options["min_matched_members"] = int(fields.get("MinMatch", 3))
        except ValueError:
            return dict(DEFAULT_NET_MATCHING)
        options["name_matching"] = fields.get("UseName", "-1") != "0"
        options["source"] = "project options"
        break
    return options


def _schematic_parts(netlist: dict) -> tuple[dict[str, _Part], bool]:
    """Compiled parts by netlist designator, and whether the .NET part list was read.

    Without it (an older netlist), the parts are the enriched components plus every
    designator in the nets; footprints then come from the sheets and comments are
    not known.
    """
    compiled = netlist.get("parts") or {}
    components = netlist.get("components") or {}
    if compiled:
        names = list(compiled)
    else:
        names = list(dict.fromkeys([*components, *(netlist.get("pin_to_net") or {})]))
    parts: dict[str, _Part] = {}
    for name in names:
        meta = components.get(name) or {}
        unique_ids = list(meta.get("unique_ids") or [])
        if meta.get("unique_id") and meta["unique_id"] not in unique_ids:
            unique_ids.append(meta["unique_id"])
        footprints = list(meta.get("footprints") or [])
        if compiled:
            footprint = compiled[name].get("footprint") or None
            comment = compiled[name].get("comment", "")
        else:
            footprint = meta.get("footprint") or (
                footprints[0] if len(footprints) == 1 else None
            )
            comment = None
        parts[name] = _Part(
            name=name,
            footprint=footprint,
            comment=comment,
            unique_ids=unique_ids,
            footprints=footprints,
            kind=int(meta.get("kind") or 0),
            sheet=meta.get("sheet"),
            logical=meta.get("logical_refdes"),
            pin_designators=list(meta.get("pin_designators") or []),
            footprint_by_uid={} if compiled else dict(meta.get("footprint_by_uid") or {}),
            unique_paths=list(meta.get("unique_paths") or []),
            has_metadata=bool(meta),
        )
    return parts, bool(compiled)


class _Board:
    """The PCB side: components, their pads by name, and net members."""

    def __init__(self, index):
        self.index = index
        self.components = {component.index: component for component in index.pcb.components}
        self.pads: dict[int, dict[str, set]] = defaultdict(lambda: defaultdict(set))
        # Every component pad on each net, linked or not: (component, pad name).
        self.net_members: dict[str, set[tuple[int, str]]] = defaultdict(set)
        rows = index.db.execute(
            "SELECT component, pad_name, net_id FROM prims "
            "WHERE kind = 'pad' AND component IS NOT NULL"
        ).fetchall()
        for row in rows:
            pad = row["pad_name"]
            if not pad:
                continue
            net = index.pcb.nets[row["net_id"]] if row["net_id"] is not None else None
            self.pads[row["component"]][pad].add(net)
            if net is not None:
                self.net_members[net].add((row["component"], pad))

    def designator(self, component) -> str:
        return component.designator or component.refdes or f"#{component.index}"

    def signature(self, component) -> dict[str, str]:
        """Pad name -> net for the component's connected pads (one net each)."""
        return {
            pad: next(iter(nets))
            for pad, nets in self.pads.get(component.index, {}).items()
            if len(nets) == 1 and None not in nets
        }

    def pad_for(self, component, pin: str) -> str | None:
        """The pad a schematic pin lands on: same designator, else the one pad whose
        designator differs only in case."""
        pads = self.pads.get(component.index, {})
        if pin in pads:
            return pin
        folded = [pad for pad in pads if pad.lower() == pin.lower()]
        return folded[0] if len(folded) == 1 else None


def _named(signature: dict[str, str]) -> dict[str, str]:
    """A pad-net signature without auto-named nets, whose names follow designators."""
    return {pad: net for pad, net in signature.items() if not _AUTO_NET.match(net)}


def _pin_exists(part: "_Part", pad: str) -> bool | None:
    """Whether the part's symbol has a pin for the pad; None when no sheet was read."""
    if not part.has_metadata:
        return None
    return any(_same(pad, pin) for pin in part.pin_designators)


def _same(a: str | None, b: str | None) -> bool:
    """Altium compares designators, footprints and comments case-insensitively."""
    return (a or "").strip().lower() == (b or "").strip().lower()


def _path_renames(parts: dict[str, _Part], board: _Board) -> dict[str, str]:
    """Sheet-symbol IDs renamed since the .Annotation file was written.

    A sheet symbol placed again gets a new unique ID, which Update PCB takes to the PCB
    while the .Annotation file keeps the old one. A part whose ID tail is on exactly
    one PCB part shows the rename segment by segment; it holds for every path under
    that sheet symbol, channel copies included. Contradicting renames are dropped.
    """
    components = [c for c in board.components.values() if c.source_unique_id]
    tails = Counter(c.source_uid_tail for c in components)
    single = {c.source_uid_tail: c.source_unique_id for c in components if tails[c.source_uid_tail] == 1}
    pcb_paths = {c.source_unique_id for c in components}
    renames: dict[str, str] = {}
    conflicts: set[str] = set()
    for part in parts.values():
        for path in part.unique_paths:
            if path in pcb_paths:
                continue
            old = path.split("\\")
            current = single.get(old[-1])
            if not current:
                continue
            new = current.split("\\")
            if len(new) != len(old):
                continue
            for before, after in zip(old[:-1], new[:-1]):
                if before != after:
                    if renames.get(before, after) != after:
                        conflicts.add(before)
                    renames[before] = after
    for segment in conflicts:
        renames.pop(segment, None)
    return renames


def _link(
    parts: dict[str, _Part], board: _Board, pin_to_net: dict
) -> tuple[dict[int, tuple], int]:
    """PCB component index -> (part name, how linked).

    By the full unique-ID path first, where the .Annotation file gives it: channel
    copies differ only there. Then by unique ID; channel copies (and the parts of a
    component placed on several sheets) share it, so a group with several parts or
    placements pairs by designator, then by pad-net signature (auto-named nets left
    out, as they follow designators), then copies with equal signatures in order (they
    are interchangeable). Within a group, a placement with the part's own footprint
    goes first, so a variant's alternate placement is not taken for the part. Components
    still unlinked pair by designator. Also returns how many .Annotation paths match
    no PCB part, after renamed sheet symbols are followed.
    """
    links: dict[int, tuple] = {}
    taken: set[str] = set()

    def link(component, name: str, how: str) -> None:
        links[component.index] = (name, how)
        taken.add(name)

    def own_footprint_first(components, names):
        return sorted(
            components,
            key=lambda c: not any(_same(c.footprint, parts[n].footprint) for n in names),
        )

    pcb_paths = {c.source_unique_id for c in board.components.values() if c.source_unique_id}
    renames = _path_renames(parts, board)
    by_path: dict[str, str] = {}
    for part in parts.values():
        for path in part.unique_paths:
            by_path.setdefault(path, part.name)
    unmatched_paths = 0
    for part in parts.values():
        for path in part.unique_paths:
            if path in pcb_paths:
                continue
            current = "\\".join(renames.get(s, s) for s in path.split("\\"))
            if current in pcb_paths:
                by_path.setdefault(current, part.name)
            else:
                unmatched_paths += 1
    by_path_components: dict[str, list] = defaultdict(list)
    for component in board.components.values():
        name = by_path.get(component.source_unique_id or "")
        if name:
            by_path_components[name].append(component)
    for name, components in by_path_components.items():
        # The placement showing the part's designator, then its footprint: a copy or a
        # variant's alternate carries the same path.
        best = min(components, key=lambda c: (
            not _same(board.designator(c), name),
            not _same(c.footprint, parts[name].footprint),
        ))
        link(best, name, "unique_id")

    by_uid: dict[str, list[str]] = defaultdict(list)
    for part in parts.values():
        for uid in part.unique_ids:
            by_uid[uid].append(part.name)
    groups: dict[str, list] = defaultdict(list)
    for component in board.components.values():
        tail = component.source_uid_tail
        if tail and tail in by_uid and component.index not in links:
            groups[tail].append(component)

    for uid, components in groups.items():
        names = [name for name in dict.fromkeys(by_uid[uid]) if name not in taken]
        if not names:
            continue
        ordered = own_footprint_first(components, names)
        if len(names) == 1 and len(ordered) == 1:
            link(ordered[0], names[0], "unique_id")
            continue
        for component in ordered:
            match = next(
                (n for n in names if n not in taken and _same(n, board.designator(component))),
                None,
            )
            if match:
                link(component, match, "unique_id")
        raw = {name: dict(pin_to_net.get(name) or {}) for name in names}
        named = {name: _named(signature) for name, signature in raw.items()}
        for component in ordered:
            if component.index in links:
                continue
            signature = _named(board.signature(component))
            matches = [
                n for n in names if n not in taken and signature and named[n] == signature
            ]
            if len(matches) == 1:
                link(component, matches[0], "unique_id")
        # Copies with identical connectivity (decoupling caps on the same rails) are
        # interchangeable: pair them in order.
        for component in ordered:
            if component.index in links:
                continue
            signature = board.signature(component)
            equal = [n for n in names if n not in taken and raw[n] == signature]
            # Per-channel values (a sheet-symbol parameter) still tell copies apart.
            equal.sort(key=lambda n: (
                not _same(parts[n].footprint, component.footprint),
                not (parts[n].comment is not None and _same(parts[n].comment, component.comment)),
            ))
            if equal:
                link(component, equal[0], "unique_id")
        left = [c for c in ordered if c.index not in links]
        free = [n for n in names if n not in taken]
        if len(left) == 1 and len(free) == 1:
            link(left[0], free[0], "unique_id")

    by_name = {name.lower(): name for name in parts}
    for component in board.components.values():
        if component.index in links:
            continue
        name = by_name.get(board.designator(component).lower())
        if name and name not in taken:
            link(component, name, "designator")
    return links, unmatched_paths


class _Report:
    def __init__(self):
        self.findings: list[dict] = []

    def add(self, check: str, severity: str, issue: str, **fields) -> None:
        self.findings.append({"check": check, "severity": severity, **fields, "issue": issue})


def _component_findings(
    report, parts, board, links, is_dnp, newer_sheets, sheet_errors: bool = False
) -> None:
    linked_parts = {name: index for index, (name, _) in links.items()}

    def sheet_fields(part: _Part) -> dict:
        fields: dict = {}
        if part.sheet:
            fields["sheet"] = part.sheet
            if part.sheet.lower() in newer_sheets:
                fields["sheet_saved_after_pcb"] = True
        return fields

    for part in parts.values():
        if part.name in linked_parts:
            continue
        fields = {"schematic": part.name, "footprint": part.footprint, **sheet_fields(part)}
        if is_dnp and is_dnp(part.name):
            fields["dnp"] = True
        if part.kind in (MECHANICAL, GRAPHICAL) or not part.footprint:
            reason = (
                f"a {KIND_NAMES[part.kind]} part" if part.kind in (MECHANICAL, GRAPHICAL)
                else "it has no footprint"
            )
            report.add(
                "missing_from_pcb", "info",
                f"{part.name} is not on the PCB; {reason}, so Update PCB may not place it.",
                **fields,
            )
            continue
        note = (
            " It is not fitted in the active variant, but Altium still places every "
            "variant's footprints." if fields.get("dnp") else ""
        )
        report.add(
            "missing_from_pcb", "warning",
            f"{part.name} is not on the PCB: Update PCB would add it.{note}",
            **fields,
        )

    linked_by_tail: dict[str, list] = defaultdict(list)
    for index, (name, _) in links.items():
        if board.components[index].source_uid_tail:
            linked_by_tail[board.components[index].source_uid_tail].append(
                (name, board.components[index])
            )
    all_uids = {uid for part in parts.values() for uid in part.unique_ids}
    for component in board.components.values():
        if component.index in links:
            continue
        designator = board.designator(component)
        fields = {"pcb": designator, "footprint": component.footprint}
        if component.kind != 0:
            fields["kind"] = KIND_NAMES.get(component.kind, str(component.kind))
        tail = component.source_uid_tail
        if component.kind == GRAPHICAL:
            report.add(
                "extra_on_pcb", "info",
                f"{designator} is a graphical part placed on the PCB only; Altium never "
                "synchronizes graphical parts.",
                **fields,
            )
        elif component.kind == MECHANICAL:
            report.add(
                "extra_on_pcb", "info",
                f"{designator} is a mechanical part on the PCB only; Altium adds or "
                "removes mechanical parts only when they are on both sides.",
                **fields,
            )
        elif tail and tail in linked_by_tail:
            twins = [
                (name, twin) for name, twin in linked_by_tail[tail]
                if not component.source_unique_id or not twin.source_unique_id
                or twin.source_unique_id == component.source_unique_id
            ]
            twins.sort(key=lambda pair: not _same(board.designator(pair[1]), designator))
            if not twins:
                name, twin = linked_by_tail[tail][0]
                report.add(
                    "extra_on_pcb", "warning",
                    f"{designator} is another channel's copy of {name} (on the PCB as "
                    f"{board.designator(twin)}), and that channel is no longer in the "
                    "compiled schematic: Update PCB would remove it.",
                    **fields,
                )
                continue
            name, twin = twins[0]
            twin_designator = board.designator(twin)
            if _same(twin_designator, designator) and not _same(
                twin.footprint, component.footprint
            ):
                report.add(
                    "extra_on_pcb", "info",
                    f"{designator} is placed twice, with footprints {twin.footprint} and "
                    f"{component.footprint}: a variant's alternate part, which Altium "
                    "places beside the main one.",
                    **fields,
                )
            else:
                report.add(
                    "extra_on_pcb", "warning",
                    f"{designator} is a second placement of {name} (on the PCB as "
                    f"{twin_designator}) with the same unique ID: a copied footprint; "
                    "Update PCB would remove it.",
                    **fields,
                )
        elif tail:
            gone = "" if tail in all_uids else " Its unique ID is in no compiled part."
            report.add(
                "extra_on_pcb", "warning",
                f"{designator} came from the schematic but its part is gone (deleted, "
                f"compile-masked or re-placed with a new ID): Update PCB would remove "
                f"it.{gone}",
                **fields,
            )
        else:
            report.add(
                "extra_on_pcb", "warning",
                f"{designator} was placed on the PCB only. Unless Remove Components is "
                "off in the project's ECO options, Update PCB would remove it; make it "
                "a Mechanical part to keep it.",
                **fields,
            )

    for index, (name, how) in links.items():
        component = board.components[index]
        part = parts[name]
        designator = board.designator(component)
        if how == "designator":
            if not part.has_metadata:
                detail = (
                    "no sheet gave the schematic part's unique ID (see sheet_errors)"
                    if sheet_errors
                    else "no sheet the loader read holds the schematic part (e.g. a sheet "
                    "missing from the saved .PrjPcb, or a channel name it does not map)"
                )
            elif not component.source_uid_tail:
                detail = "the PCB part has no schematic unique ID"
            else:
                detail = "their unique IDs differ"
            report.add(
                "component_link", "info",
                f"{designator} matched by designator: {detail}. Altium asks to match "
                "by designator (Failed to Match Unique Identifiers), or use Project > "
                "Component Links.",
                component=designator,
            )
        # Without a Texts6 designator the PCB offers only SOURCEDESIGNATOR, the
        # logical name; a shown designator is compared with the physical one.
        if not _same(designator, name) and not (
            component.designator is None and part.logical
            and _same(designator, part.logical)
        ):
            report.add(
                "designator", "warning",
                f"The schematic calls this part {name}, the PCB {designator}: the "
                "designators differ (e.g. re-annotated and not yet sent to the PCB).",
                schematic=name, pcb=designator, **sheet_fields(part),
            )
        expected = part.footprint_by_uid.get(component.source_uid_tail or "", part.footprint)
        if expected and not _same(expected, component.footprint):
            fields = {
                "component": designator,
                "schematic": expected,
                "pcb": component.footprint,
                **sheet_fields(part),
            }
            alternative = any(_same(component.footprint, f) for f in part.footprints)
            if alternative:
                fields["pcb_is_a_schematic_alternative"] = True
            report.add(
                "footprint", "warning",
                f"{designator} uses footprint {component.footprint} on the PCB; the "
                f"schematic asks for {expected}"
                + (" (the PCB one is another of the part's footprint models)." if alternative
                   else "."),
                **fields,
            )
        if part.comment is not None and component.comment is not None and not _same(
            part.comment, component.comment
        ):
            report.add(
                "comment", "warning",
                f"{designator}: comment '{component.comment}' on the PCB, "
                f"'{part.comment}' in the schematic.",
                component=designator, schematic=part.comment, pcb=component.comment,
                **sheet_fields(part),
            )


def _net_findings(report, parts, board, links, netlist, options,
                  maps: dict | None = None) -> dict:
    """Member sets, net matching, and the pin, pad and net findings. Returns counts; maps,
    when given, receives the matching (schematic net -> PCB net) under "nets"."""
    component_of = {name: board.components[index] for index, (name, _) in links.items()}
    skip = {
        index for index, component in board.components.items()
        if component.kind in (JUMPER, GRAPHICAL)
    } | {
        component_of[name].index for name, part in parts.items()
        if name in component_of and part.kind == JUMPER
    }
    counts = Counter()

    # Schematic members as (PCB component, pad), and the pins with no pad.
    sch_members: dict[str, set] = defaultdict(set)
    off_board: Counter = Counter()
    pending: Counter = Counter()  # pins behind a footprint change
    net_of_pad: dict[tuple[int, str], str] = {}
    for net, pins in (netlist.get("nets") or {}).items():
        sch_members.setdefault(net, set())
        for name, pin in pins:
            component = component_of.get(name)
            if component is None:
                off_board[net] += 1
                continue
            if component.index in skip:
                continue
            pad = board.pad_for(component, pin)
            if pad is None:
                part = parts[name]
                expected = part.footprint_by_uid.get(
                    component.source_uid_tail or "", part.footprint
                )
                if expected and not _same(expected, component.footprint):
                    # Update PCB swaps the footprint first; its pads are not on the board.
                    counts["pins_behind_a_footprint_change"] += 1
                    pending[net] += 1
                    continue
                designator = board.designator(component)
                report.add(
                    "unknown_pin", "error",
                    f"Pin {pin} of {designator} is on net {net}, but footprint "
                    f"{component.footprint} has no pad {pin}: Update PCB stops with "
                    f"'Unknown Pin: Pin {designator}-{pin}'.",
                    pin=f"{designator}-{pin}", net=net, footprint=component.footprint,
                )
                continue
            key = (component.index, pad)
            sch_members[net].add(key)
            net_of_pad.setdefault(key, net)

    linked_indexes = {component.index for component in component_of.values()} - skip
    pcb_members: dict[str, set] = defaultdict(set)
    for net, members in board.net_members.items():
        for key in members:
            if key[0] in linked_indexes:
                pcb_members[net].add(key)

    # Match nets: shared members first (the most shared first), then names.
    owner: dict[tuple[int, str], set[str]] = defaultdict(set)
    for net, members in pcb_members.items():
        for key in members:
            owner[key].add(net)
    candidates = []
    for net, members in sch_members.items():
        shared = Counter(pcb for key in members for pcb in owner.get(key, ()))
        for pcb, count in shared.items():
            size = max(len(members), len(pcb_members[pcb]))
            identical = members == pcb_members[pcb]
            if identical or (
                count >= options["min_matched_members"]
                and 100.0 * count / size >= options["min_match_percent"]
            ):
                candidates.append((-count, pcb != net, net, pcb, identical))
    candidates.sort()
    to_pcb: dict[str, str] = {}
    matched_pcb: set[str] = set()
    for _, _, net, pcb, _ in candidates:
        if net in to_pcb or pcb in matched_pcb:
            continue
        to_pcb[net] = pcb
        matched_pcb.add(pcb)
    counts["matched_by_members"] = len(to_pcb)
    if options["name_matching"]:
        unmatched_pcb = {
            net for net in board.net_members if net not in matched_pcb
        } | {net for net in board.index.pcb.nets if net not in matched_pcb}
        folded: dict[str, list[str]] = defaultdict(list)
        for net in unmatched_pcb:
            folded[net.lower()].append(net)
        for net in sch_members:
            if net in to_pcb:
                continue
            pcb = net if net in unmatched_pcb else None
            if pcb is None and len(folded.get(net.lower(), [])) == 1:
                pcb = folded[net.lower()][0]
            if pcb and pcb not in matched_pcb:
                to_pcb[net] = pcb
                matched_pcb.add(pcb)
                counts["matched_by_name"] += 1

    from_pcb = {pcb: net for net, pcb in to_pcb.items()}
    if maps is not None:
        maps["nets"] = dict(to_pcb)
    for net, pcb in to_pcb.items():
        if pcb != net:
            # Update PCB renames it; names drive classes, rules and pairs on the PCB.
            report.add(
                "net_name", "info" if _same(pcb, net) else "warning",
                f"Net {net} is named {pcb} on the PCB: Update PCB would rename it.",
                schematic=net, pcb=pcb, members=len(sch_members[net]),
            )
    for net, members in sch_members.items():
        if net in to_pcb or not (members or off_board[net] or pending[net]):
            continue  # matched, or only on jumpers (Altium does not sync their nets)
        fields = {"net": net, "members": len(members) + off_board[net] + pending[net]}
        if off_board[net]:
            fields["members_not_on_pcb"] = off_board[net]
        if pending[net]:
            fields["members_behind_a_footprint_change"] = pending[net]
        report.add(
            "net_only_in_schematic", "warning",
            f"Net {net} has no counterpart on the PCB: Update PCB would add it.",
            **fields,
        )
    for net, members in board.net_members.items():
        if net in matched_pcb or _UNNAMED_NET.match(net):
            continue
        if all(key[0] in skip for key in members):
            continue  # only on jumper or graphical pads
        compared = pcb_members.get(net, set())
        if not compared:
            if len(members) <= 1:
                counts["single_pin_pcb_nets"] += 1
                continue
            report.add(
                "net_only_on_pcb", "info",
                f"Net {net} is only on parts placed on the PCB alone.",
                net=net, members=len(members),
            )
            continue
        if len(compared) == 1:
            # One compared pad (any others on parts not compared). The Protel netlist
            # leaves out single-pin nets the PCB keeps; it is one if the pad's pin
            # exists and is on no other schematic net.
            (lone,) = compared
            lone_part = parts[links[lone[0]][0]]
            if lone not in net_of_pad and _pin_exists(lone_part, lone[1]) is not False:
                counts["single_pin_pcb_nets"] += 1
                continue
        report.add(
            "net_only_on_pcb", "warning",
            f"Net {net} is not in the schematic: Update PCB would remove it.",
            net=net, members=len(members),
        )

    # Pads: one finding per pad whose net differs from its pin's.
    for index in sorted(linked_indexes):
        component = board.components[index]
        designator = board.designator(component)
        part = parts[links[index][0]]
        for pad, nets in sorted(board.pads.get(index, {}).items()):
            key = (index, pad)
            net = net_of_pad.get(key)
            for pcb_net in sorted(nets, key=lambda value: value or ""):
                pin = f"{designator}-{pad}"
                if net is not None:
                    expected = to_pcb.get(net)
                    if pcb_net is None:
                        report.add(
                            "pad_net", "error",
                            f"Pad {pin} has no net on the PCB; the schematic connects "
                            f"it to {net}.",
                            pin=pin, schematic_net=net, pcb_net=None,
                        )
                    elif pcb_net == net and expected is None and from_pcb.get(pcb_net) is None:
                        reason = "too few shared members" + (
                            "" if options["name_matching"] else ", and name matching is off"
                        )
                        report.add(
                            "pad_net", "info",
                            f"Pad {pin} is on {net} on both sides, but the two {net} nets "
                            f"were not matched ({reason}): Update PCB would remove the "
                            f"PCB's {net} and add the schematic's.",
                            pin=pin, schematic_net=net, pcb_net=pcb_net, net_rebuilt=True,
                        )
                    elif pcb_net != expected:
                        other = from_pcb.get(pcb_net)
                        here = (
                            f" (the schematic's {other})" if other and other != pcb_net
                            else " (matched to no schematic net)" if other is None else ""
                        )
                        there = (
                            f" ({expected} on the PCB)" if expected and expected != net
                            else " (a net the PCB lacks)" if expected is None else ""
                        )
                        report.add(
                            "pad_net", "error",
                            f"Pad {pin} is on {pcb_net}{here} on the PCB; the schematic "
                            f"connects it to {net}{there}.",
                            pin=pin, schematic_net=net, pcb_net=pcb_net,
                            pcb_net_matches=other, schematic_net_on_pcb=expected,
                        )
                    continue
                if pcb_net is None:
                    continue
                exists = _pin_exists(part, pad)
                if (
                    exists is not False and pcb_net not in matched_pcb
                    and pcb_members.get(pcb_net, set()) <= {key}
                ):
                    continue  # a single-pin net, absent from the Protel netlist
                if exists is None:
                    report.add(
                        "pad_net", "warning",
                        f"Pad {pin} is on {pcb_net} on the PCB but the schematic does not "
                        "connect it; the part's pin list was not read, so it may have no "
                        "such pin.",
                        pin=pin, schematic_net=None, pcb_net=pcb_net,
                    )
                elif not exists:
                    report.add(
                        "pad_net", "warning",
                        f"Pad {pin} is on {pcb_net} on the PCB but the schematic symbol "
                        "has no such pin (e.g. a shield pad tied by hand): Update PCB "
                        "would take it off the net.",
                        pin=pin, schematic_net=None, pcb_net=pcb_net,
                    )
                else:
                    report.add(
                        "pad_net", "error",
                        f"Pad {pin} is on {pcb_net} on the PCB but its schematic pin is "
                        "not connected.",
                        pin=pin, schematic_net=None, pcb_net=pcb_net,
                    )
    counts["schematic_nets"] = len(sch_members)
    counts["pcb_nets"] = len(board.net_members)
    return counts


@dataclass
class SyncMaps:
    """How the schematic's names reach the PCB (see sync_maps)."""
    nets: dict[str, str] = field(default_factory=dict)       # schematic net -> PCB net
    parts: dict[str, str] = field(default_factory=dict)      # designator -> PCB designator
    # Parts on no PCB part that Update PCB may not place (mechanical or graphical, or no
    # footprint): the info missing_from_pcb rows of run_sync_check.
    unplaced: set[str] = field(default_factory=set)


def sync_maps(index, netlist: dict | None, net_matching: dict | None = None) -> SyncMaps:
    """How the schematic's names reach the PCB, matched as run_sync_check matches them:
    nets by shared members, then name; parts by unique ID, then designator; and the parts
    Update PCB may not place. Empty without parts."""
    parts, _ = _schematic_parts(netlist or {})
    if not parts:
        return SyncMaps()
    board = _Board(index)
    links, _ = _link(parts, board, (netlist or {}).get("pin_to_net") or {})
    maps: dict = {}
    _net_findings(_Report(), parts, board, links, netlist or {},
                  net_matching or dict(DEFAULT_NET_MATCHING), maps)
    designators = {name: board.designator(board.components[number])
                   for number, (name, _) in links.items()}
    unplaced = {part.name for part in parts.values() if part.name not in designators
                and (part.kind in (MECHANICAL, GRAPHICAL) or not part.footprint)}
    return SyncMaps(maps.get("nets", {}), designators, unplaced)


def run_sync_check(
    index,
    netlist: dict | None,
    is_dnp=None,
    checks: list[str] | None = None,
    offset: int = 0,
    limit: int = DEFAULT_LIMIT,
    sheets_saved_after_pcb: list[str] | None = None,
    net_matching: dict | None = None,
) -> dict:
    """Compare the compiled schematic with the PCB; findings are paginated."""
    requested = [check.strip().lower() for check in checks or [] if check.strip()]
    unknown = [check for check in requested if check not in CHECK_GROUPS]
    if unknown:
        return {
            "error": "invalid_checks",
            "message": f"Unknown checks {unknown}; use {sorted(CHECK_GROUPS)}.",
        }
    run = requested or list(CHECK_GROUPS)
    parts, compiled = _schematic_parts(netlist or {})
    if not parts:
        return {
            "error": "no_netlist",
            "message": "The schematic netlist has no parts: load the project with "
            "set_project_dir (or refresh_netlist) first.",
        }
    options = net_matching or dict(DEFAULT_NET_MATCHING)
    board = _Board(index)
    links, unmatched_paths = _link(parts, board, (netlist or {}).get("pin_to_net") or {})
    newer = {sheet.lower() for sheet in sheets_saved_after_pcb or []}
    report = _Report()
    _component_findings(
        report, parts, board, links, is_dnp, newer,
        sheet_errors=bool((netlist or {}).get("sheet_errors")),
    )
    counts = _net_findings(report, parts, board, links, netlist or {}, options)

    problems = Counter(
        group for finding in report.findings if finding["severity"] != "info"
        for group, group_checks in CHECK_GROUPS.items() if finding["check"] in group_checks
    )
    wanted = {check for group in run for check in CHECK_GROUPS[group]}
    findings = [finding for finding in report.findings if finding["check"] in wanted]
    findings.sort(key=lambda finding: (
        _SEVERITY_RANK[finding["severity"]],
        _CHECK_RANK[finding["check"]],
        finding["issue"],
    ))
    summary = Counter(finding["check"] for finding in findings)
    severities = Counter(finding["severity"] for finding in findings)
    offset = max(0, int(offset or 0))
    limit = max(1, int(limit or DEFAULT_LIMIT))
    how = Counter(how for _, how in links.values())
    not_checked = [NOT_COMPARED, PIN_MAP_NOTE]
    if not compiled:
        not_checked.append(
            "The netlist has no compiled part list (.NET part blocks): footprints come "
            "from the sheets and comments are not compared."
        )
    skipped_pads = index.pcb.skipped_records.get("Pads6", 0)
    if skipped_pads:
        not_checked.append(
            f"{skipped_pads} pads without copper (e.g. NPTH holes) are not read: a pin "
            "connected to one shows as an unknown pin."
        )
    if counts["pins_behind_a_footprint_change"]:
        not_checked.append(
            f"{counts['pins_behind_a_footprint_change']} pins of parts whose footprint "
            "changes were not checked against pads: the new footprint is not on the board."
        )
    notes = [IDENTICAL_NETS_NOTE]
    if unmatched_paths:
        notes.append(
            f"{unmatched_paths} unique ID paths from the .Annotation file match no PCB "
            "part: the file may be stale (Annotate again in Altium). Those parts link by "
            "unique ID and designator, which can mix up channel copies."
        )
    if counts["single_pin_pcb_nets"]:
        notes.append(
            f"{counts['single_pin_pcb_nets']} PCB nets with a single pad are not "
            "reported: the Protel netlist leaves out single-pin nets."
        )
    result: dict = {
        # Over every check, also those left out by checks.
        "in_sync": not problems,
        "checks_run": run,
        "components": {
            "schematic": len(parts),
            "pcb": len(board.components),
            "linked_by_unique_id": how["unique_id"],
            "linked_by_designator": how["designator"],
        },
        "nets": {
            "schematic": counts["schematic_nets"],
            "pcb": counts["pcb_nets"],
            "matched_by_members": counts["matched_by_members"],
            "matched_by_name": counts["matched_by_name"],
        },
        "net_matching": options,
        "summary": dict(summary),
        "severity_counts": {
            level: severities.get(level, 0) for level in ("error", "warning", "info")
        },
        "altium_names": {check: ALTIUM_NAMES[check] for check in summary},
        "findings": findings[offset : offset + limit],
        "total": len(findings),
        "offset": offset,
        "has_more": offset + limit < len(findings),
    }
    if result["has_more"]:
        result["next_offset"] = offset + limit
    others = sorted(group for group in problems if group not in run)
    if others:
        result["other_checks_with_differences"] = others
    result["not_checked"] = not_checked
    result["notes"] = notes
    return result
