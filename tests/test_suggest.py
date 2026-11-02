"""Package search ("improve my package"), its AI step and its route. In-memory fixtures only."""
import json

import httpx
import pytest

import advisor
import suggest
from season import SeasonPlayer, SeasonState, SeasonTeam
from trade import TradeError

STARTERS = {"RB": 1, "WR": 1}


def _p(pid, pos, pts, team, injury="ACTIVE"):
    return SeasonPlayer(id=pid, name=f"P{pid}", position=pos, team_id=team, injury=injury,
                        weekly={w: pts for w in range(1, 5)}, proj_week=pts)


def _league():
    """Team 1 has a spare RB (12) and a weak WR. Team 2 can only offer a mediocre WR (21), and
    has no backup, so it is worse off losing him. Team 3 has a far better WR (31) with a
    backup (32) and a weak RB, so *team 3* is the better partner for the spare RB."""
    st = SeasonState(period=5, final_period=17, playoff_teams=2, regular_weeks=8)
    rosters = {
        1: [_p(2, "RB", 22, 1), _p(12, "RB", 15, 1), _p(3, "WR", 6, 1), _p(14, "RB", 3, 1), _p(15, "WR", 2, 1)],
        2: [_p(4, "RB", 8, 2), _p(21, "WR", 9, 2)],
        3: [_p(5, "RB", 10, 3), _p(31, "WR", 18, 3), _p(32, "WR", 14, 3)],
        4: [_p(6, "RB", 12, 4), _p(41, "WR", 7, 4)],
    }
    for tid, ps in rosters.items():
        st.teams[tid] = SeasonTeam(id=tid, name=f"T{tid}", wins=2, losses=2, points_for=300.0 + tid,
                                   player_ids=[p.id for p in ps])
        for p in ps:
            st.players[p.id] = p
        st.weekly_scores[tid] = {1: 90 + tid, 2: 100 - tid, 3: 85 + 2 * tid, 4: 105 - tid}
    for wk in range(5, 9):
        st.schedule += [(wk, 1, 2) if wk % 2 else (wk, 1, 3), (wk, 3, 4) if wk % 2 else (wk, 2, 4)]
    return st


def _search(st=None, give=(12,), get=(21,), ledger=(), **kw):
    kw.setdefault("sims", 400)
    return suggest.improve_package(st or _league(), STARTERS, 1, list(give), list(get), list(ledger), **kw)


def test_finds_a_better_package_at_a_different_team_than_the_one_you_picked():
    r = _search()
    assert r["yours"]["partner_id"] == 2
    best = r["candidates"][0]
    assert best["partner_id"] == 3 and best["better_than_yours"] is True
    assert best["playoff_gain"] > r["yours"]["playoff_gain"] or best["you_weekly_delta"] > r["yours"]["you_weekly_delta"]
    assert [c["id"] for c in r["candidates"]][:2] == ["c1", "c2"]


def test_the_give_side_stays_anchored_unless_you_free_it():
    r = _search()
    assert r["candidates"] and all([p["id"] for p in c["give"]] == [12] for c in r["candidates"])


def test_free_give_can_add_or_swap_bench_pieces():
    r = _search(free_give=True)
    gives = {tuple(p["id"] for p in c["give"]) for c in r["candidates"]}
    assert gives != {(12,)} and r["searched"] > _search()["searched"]


def test_candidates_are_ranked_by_playoff_gain_then_wins():
    gains = [(c["playoff_gain"], c["wins_gain"]) for c in _search()["candidates"]]
    assert gains == sorted(gains, reverse=True)


def test_every_candidate_helps_you_and_does_not_gut_the_partner():
    for c in _search()["candidates"]:
        assert c["you_weekly_delta"] >= suggest.MIN_GAIN
        assert c["them_weekly_delta"] >= -suggest.MAX_PARTNER_LOSS


def test_the_package_you_built_is_not_offered_back_to_you():
    r = _search()
    assert not any(c["partner_id"] == 2 and [p["id"] for p in c["get"]] == [21]
                   and [p["id"] for p in c["give"]] == [12] for c in r["candidates"])


def test_partner_filter_limits_the_search_to_one_team():
    r = _search(partner_id=3)
    assert r["candidates"] and {c["partner_id"] for c in r["candidates"]} == {3}
    assert _search(partner_id=4)["candidates"] == []            # team 4 cannot afford to lose its only WR


def test_an_injured_player_is_never_suggested_as_your_return():
    st = _league()
    st.players[31].injury = "OUT"
    got = {p["id"] for c in _search(st)["candidates"] for p in c["get"]}
    assert 31 not in got


def test_a_package_with_a_full_roster_is_kept_but_flagged():
    r = _search(roster_cap=5, partner_id=3)
    two_for_one = [c for c in r["candidates"] if len(c["get"]) == 2]
    assert two_for_one and all(any("drop is needed" in f for f in c["flags"]) for c in two_for_one)


def test_give_only_package_still_gets_alternatives_and_no_grade():
    r = _search(get=())
    assert r["yours"] is None and r["candidates"]


def test_simulated_trades_are_part_of_the_league_the_search_runs_on():
    step = {"team_a": 1, "team_b": 3, "give": [12], "get": [31]}
    with pytest.raises(TradeError, match="not on team 1"):
        _search(ledger=[step])                              # 12 already left in the ledger
    r = _search(give=(31,), get=(5,), ledger=[step])        # but 31, acquired in it, can be moved on
    assert r["yours"]["give"][0]["id"] == 31


@pytest.mark.parametrize("kw,msg", [
    (dict(give=(), get=()), "add at least one player"),
    (dict(give=(99,)), "not on team 1"),
    (dict(give=(12, 12)), "twice"),
    (dict(get=(2,)), "one other team"),
])
def test_bad_packages_are_explained(kw, msg):
    with pytest.raises(TradeError, match=msg):
        _search(**kw)


def test_stale_ledger_and_unknown_team_are_rejected():
    with pytest.raises(TradeError, match="out of date"):
        _search(ledger=[{"team_a": 1, "team_b": 3, "give": [999], "get": [31]}])
    with pytest.raises(TradeError, match="unknown team"):
        suggest.improve_package(_league(), STARTERS, 99, [12], [21], [])


# ---- AI step -------------------------------------------------------------------


def _reply(content, status=200):
    body = {"choices": [{"message": {"content": content}}]}
    return httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(status, json=body)))


async def test_recommendations_are_limited_to_candidates_we_offered():
    result = _search()
    ids = [c["id"] for c in result["candidates"]]
    raw = {"your_package": {"verdict": "WEAK", "assessment": "meh"},
           "recommendations": [{"candidate_id": ids[0], "reason": "r", "risk": "k", "pitch": "p"},
                               {"candidate_id": "c99", "reason": "invented"},          # not offered
                               {"candidate_id": ids[0], "reason": "duplicate"},        # twice
                               {"candidate_id": ids[1], "reason": "r2"}]}
    out = await advisor.recommend_package(result, "k", "m", client=_reply(json.dumps(raw)))
    assert out["available"] and out["your_package"]["verdict"] == "weak"
    assert [r["candidate_id"] for r in out["recommendations"]] == [ids[0], ids[1]]


async def test_unknown_package_verdict_defaults_to_fair_and_recs_cap_at_three():
    result = _search(free_give=True)
    ids = [c["id"] for c in result["candidates"]][:5]
    raw = {"your_package": {"verdict": "great"},
           "recommendations": [{"candidate_id": i} for i in ids]}
    out = await advisor.recommend_package(result, "k", "m", client=_reply(json.dumps(raw)))
    assert out["your_package"]["verdict"] == "fair" and len(out["recommendations"]) <= 3


async def test_recommender_degrades_without_candidates_key_or_on_error():
    assert (await advisor.recommend_package({"candidates": []}, "k", "m"))["available"] is False
    assert (await advisor.recommend_package(_search(), "", "m"))["available"] is False
    out = await advisor.recommend_package(_search(), "k", "m", client=_reply("{}", 500))
    assert out["available"] is False and "500" in out["error"]


async def test_prompt_carries_candidate_ids_and_never_the_key():
    seen = {}

    def capture(req):
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    await advisor.recommend_package(_search(), "sk-secret", "m",
                                    client=httpx.AsyncClient(transport=httpx.MockTransport(capture)))
    user = json.loads(seen["body"]["messages"][1]["content"])
    assert user["candidates"][0]["id"] == "c1" and user["yours"]["partner_id"] == 2
    assert "sk-secret" not in json.dumps(seen["body"])


# ---- route ---------------------------------------------------------------------


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
                 team_count=4, roster_slots={"RB": 1, "WR": 1, "BENCH": 4})
    monkeypatch.setattr(A, "season_loader", Loader())
    monkeypatch.setattr(A, "app_cfg", cfg)
    return TestClient(A.app)


def test_route_returns_the_shortlist_even_when_the_ai_is_unavailable(client):
    r = client.post("/api/trades/suggest", json={"give": [12], "get": [21]})
    assert r.status_code == 200
    j = r.json()
    assert j["candidates"][0]["partner_id"] == 3 and j["advice"]["available"] is False   # no key in this config


def test_route_can_skip_the_ai_and_reports_prior_trades(client):
    step = {"team_a": 1, "team_b": 3, "give": [12], "get": [31]}
    j = client.post("/api/trades/suggest", json={"give": [31], "get": [5], "ledger": [step], "ai": False}).json()
    assert "advice" not in j and len(j["prior_trades"]) == 1 and "P12" in j["prior_trades"][0]


def test_route_explains_bad_requests(client):
    assert client.post("/api/trades/suggest", json={"give": [], "get": []}).status_code == 422
    stale = client.post("/api/trades/suggest", json={"give": [12], "get": [21],
                                                      "ledger": [{"team_a": 1, "team_b": 3, "give": [999], "get": [31]}]})
    assert stale.status_code == 422 and "out of date" in stale.json()["error"]
