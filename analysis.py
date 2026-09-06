"""Draft grading, roster leverage, and trade fits.

The after-the-fact companion to `recommender.py`. The recommender asks "who
should I take next?"; this asks "how did the picks I already made turn out, what
is this roster actually good at, and who should I call about a trade?". It
reuses the same vocabulary as §7 — projected points, replacement level, VOR,
roster need — so a grade here is commensurate with a score there.

Deliberately kept off the poll loop: grading a pick re-derives the board as it
stood at that moment, which is far too much work to redo every four seconds, and
§6 wants the SSE snapshot small. It runs on demand at /api/analysis instead.
"""
from __future__ import annotations

import math
from typing import Any

from config import FLEX_ELIGIBLE, Config
from recommender import roster_need
from state import DraftState, Pick, Player, assign_slot

# Tunable in one place, same as recommender.WEIGHTS (§7).
GRADE_WEIGHTS = {
    "value": 0.35,   # where he went relative to his ESPN rank — fell to you vs. reach
    "board": 0.50,   # how much of the board's remaining value he actually captured
    "need": 0.15,    # whether the pick filled a starting slot that was still open
}

CANDIDATE_DEPTH = 60      # how far down the board still counts as a realistic alternative
SPECIALISTS = ("K", "DST")  # one mandatory slot, no flex, no depth value
REACH_SLACK_ROUNDS = 1.5  # a full grade swing is worth this many rounds of rank difference
TRADE_TARGET_LIMIT = 3
MIN_TRADE_FIT = 1.0       # below this the "fit" is noise, not a trade idea

LETTER_CUTS = (
    (90, "A+"), (82, "A"), (76, "A-"), (70, "B+"), (64, "B"), (58, "B-"),
    (52, "C+"), (46, "C"), (40, "C-"), (30, "D"), (0, "F"),
)


# ---- small helpers -------------------------------------------------------------


def letter_grade(score: float) -> str:
    for cut, letter in LETTER_CUTS:
        if score >= cut:
            return letter
    return "F"


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _starting_positions(cfg: Config) -> list[str]:
    """Positions with a dedicated starting slot — FLEX is a slot, not a position."""
    return [p for p in cfg.starters if p != "FLEX"]


def board_before(st: DraftState, overall: int) -> list[Player]:
    """The available pool as it stood immediately before pick `overall`.

    Same rule as live: available = all_players - drafted_player_ids (§5.4), just
    rewound to an earlier point in the draft.
    """
    taken = {p.player_id for p in st.picks.values() if p.overall < overall}
    pool = [p for pid, p in st.players.items() if pid not in taken]
    pool.sort(key=lambda p: (p.rank, -p.projected_points))
    return pool


def waiver_baseline(st: DraftState) -> dict[str, float]:
    """Projected points of the best player still on the board at each position.

    This is the honest replacement level for a roster that already exists: what
    you could have at that position for free instead of trading for it.

    When a position is picked clean — plausible for QB or TE in a deep league —
    falling back to zero would price every bench player as pure surplus, so the
    baseline becomes the worst rostered player at that position instead: the
    cheapest thing anyone could actually be talked out of.
    """
    baseline: dict[str, float] = {}
    for player in st.available_players():
        if player.projected_points > baseline.get(player.position, 0.0):
            baseline[player.position] = player.projected_points

    floors: dict[str, float] = {}
    for pick in st.picks.values():
        player = st.player(pick.player_id)
        if player.position == "?" or player.position in baseline:
            continue
        floor = floors.get(player.position)
        if floor is None or player.projected_points < floor:
            floors[player.position] = player.projected_points
    baseline.update(floors)
    return baseline


def _filled_slots(st: DraftState, team_id: int, before: int | None = None) -> dict[str, int]:
    """Roster slots a team had filled before pick `before` (all picks when None)."""
    filled: dict[str, int] = {}
    for overall in sorted(st.picks):
        if before is not None and overall >= before:
            break
        pick = st.picks[overall]
        if pick.team_id != team_id:
            continue
        slot = assign_slot(st.player(pick.player_id).position, filled, st.cfg.roster_slots)
        filled[slot] = filled.get(slot, 0) + 1
    return filled


def unfilled_starters_before(st: DraftState, team_id: int, before: int | None = None) -> dict[str, int]:
    filled = _filled_slots(st, team_id, before)
    return {slot: max(0, count - filled.get(slot, 0)) for slot, count in st.cfg.starters.items()}


def team_players(st: DraftState, team_id: int) -> list[Player]:
    return [st.player(st.picks[o].player_id) for o in sorted(st.picks)
            if st.picks[o].team_id == team_id]


def team_ids(st: DraftState) -> list[int]:
    return sorted(st.teams) or sorted({p.team_id for p in st.picks.values()})


# ---- lineup --------------------------------------------------------------------


def optimal_lineup(players: list[Player], slots: dict[str, int]) -> tuple[dict[str, list[Player]], list[Player]]:
    """(best starting lineup by projection, bench).

    Dedicated slots are filled with the best player at each position, then FLEX
    takes the best eligible leftover. FLEX accepts a superset of what the
    dedicated slots accept, so filling in that order is optimal, not a heuristic.
    """
    order = sorted(range(len(players)), key=lambda i: -players[i].projected_points)
    used: set[int] = set()
    lineup: dict[str, list[Player]] = {}

    for slot, count in slots.items():
        if slot in ("BENCH", "IR", "FLEX") or count <= 0:
            continue
        chosen: list[int] = []
        for i in order:
            if len(chosen) >= count:
                break
            if i not in used and players[i].position == slot:
                chosen.append(i)
                used.add(i)
        lineup[slot] = [players[i] for i in chosen]

    flex_count = slots.get("FLEX", 0)
    if flex_count > 0:
        chosen = []
        for i in order:
            if len(chosen) >= flex_count:
                break
            if i not in used and players[i].position in FLEX_ELIGIBLE:
                chosen.append(i)
                used.add(i)
        lineup["FLEX"] = [players[i] for i in chosen]

    bench = [players[i] for i in order if i not in used]
    return lineup, bench


def lineup_points(lineup: dict[str, list[Player]]) -> float:
    return sum(p.projected_points for players in lineup.values() for p in players)


def points_by_position(lineup: dict[str, list[Player]]) -> dict[str, float]:
    """Starter points credited to the player's real position, not his slot — a WR
    in the FLEX still counts as WR production."""
    out: dict[str, float] = {}
    for players in lineup.values():
        for p in players:
            out[p.position] = out.get(p.position, 0.0) + p.projected_points
    return out


# ---- pick grading --------------------------------------------------------------


def grade_pick(st: DraftState, pick: Pick) -> dict[str, Any]:
    """Grade one pick against the board as it stood when it was made."""
    cfg = st.cfg
    player = st.player(pick.player_id)
    row: dict[str, Any] = {
        "overall": pick.overall,
        "round": pick.round,
        "player_id": pick.player_id,
        "name": player.name,
        "position": player.position,
        "pro_team": player.pro_team,
        "espn_rank": player.rank,
        "projected_points": round(player.projected_points, 1),
        "keeper": pick.keeper,
    }

    # Never lose a pick: an unmapped id renders ungraded rather than vanishing (§9).
    if pick.player_id not in st.players or player.position == "?" or player.projected_points <= 0:
        row.update(grade=None, score=None, value_delta=None, vor=None, passed_on=None,
                   reason="No projection data for this player — ungraded")
        return row

    board = board_before(st, pick.overall)
    unfilled = unfilled_starters_before(st, pick.team_id, before=pick.overall)

    # Kickers and defenses are graded against their own position. Every roster has
    # to carry one, and their ESPN ranks sit hundreds of picks below where anyone
    # actually takes them — measured against the whole board, every K and DST in
    # the league would grade F, which is a property of the format, not the pick.
    if player.position in SPECIALISTS:
        row.update(_grade_specialist(player, board, unfilled, pick.overall))
        return row

    replacement = _replacement_by_position(board, cfg)

    def vor_of(p: Player) -> float:
        return p.projected_points - replacement.get(p.position, 0.0)

    candidates = board[:CANDIDATE_DEPTH]
    if all(c.id != player.id for c in candidates):
        candidates = candidates + [player]

    my_vor = vor_of(player)
    better = sorted((c for c in candidates if c.id != player.id and vor_of(c) > my_vor),
                    key=lambda c: -vor_of(c))
    board_pct = 1.0 - len(better) / max(1, len(candidates) - 1)

    # Positive means he fell past his rank and you got value; negative is a reach.
    value_delta = pick.overall - player.rank
    value_norm = _clamp(value_delta / max(1.0, st.team_count * REACH_SLACK_ROUNDS), -1.0, 1.0)
    need = roster_need(player.position, unfilled, cfg)

    score = 100.0 * (
        GRADE_WEIGHTS["value"] * ((value_norm + 1) / 2)
        + GRADE_WEIGHTS["board"] * board_pct
        + GRADE_WEIGHTS["need"] * need
    ) / sum(GRADE_WEIGHTS.values())

    passed_on = _passed_on(st, pick, better[0], vor_of(better[0]) - my_vor) if better else None

    row.update(
        grade=letter_grade(score),
        score=round(score, 1),
        value_delta=value_delta,
        vor=round(my_vor, 1),
        better_available=len(better),
        filled_need=round(need, 2),
        passed_on=passed_on,
        reason=_pick_reason(player, value_delta, my_vor, need, better, passed_on, st.team_count),
    )
    return row


def _grade_specialist(player: Player, board: list[Player], unfilled: dict[str, int],
                      overall: int) -> dict[str, Any]:
    """Grade a K or DST on the only two questions that matter: is he the best one
    left, and did the pick cost you a starter somewhere that counts?"""
    at_position = sorted((p for p in board if p.position == player.position),
                         key=lambda p: -p.projected_points)
    better = [p for p in at_position if p.projected_points > player.projected_points]
    depth = max(1, min(len(at_position), 10))
    quality = _clamp(1.0 - len(better) / depth, 0.0, 1.0)

    still_needed = sorted(slot for slot, count in unfilled.items()
                          if count > 0 and slot not in SPECIALISTS and slot != player.position)
    premature = bool(still_needed)

    # Filling a mandatory slot on time is a C at worst; doing it while a real
    # starting slot is still empty is the actual mistake.
    score = (25 + 20 * quality) if premature else (45 + 50 * quality)

    if premature:
        reason = (f"{player.position} taken with your starting {'/'.join(still_needed)} "
                  f"still empty — that pick had to be a starter")
    elif not better:
        reason = f"Best {player.position} left on the board, taken at the right time"
    else:
        reason = (f"{len(better)} {player.position}"
                  f"{'' if len(better) == 1 else 's'} projected higher were still available")

    return {"grade": letter_grade(score), "score": round(score, 1),
            "value_delta": overall - player.rank, "vor": None,
            "better_available": len(better), "filled_need": 1.0 if not premature else 0.0,
            "passed_on": None, "reason": reason}


def _replacement_by_position(board: list[Player], cfg: Config) -> dict[str, float]:
    """Replacement level per position on a given board — the §7 definition, computed
    once per pick instead of once per candidate."""
    by_pos: dict[str, list[Player]] = {}
    for p in board:
        by_pos.setdefault(p.position, []).append(p)
    out: dict[str, float] = {}
    for pos, players in by_pos.items():
        players.sort(key=lambda p: -p.projected_points)
        idx = int(round(cfg.team_count * cfg.starters_at(pos)))
        out[pos] = players[min(max(idx, 0), len(players) - 1)].projected_points
    return out


def _passed_on(st: DraftState, pick: Pick, alternative: Player, vor_gap: float) -> dict[str, Any]:
    """The best player left on the board, and — the part that actually settles the
    argument — whether he was still there when this team picked again."""
    next_turn = _next_turn_overall(st, pick.team_id, pick.overall)
    still_there = None
    if next_turn is not None and st.current_overall > next_turn:
        still_there = _available_at(st, alternative.id, next_turn)
    return {
        "name": alternative.name,
        "position": alternative.position,
        "vor_gap": round(vor_gap, 1),
        "next_turn_overall": next_turn,
        "still_there_next_turn": still_there,
    }


def _next_turn_overall(st: DraftState, team_id: int, after: int) -> int | None:
    for overall in range(after + 1, len(st.snake) + 1):
        if st.snake[overall - 1] == team_id:
            return overall
    return None


def _available_at(st: DraftState, player_id: int, overall: int) -> bool:
    for pick in st.picks.values():
        if pick.player_id == player_id:
            return pick.overall >= overall
    return True


def _pick_reason(player: Player, value_delta: int, vor: float, need: float,
                 better: list[Player], passed_on: dict[str, Any] | None,
                 team_count: int) -> str:
    """One line naming whatever actually decided the grade — same discipline as the
    recommender's reason strings (§7): a bare letter tells you nothing."""
    rounds = value_delta / max(1, team_count)
    if passed_on and passed_on.get("still_there_next_turn") is False and passed_on["vor_gap"] >= 10:
        return (f"{passed_on['name']} ({passed_on['position']}) was worth "
                f"{passed_on['vor_gap']:.0f} more and went before your next pick")
    if not better:
        return "Best value on the board — nothing left projected higher over replacement"
    if value_delta <= -team_count:
        return (f"Reach: taken {abs(value_delta)} picks ({abs(rounds):.1f} rounds) ahead of "
                f"ESPN rank {player.rank}, with {len(better)} better values still there")
    if value_delta >= team_count:
        return f"Fell {value_delta} picks past rank {player.rank} — {vor:.0f} pts over replacement"
    if need >= 0.5:
        return f"Filled an open starting {player.position} slot at {vor:.0f} pts over replacement"
    if len(better) <= 3:
        return (f"Near the top of the board — only {len(better)} "
                f"{'player' if len(better) == 1 else 'players'} offered more value")
    return (f"{vor:.0f} pts over replacement {player.position}; "
            f"{len(better)} better values were on the board")


def grade_picks(st: DraftState, team_id: int) -> list[dict[str, Any]]:
    return [grade_pick(st, st.picks[o]) for o in sorted(st.picks)
            if st.picks[o].team_id == team_id]


def draft_grade(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Team-level grade: the average of the graded picks, weighted toward the early
    rounds where a mistake actually costs you a starter."""
    scored = [r for r in rows if r.get("score") is not None]
    if not scored:
        return {"letter": None, "score": None,
                "summary": "No graded picks yet — the player pool has no projections for them."}

    weights = [1.0 / math.sqrt(r["round"]) for r in scored]
    score = sum(r["score"] * w for r, w in zip(scored, weights)) / sum(weights)

    # The headline picks come from the skill positions. A last-round kicker is
    # graded on its own terms above and should never be the story of a draft.
    headline = [r for r in scored if r["position"] not in SPECIALISTS] or scored
    best = max(headline, key=lambda r: r["score"])
    worst = min(headline, key=lambda r: r["score"])
    steals = [r for r in headline if r["value_delta"] >= 12]
    reaches = [r for r in headline if r["value_delta"] <= -12]

    summary = (f"{len(scored)} graded picks. Best: {best['name']} ({best['grade']}). "
               f"Weakest: {worst['name']} ({worst['grade']}). "
               f"{len(steals)} fell to you, {len(reaches)} were reaches.")
    return {"letter": letter_grade(score), "score": round(score, 1), "summary": summary,
            "best_pick": best["name"], "worst_pick": worst["name"],
            "steals": len(steals), "reaches": len(reaches)}


# ---- roster leverage -----------------------------------------------------------


def _player_row(p: Player, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    row = {"name": p.name, "position": p.position, "pro_team": p.pro_team,
           "espn_rank": p.rank, "projected_points": round(p.projected_points, 1)}
    if extra:
        row.update(extra)
    return row


def league_context(st: DraftState) -> dict[int, dict[str, Any]]:
    """Every team's best lineup, its projected total, and its production by
    position. The comparison set for everything below — a roster is only strong
    or weak relative to the league it has to beat."""
    slots = st.cfg.roster_slots
    out: dict[int, dict[str, Any]] = {}
    for tid in team_ids(st):
        players = team_players(st, tid)
        lineup, bench = optimal_lineup(players, slots)
        out[tid] = {
            "lineup": lineup,
            "bench": bench,
            "points": lineup_points(lineup),
            "by_position": points_by_position(lineup),
        }
    return out


def positional_breakdown(st: DraftState, team_id: int,
                         context: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    """Starter production per position against the league average at that position."""
    rows = []
    for pos in _starting_positions(st.cfg):
        mine = context[team_id]["by_position"].get(pos, 0.0)
        others = [c["by_position"].get(pos, 0.0) for tid, c in context.items() if tid != team_id]
        avg = sum(others) / len(others) if others else 0.0
        rank = 1 + sum(1 for v in others if v > mine)
        rows.append({
            "position": pos,
            "my_points": round(mine, 1),
            "league_avg": round(avg, 1),
            "delta": round(mine - avg, 1),
            "rank": rank,
            "verdict": "unfilled" if mine <= 0
                       else "strength" if mine - avg > 0.08 * max(avg, 1)
                       else "weakness" if mine - avg < -0.08 * max(avg, 1)
                       else "average",
        })
    return rows


def trade_chips(st: DraftState, team_id: int, context: dict[int, dict[str, Any]],
                baseline: dict[str, float]) -> list[dict[str, Any]]:
    """Bench players worth more than the best free replacement at their position.

    That surplus is the only thing you can trade away without weakening your
    starting lineup, which is what makes it the thing to trade.
    """
    chips = []
    for p in context[team_id]["bench"]:
        surplus = p.projected_points - baseline.get(p.position, 0.0)
        if surplus <= 0 or p.position == "?":
            continue
        chips.append(_player_row(p, {"surplus": round(surplus, 1)}))
    chips.sort(key=lambda r: -r["surplus"])
    return chips


def lineup_changes(st: DraftState, team_id: int,
                   context: dict[int, dict[str, Any]]) -> list[str]:
    """Where the dashboard's draft-order slotting (§6) is not the best lineup.

    The board slots players as they are picked; that is right for watching a
    draft and wrong for setting a week-one lineup.
    """
    drafted_slotting = st.roster(team_id)
    started_by_draft = {
        row["name"] for slot, rows in drafted_slotting.items()
        if slot not in ("BENCH", "IR") for row in rows
    }
    best_names = {p.name for players in context[team_id]["lineup"].values() for p in players}
    promote = sorted(best_names - started_by_draft)
    demote = sorted(started_by_draft - best_names)
    return [f"Start {up} over {down}" for up, down in zip(promote, demote)]


def leverage_notes(st: DraftState, team_id: int, context: dict[int, dict[str, Any]],
                   breakdown: list[dict[str, Any]], chips: list[dict[str, Any]]) -> list[str]:
    """The 'so what' — plain sentences you can act on without reading a table."""
    notes: list[str] = []
    mine = context[team_id]
    others = [c["points"] for tid, c in context.items() if tid != team_id]
    avg = sum(others) / len(others) if others else 0.0
    rank = 1 + sum(1 for v in others if v > mine["points"])
    notes.append(f"Your best lineup projects {mine['points']:.0f} pts — "
                 f"{_ordinal(rank)} of {len(context)} (league average {avg:.0f}).")

    unfilled = [r["position"] for r in breakdown if r["verdict"] == "unfilled"]
    if unfilled:
        slots = ", ".join(unfilled)
        notes.append(f"Nothing at {slots} yet — "
                     + ("that is an empty starting slot every week."
                        if st.draft_status == "COMPLETE"
                        else "still to come before the draft ends."))

    strengths = [r for r in breakdown if r["verdict"] == "strength"]
    weaknesses = [r for r in breakdown if r["verdict"] == "weakness"]
    if strengths:
        top = max(strengths, key=lambda r: r["delta"])
        notes.append(f"{top['position']} is your edge: {top['my_points']:.0f} pts from starters, "
                     f"{top['delta']:+.0f} vs the league — win the weeks your {top['position']}s do.")
    if weaknesses:
        worst = min(weaknesses, key=lambda r: r["delta"])
        notes.append(f"{worst['position']} is the hole: {worst['my_points']:.0f} pts, "
                     f"{worst['delta']:+.0f} vs the league and {_ordinal(worst['rank'])} in it.")
    if chips:
        chip = chips[0]
        notes.append(f"{chip['name']} ({chip['position']}) is your trade chip — "
                     f"{chip['surplus']:.0f} pts better than the best {chip['position']} "
                     f"still on the board, and he is not in your starting lineup.")
    if not chips:
        notes.append("No real surplus on your bench — you would be trading from your "
                     "starting lineup, so target a two-for-one instead.")
    for change in lineup_changes(st, team_id, context)[:2]:
        notes.append(f"{change} — the draft board slots by pick order, not by projection.")
    return notes


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


# ---- trade fits ----------------------------------------------------------------


def _needs(st: DraftState, tid: int, context: dict[int, dict[str, Any]]) -> dict[str, float]:
    """Points below the league average at each starting position — the size of the
    hole, in the same unit as everything else."""
    needs: dict[str, float] = {}
    for pos in _starting_positions(st.cfg):
        mine = context[tid]["by_position"].get(pos, 0.0)
        others = [c["by_position"].get(pos, 0.0) for other, c in context.items() if other != tid]
        avg = sum(others) / len(others) if others else 0.0
        gap = avg - mine
        if gap > 0:
            needs[pos] = gap
    return needs


def _surplus(st: DraftState, tid: int, context: dict[int, dict[str, Any]],
             baseline: dict[str, float]) -> dict[str, list[dict[str, Any]]]:
    """Tradeable depth by position: bench players above free-agent replacement."""
    out: dict[str, list[dict[str, Any]]] = {}
    for chip in trade_chips(st, tid, context, baseline):
        out.setdefault(chip["position"], []).append(chip)
    return out


def trade_targets(st: DraftState, team_id: int, context: dict[int, dict[str, Any]],
                  baseline: dict[str, float], limit: int = TRADE_TARGET_LIMIT) -> list[dict[str, Any]]:
    """Teams whose surplus covers your hole *and* whose hole your surplus covers.

    Scored as a geometric mean on purpose: a deal only happens when both sides
    gain, so a one-sided fit scores near zero rather than merely low.
    """
    my_needs = _needs(st, team_id, context)
    my_surplus = _surplus(st, team_id, context, baseline)

    fits = []
    for tid in context:
        if tid == team_id:
            continue
        their_needs = _needs(st, tid, context)
        their_surplus = _surplus(st, tid, context, baseline)

        best = None
        for get_pos, gap in my_needs.items():
            if get_pos not in their_surplus:
                continue
            get_player = their_surplus[get_pos][0]
            for give_pos, their_gap in their_needs.items():
                if give_pos == get_pos or give_pos not in my_surplus:
                    continue
                give_player = my_surplus[give_pos][0]
                gain_me = min(gap, get_player["surplus"])
                gain_them = min(their_gap, give_player["surplus"])
                fit = math.sqrt(max(gain_me, 0.0) * max(gain_them, 0.0))
                if best is None or fit > best["fit_score"]:
                    best = {
                        "team_id": tid,
                        "team_name": st.team_name(tid),
                        "fit_score": fit,
                        "get": get_player,
                        "give": give_player,
                        "my_gap": round(gap, 1),
                        "their_gap": round(their_gap, 1),
                    }
        if best and best["fit_score"] >= MIN_TRADE_FIT:
            best["fit_score"] = round(best["fit_score"], 1)
            best["balance"] = _balance_note(best["give"], best["get"])
            best["rationale"] = _trade_rationale(best)
            fits.append(best)

    fits.sort(key=lambda f: -f["fit_score"])
    return fits[:limit]


def _balance_note(give: dict[str, Any], get: dict[str, Any]) -> str:
    """Whether the two sides are close enough that the offer is worth sending."""
    diff = give["projected_points"] - get["projected_points"]
    if abs(diff) <= 10:
        return "Roughly even on projection — send it as a straight swap."
    if diff > 0:
        return (f"You are giving up {diff:.0f} more projected points — ask for a "
                f"late-round bench piece back to balance it.")
    return (f"You would be getting {abs(diff):.0f} more projected points — expect to "
            f"add a bench player to get it done.")


def _trade_rationale(fit: dict[str, Any]) -> str:
    get, give = fit["get"], fit["give"]
    return (f"You are {fit['my_gap']:.0f} pts light at {get['position']} and they have "
            f"{get['name']} sitting on their bench; they are {fit['their_gap']:.0f} pts light "
            f"at {give['position']} where {give['name']} is surplus for you.")


# ---- entry point ---------------------------------------------------------------


def analyze(st: DraftState, team_id: int | None = None) -> dict[str, Any]:
    """Everything the /api/analysis page needs, for one team."""
    tid = st.cfg.my_team_id if team_id is None else team_id
    ids = team_ids(st)
    if ids and tid not in ids:
        return _empty(st, tid, f"Team {tid} is not in this league")

    context = league_context(st)
    if tid not in context:
        return _empty(st, tid, "No picks recorded yet — nothing to grade")

    baseline = waiver_baseline(st)
    picks = grade_picks(st, tid)
    breakdown = positional_breakdown(st, tid, context)
    chips = trade_chips(st, tid, context, baseline)
    lineup = context[tid]["lineup"]

    return {
        "team_id": tid,
        "team_name": st.team_name(tid),
        "is_me": tid == st.cfg.my_team_id,
        "draft_status": st.draft_status,
        "picks_made": len(picks),
        "draft_grade": draft_grade(picks),
        "picks": picks,
        "lineup": {slot: [_player_row(p) for p in players] for slot, players in lineup.items()},
        "bench": [_player_row(p) for p in context[tid]["bench"]],
        "projected_points": round(context[tid]["points"], 1),
        "league_avg_points": round(
            sum(c["points"] for t, c in context.items() if t != tid) / max(1, len(context) - 1), 1),
        "league_rank": 1 + sum(1 for t, c in context.items()
                               if t != tid and c["points"] > context[tid]["points"]),
        "league_size": len(context),
        "positional": breakdown,
        "trade_chips": chips[:5],
        "trade_targets": trade_targets(st, tid, context, baseline),
        "leverage": leverage_notes(st, tid, context, breakdown, chips),
        "teams": [{"id": t, "name": st.team_name(t)} for t in ids],
        "error": None,
    }


def _empty(st: DraftState, tid: int, message: str) -> dict[str, Any]:
    """Degrade to an explained empty report rather than an exception (§1)."""
    return {
        "team_id": tid, "team_name": st.team_name(tid), "is_me": tid == st.cfg.my_team_id,
        "draft_status": st.draft_status, "picks_made": 0,
        "draft_grade": {"letter": None, "score": None, "summary": message},
        "picks": [], "lineup": {}, "bench": [], "projected_points": 0.0,
        "league_avg_points": 0.0, "league_rank": 0, "league_size": len(team_ids(st)),
        "positional": [], "trade_chips": [], "trade_targets": [], "leverage": [],
        "teams": [{"id": t, "name": st.team_name(t)} for t in team_ids(st)],
        "error": message,
    }
