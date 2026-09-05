"""Thin HTTP wrapper around ESPN's undocumented v3 fantasy API. See MD/REQUIREMENTS.md §5.

Everything this module parses is [VERIFY] until confirmed against a real response —
run `python scripts/dump_samples.py` to write live payloads into samples/.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("espn")

BASE = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{season}/segments/0/leagues/{league_id}"

# A default Python UA sometimes gets a non-JSON response back (§5.1).
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Slots the player pool query cares about: QB, RB, WR, TE, D/ST, K, FLEX (§5.4).
POOL_SLOT_IDS = [0, 2, 4, 6, 16, 17, 23]
POOL_LIMIT = 400  # above 400 sometimes errors (§5.4)

ERRORS_DIR = Path("errors")


class ESPNError(Exception):
    """Base for every failure this client raises."""


class AuthError(ESPNError):
    """401 — cookies expired. Do not retry silently (§9)."""


class RateLimited(ESPNError):
    """429 — back off (§9)."""


class ShapeError(ESPNError):
    """Response parsed but did not look like what we expected (§9)."""


def _dump_bad_response(tag: str, body: str) -> None:
    """Keep the raw body so a mid-draft shape change is diagnosable (§9)."""
    try:
        ERRORS_DIR.mkdir(exist_ok=True)
        path = ERRORS_DIR / f"{tag}-{int(time.time())}.txt"
        path.write_text(body[:200_000])
        log.error("wrote unexpected response body to %s", path)
    except Exception:  # never let diagnostics take the process down
        log.exception("could not write error dump")


class ESPNClient:
    def __init__(self, league_id: int, season: int, espn_s2: str = "", swid: str = "",
                 timeout: float = 10.0):
        self.league_id = league_id
        self.season = season
        self.url = BASE.format(season=season, league_id=league_id)
        headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
        cookies = {}
        if espn_s2 and swid:
            cookies = {"espn_s2": espn_s2, "SWID": swid}
        self._client = httpx.AsyncClient(
            headers=headers, cookies=cookies, timeout=timeout,
            limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, views: list[str], extra_headers: dict[str, str] | None = None,
                   tag: str = "response") -> dict[str, Any]:
        params = [("view", v) for v in views]
        try:
            resp = await self._client.get(self.url, params=params, headers=extra_headers or {})
        except httpx.TimeoutException as exc:
            raise ESPNError(f"timeout fetching {views}") from exc
        except httpx.HTTPError as exc:
            raise ESPNError(f"transport error fetching {views}: {exc}") from exc

        if resp.status_code == 401:
            raise AuthError("ESPN returned 401 — espn_s2 / SWID are expired or wrong")
        if resp.status_code == 429:
            raise RateLimited("ESPN returned 429")
        if resp.status_code >= 400:
            raise ESPNError(f"ESPN returned {resp.status_code} for {views}")

        try:
            data = resp.json()
        except (json.JSONDecodeError, ValueError) as exc:
            _dump_bad_response(tag, resp.text)
            raise ShapeError(f"non-JSON response for {views}") from exc

        # Some league endpoints return a single-element list rather than an object.
        if isinstance(data, list):
            data = data[0] if data else {}
        if not isinstance(data, dict):
            _dump_bad_response(tag, resp.text)
            raise ShapeError(f"unexpected top-level type {type(data).__name__} for {views}")
        return data

    # ---- views ----------------------------------------------------------------

    async def draft_detail(self) -> dict[str, Any]:
        """The live pick feed. Polled every POLL_INTERVAL_SECONDS (§5.2)."""
        return await self._get(["mDraftDetail"], tag="draft_detail")

    async def league_setup(self) -> dict[str, Any]:
        """Teams, settings and the initial draft state in one stacked call (§5.1)."""
        return await self._get(
            ["mTeam", "mSettings", "mDraftDetail"], tag="league_setup"
        )

    async def player_pool(self, rank_type: str = "PPR", limit: int = POOL_LIMIT) -> dict[str, Any]:
        """Full player pool with ranks and league-adjusted projections (§5.4).

        `filterStatus` is included because ESPN wants it, but its answer is never
        trusted for availability — that is computed locally (§5.4).
        """
        fantasy_filter = {
            "players": {
                "filterStatus": {"value": ["FREEAGENT", "WAIVERS", "ONTEAM"]},
                "filterSlotIds": {"value": POOL_SLOT_IDS},
                "sortDraftRanks": {
                    "sortPriority": 100, "sortAsc": True, "value": rank_type,
                },
                "limit": min(limit, POOL_LIMIT),
            }
        }
        return await self._get(
            ["kona_player_info"],
            extra_headers={"X-Fantasy-Filter": json.dumps(fantasy_filter)},
            tag="player_pool",
        )


async def with_backoff(fn, *, attempts: int = 3, base: float = 1.0, cap: float = 30.0):
    """Retry a coroutine factory with exponential backoff, capped at 30s (§9).

    AuthError is never retried — expired cookies do not fix themselves.
    """
    delay = base
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return await fn()
        except AuthError:
            raise
        except ESPNError as exc:
            last = exc
            if attempt == attempts - 1:
                break
            log.warning("attempt %d failed (%s); retrying in %.1fs", attempt + 1, exc, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, cap)
    assert last is not None
    raise last
