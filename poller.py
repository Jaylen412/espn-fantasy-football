"""Background poll loop. See MD/REQUIREMENTS.md §6 flow and §9 error handling.

The loop body is wrapped broadly on purpose: a crashed poller mid-draft is the
one unrecoverable outcome (§9).
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from config import Config
from espn_client import AuthError, ESPNClient, ESPNError, RateLimited, with_backoff
from recommender import recommend
from state import DraftState

log = logging.getLogger("poller")

POOL_REFRESH_SECONDS = 60      # §5.2
UNHEALTHY_AFTER_ERRORS = 3     # §9
BACKOFF_CAP = 30.0             # §9


class Source(Protocol):
    """Where draft data comes from — live ESPN or recorded fixtures (§11.2)."""

    async def league_setup(self) -> dict[str, Any]: ...
    async def player_pool(self) -> dict[str, Any]: ...
    async def draft_detail(self) -> dict[str, Any]: ...
    async def aclose(self) -> None: ...


class LiveSource:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.client = ESPNClient(cfg.league_id, cfg.season, cfg.espn_s2, cfg.swid)

    async def league_setup(self) -> dict[str, Any]:
        return await with_backoff(self.client.league_setup)

    async def player_pool(self) -> dict[str, Any]:
        return await with_backoff(lambda: self.client.player_pool(self.cfg.rank_type))

    async def draft_detail(self) -> dict[str, Any]:
        return await self.client.draft_detail()

    async def aclose(self) -> None:
        await self.client.aclose()


class ReplaySource:
    """Feeds recorded responses to the poller on a timer, so the UI and
    recommender can be developed offline without burning requests (§11.2).

    Expects a directory holding `league_setup.json`, `player_pool.json`, and one
    or more `draft_detail*.json` snapshots served in filename order.
    """

    def __init__(self, directory: str | Path):
        self.dir = Path(directory)
        self.frames = sorted(self.dir.glob("draft_detail*.json"))
        self.index = 0
        if not self.frames:
            raise FileNotFoundError(f"no draft_detail*.json fixtures in {self.dir}")
        log.info("replay: %d draft_detail frames from %s", len(self.frames), self.dir)

    def _read(self, name: str) -> dict[str, Any]:
        path = self.dir / name
        if not path.exists():
            return {}
        return json.loads(path.read_text())

    async def league_setup(self) -> dict[str, Any]:
        return self._read("league_setup.json")

    async def player_pool(self) -> dict[str, Any]:
        return self._read("player_pool.json")

    async def draft_detail(self) -> dict[str, Any]:
        frame = self.frames[min(self.index, len(self.frames) - 1)]
        self.index = min(self.index + 1, len(self.frames))
        return json.loads(frame.read_text())

    async def aclose(self) -> None:
        return None


class Broadcaster:
    """Fan-out of full snapshots to every connected SSE client (§6)."""

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=4)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    @property
    def count(self) -> int:
        return len(self._subscribers)

    def publish(self, payload: dict[str, Any]) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                # A slow browser must never block the poller; it will catch up on
                # the next snapshot since these are full states, not deltas.
                pass


class Poller:
    def __init__(self, cfg: Config, source: Source, st: DraftState, bus: Broadcaster):
        self.cfg = cfg
        self.source = source
        self.state = st
        self.bus = bus
        self.snapshot: dict[str, Any] = st.snapshot()
        self._task: asyncio.Task | None = None
        self._last_pool_refresh = 0.0
        self.started = False
        # Seams so tests can exercise the retry path without real-time waits.
        self.backoff_base = 1.0
        self.backoff_cap = BACKOFF_CAP

    # ---- lifecycle ----

    async def startup(self) -> None:
        """Fetch league shape and the player pool, then reconstruct full state.

        Restarting mid-draft rebuilds everything from mDraftDetail (§12).
        """
        setup = await self.source.league_setup()
        self.state.load_league(setup)
        try:
            pool = await self.source.player_pool()
            self.state.load_players(pool)
        except ESPNError as exc:
            # Degrade to showing raw data rather than crashing (§1).
            log.error("player pool unavailable at startup (%s) — picks will still render", exc)
        self.state.apply_draft_detail(setup)
        self.state.last_successful_poll = datetime.now(timezone.utc)
        self._last_pool_refresh = asyncio.get_event_loop().time()
        self.started = True
        self._recompute()
        log.info("startup complete: %d teams, %d players, %d picks already made",
                 len(self.state.teams), len(self.state.players), len(self.state.picks))

    def start(self) -> None:
        self._task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self.source.aclose()

    # ---- loop ----

    async def run(self) -> None:
        backoff = 0.0
        while True:
            try:
                await self._tick()
                backoff = 0.0
                await asyncio.sleep(self.cfg.poll_interval)
            except asyncio.CancelledError:
                raise
            except AuthError as exc:
                # Cookies expired — surface it, do not hammer ESPN (§9).
                self.state.auth_error = True
                self._record_error(exc)
                self._recompute()
                await asyncio.sleep(self.backoff_cap)
            except RateLimited as exc:
                self._record_error(exc)
                backoff = min(max(backoff * 2, self.backoff_base), self.backoff_cap)
                log.warning("rate limited; backing off %.1fs", backoff)
                self._recompute()
                await asyncio.sleep(backoff)
            except Exception as exc:  # the poller must never die (§9)
                self._record_error(exc)
                log.exception("poll failed")
                backoff = min(max(backoff * 2, self.backoff_base), self.backoff_cap)
                self._recompute()
                await asyncio.sleep(backoff)

    async def _tick(self) -> None:
        data = await self.source.draft_detail()
        new_picks = self.state.apply_draft_detail(data)
        self.state.last_successful_poll = datetime.now(timezone.utc)
        self.state.consecutive_errors = 0
        self.state.last_error = None
        self.state.auth_error = False

        if new_picks:
            for pick in new_picks:
                player = self.state.player(pick.player_id)
                log.info("pick %d.%02d — %s → %s (%s)", pick.round,
                         pick.overall, self.state.team_name(pick.team_id),
                         player.name, player.position)
            await self._maybe_refresh_pool(force=False)

        self._recompute()

    async def _maybe_refresh_pool(self, force: bool) -> None:
        """Refresh projections roughly every 60s (§5.2). Failure here is survivable."""
        now = asyncio.get_event_loop().time()
        if not force and now - self._last_pool_refresh < POOL_REFRESH_SECONDS:
            return
        self._last_pool_refresh = now
        try:
            self.state.load_players(await self.source.player_pool())
        except ESPNError as exc:
            log.warning("player pool refresh failed (%s) — keeping last known pool", exc)

    def _recompute(self) -> None:
        """Recompute recommendations and push a full snapshot (§6 step 3)."""
        try:
            recs, best = recommend(self.state)
        except Exception:
            # A broken recommender must not take the draft board down (§1).
            log.exception("recommender failed; serving picks without recommendations")
            recs, best = [], {}
        self.snapshot = self.state.snapshot(recs, best)
        self.bus.publish(self.snapshot)

    def _record_error(self, exc: Exception) -> None:
        self.state.consecutive_errors += 1
        self.state.last_error = f"{type(exc).__name__}: {exc}"
        if self.state.consecutive_errors >= UNHEALTHY_AFTER_ERRORS:
            log.error("connection unhealthy after %d consecutive failures",
                      self.state.consecutive_errors)


def make_source(cfg: Config) -> Source:
    return ReplaySource(cfg.replay_dir) if cfg.replay_dir else LiveSource(cfg)
