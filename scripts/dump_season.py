#!/usr/bin/env python3
"""Dump the in-season views (rosters, matchups, standings, weekly player stats) into samples/live/.

Trade analysis parsers are written from these files, not from the doc (CLAUDE.md:
"verify shapes against live responses"). Kept apart from dump_samples.py so a
season dump can never touch the draft samples. Read-only GETs.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import load_config                          # noqa: E402
from espn_client import ESPNClient, ESPNError           # noqa: E402

DEFAULT_OUT = Path(__file__).resolve().parent.parent / "samples" / "live"


def _roster_player_ids(rosters: dict) -> list[int]:
    ids: list[int] = []
    for team in rosters.get("teams", []):
        for entry in team.get("roster", {}).get("entries", []):
            pid = entry.get("playerId")
            if isinstance(pid, int):
                ids.append(pid)
    return ids


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    out = args.out
    out.mkdir(parents=True, exist_ok=True)

    cfg = load_config()
    client = ESPNClient(cfg.league_id, cfg.season, cfg.espn_s2, cfg.swid)
    rc = 0

    async def save(name: str, coro) -> dict | None:
        nonlocal rc
        try:
            data = await coro
        except ESPNError as exc:
            print(f"  {name:<26} FAILED: {exc}")
            rc = 1
            return None
        path = out / name
        path.write_text(json.dumps(data, indent=2))
        print(f"  {name:<26} {path.stat().st_size / 1024:>8.0f} KB")
        return data

    try:
        rosters = await save("season_rosters.json", client.rosters())
        await save("season_matchups.json", client.matchups())
        await save("season_standings.json", client.standings())
        if rosters:
            period = rosters.get("scoringPeriodId") or 1
            ids = _roster_player_ids(rosters)
            print(f"\n  {len(ids)} rostered players, scoring period {period}")
            if ids:
                await save("season_player_weeks.json", client.player_weeks(ids, period))
    finally:
        await client.aclose()

    print("\nConfirm before writing parsers:")
    print("  - teams[].roster.entries[]: playerId, lineupSlotId, playerPoolEntry.player.injuryStatus")
    print("  - schedule[]: matchupPeriodId, home/away teamId + totalPoints")
    print("  - player stats[]: scoringPeriodId, statSourceId (0 actual / 1 projected), appliedTotal")
    return rc


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
