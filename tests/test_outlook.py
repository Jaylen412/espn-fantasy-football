"""Season outlook: projection maths, schedule parsing, AI summary and routes.

In-memory fixtures only (samples/ is gitignored).
"""
import json

import httpx
import pytest

import advisor
import outlook
import season
from season import SeasonPlayer, SeasonState, SeasonTeam

STARTERS = {"QB": 1, "RB": 1}


def _p(pid, pos, pts, team):
    return SeasonPlayer(id=pid, name=f"P{pid}", position=pos, team_id=team,
                        weekly={w: pts for w in range(1, 5)}, proj_week=pts)


def _league(sigma_games=True):
    """Four teams, two games a week for weeks 5-8. Team 1 holds a spare RB (id 12) it cannot
    start; team 2's starting RB is weak, so moving 12 to team 2 helps team 2 and costs
    team 1 nothing."""
    st = SeasonState(period=5, final_period=17, playoff_teams=2, regular_weeks=8)
    rosters = {
        1: [_p(1, "QB", 20, 1), _p(2, "RB", 22, 1), _p(12, "RB", 15, 1)],
        2: [_p(3, "QB", 18, 2), _p(4, "RB", 6, 2)],
        3: [_p(5, "QB", 19, 3), _p(6, "RB", 18, 3)],
        4: [_p(7, "QB", 12, 4), _p(8, "RB", 10, 4)],
    }
    for tid, ps in rosters.items():
        st.teams[tid] = SeasonTeam(id=tid, name=f"T{tid}", wins=2, losses=2, points_for=300.0 + tid,
                                   player_ids=[p.id for p in ps])
        for p in ps:
            st.players[p.id] = p
        st.weekly_scores[tid] = {1: 90 + tid, 2: 100 - tid, 3: 85 + 2 * tid, 4: 105 - tid} if sigma_games else {}
    for wk in range(5, 9):
        st.schedule += [(wk, 1, 2) if wk % 2 else (wk, 1, 3), (wk, 3, 4) if wk % 2 else (wk, 2, 4)]
    return st


STEP = {"team_a": 1, "team_b": 2, "give": [12], "get": [4]}         # team 1's spare RB for team 2's starter
GIVE_STRONG = {"team_a": 1, "team_b": 2, "give": [2], "get": [4]}   # team 1 hands its RB1 to team 2


def test_no_trades_in_the_ledger_means_zero_difference_everywhere():
    o = outlook.season_outlook(_league(), STARTERS, 1, [], sims=600)
    assert all(v == 0 for v in o["delta"].values())
    assert o["baseline"] == o["simulated"]


def test_a_trade_that_weakens_you_lowers_wins_and_playoff_odds():
    o = outlook.season_outlook(_league(), STARTERS, 1, [GIVE_STRONG], sims=1500)
    assert o["delta"]["expected_wins"] < 0
    assert o["delta"]["playoff_prob"] < 0
    assert o["delta"]["points_per_week"] < 0 and o["delta"]["expected_rank"] > 0


def test_a_trade_that_strengthens_you_raises_them():
    # Team 2 swaps its 6-point starting RB for team 1's spare 15-point RB: +9 points a week.
    o = outlook.season_outlook(_league(), STARTERS, 2,
                               [{"team_a": 2, "team_b": 1, "give": [4], "get": [12]}], sims=1500)
    assert o["delta"]["expected_wins"] > 0 and o["delta"]["points_per_week"] > 0


def test_the_trade_partners_new_strength_counts_against_you_when_they_face_you():
    # Team 2 is better after the trade, and team 1 plays team 2 in weeks 5 and 7.
    base = outlook.season_outlook(_league(), STARTERS, 1, [], sims=200)
    opp_before = next(w for w in base["baseline"]["weekly"] if w["opponent_id"] == 2)["opp_points"]
    o = outlook.season_outlook(_league(), STARTERS, 1, [GIVE_STRONG], sims=200)
    opp_after = next(w for w in o["simulated"]["weekly"] if w["opponent_id"] == 2)["opp_points"]
    assert opp_after > opp_before


def test_expected_wins_are_current_wins_plus_the_weekly_probabilities():
    o = outlook.season_outlook(_league(), STARTERS, 1, [GIVE_STRONG], sims=300)
    b = o["baseline"]
    assert b["expected_wins"] == pytest.approx(2 + sum(w["win_prob"] for w in b["weekly"]), abs=0.02)
    assert b["weekly"][-1]["cum_wins"] == pytest.approx(b["expected_wins"], abs=0.02)
    assert b["expected_wins"] + b["expected_losses"] == pytest.approx(4 + len(b["weekly"]))


def test_evenly_matched_teams_split_the_game_fifty_fifty():
    assert outlook._win_prob(100, 20, 100, 20) == pytest.approx(0.5)
    assert outlook._win_prob(120, 20, 100, 20) > 0.75 > outlook._win_prob(105, 20, 100, 20) > 0.5


def test_result_is_repeatable_for_a_fixed_seed():
    a = outlook.season_outlook(_league(), STARTERS, 1, [GIVE_STRONG], sims=400, seed=3)
    b = outlook.season_outlook(_league(), STARTERS, 1, [GIVE_STRONG], sims=400, seed=3)
    assert a == b


def test_playoff_override_beats_the_espn_value_and_fills_the_odds():
    two = outlook.season_outlook(_league(), STARTERS, 1, [], playoff_override=0, sims=500)
    all4 = outlook.season_outlook(_league(), STARTERS, 1, [], playoff_override=4, sims=500)
    assert two["playoff_teams"] == 2 and all4["playoff_teams"] == 4
    assert all4["baseline"]["playoff_prob"] == 1.0           # 4 of 4 teams always qualify
    assert two["baseline"]["playoff_prob"] < 1.0


def test_a_team_with_no_history_still_gets_a_floored_spread():
    sig = outlook._sigmas(_league(sigma_games=False))
    assert all(v == outlook.FALLBACK_SIGMA for v in sig.values())
    assert all(v >= outlook.MIN_SIGMA for v in outlook._sigmas(_league()).values())


def test_errors_are_explained_not_raised_as_crashes():
    with pytest.raises(outlook.OutlookError, match="unknown team"):
        outlook.season_outlook(_league(), STARTERS, 99, [])
    over = _league(); over.schedule = []
    with pytest.raises(outlook.OutlookError, match="no regular-season games"):
        outlook.season_outlook(over, STARTERS, 1, [])
    with pytest.raises(outlook.TradeError, match="out of date"):
        outlook.season_outlook(_league(), STARTERS, 1, [{**STEP, "give": [999]}])


# ---- schedule parsing ------------------------------------------------------------


def _matchups():
    game = lambda wk, h, a, hp=0, ap=0: {"matchupPeriodId": wk, "home": {"teamId": h, "totalPoints": hp},   # noqa: E731
                                          "away": {"teamId": a, "totalPoints": ap}}
    return {"settings": {"scheduleSettings": {"matchupPeriodCount": 3, "playoffTeamCount": 2}},
            "schedule": [game(1, 1, 2, 100, 90), game(2, 1, 2, 110, 95),
                         game(3, 2, 1), game(4, 1, 2)]}               # week 4 is a playoff week


def test_matchups_split_into_scores_and_a_regular_season_only_schedule():
    st = SeasonState(period=3)
    season.apply_matchups(st, _matchups())
    assert st.weekly_scores[1] == {1: 100.0, 2: 110.0}
    assert st.schedule == [(3, 2, 1)]                  # week 4 is past matchupPeriodCount
    assert (st.playoff_teams, st.regular_weeks) == (2, 3)


def test_matchups_without_settings_keep_the_whole_schedule():
    data = _matchups(); data.pop("settings")
    st = SeasonState(period=3)
    season.apply_matchups(st, data)
    assert st.schedule == [(3, 2, 1), (4, 1, 2)] and st.playoff_teams == 0


# ---- AI summary ------------------------------------------------------------------


def _outlook():
    return outlook.season_outlook(_league(), STARTERS, 1, [GIVE_STRONG], sims=200)


def _reply(content, status=200):
    body = {"choices": [{"message": {"content": content}}]}
    return httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(status, json=body)))


async def test_outlook_summary_is_parsed_and_clamped():
    good = json.dumps({"verdict": "WORTH_IT", "headline": "Take it.", "summary": "s",
                       "key_changes": list("abcde"), "risks": ["r"]})
    out = await advisor.summarize_outlook(_outlook(), "k", "m", client=_reply(good))
    assert out["available"] and out["verdict"] == "worth_it" and len(out["key_changes"]) == 3


async def test_unknown_outlook_verdict_falls_back_to_marginal():
    out = await advisor.summarize_outlook(_outlook(), "k", "m", client=_reply(json.dumps({"verdict": "yes"})))
    assert out["verdict"] == "marginal"


async def test_outlook_summary_degrades_without_a_key_or_on_error():
    assert (await advisor.summarize_outlook(_outlook(), "", "m"))["available"] is False
    out = await advisor.summarize_outlook(_outlook(), "k", "m", client=_reply("{}", 429))
    assert out["available"] is False and "429" in out["error"]


async def test_outlook_prompt_carries_the_computed_numbers_and_no_key():
    seen = {}

    def capture(req):
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    await advisor.summarize_outlook(_outlook(), "sk-secret", "m",
                                    client=httpx.AsyncClient(transport=httpx.MockTransport(capture)))
    user = json.loads(seen["body"]["messages"][1]["content"])
    assert "playoff_prob" in user["simulated"] and user["trades"]
    assert "sk-secret" not in json.dumps(seen["body"])


# ---- routes ----------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch):
    """App without its lifespan: no draft poller, no draft_log writes."""
    from fastapi.testclient import TestClient
    import app as A
    from config import Config

    st = _league()

    class Loader:
        state, error = st, None

        async def get(self, force=False):
            return st

    cfg = Config(league_id=1, season=2026, espn_s2="", swid="", my_team_id=1, scoring_format="PPR",
                 team_count=4, roster_slots={"QB": 1, "RB": 1, "BENCH": 2}, playoff_teams=0)
    monkeypatch.setattr(A, "season_loader", Loader())
    monkeypatch.setattr(A, "app_cfg", cfg)
    return TestClient(A.app)


def test_outlook_route_returns_both_scenarios(client):
    r = client.post("/api/trades/outlook", json={"ledger": [GIVE_STRONG]})
    assert r.status_code == 200
    j = r.json()
    assert j["team_id"] == 1 and j["delta"]["expected_wins"] < 0 and len(j["trades"]) == 1


def test_outlook_route_needs_a_trade_and_reports_a_stale_one(client):
    assert client.post("/api/trades/outlook", json={"ledger": []}).status_code == 422
    stale = client.post("/api/trades/outlook", json={"ledger": [{**GIVE_STRONG, "give": [999]}]})
    assert stale.status_code == 422 and "out of date" in stale.json()["error"]


def test_summary_route_returns_numbers_even_when_ai_is_unavailable(client):
    r = client.post("/api/trades/outlook/summary", json={"ledger": [GIVE_STRONG]})
    assert r.status_code == 200
    j = r.json()
    assert j["advice"]["available"] is False and "playoff_prob" in j["simulated"]


# ---- bye weeks --------------------------------------------------------------------


def test_a_bye_week_lowers_that_weeks_expected_points_and_only_that_week():
    st = _league()
    st.players[2].bye_week = 6                          # team 1's RB1 (22) is out in week 6; spare RB 12 (15) covers
    o = outlook.season_outlook(st, STARTERS, 1, [], sims=200)
    pts = {w["week"]: w["my_points"] for w in o["baseline"]["weekly"]}
    assert pts[6] < pts[5] == pts[7] == pts[8]
    assert pts[5] - pts[6] == pytest.approx(7, abs=0.2)
    wk6 = next(w for w in o["baseline"]["weekly"] if w["week"] == 6)
    assert wk6["my_bye_players"] == ["P2"] and next(w for w in o["baseline"]["weekly"] if w["week"] == 5)["my_bye_players"] == []


def test_byes_lower_expected_wins_and_points_per_week_against_the_same_team_without_them():
    plain = outlook.season_outlook(_league(), STARTERS, 1, [], sims=300)["baseline"]
    st = _league()
    st.players[2].bye_week = 6
    st.players[3].bye_week = 7
    byed = outlook.season_outlook(st, STARTERS, 1, [], sims=300)["baseline"]
    assert byed["expected_wins"] < plain["expected_wins"]
    assert byed["points_per_week"] < plain["points_per_week"]


def test_a_trade_is_judged_on_its_bye_cover_in_the_projection():
    st = _league()
    st.players[2].bye_week = 6                          # RB1 out in week 6, spare RB 12 covers
    gives_the_cover = {"team_a": 1, "team_b": 2, "give": [12], "get": [4]}     # trade the spare for team 2's weak RB4 (8)
    o = outlook.season_outlook(st, STARTERS, 1, [gives_the_cover], sims=300)
    wk = {w["week"]: w["my_points"] for w in o["simulated"]["weekly"]}
    base = {w["week"]: w["my_points"] for w in o["baseline"]["weekly"]}
    assert wk[6] < base[6]                              # the cover is gone in the bye week...
    assert wk[5] == base[5]                             # ...and nothing changes when RB1 plays
