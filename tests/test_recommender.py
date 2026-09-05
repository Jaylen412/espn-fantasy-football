"""Recommendation engine behaviour (§7)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from recommender import recommend  # noqa: E402
from tests.test_state import loaded_state  # noqa: E402


def test_recommendations_come_only_from_available_players():
    st = loaded_state(12)
    recs, _ = recommend(st)
    assert recs
    assert not ({r["player_id"] for r in recs} & st.drafted_ids)


def test_every_recommendation_carries_a_reason():
    st = loaded_state(12)
    recs, _ = recommend(st)
    assert all(r["reason"].strip() for r in recs)


def test_recommendations_are_sorted_by_score():
    st = loaded_state(12)
    recs, _ = recommend(st)
    assert [r["score"] for r in recs] == sorted((r["score"] for r in recs), reverse=True)


def test_best_available_is_grouped_and_ranked_by_projection():
    st = loaded_state(12)
    _, best = recommend(st)
    assert {"QB", "RB", "WR", "TE"} <= set(best)
    for players in best.values():
        points = [p["projected_points"] for p in players]
        assert points == sorted(points, reverse=True)


def test_filled_starters_push_that_position_down_the_board():
    """Once a position's starters are full, need stops boosting it (§7)."""
    st = loaded_state(12)
    baseline, _ = recommend(st, limit=40)
    rb_scores = [r["score"] for r in baseline if r["position"] == "RB"]

    # Hand my team two RBs and the flex, filling every RB-eligible starting slot.
    rbs = [p for p in st.available_players() if p.position == "RB"][:3]
    overall = max(st.picks) + 1 if st.picks else 1
    st.apply_draft_detail({"draftDetail": {"inProgress": True, "picks": [
        {"overallPickNumber": overall + i, "roundId": 1,
         "teamId": st.cfg.my_team_id, "playerId": p.id}
        for i, p in enumerate(rbs)
    ]}})
    after, _ = recommend(st, limit=40)
    rb_after = [r["score"] for r in after if r["position"] == "RB"]
    assert rb_after and rb_scores
    assert max(rb_after) < max(rb_scores)


def test_empty_pool_returns_nothing_rather_than_raising():
    st = loaded_state(12)
    st.players.clear()
    assert recommend(st) == ([], {})


def test_scores_stay_on_a_0_100_scale():
    st = loaded_state(12)
    recs, _ = recommend(st, limit=40)
    assert max(r["score"] for r in recs) <= 100.0


def test_reasons_are_not_all_identical():
    """A reason that says the same thing about everyone is no better than none (§7)."""
    st = loaded_state(12)
    recs, _ = recommend(st, limit=8)
    assert len({r["reason"] for r in recs}) > 1
