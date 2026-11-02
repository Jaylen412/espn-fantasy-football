"""FastAPI entrypoint. See MD/REQUIREMENTS.md §6 endpoints.

Live:    uvicorn app:app --host 127.0.0.1 --port 8000 --reload
Replay:  python app.py --replay samples/      (or set REPLAY_DIR in .env)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from advisor import advise, recommend_package, summarize_briefing, summarize_outlook
import briefing
from analysis import analyze
from config import Config, load_config
from espn_client import ESPNClient
from outlook import OutlookError, season_outlook
from poller import Broadcaster, Poller, make_source
from season import SeasonLoader
from state import DraftState
from suggest import improve_package
from trade import (TradeError, describe_step, evaluate_simulated, evaluate_trade, player_row,
                   replay)

STATIC = Path(__file__).parent / "static"
HEARTBEAT_SECONDS = 10

log = logging.getLogger("app")

bus = Broadcaster()
poller: Poller | None = None
startup_error: str | None = None
season_loader: SeasonLoader | None = None
app_cfg: Config | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global poller, startup_error, season_loader, app_cfg
    cfg = load_config()
    app_cfg = cfg
    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
    )
    st = DraftState(cfg=cfg)
    poller = Poller(cfg, make_source(cfg), st, bus)
    try:
        await poller.startup()
    except Exception as exc:
        # Serve the dashboard anyway so the user sees *why* rather than a dead port (§1).
        startup_error = f"{type(exc).__name__}: {exc}"
        st.last_error = startup_error
        st.auth_error = "AuthError" in type(exc).__name__
        log.exception("startup failed — starting poller anyway to retry")
    poller.start()
    # In-season data comes straight from ESPN; replay mode has no live league to ask.
    season_client = None
    if not cfg.replay_dir:
        season_client = ESPNClient(cfg.league_id, cfg.season, cfg.espn_s2, cfg.swid)
        season_loader = SeasonLoader(season_client)
    try:
        yield
    finally:
        await poller.stop()
        if season_client:
            await season_client.aclose()


app = FastAPI(title="ESPN Live Fantasy Draft Dashboard", lifespan=lifespan)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/state")
async def api_state() -> JSONResponse:
    """Current full snapshot — also the fallback if EventSource fails (§6)."""
    if poller is None:
        return JSONResponse({"error": "not started", "connection_healthy": False}, status_code=503)
    # Recompute health on read so a stalled poller shows red without a new pick.
    snap = dict(poller.snapshot)
    snap["connection_healthy"] = poller.state.is_healthy()
    return JSONResponse(snap)


@app.get("/analysis")
async def analysis_page() -> FileResponse:
    return FileResponse(STATIC / "analysis.html")


@app.get("/api/analysis")
async def api_analysis(team_id: int | None = None) -> JSONResponse:
    """Pick grades, roster leverage and trade fits for one team.

    On demand rather than in the snapshot: it re-derives the board as of every
    pick, which is far too much work for a 4s poll loop, and §6 wants the SSE
    payload small. Defaults to your team; any team id works, which is what makes
    the trade section checkable against the other side.
    """
    if poller is None:
        return JSONResponse({"error": "not started"}, status_code=503)
    st = poller.state
    try:
        return JSONResponse(analyze(st, team_id))
    except Exception as exc:
        # Degrade to an explained empty report; a broken analysis must never take
        # the draft board down with it (§1).
        log.exception("analysis failed")
        return JSONResponse({"error": f"{type(exc).__name__}: {exc}",
                             "team_id": team_id if team_id is not None else st.cfg.my_team_id,
                             "picks": [], "lineup": {}, "positional": [],
                             "trade_targets": [], "leverage": [], "teams": []})


# ---- trade analysis (in-season) ---------------------------------------------------


class LedgerStep(BaseModel):
    team_a: int
    team_b: int
    give: list[int]
    get: list[int]


class TradeRequest(BaseModel):
    team_a: int | None = None      # defaults to MY_TEAM_ID
    team_b: int
    give: list[int]                # players team_a sends
    get: list[int]                 # players team_a receives
    ledger: list[LedgerStep] = []  # simulated trades already made; empty = standard analysis


class LedgerRequest(BaseModel):
    ledger: list[LedgerStep] = []


class SuggestRequest(BaseModel):
    team_id: int | None = None     # defaults to MY_TEAM_ID
    give: list[int] = []           # the package being improved; the give side anchors the search
    get: list[int] = []
    ledger: list[LedgerStep] = []
    free_give: bool = False        # also let the search add or swap bench pieces on the give side
    partner_id: int | None = None  # restrict the search to one team; default is the whole league
    ai: bool = True


class BriefingRequest(BaseModel):
    team_id: int | None = None     # defaults to MY_TEAM_ID
    force: bool = False            # regenerate the AI text even if one is cached for this data


class OutlookRequest(BaseModel):
    team_id: int | None = None     # defaults to MY_TEAM_ID
    ledger: list[LedgerStep] = []


async def _season():
    if season_loader is None:
        return None, "in-season data is unavailable in replay mode"
    st = await season_loader.get()
    if st is None:
        return None, season_loader.error or "season data has not loaded yet"
    return st, None


@app.get("/trades")
async def trades_page() -> FileResponse:
    return FileResponse(STATIC / "trades.html")


def _snapshot(st) -> dict:
    snap = st.snapshot()
    for team in snap["teams"]:
        team["roster"] = sorted((player_row(p) for p in st.roster(team["id"])),
                                key=lambda r: -r["value"])
    return snap


@app.post("/api/season/simulated")
async def api_season_simulated(req: LedgerRequest) -> JSONResponse:
    """Rosters as they stand after the simulated trades. 422 if the ledger no longer
    fits the real league (a player really moved), so the page can offer a reset."""
    st, err = await _season()
    if st is None:
        return JSONResponse({"error": err}, status_code=503)
    try:
        sim = replay(st, [s.model_dump() for s in req.ledger])
    except TradeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    return JSONResponse(_snapshot(sim))


@app.get("/api/season")
async def api_season(refresh: bool = False) -> JSONResponse:
    """Standings plus every roster with weekly stats — what the trade builder picks from."""
    if refresh and season_loader is not None:
        await season_loader.get(force=True)
    st, err = await _season()
    if st is None:
        return JSONResponse({"error": err}, status_code=503)
    snap = _snapshot(st)
    snap["my_team_id"] = app_cfg.my_team_id if app_cfg else 0
    snap["stale_error"] = season_loader.error if season_loader else None
    snap["ai_configured"] = bool(app_cfg and app_cfg.openai_api_key)
    return JSONResponse(snap)


def _evaluate(st, req: TradeRequest) -> dict:
    team_a = req.team_a if req.team_a is not None else app_cfg.my_team_id
    cap = sum(app_cfg.roster_slots.values())
    if req.ledger:
        return evaluate_simulated(st, app_cfg.starters, team_a, req.team_b, req.give, req.get,
                                  [s.model_dump() for s in req.ledger], roster_cap=cap)
    return evaluate_trade(st, app_cfg.starters, team_a, req.team_b, req.give, req.get, roster_cap=cap)


@app.post("/api/trades/evaluate")
async def api_trade_evaluate(req: TradeRequest) -> JSONResponse:
    st, err = await _season()
    if st is None:
        return JSONResponse({"error": err}, status_code=503)
    try:
        return JSONResponse(_evaluate(st, req))
    except TradeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        log.exception("trade evaluation failed")
        return JSONResponse({"error": f"{type(exc).__name__}: {exc}"}, status_code=500)


@app.post("/api/trades/advise")
async def api_trade_advise(req: TradeRequest) -> JSONResponse:
    """The analytics plus the OpenAI verdict. The analytics are returned even when
    the model call fails, so a dead key never hides the numbers."""
    st, err = await _season()
    if st is None:
        return JSONResponse({"error": err}, status_code=503)
    try:
        evaluation = _evaluate(st, req)
    except TradeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        log.exception("trade evaluation failed")
        return JSONResponse({"error": f"{type(exc).__name__}: {exc}"}, status_code=500)
    evaluation["advice"] = await advise(evaluation, app_cfg.openai_api_key, app_cfg.openai_model)
    return JSONResponse(evaluation)


async def _outlook(req: OutlookRequest):
    """(outlook, None) or (None, error response). The Monte Carlo is CPU work, so it
    runs off the event loop and cannot stall the draft poller or SSE stream."""
    st, err = await _season()
    if st is None:
        return None, JSONResponse({"error": err}, status_code=503)
    if not req.ledger:
        return None, JSONResponse({"error": "apply at least one simulated trade first"}, status_code=422)
    team_id = req.team_id if req.team_id is not None else app_cfg.my_team_id
    try:
        out = await asyncio.to_thread(season_outlook, st, app_cfg.starters, team_id,
                                      [s.model_dump() for s in req.ledger], app_cfg.playoff_teams)
        return out, None
    except (TradeError, OutlookError) as exc:
        return None, JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        log.exception("season outlook failed")
        return None, JSONResponse({"error": f"{type(exc).__name__}: {exc}"}, status_code=500)


@app.post("/api/trades/outlook")
async def api_trade_outlook(req: OutlookRequest) -> JSONResponse:
    out, err = await _outlook(req)
    return err or JSONResponse(out)


@app.post("/api/trades/outlook/summary")
async def api_trade_outlook_summary(req: OutlookRequest) -> JSONResponse:
    """The outlook plus an OpenAI read of it. The numbers come back even if the model fails."""
    out, err = await _outlook(req)
    if err:
        return err
    out["advice"] = await summarize_outlook(out, app_cfg.openai_api_key, app_cfg.openai_model)
    return JSONResponse(out)


@app.post("/api/trades/suggest")
async def api_trade_suggest(req: SuggestRequest) -> JSONResponse:
    """Grade the package you built and search every team for better ones. The shortlist
    is computed first and returned even if the OpenAI step fails or is switched off."""
    st, err = await _season()
    if st is None:
        return JSONResponse({"error": err}, status_code=503)
    team_id = req.team_id if req.team_id is not None else app_cfg.my_team_id
    ledger = [s.model_dump() for s in req.ledger]
    try:
        result = await asyncio.to_thread(
            improve_package, st, app_cfg.starters, team_id, req.give, req.get, ledger,
            sum(app_cfg.roster_slots.values()), app_cfg.playoff_teams, req.free_give, req.partner_id)
    except TradeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        log.exception("package search failed")
        return JSONResponse({"error": f"{type(exc).__name__}: {exc}"}, status_code=500)
    result["prior_trades"] = [describe_step(st, s) for s in ledger]
    if req.ai:
        result["advice"] = await recommend_package(result, app_cfg.openai_api_key, app_cfg.openai_model)
    return JSONResponse(result)


# ---- briefing: computed for you on load, cached until the underlying data changes ----

_briefing_cache: dict[int, dict] = {}
_briefing_locks = {name: asyncio.Lock() for name in ("overview", "moves", "advice")}


async def _briefing_section(name: str, team_id: int, refresh: bool, compute):
    """(payload, None) or (None, error response). Cached per team on the data fingerprint, so
    a page refresh is free but a roster move, injury or new week recomputes. One lock per
    section: the slow search never blocks the fast overview."""
    st, err = await _season()
    if st is None:
        return None, JSONResponse({"error": err}, status_code=503)
    if team_id not in st.teams:
        return None, JSONResponse({"error": f"unknown team {team_id}"}, status_code=422)
    key = briefing.fingerprint(st, team_id, app_cfg.playoff_teams)
    async with _briefing_locks[name]:
        entry = _briefing_cache.get(team_id)
        if not entry or entry["key"] != key:
            entry = _briefing_cache[team_id] = {"key": key}
        cached = name in entry and not refresh
        if not cached:
            try:
                entry[name] = await asyncio.to_thread(compute, st)
            except Exception as exc:
                log.exception("briefing %s failed", name)
                return None, JSONResponse({"error": f"{type(exc).__name__}: {exc}"}, status_code=500)
            entry[name + "_at"] = datetime.now(timezone.utc).isoformat()
        return {**entry[name], "cached": cached, "computed_at": entry[name + "_at"],
                "fingerprint": key}, None


def _briefing_team(team_id: int | None) -> int:
    return team_id if team_id is not None else app_cfg.my_team_id


@app.get("/api/briefing/overview")
async def api_briefing_overview(team_id: int | None = None, refresh: bool = False) -> JSONResponse:
    tid = _briefing_team(team_id)
    out, err = await _briefing_section(
        "overview", tid, refresh,
        lambda st: briefing.overview(st, app_cfg.starters, tid, app_cfg.playoff_teams))
    return err or JSONResponse(out)


@app.get("/api/briefing/moves")
async def api_briefing_moves(team_id: int | None = None, refresh: bool = False) -> JSONResponse:
    tid = _briefing_team(team_id)
    out, err = await _briefing_section(
        "moves", tid, refresh,
        lambda st: briefing.moves_and_plan(st, app_cfg.starters, tid,
                                           sum(app_cfg.roster_slots.values()), app_cfg.playoff_teams))
    return err or JSONResponse(out)


@app.post("/api/briefing/summary")
async def api_briefing_summary(req: BriefingRequest) -> JSONResponse:
    """The written AI briefing. Runs only when the user presses the button, and a repeat press
    on unchanged data returns the saved text unless `force` is set."""
    tid = _briefing_team(req.team_id)
    over, err = await _briefing_section(
        "overview", tid, False, lambda st: briefing.overview(st, app_cfg.starters, tid, app_cfg.playoff_teams))
    if err:
        return err
    mv, err = await _briefing_section(
        "moves", tid, False,
        lambda st: briefing.moves_and_plan(st, app_cfg.starters, tid, sum(app_cfg.roster_slots.values()),
                                           app_cfg.playoff_teams))
    if err:
        return err
    async with _briefing_locks["advice"]:
        entry = _briefing_cache[tid]
        if entry.get("advice") and not req.force:
            return JSONResponse({"advice": entry["advice"], "cached": True, "computed_at": entry["advice_at"]})
        payload, refs = briefing.ai_payload(over, mv)
        advice = await summarize_briefing(payload, refs, app_cfg.openai_api_key, app_cfg.openai_model)
        if advice["available"]:                      # never cache a failure: the next press should retry
            entry["advice"], entry["advice_at"] = advice, datetime.now(timezone.utc).isoformat()
        return JSONResponse({"advice": advice, "cached": False,
                             "computed_at": datetime.now(timezone.utc).isoformat()})


@app.get("/api/health")
async def api_health() -> JSONResponse:
    """Poller status, last successful fetch, consecutive error count (§6)."""
    if poller is None:
        return JSONResponse({"status": "starting"}, status_code=503)
    st = poller.state
    return JSONResponse({
        "poller_running": poller.started,
        "last_successful_poll": st.last_successful_poll.isoformat() if st.last_successful_poll else None,
        "seconds_since_last_poll": (
            (datetime.now(timezone.utc) - st.last_successful_poll).total_seconds()
            if st.last_successful_poll else None
        ),
        "consecutive_errors": st.consecutive_errors,
        "last_error": st.last_error,
        "startup_error": startup_error,
        "auth_error": st.auth_error,
        "connection_healthy": st.is_healthy(),
        "subscribers": bus.count,
        "picks_seen": len(st.picks),
        "players_known": len(st.players),
    })


@app.get("/events")
async def events() -> StreamingResponse:
    """SSE stream of full snapshots (§6)."""
    queue = bus.subscribe()

    async def stream():
        try:
            if poller is not None:
                yield f"data: {json.dumps(poller.snapshot)}\n\n"
            while True:
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
                    yield f"data: {json.dumps(payload)}\n\n"
                except asyncio.TimeoutError:
                    # Comment frame: keeps proxies and the browser from giving up,
                    # and lets the client notice a dead server.
                    yield ": heartbeat\n\n"
        except asyncio.CancelledError:
            raise
        finally:
            bus.unsubscribe(queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                 "Connection": "keep-alive"},
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="ESPN live draft dashboard")
    parser.add_argument("--replay", metavar="DIR",
                        help="serve recorded fixtures from DIR instead of live ESPN (§11.2)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    if args.replay:
        os.environ["REPLAY_DIR"] = args.replay

    import uvicorn
    uvicorn.run("app:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
