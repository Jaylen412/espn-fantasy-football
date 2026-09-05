#!/usr/bin/env python3
"""Generate synthetic fixtures so replay mode works before a real draft exists.

Real fixtures from `probe_draft.py --record` are always better — these exist so
the UI and recommender can be exercised end to end with no credentials at all.
"""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

OUT = Path(__file__).resolve().parent.parent / "samples"
TEAM_COUNT = 12
ROUNDS = 16
POSITIONS = [(2, "RB", 90), (3, "WR", 130), (1, "QB", 32), (4, "TE", 30), (16, "DST", 20), (5, "K", 20)]


def build() -> None:
    random.seed(7)
    OUT.mkdir(exist_ok=True)

    teams = [
        {"id": i, "location": "Team", "nickname": f"{i:02d}",
         "name": name, "owners": [f"{{OWNER-{i}}}"]}
        for i, name in enumerate([
            "Gridiron Gang", "Purple Reign", "Cleats & Cleavage", "Sunday Scaries",
            "The Waiver Wire", "End Zone Militia", "Blitz Brigade", "Hail Mary Inc",
            "Fourth & Long", "Play Action Heroes", "Red Zone Rats", "Pylon Patrol",
        ], start=1)
    ]
    members = [{"id": f"{{OWNER-{i}}}", "firstName": "Owner", "lastName": str(i),
                "displayName": f"owner{i}"} for i in range(1, TEAM_COUNT + 1)]

    pick_order = list(range(1, TEAM_COUNT + 1))
    random.shuffle(pick_order)

    players, pid = [], 100000
    rank = 1
    for position_id, _, count in POSITIONS:
        for n in range(count):
            pid += 1
            base = {1: 300, 2: 260, 3: 250, 4: 180, 5: 130, 16: 130}[position_id]
            proj = max(20.0, base - n * random.uniform(2.5, 6.0))
            players.append({
                "id": pid,
                "player": {
                    "id": pid,
                    "fullName": f"{POSITIONS[[p[0] for p in POSITIONS].index(position_id)][1]} Player {n+1}",
                    "defaultPositionId": position_id,
                    "proTeamId": random.choice([1, 2, 6, 12, 21, 25, 33, 34]),
                    "draftRanksByRankType": {
                        "PPR": {"rank": rank, "auctionValue": max(1, int(proj / 6))},
                        "STANDARD": {"rank": rank, "auctionValue": max(1, int(proj / 6))},
                    },
                    "stats": [{"statSourceId": 1, "statSplitTypeId": 0,
                               "seasonId": 2026, "appliedTotal": round(proj, 1)}],
                },
            })
            rank += 1
    players.sort(key=lambda e: -e["player"]["stats"][0]["appliedTotal"])
    for i, entry in enumerate(players, start=1):
        for rt in entry["player"]["draftRanksByRankType"].values():
            rt["rank"] = i

    setup = {
        "teams": teams,
        "members": members,
        "settings": {
            "draftSettings": {"type": "SNAKE", "pickOrder": pick_order},
            "rosterSettings": {"lineupSlotCounts": {
                "0": 1, "2": 2, "4": 2, "6": 1, "23": 1, "16": 1, "17": 1, "20": 7}},
        },
        "draftDetail": {"drafted": False, "inProgress": True, "picks": []},
    }
    (OUT / "league_setup.json").write_text(json.dumps(setup, indent=2))
    (OUT / "player_pool.json").write_text(json.dumps({"players": players}, indent=2))

    # One frame per pick, so replay advances the draft one pick per poll.
    snake = []
    for rnd in range(ROUNDS):
        snake.extend(pick_order if rnd % 2 == 0 else list(reversed(pick_order)))

    taken: set[int] = set()
    picks: list[dict] = []
    for old in OUT.glob("draft_detail_*.json"):
        old.unlink()
    for overall, team_id in enumerate(snake[:40], start=1):
        pool = [e for e in players if e["id"] not in taken]
        chosen = random.choice(pool[:6])          # roughly-rational drafting
        taken.add(chosen["id"])
        picks.append({
            "id": overall, "overallPickNumber": overall,
            "roundId": (overall - 1) // TEAM_COUNT + 1,
            "roundPickNumber": (overall - 1) % TEAM_COUNT + 1,
            "teamId": team_id, "playerId": chosen["id"],
            "keeper": False, "autoDraftTypeId": 0,
        })
        frame = {"draftDetail": {"drafted": False, "inProgress": True,
                                 "picks": [dict(p) for p in picks]}}
        (OUT / f"draft_detail_{overall:04d}.json").write_text(json.dumps(frame))

    my_team = snake[3]
    print(f"wrote fixtures to {OUT}: {len(players)} players, {len(picks)} pick frames")
    print(f"set MY_TEAM_ID={my_team} (picks 4th in round 1) to watch your own turns")


if __name__ == "__main__":
    build()
