#!/usr/bin/env python3
"""Dump one real response per view into samples/ — build step 1 (§13).

Every [VERIFY] item in the spec is confirmed by reading these files. Do not write
a parser from the document alone (§5.3).
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import load_config                          # noqa: E402
from espn_client import ESPNClient, ESPNError           # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "samples"


async def main() -> int:
    cfg = load_config()
    client = ESPNClient(cfg.league_id, cfg.season, cfg.espn_s2, cfg.swid)
    OUT.mkdir(exist_ok=True)
    jobs = {
        "draft_detail_0001.json": client.draft_detail(),
        "league_setup.json": client.league_setup(),
        "player_pool.json": client.player_pool(cfg.rank_type),
    }
    rc = 0
    try:
        for name, coro in jobs.items():
            try:
                data = await coro
            except ESPNError as exc:
                print(f"  {name:<26} FAILED: {exc}")
                rc = 1
                continue
            path = OUT / name
            path.write_text(json.dumps(data, indent=2))
            print(f"  {name:<26} {path.stat().st_size / 1024:>8.0f} KB")
    finally:
        await client.aclose()

    print("\nNow confirm against MD/REQUIREMENTS.md §5.3:")
    print("  - draftDetail.picks[] field names (overallPickNumber, roundId, teamId, playerId)")
    print("  - defaultPositionId map  1=QB 2=RB 3=WR 4=TE 5=K 16=D/ST")
    print("  - lineupSlotId map       0=QB 2=RB 4=WR 6=TE 16=D/ST 17=K 20=Bench 23=FLEX")
    print("  - settings.draftSettings.pickOrder exists and lists team ids")
    return rc


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
