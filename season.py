"""In-season state — rosters, weekly actuals and standings for the trade analyzer.

Kept apart from DraftState on purpose: draft state is rebuilt from `draft.picks`
and goes stale after the first trade or waiver claim, while this is re-read from
ESPN's live rosters. Parsers follow the real payloads dumped by
`scripts/dump_season.py` (samples/live/season_*.json), not the spec.

Shapes confirmed against the dump:
  - `teams[].roster.entries[]` carries `playerId`, `lineupSlotId`, `injuryStatus`.
  - Weekly stats are `stats[]` rows with `statSplitTypeId == 1`;
    `statSourceId` 0 = actual, 1 = projected, `scoringPeriodId` = the week.
  - A bye week has no row at all. Future weeks (17, 18 here) carry an actual of
    0.0, so only periods *before* the current one count as played.
"""
from __future__ import annotations

import logging
import statistics
import time
from dataclasses import dataclass, field
from typing import Any

from config import POSITION_BY_ID, SLOT_BY_ID
from state import PRO_TEAM_BY_ID

log = logging.getLogger("season")

CACHE_SECONDS = 60.0
WEEKLY_SPLIT = 1
ACTUAL, PROJECTED = 0, 1
INACTIVE_STATUSES = {"OUT", "INJURY_RESERVE", "SUSPENSION"}


@dataclass
class SeasonPlayer:
    id: int
    name: str
    position: str
    pro_team: str = "FA"
    injury: str = "ACTIVE"
    slot: str = "BENCH"
    team_id: int = 0
    weekly: dict[int, float] = field(default_factory=dict)  # played weeks only
    proj_week: float = 0.0                                   # this week's projection
    bye_week: int = 0                                        # his NFL team's bye; 0 = unknown

    @property
    def games(self) -> int:
        return len(self.weekly)

    @property
    def total(self) -> float:
        return sum(self.weekly.values())

    @property
    def avg(self) -> float:
        return self.total / self.games if self.games else 0.0

    @property
    def recent_avg(self) -> float:
        """Mean of the last three games played; falls back to the season mean."""
        weeks = sorted(self.weekly)[-3:]
        return sum(self.weekly[w] for w in weeks) / len(weeks) if weeks else 0.0

    @property
    def stdev(self) -> float:
        return statistics.pstdev(self.weekly.values()) if self.games > 1 else 0.0

    @property
    def is_inactive(self) -> bool:
        return self.injury in INACTIVE_STATUSES


@dataclass
class SeasonTeam:
    id: int
    name: str
    wins: int = 0
    losses: int = 0
    ties: int = 0
    points_for: float = 0.0
    points_against: float = 0.0
    player_ids: list[int] = field(default_factory=list)


@dataclass
class SeasonState:
    period: int = 0                                  # current scoring period (not yet complete)
    final_period: int = 0
    teams: dict[int, SeasonTeam] = field(default_factory=dict)
    players: dict[int, SeasonPlayer] = field(default_factory=dict)
    weekly_scores: dict[int, dict[int, float]] = field(default_factory=dict)  # team -> week -> pts
    schedule: list[tuple[int, int, int]] = field(default_factory=list)        # remaining (week, home, away)
    playoff_teams: int = 0                                                    # 0 = ESPN did not say
    regular_weeks: int = 0
    byes: dict[str, int] = field(default_factory=dict)                        # NFL team abbrev -> bye week
    loaded_at: float = 0.0

    @property
    def weeks_played(self) -> int:
        return max(self.period - 1, 0)

    @property
    def weeks_left(self) -> int:
        return max(self.final_period - self.period + 1, 0)

    @property
    def weeks_ahead(self) -> list[int]:
        """Every scoring week still to play, this week included."""
        return list(range(self.period, self.final_period + 1))

    def roster(self, team_id: int) -> list[SeasonPlayer]:
        team = self.teams.get(team_id)
        return [self.players[p] for p in team.player_ids if p in self.players] if team else []

    def snapshot(self) -> dict[str, Any]:
        return {
            "period": self.period,
            "final_period": self.final_period,
            "teams": [
                {"id": t.id, "name": t.name, "record": f"{t.wins}-{t.losses}"
                 + (f"-{t.ties}" if t.ties else ""),
                 "points_for": round(t.points_for, 1),
                 "points_against": round(t.points_against, 1)}
                for t in sorted(self.teams.values(), key=lambda t: (-t.wins, -t.points_for))
            ],
        }


# ---- parsing -------------------------------------------------------------------


def _weekly_stats(stats: list[dict], current_period: int) -> tuple[dict[int, float], float]:
    """(actual points per *completed* week, this week's projection)."""
    weekly: dict[int, float] = {}
    proj = 0.0
    for row in stats or []:
        if row.get("statSplitTypeId") != WEEKLY_SPLIT:
            continue
        week, total = row.get("scoringPeriodId"), row.get("appliedTotal")
        if not isinstance(week, int) or not isinstance(total, (int, float)):
            continue
        if row.get("statSourceId") == ACTUAL and week < current_period:
            weekly[week] = float(total)
        elif row.get("statSourceId") == PROJECTED and week == current_period:
            proj = float(total)
    return weekly, proj


def _player(entry: dict, team_id: int, current_period: int) -> SeasonPlayer | None:
    raw = (entry.get("playerPoolEntry") or {}).get("player") or {}
    pid = entry.get("playerId") or raw.get("id")
    if not isinstance(pid, int):
        return None
    weekly, proj = _weekly_stats(raw.get("stats", []), current_period)
    return SeasonPlayer(
        id=pid,
        name=raw.get("fullName") or f"Unknown player {pid}",   # never drop a player (§9)
        position=POSITION_BY_ID.get(raw.get("defaultPositionId"), "?"),
        pro_team=PRO_TEAM_BY_ID.get(raw.get("proTeamId"), "FA"),
        injury=entry.get("injuryStatus") or raw.get("injuryStatus") or "ACTIVE",
        slot=SLOT_BY_ID.get(entry.get("lineupSlotId"), "BENCH"),
        team_id=team_id,
        weekly=weekly,
        proj_week=proj,
    )


def parse_rosters(data: dict[str, Any]) -> SeasonState:
    status = data.get("status") or {}
    period = int(data.get("scoringPeriodId") or status.get("latestScoringPeriod") or 1)
    st = SeasonState(period=period, final_period=int(status.get("finalScoringPeriod") or 17))
    for team in data.get("teams", []):
        tid = team.get("id")
        if not isinstance(tid, int):
            continue
        rec = (team.get("record") or {}).get("overall") or team.get("record") or {}
        name = team.get("name") or " ".join(
            filter(None, [team.get("location"), team.get("nickname")])) or f"Team {tid}"
        st_team = SeasonTeam(
            id=tid, name=name,
            wins=int(rec.get("wins", 0)), losses=int(rec.get("losses", 0)),
            ties=int(rec.get("ties", 0)),
            points_for=float(rec.get("pointsFor", 0.0)),
            points_against=float(rec.get("pointsAgainst", 0.0)),
        )
        for entry in (team.get("roster") or {}).get("entries", []):
            p = _player(entry, tid, period)
            if p:
                st.players[p.id] = p
                st_team.player_ids.append(p.id)
        st.teams[tid] = st_team
    return st


def apply_player_weeks(st: SeasonState, data: dict[str, Any]) -> None:
    """Overlay the full weekly history from a kona_playercard dump.

    The roster payload only carries last week's actual; this fills in every
    completed week so averages, trend and volatility are real.
    """
    for row in data.get("players", []):
        raw = row.get("player") or {}
        p = st.players.get(raw.get("id") or row.get("id"))
        if not p:
            continue
        weekly, proj = _weekly_stats(raw.get("stats", []), st.period)
        if weekly:
            p.weekly = weekly
        if proj:
            p.proj_week = proj


def apply_matchups(st: SeasonState, data: dict[str, Any]) -> None:
    """Completed weeks become scores; the rest of the regular season becomes `schedule`.

    Matchup period N is scoring period N in this league (verified against the dump).
    Playoff weeks are not in the schedule view, so the schedule stops at
    `matchupPeriodCount` when ESPN reports it.
    """
    sched = (data.get("settings") or {}).get("scheduleSettings") or {}
    st.regular_weeks = int(sched.get("matchupPeriodCount") or st.regular_weeks or 0)
    st.playoff_teams = int(sched.get("playoffTeamCount") or st.playoff_teams or 0)
    st.schedule = []
    for game in data.get("schedule", []):
        week = game.get("matchupPeriodId")
        if not isinstance(week, int):
            continue
        home, away = game.get("home") or {}, game.get("away") or {}
        if week >= st.period:
            h, a = home.get("teamId"), away.get("teamId")
            if isinstance(h, int) and isinstance(a, int) and (not st.regular_weeks or week <= st.regular_weeks):
                st.schedule.append((week, h, a))
            continue
        for s in (home, away):
            tid, pts = s.get("teamId"), s.get("totalPoints")
            if isinstance(tid, int) and isinstance(pts, (int, float)) and pts:
                st.weekly_scores.setdefault(tid, {})[week] = float(pts)
    st.schedule.sort()


def parse_byes(data: dict[str, Any]) -> dict[str, int]:
    """`{abbrev: bye week}` from ESPN's proTeams. Confirmed against a live dump: every NFL team
    whose bye had passed was missing exactly that week from its players' stat rows."""
    out: dict[str, int] = {}
    for team in (data.get("settings") or {}).get("proTeams") or []:
        abbrev, week = PRO_TEAM_BY_ID.get(team.get("id")), team.get("byeWeek")
        if abbrev and isinstance(week, int) and week > 0:
            out[abbrev] = week
    return out


def apply_byes(st: SeasonState, byes: dict[str, int]) -> None:
    st.byes = dict(byes)
    for p in st.players.values():
        p.bye_week = byes.get(p.pro_team, 0)


# ---- loading -------------------------------------------------------------------


class SeasonLoader:
    """Fetches and caches a SeasonState. Never raises into a route: on failure it
    returns the last good state (or None) and records the error."""

    def __init__(self, client, ttl: float = CACHE_SECONDS):
        self.client = client
        self.ttl = ttl
        self.state: SeasonState | None = None
        self.error: str | None = None
        self._byes: dict[str, int] | None = None       # fixed for the season: fetched once

    async def get(self, force: bool = False) -> SeasonState | None:
        fresh = self.state and time.monotonic() - self.state.loaded_at < self.ttl
        if fresh and not force:
            return self.state
        try:
            st = parse_rosters(await self.client.rosters())
            try:
                apply_player_weeks(st, await self.client.player_weeks(list(st.players), st.period))
            except Exception as exc:        # weekly history is upside; rosters alone still work
                log.warning("weekly stats unavailable: %s", exc)
            try:
                apply_matchups(st, await self.client.matchups())
            except Exception as exc:
                log.warning("matchups unavailable: %s", exc)
            if self._byes is None:
                try:
                    self._byes = parse_byes(await self.client.pro_schedules())
                except Exception as exc:          # without byes every player is assumed to play every week
                    log.warning("bye weeks unavailable: %s", exc)
            if self._byes:
                apply_byes(st, self._byes)
            st.loaded_at = time.monotonic()
            self.state, self.error = st, None
        except Exception as exc:
            log.exception("season refresh failed")
            self.error = str(exc)
        return self.state
