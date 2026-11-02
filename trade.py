"""Hypothetical-trade evaluation on live season data. Pure functions, no I/O.

The numbers here are what the OpenAI advisor reads (advisor.py) — the model never
does the arithmetic. Lineups are built with `analysis.optimal_lineup`, so a trade
is judged by what it does to the *starting* lineup, not by raw point totals:
swapping a bench player for a bench player is worth nothing.

A player's per-week value blends what he has actually done with what ESPN projects
for this week. Weights sit up top, like `recommender.WEIGHTS`, so they can be tuned.

Bye weeks: a player scores 0 in his team's bye week only, so every lineup is solved week
by week over the weeks still to play. A trade's "rest of season" effect is the sum of
those weekly lineup differences, not one average week times the weeks left, which is
what lets it see a bench player covering a bye or two starters sharing one.
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
BYE_CLASH = 3.0             # extra points a bye week must cost after a trade before we warn about it


class TradeError(ValueError):
    """The proposed trade cannot be evaluated (bad ids, same team, empty side)."""


def weekly_value(p: SeasonPlayer, week: int | None = None) -> float:
    """Expected points for one future week. Out/IR players score zero — they cannot start —
    and neither does a player in his bye week (`week` given and equal to `bye_week`)."""
    if p.is_inactive:
        return 0.0
    if week is not None and p.bye_week and p.bye_week == week:
        return 0.0
    if not p.games:
        return p.proj_week
    w = VALUE_WEIGHTS
    return (w["season_avg"] * p.avg + w["recent_avg"] * p.recent_avg
            + w["proj_week"] * (p.proj_week or p.avg))


def _as_player(p: SeasonPlayer, week: int | None = None, value: float | None = None) -> Player:
    """`value` is the player's precomputed full-strength weekly value, so solving many weeks
    for one roster does not recompute his averages each time."""
    if value is None:
        pts = weekly_value(p, week)
    else:
        pts = 0.0 if week is not None and p.bye_week and p.bye_week == week else value
    return Player(id=p.id, name=p.name, position=p.position, pro_team=p.pro_team,
                  projected_points=round(pts, 2))


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
        "stdev": round(p.stdev, 1), "trend": _trend(p), "bye_week": p.bye_week or None,
        "proj_week": round(p.proj_week, 1), "value": round(weekly_value(p), 1),
    }


def _side(players: list[SeasonPlayer], starters: dict[str, int], week: int | None = None,
          values: dict[int, float] | None = None) -> dict[str, Any]:
    lineup, bench = optimal_lineup(
        [_as_player(p, week, None if values is None else values[p.id]) for p in players], starters)
    return {"points": lineup_points(lineup), "by_position": points_by_position(lineup),
            "lineup": lineup, "bench": bench}


def lineup_strength(players: list[SeasonPlayer], starters: dict[str, int], week: int | None = None) -> float:
    """Expected points from the best starting lineup these players can field: in `week`
    (bye players score 0), or at full strength when no week is given."""
    return _side(players, starters, week)["points"]


def best_lineup(players: list[SeasonPlayer], starters: dict[str, int], week: int | None = None):
    """(slot -> Players, bench Players) for the best lineup these players can field. The
    returned Players carry weekly value in `projected_points`; OUT/IR and bye players read 0."""
    side = _side(players, starters, week)
    return side["lineup"], side["bench"]


def lineup_points_by_week(players: list[SeasonPlayer], starters: dict[str, int], weeks: list[int],
                          cache: dict | None = None) -> tuple[dict[int, float], dict[str, Any]]:
    """({week: best-lineup points}, the full-strength lineup). A bye can only change a week's
    lineup if it takes out someone who starts at full strength, so every other week reuses
    the full-strength answer and only the affected weeks are solved again.

    `cache` lets a search that scores thousands of rosters (suggest.py) reuse the ones that
    repeat, such as the two teams' current rosters. It must not outlive the player data."""
    key = None
    if cache is not None:
        key = (tuple(sorted(p.id for p in players)), tuple(weeks))
        if key in cache:
            return cache[key]
    values = {p.id: weekly_value(p) for p in players}
    base = _side(players, starters, None, values)
    by_id = {p.id: p for p in players}
    starting_byes = {by_id[p.id].bye_week for ps in base["lineup"].values() for p in ps
                     if p.id in by_id and by_id[p.id].bye_week}
    out = ({w: (_side(players, starters, w, values)["points"] if w in starting_byes else base["points"])
            for w in weeks}, base)
    if key is not None:
        cache[key] = out
    return out


def bench_ids(players: list[SeasonPlayer], starters: dict[str, int]) -> list[int]:
    """Players who are not in the best starting lineup — the ones a trade costs nothing."""
    return [p.id for p in _side(players, starters)["bench"]]


def _names(lineup: dict[str, list[Player]]) -> dict[str, list[str]]:
    return {slot: [p.name for p in ps] for slot, ps in lineup.items()}


def _view(before: dict, after: dict, by_before: dict[int, float], by_after: dict[int, float]) -> dict[str, Any]:
    """`before`/`after` are full-strength lineups (who starts, by position); `by_*` are the
    week-by-week points that include byes. Per-week figures are averages over the weeks left."""
    weeks = sorted(by_before)
    n = max(len(weeks), 1)
    mean_before, mean_after = sum(by_before.values()) / n, sum(by_after.values()) / n
    delta = mean_after - mean_before
    positions = sorted(set(before["by_position"]) | set(after["by_position"]))
    return {
        "before_points": round(mean_before, 1),
        "after_points": round(mean_after, 1),
        "weekly_delta": round(delta, 1),
        "rest_of_season_delta": round(sum(by_after.values()) - sum(by_before.values()), 1),
        "by_week": [{"week": w, "before": round(by_before[w], 1), "after": round(by_after[w], 1)} for w in weeks],
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


def _bye_aware(before_roster, after_roster, starters, weeks, cache=None):
    """(the view for one side, the bye detail the flags need)."""
    by_before, base_before = lineup_points_by_week(before_roster, starters, weeks, cache)
    by_after, base_after = lineup_points_by_week(after_roster, starters, weeks, cache)
    view = _view(base_before, base_after, by_before, by_after)
    return view, {"before": by_before, "after": by_after,
                  "base_before": base_before["points"], "base_after": base_after["points"]}


def _bye_flags(incoming: list[SeasonPlayer], bye: dict[str, Any], period: int) -> list[str]:
    """Warn about the two bye problems a trade can cause: an incoming player who cannot play
    this week, and a week where the new roster loses far more to byes than the old one did."""
    flags = [f"{p.name} is on his bye this week, so he cannot help until week {period + 1}"
             for p in incoming if p.bye_week == period]
    worse = []
    for w, after in bye["after"].items():
        extra = (bye["base_after"] - after) - (bye["base_before"] - bye["before"][w])   # bye cost after minus before
        if extra >= BYE_CLASH:
            worse.append((extra, w, after))
    for extra, w, after in sorted(worse, reverse=True)[:2]:
        flags.append(f"Week {w} bye: this trade costs you {extra:.1f} more points than usual that week "
                     f"(lineup {after:.1f})")
    return flags


def quick_deltas(season: SeasonState, starters: dict[str, int], team_a: int, team_b: int,
                 give_ids: list[int], get_ids: list[int], cache: dict) -> tuple[float, float]:
    """Average weekly lineup change (you, them) with byes counted — `evaluate_trade`'s two
    headline numbers without the display rows, for screening thousands of candidates. The
    caller guarantees the ids are valid; use `evaluate_trade` for anything shown to a person."""
    weeks = season.weeks_ahead or [season.period]
    out = []
    for tid, leave, arrive in ((team_a, give_ids, get_ids), (team_b, get_ids, give_ids)):
        roster = season.roster(tid)
        leaving = set(leave)
        incoming = [season.players[i] for i in arrive]
        after = [p for p in roster if p.id not in leaving] + incoming
        before_pts, _ = lineup_points_by_week(roster, starters, weeks, cache)
        after_pts, _ = lineup_points_by_week(after, starters, weeks, cache)
        out.append((sum(after_pts.values()) - sum(before_pts.values())) / len(weeks))
    return out[0], out[1]


def evaluate_trade(season: SeasonState, starters: dict[str, int], team_a: int, team_b: int,
                   give_ids: list[int], get_ids: list[int], roster_cap: int = 0,
                   cache: dict | None = None) -> dict[str, Any]:
    """Team A gives `give_ids` to team B and gets `get_ids` back. Raises TradeError
    on a trade that cannot be evaluated; everything else degrades to flags."""
    give, get = _validate(season, team_a, team_b, give_ids, get_ids)

    roster_a, roster_b = season.roster(team_a), season.roster(team_b)
    new_a = [p for p in roster_a if p.id not in set(give_ids)] + get
    new_b = [p for p in roster_b if p.id not in set(get_ids)] + give

    weeks = season.weeks_ahead or [season.period]
    left = season.weeks_left
    view_a, bye_a = _bye_aware(roster_a, new_a, starters, weeks, cache)
    view_b, bye_b = _bye_aware(roster_b, new_b, starters, weeks, cache)

    # Roster cap counts everything except IR, matching how ESPN seats players. Incoming
    # players always take a regular spot, whatever slot they held on their old team.
    count = lambda ps, incoming: sum(1 for p in ps if p.slot != "IR" or p in incoming)  # noqa: E731
    return {
        "period": season.period,
        "weeks_left": left,
        "you": {"team_id": team_a, "team_name": season.teams[team_a].name,
                "gives": [player_row(p) for p in give], "gets": [player_row(p) for p in get],
                **view_a,
                "flags": _flags(get, give, count(new_a, get), roster_cap)
                         + _bye_flags(get, bye_a, season.period)},
        "them": {"team_id": team_b, "team_name": season.teams[team_b].name,
                 "gives": [player_row(p) for p in get], "gets": [player_row(p) for p in give],
                 **view_b,
                 "flags": _flags(give, get, count(new_b, give), roster_cap)
                          + _bye_flags(give, bye_b, season.period)},
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
        by_week, _ = lineup_points_by_week(season.roster(tid), starters, season.weeks_ahead or [season.period])
        real = sum(by_week.values()) / max(len(by_week), 1)
        out[key]["real_points"] = round(real, 1)
        out[key]["vs_real"] = round(out[key]["after_points"] - real, 1)
    out["prior_trades"] = [describe_step(season, s) for s in ledger]
    return out
