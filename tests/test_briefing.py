"""Decision-first briefing: attention rules, position strength, best moves, chained plan,
caching and the AI guard rails. In-memory fixtures only."""
import json

import httpx
import pytest

import advisor
import briefing
from season import SeasonPlayer, SeasonState, SeasonTeam

STARTERS = {"RB": 1, "WR": 1}


def _p(pid, pos, pts, team, slot="BENCH", injury="ACTIVE", games=4, recent=None):
    weekly = {w: pts for w in range(1, games + 1)}
    if recent is not None:
        for w in range(max(1, games - 2), games + 1):
            weekly[w] = recent
    return SeasonPlayer(id=pid, name=f"P{pid}", position=pos, team_id=team, slot=slot, injury=injury,
                        weekly=weekly, proj_week=pts)


def _league(schedule=True):
    """Team 1 starts RB 2 and WR 3 and holds a spare RB (12). Team 3 has a far better WR (31)
    with a backup (32) and a weak RB; team 2 is thin. Positions are chosen so team 1 is
    strong at RB and weak at WR relative to the league."""
    st = SeasonState(period=5, final_period=17, playoff_teams=2, regular_weeks=8)
    rosters = {
        1: [_p(2, "RB", 22, 1, "RB"), _p(3, "WR", 6, 1, "WR"), _p(12, "RB", 15, 1), _p(14, "RB", 3, 1)],
        2: [_p(4, "RB", 8, 2, "RB"), _p(21, "WR", 9, 2, "WR")],
        3: [_p(5, "RB", 10, 3, "RB"), _p(31, "WR", 18, 3, "WR"), _p(32, "WR", 14, 3)],
        4: [_p(6, "RB", 12, 4, "RB"), _p(41, "WR", 17, 4, "WR")],
    }
    for tid, ps in rosters.items():
        st.teams[tid] = SeasonTeam(id=tid, name=f"T{tid}", wins=2, losses=2, points_for=300.0 + tid,
                                   player_ids=[p.id for p in ps])
        for p in ps:
            st.players[p.id] = p
        st.weekly_scores[tid] = {1: 90 + tid, 2: 100 - tid, 3: 85 + 2 * tid, 4: 105 - tid}
    if schedule:
        for wk in range(5, 9):
            st.schedule += [(wk, 1, 2) if wk % 2 else (wk, 1, 3), (wk, 3, 4) if wk % 2 else (wk, 2, 4)]
    return st


def _over(st=None, **kw):
    return briefing.overview(st or _league(), kw.pop("starters", STARTERS), 1, **kw)


def _kinds(over):
    return [a["kind"] for a in over["attention"]]


# ---- attention list ---------------------------------------------------------------


def test_an_injured_starter_is_the_first_thing_you_are_told():
    st = _league()
    st.players[2].injury = "OUT"
    over = _over(st)
    first = over["attention"][0]
    assert first["kind"] == "injured_starter" and first["severity"] == "high"
    assert "P2" in first["text"] and "RB slot" in first["text"] and first["player_ids"] == [2]


def test_a_questionable_starter_is_a_medium_warning_not_an_alarm():
    st = _league()
    st.players[3].injury = "QUESTIONABLE"
    item = next(a for a in _over(st)["attention"] if a["kind"] == "questionable_starter")
    assert item["severity"] == "med"


def test_an_injured_bench_player_is_not_reported():
    st = _league()
    st.players[12].injury = "OUT"
    assert "injured_starter" not in _kinds(_over(st))


def test_a_slot_with_no_healthy_player_is_flagged():
    over = _over(starters={"RB": 1, "WR": 1, "TE": 1})
    item = next(a for a in over["attention"] if a["kind"] == "empty_slot")
    assert item["severity"] == "high" and "TE" in item["text"]


def test_a_better_lineup_is_suggested_with_what_it_is_worth():
    st = _league()
    st.players[32].slot, st.players[32].team_id = "BENCH", 1       # a bench WR better than the starter
    st.teams[1].player_ids.append(32)
    st.teams[3].player_ids.remove(32)
    st.players[32].weekly = {w: 14 for w in range(1, 5)}
    item = next(a for a in _over(st)["attention"] if a["kind"] == "set_lineup")
    assert "P32" in item["text"] and "P3" in item["text"] and "+8.0" in item["text"]


def test_an_already_optimal_lineup_gets_no_lineup_advice():
    assert "set_lineup" not in _kinds(_over())


def test_only_a_real_decline_in_a_starter_is_called_out():
    st = _league()
    st.players[2] = _p(2, "RB", 22, 1, "RB", games=6, recent=10)    # 22 early, 10 lately: gap of 6
    st.players[12] = _p(12, "RB", 15, 1, games=6, recent=2)         # also declining, but on the bench
    st.players[3] = _p(3, "WR", 6, 1, "WR", games=2, recent=1)      # too few games to call a trend
    trend = [a for a in _over(st)["attention"] if a["kind"] == "trending_down"]
    assert [a["player_ids"] for a in trend] == [[2]]


def test_weak_and_strong_positions_come_from_the_league_comparison():
    over = _over()
    by = {p["position"]: p for p in over["positions"]}
    assert by["RB"]["rank"] == 1 and by["WR"]["rank"] == 4 and by["WR"]["teams"] == 4
    assert by["RB"]["mine"] == 22 and by["RB"]["league_avg"] == pytest.approx(13.0)
    kinds = _kinds(over)
    assert "weak_position" in kinds and "strong_position" in kinds


def test_attention_is_sorted_most_urgent_first_and_has_ids():
    st = _league()
    st.players[3].injury = "OUT"
    items = _over(st)["attention"]
    order = [{"high": 0, "med": 1, "info": 2}[i["severity"]] for i in items]
    assert order == sorted(order) and [i["id"] for i in items] == [f"a{n}" for n in range(1, len(items) + 1)]


# ---- status ----------------------------------------------------------------------


def test_status_carries_the_projection_when_games_remain():
    s = _over()["status"]
    assert s["projection"] is True and s["weeks_remaining"] == 4 and s["playoff_teams"] == 2
    assert 0 <= s["playoff_prob"] <= 1 and s["record_now"] == "2-2" and s["standing_now"] in (1, 2, 3, 4)


def test_status_without_remaining_games_shows_the_record_not_a_projection():
    s = _over(_league(schedule=False))["status"]
    assert s["projection"] is False and s["weeks_remaining"] == 0 and "playoff_prob" not in s


def test_unknown_team_is_rejected():
    with pytest.raises(ValueError, match="unknown team"):
        briefing.overview(_league(), STARTERS, 99)


# ---- fingerprint -------------------------------------------------------------------


def test_fingerprint_is_stable_but_changes_with_any_input_that_matters():
    a = briefing.fingerprint(_league(), 1)
    assert a == briefing.fingerprint(_league(), 1)
    inj = _league(); inj.players[2].injury = "OUT"
    wk = _league(); wk.period = 6
    moved = _league(); moved.players[12].slot = "RB"
    assert len({a, briefing.fingerprint(inj, 1), briefing.fingerprint(wk, 1),
                briefing.fingerprint(moved, 1), briefing.fingerprint(_league(), 2),
                briefing.fingerprint(_league(), 1, playoff_override=4)}) == 6


# ---- moves and plan ----------------------------------------------------------------


def _mp(st=None, **kw):
    return briefing.moves_and_plan(st or _league(), STARTERS, 1, **kw)


def test_moves_are_independent_and_never_offer_the_same_player_twice():
    mv = _mp()["moves"]
    assert 1 <= len(mv) <= briefing.MOVES
    gives = [p["id"] for m in mv for p in m["give"]]
    assert len(gives) == len(set(gives)) and [m["id"] for m in mv] == [f"m{i}" for i in range(1, len(mv) + 1)]


def test_the_best_move_helps_you_and_does_not_gut_the_partner():
    best = _mp()["moves"][0]
    assert best["partner_id"] == 3 and best["you_weekly_delta"] > 0 and best["playoff_gain"] > 0


def test_plan_steps_add_up_to_the_plans_total_and_never_flip_acquired_players():
    plan = _mp()["plan"]
    assert plan and 1 <= len(plan["steps"]) <= briefing.PLAN_STEPS
    acquired = set()
    for s in plan["steps"]:
        assert not ({p["id"] for p in s["give"]} & acquired)
        acquired |= {p["id"] for p in s["get"]}
    total = plan["outlook"]["delta"]["playoff_prob"]
    assert sum(s["playoff_gain"] for s in plan["steps"]) == pytest.approx(total, abs=0.002)
    assert plan["ledger"] == [s["step"] for s in plan["steps"]] and len(plan["trades"]) == len(plan["steps"])
    assert [s["id"] for s in plan["steps"]] == [f"p{i}" for i in range(1, len(plan["steps"]) + 1)]


def test_when_nothing_helps_there_is_no_plan_and_no_moves():
    st = SeasonState(period=5, final_period=17, regular_weeks=8, playoff_teams=1)
    for tid in (1, 2):
        st.teams[tid] = SeasonTeam(id=tid, name=f"T{tid}", player_ids=[tid * 10])
        st.players[tid * 10] = _p(tid * 10, "RB", 10, tid, "RB")
        st.weekly_scores[tid] = {1: 100, 2: 90}
    st.schedule = [(5, 1, 2)]
    out = briefing.moves_and_plan(st, {"RB": 1}, 1)
    assert out["moves"] == [] and out["plan"] is None


def test_the_ai_sees_only_computed_numbers_and_can_cite_only_what_exists():
    over, mv = _over(), _mp()
    payload, refs = briefing.ai_payload(over, mv)
    assert refs == {a["id"] for a in over["attention"]} | {m["id"] for m in mv["moves"]} | {"plan"}
    text = json.dumps(payload)
    assert '"step"' not in text and '"team_a"' not in text                # no internal step specs
    assert payload["plan"]["id"] == "plan" and payload["moves"][0]["id"] == "m1"


# ---- AI briefing --------------------------------------------------------------------


def _reply(content, status=200):
    body = {"choices": [{"message": {"content": content}}]}
    return httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(status, json=body)))


async def test_only_cited_ids_that_exist_survive_and_each_once():
    raw = {"headline": "h", "summary": "s", "watch_out": list("abcde"),
           "actions": [{"ref": "a1", "do": "Bench him", "why": "OUT"},
                       {"ref": "a1", "do": "again", "why": "dup"},
                       {"ref": "m9", "do": "invented", "why": "x"},
                       {"ref": "plan", "do": "Run the plan", "why": "+14%"}] + [{"ref": f"m{i}"} for i in (1, 2, 3, 4)]}
    out = await advisor.summarize_briefing({}, {"a1", "m1", "m2", "plan"}, "k", "m", client=_reply(json.dumps(raw)))
    assert out["available"] and [a["ref"] for a in out["actions"]] == ["a1", "plan", "m1", "m2"]
    assert len(out["watch_out"]) == 3


async def test_briefing_degrades_without_a_key_or_on_error():
    assert (await advisor.summarize_briefing({}, {"a1"}, "", "m"))["available"] is False
    out = await advisor.summarize_briefing({}, {"a1"}, "k", "m", client=_reply("{}", 500))
    assert out["available"] is False and "500" in out["error"]


# ---- routes -------------------------------------------------------------------------


@pytest.fixture
def api(monkeypatch):
    """App without its lifespan (no poller, no draft_log). Returns (client, state, module)."""
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
    A._briefing_cache.clear()
    return TestClient(A.app), st, A


def test_overview_is_computed_once_and_then_served_from_cache(api):
    c, _, _ = api
    first = c.get("/api/briefing/overview").json()
    again = c.get("/api/briefing/overview").json()
    assert first["cached"] is False and again["cached"] is True
    assert first["computed_at"] == again["computed_at"] and first["status"]["team_name"] == "T1"
    assert c.get("/api/briefing/overview?refresh=true").json()["cached"] is False


def test_a_roster_move_or_injury_invalidates_the_cache(api):
    c, st, _ = api
    c.get("/api/briefing/moves")
    assert c.get("/api/briefing/moves").json()["cached"] is True
    st.players[2].injury = "OUT"
    changed = c.get("/api/briefing/moves").json()
    assert changed["cached"] is False


def test_moves_route_returns_moves_and_a_plan(api):
    c, _, _ = api
    j = c.get("/api/briefing/moves").json()
    assert j["moves"] and j["plan"]["steps"] and j["projection"] is True


def test_unknown_team_is_a_422_not_a_crash(api):
    c, _, _ = api
    assert c.get("/api/briefing/overview?team_id=99").status_code == 422


def test_summary_without_a_key_reports_unavailable_and_is_not_cached(api):
    c, _, A = api
    r = c.post("/api/briefing/summary", json={})
    assert r.status_code == 200 and r.json()["advice"]["available"] is False
    assert "advice" not in A._briefing_cache[1]


def test_summary_is_generated_on_request_and_reused_until_forced(api, monkeypatch):
    c, _, A = api
    calls = []

    async def fake(payload, refs, key, model, client=None):
        calls.append(sorted(refs))
        return {"available": True, "headline": "h", "summary": "s", "actions": [], "watch_out": []}

    monkeypatch.setattr(A, "summarize_briefing", fake)
    c.get("/api/briefing/overview"); c.get("/api/briefing/moves")
    assert calls == []                                              # nothing calls the AI until asked
    assert c.post("/api/briefing/summary", json={}).json()["cached"] is False
    assert c.post("/api/briefing/summary", json={}).json()["cached"] is True and len(calls) == 1
    assert c.post("/api/briefing/summary", json={"force": True}).json()["cached"] is False and len(calls) == 2


# ---- bye weeks ----------------------------------------------------------------------


def test_a_starter_on_his_bye_this_week_is_an_urgent_replacement():
    st = _league()
    st.players[3].bye_week = st.period                   # team 1's WR starter; no other WR on the roster
    kinds = _kinds(_over(st))
    first = _over(st)["attention"][0]
    assert first["kind"] in ("bye_starter", "empty_slot") and first["severity"] == "high"
    item = next(a for a in _over(st)["attention"] if a["kind"] == "bye_starter")
    assert "P3" in item["text"] and "bye this week" in item["text"] and "WR slot" in item["text"]
    assert "empty_slot" in kinds                         # nobody else can fill WR this week, and it says why
    assert "bye" in next(a for a in _over(st)["attention"] if a["kind"] == "empty_slot")["text"]


def test_the_lineup_advice_replaces_a_bye_starter_with_a_bench_player():
    st = _league()
    st.players[2].bye_week = st.period                   # RB1 (22) is out this week; bench RB 12 (15) should start
    item = next(a for a in _over(st)["attention"] if a["kind"] == "set_lineup")
    assert "P12" in item["text"] and "P2" in item["text"]
    assert "+15.0" in item["text"]                       # 15 (the bench RB) replaces a bye starter who scores 0


def test_a_costly_upcoming_bye_is_flagged_with_the_week_and_what_it_costs():
    st = _league()
    st.players[2].bye_week = st.period + 1               # next week: RB1 (22) out, bench RB (15) covers -> -7
    item = next(a for a in _over(st)["attention"] if a["kind"] == "bye_week")
    assert item["severity"] == "med" and f"Week {st.period + 1} bye" in item["text"] and "P2" in item["text"]
    assert "7.0" in item["text"] and item["player_ids"] == [2]


def test_small_or_past_byes_are_not_worth_a_line():
    st = _league()
    st.players[14].bye_week = st.period + 2              # a 3-point bench player: costs nothing
    st.players[2].bye_week = st.period - 1               # already happened
    assert "bye_week" not in _kinds(_over(st)) and "bye_starter" not in _kinds(_over(st))


def test_bye_aware_moves_still_run_and_a_plan_adds_up():
    st = _league()
    st.players[2].bye_week = st.period + 1
    plan = _mp(st)["plan"]
    assert plan and sum(s["playoff_gain"] for s in plan["steps"]) == pytest.approx(plan["outlook"]["delta"]["playoff_prob"], abs=0.002)
