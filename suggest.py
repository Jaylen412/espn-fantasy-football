"""Find better trade packages than the one you built, from any team in the league.

Python proposes, the model only chooses and explains (advisor.recommend_package): every
candidate here is a legal trade between two real rosters, evaluated with the same
`trade.evaluate_trade` and `outlook.season_outlook` the rest of the lab uses, so the AI
has nothing to invent. Pure computation, no I/O.

Search: keep your give side as the anchor (or, with `free_give`, also try adding or
swapping bench pieces), then for every other team try 1- and 2-player returns drawn from
that team's best healthy players. Candidates must help your lineup, not gut the partner's,
and not carry an injured starter. The best few are re-scored on season outlook and ranked by
the change in *your playoff odds*, then wins, then lineup points.
"""
from __future__ import annotations

from itertools import combinations
from typing import Any

from outlook import OutlookError, season_outlook
from season import SeasonPlayer, SeasonState
from trade import (TradeError, bench_ids, evaluate_trade, player_row, quick_deltas, replay,
                   weekly_value)

MIN_GAIN = 0.5            # pts/week your lineup must gain
MAX_PARTNER_LOSS = 1.5    # pts/week the partner may lose and still plausibly say yes
PER_TEAM_POOL = 8         # a team's best healthy players that we try asking for
MAX_GET = 2               # players we ask for in one trade
BENCH_POOL = 5            # free_give: bench pieces we try adding to the give side
PRESELECT = 12            # candidates re-scored on season outlook
PER_PARTNER_LIMIT = 3     # keep the shortlist from being one team repeated
RESULTS = 8
OUTLOOK_SIMS = 800        # enough to rank candidates; the shared random draws cancel most noise


def _pool(players: list[SeasonPlayer]) -> list[SeasonPlayer]:
    healthy = [p for p in players if not p.is_inactive]
    return sorted(healthy, key=lambda p: -weekly_value(p))[:PER_TEAM_POOL]


def _give_sets(league: SeasonState, starters: dict, team_id: int, give: list[int],
               free_give: bool) -> list[tuple[int, ...]]:
    sets: list[tuple[int, ...]] = [tuple(give)] if give else []
    if not free_give:
        return sets
    roster = league.roster(team_id)
    bench = [i for i in bench_ids(roster, starters) if i not in give][:BENCH_POOL]
    for b in bench:
        sets.append(tuple(give) + (b,))
        sets.append((b,))
    if len(give) > 1:
        sets += [tuple(g for g in give if g != drop) for drop in give]
    if not give:                                   # nothing anchored: also try pairs of your top spares
        sets += list(combinations(bench[:3], 2))
    return list(dict.fromkeys(s for s in sets if s))            # unique, order kept


def _outlook(real, starters, team_id, ledger, playoff_override, sims):
    try:
        return season_outlook(real, starters, team_id, ledger, playoff_override, sims=sims)
    except OutlookError:
        return None                                              # season over: rank by lineup only


def _gains(base, after) -> tuple[float, float]:
    if not base or not after:
        return 0.0, 0.0
    return (after["simulated"]["playoff_prob"] - base["simulated"]["playoff_prob"],
            after["simulated"]["expected_wins"] - base["simulated"]["expected_wins"])


def _step(team_id: int, partner: int, give, get) -> dict[str, Any]:
    return {"team_a": team_id, "team_b": partner, "give": list(give), "get": list(get)}


def _package(r: dict[str, Any], step: dict[str, Any], gain: tuple[float, float]) -> dict[str, Any]:
    you, them = r["you"], r["them"]
    return {
        "partner_id": step["team_b"], "partner_name": r["them"]["team_name"],
        "step": step,
        "give": you["gives"], "get": you["gets"],
        "you_weekly_delta": you["weekly_delta"], "them_weekly_delta": them["weekly_delta"],
        "you_rest_of_season": you["rest_of_season_delta"],
        "position_delta": you["position_delta"],
        "fit_score": r["acceptance"]["fit_score"], "acceptance": r["acceptance"]["label"],
        "playoff_gain": round(gain[0], 3), "wins_gain": round(gain[1], 2),
        # A full roster is not a reason to hide a trade (ESPN lets you drop to make room),
        # so the "a drop is needed" warnings travel with the package instead.
        "flags": you["flags"], "partner_flags": them["flags"],
    }


def improve_package(real: SeasonState, starters: dict[str, int], team_id: int,
                    give_ids: list[int], get_ids: list[int], ledger: list[dict[str, Any]],
                    roster_cap: int = 0, playoff_override: int = 0, free_give: bool = False,
                    partner_id: int | None = None, sims: int = OUTLOOK_SIMS,
                    results: int = RESULTS, preselect: int = PRESELECT) -> dict[str, Any]:
    """Grade the package you built and search the league for better ones.

    `partner_id` limits the search to one team; by default every other team is tried,
    including the one you originally picked. Raises TradeError on a bad package or a
    stale ledger.
    """
    if team_id not in real.teams:
        raise TradeError(f"unknown team {team_id}")
    league = replay(real, ledger)
    mine = {p.id: p for p in league.roster(team_id)}
    if not give_ids and not free_give:
        raise TradeError("add at least one player you would give, or let the search change your give side")
    for pid in give_ids:
        if pid not in mine:
            raise TradeError(f"player {pid} is not on team {team_id}")
    if len(set(give_ids)) != len(give_ids) or len(set(get_ids)) != len(get_ids):
        raise TradeError("a player appears twice")

    base = _outlook(real, starters, team_id, ledger, playoff_override, sims)
    cache: dict = {}                       # rosters repeat across candidates; valid for this one search

    yours = None
    if give_ids and get_ids:
        owners = {league.players[i].team_id for i in get_ids if i in league.players}
        if len(owners) != 1 or team_id in owners:
            raise TradeError("the players you want must all be on one other team")
        step = _step(team_id, owners.pop(), give_ids, get_ids)
        r = evaluate_trade(league, starters, **_kw(step), roster_cap=roster_cap, cache=cache)
        after = _outlook(real, starters, team_id, ledger + [step], playoff_override, sims)
        yours = _package(r, step, _gains(base, after))

    pool_by_team = {t: _pool(league.roster(t)) for t in league.teams if t != team_id}
    give_sets = _give_sets(league, starters, team_id, list(give_ids), free_give)

    found, searched = [], 0
    for partner, pool in pool_by_team.items():
        if partner_id is not None and partner != partner_id:
            continue
        for give in give_sets:
            for size in range(1, MAX_GET + 1):
                for get in combinations([p.id for p in pool], size):
                    if yours and (partner, tuple(give), tuple(get)) == (
                            yours["partner_id"], tuple(give_ids), tuple(get_ids)):
                        continue                                  # that is the package you built
                    step = _step(team_id, partner, give, get)
                    searched += 1
                    me, them = quick_deltas(league, starters, **_kw(step), cache=cache)
                    if me < MIN_GAIN or them < -MAX_PARTNER_LOSS:
                        continue
                    found.append((me + 0.5 * min(them, 0.0), step))

    found.sort(key=lambda t: -t[0])
    shortlist, per_partner = [], {}
    for pre, step in found:
        if per_partner.get(step["team_b"], 0) >= PER_PARTNER_LIMIT:
            continue
        per_partner[step["team_b"]] = per_partner.get(step["team_b"], 0) + 1
        # Only the shortlist gets the full evaluation (display rows, flags, by-week detail).
        shortlist.append((evaluate_trade(league, starters, **_kw(step), roster_cap=roster_cap, cache=cache), step))
        if len(shortlist) >= preselect:
            break

    packages = []
    for r, step in shortlist:
        after = _outlook(real, starters, team_id, ledger + [step], playoff_override, sims)
        packages.append(_package(r, step, _gains(base, after)))

    def key(p):
        return (p["playoff_gain"], p["wins_gain"], p["you_weekly_delta"])

    packages.sort(key=key, reverse=True)
    packages = packages[:results]
    for i, p in enumerate(packages, 1):
        p["id"] = f"c{i}"
        p["better_than_yours"] = bool(yours) and key(p) > key(yours)
    return {
        "available": True,
        "team_id": team_id, "team_name": real.teams[team_id].name,
        "free_give": free_give, "partner_filter": partner_id,
        "prior_trades": [],
        "outlook": bool(base),
        "yours": yours,
        "candidates": packages,
        "searched": searched, "qualified": len(found),
    }


def _kw(step: dict[str, Any]) -> dict[str, Any]:
    return {"team_a": step["team_a"], "team_b": step["team_b"],
            "give_ids": step["give"], "get_ids": step["get"]}
