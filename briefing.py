"""Decision-first briefing: what needs attention, where the roster is strong or weak, the
best trades to make, and the best *sequence* of trades. Everything is computed here so you
read conclusions instead of building trades; the optional AI briefing (advisor.py) only
ranks and explains what this module found.

Split in two so the page can paint the fast half first:
  overview()        status, attention list, position strength          (~0.3s)
  moves_and_plan()  best trades across the league + a chained plan     (a few seconds)
Both are pure; caching lives in app.py, keyed on `fingerprint()`.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from analysis import points_by_position
from outlook import OutlookError, season_outlook
from season import SeasonState
from suggest import improve_package
from trade import best_lineup, lineup_points_by_week, lineup_strength, weekly_value

MOVES = 3
PLAN_STEPS = 3
MIN_PLAN_GAIN = 0.005         # a plan step must add at least half a point of playoff odds...
MIN_PLAN_WINS = 0.05          # ...or five hundredths of a win
SEARCH_SIMS = 500             # enough to rank candidates; the outlook for the plan itself runs full size
SEARCH_PRESELECT = 8
REFINE_SIMS = 2000            # the shown gains are re-scored at this size so steps add up to the plan total
STRUCTURAL_GAP = 0.08         # a position this far (8%) below/above the league average is called out
TREND_DROP = 4.0              # points/game below season average over the last three
LINEUP_GAIN = 1.0             # pts/week a lineup change must be worth before it is worth mentioning
POSITION_ORDER = ["QB", "RB", "WR", "TE", "DST", "K"]
BYE_LOOKAHEAD = 6             # weeks ahead we look for bye weeks that hurt
BYE_LOSS = 4.0                # pts a bye week must cost before it is worth a line
SEVERITY_ORDER = {"high": 0, "med": 1, "info": 2}
INJURY_LABEL = {"OUT": "OUT", "INJURY_RESERVE": "on injured reserve", "SUSPENSION": "suspended",
                "QUESTIONABLE": "questionable", "DOUBTFUL": "doubtful"}


def fingerprint(real: SeasonState, team_id: int, playoff_override: int = 0) -> str:
    """Changes whenever anything the briefing depends on changes, so cached results are
    reused across refreshes but never outlive a roster move, injury or new week."""
    blob = {
        "team": team_id, "playoff": playoff_override, "period": real.period,
        "games": len(real.schedule), "scores": {t: sorted(w.items()) for t, w in sorted(real.weekly_scores.items())},
        "teams": {t: [(p.id, p.slot, p.injury, round(p.avg, 2), round(p.proj_week, 1))
                      for p in sorted(real.roster(t), key=lambda p: p.id)] for t in sorted(real.teams)},
    }
    return hashlib.sha1(json.dumps(blob, sort_keys=True).encode()).hexdigest()[:16]


def _standing(real: SeasonState, team_id: int) -> int:
    order = sorted(real.teams.values(), key=lambda t: (-(t.wins + 0.5 * t.ties), -t.points_for))
    return [t.id for t in order].index(team_id) + 1


def _positions(real: SeasonState, starters: dict[str, int], team_id: int) -> list[dict[str, Any]]:
    """Starter points by position for every team (best lineup, credited to real position)."""
    per_team = {t: points_by_position(best_lineup(real.roster(t), starters)[0]) for t in real.teams}
    out = []
    for pos in [p for p in POSITION_ORDER if any(p in v for v in per_team.values())]:
        vals = {t: v.get(pos, 0.0) for t, v in per_team.items()}
        avg = sum(vals.values()) / len(vals)
        rank = 1 + sum(1 for v in vals.values() if v > vals[team_id])
        out.append({"position": pos, "mine": round(vals[team_id], 1), "league_avg": round(avg, 1),
                    "diff_pct": round((vals[team_id] - avg) / avg, 3) if avg else 0.0,
                    "rank": rank, "teams": len(vals)})
    return out


def _attention(real: SeasonState, starters: dict[str, int], team_id: int,
               positions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    roster = real.roster(team_id)
    starting = [p for p in roster if p.slot not in ("BENCH", "IR")]
    items: list[dict[str, Any]] = []

    def add(severity, kind, text, ids=()):
        items.append({"severity": severity, "kind": kind, "text": text, "player_ids": list(ids)})

    week = real.period
    for p in starting:
        if p.is_inactive:
            add("high", "injured_starter", f"{p.name} ({p.position}) is {INJURY_LABEL.get(p.injury, p.injury)} "
                f"but is still in your {p.slot} slot. Replace him before kickoff.", [p.id])
        elif p.bye_week == week:
            add("high", "bye_starter", f"{p.name} ({p.position}) is on his bye this week but is still in your "
                f"{p.slot} slot. Replace him before kickoff.", [p.id])
        elif p.injury in INJURY_LABEL:
            add("med", "questionable_starter", f"{p.name} ({p.position}) is {INJURY_LABEL[p.injury]}. "
                "Check his status and have a backup ready.", [p.id])

    lineup, _ = best_lineup(roster, starters, week)
    for slot, need in starters.items():
        if slot in ("BENCH", "IR"):
            continue
        have = sum(1 for p in lineup.get(slot, []) if p.projected_points > 0)
        if have < need:
            add("high", "empty_slot", f"You have no available player (healthy and not on bye) for {need - have} "
                f"of your {slot} slot{'s' if need > 1 else ''} this week. A trade or a waiver pickup would fix it.")

    best_ids = {p.id for ps in lineup.values() for p in ps if p.projected_points > 0}
    now_ids = {p.id for p in starting}
    gain = lineup_strength(roster, starters, week) - sum(weekly_value(p, week) for p in starting)
    if gain >= LINEUP_GAIN and best_ids != now_ids:
        by_id = {p.id: p for p in roster}
        ins, outs = sorted(best_ids - now_ids), sorted(now_ids - best_ids)
        add("med", "set_lineup", "Your lineup is not your best: start "
            f"{', '.join(by_id[i].name for i in ins)}"
            + (f" in place of {', '.join(by_id[i].name for i in outs)}" if outs else "")
            + f" (about +{gain:.1f} pts this week).", ins + outs)

    # Upcoming byes: the weeks where the best lineup you can field falls furthest below full strength.
    weeks = [w for w in range(week + 1, min(real.final_period, week + BYE_LOOKAHEAD) + 1)]
    by_week, base = lineup_points_by_week(roster, starters, weeks)
    full = {p.id: p for ps in base["lineup"].values() for p in ps}
    by_id = {p.id: p for p in roster}
    costly = sorted(((base["points"] - pts, w) for w, pts in by_week.items()
                     if base["points"] - pts >= BYE_LOSS), reverse=True)[:2]
    for loss, w in sorted(costly, key=lambda t: t[1]):
        out = [f"{by_id[i].name} ({by_id[i].position})" for i in full if by_id[i].bye_week == w]
        add("med" if w == week + 1 else "info", "bye_week", f"Week {w} bye: {', '.join(out)} "
            f"{'is' if len(out) == 1 else 'are'} out, so your best lineup that week falls about {loss:.1f} "
            f"pts to {by_week[w]:.1f}. Plan cover now.", [i for i in full if by_id[i].bye_week == w])

    for p in sorted(starting, key=lambda p: p.recent_avg - p.avg)[:2]:
        if p.games >= 3 and p.recent_avg - p.avg <= -TREND_DROP and not p.is_inactive:
            add("info", "trending_down", f"{p.name} is trending down: {p.recent_avg:.1f} over his last "
                f"3 games vs {p.avg:.1f} on the season.", [p.id])

    if positions:
        worst = min(positions, key=lambda x: x["diff_pct"])
        best = max(positions, key=lambda x: x["diff_pct"])
        if worst["diff_pct"] <= -STRUCTURAL_GAP:
            add("med", "weak_position", f"{worst['position']} is your weakest spot: {worst['mine']} pts/week "
                f"vs a league average of {worst['league_avg']} (rank {worst['rank']} of {worst['teams']}).")
        if best["diff_pct"] >= STRUCTURAL_GAP:
            add("info", "strong_position", f"{best['position']} is your strength: {best['mine']} pts/week vs "
                f"{best['league_avg']} (rank {best['rank']} of {best['teams']}). Spare depth here is trade currency.")

    items.sort(key=lambda i: SEVERITY_ORDER[i["severity"]])
    for n, item in enumerate(items, 1):
        item["id"] = f"a{n}"
    return items


def overview(real: SeasonState, starters: dict[str, int], team_id: int,
             playoff_override: int = 0) -> dict[str, Any]:
    if team_id not in real.teams:
        raise ValueError(f"unknown team {team_id}")
    team = real.teams[team_id]
    status: dict[str, Any] = {"team_id": team_id, "team_name": team.name,
                              "record_now": f"{team.wins}-{team.losses}" + (f"-{team.ties}" if team.ties else ""),
                              "standing_now": _standing(real, team_id), "teams": len(real.teams),
                              "period": real.period, "projection": False}
    try:
        o = season_outlook(real, starters, team_id, [], playoff_override)
        b = o["baseline"]
        status.update(projection=True, weeks_remaining=o["weeks_remaining"], playoff_teams=o["playoff_teams"],
                      **{k: b[k] for k in ("expected_wins", "expected_losses", "wins_range", "playoff_prob",
                                           "top_seed_prob", "expected_rank", "points_per_week")})
    except OutlookError:
        status["weeks_remaining"] = 0                      # season over: show the record, not a projection
    positions = _positions(real, starters, team_id)
    return {"status": status, "attention": _attention(real, starters, team_id, positions),
            "positions": positions}


def _gain(a: dict[str, Any], b: dict[str, Any]) -> tuple[float, float]:
    return (round(b["simulated"]["playoff_prob"] - a["simulated"]["playoff_prob"], 3),
            round(b["simulated"]["expected_wins"] - a["simulated"]["expected_wins"], 2))


def _refine(real, starters, team_id, playoff_override, moves, steps, ledger) -> None:
    """Replace the quick search-time gains with larger, paired simulations. A plan step's gain
    is measured on top of the steps before it, so the steps sum to the plan's total."""
    def run(led):
        return season_outlook(real, starters, team_id, led, playoff_override, sims=REFINE_SIMS)
    try:
        base = run([])
        for m in moves:
            m["playoff_gain"], m["wins_gain"] = _gain(base, run([m["step"]]))
        prev = base
        for k, step in enumerate(steps, 1):
            cur = run(ledger[:k])
            step["playoff_gain"], step["wins_gain"] = _gain(prev, cur)
            prev = cur
    except OutlookError:
        pass                                               # season over: keep the lineup-based ranking


def moves_and_plan(real: SeasonState, starters: dict[str, int], team_id: int, roster_cap: int = 0,
                   playoff_override: int = 0) -> dict[str, Any]:
    """The best independent trades, and the best chained sequence of them."""
    def search(ledger):
        return improve_package(real, starters, team_id, [], [], ledger, roster_cap, playoff_override,
                               free_give=True, sims=SEARCH_SIMS, results=12, preselect=SEARCH_PRESELECT)

    first = search([])
    moves, used = [], set()
    for c in first["candidates"]:
        give = {p["id"] for p in c["give"]}
        if give & used:                                    # never offer the same player twice
            continue
        used |= give
        moves.append(c)
        if len(moves) == MOVES:
            break
    for i, m in enumerate(moves, 1):
        m["id"] = f"m{i}"
        m.pop("better_than_yours", None)

    ledger, steps, acquired, res = [], [], set(), first
    for _ in range(PLAN_STEPS):
        pick = next((c for c in res["candidates"]
                     if not ({p["id"] for p in c["give"]} & acquired)       # do not flip what you just got
                     and (not res["outlook"] or c["playoff_gain"] >= MIN_PLAN_GAIN
                          or c["wins_gain"] >= MIN_PLAN_WINS)), None)
        if not pick:
            break
        pick = {k: v for k, v in pick.items() if k != "better_than_yours"}
        steps.append(pick)
        ledger.append(pick["step"])
        acquired |= {p["id"] for p in pick["get"]}
        if len(steps) < PLAN_STEPS:
            res = search(ledger)
    _refine(real, starters, team_id, playoff_override, moves, steps, ledger)
    plan = None
    if steps:
        for i, s in enumerate(steps, 1):
            s["id"] = f"p{i}"
        try:
            o = season_outlook(real, starters, team_id, ledger, playoff_override, sims=REFINE_SIMS)
            outlook = {"baseline": o["baseline"], "simulated": o["simulated"], "delta": o["delta"],
                       "weeks_remaining": o["weeks_remaining"], "playoff_teams": o["playoff_teams"]}
            trades = o["trades"]
        except OutlookError:
            outlook, trades = None, []
        plan = {"steps": steps, "ledger": ledger, "outlook": outlook, "trades": trades}
    return {"moves": moves, "plan": plan, "searched": first["searched"], "qualified": first["qualified"],
            "projection": first["outlook"]}


def _compact(p: dict[str, Any]) -> dict[str, Any]:
    keep = ("name", "position", "avg", "recent_avg", "trend", "injury")
    return {k: p[k] for k in keep if k in p}


def _pkg(c: dict[str, Any]) -> dict[str, Any]:
    return {"id": c["id"], "partner": c["partner_name"],
            "send": [_compact(p) for p in c["give"]], "get": [_compact(p) for p in c["get"]],
            "you_weekly_delta": c["you_weekly_delta"], "them_weekly_delta": c["them_weekly_delta"],
            "playoff_gain": c["playoff_gain"], "wins_gain": c["wins_gain"],
            "flags": c.get("flags", []) + c.get("partner_flags", [])}


def ai_payload(over: dict[str, Any], mv: dict[str, Any]) -> tuple[dict[str, Any], set[str]]:
    """(what the model sees, the refs it may cite). Only computed numbers, nothing raw."""
    plan = mv.get("plan")
    payload = {
        "status": over["status"],
        "attention": [{k: a[k] for k in ("id", "severity", "text")} for a in over["attention"]],
        "positions": over["positions"],
        "moves": [_pkg(m) for m in mv["moves"]],
        "plan": None if not plan else {
            "id": "plan", "steps": [_pkg(s) for s in plan["steps"]], "trades": plan["trades"],
            "outlook": plan["outlook"] and {"delta": plan["outlook"]["delta"],
                                            "playoff_prob_before": plan["outlook"]["baseline"]["playoff_prob"],
                                            "playoff_prob_after": plan["outlook"]["simulated"]["playoff_prob"]}},
    }
    refs = {a["id"] for a in over["attention"]} | {m["id"] for m in mv["moves"]} | ({"plan"} if plan else set())
    return payload, refs
