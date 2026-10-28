"""PCB manufacturing review: prompt text, typical DFA guidance and a board snapshot.

Pure text building over a PcbIndex and the project folder: no MCP. The
pcb_manufacturing_review tool returns build_manufacturing_review(...) the way
constraints_review returns its prompt.

The review walks fabrication (check_dfm against the user's fab profile, then vias,
health, connectivity and Altium's DRC report) and assembly (check_dfm's assembly checks,
the BOM) and ends in a go / no-go that lists what was not checked. The snapshot holds
bounded facts only: copper layers, outline, parts per side, embedded boards and which
fab profile check_dfm would use by default. A profile file's own name and source are
user text and stay out of it; file names appear only as format arguments.
"""

from collections import Counter
from pathlib import PureWindowsPath

try:
    from services.fab_profiles import (
        ALTIMADE, DEFAULT_FILE, LIMIT_KEYS, load_profile, profile_files,
    )
except ImportError:
    from server.services.fab_profiles import (
        ALTIMADE, DEFAULT_FILE, LIMIT_KEYS, load_profile, profile_files,
    )

# check_dfm's checks per phase, in its order; tests keep them equal to dfm.VALID_CHECKS.
FAB_CHECKS = (
    "trace_width", "spacing", "hole_size", "annular_ring", "aspect_ratio", "hole_to_hole",
    "copper_to_edge", "layer_count", "copper_weight", "board_size", "drill_count",
    "rules_vs_profile",
)
ASSEMBLY_CHECKS = (
    "part_counts", "assembly_sides", "part_spacing", "part_to_edge", "fiducials",
    "tombstoning", "fine_pitch", "pick_and_place",
)

SNAPSHOT_MAX_FILES = 5
_MAX_NAME_CHARS = 60

# ---------- typical DFA guidance ----------

_ALTIMADE_LIMITS = ALTIMADE["limits"]

# Fallback values only: the assembler's own numbers decide. Summarised in our own words
# from the cited documents; rows say where a number is not Altium's.
DFA_GUIDANCE: tuple[dict, ...] = (
    {
        "topic": "Global fiducials",
        "guidance": "Three per assembled side, near the board edge (at least one where there "
                    "is no room); copper 1–3 mm, a mask opening about twice the copper and a "
                    "clear area about the marker's diameter. A panel's rails can carry them "
                    "instead: ask the assembler",
        "source": "Altium resources: Are fiducial marker placements still necessary "
                  "(Z. Peterson)",
    },
    {
        "topic": "Local fiducials",
        "guidance": "Usually not needed at a pad pitch of 1.0 mm and above; fine-pitch parts "
                    "and BGAs may need two, diagonally outside the part. A common rule of "
                    "thumb (not Altium's) asks for them at 0.5 mm pitch and below",
        "source": "Altium resources: Are fiducial marker placements still necessary "
                  "(Z. Peterson); third-party rule of thumb",
    },
    {
        "topic": "Part to board edge",
        "guidance": "About 125 mil (3.175 mm) for top-side SMD parts on a board without "
                    "breakaway rails; much less with rails or in a panel. Copper still keeps "
                    "the fab's copper-to-edge clearance",
        "source": "Altium resources: DFA guidelines for efficient PCB design (A. Bahl)",
    },
    {
        "topic": "Part to part",
        "guidance": "No Altium-published number: the board's Component Clearance rule "
                    "(Altium's default 10 mil) or an IPC-7351 courtyard excess (about "
                    "0.25 mm at density level B); more around BGAs for rework",
        "source": "Altium Component Clearance rule documentation; IPC-7351 (third-party "
                  "summaries)",
    },
    {
        "topic": "Tombstoning",
        "guidance": "Most common on 0603 and smaller passives: both pads should heat and wet "
                    "together, so connect them alike (a thermal relief on the pour side, "
                    "comparable track widths)",
        "source": "Altium resources: DFA guidelines (A. Bahl); thermal relief design "
                  "articles (Z. Peterson)",
    },
    {
        "topic": "Altimade assembly",
        "guidance": (
            f"At most {_ALTIMADE_LIMITS['max_unique_parts']['published']} unique "
            f"components and {_ALTIMADE_LIMITS['max_placements']['published']} placements "
            f"(rows '{_ALTIMADE_LIMITS['max_unique_parts']['row']}' and "
            f"'{_ALTIMADE_LIMITS['max_placements']['row']}'); it also checks a minimum "
            "component pitch without publishing the value. Altimade's limits only, never "
            "another assembler's"
        ),
        "source": f"Altimade page, updated {ALTIMADE['source_updated']}",
    },
)

_GUIDANCE_COLUMNS = (("topic", "Topic"), ("guidance", "Guidance"), ("source", "Source"))


def _cell(text: str) -> str:
    return " ".join(str(text).replace("|", "/").split()) or "—"


def render_dfa_guidance() -> str:
    """DFA_GUIDANCE as a Markdown table."""
    lines = [
        "| " + " | ".join(title for _, title in _GUIDANCE_COLUMNS) + " |",
        "|" + "---|" * len(_GUIDANCE_COLUMNS),
    ]
    for row in DFA_GUIDANCE:
        lines.append("| " + " | ".join(_cell(row[key]) for key, _ in _GUIDANCE_COLUMNS) + " |")
    return "\n".join(lines)


# ---------- board snapshot ----------


def _short(text: str, limit: int = _MAX_NAME_CHARS) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def _copper_line(index) -> str:
    ids = [
        row["v6_id"]
        for row in index.db.execute(
            "SELECT v6_id FROM layers WHERE kind = 'copper' AND v6_id IS NOT NULL"
        )
    ]
    signal = sum(1 for v6_id in ids if 1 <= v6_id <= 32)
    planes = sum(1 for v6_id in ids if 39 <= v6_id <= 54)
    if not signal + planes:
        return "- Copper layers: none found in the stack"
    line = f"- Copper layers: {signal + planes}"
    if planes:
        line += f" ({_plural(planes, 'internal plane')} among them)"
    return line


def _outline_line(index) -> str:
    board = index.pcb.board
    shape = getattr(board, "outline_shape", None) or []
    if len(shape) >= 3 or len(board.outline_vertices or []) >= 3:
        return "- Board outline: defined (get_board_info gives its size)"
    return (
        "- Board outline: none in the PcbDoc, so check_dfm cannot measure the board size "
        "or any edge clearance"
    )


def _parts_line(index) -> str:
    components = index.pcb.components
    if not components:
        return "- Components on the PCB: none"
    sides = Counter(component.side for component in components)
    split = ", ".join(
        f"{sides[side]} {side}" for side in ("top", "bottom") if sides.get(side)
    )
    return (
        f"- Components on the PCB: {len(components)} ({split}; every kind, Not Fitted "
        "parts included: check_dfm part_counts counts the fitted placements)"
    )


def _panel_line(index) -> str | None:
    boards = getattr(index.pcb, "embedded_boards", 0) or 0
    if not boards:
        return None
    return (
        f"- Embedded boards: {boards} (this PcbDoc is a panel; the boards in it are not "
        "read: review each board's own PcbDoc too)"
    )


def profile_lines(root_dir, setting: str | None = None) -> list[str]:
    """Which fab profile check_dfm uses by default for this project folder (setting: the
    user's Fab profile setting), and the other .json files in it that a profile argument
    could name (at most SNAPSHOT_MAX_FILES). Never the profile file's own name or source
    text."""
    root = root_dir or None
    profile, error = load_profile(None, root, setting=setting)
    if error and error.get("setting"):
        message = " ".join(str(error.get("message", "")).split())
        line = (
            "- Fab profile: the user's Fab profile setting cannot be used, so check_dfm "
            f"answers {error.get('error')} until it is fixed (or a profile is passed): "
            f"{message}"
        )
    elif error:
        message = " ".join(str(error.get("message", "")).split())
        line = (
            f"- Fab profile: the project's {DEFAULT_FILE} cannot be used, so check_dfm "
            f"answers invalid_profile until it is fixed: {message}"
        )
    elif profile.chosen_by == "project_file":
        # Limits the file sets itself: not Altimade's under it, not a fallback's.
        own = sum(1 for name in LIMIT_KEYS
                  if profile.resolved(name) == name and profile.origin(name) == "file")
        if profile.based_on == "altimade":
            what = f"{_plural(own, 'limit')} of its own on top of Altimade's published limits"
        else:
            what = _plural(own, "limit")
        if not own and profile.based_on != "altimade":
            line = (
                f"- Fab profile: the project's {DEFAULT_FILE} sets no limit yet (every "
                "value is null): check_dfm uses it by default and checks nothing against "
                "it until the fab's numbers are filled in"
            )
        else:
            line = (
                f"- Fab profile: the project's {DEFAULT_FILE} ({what}) is check_dfm's "
                "default; confirm with the user that it is the fabricator building this "
                "board"
            )
    elif profile.chosen_by == "setting":
        if profile.file is not None:
            what = "a profile file"
        elif profile.key == "altimade":
            what = "Altimade's published limits"
        else:
            what = "profile none: no fabricator limit"
        line = (
            f"- Fab profile: no {DEFAULT_FILE} in the project folder, so check_dfm uses "
            f"the user's Fab profile setting ({what}); confirm with the user that it is "
            "the fabricator building this board"
        )
    else:
        line = (
            f"- Fab profile: no {DEFAULT_FILE} in the project folder, so check_dfm "
            "defaults to Altimade's published limits (Altium's ordering service), not "
            "the user's fabricator"
        )
    lines = [line]
    files, total = profile_files(root)
    others = [name for name in files if name.lower() != DEFAULT_FILE]
    other_total = total - (len(files) - len(others))
    if other_total:
        shown = ", ".join(_short(name) for name in others[:SNAPSHOT_MAX_FILES])
        if other_total > min(len(others), SNAPSHOT_MAX_FILES):
            shown += f", +{other_total - min(len(others), SNAPSHOT_MAX_FILES)} more"
        lines.append(
            f"- Other .json files in the project folder ({other_total}; a fab profile only "
            f"if the user says so, passed by name as profile): {shown}"
        )
    return lines


def manufacturing_snapshot(index, root_dir, fab_profile_setting: str | None = None) -> str:
    """Bounded facts for the review, one Markdown bullet per line: copper layers, the
    outline, parts per side, embedded boards (a panel) and the default fab profile with
    the other .json files of the project folder."""
    lines = [_copper_line(index), _outline_line(index), _parts_line(index)]
    panel = _panel_line(index)
    if panel:
        lines.append(panel)
    lines += profile_lines(root_dir, fab_profile_setting)
    return "\n".join(lines)


# ---------- prompt ----------

# Every tool the prompt names; tests check each appears in the prompt and is
# registered on the server.
MANUFACTURING_REVIEW_TOOLS = (
    "get_board_info",
    "get_design_notes",
    "check_dfm",
    "check_vias",
    "check_pcb_health",
    "check_connectivity",
    "get_drc_report",
    "get_bom",
    "check_bom",
    "check_sch_pcb_sync",
    "get_component_placement",
    "query_pcb_region",
    "list_variants",
    "set_project_dir",
    "set_active_variant",
    "run_review_checklist",
)

PCB_MANUFACTURING_REVIEW_PROMPT = """\
Project: {name}
PCB: {pcb_doc}
Active variant: {active_variant}

Board snapshot (facts from the saved PcbDoc and the project folder):
{snapshot}

You are starting a manufacturing readiness review of this board: can the user's
fabricator build the bare board, and can the assembler place and solder its parts? It
ends in a go / no-go that names everything that was not checked. Before running anything,
ask the user:
  - Which fabricator (and assembler, if another) will build it? Ask for its capability
    numbers (a link, a table or a fab_profile.json in the project folder). Altimade is
    built in; any other fab needs its own numbers.
  - The scope: fabrication only, assembly only, or both.
  - The variant to build: the active one ({active_variant}) or another (list_variants).

Fabrication and assembly can run as two independent tasks in parallel, one agent each.
Each agent can self-initialize: set_project_dir("{root_dir}") → set_active_variant with
the variant confirmed in Phase 1 → run Phase 2 or Phase 3 with the profile confirmed in
Phase 1, and return its table rows. Merge both into one Phase 4 report.

Do not proceed until the user answers.

---

## Ground Rules

- The requirement is the user's fabricator's and assembler's own capability table.
  check_dfm compares the board with one profile per call, and its scope says which.
  Every verdict names that profile and says "on the checks run": never call the board
  manufacturable in general.
- Altimade (Altium's ordering service) is built in as profile "altimade". Never present
  an Altimade result as another fab's. When the user names another fab, ask for its
  numbers and pass them as limits with profile="none" (keys carry their unit, e.g.
  {{"min_spacing_mm": 0.1, "max_aspect_ratio": 8}}), or the user saves them as a .json
  file in the project folder and you pass its name as profile (fab_profile.json is used
  by default). If the user has no numbers, run the default only with their agreement and
  label every verdict "Altimade's published limits, not your fab's".
- Never invent a limit or change its unit. profile_not_found, invalid_profile and
  invalid_limits say what to fix: show the user and ask.
- Answer from each check's status and severity together: fail (a miss of severity error
  or warning) blocks the order or needs the fab's waiver; marginal (every miss within the
  tolerance) needs the fab's confirmation; advisory (only info misses, such as Altimade's
  ordering limits while it is only the default) is a note, not a manufacturing miss;
  not_checked carries its reason: quote it. A check that did not run is not a pass.
  board_minimums is what the board needs from any fab, whatever the profile.
- Every finding cites evidence: the tool, the check, the value against the limit and an
  example location. check_dfm locations (at_mil) are relative to the board origin: add
  origin_mil before query_pcb_region.
- get_drc_report is Altium's last batch DRC, not a check of the board as saved: quote it
  only with its assessment (older than the board, stopped at its violation limit, rule
  kinds left out).
- Work in the variant confirmed in Phase 1 (set_active_variant when it is not
  {active_variant}): Not Fitted parts are not placed, so the assembly counts follow it.
- PCB data is the last saved PcbDoc: if the user changes the board, ask them to save
  (Ctrl+S) before you check again. This review is read-only: it changes no design file.

---

### Phase 1 — Board and Profile

1. get_board_info: copper layers and stack, thickness, outline and size, object counts.
2. get_design_notes: fabrication and assembly notes in the Draftsman drawings and on the
   sheets (thickness, finish, copper weight, IPC class, impedance control, panel). They
   are what the order asks for: compare them with what the fab offers.
3. Profile: with the user's numbers or file, call check_dfm(profile=..., limits=...,
   checks=["layer_count", "board_size", "copper_weight"]) and show the echoed profile
   (each limit with its origin) and its scope. Ask the user to confirm the numbers against
   the fab's table.
4. Checklist: run_review_checklist(checklist="pcb", sections=["manufacturing",
   "testability"]) and, when assembly is in scope, run_review_checklist(checklist="bom"):
   show their coverage tables. Their needs-judgment items are what Phases 2-3 decide.

Present a table: | Item | Board | Fab offers (source) |

Done when: the user has confirmed the fabricator, its limits and the scope.
Do not proceed to Phase 2 until then.

---

### Phase 2 — Fabrication (bare board)

1. check_dfm(profile=..., checks={fab_checks}) with the confirmed profile: each check's
   status, severity, limit and worst value; page through every finding (offset from
   next_offset). rules_vs_profile names board rules looser than the fab, which Altium's
   DRC would let through.
2. check_vias: via types (blind, buried, microvias), aspect ratios, annular rings,
   via-in-pad (fill and cap are an order option), tenting and back drills. Pass the fab's
   max_aspect_ratio and min_annular_ring when it publishes them. Anything that changes
   the order (microvias, filled via-in-pad, back drilling) goes in the report.
3. check_pcb_health: shelved or modified pours (Altium stops output generation), duplicate
   designators, parts outside the board, invalid rules.
4. check_connectivity: unrouted connections and dead copper. An unrouted net is a no-go
   unless the user says it is intended, and so is a net in nets_unrouted_in_altium
   (routed only through weak joins that the board's Un-Routed Net rule rejects).
5. get_drc_report: Altium's last batch DRC with its assessment. No report, or a stale one:
   say so and recommend running Tools > Design Rule Check before generating outputs.

Done when: every fabrication check has a status with evidence or a stated reason.
Do not proceed to Phase 3 until then.

---

### Phase 3 — Assembly (DFA)

1. check_dfm(profile=..., checks={assembly_checks}) with the assembler's profile (often
   the fab's): placements and unique parts against max_placements and max_unique_parts
   (Altimade's ordering limits), parts on both sides and THT on the bottom (extra process
   steps), part spacing, parts near the edge, fiducials, tombstoning risk on small
   passives, fine pitch, and the pick-and-place data (parts without pads, asymmetric
   parts, the board origin: tell the assembler which columns and origin the file
   uses). A limit the profile does not set is measured, not judged: ask
   the assembler for min_part_spacing, min_part_to_edge, min_global_fiducials,
   local_fiducial_pitch and min_pitch. Without its numbers, compare board_minimums with
   the guidance at the end in the report, marked "typical — confirm"; never pass a
   typical value as a check_dfm limit, and never count a miss of one as a fail.
2. With a netlist: get_bom for the variant's lines and quantities (its line count is the
   unique-part count an assembler quotes), then check_bom for missing MPNs and
   footprints, DNP text on fitted parts and placeholder descriptions. check_sch_pcb_sync:
   a PCB out of sync with the schematic is assembled from the wrong BOM.
3. get_component_placement for a part named in a finding (side, position, rotation and
   its neighbours).

Done when: every assembly check has a status with evidence or a stated reason.
Do not proceed to Phase 4 until then.

---

### Phase 4 — Go / No-Go

Present the findings as four Markdown tables:

  Table 1 — Fabrication (profile: name and source)
  | Check | Status | Limit | Board worst | Evidence |

  Table 2 — Assembly
  | Check | Status | Limit | Board value | Evidence |

  Table 3 — Blockers & Questions for the Fab / Assembler
  | Item | Severity | Issue | Fix or question |

  Table 4 — Not Checked
  | Item | Why | How to confirm |

Table 4 lists every not_checked entry the tools returned (solder mask, silkscreen, paste
and stencil, panels, courtyards, component heights, pick-and-place rotations and the like),
the phases or checks left out of scope, and anything a tool could not answer.

Verdict, naming the profile: GO (no fail, nothing blocking), GO WITH CONDITIONS (marginal
results, warnings or open questions the fab or assembler must confirm: list them), or
NO-GO (a fail of severity error, unrouted connections, shelved or modified pours, a PCB
out of sync with the schematic). Never a GO on checks that did not run.

Finish with the checklist records: call run_review_checklist again for each checklist
you ran in Phase 1 (same sections), with format="markdown" and decisions= giving your
Yes, No or N/A and a one-line reason for each needs-judgment item you settled. Show each
'Checklist coverage' table and record (tied to the design state it names); save them only
where the user agrees.

Ask: "Shall I draft the questions for the fabricator and assembler, or dig into a
finding?"

---

## Typical DFA Guidance (fallback only — the assembler's own numbers decide)

Use a row only when the assembler publishes nothing, and mark it "typical — confirm": it
is compared with board_minimums in the report, never passed as a check_dfm limit, and a
board value beyond it is a question for the assembler, not a fail.

{dfa_guidance}
"""

MANUFACTURING_REVIEW_NETLIST_FREE_NOTE = """
---

Netlist-free load: this project was loaded without a netlist (Altium Designer was not
running), so get_bom, check_bom and check_sch_pcb_sync answer netlist_required. check_dfm
still runs: its unique-part count is approximate (footprint and comment from the PCB, no
MPN), and Not Fitted parts come from the designators stored in the variant, which can be
stale. Say in the report that the BOM and the schematic sync were not checked.
"""


def _check_list(names: tuple[str, ...]) -> str:
    return "[" + ", ".join(f'"{name}"' for name in names) + "]"


def build_manufacturing_review(project: dict, variant_name: str, index, *,
                               netlist_free: bool = False,
                               fab_profile_setting: str | None = None) -> str:
    """The manufacturing-review prompt for a loaded project and its PCB index;
    netlist_free appends MANUFACTURING_REVIEW_NETLIST_FREE_NOTE; fab_profile_setting is
    the user's Fab profile setting (check_dfm's default after the project's file)."""
    paths = project.get("pcb_doc_paths") or []
    pcb_doc = PureWindowsPath(paths[0]).name if paths else "(none)"
    if len(paths) > 1:
        pcb_doc += f" (the project lists {len(paths)} PcbDocs; only this one is reviewed)"
    review = PCB_MANUFACTURING_REVIEW_PROMPT.format(
        name=project.get("name", "?"),
        pcb_doc=pcb_doc,
        active_variant=variant_name,
        root_dir=project.get("root_dir", ""),
        snapshot=manufacturing_snapshot(index, project.get("root_dir"),
                                        fab_profile_setting),
        fab_checks=_check_list(FAB_CHECKS),
        assembly_checks=_check_list(ASSEMBLY_CHECKS),
        dfa_guidance=render_dfa_guidance(),
    )
    if netlist_free:
        review += MANUFACTURING_REVIEW_NETLIST_FREE_NOTE
    return review
