"""Configuration loading. See MD/REQUIREMENTS.md §4."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()

# §5.3 [VERIFY] — confirmed against samples/ before trusting.
POSITION_BY_ID = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "DST"}
SLOT_BY_ID = {
    0: "QB", 2: "RB", 4: "WR", 6: "TE", 16: "DST", 17: "K",
    20: "BENCH", 21: "IR", 23: "FLEX",
}
FLEX_ELIGIBLE = {"RB", "WR", "TE"}

# Which positions may fill a FLEX slot when we assign a drafted player to a roster slot.
SLOT_FILL_ORDER = ["QB", "RB", "WR", "TE", "DST", "K", "FLEX", "BENCH"]


def _env(key: str, default: str | None = None) -> str:
    val = os.getenv(key, default)
    if val is None:
        raise RuntimeError(f"{key} is required — copy .env.example to .env and fill it in")
    return val.strip()


def _parse_roster_slots(raw: str) -> dict[str, int]:
    """'QB:1,RB:2,...' -> {'QB': 1, 'RB': 2, ...}"""
    slots: dict[str, int] = {}
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        name, _, count = part.partition(":")
        slots[name.strip().upper()] = int(count or 0)
    return slots


@dataclass
class Config:
    league_id: int
    season: int
    espn_s2: str
    swid: str
    my_team_id: int
    scoring_format: str
    team_count: int
    roster_slots: dict[str, int] = field(default_factory=dict)
    poll_interval: float = 4.0
    log_level: str = "INFO"
    replay_dir: str | None = None

    @property
    def rank_type(self) -> str:
        """ESPN's draftRanksByRankType only knows STANDARD and PPR (§5.4)."""
        return "STANDARD" if self.scoring_format.upper() == "STANDARD" else "PPR"

    @property
    def starters(self) -> dict[str, int]:
        """Starting slots only — BENCH and IR are not starters."""
        return {k: v for k, v in self.roster_slots.items() if k not in ("BENCH", "IR") and v > 0}

    def starters_at(self, position: str) -> int:
        """Starting slots a position can occupy, including its share of FLEX."""
        base = self.starters.get(position, 0)
        if position in FLEX_ELIGIBLE:
            flex = self.starters.get("FLEX", 0)
            base += flex / len(FLEX_ELIGIBLE)
        return base


def load_config() -> Config:
    replay = os.getenv("REPLAY_DIR", "").strip() or None
    # In replay mode the league credentials are never used, so don't demand them.
    required = (lambda k, d=None: os.getenv(k, d or "0").strip()) if replay else _env
    return Config(
        league_id=int(required("ESPN_LEAGUE_ID") or 0),
        season=int(os.getenv("ESPN_SEASON", "2026")),
        espn_s2=os.getenv("ESPN_S2", "").strip(),
        swid=os.getenv("ESPN_SWID", "").strip(),
        my_team_id=int(required("MY_TEAM_ID") or 0),
        scoring_format=os.getenv("SCORING_FORMAT", "PPR").strip().upper(),
        team_count=int(os.getenv("TEAM_COUNT", "12")),
        roster_slots=_parse_roster_slots(
            os.getenv("ROSTER_SLOTS", "QB:1,RB:2,WR:2,TE:1,FLEX:1,DST:1,K:1,BENCH:7")
        ),
        poll_interval=float(os.getenv("POLL_INTERVAL_SECONDS", "4")),
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
        replay_dir=replay,
    )
