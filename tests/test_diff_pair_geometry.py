"""Differential-pair geometry (services/diff_pair_geometry, check_diff_pair_geometry): coupled
gap and widths per layer, gap stretches outside the rule, uncoupled length, layer changes,
limits from the DiffPairsRouting rule or an impedance profile, inferred pairs, paging, on
synthetic boards."""

import asyncio
import json
import math
from pathlib import Path

import pytest

import server.main as main_mod
from server.parsers.pcb_doc import (
    PAD_SHAPE_RECT,
    BoardInfo,
    DiffPair,
    ImpedanceLayer,
    ImpedanceProfile,
    PcbClass,
    PcbData,
    Primitive,
    _arc_chord_points,
    shape_points,
)
from server.parsers.prj_pcb import VariantDefinition, VariantState
from server.services import diff_pair_checks as dpc
from server.services import diff_pair_geometry as dpg
from server.services.pcb_index import PcbIndex
from tests.test_reference_planes import BOTTOM, MID, TOP, V7, _layers
from tests.test_reference_planes import Board as _Board

NAMES = ["GND", "A_P", "A_N", "B_P", "B_N", "C_P", "C_N", "D+", "D-", "SIG", "SIG2"]
N = {name: number for number, name in enumerate(NAMES)}
AP, AN, BP, BN, CP, CN = N["A_P"], N["A_N"], N["B_P"], N["B_N"], N["C_P"], N["C_N"]
REPO = Path(__file__).parents[1]
W = 5.0  # track width (mil)
SPACING = 10.0  # centre to centre: a 5 mil gap


class Board(_Board):
    """The 1000 x 1000 mil board of test_reference_planes (Top / plane / Mid / Bottom) with
    generic nets, differential pairs, classes and arcs."""

    def __init__(self, **kwargs):
        kwargs.setdefault("plane", False)
        super().__init__(**kwargs)
        self.classes: list[PcbClass] = []
        self.diff_pairs: list[DiffPair] = []
        self.arcs = 0

    def pair(self, name, positive, negative):
        self.diff_pairs.append(DiffPair(name=name, positive_net=positive,
                                        negative_net=negative, unique_id=None))

    def net_class(self, name, members, kind="net"):
        self.classes.append(PcbClass(name=name, kind=kind, members=list(members),
                                     superclass=False, auto_generated=False, unique_id=None))

    def arc(self, net, layer, cx, cy, radius, start, end, width=W):
        """An arc as the parser stores it: chords carrying the arc's centre and radius."""
        points = _arc_chord_points(cx, cy, radius, start, end)
        sweep = (end - start) % 360.0 or 360.0
        chord = radius * math.radians(sweep) / (len(points) - 1)
        for (x1, y1), (x2, y2) in zip(points, points[1:]):
            self.prims.append(Primitive(
                kind="arc", layer=layer, net=net, component=None, x1=x1, y1=y1, x2=x2,
                y2=y2, width=width, source_id=self.arcs, length=chord, arc_x=cx, arc_y=cy,
                arc_radius=radius))
        self.arcs += 1

    def smd_pad(self, net, x, y, size=12.0, layer=TOP):
        self.prims.append(Primitive(
            kind="pad", layer=layer, net=net, component=None, x1=x, y1=y, x2=x, y2=y,
            width=size, height=size, shape=PAD_SHAPE_RECT, hole=0.0, pad_name="1"))

    def dp_rule(self, scope="All", gap=(5.0, 5.0, 5.0), width=(4.0, 5.0, 6.0),
                uncoupled=500.0, layers=("TOPLAYER",), **extra):
        """A DiffPairsRouting rule: gap and width (min, preferred, max) on the given
        layers, Max Uncoupled Length."""
        properties = {"MINLIMIT": f"{gap[0]}mil", "MOSTFREQGAP": f"{gap[1]}mil",
                      "MAXLIMIT": f"{gap[2]}mil", "MAXUNCOUPLEDLENGTH": f"{uncoupled}mil"}
        for slot in layers:
            properties.update({
                f"{slot}_MINWIDTH": f"{width[0]}mil", f"{slot}_PREFWIDTH": f"{width[1]}mil",
                f"{slot}_MAXWIDTH": f"{width[2]}mil", f"{slot}_MINGAP": f"{gap[0]}mil",
                f"{slot}_PREFGAP": f"{gap[1]}mil", f"{slot}_MAXGAP": f"{gap[2]}mil"})
        properties.update(extra)
        self.rule("DiffPairsRouting", scope, **properties)

    def profile(self, name="D90", layer=TOP, width=5.0, gap=5.0, low=None, high=None,
                rule=None):
        """A differential impedance profile solving one layer (min / max width and gap
        when low / high are given as (width, gap))."""
        profile_id = f"{{{name}}}"
        self.profiles.append(ImpedanceProfile(
            id=profile_id, name=name, display_name=name, target_ohm=90.0, tolerance_pct=10.0,
            differential=True, layers=[ImpedanceLayer(
                layer_v7=V7[layer], ref_top_v7=None, ref_bottom_v7=None, substack_id=None,
                width_mil=width, min_width_mil=low[0] if low else None,
                max_width_mil=high[0] if high else None, gap_mil=gap,
                min_gap_mil=low[1] if low else None, max_gap_mil=high[1] if high else None,
                calculated_ohm=90.0, error_pct=0.0, propagation_speed=None, enabled=True)]))
        return profile_id

    def index(self) -> PcbIndex:
        board = BoardInfo(
            origin_x=0.0, origin_y=0.0, sheet_x=0.0, sheet_y=0.0, sheet_width=2000.0,
            sheet_height=2000.0, display_unit="mil", layers=_layers(self.plane),
            stackup_source="v9", outline_vertices=shape_points(self.outline),
            outline_shape=list(self.outline), impedance_profiles=list(self.profiles),
        )
        data = PcbData(
            board=board, nets=list(NAMES), components=[], primitives=list(self.prims),
            pours=[], layer_name_to_v6={"top layer": TOP, "mid signal": MID,
                                        "bottom layer": BOTTOM},
            rules=list(self.rules), classes=list(self.classes),
            diff_pairs=list(self.diff_pairs),
        )
        return PcbIndex(data, None)


def _straight(board, positive=AP, negative=AN, y=100.0, x1=100.0, x2=600.0, spacing=SPACING,
              layer=TOP, width=W, n_width=None):
    board.track(positive, layer, x1, y, x2, y, width=width)
    board.track(negative, layer, x1, y + spacing, x2, y + spacing,
                width=width if n_width is None else n_width)


def _pair_a(**kwargs) -> Board:
    board = Board(**kwargs)
    board.pair("PA", "A_P", "A_N")
    return board


def _run(board: Board, **kwargs) -> dict:
    return dpg.prepare_diff_pair_geometry(board.index(), **kwargs)


def _row(result: dict, pair: str = "PA") -> dict:
    return next(row for row in result["pairs"] if row["pair"] == pair)


def _layer(row: dict, name: str = "Top Layer") -> dict:
    return next(layer for layer in row["layers"] if layer["layer"] == name)


def _findings(result: dict, check: str, severity: str | None = None) -> list[dict]:
    return [finding for finding in result["findings"] if finding["check"] == check
            and (severity is None or finding["severity"] == severity)]


# ---------- coupling geometry ----------


def test_straight_parallel_tracks_are_coupled_at_their_gap():
    board = _pair_a()
    board.dp_rule()
    _straight(board)
    result = _run(board)
    row = _row(result)
    layer = _layer(row)
    assert layer["gap_mil"] == {"min": 5.0, "mean": 5.0, "max": 5.0}
    assert layer["coupled_mil"] == {"positive": 500.0, "negative": 500.0}
    assert layer["coupled_stretches"] == 1
    assert layer["width_mil"]["positive"] == {"min": 5.0, "max": 5.0}
    assert row["uncoupled_mil"] == {"positive": 0.0, "negative": 0.0}
    assert row["rule"] == "DiffPairsRouting_0" and row["limit_source"] == "rule"
    assert row["max_uncoupled_length_mil"] == 500.0
    assert layer["limits"]["min_gap_mil"] == 5.0 and layer["limits"]["source"] == "rule"
    assert layer["coupling_distance_mil"] == 15.0  # 3x the 5 mil max gap
    assert result["findings"] == []
    assert result["summary"]["pairs_analysed"] == 1
    assert result["summary"]["limit_sources"] == {"rule": 1}


def test_diagonal_parallel_tracks_measure_the_perpendicular_gap():
    board = _pair_a()
    board.dp_rule()
    offset = SPACING / math.sqrt(2.0)
    board.track(AP, TOP, 100, 100, 400, 400, width=W)
    board.track(AN, TOP, 100 + offset, 100 - offset, 400 + offset, 400 - offset, width=W)
    layer = _layer(_row(_run(board)))
    assert layer["gap_mil"]["min"] == pytest.approx(5.0, abs=1e-3)
    assert layer["gap_mil"]["max"] == pytest.approx(5.0, abs=1e-3)
    assert layer["coupled_mil"]["positive"] == pytest.approx(300 * math.sqrt(2), abs=0.01)


def test_tracks_at_an_angle_or_side_by_side_without_overlap_are_not_coupled():
    board = _pair_a()
    board.dp_rule()
    board.track(AP, TOP, 100, 100, 300, 100, width=W)
    board.track(AN, TOP, 100, 110, 300, 160, width=W)  # 14 degrees off
    board.track(AP, MID, 100, 500, 300, 500, width=W)
    board.track(AN, MID, 320, 510, 500, 510, width=W)  # parallel, but no overlap
    row = _row(_run(board))
    assert row["coupled_mil"] == {"positive": 0.0, "negative": 0.0}
    assert row["uncoupled_mil"]["positive"] == pytest.approx(400.0)


def test_concentric_arcs_couple_along_their_common_angle():
    board = _pair_a()
    board.dp_rule()
    board.arc(AP, TOP, 500, 500, 100.0, 0.0, 90.0)
    board.arc(AN, TOP, 500, 500, 110.0, 0.0, 90.0)
    row = _row(_run(board))
    layer = _layer(row)
    assert layer["gap_mil"]["min"] == pytest.approx(5.0, abs=1e-3)
    assert layer["gap_mil"]["max"] == pytest.approx(5.0, abs=1e-3)
    assert layer["coupled_mil"]["positive"] == pytest.approx(math.pi / 2 * 100, abs=0.01)
    assert layer["coupled_mil"]["negative"] == pytest.approx(math.pi / 2 * 110, abs=0.01)
    assert row["uncoupled_mil"] == {"positive": 0.0, "negative": 0.0}
    assert layer["coupled_stretches"] == 1


def test_offset_arcs_vary_in_gap_and_couple_only_where_parallel():
    board = _pair_a()
    board.dp_rule(gap=(4.0, 5.0, 6.0))
    board.arc(AP, TOP, 500, 500, 100.0, 0.0, 180.0)
    board.arc(AN, TOP, 500, 496, 110.0, 0.0, 180.0)  # centre 4 mil down
    layer = _layer(_row(_run(board)))
    # At 90 degrees the gap is 10 - 4 - 5 = 1 mil; at 0 and 180 degrees about 5 mil.
    assert layer["gap_mil"]["min"] == pytest.approx(1.0, abs=0.05)
    assert layer["gap_mil"]["max"] == pytest.approx(4.92, abs=0.1)
    below = _findings(_run(board), "gap_below_min")
    assert below and below[0]["worst_gap_mil"] == pytest.approx(1.0, abs=0.05)


def test_an_arc_beside_a_track_couples_where_their_tangents_agree():
    board = _pair_a()
    board.dp_rule(gap=(4.0, 5.0, 30.0))
    board.arc(AP, TOP, 500, 400, 100.0, 45.0, 135.0)  # top of the arc at y = 500
    board.track(AN, TOP, 400, 510, 600, 510, width=W)
    layer = _layer(_row(_run(board)))
    coupled = layer["coupled_mil"]["positive"]
    # Within 10 degrees of the top: 20 degrees of a 100 mil radius arc.
    assert coupled == pytest.approx(math.radians(20) * 100, abs=2.5)
    assert layer["gap_mil"]["min"] == pytest.approx(5.0, abs=0.01)


def test_a_45_degree_bend_stays_coupled():
    board = _pair_a()
    board.dp_rule()
    inner = SPACING * math.tan(math.radians(22.5))
    # P outer: right then up-right; N inner (below and to the right of the bend).
    board.track(AP, TOP, 100, 100, 300, 100, width=W)
    board.track(AP, TOP, 300, 100, 400, 200, width=W)
    offset = SPACING / math.sqrt(2.0)
    board.track(AN, TOP, 100, 90, 300 + inner, 90, width=W)
    board.track(AN, TOP, 300 + inner, 90, 400 + offset, 200 - offset, width=W)
    row = _row(_run(board))
    assert row["coupled_mil"]["positive"] == pytest.approx(200 + 100 * math.sqrt(2), abs=0.5)
    assert row["uncoupled_mil"]["positive"] < 0.5
    assert _layer(row)["gap_mil"]["max"] == pytest.approx(5.0, abs=1e-3)


def test_zero_length_tracks_are_not_sampled():
    board = _pair_a()
    board.dp_rule()
    _straight(board)
    board.track(AP, TOP, 600, 100, 600, 100, width=W)
    layer = _layer(_row(_run(board)))
    assert layer["coupled_stretches"] == 1 and layer["coupled_mil"]["positive"] == 500.0


def test_a_zero_length_track_inside_a_leg_does_not_split_its_stretches():
    board = _pair_a()
    board.dp_rule()
    board.track(AP, TOP, 100, 100, 300, 100, width=W)
    board.track(AP, TOP, 300, 100, 300, 100, width=W)  # a dot where two tracks meet
    board.track(AP, TOP, 300, 100, 600, 100, width=W)
    board.track(AN, TOP, 100, 110, 285, 110, width=W)
    board.track(AN, TOP, 285, 110, 285, 112, width=W)
    board.track(AN, TOP, 285, 112, 315, 112, width=W)  # 30 mil at a 7 mil gap over the dot
    board.track(AN, TOP, 315, 112, 315, 110, width=W)
    board.track(AN, TOP, 315, 110, 600, 110, width=W)
    result = _run(board)
    assert _layer(_row(result))["coupled_stretches"] == 1
    (finding,) = _findings(result, "gap_above_max")
    assert finding["severity"] == "warning" and finding["stretch_count"] == 1


# ---------- gap and width against the rule ----------


@pytest.mark.parametrize("spacing, check", [
    (10.0, None), (10.05, None), (9.95, None),  # within GAP_TOLERANCE_MIL
    (10.3, "gap_above_max"), (9.5, "gap_below_min")])
def test_gap_at_and_around_the_limits(spacing, check):
    board = _pair_a()
    board.dp_rule()
    _straight(board, spacing=spacing)
    result = _run(board)
    gap_findings = [finding for finding in result["findings"]
                    if finding["check"] in ("gap_below_min", "gap_above_max")]
    row = _row(result)
    if check is None:
        assert not gap_findings
        assert row["uncoupled_mil"]["positive"] == 0.0
        assert row["coupled_in_gap_range_mil"]["positive"] == pytest.approx(500.0)
        return
    assert row["coupled_in_gap_range_mil"]["positive"] == 0.0
    assert row["coupled_mil"]["positive"] == pytest.approx(500.0)  # within the distance
    (finding,) = gap_findings
    assert finding["check"] == check and finding["severity"] == "warning"
    assert finding["length_mil"] == pytest.approx(500.0)
    assert finding["worst_gap_mil"] == pytest.approx(spacing - W, abs=1e-3)
    assert finding["limit_mil"] == 5.0 and finding["rule"] == "DiffPairsRouting_0"
    (stretch,) = finding["stretches"]
    assert stretch["from"]["y_mil"] == 100.0 and stretch["length_mil"] == pytest.approx(500.0)
    assert stretch["width_mil"] == {"positive": 5.0, "negative": 5.0}
    # Outside the gap range is uncoupled for Max Uncoupled Length.
    assert _row(result)["uncoupled_mil"]["positive"] == pytest.approx(500.0)


def test_short_deviations_are_info_and_min_stretch_moves_the_line():
    board = _pair_a()
    board.dp_rule()
    board.track(AP, TOP, 100, 100, 400, 100, width=W)
    board.track(AN, TOP, 100, 110, 200, 110, width=W)
    board.track(AN, TOP, 200, 110, 200, 112, width=W)
    board.track(AN, TOP, 200, 112, 215, 112, width=W)  # 15 mil at 7 mil gap
    board.track(AN, TOP, 215, 112, 215, 110, width=W)
    board.track(AN, TOP, 215, 110, 400, 110, width=W)
    (finding,) = _findings(_run(board), "gap_above_max")
    assert finding["severity"] == "info" and finding["length_mil"] == pytest.approx(15, abs=2.5)
    assert "info" in finding["message"]
    (finding,) = _findings(_run(board, min_stretch_mil=10.0), "gap_above_max")
    assert finding["severity"] == "warning"


def test_deviations_at_a_breakout_from_a_pad_are_info():
    board = _pair_a()
    board.dp_rule()
    # The legs leave their pads 12 mil apart (a 7 mil gap) for 40 mil, then close in.
    board.smd_pad(AP, 100, 100)
    board.smd_pad(AN, 100, 112)
    board.track(AP, TOP, 100, 100, 200, 100, width=W)
    board.track(AN, TOP, 100, 112, 140, 112, width=W)
    board.track(AN, TOP, 140, 112, 142, 110, width=W)
    board.track(AN, TOP, 142, 110, 400, 110, width=W)
    board.track(AP, TOP, 200, 100, 400, 100, width=W)
    result = _run(board)
    (finding,) = _findings(result, "gap_above_max")
    assert finding["severity"] == "info"
    assert finding["stretches"][0]["breakout"] == "pad"
    assert "leave a pad or via" in finding["message"]
    breakdown = _row(result)["uncoupled_breakdown_mil"]["positive"]
    assert breakdown["at_pads"] == pytest.approx(42.0, abs=2.5)
    assert breakdown["elsewhere"] == 0.0
    # Longer than BREAKOUT_MAX_MIL, the same run is a warning.
    board = _pair_a()
    board.dp_rule()
    board.smd_pad(AP, 100, 100)
    board.smd_pad(AN, 100, 112)
    board.track(AP, TOP, 100, 100, 400, 100, width=W)
    board.track(AN, TOP, 100, 112, 180, 112, width=W)
    board.track(AN, TOP, 180, 112, 182, 110, width=W)
    board.track(AN, TOP, 182, 110, 400, 110, width=W)
    (finding,) = _findings(_run(board), "gap_above_max")
    assert finding["severity"] == "warning" and "breakout" not in finding["stretches"][0]


def test_a_deviation_far_along_the_leg_from_its_pad_is_not_a_breakout():
    board = _pair_a()
    board.dp_rule()
    board.smd_pad(AP, 100, 100)
    board.smd_pad(AN, 100, 300)
    board.track(AP, TOP, 100, 100, 800, 100, width=W)
    # A_N leaves its pad far from A_P, then runs 40 mil at an 8 mil gap before closing in:
    # the stretch starts 200 mil along A_P from its pad.
    board.track(AN, TOP, 100, 300, 300, 300, width=W)
    board.track(AN, TOP, 300, 300, 300, 113, width=W)
    board.track(AN, TOP, 300, 113, 340, 113, width=W)
    board.track(AN, TOP, 340, 113, 340, 110, width=W)
    board.track(AN, TOP, 340, 110, 800, 110, width=W)
    result = _run(board)
    (finding,) = _findings(result, "gap_above_max")
    assert finding["severity"] == "warning" and "breakout" not in finding["stretches"][0]
    assert finding["length_mil"] == pytest.approx(40.0, abs=2.5)
    # Of the 240 mil uncoupled run from A_P's pad only the 6 mil inside the pad and the
    # first 50 mil beyond its edge are its breakout.
    breakdown = _row(result)["uncoupled_breakdown_mil"]["positive"]
    assert breakdown["at_pads"] == pytest.approx(56.0)
    assert breakdown["elsewhere"] == pytest.approx(184.0, abs=2.5)


def test_a_breakout_is_measured_from_the_pad_edge():
    board = _pair_a()
    board.dp_rule()
    board.smd_pad(AP, 100, 100, size=60.0)  # the legs start at the centres of large pads
    board.smd_pad(AN, 100, 200, size=60.0)
    board.track(AP, TOP, 100, 100, 600, 100, width=W)
    board.track(AN, TOP, 100, 200, 170, 200, width=W)
    board.track(AN, TOP, 170, 200, 170, 113, width=W)
    board.track(AN, TOP, 170, 113, 210, 113, width=W)  # 40 mil at an 8 mil gap
    board.track(AN, TOP, 210, 113, 210, 110, width=W)
    board.track(AN, TOP, 210, 110, 600, 110, width=W)
    result = _run(board)
    # The stretch starts 70 mil from A_P's pad centre, 40 mil from the pad's edge.
    (finding,) = _findings(result, "gap_above_max")
    assert finding["severity"] == "info" and finding["stretches"][0]["breakout"] == "pad"
    assert "breakout" in finding["message"]


def test_a_gap_just_past_the_limit_is_marginal_info():
    board = _pair_a()
    board.dp_rule()
    _straight(board, spacing=10.2)  # a 5.2 mil gap: 0.2 mil over the 5 mil maximum
    result = _run(board)
    (finding,) = _findings(result, "gap_above_max")
    assert finding["severity"] == "info" and finding["excess_mil"] == pytest.approx(0.2)
    assert finding["stretches"][0]["marginal"] is True
    assert "1 marginal" in finding["message"]
    # Still outside the gap range: uncoupled for Max Uncoupled Length.
    assert _row(result)["uncoupled_mil"]["positive"] == pytest.approx(500.0)


@pytest.mark.parametrize("width, side", [(5.0, None), (6.0, None), (4.0, None),
                                         (6.5, "above_max"), (3.5, "below_min")])
def test_width_at_and_around_the_limits(width, side):
    board = _pair_a()
    board.dp_rule()
    _straight(board, width=width, n_width=W, spacing=W / 2 + width / 2 + 5.0)
    findings = _findings(_run(board), "width_out_of_range")
    if side is None:
        assert not findings
        return
    (finding,) = findings
    assert finding["severity"] == "warning" and finding["side"] == side
    assert finding["net"] == "A_P" and finding["width_mil"] == width
    assert (finding["tracks"], finding["arcs"]) == (1, 0) and finding["length_mil"] == 500.0
    assert (finding["min_mil"], finding["max_mil"]) == (4.0, 6.0)
    assert finding["message"].startswith("1 track of A_P on Top Layer is ")


def test_an_arc_out_of_width_counts_once_as_check_rule_compliance_does():
    board = _pair_a()
    board.dp_rule()
    board.arc(AP, TOP, 300, 300, 100.0, 0.0, 90.0, width=3.0)  # stored as many chords
    board.arc(AN, TOP, 300, 300, 110.0, 0.0, 90.0, width=5.0)
    board.track(AP, TOP, 400, 300, 400, 200, width=3.0)
    (finding,) = _findings(_run(board), "width_out_of_range")
    assert (finding["tracks"], finding["arcs"]) == (1, 1)
    assert finding["message"].startswith("1 track and 1 arc of A_P on Top Layer are 3 mil")
    # The rule's own width: the same violation check_rule_compliance counts.
    assert finding["also_reported_by"] == "check_rule_compliance"
    assert "check_rule_compliance reports the same" in finding["message"]
    from server.services.pcb_compliance import check_compliance
    compliance = next(kind for kind in check_compliance(
        board.index(), kinds=["DiffPairsRouting"])["kinds"]
        if kind["kind"] == "DiffPairsRouting")
    assert compliance["violation_count"] == finding["tracks"] + finding["arcs"]


def test_gap_varies_within_a_wide_range():
    board = _pair_a()
    board.dp_rule(gap=(4.0, 5.0, 10.0))
    _straight(board, x1=100, x2=400)
    board.track(AP, TOP, 400, 100, 700, 100, width=W)
    board.track(AN, TOP, 400, 110, 400, 114, width=W)
    board.track(AN, TOP, 400, 114, 700, 114, width=W)  # 9 mil gap
    result = _run(board)
    (finding,) = _findings(result, "gap_varies")
    assert finding["severity"] == "info"
    assert finding["gap_mil"]["p5"] == pytest.approx(5.0, abs=0.01)
    assert finding["gap_mil"]["p95"] == pytest.approx(9.0, abs=0.01)
    assert not _findings(result, "gap_above_max")


# ---------- limits ----------


def test_rule_values_per_layer_and_priority_per_pair():
    board = Board()
    board.pair("PA", "A_P", "A_N")
    board.pair("PB", "B_P", "B_N")
    board.dp_rule(scope="InDifferentialPair('PA')", gap=(8.0, 8.0, 8.0), layers=("MIDLAYER1",))
    board.dp_rule(scope="All")
    _straight(board, layer=MID, spacing=13.0)  # PA: 8 mil gap on Mid
    _straight(board, BP, BN, y=300, layer=MID, spacing=13.0)  # PB: 8 mil vs 5 mil
    result = _run(board)
    pa, pb = _row(result, "PA"), _row(result, "PB")
    assert pa["rule"] == "DiffPairsRouting_0" and pb["rule"] == "DiffPairsRouting_1"
    assert _layer(pa, "Mid Signal")["limits"]["max_gap_mil"] == 8.0
    assert not [finding for finding in result["findings"] if finding["pair"] == "PA"]
    (finding,) = [finding for finding in result["findings"] if finding["pair"] == "PB"]
    assert finding["check"] == "gap_above_max" and finding["layer"] == "Mid Signal"


def test_rule_decided_per_object_by_layer():
    board = _pair_a()
    board.dp_rule(scope="InDifferentialPair('PA') and OnLayer('Mid Signal')",
                  gap=(8.0, 8.0, 8.0), layers=("MIDLAYER1",))
    board.dp_rule(scope="All")
    _straight(board, layer=TOP)
    _straight(board, y=300, layer=MID, spacing=13.0)
    result = _run(board)
    row = _row(result)
    assert _layer(row, "Top Layer")["limits"]["rule"] == "DiffPairsRouting_1"
    assert _layer(row, "Mid Signal")["limits"]["rule"] == "DiffPairsRouting_0"
    assert row["rules"] == ["DiffPairsRouting_0", "DiffPairsRouting_1"]
    assert result["findings"] == []


def test_without_a_rule_the_only_differential_profile_gives_the_limits():
    board = _pair_a()
    board.profile(low=(4.0, 4.0), high=(6.0, 6.0))
    _straight(board, spacing=12.0)  # 7 mil gap
    result = _run(board)
    row = _row(result)
    assert row["limit_source"] == "layer_profile" and row["profile"] == "D90"
    limits = _layer(row)["limits"]
    assert limits["source"] == "layer_profile" and limits["max_gap_mil"] == 6.0
    assert limits["preferred_gap_mil"] == 5.0
    (finding,) = _findings(result, "gap_above_max")
    assert finding["limit_source"] == "layer_profile" and finding["rule"] is None
    # No rule links the profile to the pair: an assumption, so info.
    assert finding["severity"] == "info" and finding["assumed_limits"] is True
    assert "assumed" in finding["message"]
    assert any("limit_source layer_profile" in note for note in result["notes"])


def test_two_differential_profiles_leave_the_pair_unjudged():
    board = _pair_a()
    board.profile(name="D90", low=(4.0, 4.0), high=(6.0, 6.0))
    board.profile(name="D100", low=(4.0, 6.0), high=(6.0, 8.0))
    _straight(board, spacing=12.0)
    result = _run(board)
    row = _row(result)
    assert row["limit_source"] == "none" and "max_gap_mil" not in _layer(row)["limits"]
    assert not _findings(result, "gap_above_max")
    assert any("limit_source none" in note for note in result["notes"])
    # Without a gap limit: coupled within 3x the width, uncoupled is what is not coupled.
    assert _layer(row)["coupling_distance_mil"] == 15.0
    assert row["uncoupled_basis"] == "coupling_distance"
    assert row["uncoupled_mil"]["positive"] == 0.0


def test_a_profile_driven_rule_takes_missing_widths_from_its_profile():
    board = _pair_a()
    profile_id = board.profile(low=(4.5, 4.0), high=(5.5, 6.0))
    board.rule("DiffPairsRouting", "All", MINLIMIT="5mil", MOSTFREQGAP="5mil",
               MAXLIMIT="5mil", MAXUNCOUPLEDLENGTH="500mil", IMPEDANCEPROFILEDRIVEN="TRUE",
               IMPEDANCEPROFILEID=profile_id)
    _straight(board, width=6.0, n_width=W, spacing=10.5)
    result = _run(board)
    limits = _layer(_row(result))["limits"]
    assert limits["source"] == "rule_and_profile" and limits["profile"] == "D90"
    assert (limits["min_width_mil"], limits["max_width_mil"]) == (4.5, 5.5)
    assert limits["max_gap_mil"] == 5.0  # the rule's own value wins
    (finding,) = _findings(result, "width_out_of_range")
    assert finding["limit_source"] == "rule_and_profile" and finding["side"] == "above_max"
    # A width from the profile: check_rule_compliance does not check it.
    assert "also_reported_by" not in finding and finding["severity"] == "warning"


def test_coupling_distance_overrides_the_default():
    board = _pair_a()
    board.dp_rule()
    _straight(board, spacing=25.0)  # 20 mil gap, beyond 3x the 5 mil max gap
    result = _run(board)
    assert _row(result)["coupled_mil"]["positive"] == 0.0
    assert not _findings(result, "gap_above_max")
    result = _run(board, coupling_distance_mil=25.0)
    assert _row(result)["coupled_mil"]["positive"] == 500.0
    assert _findings(result, "gap_above_max")[0]["worst_gap_mil"] == 20.0
    assert result["parameters"]["coupling_distance_mil"] == 25.0


# ---------- uncoupled length ----------


def test_uncoupled_length_against_max_uncoupled_length():
    board = _pair_a()
    board.dp_rule(uncoupled=100.0)
    _straight(board, x1=100, x2=400)
    board.track(AP, TOP, 400, 100, 550, 100, width=W)  # 150 mil the other leg does not follow
    board.track(AN, TOP, 400, 110, 400, 300, width=W)
    result = _run(board)
    row = _row(result)
    assert row["uncoupled_mil"]["positive"] == pytest.approx(150.0)
    assert row["uncoupled_mil"]["negative"] == pytest.approx(190.0, abs=2.5)
    (finding,) = _findings(result, "uncoupled_length_high")
    assert finding["severity"] == "warning" and finding["limit_mil"] == 100.0
    assert finding["uncoupled_breakdown_mil"]["elsewhere"] > 150.0
    assert "A_N of PA" in finding["message"]
    for side in ("positive", "negative"):
        assert row["routed_mil"][side] == pytest.approx(
            row["coupled_in_gap_range_mil"][side] + row["uncoupled_mil"][side], abs=0.02)
    assert result["summary"]["coupled_in_gap_range_length_mil"] == pytest.approx(600.0)
    board.rules.clear()
    board.dp_rule(uncoupled=500.0)
    assert not _findings(_run(board), "uncoupled_length_high")


def test_messages_give_long_lengths_without_an_exponent():
    board = _pair_a()
    board.dp_rule(uncoupled=100.0)
    board.track(AP, TOP, 0, 100, 1000, 100, width=W)
    board.track(AP, TOP, 1000, 100, 1000, 900, width=W)
    board.track(AN, TOP, 0, 500, 900, 500, width=W)
    (finding,) = _findings(_run(board), "uncoupled_length_high")
    assert "has 1800 mil not coupled" in finding["message"]
    from server.services.return_path import length_text
    assert [length_text(value, "mil") for value in (4.7244, 999.6, 1500.0)] == [
        "4.72 mil", "1000 mil", "1500 mil"]
    assert length_text(40000.0, "mm") == "1016 mm"


def test_without_max_uncoupled_length_a_large_share_is_info():
    board = _pair_a()
    _straight(board, x1=100, x2=300)
    board.track(AP, TOP, 300, 100, 600, 100, width=W)
    board.track(AN, TOP, 300, 110, 300, 400, width=W)
    result = _run(board)
    (finding,) = _findings(result, "uncoupled_length_high")
    assert finding["severity"] == "info" and finding["share"] == pytest.approx(0.6, abs=0.01)
    assert "no Max Uncoupled Length" in finding["message"]


def test_uncoupled_runs_end_at_vias_or_elsewhere():
    board = _pair_a()
    board.dp_rule()
    _straight(board, x1=200, x2=400)
    board.track(AP, TOP, 100, 100, 200, 100, width=W)  # from a via, alone
    board.via(AP, 100, 100, layer=TOP, layer_to=BOTTOM)
    board.track(AP, BOTTOM, 100, 100, 100, 50, width=W)
    breakdown = _row(_run(board))["uncoupled_breakdown_mil"]["positive"]
    # A run at the via is its breakout inside the via's copper (radius 10 mil) and up to
    # 50 mil beyond: 60 of the 100 on Top and the 50 on Bottom; the rest is elsewhere.
    assert breakdown["at_vias"] == pytest.approx(110.0)
    assert breakdown["elsewhere"] == pytest.approx(40.0)
    assert breakdown["at_pads"] == 0.0


# ---------- layer changes ----------


def _layer_change(board, net, x, y, layer_to=BOTTOM):
    board.via(net, x, y, diameter=8.0, hole=4.0, layer=TOP, layer_to=BOTTOM)
    board.track(net, layer_to, x, y, x + 200, y, width=W)


def test_matched_layer_changes_pair_up():
    board = _pair_a()
    board.dp_rule(layers=("TOPLAYER", "BOTTOMLAYER"))
    _straight(board, x1=100, x2=400)
    _layer_change(board, AP, 400, 100)
    _layer_change(board, AN, 400, 110)
    result = _run(board)
    changes = _row(result)["layer_changes"]
    assert changes["positive"] == changes["negative"] == changes["matched"] == 1
    assert changes["via_spacing_mil"] == {"min": 10.0, "max": 10.0}
    assert not _findings(result, "unmatched_layer_change")
    assert result["summary"]["layer_changes"] == {"vias": 2, "vias_matched": 2}


def test_a_stub_on_a_third_layer_at_a_via_does_not_break_the_match():
    board = _pair_a()
    board.dp_rule(layers=("TOPLAYER", "MIDLAYER1", "BOTTOMLAYER"))
    _straight(board, x1=100, x2=400)
    for net, y in ((AP, 100), (AN, 110)):
        board.via(net, 400, y, diameter=8.0, hole=4.0, layer=TOP, layer_to=BOTTOM)
        board.track(net, MID, 400, y, 600, y, width=W)
    board.track(AN, BOTTOM, 400, 110, 410, 110, width=W)  # a 10 mil stub on Bottom
    result = _run(board)
    assert _row(result)["layer_changes"]["matched"] == 1
    assert not _findings(result, "unmatched_layer_change")


def test_a_layer_change_without_the_other_leg_is_a_warning():
    board = _pair_a()
    board.dp_rule(layers=("TOPLAYER", "BOTTOMLAYER", "MIDLAYER1"))
    _straight(board, x1=100, x2=400)
    _layer_change(board, AP, 400, 100)
    _layer_change(board, AN, 400, 110, layer_to=MID)  # onto another layer
    _layer_change(board, AP, 400, 600)  # far from any via of the other leg
    board.track(AP, TOP, 400, 600, 300, 600, width=W)
    result = _run(board)
    (finding,) = _findings(result, "unmatched_layer_change")
    assert finding["severity"] == "warning" and finding["count"] == 3
    places = {(place["net"], place["at"]["y_mil"]): place for place in finding["vias"]}
    assert places[("A_N", 110.0)]["layers"] == ["Top Layer", "Mid Signal"]
    assert places[("A_P", 100.0)]["nearest_other_via"]["distance_mil"] == 10.0
    assert places[("A_P", 600.0)]["nearest_other_via"]["distance_mil"] == pytest.approx(490.0,
                                                                                         abs=0.01)
    narrow = _run(board, via_distance_mil=5.0)
    assert _row(narrow)["layer_changes"]["matched"] == 0


# ---------- pairs, routing and selection ----------


def test_inferred_pairs_are_measured_with_info_findings():
    board = _pair_a()
    board.dp_rule()
    _straight(board, CP, CN, y=300, spacing=13.0)
    _layer_change(board, CP, 600, 300)
    result = _run(board)
    row = _row(result, "C_P / C_N")
    assert row["inferred"] is True and row["style"] == "P/N"
    assert row["limit_source"] == "none" and row["rule"] is None
    assert _layer(row)["gap_mil"]["min"] == 8.0
    (finding,) = _findings(result, "unmatched_layer_change")
    assert finding["severity"] == "info" and finding["inferred_pair"] is True
    assert result["summary"]["inferred_pairs"] == 1
    assert any("inferred" in note for note in result["notes"])


def test_inferred_pairs_come_from_the_naming_check():
    index = Board().index()
    found = {(pair["positive"], pair["negative"], pair["style"])
             for pair in dpc.inferred_pairs(index)}
    assert found == {("A_P", "A_N", "P/N"), ("B_P", "B_N", "P/N"), ("C_P", "C_N", "P/N"),
                     ("D+", "D-", "+/-")}
    board = Board()
    board.pair("PA", "A_P", "A_N")
    assert ("A_P", "A_N", "P/N") not in {
        (pair["positive"], pair["negative"], pair["style"])
        for pair in dpc.inferred_pairs(board.index())}


def test_unrouted_and_partly_routed_pairs():
    board = Board()
    board.pair("PA", "A_P", "A_N")
    board.pair("PB", "B_P", "B_N")
    board.dp_rule(uncoupled=100.0)
    board.track(AP, TOP, 100, 100, 300, 100, width=W)
    result = _run(board, pairs=["PA", "PB"])
    (not_routed,) = _findings(result, "pair_not_routed")
    assert not_routed["pair"] == "PB" and not_routed["severity"] == "info"
    assert _row(result, "PB")["rule"] == "DiffPairsRouting_0"  # the pair's own rule
    (partial,) = _findings(result, "partially_routed")
    assert partial["pair"] == "PA" and partial["legs"] == {"A_N": "no_tracks"}
    assert not _findings(result, "uncoupled_length_high")  # a leg is missing


def test_a_leg_not_joining_its_pads_is_partially_routed():
    board = _pair_a()
    board.dp_rule()
    for net, y in ((AP, 100), (AN, 110)):
        board.smd_pad(net, 100, y, size=6.0)
        board.smd_pad(net, 600, y, size=6.0)
    _straight(board, x1=100, x2=500)
    (finding,) = _findings(_run(board), "partially_routed")
    assert finding["legs"] == {"A_P": "incomplete", "A_N": "incomplete"}


def test_selection_by_pair_net_and_class():
    board = Board()
    board.pair("PA", "A_P", "A_N")
    board.pair("PB", "B_P", "B_N")
    board.net_class("FAST", ["PB"], kind="diff_pair")
    board.net_class("LEGS", ["A_P", "A_N", "SIG"])
    index = board.index()
    names = {
        tuple(row["pair"] for row in dpg.prepare_diff_pair_geometry(index, **kwargs)["pairs"])
        for kwargs in [{"pairs": ["pb"]}]}
    assert names == {("PB",)}
    assert [row["pair"] for row in dpg.prepare_diff_pair_geometry(
        index, pairs=["A_N"])["pairs"]] == ["PA"]
    assert [row["pair"] for row in dpg.prepare_diff_pair_geometry(
        index, pairs=["D-"])["pairs"]] == ["D+ / D-"]
    assert [row["pair"] for row in dpg.prepare_diff_pair_geometry(
        index, net_classes=["FAST"])["pairs"]] == ["PB"]
    assert [row["pair"] for row in dpg.prepare_diff_pair_geometry(
        index, net_classes=["LEGS"])["pairs"]] == ["PA"]


def test_the_layer_filter_narrows_layers_and_findings():
    board = _pair_a()
    board.dp_rule(layers=("TOPLAYER", "MIDLAYER1"))
    _straight(board, spacing=12.0)
    _straight(board, y=300, layer=MID, spacing=12.0)
    result = _run(board, layer="Mid Signal")
    row = _row(result)
    assert [layer["layer"] for layer in row["layers"]] == ["Mid Signal"]
    assert {finding.get("layer") for finding in result["findings"]} == {"Mid Signal", None}
    assert {finding["check"] for finding in result["findings"] if "layer" not in finding} == {
        "uncoupled_length_high"}
    assert result["parameters"]["layer"] == "Mid Signal"


def test_checks_and_aliases_narrow_the_findings():
    board = _pair_a()
    board.dp_rule()
    _straight(board, spacing=12.0, width=7.0, n_width=W)
    result = _run(board, checks=["width"])
    assert result["checks_run"] == ["width_out_of_range"]
    assert {finding["check"] for finding in result["findings"]} == {"width_out_of_range"}
    gaps = _run(board, checks=["gap"])
    assert gaps["checks_run"] == ["gap_below_min", "gap_above_max", "gap_varies"]


@pytest.mark.parametrize("kwargs, error", [
    ({"checks": ["nope"]}, "invalid_checks"),
    ({"pairs": ["NOPE"]}, "pair_not_found"),
    ({"net_classes": ["Nope"]}, "net_class_not_found"),
    ({"layer": "Nope"}, "layer_not_found"),
])
def test_errors(kwargs, error):
    board = _pair_a()
    assert _run(board, **kwargs)["error"] == error


def test_paging_and_render():
    board = Board()
    for number, (positive, negative) in enumerate((("A_P", "A_N"), ("B_P", "B_N"))):
        board.pair(f"P{number}", positive, negative)
    board.dp_rule()
    _straight(board, spacing=12.0)
    _straight(board, BP, BN, y=300, spacing=12.0)
    prepared = _run(board)
    assert len(prepared["findings"]) == 2
    page = dpg.render_diff_pair_geometry(prepared, offset=0, limit=1, pairs=1)
    assert page["total"] == 2 and page["has_more"] and page["next_offset"] == 1
    assert len(page["findings"]) == 1 and len(page["pairs"]) == 1
    assert page["pairs_total"] == 2 and page["pairs_listed"] == 1
    last = dpg.render_diff_pair_geometry(prepared, offset=1, limit=1)
    assert not last["has_more"] and "next_offset" not in last
    assert page["next_pairs_offset"] == 1 and page["pairs_offset"] == 0
    second = dpg.render_diff_pair_geometry(prepared, pairs=1, pairs_offset=1)
    assert [row["pair"] for row in second["pairs"]] == [prepared["pairs"][1]["pair"]]
    assert second["pairs_offset"] == 1 and second["pairs_listed"] == 1
    assert "next_pairs_offset" not in second
    whole = dpg.render_diff_pair_geometry(prepared)
    assert len(whole["pairs"]) == 2 and "pairs_total" not in whole


def test_cached_results_are_reused_per_arguments():
    index = _pair_a().index()
    first = dpg.prepare_diff_pair_geometry_cached(index, pairs=None)
    assert dpg.prepare_diff_pair_geometry_cached(index, pairs=None) is first
    assert dpg.prepare_diff_pair_geometry_cached(index, pairs=["PA"]) is not first
    assert "error" in dpg.prepare_diff_pair_geometry_cached(index, pairs=["NOPE"])


def test_unrouted_inferred_pairs_are_left_to_the_naming_check():
    board = Board()
    board.track(N["SIG"], TOP, 0, 0, 100, 0)
    result = dpg.check_diff_pair_geometry(board.index())
    assert result["summary"]["pairs_analysed"] == 0
    assert result["summary"]["inferred_pairs_not_routed"] == 4
    assert result["findings"] == [] and result["pairs"] == []
    # Named, an unrouted inferred pair is reported.
    named = dpg.check_diff_pair_geometry(board.index(), pairs=["C_P"])
    assert [finding["check"] for finding in named["findings"]] == ["pair_not_routed"]


# ---------- the MCP tool ----------


@pytest.fixture
def tool_board(monkeypatch, tmp_path):
    board = _pair_a()
    board.pair("PB", "B_P", "B_N")
    board.dp_rule()
    _straight(board, spacing=12.0)
    _straight(board, BP, BN, y=300, spacing=12.0)
    index = board.index()
    pcb_file = tmp_path / "Board.PcbDoc"
    pcb_file.write_bytes(b"fake")
    monkeypatch.setattr(main_mod, "_project", {
        "name": "Demo", "root_dir": str(tmp_path), "prj_pcb_path": "x", "sheets": [],
        "pcb_doc_paths": [str(pcb_file)],
    })
    monkeypatch.setattr(main_mod._altium, "_netlist", None)
    monkeypatch.setattr(main_mod, "_variant_state",
                        VariantState([VariantDefinition(name="Prod", dnp_refdes=[])]))
    monkeypatch.setattr(main_mod._pcb_session, "_load", lambda path, netlist: index)
    main_mod._pcb_session._index = None
    yield index
    main_mod._pcb_session._index = None


def test_tool_runs_in_mils_and_millimetres(tool_board):
    result = json.loads(main_mod.check_diff_pair_geometry(pair="PA", units="mil"))
    assert result["pcb_doc"] == "Board.PcbDoc" and result["units"] == "mil"
    assert result["summary"]["findings_by_check"] == {"gap_above_max": 1}
    text = main_mod.check_diff_pair_geometry(pair="PA", units="mm", min_stretch=1.0)
    assert "_mil" not in text and " mil" not in text
    millimetres = json.loads(text)
    assert millimetres["parameters"]["min_stretch_mm"] == 1.0
    (finding,) = millimetres["findings"]
    assert finding["worst_gap_mm"] == 0.1778 and finding["limit_mm"] == 0.127
    assert finding["excess_mm"] == 0.0508
    row = millimetres["pairs"][0]
    assert row["coupled_in_gap_range_mm"] == {"positive": 0.0, "negative": 0.0}
    assert row["uncoupled_mm"]["positive"] == 12.7
    assert finding["stretches"][0]["from"] == {"x_mm": 2.5654, "y_mm": 2.54}
    assert " mm" in finding["message"]
    assert json.loads(main_mod.check_diff_pair_geometry(
        pair="PA", coupling_distance=0.1, units="mm"))["parameters"][
        "coupling_distance_mm"] == 0.1


@pytest.mark.parametrize("kwargs, error", [
    ({"units": "inch"}, "invalid_units"),
    ({"coupling_distance": 0}, "invalid_coupling_distance"),
    ({"min_stretch": -1}, "invalid_min_stretch"),
    ({"via_distance": float("nan")}, "invalid_via_distance"),
    ({"via_distance": 20000, "units": "mil"}, "invalid_via_distance"),
    ({"checks": ["nope"]}, "invalid_checks"),
    ({"pair": "NOPE"}, "pair_not_found"),
    ({"net_class": "Nope"}, "net_class_not_found"),
    ({"layer": "Nope"}, "layer_not_found"),
])
def test_tool_errors(tool_board, kwargs, error):
    assert json.loads(main_mod.check_diff_pair_geometry(**kwargs))["error"] == error


def test_tool_min_stretch_zero_is_allowed(tool_board):
    result = json.loads(main_mod.check_diff_pair_geometry(min_stretch=0, units="mil"))
    assert result["parameters"]["min_stretch_mil"] == 0.0


def test_tool_shrinks_its_page_to_fit(tool_board, monkeypatch):
    monkeypatch.setattr(main_mod, "QUERY_NET_MAX_RESULT_SIZE_CHARS", 6000)
    result = json.loads(main_mod.check_diff_pair_geometry())
    assert result["page_limited_to"] < 100
    assert len(json.dumps(result, indent=2)) <= 6000 or result["page_limited_to"] == 1


def test_tool_pages_the_pairs_rows(tool_board, monkeypatch):
    monkeypatch.setattr(main_mod, "DIFF_PAIR_GEOMETRY_STEPS", ((100, 10, 1),))
    first = json.loads(main_mod.check_diff_pair_geometry(units="mil"))
    assert first["pairs_total"] == 2 and first["next_pairs_offset"] == 1
    second = json.loads(main_mod.check_diff_pair_geometry(units="mil", pairs_offset=1))
    assert "next_pairs_offset" not in second
    assert {first["pairs"][0]["pair"], second["pairs"][0]["pair"]} == {"PA", "PB"}


def test_tool_without_a_project():
    main_mod._pcb_session._index = None
    original = main_mod._project
    try:
        main_mod._project = None
        assert json.loads(main_mod.check_diff_pair_geometry())["error"] == "no_project"
    finally:
        main_mod._project = original


def test_tool_registration_manifest_readme_and_instructions():
    tools = {tool.name: tool for tool in asyncio.run(main_mod.mcp.list_tools())}
    tool = tools["check_diff_pair_geometry"]
    assert tool.annotations.readOnlyHint is True
    assert tool.title == "Check Differential Pair Geometry"
    assert tool.meta["anthropic/maxResultSizeChars"] == main_mod.QUERY_NET_MAX_RESULT_SIZE_CHARS
    assert set(tool.inputSchema["properties"]) == {
        "pair", "net_class", "layer", "checks", "coupling_distance", "min_stretch",
        "via_distance", "units", "offset", "pairs_offset"}
    assert "next_pairs_offset" in tool.description
    assert "inferred" in tool.description and "next_offset" in tool.description
    assert "check_diff_pair_geometry" not in main_mod.NETLIST_TOOLS
    assert main_mod.SERVER_INSTRUCTIONS.index("check_diff_pair_geometry") > 4096
    manifest = json.loads((REPO / "manifest.json").read_text("utf-8"))
    assert "check_diff_pair_geometry" in {entry["name"] for entry in manifest["tools"]}
    assert "| `check_diff_pair_geometry` |" in (REPO / "README.md").read_text("utf-8")
    assert "`check_diff_pair_geometry`" in (REPO / "CHANGELOG.md").read_text("utf-8")
    from server.services.constraints_review import CONSTRAINTS_REVIEW_PROMPT
    from server.services.review_checklist import EVIDENCE_TOOLS
    assert "check_diff_pair_geometry" in EVIDENCE_TOOLS
    assert "check_diff_pair_geometry(pair=...)" in CONSTRAINTS_REVIEW_PROMPT
