"""Pure-function tests — snake order, picks_until_my_next_turn, VOR (§11.3)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from config import Config  # noqa: E402
from recommender import replacement_points, roster_need, survival_probability  # noqa: E402
from state import (  # noqa: E402
    DraftState, Player, assign_slot, build_snake_order, picks_until_next,
)

SLOTS = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "DST": 1, "K": 1, "BENCH": 7}


def cfg(team_count: int = 4, my_team: int = 3) -> Config:
    return Config(
        league_id=1, season=2026, espn_s2="", swid="", my_team_id=my_team,
        scoring_format="PPR", team_count=team_count, roster_slots=dict(SLOTS),
    )


# ---- snake order ----

def test_snake_reverses_every_other_round():
    assert build_snake_order([1, 2, 3, 4], 3) == [
        1, 2, 3, 4,      # round 1
        4, 3, 2, 1,      # round 2
        1, 2, 3, 4,      # round 3
    ]


def test_snake_respects_shuffled_pick_order():
    assert build_snake_order([3, 1, 2], 2) == [3, 1, 2, 2, 1, 3]


def test_snake_length_is_teams_times_rounds():
    assert len(build_snake_order(list(range(1, 13)), 16)) == 192


def test_snake_single_round_is_pick_order():
    assert build_snake_order([2, 5, 1], 1) == [2, 5, 1]


# ---- picks until next turn ----

@pytest.mark.parametrize("current,expected", [
    (1, (3, 2)),     # team 3 picks 3rd overall
    (3, (3, 0)),     # already on the clock — distance 0
    (4, (6, 2)),     # round 2 comes back at overall 6
    (7, (11, 4)),
])
def test_picks_until_next(current, expected):
    snake = build_snake_order([1, 2, 3, 4], 4)   # 1,2,3,4, 4,3,2,1, 1,2,3,4, 4,3,2,1
    assert picks_until_next(snake, current, team_id=3) == expected


def test_turn_boundary_is_back_to_back_at_the_wheel():
    """The team at the turn picks last in one round and first in the next."""
    snake = build_snake_order([1, 2, 3, 4], 2)
    # Team 4 is on the clock at overall 4, then again at overall 5.
    assert picks_until_next(snake, 4, team_id=4) == (4, 0)
    assert picks_until_next(snake, 5, team_id=4) == (5, 0)


def test_no_picks_left_returns_none():
    snake = build_snake_order([1, 2], 1)
    assert picks_until_next(snake, 3, team_id=1) == (None, -1)


def test_state_reports_my_next_pick_after_picks_land():
    st = DraftState(cfg=cfg(team_count=4, my_team=3))
    st.teams = {i: {"id": i, "name": f"T{i}"} for i in range(1, 5)}
    st.snake = build_snake_order([1, 2, 3, 4], 4)
    st.apply_draft_detail({"draftDetail": {"inProgress": True, "picks": [
        {"overallPickNumber": 1, "roundId": 1, "teamId": 1, "playerId": 10},
        {"overallPickNumber": 2, "roundId": 1, "teamId": 2, "playerId": 11},
    ]}})
    assert st.current_overall == 3
    assert st.my_next_pick() == (3, 0)


# ---- VOR ----

def _pool(points: list[float], position: str = "RB") -> list[Player]:
    return [Player(id=i, name=f"p{i}", position=position, projected_points=pts)
            for i, pts in enumerate(points, start=1)]


def test_replacement_is_the_player_at_the_starter_boundary():
    c = cfg(team_count=2)
    # RB starters = 2 dedicated + 1/3 of one FLEX -> 2.333 * 2 teams -> index 5.
    pool = _pool([200, 190, 180, 170, 160, 150, 140])
    assert replacement_points(pool, "RB", c) == 150.0


def test_replacement_clamps_to_the_shallow_end_of_a_thin_pool():
    c = cfg(team_count=12)
    pool = _pool([100, 90, 80])
    assert replacement_points(pool, "RB", c) == 80.0


def test_replacement_of_absent_position_is_zero():
    assert replacement_points(_pool([100]), "TE", cfg()) == 0.0


def test_vor_makes_positions_comparable():
    """A TE far above his replacement outranks a WR barely above his."""
    c = cfg(team_count=2)
    pool = _pool([200, 120, 118, 116, 114, 112], "WR") + _pool([180, 80, 78, 76], "TE")
    wr_repl = replacement_points(pool, "WR", c)
    te_repl = replacement_points(pool, "TE", c)
    assert 200 - wr_repl < 180 - te_repl


# ---- survival probability ----

def test_survival_is_zero_when_you_are_on_the_clock():
    p = Player(id=1, name="x", position="RB", rank=50)
    assert survival_probability(p, current_overall=10, picks_until_next=0) == 0.0


def test_survival_rises_with_a_bigger_rank_gap():
    near = Player(id=1, name="near", position="RB", rank=12)
    far = Player(id=2, name="far", position="RB", rank=90)
    assert survival_probability(near, 10, 6) < survival_probability(far, 10, 6)


def test_survival_is_clamped_to_unit_interval():
    p = Player(id=1, name="x", position="RB", rank=11)
    assert survival_probability(p, 10, 40) == 0.0
    assert 0.0 <= survival_probability(p, 10, 1) <= 1.0


# ---- roster slotting and need ----

def test_assign_slot_falls_through_starter_then_flex_then_bench():
    filled: dict[str, int] = {}
    assert assign_slot("RB", filled, SLOTS) == "RB"
    filled["RB"] = 2
    assert assign_slot("RB", filled, SLOTS) == "FLEX"
    filled["FLEX"] = 1
    assert assign_slot("RB", filled, SLOTS) == "BENCH"


def test_quarterback_never_takes_the_flex_slot():
    assert assign_slot("QB", {"QB": 1}, SLOTS) == "BENCH"


def test_need_collapses_once_starters_are_full():
    c = cfg()
    assert roster_need("RB", {"RB": 2, "FLEX": 1}, c) > 0
    assert roster_need("RB", {"RB": 0, "FLEX": 0}, c) == 0.0
