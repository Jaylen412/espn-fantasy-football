"""Scores available players for the user's next pick. See MD/REQUIREMENTS.md §7.

Raw ESPN rank is not a recommendation — it ignores roster construction and
positional scarcity. Everything here exists to fix that.
"""
from __future__ import annotations

from typing import Any

from config import FLEX_ELIGIBLE, Config
from state import DraftState, Player

# Tunable mid-draft — the whole point of keeping them in one dict (§7).
WEIGHTS = {
    "vor": 1.00,        # w1 — value over replacement, the backbone
    "scarcity": 0.35,   # w2 — the cliff behind this player at his position
    "need": 0.30,       # w3 — unfilled starting slots
    "survival": 0.45,   # w4 — subtracted: penalise players likely to come back to you
}

TOP_N = 8
BEST_AVAILABLE_N = 10


def replacement_points(pool: list[Player], position: str, cfg: Config) -> float:
    """Projected points of the player at the replacement index for a position (§7).

    Index is TEAM_COUNT * starters_at_position within that position's remaining
    list; FLEX is shared across RB/WR/TE, so starters_at can be fractional.
    """
    at_pos = [p for p in pool if p.position == position]
    if not at_pos:
        return 0.0
    at_pos.sort(key=lambda p: -p.projected_points)
    idx = int(round(cfg.team_count * cfg.starters_at(position)))
    idx = min(max(idx, 0), len(at_pos) - 1)
    return at_pos[idx].projected_points


def survival_probability(player: Player, current_overall: int, picks_until_next: int) -> float:
    """Chance the player is still there at the user's next pick (§7 v1 formula).

    Subtracting this is the point of the whole tool: take the player who will not
    come back to you, not simply the highest-ranked one.
    """
    if picks_until_next <= 0:
        return 0.0
    gap = max(1, player.rank - current_overall)
    return max(0.0, min(1.0, 1 - picks_until_next / gap))


def roster_need(position: str, unfilled: dict[str, int], cfg: Config) -> float:
    """0..1. Highest when starting slots at this position are still open, and
    collapses toward zero once only bench spots remain (§7).
    """
    open_at_pos = unfilled.get(position, 0)
    if position in FLEX_ELIGIBLE:
        open_at_pos += unfilled.get("FLEX", 0)
    if open_at_pos <= 0:
        return 0.0
    capacity = max(1.0, cfg.starters_at(position))
    return min(1.0, open_at_pos / capacity)


def scarcity_bonus(player: Player, at_position: list[Player]) -> float:
    """Raw point drop-off between this player and the next-best at his position."""
    try:
        idx = at_position.index(player)
    except ValueError:
        return 0.0
    if idx + 1 >= len(at_position):
        return 0.0
    return max(0.0, player.projected_points - at_position[idx + 1].projected_points)


def _normalize(values: list[float]) -> list[float]:
    """Scale to 0..1 so the four terms are commensurate before weighting."""
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi - lo < 1e-9:
        return [0.0 for _ in values]
    return [(v - lo) / (hi - lo) for v in values]


def _reason(player: Player, terms: dict[str, float], vor: float, cliff: float,
            survival: float, picks_away: int) -> str:
    """One human-readable line naming whichever term actually drove the score.

    A number with no explanation is useless when there are 45 seconds on the
    clock (§7), and a reason that says the same thing about every player is no
    better than none — so this reports the dominant contribution, not a fixed
    priority ladder.
    """
    risk = "" if picks_away <= 0 or survival >= 0.35 else \
        f"; won't last {picks_away} picks"
    driver = max(("vor", "scarcity", "need"), key=lambda k: terms[k])

    if driver == "scarcity" and cliff >= 5:
        return f"{cliff:.0f}-pt cliff to the next {player.position}{risk}"
    if driver == "need":
        return f"Fills your open starting {player.position} slot{risk}"
    if vor > 0:
        return f"{vor:.0f} pts over replacement {player.position}{risk}"
    if survival < 0.35 and picks_away > 0:
        return f"Best {player.position} left and unlikely to return to you"
    return f"ESPN rank {player.rank}; {player.projected_points:.0f} projected"


def recommend(st: DraftState, limit: int = TOP_N) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    """(top recommendations, best available by position). Both come off the same
    locally-computed available pool (§5.4)."""
    cfg = st.cfg
    pool = st.available_players()
    if not pool:
        return [], {}

    _, picks_away = st.my_next_pick()
    picks_away = max(picks_away, 0)
    current = st.current_overall
    unfilled = st.unfilled_starters(cfg.my_team_id)

    by_position: dict[str, list[Player]] = {}
    for p in pool:
        by_position.setdefault(p.position, []).append(p)
    for players in by_position.values():
        players.sort(key=lambda p: -p.projected_points)

    replacement = {pos: replacement_points(pool, pos, cfg) for pos in by_position}

    # Only the plausible next picks are worth scoring; the tail is noise.
    candidates = pool[:80]

    rows = []
    for player in candidates:
        vor = player.projected_points - replacement.get(player.position, 0.0)
        rows.append({
            "player": player,
            "vor": vor,
            "cliff": scarcity_bonus(player, by_position.get(player.position, [])),
            "need": roster_need(player.position, unfilled, cfg),
            "survival": survival_probability(player, current, picks_away),
        })

    vor_n = _normalize([r["vor"] for r in rows])
    cliff_n = _normalize([r["cliff"] for r in rows])

    # Divide by the positive weight mass so scores read on a 0-100 scale (§6).
    positive_mass = WEIGHTS["vor"] + WEIGHTS["scarcity"] + WEIGHTS["need"]

    out = []
    for row, v, c in zip(rows, vor_n, cliff_n):
        player: Player = row["player"]
        terms = {
            "vor": WEIGHTS["vor"] * v,
            "scarcity": WEIGHTS["scarcity"] * c,
            "need": WEIGHTS["need"] * row["need"],
        }
        score = (sum(terms.values()) - WEIGHTS["survival"] * row["survival"]) / positive_mass
        out.append({
            "player_id": player.id,
            "name": player.name,
            "position": player.position,
            "team": player.pro_team,
            "espn_rank": player.rank,
            "projected_points": round(player.projected_points, 1),
            "vor": round(row["vor"], 1),
            "survival_prob": round(row["survival"], 2),
            "score": round(score * 100, 1),
            "reason": _reason(player, terms, row["vor"], row["cliff"],
                              row["survival"], picks_away),
        })

    out.sort(key=lambda r: -r["score"])

    best_by_position = {
        pos: [
            {
                "name": p.name, "position": p.position, "team": p.pro_team,
                "espn_rank": p.rank, "projected_points": round(p.projected_points, 1),
            }
            for p in players[:BEST_AVAILABLE_N]
        ]
        for pos, players in sorted(by_position.items())
        if pos != "?"
    }
    return out[:limit], best_by_position
