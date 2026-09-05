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

from config import load_config
from poller import Broadcaster, Poller, make_source
from state import DraftState

STATIC = Path(__file__).parent / "static"
HEARTBEAT_SECONDS = 10

log = logging.getLogger("app")

bus = Broadcaster()
poller: Poller | None = None
startup_error: str | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global poller, startup_error
    cfg = load_config()
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
    try:
        yield
    finally:
        await poller.stop()


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
