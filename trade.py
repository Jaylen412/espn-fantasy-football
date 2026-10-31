"""Hypothetical-trade evaluation on live season data. Pure functions, no I/O.

The numbers here are what the OpenAI advisor reads (advisor.py) — the model never
does the arithmetic. Lineups are built with `analysis.optimal_lineup`, so a trade
is judged by what it does to the *starting* lineup, not by raw point totals:
swapping a bench player for a bench player is worth nothing.

A player's per-week value blends what he has actually done with what ESPN projects
for this week. Weights sit up top, like `recommender.WEIGHTS`, so they can be tuned.
"""
from __future__ import annotations

import copy
import math
from typing import Any

from analysis import lineup_points, optimal_lineup, points_by_position
from season import SeasonPlayer, SeasonState
from state import Player

VALUE_WEIGHTS = {"season_avg": 0.4, "recent_avg": 0.3, "proj_week": 0.3}
TREND_THRESHOLD = 2.0       # points/game gap between last-3 and season to call a trend
MIN_MUTUAL_GAIN = 0.5       # per-week points both sides must gain to read as "mutual"


class TradeError(ValueError):
    """The proposed trade cannot be evaluated (bad ids, same team, empty side)."""


def weekly_value(p: SeasonPlayer) -> float:
    """Expected points for one future week. Out/IR players score zero — they cannot start."""
    if p.is_inactive:
        return 0.0
    if not p.games:
        return p.proj_week
    w = VALUE_WEIGHTS
    return (w["season_avg"] * p.avg + w["recent_avg"] * p.recent_avg
            + w["proj_week"] * (p.proj_week or p.avg))


def _as_player(p: SeasonPlayer) -> Player:
    return Player(id=p.id, name=p.name, position=p.position, pro_team=p.pro_team,
                  projected_points=round(weekly_value(p), 2))


def _trend(p: SeasonPlayer) -> str:
    if p.games < 3:
        return "n/a"
    gap = p.recent_avg - p.avg
    return "up" if gap >= TREND_THRESHOLD else "down" if gap <= -TREND_THRESHOLD else "flat"


def player_row(p: SeasonPlayer) -> dict[str, Any]:
    return {
        "id": p.id, "name": p.name, "position": p.position, "pro_team": p.pro_team,
        "injury": p.injury, "slot": p.slot, "games": p.games,
        "avg": round(p.avg, 1), "recent_avg": round(p.recent_avg, 1),
        "stdev": round(p.stdev, 1), "trend": _trend(p),
        "proj_week": round(p.proj_week, 1), "value": round(weekly_value(p), 1),
    }


def _side(players: list[SeasonPlayer], starters: dict[str, int]) -> dict[str, Any]:
    lineup, bench = optimal_lineup([_as_player(p) for p in players], starters)
    return {"points": lineup_points(lineup), "by_position": points_by_position(lineup),
            "lineup": lineup, "bench": bench}


def lineup_strength(players: list[SeasonPlayer], starters: dict[str, int]) -> float:
    """Expected points per week from the best starting lineup these players can field."""
    return _side(players, starters)["points"]


def _names(lineup: dict[str, list[Player]]) -> dict[str, list[str]]:
    return {slot: [p.name for p in ps] for slot, ps in lineup.items()}


def _view(before: dict, after: dict, weeks_left: int) -> dict[str, Any]:
    delta = after["points"] - before["points"]
    positions = sorted(set(before["by_position"]) | set(after["by_position"]))
    return {
        "before_points": round(before["points"], 1),
        "after_points": round(after["points"], 1),
        "weekly_delta": round(delta, 1),
        "rest_of_season_delta": round(delta * weeks_left, 1),
        "position_delta": {pos: round(after["by_position"].get(pos, 0.0)
                                      - before["by_position"].get(pos, 0.0), 1)
                           for pos in positions
                           if abs(after["by_position"].get(pos, 0.0)
                                  - before["by_position"].get(pos, 0.0)) >= 0.05},
        "lineup_before": _names(before["lineup"]),
        "lineup_after": _names(after["lineup"]),
    }


def _validate(season: SeasonState, team_a: int, team_b: int,
              give: list[int], get: list[int]) -> tuple[list[SeasonPlayer], list[SeasonPlayer]]:
    if team_a == team_b:
        raise TradeError("pick two different teams")
    for tid in (team_a, team_b):
        if tid not in season.teams:
            raise TradeError(f"unknown team {tid}")
    if not give or not get:
        raise TradeError("add at least one player to each side")
    if len(set(give)) != len(give) or len(set(get)) != len(get):
        raise TradeError("a player appears twice")
    mine = {p.id: p for p in season.roster(team_a)}
    theirs = {p.id: p for p in season.roster(team_b)}
    for pid in give:
        if pid not in mine:
            raise TradeError(f"player {pid} is not on team {team_a}")
    for pid in get:
        if pid not in theirs:
            raise TradeError(f"player {pid} is not on team {team_b}")
    return [mine[i] for i in give], [theirs[i] for i in get]


def _flags(incoming: list[SeasonPlayer], outgoing: list[SeasonPlayer], roster_after: int,
           roster_cap: int) -> list[str]:
    flags = []
    for p in incoming:
        if p.is_inactive:
            flags.append(f"{p.name} is {p.injury} and cannot start")
        elif p.injury not in ("ACTIVE", "NORMAL"):
            flags.append(f"{p.name} is listed {p.injury}")
        if p.games < 3:
            flags.append(f"{p.name} has only {p.games} games of data")
        if _trend(p) == "down":
            flags.append(f"{p.name} is trending down (last 3 {p.recent_avg:.1f} vs season {p.avg:.1f})")
    if roster_cap and roster_after > roster_cap:
        flags.append(f"roster would hold {roster_after} players, over the {roster_cap}-player limit; a drop is needed")
    return flags


def _acceptance(delta_me: float, delta_them: float) -> dict[str, Any]:
    """Geometric mean of what each side gains (same idea as analysis.trade_targets):
    a trade that only helps one side scores near zero, however large that gain is."""
    fit = math.sqrt(max(delta_me, 0.0) * max(delta_them, 0.0))
    if delta_me >= MIN_MUTUAL_GAIN and delta_them >= MIN_MUTUAL_GAIN:
        label = "mutual"
    elif delta_them <= -MIN_MUTUAL_GAIN and delta_me >= MIN_MUTUAL_GAIN:
        label = "favors you; likely to be declined"
    elif delta_me <= -MIN_MUTUAL_GAIN and delta_them >= MIN_MUTUAL_GAIN:
        label = "favors them"
    elif delta_me <= -MIN_MUTUAL_GAIN and delta_them <= -MIN_MUTUAL_GAIN:
        label = "hurts both lineups"
    else:
        label = "roughly neutral for both lineups"
    return {"fit_score": round(fit, 2), "label": label}


def evaluate_trade(season: SeasonState, starters: dict[str, int], team_a: int, team_b: int,
                   give_ids: list[int], get_ids: list[int], roster_cap: int = 0) -> dict[str, Any]:
    """Team A gives `give_ids` to team B and gets `get_ids` back. Raises TradeError
    on a trade that cannot be evaluated; everything else degrades to flags."""
    give, get = _validate(season, team_a, team_b, give_ids, get_ids)

    roster_a, roster_b = season.roster(team_a), season.roster(team_b)
    new_a = [p for p in roster_a if p.id not in set(give_ids)] + get
    new_b = [p for p in roster_b if p.id not in set(get_ids)] + give

    left = season.weeks_left
    view_a = _view(_side(roster_a, starters), _side(new_a, starters), left)
    view_b = _view(_side(roster_b, starters), _side(new_b, starters), left)

    # Roster cap counts everything except IR, matching how ESPN seats players.
    count = lambda ps: sum(1 for p in ps if p.slot != "IR")  # noqa: E731
    return {
        "period": season.period,
        "weeks_left": left,
        "you": {"team_id": team_a, "team_name": season.teams[team_a].name,
                "gives": [player_row(p) for p in give], "gets": [player_row(p) for p in get],
                **view_a,
                "flags": _flags(get, give, count(new_a), roster_cap)},
        "them": {"team_id": team_b, "team_name": season.teams[team_b].name,
                 "gives": [player_row(p) for p in get], "gets": [player_row(p) for p in give],
                 **view_b,
                 "flags": _flags(give, get, count(new_b), roster_cap)},
        "acceptance": _acceptance(view_a["weekly_delta"], view_b["weekly_delta"]),
    }


# ---- simulation: chained trades on a copy of the league ---------------------------
#
# A ledger is an ordered list of {team_a, team_b, give, get} steps. The server holds
# no simulation state: each request replays the ledger onto a copy of the real
# SeasonState, so a refresh from ESPN or an undo is just a shorter/different list.


def apply_trade(season: SeasonState, team_a: int, team_b: int,
                give_ids: list[int], get_ids: list[int]) -> SeasonState:
    """A new SeasonState with the players moved. Slots reset to BENCH because every
    evaluation re-optimizes the lineup anyway; the input is never mutated."""
    _validate(season, team_a, team_b, give_ids, get_ids)
    sim = copy.deepcopy(season)
    moves = ((give_ids, team_a, team_b), (get_ids, team_b, team_a))
    for ids, src, dst in moves:
        sim.teams[src].player_ids = [i for i in sim.teams[src].player_ids if i not in set(ids)]
        sim.teams[dst].player_ids.extend(ids)
        for pid in ids:
            sim.players[pid].team_id = dst
            sim.players[pid].slot = "BENCH"
    return sim


def replay(season: SeasonState, ledger: list[dict[str, Any]]) -> SeasonState:
    """Apply every step in order. A step that no longer fits (a player was really
    traded or dropped since) means the ledger is stale: say which one, never guess."""
    sim = season
    for n, step in enumerate(ledger, 1):
        try:
            sim = apply_trade(sim, step["team_a"], step["team_b"], step["give"], step["get"])
        except (TradeError, KeyError) as exc:
            raise TradeError(f"simulation is out of date at trade {n} ({exc}); reset it") from exc
    return sim


def describe_step(season: SeasonState, step: dict[str, Any]) -> str:
    name = lambda i: season.players[i].name if i in season.players else f"player {i}"  # noqa: E731
    return (f"{season.teams[step['team_a']].name} sent {', '.join(name(i) for i in step['give'])} to "
            f"{season.teams[step['team_b']].name} for {', '.join(name(i) for i in step['get'])}")


def evaluate_simulated(season: SeasonState, starters: dict[str, int], team_a: int, team_b: int,
                       give_ids: list[int], get_ids: list[int], ledger: list[dict[str, Any]],
                       roster_cap: int = 0) -> dict[str, Any]:
    """Evaluate the next trade on top of the ledger, and report each side against the
    real (un-simulated) league so the running total is visible, not just this step."""
    sim = replay(season, ledger)
    out = evaluate_trade(sim, starters, team_a, team_b, give_ids, get_ids, roster_cap)
    for key, tid in (("you", team_a), ("them", team_b)):
        real = _side(season.roster(tid), starters)["points"]
        out[key]["real_points"] = round(real, 1)
        out[key]["vs_real"] = round(out[key]["after_points"] - real, 1)
    out["prior_trades"] = [describe_step(season, s) for s in ledger]
    return out
