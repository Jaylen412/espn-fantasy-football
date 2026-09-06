"""A synthetic drafted league, built in memory.

`samples/` holds real dumped responses whose draft never produced usable picks
(every playerId came back -1), so the analysis tests build their own league
here instead: a full snake draft with deliberate imbalances, so there is
something for the grader and the trade finder to actually find.

Picks are constructed directly rather than through `apply_draft_detail`, which
appends to draft_log.jsonl — ingestion is already covered by test_state.py.
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import Config  # noqa: E402
from state import DraftState, Pick, Player, build_snake_order  # noqa: E402

TEAM_COUNT = 10
ROSTER = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "DST": 1, "K": 1, "BENCH": 7}
ROUNDS = sum(ROSTER.values())

# (position, how many exist, best projection, per-player falloff, draft-day premium)
POOL_SHAPE = [
    ("RB", 70, 300.0, 3.4, 60.0),
    ("WR", 90, 285.0, 2.6, 50.0),
    ("QB", 32, 380.0, 4.5, 0.0),
    ("TE", 30, 230.0, 5.0, 25.0),
    ("DST", 20, 130.0, 2.0, -60.0),
    ("K", 20, 140.0, 1.5, -80.0),
]


def cfg(my_team_id: int = 1) -> Config:
    return Config(
        league_id=1, season=2026, espn_s2="", swid="", my_team_id=my_team_id,
        scoring_format="PPR", team_count=TEAM_COUNT, roster_slots=dict(ROSTER),
    )


def _build_players(rng: random.Random) -> list[Player]:
    players: list[Player] = []
    pid = 100000
    for position, count, top, falloff, premium in POOL_SHAPE:
        for n in range(count):
            pid += 1
            proj = max(20.0, top - n * falloff * rng.uniform(0.85, 1.15))
            players.append(Player(id=pid, name=f"{position} {n + 1}", position=position,
                                  pro_team="KC", projected_points=round(proj, 1)))
            # Draft rank is not projection order: a QB outscores every RB and still
            # goes later. The premium is what turns projection into draft value.
            players[-1].auction_value = proj + premium
    players.sort(key=lambda p: -p.auction_value)
    for rank, player in enumerate(players, start=1):
        player.rank = rank
    return players


def _wants(position: str, filled: dict[str, int], rnd: int) -> bool:
    """A crude but league-realistic sense of when a roster wants a position."""
    missing_kicking = [p for p in ("DST", "K") if filled.get(p, 0) == 0]
    if rnd > ROUNDS - len(missing_kicking):
        return position in missing_kicking      # last rounds go to the empty slots
    if position in ("K", "DST"):
        return False
    if position == "QB":
        return filled.get("QB", 0) == 0 or rnd >= 12
    if position == "TE":
        return filled.get("TE", 0) == 0 or rnd >= 11
    return filled.get(position, 0) < 6


def synthetic_state(my_team_id: int = 1, rounds: int = ROUNDS, seed: int = 11) -> DraftState:
    """A drafted league with a hoarder (team 2 stockpiles RBs) and a team that
    punts TE (team 3) — the two shapes a trade finder has to notice."""
    rng = random.Random(seed)
    st = DraftState(cfg=cfg(my_team_id))
    st.rounds = ROUNDS
    st.draft_status = "IN_PROGRESS"
    st.teams = {tid: {"id": tid, "name": f"Team {tid}", "owner": f"owner{tid}"}
                for tid in range(1, TEAM_COUNT + 1)}
    st.snake = build_snake_order(list(range(1, TEAM_COUNT + 1)), ROUNDS)

    for player in _build_players(rng):
        st.players[player.id] = player

    filled: dict[int, dict[str, int]] = {tid: {} for tid in st.teams}
    taken: set[int] = set()
    total = min(rounds, ROUNDS) * TEAM_COUNT

    for overall in range(1, total + 1):
        team_id = st.snake[overall - 1]
        rnd = (overall - 1) // TEAM_COUNT + 1
        board = sorted((p for p in st.players.values() if p.id not in taken),
                       key=lambda p: p.rank)

        def acceptable(player) -> bool:
            position = player.position
            if team_id == 2 and position in ("WR", "TE") and rnd <= 10:
                return False                               # hoards RBs, ignores WR early
            if team_id == 3 and position == "TE":
                return False                               # punts TE entirely
            return _wants(position, filled[team_id], rnd)

        # Look a few deep so the draft produces reaches as well as best-available
        # picks, then fall through to the whole board rather than to rank 1 —
        # otherwise nobody ever drafts a kicker.
        window = board[:12 if rng.random() < 0.25 else 4]
        choice = next((p for p in window if acceptable(p)),
                      next((p for p in board if acceptable(p)), board[0]))
        taken.add(choice.id)
        filled[team_id][choice.position] = filled[team_id].get(choice.position, 0) + 1
        st.picks[overall] = Pick(overall=overall, round=rnd, team_id=team_id,
                                 player_id=choice.id)

    if total >= ROUNDS * TEAM_COUNT:
        st.draft_status = "COMPLETE"
    return st
