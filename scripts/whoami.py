#!/usr/bin/env python3
"""Print every team's id, name and owner so you can read yours off into MY_TEAM_ID (§4)."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import load_config          # noqa: E402
from espn_client import ESPNClient      # noqa: E402
from state import _owner_of             # noqa: E402


async def main() -> int:
    cfg = load_config()
    client = ESPNClient(cfg.league_id, cfg.season, cfg.espn_s2, cfg.swid)
    try:
        data = await client._get(["mTeam"], tag="mteam")
    finally:
        await client.aclose()

    teams = data.get("teams") or []
    if not teams:
        print("No teams in the response — check ESPN_LEAGUE_ID and your cookies.")
        return 1

    print(f"\nLeague {cfg.league_id}, season {cfg.season}\n")
    print(f"{'ID':>4}  {'TEAM':<32} OWNER")
    print("-" * 70)
    for team in sorted(teams, key=lambda t: t.get("id", 0)):
        name = (team.get("name") or " ".join(
            filter(None, [team.get("location"), team.get("nickname")])) or "?").strip()
        print(f"{team.get('id', '?'):>4}  {name:<32} {_owner_of(team, data)}")
    print("\nPaste the ID of your team into MY_TEAM_ID in .env\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
