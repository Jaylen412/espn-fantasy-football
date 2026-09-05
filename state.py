"""In-memory DraftState — the single source of truth. See MD/REQUIREMENTS.md §6."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from config import FLEX_ELIGIBLE, POSITION_BY_ID, Config

log = logging.getLogger("state")

DRAFT_LOG = Path("draft_log.jsonl")

PRO_TEAM_BY_ID = {
    0: "FA", 1: "ATL", 2: "BUF", 3: "CHI", 4: "CIN", 5: "CLE", 6: "DAL", 7: "DEN",
    8: "DET", 9: "GB", 10: "TEN", 11: "IND", 12: "KC", 13: "LV", 14: "LAR", 15: "MIA",
    16: "MIN", 17: "NE", 18: "NO", 19: "NYG", 20: "NYJ", 21: "PHI", 22: "ARI",
    23: "PIT", 24: "LAC", 25: "SF", 26: "SEA", 27: "TB", 28: "WSH", 29: "CAR",
    30: "JAX", 33: "BAL", 34: "HOU",
}


@dataclass
class Player:
    id: int
    name: str
    position: str
    pro_team: str = "FA"
    rank: int = 9999
    auction_value: float = 0.0
    projected_points: float = 0.0

    @classmethod
    def unknown(cls, player_id: int) -> "Player":
        """Never lose a pick — an unmapped id still renders (§9)."""
        return cls(id=player_id, name=f"Unknown player {player_id}", position="?")


@dataclass
class Pick:
    overall: int
    round: int
    team_id: int
    player_id: int
    keeper: bool = False
    auto: bool = False


# ---- pure helpers (unit-tested, §11.3) -----------------------------------------


def build_snake_order(pick_order: list[int], rounds: int) -> list[int]:
    """Team id for every overall pick, index 0 == overall pick 1.

    Snake: odd rounds follow pick_order, even rounds reverse it.
    """
    order: list[int] = []
    for rnd in range(rounds):
        row = pick_order if rnd % 2 == 0 else list(reversed(pick_order))
        order.extend(row)
    return order


def picks_until_next(snake: list[int], current_overall: int, team_id: int) -> tuple[int | None, int]:
    """(overall number of team_id's next pick, picks away from current_overall).

    `current_overall` is the pick now on the clock. If that pick belongs to
    team_id, the answer is that pick with distance 0. Returns (None, -1) when the
    team has no picks left.
    """
    for overall in range(max(current_overall, 1), len(snake) + 1):
        if snake[overall - 1] == team_id:
            return overall, overall - current_overall
    return None, -1


def assign_slot(position: str, filled: dict[str, int], slots: dict[str, int]) -> str:
    """Which roster slot a newly drafted player occupies. Starters first, then FLEX, then bench."""
    if filled.get(position, 0) < slots.get(position, 0):
        return position
    if position in FLEX_ELIGIBLE and filled.get("FLEX", 0) < slots.get("FLEX", 0):
        return "FLEX"
    return "BENCH"


# ---- state ---------------------------------------------------------------------


@dataclass
class DraftState:
    cfg: Config
    teams: dict[int, dict[str, Any]] = field(default_factory=dict)
    players: dict[int, Player] = field(default_factory=dict)
    picks: dict[int, Pick] = field(default_factory=dict)   # keyed by overallPickNumber
    snake: list[int] = field(default_factory=list)
    rounds: int = 16
    draft_status: str = "PRE_DRAFT"
    last_successful_poll: datetime | None = None
    consecutive_errors: int = 0
    last_error: str | None = None
    auth_error: bool = False

    # ---- ingestion ----

    def load_league(self, data: dict[str, Any]) -> None:
        """Teams, settings and snake order from a stacked mTeam+mSettings call (§5.2)."""
        for team in data.get("teams", []) or []:
            tid = team.get("id")
            if tid is None:
                continue
            name = (team.get("name") or " ".join(
                filter(None, [team.get("location"), team.get("nickname")])
            ) or f"Team {tid}").strip()
            self.teams[tid] = {"id": tid, "name": name, "owner": _owner_of(team, data)}

        settings = data.get("settings") or {}
        draft_settings = settings.get("draftSettings") or {}
        roster_settings = settings.get("rosterSettings") or {}

        pick_order = [t for t in (draft_settings.get("pickOrder") or []) if t in self.teams]
        if not pick_order:
            pick_order = sorted(self.teams) or list(range(1, self.cfg.team_count + 1))

        slot_counts = roster_settings.get("lineupSlotCounts") or {}
        total_slots = sum(int(v) for v in slot_counts.values()) if slot_counts else 0
        self.rounds = total_slots or sum(self.cfg.roster_slots.values()) or 16
        self.snake = build_snake_order(pick_order, self.rounds)

        if draft_settings.get("type") not in (None, "SNAKE"):
            log.warning("draft type is %s; this app models snake drafts only (§1)",
                        draft_settings.get("type"))

    def load_players(self, data: dict[str, Any]) -> None:
        """Player pool from kona_player_info (§5.4). Merges — never drops known players."""
        entries = data.get("players")
        if entries is None:
            entries = data.get("playerPool") or []
        loaded = 0
        for entry in entries or []:
            player = _parse_player(entry, self.cfg)
            if player is not None:
                self.players[player.id] = player
                loaded += 1
        log.info("player pool: %d entries, %d known players total", loaded, len(self.players))

    def apply_draft_detail(self, data: dict[str, Any]) -> list[Pick]:
        """Diff incoming picks against known ones. Returns only the new picks (§6)."""
        detail = data.get("draftDetail") or {}
        if detail.get("inProgress"):
            self.draft_status = "IN_PROGRESS"
        elif detail.get("drafted"):
            self.draft_status = "COMPLETE"
        else:
            self.draft_status = "PRE_DRAFT"

        new: list[Pick] = []
        for raw in detail.get("picks") or []:
            overall = raw.get("overallPickNumber")
            if overall is None or overall in self.picks:
                continue
            pick = Pick(
                overall=overall,
                round=raw.get("roundId") or ((overall - 1) // max(self.team_count, 1) + 1),
                team_id=raw.get("teamId", 0),
                player_id=raw.get("playerId", 0),
                keeper=bool(raw.get("keeper")),
                auto=bool(raw.get("autoDraftTypeId")),
            )
            self.picks[overall] = pick
            new.append(pick)

        for pick in new:
            self._log_pick(pick)
        return new

    # ---- derived ----

    @property
    def team_count(self) -> int:
        return len(self.teams) or self.cfg.team_count

    @property
    def drafted_ids(self) -> set[int]:
        return {p.player_id for p in self.picks.values()}

    def available_players(self) -> list[Player]:
        """`available = all_players - drafted_player_ids`.

        Computed locally on purpose — ESPN's filterStatus lags during a live
        draft and must never be trusted for this (§5.4).
        """
        drafted = self.drafted_ids
        pool = [p for pid, p in self.players.items() if pid not in drafted]
        pool.sort(key=lambda p: (p.rank, -p.projected_points))
        return pool

    @property
    def current_overall(self) -> int:
        """The pick now on the clock — one past the highest pick made."""
        return (max(self.picks) if self.picks else 0) + 1

    def team_name(self, team_id: int) -> str:
        return (self.teams.get(team_id) or {}).get("name") or f"Team {team_id}"

    def player(self, player_id: int) -> Player:
        return self.players.get(player_id) or Player.unknown(player_id)

    def roster(self, team_id: int) -> dict[str, list[dict[str, Any]]]:
        """Drafted players slotted into the league's roster shape."""
        slots = self.cfg.roster_slots
        out: dict[str, list[dict[str, Any]]] = {k: [] for k in slots}
        out.setdefault("BENCH", [])
        filled: dict[str, int] = {}
        for overall in sorted(self.picks):
            pick = self.picks[overall]
            if pick.team_id != team_id:
                continue
            player = self.player(pick.player_id)
            slot = assign_slot(player.position, filled, slots)
            filled[slot] = filled.get(slot, 0) + 1
            out.setdefault(slot, []).append(
                {"name": player.name, "position": player.position,
                 "pro_team": player.pro_team, "overall": overall}
            )
        return out

    def unfilled_starters(self, team_id: int) -> dict[str, int]:
        """Starting slots still open, by slot name — drives roster need (§7)."""
        roster = self.roster(team_id)
        return {
            slot: max(0, count - len(roster.get(slot, [])))
            for slot, count in self.cfg.starters.items()
        }

    def my_next_pick(self) -> tuple[int | None, int]:
        return picks_until_next(self.snake, self.current_overall, self.cfg.my_team_id)

    def is_healthy(self, now: datetime | None = None, stale_after: float = 15.0) -> bool:
        """Green only when a poll succeeded recently. Silent staleness is the worst
        failure mode (§8)."""
        if self.last_successful_poll is None or self.auth_error:
            return False
        now = now or datetime.now(timezone.utc)
        return (now - self.last_successful_poll).total_seconds() <= stale_after

    # ---- output ----

    def snapshot(self, recommendations: list[dict[str, Any]] | None = None,
                 best_by_position: dict[str, list[dict[str, Any]]] | None = None) -> dict[str, Any]:
        """The full payload pushed over SSE. Full snapshots, not deltas (§6)."""
        overall = self.current_overall
        on_clock_team = self.snake[overall - 1] if 0 < overall <= len(self.snake) else None
        next_overall, picks_away = self.my_next_pick()

        current = None
        if on_clock_team is not None and self.draft_status != "COMPLETE":
            current = {
                "overall": overall,
                "round": (overall - 1) // max(self.team_count, 1) + 1,
                "team_id": on_clock_team,
                "team_name": self.team_name(on_clock_team),
                "is_me": on_clock_team == self.cfg.my_team_id,
            }

        return {
            "draft_status": self.draft_status,
            "current_pick": current,
            "next_my_pick": (
                {"overall": next_overall, "picks_away": picks_away}
                if next_overall is not None else None
            ),
            "picks": [
                {
                    "overall": p.overall,
                    "round": p.round,
                    "team_id": p.team_id,
                    "team_name": self.team_name(p.team_id),
                    "player_name": self.player(p.player_id).name,
                    "position": self.player(p.player_id).position,
                    "pro_team": self.player(p.player_id).pro_team,
                    "is_me": p.team_id == self.cfg.my_team_id,
                    "keeper": p.keeper,
                }
                for p in sorted(self.picks.values(), key=lambda x: -x.overall)
            ],
            "my_roster": self.roster(self.cfg.my_team_id),
            "roster_slots": self.cfg.roster_slots,
            "recommendations": recommendations or [],
            "best_available_by_position": best_by_position or {},
            "teams": [self.teams[t] for t in sorted(self.teams)],
            "total_rounds": self.rounds,
            "last_updated": (self.last_successful_poll or datetime.now(timezone.utc))
                .isoformat().replace("+00:00", "Z"),
            "connection_healthy": self.is_healthy(),
            "auth_error": self.auth_error,
            "last_error": self.last_error,
        }

    def _log_pick(self, pick: Pick) -> None:
        """Append-only log for the post-mortem (§1 non-goals)."""
        player = self.player(pick.player_id)
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "overall": pick.overall, "round": pick.round,
            "team_id": pick.team_id, "team_name": self.team_name(pick.team_id),
            "player_id": pick.player_id, "player_name": player.name,
            "position": player.position,
        }
        try:
            with DRAFT_LOG.open("a") as fh:
                fh.write(json.dumps(row) + "\n")
        except OSError:
            log.exception("could not append to %s", DRAFT_LOG)


def _owner_of(team: dict[str, Any], data: dict[str, Any]) -> str:
    """Owner display name. ESPN has moved this field around; try the known spots."""
    owners = team.get("owners") or []
    members = {m.get("id"): m for m in (data.get("members") or [])}
    for owner in owners:
        member = members.get(owner) if isinstance(owner, str) else None
        if member:
            name = " ".join(filter(None, [member.get("firstName"), member.get("lastName")]))
            return name.strip() or member.get("displayName") or str(owner)
    if isinstance(team.get("primaryOwner"), str):
        member = members.get(team["primaryOwner"])
        if member:
            return member.get("displayName") or team["primaryOwner"]
    return ""


def _parse_player(entry: dict[str, Any], cfg: Config) -> Player | None:
    """kona_player_info entry -> Player. Tolerant: unknown fields degrade, never raise."""
    raw = entry.get("player") if isinstance(entry.get("player"), dict) else entry
    if not isinstance(raw, dict):
        return None
    pid = raw.get("id") or entry.get("id")
    if pid is None:
        return None

    ranks = raw.get("draftRanksByRankType") or {}
    rank_info = ranks.get(cfg.rank_type) or ranks.get("PPR") or ranks.get("STANDARD") or {}

    return Player(
        id=int(pid),
        name=raw.get("fullName") or " ".join(
            filter(None, [raw.get("firstName"), raw.get("lastName")])
        ) or f"Player {pid}",
        position=POSITION_BY_ID.get(raw.get("defaultPositionId"), "?"),
        pro_team=PRO_TEAM_BY_ID.get(raw.get("proTeamId"), "FA"),
        rank=int(rank_info.get("rank") or 9999),
        auction_value=float(rank_info.get("auctionValue") or 0.0),
        projected_points=_projected_points(raw.get("stats") or [], cfg.season),
    )


def _projected_points(stats: Iterable[dict[str, Any]], season: int) -> float:
    """Season projection: statSourceId 1 (projected), statSplitTypeId 0 (season) [VERIFY §5.4]."""
    best = 0.0
    for stat in stats:
        if not isinstance(stat, dict):
            continue
        if stat.get("statSourceId") != 1:
            continue
        if stat.get("statSplitTypeId") not in (0, None):
            continue
        if stat.get("seasonId") not in (season, None):
            continue
        total = stat.get("appliedTotal")
        if isinstance(total, (int, float)):
            best = max(best, float(total))
    return best
