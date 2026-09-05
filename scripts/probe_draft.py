#!/usr/bin/env python3
"""Polls mDraftDetail once a second and timestamps every new pick (§5.5).

This settles the highest-risk open question: how far behind ESPN's REST replica
runs versus the draft room UI. Run it against the Mock Draft Lobby and eyeball
the delay. Optionally records each changed response into samples/ for replay.

  python scripts/probe_draft.py --record samples/
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import load_config                                   # noqa: E402
from espn_client import AuthError, ESPNClient, ESPNError         # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--record", metavar="DIR", help="write each changed response here")
    args = parser.parse_args()

    cfg = load_config()
    client = ESPNClient(cfg.league_id, cfg.season, cfg.espn_s2, cfg.swid)
    record_dir = Path(args.record) if args.record else None
    if record_dir:
        record_dir.mkdir(parents=True, exist_ok=True)

    seen: set[int] = set()
    frame = 0
    print(f"polling mDraftDetail every {args.interval}s — Ctrl-C to stop\n")
    try:
        while True:
            t0 = time.perf_counter()
            try:
                data = await client.draft_detail()
            except AuthError as exc:
                print(f"!! {exc}")
                return 1
            except ESPNError as exc:
                print(f"   fetch failed: {exc}")
                await asyncio.sleep(args.interval)
                continue

            latency_ms = (time.perf_counter() - t0) * 1000
            picks = (data.get("draftDetail") or {}).get("picks") or []
            new = [p for p in picks if p.get("overallPickNumber") not in seen]
            for pick in sorted(new, key=lambda p: p.get("overallPickNumber", 0)):
                overall = pick.get("overallPickNumber")
                seen.add(overall)
                print(f"{datetime.now().strftime('%H:%M:%S.%f')[:-3]}  "
                      f"pick #{overall:<3} team {pick.get('teamId'):<3} "
                      f"player {pick.get('playerId'):<9} ({latency_ms:.0f}ms fetch)")

            if new and record_dir:
                frame += 1
                (record_dir / f"draft_detail_{frame:04d}.json").write_text(json.dumps(data))

            await asyncio.sleep(args.interval)
    except KeyboardInterrupt:
        print(f"\nstopped — {len(seen)} picks observed"
              + (f", {frame} frames recorded to {record_dir}" if record_dir else ""))
        return 0
    finally:
        await client.aclose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
