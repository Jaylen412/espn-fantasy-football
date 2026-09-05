"""State ingestion, local availability, and cold-start recovery (§11.4, §12)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import Config  # noqa: E402
from state import DraftState  # noqa: E402

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


def cfg(my_team: int = 1) -> Config:
    return Config(
        league_id=1, season=2026, espn_s2="", swid="", my_team_id=my_team,
        scoring_format="PPR", team_count=12,
        roster_slots={"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1,
                      "DST": 1, "K": 1, "BENCH": 7},
    )


def loaded_state(frames: int = 12) -> DraftState:
    st = DraftState(cfg=cfg())
    st.load_league(json.loads((SAMPLES / "league_setup.json").read_text()))
    st.load_players(json.loads((SAMPLES / "player_pool.json").read_text()))
    st.apply_draft_detail(json.loads((SAMPLES / f"draft_detail_{frames:04d}.json").read_text()))
    return st


def test_league_setup_builds_teams_and_full_snake():
    st = loaded_state(1)
    assert len(st.teams) == 12
    assert len(st.snake) == 12 * 16
    assert st.rounds == 16


def test_players_parse_with_position_projection_and_rank():
    st = loaded_state(1)
    assert len(st.players) > 300
    top = min(st.players.values(), key=lambda p: p.rank)
    assert top.rank == 1
    assert top.projected_points > 0
    assert top.position in {"QB", "RB", "WR", "TE", "K", "DST"}


def test_drafted_players_leave_the_available_pool():
    """Availability is all_players - drafted_ids, never ESPN's status field (§5.4)."""
    st = loaded_state(12)
    available_ids = {p.id for p in st.available_players()}
    assert st.drafted_ids
    assert not (available_ids & st.drafted_ids)
    assert len(available_ids) == len(st.players) - len(st.drafted_ids)


def test_diff_returns_only_new_picks_and_is_idempotent():
    st = loaded_state(5)
    frame = json.loads((SAMPLES / "draft_detail_0005.json").read_text())
    assert st.apply_draft_detail(frame) == []          # replaying the same frame adds nothing
    nxt = json.loads((SAMPLES / "draft_detail_0006.json").read_text())
    new = st.apply_draft_detail(nxt)
    assert [p.overall for p in new] == [6]


def test_unknown_player_id_still_renders_the_pick():
    """Never lose a pick (§9)."""
    st = DraftState(cfg=cfg())
    st.apply_draft_detail({"draftDetail": {"inProgress": True, "picks": [
        {"overallPickNumber": 1, "roundId": 1, "teamId": 1, "playerId": 999999999},
    ]}})
    snap = st.snapshot()
    assert len(snap["picks"]) == 1
    assert "999999999" in snap["picks"][0]["player_name"]


def test_cold_start_midway_rebuilds_identical_state():
    """Restarting the server mid-draft must reconstruct full state (§12)."""
    incremental = DraftState(cfg=cfg())
    incremental.load_league(json.loads((SAMPLES / "league_setup.json").read_text()))
    incremental.load_players(json.loads((SAMPLES / "player_pool.json").read_text()))
    for i in range(1, 13):
        incremental.apply_draft_detail(
            json.loads((SAMPLES / f"draft_detail_{i:04d}.json").read_text()))

    cold = loaded_state(12)
    assert cold.drafted_ids == incremental.drafted_ids
    assert cold.current_overall == incremental.current_overall
    assert cold.snapshot()["picks"] == incremental.snapshot()["picks"]


def test_snapshot_marks_my_picks_and_my_turn():
    st = loaded_state(12)
    st.cfg.my_team_id = st.snake[12]        # whoever is on the clock at overall 13
    snap = st.snapshot()
    assert snap["current_pick"]["is_me"] is True
    assert snap["next_my_pick"]["picks_away"] == 0


def test_health_is_false_before_any_successful_poll():
    st = DraftState(cfg=cfg())
    assert st.is_healthy() is False
    assert st.snapshot()["connection_healthy"] is False
