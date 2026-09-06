"""Pick grading, roster leverage and trade fits (analysis.py)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis import (  # noqa: E402
    LETTER_CUTS, analyze, board_before, draft_grade, grade_pick, grade_picks,
    league_context, lineup_points, optimal_lineup, positional_breakdown,
    trade_chips, trade_targets, waiver_baseline,
)
from state import DraftState, Pick, Player  # noqa: E402
from tests.fixtures import ROSTER, cfg, synthetic_state  # noqa: E402

LETTERS = {letter for _, letter in LETTER_CUTS}


# ---- rewinding the board -------------------------------------------------------


def test_board_before_rewinds_availability_to_that_moment():
    st = synthetic_state()
    board = board_before(st, 25)
    ids = {p.id for p in board}
    assert {st.picks[o].player_id for o in range(1, 25)} & ids == set()
    assert st.picks[25].player_id in ids          # the pick's own player was available
    assert st.picks[40].player_id in ids          # someone taken later still was too


def test_board_before_is_sorted_by_rank():
    st = synthetic_state()
    ranks = [p.rank for p in board_before(st, 30)]
    assert ranks == sorted(ranks)


# ---- pick grades ---------------------------------------------------------------


def test_every_pick_is_graded_in_draft_order():
    st = synthetic_state(my_team_id=4)
    rows = grade_picks(st, 4)
    assert len(rows) == sum(1 for p in st.picks.values() if p.team_id == 4)
    assert [r["overall"] for r in rows] == sorted(r["overall"] for r in rows)
    assert all(r["grade"] in LETTERS for r in rows)
    assert all(0 <= r["score"] <= 100 for r in rows)


def test_every_grade_carries_a_reason():
    st = synthetic_state()
    assert all(r["reason"].strip() for r in grade_picks(st, 1))


def test_the_same_player_grades_better_the_later_he_is_taken():
    """Value is the gap between rank and where he actually went (§7 vocabulary)."""
    st = synthetic_state(rounds=4)
    target = board_before(st, 41)[20]
    early = grade_pick(st, Pick(overall=41, round=5, team_id=1, player_id=target.id))
    late = grade_pick(st, Pick(overall=95, round=10, team_id=1, player_id=target.id))
    assert late["score"] > early["score"]
    assert late["value_delta"] > early["value_delta"]


def test_unknown_player_is_reported_ungraded_rather_than_dropped():
    """Never lose a pick (§9) — and never guess a grade with no projection."""
    st = synthetic_state(rounds=2)
    st.picks[500] = Pick(overall=500, round=1, team_id=1, player_id=999999999)
    rows = grade_picks(st, 1)
    ungraded = [r for r in rows if r["overall"] == 500]
    assert len(ungraded) == 1
    assert ungraded[0]["grade"] is None
    assert "999999999" in ungraded[0]["name"]
    assert ungraded[0]["reason"].strip()


def test_passed_on_names_a_better_player_who_was_actually_available():
    st = synthetic_state()
    rows = [r for r in grade_picks(st, 1) if r["passed_on"]]
    assert rows
    for row in rows:
        alternative = row["passed_on"]
        assert alternative["vor_gap"] > 0
        assert any(p.name == alternative["name"] for p in board_before(st, row["overall"]))


def test_draft_grade_summarises_without_graded_picks():
    st = DraftState(cfg=cfg())
    assert draft_grade([])["letter"] is None
    assert draft_grade([])["summary"]


# ---- lineup --------------------------------------------------------------------


def test_optimal_lineup_starts_nobody_twice_and_fills_flex_with_the_best_left():
    players = [
        Player(id=1, name="RB A", position="RB", projected_points=300),
        Player(id=2, name="RB B", position="RB", projected_points=250),
        Player(id=3, name="RB C", position="RB", projected_points=240),
        Player(id=4, name="WR A", position="WR", projected_points=200),
        Player(id=5, name="WR B", position="WR", projected_points=190),
        Player(id=6, name="QB A", position="QB", projected_points=400),
        Player(id=7, name="TE A", position="TE", projected_points=120),
    ]
    lineup, bench = optimal_lineup(players, ROSTER)
    started = [p for group in lineup.values() for p in group]
    assert len(started) == len({p.id for p in started})
    assert [p.name for p in lineup["RB"]] == ["RB A", "RB B"]
    assert [p.name for p in lineup["FLEX"]] == ["RB C"]      # best flex-eligible left
    assert bench == []
    assert lineup["DST"] == [] and lineup["K"] == []          # nothing to fill them with


def test_optimal_lineup_is_never_worse_than_the_draft_order_slotting():
    st = synthetic_state()
    context = league_context(st)
    for tid in st.teams:
        by_draft = st.roster(tid)
        drafted_points = sum(
            st.players[st.picks[row["overall"]].player_id].projected_points
            for slot, rows in by_draft.items() if slot not in ("BENCH", "IR")
            for row in rows
        )
        assert lineup_points(context[tid]["lineup"]) >= drafted_points - 1e-6


# ---- positional strength -------------------------------------------------------


def test_positional_breakdown_matches_the_lineup_it_describes():
    st = synthetic_state()
    context = league_context(st)
    rows = positional_breakdown(st, 1, context)
    for row in rows:
        expected = sum(p.projected_points for group in context[1]["lineup"].values()
                       for p in group if p.position == row["position"])
        assert row["my_points"] == round(expected, 1)


def test_an_empty_slot_reads_as_unfilled_not_as_a_weakness():
    st = synthetic_state(rounds=3)               # far too early for a DST or K
    rows = {r["position"]: r for r in positional_breakdown(st, 1, league_context(st))}
    assert rows["DST"]["verdict"] == "unfilled"
    assert rows["K"]["verdict"] == "unfilled"


# ---- trades --------------------------------------------------------------------


def test_trade_chips_are_bench_players_worth_more_than_the_waiver_wire():
    st = synthetic_state()
    context = league_context(st)
    baseline = waiver_baseline(st)
    bench_names = {p.name for p in context[1]["bench"]}
    for chip in trade_chips(st, 1, context, baseline):
        assert chip["name"] in bench_names
        assert chip["surplus"] > 0
        assert chip["projected_points"] > baseline[chip["position"]]


def test_waiver_baseline_falls_back_to_the_worst_rostered_player_when_a_position_dries_up():
    st = synthetic_state(rounds=12)
    st.players = {pid: p for pid, p in st.players.items()
                  if p.position != "TE" or pid in st.drafted_ids}
    rostered_te = [st.player(p.player_id).projected_points for p in st.picks.values()
                   if st.player(p.player_id).position == "TE"]
    baseline = waiver_baseline(st)
    assert rostered_te                                   # the fixture drafted some
    assert baseline["TE"] == min(rostered_te)            # not 0, which would price
                                                         # every bench TE as pure surplus


def test_trade_targets_are_mutual_and_use_the_right_rosters():
    st = synthetic_state(my_team_id=1)
    context = league_context(st)
    targets = trade_targets(st, 1, context, waiver_baseline(st))
    assert targets
    mine = {p.name for p in context[1]["bench"]}
    for fit in targets:
        assert fit["team_id"] != 1
        theirs = {p.name for p in context[fit["team_id"]]["bench"]}
        assert fit["give"]["name"] in mine               # you can only trade what you own
        assert fit["get"]["name"] in theirs
        assert fit["give"]["position"] != fit["get"]["position"]
        assert fit["my_gap"] > 0 and fit["their_gap"] > 0  # both sides gain, or it is not a trade
        assert fit["rationale"].strip() and fit["balance"].strip()


def test_trade_targets_are_ranked_by_fit():
    st = synthetic_state()
    context = league_context(st)
    scores = [f["fit_score"] for f in trade_targets(st, 1, context, waiver_baseline(st))]
    assert scores == sorted(scores, reverse=True)


def test_the_team_that_hoarded_one_position_is_a_trade_partner():
    """Team 2 in the fixture drafts RBs and ignores WR — the whole point of a
    trade finder is that it notices."""
    st = synthetic_state(my_team_id=1)
    context = league_context(st)
    targets = trade_targets(st, 1, context, waiver_baseline(st), limit=10)
    assert 2 in {f["team_id"] for f in targets}


# ---- entry point ---------------------------------------------------------------


def test_analyze_returns_a_complete_report():
    report = analyze(synthetic_state(my_team_id=5))
    assert report["error"] is None
    assert report["team_id"] == 5 and report["is_me"] is True
    assert report["draft_grade"]["letter"] in LETTERS
    assert report["picks"] and report["leverage"] and report["positional"]
    assert 1 <= report["league_rank"] <= report["league_size"]
    assert report["projected_points"] > 0


def test_analyze_reads_any_team_not_just_mine():
    st = synthetic_state(my_team_id=1)
    report = analyze(st, team_id=7)
    assert report["team_id"] == 7 and report["is_me"] is False
    assert {r["overall"] for r in report["picks"]} == {
        o for o, p in st.picks.items() if p.team_id == 7}


def test_analyze_degrades_instead_of_raising_on_an_empty_draft():
    report = analyze(DraftState(cfg=cfg()))
    assert report["error"]
    assert report["picks"] == [] and report["trade_targets"] == []


def test_analyze_degrades_on_an_unknown_team():
    report = analyze(synthetic_state(), team_id=99)
    assert report["error"]
    assert report["picks"] == []


# ---- kickers and defenses ------------------------------------------------------


def test_a_mandatory_slot_filled_on_time_is_not_an_f():
    """Every roster carries a K and a DST, and their ESPN ranks sit hundreds of
    picks below where anyone takes them — that is the format, not a bad pick."""
    st = synthetic_state()
    rows = {r["position"]: r for r in grade_picks(st, 1)}
    for position in ("K", "DST"):
        assert rows[position]["score"] >= 45
        assert "reach" not in rows[position]["reason"].lower()


def test_a_kicker_taken_while_a_starting_slot_is_empty_grades_badly():
    st = synthetic_state(rounds=3)
    kicker = next(p for p in board_before(st, 40) if p.position == "K")
    early = grade_pick(st, Pick(overall=40, round=4, team_id=1, player_id=kicker.id))
    assert early["score"] < 46                       # C- or worse
    assert "still empty" in early["reason"]
