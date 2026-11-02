"""Trade evaluation, season parsing and the OpenAI advisor.

In-memory fixtures only: samples/ is gitignored, so nothing here may depend on a dump.
"""
import json

import httpx
import pytest

import advisor
import season
import trade
from season import SeasonPlayer, SeasonState, SeasonTeam

STARTERS = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "DST": 1, "K": 1}


def _p(pid, pos, pts, team, slot="BENCH", games=6, injury="ACTIVE", recent=None):
    """A player whose every weekly score is `pts` (recent games overridden by `recent`)."""
    weekly = {w: pts for w in range(1, games + 1)}
    if recent is not None:
        for w in range(max(1, games - 2), games + 1):
            weekly[w] = recent
    return SeasonPlayer(id=pid, name=f"P{pid}", position=pos, injury=injury, slot=slot,
                        team_id=team, weekly=weekly, proj_week=pts)


def _league(a_players, b_players, period=8, final=17):
    st = SeasonState(period=period, final_period=final)
    for tid, ps in ((1, a_players), (2, b_players)):
        st.teams[tid] = SeasonTeam(id=tid, name=f"Team {tid}", player_ids=[p.id for p in ps])
        for p in ps:
            st.players[p.id] = p
    return st


def _base():
    """Team 1 has a spare RB on its bench and is thin at WR; team 2 is the mirror image.

    Ids: team 1 holds 1-9 and 20-21, team 2 holds 11-18 and 22-23. Each side has two
    kickers so a K-for-K swap is the clean one-sided case.
    """
    a = [_p(1, "QB", 18, 1), _p(2, "RB", 20, 1), _p(3, "RB", 17, 1), _p(4, "RB", 16, 1),
         _p(8, "RB", 15, 1), _p(5, "WR", 8, 1), _p(6, "WR", 7, 1), _p(7, "TE", 10, 1),
         _p(20, "K", 5, 1), _p(21, "K", 2, 1)]
    b = [_p(11, "QB", 15, 2), _p(12, "RB", 8, 2), _p(13, "RB", 7, 2), _p(14, "WR", 19, 2),
         _p(15, "WR", 17, 2), _p(16, "WR", 15, 2), _p(18, "WR", 13, 2), _p(17, "TE", 9, 2),
         _p(22, "K", 14, 2), _p(23, "K", 3, 2)]
    return _league(a, b)


def test_bench_rb_for_bench_wr_helps_both_thin_lineups():
    st = _base()
    r = trade.evaluate_trade(st, STARTERS, 1, 2, give_ids=[8], get_ids=[18])
    assert r["you"]["weekly_delta"] == pytest.approx(6.0, abs=0.1)
    assert r["them"]["weekly_delta"] == pytest.approx(8.0, abs=0.1)
    assert r["acceptance"]["label"] == "mutual"
    assert r["acceptance"]["fit_score"] > 0


def test_rest_of_season_delta_scales_with_weeks_left():
    st = _base()
    r = trade.evaluate_trade(st, STARTERS, 1, 2, [8], [18])
    assert st.weeks_left == 10
    assert r["you"]["rest_of_season_delta"] == pytest.approx(r["you"]["weekly_delta"] * 10, abs=0.2)


def test_trading_a_bench_player_for_a_bench_player_changes_nothing():
    st = _league([_p(1, "QB", 18, 1), _p(2, "QB", 5, 1)], [_p(3, "QB", 16, 2), _p(4, "QB", 6, 2)])
    r = trade.evaluate_trade(st, {"QB": 1}, 1, 2, [2], [4])
    assert r["you"]["weekly_delta"] == 0 and r["them"]["weekly_delta"] == 0
    assert r["acceptance"]["label"] == "roughly neutral for both lineups"


def test_one_sided_trade_scores_zero_fit_and_says_who_wins():
    st = _base()
    # Team 1's spare kicker for team 2's starting kicker: +9 for one side, -11 for the other.
    r = trade.evaluate_trade(st, STARTERS, 1, 2, [21], [22])
    assert r["you"]["weekly_delta"] == pytest.approx(9.0, abs=0.1)
    assert r["them"]["weekly_delta"] == pytest.approx(-11.0, abs=0.1)
    assert r["acceptance"]["fit_score"] == 0
    assert "declined" in r["acceptance"]["label"]


def test_an_out_player_is_worth_zero_and_is_flagged():
    st = _base()
    st.players[14].injury = "OUT"
    assert trade.weekly_value(st.players[14]) == 0
    r = trade.evaluate_trade(st, STARTERS, 1, 2, [8], [14])
    assert any("cannot start" in f for f in r["you"]["flags"])


def test_declining_player_is_flagged_as_trending_down():
    st = _base()
    st.players[16] = _p(16, "WR", 15, 2, recent=6)
    r = trade.evaluate_trade(st, STARTERS, 1, 2, [8], [16])
    assert any("trending down" in f for f in r["you"]["flags"])
    assert r["you"]["gets"][0]["trend"] == "down"


def test_roster_over_the_cap_is_flagged_not_blocked():
    st = _base()
    r = trade.evaluate_trade(st, STARTERS, 1, 2, [8], [16, 15], roster_cap=7)
    assert any("over the 7-player limit" in f for f in r["you"]["flags"])


@pytest.mark.parametrize("give,get,msg", [
    ([], [16], "at least one"),
    ([4], [], "at least one"),
    ([4, 4], [16], "twice"),
    ([99], [16], "not on team 1"),
    ([4], [2], "not on team 2"),
])
def test_invalid_trades_raise_trade_error(give, get, msg):
    with pytest.raises(trade.TradeError, match=msg):
        trade.evaluate_trade(_base(), STARTERS, 1, 2, give, get)


def test_trading_with_yourself_or_an_unknown_team_is_rejected():
    st = _base()
    with pytest.raises(trade.TradeError):
        trade.evaluate_trade(st, STARTERS, 1, 1, [4], [4])
    with pytest.raises(trade.TradeError, match="unknown team"):
        trade.evaluate_trade(st, STARTERS, 1, 9, [4], [16])


# ---- season parsing -------------------------------------------------------------


def _stat(week, source, total, split=1):
    return {"scoringPeriodId": week, "statSourceId": source, "statSplitTypeId": split,
            "appliedTotal": total}


def _roster_payload():
    stats = [_stat(1, 0, 10.0), _stat(2, 0, 20.0), _stat(4, 0, 30.0),   # week 3 is a bye
             _stat(8, 0, 9.0),                                            # current week: not played yet
             _stat(17, 0, 0.0),                                           # future weeks read 0.0
             _stat(8, 1, 14.5), _stat(0, 0, 999.0, split=0)]            # season total is not a week
    return {"scoringPeriodId": 8, "status": {"finalScoringPeriod": 17}, "teams": [{
        "id": 6, "name": "Mine", "record": {"overall": {"wins": 4, "losses": 3, "pointsFor": 100.5}},
        "roster": {"entries": [{
            "playerId": 42, "lineupSlotId": 2, "injuryStatus": "QUESTIONABLE",
            "playerPoolEntry": {"player": {"id": 42, "fullName": "Test Back",
                                           "defaultPositionId": 2, "proTeamId": 11,
                                           "stats": stats}}}]}}]}


def test_weekly_stats_skip_byes_future_weeks_and_the_current_week():
    st = season.parse_rosters(_roster_payload())
    p = st.players[42]
    assert p.weekly == {1: 10.0, 2: 20.0, 4: 30.0}
    assert p.games == 3 and p.avg == pytest.approx(20.0)
    assert p.proj_week == 14.5
    assert (p.position, p.slot, p.pro_team, p.injury) == ("RB", "RB", "IND", "QUESTIONABLE")
    assert st.weeks_left == 10 and st.teams[6].wins == 4


def test_unknown_player_is_kept_rather_than_dropped():
    data = _roster_payload()
    data["teams"][0]["roster"]["entries"][0]["playerPoolEntry"]["player"] = {"id": 42}
    st = season.parse_rosters(data)
    assert st.players[42].name == "Unknown player 42" and st.players[42].position == "?"


def test_player_weeks_overlay_replaces_the_thin_roster_history():
    st = season.parse_rosters(_roster_payload())
    season.apply_player_weeks(st, {"players": [{"id": 42, "player": {
        "id": 42, "stats": [_stat(w, 0, float(w)) for w in range(1, 8)]}}]})
    assert sorted(st.players[42].weekly) == [1, 2, 3, 4, 5, 6, 7]


def test_matchups_record_completed_weeks_only():
    st = season.parse_rosters(_roster_payload())
    season.apply_matchups(st, {"schedule": [
        {"matchupPeriodId": 7, "home": {"teamId": 6, "totalPoints": 117.0},
         "away": {"teamId": 3, "totalPoints": 100.0}},
        {"matchupPeriodId": 8, "home": {"teamId": 6, "totalPoints": 5.0}, "away": {}}]})
    assert st.weekly_scores[6] == {7: 117.0} and st.weekly_scores[3] == {7: 100.0}


# ---- advisor --------------------------------------------------------------------


def _evaluation():
    return trade.evaluate_trade(_base(), STARTERS, 1, 2, [8], [18])


def _reply(content, status=200):
    body = {"choices": [{"message": {"content": content}}]}
    return httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(status, json=body)))


async def test_advice_is_parsed_and_clamped():
    good = json.dumps({"verdict": "ACCEPT", "confidence": "high", "summary": "Take it.",
                       "reasons": list("abcdef"), "risks": ["r"], "counter_offer": None})
    out = await advisor.advise(_evaluation(), "key", "m", client=_reply(good))
    assert out["available"] and out["verdict"] == "accept"
    assert len(out["reasons"]) == 4 and out["counter_offer"] is None


async def test_unknown_verdict_falls_back_to_counter_with_low_confidence():
    out = await advisor.advise(_evaluation(), "key", "m",
                               client=_reply(json.dumps({"verdict": "maybe", "confidence": "sure"})))
    assert out["verdict"] == "counter" and out["confidence"] == "low"


async def test_missing_key_never_calls_openai():
    def boom(req):
        raise AssertionError("should not be called")
    client = httpx.AsyncClient(transport=httpx.MockTransport(boom))
    out = await advisor.advise(_evaluation(), "", "m", client=client)
    assert out == {"available": False, "error": "OPEN_AI_KEY is not set in .env"}


@pytest.mark.parametrize("status,needle", [(401, "401"), (429, "429"), (500, "500")])
async def test_http_errors_degrade(status, needle):
    out = await advisor.advise(_evaluation(), "key", "m", client=_reply("{}", status))
    assert out["available"] is False and needle in out["error"]


async def test_non_json_reply_degrades():
    out = await advisor.advise(_evaluation(), "key", "m", client=_reply("not json"))
    assert out["available"] is False


async def test_timeout_degrades():
    def slow(req):
        raise httpx.ReadTimeout("slow")
    out = await advisor.advise(_evaluation(), "key", "m",
                               client=httpx.AsyncClient(transport=httpx.MockTransport(slow)))
    assert out == {"available": False, "error": "OpenAI timed out"}


async def test_request_carries_key_and_only_computed_numbers():
    seen = {}

    def capture(req):
        seen["auth"] = req.headers["authorization"]
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    await advisor.advise(_evaluation(), "sk-test", "my-model",
                         client=httpx.AsyncClient(transport=httpx.MockTransport(capture)))
    assert seen["auth"] == "Bearer sk-test" and seen["body"]["model"] == "my-model"
    user = json.loads(seen["body"]["messages"][1]["content"])
    assert "lineup_before" not in user["you"] and "weekly_delta" in user["you"]


# ---- simulation -----------------------------------------------------------------

STEP1 = {"team_a": 1, "team_b": 2, "give": [8], "get": [18]}   # spare RB for a spare WR


def test_apply_trade_moves_players_and_leaves_the_real_league_untouched():
    st = _base()
    sim = trade.apply_trade(st, 1, 2, [8], [18])
    assert 18 in sim.teams[1].player_ids and 8 in sim.teams[2].player_ids
    assert 8 not in sim.teams[1].player_ids and 18 not in sim.teams[2].player_ids
    assert sim.players[18].team_id == 1
    assert 8 in st.teams[1].player_ids and st.players[18].team_id == 2    # original intact


def test_chained_trade_can_reuse_a_player_acquired_in_an_earlier_step():
    st = _base()
    # Step 2 sends back the WR we just received (id 18) - impossible without the ledger.
    with pytest.raises(trade.TradeError, match="not on team 1"):
        trade.evaluate_trade(st, STARTERS, 1, 2, [18], [12])
    r = trade.evaluate_simulated(st, STARTERS, 1, 2, [18], [12], [STEP1])
    assert r["you"]["gives"][0]["id"] == 18
    assert len(r["prior_trades"]) == 1 and "P8" in r["prior_trades"][0]


def test_vs_real_reports_the_running_total_against_the_untouched_league():
    st = _base()
    first = trade.evaluate_trade(st, STARTERS, 1, 2, [8], [18])
    # A neutral second step (swap the two spare kickers' owners back and forth is not needed:
    # use a trade that changes nothing) so vs_real equals the first step's gain.
    r = trade.evaluate_simulated(st, STARTERS, 1, 2, [21], [23], [STEP1])
    assert r["you"]["real_points"] == pytest.approx(first["you"]["before_points"], abs=0.1)
    assert r["you"]["vs_real"] == pytest.approx(r["you"]["after_points"] - r["you"]["real_points"], abs=0.1)
    assert r["you"]["vs_real"] >= first["you"]["weekly_delta"] - 0.1


def test_empty_ledger_matches_standard_analysis():
    st = _base()
    a = trade.evaluate_trade(st, STARTERS, 1, 2, [8], [18])
    b = trade.evaluate_simulated(st, STARTERS, 1, 2, [8], [18], [])
    assert a["you"]["weekly_delta"] == b["you"]["weekly_delta"]
    assert b["you"]["vs_real"] == b["you"]["weekly_delta"] and b["prior_trades"] == []


def test_undo_is_replaying_a_shorter_ledger():
    st = _base()
    two = [STEP1, {"team_a": 1, "team_b": 2, "give": [18], "get": [12]}]
    assert 12 in trade.replay(st, two).teams[1].player_ids
    assert 12 not in trade.replay(st, two[:1]).teams[1].player_ids


def test_stale_ledger_names_the_step_that_no_longer_fits():
    st = _base()
    stale = [STEP1, {"team_a": 1, "team_b": 2, "give": [999], "get": [12]}]
    with pytest.raises(trade.TradeError, match="out of date at trade 2"):
        trade.replay(st, stale)
    with pytest.raises(trade.TradeError, match="out of date at trade 1"):
        trade.replay(st, [{"team_a": 1}])        # malformed step, same clean error


# ---- bye weeks ------------------------------------------------------------------

import asyncio  # noqa: E402

RB_ONLY = {"RB": 1}


def _bye_p(pid, pts, team, bye=0, slot="BENCH"):
    p = _p(pid, "RB", pts, team, slot=slot)
    p.bye_week = bye
    return p


def _bye_league(period=8):
    """Team 1: RB1 (20, bye 9) with a bench RB2 (10, bye 12). Team 2: RB3 (15, bye 9), RB4 (8, bye 12)."""
    return _league([_bye_p(1, 20, 1, 9, "RB"), _bye_p(2, 10, 1, 12)],
                   [_bye_p(3, 15, 2, 9, "RB"), _bye_p(4, 8, 2, 12)], period=period)


def test_a_player_scores_zero_in_his_bye_week_and_only_then():
    p = _bye_p(1, 20, 1, bye=10)
    assert trade.weekly_value(p, 10) == 0
    assert trade.weekly_value(p, 9) > 0 and trade.weekly_value(p) > 0       # other weeks and "no week" are unaffected
    assert trade.weekly_value(_bye_p(2, 20, 1, bye=0), 10) > 0              # unknown bye (0) never blanks a week


def test_a_starters_bye_costs_that_week_and_a_bench_player_covers_it():
    st = _bye_league()
    by_week, base = trade.lineup_points_by_week(st.roster(1), RB_ONLY, st.weeks_ahead)
    assert by_week[8] == by_week[10] == base["points"] == pytest.approx(20, abs=0.1)
    assert by_week[9] == pytest.approx(10, abs=0.1)                          # RB1 on bye: bench RB2 starts
    assert by_week[12] == pytest.approx(20, abs=0.1)                         # the bench player's bye costs nothing


def test_an_incoming_player_who_covers_a_bye_is_credited_in_that_week_only():
    st = _bye_league()
    st.players[3].bye_week = 11                       # RB3's bye is a different week from the starter's (9)
    r = trade.evaluate_trade(st, RB_ONLY, 1, 2, [2], [3])      # bench RB2 (10, bye 12) for RB3 (15, bye 11)
    wk = {w["week"]: w for w in r["you"]["by_week"]}
    assert wk[9]["before"] == pytest.approx(10, abs=0.1) and wk[9]["after"] == pytest.approx(15, abs=0.1)
    assert wk[10]["before"] == wk[10]["after"] == pytest.approx(20, abs=0.1)         # RB1 plays; nothing changes
    assert r["you"]["rest_of_season_delta"] == pytest.approx(5, abs=0.2)             # +5 in week 9, 0 elsewhere
    assert r["you"]["weekly_delta"] == pytest.approx(5 / len(st.weeks_ahead), abs=0.1)
    assert not any("bye:" in f for f in r["you"]["flags"])                           # covering a bye is not a problem


def test_stacking_the_same_bye_leaves_that_week_uncovered_and_says_so():
    st = _bye_league()                                  # RB1 (starter) and RB3 both have their bye in week 9
    r = trade.evaluate_trade(st, RB_ONLY, 1, 2, [2], [3])      # trade away the bench RB (bye 12) that was covering week 9
    wk9 = next(w for w in r["you"]["by_week"] if w["week"] == 9)
    assert wk9["before"] == pytest.approx(10, abs=0.1) and wk9["after"] == pytest.approx(0, abs=0.1)
    assert r["you"]["rest_of_season_delta"] == pytest.approx(-10, abs=0.2)
    flag = next(f for f in r["you"]["flags"] if "Week 9 bye" in f)
    assert "10.0 more points" in flag


def test_an_incoming_player_on_bye_this_week_cannot_help_yet():
    st = _bye_league(period=9)
    r = trade.evaluate_trade(st, RB_ONLY, 1, 2, [2], [3])               # RB3's bye is week 9 = this week
    assert any("on his bye this week" in f and "week 10" in f for f in r["you"]["flags"])
    assert not any("on his bye this week" in f for f in trade.evaluate_trade(_bye_league(period=8), RB_ONLY, 1, 2, [2], [3])["you"]["flags"])


def test_quick_deltas_agree_with_the_full_evaluation_and_the_cache_is_transparent():
    st = _bye_league()
    full = trade.evaluate_trade(st, RB_ONLY, 1, 2, [2], [3])
    cache = {}
    me, them = trade.quick_deltas(st, RB_ONLY, 1, 2, [2], [3], cache)
    assert me == pytest.approx(full["you"]["weekly_delta"], abs=0.06)
    assert them == pytest.approx(full["them"]["weekly_delta"], abs=0.06)
    assert cache and trade.quick_deltas(st, RB_ONLY, 1, 2, [2], [3], cache) == (me, them)
    cached = trade.evaluate_trade(st, RB_ONLY, 1, 2, [2], [3], cache=cache)
    assert cached["you"]["by_week"] == full["you"]["by_week"]


def test_player_rows_expose_the_bye_week():
    assert trade.player_row(_bye_p(1, 20, 1, 9))["bye_week"] == 9
    assert trade.player_row(_bye_p(2, 20, 1, 0))["bye_week"] is None


# ---- loading bye weeks ------------------------------------------------------------------


def test_parse_byes_maps_nfl_team_ids_to_weeks_and_ignores_junk():
    data = {"settings": {"proTeams": [{"id": 11, "byeWeek": 13}, {"id": 12, "byeWeek": 5},
                                      {"id": 0, "byeWeek": 0}, {"id": 999, "byeWeek": 4},
                                      {"id": 1, "byeWeek": "x"}, {"id": 2}]}}
    assert season.parse_byes(data) == {"IND": 13, "KC": 5}
    assert season.parse_byes({}) == {}


def test_apply_byes_sets_every_players_bye_by_nfl_team():
    st = season.parse_rosters(_roster_payload())                      # Test Back is on IND (proTeamId 11)
    season.apply_byes(st, {"IND": 13})
    assert st.players[42].bye_week == 13 and st.byes == {"IND": 13}
    season.apply_byes(st, {})
    assert st.players[42].bye_week == 0


def test_weeks_ahead_runs_from_this_week_through_the_final_period():
    st = season.parse_rosters(_roster_payload())
    assert st.weeks_ahead == list(range(8, 18)) and len(st.weeks_ahead) == st.weeks_left


class _FakeClient:
    def __init__(self, bye_error=False):
        self.bye_calls, self.bye_error = 0, bye_error

    async def rosters(self):
        return _roster_payload()

    async def player_weeks(self, ids, period):
        return {"players": []}

    async def matchups(self):
        return {"schedule": []}

    async def pro_schedules(self):
        self.bye_calls += 1
        if self.bye_error:
            raise RuntimeError("espn down")
        return {"settings": {"proTeams": [{"id": 11, "byeWeek": 13}]}}


def test_loader_fetches_byes_once_and_keeps_working_if_they_fail():
    client = _FakeClient()
    loader = season.SeasonLoader(client, ttl=0)
    first = asyncio.run(loader.get(force=True))
    second = asyncio.run(loader.get(force=True))
    assert first.players[42].bye_week == 13 and second.players[42].bye_week == 13
    assert client.bye_calls == 1                                      # fixed for the season, so not refetched each refresh

    broken = season.SeasonLoader(_FakeClient(bye_error=True), ttl=0)
    st = asyncio.run(broken.get(force=True))
    assert st is not None and st.players[42].bye_week == 0            # everyone is assumed to play: degrade, don't crash
