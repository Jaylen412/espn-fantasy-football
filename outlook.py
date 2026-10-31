"""Regular-season outlook: your season if you keep the roster vs. after simulated trades.

Pure computation, no I/O. The OpenAI summary (advisor.py) reads this output; it never
does the arithmetic.

Model, kept deliberately simple so it can be checked by hand:
  - A team's expected score each remaining week is its best starting lineup
    (`trade.lineup_strength`) — the same player values the trade screen uses.
  - Its week-to-week spread comes from its real scores so far, shrunk toward the
    league's average spread because seven games is a thin sample.
  - A game is won by the higher of two normal draws. Expected wins are exact
    (normal CDF on the point gap); playoff and seed odds come from a seeded Monte Carlo.
  - Both scenarios reuse the *same* random draws, so the difference between them is
    the effect of the trades, not sampling noise.

Known limits (shown to the user, not hidden): lineups are assumed optimal every week
(no byes or late injuries), so absolute points run high for every team alike; the
comparison between scenarios is what to trust. Playoff games are not modelled.
"""
from __future__ import annotations

import math
import random
import statistics
from typing import Any

from season import SeasonState
from trade import TradeError, describe_step, lineup_strength, replay

SIMS = 4000
SEED = 7
MIN_SIGMA = 8.0              # points; a floor so a lucky-consistent team is not treated as certain
SIGMA_PRIOR_WEIGHT = 3       # games of league-average spread blended into each team's own
FALLBACK_SIGMA = 20.0        # used only when no team has two completed games
DEFAULT_PLAYOFF_TEAMS = 6


class OutlookError(ValueError):
    """The outlook cannot be computed (unknown team, nothing left to play)."""


def _strengths(season: SeasonState, starters: dict[str, int]) -> dict[int, float]:
    return {tid: lineup_strength(season.roster(tid), starters) for tid in season.teams}


def _sigmas(season: SeasonState) -> dict[int, float]:
    scores = {tid: list(w.values()) for tid, w in season.weekly_scores.items()}
    spreads = [statistics.stdev(v) for v in scores.values() if len(v) > 1]
    league = statistics.mean(spreads) if spreads else FALLBACK_SIGMA
    out = {}
    for tid in season.teams:
        v = scores.get(tid, [])
        own = statistics.stdev(v) if len(v) > 1 else league
        n = len(v)
        out[tid] = max(MIN_SIGMA, (n * own + SIGMA_PRIOR_WEIGHT * league) / (n + SIGMA_PRIOR_WEIGHT))
    return out


def _win_prob(mu_a: float, sig_a: float, mu_b: float, sig_b: float) -> float:
    gap = (mu_a - mu_b) / math.sqrt(sig_a ** 2 + sig_b ** 2)
    return 0.5 * (1.0 + math.erf(gap / math.sqrt(2.0)))


def _percentile(sorted_vals: list[float], q: float) -> float:
    return sorted_vals[min(len(sorted_vals) - 1, int(q * len(sorted_vals)))]


def _monte_carlo(season: SeasonState, scenarios: list[tuple[dict, dict]], focus: int,
                 playoff_n: int, sims: int, seed: int) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    teams = sorted(season.teams)
    weeks = sorted({w for w, _, _ in season.schedule})
    base_wins = {t: season.teams[t].wins + 0.5 * season.teams[t].ties for t in teams}
    base_pf = {t: season.teams[t].points_for for t in teams}
    acc = [{"playoff": 0, "top": 0, "rank": 0.0, "wins": []} for _ in scenarios]

    for _ in range(sims):
        z = {(t, w): rng.gauss(0.0, 1.0) for w in weeks for t in teams}   # shared by every scenario
        for i, (mus, sigs) in enumerate(scenarios):
            wins, pf = dict(base_wins), dict(base_pf)
            for wk, h, a in season.schedule:
                sh = mus[h] + sigs[h] * z[(h, wk)]
                sa = mus[a] + sigs[a] * z[(a, wk)]
                pf[h] += sh
                pf[a] += sa
                if sh > sa:
                    wins[h] += 1
                elif sa > sh:
                    wins[a] += 1
                else:
                    wins[h] += 0.5
                    wins[a] += 0.5
            order = sorted(teams, key=lambda t: (-wins[t], -pf[t]))   # ESPN's default: wins, then points for
            rank = order.index(focus) + 1
            acc[i]["playoff"] += rank <= playoff_n
            acc[i]["top"] += rank == 1
            acc[i]["rank"] += rank
            acc[i]["wins"].append(wins[focus])
    return acc


def _scenario(season: SeasonState, mus: dict, sigs: dict, mc: dict, focus: int,
              sims: int) -> dict[str, Any]:
    games_left = [(w, h, a) for w, h, a in season.schedule if focus in (h, a)]
    team = season.teams[focus]
    cum = team.wins + 0.5 * team.ties
    weekly = []
    for wk, h, a in games_left:
        opp = a if h == focus else h
        p = _win_prob(mus[focus], sigs[focus], mus[opp], sigs[opp])
        cum += p
        weekly.append({"week": wk, "opponent_id": opp, "opponent_name": season.teams[opp].name,
                       "win_prob": round(p, 3), "my_points": round(mus[focus], 1),
                       "opp_points": round(mus[opp], 1), "cum_wins": round(cum, 2)})
    played = team.wins + team.losses + team.ties
    exp_wins = cum
    wins_sorted = sorted(mc["wins"])
    return {
        "expected_wins": round(exp_wins, 2),
        "expected_losses": round(played + len(games_left) - exp_wins, 2),
        "wins_range": [_percentile(wins_sorted, 0.1), _percentile(wins_sorted, 0.9)],
        "playoff_prob": round(mc["playoff"] / sims, 3),
        "top_seed_prob": round(mc["top"] / sims, 3),
        "expected_rank": round(mc["rank"] / sims, 2),
        "points_per_week": round(mus[focus], 1),
        "weekly": weekly,
    }


def season_outlook(real: SeasonState, starters: dict[str, int], team_id: int,
                   ledger: list[dict[str, Any]], playoff_override: int = 0,
                   sims: int = SIMS, seed: int = SEED) -> dict[str, Any]:
    """Compare `team_id`'s season on the real league vs. the league after `ledger`.

    Raises TradeError for a stale ledger and OutlookError when there is nothing to project.
    """
    if team_id not in real.teams:
        raise OutlookError(f"unknown team {team_id}")
    if not [g for g in real.schedule if team_id in g[1:]]:
        raise OutlookError("no regular-season games left to project")
    playoff_n = playoff_override or real.playoff_teams or DEFAULT_PLAYOFF_TEAMS

    sim_state = replay(real, ledger)
    # Spread is fixed from real results for both scenarios: trades change who scores,
    # not how volatile a team's week-to-week results have been.
    sigs = _sigmas(real)
    scenarios = [(_strengths(real, starters), sigs), (_strengths(sim_state, starters), sigs)]
    mc = _monte_carlo(real, scenarios, team_id, playoff_n, sims, seed)
    base = _scenario(real, *scenarios[0], mc[0], team_id, sims)
    sim = _scenario(real, *scenarios[1], mc[1], team_id, sims)

    delta = {k: round(sim[k] - base[k], 3) for k in
             ("expected_wins", "playoff_prob", "top_seed_prob", "expected_rank", "points_per_week")}
    return {
        "available": True,
        "team_id": team_id,
        "team_name": real.teams[team_id].name,
        "record_now": f"{real.teams[team_id].wins}-{real.teams[team_id].losses}",
        "weeks_remaining": len({w for w, h, a in real.schedule if team_id in (h, a)}),
        "playoff_teams": playoff_n,
        "sims": sims,
        "baseline": base,
        "simulated": sim,
        "delta": delta,
        "trades": [describe_step(real, s) for s in ledger],
    }
