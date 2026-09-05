"""Failure handling — the poller must never die (§9), and staleness must surface (§8)."""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from espn_client import AuthError, ESPNError, RateLimited, with_backoff  # noqa: E402
from poller import Broadcaster, Poller, ReplaySource  # noqa: E402
from state import DraftState  # noqa: E402
from tests.test_state import cfg  # noqa: E402

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


class FlakySource:
    """Serves real fixtures but fails on demand."""

    def __init__(self, failures: list[Exception | None]):
        self.failures = failures
        self.calls = 0
        self.frame = 0

    async def league_setup(self):
        return json.loads((SAMPLES / "league_setup.json").read_text())

    async def player_pool(self):
        return json.loads((SAMPLES / "player_pool.json").read_text())

    async def draft_detail(self):
        exc = self.failures[self.calls] if self.calls < len(self.failures) else None
        self.calls += 1
        if exc:
            raise exc
        self.frame = min(self.frame + 1, 40)
        return json.loads((SAMPLES / f"draft_detail_{self.frame:04d}.json").read_text())

    async def aclose(self):
        return None


def make_poller(source) -> Poller:
    c = cfg()
    c.poll_interval = 0.01
    st = DraftState(cfg=c)
    p = Poller(c, source, st, Broadcaster())
    p.backoff_base = 0.01      # §9 backoff is real; just not on wall-clock seconds here
    p.backoff_cap = 0.05
    return p


@pytest.mark.asyncio
async def test_poller_survives_transient_failures_and_recovers():
    """Server survives a transient ESPN outage and recovers on its own (§12)."""
    source = FlakySource([None, ESPNError("boom"), ESPNError("boom"), RateLimited("429")])
    p = make_poller(source)
    await p.startup()
    p.start()
    await asyncio.sleep(0.5)

    assert not p._task.done(), "poller died on a transient failure"
    assert source.calls > 4, "poller stopped fetching after errors"
    assert p.state.consecutive_errors == 0, "did not recover after the outage cleared"
    assert p.snapshot["picks"], "stopped serving known state during the outage"
    await p.stop()


@pytest.mark.asyncio
async def test_last_known_state_is_still_served_while_failing():
    source = FlakySource([None, None, None] + [ESPNError("down")] * 50)
    p = make_poller(source)
    await p.startup()
    p.start()
    await asyncio.sleep(0.2)
    picks_before = len(p.snapshot["picks"])
    await asyncio.sleep(0.3)

    assert not p._task.done()
    assert len(p.snapshot["picks"]) == picks_before   # frozen, not lost
    assert p.state.consecutive_errors >= 1
    await p.stop()


@pytest.mark.asyncio
async def test_expired_cookies_are_surfaced_not_retried_silently():
    """401 sets a flag the dashboard renders as a banner (§9)."""
    source = FlakySource([AuthError("401")] * 10)
    p = make_poller(source)
    p.backoff_cap = 30.0        # the real cap: a 401 must not be retried in a tight loop
    await p.startup()
    p.start()
    await asyncio.sleep(0.1)

    assert p.state.auth_error is True
    assert p.snapshot["auth_error"] is True
    assert p.snapshot["connection_healthy"] is False
    assert source.calls == 1, "hammered ESPN after a 401 instead of backing off"
    await p.stop()


@pytest.mark.asyncio
async def test_startup_without_a_player_pool_still_serves_picks():
    """Degrade to showing raw data rather than crashing (§1)."""
    class NoPool(FlakySource):
        async def player_pool(self):
            raise ESPNError("pool unavailable")

    p = make_poller(NoPool([None]))
    await p.startup()
    assert p.state.players == {}
    assert p.snapshot["recommendations"] == []
    assert p.state.snake, "league shape still loaded"
    await p.stop()


@pytest.mark.asyncio
async def test_backoff_never_retries_an_auth_error():
    calls = 0

    async def always_401():
        nonlocal calls
        calls += 1
        raise AuthError("401")

    with pytest.raises(AuthError):
        await with_backoff(always_401, attempts=3, base=0.01)
    assert calls == 1


@pytest.mark.asyncio
async def test_backoff_retries_transient_errors_then_gives_up():
    calls = 0

    async def flaky():
        nonlocal calls
        calls += 1
        raise ESPNError("5xx")

    with pytest.raises(ESPNError):
        await with_backoff(flaky, attempts=3, base=0.01)
    assert calls == 3


def test_stale_state_reports_unhealthy():
    """Green only when a poll landed within 15s (§8)."""
    st = DraftState(cfg=cfg())
    st.last_successful_poll = datetime.now(timezone.utc) - timedelta(seconds=20)
    assert st.is_healthy() is False
    st.last_successful_poll = datetime.now(timezone.utc) - timedelta(seconds=5)
    assert st.is_healthy() is True


@pytest.mark.asyncio
async def test_broadcaster_drops_frames_for_a_slow_client_without_blocking():
    bus = Broadcaster()
    q = bus.subscribe()
    for i in range(50):                 # far past the queue's maxsize
        bus.publish({"n": i})
    assert q.qsize() <= 4               # bounded; the poller was never blocked
    bus.unsubscribe(q)
    assert bus.count == 0


@pytest.mark.asyncio
async def test_replay_source_advances_one_frame_per_poll():
    source = ReplaySource(SAMPLES)
    first = await source.draft_detail()
    second = await source.draft_detail()
    assert len(second["draftDetail"]["picks"]) > len(first["draftDetail"]["picks"])
